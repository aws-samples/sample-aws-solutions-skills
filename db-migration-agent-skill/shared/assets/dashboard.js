/* db-migration-agent progress dashboard — plain polling renderer.
   No build step, no framework. Reads status.json (full snapshot, agent/Lambda overwrites
   it) and activity-log.jsonl (append-only, one line per event) — normally from the same
   directory as this page (local dev), or from two absolute presigned S3 URLs during a
   Phase 7.7 soak window (see window.DASHBOARD_STATUS_URL/DASHBOARD_LOG_URL below and
   shared/scripts/generate_presigned_urls.py). Never computes cutover_ready itself — that
   boolean is decided by the agent, from the skill's own gates; this file only renders
   what it's told.

   i18n: status.json's `lang` field ("en"/"ko"/...) picks the label set for this page's
   own static UI chrome (headers, badges, table columns, empty/error states) — separate
   from phase names/gate labels/activity text, which the agent already writes in the
   right language itself. Defaults to "en" if `lang` is absent. */
(() => {
  const POLL_MS = 5000;
  // Local dev (python3 -m http.server, relative paths) vs soak-window S3 hosting (absolute
  // presigned URLs) — shared/scripts/generate_presigned_urls.py injects these two globals
  // into index.html right before this script tag when it materializes the presigned copy;
  // when absent (local dev, or before soak start), fall back to the plain relative paths
  // this page has always used. See execution-runbooks.md §Soak automation.
  const STATUS_URL = (typeof window !== 'undefined' && window.DASHBOARD_STATUS_URL) || 'status.json';
  const LOG_URL = (typeof window !== 'undefined' && window.DASHBOARD_LOG_URL) || 'activity-log.jsonl';
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let lastLogContent = null;
  let appliedLang = null;
  let polling = false;
  const rendered = new WeakMap();

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
      logSub: 'Historical events · newest first; later entries may supersede earlier findings',
      logEmpty: 'No activity recorded yet.',
      objectsEmpty: 'No schema-object inventory yet (populated after Phase 2).',
      objectsNone: 'This source has no schema objects beyond tables.',
      footer: 'db-migration-agent · single-user, no login · auto-refreshes every 5s',
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
      soakExplain: (n) => `Production remains on the source while the target receives replicated changes. We require ${n} consecutive green day(s) plus soak-exit acceptance. Scheduled samples alone cannot rule out an overnight incident; full-period evidence must also be reviewed.`,
      soakCounterOf: (n) => `/ ${n} consecutive green`,
      soakDayLabel: (n) => `Day ${n}`,
      soakReviewBanner: (n) => `Day ${n} needs review — ask the agent to look at this before continuing`,
      soakOverdueBanner: (hrs) => `No soak check in ${hrs}h — the scheduled run may have been missed (host down, cron didn't fire, script crashed). Verify it's still running.`,
      soakWaived: (reason) => `Soak waived${reason ? ' — ' + reason : ''}.`,
      soakEmpty: 'Not started yet — begins once the target is current and validation is green.',
      soakCheckLabel: { row_count: 'Row count', checksum: 'Checksum', alarms: 'Alarms', headroom: 'Headroom', schema_drift: 'Schema drift', replication_lag: 'Replication lag', replication_errors: 'Replication errors', customer_test_suite: 'Customer test suite', period_evidence: 'Full-period evidence' },
      soakCheckPass: '✓', soakCheckFail: '✗', soakCheckUnknown: '?', soakCheckNotApplicable: '–',
      actionsH2: 'What needs your attention',
      actionsUnknown: 'Customer actions have not been recorded in this snapshot.',
      actionsEmpty: 'No pending customer actions recorded.',
      actionHelp: 'Reply in the engagement chat with the action ID. This page records no approvals.',
      actionResolved: 'Resolved actions',
      actionStatus: { pending: 'Awaiting your reply', resolved: 'Resolved' },
      owner: 'Owner', due: 'Needed by', why: 'Why it matters', request: 'Your next step',
      resolution: 'Resolution', evidence: 'Evidence references', next: 'Next step',
      outlookH2: 'Cost, timing & approach',
      costTitle: 'Cost estimate', costEmpty: 'Cost estimate not recorded yet — prepared at GATE 2.',
      monthly: 'Monthly steady state', oneTime: 'One-time migration',
      costScope: { complete: 'Full estimate', partial: 'Partial estimate — exclusions remain' },
      scopeUnknown: 'Estimate scope not recorded', asOf: 'Estimated as of',
      item: 'Item', cadence: 'Period', amount: 'Estimate', basis: 'Basis',
      assumptions: 'Assumptions & exclusions', breakdown: 'Cost breakdown',
      timelineTitle: 'Timing & next milestone', timelineEmpty: 'Timing forecast not recorded yet.',
      downtime: 'Expected write pause', budget: 'Downtime budget', completion: 'Estimated completion',
      window: 'Customer cutover window', milestone: 'Next milestone',
      timingBasis: { estimated: 'Estimated', measured: 'Measured', mixed: 'Measured + estimated' },
      timingUnknown: 'Timing basis not recorded', notRecorded: 'Not recorded',
      strategyTitle: 'Migration approach', source: 'Source', target: 'Target', method: 'Method',
      rationale: 'Why this approach', rollback: 'Rollback path', tradeoffs: 'Trade-offs & alternatives',
      risksH2: 'Risks & assumptions', risksUnknown: 'Risk register not recorded in this snapshot.',
      risksEmpty: 'No risks recorded after review.', closedRisks: 'Closed risks',
      riskStatus: { open: 'Open', mitigating: 'Mitigating', accepted: 'Accepted', closed: 'Closed' },
      severity: { high: 'High', medium: 'Medium', low: 'Low' }, severityUnknown: 'Severity not recorded',
      riskCounts: (active, accepted, closed) => `${active} open / mitigating · ${accepted} accepted · ${closed} closed`,
      mitigation: 'Mitigation / verification', impact: 'Customer impact',
      phaseDetails: 'Findings, work & evidence', findings: 'What we learned', steps: 'Work items',
      recentHistory: 'Recent phase history', historyHelp: 'Historical evidence; see the activity log for later corrections.',
      gateItems: 'Requirement detail', met: 'Met', unmet: 'Unmet',
      historyLatest: 'Latest recorded event', phasePrefix: 'Phase',
      soakMissingTime: 'Soak is active but its last check / start time is missing or invalid.',
      soakLastCheck: 'Last soak check', trendTitle: 'Sample history',
      trendHelp: 'Lag: DMS maximum over the preceding 15 minutes, or a native MySQL sample. Headroom: preceding 30-minute average free storage %. These are not daily extrema. Gaps and mechanism changes break the line.',
      lagMetric: 'Replication lag (seconds)', headroomMetric: 'Storage headroom (%)',
      latestSample: 'Latest recorded day', delta: 'Change from previous day',
      trendEmpty: 'No numeric measurements recorded.', trendSingle: 'At least two comparable days are needed for a trend.',
      sampleValues: 'Dates & measured values', date: 'Date (UTC)', seconds: 's', percentagePoints: 'percentage points',
      checkStates: { pass: 'Pass', fail: 'Fail', unknown: 'Needs review', na: 'Not applicable' },
      dayVerdict: { green: 'GREEN', red: 'RED' }, dayUnknown: 'Verdict not recorded',
      check: 'Check', result: 'Result', sampleDetail: 'Recorded check evidence',
      pendingDays: (n) => `${n} observation day(s) not yet recorded`,
      objectRecorded: 'recorded complete (legacy count)', objectLoaded: 'loaded',
      currencyUnknown: 'Currency not recorded',
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
      logSub: '과거 활동 · 최신순; 이후 기록에서 앞선 발견이 정정될 수 있습니다',
      logEmpty: '아직 기록된 활동이 없습니다.',
      objectsEmpty: '아직 스키마 객체 인벤토리가 없습니다 (Phase 2 이후 채워집니다).',
      objectsNone: '이 소스에는 테이블 외 스키마 객체가 없습니다.',
      footer: 'db-migration-agent · 단일 사용자, 로그인 없음 · 5초마다 자동 갱신',
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
      soakExplain: (n) => `운영은 소스에서 계속하고 타깃은 변경분을 복제받습니다. ${n}일 연속 green과 병행 가동 종료 승인이 필요합니다. 예약된 표본 점검만으로는 야간 장애를 배제할 수 없으므로 전체 기간의 증빙도 검토해야 합니다.`,
      soakCounterOf: (n) => `/ ${n}일 연속 green`,
      soakDayLabel: (n) => `${n}일차`,
      soakReviewBanner: (n) => `${n}일차 확인 필요 — 계속하기 전에 에이전트에게 검토를 요청하세요`,
      soakOverdueBanner: (hrs) => `${hrs}시간 동안 소크 점검이 실행되지 않았습니다 — 예약된 실행이 누락되었을 수 있습니다 (호스트 다운, cron 미실행, 스크립트 오류). 정상 동작 중인지 확인하세요.`,
      soakWaived: (reason) => `병행 가동 생략됨${reason ? ' — ' + reason : ''}.`,
      soakEmpty: '아직 시작되지 않았습니다 — 타깃이 최신 상태이고 검증이 green이 되면 시작됩니다.',
      soakCheckLabel: { row_count: '행 수', checksum: '체크섬', alarms: '알람', headroom: '여유 용량', schema_drift: '스키마 변경', replication_lag: '복제 지연', replication_errors: '복제 오류', customer_test_suite: '고객 테스트', period_evidence: '전체 기간 증빙' },
      soakCheckPass: '✓', soakCheckFail: '✗', soakCheckUnknown: '?', soakCheckNotApplicable: '–',
      actionsH2: '지금 고객 확인이 필요한 사항',
      actionsUnknown: '이 스냅샷에는 고객 요청 사항이 아직 기록되지 않았습니다.',
      actionsEmpty: '기록된 미해결 고객 요청이 없습니다.',
      actionHelp: '엔게이지먼트 채팅에서 요청 ID와 함께 답변해 주세요. 이 페이지에서는 승인을 기록하지 않습니다.',
      actionResolved: '해결된 요청',
      actionStatus: { pending: '고객 답변 대기', resolved: '해결됨' },
      owner: '담당', due: '필요 시점', why: '필요한 이유', request: '고객의 다음 조치',
      resolution: '해결 내용', evidence: '증빙 참조', next: '다음 단계',
      outlookH2: '비용·일정·이전 방식',
      costTitle: '비용 추정', costEmpty: '비용 추정이 아직 기록되지 않았습니다 — GATE 2에서 작성합니다.',
      monthly: '월 정상 운영 비용', oneTime: '일회성 이전 비용',
      costScope: { complete: '전체 추정', partial: '부분 추정 — 제외 항목 있음' },
      scopeUnknown: '추정 범위 미기록', asOf: '추정 기준일',
      item: '항목', cadence: '기간', amount: '추정액', basis: '산정 근거',
      assumptions: '가정 및 제외 항목', breakdown: '비용 상세',
      timelineTitle: '일정 및 다음 주요 단계', timelineEmpty: '일정 예측이 아직 기록되지 않았습니다.',
      downtime: '예상 쓰기 중단', budget: '허용 다운타임', completion: '예상 완료 시점',
      window: '고객 컷오버 창', milestone: '다음 주요 단계',
      timingBasis: { estimated: '추정', measured: '실측', mixed: '실측 + 추정' },
      timingUnknown: '시간 산정 기준 미기록', notRecorded: '미기록',
      strategyTitle: '이전 방식', source: '소스', target: '타깃', method: '방식',
      rationale: '이 방식을 택한 이유', rollback: '롤백 경로', tradeoffs: '트레이드오프 및 대안',
      risksH2: '위험 및 가정', risksUnknown: '이 스냅샷에는 위험 목록이 기록되지 않았습니다.',
      risksEmpty: '검토 후 기록된 위험이 없습니다.', closedRisks: '종료된 위험',
      riskStatus: { open: '열림', mitigating: '완화 진행 중', accepted: '수용됨', closed: '종료됨' },
      severity: { high: '높음', medium: '중간', low: '낮음' }, severityUnknown: '심각도 미기록',
      riskCounts: (active, accepted, closed) => `열림 / 완화 중 ${active}건 · 수용 ${accepted}건 · 종료 ${closed}건`,
      mitigation: '완화 / 검증', impact: '고객 영향',
      phaseDetails: '발견·작업·증빙', findings: '확인한 사실', steps: '작업 항목',
      recentHistory: '최근 단계 활동', historyHelp: '과거 증빙입니다. 이후 정정 사항은 활동 로그를 확인하세요.',
      gateItems: '요건 상세', met: '충족', unmet: '미충족',
      historyLatest: '최근 기록된 활동', phasePrefix: '단계',
      soakMissingTime: '병행 가동 중이지만 마지막 점검 / 시작 시각이 없거나 올바르지 않습니다.',
      soakLastCheck: '마지막 병행 가동 점검', trendTitle: '표본 측정 추이',
      trendHelp: '복제 지연: 직전 15분의 DMS 최댓값 또는 네이티브 MySQL 표본. 여유 용량: 직전 30분의 평균 여유 스토리지 %. 일일 최댓값·최솟값이 아닙니다. 누락일과 복제 방식 변경 시 선이 끊깁니다.',
      lagMetric: '복제 지연 (초)', headroomMetric: '스토리지 여유 용량 (%)',
      latestSample: '마지막 기록일', delta: '전일 대비 변화',
      trendEmpty: '수치 측정값이 기록되지 않았습니다.', trendSingle: '추이를 보려면 비교 가능한 날짜가 최소 2개 필요합니다.',
      sampleValues: '날짜별 측정값', date: '날짜 (UTC)', seconds: '초', percentagePoints: '퍼센트포인트',
      checkStates: { pass: '통과', fail: '실패', unknown: '검토 필요', na: '해당 없음' },
      dayVerdict: { green: 'GREEN', red: 'RED' }, dayUnknown: '판정 미기록',
      check: '점검', result: '결과', sampleDetail: '기록된 점검 증빙',
      pendingDays: (n) => `아직 기록되지 않은 관찰 일수: ${n}일`,
      objectRecorded: '완료로 기록됨 (기존 집계)', objectLoaded: '적재완료',
      currencyUnknown: '통화 미기록',
    },
  };

  // "success"/"in_progress"/"blocked" is the documented activity-log.jsonl vocabulary
  // (dashboard.md), written by the agent by hand. The automated soak-check scripts
  // (soak_check.py / soak_check_lambda.py) write their own day verdict here too, mapped
  // onto that same vocabulary (green->success, red->blocked) — but recognize the raw
  // "green"/"red" strings too so a log file written by an older, unfixed version of
  // those scripts still renders correctly instead of silently defaulting every entry to
  // a green checkmark regardless of its actual result.
  const ENTRY_ICON = { success: '✓', in_progress: '…', blocked: '!', green: '✓', red: '✗' };
  const ENTRY_CLASS = { success: 'success', in_progress: 'in_progress', blocked: 'blocked', green: 'success', red: 'blocked' };
  const fmtNum = (n) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString();
  const list = (v) => Array.isArray(v) ? v.filter(x => x != null) : [];
  const numeric = (v) => typeof v === 'number' && Number.isFinite(v);
  const percent = (v) => numeric(v) ? Math.max(0, Math.min(100, v)) : 0;
  const paragraph = (label, value) => value == null || value === '' ? ''
    : `<p><strong>${esc(label)}:</strong> ${esc(value)}</p>`;
  const bullets = (values) => list(values).length ? `<ul>${list(values).map(v => `<li>${esc(v)}</li>`).join('')}</ul>` : '';
  const references = (values) => list(values).length
    ? `<p class="references"><strong>${esc(L().evidence)}:</strong> ${list(values).map(esc).join(' · ')}</p>` : '';
  const disclosure = (key, title, body, open = false) => body
    ? `<details data-key="${esc(key)}"${open ? ' open' : ''}><summary>${esc(title)}</summary>${body}</details>` : '';
  const badge = (value, labels) => `<span class="badge ${esc(value || 'pending')}">${esc(labels[value] || L().notRecorded)}</span>`;

  // Polling must not close a customer's evidence panel or steal keyboard focus.
  // Skip unchanged regions, and restore disclosures by stable IDs when facts change.
  function setHTML(selector, html) {
    const node = $(selector);
    if (rendered.get(node) === html) return;
    const states = new Map();
    let focused;
    node.querySelectorAll('details[data-key]').forEach(d => {
      states.set(d.dataset.key, d.open);
      if (d.querySelector('summary') === document.activeElement) focused = d.dataset.key;
    });
    node.innerHTML = html;
    node.querySelectorAll('details[data-key]').forEach(d => {
      if (states.has(d.dataset.key)) d.open = states.get(d.dataset.key);
      if (focused === d.dataset.key) d.querySelector('summary').focus({ preventScroll: true });
    });
    rendered.set(node, html);
  }

  function fmtTime(iso) {
    if (!iso) return '';
    return esc(String(iso).replace('T', ' '));
  }

  function L() { return LABELS[appliedLang] || LABELS.en; }

  function applyStaticLabels(s) {
    const lang = Object.prototype.hasOwnProperty.call(LABELS, s.lang) ? s.lang : 'en';
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
    $('#lbl-actions-h2').textContent = l.actionsH2;
    $('#lbl-outlook-h2').textContent = l.outlookH2;
    $('#lbl-risks-h2').textContent = l.risksH2;
    $('#lbl-objects-h2').innerHTML = `${esc(l.objectsH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.objectsSub)}</span>`;
    $('#lbl-soak-h2').innerHTML = `${esc(l.soakH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.soakSub)}</span>`;
    $('#lbl-log-h2').innerHTML = `${esc(l.logH2)} <span style="color:var(--muted);font-weight:400;font-size:11px">${esc(l.logSub)}</span>`;
    $('#footer-text').textContent = l.footer;
  }

  function renderTiles(s) {
    const l = L();
    $('#pct').textContent = s.overall_progress_pct ?? 0;
    $('#pct-bar').style.width = `${percent(s.overall_progress_pct)}%`;
    const done = (s.phases || []).filter(p => p.status === 'done').length;
    $('#phase-count').textContent = `${done} / ${(s.phases || []).length}`;
    $('#phase-remaining').textContent = l.phasesRemaining((s.phases || []).length - done);
    $('#current-activity').textContent = s.current_activity || '—';
    $('#current-phase-chip').textContent = s.current_phase ? `${l.phasePrefix} ${s.current_phase}` : '';
  }

  function renderActions(s) {
    const l = L();
    const actions = list(s.customer_actions);
    const pending = actions.filter(a => a.status !== 'resolved');
    const resolved = actions.filter(a => a.status === 'resolved');
    const row = (a) => `<article class="insight action-card">
      <div class="card-heading">${badge(a.status, l.actionStatus)} <h3>${esc(a.id)} · ${esc(a.title)}</h3></div>
      ${paragraph(l.request, a.request)}${paragraph(l.why, a.why)}
      <div class="metadata">${paragraph(l.owner, a.owner)}${paragraph(l.due, a.due)}
        ${paragraph(l.phasePrefix, a.phase)}</div>
      ${paragraph(l.resolution, a.resolution)}${references(a.evidence)}</article>`;
    setHTML('#actions', (pending.length ? pending.map(row).join('')
      : `<p class="empty">${esc(Array.isArray(s.customer_actions) ? l.actionsEmpty : l.actionsUnknown)}</p>`)
      + (pending.length ? `<p class="section-note">${esc(l.actionHelp)}</p>` : '')
      + disclosure('actions-resolved', `${l.actionResolved} (${resolved.length})`, resolved.map(row).join('')));
  }

  function money(range, currency) {
    if (!range || !numeric(range.min) || range.min < 0
      || (range.max != null && (!numeric(range.max) || range.max < range.min))) return esc(L().notRecorded);
    const amount = (v) => v.toLocaleString(appliedLang, { maximumFractionDigits: 2 });
    return `${esc(currency || L().currencyUnknown)} ${amount(range.min)}${numeric(range.max) && range.max !== range.min ? '–' + amount(range.max) : ''}`;
  }

  function renderOutlook(s) {
    const l = L();
    const cost = s.estimates?.cost;
    const timeline = s.estimates?.timeline;
    const costRows = list(cost?.items).map(it => `<tr><td>${esc(it.label)}</td>
      <td>${esc(({ monthly: l.monthly, one_time: l.oneTime })[it.cadence] || l.notRecorded)}</td>
      <td class="num">${money(it.amount, cost.currency)}</td><td>${esc(it.basis)}</td></tr>`).join('');
    const costBody = cost ? `<div class="estimate-totals">
        <div><span>${esc(l.monthly)}</span><b>${money(cost.monthly, cost.currency)}</b></div>
        <div><span>${esc(l.oneTime)}</span><b>${money(cost.one_time, cost.currency)}</b></div></div>
      <p class="${cost.scope === 'partial' ? 'attention' : 'section-note'}">${esc(l.costScope[cost.scope] || l.scopeUnknown)}</p>
      ${paragraph(l.asOf, cost.as_of)}
      ${disclosure('cost-breakdown', l.breakdown, (costRows ? `<div class="table-scroll"><table class="obj-table">
        <thead><tr><th>${esc(l.item)}</th><th>${esc(l.cadence)}</th><th>${esc(l.amount)}</th><th>${esc(l.basis)}</th></tr></thead>
        <tbody>${costRows}</tbody></table></div>` : '') + paragraph(l.basis, cost.basis))}
      ${list(cost.assumptions).length ? `<h4>${esc(l.assumptions)}</h4>${bullets(cost.assumptions)}` : ''}
      ${references(cost.evidence)}` : `<p class="empty">${esc(l.costEmpty)}</p>`;
    const timingBody = timeline ? `<div class="estimate-totals"><div><span>${esc(l.downtime)}</span>
        <b>${esc(timeline.downtime_estimate || l.notRecorded)}</b></div>
        <div><span>${esc(l.budget)}</span><b>${esc(timeline.downtime_budget || l.notRecorded)}</b></div></div>
      <p class="section-note">${esc(l.timingBasis[timeline.basis] || l.timingUnknown)}</p>
      ${paragraph(l.milestone, timeline.next_milestone)}${paragraph(l.completion, timeline.estimated_completion)}
      ${paragraph(l.window, timeline.cutover_window)}
      ${bullets(timeline.assumptions)}${references(timeline.evidence)}` : `<p class="empty">${esc(l.timelineEmpty)}</p>`;
    setHTML('#outlook', `<article class="insight"><h3>${esc(l.costTitle)}</h3>${costBody}</article>
      <article class="insight"><h3>${esc(l.timelineTitle)}</h3>${timingBody}</article>`);
    const strategy = s.strategy;
    setHTML('#strategy', strategy ? `<article class="insight"><h3>${esc(l.strategyTitle)}</h3>
      <div class="route"><div>${paragraph(l.source, strategy.source || l.notRecorded)}</div>
        <span aria-hidden="true">→</span><div>${paragraph(l.target, strategy.target || l.notRecorded)}</div></div>
      ${paragraph(l.method, strategy.method)}${paragraph(l.rationale, strategy.rationale)}
      ${paragraph(l.rollback, strategy.rollback)}
      ${disclosure('strategy-tradeoffs', l.tradeoffs, bullets(strategy.tradeoffs))}
      ${references(strategy.evidence)}</article>` : '');
  }

  function renderRisks(s) {
    const l = L();
    const risks = list(s.risks);
    const closed = risks.filter(r => r.status === 'closed');
    const active = risks.filter(r => r.status !== 'closed');
    const priority = { high: 0, medium: 1, low: 2 };
    active.sort((a, b) => (a.status === 'accepted') - (b.status === 'accepted')
      || (priority[a.severity] ?? 3) - (priority[b.severity] ?? 3));
    const row = (r) => `<article class="insight risk-card ${esc(r.severity || '')}">
      <div class="card-heading">${badge(r.status, l.riskStatus)} <h3>${esc(r.id)} · ${esc(r.title)}</h3>
        <span class="severity">${esc(l.severity[r.severity] || l.severityUnknown)}</span></div>
      ${paragraph(l.impact, r.impact)}${paragraph(l.mitigation, r.mitigation)}
      <div class="metadata">${paragraph(l.owner, r.owner)}${paragraph(l.phasePrefix, r.phase)}
        ${paragraph(l.updatedPrefix, r.updated_at)}</div>${references(r.evidence)}</article>`;
    const accepted = active.filter(r => r.status === 'accepted').length;
    setHTML('#risks', risks.length ? `<p class="section-note">${esc(l.riskCounts(active.length - accepted, accepted, closed.length))}</p>
      ${active.map(row).join('')}${disclosure('risks-closed', `${l.closedRisks} (${closed.length})`, closed.map(row).join(''))}`
      : `<p class="empty">${esc(Array.isArray(s.risks) ? l.risksEmpty : l.risksUnknown)}</p>`);
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
    setHTML('#gates', gates.map(g => `
      <div class="gate ${g.met ? 'met' : 'unmet'}">
        <span class="icon">${g.met ? '✓' : '·'}</span>
        <div class="gate-body"><div class="label">${esc(g.label)}</div><div class="detail">${esc(g.detail || '')}</div>
        ${disclosure(`gate-${g.key}`, l.gateItems, list(g.items).slice().sort((a, b) => (a.met === true) - (b.met === true))
          .map(it => `<div class="work-item">
            <strong>${it.met === true ? '✓' : '·'} ${esc(it.label)}</strong>
            <span class="section-note">${esc(it.met === true ? l.met : l.unmet)}</span>
            ${paragraph(l.result, it.detail)}${paragraph(l.owner, it.owner)}${references(it.evidence)}</div>`).join(''), !g.met)}
        </div>
      </div>`).join('') || `<div class="gate unmet"><span class="icon">·</span><div class="label">${esc(l.noGateData)}</div></div>`);
  }

  function renderPhases(s, log) {
    const l = L();
    setHTML('#phases', (s.phases || []).map(p => {
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      const history = list(log).filter(e => String(e.phase) === String(p.id)
        || (String(p.id) === '6' && String(e.phase) === '6.5')).slice(-3);
      const work = list(p.steps).map(step => `<div class="work-item">
        ${badge(step.status, l.phaseBadge)} <strong>${esc(step.label)}</strong>
        ${paragraph(l.result, step.detail)}${paragraph(l.owner, step.owner)}${references(step.evidence)}</div>`).join('');
      const detail = (list(p.findings).length ? `<h4>${esc(l.findings)}</h4>${bullets(p.findings)}` : '')
        + (work ? `<h4>${esc(l.steps)}</h4>${work}` : '') + paragraph(l.next, p.next_step) + references(p.evidence)
        + (history.length ? `<h4>${esc(l.recentHistory)}</h4><p class="section-note">${esc(l.historyHelp)}</p>${logRows(history)}` : '');
      return `<div class="phase">
        <div class="phase-row">
          <span class="badge ${esc(p.status)}">${esc(l.phaseBadge[p.status] || p.status)}</span>
          <span class="phase-name">${esc(l.phasePrefix)} ${esc(p.id)} · ${esc(p.name)}</span>
          <span class="phase-count">${esc(p.done)}/${esc(p.total)} · ${pct}%</span>
        </div>
        <div class="pbar"><i style="width:${percent(pct)}%"></i></div>
        ${p.summary ? `<p class="phase-summary">${esc(p.summary)}</p>` : ''}
        ${p.note ? `<p class="section-note">${esc(p.note)}</p>` : ''}
        ${!p.summary && history.length ? paragraph(l.historyLatest, history[history.length - 1].title) : ''}
        ${disclosure(`phase-${p.id}`, l.phaseDetails, detail, p.status === 'in_progress')}
      </div>`;
    }).join(''));
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
    const doneCount = (o.validated ?? o.created ?? o.loaded ?? o.done ?? 0);
    const countLabel = o.validated != null ? l.objectsCountSuffix
      : o.loaded != null ? l.objectLoaded : o.done != null ? l.objectRecorded : l.notRecorded;
    if (typeKey === 'tables') {
      return `<div class="obj-card obj-card-wide">
        <div class="obj-card-head"><span class="obj-title">${esc(label)}</span>
          <span class="obj-count">${esc(doneCount)}/${esc(o.total)} ${esc(countLabel)}</span></div>
        <div class="table-scroll"><table class="obj-table">
          <thead><tr><th>${esc(l.objTableCol.name)}</th><th>${esc(l.objTableCol.src)}</th><th>${esc(l.objTableCol.tgt)}</th><th>${esc(l.objTableCol.cs)}</th><th>${esc(l.objTableCol.status)}</th></tr></thead>
          <tbody>${renderTableRows(o.items || [])}</tbody>
        </table></div>
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
        <span class="obj-count">${esc(doneCount)}/${esc(o.total)}</span></div>
      <div class="obj-items">${items}</div>
    </div>`;
  }

  function renderObjects(s) {
    const l = L();
    const mo = s.migration_objects;
    if (!mo || !Object.keys(mo).length) { setHTML('#objects', `<p class="empty">${esc(l.objectsEmpty)}</p>`); return; }
    const order = ['tables', 'views', 'procedures', 'functions', 'triggers', 'events'];
    const html = order.filter(k => mo[k] && mo[k].total > 0).map(k => renderObjectCard(k, mo[k])).join('');
    setHTML('#objects', html || `<p class="empty">${esc(l.objectsNone)}</p>`);
  }

  function checkState(value) {
    return value === true ? 'pass' : value === false ? 'fail' : value === 'not_applicable' ? 'na' : 'unknown';
  }

  function checkMark(value) {
    const l = L();
    const state = checkState(value);
    const mark = { pass: l.soakCheckPass, fail: l.soakCheckFail, unknown: l.soakCheckUnknown, na: l.soakCheckNotApplicable }[state];
    return `<span class="check-${state}">${mark} ${esc(l.checkStates[state])}</span>`;
  }

  function renderTrend(days, key, label, unit, checkKey) {
    const l = L();
    const points = days.map(day => {
      const raw = day.detail?.[key];
      const time = /^\d{4}-\d{2}-\d{2}$/.test(day.date || '') ? Date.parse(`${day.date}T00:00:00Z`) : NaN;
      return { day, time, value: numeric(raw) && raw >= 0 && (key !== 'headroom_pct' || raw <= 100)
        && day.checks?.[checkKey] !== 'not_applicable' ? raw : null };
    });
    const valid = points.filter(p => p.value !== null && Number.isFinite(p.time));
    const last = points[points.length - 1];
    const previous = points[points.length - 2];
    const comparable = (a, b) => a && b && a.value !== null && b.value !== null
      && b.time - a.time === 86400000
      && (key !== 'replication_lag_seconds'
        || (a.day.detail?.replication_lag_mechanism || '') === (b.day.detail?.replication_lag_mechanism || ''));
    const valueText = p => p && p.value !== null ? `${fmtNum(p.value)} ${esc(unit)}` : esc(l.notRecorded);
    let plot = `<p class="empty">${esc(l.trendEmpty)}</p>`;
    if (valid.length) {
      const times = points.map(p => p.time).filter(Number.isFinite);
      const start = Math.min(...times), end = Math.max(...times);
      const max = key === 'headroom_pct' ? 100 : Math.max(1, ...valid.map(p => p.value));
      const x = p => 42 + (end === start ? 0.5 : (p.time - start) / (end - start)) * 340;
      const y = p => 102 - (p.value / max) * 82;
      const lines = points.slice(1).map((p, i) => comparable(points[i], p)
        ? `<line x1="${x(points[i])}" y1="${y(points[i])}" x2="${x(p)}" y2="${y(p)}" class="trend-line"/>` : '').join('');
      const dots = valid.map(p => `<circle cx="${x(p)}" cy="${y(p)}" r="3.5">
        <title>${esc(p.day.date)}: ${valueText(p)}</title></circle>`).join('');
      plot = `<svg class="trend-chart" viewBox="0 0 410 132" role="img" aria-label="${esc(label)}">
        <title>${esc(label)} · ${esc(l.sampleValues)}</title>
        <line x1="42" y1="102" x2="382" y2="102" class="trend-axis"/>
        <text x="2" y="24">${fmtNum(max)}</text><text x="2" y="105">0</text>
        ${lines}${dots}<text x="42" y="124">${esc(new Date(start).toISOString().slice(0, 10))}</text>
        <text x="382" y="124" text-anchor="end">${esc(new Date(end).toISOString().slice(0, 10))}</text></svg>
        ${!points.some((p, i) => comparable(points[i - 1], p)) ? `<p class="section-note">${esc(l.trendSingle)}</p>` : ''}`;
    }
    const delta = comparable(previous, last) ? last.value - previous.value : null;
    const deltaText = delta === null ? l.notRecorded
      : `${delta > 0 ? '+' : ''}${Number(delta.toFixed(2)).toLocaleString(appliedLang)} ${key === 'headroom_pct' ? l.percentagePoints : unit}`;
    const rows = points.map(p => `<tr><td>${esc(p.day.date)}</td><td>${valueText(p)}</td><td>${checkMark(p.day.checks?.[checkKey])}</td></tr>`).join('');
    return `<article class="trend"><h4>${esc(label)}</h4>
      <p>${esc(l.latestSample)} (${esc(last?.day.date || '—')}): <strong>${valueText(last)}</strong></p>
      ${paragraph(l.delta, deltaText)}${plot}
      ${disclosure(`trend-${key}`, l.sampleValues, `<div class="table-scroll"><table class="obj-table">
        <thead><tr><th>${esc(l.date)}</th><th>${esc(label)}</th><th>${esc(l.result)}</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`)}</article>`;
  }

  function renderSoak(s) {
    const l = L();
    const soak = s.soak;
    if (!soak || soak.waived) {
      setHTML('#soak', soak && soak.waived
        ? `<div class="soak-waived">${esc(l.soakWaived(soak.waived_reason))}</div>`
        : `<div id="soak-empty">${esc(l.soakEmpty)}</div>`);
      return;
    }
    const nTotal = soak.n_total || 0;
    const days = list(soak.days).slice().sort((a, b) => String(a.date || '').localeCompare(String(b.date || '')));
    const consecutive = soak.consecutive_green || 0;
    const needsReview = days.some(d => d.needs_agent_review);
    const lastReviewDay = days.map((d, i) => ({ d, i })).filter(x => x.d.needs_agent_review).pop();
    // Distinct from the 15-min chat-staleness badge, which assumes an active session —
    // a soak check runs roughly daily, so "overdue" means missing a run, not missing
    // a few minutes. 36h gives one day's cadence a buffer before flagging.
    const lastExpectedRun = soak.last_checked_at || soak.started_at;
    const overdueHrs = soak.state === 'active' && lastExpectedRun
      ? (Date.now() - new Date(lastExpectedRun).getTime()) / 3600000 : 0;
    const isOverdue = soak.state === 'active' && (!lastExpectedRun || !Number.isFinite(overdueHrs) || overdueHrs > 36);

    const dayCells = days.map((d, i) => disclosure(`soak-day-${d.date}`,
      `${l.soakDayLabel(i + 1)} · ${d.date || '—'} · ${l.dayVerdict[d.overall] || l.dayUnknown}`,
      `<table class="obj-table"><thead><tr><th>${esc(l.check)}</th><th>${esc(l.result)}</th></tr></thead><tbody>
      ${Object.entries(l.soakCheckLabel).map(([k, label]) => `<tr><td>${esc(label)}</td><td>${checkMark(d.checks?.[k])}</td></tr>`).join('')}
      </tbody></table>${d.detail && Object.keys(d.detail).length
        ? disclosure(`soak-evidence-${d.date}`, l.sampleDetail, `<pre>${esc(JSON.stringify(d.detail, null, 2))}</pre>`) : ''}`,
      i === days.length - 1)).join('');

    setHTML('#soak', `
      <div class="soak-explain">${esc(l.soakExplain(nTotal))}</div>
      ${paragraph(l.soakLastCheck, soak.last_checked_at || l.notRecorded)}
      ${isOverdue ? `<div class="soak-review-banner">${esc(!lastExpectedRun || !Number.isFinite(overdueHrs)
        ? l.soakMissingTime : l.soakOverdueBanner(Math.floor(overdueHrs)))}</div>` : ''}
      ${needsReview ? `<div class="soak-review-banner">${esc(l.soakReviewBanner(lastReviewDay.i + 1))}</div>` : ''}
      <div class="soak-counter"><span class="n">${esc(consecutive)}</span><span class="of">${esc(l.soakCounterOf(nTotal))}</span></div>
      ${days.length ? `<h3>${esc(l.trendTitle)}</h3><p class="section-note">${esc(l.trendHelp)}</p>
        <div class="trends">${renderTrend(days, 'replication_lag_seconds', l.lagMetric, l.seconds, 'replication_lag')}
        ${renderTrend(days, 'headroom_pct', l.headroomMetric, '%', 'headroom')}</div>` : ''}
      <div class="soak-days">${dayCells}</div>
      ${nTotal > days.length ? `<p class="section-note">${esc(l.pendingDays(nTotal - days.length))}</p>` : ''}`);
  }

  function logRows(lines) {
    return lines.slice().reverse().map(e => `
      <div class="entry ${ENTRY_CLASS[e.result] || 'success'}">
        <span class="icon">${ENTRY_ICON[e.result] || '✓'}</span>
        <div class="body">
          <div class="title">${esc(e.title)} <span class="time">${fmtTime(e.time)}</span></div>
          <div class="action">${esc(e.action || '')}</div>
          ${e.detail ? `<div class="detail">${esc(e.detail)}</div>` : ''}
          ${references(e.files)}
        </div>
      </div>`).join('');
  }

  function renderLog(lines) {
    setHTML('#log', lines.length ? logRows(lines) : `<div id="log-empty">${esc(L().logEmpty)}</div>`);
  }

  // No manual `?_=timestamp` cache-buster: `cache:'no-store'` already forces a real
  // network fetch every poll, and — critically — appending an extra query param to an S3
  // presigned URL breaks its SigV4 signature (the signature covers the exact query string
  // present at sign time; anything added after, including a stray `&_=...`, makes S3
  // recompute a different canonical request and reject it). Confirmed live: with the
  // buster, every poll against a presigned status.json/activity-log.jsonl URL 403'd.
  async function fetchJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url.split('?')[0]}: ${r.status}`);
    return r.json();
  }

  async function fetchJSONL(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (r.status === 404) return [];   // not yet created / not yet appended to — not an error
    if (!r.ok) throw new Error(`${url.split('?')[0]}: ${r.status}`);
    const text = await r.text();
    return text.split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  }

  async function tick() {
    if (polling) return;
    polling = true;
    try {
      const [status, log] = await Promise.all([fetchJSON(STATUS_URL), fetchJSONL(LOG_URL)]);
      applyStaticLabels(status);
      renderTiles(status);
      renderActions(status);
      renderCutover(status);
      renderOutlook(status);
      renderRisks(status);
      renderPhases(status, log);
      renderObjects(status);
      renderSoak(status);
      const logContent = JSON.stringify([appliedLang, log]);
      if (logContent !== lastLogContent) { renderLog(log); lastLogContent = logContent; }
      $('#updated-text').innerHTML = `${esc(L().updatedPrefix)} <b>${fmtTime(status.updated_at)}</b>`;
      // The 15-minute staleness badge assumes an active interactive session — correct
      // outside the soak window, but during an active soak (once-a-day cadence by
      // design) `updated_at` legitimately doesn't move for ~24h between runs, which
      // would otherwise make this badge show constantly for the entire soak window.
      // Suppress it during an active soak; the soak section's own 36-hour overdue
      // banner (renderSoak) already covers "a scheduled run may have been missed" for
      // that period specifically.
      const soakActive = status.soak && !status.soak.waived && status.soak.state === 'active';
      const stale = !soakActive && status.updated_at
        && (Date.now() - new Date(status.updated_at).getTime() > 15 * 60 * 1000);
      $('#stale-badge').style.display = stale ? 'inline' : 'none';
      $('#conn-error').style.display = 'none';
    } catch (err) {
      $('#conn-error').style.display = 'block';
      $('#conn-error').textContent = L().connError(err.message);
    } finally {
      polling = false;
    }
  }

  tick();
  setInterval(tick, POLL_MS);
})();
