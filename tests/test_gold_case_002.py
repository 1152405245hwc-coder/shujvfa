from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from legal_funds_agent.parsers.file_parsers import (
    extract_document_text,
    extract_transactions_csv_detailed,
)
from legal_funds_agent.services.transaction_analysis import identify_refund_transactions
from legal_funds_agent.workflow.vertical_slice import run_case_inputs

ROOT = Path(__file__).resolve().parents[1]
GOLD_PKG = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_002"
TRUTH_DIR = GOLD_PKG / "hidden"

# The indictment anchors each of the four claims with a distinct total amount.
CLAIM_ANCHORS = ("240万元", "180万元", "160万元", "120万元")


def _sentence_containing(text: str, keyword: str) -> str:
    matches = [segment + "。" for segment in text.split("。") if keyword in segment]
    if len(matches) != 1:
        raise AssertionError(f"indictment anchor {keyword!r} is not unique: {len(matches)} matches")
    return matches[0]


class GoldCase002ShimProvider:
    """Offline provider replaying the four truth claims of GOLD_CASE_002.

    Each ``source_text`` is located inside the real indictment text, so the
    extraction guard and unique-span checks run exactly as they would with a
    live model.
    """

    name = "gold_case_002_shim"
    prompt_version = "truth-v1"
    last_call_metrics: dict = {}

    def __init__(self, indictment_text: str):
        truth = json.loads((TRUTH_DIR / "claim_truth.json").read_text(encoding="utf-8"))
        self.rows = [
            {
                "victim_name": claim["victim_name"],
                "alleged_recipient_name": claim["recipient"],
                "claimed_amount": claim["amount"],
                "time_start": claim["time_start"],
                "time_end": claim["time_end"],
                "source_text": _sentence_containing(indictment_text, anchor),
            }
            for claim, anchor in zip(truth["claims"], CLAIM_ANCHORS, strict=True)
        ]

    def generate_structured(self, *, text: str, schema_name: str):
        if schema_name == "payment_claim_v0.1":
            return self.rows
        raise ValueError(f"unsupported schema: {schema_name}")


class GoldCase002Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        visible = GOLD_PKG / "visible"
        cls.indictment_text = extract_document_text(
            (visible / "documents" / "01_起诉书.docx").read_bytes(),
            filename="01_起诉书.docx",
        )
        # 多被害人案件：三份陈述合并为一个文本进入主链，与 UI 多文件上传后的
        # 拼接行为一致；各被害人段落由 "被害人X陈述" 标题切分。
        cls.statement_text = "\n\n".join(
            extract_document_text(
                (visible / "documents" / name).read_bytes(), filename=name,
            )
            for name in (
                "03_刘某陈述.docx",
                "04_周某陈述.docx",
                "05_郑某陈述.docx",
            )
        )
        csv_text, skip_stats = extract_transactions_csv_detailed(
            (visible / "bank" / "02_银行流水账单.xlsx").read_bytes(),
            filename="02_银行流水账单.xlsx",
        )
        cls.csv_skip_stats = skip_stats
        cls.csv_text = csv_text
        cls.result = run_case_inputs(
            indictment_text=cls.indictment_text,
            statement_text=cls.statement_text,
            csv_text=csv_text,
            case_id="GOLD_CASE_002",
            task_id="TASK-GOLD-002",
            provider=GoldCase002ShimProvider(cls.indictment_text),
            allow_multiple_claims=True,
            allow_missing_statement=True,
            transaction_evidence_id="EVI-BANK-XLSX",
        )
        cls.claims = {claim.id: claim for claim in cls.result.claims}

    def candidate_index(self, claim_id: str) -> dict[tuple, object]:
        index = {}
        for candidate in self.result.candidates_by_claim[claim_id]:
            tx = self.result.transactions[candidate.transaction_id]
            index[(tx.date, tx.amount, tx.payee_account_id)] = candidate
        return index

    def test_bank_statement_parses_without_loss(self):
        self.assertEqual(sum(self.csv_skip_stats.values()), 0)
        self.assertEqual(len(self.result.transactions), 208)

    def test_four_truth_claims_are_extracted(self):
        self.assertEqual(len(self.result.claims), 4)
        total = sum((c.claimed_amount for c in self.result.claims), Decimal("0"))
        self.assertEqual(total, Decimal("7000000.00"))
        victims = [self.result.claims[i].victim_name for i in range(4)]
        self.assertEqual(victims, ["刘某", "周某", "郑某", "刘某"])

    def test_liu_statement_extracts_total_without_fabricated_conflict(self):
        facts = self.result.statement_facts_by_victim
        fact = facts["刘某"]
        self.assertIsNotNone(fact)
        self.assertEqual(fact.recipient_name, "陈某")
        self.assertEqual(fact.amount, Decimal("2400000.00"))
        self.assertIsNone(fact.payment_date)
        # 周某、郑某的陈述没有"共/共计/累计"锚定的总额，诚实报告为不可提取，
        # 而不是拿分期金额伪造金额冲突。
        self.assertIsNone(facts["周某"])
        self.assertIsNone(facts["郑某"])
        self.assertEqual(
            self.result.statement_extraction_warnings,
            ["STATEMENT_FACT_UNAVAILABLE", "STATEMENT_FACT_UNAVAILABLE"],
        )
        # 第一主张（刘某 C1）被其陈述覆盖且无冲突。
        self.assertEqual(self.result.review_required_reasons, [])
        self.assertEqual(self.result.statement_conflicts, [])

    def test_statement_facts_are_attributed_per_victim_and_claim(self):
        c1, c2, c3, c4 = self.result.claims
        conflicts = self.result.statement_conflicts_by_claim
        # 任何 Claim 都不得因他人陈述或同一被害人其他主张而产生金额/日期/收款人冲突。
        self.assertEqual(conflicts[c1.id], [])
        self.assertEqual(conflicts[c2.id], [])
        self.assertEqual(conflicts[c3.id], [])
        self.assertEqual(conflicts[c4.id], [])

    def test_claim1_recalls_all_four_liu_payments(self):
        c1 = self.result.claims[0]
        index = self.candidate_index(c1.id)
        expected = {
            (date(2025, 3, 20), Decimal("800000.00"), "A101"),
            (date(2025, 4, 6), Decimal("800000.00"), "A101"),
            (date(2025, 4, 25), Decimal("800000.00"), "A102"),
            # E004 落在 C1 默认 +3 日召回窗口内，是跨 Claim 重复预警的对象。
            (date(2025, 5, 2), Decimal("600000.00"), "A101"),
        }
        self.assertTrue(expected.issubset(set(index)))

    def test_claim2_misses_third_party_payer_by_design(self):
        c2 = self.result.claims[1]
        index = self.candidate_index(c2.id)
        self.assertIn((date(2025, 4, 12), Decimal("600000.00"), "A102"), index)
        self.assertIn((date(2025, 4, 22), Decimal("600000.00"), "A101"), index)
        # E007 由孙某（B204）代付，严格付款方规则下预期漏召，留给人工补录。
        payer_accounts = {
            self.result.transactions[c.transaction_id].payer_account_id
            for c in self.result.candidates_by_claim[c2.id]
        }
        self.assertNotIn("B204", payer_accounts)

    def test_third_party_payer_surfaces_as_weak_signal(self):
        # E007（孙某代周某支付 60 万）不作为候选计入，但必须以弱信号形式
        # 呈现在 C2 的复核视野内，供人工确认代付关系。
        c2 = self.result.claims[1]
        signals = self.result.weak_signals_by_claim[c2.id]
        signal_txs = {
            (
                self.result.transactions[s.transaction_id].date,
                self.result.transactions[s.transaction_id].amount,
                self.result.transactions[s.transaction_id].payer_account_id,
            )
            for s in signals
        }
        self.assertIn((date(2025, 4, 16), Decimal("600000.00"), "B204"), signal_txs)
        for signal in signals:
            self.assertIn("WEAK_PAYER_SIGNAL", signal.risk_codes)
            self.assertFalse(signal.blocking_conflict)
        # 弱信号绝不进入候选集，不影响金额与决定。
        all_candidate_ids = {
            c.transaction_id
            for candidates in self.result.candidates_by_claim.values()
            for c in candidates
        }
        for signal in signals:
            self.assertNotIn(signal.transaction_id, all_candidate_ids)

    def test_cross_claim_duplication_is_flagged_at_candidate_level(self):
        c1, c4 = self.result.claims[0], self.result.claims[3]
        key = (date(2025, 5, 2), Decimal("600000.00"), "A101")
        for claim_id in (c1.id, c4.id):
            candidate = self.candidate_index(claim_id)[key]
            self.assertIn("CROSS_CLAIM_DUPLICATION", candidate.risk_codes)
            # 预警而非阻断：如何分配该笔流水由人工复核决定。
            self.assertFalse(candidate.blocking_conflict)

    def test_ma_third_party_account_is_flagged(self):
        c3 = self.result.claims[2]
        c4 = self.result.claims[3]
        third_party_hits = [
            (c3.id, (date(2025, 6, 12), Decimal("800000.00"), "A105")),
            (c4.id, (date(2025, 5, 12), Decimal("600000.00"), "A105")),
        ]
        for claim_id, key in third_party_hits:
            candidate = self.candidate_index(claim_id)[key]
            self.assertIn("THIRD_PARTY_RECIPIENT", candidate.risk_codes)
            self.assertTrue(candidate.blocking_conflict)

    def test_refund_identification_matches_truth(self):
        refunds = identify_refund_transactions(
            self.result.claims, self.result.transactions.values()
        )
        self.assertEqual(len(refunds), 5)
        self.assertEqual(
            sum((tx.amount for tx in refunds), Decimal("0")), Decimal("1150000.00")
        )
        # 噪声（工资、消费退款、缴费等流入）不得再被当作疑似转回。
        payers = {(tx.payer_name, tx.payee_name, tx.amount) for tx in refunds}
        self.assertEqual(
            payers,
            {
                ("陈某", "刘某", Decimal("300000.00")),
                ("陈某", "刘某", Decimal("250000.00")),
                ("陈某", "周某", Decimal("250000.00")),
                ("陈某", "郑某", Decimal("200000.00")),
                ("马某", "刘某", Decimal("150000.00")),
            },
        )


if __name__ == "__main__":
    unittest.main()
