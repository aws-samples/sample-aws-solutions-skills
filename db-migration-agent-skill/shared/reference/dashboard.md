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
cp <skill>/shared/templates/dashboard.html dashboard/index.html   # then fill {prefix}/{mode}
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

## The update rule — one habit, not two

**Whenever you update `migration-plan.md`, also overwrite `dashboard/status.json` and
append one line to `dashboard/activity-log.jsonl`.** Same moments, same triggers you
already have: every GATE sign-off, every phase completion, every soak-report day, every
client-inventory row confirmed, rehearsal completion, runbook generation, A4b/A4 signature.
If it was worth a line in the plan, it is worth updating both dashboard files.

## `status.json` — full snapshot, OVERWRITTEN every time (never appended)

```json
{
  "engagement": "{prefix}",
  "updated_at": "2026-08-31T14:59:37+09:00",
  "mode": "2",
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
  "cutover_ready": false
}
```

Field notes:
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
| `validation` | GATE 3 evidence signed **and**, if the chosen validation depth includes a customer test suite (Q18), its final pre-cutover sign-off is also in (`customer-test-integration.md`) | row counts/checksums pass but the customer's own suite hasn't run its final pass |
| `soak` | the chosen parallel-run parameter is satisfied: N consecutive greens **and** the soak-exit row signed (`SKILL.md` Phase 7.7 requires both) — **or** a dated waiver in `authorizations.md` for skipping/shortening it | N greens reached but soak-exit isn't signed yet; also applies to Mode 2 handover depth (b), where the customer runs the soak themselves — stays `false` until they report it done or sign the skip waiver |
| `rehearsal` | the chosen rehearsal parameter is satisfied (one clone rehearsal done, or repeat-until-converged reached) — **or** a dated waiver for rehearsal `none` | a rehearsal is scheduled but hasn't produced measured timings yet |
| `runbook` | `cutover-runbook.md` + `rollback-runbook.md` exist with zero placeholders **and** (reverse replication created + connection-tested, **or** the alternative rollback strategy + RPO acknowledgment is signed) — hard constraint 6's rollback-path requirement | the runbook file exists but reverse replication hasn't been connection-tested |
| `approvals` | every applicable `authorizations.md` row for this point is signed: GATE 1, GATE 2, GATE 3, **and** — Mode 2: **A4b** handover acceptance; Mode 3: **A4** cutover authorization | GATE 1/2 are signed but A4b/A4 (which happens at Phase 8, near the end) is still pending — this is why `approvals` is usually the last gate to flip, not an early one |

- `cutover_ready` — **you compute this** as a plain boolean AND over `cutover_gates[].met`.
  The page never recomputes it and never infers readiness from `overall_progress_pct` — a
  94% progress bar next to one unmet gate must still show "아직 컷오버 불가." Never derive
  one from the other.
- `overall_progress_pct` is informational only — sum of `done` across all phases ÷ sum of
  `total`, or your own reasonable estimate early on. It is deliberately **not** part of the
  cutover decision.

## `activity-log.jsonl` — JSON Lines, APPEND ONLY, never rewritten

One line per event, oldest first in the file (the page reverses it for display):

```json
{"time": "2026-08-31T14:42:05+09:00", "phase": "7.7", "title": "병행 가동 Day 3 리포트", "action": "표본 3개 테이블 행수·체크섬 재확인, 복제 지연 1.8s", "result": "success", "detail": "연속 3/7 green", "files": ["dashboard/../soak-report-day3.md"]}
```

`result` is one of `success|in_progress|blocked` (maps to the page's icon/color — blocked is
reserved for an actual abort/rollback trigger, not a normal in-progress step). `files` is
optional, an array of paths relative to the engagement root.

**Why JSON Lines instead of reading `migration-plan.md` directly**: the page must never
regex-parse prose tables — fragile the moment wording drifts. Two small structured files,
written by the same hand that already keeps the plan current, cost nothing extra and never
break the render.

## What this page deliberately does not do

No websocket/push (client-side polling every 5s is enough for a human watching a status
page), no auth/login (localhost-only, single user — do not deploy this anywhere), no
multi-engagement view (one `dashboard/` per engagement, same as `migration-plan.md`), and it
never starts its own server process.
