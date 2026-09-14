from __future__ import annotations

from datetime import date, timedelta
from dataclasses import dataclass
from decimal import Decimal

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

# Recall heuristic only: tolerates minor posting-date vs value-date drift when
# surfacing candidate transactions. It is not a legal determination of the
# transaction date; final correspondence is always confirmed by a human
# against the original bank records. Override per call if a case needs a
# different window.
DEFAULT_DATE_WINDOW_DAYS = 3


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


def _party_key(value: str | None, alias_registry) -> str:
    """Normalize a party name, then apply any human-confirmed alias mapping.

    Without a registry this is exactly ``normalize_party_name``, so behaviour is
    unchanged unless a reviewer has explicitly confirmed a merge.
    """
    normalized = normalize_party_name(value)
    if alias_registry is None:
        return normalized
    return alias_registry.resolve(normalized)


def match_claim_transactions(claim: Claim, transactions: list[Transaction],
                             date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
                             alias_registry=None) -> list[CandidateMatch]:
    candidates: list[CandidateMatch] = []
    seen_events: dict[tuple[str, str, str, str, str], set[str | None]] = {}
    raw_start, raw_end = claim.time_start, claim.time_end
    start = raw_start if raw_start is not None else date.min
    end = raw_end if raw_end is not None else date.max
    victim_key = _party_key(claim.victim_name, alias_registry)
    recipient_key = _party_key(claim.alleged_recipient_name, alias_registry)
    for tx in transactions:
        payer_name_exact = _party_key(tx.payer_name, alias_registry) == victim_key
        payer_account_exact = bool(claim.victim_account and tx.payer_account == claim.victim_account)
        payer_exact = payer_name_exact or payer_account_exact
        payee_name_exact = bool(
            claim.alleged_recipient_name
            and _party_key(tx.payee_name, alias_registry) == recipient_key
        )
        payee_account_exact = bool(claim.alleged_recipient_account and tx.payee_account == claim.alleged_recipient_account)
        payee_account_id_exact = bool(
            claim.alleged_recipient_account_id
            and tx.payee_account_id == claim.alleged_recipient_account_id
        )
        payee_exact = payee_name_exact or payee_account_exact or payee_account_id_exact
        in_range = start <= tx.date <= end
        if raw_start is None and raw_end is None:
            in_window = True
        else:
            window_start = start - timedelta(days=date_window_days) if raw_start is not None else date.min
            window_end = end + timedelta(days=date_window_days) if raw_end is not None else date.max
            in_window = window_start <= tx.date <= window_end
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
        if tx.amount == claim.claimed_amount:
            amount_match = "EXACT"
        elif tx.amount < claim.claimed_amount:
            partial_floor = max(claim.claimed_amount * Decimal("0.01"), Decimal("100"))
            amount_match = "PARTIAL" if tx.amount >= partial_floor else "NO_MATCH"
        else:
            amount_match = "EXCEEDS"
        rules = ["M01" if payer_account_exact else "M02", "M05" if in_range else "M06"]
        if payee_exact:
            rules.append("M03" if payee_account_exact or payee_account_id_exact else "M04")
        if amount_match == "EXACT": rules.append("M07")
        elif amount_match == "PARTIAL": rules.append("M08")
        elif amount_match == "EXCEEDS": rules.append("M09")
        risks: list[str] = []
        if claim.victim_account and tx.payer_account and claim.victim_account != tx.payer_account:
            risks.append("PAYER_ACCOUNT_MISMATCH")
        if claim.alleged_recipient_account and tx.payee_account and claim.alleged_recipient_account != tx.payee_account:
            risks.append("PAYEE_ACCOUNT_MISMATCH")
        if not payee_exact: risks.append("THIRD_PARTY_RECIPIENT")
        if amount_match == "EXCEEDS": risks.append("AMOUNT_EXCEEDS_CLAIM")
        candidates.append(CandidateMatch(claim.id, tx.id, MatchLevel.EXACT if payer_exact else MatchLevel.MISMATCH, MatchLevel.EXACT if payee_exact else MatchLevel.MISMATCH, amount_match, "EXACT" if in_range else "WINDOW", tuple(rules), bool(risks), tuple(risks)))
    return candidates


def find_weak_payer_signals(
    claim: Claim, transactions: list[Transaction],
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
    alias_registry=None,
    exclude_payer_names: frozenset[str] = frozenset(),
) -> list[CandidateMatch]:
    """Recall safety net: transfers that reach the claim's recipient inside the
    claim window but were NOT paid by the victim — e.g. a relative paying on the
    victim's behalf.

    These are display-only review leads. They never enter the candidate set,
    never affect amounts or decisions, and disappear once a human confirms the
    payer as an alias of the victim (the strict matcher then recalls them
    normally). Payers who are victims of other claims in the same case are
    excluded: those transfers are covered by their own claim.
    """
    signals: list[CandidateMatch] = []
    seen_events: set[tuple[str, str, str, str]] = set()
    raw_start, raw_end = claim.time_start, claim.time_end
    start = raw_start if raw_start is not None else date.min
    end = raw_end if raw_end is not None else date.max
    window_start = start - timedelta(days=date_window_days) if raw_start is not None else date.min
    window_end = end + timedelta(days=date_window_days) if raw_end is not None else date.max
    victim_key = _party_key(claim.victim_name, alias_registry)
    recipient_key = _party_key(claim.alleged_recipient_name, alias_registry)
    for tx in transactions:
        if not (window_start <= tx.date <= window_end):
            continue
        if _party_key(tx.payer_name, alias_registry) == victim_key:
            continue  # a strict candidate, not a weak signal
        if normalize_party_name(tx.payer_name) in exclude_payer_names:
            continue  # another victim's own payment, covered by their claim
        payee_is_recipient = bool(
            (recipient_key and _party_key(tx.payee_name, alias_registry) == recipient_key)
            or (claim.alleged_recipient_account and tx.payee_account == claim.alleged_recipient_account)
            or (claim.alleged_recipient_account_id and tx.payee_account_id == claim.alleged_recipient_account_id)
        )
        if not payee_is_recipient:
            continue
        partial_floor = max(claim.claimed_amount * Decimal("0.01"), Decimal("100"))
        if tx.amount < partial_floor:
            continue
        canonical_key = transaction_canonical_key(tx)
        if canonical_key in seen_events:
            continue
        seen_events.add(canonical_key)
        amount_match = (
            "EXACT" if tx.amount == claim.claimed_amount
            else "PARTIAL" if tx.amount < claim.claimed_amount
            else "EXCEEDS"
        )
        signals.append(CandidateMatch(
            claim.id, tx.id, MatchLevel.MISMATCH, MatchLevel.EXACT, amount_match,
            "EXACT" if start <= tx.date <= end else "WINDOW",
            ("W01",), False, ("WEAK_PAYER_SIGNAL",),
        ))
    return signals
