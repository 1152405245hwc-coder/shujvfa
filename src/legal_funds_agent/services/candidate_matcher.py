from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass

from legal_funds_agent.domain.models import Claim, MatchLevel, Transaction
from legal_funds_agent.services.transaction_analysis import normalize_party_name, transaction_canonical_key


@dataclass(frozen=True)
class CandidateMatch:
    claim_id: str
    transaction_id: str
    payer_match: MatchLevel
    payee_match: MatchLevel
    amount_match: str
    date_match: str
    matched_rules: tuple[str, ...]
    blocking_conflict: bool
    risk_codes: tuple[str, ...]


RISK_WEIGHTS = {
    "CROSS_CLAIM_DUPLICATION": 120,
    "DUPLICATE_TRANSACTION": 110,
    "THIRD_PARTY_RECIPIENT": 100,
    "PAYER_ACCOUNT_MISMATCH": 90,
    "PAYEE_ACCOUNT_MISMATCH": 90,
    "AMOUNT_EXCEEDS_CLAIM": 80,
    "STATEMENT_AMOUNT_CONFLICT": 70,
    "STATEMENT_DATE_CONFLICT": 70,
    "STATEMENT_RECIPIENT_CONFLICT": 70,
}


def candidate_risk_level(candidate: CandidateMatch) -> str:
    """Return a deterministic review priority for presentation only."""
    if candidate.blocking_conflict or candidate.risk_codes:
        return "高"
    return "低"


def candidate_review_priority(candidate: CandidateMatch) -> int:
    """Score candidates so the most consequential checks appear first."""
    return max((RISK_WEIGHTS.get(code, 50) for code in candidate.risk_codes), default=0)


def sort_candidates_for_review(
    candidates: list[CandidateMatch], transactions: dict[str, Transaction]
) -> list[CandidateMatch]:
    """Sort by audit risk, then amount, date, and stable transaction number."""
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate_review_priority(candidate),
            -float(transactions[candidate.transaction_id].amount),
            transactions[candidate.transaction_id].date,
            transactions[candidate.transaction_id].transaction_id,
        ),
    )


def match_claim_transactions(claim: Claim, transactions: list[Transaction], date_window_days: int = 3) -> list[CandidateMatch]:
    candidates: list[CandidateMatch] = []
    seen_events: dict[tuple[str, str, str, str, str], set[str | None]] = {}
    start, end = claim.time_start, claim.time_end
    for tx in transactions:
        payer_name_exact = normalize_party_name(tx.payer_name) == normalize_party_name(claim.victim_name)
        payer_account_exact = bool(claim.victim_account and tx.payer_account == claim.victim_account)
        payer_exact = payer_name_exact or payer_account_exact
        payee_name_exact = bool(
            claim.alleged_recipient_name
            and normalize_party_name(tx.payee_name) == normalize_party_name(claim.alleged_recipient_name)
        )
        payee_account_exact = bool(claim.alleged_recipient_account and tx.payee_account == claim.alleged_recipient_account)
        payee_account_id_exact = bool(
            claim.alleged_recipient_account_id
            and tx.payee_account_id == claim.alleged_recipient_account_id
        )
        payee_exact = payee_name_exact or payee_account_exact or payee_account_id_exact
        in_range = start <= tx.date <= end
        in_window = start - timedelta(days=date_window_days) <= tx.date <= end + timedelta(days=date_window_days)
        if not (payer_exact and in_window):
            continue
        canonical_key = transaction_canonical_key(tx)
        prior_sources = seen_events.get(canonical_key, set())
        # A workbook contains both sides of one transfer in different account
        # sheets. Keep the first side, but preserve same-sheet duplicates so the
        # safeguard can still block their joint inclusion.
        if (
            tx.source_account_id is not None
            and prior_sources
            and all(source is not None and source != tx.source_account_id for source in prior_sources)
        ):
            continue
        seen_events.setdefault(canonical_key, set()).add(tx.source_account_id)
        amount_match = "EXACT" if tx.amount == claim.claimed_amount else ("PARTIAL" if tx.amount < claim.claimed_amount else "EXCEEDS")
        rules = ["M01" if payer_account_exact else "M02", "M05" if in_range else "M06"]
        if payee_exact:
            rules.append("M03" if payee_account_exact or payee_account_id_exact else "M04")
        if amount_match == "EXACT": rules.append("M07")
        elif amount_match == "PARTIAL": rules.append("M08")
        else: rules.append("M09")
        risks: list[str] = []
        if claim.victim_account and tx.payer_account and claim.victim_account != tx.payer_account:
            risks.append("PAYER_ACCOUNT_MISMATCH")
        if claim.alleged_recipient_account and tx.payee_account and claim.alleged_recipient_account != tx.payee_account:
            risks.append("PAYEE_ACCOUNT_MISMATCH")
        if not payee_exact: risks.append("THIRD_PARTY_RECIPIENT")
        if amount_match == "EXCEEDS": risks.append("AMOUNT_EXCEEDS_CLAIM")
        candidates.append(CandidateMatch(claim.id, tx.id, MatchLevel.EXACT if payer_exact else MatchLevel.MISMATCH, MatchLevel.EXACT if payee_exact else MatchLevel.MISMATCH, amount_match, "EXACT" if in_range else "WINDOW", tuple(rules), bool(risks), tuple(risks)))
    return candidates
