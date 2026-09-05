# Progress Dashboard — `dashboard/`

> A local, read-only page the customer opens themselves to see exactly how much of the
> migration is done and — the reason it exists — **whether every hard cutover gate is
> actually met**, before they decide to cut over. It is a rendering of facts already
> binding elsewhere in this skill (hard constraint 6, GATE 3, the Phase 7.7 soak counter,
> the rehearsal step, the runbook, `authorizations.md`). It invents no new criteria and
> makes no decisions — it shows what you already know and lets the customer read it
> without opening `migration-plan.md`.

## Scaffold at Phase 0, alongside `migration-plan.md`/`authorizations.md`

```bash
mkdir -p dashboard/assets
cp <skill>/shared/assets/dashboard.css dashboard/assets/
cp <skill>/shared/assets/dashboard.js  dashboard/assets/
cp <skill>/shared/templates/dashboard.html dashboard/index.html   # verbatim, no edits needed
: > dashboard/activity-log.jsonl                                  # must exist, even empty
```

🔴 **Both data files must exist from the first moment, not just `status.json`.**
`dashboard.js` fetches `status.json` and `activity-log.jsonl` together in one `Promise.all` — if
the log file is missing, that 404 fails the *entire* render, including the parts (progress,
cutover gates) that were otherwise fine. Create the empty file at scaffold time even before the
first activity line exists.

Seed `dashboard/status.json` with the phase list populated (all `pending`, `done:0`) and
`cutover_gates` populated (all `met:false`) the moment the plan is created — never leave the
dashboard folder empty while the engagement is underway. Print once, at Phase 0:

```
대시보드: cd dashboard && python3 -m http.server 8080   →   http://localhost:8080
```

Do **not** start this server yourself in the background — print the command and let the
customer (or you, on their explicit ask) run it. Same reasoning as never opening a port
without being asked.

This local setup is for active work — discovery through rehearsal. During Phase 7.7
(soak) specifically, `dashboard/` relocates to a private S3 bucket (the soak-stack's
`DashboardBucket` — `../patterns/cdk-stacks.md` §soak-stack.ts) alongside
`soak_check_lambda.py`, and gets viewed via a presigned URL instead of directly — see
execution-runbooks.md §Soak automation for why (the Lambda and the viewer must never be
looking at two different copies) and §Presigned-URL dashboard access for the exact
commands. Everything below this point (the JSON schemas, the update rule, the rendering
rules) is identical whether the files are sitting on local disk or in that bucket —
only *where* they live changes, never their shape.

## Presigned-URL viewing (soak window only)

`shared/scripts/generate_presigned_urls.py` (run once, at soak start) presigns GET URLs
for `index.html`, both files under `assets/`, `status.json`, and `activity-log.jsonl`, all
expiring together at the end of the soak window, then rewrites `index.html` so its CSS
`href`, JS `src`, and the two data-fetch targets are those absolute presigned URLs instead
of the relative paths used for local viewing. `dashboard.js` picks this up automatically:
it reads `window.DASHBOARD_STATUS_URL`/`window.DASHBOARD_LOG_URL` if the page defines them
(the rewritten `index.html` does, via a small inline `<script>` block the generator
injects just before the `dashboard.js` `<script>` tag) and falls back to the plain
`status.json`/`activity-log.jsonl` relative fetches otherwise — the same file works
unmodified for local dev and for the presigned/S3 case.

Two mechanics worth understanding before relying on this, both confirmed against a real
browser while building it, not just reasoned about:

- **Why the rewrite is necessary at all**: a relative `fetch('status.json')` issued from a
  page whose own URL happens to carry a presigned querystring does **not** inherit that
  querystring for the sibling request — relative URL resolution only carries over
  scheme/host/path, never the query string of the *document's own* URL. Against a bucket
  with all public access blocked, that lands as an unsigned GET, i.e. an unconditional
  403, indistinguishable at a glance from a real permissions problem. Every sub-resource
  the page loads needs its *own* presigned query string, embedded as an absolute URL.
- **Why there's no `?_=timestamp` cache-buster in the fetch calls**: S3's SigV4 signature
  for a presigned URL covers the exact query string present when it was signed — appending
  anything afterward, including an innocuous cache-busting parameter, changes the
  canonical request S3 recomputes on receipt and makes it reject the whole request.
  `fetch(url, { cache: 'no-store' })` already forces a real network request on every poll
  without needing a query-string trick, so the fix was simply to rely on that and drop the
  manual buster — which also happens to be simpler for the local-dev path.

**CORS**: the bucket needs a CORS rule allowing `GET` (see `cdk-stacks.md`'s
`DashboardBucket` — `AllowedMethods: [GET]`, permissive `AllowedOrigins`/`AllowedHeaders`,
since these are unauthenticated-by-header GETs with no cookies involved). Verify this in an
actual browser, not `curl` — preflight behavior and origin handling for a page whose own
origin *is* the bucket's origin is exactly the kind of thing that looks fine from the
command line and only shows its real behavior once a browser's fetch/CORS engine is
actually involved.

**Expiry**: presigned URLs work for repeated GETs until they expire — not single-use — so
the dashboard's 5-second polling keeps working against the same link for the entire soak
duration without anything being regenerated mid-window. Past expiry, confirmed against a
real browser (not just `curl`): every S3 GET — the initial page load and every 5-second
poll alike — comes back HTTP 403 with a small XML body (`<Error><Code>AccessDenied</Code>
<Message>Request has expired</Message>...`). If the link itself (`index.html`) has expired,
that's what the browser shows in place of the page — Chromium renders it with its built-in
XML viewer, so the customer sees a plain, readable "Request has expired" error, not a blank
screen or a silent hang. If the page was already open and only the *data* URLs
(`status.json`/`activity-log.jsonl`) have since expired, the page itself keeps rendering
and `dashboard.js`'s own `#conn-error` banner appears on the next failed poll instead. Both
are the intended failure mode — say so up front when handing over the link, so "the link
stopped working" on day 8 of a 7-day soak reads as expected, not as a bug. See
execution-runbooks.md's credential-longevity caveat for the one way this can fail *earlier*
than the stated expiry (signing with short-lived credentials for a multi-day tier).

## The update rule — one habit, not two

**Whenever you update `migration-plan.md`, also overwrite `dashboard/status.json` and
append one line to `dashboard/activity-log.jsonl`.** Same moments, same triggers you
already have: every GATE sign-off, every phase completion, every soak-report day, every
client-inventory row confirmed, rehearsal completion, runbook generation, A4b/A4 signature.
If it was worth a line in the plan, it is worth updating both dashboard files.

🔴 **`phases[]` must stay at exactly 11 entries, one per id, every time you overwrite
`status.json` — never 10, never 12.** The failure mode seen in practice: advancing several
phases in one turn and hand-authoring the new JSON, which re-adds the phases you just
completed near the top but leaves the original `pending` placeholder for those same ids
sitting further down, unremoved — the array now has 16 entries with `4-5`/`6`/`7` etc.
appearing twice (once `done`, once still `pending`). `dashboard.js` does not dedup by id;
it renders the raw array, so the customer sees each duplicated phase listed twice with
contradictory status, and the phase-count tile (`done ÷ length`) is thrown off by the extra
entries. Load the current file, mutate the `status`/`done`/`total` fields **in place** on
the existing objects for whichever phase(s) changed, and write back the same 11 objects —
never compose the array from scratch or insert a fresh object for a phase id that already
has one. Before moving on, re-open the file you just wrote and confirm: 11 entries, 11
distinct ids, no id repeated.

## `status.json` — full snapshot, OVERWRITTEN every time (never appended)

```json
{
  "engagement": "{prefix}",
  "updated_at": "2026-08-31T14:59:37+09:00",
  "mode": "2",
  "lang": "ko",
  "overall_progress_pct": 62,
  "current_phase": "7.7",
  "current_activity": "병행 가동 — Day 3 리포트 생성 중",
  "phases": [
    {"id": "0",   "name": "사전 점검",        "status": "done",        "done": 4, "total": 4},
    {"id": "1",   "name": "조사 입력 (GATE 1)", "status": "done",       "done": 18, "total": 18},
    {"id": "2",   "name": "환경 조사",          "status": "done",       "done": 6, "total": 6},
    {"id": "3",   "name": "방식 선정 (GATE 2)", "status": "done",       "done": 1, "total": 1},
    {"id": "4-5", "name": "타깃 구축",          "status": "done",       "done": 5, "total": 5},
    {"id": "6",   "name": "데이터 이전",        "status": "done",       "done": 1, "total": 1},
    {"id": "7",   "name": "검증 (GATE 3)",      "status": "done",       "done": 5, "total": 5},
    {"id": "7.5", "name": "클라이언트 인벤토리", "status": "in_progress","done": 4, "total": 6},
    {"id": "7.7", "name": "병행 가동",          "status": "in_progress","done": 3, "total": 7, "note": "3/7 연속 green"},
    {"id": "8",   "name": "컷오버 인수인계",    "status": "pending",     "done": 0, "total": 5},
    {"id": "9",   "name": "사후 정리",          "status": "pending",     "done": 0, "total": 3}
  ],
  "cutover_gates": [
    {"key": "client_inventory", "label": "클라이언트 인벤토리 100%",   "met": false, "detail": "4/6 확인 (Phase 7.5, 하드 제약 6)"},
    {"key": "validation",       "label": "검증 요건 충족 (GATE 3)",    "met": true,  "detail": "행 수·체크섬·스키마 객체 일치 — 선택된 심층 검증 없음"},
    {"key": "soak",             "label": "병행 가동 요건 충족",        "met": false, "detail": "3/7일 연속 green, 종료 서명 전 (Phase 7.7)"},
    {"key": "rehearsal",        "label": "리허설 요건 충족",           "met": false, "detail": "미실시 — 클론 리허설 1회 예정"},
    {"key": "runbook",          "label": "런북 · 롤백 준비",           "met": false, "detail": "생성 대기"},
    {"key": "approvals",        "label": "필요 승인 서명",             "met": false, "detail": "GATE 1·2 완료, A4b 인수인계 승인 대기 (authorizations.md)"}
  ],
  "cutover_ready": false,
  "migration_objects": {
    "tables": {
      "total": 4, "loaded": 4, "validated": 3,
      "items": [
        {"name": "customers",    "rows_source": 200000,  "rows_target": 200000,  "checksum_match": true,  "status": "validated"},
        {"name": "orders",       "rows_source": 800000,  "rows_target": 800000,  "checksum_match": true,  "status": "validated"},
        {"name": "order_items",  "rows_source": 2000000, "rows_target": 2000000, "checksum_match": true,  "status": "validated"},
        {"name": "seed_numbers", "rows_source": 1000,    "rows_target": 1000,    "checksum_match": null,  "status": "loaded"}
      ]
    },
    "views":      {"total": 1, "created": 1, "items": [{"name": "v_customer_order_totals", "status": "created"}]},
    "procedures": {"total": 1, "created": 1, "items": [{"name": "sp_recent_orders",         "status": "created"}]},
    "functions":  {"total": 0, "created": 0, "items": []},
    "triggers":   {"total": 1, "created": 0, "items": [{"name": "trg_orders_default_status", "status": "deferred", "note": "created last, immediately before cutover — execution-runbooks.md load order"}]},
    "events":     {"total": 0, "created": 0, "items": []}
  },
  "soak": {
    "n_total": 3,
    "consecutive_green": 2,
    "state": "active",
    "last_checked_at": "2026-09-02T09:00:11+00:00",
    "days": [
      {"date": "2026-09-01", "overall": "red", "needs_agent_review": true,
       "checks": {"row_count": true, "checksum": true, "alarms": true, "headroom": true, "schema_drift": true, "replication_lag": false, "replication_errors": "not_applicable", "customer_test_suite": "not_applicable"},
       "detail": {}},
      {"date": "2026-09-02", "overall": "green", "needs_agent_review": false,
       "checks": {"row_count": true, "checksum": true, "alarms": true, "headroom": true, "schema_drift": true, "replication_lag": true, "replication_errors": "not_applicable", "customer_test_suite": "not_applicable"},
       "detail": {}}
    ]
  }
}
```

Field notes:
- `lang` — an ISO 639-1 code (`"ko"`, `"en"`, ...) for the **page's own static UI chrome**
  (tile labels, section headers, table column headers, badges, empty-state and
  connection-error messages) — separate from the phase names/gate labels/activity text you
  write yourself, which already follow the Language rule naturally since you write them
  fresh each time. `dashboard.js` ships an `en`/`ko` label dictionary and defaults to `en`
  if `lang` is absent (so a status.json written before this field existed still renders
  correctly, just in English rather than the engagement's language). Set this once at
  Phase 0 scaffold time to match the conversation language you're actually operating in —
  don't leave it defaulted to `en` for a Korean-language engagement.
- `phases[]` — one entry per `SKILL.md` phase (`0,1,2,3,4-5,6,7,7.5,7.7,8,9`). `status` is
  exactly one of `done|in_progress|pending` (a phase you have not started is `pending`, not
  omitted — the page always shows all 11 rows).
- `cutover_gates[]` — exactly the six keys above, every engagement, every mode. In Mode 1
  every gate stays `met:false` (there is no target to be ready) and `cutover_ready` stays
  `false` — the dashboard is still valid to show, it just reports nothing is gate-eligible.

**When each gate is allowed to flip to `met:true`** — a gate is a *requirement being resolved*,
not necessarily *work being performed*: a customer-signed, dated waiver for a skipped or
shortened step satisfies the gate exactly as completing the step would, because
`engagement-safety.md`'s waiver protocol is itself a valid way to close a requirement. Never
mark a gate `met:true` for either reason without one of these:

| key | `met:true` only when | never mark true just because |
|---|---|---|
| `client_inventory` | Phase 7.5 table has every client at ✅✅ (hard constraint 6) | most clients are done — this is all-or-nothing |
| `validation` | GATE 3 evidence block confirmed **and**, if the chosen validation depth includes a customer test suite (Q18), its final pre-cutover sign-off is also in (`customer-test-integration.md`) | row counts/checksums pass but the customer's own suite hasn't run its final pass |
| `soak` | the chosen parallel-run parameter is satisfied: N consecutive greens **and** the soak-exit block confirmed (`SKILL.md` Phase 7.7 requires both) — **or** a dated waiver block in `authorizations.md` for skipping/shortening it | N greens reached but soak-exit isn't confirmed yet; also applies to Mode 2 handover depth (b), where the customer runs the soak themselves — stays `false` until they report it done or confirm the skip waiver |
| `rehearsal` | the chosen rehearsal parameter is satisfied (one clone rehearsal done, or repeat-until-converged reached) — **or** a dated waiver for rehearsal `none` | a rehearsal is scheduled but hasn't produced measured timings yet |
| `runbook` | `cutover-runbook.md` + `rollback-runbook.md` exist with zero placeholders **and** (reverse replication created + connection-tested, **or** the alternative rollback strategy + RPO acknowledgment block is confirmed) — hard constraint 6's rollback-path requirement | the runbook file exists but reverse replication hasn't been connection-tested |
| `approvals` | every applicable `authorizations.md` block for this point is confirmed: GATE 1 (in `discovery-questions.md`), GATE 2, GATE 3, **and** — Mode 2: **A4b** handover acceptance; Mode 3: **A4** cutover authorization | GATE 1/2 are confirmed but A4b/A4 (which happens at Phase 8, near the end) is still pending — this is why `approvals` is usually the last gate to flip, not an early one |

- `cutover_ready` — **you compute this** as a plain boolean AND over `cutover_gates[].met`.
  The page never recomputes it and never infers readiness from `overall_progress_pct` — a
  94% progress bar next to one unmet gate must still show "아직 컷오버 불가." Never derive
  one from the other.
- `overall_progress_pct` is informational only — sum of `done` across all phases ÷ sum of
  `total`, or your own reasonable estimate early on. It is deliberately **not** part of the
  cutover decision.
- `soak` — rendered as its own prominent dashboard section, not folded into the phases
  list, because this is the one gate stakeholders ask about most. `last_checked_at` is set
  by `soak_check.py` on every run, and the dashboard flags it if more than 36 hours pass
  without an update — a missed scheduled run (host down, cron didn't fire) otherwise looks
  identical to "waiting for tomorrow." This is separate from the 15-minute chat-staleness
  badge, which assumes an active session and would misfire on a normal once-daily cadence.
  `n_total` and `consecutive_green` drive the "Day k / N" counter; `days[]` holds one entry
  per period, each with the 8-check result set from `shared/templates/soak-report.md`.
  Each check value is one of **four** states, not just true/false:
  - `true`/`false` — actually measured, passed or failed.
  - `null` — something IS configured for this check on this engagement but the data came
    back missing/unreachable/`INSUFFICIENT_DATA` this run — counts as
    `needs_agent_review`, **never** a silent pass. This is the state that used to be
    permanently stuck for `replication_lag`/`customer_test_suite` before both scripts
    measured/gated them properly — see below.
  - `"not_applicable"` — genuinely nothing configured for this check on this engagement
    (no DMS task in play, no customer test suite per discovery Q18). Excluded from
    **both** the green calculation and `needs_agent_review` — this is what actually lets
    a clean engagement reach `soak.state: "complete"` instead of being stuck forever on a
    check that was never going to apply.
  `replication_lag` is measured from CloudWatch `CDCLatencySource`/`CDCLatencyTarget`
  when a DMS task is configured, or `SHOW REPLICA STATUS`/a PostgreSQL replay-lag query
  for non-DMS replication — `"not_applicable"` only when this engagement has no
  replication mechanism wired in at all. `replication_errors` pulls DMS task stats
  (`TablesErrored`, task status, last failure message) when a DMS task is configured,
  `"not_applicable"` otherwise. `customer_test_suite` can never be measured
  automatically (it needs the customer's own external test run) — it's `"not_applicable"`
  unless a suite was actually documented as provided at discovery Q18, in which case it
  stays `null` (a real, intentional "needs review" — waiting on that result) until the
  agent or the customer supplies `true`/`false`. Never guess any of these three.
  `shared/scripts/soak_check.py` (reference implementation, run by hand) and
  `shared/scripts/soak_check_lambda.py` (the production path — VPC-attached Lambda on an
  EventBridge Scheduler cadence, writing straight into this bucket) both run every
  mechanical check (row count, checksum, schema drift, alarm state, storage headroom,
  replication lag, replication errors) against source and target directly, using a
  dedicated SELECT-only credential — never the admin/master secret — and write this
  object themselves, no agent invocation needed for the routine all-green case; see
  `execution-runbooks.md` §Soak automation for how to configure and deploy either. A day
  it writes with `needs_agent_review: true` (any RED, or any check that came back `null`)
  must get you to actually look at it before the next period — the dashboard surfaces
  this as a standalone banner, not just a colored cell. If soak is waived, set
  `{"waived": true, "waived_reason": "..."}` instead of the fields above — the page
  renders the waiver reason plainly rather than an empty section.
- `migration_objects` — the detail behind the phase percentages: exactly how many of each
  schema-object type exist, how many are done, and per-item evidence (row counts, checksum
  match) rather than just a rollup number. One key per object type your source actually has
  (`tables`, `views`, `procedures`, `functions`, `triggers`, `events` — omit a type entirely
  if the source has zero of it, don't pad with empty totals). Populate progressively, not
  all at once:
  - **Phase 2** (assessment): set every `total` from the discovered object inventory, all
    `items` at `status:"pending"`, no row counts yet.
  - **Phase 6** (data load): as each table finishes loading, set its `rows_target` and
    `status:"loaded"`.
  - **Phase 7** (validation, GATE 3): as each table's checksum is confirmed, set
    `checksum_match` and `status:"validated"`. A table with `checksum_match:false` is a
    validation failure, not a display state — it blocks the `validation` cutover gate.
  - **Views/procedures/functions/events**: `status:"created"` the moment their DDL is
    applied on the target — this generally happens once, right after the data load
    (`execution-runbooks.md` load order).
  - **Triggers**: stay at `status:"deferred"` (not `"pending"` — this is the *expected*
    state, not something stuck) until immediately before cutover, per the same load-order
    guidance. Only flip to `"created"` as part of the cutover sequence itself.
  - This section updates on the same "whenever `migration-plan.md` changes" trigger as
    everything else on this page — no separate habit to remember.

## `activity-log.jsonl` — JSON Lines, APPEND ONLY, never rewritten

One line per event, oldest first in the file (the page reverses it for display):

```json
{"time": "2026-08-31T14:42:05+09:00", "phase": "7.7", "title": "병행 가동 Day 3 리포트", "action": "표본 3개 테이블 행수·체크섬 재확인, 복제 지연 1.8s", "result": "success", "detail": "연속 3/7 green", "files": ["dashboard/../soak-report-day3.md"]}
```

`result` is one of `success|in_progress|blocked` for hand-written entries (maps to the
page's icon/color — blocked is reserved for an actual abort/rollback trigger, not a normal
in-progress step). The automated soak-check scripts (`soak_check.py`/
`soak_check_lambda.py`) write their own daily verdict here too, mapped onto the same
vocabulary — `green`→`success`, `red`→`blocked` — never the raw `green`/`red` strings
themselves (those belong to `status.json`'s `soak.days[].overall`, a separate field with
its own vocabulary; `dashboard.js` also recognizes `green`/`red` defensively in `result`
so a log line written by an older version of those scripts still renders with the correct
icon/color instead of silently defaulting to a green checkmark). `files` is optional, an
array of paths relative to the engagement root.

**Idempotent per calendar day.** EventBridge Scheduler retries are at-least-once, and a
human can re-run `soak_check.py` by hand for a day already checked — both scripts detect
an existing `activity-log.jsonl` line for the same date (matched on `phase:"7.7"` +
`date`) and overwrite it in place rather than appending a duplicate; `status.json`'s
`soak.days[]` gets the same treatment, and `consecutive_green` is always recomputed from
`days[]` itself (the trailing run of green entries), never incremented — so a retry can
never double-count or drift.

**Why JSON Lines instead of reading `migration-plan.md` directly**: the page must never
regex-parse prose tables — fragile the moment wording drifts. Two small structured files,
written by the same hand that already keeps the plan current, cost nothing extra and never
break the render.

## What this page deliberately does not do

No websocket/push (client-side polling every 5s is enough for a human watching a status
page), no auth/login of its own — outside the soak window this is localhost-only,
single-user, and not deployed anywhere; during the soak window, access control is the
presigned URL itself (only someone holding the link can reach the private bucket, and only
until it expires) rather than any login the page implements. No multi-engagement view (one
`dashboard/` per engagement, same as `migration-plan.md`), and it never starts its own
server process.
