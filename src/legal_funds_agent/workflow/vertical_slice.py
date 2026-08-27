from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from legal_funds_agent.audit.logger import AuditEvent, completed_event, failed_event
from legal_funds_agent.domain.models import Claim, DecisionType, ReviewDecision, Transaction
from legal_funds_agent.llm.mock_provider import MockProvider
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions
from legal_funds_agent.services.candidate_matcher import CandidateMatch, match_claim_transactions
from legal_funds_agent.services.claim_extractor import extract_claims
from legal_funds_agent.services.report_service import build_report
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.statement_extractor import (
    StatementPaymentFact,
    compare_statement_to_claim,
    extract_statement_payment,
)
from legal_funds_agent.services.verification_engine import find_duplicate_transactions, verify_decision


@dataclass
class WorkflowResult:
    claim: Claim
    statement_fact: StatementPaymentFact
    statement_conflicts: list[str]
    duplicate_groups: dict[str, list[str]]
    transactions: dict[str, Transaction]
    candidates: list[CandidateMatch]
    system_decision: ReviewDecision
    audit_events: list[AuditEvent]


class WorkflowExecutionError(RuntimeError):
    def __init__(self, message: str, audit_events: list[AuditEvent]):
        super().__init__(message)
        self.audit_events = audit_events


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_case_inputs(*, indictment_text: str, statement_text: str, csv_text: str,
                    case_id: str = "CASE-0001", task_id: str = "TASK-0001",
                    provider: LLMProvider | None = None) -> WorkflowResult:
    logs: list[AuditEvent] = []
    provider = provider or MockProvider()
    step, tool, started, step_input = "claim_extraction", f"{provider.name}_structured", datetime.now(timezone.utc), indictment_text
    try:
        claims, _ = extract_claims(indictment_text, case_id=case_id, evidence_id="EVI-INDICTMENT", provider=provider)
        claim = claims[0]
        claim.extraction_status = "human_confirmed"
        metrics = getattr(provider, "last_call_metrics", {})
        logs.append(completed_event(
            task_id, case_id, step, tool, started, model=provider.name,
            prompt_version=getattr(provider, "prompt_version", None),
            input_hash=_hash_text(indictment_text), output_hash=_hash_text(claim.model_dump_json()),
            input_tokens=metrics.get("input_tokens"), output_tokens=metrics.get("output_tokens"),
            latency_ms=metrics.get("latency_ms"),
        ))

        step, tool, started, step_input = "statement_comparison", "statement_parser_v0.1", datetime.now(timezone.utc), statement_text
        statement_fact = extract_statement_payment(statement_text, victim_name=claim.victim_name)
        statement_conflicts = compare_statement_to_claim(statement_fact, claim)
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            input_hash=_hash_text(statement_text), output_hash=_hash_text("|".join(statement_conflicts)),
        ))

        step, tool, started, step_input = "transaction_parser", "csv_parser_v0.1", datetime.now(timezone.utc), csv_text
        transactions = parse_transactions(csv_text, case_id=case_id, evidence_id="EVI-BANK-CSV")
        tx_index = {tx.id: tx for tx in transactions}
        duplicate_groups = find_duplicate_transactions(transactions)
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            input_hash=_hash_text(csv_text), output_hash=_hash_text("|".join(tx.dedup_fingerprint for tx in transactions)),
        ))

        step, tool, started, step_input = "candidate_matcher", "candidate_matcher_v0.1", datetime.now(timezone.utc), claim.model_dump_json()
        candidates = match_claim_transactions(claim, transactions)
        system_risks = list(statement_conflicts)
        if duplicate_groups:
            system_risks.append("DUPLICATE_TRANSACTION")
        system_decision = build_decision(
            claim, tx_index, has_pending_candidates=bool(candidates),
            material_conflict=bool(system_risks), reason_codes=system_risks,
        )
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            input_hash=_hash_text(step_input), output_hash=_hash_text(str(candidates)),
        ))
    except Exception as exc:
        logs.append(failed_event(
            task_id, case_id, step, tool, started, exc,
            model=provider.name if step == "claim_extraction" else None,
            input_hash=_hash_text(step_input),
        ))
        raise WorkflowExecutionError(str(exc), logs) from exc
    return WorkflowResult(claim, statement_fact, statement_conflicts, duplicate_groups, tx_index, candidates, system_decision, logs)


def run_demo_case(case_dir: Path, *, provider: LLMProvider | None = None) -> WorkflowResult:
    return run_case_inputs(
        indictment_text=_read(case_dir / "indictment.txt"),
        statement_text=_read(case_dir / "victim_statement_zhang.txt"),
        csv_text=_read(case_dir / "transactions.csv"),
        provider=provider,
    )


def confirm_transactions(result: WorkflowResult, transaction_ids: list[str], *, reviewer: str) -> tuple[ReviewDecision, dict]:
    return review_transactions(result, {transaction_id: "INCLUDED" for transaction_id in transaction_ids}, reviewer=reviewer)


def review_transactions(result: WorkflowResult, dispositions: dict[str, str], *, reviewer: str,
                        note: str | None = None,
                        supersedes: ReviewDecision | None = None) -> tuple[ReviewDecision, dict]:
    allowed = {"INCLUDED", "EXCLUDED", "DISPUTED"}
    if any(value not in allowed for value in dispositions.values()):
        raise ValueError("unsupported transaction disposition")
    included = [key for key, value in dispositions.items() if value == "INCLUDED"]
    excluded = [key for key, value in dispositions.items() if value == "EXCLUDED"]
    disputed = [key for key, value in dispositions.items() if value == "DISPUTED"]
    previous = supersedes or result.system_decision
    decision = build_decision(
        result.claim,
        result.transactions,
        included=included, excluded=excluded, disputed=disputed,
        decision_type=DecisionType.HUMAN_CONFIRMED,
        version=previous.version + 1,
        supersedes_decision_id=previous.id,
        reviewer=reviewer,
        reviewed_at=datetime.now(timezone.utc),
        note=note,
        material_conflict=bool(result.statement_conflicts),
        reason_codes=result.statement_conflicts,
    )
    errors = verify_decision(result.claim, decision, result.transactions)
    if errors:
        raise ValueError(f"human confirmation blocked: {', '.join(errors)}")
    return decision, build_report(
        result.claim, decision, result.transactions, statement_conflicts=result.statement_conflicts,
        duplicate_groups=result.duplicate_groups,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    result = run_demo_case(root / "sample_data" / "demo_case_001")
    selected = [candidate.transaction_id for candidate in result.candidates if not candidate.blocking_conflict]
    decision, report = confirm_transactions(result, selected, reviewer="demo-reviewer")
    print(json.dumps({
        "system_status": result.system_decision.status.value,
        "human_status": decision.status.value,
        "covered_amount": str(decision.covered_amount),
        "uncovered_amount": str(decision.uncovered_amount),
        "candidate_count": len(result.candidates),
        "audit_steps": [event.step for event in result.audit_events],
        "disclaimer": report["disclaimer"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
