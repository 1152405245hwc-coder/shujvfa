from __future__ import annotations

from decimal import Decimal
from typing import Any

from legal_funds_agent.domain.models import Transaction
from legal_funds_agent.services.transaction_analysis import unique_transactions


def _document_text(documents: list[dict[str, str]] | None, prefix: str) -> tuple[str, str]:
    for document in documents or []:
        filename = str(document.get("filename") or "")
        if filename.startswith(prefix):
            return filename, str(document.get("text") or "")
    return "", ""


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def build_evidence_conflict_matrix(
    transactions: dict[str, Transaction],
    supplementary_documents: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build a small, source-labelled conflict matrix for the showcase case.

    This is deliberately a presentation aid. It does not infer a legal finding;
    it only joins deterministic transaction controls with explicit statements
    found in registered witness/defendant materials.
    """
    witness_name, witness_text = _document_text(supplementary_documents, "03")
    defendant_name, defendant_text = _document_text(supplementary_documents, "04")
    if not witness_text and not defendant_text:
        return []

    unique = unique_transactions(transactions.values())
    third_party_amount = sum(
        (tx.amount for tx in unique if tx.payee_account_id == "A005"),
        Decimal("0"),
    )
    securities_amount = sum(
        (
            tx.amount
            for tx in unique
            if tx.payee_account_id == "A004" and tx.payer_account_id in {"A002", "A003"}
        ),
        Decimal("0"),
    )

    matrix: list[dict[str, Any]] = []
    if third_party_amount and ("林某" in witness_text or "林某" in defendant_text):
        matrix.append({
            "id": "CONFLICT-01",
            "priority": "高",
            "title": "林某 A005 账户的性质",
            "materials": [
                {"source": defendant_name or "04 被告人供述与辩解", "finding": "何某称林某账户用于项目款归集，并称 125 万元按其指示转入。"},
                {"source": witness_name or "03 证人证言", "finding": "林某否认自己是项目财务人员，称仅按要求协助收款或转款。"},
                {"source": "02 银行流水账单.xlsx", "finding": f"A005 收取 {_money(third_party_amount)}，其中存在后续转出记录。"},
            ],
            "conclusion": "存在证据冲突，当前只能列为争议，不能仅凭收款记录认定账户实际控制或代收关系。",
            "next_action": "调取 A005 开户资料、实际控制人信息、通信指示和后续分流流水，核对代收与实际控制关系。",
        })

    if securities_amount and ("证券" in witness_text or "股票" in defendant_text or "基金" in defendant_text):
        matrix.append({
            "id": "CONFLICT-02",
            "priority": "高",
            "title": "证券账户入金与所谓项目投资是否同一事实",
            "materials": [
                {"source": "02 银行流水账单.xlsx", "finding": f"A002/A003 向 A004 证券账户转入 {_money(securities_amount)}。"},
                {"source": defendant_name or "04 被告人供述与辩解", "finding": "何某称部分资金用于股票、基金等证券投资。"},
                {"source": witness_name or "03 证人证言", "finding": "证人称公司不存在其所称的内部份额或对应项目安排。"},
            ],
            "conclusion": "真实发生证券投资不当然等于履行所宣称的项目投资，应分开核对资金用途与项目事实。",
            "next_action": "回查 A004 证券账户交割单、资金去向和项目文件，分别记录证券投资事实与项目承诺事实。",
        })

    if ("241" in defendant_text or "241万元" in defendant_text) and ("105" in witness_text and "62" in witness_text):
        matrix.append({
            "id": "CONFLICT-03",
            "priority": "中",
            "title": "何某所述 241 万元旧债的证据范围",
            "materials": [
                {"source": defendant_name or "04 被告人供述与辩解", "finding": "何某称以相关资金偿还旧债合计 241 万元。"},
                {"source": witness_name or "03 证人证言", "finding": "王某、刘某的证言分别支持 105 万元和 62 万元旧债，共 167 万元。"},
            ],
            "conclusion": "旧债事实目前仅有部分获得独立言词证据支持，余款证明力需要单独核查。",
            "next_action": "按债权人、形成时间、还款流水和原始凭证逐笔核对 241 万元构成，不把自述总额直接作为已核实数额。",
        })

    if ("非法占有" in defendant_text or "不认为" in defendant_text) and ("承认" in defendant_text or "确实" in defendant_text):
        matrix.append({
            "id": "CONFLICT-04",
            "priority": "中",
            "title": "客观事实供述与主观意图判断",
            "materials": [
                {"source": defendant_name or "04 被告人供述与辩解", "finding": "何某对部分收款、转款或资金用途作出客观事实说明，同时否认非法占有目的。"},
                {"source": "核验规则", "finding": "资金流向、账户关系和用途记录只能说明可核验事实，不替代对主观意图的独立审查。"},
            ],
            "conclusion": "客观事实承认不等于对诈骗故意或非法占有目的的自认，两类问题应分别留痕。",
            "next_action": "将客观资金事实与主观意图相关材料分栏回查，记录相互印证、矛盾和仍待查证的部分。",
        })

    return matrix
