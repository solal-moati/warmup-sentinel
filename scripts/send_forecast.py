#!/usr/bin/env python3
"""Send forecast for the coming days (read-only): follow-ups already
committed per mailbox and per day, room left for new leads, and a
steady-rhythm plan.

The cruise-speed figure (~3.5 emails per lead) ignores the follow-ups
already scheduled: the morning after a large push it is wrong. This script
reads what is really committed.

What lemlist exposes (verified live, Sept 2026):
  · GET campaigns/{id}/sequences: the steps (type email / conditional,
    delay) and the branches (parentId, conditionalStepId);
  · GET campaigns/{id}/schedules: sending days (isoweekday), window,
    timezone;
  · activities emailsSent: per lead, the date, the step (stepId,
    sequenceId, sequenceStep) and the sending mailbox.
Delay rule VERIFIED on 1,011 real follow-ups: `delay` counts the schedule's
BUSINESS days after the previous send (96% exact; calendar days: 45%). The
script re-runs that check on your own past sends every time, prints the
result, and withholds the plan below RELIABLE_MIN.

Assumptions, written down rather than hidden:
  · a lead keeps going through its sequence (no reply): a high forecast,
    which is what you want to never exceed the cap;
  · an inProgress lead never sent goes out at the next slot, split evenly
    between the campaign's senders;
  · new leads split evenly between eligible mailboxes (lemlist picks the
    sender at random);
  · per-mailbox cap = BOX_DAILY_CAP minus warmup; a RECOVERING mailbox takes
    half of it in new leads; RESTING and QUARANTINE take none (their
    committed follow-ups still go out).

Usage:
  python3 send_forecast.py [--days 5] [--sequence 0,3,5,7] [--json]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from lib import env, warmup

try:
    from zoneinfo import ZoneInfo
except Exception:  # no timezone database: dates stay in UTC
    ZoneInfo = None

DEFAULT_WEEKDAYS = {1, 2, 3, 4, 5}
DEFAULT_SEQUENCE = (0, 3, 5, 7)   # delays between emails of the campaign to come
HORIZON_DAYS = 35                 # calendar days covered by committed follow-ups
RELIABLE_MIN = 0.85               # below this share of exact predictions, no plan


def tz_of(name: str):
    if ZoneInfo and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


# ── Calendar ─────────────────────────────────────────────────────────────────
def add_business_days(day: date, n: int, weekdays=frozenset(DEFAULT_WEEKDAYS)) -> date:
    """Send day of a step with delay `n` after a send on `day`: `n` business
    days of the schedule, then the first business day (a delay landing on a
    weekend goes out on Monday). Rule verified on real sends."""
    d = day
    while n > 0:
        d += timedelta(days=1)
        if d.isoweekday() in weekdays:
            n -= 1
    while d.isoweekday() not in weekdays:
        d += timedelta(days=1)
    return d


def business_days(start: date, count: int, weekdays=frozenset(DEFAULT_WEEKDAYS)) -> list:
    """The next `count` business days from `start` included."""
    out, d = [], start
    while len(out) < count:
        if d.isoweekday() in weekdays:
            out.append(d)
        d += timedelta(days=1)
    return out


# ── Sequences ────────────────────────────────────────────────────────────────
def _steps(seq: dict) -> list:
    return sorted(seq.get("steps") or [], key=lambda s: s.get("sequenceStep", 0))


def next_email_delay(sequences: dict, sequence_id: str, step: int, acc: int = 0):
    """Delay (business days) to the next email after step `step` of
    `sequence_id`, or None at the end of the sequence. A non-email step
    (LinkedIn, call...) adds its delay; a conditional step follows its first
    branch that holds an email (the "no reply" assumption)."""
    seq = sequences.get(sequence_id) or {}
    for st in _steps(seq):
        if st.get("sequenceStep", 0) <= step:
            continue
        kind, delay = st.get("type"), int(st.get("delay") or 0)
        if kind == "email":
            return acc + delay
        if kind == "conditional":
            for child in sequences.values():
                if child.get("conditionalStepId") == st.get("_id"):
                    found = next_email_delay(sequences, child["_id"], -1, acc + delay)
                    if found is not None:
                        return found
            return None
        acc += delay
    return None


def _advance(sequences: dict, sequence_id: str, step: int):
    """(step, sequence_id) of the next email step reached after `step`."""
    seq = sequences.get(sequence_id) or {}
    for st in _steps(seq):
        if st.get("sequenceStep", 0) <= step:
            continue
        if st.get("type") == "email":
            return st.get("sequenceStep", 0), sequence_id
        if st.get("type") == "conditional":
            for child in sequences.values():
                if child.get("conditionalStepId") == st.get("_id"):
                    pos = _advance(sequences, child["_id"], -1)
                    if pos[1] is not None:
                        return pos
            return step, None
    return step, None


def email_days(sequences: dict, sequence_id: str, step: int, last_day: date,
               weekdays, until: date) -> list:
    """Send days of a lead's next emails, up to `until`."""
    out, day, seq_id = [], last_day, sequence_id
    while True:
        delay = next_email_delay(sequences, seq_id, step)
        if delay is None:
            return out
        day = add_business_days(day, delay, weekdays)
        if day > until:
            return out
        out.append(day)
        step, seq_id = _advance(sequences, seq_id, step)
        if seq_id is None:
            return out


# ── Committed follow-ups and room left ───────────────────────────────────────
def committed_sends(leads: list, start: date, until: date) -> dict:
    """{(mailbox, day): emails} of the follow-ups already committed. `leads`:
    [{sequences, weekdays, sequence_id, step, last_day, sender, senders,
    first_day}]; a lead never sent (last_day None) goes out on `first_day`,
    split over `senders`. An overdue send (planned day in the past) goes out
    at the first slot, `start`."""
    out = defaultdict(float)
    for lead in leads:
        wd = lead.get("weekdays") or DEFAULT_WEEKDAYS
        if lead.get("last_day") is None:
            senders = lead.get("senders") or []
            if not senders:
                continue
            first = max(lead["first_day"], start)
            days = [first] + email_days(lead["sequences"], lead["sequence_id"], 0, first, wd, until)
            for d in days:
                for s in senders:
                    out[(s, d)] += 1 / len(senders)
            continue
        days = email_days(lead["sequences"], lead["sequence_id"], lead["step"],
                          lead["last_day"], wd, until)
        for d in days:
            out[(lead["sender"], max(d, start))] += 1
    return dict(out)


def room_by_day(committed: dict, boxes: list, days: list) -> dict:
    """{day: {room, committed, committed_resting, per_box}}: the room for NEW
    leads = per mailbox, min(allowed share, cap minus committed follow-ups),
    never negative. `boxes`: [{email, state, warm_email_max}]."""
    out = {}
    for d in days:
        per_box, room, total, resting = {}, 0.0, 0.0, 0.0
        for b in boxes:
            cap = max(warmup.BOX_DAILY_CAP - (warmup.to_float(b.get("warm_email_max")) or 0), 0)
            share = warmup.NEW_LEAD_SHARE.get(b.get("state"), 0.0)
            busy = committed.get((b["email"], d), 0.0)
            free = max(0.0, min(cap * share, cap - busy))
            per_box[b["email"]] = {"committed": busy, "room": free, "state": b.get("state")}
            room += free
            total += busy
            if not share:
                resting += busy
        out[d] = {"room": room, "committed": total, "committed_resting": resting,
                  "per_box": per_box}
    return out


def touch_days(day: date, sequence, weekdays) -> list:
    """Send days of a lead pushed on `day` (day 0, then each follow-up,
    each counted in business days from the previous one)."""
    out = []
    for delay in sequence:
        day = add_business_days(day, delay, weekdays)
        out.append(day)
    return out


def even_plan(room: dict, days: list, sequence=DEFAULT_SEQUENCE,
              weekdays=frozenset(DEFAULT_WEEKDAYS)) -> tuple:
    """(n per day, {day: n}, tightest day): the largest UNIFORM number of new
    leads per day such that no day of the horizon exceeds its room,
    follow-ups included. The rhythm is assumed SUSTAINED over the whole
    horizon (the following weeks too), not only over `days`: otherwise the
    plan would exploit an empty week by overloading the next one. A steady
    rhythm, not a maximum: filling the mailboxes to the brim for three days
    then leaving them empty would be worse for reputation."""
    rhythm = sorted(room)
    touches = {d: touch_days(d, sequence, weekdays) for d in rhythm}

    def left_after(n: int) -> dict:
        left = {d: v["room"] for d, v in room.items()}
        for d in rhythm:
            for t in touches[d]:
                if t in left:
                    left[t] -= n
        return left

    lo, hi = 0, int(max((v["room"] for v in room.values()), default=0))
    while lo < hi:                       # bisection: feasible(n) is monotonic
        mid = (lo + hi + 1) // 2
        if min(left_after(mid).values(), default=0) >= 0:
            lo = mid
        else:
            hi = mid - 1
    left = left_after(lo)
    touched = sorted({t for ts in touches.values() for t in ts if t in left})
    binding = min(touched, key=lambda t: left[t]) if touched else None   # ties: the nearest
    return lo, {d: lo for d in days}, binding


# ── Collection (read-only) ───────────────────────────────────────────────────
def _hhmm(value: str, default: int) -> int:
    try:
        return int(str(value).split(":")[0])
    except (TypeError, ValueError):
        return default


def collect(client, now: datetime) -> tuple:
    """(leads, backtest, tz) from lemlist. `backtest` = (pairs, exact) of the
    delay rule over the follow-ups already sent; `tz` = the schedules'
    timezone (first running campaign)."""
    running = {}
    for user in client.get_senders():
        for c in user.get("campaigns") or []:
            if c.get("status") == "running":
                running[c["_id"]] = c.get("name") or c["_id"]
    leads, pairs, exact, tz = [], 0, 0, None
    for cid, name in sorted(running.items(), key=lambda kv: kv[1]):
        _, seqs = client._request("GET", f"campaigns/{cid}/sequences")
        _, sch = client._request("GET", f"campaigns/{cid}/schedules")
        camp = client.get_campaign(cid)
        if not warmup.is_valid(seqs) or not isinstance(sch, list):
            print(f"  ! {name[:40]}: sequences or schedule unreadable, campaign skipped")
            continue
        sch0 = sch[0] if sch else {}
        ctz = tz_of(sch0.get("timezone"))
        tz = tz or ctz
        local = now.astimezone(ctz)
        weekdays = set(sch0.get("weekdays") or DEFAULT_WEEKDAYS)
        end_hour = _hhmm(sch0.get("end"), 18)
        first_day = local.date() if local.hour < end_hour else local.date() + timedelta(days=1)
        first_day = add_business_days(first_day, 0, weekdays)
        senders = [s for s in ((x.get("email") or "").lower() for x in camp.get("senders") or []) if s]
        _, export = client._request("GET", f"campaigns/{cid}/export/leads",
                                    params={"state": "all"})
        rows = list(csv.DictReader(io.StringIO(export.get("raw", "") if isinstance(export, dict) else "")))
        active = {(r.get("email") or "").lower() for r in rows if r.get("status") == "inProgress"}
        acts, offset = [], 0
        while True:
            batch = client.get_activities(cid, "emailsSent", limit=100, offset=offset)
            acts += batch
            if len(batch) < 100:
                break
            offset += 100
        delay_of = {st["_id"]: int(st.get("delay") or 0)
                    for s in seqs.values() for st in s.get("steps") or []}
        by_lead = defaultdict(list)
        for a in acts:
            by_lead[(a.get("leadEmail") or "").lower()].append(a)
        for email, xs in by_lead.items():
            xs.sort(key=lambda a: a["createdAt"])
            for prev, nxt in zip(xs, xs[1:]):   # delay-rule check on real sends
                d = delay_of.get(nxt.get("stepId"))
                if d is None:
                    continue
                p = warmup.parse_ts(prev["createdAt"]).astimezone(ctz).date()
                n = warmup.parse_ts(nxt["createdAt"]).astimezone(ctz).date()
                pairs += 1
                exact += add_business_days(p, d, weekdays) == n
            if email not in active:
                continue
            last = xs[-1]
            leads.append({
                "campaign": name, "sequences": seqs, "weekdays": weekdays,
                "sequence_id": last.get("sequenceId"), "step": last.get("sequenceStep", 0),
                "last_day": warmup.parse_ts(last["createdAt"]).astimezone(ctz).date(),
                "sender": (last.get("sendUserEmail") or "").lower(), "senders": senders,
                "first_day": first_day,
            })
        main_seq = next((sid for sid, s in seqs.items() if not s.get("parentId")), None)
        for email in active - set(by_lead):     # inProgress, never sent
            leads.append({"campaign": name, "sequences": seqs, "weekdays": weekdays,
                          "sequence_id": main_seq, "step": 0, "last_day": None,
                          "sender": "", "senders": senders, "first_day": first_day})
    return leads, (pairs, exact), tz or timezone.utc


def box_states(now: datetime) -> list:
    history, latest = warmup.read_history(), warmup.read_latest()
    boxes = []
    for mid, b in (latest.get("boxes") or {}).items():
        if b.get("error"):
            continue
        boxes.append({"email": b.get("email"), "warm_email_max": b.get("warm_email_max"),
                      "state": warmup.classify_box(history, mid, now)["state"]})
    return boxes


# ── Rendering ────────────────────────────────────────────────────────────────
def fmt_day(d: date) -> str:
    return d.strftime("%a %d/%m")


def render(days: list, horizon: list, room: dict, plan: dict, per_day: int, binding,
           boxes: list, sequence, backtest, n_leads: int, now: datetime, tz) -> str:
    pairs, exact = backtest
    share = exact / pairs if pairs else 0.0
    seq_txt = "D" + "".join(f"/+{d}" for d in sequence[1:])
    out = [f"# Send forecast · {now.astimezone(tz):%Y-%m-%d %H:%M} ({getattr(tz, 'key', 'UTC')})", "",
           f"{n_leads} leads in sequence across the running campaigns · delay rule "
           f"(business days) checked on {pairs} past follow-ups: {100 * share:.0f}% exact."]
    if pairs and share < RELIABLE_MIN:
        out += ["", f"**Forecast not reliable on this account** (under {100 * RELIABLE_MIN:.0f}% "
                "exact): the committed follow-ups below are indicative, no plan is given."]
    out += ["", f"| Day | Committed follow-ups | of which resting mailboxes | Room for new leads | Plan ({seq_txt}) |",
            "|---|---|---|---|---|"]
    for d in days:
        r = room[d]
        cell = "n/a" if pairs and share < RELIABLE_MIN else f"**{plan[d]}**"
        out.append(f"| {fmt_day(d)} | {r['committed']:.0f} | {r['committed_resting']:.0f} | "
                   f"{r['room']:.0f} | {cell} |")
    later = [(d, room[d]["committed"]) for d in horizon if d not in days and room[d]["committed"] >= 1]
    if later:
        out += ["", "Already committed afterwards: "
                + " · ".join(f"{fmt_day(d)} {c:.0f}" for d, c in later) + "."]
    if not (pairs and share < RELIABLE_MIN):
        why = ""
        if binding is not None:
            why = (f" What binds: {fmt_day(binding)}, {room[binding]['committed']:.0f} follow-up(s) "
                   "already committed plus the plan's own.")
        out += ["", f"Plan: **{per_day} new leads per day, {sum(plan.values())} in total** over "
                f"{len(days)} business days, at a steady rhythm, follow-ups included.{why}"]
    out += ["", "Committed follow-ups per mailbox (rotation state, then emails per day):", "",
            "| Mailbox | State | " + " | ".join(fmt_day(d) for d in days) + " |",
            "|---|---|" + "---|" * len(days)]
    order = {"ACTIVE": 0, "RECOVERING": 1, "RESTING": 2, "QUARANTINE": 3}
    for b in sorted(boxes, key=lambda b: (order.get(b["state"], 9), b["email"])):
        cells = " | ".join(f"{room[d]['per_box'].get(b['email'], {}).get('committed', 0):.0f}"
                           for d in days)
        out.append(f"| {b['email']} | {b['state']} | {cells} |")
    out += ["", "_Read-only: no action taken, no setting touched._"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="business days to plan")
    ap.add_argument("--sequence", default=",".join(map(str, DEFAULT_SEQUENCE)),
                    help="delays between the emails of the campaign to come (e.g. 0,3,5,7)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()
    sequence = tuple(int(x) for x in args.sequence.split(",") if x.strip())

    now = warmup.now_utc()
    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"))
    boxes = box_states(now)
    if not boxes:
        print("data/latest.json missing or empty: run sentinel_collect.py first")
        return 1
    leads, backtest, tz = collect(client, now)
    local = now.astimezone(tz)
    # first slot: today before 18:00 (lemlist window), otherwise the next business day
    start = add_business_days(local.date() if local.hour < 18 else local.date() + timedelta(days=1), 0)
    until = start + timedelta(days=HORIZON_DAYS)
    days = business_days(start, args.days)
    # the room must be known up to the last follow-up of the plan's leads
    horizon = business_days(start, args.days + sum(sequence) + 1)
    committed = committed_sends(leads, start, until)
    room = room_by_day(committed, boxes, horizon)
    per_day, plan, binding = even_plan(room, days, sequence)
    pairs, exact = backtest
    reliable = not pairs or exact / pairs >= RELIABLE_MIN
    if args.json:
        print(json.dumps({"generated_at": warmup.iso(now), "reliable": reliable,
                          "per_day": per_day if reliable else None,
                          "binding": binding.isoformat() if binding and reliable else None,
                          "days": [{"day": d.isoformat(), "committed": room[d]["committed"],
                                    "room": room[d]["room"],
                                    "plan": plan[d] if reliable else None} for d in days],
                          "backtest": {"pairs": pairs, "exact": exact}},
                         ensure_ascii=False, indent=1))
    else:
        print(render(days, horizon, room, plan, per_day, binding, boxes, sequence,
                     backtest, len(leads), now, tz))
    return 0


if __name__ == "__main__":
    sys.exit(main())
