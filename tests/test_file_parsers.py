import io
import unittest
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pypdf import PdfReader, PdfWriter

from legal_funds_agent.parsers.file_parsers import extract_document_text, extract_transactions_csv


class FileParserTest(unittest.TestCase):
    def test_docx_text_extraction(self):
        document = Document()
        document.add_paragraph("起诉书：何某收取被害人朱某人民币50000元。")
        data = io.BytesIO()
        document.save(data)
        self.assertIn("起诉书", extract_document_text(data.getvalue(), filename="indictment.docx"))

    def test_pdf_text_extraction_reports_scanned_pdf(self):
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        data = io.BytesIO()
        writer.write(data)
        with self.assertRaisesRegex(ValueError, "OCR"):
            extract_document_text(data.getvalue(), filename="scan.pdf")

    def test_xlsx_bank_sheet_to_canonical_csv(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "01_账户"
        sheet.append(["户名", "何某", "账户ID", "A002"])
        sheet.append([None])
        sheet.append(["交易时间", "银行流水号", "收/支", "对方户名/账户", "金额", "摘要/备注"])
        sheet.append(["2025-03-12 10:21:14", "S001", "收入", "朱某", 50000, "项目款"])
        data = io.BytesIO()
        workbook.save(data)
        csv_text = extract_transactions_csv(data.getvalue(), filename="bank.xlsx")
        self.assertIn("S001,2025-03-12,10:21:14,朱某,,何某,A002,50000.00,项目款", csv_text)

    def test_case_package_formats_extract(self):
        root = Path(__file__).resolve().parents[1] / "sample_data" / "case_packages" / "GOLD_CASE_001"
        indictment = extract_document_text((root / "visible/documents/01_起诉书.docx").read_bytes(), filename="01_起诉书.docx")
        csv_text = extract_transactions_csv((root / "visible/bank/02_银行流水账单.xlsx").read_bytes(), filename="02_银行流水账单.xlsx")
        self.assertIn("起 诉 书", indictment)
        self.assertGreaterEqual(csv_text.count("\n"), 10)


if __name__ == "__main__":
    unittest.main()
