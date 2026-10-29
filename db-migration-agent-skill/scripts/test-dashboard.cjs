#!/usr/bin/env node
// No dependencies. Exercise the shipped IIFE, including both fetch paths and polling.
// Defaults to the public synthetic fixture; supplied engagements are read in place.
// Optional-field cases always extend the synthetic baseline in memory, never real history.
// Usage: node scripts/test-dashboard.cjs [/path/to/engagements]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const syntheticFixtures = path.join(__dirname, 'fixtures/engagements');
const fixtures = path.resolve(process.argv[2] || syntheticFixtures);
const source = fs.readFileSync(path.join(root, 'shared/assets/dashboard.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'shared/templates/dashboard.html'), 'utf8');
const spec = fs.readFileSync(path.join(root, 'shared/reference/dashboard.md'), 'utf8');
const examples = [...spec.matchAll(/```json\n([\s\S]*?)\n```/g)].map(m => JSON.parse(m[1]));
const phaseFields = examples.find(x => x.summary);
const gateFields = examples.find(x => x.items);
const actionFields = examples.find(x => x.customer_actions);
const estimateFields = examples.find(x => x.estimates);
const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[c]);

async function mount(initial, initialLog, signed = false) {
  // Small DOM stub: IDs come from the actual shell; disclosures retain identity/open
  // state and focus so changed and unchanged polling paths are both exercised.
  function element() {
    let content = '', details = [];
    return {
      textContent: '', style: {}, writes: 0,
      get innerHTML() { return content; },
      set innerHTML(value) {
        content = value; this.writes++;
        details = [...value.matchAll(/<details data-key="([^"]+)"([^>]*)>/g)].map(match => {
          const summary = { focus: () => { document.activeElement = summary; } };
          return { dataset: { key: match[1] }, open: /\bopen\b/.test(match[2]),
            querySelector: () => summary };
        });
      },
      querySelectorAll: () => details,
    };
  }
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], element()]));
  let status = structuredClone(initial), log = initialLog, interval, responseCode = 200, logCode = 200;
  const requests = [];
  const urls = signed ? {
    DASHBOARD_STATUS_URL: 'https://example.invalid/status.json?X-Amz-Signature=unchanged&X-Amz-Expires=604800',
    DASHBOARD_LOG_URL: 'https://example.invalid/activity-log.jsonl?X-Amz-Signature=also-unchanged',
  } : {};
  const document = {
    documentElement: {}, activeElement: null,
    querySelector: selector => {
      const node = nodes.get(selector.slice(1));
      assert(node, `Missing element in real HTML: ${selector}`);
      return node;
    },
  };
  const context = vm.createContext({
    document, window: urls, console,
    setInterval: (fn, ms) => { assert.equal(ms, 5000); interval = fn; },
    fetch: async (url, options) => {
      requests.push(url);
      assert.equal(options.cache, 'no-store');
      const isStatus = url === (urls.DASHBOARD_STATUS_URL || 'status.json');
      assert(isStatus || url === (urls.DASHBOARD_LOG_URL || 'activity-log.jsonl'), `Unexpected fetch: ${url}`);
      const code = isStatus ? responseCode : logCode;
      return { ok: code === 200, status: code,
        json: async () => structuredClone(status), text: async () => log };
    },
  });
  // Expose only dictionaries for a structural translation parity assertion.
  vm.runInContext(source.replace('  tick();', '  globalThis.testLabels = LABELS;\n  tick();'), context);
  await new Promise(resolve => setImmediate(resolve));
  const get = id => nodes.get(id);
  const healthy = () => {
    assert.equal(get('conn-error').style.display, 'none', get('conn-error').textContent);
    for (const [id, node] of nodes) {
      assert(!/NaN|undefined|\[object Object\]/.test(node.innerHTML), `Bad rendering in ${id}`);
    }
  };
  healthy();
  return {
    get, healthy, document, labels: context.testLabels, requests,
    update: async (next = status, nextLog = log) => {
      status = structuredClone(next); log = nextLog; await interval();
    },
    fail: async (code, onlyLog = false) => {
      if (onlyLog) logCode = code; else responseCode = code;
      await interval();
    },
  };
}

function keyShape(object) {
  return Object.fromEntries(Object.entries(object).map(([key, value]) =>
    [key, typeof value === 'object' ? keyShape(value) : typeof value]));
}

function optionalPaths(value, prefix = []) {
  return Object.keys(value).flatMap(key => {
    const current = [...prefix, key];
    const nested = value[key];
    return [current, ...(nested && typeof nested === 'object'
      ? optionalPaths(nested, current) : [])];
  });
}

async function main() {
  const files = fs.readdirSync(fixtures).map(name => path.join(fixtures, name, 'dashboard/status.json'))
    .filter(file => fs.existsSync(file)).sort();
  assert(files.length, `No engagement fixtures found in ${fixtures} (expected */dashboard/status.json)`);
  const baselineDir = path.join(syntheticFixtures, 'synthetic/dashboard');
  const small = JSON.parse(fs.readFileSync(path.join(baselineDir, 'status.json'), 'utf8'));
  const smallLog = fs.readFileSync(path.join(baselineDir, 'activity-log.jsonl'), 'utf8');
  for (const file of files) {
    const status = JSON.parse(fs.readFileSync(file, 'utf8'));
    const logPath = path.join(path.dirname(file), 'activity-log.jsonl');
    const log = fs.readFileSync(logPath, 'utf8');
    const original = JSON.stringify(status);
    for (const signed of [false, true]) {
      const page = await mount(status, log, signed);
      assert.equal((page.get('phases').innerHTML.match(/class="phase"/g) || []).length, status.phases.length);
      assert.equal(status.phases.length, 11, `${file}: phase invariant`);
      assert.equal(new Set(status.phases.map(p => p.id)).size, 11, `${file}: repeated phase ID`);
      assert.deepEqual(status.cutover_gates.map(g => g.key).sort(),
        ['client_inventory', 'validation', 'soak', 'rehearsal', 'runbook', 'approvals'].sort());
      for (const phase of status.phases) assert(page.get('phases').innerHTML.includes(escapeHTML(phase.name)));
      assert.equal(page.get('cutover').className, 'cutover ' + (status.cutover_ready ? 'ready' : 'notready'));
      assert.equal(page.document.documentElement.lang, status.lang === 'ko' ? 'ko' : 'en');
      const before = page.get('phases').innerHTML;
      await page.update();
      page.healthy();
      assert.equal(page.get('phases').innerHTML, before);
      assert.equal(page.requests.length, 4);
      assert.equal(JSON.stringify(status), original, 'Renderer mutated fixture');
      assert.deepEqual(keyShape(page.labels.en), keyShape(page.labels.ko), 'Translation key/type mismatch');
    }
    console.log(`PASS local + presigned: ${file}`);
  }
  assert(small, 'The synthetic baseline is required for optional-field cases');
  const enriched = structuredClone(small);
  Object.assign(enriched, actionFields, estimateFields);
  Object.assign(enriched.phases[2], phaseFields);
  Object.assign(enriched.cutover_gates[0], gateFields);
  for (const lang of ['en', 'ko']) {
    enriched.lang = lang;
    const page = await mount(enriched, smallLog);
    for (const [id, text] of [
      ['actions', enriched.customer_actions[0].request], ['risks', enriched.risks[0].mitigation],
      ['outlook', '3.25'], ['strategy', enriched.strategy.rationale],
      ['phases', phaseFields.findings[0]], ['gates', gateFields.items[1].detail],
    ]) assert(page.get(id).innerHTML.includes(escapeHTML(text)), `Missing insight: ${id}`);
    const contradictory = structuredClone(enriched);
    contradictory.cutover_gates.forEach(g => { g.met = true; });
    contradictory.cutover_ready = false;
    await page.update(contradictory);
    assert.equal(page.get('cutover').className, 'cutover notready', 'Page computed readiness');
    contradictory.cutover_gates.forEach(g => { g.met = false; });
    contradictory.cutover_ready = true;
    await page.update(contradictory);
    assert.equal(page.get('cutover').className, 'cutover ready', 'Page replaced the recorded verdict');
  }
  // Every newly documented field, including nested fields, is individually omittable.
  const newRoots = {
    ...actionFields, ...estimateFields,
    phases: enriched.phases.map(p => p.id === '2' ? phaseFields : {}),
    cutover_gates: enriched.cutover_gates.map(g => g.key === 'client_inventory' ? gateFields : {}),
  };
  let omissions = 0;
  for (const keys of optionalPaths(newRoots)) {
    // Preserve mandatory arrays and their 11/6 entries; only omit their new properties.
    if (['phases', 'cutover_gates'].includes(keys[0]) && keys.length < 3) continue;
    const variant = structuredClone(enriched);
    let parent = variant;
    for (const key of keys.slice(0, -1)) parent = parent[key];
    if (Array.isArray(parent)) parent.splice(Number(keys.at(-1)), 1);
    else delete parent[keys.at(-1)];
    await mount(variant, smallLog);
    omissions++;
  }
  const states = [true, false, null, 'not_applicable'];
  const dayTemplate = examples.find(x => x.soak).soak.days[0];
  const soakStatus = structuredClone(enriched);
  soakStatus.soak = {
    state: 'active', n_total: 7, consecutive_green: 0, last_checked_at: new Date().toISOString(),
    days: states.map((value, i) => ({
      ...structuredClone(dayTemplate), date: `2026-09-${String(i + 1).padStart(2, '0')}`,
      checks: Object.fromEntries(Object.keys(dayTemplate.checks).map(k => [k, value])),
      detail: { replication_lag_seconds: [0, 12, null, null][i],
        headroom_pct: [60, 40, null, null][i], replication_lag_mechanism: 'dms' },
    })),
  };
  soakStatus.lang = 'en';
  const page = await mount(soakStatus, smallLog);
  const soakHTML = page.get('soak').innerHTML;
  for (const state of ['pass', 'fail', 'unknown', 'na']) assert(soakHTML.includes(`check-${state}`));
  assert.equal((soakHTML.match(/class="trend-line"/g) || []).length, 2, 'Missing values must break both lines');
  assert(soakHTML.includes('0 s'), 'Numeric zero was lost');
  assert(!soakHTML.includes('-40 percentage points'), 'Missing latest day reused older value');
  const gap = structuredClone(soakStatus);
  gap.soak.days = [gap.soak.days[0], gap.soak.days[1]];
  gap.soak.days[1].date = '2026-09-04';
  await page.update(gap);
  assert(!page.get('soak').innerHTML.includes('class="trend-line"'), 'Missing date bridged');
  gap.soak.days[1].date = '2026-09-02';
  gap.soak.days[1].detail.replication_lag_mechanism = 'mysql_replica_status';
  await page.update(gap);
  assert.equal((page.get('soak').innerHTML.match(/class="trend-line"/g) || []).length, 1, 'Mechanism change bridged');
  assert(page.get('soak').innerHTML.includes('-20 percentage points'), 'Headroom delta incorrect');
  gap.soak.last_checked_at = null;
  await page.update(gap);
  assert(page.get('soak').innerHTML.includes('missing or invalid'));
  gap.soak.waived = true; gap.soak.waived_reason = 'Recorded waiver';
  await page.update(gap);
  assert(page.get('soak').innerHTML.includes('Recorded waiver'));
  assert(!page.get('soak').innerHTML.includes('<svg'));
  // Optional soak.compressed_window: absent = byte-identical output; present = rendered,
  // escaped, every field individually omittable, also shown alongside active daily soak.
  const waivedOnly = page.get('soak').innerHTML;
  assert(!waivedOnly.includes('soak-compressed'), 'Compressed window rendered when absent');
  const cwExample = examples.find(x => x.soak && x.soak.compressed_window).soak;
  const cwStatus = structuredClone(gap);
  Object.assign(cwStatus.soak, structuredClone(cwExample));
  await page.update(cwStatus);
  page.healthy();
  let cwHTML = page.get('soak').innerHTML;
  assert(cwHTML.includes(escapeHTML(cwExample.waived_reason)), 'Waiver reason lost');
  assert(cwHTML.includes('6 h required (originally 3 day(s))'), 'Compressed duration missing');
  assert(cwHTML.includes(escapeHTML(cwExample.compressed_window.waiver_ref)), 'Waiver ref missing');
  assert(cwHTML.includes(escapeHTML(cwExample.compressed_window.intervals[0].limitations[0])), 'Interval limitation missing');
  assert(cwHTML.includes('reports/soak-manual-0800-1000.md'), 'Interval evidence missing');
  assert(cwHTML.includes('GREEN'), 'Interval verdict missing');
  // OBS-42: hour-granular intervals render with their check table (compressed_window
  // intervals carrying `checks`/`detail`), and sub-day soak.days entries render as
  // time-labelled intervals with unique keys within one date.
  const hourly = structuredClone(cwStatus);
  hourly.soak.compressed_window.intervals = [
    { started_at: '2026-10-28T18:55:00Z', ended_at: '2026-10-28T19:55:00Z', overall: 'green',
      checks: { row_count: true, checksum: true, alarms: true }, detail: { orders_lag_rows: 0 } },
    { started_at: '2026-10-28T19:55:00Z', ended_at: '2026-10-28T20:55:00Z', overall: 'unknown',
      checks: { row_count: null } }];
  const hourlyPage = await mount(hourly, smallLog);
  const hHTML = hourlyPage.get('soak').innerHTML;
  assert(hHTML.includes('2026-10-28 18:55:00Z → 2026-10-28 19:55:00Z') && hHTML.includes('2026-10-28 19:55:00Z → 2026-10-28 20:55:00Z'), 'hour intervals missing');
  assert(hHTML.includes('Row count') && hHTML.includes('orders_lag_rows'), 'interval check table / detail missing');
  const subDay = structuredClone(soakStatus);
  subDay.soak.days = [0, 1, 2].map(h => ({ date: '2026-10-28', started_at: `2026-10-28T${19 + h}:00:00Z`,
    ended_at: `2026-10-28T${20 + h}:00:00Z`, overall: 'green', checks: { row_count: true } }));
  const sdHTML = (await mount(subDay, smallLog)).get('soak').innerHTML;
  for (const h of [0, 1, 2]) assert(sdHTML.includes(`Interval ${h + 1} · 2026-10-28 ${19 + h}:00:00Z → 2026-10-28 ${20 + h}:00:00Z`), `interval ${h + 1} label`);
  const keys = [...sdHTML.matchAll(/data-key="(soak-day-[^"]+)"/g)].map(m => m[1]);
  assert.equal(new Set(keys).size, 3, 'sub-day entries need unique disclosure keys');
  const koSub = (await mount({ ...subDay, lang: 'ko' }, smallLog)).get('soak').innerHTML;
  assert(koSub.includes('1번째 구간'), 'ko interval label');
  delete cwStatus.soak.compressed_window;
  await page.update(cwStatus);
  assert.equal(page.get('soak').innerHTML.replace(escapeHTML(cwExample.waived_reason), escapeHTML('Recorded waiver')),
    waivedOnly, 'Absent compressed window changed waived rendering');
  for (const keys of optionalPaths(cwExample.compressed_window)) {
    const variant = structuredClone(gap);
    variant.soak.compressed_window = structuredClone(cwExample.compressed_window);
    let parent = variant.soak.compressed_window;
    for (const key of keys.slice(0, -1)) parent = parent[key];
    if (Array.isArray(parent)) parent.splice(Number(keys.at(-1)), 1);
    else delete parent[keys.at(-1)];
    await mount(variant, smallLog);
    omissions++;
  }
  const cwHostile = structuredClone(gap);
  cwHostile.soak.compressed_window = { ...structuredClone(cwExample.compressed_window),
    waiver_ref: '<script>x</script>', state: '<b>', intervals: [{ overall: '<i>', limitations: ['<img src=x>'] }, null, 'bad'] };
  await page.update(cwHostile);
  page.healthy();
  cwHTML = page.get('soak').innerHTML;
  assert(!/<script|<img|<b>|<i>/.test(cwHTML) && cwHTML.includes('&lt;script&gt;'), 'Compressed window not escaped');
  const cwActive = structuredClone(soakStatus);
  cwActive.soak.compressed_window = structuredClone(cwExample.compressed_window);
  await page.update(cwActive);
  page.healthy();
  assert(page.get('soak').innerHTML.includes('soak-compressed') && page.get('soak').innerHTML.includes('class="trend-line"'),
    'Compressed window not shown alongside daily soak');
  cwActive.lang = 'ko';
  await page.update(cwActive);
  page.healthy();
  assert(page.get('soak').innerHTML.includes('필요 6시간'), 'Korean compressed label missing');
  await page.update(gap);
  const hostile = structuredClone(enriched);
  hostile.phases[2].summary = '<img src=x onerror="alert(1)">';
  await page.update(hostile);
  assert(page.get('phases').innerHTML.includes('&lt;img'));
  assert(!page.get('phases').innerHTML.includes('<img'));
  await page.fail(404, true);
  page.healthy();
  await page.fail(403, true);
  assert.equal(page.get('conn-error').style.display, 'block');
  assert(page.get('phases').innerHTML.includes('&lt;img'), 'Last snapshot not retained on failure');
  await page.fail(200, true);
  page.healthy();
  const logs = states.map((_, i) => JSON.stringify({ phase: '0', title: `result-${i}`, result: ['success', 'blocked', 'green', 'red'][i] }));
  await page.update(enriched, logs.join('\n') + '\nnot valid JSON\n');
  assert.equal((page.get('log').innerHTML.match(/entry success/g) || []).length, 2);
  assert.equal((page.get('log').innerHTML.match(/entry blocked/g) || []).length, 2);
  const phaseNode = page.get('phases');
  const detail = phaseNode.querySelectorAll().find(d => d.dataset.key === 'phase-2');
  detail.open = true;
  detail.querySelector().focus();
  const writes = phaseNode.writes;
  await page.update(enriched, logs.join('\n') + '\nnot valid JSON\n');
  assert.equal(phaseNode.writes, writes, 'Unchanged poll rebuilt a region');
  const changed = structuredClone(enriched);
  changed.phases[2].findings.push('New result during review');
  await page.update(changed);
  const refreshed = phaseNode.querySelectorAll().find(d => d.dataset.key === 'phase-2');
  assert.equal(refreshed.open, true, 'Changed poll closed the evidence panel');
  assert.equal(page.document.activeElement, refreshed.querySelector(), 'Changed poll lost summary focus');
  await page.update({ ...changed, lang: 'fr', engagement: 'new-engagement', mode: '1' });
  assert.equal(page.document.documentElement.lang, 'en');
  assert(page.get('page-h1').textContent.includes('new-engagement'), 'Header did not refresh');
  await page.update({ ...changed, lang: '__proto__' });
  page.healthy();
  const costVariant = structuredClone(enriched);
  costVariant.estimates.cost.monthly = { min: 0 };
  costVariant.estimates.cost.one_time = { min: 120, max: 130 };
  await page.update(costVariant);
  assert(page.get('outlook').innerHTML.includes('USD 0'));
  assert(page.get('outlook').innerHTML.includes('USD 120–130'));
  costVariant.lang = 'en';
  costVariant.estimates.cost.scope = 'partial';
  costVariant.estimates.cost.items.push({ label: 'Unpriced dependency', cadence: 'one_time' });
  costVariant.risks.push({ id: 'accepted', title: 'Accepted residual risk', status: 'accepted' },
    { id: 'closed', title: 'Resolved risk', status: 'closed' });
  costVariant.customer_actions[0].status = 'resolved';
  costVariant.customer_actions[0].resolution = 'Specific acceptance recorded in the plan';
  await page.update(costVariant);
  assert(page.get('outlook').innerHTML.includes('Partial estimate'));
  assert(page.get('outlook').innerHTML.includes('Unpriced dependency'));
  assert(page.get('risks').innerHTML.includes('1 open / mitigating · 1 accepted · 1 closed'));
  assert(page.get('actions').innerHTML.includes('No pending customer actions recorded'));
  await page.update({ ...small, lang: 'en', risks: [], customer_actions: [] });
  assert(page.get('risks').innerHTML.includes('No risks recorded after review'));
  await page.update({ ...small, lang: 'en' });
  assert(page.get('risks').innerHTML.includes('Risk register not recorded'));
  assert(page.get('actions').innerHTML.includes('Customer actions have not been recorded'));
  // Long activity logs: newest 25 entries, the rest behind a "show all (N)" disclosure.
  const many = Array.from({ length: 472 }, (_, i) => JSON.stringify({ phase: '7.7', title: `ev-${i}`, result: 'success', time: '2026-10-06T00:00:00Z' })).join('\n');
  const logPage = await mount({ ...small, lang: 'en' }, many);
  const logHTML = logPage.get('log').innerHTML;
  const [visible, hidden] = logHTML.split('<details data-key="log-older">');
  assert.equal((visible.match(/class="entry /g) || []).length, 25, 'newest 25 visible');
  assert.equal((hidden.match(/class="entry /g) || []).length, 447, 'older entries behind the toggle');
  assert(visible.indexOf('ev-471') < visible.indexOf('ev-447') && !visible.includes('ev-446<'), 'newest first');
  assert(hidden.includes('<summary>Show all (472)</summary>'));
  const logDetails = logPage.get('log').querySelectorAll()[0];
  logDetails.open = true;
  await logPage.update({ ...small, lang: 'en' }, many + '\n' + JSON.stringify({ phase: '9', title: 'ev-new', result: 'success' }));
  assert(logPage.get('log').innerHTML.includes('Show all (473)'));
  assert.equal(logPage.get('log').querySelectorAll()[0].open, true, 'toggle state preserved across polls');
  const koLog = await mount({ ...small, lang: 'ko' }, many);
  assert(koLog.get('log').innerHTML.includes('모두 보기 (472)'));
  const shortLog = await mount({ ...small, lang: 'en' }, many.split('\n').slice(0, 10).join('\n'));
  assert(!shortLog.get('log').innerHTML.includes('<details'), 'short logs unchanged');
  const xss = await mount({ ...small, lang: 'en' }, JSON.stringify({ title: '<img src=x>', result: 'success' }));
  assert(!xss.get('log').innerHTML.includes('<img'), 'log stays escaped');

  // Post-cutover: an additive `cutover` block replaces the readiness verdict with a completion card.
  const cut = { ...small, lang: 'en', cutover_ready: true, cutover: { completed_at: '2026-10-06T14:03:01Z',
    measured_write_pause_seconds: 45.3, target_endpoint: 'db.example.ap-northeast-2.rds.amazonaws.com',
    rollback_window_ends: '2026-10-13T14:03:00Z', rollback_path_state: 'reverse DMS running, applying' } };
  const cutPage = await mount(cut, smallLog);
  assert.equal(cutPage.get('cutover').className, 'cutover done');
  assert.equal(cutPage.get('verdict-text').textContent, 'Cutover completed');
  for (const part of ['45.3 s', 'db.example.ap-northeast-2', 'reverse DMS running']) assert(cutPage.get('verdict-sub').textContent.includes(part));
  const koCut = await mount({ ...cut, lang: 'ko' }, smallLog);
  assert.equal(koCut.get('verdict-text').textContent, '컷오버 완료');
  assert(koCut.get('verdict-sub').textContent.includes('45.3초'));
  const notDone = await mount({ ...small, lang: 'en', cutover: { target_endpoint: 'x' } }, smallLog);
  assert.equal(notDone.get('cutover').className, 'cutover ' + (small.cutover_ready ? 'ready' : 'notready'), 'no completed_at = readiness card');
  console.log('PASS long-log toggle (en/ko, newest-first, state preserved, escaped); cutover-completed card (en/ko, additive)');
  // Cache engagements (cache_compare.py): additive `keyspaces` object type and cache soak days.
  const ksExample = examples.find(x => x.migration_objects && x.migration_objects.keyspaces);
  assert(ksExample, 'dashboard.md must document the keyspaces example');
  for (const lang of ['en', 'ko']) {
    const ks = { ...structuredClone(small), lang, migration_objects: {
      ...structuredClone(small.migration_objects || {}), ...structuredClone(ksExample.migration_objects) } };
    const ksPage = await mount(ks, smallLog);
    const objHTML = ksPage.get('objects').innerHTML;
    for (const it of ksExample.migration_objects.keyspaces.items) assert(objHTML.includes(escapeHTML(it.name)), `keyspace ${it.name} missing`);
    assert(objHTML.includes(lang === 'en' ? 'Keyspaces (cache)' : '키스페이스 (캐시)'), 'keyspace card label missing');
    assert((objHTML.match(/row-mismatch/g) || []).length >= 1, 'a false sample_match must be highlighted');
    if (small.migration_objects && small.migration_objects.tables) assert(objHTML.includes(escapeHTML(small.migration_objects.tables.items[0].name)), 'tables card lost');
    // OBS-28: a row with key counts only (no sampled/sample_match/comparison) must say so
    // and must not render as validated, even if a writer set status "validated".
    const countsOnly = { ...structuredClone(ks), migration_objects: { keyspaces: { total: 2, validated: 1, items: [
      { name: 'db0', keys_source: 9514532, keys_target: 9514532, key_count_match: true, status: 'validated' },
      { name: 'db1', keys_source: 1000, keys_target: 1000, sampled: 1000, mismatches: 0, key_count_match: true,
        sample_match: true, comparison: 'match', status: 'validated' }] } } };
    const coHTML = (await mount(countsOnly, smallLog)).get('objects').innerHTML;
    const [db0Row, db1Row] = coHTML.split('<tr').filter(r => r.includes('>db0<') || r.includes('>db1<'));
    const co = lang === 'en' ? 'counts only' : '건수만';
    const coStatus = lang === 'en' ? 'counts only (no key sample)' : '건수만 일치 (표본 미비교)';
    const validatedLabel = lang === 'en' ? '>validated<' : '>검증완료<';
    assert((db0Row.match(new RegExp(co.replace(/[()]/g, '\\$&'), 'g')) || []).length >= 2, 'counts-only row must label the sample columns');
    assert(db0Row.includes(coStatus) && !db0Row.includes(validatedLabel), 'counts-only row must not render as validated');
    assert(db1Row.includes(validatedLabel) && !db1Row.includes(coStatus), 'sampled row keeps its validated badge');
    assert(coHTML.includes(lang === 'en' ? '1/2 validated' : '1/2 검증완료'), 'aggregate recomputed from rows');
    // Review r5 #12: partial sample fields never earn "validated", and the card aggregate is
    // recomputed from the rows (a writer's inflated count is flagged, not shown).
    const partial = { ...structuredClone(ks), migration_objects: { keyspaces: { total: 3, validated: 3, items: [
      { name: 'db0', keys_source: 500, keys_target: 500, sampled: 0, key_count_match: true, status: 'validated' },
      { name: 'db1', keys_source: 500, keys_target: 500, sampled: 0, sample_match: true, comparison: 'match', key_count_match: true, status: 'validated' },
      { name: 'db2', keys_source: 0, keys_target: 0, sampled: 0, sample_match: true, comparison: 'match', key_count_match: true, status: 'validated' }] } } };
    const pHTML = (await mount(partial, smallLog)).get('objects').innerHTML;
    const pRows = Object.fromEntries(pHTML.split('<tr').filter(r => /mono">db\d</.test(r)).map(r => [r.match(/>(db\d)</)[1], r]));
    const noResult = lang === 'en' ? 'not validated (no sample result)' : '검증 미완료 (표본 결과 없음)';
    assert(pRows.db0.includes(noResult) && !pRows.db0.includes(validatedLabel), 'sampled:0 without a result must downgrade');
    assert(pRows.db1.includes(noResult), 'a source with keys but 0 sampled keys is not a sample result');
    assert(pRows.db2.includes(validatedLabel), 'an empty keyspace with a match result stays validated');
    assert(pHTML.includes(lang === 'en' ? '1/3 validated' : '1/3 검증완료'), 'aggregate from downgraded rows');
    assert(pHTML.includes(lang === 'en' ? 'status file says 3' : '상태 파일 값 3'), 'inflated aggregate flagged');
    // Review r6 #6: missing counts or key_count_match:false never validate; the declared total
    // stays the denominator and a gap between it and the listed rows is flagged.
    const contra = { ...structuredClone(ks), migration_objects: { keyspaces: { total: 3, validated: 1, items: [
      { name: 'db0', keys_source: 10, keys_target: 10, sampled: 10, sample_match: true, comparison: 'match', key_count_match: true, status: 'validated' },
      { name: 'db1', sample_match: true, comparison: 'match', status: 'validated' },
      { name: 'db2', keys_source: 10, keys_target: 9, sampled: 9, sample_match: true, comparison: 'match', key_count_match: false, status: 'validated' }] } } };
    const cHTML = (await mount(contra, smallLog)).get('objects').innerHTML;
    const cRows = Object.fromEntries(cHTML.split('<tr').filter(r => /mono">db\d</.test(r)).map(r => [r.match(/>(db\d)</)[1], r]));
    assert(cRows.db0.includes(validatedLabel), 'complete evidence stays validated');
    assert(cRows.db1.includes(noResult) && !cRows.db1.includes(validatedLabel), 'missing counts must not validate');
    assert(cRows.db2.includes(noResult) && !cRows.db2.includes(validatedLabel), 'key_count_match:false must not validate');
    assert(cHTML.includes(lang === 'en' ? '1/3 validated' : '1/3 검증완료'), 'declared total kept, count recomputed');
    const short = { ...structuredClone(ks), migration_objects: { keyspaces: { total: 3, validated: 1, items: [contra.migration_objects.keyspaces.items[0]] } } };
    const sHTML = (await mount(short, smallLog)).get('objects').innerHTML;
    assert(sHTML.includes(lang === 'en' ? '1/3 validated' : '1/3 검증완료') && !sHTML.includes('1/1'), 'declared inventory is the denominator');
    assert(sHTML.includes(lang === 'en' ? '1 of 3 declared keyspaces listed' : '선언된 3개 중 1개만 표시'), 'inventory gap flagged');
    // Review r7 #6: contradictory or malformed evidence never validates; declared total kept.
    const base = { keys_source: 10, keys_target: 10, sampled: 10, sample_match: true, comparison: 'match', status: 'validated' };
    const evil = { ...structuredClone(ks), migration_objects: { keyspaces: { total: 7, validated: 7, items: [
      { name: 'db0', ...base, key_count_match: true },
      { name: 'db1', ...base, keys_target: 9, sampled: 1 },                                  // unequal, no key_count_match
      { name: 'db2', ...base, mismatches: 3 },                                                // positive mismatch count
      { name: 'db3', ...base, keys_source: -1, keys_target: -1 },                            // negative count
      { name: 'db4', ...base, sampled: 2.5 },                                                 // non-integer
      { name: 'db5', ...base, keys_target: 9, key_count_match: true, key_count_tolerance: 0 }, // diff beyond tolerance
      { name: 'db6', ...base, keys_target: 9, key_count_match: true, key_count_tolerance: 1 }] } } }; // tool tolerance
    const eHTML = (await mount(evil, smallLog)).get('objects').innerHTML;
    const eRows = Object.fromEntries(eHTML.split('<tr').filter(r => /mono">db\d</.test(r)).map(r => [r.match(/>(db\d)</)[1], r]));
    for (const k of ['db1', 'db2', 'db3', 'db4', 'db5']) assert(eRows[k].includes(noResult) && !eRows[k].includes(validatedLabel), `${k} must not validate`);
    for (const k of ['db0', 'db6']) assert(eRows[k].includes(validatedLabel), `${k} keeps its validated badge`);
    assert(eHTML.includes(lang === 'en' ? '2/7 validated' : '2/7 검증완료'), 'declared denominator, recomputed numerator');
  }
  const cacheSoak = structuredClone(soakStatus);
  cacheSoak.soak.days = [{ date: '2026-10-27', overall: 'red', needs_agent_review: true,
    checks: { key_count: true, sample_match: true, replication_link: 'not_applicable', alarms: true,
      headroom: 'not_applicable', customer_test_suite: 'not_applicable', period_evidence: null },
    detail: { sampler: 'cache_compare' } }, structuredClone(soakStatus.soak.days[0])];
  cacheSoak.soak.days[1].date = '2026-10-28';
  const cachePage = await mount(cacheSoak, smallLog);
  const cacheHTML = cachePage.get('soak').innerHTML;
  assert(cacheHTML.includes('Sampled keys (type/TTL/value)') && cacheHTML.includes('Replication link'), 'cache soak labels missing');
  assert(cacheHTML.includes('Row count'), 'SQL soak day must keep its original rows');
  const [cacheDay] = cacheHTML.split('2026-10-28');
  assert(!cacheDay.includes('Row count') && !cacheDay.includes('Schema drift'), 'cache day rendered SQL-only checks');
  console.log('PASS cache keyspaces card (en/ko, counts-only / no-sample-result rows downgraded, aggregate recomputed) and cache soak-day check rows (additive)');
  const empty = await mount({ phases: [], cutover_gates: [] }, '');
  assert.equal(empty.document.documentElement.lang, 'en');
  console.log(`PASS ${omissions} optional-field omissions; spec examples; en/ko parity; readiness; four-state soak; gaps; compressed soak window; escaping; fetch recovery; legacy logs; disclosure/focus preservation; zero/range costs`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
