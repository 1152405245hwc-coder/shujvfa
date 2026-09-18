import unittest
from pathlib import Path

from legal_funds_agent.workflow.vertical_slice import run_case_inputs


class StatementReviewTest(unittest.TestCase):
    def setUp(self):
        case_dir = Path(__file__).resolve().parents[1] / "sample_data" / "demo_case_001"
        self.indictment = (case_dir / "indictment.txt").read_text(encoding="utf-8")
        self.csv_text = (case_dir / "transactions.csv").read_text(encoding="utf-8")

    def test_matching_statement_has_no_conflict(self):
        result = run_case_inputs(
            indictment_text=self.indictment,
            statement_text="我在2026年3月15日按照李某要求转款人民币50000元。",
            csv_text=self.csv_text,
        )
        self.assertEqual(result.statement_conflicts, [])

    def test_amount_conflict_forces_conflicting_status(self):
        result = run_case_inputs(
            indictment_text=self.indictment,
            statement_text="我在2026年3月15日按照李某要求转款人民币30000元。",
            csv_text=self.csv_text,
        )
        self.assertEqual(result.statement_conflicts, ["STATEMENT_AMOUNT_CONFLICT"])
        self.assertEqual(result.system_decision.status.value, "CONFLICTING")

    def test_statement_amount_accepts_thousands_separators(self):
        result = run_case_inputs(
            indictment_text="何某于2025年3月12日收取被害人朱某人民币7368000元。",
            statement_text=(
                "从2025年3月12日开始，我按照何某的要求分很多次转钱。"
                "经逐笔核对，我总共转出7,368,000元。"
            ),
            csv_text=(
                "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
                "T001,2025-03-12,10:00:00,朱某,A001,何某,A002,7368000,项目款\n"
            ),
        )
        self.assertEqual(result.statement_fact.amount, 7368000)
        self.assertEqual(result.statement_conflicts, [])

    def test_single_claim_reports_date_conflict_instead_of_silent_not_covered(self):
        """一个被害人只有一条主张时，陈述与主张的日期差异必须报为冲突。

        历史缺陷：归属过滤（为避免同一被害人多条主张之间互相「冲突」）会把这类事实
        判为「陈述未覆盖该主张」，于是可检测的日期矛盾被降级为 PENDING_REVIEW。
        封存的留出集 H04 正是为此设计，期望 CONFLICTING + STATEMENT_DATE_CONFLICT。
        """
        result = run_case_inputs(
            indictment_text="经查，钱某于2026年5月10日收取被害人冯某支付的人民币40000元。",
            statement_text="我于2026年5月13日按照钱某的要求，通过银行转账支付人民币40000元。",
            csv_text=(
                "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
                "TX-H04-T001,2026-05-10,10:00:00,冯某,A001,钱某,A002,40000.00,转账\n"
            ),
        )
        self.assertEqual(result.statement_conflicts, ["STATEMENT_DATE_CONFLICT"])
        self.assertEqual(result.review_required_reasons, [])
        self.assertEqual(result.system_decision.status.value, "CONFLICTING")

    def test_single_claim_reports_recipient_conflict(self):
        result = run_case_inputs(
            indictment_text="经查，钱某于2026年5月10日收取被害人冯某支付的人民币60000元。",
            statement_text="我于2026年5月10日按照孙某的要求，通过银行转账支付人民币60000元。",
            csv_text=(
                "transaction_id,date,time,payer,payer_account,payee,payee_account,amount,remark\n"
                "TX-H05-T001,2026-05-10,10:00:00,冯某,A001,钱某,A002,60000.00,转账\n"
            ),
        )
        self.assertEqual(result.statement_conflicts, ["STATEMENT_RECIPIENT_CONFLICT"])
        self.assertEqual(result.system_decision.status.value, "CONFLICTING")


if __name__ == "__main__":
    unittest.main()
