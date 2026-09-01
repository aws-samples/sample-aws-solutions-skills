/* db-migration-agent progress dashboard — plain polling renderer.
   No build step, no framework. Reads status.json (full snapshot, agent overwrites it)
   and activity-log.jsonl (append-only, agent appends one line per event) from the same
   directory as this page. Never computes cutover_ready itself — that boolean is decided
   by the agent, from the skill's own gates; this file only renders what it's told. */
(() => {
  const POLL_MS = 5000;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let lastLogCount = 0;
  let lastUpdatedAt = null;

  const PHASE_BADGE = { done: '완료', in_progress: '진행중', pending: '대기', blocked: '중단' };
  const ENTRY_ICON  = { success: '✓', in_progress: '…', blocked: '!' };

  function fmtTime(iso) {
    if (!iso) return '';
    try { return iso.replace('T', ' ').replace(/\+.*$/, '').slice(0, 19); } catch { return iso; }
  }

  function renderTiles(s) {
    $('#pct').textContent = s.overall_progress_pct ?? 0;
    $('#pct-bar').style.width = `${s.overall_progress_pct ?? 0}%`;
    const done = (s.phases || []).filter(p => p.status === 'done').length;
    $('#phase-count').textContent = `${done} / ${(s.phases || []).length}`;
    $('#phase-remaining').textContent = `${(s.phases || []).length - done} remaining`;
    $('#current-activity').textContent = s.current_activity || '—';
    $('#current-phase-chip').textContent = s.current_phase ? `Phase ${s.current_phase}` : '';
  }

  function renderCutover(s) {
    const gates = s.cutover_gates || [];
    const ready = !!s.cutover_ready;
    const unmet = gates.filter(g => !g.met).length;
    const box = $('#cutover');
    box.className = 'cutover ' + (ready ? 'ready' : 'notready');
    $('#verdict-text').textContent = ready ? '컷오버 가능' : `아직 컷오버 불가 — ${unmet}개 게이트 미충족`;
    $('#verdict-sub').textContent = ready
      ? '아래 게이트가 모두 충족되었습니다. 컷오버 시점은 여전히 고객이 결정합니다.'
      : '컷오버는 아래 게이트가 전부 충족될 때까지 열리지 않습니다 — 진행률과는 별개의 판단입니다.';
    $('#gates').innerHTML = gates.map(g => `
      <div class="gate ${g.met ? 'met' : 'unmet'}">
        <span class="icon">${g.met ? '✓' : '·'}</span>
        <div><div class="label">${esc(g.label)}</div><div class="detail">${esc(g.detail || '')}</div></div>
      </div>`).join('') || '<div class="gate unmet"><span class="icon">·</span><div class="label">게이트 정보 없음</div></div>';
  }

  function renderPhases(s) {
    $('#phases').innerHTML = (s.phases || []).map(p => {
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      return `<div class="phase">
        <div class="phase-row">
          <span class="badge ${p.status}">${PHASE_BADGE[p.status] || p.status}</span>
          <span class="phase-name">Phase ${esc(p.id)} · ${esc(p.name)}</span>
          <span class="phase-count">${p.done}/${p.total} · ${pct}%</span>
        </div>
        <div class="pbar"><i style="width:${pct}%"></i></div>
        ${p.note ? `<div class="phase-count" style="margin-top:6px">${esc(p.note)}</div>` : ''}
      </div>`;
    }).join('');
  }

  const OBJ_TYPE_LABEL = { tables: 'Tables', views: 'Views', procedures: 'Procedures',
    functions: 'Functions', triggers: 'Triggers', events: 'Events' };
  const OBJ_STATUS_BADGE = { pending: '대기', loading: '적재중', loaded: '적재완료',
    validated: '검증완료', created: '생성완료', deferred: '컷오버 시 생성' };
  const fmtNum = (n) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString();

  function renderTableRows(items) {
    return items.map(it => {
      const mismatch = it.checksum_match === false;
      const cs = it.checksum_match === true ? '<span class="cs-ok">✓ 일치</span>'
        : it.checksum_match === false ? '<span class="cs-bad">✗ 불일치</span>'
        : '<span class="cs-pending">—</span>';
      return `<tr class="${mismatch ? 'row-mismatch' : ''}">
        <td class="mono">${esc(it.name)}</td>
        <td class="num mono">${fmtNum(it.rows_source)}</td>
        <td class="num mono">${fmtNum(it.rows_target)}</td>
        <td>${cs}</td>
        <td><span class="obj-badge ${esc(it.status)}">${OBJ_STATUS_BADGE[it.status] || esc(it.status)}</span></td>
      </tr>`;
    }).join('');
  }

  function renderObjectCard(typeKey, o) {
    const label = OBJ_TYPE_LABEL[typeKey] || typeKey;
    const doneCount = (o.validated ?? o.created ?? o.loaded ?? 0);
    if (typeKey === 'tables') {
      return `<div class="obj-card obj-card-wide">
        <div class="obj-card-head"><span class="obj-title">${label}</span>
          <span class="obj-count">${doneCount}/${o.total} validated</span></div>
        <table class="obj-table">
          <thead><tr><th>Table</th><th>Source rows</th><th>Target rows</th><th>Checksum</th><th>Status</th></tr></thead>
          <tbody>${renderTableRows(o.items || [])}</tbody>
        </table>
      </div>`;
    }
    const items = (o.items || []).map(it => {
      const name = typeof it === 'string' ? it : it.name;
      const status = typeof it === 'string' ? 'created' : it.status;
      const note = typeof it === 'string' ? '' : (it.note || '');
      return `<div class="obj-item">
        <span class="obj-badge ${esc(status)}">${OBJ_STATUS_BADGE[status] || esc(status)}</span>
        <span class="mono">${esc(name)}</span>
        ${note ? `<span class="obj-note">${esc(note)}</span>` : ''}
      </div>`;
    }).join('') || '<div class="obj-item obj-empty">—</div>';
    return `<div class="obj-card">
      <div class="obj-card-head"><span class="obj-title">${label}</span>
        <span class="obj-count">${doneCount}/${o.total}</span></div>
      <div class="obj-items">${items}</div>
    </div>`;
  }

  function renderObjects(s) {
    const mo = s.migration_objects;
    const box = $('#objects');
    if (!mo || !Object.keys(mo).length) { box.innerHTML = '<div id="log-empty">아직 스키마 객체 인벤토리가 없습니다 (Phase 2 이후 채워집니다).</div>'; return; }
    const order = ['tables', 'views', 'procedures', 'functions', 'triggers', 'events'];
    const html = order.filter(k => mo[k] && mo[k].total > 0).map(k => renderObjectCard(k, mo[k])).join('');
    box.innerHTML = html || '<div id="log-empty">이 소스에는 테이블 외 스키마 객체가 없습니다.</div>';
  }

  function renderLog(lines) {
    if (!lines.length) { $('#log').innerHTML = '<div id="log-empty">아직 기록된 활동이 없습니다.</div>'; return; }
    const rows = lines.slice().reverse().map(e => `
      <div class="entry ${e.result || 'success'}">
        <span class="icon">${ENTRY_ICON[e.result] || '✓'}</span>
        <div class="body">
          <div class="title">${esc(e.title)} <span class="time">${fmtTime(e.time)}</span></div>
          <div class="action">${esc(e.action || '')}</div>
          ${e.detail ? `<div class="detail">${esc(e.detail)}</div>` : ''}
          ${e.files && e.files.length ? `<div class="files">${e.files.map(esc).join(' · ')}</div>` : ''}
        </div>
      </div>`).join('');
    $('#log').innerHTML = rows;
  }

  async function fetchJSON(url) {
    const r = await fetch(`${url}?_=${Date.now()}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  async function fetchJSONL(url) {
    const r = await fetch(`${url}?_=${Date.now()}`, { cache: 'no-store' });
    if (r.status === 404) return [];   // not yet created / not yet appended to — not an error
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    const text = await r.text();
    return text.split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  }

  async function tick() {
    try {
      const [status, log] = await Promise.all([fetchJSON('status.json'), fetchJSONL('activity-log.jsonl')]);
      renderTiles(status);
      renderCutover(status);
      renderPhases(status);
      renderObjects(status);
      if (log.length !== lastLogCount) { renderLog(log); lastLogCount = log.length; }
      $('#updated-text').innerHTML = `updated <b>${fmtTime(status.updated_at)}</b>`;
      const stale = status.updated_at && (Date.now() - new Date(status.updated_at).getTime() > 15 * 60 * 1000);
      $('#stale-badge').style.display = stale ? 'inline' : 'none';
      lastUpdatedAt = status.updated_at;
      $('#conn-error').style.display = 'none';
    } catch (err) {
      $('#conn-error').style.display = 'block';
      $('#conn-error').textContent = `status.json / activity-log.jsonl을 읽을 수 없습니다 — ${err.message}`;
    }
  }

  tick();
  setInterval(tick, POLL_MS);
})();
