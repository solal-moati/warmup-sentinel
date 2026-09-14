#!/usr/bin/env python3
"""Deliverability digest: anomalies, rotation ranking, domains, trends.

No network calls: reads data/history.csv and latest.json, writes
data/report.md (and prints it). Never recommends pulling a mailbox out of a
running campaign, since a lead keeps its sender for its whole sequence: a
degraded mailbox still in rotation is flagged, nothing more.

--check: exit code 1 on an anomaly (stale collection, invalid response,
mailbox gone or disconnected, warmup stopped, frozen score, recent spam
alert, outgoing warmup that stalls or resumes, domain entering or leaving a
blocklist). An already-flagged stall or listing does not turn the run red
again. In the
workflow this check runs AFTER the commit: the day is saved even when the
run ends red.

Usage:
  python3 sentinel_report.py [--check] [--fail-for-test]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import timedelta, timezone

from lib import config, warmup

UTC = timezone.utc
ORDER = ["QUARANTINE", "RESTING", "RECOVERING", "ACTIVE", "UNKNOWN"]


def fmt_ts(value) -> str:
    t = warmup.parse_ts(value)
    return t.astimezone(UTC).strftime("%d %b %H:%M") if t else "?"


def fmt(v, digits: int = 0, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v:.{digits}f}{suffix}"


def signed(v) -> str:
    return "n/a" if v is None else f"{v:+.0f}"


def avg(values) -> float | None:
    xs = [v for v in values if v is not None]
    return sum(xs) / len(xs) if xs else None


def box_rows(history: list, boxes: dict, now) -> list:
    rows = []
    for mid, b in boxes.items():
        c = warmup.classify_box(history, mid, now,
                                blacklisted=bool(warmup.to_float(b.get("blacklisted"))))
        s = warmup.series(history, mid)
        last = s[-1] if s else {}
        prev = s[-2] if len(s) > 1 else None

        def delta(field):
            if prev is None:
                return None
            a, z = warmup.to_float(prev.get(field)), warmup.to_float(last.get(field))
            return None if a is None or z is None else z - a

        rows.append({
            "mid": mid, "email": b.get("email", mid), "domain": b.get("domain", ""),
            "score": warmup.to_float(last.get("score")),
            "inbox": warmup.to_float(last.get("inbox_pct")),
            "d_score": delta("score"), "d_inbox": delta("inbox_pct"),
            "running": b.get("running_campaigns") or [], **c,
        })
    rows.sort(key=lambda r: (ORDER.index(r["state"]),
                             r["mean_inbox"] if r["mean_inbox"] is not None else 0))
    return rows


def weekly_summary(history: list, latest: dict, boxes: dict, anomalies_found: list,
                   now) -> list:
    """Six lines, on Mondays: mailbox states, capacity and the advised
    sourcing volume, what moved, what the guard stopped. For a human who
    looks at deliverability once a week, this block should be enough to size
    the week."""
    kls = {m: warmup.classify_box(history, m, now)["state"] for m in boxes}
    states = Counter(kls.values())
    cap = warmup.capacity([{"email": b.get("email"), "state": kls[m],
                            "warm_email_max": b.get("warm_email_max")}
                           for m, b in boxes.items()])
    frozen = warmup.frozen_boxes(boxes, warmup.parse_ts(latest.get("fetched_at")))
    lists_bits = []
    for name, res in (latest.get("domain_listings") or {}).items():
        lists_bits.append(f"{name} not verifiable" if not res.get("usable") else
                          f"{name} {len(res.get('listed') or {})} listed")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    pp_path = config.REPO_ROOT / "data" / "paused-leads.json"
    stopped = [r for r in (json.loads(pp_path.read_text()) if pp_path.exists() else [])
               if (r.get("stopped_at") or "") >= week_ago]
    reasons = Counter(r.get("reason", "?") for r in stopped)
    return [
        f"- Mailboxes: {states.get('ACTIVE', 0)} healthy · {states.get('RECOVERING', 0)} "
        f"recovering · {states.get('RESTING', 0)} resting · "
        f"{states.get('QUARANTINE', 0)} quarantined.",
        f"- Capacity: ~{cap['leads']:.0f} new leads/day, "
        f"~{5 * cap['leads']:.0f} per working week → source "
        f"~{5 * cap['leads'] * 1.1:.0f} addresses/week to fill it "
        "(~5% verification waste, plus duplicates).",
        "- Outgoing warmup stuck: "
        + (", ".join(sorted(boxes[m].get("email", m) for m in frozen)) or "none") + ".",
        "- Domain blocklists: " + (" · ".join(lists_bits) or "not checked") + ".",
        f"- Guard: {len(stopped)} lead(s) stopped over 7 days"
        + (f" ({', '.join(f'{k} {v}' for k, v in sorted(reasons.items()))})" if stopped else "")
        + ("; details in data/paused-leads.json." if stopped else "."),
        "- " + (f"{len(anomalies_found)} anomaly(ies) this morning, details below."
                if anomalies_found else "Nothing needed."),
    ]


def build_report(history: list, latest: dict, now) -> str:
    boxes = {m: b for m, b in (latest.get("boxes") or {}).items()
             if not b.get("error")}
    stamps = [warmup.parse_ts(r.get("collected_at")) for r in history]
    first = min((t for t in stamps if t), default=None)
    since = (f"history since {first.astimezone(UTC):%Y-%m-%d}"
             if first else "history empty")
    out = [f"# lemwarm deliverability · {now.astimezone(UTC):%Y-%m-%d %H:%M} UTC", "",
           f"Last collection {fmt_ts(latest.get('fetched_at'))} UTC · "
           f"{len(boxes)} mailboxes · {since} · {len(history)} readings"]

    found = warmup.anomalies(latest, history, now)
    if now.astimezone(UTC).weekday() == 0:   # Mondays: the weekly read
        out += ["", "## Week in review", ""] + weekly_summary(
            history, latest, boxes, found, now)
    out += ["", "## Anomalies", ""] + ([f"- {a}" for a in found] or ["- none"])
    frozen = warmup.frozen_boxes(boxes, warmup.parse_ts(latest.get("fetched_at")))
    if frozen:
        out += ["", "**Outgoing warmup stuck** (already flagged, no longer turns the run "
                "red; it will ring on resumption):", ""]
        out += [f"- {boxes[m].get('email', m)}: next send scheduled {fmt_ts(t)}, never left"
                for m, t in sorted(frozen.items(), key=lambda kv: boxes[kv[0]].get("email", ""))]

    rows = box_rows(history, boxes, now)
    out += ["", "## Rotation ranking", "",
            f"{warmup.WINDOW_DAYS}-day window. ACTIVE: mean score ≥ {warmup.ACTIVE_MIN}. "
            f"RECOVERING: ≥ {warmup.RECOVERING_MIN}. RESTING: one score < "
            f"{warmup.RECOVERING_MIN} in the window (7 days to get out). "
            f"QUARANTINE: mean inbox < {warmup.QUARANTINE_INBOX_BELOW}% or blacklisted. "
            f"`*` = provisional, the window has fewer than {warmup.WINDOW_DAYS} readings. "
            "Δ = change since the previous computation.", "",
            "| Mailbox | State | Reason | Score | Δ | Inbox | Δ | 7d mean (score / inbox) | Readings | Running campaigns |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        star = "" if r["n"] >= warmup.WINDOW_DAYS else " *"
        out.append(
            f"| {r['email']} | {r['state']}{star} | {r['why']} | {fmt(r['score'])} | "
            f"{signed(r['d_score'])} | {fmt(r['inbox'], suffix='%')} | {signed(r['d_inbox'])} | "
            f"{fmt(r['mean_score'], 1)} / {fmt(r['mean_inbox'], 0, '%')} | "
            f"{r['n']}/{warmup.WINDOW_DAYS} | {len(r['running'])} |")
    counts = Counter(r["state"] for r in rows)
    out += ["", "Totals: " + " · ".join(f"{s} {counts[s]}" for s in ORDER if counts[s])]

    keep = [r for r in rows if r["state"] in ("RESTING", "QUARANTINE") and r["running"]]
    if keep:
        out += ["", "**Degraded but still senders of running campaigns**: do not remove "
                "them (their leads would lose their follow-ups), stop assigning them new "
                "leads (per-campaign detail below).", ""]
        out += [f"- {r['email']} ({r['state']}): {len(r['running'])} running campaigns"
                for r in keep]

    state_of = latest.get("boxes") or {}
    cap = warmup.capacity([{"email": r["email"], "state": r["state"],
                            "warm_email_max": state_of.get(r["mid"], {}).get("warm_email_max")}
                           for r in rows])
    groups = defaultdict(list)
    for b in cap["boxes"]:
        groups[b["state"] if b["share"] else "NONE"].append(b)
    out += ["", "## Capacity for new leads", "",
            f"Cap of {warmup.BOX_DAILY_CAP} emails per mailbox per day, warmup included. "
            f"One new lead costs ~{warmup.EMAILS_PER_LEAD:g} emails on the same mailbox "
            "(3-4 step sequences): at cruise, n new leads per day means "
            f"{warmup.EMAILS_PER_LEAD:g} × n emails per day.", "",
            "| Mailboxes | Count | Share of new leads | Campaign emails/day | New leads/day |",
            "|---|---|---|---|---|"]
    for key, name, label in (("ACTIVE", "ACTIVE", "full"),
                             ("RECOVERING", "RECOVERING", "half volume"),
                             ("NONE", "RESTING / QUARANTINE", "none, in-flight follow-ups only")):
        bs = groups.get(key, [])
        per = f" ({bs[0]['emails']:.0f} per mailbox)" if bs and bs[0]["emails"] else ""
        out.append(f"| {name} | {len(bs)} | {label}{per} | "
                   f"{sum(b['emails'] for b in bs):.0f} | {sum(b['leads'] for b in bs):.0f} |")
    out.append(f"| **Total** | {len(cap['boxes'])} | | **{cap['emails']:.0f}** | "
               f"**{cap['leads']:.0f}** |")
    gap = cap["leads"] - warmup.TARGET_NEW_LEADS
    step = (avg(b["room"] for b in cap["boxes"]) or 0) / 2 / warmup.EMAILS_PER_LEAD
    out += ["", f"Target of {warmup.TARGET_NEW_LEADS} new leads per day: capacity "
            f"{cap['leads']:.0f}, " + (f"margin of {gap:.0f}." if gap >= 0 else
                                       f"{-gap:.0f} short. Each mailbox moving up one "
                                       f"tier adds ~{step:.0f}.")]
    out += ["", "**Senders for new leads** (to apply in lemlist, the script touches "
            "nothing):", ""]
    for key, label in (("ACTIVE", "full volume"), ("RECOVERING", "half volume"),
                       ("NONE", "no new leads")):
        names = sorted(b["email"] for b in groups.get(key, []))
        out.append(f"- {label} ({len(names)}): {', '.join(names) or 'none'}")
    over = [b for b in state_of.values() if not b.get("error")
            and (warmup.to_float(b.get("email_limit")) or 0)
            + (warmup.to_float(b.get("warm_email_max")) or 0) > warmup.BOX_DAILY_CAP]
    if over:
        room = warmup.BOX_DAILY_CAP - (warmup.to_float(over[0].get("warm_email_max")) or 0)
        out += ["", f"lemlist setting above the cap on {len(over)} mailbox(es): "
                f"emailLimit {over[0].get('email_limit')} + warmup "
                f"{over[0].get('warm_email_max')} > {warmup.BOX_DAILY_CAP}. For lemlist to "
                f"enforce it itself: emailLimit at {room:.0f} ({room / 2:.0f} on "
                "RECOVERING mailboxes)."]
    per_campaign = defaultdict(list)
    for r in rows:
        for name in r["running"]:
            per_campaign[name].append(r["state"])
    risky = [(n, sum(s in ("RESTING", "QUARANTINE") for s in st), len(st))
             for n, st in per_campaign.items()]
    risky = sorted((t for t in risky if t[1]), key=lambda t: -t[1] / t[2])
    if risky:
        out += ["", "**Running campaigns**: lemlist assigns each new lead to a sender "
                "picked at random among the campaign's senders. Pushing leads there sends "
                "a share of them from degraded mailboxes:", "",
                "| Campaign | Degraded senders | Share of new leads affected |",
                "|---|---|---|"]
        for n, b, t in risky:
            name = n.replace("|", "\\|")  # a "|" in a campaign name would break the table
            out.append(f"| {name} | {b}/{t} | {100 * b / t:.0f}% |")

    listings = latest.get("domain_listings") or {}
    if listings:
        out += ["", "## Domain blocklists", "",
                "lemwarm only checks IP lists. These domain lists filter emails that "
                "contain the domain (sender, links, tracking "
                f"{latest.get('tracking_domain') or 'unknown'}).", ""]
        for name, res in listings.items():
            if not res.get("usable"):
                out.append(f"- {name}: not verifiable from this DNS resolver")
            elif res.get("listed"):
                items = ", ".join(f"{d} {warmup.listing_label(name, c)}"
                                  for d, c in sorted(res["listed"].items()))
                out.append(f"- **{name}: {len(res['listed'])} domain(s) listed**: {items}")
            else:
                out.append(f"- {name}: no domain listed")
    month_ago = now - timedelta(days=30)
    alerts = sorted((b.get("email", ""), b.get("spam_alert")) for b in boxes.values()
                    if (warmup.parse_ts(b.get("spam_alert")) or month_ago) > month_ago)
    if alerts:
        out += ["", "lemwarm spam alerts over the last 30 days: "
                + ", ".join(f"{e} ({fmt_ts(t)})" for e, t in alerts) + "."]

    by_domain = defaultdict(list)
    for r in rows:
        by_domain[r["domain"]].append(r)
    out += ["", "## By domain", "",
            "| Domain | Mailboxes | Inbox (latest) | Inbox 7d mean | Mean score |",
            "|---|---|---|---|---|"]
    for dom, rs in sorted(by_domain.items(),
                          key=lambda kv: avg(r["mean_inbox"] for r in kv[1]) or 0):
        out.append(f"| {dom} | {len(rs)} | {fmt(avg(r['inbox'] for r in rs), 1, '%')} | "
                   f"{fmt(avg(r['mean_inbox'] for r in rs), 1, '%')} | "
                   f"{fmt(avg(r['score'] for r in rs), 1)} |")

    out += ["", "## Moves since the previous computation", ""]
    moves = [r for r in rows
             if (r["d_inbox"] is not None and abs(r["d_inbox"]) >= 5)
             or (r["d_score"] is not None and abs(r["d_score"]) >= 2)]
    for r in sorted(moves, key=lambda r: r["d_inbox"] or 0):
        out.append(f"- {r['email']}: inbox {signed(r['d_inbox'])} pts, "
                   f"score {signed(r['d_score'])}")
    if not moves:
        out.append("- no move ≥ 5 inbox pts or ≥ 2 score pts")
    down = sum(1 for r in rows if (r["d_inbox"] or 0) < 0)
    up = sum(1 for r in rows if (r["d_inbox"] or 0) > 0)
    out += ["", f"Inbox down on {down} mailboxes, up on {up}. lemlist warns that its spam "
            "rate needs ~300 warmup sends to stabilize: a one-day move is only confirmed "
            "over 7 days."]

    out += ["", "## 7-day and 30-day trend", ""]
    trend = [(r, warmup.delta_since(history, r["mid"], "inbox_pct", now, 7),
              warmup.delta_since(history, r["mid"], "score", now, 7),
              warmup.delta_since(history, r["mid"], "inbox_pct", now, 30),
              warmup.delta_since(history, r["mid"], "score", now, 30)) for r in rows]
    if all(t[1] is None for t in trend):
        if first:
            out.append(f"Available from {(first + timedelta(days=7)).astimezone(UTC):%Y-%m-%d} "
                       f"(7d) and {(first + timedelta(days=30)).astimezone(UTC):%Y-%m-%d} (30d).")
    else:
        out += ["| Mailbox | Inbox 7d | Score 7d | Inbox 30d | Score 30d |",
                "|---|---|---|---|---|"]
        for r, i7, s7, i30, s30 in sorted(trend, key=lambda t: t[1] or 0):
            out.append(f"| {r['email']} | {signed(i7)} | {signed(s7)} | "
                       f"{signed(i30)} | {signed(s30)} |")

    out += ["", "_Generated by sentinel_report.py. No automatic action here: no mailbox "
            "removal, no pause, no setting change._"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="write nothing, exit 1 on an anomaly")
    ap.add_argument("--fail-for-test", action="store_true",
                    help="force the check to fail (test the alarm email)")
    args = ap.parse_args()

    now = warmup.now_utc()
    history, latest = warmup.read_history(), warmup.read_latest()
    if args.check or args.fail_for_test:
        found = warmup.anomalies(latest, history, now)
        for a in found:
            print(f"ANOMALY  {a}")
        if args.fail_for_test:
            print("forced failure (test_alarm): if the GitHub email arrives, "
                  "the alarm channel works")
            return 1
        print(f"check: {len(found)} anomaly(ies)" if found
              else "check: no anomaly")
        return 1 if found else 0

    report = build_report(history, latest, now)
    warmup.REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    warmup.REPORT_MD.write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
