"""Validate cited proposals locally. No network or automatic action execution.

These checks establish structural/source consistency, not semantic truth. All accepted
proposals still need human review, especially with partial CRM coverage.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

DECISIONS = {'review', 'keep_plan', 'wait', 'prepare_followup', 'prepare_meeting_brief'}
MONTHS = {'january': 1, 'janvier': 1, 'february': 2, 'février': 2, 'march': 3, 'mars': 3,
          'april': 4, 'avril': 4, 'may': 5, 'mai': 5, 'june': 6, 'juin': 6,
          'july': 7, 'juillet': 7, 'august': 8, 'août': 8, 'september': 9, 'septembre': 9,
          'october': 10, 'octobre': 10, 'november': 11, 'novembre': 11,
          'december': 12, 'décembre': 12}
WEEKDAYS = {'monday': 0, 'lundi': 0, 'tuesday': 1, 'mardi': 1, 'wednesday': 2, 'mercredi': 2,
            'thursday': 3, 'jeudi': 3, 'friday': 4, 'vendredi': 4, 'saturday': 5, 'samedi': 5,
            'sunday': 6, 'dimanche': 6}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_PATTERN = re.compile(
    rf'\b(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})\b|'
    rf'\b(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?\b|'
    rf'\b(?P<day2>\d{{1,2}})(?:er)?\s+(?P<month2>{MONTH_PATTERN})(?:\s+(?P<year2>\d{{4}}))?\b', re.I)
# Recognized hints are deliberately conservative, not a multilingual intent classifier.
HOLD_HINT = re.compile(r"\b(?:not interested|no interest|do not contact|don't contact|not before|"
                       r"pas intéress[ée]|ne .{0,30}contact|pas .{0,20}avant|attendre|report[ée]|"
                       r"recontact|rappel|call me|contact me|remind me|later|le mois prochain)\b", re.I)
DATE_IN_PROSE = re.compile(r'\b(?:\d{1,2}[/-]\d{1,2}|today|tomorrow|aujourd.hui|demain|'
                           r'next week|this week|end of week|business days|semaine prochaine|fin de semaine|'
                           + '|'.join(WEEKDAYS) + r')\b', re.I)


def dates_in_text(text, year):
    for match in DATE_PATTERN.finditer(text):
        try:
            if match['iso']:
                value = date.fromisoformat(match['iso'])
            else:
                month = match['month'] or match['month2']
                value = date(int(match['year'] or match['year2'] or year), MONTHS[month.lower()],
                             int(match['day'] or match['day2']))
        except ValueError:
            value = None
        yield match, value


def audit_legacy_dates(text, reference_date):
    """Review flags only; a historical mention is not necessarily a proposed deadline."""
    issues = []
    for match, value in dates_in_text(text, reference_date.year):
        if value is None:
            issues.append('invalid_date')
            continue
        if value < reference_date:
            issues.append('past_date_requires_review')
        prefix = text[max(0, match.start() - 20):match.start()].lower()
        weekday = re.search(r'\b(' + '|'.join(WEEKDAYS) + r')[ ,(]*$', prefix)
        if weekday and WEEKDAYS[weekday[1]] != value.weekday():
            issues.append('weekday_date_mismatch')
    return sorted(set(issues))


def evidence_from(deal, engagements, next_step_property='hs_next_step', max_chars=3000):
    """Preserve stable references and explicitly flag omitted/shortened evidence."""
    records = []
    current = (deal.get('properties', {}).get(next_step_property) or '').strip()
    if current:
        records.append({'source_id': 'deal.current_next_step', 'kind': 'current_next_step',
                        'at': None, 'text': current[:max_chars], 'truncated': len(current) > max_chars})
    fields = {
        'emails': ['hs_email_subject', 'hs_email_direction', 'hs_email_status', 'hs_email_text'],
        'meetings': ['hs_meeting_title', 'hs_meeting_outcome', 'hs_meeting_body'],
        'calls': ['hs_call_title', 'hs_call_status', 'hs_call_body'],
        'notes': ['hs_note_body'], 'tasks': ['hs_task_subject', 'hs_task_status'],
    }
    omitted = False
    # Small prompt context, as in the original generator. Never a limit on deals.
    for kind, keys in fields.items():
        items = engagements.get(kind, [])
        omitted = omitted or len(items) > 5
        for item in items[:5]:
            text = '\n'.join(f'{key}: {item[key]}' for key in keys if item.get(key))
            if not text:
                continue
            identity = item.get('_source_id') or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
            records.append({'source_id': f'{kind}:{identity}', 'kind': kind,
                            'at': item.get('hs_meeting_start_time') if kind == 'meetings' else item.get('hs_timestamp'),
                            'text': text[:max_chars], 'truncated': len(text) > max_chars})
    return {'records': records, 'omitted': omitted,
            'truncated': any(r['truncated'] for r in records), 'coverage': 'direct_associations_only'}


def normalize(text):
    return ' '.join(text.split())


def validate_proposal(raw, evidence, signals, reference_date):
    """Fail closed into a review item; never expose rejected copy as actionable text."""
    issues = []
    try:
        proposal = json.loads(raw)
    except (ValueError, TypeError):
        proposal = None
    if not isinstance(proposal, dict):
        return rejected(['invalid_json'])
    required = {'decision', 'reason', 'action_text', 'due_date', 'citations'}
    if set(proposal) != required:
        issues.append('invalid_schema')
    decision = proposal.get('decision')
    if not isinstance(decision, str) or decision not in DECISIONS:
        issues.append('invalid_decision')
    for field in ('reason', 'action_text'):
        if not isinstance(proposal.get(field), str) or not proposal[field].strip():
            issues.append(f'missing_{field}')
    sources = {r['source_id']: r for r in evidence.get('records', [])}
    citations = proposal.get('citations')
    checked = []
    if not isinstance(citations, list) or not citations:
        issues.append('missing_citations')
    else:
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {'source_id', 'quote'}:
                issues.append('invalid_citation')
                continue
            source_id, quote = citation['source_id'], citation['quote']
            source = sources.get(source_id) if isinstance(source_id, str) else None
            if not source or not isinstance(quote, str) or len(normalize(quote)) < 12:
                issues.append('invalid_citation')
                continue
            if normalize(quote) not in normalize(source['text']):
                issues.append('quote_not_found')
                continue
            if 'weekday_date_mismatch' in audit_legacy_dates(quote, reference_date):
                issues.append('inconsistent_source_date')
            checked.append({'source_id': source_id, 'quote': quote, 'at': source.get('at')})
    due = proposal.get('due_date')
    if due is not None:
        try:
            if not isinstance(due, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', due):
                raise ValueError()
            deadline = date.fromisoformat(due)
            if deadline < reference_date:
                issues.append('past_deadline')
            # A date must occur in the cited text. Event timestamps alone are not deadlines.
            supported = {d for citation in checked for _, d in dates_in_text(citation['quote'], reference_date.year) if d}
            if deadline not in supported:
                issues.append('unsupported_deadline')
            if decision in {'wait', 'keep_plan'}:
                from signals import to_dt
                planned_dates = [to_dt(a.get('at')) for a in signals.get('planned_actions', [])]
                planned_dates = [dt.date() for dt in planned_dates if dt]
                if planned_dates and deadline < min(planned_dates):
                    issues.append('deadline_precedes_existing_plan')
        except (ValueError, TypeError):
            issues.append('invalid_deadline')
    text = proposal.get('action_text', '')
    if isinstance(text, str) and (DATE_PATTERN.search(text) or DATE_IN_PROSE.search(text)):
        issues.append('date_in_action_text')  # Dates belong in the checked due_date field.
    if isinstance(text, str) and '\u2014' in text:
        issues.append('em_dash_in_copy')
    cited_ids = {c['source_id'] for c in checked}
    if 'deal.current_next_step' in sources and 'deal.current_next_step' not in cited_ids:
        issues.append('existing_action_not_cited')
    if decision == 'prepare_followup':
        if signals.get('planned_actions'):
            issues.append('existing_planned_action')
        if signals.get('inbound_after_last_outbound'):
            issues.append('unresolved_inbound_intent')
        if signals.get('warnings') or evidence.get('truncated') or evidence.get('omitted'):
            issues.append('incomplete_context')
        if any(HOLD_HINT.search(r['text']) for r in sources.values()):
            issues.append('possible_contact_restriction')
        if any('hs_email_status: DRAFT' in r['text'] for r in sources.values()):
            issues.append('existing_draft')
    if decision == 'prepare_meeting_brief':
        scheduled = [a for a in signals.get('planned_actions', []) if a.get('kind') == 'meeting']
        if not scheduled:
            issues.append('no_scheduled_meeting')
        if not any(source_id.startswith('meetings:') for source_id in cited_ids):
            issues.append('meeting_not_cited')
        if signals.get('warnings') or evidence.get('truncated') or evidence.get('omitted'):
            issues.append('incomplete_context')
    if decision == 'keep_plan' and not signals.get('planned_actions') and not signals.get('current_next_step'):
        issues.append('no_existing_plan')
    if issues:
        return rejected(issues)
    safe_text = proposal['action_text'].strip()
    # These decisions do not authorize a newly worded follow-up. Preserve the plan
    # instead of trusting a potentially contradictory model-generated action string.
    if decision == 'keep_plan':
        current = signals.get('current_next_step') or signals['planned_actions'][0]['title']
        safe_text = 'Conserver l’action déjà prévue : ' + current
    elif decision == 'wait':
        safe_text = 'Respecter le report indiqué dans les sources. Aucun nouveau suivi proposé.'
    elif decision == 'review':
        safe_text = 'Vérifier le point indiqué dans le motif avant de préparer une nouvelle action.'
    return {'status': 'checks_passed', 'requires_human_review': True, 'issues': [],
            'decision': decision, 'text': safe_text.replace('\u2014', ','), 'reason': proposal['reason'].strip(),
            'due_date': due, 'citations': checked,
            'coverage': evidence.get('coverage', 'unknown')}


def prepared_action(validation):
    """Turn a checked proposal into an honest local artifact for the recap."""
    decision = validation.get('decision', 'review')
    titles = {
        'prepare_followup': ('followup_proposal', 'Proposition locale de suivi'),
        'prepare_meeting_brief': ('meeting_brief', 'Brief du prochain rendez-vous'),
        'keep_plan': ('keep_plan', 'Plan existant à conserver'),
        'wait': ('wait', 'Attente convenue'),
        'review': ('review', 'Vérification requise'),
    }
    kind, title = titles.get(decision, titles['review'])
    if validation.get('status') != 'checks_passed':
        kind, title = titles['review']
    return {
        'kind': kind,
        'status': 'prepared_local' if validation.get('status') == 'checks_passed' else 'needs_review',
        'title': title,
        'text': validation['text'],
        'reason': validation['reason'],
        'due_date': validation.get('due_date'),
        'citations': validation.get('citations', []),
    }


REVIEW_REASONS = {
    'missing_review': 'Ce deal n’a pas encore de proposition de l’assistant.',
    'invalid_json': 'La réponse ne respecte pas le format attendu.',
    'invalid_schema': 'La réponse contient des champs manquants ou inattendus.',
    'invalid_decision': 'La décision proposée est inconnue.',
    'missing_reason': 'Le motif manque.',
    'missing_action_text': 'Le texte de la proposition manque.',
    'missing_citations': 'Aucune preuve exploitable n’est citée.',
    'invalid_citation': 'Une référence de source est absente ou invalide.',
    'quote_not_found': 'Un extrait cité ne figure pas dans la source fournie.',
    'past_deadline': 'L’échéance proposée est déjà passée.',
    'unsupported_deadline': 'La date proposée n’est pas étayée par l’extrait cité.',
    'invalid_deadline': 'L’échéance proposée est invalide.',
    'date_in_action_text': 'Une date ou un délai doit être vérifié séparément du texte de l’action.',
    'em_dash_in_copy': 'La ponctuation de la proposition doit être corrigée.',
    'existing_action_not_cited': 'La prochaine action déjà renseignée n’a pas été citée.',
    'existing_planned_action': 'Une action datée est déjà prévue.',
    'existing_undated_action': 'Une action existe déjà mais sa date reste à confirmer.',
    'unresolved_inbound_intent': 'Le sens du dernier message entrant reste à vérifier.',
    'incomplete_context': 'Une partie du contexte est manquante, tronquée ou incertaine.',
    'possible_contact_restriction': 'Une source contient un possible refus ou report de contact.',
    'existing_draft': 'Un brouillon est déjà mentionné dans les sources.',
    'no_scheduled_meeting': 'Aucun rendez-vous futur confirmé ne permet de préparer ce brief.',
    'meeting_not_cited': 'Le rendez-vous futur n’est pas cité dans les preuves.',
    'no_existing_plan': 'Aucune action existante ne permet de justifier cette décision.',
    'truncated_model_response': 'La génération a été interrompue avant sa fin.',
    'deadline_precedes_existing_plan': 'La date proposée précède l’action déjà prévue.',
    'inconsistent_source_date': 'Le jour de la semaine et la date cités se contredisent.',
}


def rejected(issues):
    return {'status': 'needs_review', 'requires_human_review': True,
            'issues': sorted(set(issues)), 'decision': 'review',
            'text': 'Vérifier le contexte et les engagements existants avant de préparer une nouvelle action.',
            'reason': ' '.join(REVIEW_REASONS.get(issue, 'Un contrôle supplémentaire est nécessaire.') for issue in sorted(set(issues))),
            'due_date': None, 'citations': []}
