"""Python wrapper that registers and mounts the evidence graph Cytoscape component."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.components.v2 import component as _v2_component

# The cytoscape/elk UMD bundles are shared with the fund_flow component; only
# this component's own renderer and css live here.
_FUND_FLOW_ASSETS = Path(__file__).resolve().parents[1] / "fund_flow" / "assets"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_JS_LIBRARY_ORDER = (
    "cytoscape.min.js",
    "elk.bundled.js",
    "cytoscape-elk.min.js",
)


@st.cache_resource
def _evidence_graph_component():
    # Streamlit v2 loads component JS as an ES module, where top-level `this`
    # is undefined. UMD bundles (cytoscape-elk does `t.cytoscapeElk = e(t.ELK)`)
    # expect `this` to be the global object, so run them inside an IIFE
    # explicitly bound to globalThis; renderer.js stays at module top level
    # because it carries the required `export default`.
    libs = "\n;\n".join((_FUND_FLOW_ASSETS / name).read_text(encoding="utf-8") for name in _JS_LIBRARY_ORDER)
    renderer = (_ASSETS_DIR / "renderer.js").read_text(encoding="utf-8")
    js = "(function () {\n" + libs + "\n}).call(globalThis);\n" + renderer
    css = (_ASSETS_DIR / "component.css").read_text(encoding="utf-8")
    return _v2_component(
        "evidence_graph",
        html='<div class="eg-root"></div>',
        css=css,
        js=js,
    )


def render_evidence_graph(payload: dict[str, Any], *, height: int = 560, key: str | None = None):
    """Mount the evidence relationship graph and return its state (``.selection``).

    Selection changes are echoed back without forcing a rerun rebuild: the
    renderer keeps its live Cytoscape instance whenever the incoming payload is
    identical (the rerun lesson documented in fund_flow), so clicking a node
    only updates component state.
    """
    evidence_graph = _evidence_graph_component()
    return evidence_graph(
        key=key,
        # Pass the pixel height through data: the percentage-height chain from
        # Streamlit's layout wrapper through the shadow root is not guaranteed
        # to resolve, so the renderer sets explicit pixel heights itself.
        data={**payload, "view_height": height, "component_key": key or ""},
        height=height,
        default={"selection": None},
        on_selection_change=lambda: None,
    )
