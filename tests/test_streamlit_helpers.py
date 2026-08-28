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
