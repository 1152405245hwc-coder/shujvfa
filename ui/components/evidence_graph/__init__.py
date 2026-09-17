"""Evidence relationship graph custom component (Cytoscape.js + ELK via Streamlit Components v2).

Reuses the same component-registration, payload-passing, and selection-echo
pattern as ``components.fund_flow`` (see that package for the rerun lessons).
"""

from components.evidence_graph.component import render_evidence_graph
from components.evidence_graph.payload import evidence_graph_to_payload

__all__ = ["render_evidence_graph", "evidence_graph_to_payload"]
