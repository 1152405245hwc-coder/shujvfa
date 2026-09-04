from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction
from legal_funds_agent.services.transaction_analysis import transaction_canonical_key


def verify_decision(claim: Claim, decision: ReviewDecision, transactions: dict[str, Transaction]) -> list[str]:
    errors: list[str] = []
    included = decision.included_transaction_ids
    if len(included) != len(set(included)):
        errors.append("DUPLICATE_INCLUDED_TRANSACTION")
    if any(tid not in transactions for tid in included + decision.excluded_transaction_ids + decision.disputed_transaction_ids):
        errors.append("TRANSACTION_NOT_FOUND")
    covered = sum((transactions[tid].amount for tid in set(included) if tid in transactions), Decimal("0"))
    included_keys = [transaction_canonical_key(transactions[tid]) for tid in set(included) if tid in transactions]
    if len(included_keys) != len(set(included_keys)):
        errors.append("DUPLICATE_TRANSACTION")
    disputed = sum((transactions[tid].amount for tid in set(decision.disputed_transaction_ids) if tid in transactions), Decimal("0"))
    if covered != decision.covered_amount:
        errors.append("AMOUNT_VERIFICATION_FAILED")
    if disputed != decision.disputed_amount:
        errors.append("DISPUTED_AMOUNT_VERIFICATION_FAILED")
    if max(claim.claimed_amount - covered, Decimal("0")) != decision.uncovered_amount:
        errors.append("UNRESOLVED_AMOUNT_VERIFICATION_FAILED")
    if decision.decision_type.value == "HUMAN_CONFIRMED":
        if not decision.reviewer or decision.reviewed_at is None:
            errors.append("HUMAN_REVIEW_METADATA_REQUIRED")
        if decision.version <= 1 or not decision.supersedes_decision_id:
            errors.append("DECISION_VERSION_CHAIN_INVALID")
    if decision.decision_type.value == "HUMAN_CONFIRMED" and errors:
        errors.append("HUMAN_CONFIRMATION_BLOCKED")
    return errors


def verify_case_decisions(decisions: list[ReviewDecision]) -> list[str]:
    included_by_transaction: dict[str, list[str]] = {}
    for decision in decisions:
        for transaction_id in set(decision.included_transaction_ids):
            included_by_transaction.setdefault(transaction_id, []).append(decision.claim_id)
    if any(len(set(claim_ids)) > 1 for claim_ids in included_by_transaction.values()):
        return ["CROSS_CLAIM_DUPLICATION"]
    return []


def find_duplicate_transactions(transactions: list[Transaction]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for tx in transactions:
        key = "|".join(transaction_canonical_key(tx))
        groups.setdefault(key, []).append(tx.id)
    return {fingerprint: ids for fingerprint, ids in groups.items() if len(ids) > 1}


@dataclass
class CaseReviewSummary:
    case_id: str
    total_claimed_amount: Decimal
    total_covered_amount: Decimal
    total_uncovered_amount: Decimal
    total_disputed_amount: Decimal
    claim_count: int
    fully_corroborated_count: int
    partially_corroborated_count: int
    unsupported_count: int
    pending_count: int
    conflicting_count: int
    cross_claim_errors: list[str]


def summarize_case_reviews(
    claims: list[Claim], decisions: list[ReviewDecision]
) -> CaseReviewSummary:
    case_id = claims[0].case_id if claims else (decisions[0].case_id if decisions else "")
    total_claimed = sum((c.claimed_amount for c in claims), Decimal("0"))
    total_covered = sum((d.covered_amount for d in decisions), Decimal("0"))
    total_uncovered = sum((d.uncovered_amount for d in decisions), Decimal("0"))
    total_disputed = sum((d.disputed_amount for d in decisions), Decimal("0"))

    status_counts = {
        "FULLY_CORROBORATED": 0,
        "PARTIALLY_CORROBORATED": 0,
        "UNSUPPORTED": 0,
        "PENDING_REVIEW": 0,
        "CONFLICTING": 0,
    }
    for d in decisions:
        status_name = d.status.value if hasattr(d.status, "value") else str(d.status)
        if status_name in status_counts:
            status_counts[status_name] += 1

    cross_errors = verify_case_decisions(decisions)
    return CaseReviewSummary(
        case_id=case_id,
        total_claimed_amount=total_claimed,
        total_covered_amount=total_covered,
        total_uncovered_amount=total_uncovered,
        total_disputed_amount=total_disputed,
        claim_count=len(claims),
        fully_corroborated_count=status_counts["FULLY_CORROBORATED"],
        partially_corroborated_count=status_counts["PARTIALLY_CORROBORATED"],
        unsupported_count=status_counts["UNSUPPORTED"],
        pending_count=status_counts["PENDING_REVIEW"],
        conflicting_count=status_counts["CONFLICTING"],
        cross_claim_errors=cross_errors,
    )
