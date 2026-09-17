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

    def test_two_findings_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse(findings=self.findings[:2])

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


if __name__ == "__main__":
    unittest.main()
