#!/usr/bin/env python3
"""Checks a campaign's senders against today's deliverability ranking, so a
campaign only ever sends from mailboxes with a good grade.

The public lemlist API can NOT set a campaign's senders (verified live:
PATCH campaigns/{id} with senders / sendUserMailboxIds answers 200 and
changes nothing, POST campaigns/{id}/senders answers 405). Picking senders
stays a click in the UI at creation time; this script is the guardrail right
behind it: it compares the campaign's actual senders with today's ranking
(data/) and EXITS NON-ZERO when a resting or quarantined mailbox is ticked.
Batch pushes run it before inserting a single lead: no lead can enter a
misconfigured campaign.

Exit 0: senders compliant (recovering mailboxes are allowed, they take new
leads at half volume by construction of the pacing).
Exit 1: at least one RESTING/QUARANTINE mailbox ticked, or no healthy
mailbox at all; the detail says exactly what to tick and untick in the UI.

Usage:
  python3 sender_check.py <cam_id | part of the name>
"""

from __future__ import annotations

import sys

from lib import env, warmup

OK_STATES = ("ACTIVE", "RECOVERING")


def resolve_campaign(client, needle: str) -> tuple:
    if needle.startswith("cam_"):
        _, data = client._request("GET", f"campaigns/{needle}")
        if warmup.is_valid(data, "name"):
            return needle, data["name"]
        raise SystemExit(f"campaign {needle} not found")
    matches = {}
    for user in client.get_senders():
        for c in user.get("campaigns") or []:
            if needle.lower() in (c.get("name") or "").lower():
                matches[c["_id"]] = c.get("name")
    if len(matches) != 1:
        raise SystemExit(f"\"{needle}\": {len(matches)} campaign(s) found "
                         f"({', '.join(sorted(matches.values())) or 'none'}), be specific")
    return next(iter(matches.items()))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"))
    cid, name = resolve_campaign(client, sys.argv[1])
    _, camp = client._request("GET", f"campaigns/{cid}")
    selected = {(s.get("email") or "").lower() for s in camp.get("senders") or []}

    history, now = warmup.read_history(), warmup.now_utc()
    state_of = {}
    for mid, box in warmup.enumerate_mailboxes(client).items():
        state_of[box["email"]] = warmup.classify_box(history, mid, now)["state"]

    good = {e for e, st in state_of.items() if st in OK_STATES}
    bad_selected = sorted(e for e in selected if state_of.get(e) not in OK_STATES)
    missing = sorted(good - selected)

    print(f"campaign: {name} ({cid}) · {len(selected)} sender(s) ticked")
    for e in sorted(selected):
        print(f"  {'OK ' if e in good else '!! '} {e:34s} {state_of.get(e, 'unknown to lemwarm')}")
    if missing:
        print("also tick (healthy or recovering mailboxes not selected):")
        for e in missing:
            print(f"  +   {e:34s} {state_of[e]}")
    if bad_selected:
        print(f"REFUSED: {len(bad_selected)} resting/quarantined mailbox(es) ticked: "
              + ", ".join(bad_selected))
        if camp.get("status") == "running" or camp.get("inSequenceLeadCount"):
            print("→ campaign already running: do NOT untick (its leads keep their "
                  "sender), just stop pushing new leads into it.")
        else:
            print("→ untick them in the UI before the first lead push.")
        return 1
    if not selected & good:
        print("REFUSED: no usable mailbox ticked.")
        return 1
    print("senders compliant: the push can go.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
