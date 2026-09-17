"""Serialize an EvidenceGraph into the JSON payload consumed by the component.

Thin re-export so callers can ``from components.evidence_graph import
evidence_graph_to_payload`` without touching the services layer.
"""

from __future__ import annotations

from legal_funds_agent.services.evidence_graph_service import (
    evidence_graph_to_payload,
)

__all__ = ["evidence_graph_to_payload"]
