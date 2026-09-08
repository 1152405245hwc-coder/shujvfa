"""Python wrapper that registers and mounts the fund flow Cytoscape component."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.components.v2 import component as _v2_component

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_JS_LIBRARY_ORDER = (
    "cytoscape.min.js",
    "elk.bundled.js",
    "cytoscape-elk.min.js",
)


@st.cache_resource
def _fund_flow_component():
    # Streamlit v2 loads component JS as an ES module, where top-level `this`
    # is undefined. UMD bundles (cytoscape-elk does `t.cytoscapeElk = e(t.ELK)`)
    # expect `this` to be the global object, so run them inside an IIFE
    # explicitly bound to globalThis; renderer.js stays at module top level
    # because it carries the required `export default`.
    libs = "\n;\n".join((_ASSETS_DIR / name).read_text(encoding="utf-8") for name in _JS_LIBRARY_ORDER)
    renderer = (_ASSETS_DIR / "renderer.js").read_text(encoding="utf-8")
    js = "(function () {\n" + libs + "\n}).call(globalThis);\n" + renderer
    css = (_ASSETS_DIR / "component.css").read_text(encoding="utf-8")
    return _v2_component(
        "fund_flow",
        html='<div class="ff-root"></div>',
        css=css,
        js=js,
    )


def render_fund_flow(payload: dict[str, Any], *, height: int = 420, key: str | None = None):
    """Mount the fund flow graph and return its state (``.selection``)."""
    fund_flow = _fund_flow_component()
    return fund_flow(
        key=key,
        # Pass the pixel height through data: the percentage-height chain from
        # Streamlit's layout wrapper through the shadow root is not guaranteed
        # to resolve, so the renderer sets explicit pixel heights itself.
        data={**payload, "view_height": height},
        height=height,
        default={"selection": None},
        on_selection_change=lambda: None,
    )
