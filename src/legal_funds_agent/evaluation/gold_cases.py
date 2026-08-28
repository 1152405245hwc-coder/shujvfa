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


def _evaluate_case(case_root: Path, spec: GoldCaseSpec, provider: LLMProvider) -> dict[str, Any]:
    indictment_text = _read(case_root / "indictment.txt")
    result = run_case_inputs(
        indictment_text=indictment_text,
        statement_text=_read(case_root / "victim_statement.txt"),
        csv_text=_read(case_root / "transactions.csv"),
        case_id=f"GOLD-{spec.id}",
        task_id=f"EVAL-{spec.id}",
        provider=provider,
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
        "error": None,
    }


def evaluate_gold_cases(gold_root: Path, *, provider: LLMProvider,
                        stop_on_error: bool = True) -> dict[str, Any]:
    manifest = GoldManifest.model_validate_json(_read(gold_root / "manifest.json"))
    outcomes: list[dict[str, Any]] = []
    for spec in manifest.cases:
        try:
            outcomes.append(_evaluate_case(gold_root / spec.id, spec, provider))
        except Exception as exc:
            outcomes.append({
                "id": spec.id,
                "title": spec.title,
                "passed": False,
                "format_valid": False,
                "checks": {},
                "actual": None,
                "metrics": {},
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
        },
        "cases": outcomes,
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
    args = parser.parse_args()
    report = evaluate_gold_cases(
        args.gold_root,
        provider=provider_from_environment(args.provider),
        stop_on_error=not args.continue_on_error,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if report["summary"]["passed_cases"] != report["summary"]["declared_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
