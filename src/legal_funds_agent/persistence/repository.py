from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from legal_funds_agent.audit.logger import AuditEvent
from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction


def _mask_account(value: str | None) -> str | None:
    if not value:
        return value
    return "*" * max(len(value) - 4, 0) + value[-4:]


class Repository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _insert_immutable(self, table: str, object_id: str, columns: tuple[str, ...], values: tuple) -> None:
        existing = self.connection.execute(
            f"SELECT payload_json FROM {table} WHERE id = ?", (object_id,)
        ).fetchone()
        payload = values[-1]
        if existing:
            if existing["payload_json"] == payload:
                return
            raise ValueError(f"immutable {table[:-1]} already exists: {object_id}")
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})", values
        )

    def save_claim(self, claim: Claim) -> None:
        payload = claim.model_dump(mode="json")
        payload["victim_account"] = _mask_account(claim.victim_account)
        payload["alleged_recipient_account"] = _mask_account(claim.alleged_recipient_account)
        self._insert_immutable(
            "claims", claim.id, ("id", "case_id", "payload_json"),
            (claim.id, claim.case_id, json.dumps(payload, ensure_ascii=False)),
        )
        self.connection.commit()

    def save_transactions(self, transactions: list[Transaction]) -> None:
        try:
            for tx in transactions:
                payload = tx.model_dump(mode="json")
                payload["payer_account"] = _mask_account(tx.payer_account)
                payload["payee_account"] = _mask_account(tx.payee_account)
                # Bank serial numbers are only unique within a source account.
                # Older databases may already contain the unscoped key, so reuse
                # it for the same case and namespace collisions from another case.
                storage_id = tx.id
                existing = self.connection.execute(
                    "SELECT case_id FROM transactions WHERE id = ?", (storage_id,)
                ).fetchone()
                if existing and existing["case_id"] != tx.case_id:
                    storage_id = f"{tx.case_id}::{tx.id}"
                self._insert_immutable(
                    "transactions", storage_id, ("id", "case_id", "fingerprint", "payload_json"),
                    (storage_id, tx.case_id, tx.dedup_fingerprint, json.dumps(payload, ensure_ascii=False)),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def save_decision(self, decision: ReviewDecision) -> None:
        payload = decision.model_dump_json()
        existing = self.connection.execute("SELECT payload_json FROM decisions WHERE id = ?", (decision.id,)).fetchone()
        if existing:
            if existing["payload_json"] == payload:
                return
            raise ValueError(f"immutable decision already exists: {decision.id}")
        self.connection.execute(
            "INSERT INTO decisions(id, case_id, claim_id, version, decision_type, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (decision.id, decision.case_id, decision.claim_id, decision.version, decision.decision_type.value, payload),
        )
        self.connection.commit()

    def list_decisions(self, claim_id: str) -> list[ReviewDecision]:
        rows = self.connection.execute(
            "SELECT payload_json FROM decisions WHERE claim_id = ? ORDER BY version", (claim_id,)
        ).fetchall()
        return [ReviewDecision.model_validate_json(row["payload_json"]) for row in rows]

    def save_audit_events(self, events: list[AuditEvent]) -> None:
        self.connection.executemany(
            "INSERT INTO audit_events(task_id, case_id, step, payload_json) VALUES (?, ?, ?, ?)",
            [(event.task_id, event.case_id, event.step, json.dumps(event.to_dict(), ensure_ascii=False)) for event in events],
        )
        self.connection.commit()

    def list_cases(self) -> list[dict[str, Any]]:
        """List summary of all cases saved in the repository."""
        rows = self.connection.execute(
            """
            SELECT c.case_id,
                   COUNT(DISTINCT c.id) as claim_count,
                   (SELECT COUNT(*) FROM transactions t WHERE t.case_id = c.case_id) as tx_count,
                   (SELECT COUNT(*) FROM decisions d WHERE d.case_id = c.case_id) as decision_count
            FROM claims c
            GROUP BY c.case_id
            UNION
            SELECT t.case_id,
                   0 as claim_count,
                   COUNT(*) as tx_count,
                   (SELECT COUNT(*) FROM decisions d WHERE d.case_id = t.case_id) as decision_count
            FROM transactions t
            WHERE t.case_id NOT IN (SELECT DISTINCT case_id FROM claims)
            GROUP BY t.case_id
            """
        ).fetchall()
        return [
            {
                "case_id": row["case_id"],
                "claim_count": row["claim_count"],
                "tx_count": row["tx_count"],
                "decision_count": row["decision_count"],
            }
            for row in rows
        ]

    def load_case_claims(self, case_id: str) -> list[Claim]:
        rows = self.connection.execute(
            "SELECT payload_json FROM claims WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        return [Claim.model_validate_json(row["payload_json"]) for row in rows]

    def load_case_transactions(self, case_id: str) -> dict[str, Transaction]:
        rows = self.connection.execute(
            "SELECT payload_json FROM transactions WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        transactions = [Transaction.model_validate_json(row["payload_json"]) for row in rows]
        return {tx.id: tx for tx in transactions}

    def load_latest_decisions_by_claim(self, case_id: str) -> dict[str, ReviewDecision]:
        """Load latest ReviewDecision for each claim in the case."""
        rows = self.connection.execute(
            """
            SELECT d.payload_json FROM decisions d
            INNER JOIN (
                SELECT claim_id, MAX(version) as max_ver FROM decisions WHERE case_id = ? GROUP BY claim_id
            ) m ON d.claim_id = m.claim_id AND d.version = m.max_ver
            WHERE d.case_id = ?
            """,
            (case_id, case_id),
        ).fetchall()
        decisions = [ReviewDecision.model_validate_json(row["payload_json"]) for row in rows]
        return {d.claim_id: d for d in decisions}

    def load_case_audit_events(self, case_id: str) -> list[AuditEvent]:
        rows = self.connection.execute(
            "SELECT payload_json FROM audit_events WHERE case_id = ? ORDER BY event_id", (case_id,)
        ).fetchall()
        events = []
        for r in rows:
            data = json.loads(r["payload_json"])
            fallback_time = datetime.now(timezone.utc).isoformat()
            events.append(AuditEvent(
                task_id=data.get("task_id", ""),
                case_id=data.get("case_id", case_id),
                step=data.get("step", ""),
                started_at=data.get("started_at", fallback_time),
                finished_at=data.get("finished_at", fallback_time),
                duration_ms=int(data.get("duration_ms", 0) or 0),
                tool=data.get("tool", ""),
                status=data.get("status", "success"),
                model=data.get("model"),
                prompt_version=data.get("prompt_version"),
                input_hash=data.get("input_hash"),
                output_hash=data.get("output_hash"),
                input_tokens=data.get("input_tokens"),
                output_tokens=data.get("output_tokens"),
                latency_ms=data.get("latency_ms"),
                error=data.get("error"),
                details=data.get("details") or {},
            ))
        return events

    def save_investigation_items(self, case_id: str, items: list[dict[str, Any]]) -> None:
        """Persist mutable follow-up status without changing immutable evidence."""
        self.connection.executemany(
            """
            INSERT INTO investigation_items(case_id, item_id, status, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(case_id, item_id) DO UPDATE SET
                status = excluded.status,
                payload_json = excluded.payload_json
            """,
            [
                (
                    case_id,
                    str(item.get("item_id") or ""),
                    str(item.get("status") or "待核查"),
                    json.dumps(item, ensure_ascii=False),
                )
                for item in items
                if item.get("item_id")
            ],
        )
        self.connection.commit()

    def load_investigation_items(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM investigation_items WHERE case_id = ? ORDER BY rowid",
            (case_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
