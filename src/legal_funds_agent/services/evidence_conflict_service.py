"""证据冲突矩阵。

原实现把演示案（`GOLD_CASE_001`）的冲突内容直接写死在函数里——`"林某" in witness_text`、
写死 `A002/A003/A004/A005`、标题「林某 A005 账户的性质」。换个案子就输出空。

现在拆成两半：

- ``showcase_conflict_matrix()``：演示案专用固定内容，只在 GOLD_CASE_001 演示路径使用。
  显式命名，不再冒充通用能力。
- ``build_evidence_conflict_matrix()``：通用实现。**确定性代码先算出资金事实**
  （哪些账户收了钱但不是起诉书指称的收款对象、是否继续转出），
  再由模型在已登记材料中找出与这些事实相关的陈述并标注立场。

边界：模型只做「材料 ↔ 事实」的配对与引文，不评价证明力、不判断是否构成犯罪。
每条引文必须逐字命中材料，命中不了的整条丢弃。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from legal_funds_agent.domain.models import Transaction
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_EVIDENCE_CONFLICT,
    build_evidence_conflict_input,
    supports_schema,
)
from legal_funds_agent.services.transaction_analysis import (
    RELATED_ACCOUNT_MIN_AMOUNT,
    normalize_account_reference,
    normalize_party_name,
    unique_transactions,
)

# Same recall window as the candidate matcher: a victim's transfer counts as
# case-sourced only near a claim's stated period.
_CASE_SOURCE_WINDOW_DAYS = 3


def _in_any_claim_window(tx_date: Any, claims: list[Any]) -> bool:
    for claim in claims:
        start = getattr(claim, "time_start", None)
        end = getattr(claim, "time_end", None)
        if start is None or end is None:
            return True
        if start - timedelta(days=_CASE_SOURCE_WINDOW_DAYS) <= tx_date <= end + timedelta(days=_CASE_SOURCE_WINDOW_DAYS):
            return True
    return False

STANCE_LABEL = {"supports": "印证", "contradicts": "矛盾", "qualifies": "限定"}

# Review priority is a deterministic ordering signal (a flow-through account is
# structurally more consequential than a terminal one). The model may escalate it but
# must never be able to bury a deterministic signal by downgrading it.
PRIORITY_RANK = {"低": 0, "中": 1, "高": 2}
PRIORITY_BY_RANK = {rank: label for label, rank in PRIORITY_RANK.items()}


def _stronger_priority(deterministic: str, proposed: str | None) -> str:
    if not proposed:
        return deterministic
    return PRIORITY_BY_RANK[max(
        PRIORITY_RANK.get(deterministic, 1), PRIORITY_RANK.get(proposed, 1)
    )]


def _document_text(documents: list[dict[str, str]] | None, prefix: str) -> tuple[str, str]:
    for document in documents or []:
        filename = str(document.get("filename") or "")
        if filename.startswith(prefix):
            return filename, str(document.get("text") or "")
    return "", ""


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def third_party_account_facts(
    transactions: dict[str, Transaction], claims: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Accounts that receive case-sourced funds but that no claim names as its recipient.

    An inflow is case-sourced when it is a substantial victim transfer inside a
    claim's time window, or a substantial onward transfer out of an account that
    already holds case-sourced funds (the claim recipient, or an account flagged
    by the same rule). A victim's ordinary life spending and the victim's own
    inbound salary therefore never turn merchants or the victims themselves into
    "third-party accounts". Without claims there is no case context to filter
    with, and the historical behaviour is kept: every payee is a candidate.

    A flow-through account (one that also pays out) is the interesting case: it is the
    pattern behind 代收代转, so it is flagged for the human reviewer.
    """
    unique = unique_transactions(transactions.values())
    claims_list = list(claims or [])

    recipient_account_ids = {
        value for claim in claims_list
        for value in (getattr(claim, "alleged_recipient_account_id", None),)
        if value
    }
    recipient_names = {
        normalize_party_name(getattr(claim, "alleged_recipient_name", None))
        for claim in claims_list
        if getattr(claim, "alleged_recipient_name", None)
    }
    victim_names = {
        normalize_party_name(getattr(claim, "victim_name", None))
        for claim in claims_list
        if getattr(claim, "victim_name", None)
    }
    victim_account_refs = {
        normalize_account_reference(getattr(claim, "victim_account", None))
        for claim in claims_list
        if getattr(claim, "victim_account", None)
    }

    if claims_list:
        # Taint propagation: seed with the claim recipients' accounts, then follow
        # substantial transfers hop by hop. An inflow qualifies when its payer is
        # a victim paying inside a claim window, or an account already holding
        # case-sourced funds.
        tainted: set[str] = set(recipient_account_ids)
        qualifying_tx_ids: set[str] = set()
        changed = True
        while changed:
            changed = False
            for tx in unique:
                if tx.id in qualifying_tx_ids or tx.amount < RELATED_ACCOUNT_MIN_AMOUNT:
                    continue
                payer_key = tx.payer_account_id or normalize_account_reference(tx.payer_account)
                payer_is_victim = normalize_party_name(tx.payer_name) in victim_names
                if payer_is_victim:
                    if not _in_any_claim_window(tx.date, claims_list):
                        continue
                elif payer_key not in tainted:
                    continue
                qualifying_tx_ids.add(tx.id)
                payee_key = tx.payee_account_id or normalize_account_reference(tx.payee_account)
                if payee_key and payee_key not in tainted:
                    tainted.add(payee_key)
                    changed = True
        qualifying = [tx for tx in unique if tx.id in qualifying_tx_ids]
    else:
        qualifying = unique

    received: dict[str, dict[str, Any]] = {}
    for tx in qualifying:
        account_id = tx.payee_account_id or tx.payee_account
        if not account_id:
            continue
        if tx.payee_account_id and tx.payee_account_id in recipient_account_ids:
            continue
        payee_name = normalize_party_name(tx.payee_name)
        if payee_name in recipient_names or payee_name in victim_names:
            continue
        if normalize_account_reference(tx.payee_account) in victim_account_refs:
            continue
        bucket = received.setdefault(str(account_id), {
            "account_id": str(account_id),
            "payee_name": tx.payee_name or "-",
            "received_amount": Decimal("0"),
            "received_count": 0,
            "transaction_ids": [],
        })
        bucket["received_amount"] += tx.amount
        bucket["received_count"] += 1
        bucket["transaction_ids"].append(tx.transaction_id)

    outflow: dict[str, dict[str, Any]] = {}
    for tx in unique:
        account_id = tx.payer_account_id or tx.payer_account
        if not account_id or str(account_id) not in received:
            continue
        bucket = outflow.setdefault(str(account_id), {"amount": Decimal("0"), "count": 0})
        bucket["amount"] += tx.amount
        bucket["count"] += 1

    facts: list[dict[str, Any]] = []
    for account_id, bucket in sorted(received.items()):
        out = outflow.get(account_id, {"amount": Decimal("0"), "count": 0})
        facts.append({
            **bucket,
            "fact_id": f"FACT-TP-{account_id}",
            "outflow_amount": out["amount"],
            "outflow_count": out["count"],
            "flow_through": out["count"] > 0,
        })
    return facts


def _fact_entry(fact: dict[str, Any]) -> dict[str, Any]:
    """The deterministic half of a conflict entry: the funds fact on its own."""
    finding = (
        f"{fact['account_id']}（{fact['payee_name']}）收取 ¥{_money(fact['received_amount'])}"
        f"（{fact['received_count']} 笔）"
    )
    if fact["flow_through"]:
        finding += (
            f"，其中后续转出 ¥{_money(fact['outflow_amount'])}"
            f"（{fact['outflow_count']} 笔），呈现代收代转特征"
        )
    return {
        "id": f"CONFLICT-TP-{fact['account_id']}",
        "fact_id": fact["fact_id"],
        "priority": "高" if fact["flow_through"] else "中",
        "title": f"第三方账户 {fact['account_id']}（{fact['payee_name']}）的性质",
        "materials": [{"source": "银行流水", "finding": finding}],
        "conclusion": (
            "该账户不是起诉书指称的收款对象。收款记录本身不能认定账户实际控制或代收关系。"
        ),
        "next_action": (
            f"调取 {fact['account_id']} 开户资料、实际控制人信息与后续分流流水，"
            "核对是否存在代收、借用卡或二次分流事实。"
        ),
        "transaction_ids": list(fact["transaction_ids"]),
    }


def enrich_conflict_entries(
    entries: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    materials: list[dict[str, str]],
    provider: LLMProvider | None,
    *,
    raise_on_provider_error: bool = False,
) -> list[dict[str, Any]]:
    """The model half: attach material statements to the deterministic facts.

    Exposed separately so callers that render repeatedly (the Streamlit workbench) can
    cache on content instead of re-billing the model on every rerender.
    """
    if not entries or not materials or provider is None:
        return entries
    if not supports_schema(provider, SCHEMA_EVIDENCE_CONFLICT):
        return entries

    by_fact_id = {entry["fact_id"]: entry for entry in entries}
    payload_facts = [
        {
            "fact_id": fact["fact_id"],
            "account_id": fact["account_id"],
            "payee_name": fact["payee_name"],
            "received_amount": str(fact["received_amount"]),
            "received_count": fact["received_count"],
            "outflow_amount": str(fact["outflow_amount"]),
            "outflow_count": fact["outflow_count"],
        }
        for fact in facts
    ]
    try:
        rows = provider.generate_structured(
            text=build_evidence_conflict_input(payload_facts, materials),
            schema_name=SCHEMA_EVIDENCE_CONFLICT,
        )
    except Exception:  # noqa: BLE001 - enrichment is optional, never fatal
        if raise_on_provider_error:
            raise
        return entries

    corpus = "\n".join(material["text"] for material in materials)
    for row in rows:
        entry = by_fact_id.get(row["fact_id"])
        if entry is None:
            continue
        verified = [
            {
                "source": position["source"],
                "finding": f"{STANCE_LABEL[position['stance']]}：{position['source_text']}",
            }
            for position in row["positions"]
            if position["source_text"] in corpus
        ]
        if not verified:
            continue
        entry["materials"] = [*entry["materials"], *verified]
        if row["title"]:
            entry["title"] = row["title"]
        if row["conclusion"]:
            entry["conclusion"] = row["conclusion"]
        if row["next_action"]:
            entry["next_action"] = row["next_action"]
        entry["priority"] = _stronger_priority(entry["priority"], row["priority"])

    return entries


def deterministic_conflict_entries(
    transactions: dict[str, Transaction], claims: list[Any] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The deterministic half only, with no model call.

    Exposed so callers can render the facts immediately and cache the model enrichment
    separately, instead of re-billing on every render.
    """
    facts = third_party_account_facts(transactions, claims)
    return [_fact_entry(fact) for fact in facts], facts


def build_evidence_conflict_matrix(
    transactions: dict[str, Transaction],
    supplementary_documents: list[dict[str, str]] | None = None,
    *,
    claims: list[Any] | None = None,
    provider: LLMProvider | None = None,
) -> list[dict[str, Any]]:
    """Generic conflict matrix: deterministic funds facts, optionally enriched by a model.

    Without a provider this still returns the deterministic third-party account facts, so
    a case other than the showcase one is no longer silently empty. With a provider, the
    model adds the material statements that bear on each fact, each with a verbatim quote.
    """
    facts = third_party_account_facts(transactions, claims)
    if not facts:
        return []
    entries = [_fact_entry(fact) for fact in facts]

    materials = [
        {"label": str(document.get("filename") or f"材料{index}"), "text": str(document.get("text") or "")}
        for index, document in enumerate(supplementary_documents or [], start=1)
        if str(document.get("text") or "").strip()
    ]
    return enrich_conflict_entries(entries, facts, materials, provider)


def showcase_conflict_matrix(
    transactions: dict[str, Transaction],
    supplementary_documents: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Fixed conflict content for the GOLD_CASE_001 showcase.

    This is presentation material for one fabricated demo case, not a general capability.
    It is kept verbatim so the showcase keeps rendering the four conflicts the review
    packet documents; every other case goes through ``build_evidence_conflict_matrix``.
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
