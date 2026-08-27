from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass

from legal_funds_agent.domain.models import Claim, MatchLevel, Transaction


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


def match_claim_transactions(claim: Claim, transactions: list[Transaction], date_window_days: int = 3) -> list[CandidateMatch]:
    candidates: list[CandidateMatch] = []
    start, end = claim.time_start, claim.time_end
    for tx in transactions:
        payer_name_exact = tx.payer_name == claim.victim_name
        payer_account_exact = bool(claim.victim_account and tx.payer_account == claim.victim_account)
        payer_exact = payer_name_exact or payer_account_exact
        payee_name_exact = bool(claim.alleged_recipient_name and tx.payee_name == claim.alleged_recipient_name)
        payee_account_exact = bool(claim.alleged_recipient_account and tx.payee_account == claim.alleged_recipient_account)
        payee_exact = payee_name_exact or payee_account_exact
        in_range = start <= tx.date <= end
        in_window = start - timedelta(days=date_window_days) <= tx.date <= end + timedelta(days=date_window_days)
        if not (payer_exact and in_window):
            continue
        amount_match = "EXACT" if tx.amount == claim.claimed_amount else ("PARTIAL" if tx.amount < claim.claimed_amount else "EXCEEDS")
        rules = ["M01" if payer_account_exact else "M02", "M05" if in_range else "M06"]
        if payee_exact: rules.append("M03" if payee_account_exact else "M04")
        if amount_match == "EXACT": rules.append("M07")
        elif amount_match == "PARTIAL": rules.append("M08")
        else: rules.append("M09")
        risks: list[str] = []
        if not payee_exact: risks.append("THIRD_PARTY_RECIPIENT")
        if amount_match == "EXCEEDS": risks.append("AMOUNT_EXCEEDS_CLAIM")
        candidates.append(CandidateMatch(claim.id, tx.id, MatchLevel.EXACT if payer_exact else MatchLevel.MISMATCH, MatchLevel.EXACT if payee_exact else MatchLevel.MISMATCH, amount_match, "EXACT" if in_range else "WINDOW", tuple(rules), bool(risks), tuple(risks)))
    return candidates
