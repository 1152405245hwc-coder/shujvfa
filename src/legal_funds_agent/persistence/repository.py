from __future__ import annotations

import json
import sqlite3

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
        payload["alleged_recipient_account_id"] = None
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
                payload["payer_account_id"] = None
                payload["payee_account_id"] = None
                self._insert_immutable(
                    "transactions", tx.id, ("id", "case_id", "fingerprint", "payload_json"),
                    (tx.id, tx.case_id, tx.dedup_fingerprint, json.dumps(payload, ensure_ascii=False)),
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
