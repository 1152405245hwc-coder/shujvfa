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


class ContentKeyTest(unittest.TestCase):
    """``_content_key`` guards model billing, so it must actually run.

    It is exec'd against the app's real import block: a helper that references a name the
    module never imports would otherwise only fail at runtime.
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
        start = source.index("def _content_key")
        end = source.index("\n\n\n", start + 1)
        namespace = self._namespace()
        exec(source[start:end], namespace)
        return namespace["_content_key"]

    def test_key_is_stable_and_order_independent(self):
        helper = self._helper()
        first = helper({"a": 1, "b": [2, 3]})
        second = helper({"b": [2, 3], "a": 1})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_key_changes_with_content(self):
        helper = self._helper()
        self.assertNotEqual(helper({"a": 1}), helper({"a": 2}))

    def test_key_handles_decimals_and_dates(self):
        from datetime import date
        from decimal import Decimal

        helper = self._helper()
        self.assertEqual(len(helper({"amount": Decimal("1.00"), "date": date(2026, 3, 15)})), 64)
