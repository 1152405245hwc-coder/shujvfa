from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from legal_funds_agent.domain.models import Claim, DecisionType, ReviewDecision, ReviewStatus, Transaction


def build_decision(claim: Claim, transactions: dict[str, Transaction], *, included: list[str] | None = None,
                   excluded: list[str] | None = None, disputed: list[str] | None = None,
                   decision_type: DecisionType = DecisionType.SYSTEM_PROPOSED, version: int = 1,
                   supersedes_decision_id: str | None = None, reviewer: str | None = None,
                   reviewed_at: datetime | None = None, note: str | None = None,
                   reason_codes: list[str] | None = None, material_conflict: bool = False,
                   has_pending_candidates: bool = False) -> ReviewDecision:
    included = included or []
    excluded = excluded or []
    disputed = disputed or []
    if set(included) & (set(excluded) | set(disputed)) or set(excluded) & set(disputed):
        raise ValueError("included, excluded and disputed transaction sets must not overlap")
    covered = sum((transactions[tid].amount for tid in set(included)), Decimal("0"))
    disputed_amount = sum((transactions[tid].amount for tid in set(disputed)), Decimal("0"))
    uncovered = max(claim.claimed_amount - covered, Decimal("0"))
    if material_conflict:
        status = ReviewStatus.CONFLICTING
        reasons = [*(reason_codes or []), "MATERIAL_EVIDENCE_CONFLICT"]
    elif covered > claim.claimed_amount:
        status = ReviewStatus.CONFLICTING
        reasons = [*(reason_codes or []), "OVER_COVERED_AMOUNT"]
    elif disputed or has_pending_candidates:
        status = ReviewStatus.PENDING_REVIEW
        reasons = [*(reason_codes or []), "DISPUTED_TRANSACTION"]
    elif covered == claim.claimed_amount:
        status = ReviewStatus.FULLY_CORROBORATED
        reasons = reason_codes or []
    elif covered > 0:
        status = ReviewStatus.PARTIALLY_CORROBORATED
        reasons = [*(reason_codes or []), "AMOUNT_MISMATCH"]
    else:
        status = ReviewStatus.UNSUPPORTED
        reasons = [*(reason_codes or []), "MISSING_TRANSACTION"]
    return ReviewDecision(id=f"DEC-{claim.id}-v{version}", case_id=claim.case_id, claim_id=claim.id,
        version=version, decision_type=decision_type, supersedes_decision_id=supersedes_decision_id,
        status=status, included_transaction_ids=list(included), excluded_transaction_ids=list(excluded),
        disputed_transaction_ids=list(disputed), covered_amount=covered, uncovered_amount=uncovered,
        disputed_amount=disputed_amount, reason_codes=reasons, reviewer=reviewer, reviewed_at=reviewed_at,
        note=note,
        verification_error_codes=[])
