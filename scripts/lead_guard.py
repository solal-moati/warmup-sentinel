#!/usr/bin/env python3
"""Daily campaign guard: pause, never more than PAUSE_CAP per run, the
still-active leads whose address turns out bad, and log them. This is the
agent's ONLY write, and it is capped. The human is informed, never asked:
every pause is printed in the run, appended to data/paused-leads.json
(committed by the workflow) and summed up in the Monday digest.

Triggers, on every running campaign:
  · an inProgress lead whose emailStatus (lemlist verdict) is undeliverable
    (the mailbox does not exist);
  · an inProgress lead whose last event is a bounce.
A "risky" verdict alone never pauses: it means an unconfirmed mailbox on a
catch-all domain, the email was accepted, and pausing it would lose a lead
that may well answer.

Never touched: internal test leads (TEST_LEAD_DOMAINS variable, comma
separated), leads already logged, any done/paused/unsubscribed lead. Pause
only, never removal: a lead removed then re-inserted would restart its
sequence from step 1.

Usage:
  python3 lead_guard.py            # dry-run: shows, touches nothing
  python3 lead_guard.py --commit   # pauses (cap PAUSE_CAP)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

from lib import config, env, warmup

LEDGER = config.REPO_ROOT / "data" / "paused-leads.json"
TEST_LEAD_DOMAINS = tuple(d.strip() for d in
                          os.environ.get("TEST_LEAD_DOMAINS", "").split(",") if d.strip())
PAUSE_CAP = 10          # per-run cap: beyond it, something else is wrong
BAD = ("undeliverable",)     # never "risky": accepted mail, unconfirmed mailbox


def _atomic_write(path, obj) -> None:
    """Write then rename: a crash never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _fix(text: str) -> str:
    """lemlist export mojibake (UTF-8 decoded as latin-1): "JoÃ£o" → "João"."""
    try:
        return (text or "").encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text or ""


def guard_candidates(rows: list, known_ids: set, cap: int = PAUSE_CAP) -> list:
    """The leads to pause among a campaign export's rows. Pure function,
    covered by sentinel_selftest.py."""
    out = []
    for r in rows:
        if len(out) >= cap:
            break
        email = (r.get("email") or "").strip().lower()
        status = r.get("status") or ""
        verdict = (r.get("emailStatus") or "").lower()
        bounced = r.get("lastState") == "emailsBounced"
        if status != "inProgress" or not email:
            continue                      # done/paused: nothing left to stop
        if TEST_LEAD_DOMAINS and email.endswith(TEST_LEAD_DOMAINS):
            continue                      # internal test leads: untouchable
        if r.get("_id") in known_ids:
            continue                      # already logged
        if verdict in BAD or bounced:
            out.append(dict(r, reason=("bounce" if bounced else f"lemlist_{verdict}")))
    return out


def sends_for(client, cid: str, email: str) -> tuple:
    """(emails received, last sender) for one lead, from the send activities."""
    count, sender, offset = 0, "", 0
    while True:
        batch = client.get_activities(cid, "emailsSent", limit=100, offset=offset)
        for act in batch:
            if (act.get("leadEmail") or act.get("email") or "").lower() == email:
                count += 1
                sender = act.get("sendUserEmail") or sender
        if len(batch) < 100:
            return count, sender
        offset += 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="pause and log; dry-run otherwise")
    args = ap.parse_args()

    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"),
                                 read_only=not args.commit)
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    known = {r.get("lead_id") for r in ledger}

    running = {}
    for user in client.get_senders():
        for c in user.get("campaigns") or []:
            if c.get("status") == "running":
                running[c["_id"]] = c.get("name") or c["_id"]

    paused, today = [], datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for cid, name in sorted(running.items(), key=lambda kv: kv[1]):
        _, data = client._request("GET", f"campaigns/{cid}/export/leads",
                                  params={"state": "all"})
        raw = data.get("raw", "") if isinstance(data, dict) else ""
        rows = list(csv.DictReader(io.StringIO(raw)))
        for cand in guard_candidates(rows, known, PAUSE_CAP - len(paused)):
            email = cand["email"].strip().lower()
            entry = {
                "email": email,
                "full_name": _fix(f"{cand.get('firstName', '')} {cand.get('lastName', '')}").strip(),
                "company": _fix(cand.get("companyName") or ""),
                "job_title": _fix(cand.get("jobTitle") or ""),
                "linkedin_url": cand.get("linkedinUrl") or "",
                "campaign": name, "campaign_id": cid,
                "reason": cand["reason"],
                "lemlist_verdict": ((cand.get("emailStatus") or "").lower()
                                    if (cand.get("emailStatus") or "").lower() in BAD else ""),
                "emails_received": 0, "sender": "",
                "stopped_at": today, "lead_id": cand.get("_id") or "",
            }
            if args.commit:
                client._request("POST", f"leads/pause/{entry['lead_id']}?campaignId={cid}")
                entry["emails_received"], entry["sender"] = sends_for(client, cid, email)
                ledger.append(entry)
                known.add(entry["lead_id"])
            paused.append(entry)
            print(f"  {'PAUSED' if args.commit else 'pause (dry-run)'}  "
                  f"{name[:40]:40s} {entry['reason']:22s} received={entry['emails_received']}")
        if len(paused) >= PAUSE_CAP:
            print(f"  ! cap of {PAUSE_CAP} reached: the rest waits for the "
                  "next run (worth investigating)")
            break

    if args.commit and paused:
        _atomic_write(LEDGER, ledger)
        print(f"logged: data/paused-leads.json (+{len(paused)})")
    print(f"guard: {len(paused)} lead(s) {'paused' if args.commit else 'to pause (dry-run)'} "
          f"across {len(running)} running campaign(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
