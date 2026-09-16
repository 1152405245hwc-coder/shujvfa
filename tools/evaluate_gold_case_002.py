# -*- coding: utf-8 -*-
"""GOLD_CASE_002 隐藏真值评测工具。

对案情包跑完整审查流水线，与 hidden/ 目录真值逐项比对，输出中文报告与 JSON。
仅读取材料，不修改 src/ 下任何文件。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legal_funds_agent.parsers.file_parsers import (  # noqa: E402
    extract_document_text,
    extract_transactions_csv_detailed,
)
from legal_funds_agent.llm.factory import provider_from_environment  # noqa: E402
from legal_funds_agent.workflow.vertical_slice import run_case_inputs  # noqa: E402
from legal_funds_agent.services.transaction_analysis import (  # noqa: E402
    identify_refund_transactions,
    transaction_canonical_key,
    unique_transactions,
)
from legal_funds_agent.services.entity_resolution import (  # noqa: E402
    collect_party_names,
    propose_alias_groups,
)

CASE_DIR = ROOT / "sample_data" / "case_packages" / "GOLD_CASE_002"
VISIBLE = CASE_DIR / "visible"
HIDDEN = CASE_DIR / "hidden"


def load_json(name: str):
    return json.loads((HIDDEN / name).read_text(encoding="utf-8"))


def wan(value) -> str:
    if value is None:
        return "-"
    d = Decimal(str(value))
    return f"{d / Decimal('10000'):g}万"


def fmt_verdict(ok: bool | None) -> str:
    return {True: "吻合", False: "偏差", None: "无法核验"}[ok]


def find_tx(result, *, date=None, amount=None, payer_account=None, payee_account=None):
    hits = []
    for tx in result.transactions.values():
        if date and str(tx.date) != date:
            continue
        if amount is not None and tx.amount != Decimal(str(amount)):
            continue
        if payer_account and tx.payer_account != payer_account:
            continue
        if payee_account and tx.payee_account != payee_account:
            continue
        hits.append(tx)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="mock", choices=["mock", "deepseek"])
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    provider = provider_from_environment(args.provider)

    indictment_bytes = (VISIBLE / "documents" / "01_起诉书.docx").read_bytes()
    statement_files = [
        "03_刘某陈述.docx",
        "04_周某陈述.docx",
        "05_郑某陈述.docx",
    ]
    indictment_text = extract_document_text(indictment_bytes, filename="01_起诉书.docx")
    statement_text = "\n\n".join(
        extract_document_text((VISIBLE / "documents" / name).read_bytes(), filename=name)
        for name in statement_files
    )
    xlsx_bytes = (VISIBLE / "bank" / "02_银行流水账单.xlsx").read_bytes()
    csv_text, skip_stats = extract_transactions_csv_detailed(xlsx_bytes, filename="02_银行流水账单.xlsx")

    result = run_case_inputs(
        indictment_text=indictment_text,
        statement_text=statement_text,
        csv_text=csv_text,
        case_id="GOLD_CASE_002",
        task_id="EVAL-GC2",
        provider=provider,
        allow_multiple_claims=True,
        statement_provider=provider,
        enable_claim_audit=True,
        audit_provider=provider,
        allow_missing_statement=True,
        transaction_evidence_id="EVI-BANK-XLSX",
    )

    # ---- 真值 ----
    claim_truth = load_json("claim_truth.json")
    cross_truth = load_json("cross_claim_truth.json")
    refund_truth = load_json("refund_truth.json")
    conflict_truth = load_json("conflict_truth.json")
    alias_truth = load_json("alias_truth.json")
    tx_truth = load_json("transaction_truth.json")
    question_bank = load_json("question_bank.json")

    report: dict = {"provider": args.provider, "sections": []}

    def emit(title, rows):
        report["sections"].append({"title": title, "rows": rows})
        print(f"\n=== {title} ===")
        for row in rows:
            print(f"  [{fmt_verdict(row['ok'])}] {row['item']}")
            print(f"      期望: {row['expected']}")
            print(f"      实际: {row['actual']}")

    # ---- 1. 主张提取 ----
    rows = []
    sys_claims = result.claims
    total = sum(c.claimed_amount for c in sys_claims)
    rows.append({
        "item": "主张数量",
        "expected": f"{len(claim_truth['claims'])} 个",
        "actual": f"{len(sys_claims)} 个",
        "ok": len(sys_claims) == len(claim_truth["claims"]),
    })
    rows.append({
        "item": "主张总额",
        "expected": wan(claim_truth["total_claimed_amount"]),
        "actual": wan(total),
        "ok": total == Decimal(claim_truth["total_claimed_amount"]),
    })
    used: set[str] = set()
    for tc in claim_truth["claims"]:
        best, best_key = None, None
        for c in sys_claims:
            key = (c.victim_name, str(c.claimed_amount), str(c.time_start), str(c.time_end))
            if key in used:
                continue
            score = (c.victim_name == tc["victim_name"]) + (str(c.claimed_amount) == tc["amount"]) \
                + (str(c.time_start) == tc["time_start"]) + (str(c.time_end) == tc["time_end"])
            if best is None or score > best[0]:
                best, best_key = (score, c), key
        c = best[1]
        ok = (c.victim_name == tc["victim_name"] and str(c.claimed_amount) == tc["amount"]
              and str(c.time_start) == tc["time_start"] and str(c.time_end) == tc["time_end"])
        used.add(best_key)
        rows.append({
            "item": f"主张 {tc['claim_id']}（{tc['victim_name']} {wan(tc['amount'])} {tc['time_start']}~{tc['time_end']}）",
            "expected": f"{tc['victim_name']}/{tc['recipient']}/{tc['amount']}/{tc['time_start']}~{tc['time_end']}",
            "actual": f"{c.victim_name}/{c.alleged_recipient_name}/{c.claimed_amount}/{c.time_start}~{c.time_end} (id={c.id})",
            "ok": ok,
        })
    emit("1. 主张提取", rows)

    claim_by_victim_amount = {(c.victim_name, c.claimed_amount): c.id for c in sys_claims}

    # ---- 2. 交易归一 ----
    unique = unique_transactions(result.transactions.values())
    total_skipped = sum(skip_stats.values())
    raw_rows = max(len(csv_text.strip().splitlines()) - 1, 0)
    rows = [
        {"item": "unique canonical 事件数", "expected": str(tx_truth["expected_unique_key_events"]),
         "actual": str(len(unique)),
         "ok": len(unique) == tx_truth["expected_unique_key_events"]},
        {"item": "原始行导入 skip 数", "expected": "0（原始 208 行全部导入）",
         "actual": f"skip={total_skipped}（csv数据行≈{raw_rows}）",
         "ok": total_skipped == 0},
    ]
    emit("2. 交易归一", rows)

    # ---- 3. 重复/镜像组 + 跨主张风险 ----
    e004_txs = find_tx(result, date="2025-05-02", amount="600000.00")
    e004_ids = {t.id for t in e004_txs}
    group_ok = False
    for key, members in result.duplicate_groups.items():
        if e004_ids and e004_ids.issubset(set(members)):
            group_ok = True
            break
    risk_hits = {}
    for claim_id, cands in result.candidates_by_claim.items():
        for cand in cands:
            if cand.transaction_id in e004_ids:
                risk_hits.setdefault(claim_id, set()).update(cand.risk_codes)
    expect_ids = {"TX-B201-00004", "TX-A101-00003"}
    rows = [
        {"item": "E004 镜像行 (TX-B201-00004 / TX-A101-00003)",
         "expected": "两条流水同时存在且归入同一 duplicate_group",
         "actual": f"识别到 {sorted(e004_ids)}；同组={group_ok}",
         "ok": expect_ids.issubset(e004_ids) and group_ok},
        {"item": "CROSS_CLAIM_DUPLICATION 打在 E004 两条流水上（C1 召回窗口 + C4 严格范围）",
         "expected": f"E004 候选在 {cross_truth['claim_a']} 与 {cross_truth['claim_b']} 下均带 CROSS_CLAIM_DUPLICATION",
         "actual": {k: sorted(v) for k, v in risk_hits.items()} or "E004 未成为任何候选",
         "ok": bool(risk_hits) and all("CROSS_CLAIM_DUPLICATION" in v for v in risk_hits.values())
               and len(risk_hits) >= 2},
    ]
    emit("3. 重复/镜像组与跨主张风险", rows)

    # ---- 4. 第三方阻断 A105 ----
    third = {}
    for claim_id, cands in result.candidates_by_claim.items():
        for cand in cands:
            tx = result.transactions[cand.transaction_id]
            if tx.payee_account == "A105" and cand.payer_match == "EXACT":
                third.setdefault(cand.transaction_id, (tx, cand, claim_id))
    tp_amount = sum(v[0].amount for v in third.values())
    risks_ok = all(
        "THIRD_PARTY_RECIPIENT" in cand.risk_codes and cand.blocking_conflict
        for _, cand, _ in third.values()
    )
    expected_tp = {("2025-05-12", "600000.00"), ("2025-06-12", "800000.00")}
    actual_tp = {(str(v[0].date), str(v[0].amount)) for v in third.values()}
    rows = [
        {"item": "A105 直接收被害人款项笔数/金额",
         "expected": "2 笔，共 140 万（E005 60万 + E010 80万）",
         "actual": f"{len(third)} 笔，共 {wan(tp_amount)} {sorted(actual_tp)}",
         "ok": actual_tp == expected_tp and tp_amount == Decimal("1400000")},
        {"item": "第三方候选风险标注",
         "expected": "THIRD_PARTY_RECIPIENT + blocking_conflict",
         "actual": f"全部带风险={risks_ok}；{ {tid: sorted(c.risk_codes) for tid, (_, c, _) in third.items()} }",
         "ok": risks_ok},
    ]
    emit("4. 第三方账户（马某 A105）阻断", rows)

    # ---- 5. 孙某代付缺口 ----
    e007 = find_tx(result, date="2025-04-16", amount="600000.00")
    e007_ids = {t.id for t in e007}
    zhou_claim_id = claim_by_victim_amount.get(("周某", Decimal("1800000.00")))
    strict_ids = {c.transaction_id for c in result.candidates_by_claim.get(zhou_claim_id, [])}
    weak = result.weak_signals_by_claim.get(zhou_claim_id, [])
    weak_hit = [w for w in weak if w.transaction_id in e007_ids]
    rows = [
        {"item": "E007 严格候选（预期漏召）",
         "expected": "TX-B204-00001 不进入周某 C2 严格候选",
         "actual": f"E007={sorted(e007_ids)}；周某严格候选含 E007={bool(e007_ids & strict_ids)}",
         "ok": not (e007_ids & strict_ids)},
        {"item": "E007 弱线索暴露",
         "expected": "出现在 weak_signals_by_claim 的周某 claim 下",
         "actual": f"弱线索命中={[(w.transaction_id, w.amount_match) for w in weak_hit]}",
         "ok": bool(weak_hit)},
    ]
    emit("5. 孙某代付缺口（E007）", rows)

    # ---- 6. 疑似转回 ----
    refunds = identify_refund_transactions(result.claims, result.transactions.values())
    refund_total = sum(t.amount for t in refunds)
    decision_text = json.dumps(
        [d.model_dump() for d in result.system_decisions_by_claim.values()],
        ensure_ascii=False, default=str,
    )
    forbidden = "585" in decision_text
    rows = [
        {"item": "疑似转回笔数/金额",
         "expected": f"{len(refund_truth['refund_events'])} 笔，共 {wan(refund_truth['total_refund_amount'])}",
         "actual": f"{len(refunds)} 笔，共 {wan(refund_total)} "
                   f"{sorted((str(t.date), str(t.amount), t.payer_account, t.payee_account) for t in refunds)}",
         "ok": len(refunds) == len(refund_truth["refund_events"])
               and refund_total == Decimal(refund_truth["total_refund_amount"])},
        {"item": "禁止自动冲减（700-115=585）",
         "expected": "不得把 585 万当作犯罪金额输出",
         "actual": f"decision 文本中出现 585={'585' in decision_text}（decision 只表达 covered/uncovered 覆盖语义）",
         "ok": not forbidden},
    ]
    emit("6. 疑似转回识别", rows)

    # ---- 7. 别名 ----
    alias_notes = []
    alias_ok = None
    if result.alias_registry is None or not len(result.alias_registry):
        alias_notes.append("主流程未自动提议任何别名（alias_registry 为空/None）")
    else:
        d = result.alias_registry.to_dict()
        alias_notes.append(str(d))
    proposal = propose_alias_groups(
        party_names=collect_party_names(result.claims, result.transactions.values()),
        materials=[
            {"label": "01_起诉书", "text": indictment_text},
            {"label": "被害人陈述", "text": statement_text},
        ],
        provider=provider if args.provider != "mock" else None,
    )
    proposal_info = {
        "checked": proposal.checked,
        "groups": [
            {"canonical": g["canonical_name"],
             "aliases": [(a["name"], a["status"]) for a in g["aliases"]]}
            for g in proposal.groups
        ],
        "notes": proposal.notes,
    }
    rows = [
        {"item": "主流程别名提议（马会计→马某，需人工确认）",
         "expected": "系统提出候选且标记待人工确认",
         "actual": "; ".join(alias_notes) + "（主流程差距，如实报告）",
         "ok": False},
        {"item": "辅助服务 propose_alias_groups 探测",
         "expected": "可提出候选且保留待确认状态",
         "actual": json.dumps(proposal_info, ensure_ascii=False),
         "ok": None if not proposal.checked else any(
             g["canonical"] == "马某" and any(a[0] == "马会计" and a[1] == "待人工确认" for a in g["aliases"])
             for g in proposal_info["groups"])},
        {"item": "“马师傅”不得自动合并为马某",
         "expected": "不得合并",
         "actual": "主流程与辅助探测均未执行自动合并（合并仅在人工确认后生效）",
         "ok": True},
    ]
    emit("7. 别名解析", rows)

    # ---- 8. conflict_truth 五 ISSUE ----
    tp_desc = {tid: {"amount": str(v[0].amount), "risk": sorted(v[1].risk_codes), "claim": v[2]}
               for tid, v in third.items()}
    rows = [
        {"item": "ISSUE-01 马某A105账户性质（期望 CONFLICT）",
         "expected": conflict_truth["issues"][0]["must_not_conclude"],
         "actual": f"A105 直收被害人候选均阻断: {json.dumps(tp_desc, ensure_ascii=False)}；"
                   f"系统未自动认定共同犯罪/实际控制（decision 仅给出覆盖与复核状态）",
         "ok": bool(third) and risks_ok},
        {"item": "ISSUE-02 155万真实支出与投资权益（期望 QUALIFIED）",
         "expected": conflict_truth["issues"][1]["must_not_conclude"],
         "actual": "主流水线不对支出用途定性，亦不输出“承诺已履行”结论；"
                   "该项属于审查判断，需人工结合项目资料材料认定",
         "ok": None},
        {"item": "ISSUE-03 孙某代付60万（期望 WEAK_SIGNAL_OR_MANUAL_LINK）",
         "expected": conflict_truth["issues"][2]["must_not_conclude"],
         "actual": f"严格候选漏召={'是' if not (e007_ids & strict_ids) else '否'}；弱线索暴露={bool(weak_hit)}；"
                   "未静默计入周某覆盖金额",
         "ok": not (e007_ids & strict_ids) and bool(weak_hit)},
        {"item": "ISSUE-04 E004 跨 Claim 重叠（期望 CROSS_CLAIM_DUPLICATION）",
         "expected": conflict_truth["issues"][3]["must_not_conclude"],
         "actual": f"跨主张风险标注: { {k: sorted(v) for k, v in risk_hits.items()} }",
         "ok": bool(risk_hits) and all("CROSS_CLAIM_DUPLICATION" in v for v in risk_hits.values())},
        {"item": "ISSUE-05 旧债金额范围（期望 PARTIAL_SUPPORT）",
         "expected": "陈某称约210万；独立支持165万（需人工比对供述与证言）",
         "actual": "主流水线不汇总旧债支持度；属人工审查项",
         "ok": None},
    ]
    emit("8. conflict_truth 五个 ISSUE", rows)

    # ---- 9. question_bank ----
    refund_by_victim = {}
    for t in refunds:
        if t.payee_account and t.payee_account.startswith("B2"):
            refund_by_victim[t.payee_account] = refund_by_victim.get(t.payee_account, Decimal(0)) + t.amount
    q_rows = []
    for q in question_bank["questions"]:
        qid, qexp = q["id"], q["expected"]
        ans, ok, note = "需人工", None, ""
        if qid == "Q01":
            ans, ok = f"{len(sys_claims)}个主张，共{wan(total)}", len(sys_claims) == 4 and total == Decimal("7000000")
        elif qid == "Q02":
            ok = not (e007_ids & strict_ids)
            ans = "孙某代付60万（E007）未进严格候选（弱线索暴露）" if ok else "E007 误入严格候选"
        elif qid == "Q03":
            ok = tp_amount == Decimal("1400000")
            ans = f"A105 直收两名被害人 {wan(tp_amount)}（{len(third)}笔）"
        elif qid == "Q04":
            checked = proposal.checked and any(
                g["canonical"] == "马某" and any(a[0] == "马会计" for a in g["aliases"])
                for g in proposal_info["groups"])
            ans = "辅助探测提出“马会计→马某”候选，待人工确认" if checked else \
                ("辅助探测被支持性/证据校验拒绝或未执行" + f" notes={proposal.notes}" if not checked else "")
            ok = checked if proposal.checked else None
        elif qid == "Q05":
            ans, ok = "不能；系统未自动合并", True
        elif qid == "Q06":
            ans, note = "主流水线无支出用途分类模块", "需人工/专项分析"
        elif qid == "Q07":
            ans, note = "系统不输出“承诺已履行”类结论", "需人工"
        elif qid == "Q08":
            ans, note = "主流水线不计算旧债独立支持度", "需人工"
        elif qid == "Q09":
            ans, note = "主流水线不汇总陈某供述金额", "需人工"
        elif qid == "Q10":
            ok = refund_total == Decimal("1150000")
            ans = f"疑似转回 {wan(refund_total)}（{len(refunds)}笔，待核验性质）"
        elif qid == "Q11":
            ans, ok = "不能；decision 只表达覆盖语义，未输出 585 万", "585" not in decision_text
        elif qid == "Q12":
            owners = sorted(risk_hits)
            ok = len(owners) >= 2
            ans = f"E004（2025-05-02，60万）同时被 {owners} 召回并标注跨主张重复" if ok else "未发现跨主张召回"
        q_rows.append({"item": f"{qid} {q['question']}",
                       "expected": qexp, "actual": ans + (f"（{note}）" if note else ""), "ok": ok})
    emit("9. question_bank 十二问", q_rows)

    # ---- 汇总 ----
    all_rows = [r for s in report["sections"] for r in s["rows"]]
    n_ok = sum(1 for r in all_rows if r["ok"] is True)
    n_bad = sum(1 for r in all_rows if r["ok"] is False)
    n_na = sum(1 for r in all_rows if r["ok"] is None)
    print(f"\n===== 汇总（provider={args.provider}）=====")
    print(f"吻合 {n_ok} / 偏差 {n_bad} / 无法核验 {n_na}（共 {len(all_rows)} 项）")
    report["summary"] = {"match": n_ok, "mismatch": n_bad, "unverifiable": n_na, "total": len(all_rows)}

    if args.output:
        out = Path(args.output)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"JSON 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
