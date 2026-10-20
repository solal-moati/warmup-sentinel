"""User-defined schedule planning. No daemon, installation or external delivery."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_schedule(schedule: dict) -> dict:
    if not isinstance(schedule, dict):
        raise ValueError('schedule must be an object')
    mode = schedule.get('mode', 'manual')
    if mode not in {'manual', 'scheduled'}:
        raise ValueError('schedule.mode must be manual or scheduled')
    if mode == 'manual':
        return {'mode': 'manual'}
    try:
        zone = ZoneInfo(schedule['timezone'])
    except (KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError('A valid IANA timezone is required for scheduled mode') from exc
    result = {'mode': mode, 'timezone': zone.key}
    for phase in ('analysis', 'delivery'):
        entries = schedule.get(phase)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f'schedule.{phase} must contain at least one chosen time')
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Each schedule entry must be an object')
            days, at = entry.get('weekdays'), entry.get('at')
            if (not isinstance(days, list) or not days or
                    any(type(d) is not int or d not in range(7) for d in days)):
                raise ValueError('weekdays must contain numbers 0 (Monday) to 6 (Sunday)')
            if not isinstance(at, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', at):
                raise ValueError('at must be a valid HH:MM time')
            normalized.append({'weekdays': sorted(set(days)), 'at': at})
        result[phase] = normalized
    return result


def next_occurrences(schedule: dict, phase: str, after: datetime, count: int = 3) -> list[datetime]:
    schedule = validate_schedule(schedule)
    if phase not in {'analysis', 'delivery'} or count < 1:
        raise ValueError('Choose analysis/delivery and a positive count')
    if after.tzinfo is None:
        raise ValueError('after must include a timezone')
    if schedule['mode'] == 'manual':
        return []
    zone = ZoneInfo(schedule['timezone'])
    local_day = after.astimezone(zone).date()
    result = set()
    # Each valid schedule has at least one weekly slot. Skip nonexistent DST times;
    # execute an ambiguous wall-clock time once, at its first occurrence (fold=0).
    for offset in range((count + 2) * 7):
        day = local_day + timedelta(days=offset)
        for entry in schedule[phase]:
            if day.weekday() not in entry['weekdays']:
                continue
            candidate = datetime.combine(day, time.fromisoformat(entry['at']), zone)
            utc = candidate.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
                continue
            if utc > after.astimezone(timezone.utc):
                result.add(utc)
        if len(result) >= count:
            break
    return [dt.astimezone(zone) for dt in sorted(result)[:count]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--after', help='ISO timestamp with timezone; defaults to now')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    schedule = validate_schedule(config.get('schedule', {'mode': 'manual'}))
    after = datetime.fromisoformat(args.after.replace('Z', '+00:00')) if args.after else datetime.now(timezone.utc)
    print(json.dumps({'mode': schedule['mode'], 'installed': False,
                      **{phase: [dt.isoformat() for dt in next_occurrences(schedule, phase, after)]
                         for phase in ('analysis', 'delivery')}}, indent=2))


if __name__ == '__main__':
    main()
