"""Human-readable dates and punctuation for previews; source timestamps stay intact."""
import re
from zoneinfo import ZoneInfo
from signals import to_dt

WEEKDAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')
MONTHS = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
          'septembre', 'octobre', 'novembre', 'décembre')
ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})')


def display_date(value, timezone='UTC', with_time=False):
    dt = to_dt(value)
    if not dt:
        return 'date à confirmer'
    local = dt.astimezone(ZoneInfo(timezone))
    text = f'{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} {local.year}'
    if with_time:
        text += f' à {local.hour} h' + (f' {local.minute:02}' if local.minute else '')
    return text


def display_text(value, timezone='UTC', with_time=False):
    text = str(value).replace('\u2014', ',').replace(' ,', ',')
    return ISO_DATE.sub(lambda match: display_date(match.group(), timezone, with_time), text)
