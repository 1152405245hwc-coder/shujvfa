from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, time
from decimal import Decimal

from legal_funds_agent.domain.models import Transaction
from legal_funds_agent.services.transaction_analysis import normalize_account_reference

REQUIRED_COLUMNS = {"transaction_id", "date", "time", "payer", "payer_account", "payee", "payee_account", "amount", "remark"}


def _fingerprint(row: dict[str, str]) -> str:
    payer = (row.get("payer_account_id") or "").strip() or normalize_account_reference(row.get("payer_account")) or (row.get("payer") or "").strip()
    payee = (row.get("payee_account_id") or "").strip() or normalize_account_reference(row.get("payee_account")) or (row.get("payee") or "").strip()
    keys = ["date", "time", "amount"]
    raw = "|".join((row.get(k) or "").strip() for k in keys) + f"|{payer}|{payee}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_transactions(csv_text: str, *, case_id: str, evidence_id: str) -> list[Transaction]:
    csv_text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise ValueError(f"CSV columns must include: {', '.join(sorted(REQUIRED_COLUMNS))}")
    result: list[Transaction] = []
    for row_number, row in enumerate(reader, start=2):
        amount = Decimal((row.get("amount") or "").strip()).quantize(Decimal("0.01"))
        payer = (row.get("payer") or "").strip() or None
        payer_account = (row.get("payer_account") or "").strip() or None
        payer_account_id = (row.get("payer_account_id") or "").strip() or None
        payee = (row.get("payee") or "").strip() or None
        payee_account = (row.get("payee_account") or "").strip() or None
        payee_account_id = (row.get("payee_account_id") or "").strip() or None
        source_account_id = (row.get("source_account_id") or "").strip() or None
        try:
            source_row = int((row.get("source_row") or "").strip())
        except ValueError:
            source_row = row_number
        result.append(Transaction(
            id=f"TX-{row['transaction_id'].strip()}", case_id=case_id,
            transaction_id=row["transaction_id"].strip(), date=date.fromisoformat(row["date"].strip()),
            time=time.fromisoformat(row["time"].strip()) if row.get("time") else None,
            payer_name=payer, payer_account=payer_account,
            payee_name=payee, payee_account=payee_account,
            payer_account_id=payer_account_id, payee_account_id=payee_account_id,
            amount=amount, remark=(row.get("remark") or "").strip() or None,
            source_evidence_id=evidence_id, source_account_id=source_account_id,
            source_row=source_row, dedup_fingerprint=_fingerprint(row),
        ))
    return result
