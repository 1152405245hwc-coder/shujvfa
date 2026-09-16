import tempfile
import unittest
from pathlib import Path

from legal_funds_agent.domain.models import Claim
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository


def _make_claim(index: int, status: str = "model_extracted") -> Claim:
    return Claim(
        id=f"CLM-T{index:03d}",
        case_id="CASE-SNAP-001",
        victim_name="受害人甲",
        victim_account=f"622200010001000{index}",
        alleged_recipient_name="嫌疑人乙",
        alleged_recipient_account=f"621700020002000{index}",
        claimed_amount=f"{index * 10000}",
        source_locator_ids=[f"LOC-{index}"],
        extraction_status=status,
    )


class CaseSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "case.db"
        self.connection = connect(self.db_path)
        self.repository = Repository(self.connection)

    def tearDown(self):
        self.connection.close()
        self._tmp.cleanup()

    def test_snapshot_roundtrip_preserves_claims_and_order(self):
        claims = [_make_claim(i) for i in range(1, 5)]
        self.repository.save_case_snapshot("CASE-SNAP-001", claims)
        loaded = self.repository.load_case_snapshot("CASE-SNAP-001")
        self.assertEqual(len(loaded), 4)
        self.assertEqual([c.id for c in loaded], [c.id for c in claims])
        for original, restored in zip(claims, loaded):
            self.assertEqual(restored.id, original.id)
            self.assertEqual(restored.case_id, original.case_id)
            self.assertEqual(restored.victim_name, original.victim_name)
            self.assertEqual(restored.alleged_recipient_name, original.alleged_recipient_name)
            self.assertEqual(str(restored.claimed_amount), str(original.claimed_amount))
            self.assertEqual(restored.extraction_status, original.extraction_status)
            self.assertEqual(restored.source_locator_ids, original.source_locator_ids)

    def test_snapshot_payload_masks_account_numbers(self):
        claims = [_make_claim(1)]
        self.repository.save_case_snapshot("CASE-SNAP-001", claims)
        row = self.connection.execute(
            "SELECT payload_json FROM case_snapshots WHERE case_id = ?", ("CASE-SNAP-001",)
        ).fetchone()
        raw = row["payload_json"]
        self.assertNotIn("6222000100010001", raw)
        self.assertNotIn("6217000200020001", raw)
        self.assertIn("****", raw)
        loaded = self.repository.load_case_snapshot("CASE-SNAP-001")
        self.assertIn("****", loaded[0].victim_account)

    def test_resaving_same_case_replaces_snapshot(self):
        self.repository.save_case_snapshot("CASE-SNAP-001", [_make_claim(1), _make_claim(2)])
        self.repository.save_case_snapshot("CASE-SNAP-001", [_make_claim(1)])
        loaded = self.repository.load_case_snapshot("CASE-SNAP-001")
        self.assertEqual(len(loaded), 1)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM case_snapshots WHERE case_id = ?", ("CASE-SNAP-001",)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_load_missing_case_returns_none(self):
        self.assertIsNone(self.repository.load_case_snapshot("CASE-DOES-NOT-EXIST"))

    def test_list_cases_includes_unconfirmed_snapshot_only_case(self):
        self.repository.save_case_snapshot("CASE-SNAP-001", [_make_claim(1)])
        cases = self.repository.list_cases()
        self.assertEqual([item["case_id"] for item in cases], ["CASE-SNAP-001"])
        self.assertEqual(cases[0]["claim_count"], 1)
        self.assertEqual(cases[0]["tx_count"], 0)

    def test_delete_case_removes_all_persisted_rows_but_not_other_cases(self):
        self.repository.save_case_snapshot("CASE-SNAP-001", [_make_claim(1)])
        self.repository.save_case_snapshot("CASE-SNAP-002", [_make_claim(2)])
        self.repository.save_case_display_name("CASE-SNAP-001", "待删除案件")
        self.repository.save_case_display_name("CASE-SNAP-002", "保留案件")

        self.repository.delete_case("CASE-SNAP-001")

        self.assertIsNone(self.repository.load_case_snapshot("CASE-SNAP-001"))
        self.assertNotIn("CASE-SNAP-001", self.repository.load_case_display_names())
        self.assertIsNotNone(self.repository.load_case_snapshot("CASE-SNAP-002"))
        self.assertEqual(self.repository.load_case_display_names()["CASE-SNAP-002"], "保留案件")

    def test_snapshot_coexists_with_immutable_confirmed_claims(self):
        snapshot_claims = [_make_claim(1), _make_claim(2)]
        self.repository.save_case_snapshot("CASE-SNAP-001", snapshot_claims)
        confirmed = _make_claim(1, status="human_confirmed")
        self.repository.save_claim(confirmed)
        # Both stores hold their own copy of the case state.
        snapshot = self.repository.load_case_snapshot("CASE-SNAP-001")
        self.assertEqual(len(snapshot), 2)
        stored_claims = self.repository.load_case_claims("CASE-SNAP-001")
        self.assertEqual(len(stored_claims), 1)
        self.assertEqual(stored_claims[0].extraction_status, "human_confirmed")
        # Saving again after confirmation still replaces the snapshot cleanly.
        self.repository.save_case_snapshot("CASE-SNAP-001", [confirmed])
        self.assertEqual(len(self.repository.load_case_snapshot("CASE-SNAP-001")), 1)

    def test_existing_database_without_snapshot_table_gets_migration(self):
        # Simulate a pre-snapshot database file: drop the table, close, then
        # reconnect through connect() and confirm the table is recreated.
        self.connection.execute("DROP TABLE case_snapshots")
        self.connection.commit()
        self.connection.close()
        self.connection = connect(self.db_path)
        self.repository = Repository(self.connection)
        self.repository.save_case_snapshot("CASE-SNAP-001", [_make_claim(1)])
        self.assertEqual(len(self.repository.load_case_snapshot("CASE-SNAP-001")), 1)


if __name__ == "__main__":
    unittest.main()
