import json
import unittest

from pydantic import ValidationError

from src.review.review_schema import ReviewFinding, ReviewResponse, review_response_content
from src.review.review_validation import validate_review


class ReviewSchemaTests(unittest.TestCase):
    def setUp(self):
        self.findings = [
            ReviewFinding(
                Finding=f"Finding {index}",
                File="/terraform/versions.tf",
                **{"Evidence line": 2},
                **{
                    "Why it matters": "Predictable provider behavior",
                    "Verification needed": "Check the approved version range",
                },
                Confidence="Inference",
            )
            for index in range(3)
        ]
        self.response = ReviewResponse(findings=self.findings)

    def test_valid_structured_response(self):
        self.assertIsInstance(self.response, ReviewResponse)
        self.assertEqual(len(self.response.findings), 3)

    def test_zero_to_three_findings_are_accepted(self):
        for count in range(4):
            response = ReviewResponse(findings=self.findings[:count])
            self.assertEqual(len(response.findings), count)

    def test_four_findings_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse(findings=self.findings + [self.findings[0]])

    def test_all_required_fields_are_present(self):
        finding = self.response.findings[0]
        self.assertEqual(
            set(finding.model_dump(by_alias=True)),
            {
                "Finding",
                "File",
                "Evidence line",
                "Evidence end line",
                "Why it matters",
                "Verification needed",
                "Confidence",
            },
        )

    def test_conversion_matches_review_validation_format(self):
        content = review_response_content(
            self.response,
            {"/terraform/versions.tf": 'terraform {\n  required_version = ">= 1.5.0"\n}'},
        )
        accepted, rejected = validate_review(
            content,
            {"/terraform/versions.tf": 'terraform {\n  required_version = ">= 1.5.0"\n}'},
        )
        self.assertEqual(len(accepted), 3)
        self.assertFalse(rejected)
        self.assertEqual(json.loads(content)["findings"][0]["File"], "/terraform/versions.tf")
        self.assertEqual(
            json.loads(content)["findings"][0]["Evidence"],
            '  required_version = ">= 1.5.0"',
        )

    def test_nonexistent_line_is_rejected(self):
        response = ReviewResponse(
            findings=[
                finding.model_copy(update={"evidence_line": 99})
                for finding in self.findings
            ]
        )
        with self.assertRaisesRegex(ValueError, "evidence line 99 does not exist"):
            review_response_content(
                response,
                {"/terraform/versions.tf": 'terraform {\n  required_version = ">= 1.5.0"\n}'},
            )

    def test_wrong_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "file was not read this turn"):
            review_response_content(
                self.response,
                {"/terraform/main.tf": "resource \"x\" \"y\" {}"},
            )

    def test_exact_evidence_preserves_original_whitespace(self):
        response = ReviewResponse(
            findings=[
                ReviewFinding(
                    Finding=f"Finding {index}",
                    File="/terraform/main.tf",
                    **{
                        "Evidence line": 2,
                        "Why it matters": "Possible risk",
                        "Verification needed": "Check deployment requirements",
                    },
                    Confidence="Inference",
                )
                for index in range(3)
            ]
        )
        content = review_response_content(
            response,
            {"/terraform/main.tf": "resource \"x\" \"y\" {\n    name = \"exact\"  \n}"},
        )
        self.assertEqual(
            json.loads(content)["findings"][0]["Evidence"],
            '    name = "exact"  ',
        )

    def test_multiline_exact_crlf_evidence(self):
        source = 'terraform {\r\n  required_version = ">= 1.5.0"\r\n}\r\n'
        finding = self.findings[0].model_copy(update={"evidence_line": 1, "evidence_end_line": 3})
        files = {finding.File: source}
        content = review_response_content(ReviewResponse(findings=[finding]), files)
        self.assertEqual(json.loads(content)["findings"][0]["Evidence"], source[:-2])
        self.assertFalse(validate_review(content, files)[1])

    def test_invalid_and_oversized_ranges(self):
        for start, end, source in ((2, 1, 'a\nb'), (1, 3, 'a\nb'),
                                   (1, 21, 'a\n' * 21), (1, 1, 'a' * 1001)):
            with self.subTest(start=start, end=end):
                finding = self.findings[0].model_copy(update={"evidence_line": start, "evidence_end_line": end})
                with self.assertRaises(ValueError):
                    review_response_content(ReviewResponse(findings=[finding]), {finding.File: source})

    def test_line_numbers_are_strict_integers(self):
        for value in (True, "1", 0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReviewFinding.model_validate({**self.findings[0].model_dump(by_alias=True), "Evidence end line": value})

    def test_azure_schema_requires_nullable_end_line(self):
        schema = ReviewResponse.model_json_schema()["$defs"]["ReviewFinding"]
        self.assertIn("Evidence end line", schema["required"])


if __name__ == "__main__":
    unittest.main()
