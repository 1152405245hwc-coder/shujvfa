import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEAL_PATH = ROOT / "docs" / "evaluation" / "holdout_v0.1_seal.json"


class HoldoutSealTest(unittest.TestCase):
    def test_sealed_holdout_files_are_unchanged(self):
        seal = json.loads(SEAL_PATH.read_text(encoding="utf-8"))
        mismatches = []
        for relative_path, expected_hash in seal["files"].items():
            actual_hash = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                mismatches.append(relative_path)
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
