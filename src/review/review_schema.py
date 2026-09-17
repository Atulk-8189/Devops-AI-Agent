"""Structured schema for final infrastructure review responses."""
import json
import posixpath

from pydantic import BaseModel, ConfigDict, Field


class ReviewFinding(BaseModel):
    Finding: str
    File: str
    evidence_line: int = Field(alias="Evidence line")
    why_it_matters: str = Field(alias="Why it matters")
    verification_needed: str = Field(alias="Verification needed")
    Confidence: str

    model_config = ConfigDict(populate_by_name=True)


class ReviewResponse(BaseModel):
    findings: list[ReviewFinding] = Field(min_length=3, max_length=3)


def review_response_content(response: ReviewResponse, files: dict[str, str]) -> str:
    findings = []
    for finding in response.findings:
        path = posixpath.normpath("/" + finding.File.lstrip("/"))
        if path not in files:
            raise ValueError(f"Review rejected: referenced file was not read this turn: {path}.")
        lines = files[path].splitlines()
        if finding.evidence_line < 1 or finding.evidence_line > len(lines):
            raise ValueError(
                f"Review rejected: evidence line {finding.evidence_line} does not exist in {path}."
            )
        item = finding.model_dump(by_alias=True)
        item["File"] = path
        item["Evidence"] = lines[finding.evidence_line - 1]
        del item["Evidence line"]
        findings.append(item)
    return json.dumps({"findings": findings})