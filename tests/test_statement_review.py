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


if __name__ == "__main__":
    unittest.main()

