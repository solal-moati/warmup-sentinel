"""Local review memory. Never changes a deal or treats a review as a completed task."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from context_links import content_hash
from review import write_private_json
from signals import to_dt


def fingerprint(row):
    """Age counters and model wording do not re-open an otherwise unchanged item."""
    signals = row.get("signals", {})
    value = {key: row.get(key) for key in (
        "deal_id", "stage", "owner_id", "current_next_step", "bucket", "coverage",
        "activity_version", "error", "context_scan", "commitments")}
    value["facts"] = {key: signals.get(key) for key in (
        "warnings", "planned_actions", "overdue_actions", "last_inbound_at")}
    return content_hash(value)


def apply_memory(rows, state, scope, *, decisions=(), now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("Use a scope identifying CRM/account/pipeline/owner")
    if not isinstance(state, dict) or state.get("version", 1) != 1:
        raise ValueError("Unsupported memory version")
    result = copy.deepcopy(state or {"version": 1, "scopes": {}})
    scopes = result.setdefault("scopes", {})
    previous = scopes.get(scope, {"deals": {}})
    last_run = to_dt(previous.get("updated_at"))
    if last_run and now < last_run:
        raise ValueError("Memory clock would go backwards")
    ids = [str(row["deal_id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate deals")
    choices = {}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"deal_id", "fingerprint", "status", "until", "note"}:
            raise ValueError("Decision needs deal_id, fingerprint, status, until, note")
        key = str(decision["deal_id"])
        if key not in ids or key in choices:
            raise ValueError("Duplicate or out-of-scope decision")
        if decision["status"] not in {"reviewed", "dismissed", "snoozed", "reopen"}:
            raise ValueError("Unknown review status")
        if not isinstance(decision["note"], str) or not decision["note"].strip():
            raise ValueError("Explain the review decision")
        until = to_dt(decision["until"])
        if decision["status"] == "snoozed":
            if not until or until <= now:
                raise ValueError("Snooze requires a future timestamp with timezone")
        elif decision["until"] is not None:
            raise ValueError("Only snooze accepts until")
        choices[key] = decision
    output, new_deals = [], copy.deepcopy(previous["deals"])
    for original in rows:
        row = copy.deepcopy(original)
        key = str(row["deal_id"])
        captured = to_dt(row.get("analyzed_at"))
        if not captured or captured > now:
            raise ValueError("Each deal needs a valid capture time")
        if not row.get("activity_version"):
            raise ValueError("Collect again to include activity_version")
        old = previous["deals"].get(key)
        if old and captured < to_dt(old["captured_at"]):
            raise ValueError("An older snapshot cannot replace newer memory")
        digest = fingerprint(row)
        changed = bool(old and (digest != old["fingerprint"] or not old.get("present", True)))
        if old and captured == to_dt(old["captured_at"]) and digest != old["fingerprint"]:
            # Local enrichments can change the fingerprint at the same capture time.
            changed = True
        entry = {"fingerprint": digest, "captured_at": captured.isoformat(), "present": True}
        if old and not changed and old.get("decision"):
            entry["decision"] = old["decision"]
        choice = choices.get(key)
        if choice:
            if choice["fingerprint"] != digest:
                raise ValueError("Decision is stale; review the latest facts")
            if choice["status"] == "reopen":
                entry.pop("decision", None)
            else:
                entry["decision"] = {**choice, "at": now.isoformat()}
        decision = entry.get("decision")
        expired = bool(decision and decision["status"] == "snoozed"
                       and to_dt(decision["until"]) <= now)
        # Collection uncertainty must remain visible even after a local dismissal.
        unsafe = bool(row.get("error") or row.get("signals", {}).get("warnings")
                      or (row.get("recommendation_validation") or {}).get("status") == "needs_review")
        attention = not decision or expired or unsafe
        label = ("verification_required" if unsafe else "snooze_expired" if expired
                 else decision["status"] if decision else "changed" if changed
                 else "new" if old is None else "pending")
        if (old and not choice and not changed and not unsafe and not expired
                and captured == to_dt(old["captured_at"])):
            label = old.get("last_status", label)
        entry["last_status"] = label
        row["followup_memory"] = {"fingerprint": digest, "status": label,
                                 "attention": attention, "decision": decision}
        new_deals[key] = entry
        output.append(row)
    for key in set(new_deals) - set(ids):
        new_deals[key]["present"] = False
    scopes[scope] = {"updated_at": now.isoformat(), "deals": new_deals}
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("input", "output", "state"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--scope", required=True, help="CRM/account/pipeline/owner, stable for this full scope")
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    paths = [args.input, args.input.with_suffix(".meta.json"), args.output,
             args.output.with_suffix(".meta.json"), args.state]
    if args.decisions:
        paths.append(args.decisions)
    if len({p.resolve() for p in paths}) != len(paths) or args.output.suffix != ".json":
        parser.error("Input, metadata, output, decisions and state must be distinct; output must be JSON")
    rows = json.loads(args.input.read_text())
    meta = json.loads(args.input.with_suffix(".meta.json").read_text())
    if (meta.get("complete") is not True or meta.get("errors")
            or meta.get("expected_deals") != len(rows) or meta.get("processed_deals") != len(rows)):
        parser.error("Memory requires a complete collection of the same configured scope")
    decisions = json.loads(args.decisions.read_text()) if args.decisions else []
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(args.state) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(args.state.read_text()) if args.state.exists() else {}
        result, updated = apply_memory(rows, state, args.scope, decisions=decisions)
        write_private_json(args.output, result)
        write_private_json(args.output.with_suffix(".meta.json"), meta)
        write_private_json(args.state, updated)
    print(f"{sum(r['followup_memory']['attention'] for r in result)} deals to review; all {len(result)} retained.")


if __name__ == "__main__":
    main()
