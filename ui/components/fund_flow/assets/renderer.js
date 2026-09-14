const FF_COLORS = {
  navy: '#1f3a5f',
  ink: '#10222f',
  gold: '#b07d2b',
  refund: '#5b7a8c',
  excluded: '#b9c2c9',
  pending: '#8b98a2',
  border: '#d8dee4',
  label: '#55636e',
};

const FF_NODE_W = 240;
const FF_NODE_H = 84;

// Wheel zoom step: zoom multiplies by FF_WHEEL_STEP for every 100px of wheel
// delta (one notch on a standard mouse). Cytoscape's own wheel handling is
// bypassed (see the capture-phase listener below), so this is the single
// place that controls zoom speed.
const FF_WHEEL_STEP = 1.25;

// Viewport (zoom + pan) survives a remount: Streamlit can rebuild the
// component subtree (expander collapse, changed data, session restore), and
// the graph used to jump back to a fit-all view each time. Keyed by the
// caller's ``component_key`` and only reused when the graph is unchanged.
const FF_VIEWPORTS = (window.__ffViewports = window.__ffViewports || {});

function ffPartition(role) {
  if (role === 'victim') return 0;
  if (role === 'primary_suspect') return 1;
  return 2;
}

const FF_ROLE_TEXT = {
  victim: '被害人 / 资金来源',
  suspect: '涉案一级账户',
  third_party_disputed: '第三方争议账户',
  downstream: '后续流向账户',
};

function ffNodeDisplay(d) {
  const marker = d.display_role === 'third_party_disputed' ? '! ' : '';
  const lines = [marker + d.name];
  if (d.masked_account) lines.push(d.masked_account);
  if (d.role === 'victim') {
    lines.push('转出 ' + d.total_out_full);
  } else {
    lines.push('流入 ' + d.total_in_full);
  }
  return lines.join('\n');
}

function ffRunLayout(cy, onDone, marker) {
  let settled = false;
  const finish = (mode) => {
    if (!settled) {
      settled = true;
      if (typeof console !== 'undefined' && console.debug) console.debug('[fund_flow] layout:', mode);
      if (marker) marker(mode);
      onDone();
    }
  };
  if (typeof ELK !== 'undefined') {
    try {
      // Note: elkjs 0.11 does not honor partitioning constraints (verified
      // against elk.bundled.js directly), so ELK is used for its crossing
      // minimization / vertical ordering only; ffSnapColumns then enforces
      // the semantic columns (victim / suspect / downstream) deterministically.
      const layout = cy.layout({
        name: 'elk',
        animate: false,
        nodeDimensionsIncludeLabels: true,
        elk: {
          'elk.algorithm': 'layered',
          'elk.direction': 'RIGHT',
          'elk.separateConnectedComponents': false,
          'elk.spacing.nodeNode': 40,
          'elk.layered.spacing.nodeNodeBetweenLayers': 130,
          'elk.spacing.componentComponent': 60,
          'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
          'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
        },
      });
      layout.one('layoutstop', () => {
        ffSnapColumns(cy);
        finish('elk+snap');
      });
      const promise = layout.promiseOn ? layout.promiseOn('layoutstop') : null;
      if (promise && typeof promise.catch === 'function') {
        promise.catch(() => {
          ffPresetLayout(cy);
          finish('preset-error');
        });
      }
      layout.run();
      // If the ELK worker fails asynchronously (no layoutstop, no rejection),
      // fall back to the deterministic preset layout after a grace period.
      setTimeout(() => {
        if (!settled) {
          ffPresetLayout(cy);
          finish('preset-timeout');
        }
      }, 3000);
      return;
    } catch (err) {
      // fall through to deterministic preset layout
    }
  }
  ffPresetLayout(cy);
  finish('preset-no-elk');
}

function ffSnapColumns(cy) {
  // Enforce the semantic columns (0 = victim / 1 = suspect / 2 = downstream)
  // while keeping ELK's vertical ordering within each column. A semantic
  // column that stacks too deep flows into two side-by-side sub-columns:
  // wide graphs fit the viewport at a far more readable zoom than tall ones.
  const cols = [[], [], []];
  cy.nodes('[kind = "account"]').forEach((n) => cols[n.data('partition')].push(n));
  const gapX = FF_NODE_W + 110;
  const gapY = FF_NODE_H + 48;
  let xOffset = 0;
  cols.forEach((col) => {
    col.sort((a, b) => a.position('y') - b.position('y'));
    const subCols = col.length > 6 ? 2 : 1;
    const perCol = Math.ceil(col.length / subCols);
    col.forEach((n, i) => {
      n.position({ x: xOffset + Math.floor(i / perCol) * gapX, y: (i % perCol) * gapY });
    });
    xOffset += subCols * gapX;
  });
}

function ffPresetLayout(cy) {
  const columns = [[], [], []];
  cy.nodes().forEach((n) => {
    columns[n.data('partition')].push(n);
  });
  columns.forEach((col) => {
    col.sort((a, b) => (b.data('total_in') || 0) - (a.data('total_in') || 0));
    col.forEach((n, i) => {
      n.position({ x: 0, y: 0 }); // placeholder, set below
    });
  });
  const colX = [0, FF_NODE_W + 150, (FF_NODE_W + 150) * 2];
  columns.forEach((col, ci) => {
    col.forEach((n, i) => {
      n.position({ x: colX[ci], y: i * (FF_NODE_H + 56) });
    });
  });
}

function ffAccentColor(displayRole) {
  if (displayRole === 'victim') return FF_COLORS.navy;
  if (displayRole === 'suspect') return FF_COLORS.ink;
  if (displayRole === 'third_party_disputed') return FF_COLORS.gold;
  return '#9aa7b0';
}

function ffSyncAccent(node) {
  const accent = node.data('_accent');
  if (!accent) return;
  const pos = node.position();
  accent.position({ x: pos.x, y: pos.y - FF_NODE_H / 2 + 2 });
}

function ffAddAccents(cy) {
  const accents = [];
  cy.nodes('[kind = "account"]').forEach((n) => {
    const id = n.id() + '__rule';
    accents.push({
      group: 'nodes',
      data: { id, kind: 'rule', color: ffAccentColor(n.data('display_role')) },
      selectable: false,
      grabbable: false,
    });
  });
  const added = cy.add(accents);
  cy.nodes('[kind = "account"]').forEach((n) => {
    n.data('_accent', cy.getElementById(n.id() + '__rule'));
    ffSyncAccent(n);
  });
  return added;
}

function ffRouteRefunds(cy) {
  // Route return edges below the graph via taxi elbow routing.
  let maxY = 0;
  cy.nodes('[kind = "account"]').forEach((n) => {
    maxY = Math.max(maxY, n.position('y'));
  });
  const refunds = cy.edges('[?is_return]');
  refunds.forEach((edge, i) => {
    const srcY = edge.source().position('y');
    edge.data('taxiTurn', Math.max(40, maxY + 90 + i * 30 - srcY));
  });
}

function ffFitWhenReady(cy, container, restore) {
  // Streamlit can run the component JS before the shadow host has its final
  // size (tab fade-in, expander reveal, sidebar reflow). fit() on a zero-size
  // container is silently ignored, leaving zoom=1/pan=0 with all content
  // stacked at the origin. Retry until the container has a real size.
  let done = false;
  const tryFit = () => {
    if (done) return true;
    if (container.clientWidth < 10 || container.clientHeight < 10) return false;
    cy.resize();
    if (restore) {
      // Same graph as the last mount: put the viewport back exactly where the
      // reviewer left it instead of re-centring on a fresh fit().
      cy.viewport({ zoom: restore.zoom, pan: restore.pan });
      done = true;
      return true;
    }
    cy.fit(undefined, 24);
    // readability floor: on dense graphs fit() can shrink text below a usable
    // size; prefer a readable zoom centred on the fund source over showing
    // everything at once.
    if (cy.zoom() < 0.45) {
      cy.zoom(0.45);
      const victim = cy.nodes('[display_role = "victim"]').first();
      if (victim && victim.length) cy.center(victim); else cy.center();
    }
    done = true;
    return true;
  };
  if (tryFit()) return;
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => { if (tryFit()) ro.disconnect(); });
    ro.observe(container);
    setTimeout(() => { ro.disconnect(); tryFit(); }, 5000);
  } else {
    setTimeout(tryFit, 300);
  }
}

function ffBuildTooltip(tip, title, rows) {
  let html = '<div class="ff-tt-title">' + title + '</div>';
  rows.forEach(([k, v]) => {
    if (!v) return;
    html += '<div class="ff-tt-row"><span>' + k + '</span><span>' + v + '</span></div>';
  });
  tip.innerHTML = html;
}

export default function (component) {
  const { data, setStateValue, parentElement, key } = component;
  let root = parentElement.querySelector('.ff-root');
  if (!root) {
    root = document.createElement('div');
    root.className = 'ff-root';
    parentElement.appendChild(root);
  }

  const payload = data || { nodes: [], edges: [] };
  // The percentage-height chain from Streamlit's layout wrapper through the
  // shadow root is not guaranteed to resolve to a definite height, so set
  // explicit pixel heights on the canvas ourselves.
  const viewH = Math.max(240, Number(payload.view_height) || 430);
  const graphSig = JSON.stringify([payload.nodes, payload.edges, viewH]);
  const viewKey = payload.component_key || key || 'ff';
  // Viewport from the previous mount of this same graph, if any.
  const savedView = FF_VIEWPORTS[viewKey];
  const restoreView = savedView && savedView.sig === graphSig ? savedView : null;

  // Streamlit reruns re-invoke this renderer with the SAME parentElement and a
  // fresh data copy. Rebuilding there would blank the canvas for a frame and
  // relayout+refit, making the view jump. When the graph content is unchanged,
  // keep the live instance exactly as-is: zoom, pan, listeners, and the focus
  // the user just tapped are all newer than the payload's selection echo
  // (Python lags one rerun behind), so re-applying the echo would visibly
  // revert the highlight for a frame. The echo is only consumed on a fresh
  // mount below.
  if (root.__ffSig === graphSig && root.__cy && !root.__cy.destroyed()) {
    return root.__ffCleanup;
  }
  root.__ffSig = graphSig;

  // A previous render may have left a live cytoscape instance on this root;
  // destroy it before wiping the DOM so its listeners don't leak.
  if (root.__cy && !root.__cy.destroyed()) root.__cy.destroy();

  root.innerHTML =
    '<div class="ff-canvas"></div>' +
    '<div class="ff-toolbar">' +
    '<button type="button" data-ff="fit">适配视图</button>' +
    '<button type="button" data-ff="reset">重置缩放</button>' +
    '<button type="button" data-ff="png">导出高清图</button>' +
    '</div>' +
    '<div class="ff-legend">' +
    '<span class="ff-key"><i class="ff-line"></i>已纳入</span>' +
    '<span class="ff-key"><i class="ff-line dashed"></i>争议项</span>' +
    '<span class="ff-key"><i class="ff-line refund"></i>疑似转回</span>' +
    '<span class="ff-key"><i class="ff-line excluded"></i>已排除</span>' +
    '</div>' +
    '<div class="ff-tooltip"></div>';

  const canvas = root.querySelector('.ff-canvas');
  const tip = root.querySelector('.ff-tooltip');
  root.style.height = viewH + 'px';
  canvas.style.height = viewH + 'px';

  if (!payload.nodes || payload.nodes.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'ff-empty';
    empty.textContent = '暂无资金流向数据';
    root.appendChild(empty);
    return;
  }

  const elements = [];
  payload.nodes.forEach((n) => {
    elements.push({
      group: 'nodes',
      data: {
        id: n.id,
        kind: 'account',
        role: n.role,
        display_role: n.display_role || 'downstream',
        partition: ffPartition(n.role),
        name: n.name,
        masked_account: n.masked_account || '',
        total_in: n.total_in,
        total_out: n.total_out,
        total_in_full: n.total_in_full,
        total_out_full: n.total_out_full,
        display: ffNodeDisplay(n),
      },
    });
  });
  // Among parallel edges between the same account pair, only the largest one
  // keeps its amount label; the rest are available via hover/click. This keeps
  // dense corridors readable.
  const byPair = {};
  payload.edges.forEach((e) => {
    const key = e.source + '->' + e.target;
    (byPair[key] = byPair[key] || []).push(e);
  });
  Object.values(byPair).forEach((group) => {
    if (group.length < 2) return;
    group.sort((a, b) => b.amount - a.amount);
    group.slice(1).forEach((e) => { e.amount_label = ''; });
  });

  payload.edges.forEach((e) => {
    elements.push({
      group: 'edges',
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        disposition: e.disposition,
        disposition_label: e.disposition_label,
        amount_label: e.amount_label,
        amount_full: e.amount_full,
        count: e.count,
        reason: e.reason || '',
        date_min: e.date_min,
        date_max: e.date_max,
        width: e.width,
        is_return: !!e.is_return,
        transaction_ids: e.transaction_ids || [],
        source_refs: e.source_refs || [],
        taxiTurn: 60,
      },
    });
  });

  const cy = cytoscape({
    container: canvas,
    elements,
    minZoom: 0.3,
    // Headroom for the faster wheel step: at 25% per notch the old 2.5 ceiling
    // was reached in four notches, which capped detail reading.
    maxZoom: 4,
    style: [
      {
        selector: 'node[kind = "account"]',
        style: {
          shape: 'rectangle',
          width: FF_NODE_W,
          height: FF_NODE_H,
          'background-color': '#ffffff',
          'border-width': 1,
          'border-color': FF_COLORS.border,
          label: 'data(display)',
          'text-wrap': 'wrap',
          'text-max-width': String(FF_NODE_W - 20) + 'px',
          'font-size': 14,
          'line-height': 1.45,
          color: FF_COLORS.ink,
          'text-valign': 'center',
          'text-halign': 'center',
          padding: '8px',
        },
      },
      {
        selector: 'node[kind = "account"][display_role = "third_party_disputed"]',
        style: { 'border-color': '#e3d3ae' },
      },
      {
        selector: 'node[kind = "rule"]',
        style: {
          shape: 'rectangle',
          width: FF_NODE_W,
          height: 3,
          'background-color': 'data(color)',
          'border-width': 0,
          label: '',
          events: 'no',
        },
      },
      {
        selector: 'edge',
        style: {
          width: 'data(width)',
          'line-color': FF_COLORS.navy,
          'target-arrow-color': FF_COLORS.navy,
          'target-arrow-shape': 'triangle',
          'arrow-scale': 0.9,
          'curve-style': 'bezier',
          label: 'data(amount_label)',
          'font-size': 12,
          color: FF_COLORS.label,
          'text-background-color': '#fdfdfb',
          'text-background-opacity': 1,
          'text-background-padding': '2px',
          'text-margin-y': -8,
        },
      },
      {
        selector: 'edge[disposition = "DISPUTED"]',
        style: {
          'line-color': FF_COLORS.gold,
          'target-arrow-color': FF_COLORS.gold,
          'line-style': 'dashed',
          'line-dash-pattern': [6, 4],
        },
      },
      {
        selector: 'edge[disposition = "PENDING"]',
        style: {
          'line-color': FF_COLORS.pending,
          'target-arrow-color': FF_COLORS.pending,
          'line-style': 'dashed',
          'line-dash-pattern': [3, 4],
        },
      },
      {
        selector: 'edge[disposition = "EXCLUDED"]',
        style: {
          'line-color': FF_COLORS.excluded,
          'target-arrow-color': FF_COLORS.excluded,
          width: 1.5,
          opacity: 0.6,
        },
      },
      {
        selector: 'edge[?is_return]',
        style: {
          'curve-style': 'taxi',
          'taxi-direction': 'downward',
          'taxi-turn': 'data(taxiTurn)',
          'taxi-radius': 2,
          'line-color': FF_COLORS.refund,
          'target-arrow-color': FF_COLORS.refund,
          'line-style': 'solid',
          'text-margin-y': -10,
        },
      },
      {
        selector: 'node:selected',
        style: { 'border-color': FF_COLORS.navy, 'border-width': 2 },
      },
      {
        selector: 'edge:selected',
        style: { 'line-style': 'solid' },
      },
      {
        selector: '.ff-dim',
        style: { opacity: 0.12, 'text-opacity': 0.3 },
      },
      {
        selector: 'node.ff-neighbor',
        style: {
          'border-color': FF_COLORS.navy,
          'border-width': 2,
          'background-color': '#eef3f8',
        },
      },
      {
        selector: 'edge.ff-neighbor',
        style: {
          opacity: 1,
          'z-index': 20,
          'text-background-color': '#fdf3dd',
          'text-background-opacity': 1,
        },
      },
    ],
  });

  // Remember the viewport so a remount (tab switch, data reload) can put the
  // reviewer back where they were instead of jumping to a fresh fit().
  let ffViewRaf = 0;
  const ffSaveView = () => {
    if (ffViewRaf) return;
    ffViewRaf = requestAnimationFrame(() => {
      ffViewRaf = 0;
      if (cy.destroyed()) return;
      const pan = cy.pan();
      FF_VIEWPORTS[viewKey] = { sig: graphSig, zoom: cy.zoom(), pan: { x: pan.x, y: pan.y } };
    });
  };
  cy.on('viewport', ffSaveView);

  // Cytoscape's built-in wheel zoom samples the first four wheel events before
  // deciding how to normalise the delta, so the opening notches move at a
  // different rate from the rest, and any sensitivity that keeps those first
  // notches sane is far too slow afterwards. Own the wheel instead: this
  // capture-phase listener on the wrapper runs before Cytoscape's own
  // container-level listener and stops the event there, then applies one
  // fixed, generous step per notch.
  const ffOnWheel = (evt) => {
    if (!canvas.contains(evt.target)) return; // outside the canvas: let the page scroll
    evt.preventDefault();
    evt.stopPropagation();
    let delta = evt.deltaY;
    if (evt.deltaMode === 1) delta *= 33; // DOM_DELTA_LINE
    else if (evt.deltaMode === 2) delta *= 400; // DOM_DELTA_PAGE
    delta = Math.max(-300, Math.min(300, delta)); // trackpads can emit huge deltas
    const rect = canvas.getBoundingClientRect();
    cy.zoom({
      level: cy.zoom() * Math.pow(FF_WHEEL_STEP, -delta / 100),
      renderedPosition: { x: evt.clientX - rect.left, y: evt.clientY - rect.top },
    });
  };
  root.addEventListener('wheel', ffOnWheel, { capture: true, passive: false });
  cy.on('destroy', () => root.removeEventListener('wheel', ffOnWheel, { capture: true }));

  // Paint the deterministic column layout immediately so the graph is visible
  // on the first frame; ELK then refines positions asynchronously.
  ffPresetLayout(cy);
  ffRunLayout(cy, () => {
    ffAddAccents(cy);
    ffRouteRefunds(cy);
    ffFitWhenReady(cy, canvas, restoreView);
    // On a genuine remount Python echoes the last selection back in the
    // payload; re-apply the focus highlight here. (On a plain rerun the guard
    // above keeps the live instance, which already carries the newer
    // selection, so this only runs for a real rebuild.)
    if (payload.selected && payload.selected.id) {
      const el = cy.getElementById(payload.selected.id);
      if (el && el.length) {
        el.select();
        ffApplyFocus(el);
      }
    }
  }, (mode) => {
    root.dataset.ffLayout = mode;
  });

  cy.nodes('[kind = "account"]').on('position', (evt) => ffSyncAccent(evt.target));

  root.__cy = cy;

  // Cytoscape caches the container's client rect and invalidates it via
  // scroll listeners on the container's parentNode chain — but that chain
  // stops at this component's shadow root, and Streamlit scrolls a page-level
  // div outside it. After page scroll the cached rect goes stale and pointer
  // hits land offset by the scroll delta. Scroll events do not bubble, so
  // listen on the whole document in the capture phase.
  let ffScrollRaf = 0;
  const onDocScroll = () => {
    if (ffScrollRaf) return;
    ffScrollRaf = requestAnimationFrame(() => {
      ffScrollRaf = 0;
      if (!cy.destroyed()) cy.resize();
    });
  };
  document.addEventListener('scroll', onDocScroll, { capture: true, passive: true });
  cy.on('destroy', () => document.removeEventListener('scroll', onDocScroll, { capture: true }));

  root.querySelector('[data-ff="fit"]').addEventListener('click', () => cy.fit(undefined, 24));
  root.querySelector('[data-ff="reset"]').addEventListener('click', () => {
    cy.zoom(1);
    cy.center();
  });
  root.querySelector('[data-ff="png"]').addEventListener('click', () => {
    const uri = cy.png({ full: true, scale: 3, bg: '#fdfdfb', maxWidth: 8000, maxHeight: 8000 });
    const a = document.createElement('a');
    a.href = uri;
    a.download = '资金流向图.png';
    a.click();
  });

  function moveTip(evt) {
    const rect = root.getBoundingClientRect();
    const rp = evt.renderedPosition || (evt.originalEvent ? {
      x: evt.originalEvent.clientX - rect.left,
      y: evt.originalEvent.clientY - rect.top,
    } : { x: 0, y: 0 });
    tip.style.left = Math.min(rp.x + 14, rect.width - 240) + 'px';
    tip.style.top = Math.max(rp.y - 10, 8) + 'px';
  }

  cy.on('mouseover', 'node[kind = "account"]', (evt) => {
    const d = evt.target.data();
    const roleText = FF_ROLE_TEXT[d.display_role] || '其他账户';
    ffBuildTooltip(tip, d.name + (d.masked_account ? ' · ' + d.masked_account : ''), [
      ['账户性质', roleText],
      ['累计流入', d.total_in_full],
      ['累计流出', d.total_out_full],
    ]);
    tip.style.display = 'block';
    moveTip(evt);
  });
  cy.on('mousemove', 'node[kind = "account"]', moveTip);
  cy.on('mouseout', 'node[kind = "account"]', () => { tip.style.display = 'none'; });

  cy.on('mouseover', 'edge', (evt) => {
    const d = evt.target.data();
    const dates = d.date_min === d.date_max ? d.date_min : d.date_min + ' ~ ' + d.date_max;
    ffBuildTooltip(tip, d.amount_full + ' · ' + d.disposition_label, [
      ['交易笔数', String(d.count) + ' 笔'],
      ['日期范围', dates],
      ['说明', d.reason],
    ]);
    tip.style.display = 'block';
    moveTip(evt);
  });
  cy.on('mousemove', 'edge', moveTip);
  cy.on('mouseout', 'edge', () => { tip.style.display = 'none'; });

  // Focus the selected element's neighborhood: keep the selected node/edge,
  // its connected edges and neighbor accounts (plus their accent rules) at
  // full contrast, and dim everything else so one subject's fund movement can
  // be read in isolation.
  function ffApplyFocus(target) {
    cy.elements().removeClass('ff-dim ff-neighbor');
    if (!target) return;
    let keep = target.isNode()
      ? target.closedNeighborhood()
      : target.union(target.source()).union(target.target());
    keep.forEach((el) => {
      if (el.isNode && el.isNode() && el.data('kind') === 'account') {
        const accent = el.data('_accent');
        if (accent) keep = keep.union(accent);
      }
    });
    cy.elements().difference(keep).addClass('ff-dim');
    keep.forEach((el) => { if (el !== target) el.addClass('ff-neighbor'); });
  }

  cy.on('tap', 'node[kind = "account"]', (evt) => {
    ffApplyFocus(evt.target);
    const d = evt.target.data();
    setStateValue('selection', {
      type: 'node',
      id: d.id,
      name: d.name,
      role: d.role,
      display_role: d.display_role,
      masked_account: d.masked_account,
      total_in_full: d.total_in_full,
      total_out_full: d.total_out_full,
    });
  });
  cy.on('tap', 'edge', (evt) => {
    ffApplyFocus(evt.target);
    const d = evt.target.data();
    setStateValue('selection', {
      type: 'edge',
      id: d.id,
      count: d.count,
      amount_full: d.amount_full,
      disposition_label: d.disposition_label,
      reason: d.reason,
      date_min: d.date_min,
      date_max: d.date_max,
      transaction_ids: d.transaction_ids,
      source_refs: d.source_refs || [],
    });
  });
  cy.on('tap', (evt) => {
    if (evt.target === cy) {
      ffApplyFocus(null);
      setStateValue('selection', null);
    }
  });

  root.__ffCleanup = () => {
    try { cy.destroy(); } catch (err) { /* already destroyed */ }
  };
  return root.__ffCleanup;
}
