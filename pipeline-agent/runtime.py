"""Project-local configuration and read-only HubSpot transport."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from scheduling import validate_schedule
from signals import DEFAULT_RULES

PROJECT_DIR = Path(__file__).resolve().parent
_SESSION = None
_BASE = "https://api.hubapi.com/"


def load_settings(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Missing {path}. Copy config.example.json and configure your open pipeline stages.")
    data = json.loads(path.read_text())
    if not isinstance(data.get("pipeline_id"), str) or not data["pipeline_id"].strip():
        raise ValueError("pipeline_id must be a nonempty string")
    stages = data.get("stages")
    if not isinstance(stages, dict) or not stages or not all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in stages.items()
    ):
        raise ValueError("stages must map open stage IDs to labels")
    if any("REPLACE" in value for value in [data["pipeline_id"], *stages]):
        raise ValueError("Replace example pipeline/stage IDs using probe.py --list-stages")
    domains = data.get("automation_sender_domains", [])
    if not isinstance(domains, list) or not all(isinstance(d, str) and d.strip() for d in domains):
        raise ValueError("automation_sender_domains must be a list of domain names")
    data["automation_sender_domains"] = [d.strip().lower() for d in domains]
    stage_context = data.get("stage_context", {})
    if (not isinstance(stage_context, dict) or set(stage_context) - set(stages)
            or any(not isinstance(text, str) or not text.strip() for text in stage_context.values())):
        raise ValueError("stage_context must map configured stage IDs to nonempty descriptions")
    data["schedule"] = validate_schedule(data.get("schedule", {"mode": "manual"}))
    if type(data.get("indirect_context", False)) is not bool:
        raise ValueError("indirect_context must be true or false")
    if not isinstance(data.get("context_confirmations", []), list):
        raise ValueError("context_confirmations must be a list")
    for field in ('next_step_property', 'next_step_date_property'):
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_]+', value)):
            raise ValueError(f'{field} must be a CRM property name or null')
    portal = data.get('hubspot_portal_id')
    if portal is not None and (not isinstance(portal, str) or not portal.isdigit()):
        raise ValueError('hubspot_portal_id must be a numeric string or null')
    if not isinstance(data.get('rules', {}), dict) or not isinstance(data.get('stage_rules', {}), dict):
        raise ValueError('rules and stage_rules must be objects')
    if set(data.get('stage_rules', {})) - set(stages):
        raise ValueError('stage_rules references an unconfigured stage')
    for rule_set in [data.get('rules', {}), *data.get('stage_rules', {}).values()]:
        if not isinstance(rule_set, dict):
            raise ValueError('Each stage rule set must be an object')
        for name, value in rule_set.items():
            if name not in DEFAULT_RULES or type(value) is not int or value < 1:
                raise ValueError(f'Invalid rule: {name}')
    return data


def configure(env_path: Path | None = None) -> None:
    """Load the selected private env file; importing has no API/env side effects."""
    from dotenv import load_dotenv
    import requests
    load_dotenv(env_path or PROJECT_DIR / ".env", override=False)
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("Set HUBSPOT_ACCESS_TOKEN in the project .env or process environment")
    global _SESSION
    _SESSION = requests.Session()
    _SESSION.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


def request_read(method: str, path: str, *, params=None, body=None) -> dict:
    """Allow only the exact CRM reads used by this prototype, including POST searches."""
    patterns = {
        "GET": r"crm/v3/(owners|pipelines/deals)",
        "POST": r"crm/v3/objects/(deals|contact|company|notes|emails|calls|meetings|tasks)/(search|batch/read)|crm/v4/associations/(deal|contact|company|notes|emails|calls|meetings|tasks)/(deal|contact|company|notes|emails|calls|meetings|tasks)/batch/read",
    }
    if method not in patterns or not re.fullmatch(patterns[method], path):
        raise ValueError(f"Unsupported read route: {method} {path}")
    if _SESSION is None:
        raise RuntimeError("Call configure() before a live read")
    for attempt in range(5):
        response = _SESSION.request(method, _BASE + path, params=params, json=body,
                                    timeout=(10, 45), allow_redirects=False)
        if response.status_code == 429 and attempt < 4:
            try:
                wait = min(60, max(1, float(response.headers.get("Retry-After", "2"))))
            except ValueError:
                wait = 2
            time.sleep(wait)
            continue
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected status {response.status_code} on CRM read")
        return response.json()
    raise RuntimeError("HubSpot read failed")


def validate_open_stages(settings: dict) -> None:
    pipelines = request_read("GET", "crm/v3/pipelines/deals").get("results", [])
    pipeline = next((p for p in pipelines if p.get("id") == settings["pipeline_id"]), None)
    if not pipeline:
        raise ValueError("Configured pipeline does not exist in this account")
    stages = {s["id"]: s for s in pipeline.get("stages", [])}
    for stage_id in settings["stages"]:
        if stage_id not in stages:
            raise ValueError(f"Stage {stage_id} does not belong to this pipeline")
        closed = stages[stage_id].get("metadata", {}).get("isClosed")
        if str(closed).lower() != "false":
            raise ValueError(f"Stage {stage_id} is closed or has unknown closed status; select confirmed open stages")
