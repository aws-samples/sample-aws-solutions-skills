# Dashboard Design Patterns, Gotchas & Validation

## 0. Dashboard structure — single dashboard + tabs vs separate (ask BEFORE the plan)

Before presenting the sheet/visual plan (§1), decide how many dashboards. Ask once, in the user's language:

```
How should the dashboards be organized for this project?
  a) Single dashboard + per-topic tabs (sheets) (recommended ✓ — single URL, shared filters, manage once)
  b) Separate dashboards per topic (when per-team access control differs, or sheet count exceeds 8)
```

**Default = (a)** single dashboard with per-topic tabs (sheets): one URL, shared filters, one thing to manage and refresh. Only split into separate dashboards when:
- Different teams need different RLS / Space access
- Different refresh cadences are required
- Sheet count exceeds ~8
- An executive needs a 1-page summary deployed separately

---

## 1. Dashboard plan — SHOW TO USER and get approval BEFORE building

Before generating ANY dashboard definition, CDK, or temp-validation dashboard, present the plan to the user and wait for approval. This is the **one place** in the consumption flow where user feedback is required before execution — the user may want to adjust sheets, add/remove visuals, or change the layout, and it is far cheaper to change the plan than a deployed dashboard.

Present it like this (in the user's language):

```
Dashboard plan:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sheet 1: [name]
  - Visual 1: [type] — [description]
  - Visual 2: [type] — [description]
  ...
Sheet 2: [name]
  - Visual 1: ...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dataset: [name] (SPICE, refresh: daily 04:00 KST)
Filters: [list of common filters]
```

**Wait for explicit user approval before building.** Only after the user confirms (or adjusts) the plan do you generate the definition, run STRICT validation, and deploy.

> **Recommended for dashboards with > 5 visuals: UI-first, then export.** Build the dashboard interactively in the Quick Sight UI (drag visuals, configure field wells, tweak formatting), then export the definition and check it into the CDK project as the source of truth:
>
> ```bash
> aws quicksight describe-dashboard-definition \
>   --aws-account-id {account_id} \
>   --dashboard-id {dashboard_id} \
>   --region {region} > dashboards/{name}-definition.json
> ```
>
> Inline-from-scratch CDK definitions should be reserved for **simple dashboards (≤ 5 visuals) or demo scenarios**. The Quick Sight definition schema is large and its validation errors are not actionable — building in the UI is dramatically faster than guessing the right shape from the schema.

The skill picks layouts based on the customer's domain, derived from `business_questions`. If the domain is ambiguous, ask the user once and pick.

---

## 2. Domain layout patterns

### Manufacturing (quality management)

| Component | Visual | Notes |
|---|---|---|
| Top row | KPI cards | Overall defect rate, total inspections, pass rate, MoM change |
| Mid row L | Time series | Monthly defect rate trend with reference line at target threshold |
| Mid row R | Bar chart | Defect count by inspection item, TOP 5 |
| Bottom row | Table | Vendor-level defect comparison, sortable |
| Filters | Date range, vendor, product, defect type | Persist across visuals |
| Drill-down | Lot → Process → Defect type | Use parameters + actions |

### Retail (sales / inventory)

| Component | Visual |
|---|---|
| Top row | Total revenue, order count, AOV, conversion rate |
| Mid row L | Daily/weekly sales time series |
| Mid row R | Sankey: traffic source → category → conversion |
| Bottom L | Geo map: regional sales |
| Bottom R | Product performance ranking table |

### General (any domain) — fallback

| Component | Visual |
|---|---|
| Header | 3–5 KPI summary cards |
| Primary | Time series (the main metric over time) |
| Secondary | Bar or pie comparison (the main metric by category) |
| Detail | Table with search and filter |
| Always | Date filter, category filter, export-to-CSV button |

---

## 3. Common dashboard definition gotchas

These are the validation errors that bite every Data Lab build. Most surface only at deploy time and trigger full CFN rollback — fix them in the definition before deploying.

| Issue | Symptom | Fix |
|---|---|---|
| `KPIVisual` *comparison/trend* requires a `TargetValues` or `TrendGroups` well | The comparison number, progress bar, trend arrow, or sparkline silently renders nothing | A bare KPI (`Values` only) is valid and deploys fine — `TargetValues`/`TrendGroups` are both optional in `KPIFieldWells`. But the moment you ask for a comparison (`Comparison`), a progress bar, `TrendArrows`, or a `Sparkline`, you must supply the matching well, or that feature no-ops. (Verified: every plain card in `dashboard-definitions.md` — `kpi-5/7/27/…` — ships with empty `TargetValues`/`TrendGroups`.) |
| `NumericalMeasureField` rejects STRING columns | "column type mismatch" even when the aggregate is `COUNT` | Use `categoricalMeasureField` for STRING columns (with `COUNT` aggregation); `numericalMeasureField` is INTEGER/DECIMAL only |
| `formatConfiguration` double-wrapping | "unexpected property" or schema-mismatch error | Correct shape: `{ formatConfiguration: { percentageDisplayFormatConfiguration: {…} } }`. Don't nest a second `formatConfiguration` inside. |
| `StringFilter` requires explicit `nullOption` | Validation error on filter | Always include `nullOption: 'ALL_VALUES'` (or `NON_NULLS_ONLY` if intentional) |

### Extended visual schema catalog (verified constraints)

Beyond the standard Bar/Line/Pie/Table/KPI set, these visual types have non-obvious constraints that fail validation or render-time:

| Visual | Key constraint | Safe values |
|--------|---------------|-------------|
| GaugeChart | `ArcAngle` | ONLY `180` / `270` / `300` / `330` / `360` — arbitrary values rejected |
| GaugeChart | Target field | MANDATORY **meaningful** target — without one the agent fills garbage (a `line_count` of 11 was used as a "85% utilization" target). No real target → don't use a gauge. |
| KPI sparkline | `TrendGroups` date | Must come from the SAME dataset as `Values` — NOT the single-row KPI dataset (§10) |
| InsightVisual | `CustomNarrative` | Must be valid XML — plain text causes `Content not allowed in prolog` |
| InsightVisual | `TopBottomRanked` | `ResultSize` is REQUIRED |
| TextBox | `Content` | Only the specific XML schema — no plain text or arbitrary HTML |
| CategoricalMeasureField | `FormatConfiguration` | `{ NumericFormatConfiguration: … }` — different shape from `NumericalMeasureField`! |
| CreateTheme `FontFamilies` | Only generic fallback fonts | Arbitrary fonts (e.g. `Noto Sans KR`) are REJECTED |

> **Agent pattern for unfamiliar visuals:** for any visual type NOT in the standard catalog, do a STRICT probe with a minimal temp dashboard (§4) FIRST, capture the validated definition shape, then incorporate it — don't guess the schema inline.

---

## 4. Pre-flight STRICT validation (no deploy needed)

Run validation against a temp dashboard before committing the definition to CDK. Same validation engine as deploy; runs in ~2 seconds; no CFN rollback.

```bash
aws quicksight create-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id "validation-temp-$(date +%s)" \
  --name "Validation Test" \
  --definition file://dashboards/quality-definition.json \
  --validation-strategy STRICT \
  --region {region}

# Check the result, then delete:
aws quicksight delete-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id "validation-temp-..." \
  --region {region}
```

Iterate the definition against this command until clean, then commit and `cdk deploy`.

---

## 5. Post-STRICT verification (catches what STRICT misses)

`--validation-strategy STRICT` does NOT catch everything. Some errors — most commonly `COLUMN_TYPE_INCOMPATIBLE` (e.g. a DATE column in a `CategoricalDimensionField`, or a STRING in a numerical measure) — **pass STRICT but fail at render time**. After STRICT passes and the dashboard is created, always verify the actual dashboard version status:

```bash
aws quicksight describe-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id {dashboard_id} \
  --region {region} \
  --query 'Dashboard.Version.{Status:Status,Errors:Errors}'
```

If `Status != CREATION_SUCCESSFUL` or `Errors != []`, the dashboard has render-time issues STRICT didn't catch (typically column-type mismatches in field wells). Read the `Errors` array, fix the offending field well's type, and redeploy — do NOT treat "STRICT passed" as "dashboard works."

---

## 6. Dashboard update flow (3 separate API calls)

Updating an existing dashboard is NOT a single call. `update-dashboard` creates a new **draft** version that is neither visible nor published — you must follow it with two more calls:

1. **`update-dashboard`** — creates a new version (draft, not visible to readers).
2. **`update-dashboard-permissions`** — grants access to users/groups. Permissions are **NOT** inherited from the original create; skipping this means the dashboard exists but users can't see it.
3. **`update-dashboard-published-version`** — promotes the new version to live. Skipping this means users keep seeing the **OLD** version.

```bash
aws quicksight update-dashboard --aws-account-id {account_id} --dashboard-id {id} \
  --name "..." --definition file://dashboards/{name}.json --region {region}
# capture the returned VersionArn / version number

aws quicksight update-dashboard-permissions --aws-account-id {account_id} --dashboard-id {id} \
  --grant-permissions Principal={group_arn},Actions=... --region {region}

aws quicksight update-dashboard-published-version --aws-account-id {account_id} --dashboard-id {id} \
  --version-number {n} --region {region}
```

Missing step 2 → dashboard exists but users can't see it. Missing step 3 → users see the OLD version.

---

## 7. Versioning

Dashboards have a `versionDescription` field. Use it. Bump on every deploy:
"v3 — added vendor drill-down per quality team request, 2026-01-15".

The `CfnDashboard` CDK block itself → `quicksight-cdk.md` §8.

---

## 8. 🔴 KPI Numerical Accuracy Verification (MANDATORY post-deploy step)

> **"Validation passed ≠ correct answer."** Row-count > 0, null-checks, `--validation-strategy STRICT`, and `describe-dashboard` `CREATION_SUCCESSFUL` all confirm the dashboard is **structurally** valid. They say NOTHING about whether the displayed numbers are **correct**. In a real Data Lab build, every one of those passed while **6 KPIs showed wrong numbers** to the user. This step is the gap-closer — do NOT skip it, and do NOT report a dashboard as done until it passes.

### Procedure — for EVERY KPI visual in the dashboard

After STRICT validation passes and the dashboard renders:

1. **Identify the metric** being displayed (e.g. "total production", "defect rate", "delayed order count").
2. **Write the SAME aggregation as a direct Athena query against the SOURCE table** (the base table, not the mart).
3. **Compare three numbers:**
   - Source-table result
   - Mart-table result
   - Dashboard-displayed value
4. **All three MUST match within 1% tolerance.** If any disagree, investigate by which pair diverges:
   - **Source ≠ Mart** → row loss or value corruption in the ETL (date-parse miss, cast-to-NULL, join miss, NFD filename, encoding). Fix in the **pipeline** (`data-platform-pipeline` → dirty-data handling) and re-run; do NOT patch around it in Quick Sight.
   - **Mart ≠ Dashboard** → grain duplication on SUM (multi-grain mart feeding a KPI card → use the single-row KPI dataset, §10), or the wrong aggregation function in the field well (`COUNT` vs `COUNT(DISTINCT)`, SUM over a column not listed in the mart's `sum_safe_columns`).

```sql
-- Verify "total production" KPI matches the SOURCE
SELECT SUM(quantity_good) AS source_total FROM base_mes_production;
-- Compare with the MART
SELECT SUM(daily_production_qty) AS mart_total FROM mart_daily_production;
-- source_total ≠ mart_total → investigate ETL row loss (date parse? null filter? join miss?)
-- mart_total ≠ dashboard → grain duplication on SUM, or wrong agg function in the field well
```

### The six root-cause patterns (each shipped a wrong number despite passing validation)

| KPI | Displayed (wrong) | Correct | Root cause | Where to fix |
|---|---|---|---|---|
| QMEL notifications | 3,527 | 426 | Mart grain (material × defect_code) → 8.3× duplication on SUM | Single-row KPI dataset (§10) / pipeline grain |
| Total production | 2,022,330 | 2,399,700 | 3rd date format (`yyyy/M/d H:m:s`) unparsed → 16% row loss | Pipeline date-parse chain |
| Delayed orders | 703 | 439 | `COUNT(delay_days)` counted completed orders | `WHERE is_on_time = false` |
| Defect materials | 165 | 20 | `COUNT` instead of `COUNT(DISTINCT material_key)` | `COUNT(DISTINCT …)` in the KPI dataset |
| Avg delay days | 3.69 | 9.78 | Early completions (negative values) diluted the average | Exclude negatives: `AVG(CASE WHEN delay_days > 0 …)` |
| Gauge utilization target | 11 (line_count) | 85% | Meaningless column used as the gauge target | Provide a real target or omit the gauge (§3 extended catalog) |

> Read `platform.yaml` for each mart's `grain`, `sum_safe_columns`, `single_row`, and `validation_sql` — the pipeline records them precisely so the consumption layer knows which columns are safe to SUM and which ground-truth aggregate each mart should reproduce.

⚠️ **"STRICT pass + render success + row > 0" is NOT enough. Numbers must be CORRECT.**

---

## 9. Layout integrity check (catches "structurally valid but visually broken")

After STRICT + render + numerical accuracy, parse the deployed dashboard definition (`describe-dashboard-definition`) and verify the layout:

- **No visual overlaps** — for any two visuals on the same rows, `column + columnSpan` must not exceed the 36-column grid (and they must not occupy overlapping column ranges).
- **No orphan whitespace** > ~10% of the grid (large empty bands read as broken).
- **Every visual fits within the 36-column grid** (`column + columnSpan ≤ 36`).
- **Consistent visual heights within a row** (ragged heights look unfinished).

A dashboard can pass STRICT and render yet still be visually broken (overlapping tiles, a visual hanging off the grid, half the sheet empty). This check is mechanical — do it before handoff.

---

## 10. KPI cards: single-row pre-aggregated dataset

**Problem:** A KPI card aggregates over the ENTIRE dataset with SUM/COUNT. If the dataset is a multi-grain mart (multiple rows per entity), the aggregation doubles/triples the real value — this is exactly how QMEL notifications showed 3,527 instead of 426 (8.3× the rows).

**Pattern:** point KPI cards at a dedicated **single-row** dataset where every measure is pre-aggregated with the CORRECT function. The pipeline builds this as `mart_kpi_summary` (one row); see `data-platform-pipeline` → `reference/scripts.md` → single-row KPI mart. The consumption dataset is a plain custom-SQL `SELECT * FROM mart_kpi_summary`:

```sql
SELECT
    COUNT(DISTINCT CASE WHEN result='fail' THEN material_key END) AS defect_materials, -- NOT COUNT(material_key)
    SUM(CASE WHEN result='fail' THEN 1 ELSE 0 END)                AS total_defects,
    COUNT(CASE WHEN is_on_time = false THEN 1 END)                AS delayed_orders,
    AVG(CASE WHEN delay_days > 0 THEN delay_days END)             AS avg_delay_days_late
FROM base_quality_notifications;
```

- **KPI cards** pull from this single-row dataset → any aggregation returns the same (correct) answer, so grain can't duplicate it.
- **Trend / ranking charts** pull from the grain-level mart (they need the rows).
- If you can't get a single-row mart from the pipeline, at minimum verify the KPI's field-well aggregation against §8 before shipping.

---

## 11. Beautify checklist (apply to EVERY dashboard before handoff)

**Numbers:**
- [ ] Currency: `₩` prefix + `NumberScale: AUTO` (shows B/M/K)
- [ ] Percentages: if the value is ALREADY `78.3`, use Number + `"%"` suffix — NOT `PercentageDisplay`, which ×100s it into 7830%
- [ ] Thousands separator enabled
- [ ] **Use myriad units (man/eok), not B/M/K, for a CJK/Korean audience.** Korean (CJK) groups large numbers by 4 digits — man (10⁴), eok (10⁸), jo (10¹²) — so `123,456,789` reads as "1 eok 2,345 man", *not* `123.5M`. QuickSight's `NumberScale: AUTO` only does the Western K/M/B, which reads unnaturally on a revenue/cost card. There is no native man/eok scale, so for a headline KRW figure prefer a `CalculatedField` that divides (`{cost}/100000000`) with a "hundred-million-won" suffix, or label the axis/title accordingly and pre-scale upstream. (Microsoft globalization: number-formatting locale ko-KR.)
- [ ] No false precision — round consistently (defect rate `0.0%`, utilization rate whole `%`), same decimals across sibling cards so the row scans cleanly.

**Charts:**
- [ ] TOP-N bars: `SortConfiguration` DESC + `CategoryItemsLimit` with `OtherCategories: EXCLUDE`
- [ ] Data labels: `Visibility: VISIBLE` + `Overlap: DISABLE_OVERLAP`
- [ ] Reference lines for targets (only if a real target value was provided — see §0/Q10). Manufacturing has standard targets to anchor lines/gauges: **world-class OEE 85% · Availability 90% · Performance 95% · Quality 99.9%** (Nakajima/TPM, oee.com) — use the customer's real numbers, fall back to these only as illustrative.
- [ ] **Pareto for cause analysis** (defect causes, downtime reasons): a `ComboChart` with count bars sorted DESC + a cumulative-% line, cumulative-% computed as a `CalculatedField` (`runningSum(...) / sum(...)` ×100), plus an 80% reference line (bound to `SECONDARY_YAXIS`) to mark the vital few. In a combo chart the **`LineValues` land on the secondary Y-axis automatically — there is no per-series `AxisBinding` to set** (`DataFieldComboSeriesItem.Settings` is `ComboChartSeriesSettings`, which has no `AxisBinding`; per-series binding exists only on *line* charts via `DataFieldSeriesItem`). Scale/label the secondary axis 0–100% via `SecondaryYAxisDisplayOptions`/`SecondaryYAxisLabelOptions`. Beats a flat bar for "where do I focus." (Full worked JSON: `dashboard-definitions.md` Part F1.)
- [ ] **Heat maps use a single-hue sequential ramp**, not the default categorical palette: set a `ColorScale` with `ColorFillType: GRADIENT` and 2–3 `Colors` (light→dark of one hue). Sequential = ordered magnitude; categorical hues on a heat map read as noise.
- [ ] **Gray is for context.** Use saturated color only on the focal series; render secondary/context series and non-data ink (gridlines, axes) in gray. Reserve red/amber/green strictly for status-vs-target, kept consistent across visuals (field-based coloring).
- [ ] **Bars over pies for comparison.** Keep a pie/donut only for true part-of-whole with < ~7 slices; otherwise a sorted horizontal bar wins. If a pie's category count is unbounded, add `CategoryItemsLimit` + an "Other" bucket.
- [ ] **Axis labels + units.** Give each axis a `ChartAxisLabelOptions` with the unit (`%`, `₩`, count) when the title alone doesn't make it obvious.

**Tables:**
- [ ] `SortConfiguration.RowSort` DESC on the ranking measure when the title says "top/TOP" — an empty sort makes "top" arbitrary.
- [ ] `PaginationConfiguration` (`PageSize`) so a "top N" table actually shows N rows.
- [ ] Conditional-format the status column (e.g. `variance_pct` red when over budget) via `ConditionalFormatting` → `Cell` → `TextFormat`.

**KPI cards:**
- [ ] Conditional formatting uses an AGGREGATION expression (`min({col}) >= 80`), NOT a raw `FieldId`
- [ ] Hero KPIs (1–2 key metrics) sized larger than secondary KPIs
- [ ] `Subtitle` either carries one line (unit / "last 30 days" / source) or is set `Visibility: HIDDEN` — never VISIBLE-but-empty (leaves a dead caption slot).

**Layout:**
- [ ] NOT every tab the identical "KPI row + 2-column grid" (anti-cookie-cutter)
- [ ] Each domain tab has a distinctive primary visual type
- [ ] Generous margins / whitespace over cramming; most-important KPI top-left ("5-second rule"); size = importance. Korean enterprise dashboards run denser — get density from *more aligned panels*, not clutter inside each panel.
- [ ] Hangul legibility: don't undersize text; a Hangul-complete font reads better than a Latin-only fallback. (`CreateTheme` only accepts generic fallback fonts — §3 — so set the font on the **theme/UI**, not in the definition.)
