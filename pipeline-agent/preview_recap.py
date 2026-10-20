"""Complete local report + paginated Slack payloads; no network calls or sends."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from presentation import display_date, display_text

# Display order, never a cap on the number of opportunities.
GROUPS = [
    ('À traiter', {'overdue_action', 'stalled_post_meeting', 'stalled_post_meeting_long', 'stalled_pre_meeting'}),
    ('À vérifier', {'needs_reply_sentiment_check', 'stalled_with_negative_reply', 'unclear', 'next_step_needs_date'}),
    ('Déjà prévu', {'action_planned'}),
    ('Activité récente', {'recent_activity_no_step_recorded'}),
]
REASONS = {
    'overdue_action': 'Une action enregistrée a atteint son échéance ; vérifier son exécution.',
    'needs_reply_sentiment_check': 'Un message entrant est plus récent que le dernier envoi retrouvé. Son intention reste à vérifier.',
    'next_step_needs_date': 'Une prochaine action est renseignée, mais sa date reste à confirmer.',
    'action_planned': 'Une action datée est déjà prévue ; en tenir compte avant toute relance.',
    'recent_activity_no_step_recorded': 'Une activité récente est enregistrée. Aucun blocage établi par ces seuls signaux.',
    'unclear': 'Le contexte disponible ne permet pas de conclure.',
}


def safe_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlparse(value)
        return value if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def grouped(deals):
    groups = {label: [] for label, _ in GROUPS}
    if any(d.get('followup_memory') for d in deals):
        groups['Déjà examiné'] = []
    for deal in deals:
        label = next((label for label, buckets in GROUPS if deal.get('bucket') in buckets), 'À vérifier')
        if deal.get('error') or (deal.get('recommendation_validation') or {}).get('status') == 'needs_review':
            label = 'À vérifier'
        elif deal.get('followup_memory', {}).get('attention') is False:
            label = 'Déjà examiné'
        groups[label].append(deal)
    return groups


def reason(deal):
    if deal.get('error'):
        return 'Collecte ou analyse en échec : ' + str(deal['error'])
    bucket = deal.get('bucket')
    if bucket in REASONS:
        return REASONS[bucket]
    days = deal.get('signals', {}).get('days_since_personal_touch')
    return f'Dernier échange personnel retrouvé : il y a {days} jours.' if days is not None else 'Contexte à examiner.'


def prepared_text(action, timezone="UTC"):
    text = display_text(action.get('text', ''), timezone, with_time=action.get('kind') == 'meeting_brief')
    if action.get('reason'):
        text += "\nMotif : " + display_text(action['reason'], timezone)
    if action.get('due_date'):
        # A checked date-only deadline does not acquire a timezone-dependent clock time.
        from datetime import date
        from presentation import WEEKDAYS, MONTHS
        day = date.fromisoformat(action['due_date'])
        text += f"\nÉchéance : {WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]} {day.year}"
    for citation in action.get('citations', []):
        kind = citation['source_id'].split(':', 1)[0]
        label = {'emails': 'Email', 'notes': 'Note CRM', 'calls': 'Compte rendu d’appel',
                 'meetings': 'Rendez-vous', 'tasks': 'Action CRM',
                 'deal.current_next_step': 'Prochaine action CRM'}.get(kind, 'Contexte CRM')
        if citation.get('at'):
            label += ' du ' + display_date(citation['at'], timezone)
        text += "\nSource : " + label + "\n« " + display_text(citation['quote'], timezone) + " »"
    return text


def action_texts(deal, timezone="UTC"):
    actions = [display_text(a.get('title', 'Préparation'), timezone) + "\n" + prepared_text(a, timezone)
               for a in deal.get('prepared_actions', [])]
    generated_kinds = {'followup_proposal', 'meeting_brief', 'keep_plan', 'wait', 'review', 'suggestion'}
    if deal.get('suggested_next_step') and not any(a.get('kind') in generated_kinds for a in deal.get('prepared_actions', [])):
        actions.append('Proposition à relire : ' + display_text(deal['suggested_next_step'], timezone))
    return actions


def card_text(deal, timezone="UTC"):
    name = deal.get('dealname') or deal['deal_id']
    parts = [f"{name} · {deal.get('stage') or 'Étape inconnue'}", reason(deal)]
    memory = deal.get('followup_memory')
    if memory:
        labels = {'new': 'Première revue', 'changed': 'Nouveau contexte depuis la dernière revue',
                  'pending': 'Reste à examiner', 'reviewed': 'Déjà examiné', 'dismissed': 'Écarté pour ce contexte',
                  'snoozed': 'Reporté', 'snooze_expired': 'Date de reprise atteinte',
                  'verification_required': 'Incertitude à vérifier'}
        parts.append(labels[memory['status']])
        if memory.get('decision'):
            decision = memory['decision']
            parts.append('Votre note : ' + decision['note'])
            if decision.get('until'):
                parts.append('Reprise : ' + display_date(decision['until'], timezone, with_time=True))
    if deal.get('current_next_step'):
        parts.append('Action renseignée : ' + deal['current_next_step'])
    parts.extend(action_texts(deal, timezone))
    for action in deal.get('signals', {}).get('planned_actions', []):
        parts.append(f"Prévu : {action['title']}, {display_date(action['at'], timezone, with_time=True)}")
    warnings = deal.get('signals', {}).get('warnings', [])
    if warnings:
        parts.append('À confirmer : ' + '; '.join(warnings))
    if deal.get('coverage') == 'direct_associations_only':
        parts.append('Couverture : associations directes du deal ; contexte indirect non parcouru.')
    elif deal.get('coverage') == 'partial_snapshot':
        parts.append('Couverture : export partiel ; des échanges peuvent manquer.')
    elif deal.get('coverage') == 'verified_snapshot_associations':
        parts.append('Couverture : associations déclarées vérifiées par le producteur de l’export.')
    elif deal.get('coverage') == 'indirect_context_examined':
        parts.append('Couverture : contacts et entreprises parcourus ; échanges non attribués exclus du diagnostic.')
    for candidate in deal.get('context_scan', {}).get('candidates', []):
        props = candidate['properties']
        excerpts = '\n'.join(str(value) for key, value in props.items()
                             if key in {'hs_note_body', 'hs_email_subject', 'hs_email_text',
                                        'hs_call_body', 'hs_meeting_body', 'hs_task_subject', 'text', 'title'})
        parts.append('Contexte à rattacher : ' + candidate['source_id']
                     + '\nRetrouvé via : ' + ', '.join(candidate.get('via', []))
                     + '\n' + excerpts + '\nExclu des faits du deal tant que le lien reste incertain.')
    return display_text('\n'.join(parts), timezone)


def render_recap(deals, deadline=None, timezone="UTC"):
    """Return ALL blocks; legacy deadline argument ignored (no imposed due date)."""
    groups = grouped(deals)
    summary = (f"Pipeline : {len(deals)} deals · {len(groups['À traiter'])} à traiter · "
               f"{len(groups['À vérifier'])} triage · {len(groups['Déjà prévu'])} déjà prévus · "
               f"{len(groups['Activité récente'])} avec activité récente")
    if 'Déjà examiné' in groups:
        summary += f" · {len(groups['Déjà examiné'])} déjà examinés, conservés dans le détail"
    blocks = [{'type': 'header', 'text': {'type': 'plain_text', 'text': 'Revue du pipeline'}}]
    if any(deal.get('fictional_example') for deal in deals):
        blocks.append({'type': 'section', 'text': {'type': 'plain_text', 'text': 'Démonstration : données fictives.'}})
    for label, rows in groups.items():
        if not rows:
            continue
        blocks.append({'type': 'header', 'text': {'type': 'plain_text', 'text': f'{label} ({len(rows)})'}})
        for deal in rows:
            if label == 'Déjà examiné':
                state = {'reviewed': 'Déjà examiné', 'dismissed': 'Écarté pour ce contexte',
                         'snoozed': 'Reporté'}[deal['followup_memory']['status']]
                text = f"{deal.get('dealname') or deal['deal_id']} · {state}"
            else:
                text = card_text(deal, timezone)
            # Split text without dropping content. Slack sections allow 3,000 chars.
            for offset in range(0, len(text), 2800):
                blocks.append({'type': 'section', 'text': {'type': 'plain_text', 'text': text[offset:offset + 2800]}})
            url = safe_url(deal.get('deal_url'))
            if url:
                blocks.append({'type': 'actions', 'elements': [{'type': 'button', 'text': {'type': 'plain_text', 'text': 'Ouvrir le deal'}, 'url': url}]})
    return summary, blocks


def slack_messages(summary, blocks):
    """One root + grouped thread replies. Physical Slack limits do not drop deals."""
    pages, current, size = [], [], 0
    for block in blocks:
        block_size = len(json.dumps(block, ensure_ascii=False))
        if current and (len(current) >= 45 or size + block_size > 24000):
            pages.append(current)
            current, size = [], 0
        current.append(block)
        size += block_size
    if current:
        pages.append(current)
    return [{'placement': 'root' if i == 0 else 'thread_reply',
             'payload': {'text': f'{summary} · partie {i + 1}/{len(pages)}', 'blocks': page}}
            for i, page in enumerate(pages)]


def render_html(deals, summary, metadata=None, timezone="UTC"):
    esc = lambda value: html.escape(display_text(value, timezone), quote=True)
    parts = ['<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Aperçu du récapitulatif Slack</title><style>body{font:16px/1.6 system-ui;background:#f4f6f8;color:#17212b;max-width:1050px;margin:40px auto;padding:0 24px}h1{font-size:32px}article{background:white;border:1px solid #dde3e8;border-radius:12px;padding:24px;margin:16px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;font:inherit}a{color:#175fc2}.note{color:#566473}nav a{margin-right:18px}h3{margin-top:0}</style>',
             '<h1>Aperçu du récapitulatif Slack</h1>',
             '<p class="note">Revue locale des deals et des préparations. Le fichier JSON contient le format de récapitulatif Slack ; aucun message n’est envoyé. La livraison et la planification restent à connecter selon votre environnement.</p>',
             f'<p class="note">Heures affichées : {esc(timezone)}. Les sections dépliables ci-dessous servent à examiner les préparations locales.</p>', f'<p>{esc(summary)}</p>']
    if any(d.get('fictional_example') for d in deals):
        parts.append('<p class="note">Démonstration entièrement fictive. Aucune action externe effectuée.</p>')
    if metadata and not metadata.get('complete'):
        parts.append(f'<p><strong>Analyse interrompue : {esc(metadata.get("processed_deals"))}/{esc(metadata.get("expected_deals"))} deals traités.</strong></p>')
    if not deals:
        parts.append('<p>Aucun résultat dans ce fichier. Cela ne prouve pas que le pipeline ne contient aucune priorité.</p>')
    groups = grouped(deals)
    parts.append('<nav>' + ''.join(f'<a href="#group-{i}">{esc(label)} ({len(rows)})</a>' for i, (label, rows) in enumerate(groups.items())) + '</nav>')
    index = 0
    for i, (label, rows) in enumerate(groups.items()):
        parts.append(f'<h2 id="group-{i}">{esc(label)} ({len(rows)})</h2>')
        for deal in rows:
            index += 1
            if label == 'Déjà examiné':
                parts.append(f'<details><summary>{esc(deal.get("dealname") or deal["deal_id"])} : déjà examiné</summary>')
            parts.append(f'<article id="deal-{index}"><h3>{esc(deal.get("dealname") or deal["deal_id"])}</h3><p>{esc(deal.get("stage") or "")}</p><p>{esc(reason(deal))}</p>')
            if deal.get('current_next_step'):
                parts.append(f'<p>Action renseignée : {esc(deal["current_next_step"])}</p>')
            if safe_url(deal.get('deal_url')):
                parts.append(f'<a href="{esc(deal["deal_url"])}" rel="noreferrer">Ouvrir le deal</a>')
            for action in deal.get('prepared_actions', []):
                parts.append(f'<details><summary>{esc(action.get("title", "Préparation"))}</summary><pre>{esc(prepared_text(action, timezone))}</pre></details>')
            generated_kinds = {'followup_proposal', 'meeting_brief', 'keep_plan', 'wait', 'review', 'suggestion'}
            if deal.get('suggested_next_step') and not any(a.get('kind') in generated_kinds for a in deal.get('prepared_actions', [])):
                parts.append(f'<details><summary>Proposition à relire</summary><pre>{esc(deal["suggested_next_step"])}</pre></details>')
            parts.append(f'<details><summary>Contexte et couverture</summary><pre>{esc(card_text(deal, timezone))}</pre></details></article>')
            if label == 'Déjà examiné':
                parts.append('</details>')
    parts.append('</html>')
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--timezone', default='UTC', help='Display timezone, e.g. Europe/Paris')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'output' / 'recap.json')
    args = parser.parse_args()
    ZoneInfo(args.timezone)
    deals = json.loads(args.input.read_text())
    if not isinstance(deals, list) or any(not isinstance(d, dict) or 'deal_id' not in d for d in deals):
        parser.error('Input must be a list of analyzed deals with deal_id')
    meta_path = args.input.with_suffix('.meta.json')
    metadata = json.loads(meta_path.read_text()) if meta_path.exists() else None
    summary, blocks = render_recap(deals, timezone=args.timezone)
    if metadata and not metadata.get('complete'):
        warning = f"Analyse incomplète : {metadata.get('processed_deals')}/{metadata.get('expected_deals')} deals traités. "
        summary = warning + summary
        blocks.insert(0, {'type': 'section', 'text': {'type': 'plain_text', 'text': warning}})
    result = {'text': summary, 'preview_only': True, 'deal_count': len(deals),
              'collection': metadata, 'messages': slack_messages(summary, blocks)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(args.output.parent, 0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    os.chmod(args.output, 0o600)
    html_path = args.output.with_suffix('.html')
    html_path.write_text(render_html(deals, summary, metadata, timezone=args.timezone))
    os.chmod(html_path, 0o600)
    print(summary)
    print(f'Local report: {html_path}')
    print(f'Slack preview: {args.output} ({len(result["messages"])} messages, none sent)')


if __name__ == '__main__':
    main()
