import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
from commitments import apply_commitments, validate_commitments
from community_demo import run_demo
from context_links import collect_indirect, content_hash
from followup_state import apply_memory, fingerprint
from preview_recap import grouped, render_html, render_recap
from review import apply_proposals
from snapshot import analyze_snapshot
from runtime import request_read

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 13, 18, tzinfo=timezone.utc)


def analysis():
    return analyze_snapshot(json.loads((ROOT / "examples/neutral-snapshot.json").read_text()), now=NOW)[0]


def choice(row, status="reviewed", until=None):
    return {"deal_id": row["deal_id"], "fingerprint": fingerprint(row),
            "status": status, "until": until, "note": "Reviewed the specific context."}


class MemoryTests(unittest.TestCase):
    def test_three_cycles_reopen_only_changed_or_due_and_keep_all_deals(self):
        with tempfile.TemporaryDirectory() as folder:
            runs = run_demo(folder)
            self.assertEqual([sum(r["followup_memory"]["attention"] for r in run) for run in runs], [3, 0, 2])
            self.assertEqual([len(run) for run in runs], [3, 3, 3])
            self.assertEqual([r["followup_memory"]["status"] for r in runs[2]],
                             ["changed", "snooze_expired", "reviewed"])
            self.assertIn("Déjà examiné", grouped(runs[2]))
            for row in runs[2]:
                self.assertIn(row["dealname"], Path(folder, "cycle-3.html").read_text())
            self.assertEqual(Path(folder, "memory.json").stat().st_mode & 0o777, 0o600)

    def test_identical_rerun_is_idempotent_and_scopes_are_isolated(self):
        rows = analysis()
        first, state = apply_memory(rows, {}, "crm/account-a", now=NOW, decisions=[choice(rows[0])])
        again, state2 = apply_memory(rows, state, "crm/account-a", now=NOW)
        self.assertEqual(first, again)
        self.assertEqual(state, state2)
        other, _ = apply_memory(rows, state, "crm/account-b", now=NOW)
        self.assertTrue(other[0]["followup_memory"]["attention"])

    def test_age_alone_does_not_repeat_review(self):
        rows = analysis()
        _, state = apply_memory(rows, {}, "scope", now=NOW, decisions=[choice(rows[0])])
        rows[0]["signals"]["days_in_stage"] += 1
        rows[0]["analyzed_at"] = (NOW + timedelta(days=1)).isoformat()
        result, _ = apply_memory(rows, state, "scope", now=NOW + timedelta(days=1))
        self.assertFalse(result[0]["followup_memory"]["attention"])

    def test_stale_decision_or_snapshot_is_rejected(self):
        rows = analysis()
        decision = choice(rows[0])
        _, state = apply_memory(rows, {}, "scope", now=NOW)
        rows[0]["activity_version"] = "new-evidence"
        with self.assertRaises(ValueError):
            apply_memory(rows, state, "scope", decisions=[decision], now=NOW)
        rows[0]["analyzed_at"] = (NOW - timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            apply_memory(rows, state, "scope", now=NOW)

    def test_warning_cannot_be_silenced_and_new_plan_reopens(self):
        rows = analysis()
        rows[0]["signals"]["warnings"] = ["Incomplete context"]
        result, _ = apply_memory(rows, {}, "scope", decisions=[choice(rows[0], "dismissed")], now=NOW)
        self.assertTrue(result[0]["followup_memory"]["attention"])
        rows = analysis()
        _, state = apply_memory(rows, {}, "scope", decisions=[choice(rows[0])], now=NOW)
        rows[0]["current_next_step"] = "New buyer-agreed plan"
        result, _ = apply_memory(rows, state, "scope", now=NOW)
        self.assertEqual(result[0]["followup_memory"]["status"], "changed")

    def test_deal_reappearing_in_scope_is_reopened(self):
        rows = analysis()
        _, state = apply_memory(rows, {}, "scope", decisions=[choice(rows[0])], now=NOW)
        _, state = apply_memory([], state, "scope", now=NOW)
        result, _ = apply_memory(rows, state, "scope", now=NOW)
        self.assertTrue(result[0]["followup_memory"]["attention"])


class ContextTests(unittest.TestCase):
    def collect(self, links=None, confirmations=()):
        graph = {("company", "contact"): {"co": ["forgotten-contact"]},
                 ("contact", "notes"): {"forgotten-contact": ["n1"]},
                 ("company", "notes"): {"co": ["n1"]},
                 ("notes", "deal"): {"n1": links or []}}
        def associate(source, target, ids):
            return {key: graph.get((source, target), {}).get(key, []) for key in ids}
        return collect_indirect("d1", [], ["co"], {"notes": []}, associate=associate,
                                read=lambda *args: {"n1": {"hs_note_body": "Security requires a document."}},
                                properties={"notes": ["hs_note_body"]}, confirmations=confirmations)

    def test_contact_missing_from_deal_is_discovered_but_not_assumed(self):
        events, scan = self.collect()
        self.assertEqual(events["notes"], [])
        self.assertEqual(scan["contacts_examined"], 1)
        self.assertEqual(len(scan["candidates"]), 1)
        self.assertIn("contact:forgotten-contact", scan["candidates"][0]["via"])

    def test_other_deal_and_shared_deal_context_are_excluded(self):
        for links in (["d2"], ["d1", "d2"]):
            events, scan = self.collect(links)
            self.assertEqual(scan["excluded_other_deal"], 1)
            self.assertFalse(events["notes"])
            self.assertFalse(scan["candidates"])

    def test_confirmed_association_pinned_to_content_and_stale_confirmation_flagged(self):
        _, scan = self.collect()
        item = scan["candidates"][0]
        confirmation = {"deal_id": "d1", "source_id": item["source_id"],
                        "content_hash": item["content_hash"], "reason": "Reviewed the actual note."}
        events, scan = self.collect(confirmations=[confirmation])
        self.assertEqual(len(events["notes"]), 1)
        confirmation["content_hash"] = "old-version"
        events, scan = self.collect(confirmations=[confirmation])
        self.assertFalse(events["notes"])
        self.assertEqual(scan["candidates"][0]["status"], "confirmation_stale")

    def test_explicit_deal_link_can_be_used(self):
        events, scan = self.collect(["d1"])
        self.assertEqual(len(events["notes"]), 1)
        self.assertEqual(scan["confirmed_sources"], ["notes:n1"])

    def test_association_pages_complete_and_missing_result_fails(self):
        responses = [{"results": [{"from": {"id": "d"}, "to": [{"toObjectId": "c1"}],
                                   "paging": {"next": {"after": "next-page"}}}]},
                     {"results": [{"from": {"id": "d"}, "to": [{"toObjectId": "c2"}]}]}]
        with patch.object(analyze, "_post", side_effect=responses) as request:
            self.assertEqual(analyze.fetch_assoc("deal", "contact", ["d"]), {"d": ["c1", "c2"]})
            self.assertEqual(request.call_args.args[1]["inputs"], [{"id": "d", "after": "next-page"}])
        with patch.object(analyze, "_post", return_value={"results": []}), self.assertRaises(ValueError):
            analyze.fetch_assoc("company", "contact", ["co"])

    def test_context_write_routes_remain_blocked(self):
        for method, route in (("POST", "crm/v4/associations/company/contact/batch/create"),
                              ("PATCH", "crm/v3/objects/deals/1"),
                              ("DELETE", "crm/v4/associations/company/contact/batch/archive")):
            with self.assertRaises(ValueError):
                request_read(method, route)

    def test_snapshot_preserves_ambiguous_context_without_using_it_as_evidence(self):
        snapshot = json.loads((ROOT / "examples/neutral-snapshot.json").read_text())
        snapshot["deals"][0]["events"][0]["association"] = "ambiguous"
        rows, _ = analyze_snapshot(snapshot, now=NOW)
        self.assertEqual(len(rows[0]["context_scan"]["candidates"]), 1)
        self.assertNotIn("notes:security-note", [e["source_id"] for e in rows[0]["review_context"]["evidence"]["records"]])
        self.assertIn("Contexte à rattacher", render_html(rows, "Demo"))


class CommitmentTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {"records": [
            {"source_id": "notes:1", "text": "We will send the document on 12 October 2026.",
             "at": "2026-10-09T12:00:00Z", "truncated": False},
            {"source_id": "emails:2", "text": "Here is the requested security document.",
             "at": "2026-10-12T10:00:00Z", "truncated": False}]}
        self.item = {"id": "send", "side": "seller", "action": "Send the security document",
                     "status": "outstanding", "due_date": "2026-10-12", "depends_on": [],
                     "citations": [{"source_id": "notes:1", "quote": self.evidence["records"][0]["text"]}],
                     "completion_citations": []}

    def validate(self, items):
        return validate_commitments(items, self.evidence, reference_date=NOW.date())

    def test_bilateral_dependency_is_explicit(self):
        buyer = {**copy.deepcopy(self.item), "id": "approve", "side": "buyer", "depends_on": ["send"]}
        result = self.validate([self.item, buyer])
        self.assertEqual(result[1]["blocked_by"], ["send"])
        self.assertTrue(result[0]["human_review_required"])

    def test_invented_quote_date_or_dependency_is_rejected(self):
        for field, value in (("due_date", "2026-10-14"), ("depends_on", ["missing"]),
                             ("citations", [{"source_id": "notes:1", "quote": "This text does not exist."}])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate([{**self.item, field: value}])

    def test_completion_needs_later_distinct_evidence(self):
        done = {**self.item, "status": "completed", "completion_citations": self.item["citations"]}
        with self.assertRaises(ValueError):
            self.validate([done])
        done["completion_citations"] = [{"source_id": "emails:2", "quote": self.evidence["records"][1]["text"]}]
        self.assertEqual(self.validate([done])[0]["status"], "completed")

    def test_cycle_is_rejected(self):
        a = {**self.item, "depends_on": ["b"]}
        b = {**self.item, "id": "b", "depends_on": ["send"]}
        with self.assertRaises(ValueError):
            self.validate([a, b])

    def test_binding_and_freshness(self):
        rows = analysis()
        proposal = [{"deal_id": rows[0]["deal_id"], "review_id": rows[0]["review_id"], "commitments": []}]
        self.assertEqual(apply_commitments(rows, proposal, now=NOW)[0]["commitments"], [])
        with self.assertRaises(ValueError):
            apply_commitments(rows, proposal, now=NOW + timedelta(hours=25))
        proposal[0]["review_id"] = "another-snapshot"
        with self.assertRaises(ValueError):
            apply_commitments(rows, proposal, now=NOW)

    def test_pending_own_commitment_blocks_followup_preparation(self):
        rows = analysis()
        rows[0]["commitments"] = [{"side": "seller", "status": "outstanding", "blocked_by": []}]
        proposal = [{"deal_id": rows[0]["deal_id"], "review_id": rows[0]["review_id"], "proposal": {
            "decision": "prepare_followup", "reason": "Follow up", "action_text": "Hello",
            "due_date": None, "citations": []}}]
        result = apply_proposals(rows, proposal, now=NOW)
        self.assertIn("pending_commitment_requires_review", str(result[0]["recommendation_validation"]))
        proposal[0]["proposal"] = None
        result = apply_proposals(rows, proposal, now=NOW)
        self.assertEqual(result[0]["recommendation_validation"]["status"], "needs_review")


class CommunityCasesTests(unittest.TestCase):
    def test_contributed_cases_match_expected_behavior(self):
        files = list((ROOT / "examples").glob("community-case-*.json"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(case=path.name):
                case = json.loads(path.read_text())
                self.assertTrue(case["snapshot"]["fictional_example"])
                captured = datetime.fromisoformat(case["snapshot"]["as_of"].replace("Z", "+00:00"))
                rows, _ = analyze_snapshot(case["snapshot"], now=captured)
                self.assertEqual({r["deal_id"]: r["bucket"] for r in rows}, case["expected_buckets"])
                self.assertEqual({r["deal_id"]: [a["kind"] for a in r["prepared_actions"]] for r in rows},
                                 case["expected_action_kinds"])


if __name__ == "__main__":
    unittest.main()
