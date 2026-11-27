"""Typed role results and the side-effect-free M2 role simulation."""

from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchResult(RoleResult):
    kind: Literal["findings"] = "findings"
    source_file: Literal["csv_import.py"]
    finding: str = Field(min_length=1, max_length=1000)


class TestEvidence(RoleResult):
    command: Literal["python -B -m unittest -v"]
    baseline_exit: int
    candidate_exit: int


class CodingResult(RoleResult):
    kind: Literal["candidate"] = "candidate"
    source_file: Literal["csv_import.py"]
    change: str = Field(min_length=1, max_length=2000)
    digest: str = Field(pattern="^[0-9a-f]{64}$")
    evidence: TestEvidence | None = None


class ReviewResult(RoleResult):
    kind: Literal["review"] = "review"
    candidate_id: str
    verdict: Literal["pass", "revise"]
    reason: str = Field(min_length=1, max_length=1000)


RESULT_TYPES = {"research": ResearchResult, "coding": CodingResult, "review": ReviewResult}


def candidate_digest(change: str) -> str:
    return sha256(change.encode()).hexdigest()


def evidence_problem(candidate: CodingResult) -> str | None:
    if candidate.digest != candidate_digest(candidate.change):
        return "Candidate digest does not match the simulated change"
    if candidate.evidence is None:
        return "Candidate is missing runtime test evidence"
    if candidate.evidence.baseline_exit != 1 or candidate.evidence.candidate_exit != 0:
        return "Candidate lacks a failing baseline and a passing regression"
    return None


def simulate_role(node_id: str, previous: dict | None) -> RoleResult:
    if node_id == "research":
        return ResearchResult(source_file="csv_import.py", finding="Empty record from CSV reader")
    if node_id == "coding":
        findings = ResearchResult.model_validate(previous["payload"])
        change = "Simulated CSV importer skips empty records"
        return CodingResult(
            source_file=findings.source_file, change=change, digest=candidate_digest(change),
            evidence=TestEvidence(command="python -B -m unittest -v", baseline_exit=1, candidate_exit=0),
        )
    candidate = CodingResult.model_validate(previous["payload"])
    problem = evidence_problem(candidate)
    return ReviewResult(candidate_id=previous["attempt_id"], verdict="revise" if problem else "pass",
                        reason=problem or "Simulated candidate matches runtime test evidence")
