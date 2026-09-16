from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from legal_funds_agent.audit.logger import AuditEvent
from legal_funds_agent.domain.models import Claim, ReviewDecision, Transaction


from legal_funds_agent.utils import mask_account as _mask_account


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
        existing = self.connection.execute(
            "SELECT payload_json FROM claims WHERE id = ?", (claim.id,)
        ).fetchone()
        if existing:
            # Older checkpoints did not persist the optional locator payload.
            # A locator enrichment is safe to replay; substantive claim fields
            # remain immutable and still raise below.
            stored_payload = json.loads(existing["payload_json"])
            identity_fields = set(payload) - {"source_locator_ids", "source_locators"}
            if all(stored_payload.get(field) == payload.get(field) for field in identity_fields):
                return
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
                    "SELECT case_id, payload_json FROM transactions WHERE id = ?", (storage_id,)
                ).fetchone()
                if existing and existing["case_id"] != tx.case_id:
                    storage_id = f"{tx.case_id}::{tx.id}"
                elif existing:
                    # Re-importing the same evidence through CSV/XLSX can
                    # legitimately change only the source row or locator ID.
                    # Those fields identify where to look, not which money
                    # event occurred; keep the immutable stored event while
                    # allowing the signature flow to proceed idempotently.
                    stored_payload = json.loads(existing["payload_json"])
                    identity_fields = set(payload) - {"source_row", "source_evidence_id"}
                    if all(stored_payload.get(field) == payload.get(field) for field in identity_fields):
                        continue
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
            UNION
            SELECT s.case_id,
                   (SELECT COUNT(*) FROM case_snapshots s2 WHERE s2.case_id = s.case_id) as claim_count,
                   0 as tx_count,
                   (SELECT COUNT(*) FROM decisions d WHERE d.case_id = s.case_id) as decision_count
            FROM case_snapshots s
            WHERE s.case_id NOT IN (SELECT DISTINCT case_id FROM claims)
              AND s.case_id NOT IN (SELECT DISTINCT case_id FROM transactions)
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

    def delete_case(self, case_id: str) -> None:
        """Delete one locally persisted case and all of its mutable/immutable rows.

        This is intentionally scoped to an exact case id. Original uploaded Word,
        Excel and other evidence files are never touched; the repository only
        stores the脱敏 checkpoint and audit metadata in SQLite.
        """
        tables = (
            "investigation_items",
            "audit_events",
            "decisions",
            "transactions",
            "claims",
            "case_snapshots",
            "case_meta",
        )
        try:
            self.connection.execute("BEGIN")
            for table in tables:
                self.connection.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def save_case_snapshot(self, case_id: str, claims: list[Claim]) -> None:
        """Upsert the mutable workbench state of a case's claim list.

        The claims table is an immutable signing record: a row only lands
        there after human confirmation, so unconfirmed model-extracted claims
        would be lost on UI reload. The snapshot is separate mutable state
        that preserves the full in-progress claim list; restoration should
        combine confirmed claims from `claims` with the snapshot to recover
        claims that were never confirmed. Account numbers are masked for the
        same reason as save_claim.
        """
        payload = {
            "case_id": case_id,
            "claims": [],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        for claim in claims:
            item = claim.model_dump(mode="json")
            item["victim_account"] = _mask_account(claim.victim_account)
            item["alleged_recipient_account"] = _mask_account(claim.alleged_recipient_account)
            payload["claims"].append(item)
        self.connection.execute(
            """
            INSERT INTO case_snapshots(case_id, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (case_id, json.dumps(payload, ensure_ascii=False), payload["saved_at"]),
        )
        self.connection.commit()

    def load_case_snapshot(self, case_id: str) -> list[Claim] | None:
        """Restore the snapshot claim list, or None when no snapshot exists."""
        row = self.connection.execute(
            "SELECT payload_json FROM case_snapshots WHERE case_id = ?", (case_id,)
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        return [Claim.model_validate(item) for item in payload.get("claims", [])]

    def save_case_display_name(self, case_id: str, display_name: str) -> None:
        """Store a human-chosen display alias for a case.

        The case id is the join key of every immutable record and never
        changes; the display name is mutable presentation-only metadata kept
        in a separate table so renaming a case can never touch signed claims
        or decisions.
        """
        self.connection.execute(
            """
            INSERT INTO case_meta(case_id, display_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (case_id, display_name, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def load_case_display_names(self) -> dict[str, str]:
        """Return all case display aliases keyed by case_id."""
        rows = self.connection.execute(
            "SELECT case_id, display_name FROM case_meta"
        ).fetchall()
        return {row["case_id"]: row["display_name"] for row in rows}

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
            events.append(AuditEvent(
                task_id=data.get("task_id", ""),
                case_id=data.get("case_id", case_id),
                step=data.get("step", ""),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
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
