import importlib.util
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py"


class EditorRecordsTest(unittest.TestCase):
    @staticmethod
    def _load_helper():
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def _editor_records")
        end = source.index("\n\n\ndef ", start + 1)
        namespace = {}
        exec(source[start:end], namespace)
        return namespace["_editor_records"]

    def test_list_input_is_preserved(self):
        helper = self._load_helper()
        rows = [{"交易ID": "TX-T001", "处置": "INCLUDED"}]
        self.assertEqual(helper(rows), rows)

    def test_dataframe_like_input_uses_records(self):
        helper = self._load_helper()

        class FrameLike:
            def to_dict(self, orient):
                self.orient = orient
                return [{"交易ID": "TX-T001"}]

        frame = FrameLike()
        self.assertEqual(helper(frame), [{"交易ID": "TX-T001"}])
        self.assertEqual(frame.orient, "records")


if __name__ == "__main__":
    unittest.main()


class JsonArgTest(unittest.TestCase):
    """``_json_arg`` feeds the cached model calls, so it must round-trip.

    A previous version passed a SHA-256 hash instead of the serialized payload; the cached
    function then did ``json.loads`` on hex and blew up, silently disabling the whole model
    enhancement path in the UI. The round-trip assertion below is what catches that.
    """

    @staticmethod
    def _namespace():
        source = APP_PATH.read_text(encoding="utf-8")
        imports = [
            line for line in source.splitlines()
            if line.startswith(("import ", "from ")) and "legal_funds_agent" not in line
        ]
        namespace = {}
        exec("\n".join(imports), namespace)
        return namespace

    def _helper(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def _json_arg")
        end = source.index("\n\n\n", start + 1)
        namespace = self._namespace()
        exec(source[start:end], namespace)
        return namespace["_json_arg"]

    def test_round_trips_back_into_data(self):
        import json

        helper = self._helper()
        payload = [{"item_id": "INV-1", "facts": {"amount": "1,250,000.00"}}]
        self.assertEqual(json.loads(helper(payload)), payload)

    def test_is_deterministic_and_order_independent(self):
        helper = self._helper()
        self.assertEqual(helper({"a": 1, "b": [2, 3]}), helper({"b": [2, 3], "a": 1}))

    def test_changes_with_content(self):
        helper = self._helper()
        self.assertNotEqual(helper({"a": 1}), helper({"a": 2}))

    def test_handles_decimals_and_dates(self):
        import json
        from datetime import date
        from decimal import Decimal

        helper = self._helper()
        parsed = json.loads(helper({"amount": Decimal("1.00"), "date": date(2026, 3, 15)}))
        self.assertEqual(parsed, {"amount": "1.00", "date": "2026-03-15"})


if __name__ == "__main__":
    unittest.main()


class NextAvailableCaseIdTest(unittest.TestCase):
    """``GOLD_CASE_001`` has no hyphen; the id derivation must not assume one.

    Restoring that case via ``?case_id=`` crashed the case page with
    "not enough values to unpack (expected 2, got 1)" before this was fixed.
    """

    def _helper(self, existing_ids):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def _next_available_case_id")
        end = source.index("\n\n\ndef ", start + 1)
        namespace = self._namespace(existing_ids)
        exec(source[start:end], namespace)
        return namespace["_next_available_case_id"]

    @staticmethod
    def _namespace(existing_ids):
        from contextlib import closing
        from pathlib import Path

        class _Repo:
            def __init__(self, connection):
                pass

            def list_cases(self):
                return [{"case_id": value} for value in existing_ids]

        class _Connection:
            def close(self):
                pass

        return {
            "closing": closing,
            "Path": Path,
            "connect": lambda _path: _Connection(),
            "Repository": _Repo,
        }

    def test_hyphenated_id_advances_the_numeric_suffix(self):
        helper = self._helper(["CASE-0001", "CASE-0002"])
        self.assertEqual(helper(Path(__file__), base="CASE-0001"), "CASE-0003")

    def test_id_without_hyphen_gets_a_suffix_instead_of_crashing(self):
        helper = self._helper(["GOLD_CASE_001"])
        self.assertEqual(helper(Path(__file__), base="GOLD_CASE_001"), "GOLD_CASE_001-0001")

    def test_non_numeric_suffix_does_not_collide(self):
        helper = self._helper(["CASE-ABC"])
        self.assertEqual(helper(Path(__file__), base="CASE-ABC"), "CASE-ABC-0001")

    def test_unused_base_is_returned_as_is(self):
        helper = self._helper(["OTHER-1"])
        self.assertEqual(helper(Path(__file__), base="CASE-0001"), "CASE-0001")
