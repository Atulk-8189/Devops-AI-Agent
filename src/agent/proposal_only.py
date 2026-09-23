"""Explicit CLI: python -m src.agent.proposal_only REQUEST.json POLICY.json.

Inputs are operator-owned local files, not model or MCP instructions. This
entry point collects evidence, displays a proposal, and exits. No executor.
"""
import argparse
import asyncio
import getpass
import hashlib
from pathlib import Path

from src.agent.proposals import Action, StrictModel, Text, build_proposal, display_proposal
from src.mcp.runtime import MCPRuntime
from src.policy.write_policy import WritePolicy
from src.review.terraform_evidence import gather_terraform_evidence
from src.review.review_validation import retrieved_files
from src.safe_diagnostics import classify_error


class ProposalRequest(StrictModel):
    action: Action
    question: Text
    expected_impact: Text


async def propose_only(request, policy, runtime, *, approval_audit=None):
    # Fail closed before opening MCP for an out-of-scope request.
    action = request.action
    preliminary = build_proposal(action, requested_by=getpass.getuser(), reason=request.question,
                                 expected_impact=request.expected_impact, risk_level="high", policy=policy)
    if action.target.branch != "main" or not action.target.path.startswith("/terraform/"):
        raise PermissionError("Current proposal evidence supports only /terraform files on main")
    await runtime.initialize()
    try:
        messages = await gather_terraform_evidence(runtime.tools, log=lambda *args: None)
        files = retrieved_files(messages)
        if messages.discovery.get("repository_id") != action.target.repository_id:
            raise PermissionError("Evidence repository does not match proposal")
        source = files.get(action.target.path)
        if source is None or hashlib.sha256(source.encode("utf-8")).hexdigest() != action.preconditions.original_content_sha256:
            raise ValueError("Original content is unavailable or does not match precondition")
        # Rebuild after collection so expiration covers the displayed proposal.
        proposal = build_proposal(action, requested_by=preliminary.requested_by, reason=request.question,
                                  expected_impact=request.expected_impact, risk_level="high", policy=policy)
        if approval_audit is not None:
            from src.agent.approval import ApprovalSession
            session = ApprovalSession(proposal, policy, approval_audit)
            record = session.request_from_terminal(proposal, policy)
            return ("Approval status: " + record.approval_status
                    + "\nSTOP: approval recorded for this session only; writes remain disabled. No execution occurred.")
        return (display_proposal(proposal) + "\nCommit precondition: operator supplied, NOT verified."
                + "\nDiscovery incomplete: " + str(messages.discovery.get("incomplete", True))
                + "\nSTOP: proposal only; no approval or execution is available.")
    finally:
        await runtime.close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--approval-audit", type=Path, help="Request terminal approval; create a NEW owner-only JSONL audit file")
    args = parser.parse_args()
    try:
        request = ProposalRequest.model_validate_json(args.request.read_text())
        policy = WritePolicy.model_validate_json(args.policy.read_text())
        if args.approval_audit is None:
            print(asyncio.run(propose_only(request, policy, MCPRuntime())))
        else:
            from src.agent.approval import AuditLog
            audit = AuditLog(args.approval_audit)
            try:
                print(asyncio.run(propose_only(request, policy, MCPRuntime(), approval_audit=audit)))
            finally:
                audit.close()
    except Exception as error:
        # Pydantic/MCP exceptions can embed credentials or input payloads.
        print("Proposal unavailable. " + classify_error(error).user_message() + " No write performed.")
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
