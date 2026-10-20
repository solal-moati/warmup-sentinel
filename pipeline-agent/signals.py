"""Evidence-based, offline pipeline signals. No interpretation of email sentiment."""
from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_RULES = {'inactivity_days': 14, 'stage_age_days': 30}


def to_dt(value):
    if value is None or value == '':
        return None
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Do not silently interpret a timezone-less CRM timestamp.
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def compute(deal, engagements, *, now, stage_labels, is_automated, rules=None,
            next_step_property='hs_next_step', next_step_date_property=None):
    rules = {**DEFAULT_RULES, **(rules or {})}
    props = deal.get('properties', {})
    stage = props.get('dealstage')
    warnings = []

    def dated(kind, time_property):
        result = []
        for event in engagements.get(kind, []):
            dt = to_dt(event.get(time_property))
            if dt is None:
                warnings.append(f'{kind}: date absente ou invalide')
            else:
                result.append((dt, event))
        return sorted(result, key=lambda item: item[0], reverse=True)

    meetings = dated('meetings', 'hs_meeting_start_time')
    past_meetings = [(dt, e) for dt, e in meetings if dt <= now and e.get('hs_meeting_outcome') == 'COMPLETED']
    future_meetings = [(dt, e) for dt, e in meetings if dt > now and e.get('hs_meeting_outcome') == 'SCHEDULED']
    unknown_meetings = [e for dt, e in meetings if not e.get('hs_meeting_outcome') or
                        (dt <= now and e.get('hs_meeting_outcome') == 'SCHEDULED')]
    if unknown_meetings:
        warnings.append('Rendez-vous dont la tenue reste à confirmer')
    calls = [(dt, e) for dt, e in dated('calls', 'hs_timestamp')
             if dt <= now and e.get('hs_call_status') == 'COMPLETED']
    emails = [(dt, e) for dt, e in dated('emails', 'hs_timestamp')
              if dt <= now and not is_automated(e) and
              (e.get('hs_email_direction') in {'INCOMING_EMAIL', 'INCOMING'} or e.get('hs_email_status') == 'SENT')]
    incoming = [(dt, e) for dt, e in emails if e.get('hs_email_direction') in {'INCOMING_EMAIL', 'INCOMING'}]
    outgoing = [(dt, e) for dt, e in emails if e.get('hs_email_direction') == 'EMAIL']
    last_in = incoming[0] if incoming else None
    last_out = outgoing[0] if outgoing else None
    touches = [dt for dt, _ in past_meetings + calls + emails]
    last_touch = max(touches) if touches else None
    entered = to_dt(props.get(f'hs_v2_date_entered_{stage}'))
    stage_age = (now - entered).days if entered and entered <= now else None
    next_step = (props.get(next_step_property) or '').strip()
    step_date = to_dt(props.get(next_step_date_property)) if next_step_date_property else None
    if next_step_date_property and props.get(next_step_date_property) and not step_date:
        warnings.append('Date de prochaine action invalide ou sans fuseau')
    tasks = [(dt, e) for dt, e in dated('tasks', 'hs_timestamp')
             if e.get('hs_task_status') not in {'COMPLETED'}]
    known_tasks = [(dt, e) for dt, e in tasks if e.get('hs_task_status')]
    if len(known_tasks) != len(tasks):
        warnings.append('Statut de tâche inconnu')
    planned = [{'kind': 'meeting', 'at': dt.isoformat(), 'title': e.get('hs_meeting_title') or 'Rendez-vous'}
               for dt, e in future_meetings]
    planned += [{'kind': 'task', 'at': dt.isoformat(), 'title': e.get('hs_task_subject') or 'Action prévue'}
                for dt, e in known_tasks if dt > now]
    if next_step and step_date and step_date > now:
        planned.append({'kind': 'next_step', 'at': step_date.isoformat(), 'title': next_step})
    overdue = [{'kind': 'task', 'at': dt.isoformat(), 'title': e.get('hs_task_subject') or 'Action prévue'}
               for dt, e in known_tasks if dt <= now]
    if next_step and step_date and step_date <= now:
        overdue.append({'kind': 'next_step', 'at': step_date.isoformat(), 'title': next_step})
    return {
        'stage_id': stage, 'stage_label': stage_labels.get(stage, stage),
        'days_in_stage': stage_age,
        'days_since_personal_touch': (now - last_touch).days if last_touch else None,
        'had_meeting_ever': bool(past_meetings),
        'last_meeting': past_meetings[0][1] if past_meetings else None,
        'last_call': calls[0][1] if calls else None,
        'last_personal_email': emails[0][1] if emails else None,
        'last_inbound_email': last_in[1] if last_in else None,
        'last_inbound_at': last_in[0].isoformat() if last_in else None,
        'inbound_after_last_outbound': bool(last_in and (not last_out or last_in[0] > last_out[0])),
        'planned_actions': sorted(planned, key=lambda p: p['at']),
        'overdue_actions': sorted(overdue, key=lambda p: p['at']),
        'current_next_step': next_step,
        'next_step_undated': bool(next_step and not step_date),
        'warnings': sorted(set(warnings)), 'rules': rules,
        'counts': {k: len(v) for k, v in engagements.items()},
        'personal_email_count': len(emails),
        'lemlist_email_count': sum(is_automated(e) for e in engagements.get('emails', [])),
    }


def classify(signals):
    if signals['overdue_actions']:
        return 'overdue_action'
    if signals['inbound_after_last_outbound']:
        return 'needs_reply_sentiment_check'
    if signals['planned_actions']:
        return 'action_planned'
    if signals['next_step_undated']:
        return 'next_step_needs_date'
    if signals['warnings']:
        return 'unclear'
    age, stage_age = signals['days_since_personal_touch'], signals['days_in_stage']
    if age is not None and age >= signals['rules']['inactivity_days']:
        return 'stalled_post_meeting' if signals['had_meeting_ever'] else 'stalled_pre_meeting'
    if age is None and stage_age is not None and stage_age >= signals['rules']['stage_age_days']:
        return 'unclear'  # No recorded interaction does not prove inactivity.
    if age is not None:
        return 'recent_activity_no_step_recorded'
    return 'unclear'


def prepare_actions(signals):
    """Prepare local work from facts only, without fabricating an email reply."""
    actions = []
    for action in signals['overdue_actions']:
        actions.append({'kind': 'commitment_review', 'status': 'prepared_local',
                        'title': 'Vérifier l’action arrivée à échéance',
                        'text': f"{action['title']} : échéance {action['at']}. Son exécution reste à confirmer dans les sources accessibles."})
    if signals['inbound_after_last_outbound']:
        email = signals['last_inbound_email'] or {}
        actions.append({'kind': 'reply_review', 'status': 'prepared_local',
                        'title': 'Dernier message entrant à examiner',
                        'text': f"Reçu le {signals['last_inbound_at']}. {email.get('hs_email_subject') or ''}\n{email.get('hs_email_text') or 'Contenu du message indisponible.'}\nVérifier question, refus ou report avant toute relance."})
    for action in signals['planned_actions']:
        if action['kind'] == 'meeting':
            actions.append({'kind': 'meeting_brief', 'status': 'prepared_local',
                            'title': 'Repères pour le prochain rendez-vous',
                            'text': f"{action['title']} : {action['at']}.\nProchaine action renseignée : {signals['current_next_step'] or 'aucune'}.\nParticipants, objections et décisions attendues restent à confirmer ; synthèse de comptes rendus non disponible dans cette version."})
    if signals.get('next_step_undated'):
        actions.append({'kind': 'next_step_review', 'status': 'prepared_local',
                        'title': 'Action existante à confirmer',
                        'text': (f"Action renseignée : {signals['current_next_step']}\n"
                                 "Confirmer si elle reste d’actualité, qui doit agir et à quel moment.")})
    if 'Rendez-vous dont la tenue reste à confirmer' in signals.get('warnings', []):
        actions.append({'kind': 'meeting_status_review', 'status': 'prepared_local',
                        'title': 'Statut de rendez-vous à confirmer',
                        'text': ('Un rendez-vous a un statut manquant, ou reste planifié après sa date. '
                                 'Vérifier sa tenue ou sa programmation avant de décider du suivi.')})
    return actions
