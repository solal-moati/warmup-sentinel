"""lemwarm deliverability: mailbox enumeration, parsing, rotation
classification, anomaly checks. Pure logic, no network, except WarmupClient
(a thin subclass of the lemlist client) and the DNS lookups for the domain
blocklists.

API facts verified live, Sept 09-14 2026, on a real 16-mailbox account:

  · a nonexistent path returns HTTP 200 + the app's HTML shell (`raw` key in
    lib/lemlist.py) and an unknown user id returns `{}` with 200: the HTTP
    status proves nothing, we validate the CONTENT of every response;
  · mailboxes enumerate via GET /team → userIds, then GET /users/{id} →
    mailboxes[] (status, lemlist.emailLimit, lemwarm.active): a mailbox that
    sits in no campaign stays monitored;
  · the lemwarm score is roughly 70 + (warmups landed in inbox ÷ 10): the
    DNS, age and sent-volume parts were already capped on every mailbox.
    The score therefore follows the COUNT of inbox warmups, not the rate: it
    rises with warmup volume, hence the QUARANTINE veto on real placement;
  · inbox placement moves 5 to 12 points per day on the same mailbox
    (lemlist: the spam rate needs "~300 sends" to stabilize): classification
    runs on 7 days, never on a single day's value;
  · a warmNextEmailAt frozen in the past = outgoing warmup stopped (3
    mailboxes, Sept 01-04) while lastWarmAt keeps moving: that is the field
    to test — the UI still shows the mailbox as "active";
  · lemwarm's blacklist check (blacklistsInfo) reported no listing while
    every sending domain sat on SURBL: it does not appear to cover domain
    blocklists, so SURBL, Spamhaus DBL and URIBL are checked here, over DNS.
"""

from __future__ import annotations

import csv
import json
import os
import socket
from datetime import datetime, timedelta, timezone

from lib import config
from lib.lemlist import LemlistClient

DATA_DIR = config.REPO_ROOT / "data"
HISTORY_CSV = DATA_DIR / "history.csv"
LATEST_JSON = DATA_DIR / "latest.json"
REPORT_MD = DATA_DIR / "report.md"

FIELDS = [
    "collected_at", "last_at", "usm_id", "email", "domain", "user_id",
    "status", "lemwarm_active", "email_limit", "warm_email_max",
    "score", "inbox", "total", "inbox_pct", "total_sent",
    "dns_score", "blacklisted", "mailbox_age",
    "last_warm_at", "warm_next_email_at", "spam_alert", "last_bounced",
]
LATEST_KEYS = [
    "email", "domain", "status", "lemwarm_active", "email_limit",
    "warm_email_max", "score", "inbox_pct", "blacklisted", "last_at",
    "last_warm_at", "warm_next_email_at", "spam_alert", "last_bounced",
]

def setting(name: str, default):
    """An operator rule, overridable through the environment or .env (same
    type as the default). These are OUR operating rules, not provider
    limits: adjust them to your setup without touching the code."""
    from lib import env
    raw = env.get(name, required=False).strip()
    if not raw:
        return default
    try:
        return type(default)(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a valid {type(default).__name__}")


# Rotation classification (operator decision, 2026-09-09: thresholds
# recalibrated on the real score distribution, 78 to 87; a gate at 90 would
# classify no mailbox at all). Override: ACTIVE_MIN, RECOVERING_MIN,
# QUARANTINE_INBOX_BELOW.
WINDOW_DAYS = 7
ACTIVE_MIN = setting("ACTIVE_MIN", 84)
RECOVERING_MIN = setting("RECOVERING_MIN", 80)     # one reading below = RESTING for 7 days
QUARANTINE_INBOX_BELOW = setting("QUARANTINE_INBOX_BELOW", 70)   # veto on real placement

# Anomaly checks (sentinel_report.py --check)
COLLECT_STALE_H = 36
WARM_STALE_H = 48
WARM_NEXT_OVERDUE_H = 24      # warmNextEmailAt overdue = outgoing warmup stuck
SCORE_FROZEN_H = 72
SPAM_ALERT_FRESH_H = 36


class ReadOnlyViolation(RuntimeError):
    """Write attempted on a client opened read-only."""


class WarmupClient(LemlistClient):
    """lemlist client, READ-ONLY by default: any verb other than GET raises
    before a single network call. Only an explicit write script, run with
    --commit, opens it with read_only=False."""

    def __init__(self, api_key: str, read_only: bool = True):
        super().__init__(api_key)
        self.read_only = read_only

    def _request(self, method, endpoint, **kwargs):
        if self.read_only and method.upper() != "GET":
            raise ReadOnlyViolation(
                f"{method} {endpoint} refused: client opened read-only")
        return super()._request(method, endpoint, **kwargs)

    def get_campaign(self, campaign_id: str) -> dict:
        _, data = self._request("GET", f"campaigns/{campaign_id}")
        return data

    def get_lemwarm_settings(self, usm_id: str) -> dict:
        _, data = self._request("GET", f"lemwarm/{usm_id}/settings")
        return data


# ── Time and numbers ─────────────────────────────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Response guard ───────────────────────────────────────────────────────────
def is_valid(data, required: str | None = None) -> bool:
    """A usable response = a non-empty dict, without the `raw` key (HTML shell
    served with a 200 on a nonexistent path), holding `required` if asked."""
    if not isinstance(data, dict) or not data or "raw" in data:
        return False
    return required is None or required in data


# ── Enumeration ──────────────────────────────────────────────────────────────
def boxes_from_users(users: list) -> dict:
    """{usm_id: mailbox info} from GET /users/{id} responses."""
    boxes = {}
    for user in users:
        for m in user.get("mailboxes") or []:
            email = (m.get("email") or "").lower()
            boxes[m["_id"]] = {
                "email": email,
                "domain": email.split("@")[-1],
                "user_id": user.get("_id", ""),
                "status": m.get("status") or "",
                "email_limit": (m.get("lemlist") or {}).get("emailLimit"),
                "lemwarm_active": (m.get("lemwarm") or {}).get("active"),
            }
    return boxes


def enumerate_mailboxes(client) -> dict:
    """Every mailbox connected to lemlist, in a campaign or not. Raises when a
    response is unusable: a failed enumeration must never pass for "zero
    mailboxes"."""
    team = client.get_team()
    if not is_valid(team, "userIds"):
        raise RuntimeError("GET /team unusable (API key or path?)")
    users = []
    for uid in team["userIds"]:
        user = client.get_user(uid)
        if not is_valid(user, "mailboxes"):
            raise RuntimeError(f"GET /users/{uid} unusable")
        users.append(user)
    return boxes_from_users(users)


def tracking_domain(client) -> str:
    """The team's tracking domain (links and pixel), or '' when absent."""
    team = client.get_team()
    return (team.get("customDomain") or "").lower() if is_valid(team) else ""


def running_campaigns_by_mailbox(client) -> dict:
    """{usm_id: [running campaigns where the mailbox is a sender]}. Feeds the
    rule "never pull a mailbox out of a rotation in flight"."""
    running = {}
    for user in client.get_senders():
        for c in user.get("campaigns") or []:
            if c.get("status") == "running":
                running[c["_id"]] = c.get("name") or c["_id"]
    out = {}
    for cid, name in sorted(running.items(), key=lambda kv: kv[1]):
        data = client.get_campaign(cid)
        if not is_valid(data, "senders"):
            raise RuntimeError(f"GET /campaigns/{cid} unusable")
        for s in data.get("senders") or []:
            mid = s.get("sendUserMailboxId")
            if mid and name not in out.setdefault(mid, []):
                out[mid].append(name)
    return out


# ── Parsing ──────────────────────────────────────────────────────────────────
def _block(details, key: str) -> dict:
    """The deliverability.details block holding `key` (order not guaranteed)."""
    for block in details or []:
        if isinstance(block, dict) and key in block:
            return block
    return {}


def parse_settings(box: dict, usm_id: str, settings: dict,
                   collected_at: str) -> dict:
    """One history row from GET /lemwarm/{id}/settings."""
    dl = settings.get("deliverability") or {}
    details = dl.get("details") or []
    placement = _block(details, "percent")
    breakdown = _block(details, "totalEmailSent")
    listed = (breakdown.get("blacklistsInfo") or {}).get("blacklistedIn") or []
    bounced = settings.get("lastBounced")
    if isinstance(bounced, dict):
        bounced = bounced.get("date")
    return {
        "collected_at": collected_at,
        "last_at": dl.get("lastAt") or "",
        "usm_id": usm_id,
        "email": box.get("email", ""),
        "domain": box.get("domain", ""),
        "user_id": box.get("user_id", ""),
        "status": box.get("status", ""),
        "lemwarm_active": settings.get("active"),
        "email_limit": box.get("email_limit"),
        "warm_email_max": settings.get("warmEmailMax"),
        "score": dl.get("score"),
        "inbox": placement.get("inbox"),
        "total": placement.get("total"),
        "inbox_pct": placement.get("percent"),
        "total_sent": breakdown.get("totalEmailSent"),
        "dns_score": _block(details, "dnsScore").get("dnsScore"),
        "blacklisted": len(listed),
        "mailbox_age": _block(details, "mailboxAge").get("mailboxAge"),
        "last_warm_at": settings.get("lastWarmAt") or "",
        "warm_next_email_at": settings.get("warmNextEmailAt") or "",
        "spam_alert": settings.get("spamAlert") or "",
        "last_bounced": bounced or "",
    }


# ── History ──────────────────────────────────────────────────────────────────
def read_history(path=HISTORY_CSV) -> list:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def new_rows(parsed: list, history: list) -> list:
    """Per-mailbox dedupe on lastAt: a row only when lemlist recomputed the
    score since the mailbox's last reading. Without it, the same computation
    would count twice and every delta would lie."""
    seen = {}
    for r in history:
        t = parse_ts(r.get("last_at"))
        if t and (r["usm_id"] not in seen or t > seen[r["usm_id"]]):
            seen[r["usm_id"]] = t
    fresh = []
    for p in parsed:
        t = parse_ts(p.get("last_at"))
        if t and (p["usm_id"] not in seen or t > seen[p["usm_id"]]):
            fresh.append(p)
    return fresh


def append_history(rows: list, path=HISTORY_CSV) -> None:
    """Appends rows. When FIELDS gained a column since the file was created,
    the header is rewritten once with the old rows kept (new field empty): a
    plain append would shift every column."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open(newline="") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:
            old = read_history(path)
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k) or "" for k in FIELDS})
    header = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if header:
            w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r.get(k) for k in FIELDS})


def read_latest(path=LATEST_JSON) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_latest(state: dict, path=LATEST_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n")


# ── Classification ───────────────────────────────────────────────────────────
def series(history: list, usm_id: str) -> list:
    """One mailbox's readings, oldest to newest computation."""
    rows = [r for r in history
            if r["usm_id"] == usm_id and parse_ts(r.get("last_at"))]
    return sorted(rows, key=lambda r: parse_ts(r["last_at"]))


def window(history: list, usm_id: str, now: datetime,
           days: int = WINDOW_DAYS) -> list:
    cutoff = now - timedelta(days=days)
    return [r for r in series(history, usm_id)
            if parse_ts(r["last_at"]) > cutoff]


def classify(scores: list, inbox_pcts: list, blacklisted: bool = False):
    """(state, reason) from the window's readings.

    QUARANTINE  blacklisted, or mean inbox < 70%  (veto, overrides the score)
    RESTING     at least one score < 80 in the window: it takes 7 consecutive
                days above to get out (no flip-flopping)
    ACTIVE      mean score ≥ 84
    RECOVERING  otherwise
    The mean smooths the daily placement noise (±5 to 12 points)."""
    if not scores:
        return "UNKNOWN", "no score computed over 7d"
    if blacklisted:
        return "QUARANTINE", "blacklisted"
    if inbox_pcts:
        mean_inbox = sum(inbox_pcts) / len(inbox_pcts)
        if mean_inbox < QUARANTINE_INBOX_BELOW:
            return "QUARANTINE", (f"mean inbox {mean_inbox:.0f}% "
                                  f"(< {QUARANTINE_INBOX_BELOW}%)")
    if min(scores) < RECOVERING_MIN:
        return "RESTING", f"score at {min(scores):.0f} within 7d (< {RECOVERING_MIN})"
    mean_score = sum(scores) / len(scores)
    if mean_score >= ACTIVE_MIN:
        return "ACTIVE", f"mean score {mean_score:.1f}"
    return "RECOVERING", f"mean score {mean_score:.1f}"


def classify_box(history: list, usm_id: str, now: datetime,
                 blacklisted: bool = False) -> dict:
    rows = window(history, usm_id, now)
    scores = [s for s in (to_float(r.get("score")) for r in rows) if s is not None]
    inbox = [p for p in (to_float(r.get("inbox_pct")) for r in rows) if p is not None]
    state, why = classify(scores, inbox, blacklisted)
    return {
        "state": state, "why": why, "n": len(rows),
        "mean_score": sum(scores) / len(scores) if scores else None,
        "mean_inbox": sum(inbox) / len(inbox) if inbox else None,
    }


def delta_since(history: list, usm_id: str, field: str, now: datetime,
                days: int) -> float | None:
    """Latest value minus the value in force `days` ago (None until the
    history reaches back that far)."""
    rows = series(history, usm_id)
    target = now - timedelta(days=days)
    past = [r for r in rows if parse_ts(r["last_at"]) <= target]
    if not rows or not past:
        return None
    a, b = to_float(past[-1].get(field)), to_float(rows[-1].get(field))
    return None if a is None or b is None else b - a


# ── Send capacity ────────────────────────────────────────────────────────────
# Operating rule: at most 50 emails per mailbox per day INCLUDING warmup;
# RECOVERING mailboxes take new leads at half volume; one lead costs 3 to 4
# emails on the same mailbox over its sequence (measured live). Override:
# BOX_DAILY_CAP, TARGET_NEW_LEADS, EMAILS_PER_LEAD.
BOX_DAILY_CAP = setting("BOX_DAILY_CAP", 50)
TARGET_NEW_LEADS = setting("TARGET_NEW_LEADS", 80)
EMAILS_PER_LEAD = setting("EMAILS_PER_LEAD", 3.5)
NEW_LEAD_SHARE = {"ACTIVE": 1.0, "RECOVERING": 0.5}


def capacity(boxes: list) -> dict:
    """Daily capacity for NEW leads. `boxes`: [{email, state,
    warm_email_max}]. Campaign room = cap minus warmup; ACTIVE takes it in
    full, RECOVERING takes half, the others none (their in-flight leads keep
    their follow-ups). At cruise, one new lead per day costs EMAILS_PER_LEAD
    sends per day on its mailbox."""
    per_box = []
    for b in boxes:
        share = NEW_LEAD_SHARE.get(b.get("state"), 0.0)
        room = max(BOX_DAILY_CAP - (to_float(b.get("warm_email_max")) or 0), 0)
        emails = room * share
        per_box.append(dict(b, share=share, room=room, emails=emails,
                            leads=emails / EMAILS_PER_LEAD))
    total = sum(p["emails"] for p in per_box)
    return {"boxes": per_box, "emails": total, "leads": total / EMAILS_PER_LEAD}


# ── Domain blocklists ────────────────────────────────────────────────────────
# On a real account, all 6 sending domains and the tracking domain sat on
# SURBL ABUSE while lemwarm's blacklist check showed 100/100 and no listing.
# A list is only judged when its test point answers and the clean control
# resolves to "not listed" (NXDOMAIN): a public resolver that refuses SURBL
# (1.1.1.1 does) yields "not verifiable", never a false positive. A DNS
# failure on one domain (SERVFAIL, timeout) leaves it out of that day's
# checked scope instead of faking a delisting.
DOMAIN_LISTS = {
    "SURBL": ("multi.surbl.org", "test.surbl.org"),
    "Spamhaus DBL": ("dbl.spamhaus.org", "dbltest.com"),
    "URIBL": ("multi.uribl.com", "test.uribl.com"),
}
CLEAN_CONTROL = "example.com"
# Extra domains to watch (your website, a tracking domain...), comma-separated.
SITE_DOMAINS = tuple(d.strip() for d in
                     os.environ.get("SITE_DOMAINS", "").split(",") if d.strip())
SURBL_BITS = {8: "PH", 16: "MW", 64: "ABUSE", 128: "CR"}
UNKNOWN = "?"   # indeterminate DNS answer (not NXDOMAIN, no address)


def _resolve(name: str):
    """The returned address, None when the name does not exist (NXDOMAIN =
    not listed), UNKNOWN for any transient failure."""
    try:
        return socket.gethostbyname(name)
    except socket.gaierror as e:
        nxdomain = {socket.EAI_NONAME}
        if hasattr(socket, "EAI_NODATA"):
            nxdomain.add(socket.EAI_NODATA)
        return None if e.errno in nxdomain else UNKNOWN
    except OSError:
        return UNKNOWN


def _is_listing(code) -> bool:
    """A genuine listing return code. 127.0.0.1 (query refused),
    127.255.255.x (Spamhaus: public resolver, abuse) and UNKNOWN are not."""
    return bool(code) and code != UNKNOWN and code.startswith("127.") \
        and code != "127.0.0.1" and not code.startswith("127.255.255.")


def domain_listings(domains, resolve=_resolve) -> dict:
    """{list: {"usable": bool, "listed": {domain: code}, "checked": [domains
    with a definite answer]}}."""
    out = {}
    for name, (zone, test) in DOMAIN_LISTS.items():
        usable = _is_listing(resolve(f"{test}.{zone}")) and \
            resolve(f"{CLEAN_CONTROL}.{zone}") is None
        listed, checked = {}, []
        if usable:
            for d in sorted(set(domains)):
                code = resolve(f"{d}.{zone}")
                if code == UNKNOWN:
                    continue
                checked.append(d)
                if _is_listing(code):
                    listed[d] = code
        out[name] = {"usable": usable, "listed": listed, "checked": checked}
    return out


def listing_label(list_name: str, code: str) -> str:
    if list_name == "SURBL":
        try:
            bits = int(code.rsplit(".", 1)[-1])
        except ValueError:
            return code
        names = [v for k, v in SURBL_BITS.items() if bits & k]
        return f"{code} ({', '.join(names) or '?'})"
    return code


def listing_changes(reference, current) -> list:
    """Entries, exits and code changes since each list's last VERIFIABLE
    reference (the collector carries it from run to run, a "not verifiable"
    day never erases it), over the domains checked on both sides only. The
    CHANGE rings, not the state: otherwise a listed domain would turn the run
    red every single day. The first verifiable check of a list has no
    reference: every domain already listed rings once, then never again."""
    out = []
    for name, cur in (current or {}).items():
        ref = (reference or {}).get(name) or {}
        if not cur.get("usable"):
            continue
        if not ref.get("usable"):
            out += [f"{d} is listed on {name} ({listing_label(name, c)}), first check"
                    for d, c in sorted((cur.get("listed") or {}).items())]
            continue
        was, now_ = ref.get("listed") or {}, cur.get("listed") or {}
        scopes = [set(r["checked"]) for r in (cur, ref) if "checked" in r]
        scope = set.intersection(*scopes) if scopes else set(was) | set(now_)
        out += [f"{d} just entered {name} ({listing_label(name, now_[d])})"
                for d in sorted((set(now_) - set(was)) & scope)]
        out += [f"{d} left {name}" for d in sorted((set(was) - set(now_)) & scope)]
        out += [f"{d}: {name} listing changed ({listing_label(name, was[d])} -> "
                f"{listing_label(name, now_[d])})"
                for d in sorted(set(was) & set(now_) & scope) if was[d] != now_[d]]
    return out


def advance_reference(reference, current, stamp: str) -> dict:
    """The new reference: every list verifiable today replaces its own; a
    non-verifiable list keeps its last known reference."""
    ref = dict(reference or {})
    for name, cur in (current or {}).items():
        if cur.get("usable"):
            ref[name] = dict(cur, checked_at=stamp)
    return ref


def business_hours_between(start, end) -> float:
    """Hours elapsed between two instants, Saturdays and Sundays excluded:
    lemwarm sends no warmup on weekends (verified live, 16/16 mailboxes).
    Without this exclusion, every Monday afternoon flagged all healthy
    mailboxes as "warmup stopped for more than 48h"."""
    if not start or not end or end <= start:
        return 0.0
    hours, cur = 0.0, start
    while cur < end:
        midnight = (cur + timedelta(days=1)).replace(hour=0, minute=0,
                                                     second=0, microsecond=0)
        step = min(end, midnight)
        if cur.weekday() < 5:   # Monday=0 … Friday=4
            hours += (step - cur).total_seconds() / 3600
        cur = step
    return hours


# ── Stuck outgoing warmup ────────────────────────────────────────────────────
def frozen_boxes(boxes, ref) -> dict:
    """{usm_id: warmNextEmailAt} of mailboxes whose outgoing warmup is stuck:
    lemwarm active and next send overdue by more than 24 business hours at
    time `ref`."""
    out = {}
    for mid, b in (boxes or {}).items():
        nxt = parse_ts(b.get("warm_next_email_at"))
        if b.get("lemwarm_active") and nxt and ref and \
                business_hours_between(nxt, ref) > WARM_NEXT_OVERDUE_H:
            out[mid] = b.get("warm_next_email_at")
    return out


def warmup_changes(previous: dict, boxes: dict, stamp: str) -> list:
    """Outgoing-warmup stalls and resumptions since the previous collection.
    The CHANGE rings, not the state: a mailbox stuck for days stays in the
    digest without turning the run red every morning. A mailbox unreadable
    today is never declared "resumed"."""
    old = (previous or {}).get("boxes") or {}
    before = frozen_boxes(old, parse_ts((previous or {}).get("fetched_at")))
    now_ = frozen_boxes(boxes, parse_ts(stamp))
    email = {m: (boxes.get(m) or old.get(m) or {}).get("email", m) for m in set(before) | set(now_)}
    out = [f"{email[m]}: outgoing warmup stuck, the send scheduled for {now_[m]} never left"
           for m in sorted(set(now_) - set(before), key=email.get)]
    out += [f"{email[m]}: outgoing warmup resumed"
            for m in sorted(set(before) - set(now_), key=email.get)
            if m in boxes and not boxes[m].get("error")]
    return out


# ── Anomaly checks ───────────────────────────────────────────────────────────
def anomalies(latest: dict, history: list, now: datetime) -> list:
    """What must turn the run red: breakage and acute incidents. A lasting
    state (a RESTING or QUARANTINE mailbox, an already-listed domain) belongs
    to the digest, not here. Per-mailbox delays are measured at collection
    time: a missed collection is flagged once, as itself."""
    out = []
    fetched = parse_ts(latest.get("fetched_at"))
    ref = fetched or now
    if not fetched or now - fetched > timedelta(hours=COLLECT_STALE_H):
        out.append("collection stale: last run "
                   f"{latest.get('fetched_at') or 'never'}")
    out += [f"invalid response: {e}" for e in latest.get("errors") or []]
    out += [f"domain blocklist: {c}" for c in latest.get("listing_changes") or []]
    out += [f"warmup: {c}" for c in latest.get("warmup_changes") or []]
    boxes = latest.get("boxes") or {}
    cutoff = now - timedelta(days=WINDOW_DAYS)
    tracked = {}
    for r in history:
        t = parse_ts(r.get("collected_at"))
        if t and t > cutoff:
            tracked[r["usm_id"]] = r.get("email", r["usm_id"])
    for mid, email in sorted(tracked.items(), key=lambda kv: kv[1]):
        if mid not in boxes:
            out.append(f"{email}: missing from lemlist while tracked over the "
                       f"last {WINDOW_DAYS} days")
    for mid, b in sorted(boxes.items(), key=lambda kv: kv[1].get("email", "")):
        if b.get("error"):
            continue  # already surfaced in errors
        e = b.get("email", mid)
        if b.get("status") != "OK":
            out.append(f"{e}: status {b.get('status') or 'empty'} "
                       "(mailbox disconnected?)")
        warm = parse_ts(b.get("last_warm_at"))
        if b.get("lemwarm_active") and (
                not warm or business_hours_between(warm, ref) > WARM_STALE_H):
            out.append(f"{e}: no warmup email since "
                       f"{b.get('last_warm_at') or 'ever'} (weekends excluded)")
        calc = parse_ts(b.get("last_at"))
        if not calc or ref - calc > timedelta(hours=SCORE_FROZEN_H):
            out.append(f"{e}: score not recomputed since "
                       f"{b.get('last_at') or 'ever'}")
        spam = parse_ts(b.get("spam_alert"))
        if spam and now - spam < timedelta(hours=SPAM_ALERT_FRESH_H):
            out.append(f"{e}: lemwarm spam alert on {b.get('spam_alert')}")
    return out
