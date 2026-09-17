const EG_COLORS = {
  navy: '#1f3a5f',
  ink: '#10222f',
  gold: '#b07d2b',
  border: '#d8dee4',
  label: '#55636e',
  muted: '#8b98a2',
};

const EG_TYPE_COLORS = {
  person: '#1f3a5f',
  account: '#10222f',
  claim: '#7a6c3f',
  evidence: '#5b7a8c',
};

const EG_NODE_W = 230;
const EG_NODE_H = 78;

const EG_WHEEL_STEP = 1.25;

const EG_VIEWPORTS = (window.__egViewports = window.__egViewports || {});

function egPartition(type) {
  if (type === 'person') return 0;
  if (type === 'account') return 1;
  if (type === 'claim') return 2;
  return 3;
}

const EG_TYPE_TEXT = {
  person: '人物',
  account: '账户',
  claim: '付款主张',
  evidence: '证据材料',
};

const EG_EDGE_TEXT = {
  '持有/关联账户': '持有/关联账户',
  '支付/收款': '支付/收款',
  '起诉书指称': '起诉书指称',
  '材料提及': '材料提及',
  '人工确认别名': '人工确认别名',
};

function egShape(type) {
  if (type === 'person') return 'ellipse';
  if (type === 'claim') return 'diamond';
  if (type === 'evidence') return 'round-rectangle';
  return 'rectangle';
}

function egNodeSize(d) {
  if (d.type === 'claim') return { w: EG_NODE_H, h: EG_NODE_H };
  if (d.type === 'person') return { w: EG_NODE_W - 40, h: EG_NODE_H };
  return { w: EG_NODE_W, h: EG_NODE_H };
}

function egNodeDisplay(d) {
  const lines = [d.name];
  if (d.type === 'account' && d.masked_account) lines.push(d.masked_account);
  if (d.type === 'person' && d.role_label) lines.push(d.role_label);
  if (d.type === 'claim') lines.push('¥' + Number(d.amount || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 }));
  return lines.join('\n');
}

function egRunLayout(cy, onDone) {
  let settled = false;
  const finish = () => {
    if (!settled) {
      settled = true;
      onDone();
    }
  };
  if (typeof ELK !== 'undefined') {
    try {
      const layout = cy.layout({
        name: 'elk',
        animate: false,
        nodeDimensionsIncludeLabels: true,
        elk: {
          'elk.algorithm': 'layered',
          'elk.direction': 'RIGHT',
          'elk.separateConnectedComponents': false,
          'elk.spacing.nodeNode': 40,
          'elk.layered.spacing.nodeNodeBetweenLayers': 120,
          'elk.spacing.componentComponent': 60,
          'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
          'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
        },
      });
      layout.one('layoutstop', () => {
        egSnapColumns(cy);
        finish();
      });
      const promise = layout.promiseOn ? layout.promiseOn('layoutstop') : null;
      if (promise && typeof promise.catch === 'function') {
        promise.catch(() => {
          egPresetLayout(cy);
          finish();
        });
      }
      layout.run();
      setTimeout(() => {
        if (!settled) {
          egPresetLayout(cy);
          finish();
        }
      }, 3000);
      return;
    } catch (err) {
      // fall through to deterministic preset layout
    }
  }
  egPresetLayout(cy);
  finish();
}

function egSnapColumns(cy) {
  const cols = [[], [], [], []];
  cy.nodes().forEach((n) => cols[n.data('partition')].push(n));
  const gapX = EG_NODE_W + 100;
  const gapY = EG_NODE_H + 48;
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

function egPresetLayout(cy) {
  const columns = [[], [], [], []];
  cy.nodes().forEach((n) => columns[n.data('partition')].push(n));
  const colX = [0, EG_NODE_W + 140, (EG_NODE_W + 140) * 2, (EG_NODE_W + 140) * 3];
  columns.forEach((col, ci) => {
    col.sort((a, b) => (b.data('amount') || 0) - (a.data('amount') || 0));
    col.forEach((n, i) => {
      n.position({ x: colX[ci], y: i * (EG_NODE_H + 56) });
    });
  });
}

function egFitWhenReady(cy, container, restore) {
  let done = false;
  const tryFit = () => {
    if (done) return true;
    if (container.clientWidth < 10 || container.clientHeight < 10) return false;
    cy.resize();
    if (restore) {
      cy.viewport({ zoom: restore.zoom, pan: restore.pan });
      done = true;
      return true;
    }
    cy.fit(undefined, 24);
    if (cy.zoom() < 0.4) {
      cy.zoom(0.4);
      cy.center();
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

function egBuildTooltip(tip, title, rows) {
  let html = '<div class="eg-tt-title">' + title + '</div>';
  rows.forEach(([k, v]) => {
    if (!v) return;
    html += '<div class="eg-tt-row"><span>' + k + '</span><span>' + v + '</span></div>';
  });
  tip.innerHTML = html;
}

export default function (component) {
  const { data, setStateValue, parentElement, key } = component;
  let root = parentElement.querySelector('.eg-root');
  if (!root) {
    root = document.createElement('div');
    root.className = 'eg-root';
    parentElement.appendChild(root);
  }

  const payload = data || { nodes: [], edges: [] };
  const viewH = Math.max(240, Number(payload.view_height) || 560);
  const graphSig = JSON.stringify([payload.nodes, payload.edges, viewH]);
  const viewKey = payload.component_key || key || 'eg';
  const savedView = EG_VIEWPORTS[viewKey];
  const restoreView = savedView && savedView.sig === graphSig ? savedView : null;

  // Same payload as the live instance: keep it untouched (zoom, pan, focus,
  // and the selection newer than Python's echo all survive a rerun).
  if (root.__egSig === graphSig && root.__cy && !root.__cy.destroyed()) {
    return root.__egCleanup;
  }
  root.__egSig = graphSig;

  if (root.__cy && !root.__cy.destroyed()) root.__cy.destroy();

  root.innerHTML =
    '<div class="eg-canvas"></div>' +
    '<div class="eg-toolbar">' +
    '<button type="button" data-eg="fit">适配视图</button>' +
    '<button type="button" data-eg="reset">重置缩放</button>' +
    '<button type="button" data-eg="png">导出高清图</button>' +
    '</div>' +
    '<div class="eg-legend">' +
    '<span class="eg-key"><i class="eg-node person"></i>人物</span>' +
    '<span class="eg-key"><i class="eg-node account"></i>账户</span>' +
    '<span class="eg-key"><i class="eg-node claim"></i>付款主张</span>' +
    '<span class="eg-key"><i class="eg-node evidence"></i>证据材料</span>' +
    '<span class="eg-key"><i class="eg-line dashed"></i>待证/争议</span>' +
    '</div>' +
    '<div class="eg-tooltip"></div>';

  const canvas = root.querySelector('.eg-canvas');
  const tip = root.querySelector('.eg-tooltip');
  root.style.height = viewH + 'px';
  canvas.style.height = viewH + 'px';

  if (!payload.nodes || payload.nodes.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'eg-empty';
    empty.textContent = '暂无案件关系数据';
    root.appendChild(empty);
    return;
  }

  const elements = [];
  payload.nodes.forEach((n) => {
    const size = egNodeSize(n);
    elements.push({
      group: 'nodes',
      data: {
        id: n.id,
        type: n.type,
        partition: egPartition(n.type),
        name: n.name,
        label: n.label || n.name,
        role: n.role || '',
        role_label: n.role_label || '',
        masked_account: n.masked_account || '',
        amount: n.amount || 0,
        source_refs: n.source_refs || [],
        display: egNodeDisplay(n),
        node_w: size.w,
        node_h: size.h,
      },
    });
  });
  payload.edges.forEach((e) => {
    const amountLabel = e.type === '支付/收款' && e.amount
      ? '¥' + Number(e.amount).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) +
        (e.count > 1 ? ' · ' + e.count + '笔' : '')
      : '';
    elements.push({
      group: 'edges',
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        edge_type: e.type,
        edge_label: EG_EDGE_TEXT[e.type] || e.type,
        disputed: !!e.disputed,
        disputed_label: e.disputed ? '待证/争议' : '',
        amount_label: amountLabel,
        amount: e.amount || 0,
        count: e.count || 1,
        reason: e.reason || '',
        source_refs: e.source_refs || [],
        width: e.width || 2,
      },
    });
  });

  const cy = cytoscape({
    container: canvas,
    elements,
    minZoom: 0.25,
    maxZoom: 4,
    style: [
      {
        selector: 'node',
        style: {
          shape: 'data(type)',
          width: 'data(node_w)',
          height: 'data(node_h)',
          'background-color': '#ffffff',
          'border-width': 1,
          'border-color': EG_COLORS.border,
          label: 'data(display)',
          'text-wrap': 'wrap',
          'text-max-width': String(EG_NODE_W - 24) + 'px',
          'font-size': 13,
          'line-height': 1.4,
          color: EG_COLORS.ink,
          'text-valign': 'center',
          'text-halign': 'center',
          padding: '8px',
        },
      },
      {
        selector: 'node[type = "person"]',
        style: { 'border-color': EG_TYPE_COLORS.person },
      },
      {
        selector: 'node[type = "account"]',
        style: { 'border-color': EG_TYPE_COLORS.account },
      },
      {
        selector: 'node[type = "claim"]',
        style: { 'border-color': EG_TYPE_COLORS.claim },
      },
      {
        selector: 'node[type = "evidence"]',
        style: { 'border-color': EG_TYPE_COLORS.evidence },
      },
      {
        selector: 'edge',
        style: {
          width: 'data(width)',
          'line-color': EG_COLORS.navy,
          'target-arrow-color': EG_COLORS.navy,
          'target-arrow-shape': 'triangle',
          'arrow-scale': 0.8,
          'curve-style': 'bezier',
          label: 'data(amount_label)',
          'font-size': 11,
          color: EG_COLORS.label,
          'text-background-color': '#fdfdfb',
          'text-background-opacity': 1,
          'text-background-padding': '2px',
          'text-margin-y': -8,
        },
      },
      {
        selector: 'edge[?disputed]',
        style: {
          'line-color': EG_COLORS.gold,
          'target-arrow-color': EG_COLORS.gold,
          'line-style': 'dashed',
          'line-dash-pattern': [6, 4],
        },
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 2, 'border-color': EG_COLORS.navy },
      },
      {
        selector: 'edge:selected',
        style: { 'line-style': 'solid' },
      },
      {
        selector: '.eg-dim',
        style: { opacity: 0.12, 'text-opacity': 0.3 },
      },
      {
        selector: 'node.eg-neighbor',
        style: { 'border-width': 2, 'background-color': '#eef3f8' },
      },
      {
        selector: 'edge.eg-neighbor',
        style: {
          opacity: 1,
          'z-index': 20,
          'text-background-color': '#fdf3dd',
          'text-background-opacity': 1,
        },
      },
    ],
  });

  let egViewRaf = 0;
  const egSaveView = () => {
    if (egViewRaf) return;
    egViewRaf = requestAnimationFrame(() => {
      egViewRaf = 0;
      if (cy.destroyed()) return;
      const pan = cy.pan();
      EG_VIEWPORTS[viewKey] = { sig: graphSig, zoom: cy.zoom(), pan: { x: pan.x, y: pan.y } };
    });
  };
  cy.on('viewport', egSaveView);

  const egOnWheel = (evt) => {
    if (!canvas.contains(evt.target)) return;
    evt.preventDefault();
    evt.stopPropagation();
    let delta = evt.deltaY;
    if (evt.deltaMode === 1) delta *= 33;
    else if (evt.deltaMode === 2) delta *= 400;
    delta = Math.max(-300, Math.min(300, delta));
    const rect = canvas.getBoundingClientRect();
    cy.zoom({
      level: cy.zoom() * Math.pow(EG_WHEEL_STEP, -delta / 100),
      renderedPosition: { x: evt.clientX - rect.left, y: evt.clientY - rect.top },
    });
  };
  root.addEventListener('wheel', egOnWheel, { capture: true, passive: false });
  cy.on('destroy', () => root.removeEventListener('wheel', egOnWheel, { capture: true }));

  egPresetLayout(cy);
  egRunLayout(cy, () => {
    egFitWhenReady(cy, canvas, restoreView);
    // Consume the selection echo only on a genuine remount (see fund_flow).
    if (payload.selected && payload.selected.id) {
      const el = cy.getElementById(payload.selected.id);
      if (el && el.length) {
        el.select();
        egApplyFocus(el);
      }
    }
  });

  root.__cy = cy;

  let egScrollRaf = 0;
  const onDocScroll = () => {
    if (egScrollRaf) return;
    egScrollRaf = requestAnimationFrame(() => {
      egScrollRaf = 0;
      if (!cy.destroyed()) cy.resize();
    });
  };
  document.addEventListener('scroll', onDocScroll, { capture: true, passive: true });
  cy.on('destroy', () => document.removeEventListener('scroll', onDocScroll, { capture: true }));

  root.querySelector('[data-eg="fit"]').addEventListener('click', () => cy.fit(undefined, 24));
  root.querySelector('[data-eg="reset"]').addEventListener('click', () => {
    cy.zoom(1);
    cy.center();
  });
  root.querySelector('[data-eg="png"]').addEventListener('click', () => {
    const uri = cy.png({ full: true, scale: 3, bg: '#fdfdfb', maxWidth: 8000, maxHeight: 8000 });
    const a = document.createElement('a');
    a.href = uri;
    a.download = '案件关系图.png';
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

  cy.on('mouseover', 'node', (evt) => {
    const d = evt.target.data();
    const rows = [];
    if (d.type === 'person') rows.push(['角色', d.role_label || '人物']);
    if (d.type === 'account' && d.masked_account) rows.push(['账号', d.masked_account]);
    if (d.type === 'claim') rows.push(['主张金额', '¥' + Number(d.amount || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })]);
    rows.push(['来源条目', String((evt.target.data('source_refs') || []).length || '—')]);
    egBuildTooltip(tip, d.label, rows);
    tip.style.display = 'block';
    moveTip(evt);
  });
  cy.on('mousemove', 'node', moveTip);
  cy.on('mouseout', 'node', () => { tip.style.display = 'none'; });

  cy.on('mouseover', 'edge', (evt) => {
    const d = evt.target.data();
    egBuildTooltip(tip, d.edge_label + (d.disputed ? ' · 待证/争议' : ''), [
      ['笔数', String(d.count)],
      ['说明', d.reason],
    ]);
    tip.style.display = 'block';
    moveTip(evt);
  });
  cy.on('mousemove', 'edge', moveTip);
  cy.on('mouseout', 'edge', () => { tip.style.display = 'none'; });

  function egApplyFocus(target) {
    cy.elements().removeClass('eg-dim eg-neighbor');
    if (!target) return;
    const keep = target.isNode() ? target.closedNeighborhood() : target.union(target.source()).union(target.target());
    cy.elements().difference(keep).addClass('eg-dim');
    keep.forEach((el) => { if (el !== target) el.addClass('eg-neighbor'); });
  }

  cy.on('tap', 'node', (evt) => {
    egApplyFocus(evt.target);
    const d = evt.target.data();
    setStateValue('selection', {
      type: 'node',
      id: d.id,
      node_type: d.type,
      name: d.name,
      label: d.label,
      role: d.role,
      role_label: d.role_label,
      masked_account: d.masked_account,
      source_refs: d.source_refs || [],
    });
  });
  cy.on('tap', 'edge', (evt) => {
    egApplyFocus(evt.target);
    const d = evt.target.data();
    setStateValue('selection', {
      type: 'edge',
      id: d.id,
      edge_type: d.edge_type,
      disputed: d.disputed,
      amount: d.amount,
      count: d.count,
      reason: d.reason,
      source_refs: d.source_refs || [],
    });
  });
  cy.on('tap', (evt) => {
    if (evt.target === cy) {
      egApplyFocus(null);
      setStateValue('selection', null);
    }
  });

  root.__egCleanup = () => {
    try { cy.destroy(); } catch (err) { /* already destroyed */ }
  };
  return root.__egCleanup;
}
