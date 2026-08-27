import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from legal_funds_agent.domain.models import Claim, DecisionType
from legal_funds_agent.parsers.transaction_csv_parser import parse_transactions
from legal_funds_agent.services.candidate_matcher import match_claim_transactions
from legal_funds_agent.services.review_engine import build_decision
from legal_funds_agent.services.verification_engine import (
    find_duplicate_transactions,
    verify_case_decisions,
    verify_decision,
)


HEADER = "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"


def make_claim(claim_id="CLM-001", amount="50000.00"):
    return Claim(
        id=claim_id,
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


def parse(*rows):
    return parse_transactions(HEADER + "\n".join(rows) + "\n", case_id="CASE-001", evidence_id="EVI-CSV")


def index(txs):
    return {tx.id: tx for tx in txs}


class CoreReviewCases(unittest.TestCase):
    def test_d01_partial_coverage(self):
        txs = parse("T001,2026-03-15,14:31:22,张某,6222,李某,6217,30000,投资款")
        decision = build_decision(make_claim(), index(txs), included=["TX-T001"])
        self.assertEqual(decision.status.value, "PARTIALLY_CORROBORATED")
        self.assertEqual(decision.covered_amount, Decimal("30000.00"))
        self.assertEqual(decision.uncovered_amount, Decimal("20000.00"))
        self.assertEqual(verify_decision(make_claim(), decision, index(txs)), [])

    def test_d02_full_coverage(self):
        txs = parse("T002,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款")
        decision = build_decision(make_claim(), index(txs), included=["TX-T002"])
        self.assertEqual(decision.status.value, "FULLY_CORROBORATED")

    def test_d03_no_transaction_is_unsupported(self):
        decision = build_decision(make_claim(), {})
        self.assertEqual(decision.status.value, "UNSUPPORTED")
        self.assertEqual(decision.covered_amount, Decimal("0"))

    def test_d04_split_transactions_sum_exactly(self):
        txs = parse(
            "T004,2026-03-15,10:00:00,张某,6222,李某,6217,20000,投资款",
            "T005,2026-03-15,11:00:00,张某,6222,李某,6217,30000,投资款",
        )
        decision = build_decision(make_claim(), index(txs), included=["TX-T004", "TX-T005"])
        self.assertEqual(decision.status.value, "FULLY_CORROBORATED")

    def test_d05_same_amount_different_payer_not_recalled(self):
        txs = parse("T006,2026-03-15,10:00:00,王某,6333,李某,6217,50000,投资款")
        self.assertEqual(match_claim_transactions(make_claim(), txs), [])

    def test_d06_third_party_recipient_is_disputed(self):
        txs = parse("T007,2026-03-15,10:00:00,张某,6222,王某,6888,50000,投资款")
        candidates = match_claim_transactions(make_claim(), txs)
        self.assertIn("THIRD_PARTY_RECIPIENT", candidates[0].risk_codes)
        decision = build_decision(make_claim(), index(txs), disputed=["TX-T007"])
        self.assertEqual(decision.status.value, "PENDING_REVIEW")

    def test_d07_over_amount_is_not_split_automatically(self):
        txs = parse("T008,2026-03-15,10:00:00,张某,6222,李某,6217,60000,投资款")
        candidate = match_claim_transactions(make_claim(), txs)[0]
        self.assertEqual(candidate.amount_match, "EXCEEDS")
        self.assertIn("AMOUNT_EXCEEDS_CLAIM", candidate.risk_codes)

    def test_d08_duplicate_rows_are_detected(self):
        txs = parse(
            "T009,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款",
            "T010,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款",
        )
        duplicates = find_duplicate_transactions(txs)
        self.assertEqual(list(duplicates.values()), [["TX-T009", "TX-T010"]])

    def test_d09_cross_claim_duplication_is_blocked(self):
        txs = parse("T011,2026-03-15,10:00:00,张某,6222,李某,6217,50000,投资款")
        first = build_decision(make_claim("CLM-001"), index(txs), included=["TX-T011"])
        second = build_decision(make_claim("CLM-002"), index(txs), included=["TX-T011"])
        self.assertEqual(verify_case_decisions([first, second]), ["CROSS_CLAIM_DUPLICATION"])

    def test_d10_material_conflict_overrides_coverage(self):
        txs = parse("T012,2026-03-15,10:00:00,张某,6222,李某,6217,30000,投资款")
        decision = build_decision(make_claim(), index(txs), included=["TX-T012"], material_conflict=True)
        self.assertEqual(decision.status.value, "CONFLICTING")
        self.assertIn("MATERIAL_EVIDENCE_CONFLICT", decision.reason_codes)

    def test_d11_outside_date_window_not_recalled(self):
        txs = parse("T013,2026-03-19,10:00:00,张某,6222,李某,6217,50000,投资款")
        self.assertEqual(match_claim_transactions(make_claim(), txs), [])

    def test_d12_manual_amount_tampering_is_rejected(self):
        txs = parse("T001,2026-03-15,,张某,6222,李某,6217,30000,")
        decision = build_decision(
            make_claim(), index(txs), included=["TX-T001"],
            decision_type=DecisionType.HUMAN_CONFIRMED, version=2,
            supersedes_decision_id="DEC-CLM-001-v1", reviewer="reviewer",
            reviewed_at=datetime.now(timezone.utc),
        )
        decision.covered_amount = Decimal("50000.00")
        errors = verify_decision(make_claim(), decision, index(txs))
        self.assertIn("AMOUNT_VERIFICATION_FAILED", errors)
        self.assertIn("HUMAN_CONFIRMATION_BLOCKED", errors)


if __name__ == "__main__":
    unittest.main()

