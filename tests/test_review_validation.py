import json
import unittest

from src.review.review_validation import validate_review


class ReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.finding = {
            'Finding': 'Review resilience', 'Evidence': 'node_count = 1',
            'File': '/terraform/main.tf', 'Why it matters': 'Possible risk',
            'Verification needed': 'Check workload topology', 'Confidence': 'Inference',
        }
        self.files = {'/terraform/main.tf': 'node_count = 1\n# other code'}

    def reject(self, payload, expected):
        accepted, errors = validate_review(payload, self.files)
        self.assertFalse(accepted)
        self.assertIn(expected, '\n'.join(errors))

    def test_structure_errors(self):
        for payload, expected in [
            ('{', 'invalid JSON at line 1'),
            ('{}', 'missing findings array'),
            ('[]', 'expected a JSON object'),
            ('{"findings": {}}', 'findings must be an array'),
            ('{"findings": []}', 'got 0; expected at least 1'),
            ('{"findings": [null]}', 'expected an object'),
        ]:
            with self.subTest(payload=payload):
                self.reject(payload, expected)

    def test_every_required_field(self):
        for key in self.finding:
            with self.subTest(key=key):
                finding = dict(self.finding)
                del finding[key]
                self.reject(json.dumps({'findings': [finding]}), 'missing required field(s): ' + key)
                finding[key] = ' '
                self.reject(json.dumps({'findings': [finding]}), 'must be nonempty strings: ' + key)

    def test_evidence_failures(self):
        for changes, expected in [
            ({'File': '/terraform/unread.tf'}, 'not read this turn: /terraform/unread.tf'),
            ({'Evidence': 'node_count  = 1'}, 'Evidence excerpt not found in /terraform/main.tf'),
            ({'Evidence': 'x' * 1001}, 'exceeds 1000 characters'),
        ]:
            with self.subTest(expected=expected):
                self.reject(json.dumps({'findings': [{**self.finding, **changes}]}), expected)
        _, errors = validate_review(json.dumps({'findings': [{**self.finding, 'Evidence': 'node_count = 2'}]}), self.files)
        self.assertIn('node_count = 2', errors[0])
        self.assertNotIn('# other code', errors[0])

    def test_existing_acceptance_rules(self):
        for count in (1, 2, 3, 4):
            accepted, errors = validate_review(json.dumps({'findings': [self.finding] * count}), self.files)
            self.assertEqual(len(accepted), count)
            self.assertFalse(errors)
            self.assertEqual(accepted[0]['Confidence'], 'Inference')


if __name__ == '__main__':
    unittest.main()
