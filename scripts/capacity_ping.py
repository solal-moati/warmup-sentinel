#!/usr/bin/env python3
"""Morning chat ping (Google Chat or Slack, incoming webhook): today's send
capacity, in a few lines readable in three seconds.

Reads lemlist (read-only) and the data/ history, computes capacity the same
way the daily report does (50 emails per mailbox per day including warmup,
recovering mailboxes at half volume), and posts:
  · how many new leads can go out today;
  · the state of the mailboxes;
  · the current watch items (stuck warmup, listed domain).

Business days only (the cron handles that). NEVER fails the run: an error is
posted to the chat when possible, printed otherwise, and the script exits 0
so a comfort ping never triggers a failure email.

Usage:
  python3 capacity_ping.py [--dry-run]   # --dry-run: print without posting
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter

from lib import env, warmup


def post_chat(text: str) -> bool:
    webhook = env.get("CHAT_WEBHOOK", required=False)
    if not webhook:
        print("(CHAT_WEBHOOK not set, nothing posted)")
        return False
    req = urllib.request.Request(webhook, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
    return True


def build_message() -> str:
    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"))
    history, now = warmup.read_history(), warmup.now_utc()
    boxes = warmup.enumerate_mailboxes(client)
    rows = []
    for mid, b in boxes.items():
        s = client.get_lemwarm_settings(mid)
        rows.append({"email": b["email"],
                     "state": warmup.classify_box(history, mid, now)["state"],
                     "warm_email_max": s.get("warmEmailMax"),
                     "warm_next_email_at": s.get("warmNextEmailAt") or "",
                     "lemwarm_active": s.get("active")})
    cap = warmup.capacity(rows)
    states = Counter(r["state"] for r in rows)
    if rows and states.get("UNKNOWN", 0) == len(rows):
        return (f"📮 *{len(rows)} mailboxes found, no history yet.* The daily "
                "collection has not run: the capacity figure starts tomorrow.")
    frozen = sorted(r["email"] for r in rows
                    if r["lemwarm_active"] and warmup.parse_ts(r["warm_next_email_at"])
                    and warmup.business_hours_between(
                        warmup.parse_ts(r["warm_next_email_at"]), now) > warmup.WARM_NEXT_OVERDUE_H)
    latest = warmup.read_latest()
    listed = sorted({d for res in (latest.get("domain_listings") or {}).values()
                     for d in (res.get("listed") or {})})
    # Format rule: short, readable in three seconds, the number first, never
    # a raw dump of addresses, and always end with "nothing needed" or the
    # expected action.
    resting = states.get("RESTING", 0) + states.get("QUARANTINE", 0)
    lines = [
        f"📮 *You can push ~{cap['leads']:.0f} new leads today.*",
        "",
        f"✅ {states.get('ACTIVE', 0)} mailboxes at full volume"
        f"   🟡 {states.get('RECOVERING', 0)} at half volume"
        f"   ⛔ {resting} resting",
    ]
    watch = []
    if frozen:
        watch.append(f"• {len(frozen)} warmup(s) still stuck on lemlist's side")
    if listed:
        watch.append(f"• {len(listed)} domain(s) on a blocklist: known, watched daily")
    if watch:
        lines += [""] + watch
    lines += ["", "👍 *Nothing needed from you.*"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print without posting")
    args = ap.parse_args()
    try:
        text = build_message()
    except Exception as e:  # never a red run for a comfort ping
        text = (f"⚠️ Capacity ping unavailable this morning ({type(e).__name__}); "
                "the daily monitor is still in place.")
    print(text)
    if not args.dry_run:
        try:
            if post_chat(text):
                print("→ posted to the chat webhook")
        except Exception as e:
            print(f"(chat post failed: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
