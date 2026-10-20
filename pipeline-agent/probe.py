"""Read-only probe for /deals-no-next-step.

Counts deals in the configured open pipeline stages with empty
`hs_next_step`, grouped by owner. Also dumps the deal-pipeline stage map so
we can confirm the stage-ID-to-label mapping before any further work.

NO writes. No Slack. No Sonnet spend.

Usage:
    python3 probe.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import argparse
from runtime import PROJECT_DIR, configure, load_settings, request_read

OUT = PROJECT_DIR / "output"
PIPELINE_ID = ""


def _get(path: str, params: dict | None = None) -> dict:
    return request_read("GET", path, params=params)


def _post(path: str, body: dict) -> dict:
    return request_read("POST", path, body=body)


def list_pipeline_stages() -> list[dict]:
    """Returns all deal-pipeline stages with id + label + displayOrder."""
    data = _get("crm/v3/pipelines/deals")
    stages: list[dict] = []
    for pipe in data.get("results", []):
        for s in pipe.get("stages", []):
            stages.append({
                "pipeline": pipe.get("label"),
                "pipeline_id": pipe.get("id"),
                "stage_id": s.get("id"),
                "label": s.get("label"),
                "displayOrder": s.get("displayOrder"),
                "metadata": s.get("metadata", {}),
            })
    return stages


def search_deals_empty_next_step(stage_ids: list[str]) -> list[dict]:
    """Paginated search for deals in given stages with empty hs_next_step."""
    all_deals: list[dict] = []
    after: str | None = None
    while True:
        body: dict = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "dealstage", "operator": "IN", "values": stage_ids},
                        {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_ID},
                        {"propertyName": "hs_next_step", "operator": "NOT_HAS_PROPERTY"},
                    ]
                }
            ],
            "properties": [
                "dealname", "dealstage", "amount", "hubspot_owner_id",
                "hs_next_step", "hs_lastmodifieddate", "notes_last_updated",
                "createdate",
            ],
            "limit": 100,
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
        }
        if after:
            body["after"] = after
        data = _post("crm/v3/objects/deals/search", body)
        all_deals.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return all_deals


def owner_map(owner_ids: list[str]) -> dict[str, str]:
    """Map owner id → email."""
    out: dict[str, str] = {}
    page = _get("crm/v3/owners", params={"limit": 100})
    while True:
        for o in page.get("results", []):
            out[str(o["id"])] = o.get("email", "")
        nxt = page.get("paging", {}).get("next", {}).get("after")
        if not nxt:
            break
        page = _get("crm/v3/owners", params={"limit": 100, "after": nxt})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only inventory of deals without a next step")
    ap.add_argument("--config", type=Path, default=PROJECT_DIR / "config.json")
    ap.add_argument("--list-stages", action="store_true", help="List stage IDs without scanning deals")
    args = ap.parse_args()
    settings = None if args.list_stages else load_settings(args.config)
    configure()
    OUT.mkdir(exist_ok=True)
    print("── Step 1: pipeline stage map ──")
    stages = list_pipeline_stages()
    for s in stages:
        print(f"  {s['stage_id']:<20} {s['label']:<40} (pipeline={s['pipeline']})")

    (OUT / "pipeline_stages.json").write_text(json.dumps(stages, indent=2))

    if args.list_stages:
        return
    global PIPELINE_ID
    PIPELINE_ID = settings["pipeline_id"]
    target = [s for s in stages if s["pipeline_id"] == PIPELINE_ID
              and s["stage_id"] in settings["stages"]]
    if len(target) != len(settings["stages"]):
        sys.exit("Configured stages do not match this pipeline. Run --list-stages and fix config.json.")
    if any(str(s.get("metadata", {}).get("isClosed")).lower() != "false" for s in target):
        sys.exit("A configured stage is closed or has unknown closed status in HubSpot. Choose open stages only.")
    target_ids = [s["stage_id"] for s in target]
    print(f"\n── Step 2: matched target stages: {len(target)} ──")
    for s in target:
        print(f"  → {s['stage_id']:<20} {s['label']}")
    if not target_ids:
        print("No stages matched. Inspect pipeline_stages.json and config.json.")
        sys.exit(1)

    print(f"\n── Step 3: searching deals in {target_ids} with empty hs_next_step ──")
    deals = search_deals_empty_next_step(target_ids)
    print(f"Found {len(deals)} active deals with empty hs_next_step.")

    by_owner: Counter[str] = Counter()
    by_stage: Counter[str] = Counter()
    by_owner_stage: dict[tuple[str, str], int] = defaultdict(int)
    for d in deals:
        p = d.get("properties", {})
        oid = p.get("hubspot_owner_id") or "(unassigned)"
        sid = p.get("dealstage") or "(none)"
        by_owner[oid] += 1
        by_stage[sid] += 1
        by_owner_stage[(oid, sid)] += 1

    owners = owner_map(list(by_owner.keys()))
    stage_label = {s["stage_id"]: s["label"] for s in stages}

    print("\n── By owner ──")
    for oid, n in by_owner.most_common():
        email = owners.get(oid, "(unknown)") if oid != "(unassigned)" else "(unassigned)"
        print(f"  {n:>4}  {email}  ({oid})")

    print("\n── By stage ──")
    for sid, n in by_stage.most_common():
        print(f"  {n:>4}  {stage_label.get(sid, sid)}")

    print("\n── By owner × stage ──")
    for (oid, sid), n in sorted(by_owner_stage.items(), key=lambda x: -x[1]):
        email = owners.get(oid, "(unknown)") if oid != "(unassigned)" else "(unassigned)"
        print(f"  {n:>4}  {email:<35}  {stage_label.get(sid, sid)}")

    snapshot = {
        "target_stages": [{"id": s["stage_id"], "label": s["label"]} for s in target],
        "total_empty_next_step": len(deals),
        "by_owner": [
            {"owner_id": oid, "email": owners.get(oid, ""), "count": n}
            for oid, n in by_owner.most_common()
        ],
        "by_stage": [
            {"stage_id": sid, "label": stage_label.get(sid, sid), "count": n}
            for sid, n in by_stage.most_common()
        ],
        "sample_deals": [
            {
                "id": d["id"],
                "dealname": d.get("properties", {}).get("dealname"),
                "stage": stage_label.get(d.get("properties", {}).get("dealstage"), ""),
                "owner_email": owners.get(d.get("properties", {}).get("hubspot_owner_id") or "", ""),
                "amount": d.get("properties", {}).get("amount"),
                "last_modified": d.get("properties", {}).get("hs_lastmodifieddate"),
            }
            for d in deals[:25]
        ],
    }
    (OUT / "probe_snapshot.json").write_text(json.dumps(snapshot, indent=2))
    print(f"\nWrote {OUT / 'probe_snapshot.json'} and {OUT / 'pipeline_stages.json'}.")


if __name__ == "__main__":
    main()
