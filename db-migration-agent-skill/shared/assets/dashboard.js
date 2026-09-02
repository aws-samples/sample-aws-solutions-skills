/* db-migration-agent progress dashboard — plain polling renderer.
   No build step, no framework. Reads status.json (full snapshot, agent overwrites it)
   and activity-log.jsonl (append-only, agent appends one line per event) from the same
   directory as this page. Never computes cutover_ready itself — that boolean is decided
   by the agent, from the skill's own gates; this file only renders what it's told.

   i18n: status.json's `lang` field ("en"/"ko"/...) picks the label set for this page's
   own static UI chrome (headers, badges, table columns, empty/error states) — separate
   from phase names/gate labels/activity text, which the agent already writes in the
   right language itself. Defaults to "en" if `lang` is absent. */
(() => {
  const POLL_MS = 5000;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let lastLogCount = 0;
  let appliedLang = null;

  const LABELS = {
    en: {
      pageTitle: 'Migration Progress',
      eyebrow: (mode) => `DB Migration · Mode ${mode}`,
      h1: (prefix) => `${prefix} — Migration Progress`,
      staleBadge: 'no update in 15+ min',
      overallProgress: 'Overall Progress',
      phasesTile: 'Phases',
      phasesRemaining: (n) => `${n} remaining`,
      nowRunning: 'Now Running',
      verdictReady: 'Cutover ready',
      verdictNotReady: (unmet) => `Not cutover-ready — ${unmet} gate(s) unmet`,
      verdictSubReady: 'All gates below are met. The customer still decides when to actually cut over.',
      verdictSubNotReady: 'Cutover stays locked until every gate below is met — independent of the progress %.',
      noGateData: 'No gate data',
      phasesH2: 'Phases',
      objectsH2: 'Migration Objects',
      objectsSub: 'tables/views/procedures/triggers/events — row counts & checksum status',
      logH2: 'Activity Log',
      logSub: 'dashboard/activity-log.jsonl · newest first',
      logEmpty: 'No activity recorded yet.',
      objectsEmpty: 'No schema-object inventory yet (populated after Phase 2).',
      objectsNone: 'This source has no schema objects beyond tables.',
      footer: 'db-migration-agent · local only · auto-refreshes every 5s · nothing leaves this machine',
      updatedPrefix: 'updated',
      connError: (msg) => `Can't read status.json / activity-log.jsonl — ${msg}`,
      checksumMatch: '✓ match',
      checksumMismatch: '✗ mismatch',
      objTableCol: { name: 'Table', src: 'Source rows', tgt: 'Target rows', cs: 'Checksum', status: 'Status' },
      objTypeLabel: { tables: 'Tables', views: 'Views', procedures: 'Procedures', functions: 'Functions', triggers: 'Triggers', events: 'Events' },
      objStatusBadge: { pending: 'pending', loading: 'loading', loaded: 'loaded', validated: 'validated', created: 'created', deferred: 'deferred to cutover' },
      phaseBadge: { done: 'done', in_progress: 'in progress', pending: 'pending', blocked: 'blocked' },
      objectsCountSuffix: 'validated',
      soakH2: 'Parallel-Run Soak',
      soakSub: 'observation window before cutover is recommended',
      soakExplain: (n) => `The old database stays live and authoritative; the new one just receives replicated changes in the background — nothing is at risk yet. We watch it for ${n} consecutive green day(s) before recommending cutover, because a one-time check can only see what's true right now — it can't catch something like a job that only runs at 2am, which a snapshot check would simply never observe.`,
      soakCounterOf: (n) => `/ ${n} consecutive green`,
      soakDayLabel: (n) => `Day ${n}`,
      soakReviewBanner: (n) => `Day ${n} needs review — ask the agent to look at this before continuing`,
      soakOverdueBanner: (hrs) => `No soak check in ${hrs}h — the scheduled run may have been missed (host down, cron didn't fire, script crashed). Verify it's still running.`,
      soakWaived: (reason) => `Soak waived${reason ? ' — ' + reason : ''}.`,
      soakEmpty: 'Not started yet — begins once the target is current and validation is green.',
      soakCheckLabel: { row_count: 'Row count', checksum: 'Checksum', alarms: 'Alarms', headroom: 'Headroom', schema_drift: 'Schema drift', replication_lag: 'Replication lag', customer_test_suite: 'Customer test suite' },
      soakCheckPass: '✓', soakCheckFail: '✗', soakCheckUnknown: '?',
    },
    ko: {
      pageTitle: '마이그레이션 진행 상태',
      eyebrow: (mode) => `DB 마이그레이션 · Mode ${mode}`,
      h1: (prefix) => `${prefix} 마이그레이션 진행 상태`,
      staleBadge: '15분 이상 갱신 없음',
      overallProgress: '전체 진행률',
      phasesTile: '단계',
      phasesRemaining: (n) => `${n}개 남음`,
      nowRunning: '진행 중',
      verdictReady: '컷오버 가능',
      verdictNotReady: (unmet) => `아직 컷오버 불가 — ${unmet}개 게이트 미충족`,
      verdictSubReady: '아래 게이트가 모두 충족되었습니다. 컷오버 시점은 여전히 고객이 결정합니다.',
      verdictSubNotReady: '컷오버는 아래 게이트가 전부 충족될 때까지 열리지 않습니다 — 진행률과는 별개의 판단입니다.',
      noGateData: '게이트 정보 없음',
      phasesH2: '단계',
      objectsH2: '마이그레이션 객체',
      objectsSub: '테이블/뷰/프로시저/트리거/이벤트 — 행 수 및 체크섬 상태',
      logH2: '활동 로그',
      logSub: 'dashboard/activity-log.jsonl · 최신순',
      logEmpty: '아직 기록된 활동이 없습니다.',
      objectsEmpty: '아직 스키마 객체 인벤토리가 없습니다 (Phase 2 이후 채워집니다).',
      objectsNone: '이 소스에는 테이블 외 스키마 객체가 없습니다.',
      footer: 'db-migration-agent · 로컬 전용 · 5초마다 자동 갱신 · 외부로 전송되지 않습니다',
      updatedPrefix: '업데이트',
      connError: (msg) => `status.json / activity-log.jsonl을 읽을 수 없습니다 — ${msg}`,
      checksumMatch: '✓ 일치',
      checksumMismatch: '✗ 불일치',
      objTableCol: { name: '테이블', src: '소스 행 수', tgt: '타깃 행 수', cs: '체크섬', status: '상태' },
      objTypeLabel: { tables: '테이블', views: '뷰', procedures: '프로시저', functions: '함수', triggers: '트리거', events: '이벤트' },
      objStatusBadge: { pending: '대기', loading: '적재중', loaded: '적재완료', validated: '검증완료', created: '생성완료', deferred: '컷오버 시 생성' },
      phaseBadge: { done: '완료', in_progress: '진행중', pending: '대기', blocked: '중단' },
      objectsCountSuffix: '검증완료',
      soakH2: '병행 가동 (Soak)',
      soakSub: '컷오버 권고 전 관찰 기간',
      soakExplain: (n) => `기존 DB는 그대로 라이브 상태를 유지하고, 새 DB는 백그라운드로 변경분만 복제받습니다 — 아직 아무 위험도 없습니다. 컷오버를 권고하기 전까지 ${n}일 연속 green을 확인합니다. 한 번의 점검으로는 "지금 이 순간" 사실만 알 수 있고, 새벽 2시에만 실행되는 작업처럼 스냅샷 점검으로는 절대 볼 수 없는 것들이 있기 때문입니다.`,
      soakCounterOf: (n) => `/ ${n}일 연속 green`,
      soakDayLabel: (n) => `${n}일차`,
      soakReviewBanner: (n) => `${n}일차 확인 필요 — 계속하기 전에 에이전트에게 검토를 요청하세요`,
      soakOverdueBanner: (hrs) => `${hrs}시간 동안 소크 점검이 실행되지 않았습니다 — 예약된 실행이 누락되었을 수 있습니다 (호스트 다운, cron 미실행, 스크립트 오류). 정상 동작 중인지 확인하세요.`,
      soakWaived: (reason) => `병행 가동 생략됨${reason ? ' — ' + reason : ''}.`,
      soakEmpty: '아직 시작되지 않았습니다 — 타깃이 최신 상태이고 검증이 green이 되면 시작됩니다.',
      soakCheckLabel: { row_count: '행 수', checksum: '체크섬', alarms: '알람', headroom: '여유 용량', schema_drift: '스키마 변경', replication_lag: '복제 지연', customer_test_suite: '고객 테스트' },
      soakCheckPass: '✓', soakCheckFail: '✗', soakCheckUnknown: '?',
    },
  };

  const ENTRY_ICON = { success: '✓', in_progress: '…', blocked: '!' };
  const fmtNum = (n) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString();

  function fmtTime(iso) {
    if (!iso) return '';
    try { return iso.replace('T', ' ').replace(/\+.*$/, '').slice(0, 19); } catch { return iso; }
  }

  function L() { return LABELS[appliedLang] || LABELS.en; }

  function applyStaticLabels(s) {
    const lang = (s.lang && LABELS[s.lang]) ? s.lang : 'en';
    if (lang === appliedLang) return;
    appliedLang = lang;
    const l = L();
    document.documentElement.lang = lang;
    document.title = `${s.engagement || ''} — ${l.pageTitle}`.replace(/^ — /, '');
    $('#eyebrow').textContent = l.eyebrow(s.mode ?? '');
    $('#page-h1').textContent = l.h1(s.engagement || '');
    $('#stale-badge').textContent = l.staleBadge;
    $('#lbl-overall-progress').textContent = l.overallProgress;
    $('#lbl-phases-tile').textContent = l.phasesTile;
    $('#lbl-now-running').textContent = l.nowRunning;
    $('#lbl-phases-h2').textContent = l.phasesH2;
    $('#lbl-objects-h2').innerHTML = `${esc(l.objectsH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.objectsSub)}</span>`;
    $('#lbl-soak-h2').innerHTML = `${esc(l.soakH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.soakSub)}</span>`;
    $('#lbl-log-h2').innerHTML = `${esc(l.logH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.logSub)}</span>`;
    $('#footer-text').textContent = l.footer;
  }

  function renderTiles(s) {
    const l = L();
    $('#pct').textContent = s.overall_progress_pct ?? 0;
    $('#pct-bar').style.width = `${s.overall_progress_pct ?? 0}%`;
    const done = (s.phases || []).filter(p => p.status === 'done').length;
    $('#phase-count').textContent = `${done} / ${(s.phases || []).length}`;
    $('#phase-remaining').textContent = l.phasesRemaining((s.phases || []).length - done);
    $('#current-activity').textContent = s.current_activity || '—';
    $('#current-phase-chip').textContent = s.current_phase ? `Phase ${s.current_phase}` : '';
  }

  function renderCutover(s) {
    const l = L();
    const gates = s.cutover_gates || [];
    const ready = !!s.cutover_ready;
    const unmet = gates.filter(g => !g.met).length;
    const box = $('#cutover');
    box.className = 'cutover ' + (ready ? 'ready' : 'notready');
    $('#verdict-text').textContent = ready ? l.verdictReady : l.verdictNotReady(unmet);
    $('#verdict-sub').textContent = ready ? l.verdictSubReady : l.verdictSubNotReady;
    $('#gates').innerHTML = gates.map(g => `
      <div class="gate ${g.met ? 'met' : 'unmet'}">
        <span class="icon">${g.met ? '✓' : '·'}</span>
        <div><div class="label">${esc(g.label)}</div><div class="detail">${esc(g.detail || '')}</div></div>
      </div>`).join('') || `<div class="gate unmet"><span class="icon">·</span><div class="label">${esc(l.noGateData)}</div></div>`;
  }

  function renderPhases(s) {
    const l = L();
    $('#phases').innerHTML = (s.phases || []).map(p => {
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      return `<div class="phase">
        <div class="phase-row">
          <span class="badge ${p.status}">${l.phaseBadge[p.status] || p.status}</span>
          <span class="phase-name">Phase ${esc(p.id)} · ${esc(p.name)}</span>
          <span class="phase-count">${p.done}/${p.total} · ${pct}%</span>
        </div>
        <div class="pbar"><i style="width:${pct}%"></i></div>
        ${p.note ? `<div class="phase-count" style="margin-top:6px">${esc(p.note)}</div>` : ''}
      </div>`;
    }).join('');
  }

  function renderTableRows(items) {
    const l = L();
    return items.map(it => {
      const mismatch = it.checksum_match === false;
      const cs = it.checksum_match === true ? `<span class="cs-ok">${esc(l.checksumMatch)}</span>`
        : it.checksum_match === false ? `<span class="cs-bad">${esc(l.checksumMismatch)}</span>`
        : '<span class="cs-pending">—</span>';
      return `<tr class="${mismatch ? 'row-mismatch' : ''}">
        <td class="mono">${esc(it.name)}</td>
        <td class="num mono">${fmtNum(it.rows_source)}</td>
        <td class="num mono">${fmtNum(it.rows_target)}</td>
        <td>${cs}</td>
        <td><span class="obj-badge ${esc(it.status)}">${l.objStatusBadge[it.status] || esc(it.status)}</span></td>
      </tr>`;
    }).join('');
  }

  function renderObjectCard(typeKey, o) {
    const l = L();
    const label = l.objTypeLabel[typeKey] || typeKey;
    const doneCount = (o.validated ?? o.created ?? o.loaded ?? 0);
    if (typeKey === 'tables') {
      return `<div class="obj-card obj-card-wide">
        <div class="obj-card-head"><span class="obj-title">${esc(label)}</span>
          <span class="obj-count">${doneCount}/${o.total} ${esc(l.objectsCountSuffix)}</span></div>
        <table class="obj-table">
          <thead><tr><th>${esc(l.objTableCol.name)}</th><th>${esc(l.objTableCol.src)}</th><th>${esc(l.objTableCol.tgt)}</th><th>${esc(l.objTableCol.cs)}</th><th>${esc(l.objTableCol.status)}</th></tr></thead>
          <tbody>${renderTableRows(o.items || [])}</tbody>
        </table>
      </div>`;
    }
    const items = (o.items || []).map(it => {
      const name = typeof it === 'string' ? it : it.name;
      const status = typeof it === 'string' ? 'created' : it.status;
      const note = typeof it === 'string' ? '' : (it.note || '');
      return `<div class="obj-item">
        <span class="obj-badge ${esc(status)}">${l.objStatusBadge[status] || esc(status)}</span>
        <span class="mono">${esc(name)}</span>
        ${note ? `<span class="obj-note">${esc(note)}</span>` : ''}
      </div>`;
    }).join('') || '<div class="obj-item obj-empty">—</div>';
    return `<div class="obj-card">
      <div class="obj-card-head"><span class="obj-title">${esc(label)}</span>
        <span class="obj-count">${doneCount}/${o.total}</span></div>
      <div class="obj-items">${items}</div>
    </div>`;
  }

  function renderObjects(s) {
    const l = L();
    const mo = s.migration_objects;
    const box = $('#objects');
    if (!mo || !Object.keys(mo).length) { box.innerHTML = `<div id="log-empty">${esc(l.objectsEmpty)}</div>`; return; }
    const order = ['tables', 'views', 'procedures', 'functions', 'triggers', 'events'];
    const html = order.filter(k => mo[k] && mo[k].total > 0).map(k => renderObjectCard(k, mo[k])).join('');
    box.innerHTML = html || `<div id="log-empty">${esc(l.objectsNone)}</div>`;
  }

  function renderSoak(s) {
    const l = L();
    const box = $('#soak');
    const soak = s.soak;
    if (!soak || soak.waived) {
      box.innerHTML = soak && soak.waived
        ? `<div class="soak-waived">${esc(l.soakWaived(soak.waived_reason))}</div>`
        : `<div id="soak-empty">${esc(l.soakEmpty)}</div>`;
      return;
    }
    const nTotal = soak.n_total || 0;
    const days = soak.days || [];
    const consecutive = soak.consecutive_green || 0;
    const needsReview = days.some(d => d.needs_agent_review);
    const lastReviewDay = days.map((d, i) => ({ d, i })).filter(x => x.d.needs_agent_review).pop();
    // Distinct from the 15-min chat-staleness badge, which assumes an active session —
    // a soak check runs roughly daily, so "overdue" means missing a run, not missing
    // a few minutes. 36h gives one day's cadence a buffer before flagging.
    const overdueHrs = soak.state === 'active' && soak.last_checked_at
      ? (Date.now() - new Date(soak.last_checked_at).getTime()) / 3600000 : 0;
    const isOverdue = overdueHrs > 36;

    const dayCells = days.map((d, i) => {
      const cls = d.overall === 'green' ? 'green' : 'red';
      const checksHtml = Object.entries(d.checks || {}).map(([k, v]) => {
        const mark = v === true ? l.soakCheckPass : v === false ? l.soakCheckFail : l.soakCheckUnknown;
        return `<div><span>${esc(l.soakCheckLabel[k] || k)}</span><span>${mark}</span></div>`;
      }).join('');
      return `<div class="soak-day ${cls}">${i + 1}
        <div class="soak-day-detail"><b>${esc(l.soakDayLabel(i + 1))} · ${esc(d.date || '')}</b>${checksHtml}</div>
      </div>`;
    }).join('') + Array.from({ length: Math.max(0, nTotal - days.length) }).map(() =>
      `<div class="soak-day pending">·</div>`).join('');

    box.innerHTML = `
      <div class="soak-explain">${esc(l.soakExplain(nTotal))}</div>
      ${isOverdue ? `<div class="soak-review-banner">${esc(l.soakOverdueBanner(Math.floor(overdueHrs)))}</div>` : ''}
      ${needsReview ? `<div class="soak-review-banner">${esc(l.soakReviewBanner(lastReviewDay.i + 1))}</div>` : ''}
      <div class="soak-counter"><span class="n">${consecutive}</span><span class="of">${esc(l.soakCounterOf(nTotal))}</span></div>
      <div class="soak-days">${dayCells}</div>`;
  }

  function renderLog(lines) {
    const l = L();
    if (!lines.length) { $('#log').innerHTML = `<div id="log-empty">${esc(l.logEmpty)}</div>`; return; }
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
      applyStaticLabels(status);
      renderTiles(status);
      renderCutover(status);
      renderPhases(status);
      renderObjects(status);
      renderSoak(status);
      if (log.length !== lastLogCount) { renderLog(log); lastLogCount = log.length; }
      $('#updated-text').innerHTML = `${esc(L().updatedPrefix)} <b>${fmtTime(status.updated_at)}</b>`;
      const stale = status.updated_at && (Date.now() - new Date(status.updated_at).getTime() > 15 * 60 * 1000);
      $('#stale-badge').style.display = stale ? 'inline' : 'none';
      $('#conn-error').style.display = 'none';
    } catch (err) {
      $('#conn-error').style.display = 'block';
      $('#conn-error').textContent = L().connError(err.message);
    }
  }

  tick();
  setInterval(tick, POLL_MS);
})();
