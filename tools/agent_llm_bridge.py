"""外部模型语义提取结果接入确定性流水线的桥接入口。

背景
----
项目的语义提取层通过 ``LLMProvider`` 协议与确定性核心解耦
（见 ``src/legal_funds_agent/llm/base.py``）。本脚本提供一个不联网的
Provider 实现，把外部模型（对话侧 Agent / 其他大模型）产出的结构化结果
直接喂给现有流程：

- 不需要 ``DEEPSEEK_API_KEY``，也不会产生 API 费用；
- 金额加总、候选匹配、去重、风险阻断、签署留痕仍由确定性代码完成；
- 提取结果仍会经过 ``source_text`` 原文唯一性校验，模型不能凭空造事实。

支持三个语义契约（见 ``llm/schemas.py``）：

======================  ============================  ==========================
schema                  作用                          对应输入
======================  ============================  ==========================
payment_claim_v0.1      从起诉书提取付款主张           起诉书原文
statement_fact_v0.1     从被害人陈述提取付款事实       陈述原文
claim_audit_v0.1        漏提复核，找未被覆盖的主张     起诉书 + 已提取主张
======================  ============================  ==========================

提取文件格式
------------
三个段落都可选，按需提供；未提供的段落会走确定性 fallback 并留下告警。

.. code-block:: json

    {
      "provider": "agent-bridge-v0.1",
      "extractions": [
        {
          "indictment": "<起诉书原文，需与材料完全一致>",
          "claims": [ { ...payment_claim_v0.1... } ]
        }
      ],
      "statement_facts": [
        {
          "statement": "<陈述原文，需与材料完全一致>",
          "fact": { ...statement_fact_v0.1... }
        }
      ],
      "audit_results": [
        {
          "indictment": "<起诉书原文>",
          "missing_claims": [ { ...payment_claim_v0.1... } ]
        }
      ]
    }

用法
----
::

    # 单案例：输出系统判定、候选清单、提取质量信号与漏提复核队列
    python tools/agent_llm_bridge.py run \\
        --case-dir sample_data/gold_cases/G01 \\
        --extraction extraction.json --audit

    # 回归：用外部提取结果跑 Gold Case 评测（与 manifest 期望值逐项比对）
    python tools/agent_llm_bridge.py eval \\
        --gold-root sample_data/gold_cases \\
        --extraction extraction.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from legal_funds_agent.domain.models import TransactionReviewAction  # noqa: E402
from legal_funds_agent.evaluation.gold_cases import evaluate_gold_cases  # noqa: E402
from legal_funds_agent.llm.schemas import (  # noqa: E402
    SCHEMA_CLAIM_AUDIT,
    SCHEMA_PAYMENT_CLAIM,
    SCHEMA_STATEMENT_FACT,
)
from legal_funds_agent.workflow.vertical_slice import (  # noqa: E402
    confirm_claim_extraction,
    review_transactions,
    run_case_inputs,
)


class BridgeProvider:
    """把外部模型产出的结构化结果回放给确定性流水线的 Provider。

    只做"回放"，不做任何语义判断：给定材料原文，返回调用方预先准备好的结果。
    原文缺失时直接报错或返回空，避免静默使用错误材料。
    """

    supported_schemas = (SCHEMA_PAYMENT_CLAIM, SCHEMA_STATEMENT_FACT, SCHEMA_CLAIM_AUDIT)

    def __init__(self, *, claims: dict[str, list[dict[str, Any]]],
                 statement_facts: dict[str, dict[str, Any]] | None = None,
                 audit_results: dict[str, list[dict[str, Any]]] | None = None,
                 name: str = "agent-bridge-v0.1"):
        self.name = name
        self.prompt_version = SCHEMA_PAYMENT_CLAIM
        self._claims = claims
        self._statement_facts = statement_facts or {}
        self._audit_results = audit_results or {}
        self.last_call_metrics: dict[str, int | None] = {
            "input_tokens": None, "output_tokens": None, "latency_ms": 0,
        }

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        if schema_name == SCHEMA_PAYMENT_CLAIM:
            key = text.strip()
            if key not in self._claims:
                raise ValueError(
                    "no claim extraction supplied for this material; "
                    "the bridge provider only replays pre-computed results"
                )
            return self._claims[key]

        if schema_name == SCHEMA_STATEMENT_FACT:
            fact = self._statement_facts.get(text.strip())
            # No canned fact means "let the deterministic parser handle it".
            return [fact] if fact else []

        if schema_name == SCHEMA_CLAIM_AUDIT:
            try:
                indictment = json.loads(text)["indictment"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError("claim audit input is not a valid envelope") from exc
            return self._audit_results.get(indictment.strip(), [])

        raise ValueError(f"unsupported schema: {schema_name}")


def load_extractions(path: Path) -> BridgeProvider:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))

    claims: dict[str, list[dict[str, Any]]] = {}
    for entry in payload.get("extractions", []):
        indictment = str(entry["indictment"]).strip()
        if not indictment:
            raise ValueError("extraction entry has an empty indictment text")
        if indictment in claims:
            raise ValueError("duplicate indictment text in extraction file")
        claims[indictment] = list(entry.get("claims", []))

    statement_facts: dict[str, dict[str, Any]] = {}
    for entry in payload.get("statement_facts", []):
        statement = str(entry["statement"]).strip()
        if statement in statement_facts:
            raise ValueError("duplicate statement text in extraction file")
        statement_facts[statement] = dict(entry["fact"])

    audit_results: dict[str, list[dict[str, Any]]] = {}
    for entry in payload.get("audit_results", []):
        audit_results[str(entry["indictment"]).strip()] = list(entry.get("missing_claims", []))

    if not (claims or statement_facts or audit_results):
        raise ValueError("extraction file contains no extractions")

    return BridgeProvider(
        claims=claims,
        statement_facts=statement_facts,
        audit_results=audit_results,
        name=str(payload.get("provider") or "agent-bridge-v0.1"),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _find_statement(case_dir: Path) -> Path:
    for candidate in ("victim_statement.txt", "victim_statement_zhang.txt"):
        path = case_dir / candidate
        if path.exists():
            return path
    raise FileNotFoundError(f"no victim statement found in {case_dir}")


def run_single_case(case_dir: Path, provider: BridgeProvider, *, case_id: str, task_id: str,
                    review_path: Path | None = None,
                    enable_audit: bool = False) -> dict[str, Any]:
    result = run_case_inputs(
        indictment_text=_read(case_dir / "indictment.txt"),
        statement_text=_read(_find_statement(case_dir)),
        csv_text=_read(case_dir / "transactions.csv"),
        case_id=case_id,
        task_id=task_id,
        provider=provider,
        statement_provider=provider,
        enable_claim_audit=enable_audit,
        audit_provider=provider,
    )
    report: dict[str, Any] = {
        "provider": provider.name,
        "case_id": case_id,
        "claim": {
            "victim_name": result.claim.victim_name,
            "alleged_recipient_name": result.claim.alleged_recipient_name,
            "claimed_amount": str(result.claim.claimed_amount),
            "time_range": [str(result.claim.time_start), str(result.claim.time_end)],
            "extraction_status": result.claim.extraction_status,
        },
        "source_locators": [
            {
                "label": locator.label,
                "start_offset": locator.start_offset,
                "end_offset": locator.end_offset,
                "source_text": locator.source_text,
            }
            for locator in result.claim_locators
        ],
        "statement_fact": (
            {
                "recipient_name": result.statement_fact.recipient_name,
                "amount": str(result.statement_fact.amount),
                "payment_date": str(result.statement_fact.payment_date),
                "extraction_source": result.statement_fact.extraction_source,
            }
            if result.statement_fact
            else None
        ),
        "statement_conflicts": list(result.statement_conflicts),
        "statement_extraction_warnings": list(result.statement_extraction_warnings),
        "review_required_reasons": list(result.review_required_reasons),
        "extraction_issues": list(result.extraction_issues),
        "duplicate_groups": result.duplicate_groups,
        "candidates": [
            {
                "transaction_id": candidate.transaction_id,
                "amount": str(result.transactions[candidate.transaction_id].amount),
                "payee": result.transactions[candidate.transaction_id].payee_name,
                "amount_match": candidate.amount_match,
                "date_match": candidate.date_match,
                "matched_rules": list(candidate.matched_rules),
                "risk_codes": list(candidate.risk_codes),
                "blocking_conflict": candidate.blocking_conflict,
            }
            for candidate in result.candidates
        ],
        "system_status": result.system_decision.status.value,
        "system_covered_amount": str(result.system_decision.covered_amount),
        "audit_steps": [event.step for event in result.audit_events],
    }

    if result.claim_audit is not None:
        report["claim_audit"] = {
            "checked": result.claim_audit.checked,
            "missing_count": len(result.claim_audit.missing_claims),
            "missing_claims": result.claim_audit.missing_claims,
            "notes": result.claim_audit.notes,
        }

    if review_path is not None:
        actions = [TransactionReviewAction.model_validate(item)
                   for item in json.loads(review_path.read_text(encoding="utf-8-sig"))]
        result.claim = confirm_claim_extraction(result.claim)
        decision, _ = review_transactions(result, actions, reviewer="agent-bridge-reviewer")
        report["human_status"] = decision.status.value
        report["human_covered_amount"] = str(decision.covered_amount)
        report["human_uncovered_amount"] = str(decision.uncovered_amount)
        report["human_disputed_amount"] = str(decision.disputed_amount)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="把外部模型的语义提取结果接入确定性流水线。")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="对单个案例跑完整流水线")
    run_parser.add_argument("--case-dir", type=Path, required=True)
    run_parser.add_argument("--extraction", type=Path, required=True)
    run_parser.add_argument("--case-id", default="CASE-BRIDGE-0001")
    run_parser.add_argument("--task-id", default="TASK-BRIDGE-0001")
    run_parser.add_argument("--review", type=Path, help="逐笔处置动作 JSON，给出后追加人工确认结果")
    run_parser.add_argument("--audit", action="store_true", help="启用漏提复核")
    run_parser.add_argument("--output", type=Path)

    eval_parser = sub.add_parser("eval", help="用外部提取结果跑 Gold Case 评测")
    eval_parser.add_argument("--extraction", type=Path, required=True)
    eval_parser.add_argument("--gold-root", type=Path, default=REPO_ROOT / "sample_data" / "gold_cases")
    eval_parser.add_argument("--output", type=Path)
    eval_parser.add_argument("--continue-on-error", action="store_true")

    args = parser.parse_args()
    provider = load_extractions(args.extraction)

    if args.command == "run":
        report = run_single_case(
            args.case_dir, provider, case_id=args.case_id, task_id=args.task_id,
            review_path=args.review, enable_audit=args.audit,
        )
    else:
        report = evaluate_gold_cases(
            args.gold_root, provider=provider, stop_on_error=not args.continue_on_error
        )

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)

    if args.command == "eval" and report["summary"]["passed_cases"] != report["summary"]["declared_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
