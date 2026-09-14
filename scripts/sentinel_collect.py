#!/usr/bin/env python3
"""Daily collection of the sending mailboxes' lemwarm scores → data/.

The API only returns the current value, no history: a day not collected is
lost. Hence a minimal, idempotent, read-only script:

  · read-only by construction (WarmupClient refuses any verb other than GET);
  · a history row only when lemlist recomputed a mailbox's score (per-mailbox
    dedupe on deliverability.lastAt): re-running adds nothing;
  · an unusable response (HTML served with a 200, empty dict) becomes an
    error surfaced by sentinel_report.py --check, never a row;
  · the sending and tracking domains are checked against the domain
    blocklists (SURBL, Spamhaus DBL, URIBL), which lemwarm never sees.

Writes:
  data/history.csv   score series, append-only
  data/latest.json   current state of every mailbox (status, warmup,
                     running campaigns) and of the domain lists,
                     rewritten on every run

Usage:
  python3 sentinel_collect.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

from lib import env, warmup


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="read the API and print, write nothing")
    args = ap.parse_args()

    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"))
    stamp = warmup.iso(warmup.now_utc())
    try:
        boxes = warmup.enumerate_mailboxes(client)
    except Exception as e:  # key, network, path: the whole collection is lost
        print(f"FAILED to enumerate mailboxes: {e}")
        return 1

    errors, warnings = [], []
    try:
        running = warmup.running_campaigns_by_mailbox(client)
    except Exception as e:  # the rotation note will be missing, not the scores
        running = {}
        warnings.append(f"running campaigns unreadable ({e})")
    if not running:
        warnings.append("no running campaign found: rotation note empty")

    parsed, state = [], {}
    for mid, box in sorted(boxes.items(), key=lambda kv: kv[1]["email"]):
        try:
            settings = client.get_lemwarm_settings(mid)
        except Exception as e:  # one failing mailbox must not cost the others
            settings = {"raw": str(e)}
        if not warmup.is_valid(settings, "deliverability"):
            errors.append(f"{box['email']}: lemwarm/{mid}/settings unusable")
            state[mid] = {**box, "error": "unusable response"}
            continue
        row = warmup.parse_settings(box, mid, settings, stamp)
        parsed.append(row)
        state[mid] = {k: row[k] for k in warmup.LATEST_KEYS}
        state[mid]["running_campaigns"] = running.get(mid, [])

    try:
        tracking = warmup.tracking_domain(client)
    except Exception as e:
        tracking = ""
        warnings.append(f"tracking domain unreadable ({e})")
    domains = sorted({b["domain"] for b in boxes.values()} | set(warmup.SITE_DOMAINS)
                     | ({tracking} if tracking else set()))
    listings = warmup.domain_listings(domains)
    previous = warmup.read_latest()
    warm_changes = warmup.warmup_changes(previous, state, stamp)
    reference = previous.get("domain_listings_ref") or {
        n: r for n, r in (previous.get("domain_listings") or {}).items() if r.get("usable")}
    changes = warmup.listing_changes(reference, listings)
    reference = warmup.advance_reference(reference, listings, stamp)

    fresh = warmup.new_rows(parsed, warmup.read_history())
    fresh_ids = {r["usm_id"] for r in fresh}
    for r in parsed:
        mark = "+ new computation" if r["usm_id"] in fresh_ids else "= unchanged"
        print(f"  {r['email']:34s} score {r['score']!s:>3}  "
              f"inbox {r['inbox_pct']!s:>3}%  {mark}")
    print(f"\n{len(boxes)} mailboxes · {len(parsed)} valid readings · "
          f"{len(fresh)} new row(s) · {len(errors)} error(s)")
    for name, res in listings.items():
        verdict = ("not verifiable from this resolver" if not res["usable"]
                   else f"{len(res['listed'])} domain(s) listed out of {len(domains)}")
        print(f"  list {name}: {verdict}")
    for msg in errors + warnings + changes + warm_changes:
        print(f"  ! {msg}")

    if args.dry_run:
        print("(dry-run: nothing written)")
    else:
        warmup.append_history(fresh)
        warmup.write_latest({"fetched_at": stamp, "errors": errors, "warnings": warnings,
                             "boxes": state, "tracking_domain": tracking,
                             "domain_listings": listings, "domain_listings_ref": reference,
                             "listing_changes": changes, "warmup_changes": warm_changes})
        print(f"written: data/history.csv (+{len(fresh)}), data/latest.json")
    return 0 if parsed else 1


if __name__ == "__main__":
    sys.exit(main())
