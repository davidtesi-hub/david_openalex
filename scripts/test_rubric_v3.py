import sqlite3
import tempfile
import unittest
from pathlib import Path

from rubric_v3 import INSTRUCTION, open_db, parse_response, save_response


ABSTRACT = 'We measure a new effect in 40 stars. It reduces the main uncertainty. The method improves prior measurements.'
RESPONSE = '''T=E
Q1=B
E1="40 stars"
J1="-"
Q2=C
E2="It reduces the main uncertainty."
J2="The reported reduction resolves a meaningful uncertainty rather than a limited improvement."
Q3=B
E3="40 stars"
J3="-"
Q4=B
E4="improves prior measurements"
J4="-"'''


class RubricV3Tests(unittest.TestCase):
    def test_prompt_has_correct_count_and_exact_template(self):
        self.assertIn('Return exactly these thirteen lines', INSTRUCTION)
        self.assertEqual(len(RESPONSE.splitlines()), 13)

    def test_score_and_audit(self):
        parsed = parse_response(RESPONSE, ABSTRACT)
        self.assertEqual(parsed['score'], '4.67')
        self.assertEqual(parsed['coverage_percent'], 100)
        with tempfile.TemporaryDirectory() as directory:
            db = open_db(Path(directory) / 'v3.sqlite')
            save_response(db, work_id='W1', model='test', abstract=ABSTRACT, raw=RESPONSE)
            self.assertEqual(db.execute('SELECT puntuacion,Q2,E2,validation_error FROM evaluations').fetchone(),
                             ('4.67', 'C', 'It reduces the main uncertainty.', None))
            db.close()

    def test_unknown_hides_single_score(self):
        text = RESPONSE.replace('Q4=B\nE4="improves prior measurements"', 'Q4=U\nE4="-"')
        parsed = parse_response(text, ABSTRACT)
        self.assertIsNone(parsed['score'])
        self.assertEqual(parsed['coverage_percent'], 80)
        self.assertEqual((parsed['minimum_score'], parsed['maximum_score']), ('4.00', '6.00'))

    def test_invalid_quote_or_justification(self):
        with self.assertRaisesRegex(ValueError, 'exact abstract excerpt'):
            parse_response(RESPONSE.replace('E1="40 stars"', 'E1="invented"'), ABSTRACT)
        with self.assertRaisesRegex(ValueError, 'J2 required'):
            parse_response(RESPONSE.replace('J2="The reported reduction resolves a meaningful uncertainty rather than a limited improvement."', 'J2="-"'), ABSTRACT)


if __name__ == '__main__':
    unittest.main()
