from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from legal_funds_agent.domain.models import Claim
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import SCHEMA_STATEMENT_FACT, supports_schema

REGEX_SOURCE = "regex_v0.1"


@dataclass(frozen=True)
class StatementPaymentFact:
    victim_name: str
    recipient_name: str | None
    amount: Decimal
    # None when the statement only anchors the payment by recipient and total
    # amount ("向陈某账户转了三笔钱……共240万元") without a full calendar date.
    payment_date: date | None
    source_text: str
    start_offset: int
    end_offset: int
    extraction_source: str = REGEX_SOURCE


# An amount may be written as "50000元" or "80万元"; the 万 multiplier is applied
# by ``_amount_from_match`` so both spellings land on the same Decimal value.
def _amount_from_match(match: re.Match[str]) -> Decimal:
    amount = Decimal(match["amount"].replace(",", ""))
    if match["wan"]:
        amount *= 10000
    return amount.quantize(Decimal("0.01"))


def _regex_fact(text: str, *, victim_name: str) -> StatementPaymentFact:
    match = re.search(
        r"(?:从)?(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
        r".*?(?:按照|向)(?P<recipient>[\u4e00-\u9fff]{1,3}某)(?:的)?(?:要求|指示)?.*?"
        r"(?:转款|转账|支付|转出|转了|转入|支付了|累计转入).*?(?:人民币)?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<wan>万)?元",
        text,
        re.DOTALL,
    )
    if not match:
        match = re.search(
            r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r".*?(?:转入|转给|转账给|付给)(?P<recipient>[\u4e00-\u9fff]{1,3}某).*?"
            r"(?:人民币)?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<wan>万)?元",
            text,
            re.DOTALL,
        )
    if match:
        return StatementPaymentFact(
            victim_name=victim_name,
            recipient_name=match["recipient"],
            amount=_amount_from_match(match),
            payment_date=date(int(match["year"]), int(match["month"]), int(match["day"])),
            source_text=match.group(0), start_offset=match.start(), end_offset=match.end(),
        )
    # Fallback for statements that name the recipient and the total but never
    # write a full calendar date ("向陈某本人账户转了三笔钱，每笔80万元，共240万元").
    # Only a total marked by 共/共计/累计 qualifies: a partial instalment amount
    # would be dressed up as the whole payment and fabricate an amount conflict.
    match = re.search(
        r"(?:向|给)(?P<recipient>[一-鿿]{1,3}某)(?:本人)?(?:的)?(?:账户|要求|指示)?[^。；;]*?"
        r"(?:共|共计|累计)(?:人民币)?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<wan>万)?元",
        text,
    )
    if not match:
        raise ValueError("victim statement payment fact could not be extracted")
    return StatementPaymentFact(
        victim_name=victim_name,
        recipient_name=match["recipient"],
        amount=_amount_from_match(match),
        payment_date=None,
        source_text=match.group(0), start_offset=match.start(), end_offset=match.end(),
    )


def _model_fact(row: dict, text: str, *, victim_name: str, provider_name: str) -> StatementPaymentFact:
    """Build a fact from a model row, re-locating ``source_text`` in the original material.

    A model cannot assert a payment that is not literally present: the span has to be
    found in the statement text and has to be unique, exactly like claim extraction.
    """
    source_text = str(row.get("source_text") or "")
    if not source_text:
        raise ValueError("statement source_text is empty")
    start_offset = text.find(source_text)
    if start_offset < 0:
        raise ValueError("statement source_text is not present in the statement text")
    if text.find(source_text, start_offset + 1) >= 0:
        raise ValueError("statement source_text matches multiple locations")
    try:
        raw_amount = str(row.get("amount") or "0").replace(",", "").strip()
        amount = Decimal(raw_amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"statement amount is not a decimal: {row.get('amount')!r}") from exc
    if amount <= Decimal("0"):
        raise ValueError("statement amount must be positive")
    raw_date = str(row.get("payment_date") or "")
    try:
        year, month, day = (int(part) for part in raw_date.split("-"))
        payment_date = date(year, month, day)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"statement payment_date is not YYYY-MM-DD: {raw_date!r}") from exc
    return StatementPaymentFact(
        victim_name=victim_name,
        recipient_name=str(row.get("recipient_name") or "").strip() or None,
        amount=amount,
        payment_date=payment_date,
        source_text=source_text,
        start_offset=start_offset,
        end_offset=start_offset + len(source_text),
        extraction_source=f"{provider_name}:{SCHEMA_STATEMENT_FACT}",
    )


def extract_statement_payment(
    text: str,
    *,
    victim_name: str,
    provider: LLMProvider | None = None,
    warnings: list[str] | None = None,
    allow_missing: bool = False,
) -> StatementPaymentFact | None:
    """Extract the victim's payment fact.

    When a provider that supports ``statement_fact_v0.1`` is supplied, the model reads the
    statement first. Every failure mode degrades to the deterministic regex instead of
    aborting the case, but each degradation is recorded in ``warnings`` so that a silent
    fallback is impossible. Without a provider the behaviour is exactly the regex path.

    ``allow_missing`` decides what happens when no payment fact can be established at all
    — for example a statement that only says "陆陆续续转了三十来万，记不清了". Raising
    (the default) keeps the historical contract; returning ``None`` lets the caller turn
    the gap into a human review item instead of stopping the whole case.
    """
    if provider is not None and supports_schema(provider, SCHEMA_STATEMENT_FACT):
        try:
            rows = provider.generate_structured(text=text, schema_name=SCHEMA_STATEMENT_FACT)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades to regex
            if warnings is not None:
                warnings.append(f"STATEMENT_MODEL_CALL_FAILED:{type(exc).__name__}")
            rows = None
        if rows:
            try:
                return _model_fact(rows[0], text, victim_name=victim_name, provider_name=provider.name)
            except ValueError as exc:
                if warnings is not None:
                    warnings.append(f"STATEMENT_MODEL_OUTPUT_REJECTED:{exc}")
        elif rows == [] and warnings is not None:
            warnings.append("STATEMENT_MODEL_NO_FACT")
    try:
        return _regex_fact(text, victim_name=victim_name)
    except ValueError:
        if not allow_missing:
            raise
        if warnings is not None:
            warnings.append("STATEMENT_FACT_UNAVAILABLE")
        return None


# Two heading conventions occur in real materials: the narrative form
# "被害人刘某陈述" and the transcript (笔录) form "被害人：刘某". The transcript
# form is what an actual 被害人陈述笔录 carries, so it has to be recognised or
# multi-victim cases silently collapse into a single unattributed statement.
_VICTIM_HEADING = re.compile(r"被害人(?P<name>[一-鿿]{1,4})陈述")
_VICTIM_LABEL = re.compile(r"被害人[:：]\s*(?P<name>[一-鿿]{1,4})")


def _victim_heading_matches(text: str):
    """Heading matches for whichever convention the document actually uses.

    The narrative form wins when present so existing materials keep their
    current sectioning; the transcript form is only used as a fallback.
    """
    matches = list(_VICTIM_HEADING.finditer(text))
    return matches or list(_VICTIM_LABEL.finditer(text))


def split_victim_statements(text: str) -> dict[str, str]:
    """Split a combined statement document into per-victim sections.

    Sections are delimited by "被害人X陈述" headings, or by "被害人：X" lines in
    material using the 笔录 (transcript) layout. Returns an empty dict when the
    text carries neither — the caller then treats the whole text as a single
    statement (the historical single-victim behaviour).
    """
    matches = _victim_heading_matches(text)
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match["name"]] = text[match.start():end]
    return sections


def compare_statement_to_claim(fact: StatementPaymentFact, claim: Claim) -> list[str]:
    conflicts: list[str] = []
    if fact.amount != claim.claimed_amount:
        conflicts.append("STATEMENT_AMOUNT_CONFLICT")
    if fact.payment_date is not None and not (claim.time_start <= fact.payment_date <= claim.time_end):
        conflicts.append("STATEMENT_DATE_CONFLICT")
    if claim.alleged_recipient_name and fact.recipient_name != claim.alleged_recipient_name:
        conflicts.append("STATEMENT_RECIPIENT_CONFLICT")
    return conflicts
