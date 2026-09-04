from __future__ import annotations

import io
import re
from typing import Any
from datetime import datetime


class OCREngine:
    """Singleton wrapper around RapidOCR engine."""
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                cls._instance = RapidOCR()
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 rapidocr_onnxruntime，请先执行 pip install rapidocr_onnxruntime"
                ) from exc
        return cls._instance


def extract_text_from_image(image_bytes: bytes, min_score: float = 0.4) -> str:
    """Extract Chinese and alphanumeric text from raw image bytes (PNG, JPG, etc.)."""
    if not image_bytes:
        raise ValueError("传入的图像数据为空")

    ocr = OCREngine.get_instance()
    result, _ = ocr(image_bytes)
    if not result:
        return ""

    lines: list[str] = []
    for item in result:
        if len(item) >= 3:
            text = str(item[1]).strip()
            score = float(item[2])
            if text and score >= min_score:
                lines.append(text)
    return "\n".join(lines)


def extract_text_from_scanned_pdf(pdf_bytes: bytes, max_pages: int = 50, scale: float = 2.0) -> str:
    """Render pages of a scanned PDF using pypdfium2 and extract text via OCR."""
    if not pdf_bytes:
        raise ValueError("传入的 PDF 数据为空")

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("PDF 渲染需要安装 pypdfium2") from exc

    doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    page_texts: list[str] = []

    total_pages = min(len(doc), max_pages)
    for i in range(total_pages):
        page = doc[i]
        pil_image = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        page_bytes = buf.getvalue()
        text = extract_text_from_image(page_bytes)
        if text.strip():
            page_texts.append(text)

    return "\n\n".join(page_texts)


def parse_screenshot_transaction(text: str) -> dict[str, Any] | None:
    """Attempt to parse core financial transfer elements from OCR text of a transfer screenshot."""
    if not text:
        return None

    # Try matching amount (e.g. ¥50,000.00 or 50000.00元 or -50000.00)
    amount_match = re.search(r"(?:金额[:：\s]*|[¥￥\-]\s*|\b(?<!\d))(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:元|CNY)?", text)
    amount = amount_match.group(1).replace(",", "") if amount_match else None

    # Try matching payee / counterparty
    payee_match = re.search(r"(?:收款人|对方户名|转账给|转入账户|户名|收款方)[:：\s]*([\u4e00-\u9fff]{2,10}(?:某)?)", text)
    payee = payee_match.group(1).strip() if payee_match else None

    # Try matching date / time
    date_match = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)", text)
    trans_time = date_match.group(1) if date_match else None

    # Try matching transaction ID / order number
    id_match = re.search(r"(?:转账单号|交易单号|单号|流水号)[:：\s]*([A-Za-z0-9_-]{8,35})", text)
    tx_id = id_match.group(1) if id_match else f"TX-IMG-{int(datetime.now().timestamp())}"

    if amount and (payee or trans_time):
        return {
            "transaction_id": tx_id,
            "amount": amount,
            "payee": payee or "待确认收款人",
            "time": trans_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_text": text,
        }
    return None
