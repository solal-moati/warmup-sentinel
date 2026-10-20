"""Same evidence must survive different CRM stage names and field conventions."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
from runtime import load_settings

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
EVENTS = {'meetings': [{'hs_meeting_start_time': '2026-09-01T10:00:00Z', 'hs_meeting_outcome': 'COMPLETED'}],
          'notes': [], 'calls': [], 'emails': [], 'tasks': []}


class AdaptabilityTests(unittest.TestCase):
    def run_case(self, stage_id, label, settings, properties=None):
        deal = {'id': 'fictional', 'properties': {'dealstage': stage_id, **(properties or {})}}
        with patch.object(analyze, 'SETTINGS', settings), patch.object(analyze, 'STAGE_LABELS', {stage_id: label}):
            signals = analyze.compute_signals(deal, EVENTS, now=NOW)
            context = analyze.build_context_blob(deal, [], [], EVENTS, signals)
        return signals, context

    def test_renaming_or_reordering_stages_does_not_change_diagnosis(self):
        configurations = [('alpha', '2. Demo'), ('beta', 'Validation interne'), ('gamma', 'Renouvellement')]
        buckets = []
        for stage, label in configurations:
            signals, _ = self.run_case(stage, label, {})
            buckets.append(analyze.classify(signals))
        self.assertEqual(buckets, ['stalled_post_meeting'] * 3)

    def test_existing_action_survives_a_different_crm_field_name(self):
        signals, context = self.run_case('beta', 'Validation interne', {
            'next_step_property': 'action_commerciale', 'next_step_date_property': 'date_action'},
            {'action_commerciale': 'Attendre la validation du comité', 'date_action': '2026-10-12T12:00:00Z'})
        self.assertEqual(analyze.classify(signals), 'action_planned')
        self.assertIn('Attendre la validation du comité', context)
        self.assertFalse(any(a['kind'] == 'commitment_review' for a in analyze.prepare_actions(signals)))

    def test_same_inactivity_has_different_meaning_in_long_and_short_cycles(self):
        short, _ = self.run_case('alpha', 'Validation', {'stage_rules': {'alpha': {'inactivity_days': 14}}})
        long, _ = self.run_case('alpha', 'Validation', {'stage_rules': {'alpha': {'inactivity_days': 60}}})
        self.assertEqual(analyze.classify(short), 'stalled_post_meeting')
        self.assertEqual(analyze.classify(long), 'recent_activity_no_step_recorded')

    def test_original_generation_receives_configured_meaning_and_existing_action(self):
        meaning = 'Le comité examine le dossier. Aucun rendez-vous supplémentaire obligatoire.'
        signals, context = self.run_case('alpha', 'Étape X', {'stage_context': {'alpha': meaning}},
                                        {'hs_next_step': 'Attendre le comité'})
        client = Mock()
        client.messages.create.return_value = Mock(content=[Mock(text='Conserver le suivi prévu.')],
                                                   usage=Mock(input_tokens=1, output_tokens=1))
        with patch.dict('os.environ', {'ANTHROPIC_MODEL': 'test-model'}):
            analyze.generate_next_step(analyze.classify(signals), context, client)
        prompt = client.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertIn(meaning, prompt)
        self.assertIn('Attendre le comité', prompt)
        self.assertNotIn('A deal has no `hs_next_step`', prompt)

    def test_stage_meaning_must_reference_the_selected_pipeline(self):
        settings = {'pipeline_id': 'fictional', 'stages': {'alpha': 'Étape X'},
                    'stage_context': {'unknown': 'Not in this pipeline'}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                load_settings(path)


if __name__ == '__main__':
    unittest.main()
