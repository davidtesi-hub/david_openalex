import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('evaluation', Path(__file__).resolve().parents[1] / 'scripts/evaluate_openalex_importance.py')
e = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = e
spec.loader.exec_module(e)


class FakeModel:
    def __init__(self, content):
        self.content = content

    def create_chat_completion(self, **kwargs):
        self.messages = kwargs['messages']
        return {'choices': [{'message': {'content': self.content}}]}


class EvaluationTests(unittest.TestCase):
    def test_newest_day_and_rotation(self):
        rows = [e.Candidate('W1', 'a', '2026-11-01', 'abstract'),
                e.Candidate('W2', 'b', '2026-11-01', 'abstract'),
                e.Candidate('W3', 'c', '2026-10-31', 'abstract')]
        candidates = {r.work_id: r for r in rows}
        self.assertEqual(e.select_next(candidates, ['a', 'b', 'c'], -1).work_id, 'W1')
        self.assertEqual(e.select_next(candidates, ['a', 'b', 'c'], 0).work_id, 'W2')
        candidates.pop('W1'); candidates.pop('W2')
        self.assertEqual(e.select_next(candidates, ['a', 'b', 'c'], 1).work_id, 'W3')

    def test_score_contract_and_complete_abstract(self):
        candidate = e.Candidate('W1', 'a', '2026-11-01', 'Distinctive measured result. ' * 500)
        model = FakeModel('{"importance": 6, "justification": "Resultado concreto del abstract."}')
        self.assertEqual(e.score_abstract(model, candidate)[0], 6)
        self.assertIn(candidate.summary.strip(), model.messages[-1]['content'])
        for bad in ['{"importance": true, "justification":"x"}',
                    '{"importance":11,"justification":"x"}',
                    '{"importance":5,"justification":""}',
                    '{"importance":5,"justification":"x","extra":1}']:
            with self.assertRaises(ValueError):
                e.score_abstract(FakeModel(bad), candidate)
