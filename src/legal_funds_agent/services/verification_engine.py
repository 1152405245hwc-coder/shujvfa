from __future__ import annotations

from decimal import Decimal

from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction


def verify_decision(claim: Claim, decision: ReviewDecision, transactions: dict[str, Transaction]) -> list[str]:
    errors: list[str] = []
    included = decision.included_transaction_ids
    if len(included) != len(set(included)):
        errors.append("DUPLICATE_INCLUDED_TRANSACTION")
    if any(tid not in transactions for tid in included + decision.excluded_transaction_ids + decision.disputed_transaction_ids):
        errors.append("TRANSACTION_NOT_FOUND")
    covered = sum((transactions[tid].amount for tid in set(included) if tid in transactions), Decimal("0"))
    included_fingerprints = [transactions[tid].dedup_fingerprint for tid in set(included) if tid in transactions]
    if len(included_fingerprints) != len(set(included_fingerprints)):
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
        groups.setdefault(tx.dedup_fingerprint, []).append(tx.id)
    return {fingerprint: ids for fingerprint, ids in groups.items() if len(ids) > 1}
