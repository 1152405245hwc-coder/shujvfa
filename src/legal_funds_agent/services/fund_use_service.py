"""Deterministic fund-use summary for the GOLD_CASE_001 showcase.

Rows are derived from canonical transactions plus the registered supplementary
documents. No model output and no hard-coded totals are used: every amount is
either summed from the bank records or parsed from the recorded statement text.
The account layout (A002/A003 suspect accounts, A004 securities, A005
third-party collection) is specific to this fabricated demo case, so callers
should render this summary only for ``GOLD_CASE_001``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from legal_funds_agent.services.transaction_analysis import unique_transactions

SECURITIES_PAYEE_ACCOUNT = "A004"
SUSPECT_ACCOUNTS = {"A002", "A003"}
THIRD_PARTY_ACCOUNT = "A005"

_DEBT_TOTAL_RE = re.compile(r"合计\s*([0-9]{1,6})\s*万元")
# Witness statements summarise the confirmed part in several wording variants:
# "两笔共105万元", "两笔共计105万元", "合计62万元".
_DEBT_CONFIRMED_RE = re.compile(r"(?:两笔共|两笔共计|合计|共计)\s*([0-9]{1,6})\s*万元")


def _document_text(documents: list[Mapping[str, str]] | None, prefix: str) -> str:
    for document in documents or []:
        if str(document.get("filename") or "").startswith(prefix):
            return str(document.get("text") or "")
    return ""


def _parse_debt_amounts(
    documents: list[Mapping[str, str]] | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Parse the defendant's 241万 self-account and the witness-confirmed part.

    Returns ``(total, confirmed)``; either may be ``None`` when the statement
    text is missing, so the UI can say "未提供" instead of inventing a number.
    """
    defendant = _document_text(documents, "04")
    witness = _document_text(documents, "03")

    total: Decimal | None = None
    for line in defendant.splitlines():
        if "债" in line or "欠" in line:
            match = _DEBT_TOTAL_RE.search(line)
            if match:
                total = Decimal(match.group(1)) * 10000
                break

    confirmed = Decimal("0")
    found = False
    for line in witness.splitlines():
        if "借" in line or "欠" in line or "旧账" in line:
            match = _DEBT_CONFIRMED_RE.search(line)
            if match:
                confirmed += Decimal(match.group(1)) * 10000
                found = True

    return total, (confirmed if found else None)


def _sum_amount(transactions) -> Decimal:
    return sum((tx.amount for tx in transactions), Decimal("0"))


def build_fund_use_summary(
    transactions,
    *,
    supplementary_documents: list[Mapping[str, str]] | None = None,
    refund_transactions=None,
) -> list[dict[str, Any]]:
    """Return the deterministic fund-use rows shown on the 02 page."""
    unique = unique_transactions(transactions.values())
    securities = [
        tx
        for tx in unique
        if tx.payee_account_id == SECURITIES_PAYEE_ACCOUNT and tx.payer_account_id in SUSPECT_ACCOUNTS
    ]
    third_party = [tx for tx in unique if tx.payee_account_id == THIRD_PARTY_ACCOUNT]
    refunds = list(refund_transactions or [])
    debt_total, debt_confirmed = _parse_debt_amounts(supplementary_documents)

    rows: list[dict[str, Any]] = [
        {
            "key": "securities",
            "label": "证券账户转入（A004）",
            "amount": _sum_amount(securities),
            "status": "已确认资金事实",
            "evidence": (
                f"{len(securities)} 笔流水进入证券账户；何某供述用于股票、基金等证券投资，"
                "但不是其向朱某宣称的云岭新材内部份额项目。"
            ),
            "transactions": securities,
        },
        {
            "key": "third_party",
            "label": "第三方账户代收（林某 A005）",
            "amount": _sum_amount(third_party),
            "status": "争议账户 · 待人工核验",
            "evidence": (
                f"{len(third_party)} 笔流入；林某否认是项目财务人员，称仅按何某要求代收代转，"
                "账户性质仍属争议。"
            ),
            "transactions": third_party,
        },
        {
            "key": "debt",
            "label": "疑似偿还旧债（依据被告人供述）",
            "amount": debt_total,
            "status": "部分独立印证" if debt_confirmed else "仅被告人供述 · 待核查",
            "evidence": (
                f"何某自述以相关资金偿还旧债合计；其中王某、刘某 {debt_confirmed:,.2f} 元另有证言印证，"
                "其余部分主要依赖被告人供述，不能直接当作已核实数额。"
                if debt_confirmed
                else "卷宗未提供可解析的旧债金额，需按债权人逐笔回查。"
            ),
            "transactions": [],
        },
        {
            "key": "refund",
            "label": "向朱某转回",
            "amount": _sum_amount(refunds),
            "status": "转账事实确认 · 性质待查",
            "evidence": (
                f"{len(refunds)} 笔转回；流水只能证明转账事实，不能单独证明属于项目收益、分红或法定返还。"
            ),
            "transactions": refunds,
        },
        {
            "key": "other",
            "label": "其他支出 / 消费（未分类）",
            "amount": None,
            "status": "需逐笔查看",
            "evidence": "剩余流出未在本摘要中归入上述用途，请在下方的涉案流出支付流水中逐笔核验。",
            "transactions": [],
        },
    ]
    return rows
