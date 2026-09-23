"""Structured schema for final infrastructure review responses."""
import json
import posixpath

from pydantic import BaseModel, ConfigDict, Field
from src.review.review_validation import MAX_REVIEW_EXCERPT_CHARS

MAX_REVIEW_EXCERPT_LINES = 20


class ReviewFinding(BaseModel):
    Finding: str
    File: str
    evidence_line: int = Field(alias="Evidence line", strict=True, ge=1)
    evidence_end_line: int | None = Field(default=None, alias="Evidence end line", strict=True, ge=1)
    why_it_matters: str = Field(alias="Why it matters")
    verification_needed: str = Field(alias="Verification needed")
    Confidence: str

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ReviewResponse(BaseModel):
    findings: list[ReviewFinding] = Field(max_length=3)

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        # Azure strict output requires nullable fields to be explicitly present.
        finding = schema["$defs"]["ReviewFinding"]
        finding["required"] = list(finding["properties"])
        for field in finding["properties"].values():
            field.pop("default", None)
        return schema


def review_response_content(response: ReviewResponse, files: dict[str, str]) -> str:
    findings = []
    for finding in response.findings:
        path = posixpath.normpath("/" + finding.File.lstrip("/"))
        if path not in files:
            raise ValueError(f"Review rejected: referenced file was not read this turn: {path}.")
        lines = files[path].splitlines(keepends=True)
        end = finding.evidence_end_line if finding.evidence_end_line is not None else finding.evidence_line
        if finding.evidence_line < 1 or end < finding.evidence_line or end > len(lines):
            raise ValueError(
                f"Review rejected: evidence line {finding.evidence_line} does not exist in {path}."
            )
        if end - finding.evidence_line + 1 > MAX_REVIEW_EXCERPT_LINES:
            raise ValueError("Review rejected: evidence range exceeds line limit.")
        excerpt = "".join(lines[finding.evidence_line - 1:end])
        # Exclude only the final line separator; retain all internal source bytes.
        excerpt = excerpt.removesuffix("\n").removesuffix("\r")
        if len(excerpt) > MAX_REVIEW_EXCERPT_CHARS:
            raise ValueError("Review rejected: evidence excerpt exceeds character limit.")
        item = finding.model_dump(by_alias=True)
        item["File"] = path
        item["Evidence"] = excerpt
        findings.append(item)
    return json.dumps({"findings": findings})
