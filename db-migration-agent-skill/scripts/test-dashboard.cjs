#!/usr/bin/env node
// No dependencies. Exercise the shipped IIFE, including both fetch paths and polling.
// Fixtures are read in place; optional-field cases extend real snapshots in memory.
// Usage: node scripts/test-dashboard.cjs [/path/to/dryrun3/engagements]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const fixtures = path.resolve(process.argv[2] || path.join(root, '../dryrun3/engagements'));
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
  assert(files.length, 'No real engagement fixtures found');
  let small, smallLog;
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
    if (file.endsWith('/small/dashboard/status.json')) { small = status; smallLog = log; }
    console.log(`PASS local + presigned: ${file}`);
  }
  assert(small, 'The small real engagement is required for optional-field cases');
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
  const empty = await mount({ phases: [], cutover_gates: [] }, '');
  assert.equal(empty.document.documentElement.lang, 'en');
  console.log(`PASS ${omissions} optional-field omissions; spec examples; en/ko parity; readiness; four-state soak; gaps; escaping; fetch recovery; legacy logs; disclosure/focus preservation; zero/range costs`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
