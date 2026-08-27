import unittest

from legal_funds_agent.services.claim_extractor import extract_claims


class FabricatingProvider:
    name = "fabricating-test"

    def generate_structured(self, *, text, schema_name):
        return [{
            "victim_name": "张某", "alleged_recipient_name": "李某",
            "claimed_amount": "50000.00", "time_start": "2026-03-15", "time_end": "2026-03-15",
            "source_text": "原文中不存在的付款事实", "start_offset": 0, "end_offset": 10,
        }]


class ClaimExtractorTest(unittest.TestCase):
    def test_fabricated_source_quote_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not present"):
            extract_claims("这里只记载了其他内容。", case_id="CASE-1", evidence_id="EVI-1", provider=FabricatingProvider())


if __name__ == "__main__":
    unittest.main()

