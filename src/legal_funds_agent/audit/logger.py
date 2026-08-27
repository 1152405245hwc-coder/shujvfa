from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    task_id: str
    case_id: str
    step: str
    started_at: str
    finished_at: str
    duration_ms: int
    tool: str
    status: str
    model: str | None = None
    prompt_version: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def completed_event(task_id: str, case_id: str, step: str, tool: str, started: datetime, *,
                    model: str | None = None, input_hash: str | None = None,
                    output_hash: str | None = None, prompt_version: str | None = None,
                    input_tokens: int | None = None, output_tokens: int | None = None,
                    latency_ms: int | None = None) -> AuditEvent:
    finished = datetime.now(timezone.utc)
    return AuditEvent(task_id, case_id, step, started.isoformat(), finished.isoformat(),
                      int((finished - started).total_seconds() * 1000), tool, "success", model=model,
                      prompt_version=prompt_version, input_hash=input_hash, output_hash=output_hash,
                      input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms)


def failed_event(task_id: str, case_id: str, step: str, tool: str, started: datetime, error: Exception, *,
                 model: str | None = None, input_hash: str | None = None) -> AuditEvent:
    finished = datetime.now(timezone.utc)
    return AuditEvent(
        task_id, case_id, step, started.isoformat(), finished.isoformat(),
        int((finished - started).total_seconds() * 1000), tool, "error", model=model,
        input_hash=input_hash, error=f"{type(error).__name__}: {error}",
    )
