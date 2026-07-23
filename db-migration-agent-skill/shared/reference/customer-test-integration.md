# Integrating the Customer's Own Tests (Discovery Q18 → Rehearsal/Soak Gates)

> How to actually *run* the customer's existing tests against the target. The tests live
> in the customer's systems (CI, QA tools, repos) — **never ask them to paste test code
> into chat.** The working principle: **their tests, their runner, your endpoint.** The
> skill supplies a target endpoint + credentials; the customer's existing machinery
> executes; the pass/fail evidence flows back into the soak report.

## The four integration patterns (pick per what the customer has)

| Customer has | Integration | Who runs it | Evidence back |
|--------------|-------------|-------------|---------------|
| **CI pipeline** (Jenkins/GitHub Actions/GitLab/CodePipeline) with a test stage that takes a DB endpoint/profile | Ask for a **parameterized run**: they trigger the existing job with the DB host/secret overridden to the target (most test stages already read `DB_HOST`/JDBC URL from env or a profile). If the runner is outside the VPC, provide the private endpoint via their existing runner network path — never a public endpoint | Customer's CI | The CI run URL + pass/fail counts, pasted or screenshotted into the soak report row |
| **A test/staging environment** of the application | Repoint the *staging app's* DB config at the target (same client-repoint mechanics as Phase 7.5, applied to staging) and let QA run their normal UAT pass | Customer QA | QA sign-off note recorded in `authorizations.md`; defects triaged migration-related vs pre-existing |
| **Runnable scripts/suites in a repo** (pytest/JUnit/k6/JMeter/SQL scripts) but no convenient runner in-VPC | Stand up a **throwaway test-runner host inside the migration VPC** (small EC2/ECS task, SSM-only, torn down with the engagement); customer grants read access to the repo or supplies the artifact; the agent executes verbatim — **never edit their tests**; a failing test is a finding, not something to fix | Agent, on the runner | Full run log attached to the plan; summary row in the soak report |
| **Nothing formal** (very common) | Do NOT fabricate a "suite" and call it theirs. Offer two honest options: (a) the skill's built-in minimum — per-major-table CRUD round-trip + top-N query replay against the baseline (validation-patterns.md); (b) a **10-minute interview**: "name the 3–5 business actions that must work" (place order, refund, monthly report...) → script exactly those as named smoke checks the customer reviews and approves as *their* acceptance list | Agent | The approved checklist + results; Tier 3 without a real suite = recorded waiver |

## Rules

1. **Read-only vs writing tests.** Ask which the suite is. Write-tests are fine against a
   rehearsal clone or a soaking target **before** it becomes authoritative, but their
   writes must be excluded from row-count/checksum comparisons (tag a test window, or
   diff-check only outside it). Never run customer write-tests against the target after
   cutover authorization without noting the window.
2. **Their tests are immutable.** If a test fails on the target, the finding goes in the
   plan (migration defect? pre-existing failure? environment config?) — the agent never
   "fixes" the test to make it pass. Ask the customer to run the same suite against the
   *source* once, early, to establish which failures are pre-existing.
3. **Where results live**: each suite execution = a row in the daily soak report
   (`{suite: n pass / n fail}`) + the run log/URL referenced in `migration-plan.md`
   Phase 7.7. Tier 3: the final pre-cutover run is a named sign-off row in
   `authorizations.md`.
4. **Load tests** (k6/JMeter/Gatling/nGrinder): run against the target during the soak at
   production-like concurrency — this is the Tier 3 performance-validation input. Compare
   p95/p99 against the source baseline from Phase 2, not against absolute thresholds.
5. **Secrets**: the runner gets the target credentials the same way everything else does
   — a scoped Secrets Manager secret, never pasted into a pipeline variable in chat.
