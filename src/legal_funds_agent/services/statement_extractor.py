from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from legal_funds_agent.domain.models import Claim


@dataclass(frozen=True)
class StatementPaymentFact:
    victim_name: str
    recipient_name: str | None
    amount: Decimal
    payment_date: date
    source_text: str
    start_offset: int
    end_offset: int


def extract_statement_payment(text: str, *, victim_name: str) -> StatementPaymentFact:
    match = re.search(
        r"(?:从)?(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
        r".*?(?:按照|向)(?P<recipient>[\u4e00-\u9fff]{1,3}某)(?:的)?(?:要求|指示)?.*?"
        r"(?:转款|转账|支付|转出|转了|转入|支付了|累计转入).*?(?:人民币)?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)元",
        text,
        re.DOTALL,
    )
    if not match:
        match = re.search(
            r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r".*?(?:转入|转给|转账给|付给)(?P<recipient>[\u4e00-\u9fff]{1,3}某).*?"
            r"(?:人民币)?(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)元",
            text,
            re.DOTALL,
        )
    if not match:
        raise ValueError("victim statement payment fact could not be extracted")
    return StatementPaymentFact(
        victim_name=victim_name,
        recipient_name=match["recipient"],
        amount=Decimal(match["amount"].replace(",", "")).quantize(Decimal("0.01")),
        payment_date=date(int(match["year"]), int(match["month"]), int(match["day"])),
        source_text=match.group(0), start_offset=match.start(), end_offset=match.end(),
    )


def compare_statement_to_claim(fact: StatementPaymentFact, claim: Claim) -> list[str]:
    conflicts: list[str] = []
    if fact.amount != claim.claimed_amount:
        conflicts.append("STATEMENT_AMOUNT_CONFLICT")
    if not (claim.time_start <= fact.payment_date <= claim.time_end):
        conflicts.append("STATEMENT_DATE_CONFLICT")
    if claim.alleged_recipient_name and fact.recipient_name != claim.alleged_recipient_name:
        conflicts.append("STATEMENT_RECIPIENT_CONFLICT")
    return conflicts
