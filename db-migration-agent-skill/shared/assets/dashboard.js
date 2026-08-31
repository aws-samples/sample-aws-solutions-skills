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
