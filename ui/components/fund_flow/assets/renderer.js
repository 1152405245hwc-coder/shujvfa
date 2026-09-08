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

const FF_NODE_W = 210;
const FF_NODE_H = 72;

function ffPartition(role) {
  if (role === 'victim') return 0;
  if (role === 'primary_suspect') return 1;
  return 2;
}

function ffNodeDisplay(d) {
  const marker = d.role === 'secondary_account' ? '! ' : '';
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
      const layout = cy.layout({
        name: 'elk',
        animate: false,
        nodeDimensionsIncludeLabels: true,
        elk: {
          'elk.algorithm': 'layered',
          'elk.direction': 'RIGHT',
          'elk.partitioning.activate': true,
          'elk.separateConnectedComponents': false,
          'elk.spacing.nodeNode': 40,
          'elk.layered.spacing.nodeNodeBetweenLayers': 130,
          'elk.spacing.componentComponent': 60,
          'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
          'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
        },
        nodeLayoutOptions: (node) => ({
          'partition': String(node.data('partition')),
        }),
      });
      layout.one('layoutstop', () => finish('elk'));
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

function ffAccentColor(role) {
  if (role === 'victim') return FF_COLORS.navy;
  if (role === 'primary_suspect') return FF_COLORS.ink;
  return FF_COLORS.gold;
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
      data: { id, kind: 'rule', color: ffAccentColor(n.data('role')) },
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

function ffBuildTooltip(tip, title, rows) {
  let html = '<div class="ff-tt-title">' + title + '</div>';
  rows.forEach(([k, v]) => {
    if (!v) return;
    html += '<div class="ff-tt-row"><span>' + k + '</span><span>' + v + '</span></div>';
  });
  tip.innerHTML = html;
}

export default function (component) {
  const { data, setStateValue, parentElement } = component;
  let root = parentElement.querySelector('.ff-root');
  if (!root) {
    root = document.createElement('div');
    root.className = 'ff-root';
    parentElement.appendChild(root);
  }

  root.innerHTML =
    '<div class="ff-canvas"></div>' +
    '<div class="ff-toolbar">' +
    '<button type="button" data-ff="fit">适配视图</button>' +
    '<button type="button" data-ff="reset">重置缩放</button>' +
    '</div>' +
    '<div class="ff-legend">' +
    '<span class="ff-key"><i class="ff-line"></i>已纳入</span>' +
    '<span class="ff-key"><i class="ff-line dashed"></i>争议项</span>' +
    '<span class="ff-key"><i class="ff-line refund"></i>疑似转回</span>' +
    '<span class="ff-key"><i class="ff-line excluded"></i>已排除</span>' +
    '</div>' +
    '<div class="ff-tooltip"></div>';

  const payload = data || { nodes: [], edges: [] };

  // The percentage-height chain from Streamlit's layout wrapper through the
  // shadow root is not guaranteed to resolve to a definite height, so set
  // explicit pixel heights on the canvas ourselves.
  const viewH = Math.max(240, Number(payload.view_height) || 430);
  root.style.height = viewH + 'px';
  const canvas = root.querySelector('.ff-canvas');
  canvas.style.height = viewH + 'px';
  const tip = root.querySelector('.ff-tooltip');

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
        taxiTurn: 60,
      },
    });
  });

  const cy = cytoscape({
    container: canvas,
    elements,
    wheelSensitivity: 0.25,
    minZoom: 0.3,
    maxZoom: 2.5,
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
          'font-size': 11,
          'line-height': 1.45,
          color: FF_COLORS.ink,
          'text-valign': 'center',
          'text-halign': 'center',
          padding: '8px',
        },
      },
      {
        selector: 'node[kind = "account"][role = "secondary_account"]',
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
          'font-size': 10,
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
    ],
  });

  // Paint the deterministic column layout immediately so the graph is visible
  // on the first frame; ELK then refines positions asynchronously.
  if (typeof window !== 'undefined' && window.__FF_DEBUG) window.__cy = cy;
  ffPresetLayout(cy);
  ffRunLayout(cy, () => {
    cy.resize();
    ffAddAccents(cy);
    ffRouteRefunds(cy);
    cy.fit(undefined, 24);
  }, (mode) => {
    root.dataset.ffLayout = mode;
  });

  cy.nodes('[kind = "account"]').on('position', (evt) => ffSyncAccent(evt.target));

  root.querySelector('[data-ff="fit"]').addEventListener('click', () => cy.fit(undefined, 24));
  root.querySelector('[data-ff="reset"]').addEventListener('click', () => {
    cy.zoom(1);
    cy.center();
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
    const roleText = { victim: '被害人', primary_suspect: '涉案一级账户', secondary_account: '关联第三方账户' }[d.role] || '其他账户';
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

  cy.on('tap', 'node[kind = "account"]', (evt) => {
    const d = evt.target.data();
    setStateValue('selection', {
      type: 'node',
      id: d.id,
      name: d.name,
      role: d.role,
      masked_account: d.masked_account,
      total_in_full: d.total_in_full,
      total_out_full: d.total_out_full,
    });
  });
  cy.on('tap', 'edge', (evt) => {
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
    });
  });
  cy.on('tap', (evt) => {
    if (evt.target === cy) setStateValue('selection', null);
  });

  return () => {
    try { cy.destroy(); } catch (err) { /* already destroyed */ }
  };
}
