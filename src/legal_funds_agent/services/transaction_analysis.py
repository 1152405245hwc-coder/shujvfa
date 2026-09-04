from __future__ import annotations

import re
from collections.abc import Iterable
from legal_funds_agent.domain.models import Claim, Transaction


def normalize_party_name(value: str | None) -> str:
    """Return the party name without the bank/instrument detail in parentheses."""
    if not value:
        return ""
    text = str(value).strip()
    text = re.sub(r"\s*[（(].*[）)]\s*$", "", text)
    return text.strip()


def normalize_account_reference(value: str | None) -> str:
    """Normalize an account reference for comparison while retaining mask characters."""
    if not value:
        return ""
    return re.sub(r"[^0-9A-Za-z*]", "", str(value)).upper()


def transaction_canonical_key(tx: Transaction) -> tuple[str, str, str, str, str]:
    """Build a source-independent identity for one bank transfer event.

    Bank statement serial numbers are account-specific, so the identity is based on
    timestamp, amount, and both canonical account endpoints. Names are a fallback
    for legacy CSV rows that do not contain account identifiers.
    """
    payer = (
        tx.payer_account_id
        or normalize_account_reference(tx.payer_account)
        or normalize_party_name(tx.payer_name)
    )
    payee = (
        tx.payee_account_id
        or normalize_account_reference(tx.payee_account)
        or normalize_party_name(tx.payee_name)
    )
    time_text = tx.time.isoformat() if tx.time else ""
    return str(tx.date), time_text, f"{tx.amount:.2f}", payer, payee


def unique_transactions(transactions: Iterable[Transaction]) -> list[Transaction]:
    """Keep one traceable row for each canonical transfer event."""
    result: list[Transaction] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for tx in transactions:
        key = transaction_canonical_key(tx)
        if key in seen:
            continue
        seen.add(key)
        result.append(tx)
    return result


def _claim_account_ids(claim: Claim, *, side: str) -> set[str]:
    account_ids: set[str] = set()
    account_ref = claim.victim_account if side == "victim" else claim.alleged_recipient_account
    if account_ref:
        normalized = normalize_account_reference(account_ref)
        if normalized:
            account_ids.add(normalized)
    account_id = claim.alleged_recipient_account_id if side == "recipient" else None
    if account_id:
        account_ids.add(account_id)
    return account_ids


def identify_refund_transactions(
    claims: Iterable[Claim], transactions: Iterable[Transaction]
) -> list[Transaction]:
    """Identify possible return transfers from related accounts to victim accounts.

    This is deliberately relationship-based. A remark such as "收益" or "份额"
    is retained as evidence text but cannot by itself turn a payment into a refund.
    The returned rows are canonical unique events and remain subject to human review.
    """
    claims_list = list(claims)
    all_transactions = list(transactions)
    unique = unique_transactions(all_transactions)
    victim_names = {normalize_party_name(c.victim_name) for c in claims_list if c.victim_name}
    recipient_names = {
        normalize_party_name(c.alleged_recipient_name)
        for c in claims_list
        if c.alleged_recipient_name
    }
    victim_account_ids: set[str] = set()
    victim_account_refs: set[str] = set()
    for claim in claims_list:
        victim_account_ids.update(_claim_account_ids(claim, side="victim"))
        if claim.victim_account:
            victim_account_refs.add(normalize_account_reference(claim.victim_account))

    # Accounts receiving a payment from a victim are related accounts for this
    # case, including an intermediary such as A005.
    related_account_ids = set(victim_account_ids)
    related_names = set(recipient_names)
    for claim in claims_list:
        if claim.alleged_recipient_account_id:
            related_account_ids.add(claim.alleged_recipient_account_id)
    for tx in unique:
        payer_name = normalize_party_name(tx.payer_name)
        payee_name = normalize_party_name(tx.payee_name)
        if payer_name in victim_names:
            if tx.payee_account_id:
                related_account_ids.add(tx.payee_account_id)
            normalized_payee_account = normalize_account_reference(tx.payee_account)
            if normalized_payee_account:
                related_account_ids.add(normalized_payee_account)
            if payee_name:
                related_names.add(payee_name)

    refunds: list[Transaction] = []
    for tx in unique:
        payee_name = normalize_party_name(tx.payee_name)
        payer_name = normalize_party_name(tx.payer_name)
        to_victim = bool(
            (tx.payee_account_id and tx.payee_account_id in victim_account_ids)
            or normalize_account_reference(tx.payee_account) in victim_account_refs
            or payee_name in victim_names
        )
        from_related = bool(
            (tx.payer_account_id and tx.payer_account_id in related_account_ids)
            or payer_name in related_names
        )
        if to_victim and from_related and payer_name not in victim_names:
            refunds.append(tx)
    return refunds
