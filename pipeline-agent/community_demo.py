"""Three fictional review cycles, reproducible offline; no CRM or model calls."""
import argparse
import copy
import json
from datetime import datetime
from pathlib import Path

from commitments import apply_commitments
from followup_state import apply_memory
from preview_recap import render_html, render_recap
from review import write_private_json
from snapshot import analyze_snapshot

ROOT = Path(__file__).parent


def run_demo(output):
    output = Path(output)
    snapshot = json.loads((ROOT / "examples/neutral-snapshot.json").read_text())
    promise = ("Nous enverrons le schéma de déploiement le 12 octobre 2026. "
               "Le client validera le pilote après réception de ce document.")
    snapshot["deals"][0]["events"][0]["text"] = promise
    state, history = {}, []
    for number, at in enumerate(("2026-10-13T18:00:00+00:00", "2026-10-14T18:00:00+00:00",
                                 "2026-10-16T18:00:00+00:00"), 1):
        snapshot["as_of"] = at
        now = datetime.fromisoformat(at)
        if number == 3:
            snapshot["deals"][0]["events"].append({
                "id": "new-security-reply", "kind": "email", "association": "direct",
                "at": "2026-10-16T12:00:00Z", "direction": "inbound", "status": "received",
                "automated": False, "title": "Autre question avant le pilote",
                "text": "Nous avons aussi besoin de préciser où sont hébergées les données."})
        rows, meta = analyze_snapshot(snapshot, now=now)
        # An explicitly simulated assistant extraction, not a model-generated result.
        extraction = [{"deal_id": rows[0]["deal_id"], "review_id": rows[0]["review_id"], "commitments": [
            {"id": "send-document", "side": "seller", "action": "Envoyer le schéma de déploiement",
             "status": "outstanding", "due_date": "2026-10-12", "depends_on": [],
             "citations": [{"source_id": "notes:security-note", "quote": promise}],
             "completion_citations": []},
            {"id": "approve-pilot", "side": "buyer", "action": "Valider le pilote après réception",
             "status": "outstanding", "due_date": None, "depends_on": ["send-document"],
             "citations": [{"source_id": "notes:security-note", "quote": promise}],
             "completion_citations": []}]}]
        rows = apply_commitments(rows, extraction, now=now)
        decisions = []
        if number == 2:
            for row in history[0]:
                budget = row["deal_id"] == "fictional-budget"
                decisions.append({"deal_id": row["deal_id"],
                                  "fingerprint": row["followup_memory"]["fingerprint"],
                                  "status": "snoozed" if budget else "reviewed",
                                  "until": "2026-10-16T09:00:00Z" if budget else None,
                                  "note": "Revoir le dossier en interne ; respecter le report client."
                                  if budget else "Contexte examiné ; préparation prise en compte."})
        rows, state = apply_memory(rows, state, "fictional/crm/pipeline/rep", decisions=decisions, now=now)
        history.append(copy.deepcopy(rows))
        path = output / f"cycle-{number}.json"
        write_private_json(path, rows)
        write_private_json(path.with_suffix(".meta.json"), meta)
        summary, _ = render_recap(rows, timezone="Europe/Paris")
        path.with_suffix(".html").write_text(render_html(rows, summary, meta, "Europe/Paris"))
        path.with_suffix(".html").chmod(0o600)
    write_private_json(output / "memory.json", state)
    return history


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/community-demo")
    result = run_demo(parser.parse_args().output_dir)
    print("Fictional cycles (deals requiring review): " +
          " → ".join(str(sum(r["followup_memory"]["attention"] for r in rows)) for rows in result))
    print("Commitment extraction is simulated. No live service or model was called.")
