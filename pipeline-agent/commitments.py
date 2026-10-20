"""Import cited bilateral commitments from an existing assistant. Local review only."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from recommendations import dates_in_text
from review import review_id, write_private_json
from signals import to_dt


def checked_citations(citations, sources):
    if not isinstance(citations, list) or not citations:
        raise ValueError("An engagement needs source citations")
    checked = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {"source_id", "quote"}:
            raise ValueError("Each citation needs source_id and quote")
        source = sources.get(citation["source_id"])
        quote = citation["quote"]
        if (not source or source.get("truncated") or not isinstance(quote, str)
                or len(quote.strip()) < 12 or quote not in source["text"]):
            raise ValueError("Citation missing, truncated or not an exact excerpt")
        checked.append({**citation, "at": source.get("at")})
    return checked


def validate_commitments(items, evidence, *, reference_date):
    if not isinstance(items, list):
        raise ValueError("commitments must be a list")
    sources = {record["source_id"]: record for record in evidence["records"]}
    output, seen = [], set()
    for item in items:
        required = {"id", "side", "action", "status", "due_date", "depends_on",
                    "citations", "completion_citations"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("Invalid commitment schema")
        identity = item["id"]
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("Commitment IDs must be unique nonempty strings")
        seen.add(identity)
        if item["side"] not in {"seller", "buyer", "unknown"}:
            raise ValueError("side must be seller, buyer or unknown")
        if item["status"] not in {"outstanding", "completed", "unclear"}:
            raise ValueError("Unknown commitment status")
        if not isinstance(item["action"], str) or not item["action"].strip():
            raise ValueError("Describe the specific commitment")
        citations = checked_citations(item["citations"], sources)
        completion = []
        if item["status"] == "completed":
            completion = checked_citations(item["completion_citations"], sources)
            # A promise is not its execution proof. A later, separate record is required.
            promised_dates = [to_dt(c["at"]) for c in citations]
            done_dates = [to_dt(c["at"]) for c in completion]
            if (not all(promised_dates) or not all(done_dates)
                    or min(done_dates) < max(promised_dates)
                    or {c["source_id"] for c in citations} & {c["source_id"] for c in completion}):
                raise ValueError("Completion needs distinct, dated execution evidence after the promise")
        elif item["completion_citations"] != []:
            raise ValueError("Only completed engagements have completion citations")
        due = item["due_date"]
        if due is not None:
            try:
                parsed = date.fromisoformat(due)
            except (TypeError, ValueError):
                raise ValueError("due_date must be YYYY-MM-DD or null") from None
            if due != parsed.isoformat():
                raise ValueError("due_date must use YYYY-MM-DD")
            supported = {value for c in citations for _, value in dates_in_text(c["quote"], reference_date.year)}
            if parsed not in supported:
                raise ValueError("Deadline must be explicit in a cited quote, not the event timestamp")
        dependencies = item["depends_on"]
        if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
            raise ValueError("depends_on must list commitment IDs")
        if identity in dependencies or len(set(dependencies)) != len(dependencies):
            raise ValueError("Invalid commitment dependencies")
        output.append({**item, "citations": citations, "completion_citations": completion,
                       "human_review_required": True})
    by_id = {item["id"]: item for item in output}
    def visit(key, stack):
        if key not in by_id or key in stack:
            raise ValueError("Unknown or cyclic commitment dependency")
        for dep in by_id[key]["depends_on"]:
            visit(dep, stack | {key})
    for item in output:
        visit(item["id"], set())
        item["blocked_by"] = [dep for dep in item["depends_on"] if by_id[dep]["status"] != "completed"]
    return output


def apply_commitments(rows, proposals, *, now=None):
    now = now or datetime.now(timezone.utc)
    ids = [str(row["deal_id"]) for row in rows]
    if len(set(ids)) != len(ids) or not isinstance(proposals, list):
        raise ValueError("Unique analysis IDs and a proposal list are required")
    index = {}
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) != {"deal_id", "review_id", "commitments"}:
            raise ValueError("Each extraction needs deal_id, review_id, commitments")
        key = str(proposal["deal_id"])
        if key not in ids or key in index:
            raise ValueError("Duplicate or out-of-scope extraction")
        index[key] = proposal
    output = []
    for original in rows:
        row = copy.deepcopy(original)
        extraction = index.get(str(row["deal_id"]))
        if extraction is None:
            output.append(row)
            continue
        captured = to_dt(row.get("analyzed_at"))
        if (not captured or captured > now or now - captured > timedelta(hours=24)
                or not row.get("review_context") or row.get("review_id") != review_id(row)
                or extraction["review_id"] != row["review_id"]):
            raise ValueError("Missing, stale or mismatched extraction context")
        items = validate_commitments(extraction["commitments"],
                                     row["review_context"]["evidence"], reference_date=captured.date())
        row["commitments"] = items
        row["prepared_actions"] = [a for a in row.get("prepared_actions", []) if a.get("kind") != "bilateral_commitment"]
        for item in items:
            side = {"seller": "Notre équipe", "buyer": "Côté client", "unknown": "Responsable à préciser"}[item["side"]]
            status = {"outstanding": "Exécution à vérifier", "completed": "Exécution documentée, à relire",
                      "unclear": "Statut à clarifier"}[item["status"]]
            dependencies = ", ".join(next(i["action"] for i in items if i["id"] == key) for key in item["blocked_by"])
            row["prepared_actions"].append({
                "kind": "bilateral_commitment", "title": side + " : " + item["action"],
                "text": status + (" · Dépend de : " + dependencies if dependencies else ""),
                "due_date": item["due_date"], "citations": item["citations"] + item["completion_citations"],
                "reason": "Interprétation des sources à confirmer par le commercial."})
        output.append(row)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input", "proposals", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    paths = [args.input, args.proposals, args.output, args.input.with_suffix(".meta.json"),
             args.output.with_suffix(".meta.json")]
    if args.output.suffix != ".json" or len({p.resolve() for p in paths}) != len(paths):
        parser.error("Keep inputs, output and metadata distinct; output must be JSON")
    rows = json.loads(args.input.read_text())
    meta = json.loads(args.input.with_suffix(".meta.json").read_text())
    if (meta.get("complete") is not True or meta.get("errors")
            or meta.get("processed_deals") != len(rows) or meta.get("expected_deals") != len(rows)):
        parser.error("A complete collection is required")
    output = apply_commitments(rows, json.loads(args.proposals.read_text()))
    write_private_json(args.output, output)
    write_private_json(args.output.with_suffix(".meta.json"), meta)
    print("Cited commitments prepared locally. Interpretation still needs human review.")


if __name__ == "__main__":
    main()
