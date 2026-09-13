from datetime import date
from decimal import Decimal

from legal_funds_agent.domain.models import Claim
from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions
from legal_funds_agent.services.candidate_matcher import match_claim_transactions


HEADER = "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"


def make_claim(amount: str = "100000.00") -> Claim:
    return Claim(
        id="CLM-001",
        case_id="CASE-001",
        victim_name="张某",
        victim_account="6222",
        alleged_recipient_name="李某",
        alleged_recipient_account="6217",
        claimed_amount=Decimal(amount),
        time_start=date(2026, 3, 15),
        time_end=date(2026, 3, 15),
        source_locator_ids=["L1"],
        extraction_status="human_confirmed",
    )


def parse(*rows: str) -> list:
    return parse_transactions(HEADER + "\n".join(rows) + "\n", case_id="CASE-001", evidence_id="EVI-CSV")


def test_tiny_amounts_are_not_partial():
    txs = parse(
        "T001,2026-03-15,10:00:00,张某,6222,李某,6217,0.01,测试",
        "T002,2026-03-15,10:00:00,张某,6222,李某,6217,50.00,测试",
    )
    candidates = match_claim_transactions(make_claim("100000.00"), txs)
    assert len(candidates) == 2
    assert all(c.amount_match != "PARTIAL" for c in candidates)
    assert candidates[0].amount_match == "NO_MATCH"
    assert candidates[1].amount_match == "NO_MATCH"


def test_amounts_above_one_percent_floor_are_partial():
    txs = parse("T001,2026-03-15,10:00:00,张某,6222,李某,6217,30000.00,测试")
    candidates = match_claim_transactions(make_claim("50000.00"), txs)
    assert len(candidates) == 1
    assert candidates[0].amount_match == "PARTIAL"


def test_exact_and_exceeds_are_unchanged():
    txs = parse(
        "T001,2026-03-15,10:00:00,张某,6222,李某,6217,100000.00,测试",
        "T002,2026-03-15,10:00:00,张某,6222,李某,6217,150000.00,测试",
    )
    candidates = match_claim_transactions(make_claim("100000.00"), txs)
    assert {c.transaction_id: c.amount_match for c in candidates} == {
        "TX-T001": "EXACT",
        "TX-T002": "EXCEEDS",
    }
