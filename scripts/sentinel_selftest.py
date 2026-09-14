#!/usr/bin/env python3
"""Selftest of warmup-sentinel's guardrails: pure logic, no network calls.

Fixtures come from two real readings on a 16-mailbox account (anonymized)
plus the edge cases. Runs in a second. In the workflow it runs AFTER the
commit: a red test never loses the day's collection.

Usage:
  python3 sentinel_selftest.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta

from lib import warmup

NOW = warmup.parse_ts("2026-09-10T21:00:00Z")


def ago(hours: float) -> str:
    return warmup.iso(NOW - timedelta(hours=hours))


# Real readings (score, inbox %) from two consecutive days, anonymized. A
# single value when lemlist did not recompute the score in between (dedupe
# on lastAt).
REAL = {
    "alex@getnorthwind.com": [(85, 88), (87, 89)],
    "sam.miller@getnorthwind.com": [(82, 79), (80, 67)],
    "sam@getnorthwind.com": [(83, 81), (82, 75)],
    "alex@joinnorthwind.com": [(85, 88), (85, 83)],
    "sam@joinnorthwind.com": [(81, 72), (79, 70)],
    "alex@northwindapp.com": [(85, 88), (85, 83)],
    "s.miller@northwindapp.com": [(79, 82), (78, 80)],
    "sam@northwindapp.com": [(78, 73)],
    "alex@northwindhq.com": [(84, 87), (83, 81)],
    "sam@northwindhq.com": [(83, 87), (84, 82)],
    "a.miller@trynorthwind.com": [(85, 93), (84, 87)],
    "alex@trynorthwind.com": [(83, 81), (84, 82)],
    "sam@trynorthwind.com": [(82, 86), (83, 86)],
    "alex.miller@usenorthwind.com": [(83, 76), (83, 76)],
    "alex@usenorthwind.com": [(78, 62), (80, 72)],
    "sam@usenorthwind.com": [(78, 80)],
}

# The real shape of GET /lemwarm/{id}/settings (truncated)
SETTINGS = {
    "active": True, "warmEmailMax": 14, "lastWarmAt": "2026-09-10T17:03:23.780Z",
    "spamAlert": None, "lastBounced": {"date": "2026-07-31T15:00:00.000Z"},
    "deliverability": {
        "score": 78, "lastAt": "2026-09-09T14:03:56.069Z",
        "details": [
            {"mx": {"ok": True}, "dnsScore": 100},
            {"inbox": 82, "total": 112, "percent": 73},
            {"mailboxAge": 30},
            {"dnsNormal": 40, "inboxStatsNormal": 8, "totalEmailSentNormal": 37.3,
             "ageNormal": 10, "totalEmailSent": 112,
             "blacklistsInfo": {"score": 100, "blacklistedIn": []}},
        ],
    },
}
BOX = {"email": "sam@northwindapp.com", "domain": "northwindapp.com",
       "user_id": "usr_x", "status": "OK", "email_limit": 40}

CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def row(usm, last_at, score=80, inbox=80):
    return {"usm_id": usm, "email": f"{usm}@x.com", "last_at": last_at,
            "collected_at": last_at, "score": str(score), "inbox_pct": str(inbox)}


# ── 1. Nothing goes out by mistake ───────────────────────────────────────────
class _Resp:
    status_code, text, headers = 200, '{"ok": true}', {}

    def json(self):
        return {"ok": True}


@case("read-only: a POST raises before the network, a GET goes through")
def t_read_only():
    client = warmup.WarmupClient("fake-key")
    calls = []
    client.session.request = lambda method, url, **kw: calls.append(method) or _Resp()
    try:
        client._request("POST", "deliverability/alerts", json={})
    except warmup.ReadOnlyViolation:
        pass
    else:
        raise AssertionError("the POST should have been refused")
    assert calls == [], f"a request went out despite the refusal: {calls}"
    client._request("GET", "team")
    assert calls == ["GET"], calls


# ── 2. The history never lies ────────────────────────────────────────────────
@case("guard: HTML shell served with a 200 (raw key) rejected")
def t_guard_html():
    assert not warmup.is_valid({"raw": "<!DOCTYPE html>"}, "deliverability")


@case("guard: empty dict served with a 200 (users/<unknown id>) rejected")
def t_guard_empty():
    assert not warmup.is_valid({}, "mailboxes")


@case("guard: required key checked, list refused")
def t_guard_shape():
    assert warmup.is_valid({"mailboxes": []}, "mailboxes")
    assert not warmup.is_valid({"error": "User mailbox not found"}, "deliverability")
    assert not warmup.is_valid([{"a": 1}])


@case("parsing: the real settings shape")
def t_parse():
    r = warmup.parse_settings(BOX, "usm_x", SETTINGS, ago(0))
    assert (r["score"], r["inbox"], r["total"], r["inbox_pct"]) == (78, 82, 112, 73), r
    assert (r["dns_score"], r["blacklisted"], r["mailbox_age"], r["total_sent"]) == (100, 0, 30, 112), r
    assert r["last_bounced"] == "2026-07-31T15:00:00.000Z" and r["spam_alert"] == "", r
    assert r["last_at"] == "2026-09-09T14:03:56.069Z", r


@case("parsing: the order of the details blocks is not guaranteed")
def t_parse_order():
    dl = dict(SETTINGS["deliverability"], details=SETTINGS["deliverability"]["details"][::-1])
    r = warmup.parse_settings(BOX, "usm_x", dict(SETTINGS, deliverability=dl), ago(0))
    assert (r["inbox_pct"], r["dns_score"], r["mailbox_age"]) == (73, 100, 30), r


@case("dedupe: same lastAt, no row (the real not-recomputed case)")
def t_dedup_same():
    hist = [row("usm_a", "2026-09-09T14:03:56.069Z")]
    assert warmup.new_rows([row("usm_a", "2026-09-09T14:03:56.069Z")], hist) == []


@case("dedupe: newer lastAt, one row, per mailbox")
def t_dedup_new():
    hist = [row("usm_a", "2026-09-08T16:57:48.589Z"), row("usm_b", "2026-09-09T14:03:59.420Z")]
    fresh = warmup.new_rows([row("usm_a", "2026-09-10T16:57:00.000Z"),
                             row("usm_b", "2026-09-09T14:03:59.420Z")], hist)
    assert [r["usm_id"] for r in fresh] == ["usm_a"], fresh


@case("dedupe: score never computed (empty lastAt), no row")
def t_dedup_empty():
    assert warmup.new_rows([row("usm_c", "")], []) == []


@case("classification: thresholds 84 / 80 / 70 and the blacklist veto")
def t_bounds():
    c = warmup.classify
    assert c([85, 84], [88, 87])[0] == "ACTIVE"
    assert c([84, 84], [80, 80])[0] == "ACTIVE"
    assert c([83, 84], [81, 82])[0] == "RECOVERING"
    assert c([80, 80], [75, 75])[0] == "RECOVERING"
    assert c([81, 79], [72, 70])[0] == "RESTING"
    assert c([85, 85], [69, 69])[0] == "QUARANTINE"
    assert c([85], [70])[0] == "ACTIVE"
    assert c([87], [89], blacklisted=True)[0] == "QUARANTINE"
    assert c([], [])[0] == "UNKNOWN"


@case("classification: one score below 80 keeps the mailbox RESTING for 7 days")
def t_resting_sticky():
    assert warmup.classify([79, 84, 84, 85, 85, 86], [80] * 6)[0] == "RESTING"
    assert warmup.classify([84, 84, 85, 85, 86, 86, 86], [80] * 7)[0] == "ACTIVE"


@case("real noise: day to day, 6 mailboxes out of 16 flip state in 24h")
def t_flapping():
    def day(score, inbox):
        return warmup.classify([score], [inbox])[0]
    changed = sum(1 for pts in REAL.values() if len(pts) == 2 and day(*pts[0]) != day(*pts[1]))
    assert changed == 6, changed


@case("real two-day ranking: 4 ACTIVE, 7 RECOVERING, 4 RESTING, 1 QUARANTINE")
def t_real():
    hist = []
    for i, (email, points) in enumerate(sorted(REAL.items())):
        for d, (score, inbox) in enumerate(points):
            r = row(f"usm_{i}", f"2026-09-{9 + d:02d}T12:00:00Z", score, inbox)
            r["email"] = email
            hist.append(r)
    states = {r["email"]: warmup.classify_box(hist, r["usm_id"], NOW)["state"] for r in hist}
    got = dict(Counter(states.values()))
    assert got == {"ACTIVE": 4, "RECOVERING": 7, "RESTING": 4, "QUARANTINE": 1}, got
    assert states["alex@usenorthwind.com"] == "QUARANTINE"
    assert states["sam@joinnorthwind.com"] == "RESTING"


@case("capacity: 50 cap warmup included, ACTIVE full, RECOVERING half, others none")
def t_capacity():
    boxes = ([{"email": f"a{i}", "state": "ACTIVE", "warm_email_max": 14} for i in range(4)]
             + [{"email": f"r{i}", "state": "RECOVERING", "warm_email_max": 14} for i in range(7)]
             + [{"email": f"s{i}", "state": "RESTING", "warm_email_max": 14} for i in range(4)]
             + [{"email": "q", "state": "QUARANTINE", "warm_email_max": 14}])
    cap = warmup.capacity(boxes)
    assert round(cap["emails"]) == 270, cap["emails"]   # 4 × 36 + 7 × 18
    assert round(cap["leads"]) == 77, cap["leads"]      # 270 ÷ 3.5
    assert all(b["emails"] == 0 for b in cap["boxes"]
               if b["state"] in ("RESTING", "QUARANTINE"))


# ── 3. A breakage never goes unnoticed ───────────────────────────────────────
def latest(**over):
    box = {"email": "sam@getnorthwind.com", "status": "OK", "lemwarm_active": True,
           "last_warm_at": ago(1), "last_at": ago(5), "spam_alert": ""}
    box.update(over)
    return {"fetched_at": ago(0), "errors": [], "boxes": {"usm_a": box}}


def hits(state, word, history=()):
    return [a for a in warmup.anomalies(state, list(history), NOW) if word in a]


@case("checks: healthy state, no anomaly")
def t_ok():
    assert warmup.anomalies(latest(), [], NOW) == []


@case("checks: disconnected mailbox (status != OK)")
def t_status():
    assert hits(latest(status="ERROR"), "status")


@case("checks: warmup stopped for more than 48h, unless paused on purpose")
def t_warm():
    assert hits(latest(last_warm_at=ago(49)), "warmup")
    assert warmup.anomalies(latest(last_warm_at=ago(49), lemwarm_active=False), [], NOW) == []


@case("checks: score not recomputed for more than 3 days")
def t_frozen():
    assert hits(latest(last_at=ago(80)), "recomputed")


@case("checks: recent lemwarm spam alert flagged, the old one not")
def t_spam():
    assert hits(latest(spam_alert=ago(10)), "spam")
    assert warmup.anomalies(latest(spam_alert=ago(24 * 9)), [], NOW) == []


@case("checks: mailbox gone from lemlist")
def t_gone():
    history = [{"usm_id": "usm_b", "email": "b@x.com", "collected_at": ago(24), "last_at": ago(24)}]
    assert hits(latest(), "missing", history)


@case("checks: invalid response and stale collection")
def t_errors():
    state = latest()
    state["errors"] = ["x@y.com: lemwarm/usm_x/settings unusable"]
    assert hits(state, "invalid")
    state = latest()
    state["fetched_at"] = ago(40)
    assert hits(state, "stale")


@case("stuck warmup: spotted at 24+ business hours overdue, not when paused on purpose")
def t_frozen_next():
    boxes = latest(warm_next_email_at=ago(30))["boxes"]
    assert list(warmup.frozen_boxes(boxes, NOW)) == ["usm_a"]
    assert warmup.frozen_boxes(latest(warm_next_email_at=ago(-1))["boxes"], NOW) == {}  # due in 1h
    assert warmup.frozen_boxes(latest(warm_next_email_at=ago(30), lemwarm_active=False)["boxes"], NOW) == {}


@case("stuck warmup: rings on stall and on resumption, never daily for a known state")
def t_frozen_changes():
    ok, stuck = latest(warm_next_email_at=ago(-1)), latest(warm_next_email_at=ago(30))
    new = warmup.warmup_changes(ok, stuck["boxes"], ago(0))
    assert len(new) == 1 and "stuck" in new[0], new
    assert warmup.warmup_changes(stuck, stuck["boxes"], ago(0)) == []          # already known
    back = warmup.warmup_changes(stuck, ok["boxes"], ago(0))
    assert len(back) == 1 and "resumed" in back[0], back
    unreadable = {"usm_a": {"email": "sam@getnorthwind.com", "error": "unusable response"}}
    assert warmup.warmup_changes(stuck, unreadable, ago(0)) == []              # never "resumed" wrongly
    assert warmup.anomalies(stuck, [], NOW) == []                               # the state alone rings no more
    stuck["warmup_changes"] = new
    assert hits(stuck, "stuck")


@case("checks: a missed collection is flagged once, as itself")
def t_stale_once():
    state = latest(last_warm_at=ago(41), last_at=ago(45))
    state["fetched_at"] = ago(40)
    found = warmup.anomalies(state, [], NOW)
    assert len(found) == 1 and "stale" in found[0], found


@case("weekend: lemwarm sends nothing on Saturday-Sunday, Monday never rings wrongly")
def t_weekend_hours():
    monday = warmup.parse_ts("2026-09-14T15:00:00Z")
    friday_night = warmup.parse_ts("2026-09-11T23:00:00Z")
    prior_tuesday = warmup.parse_ts("2026-09-08T12:00:00Z")
    h = warmup.business_hours_between(friday_night, monday)
    assert 15 < h < 17, h          # 1h of Friday + 15h of Monday: under 48h
    assert warmup.business_hours_between(prior_tuesday, monday) > 48
    # Monday reading: last warmup Friday night = healthy; prior Tuesday = anomaly
    state = latest(last_warm_at="2026-09-11T23:00:00Z", last_at="2026-09-12T05:00:00Z")
    state["fetched_at"] = "2026-09-14T15:00:00Z"
    assert warmup.anomalies(state, [], monday) == []
    state["boxes"]["usm_a"]["last_warm_at"] = "2026-09-08T12:00:00Z"
    assert [a for a in warmup.anomalies(state, [], monday) if "warmup" in a]


def resolver(table):
    return lambda name: table.get(name)


@case("domain lists: judged only when the test point answers and the control is clean")
def t_listings():
    ok = {"test.surbl.org.multi.surbl.org": "127.0.0.254",
          "getnorthwind.com.multi.surbl.org": "127.0.0.64"}
    r = warmup.domain_listings(["getnorthwind.com", "trynorthwind.com"], resolve=resolver(ok))
    assert r["SURBL"]["usable"] and r["SURBL"]["listed"] == {"getnorthwind.com": "127.0.0.64"}, r
    assert not r["Spamhaus DBL"]["usable"] and r["Spamhaus DBL"]["listed"] == {}
    blocked = {n: "127.0.0.1" for n in ("test.surbl.org.multi.surbl.org",
               "example.com.multi.surbl.org", "getnorthwind.com.multi.surbl.org")}
    r = warmup.domain_listings(["getnorthwind.com"], resolve=resolver(blocked))
    assert not r["SURBL"]["usable"] and r["SURBL"]["listed"] == {}, r
    dirty = dict(ok, **{"example.com.multi.surbl.org": "127.0.0.64"})
    assert not warmup.domain_listings(["getnorthwind.com"], resolve=resolver(dirty))["SURBL"]["usable"]
    assert warmup.listing_label("SURBL", "127.0.0.64") == "127.0.0.64 (ABUSE)"


@case("domain lists: an entry or an exit rings, the state does not")
def t_listing_changes():
    before = {"SURBL": {"usable": True, "listed": {"a.com": "127.0.0.64"}}}
    after = {"SURBL": {"usable": True, "listed": {"a.com": "127.0.0.64", "b.com": "127.0.0.64"}}}
    changes = warmup.listing_changes(before, after)
    assert len(changes) == 1 and "b.com" in changes[0], changes
    assert warmup.listing_changes(after, after) == []
    assert "left" in warmup.listing_changes(after, before)[0]
    # first verifiable check (no reference yet): what is already listed rings once
    first = warmup.listing_changes(None, after)
    assert len(first) == 2 and all("first check" in c for c in first), first
    assert warmup.listing_changes({"SURBL": {"usable": False}}, after) == first
    assert warmup.listing_changes(None, {"SURBL": {"usable": False, "listed": {}}}) == []
    state = latest()
    state["listing_changes"] = changes
    assert hits(state, "b.com")
    # a DNS failure on b.com that day: out of scope, no fake delisting
    ref = {"SURBL": dict(after["SURBL"], checked=["a.com", "b.com"])}
    dns_down = {"SURBL": {"usable": True, "listed": {"a.com": "127.0.0.64"}, "checked": ["a.com"]}}
    assert warmup.listing_changes(ref, dns_down) == []
    # a "not verifiable" day never replaces the reference
    kept = warmup.advance_reference(ref, {"SURBL": {"usable": False, "listed": {}}}, ago(0))
    assert kept["SURBL"]["listed"] == ref["SURBL"]["listed"], kept


@case("history: a new column rewrites the header without losing or shifting rows")
def t_header_migration():
    import csv as _csv
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "history.csv"
        old = [f for f in warmup.FIELDS if f != "warm_next_email_at"]
        with path.open("w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=old)
            w.writeheader()
            w.writerow({**{k: "" for k in old}, "usm_id": "usm_a",
                        "last_at": "2026-09-09T00:00:00Z", "score": "80", "spam_alert": "x"})
        new = dict(row("usm_b", "2026-09-10T00:00:00Z"), warm_next_email_at="2026-09-10T01:00:00Z")
        warmup.append_history([new], path=path)
        rows = warmup.read_history(path)
        assert list(rows[0].keys()) == warmup.FIELDS, list(rows[0].keys())
        assert [r["usm_id"] for r in rows] == ["usm_a", "usm_b"], rows
        assert rows[0]["score"] == "80" and rows[0]["spam_alert"] == "x" and rows[0]["warm_next_email_at"] == ""
        assert rows[1]["warm_next_email_at"] == "2026-09-10T01:00:00Z"


# ── 4. The lead guard ────────────────────────────────────────────────────────
@case("lead guard: cap, reasons, already logged, test leads untouchable")
def t_guard_candidates():
    import lead_guard as g
    rows = [
        {"_id": "a", "email": "x@a.com", "status": "inProgress", "emailStatus": "risky", "lastState": "emailsSent"},
        {"_id": "c", "email": "y@a.com", "status": "done", "emailStatus": "undeliverable", "lastState": "emailsSent"},
        {"_id": "d", "email": "z@a.com", "status": "inProgress", "emailStatus": "", "lastState": "emailsBounced"},
        {"_id": "e", "email": "k@a.com", "status": "inProgress", "emailStatus": "undeliverable", "lastState": "emailsSent"},
        {"_id": "f", "email": "m@a.com", "status": "inProgress", "emailStatus": "deliverable", "lastState": "emailsOpened"},
    ]
    got = g.guard_candidates(rows, known_ids={"e"})
    assert [r["_id"] for r in got] == ["a", "d"], got
    assert got[0]["reason"] == "lemlist_risky" and got[1]["reason"] == "bounce", got
    assert [r["_id"] for r in g.guard_candidates(rows, set(), cap=1)] == ["a"]
    assert g.guard_candidates(rows, set(), cap=0) == []
    assert g._fix("JoÃ£o") == "João" and g._fix("João") == "João"


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  OK      {name}")
        except Exception as e:  # a crashing test is a red test
            failed += 1
            print(f"  FAILED  {name}\n          {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
