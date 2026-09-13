import unittest

from legal_funds_agent.parsers.ocr_service import _extract_amount_candidates


class OCRExtractionTest(unittest.TestCase):
    def test_reject_bare_year_in_transfer_text(self):
        candidates = _extract_amount_candidates("2026年3月15日转账")
        self.assertNotIn("2026", candidates)

    def test_keep_amount_with_prefix(self):
        candidates = _extract_amount_candidates("金额：2026元")
        self.assertEqual(candidates, ["2026"])

    def test_keep_amount_with_currency_symbol(self):
        candidates = _extract_amount_candidates("¥1,000.00")
        self.assertEqual(candidates, ["1000.00"])


if __name__ == "__main__":
    unittest.main()
