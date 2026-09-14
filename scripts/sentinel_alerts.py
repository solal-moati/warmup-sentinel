#!/usr/bin/env python3
"""lemlist deliverability alerts: 4 rules, nothing else.

Read-only by default: prints the rules, the alerts already in place and what
would be created. `--commit --only <rule>` creates ONE rule; a rule already
in place (same widget, metric, scope, threshold, period) is never recreated.
No deletion, no modification. Create ONE rule first and check the email
lands before creating the rest.

Channels: lemlist in-app notification + email to ALERT_EMAIL, at most one
reminder per day per alert. The spam rate gets no rule on purpose:
simulated on our real numbers, a 3% threshold rang on all 16 mailboxes.

Usage:
  python3 sentinel_alerts.py                       # dry-run
  python3 sentinel_alerts.py --commit --only score-7d
"""

from __future__ import annotations

import argparse
import json
import sys

from lib import env, warmup


def _recipients() -> list:
    return [e.strip() for e in env.get("ALERT_EMAIL").split(",")]


RULES = {
    "score-7d": dict(widget="warmup", metric="score", severity="warning", scope="mailbox",
                     threshold=80, comparisonOperator="below", periodDays=7, periodMode="rolling"),
    "score-3d": dict(widget="warmup", metric="score", severity="critical", scope="mailbox",
                     threshold=75, comparisonOperator="below", periodDays=3, periodMode="consecutive"),
    "inbox-domain": dict(widget="warmup", metric="inboxRate", severity="warning", scope="domain",
                         threshold=75, comparisonOperator="below", periodDays=7, periodMode="rolling"),
    # the "global" scope only applies to warmup (lemlist spec): bounces go per domain
    "bounce": dict(widget="outreach", metric="bounceRate", severity="critical", scope="domain",
                   threshold=3, comparisonOperator="above", periodDays=7, periodMode="rolling"),
}
SAME = ("widget", "metric", "scope", "threshold", "comparisonOperator", "periodDays", "periodMode")


def payload(rule: dict) -> dict:
    """Creation body (lemlist deliverability-alerts OpenAPI spec). Without
    scopeEntities, lemlist checks every mailbox or domain of that scope
    (per its API spec)."""
    return dict(rule, recheckDelayHours=24, channelConfig={
        "inapp": {"enabled": True},
        "email": {"enabled": True, "addresses": _recipients()},
        "webhook": {"enabled": False},
        "slack": {"enabled": False},
    })


def existing(client) -> list:
    _, data = client._request("GET", "deliverability/alerts")
    if not warmup.is_valid(data, "alerts"):
        raise RuntimeError(f"GET deliverability/alerts unusable: {str(data)[:160]}")
    return data["alerts"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="create the rule picked by --only")
    ap.add_argument("--only", choices=sorted(RULES), help="one single rule")
    args = ap.parse_args()
    if args.commit and not args.only:
        ap.error("--commit requires --only <rule>: one alert at a time")

    client = warmup.WarmupClient(env.get("LEMLIST_API_KEY"), read_only=not args.commit)
    boxes = warmup.enumerate_mailboxes(client)
    current = existing(client)
    print(f"{len(current)} alert(s) already in place · {len(boxes)} mailboxes")
    for name in ([args.only] if args.only else sorted(RULES)):
        rule = RULES[name]
        dup = [a for a in current if all(a.get(k) == rule[k] for k in SAME)]
        label = (f"{name:14s} {rule['metric']} {rule['comparisonOperator']} {rule['threshold']} "
                 f"({rule['periodMode']} {rule['periodDays']}d, {rule['scope']}, {rule['severity']})")
        if dup:
            print(f"  = {label}: already in place ({dup[0].get('_id') or dup[0].get('id')})")
            continue
        if not args.commit:
            print(f"  + {label}: would be created")
            continue
        _, data = client._request("POST", "deliverability/alerts", json=payload(rule), retries=1)
        if not warmup.is_valid(data):
            print(f"  ! {label}: unexpected response {str(data)[:200]}")
            return 1
        print(f"  ✓ {label}: created\n{json.dumps(data, ensure_ascii=False)[:600]}")
    if not args.commit:
        print("(dry-run: nothing created)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
