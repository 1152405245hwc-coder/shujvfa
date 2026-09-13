from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from legal_funds_agent.domain.models import TransactionReviewAction
from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.factory import provider_from_environment
from legal_funds_agent.llm.provenance import build_provenance
from legal_funds_agent.workflow.vertical_slice import (
    confirm_claim_extraction,
    review_transactions,
    run_case_inputs,
)


class ExpectedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_status: str
    human_status: str
    covered_amount: Decimal
    uncovered_amount: Decimal
    disputed_amount: Decimal
    risk_codes: list[str]
    duplicate_group_count: int


class GoldCaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    expected_candidate_ids: list[str]
    review_actions: list[TransactionReviewAction]
    expected: ExpectedOutcome


class GoldManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    cases: list[GoldCaseSpec]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _claim_metrics(result) -> dict[str, int | None]:
    event = next(event for event in result.audit_events if event.step == "claim_extraction")
    return {
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "latency_ms": event.latency_ms,
    }


def _all_call_metrics(result) -> dict[str, int | None]:
    """Aggregate every model call in the case, not just claim extraction.

    ``_claim_metrics`` stays claim-only so the frozen V0.1 baselines remain comparable;
    this second view is the honest cost of running the full semantic layer.
    """
    inputs = outputs = latency = 0
    calls = 0
    for event in result.audit_events:
        # Mock and the offline bridge report zeros/None; only bill real model traffic.
        if not (event.input_tokens or event.output_tokens):
            continue
        calls += 1
        inputs += event.input_tokens or 0
        outputs += event.output_tokens or 0
        latency += event.latency_ms or 0
    return {
        "model_calls": calls,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "latency_ms": latency,
    }


def _evaluate_case(case_root: Path, spec: GoldCaseSpec, provider: LLMProvider, *,
                   statement_provider: LLMProvider | None = None,
                   enable_claim_audit: bool = False,
                   audit_provider: LLMProvider | None = None) -> dict[str, Any]:
    indictment_text = _read(case_root / "indictment.txt")
    result = run_case_inputs(
        indictment_text=indictment_text,
        statement_text=_read(case_root / "victim_statement.txt"),
        csv_text=_read(case_root / "transactions.csv"),
        case_id=f"GOLD-{spec.id}",
        task_id=f"EVAL-{spec.id}",
        provider=provider,
        statement_provider=statement_provider,
        enable_claim_audit=enable_claim_audit,
        audit_provider=audit_provider,
    )
    source_reference_valid = all(
        bool(locator.source_text) and locator.source_text in indictment_text
        for locator in result.claim_locators
    )
    result.claim = confirm_claim_extraction(result.claim)
    decision, _ = review_transactions(
        result,
        spec.review_actions,
        reviewer="gold-case-evaluator",
    )
    candidate_ids = sorted(candidate.transaction_id for candidate in result.candidates)
    risk_codes = sorted({
        *result.statement_conflicts,
        *(risk for candidate in result.candidates for risk in candidate.risk_codes),
    })
    actual = {
        "candidate_ids": candidate_ids,
        "system_status": result.system_decision.status.value,
        "human_status": decision.status.value,
        "covered_amount": decision.covered_amount,
        "uncovered_amount": decision.uncovered_amount,
        "disputed_amount": decision.disputed_amount,
        "risk_codes": risk_codes,
        "duplicate_group_count": len(result.duplicate_groups),
        "source_reference_valid": source_reference_valid,
    }
    expected = spec.expected
    checks = {
        "candidate_ids": candidate_ids == sorted(spec.expected_candidate_ids),
        "system_status": actual["system_status"] == expected.system_status,
        "human_status": actual["human_status"] == expected.human_status,
        "covered_amount": actual["covered_amount"] == expected.covered_amount,
        "uncovered_amount": actual["uncovered_amount"] == expected.uncovered_amount,
        "disputed_amount": actual["disputed_amount"] == expected.disputed_amount,
        "risk_codes": risk_codes == sorted(expected.risk_codes),
        "duplicate_group_count": actual["duplicate_group_count"] == expected.duplicate_group_count,
        "source_reference": source_reference_valid,
    }
    return {
        "id": spec.id,
        "title": spec.title,
        "passed": all(checks.values()),
        "format_valid": True,
        "checks": checks,
        "actual": {
            **actual,
            "covered_amount": str(actual["covered_amount"]),
            "uncovered_amount": str(actual["uncovered_amount"]),
            "disputed_amount": str(actual["disputed_amount"]),
        },
        "metrics": _claim_metrics(result),
        "metrics_all_calls": _all_call_metrics(result),
        "statement_extraction_source": (
            result.statement_fact.extraction_source if result.statement_fact else None
        ),
        "review_required_reasons": list(result.review_required_reasons),
        "statement_extraction_warnings": list(result.statement_extraction_warnings),
        "extraction_issues": list(result.extraction_issues),
        "claim_audit": None if result.claim_audit is None else {
            "checked": result.claim_audit.checked,
            "missing_count": len(result.claim_audit.missing_claims),
            "missing_claims": result.claim_audit.missing_claims,
            "notes": result.claim_audit.notes,
        },
        "error": None,
    }


def evaluate_gold_cases(gold_root: Path, *, provider: LLMProvider,
                        stop_on_error: bool = True,
                        statement_provider: LLMProvider | None = None,
                        enable_claim_audit: bool = False,
                        audit_provider: LLMProvider | None = None) -> dict[str, Any]:
    """Run the frozen regression set.

    ``statement_provider`` and ``enable_claim_audit`` are opt-in so that the historical
    baselines stay reproducible: without them the run exercises claim extraction only.
    """
    manifest = GoldManifest.model_validate_json(_read(gold_root / "manifest.json"))
    outcomes: list[dict[str, Any]] = []
    for spec in manifest.cases:
        try:
            outcomes.append(_evaluate_case(
                gold_root / spec.id, spec, provider,
                statement_provider=statement_provider,
                enable_claim_audit=enable_claim_audit,
                audit_provider=audit_provider,
            ))
        except Exception as exc:
            outcomes.append({
                "id": spec.id,
                "title": spec.title,
                "passed": False,
                "format_valid": False,
                "checks": {},
                "actual": None,
                "metrics": {},
                "metrics_all_calls": {},
                "statement_extraction_source": None,
                "review_required_reasons": [],
                "statement_extraction_warnings": [],
                "extraction_issues": [],
                "claim_audit": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            if stop_on_error:
                break
    evaluated = len(outcomes)
    passed = sum(outcome["passed"] for outcome in outcomes)
    all_checks = [check for outcome in outcomes for check in outcome["checks"].values()]
    latencies = [
        outcome["metrics"].get("latency_ms")
        for outcome in outcomes
        if outcome["metrics"].get("latency_ms") is not None
    ]
    return {
        "schema_version": manifest.schema_version,
        "provider": provider.name,
        "provenance": build_provenance([
            ("claim_extraction", provider),
            ("statement_extraction", statement_provider),
            ("claim_audit", audit_provider if enable_claim_audit else None),
        ]),
        "summary": {
            "declared_cases": len(manifest.cases),
            "evaluated_cases": evaluated,
            "passed_cases": passed,
            "case_pass_rate": passed / evaluated if evaluated else 0,
            "check_pass_rate": sum(all_checks) / len(all_checks) if all_checks else 0,
            "format_pass_rate": (
                sum(outcome["format_valid"] for outcome in outcomes) / evaluated if evaluated else 0
            ),
            "total_input_tokens": sum(
                outcome["metrics"].get("input_tokens") or 0 for outcome in outcomes
            ),
            "total_output_tokens": sum(
                outcome["metrics"].get("output_tokens") or 0 for outcome in outcomes
            ),
            "average_latency_ms": sum(latencies) / len(latencies) if latencies else None,
            "statement_model_cases": sum(
                1 for outcome in outcomes
                if (outcome.get("statement_extraction_source") or "").startswith(provider.name)
            ),
            "statement_degraded_cases": sum(
                1 for outcome in outcomes if outcome.get("statement_extraction_warnings")
            ),
            "extraction_issue_total": sum(
                len(outcome.get("extraction_issues") or []) for outcome in outcomes
            ),
            "claim_audit_missing_total": sum(
                (outcome.get("claim_audit") or {}).get("missing_count", 0) for outcome in outcomes
            ),
            "all_calls_model_calls": sum(
                (outcome.get("metrics_all_calls") or {}).get("model_calls", 0) for outcome in outcomes
            ),
            "all_calls_input_tokens": sum(
                (outcome.get("metrics_all_calls") or {}).get("input_tokens", 0) for outcome in outcomes
            ),
            "all_calls_output_tokens": sum(
                (outcome.get("metrics_all_calls") or {}).get("output_tokens", 0) for outcome in outcomes
            ),
            "all_calls_latency_ms": sum(
                (outcome.get("metrics_all_calls") or {}).get("latency_ms", 0) for outcome in outcomes
            ),
        },
        "cases": outcomes,
    }


def compare_to_baseline(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Compare a fresh run against a frozen baseline and make drift visible.

    A baseline is only useful if you can tell when it stopped reproducing. Leaving a
    stale token count in the docs without a drift check is how a "frozen" baseline
    quietly becomes fiction.
    """
    base_summary = baseline.get("summary", {})
    run_summary = report.get("summary", {})
    base_cases = {case["id"]: case for case in baseline.get("cases", [])}
    run_cases = {case["id"]: case for case in report.get("cases", [])}

    shared = sorted(set(base_cases) & set(run_cases))
    conclusion_mismatches = [
        case_id for case_id in shared
        if base_cases[case_id].get("passed") != run_cases[case_id].get("passed")
    ]

    def drift(key: str) -> dict[str, Any] | None:
        base_value = base_summary.get(key)
        run_value = run_summary.get(key)
        if base_value is None or run_value is None:
            return None
        delta = run_value - base_value
        return {
            "baseline": base_value,
            "current": run_value,
            "delta": delta,
            "delta_pct": (delta / base_value * 100) if base_value else None,
        }

    # Both calibers are reported. ``total_*`` is claim-extraction only and stays
    # comparable with the v0.1 baselines; ``all_calls_*`` is the real cost of the full
    # semantic layer. A caliber absent on either side is skipped rather than guessed.
    drift_keys = [
        "total_input_tokens",
        "total_output_tokens",
        "all_calls_model_calls",
        "all_calls_input_tokens",
        "all_calls_output_tokens",
    ]
    token_drift = {key: value for key in drift_keys if (value := drift(key)) is not None}
    base_fp = (baseline.get("provenance") or {}).get("prompt_fingerprints")
    run_fp = (report.get("provenance") or {}).get("prompt_fingerprints")
    prompt_unchanged = None if base_fp is None else base_fp == run_fp

    input_delta = (token_drift.get("total_input_tokens") or {}).get("delta")
    output_delta = (token_drift.get("total_output_tokens") or {}).get("delta")

    if conclusion_mismatches:
        verdict = f"结论出现分歧：{', '.join(conclusion_mismatches)}。必须人工复核后方可继续。"
    elif input_delta == 0 and output_delta == 0:
        verdict = "完全复现：结论与 token 计数均与基线一致。"
    elif input_delta and not output_delta:
        verdict = (
            f"结论一致，但输入 token 漂移 {input_delta:+d} 而输出 token 完全一致。"
            "在提示词未变的前提下，这通常意味着服务端模型或分词器已变更——"
            "基线数字不再可复现，基线结论也未必由当前模型产出。"
        )
    elif not input_delta and output_delta:
        verdict = (
            f"结论一致，输入 token 完全一致，仅输出 token 漂移 {output_delta:+d}。"
            "输入未变说明提示词与分词器未变；输出长度波动属于服务端生成的非确定性"
            "（temperature=0 不保证逐字节可复现），不构成模型变更的证据。"
        )
    else:
        verdict = (
            f"结论一致，但输入 token 漂移 {input_delta:+d}、输出 token 漂移 {output_delta:+d}，"
            "两个方向同时变化，需人工判断是分词器变更还是生成波动。"
        )

    return {
        "baseline_provider": baseline.get("provider"),
        "current_provider": report.get("provider"),
        "baseline_captured_at": (baseline.get("provenance") or {}).get("captured_at"),
        "current_captured_at": (report.get("provenance") or {}).get("captured_at"),
        "prompt_fingerprints_unchanged": prompt_unchanged,
        "cases_missing_from_run": sorted(set(base_cases) - set(run_cases)),
        "cases_added_in_run": sorted(set(run_cases) - set(base_cases)),
        "conclusion_mismatches": conclusion_mismatches,
        "token_drift": token_drift,
        "verdict": verdict,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the V0.1 fabricated Gold Case evaluation.")
    parser.add_argument("--provider", choices=["mock", "openai", "deepseek"], default="mock")
    parser.add_argument(
        "--gold-root",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "sample_data" / "gold_cases",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--with-statement-model", action="store_true",
        help="让同一个 provider 同时负责被害人陈述解析（statement_fact_v0.1）",
    )
    parser.add_argument(
        "--with-claim-audit", action="store_true",
        help="启用漏提复核（claim_audit_v0.1），疑似漏项只作为待人工确认事项",
    )
    parser.add_argument(
        "--baseline", type=Path,
        help="与一份冻结基线逐项比对，输出结论差异与 token 漂移",
    )
    args = parser.parse_args()
    provider = provider_from_environment(args.provider)
    report = evaluate_gold_cases(
        args.gold_root,
        provider=provider,
        stop_on_error=not args.continue_on_error,
        statement_provider=provider if args.with_statement_model else None,
        enable_claim_audit=args.with_claim_audit,
        audit_provider=provider if args.with_claim_audit else None,
    )
    if args.baseline:
        baseline = json.loads(_read(args.baseline))
        report["baseline_comparison"] = compare_to_baseline(report, baseline)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.baseline:
        import sys

        comparison = report["baseline_comparison"]
        print(f"[基线比对] {comparison['verdict']}", file=sys.stderr)
    if report["summary"]["passed_cases"] != report["summary"]["declared_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
