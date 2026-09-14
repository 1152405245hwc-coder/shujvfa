from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from legal_funds_agent.audit.logger import AuditEvent, completed_event, failed_event
from legal_funds_agent.domain.models import (
    Claim,
    DecisionType,
    ReviewDecision,
    SourceLocator,
    Transaction,
    TransactionReviewAction,
)
from legal_funds_agent.llm.mock_provider import MockProvider
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.provenance import prompt_fingerprint
from legal_funds_agent.llm.schemas import (
    SCHEMA_CLAIM_AUDIT,
    SCHEMA_PAYMENT_CLAIM,
    SCHEMA_STATEMENT_FACT,
)
from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions
from legal_funds_agent.services.candidate_matcher import (
    CandidateMatch,
    find_weak_payer_signals,
    match_claim_transactions,
)
from legal_funds_agent.services.claim_audit import ClaimAuditResult, audit_claim_extraction
from legal_funds_agent.services.claim_extractor import extract_claims
from legal_funds_agent.services.extraction_guard import collect_claim_anchoring_issues
from legal_funds_agent.services.entity_resolution import PartyAliasRegistry
from legal_funds_agent.services.report_service import build_report
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.statement_extractor import (
    StatementPaymentFact,
    compare_statement_to_claim,
    extract_statement_payment,
    split_victim_statements,
)
from legal_funds_agent.services.transaction_analysis import (
    normalize_party_name,
    transaction_canonical_key,
)
from legal_funds_agent.services.verification_engine import find_duplicate_transactions, verify_decision


@dataclass
class WorkflowResult:
    task_id: str
    claim: Claim
    claim_locators: list[SourceLocator]
    statement_fact: StatementPaymentFact | None
    statement_conflicts: list[str]
    duplicate_groups: dict[str, list[str]]
    transactions: dict[str, Transaction]
    candidates: list[CandidateMatch]
    system_decision: ReviewDecision
    audit_events: list[AuditEvent]
    claims: list[Claim] = field(default_factory=list)
    candidates_by_claim: dict[str, list[CandidateMatch]] = field(default_factory=dict)
    system_decisions_by_claim: dict[str, ReviewDecision] = field(default_factory=dict)
    # Extraction-quality signals. These never feed the deterministic money decision;
    # they exist so that a weak extraction is visible instead of silently accepted.
    extraction_issues: list[dict] = field(default_factory=list)
    statement_extraction_warnings: list[str] = field(default_factory=list)
    claim_audit: ClaimAuditResult | None = None
    # Reasons that force PENDING_REVIEW without asserting a contradiction, e.g. an
    # unusable victim statement. Recorded separately from statement_conflicts so that
    # "we could not check" is never dressed up as "the materials disagree".
    review_required_reasons: list[str] = field(default_factory=list)
    # Per-victim statement facts and per-claim statement conflicts. In a
    # multi-victim case a fact is only ever compared with its own victim's
    # claims; the singular fields above keep the first claim's view for
    # backwards compatibility.
    statement_facts_by_victim: dict[str, StatementPaymentFact | None] = field(default_factory=dict)
    statement_conflicts_by_claim: dict[str, list[str]] = field(default_factory=dict)
    # Display-only recall leads: transfers reaching a claim's recipient inside
    # the window but not paid by the victim (e.g. a relative paying on their
    # behalf). Never enter the candidate set, never affect amounts or decisions.
    weak_signals_by_claim: dict[str, list[CandidateMatch]] = field(default_factory=dict)
    # Human-confirmed party merges, recorded so the report can always answer
    # "why were these two names treated as one person".
    alias_registry: PartyAliasRegistry | None = None



class WorkflowExecutionError(RuntimeError):
    def __init__(self, message: str, audit_events: list[AuditEvent]):
        super().__init__(message)
        self.audit_events = audit_events


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fact_may_describe_claim(fact: StatementPaymentFact, claim: Claim) -> bool:
    """Conservative attribution test: nothing in the fact rules out this claim.

    A recipient mismatch rules the claim out; a full payment date outside the
    claim's stated range rules it out. A missing date rules nothing out.
    """
    if (
        claim.alleged_recipient_name and fact.recipient_name
        and normalize_party_name(fact.recipient_name) != normalize_party_name(claim.alleged_recipient_name)
    ):
        return False
    if (
        fact.payment_date is not None
        and claim.time_start is not None and claim.time_end is not None
    ):
        return claim.time_start <= fact.payment_date <= claim.time_end
    return True


def run_case_inputs(*, indictment_text: str, statement_text: str, csv_text: str,
                    case_id: str = "CASE-0001", task_id: str = "TASK-0001",
                    provider: LLMProvider | None = None,
                    allow_multiple_claims: bool = False,
                    statement_provider: LLMProvider | None = None,
                    enable_claim_audit: bool = False,
                    audit_provider: LLMProvider | None = None,
                    allow_missing_statement: bool = False,
                    alias_registry: PartyAliasRegistry | None = None,
                    transaction_evidence_id: str = "EVI-BANK-CSV") -> WorkflowResult:
    logs: list[AuditEvent] = []
    provider = provider or MockProvider()
    step, tool, started, step_input = "claim_extraction", f"{provider.name}_structured", datetime.now(timezone.utc), indictment_text
    try:
        claims, claim_locators = extract_claims(
            indictment_text, case_id=case_id, evidence_id="EVI-INDICTMENT", provider=provider
        )
        if not claims:
            raise ValueError("未能从起诉书中提取到资金主张，请检查起诉书文本或材料上传是否完整")
        if not allow_multiple_claims and len(claims) != 1:
            raise ValueError("V0.1 MVP currently supports exactly one PaymentClaim")
        claim = claims[0]
        metrics = getattr(provider, "last_call_metrics", {})
        logs.append(completed_event(
            task_id, case_id, step, tool, started, model=provider.name,
            prompt_version=getattr(provider, "prompt_version", None),
            input_hash=_hash_text(indictment_text), output_hash=_hash_text(claim.model_dump_json()),
            input_tokens=metrics.get("input_tokens"), output_tokens=metrics.get("output_tokens"),
            latency_ms=metrics.get("latency_ms"),
            details={"prompt_sha256": prompt_fingerprint(SCHEMA_PAYMENT_CLAIM)},
        ))

        # Extraction-quality guard: a claim amount must have a literal basis inside the
        # span the model cited. This is a review signal, not a funds-review risk, so it
        # deliberately stays out of system_risks and the money decision.
        extraction_issues = collect_claim_anchoring_issues(claims)
        if extraction_issues:
            logs.append(completed_event(
                task_id, case_id, "extraction_guard", "extraction_guard_v0.1",
                datetime.now(timezone.utc), details={"issues": extraction_issues},
            ))

        step, tool, started, step_input = "statement_comparison", "statement_parser_v0.1", datetime.now(timezone.utc), statement_text
        statement_warnings: list[str] = []
        # In a multi-victim case each victim's statement is its own section
        # ("被害人X陈述"). Comparing one victim's fact against another victim's
        # claim would fabricate a conflict, so facts are extracted per victim and
        # only ever compared with that victim's own claims. A victim without a
        # locatable statement section gets a review item, never a conflict.
        victim_keys: dict[str, str] = {}
        for c in claims:
            victim_keys.setdefault(normalize_party_name(c.victim_name), c.victim_name)
        multi_victim = len(victim_keys) > 1
        sections = split_victim_statements(statement_text) if multi_victim else {}
        statement_facts_by_victim: dict[str, StatementPaymentFact | None] = {}
        for key, raw_name in victim_keys.items():
            if multi_victim:
                section = sections.get(key)
                if section is None:
                    statement_facts_by_victim[key] = None
                    statement_warnings.append(f"STATEMENT_SECTION_MISSING:{key}")
                    continue
                text_for_victim = section
            else:
                text_for_victim = statement_text
            statement_facts_by_victim[key] = extract_statement_payment(
                text_for_victim, victim_name=raw_name,
                provider=statement_provider, warnings=statement_warnings,
                allow_missing=allow_missing_statement or multi_victim,
            )
        primary_victim_key = normalize_party_name(claim.victim_name)
        statement_fact = statement_facts_by_victim.get(primary_victim_key)
        statement_metrics = getattr(statement_provider, "last_call_metrics", {}) if statement_provider else {}
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            model=statement_provider.name if statement_provider else None,
            prompt_version=getattr(statement_provider, "prompt_version", None) if statement_provider else None,
            input_hash=_hash_text(statement_text),
            output_hash=_hash_text(";".join(
                f"{key}:{fact.extraction_source if fact else 'NONE'}"
                for key, fact in statement_facts_by_victim.items()
            )),
            input_tokens=statement_metrics.get("input_tokens"),
            output_tokens=statement_metrics.get("output_tokens"),
            latency_ms=statement_metrics.get("latency_ms"),
            details={
                "extraction_source": statement_fact.extraction_source if statement_fact else None,
                "warnings": statement_warnings,
                "victims": {
                    key: (fact.extraction_source if fact else None)
                    for key, fact in statement_facts_by_victim.items()
                },
                "prompt_sha256": (
                    prompt_fingerprint(SCHEMA_STATEMENT_FACT) if statement_provider else None
                ),
            },
        ))

        step, tool, started, step_input = "transaction_parser", "csv_parser_v0.1", datetime.now(timezone.utc), csv_text
        transactions = parse_transactions(csv_text, case_id=case_id, evidence_id=transaction_evidence_id)
        tx_index = {tx.id: tx for tx in transactions}
        duplicate_groups = find_duplicate_transactions(transactions)
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            input_hash=_hash_text(csv_text), output_hash=_hash_text("|".join(tx.dedup_fingerprint for tx in transactions)),
        ))

        step, tool, started, step_input = "candidate_matcher", "candidate_matcher_v0.1", datetime.now(timezone.utc), claim.model_dump_json()
        candidates_by_claim: dict[str, list[CandidateMatch]] = {}
        system_decisions_by_claim: dict[str, ReviewDecision] = {}
        statement_conflicts_by_claim: dict[str, list[str]] = {}
        review_reasons_by_claim: dict[str, list[str]] = {}
        duplicate_risk = ["DUPLICATE_TRANSACTION"] if duplicate_groups else []

        # Attribute each victim's statement fact to the claim it actually
        # describes. A fact about one payment must not "conflict" with the same
        # victim's other claims — those are simply not covered by the statement,
        # which is a review gap, not a contradiction.
        fact_attributed_claims: set[str] = set()
        claims_by_victim: dict[str, list[Claim]] = {}
        for c in claims:
            claims_by_victim.setdefault(normalize_party_name(c.victim_name), []).append(c)
        for key, victim_claims in claims_by_victim.items():
            fact = statement_facts_by_victim.get(key)
            if fact is None:
                continue
            compatible = [c for c in victim_claims if _fact_may_describe_claim(fact, c)]
            exact = [c for c in compatible if fact.amount == c.claimed_amount]
            for c in (exact or compatible[:1]):
                fact_attributed_claims.add(c.id)

        for c in claims:
            c_fact = statement_facts_by_victim.get(normalize_party_name(c.victim_name))
            if c_fact is None:
                c_conflicts = []
                c_review_reasons = ["STATEMENT_FACT_UNAVAILABLE"]
            elif c.id in fact_attributed_claims:
                c_conflicts = compare_statement_to_claim(c_fact, c)
                c_review_reasons = []
            else:
                c_conflicts = []
                c_review_reasons = ["STATEMENT_CLAIM_NOT_COVERED"]
            statement_conflicts_by_claim[c.id] = c_conflicts
            review_reasons_by_claim[c.id] = c_review_reasons
            c_risks = list(c_conflicts) + duplicate_risk
            c_candidates = match_claim_transactions(c, transactions, alias_registry=alias_registry)
            candidates_by_claim[c.id] = c_candidates
            c_decision = build_decision(
                c, tx_index, has_pending_candidates=bool(c_candidates),
                material_conflict=bool(c_risks), reason_codes=c_risks,
                review_required_reasons=c_review_reasons,
            )
            system_decisions_by_claim[c.id] = c_decision

        # Backwards-compatible singular fields: the first claim's view.
        statement_conflicts = statement_conflicts_by_claim[claim.id]
        review_required_reasons = review_reasons_by_claim[claim.id]

        # Display-only recall leads (e.g. 代付): visible to the reviewer, never
        # counted anywhere.
        all_victim_names = frozenset(
            normalize_party_name(c.victim_name) for c in claims if c.victim_name
        )
        weak_signals_by_claim = {
            c.id: find_weak_payer_signals(
                c, transactions, alias_registry=alias_registry,
                exclude_payer_names=all_victim_names,
            )
            for c in claims
        }

        # Cross-claim pre-warning: one canonical transfer recalled under more than
        # one claim would be double-counted if every claim included it. Flag the
        # risk at candidate level so the reviewer sees it before deciding. This is
        # a warning, not a blocking conflict — the decision how to allocate the
        # transfer belongs to the human reviewer.
        key_owners: dict[tuple[str, str, str, str], set[str]] = {}
        for claim_id, claim_candidates in candidates_by_claim.items():
            for candidate in claim_candidates:
                key_owners.setdefault(
                    transaction_canonical_key(tx_index[candidate.transaction_id]), set()
                ).add(claim_id)
        duplicated_keys = {key for key, owners in key_owners.items() if len(owners) > 1}
        if duplicated_keys:
            for claim_id, claim_candidates in candidates_by_claim.items():
                candidates_by_claim[claim_id] = [
                    replace(
                        candidate,
                        risk_codes=tuple(
                            dict.fromkeys(candidate.risk_codes + ("CROSS_CLAIM_DUPLICATION",))
                        ),
                    )
                    if transaction_canonical_key(tx_index[candidate.transaction_id]) in duplicated_keys
                    and "CROSS_CLAIM_DUPLICATION" not in candidate.risk_codes
                    else candidate
                    for candidate in claim_candidates
                ]

        candidates = candidates_by_claim[claim.id]
        system_decision = system_decisions_by_claim[claim.id]
        logs.append(completed_event(
            task_id, case_id, step, tool, started,
            input_hash=_hash_text(step_input), output_hash=_hash_text(str(candidates)),
        ))

        # Adversarial second pass for missed claims. Its output is a review queue only:
        # a suspected omission never becomes a Claim on its own.
        if alias_registry is not None and len(alias_registry):
            logs.append(completed_event(
                task_id, case_id, "party_alias", "entity_resolution_v0.1",
                datetime.now(timezone.utc), details=alias_registry.to_dict(),
            ))

        claim_audit: ClaimAuditResult | None = None
        if enable_claim_audit:
            audit_started = datetime.now(timezone.utc)
            claim_audit_provider = audit_provider or provider
            claim_audit = audit_claim_extraction(
                indictment_text=indictment_text, claims=claims,
                provider=claim_audit_provider, case_id=case_id,
            )
            audit_metrics = getattr(claim_audit_provider, "last_call_metrics", {}) or {}
            logs.append(completed_event(
                task_id, case_id, "claim_audit", "claim_audit_v0.1", audit_started,
                model=claim_audit.provider,
                input_tokens=audit_metrics.get("input_tokens"),
                output_tokens=audit_metrics.get("output_tokens"),
                latency_ms=audit_metrics.get("latency_ms"),
                details={
                    "checked": claim_audit.checked,
                    "missing_count": len(claim_audit.missing_claims),
                    "notes": claim_audit.notes,
                    "prompt_sha256": prompt_fingerprint(SCHEMA_CLAIM_AUDIT),
                },
            ))
    except Exception as exc:
        logs.append(failed_event(
            task_id, case_id, step, tool, started, exc,
            model=provider.name if step == "claim_extraction" else None,
            input_hash=_hash_text(step_input),
        ))
        raise WorkflowExecutionError(str(exc), logs) from exc
    return WorkflowResult(
        task_id=task_id,
        claim=claim,
        claim_locators=claim_locators,
        statement_fact=statement_fact,
        statement_conflicts=statement_conflicts,
        duplicate_groups=duplicate_groups,
        transactions=tx_index,
        candidates=candidates,
        system_decision=system_decision,
        audit_events=logs,
        claims=claims,
        candidates_by_claim=candidates_by_claim,
        system_decisions_by_claim=system_decisions_by_claim,
        extraction_issues=extraction_issues,
        statement_extraction_warnings=statement_warnings,
        claim_audit=claim_audit,
        review_required_reasons=review_required_reasons,
        statement_facts_by_victim=statement_facts_by_victim,
        statement_conflicts_by_claim=statement_conflicts_by_claim,
        weak_signals_by_claim=weak_signals_by_claim,
        alias_registry=alias_registry,
    )


def run_demo_case(case_dir: Path, *, provider: LLMProvider | None = None) -> WorkflowResult:
    return run_case_inputs(
        indictment_text=_read(case_dir / "indictment.txt"),
        statement_text=_read(case_dir / "victim_statement_zhang.txt"),
        csv_text=_read(case_dir / "transactions.csv"),
        provider=provider,
    )


def confirm_claim_extraction(claim: Claim) -> Claim:
    if claim.extraction_status not in {"model_extracted", "human_corrected", "human_confirmed"}:
        raise ValueError("claim extraction requires review before confirmation")
    return claim.model_copy(update={"extraction_status": "human_confirmed"})


def confirm_all_claims(claims: list[Claim]) -> list[Claim]:
    return [confirm_claim_extraction(c) for c in claims]


def confirm_transactions(result: WorkflowResult, transaction_ids: list[str], *, reviewer: str, claim_id: str | None = None) -> tuple[ReviewDecision, dict]:
    target_claim = result.claim
    if claim_id:
        target_claim = next((c for c in (result.claims or [result.claim]) if c.id == claim_id), result.claim)
    candidates = result.candidates_by_claim.get(target_claim.id, result.candidates) if result.candidates_by_claim else result.candidates
    included = set(transaction_ids)
    actions = [
        TransactionReviewAction(
            transaction_id=candidate.transaction_id,
            # A batch action may accept ordinary candidates, but it must never
            # silently override a deterministic blocking conflict.
            disposition=(
                "DISPUTED"
                if candidate.blocking_conflict
                else "INCLUDED" if candidate.transaction_id in included else "EXCLUDED"
            ),
            reason_code=(
                "THIRD_PARTY_RECIPIENT"
                if candidate.blocking_conflict
                else "MATCHED_CLAIM" if candidate.transaction_id in included else "UNRELATED_TRANSACTION"
            ),
        )
        for candidate in candidates
    ]
    return review_transactions(result, actions, reviewer=reviewer, claim_id=target_claim.id)


def review_transactions(result: WorkflowResult, actions: list[TransactionReviewAction], *, reviewer: str,
                        claim_id: str | None = None,
                        note: str | None = None,
                        supersedes: ReviewDecision | None = None) -> tuple[ReviewDecision, dict]:
    target_claim = result.claim
    if claim_id and claim_id != result.claim.id:
        target_claim = next((c for c in (result.claims or [result.claim]) if c.id == claim_id), result.claim)
    if target_claim.extraction_status != "human_confirmed":
        raise ValueError("CLAIM_EXTRACTION_CONFIRMATION_REQUIRED")
    candidates = result.candidates_by_claim.get(target_claim.id, result.candidates) if result.candidates_by_claim else result.candidates
    candidate_ids = {candidate.transaction_id for candidate in candidates}
    reviewed_ids = {action.transaction_id for action in actions}
    if len(reviewed_ids) != len(actions) or candidate_ids != reviewed_ids:
        raise ValueError("PENDING_CANDIDATE_REVIEW_REQUIRED")
    dispositions = {action.transaction_id: action.disposition for action in actions}
    blocking_ids = {
        candidate.transaction_id
        for candidate in candidates
        if candidate.blocking_conflict
    }
    if blocking_ids & {
        transaction_id
        for transaction_id, disposition in dispositions.items()
        if disposition == "INCLUDED"
    }:
        raise ValueError("BLOCKING_CANDIDATE_REQUIRES_DISPUTED")
    included = [key for key, value in dispositions.items() if value == "INCLUDED"]
    excluded = [key for key, value in dispositions.items() if value == "EXCLUDED"]
    disputed = [key for key, value in dispositions.items() if value == "DISPUTED"]
    previous = supersedes or result.system_decisions_by_claim.get(target_claim.id, result.system_decision)
    if previous.claim_id != target_claim.id:
        raise ValueError("SUPERSEDES_CLAIM_MISMATCH")
    if previous.case_id != target_claim.case_id:
        raise ValueError("SUPERSEDES_CASE_MISMATCH")
    claim_conflicts = result.statement_conflicts_by_claim.get(
        target_claim.id, result.statement_conflicts
    )
    decision = build_decision(
        target_claim,
        result.transactions,
        included=included, excluded=excluded, disputed=disputed,
        decision_type=DecisionType.HUMAN_CONFIRMED,
        version=previous.version + 1,
        supersedes_decision_id=previous.id,
        reviewer=reviewer,
        reviewed_at=datetime.now(timezone.utc),
        note=note,
        material_conflict=bool(claim_conflicts),
        reason_codes=claim_conflicts,
        transaction_review_actions=actions,
    )
    review_started = datetime.now(timezone.utc)
    counts = {
        "reviewer": reviewer,
        "decision_version": decision.version,
        "included_count": len(included),
        "excluded_count": len(excluded),
        "disputed_count": len(disputed),
    }
    result.audit_events.append(completed_event(
        result.task_id, target_claim.case_id, "human_review", "manual_review_v0.1",
        review_started, details=counts,
    ))
    verification_started = datetime.now(timezone.utc)
    errors = verify_decision(target_claim, decision, result.transactions)
    if errors:
        decision.verification_error_codes = errors
        error = ValueError(f"human confirmation blocked: {', '.join(errors)}")
        result.audit_events.append(failed_event(
            result.task_id, target_claim.case_id, "verification", "verification_engine_v0.1",
            verification_started, error, details={"verification_error_codes": errors},
        ))
        raise error
    result.audit_events.append(completed_event(
        result.task_id, target_claim.case_id, "verification", "verification_engine_v0.1",
        verification_started, details={"verification_error_codes": []},
    ))
    if result.system_decisions_by_claim:
        result.system_decisions_by_claim[target_claim.id] = decision
    return decision, build_report(
        result.claim, decision, result.transactions, claim_locators=result.claim_locators,
        statement_conflicts=result.statement_conflicts,
        duplicate_groups=result.duplicate_groups,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    result = run_demo_case(root / "sample_data" / "demo_case_001")
    result.claim = confirm_claim_extraction(result.claim)
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
