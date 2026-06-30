# Scripts Reference — Glue jobs, Athena SQL, helper scripts

All script bodies the pipeline generates. The thin core (`SKILL.md`) points here; copy these into the CDK project's `glue-scripts/`, `athena-views/`, and `scripts/` directories and adapt to the **actual discovered schema** (never the illustrative `quality_inspections` columns).

> **Schema adaptability:** Every script below uses the ERP quality-inspection sample schema (`inspection_id`, `supplier_id`, `result`, `inspection_date`, …) as a **template, not a contract**. Read the real source schema FIRST (`spark.read.csv(path, header=True, inferSchema=True).printSchema()` for S3; driver metadata / `SHOW COLUMNS` for JDBC; `aws glue get-table` for a cataloged table), then adapt all transforms, types, partition columns, JOIN keys, and SQL. Only STOP and ask the user when a column mapping is genuinely ambiguous.

---

## Dirty real-world data handling

> 🔴 These are the VALUE-level corruptions that pass every structural validation (row-count > 0, null-check, STRICT) yet produce WRONG numbers. Korean manufacturing ERP exports (SAP + MES + hand-made finance Excel) hit all of them. Screen for each one in the Spark job BEFORE trusting any aggregate, then reconcile counts (last subsection). SKILL.md §4 → "Dirty real-world data" carries the summary table; this is the implementation.

### 1. Unicode NFD filenames (Korean decomposed form on macOS)

macOS stores Korean filenames in NFD (decomposed) form, so `MES_생산실적_202601.csv` on disk is byte-different from the NFC string you type. A literal `s3.get_object(Key="...생산실적...")` then fails with `NoSuchKey`. **This breaks on EVERY Korean/CJK filename uploaded from a Mac.**

```python
import boto3
# Do NOT hard-code the key. List the prefix and use the ACTUAL byte key returned.
s3 = boto3.client("s3")
resp = s3.list_objects_v2(Bucket=bucket, Prefix="mes/production/")
keys = [o["Key"] for o in resp.get("Contents", [])]   # real byte keys (NFD as stored)
# Match by a stable substring or extension, never by an NFC literal:
prod_keys = [k for k in keys if k.endswith(".csv")]
```

In Spark, point the reader at the **prefix** (`s3://{bucket}/mes/production/`) rather than a single filename — Spark enumerates the keys itself and sidesteps the NFC/NFD mismatch entirely.

### 2. Mixed encoding per source (EUC-KR vs UTF-8 in one pipeline)

MES exports are often **EUC-KR**, SAP exports **UTF-8** — in the same pipeline. Reading EUC-KR as UTF-8 yields mojibake in every Korean dimension. Branch the encoding per source (per ingest job, since this skill is one-job-per-logical-table):

```python
# encoding is a job parameter per source; default UTF-8, MES uses euc-kr
df = (spark.read
      .option("header", "true")
      .option("encoding", args.get("ENCODING", "UTF-8"))   # 'euc-kr' for MES
      .option("inferSchema", "true")
      .csv(args["SOURCE_PATH"]))
```

### 3. SAP trailing-minus negative numbers (`150.000-` = -150)

SAP writes negative amounts with a **trailing** minus (`150.000-`). `col.cast('double')` returns NULL for these, so every cost/amount KPI silently becomes 0 — and in real data this hit over half the rows. Move the sign to the front before casting:

```python
from pyspark.sql.functions import when, concat, lit, regexp_replace, col

def parse_num(c):
    """SAP trailing-minus aware numeric parse. '150.000-' -> -150.0, '150.000' -> 150.0."""
    return (when(c.endswith('-'),
                 concat(lit('-'), regexp_replace(c, '-$', '')).cast('double'))
            .otherwise(c.cast('double')))

transformed = raw_df.withColumn("amount", parse_num(col("amount")))
```

### 4. Mixed date formats (3+ patterns + literal 'NULL' in one column)

One column can carry `yyyyMMddHHmmss`, `yyyy-MM-dd HH:mm:ss`, **and** `yyyy/M/d H:m:s` (no zero-padding), plus the literal string `'NULL'`. Missing even one format silently drops those rows — a missed slash format caused 16% row loss and a production KPI that was 16% too low. Coalesce a chain of `to_timestamp` over EVERY observed format, and filter the literal `'NULL'` string first:

```python
from pyspark.sql.functions import to_timestamp, coalesce, col, when, lit

def parse_ts(c):
    cleaned = when(col(c) == 'NULL', lit(None)).otherwise(col(c))   # literal 'NULL' -> real null
    return coalesce(
        to_timestamp(cleaned, "yyyyMMddHHmmss"),
        to_timestamp(cleaned, "yyyy-MM-dd HH:mm:ss"),
        to_timestamp(cleaned, "yyyy/M/d H:m:s"),   # no zero-padding — easy to miss
    )

transformed = raw_df.withColumn("event_ts", parse_ts("event_time"))
```

> Before settling the format list, run `SELECT DISTINCT <col> LIMIT 100` (or `df.select(c).distinct()`) on the real column and confirm you have a `to_timestamp` pattern for every shape present. Then assert post-parse null-rate ≈ source null-rate — a jump means an unhandled format is being dropped.

### 5. Join-key leading-zero / whitespace inconsistency

The same logical key appears as `10010015`, `000000000010010002`, and `  000000000010010009` across sources (SAP zero-pads to 18 chars; MES doesn't; some exports add whitespace). Joins then return 0 rows → empty dimensions. Normalize **both sides** before any join:

```python
from pyspark.sql.functions import regexp_replace, trim, col

def norm_key(c):
    """Strip surrounding whitespace and leading zeros so keys join across sources."""
    return regexp_replace(trim(col(c)), '^0+', '')

orders   = orders.withColumn("matnr_norm", norm_key("matnr"))
material = material.withColumn("matnr_norm", norm_key("matnr"))
joined   = orders.join(material, "matnr_norm", "left")
```

### 6. Cross-source bridge mapping (no common key)

Two sources can describe the same entities with **no shared key** — SAP material groups (`FG100`, `FG200`, …) and a finance report's product categories (`브라켓류`, `하우징류`, …). There is nothing to join on directly. Build a **domain-knowledge bridge table** that infers the mapping from name-membership overlap (e.g. a material whose name contains the product-category name), then join through the bridge:

```python
# bridge: (material_group, product_category) inferred from name membership.
# Built once from domain logic, materialized as its own small Iceberg lookup table.
bridge = spark.createDataFrame([
    ("FG100", "브라켓류"),
    ("FG200", "하우징류"),
    # …derived by matching material_name CONTAINS product_category, reviewed by a human
], ["material_group", "product_category"])

enriched = sap_df.join(bridge, "material_group", "left") \
                 .join(finance_df, "product_category", "left")
```

> Document the bridge logic in `ARCHITECTURE.md` (it encodes a business assumption) and surface low-confidence rows for human review rather than guessing silently.

### Excel normalization checklist (Korean hand-made finance reports)

Finance Excels are authored by humans, not systems — the Glue **Python Shell** job (pandas + openpyxl) must normalize all of the following before writing Parquet:

1. **`skiprows` until the real header** — title often at row 2, header at row 4. Don't assume row 0. Use a `scan_excel_for_header_row` helper that finds the first row whose cells match expected header tokens.
2. **Forward-fill merged cells** — factory/department appears only in the first row of each merged group; `df[col].ffill()` to repopulate.
3. **Tag rows as `data` / `subtotal` / `total`** by keyword detection (`소계`, `합계`, `계`) and EXCLUDE subtotal/total rows from the fact table — summing a column that already contains its own subtotals double-counts.
4. **Strip currency formatting** — remove `₩` and thousands commas, convert to float (`"1,234,567" → 1234567.0`).
5. **Ignore footnote columns** — drop everything past the last non-empty header cell (footnotes/notes live to the right of the real table).

```python
import pandas as pd

def scan_excel_for_header_row(path, sheet, expected_tokens, max_scan=10):
    """Return the 0-based row index whose cells contain the expected header tokens."""
    probe = pd.read_excel(path, sheet_name=sheet, header=None, nrows=max_scan)
    for i, row in probe.iterrows():
        cells = {str(v).strip() for v in row.tolist()}
        if expected_tokens & cells:
            return i
    raise ValueError(f"header row not found in first {max_scan} rows")

hdr = scan_excel_for_header_row(path, sheet, {"공장", "제품", "금액"})
df = pd.read_excel(path, sheet_name=sheet, header=hdr)
df["factory"] = df["factory"].ffill()                          # merged cells
df["row_kind"] = df["item"].apply(
    lambda v: "total" if any(k in str(v) for k in ("소계", "합계", "계")) else "data")
df = df[df["row_kind"] == "data"].copy()                       # drop subtotal/total rows
df["amount"] = (df["amount"].astype(str)
                .str.replace(r"[₩,]", "", regex=True).astype(float))
df = df.loc[:, :df.columns[df.columns.notna()][-1]]            # drop footnote columns
```

### Source ↔ target reconciliation (the check that catches all of the above)

After ingest, prove the base table didn't silently lose or zero rows. Compare against the raw source COUNT and key SUMs — a gap beyond ~1% means one of the corruptions above bit:

```python
# Raw source row count (Spark reads the raw files directly — no download)
raw_n = spark.read.option("header", "true").option("encoding", enc).csv(src_path).count()
# Base table row count after ingest
tgt_n = spark.table(f"s3tablescatalog.{ns}.{table}").count()
assert abs(raw_n - tgt_n) / raw_n <= 0.01, \
    f"row loss {raw_n}->{tgt_n}: check date parse / cast-to-NULL / join / NFD filename"
```

```sql
-- Athena equivalent: reconcile a SUM against the source-derived ground truth.
-- If target SUM != expected, a measure was zeroed (trailing-minus) or rows dropped.
SELECT SUM(amount) AS target_sum FROM "{prefix}_db"."base_sap_costs";
-- compare with the source-file SUM computed the same way (parse_num applied)
```

---

## Iceberg path (default)

### `glue-scripts/ingest-iceberg.py` — Case A: S3 file source → Iceberg

Raw CSV/JSON lands in `{prefix}-raw-zone`. The job reads it, types/cleans/filters in Spark, and writes straight into the Iceberg table. No external table, no CTAS, no crawler.

```python
# glue-scripts/ingest-iceberg.py
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, to_date, current_timestamp

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'PREFIX', 'SOURCE_PATH', 'TABLE_BUCKET', 'NAMESPACE', 'TABLE_NAME',
])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# NOTE: Do NOT configure the Iceberg / S3 Tables catalog here. spark.sql.extensions
# and the spark.sql.catalog.* keys are STATIC configs in Glue 5 (Spark 3.5) and
# calling spark.conf.set(...) on them fails with "Cannot modify the value of a
# static config". They are set in the job's defaultArguments --conf (see
# reference/iceberg-cdk.md), together with --datalake-formats iceberg, --extra-jars
# (the S3 Tables catalog JAR), and --user-jars-first true. The session arrives
# already configured with the `s3tablescatalog` catalog.

# Read raw CSV from S3
raw_df = spark.read.option("header", "true").option("inferSchema", "true").csv(args['SOURCE_PATH'])

# Apply transformations (typing, cleaning, filtering)
transformed = raw_df \
    .withColumn("inspection_date", to_date(col("inspection_date"), "yyyy-MM-dd")) \
    .withColumn("ingestion_timestamp", current_timestamp()) \
    .filter(col("inspection_date").isNotNull())

# Write directly to Iceberg / S3 Tables. createOrReplace() for full refresh;
# use append() for incremental, or a MERGE INTO (Case C) for upserts.
target = f"s3tablescatalog.{args['NAMESPACE']}.{args['TABLE_NAME']}"
transformed.writeTo(target) \
    .using("iceberg") \
    .tableProperty("format-version", "2") \
    .tableProperty("write.parquet.compression-codec", "zstd") \
    .createOrReplace()

job.commit()
```

> S3 Tables manages its own storage location — never set a `path`/`location`. Write through the `s3tablescatalog` Spark catalog (the `warehouse` conf is the table-bucket ARN) and Iceberg places the data. The job's IAM role needs the `s3tables:*` + `glue:*` grants from reference/iceberg-cdk.md.

### `glue-scripts/ingest-jdbc-iceberg.py` — Case B: DB source → Iceberg (skip raw S3)

For a JDBC source there is **no need to stage raw files in S3**. The Glue job reads the table over JDBC, transforms in Spark, and writes straight into Iceberg — one job, one hop.

```python
# glue-scripts/ingest-jdbc-iceberg.py (excerpt — no spark.conf.set; the s3tablescatalog
# catalog is configured via the job's defaultArguments --conf + --extra-jars, same as Case A)
df = glueContext.create_dynamic_frame.from_options(
    connection_type=args['ENGINE'],            # sqlserver | mysql | postgresql | oracle
    connection_options={
        'useConnectionProperties': 'true',
        'connectionName': args['CONNECTION_NAME'],
        'dbtable': args['TABLE_NAME'],
    },
).toDF()

transformed = df.withColumn("ingestion_timestamp", current_timestamp())

transformed.writeTo(f"s3tablescatalog.{args['NAMESPACE']}.{args['TABLE_NAME']}") \
    .using("iceberg") \
    .tableProperty("write.parquet.compression-codec", "zstd") \
    .createOrReplace()
```

> Use this whenever the source is a database. Staging raw Parquet in `{prefix}-raw-zone` first (the Hive ingest-jdbc pattern) is only worth it if you need an immutable landing copy for replay/audit; otherwise write straight to Iceberg.

### Case C — Incremental loads / upserts: Spark `MERGE INTO`

For daily batch upserts, run a Spark `MERGE INTO` inside the same Glue job — Iceberg v2 supports row-level updates natively. No crawler, no Job Bookmark (Iceberg snapshots are self-tracking).

```python
incremental.createOrReplaceTempView("source")
spark.sql(f"""
  MERGE INTO s3tablescatalog.{args['NAMESPACE']}.{args['TABLE_NAME']} target
  USING source
  ON target.inspection_id = source.inspection_id
  WHEN MATCHED THEN UPDATE SET *
  WHEN NOT MATCHED THEN INSERT *
""")
```

### Athena CTAS/INSERT fallback — one-time exploratory loads only

Athena CTAS/INSERT INTO can create and populate an Iceberg table in pure SQL, with **no Glue job**. Reserve this for **one-time exploratory or ad-hoc loads**. It is **not** the scheduled production path (Athena has no scheduler), and it cannot do the row-level Spark transforms the Glue job can. Declare the raw CSV as an explicit external table first, then CTAS:

```sql
-- 1) Declare raw CSV explicitly (all columns string — OpenCSVSerde does not honor
--    types or empty numeric values; CAST to real types in the CTAS below).
CREATE EXTERNAL TABLE "AwsDataCatalog"."{prefix}_raw"."raw_quality_inspections" (
  inspection_id string, lot_number string, product_code string, supplier_id string,
  inspection_type string, inspection_date string, result string,
  defect_count string, inspected_qty string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"', 'escapeChar' = '\\')
STORED AS TEXTFILE
LOCATION 's3://{prefix}-raw-zone/erp/quality_inspections/'
TBLPROPERTIES ('skip.header.line.count' = '1');

-- 2) CTAS straight into the Iceberg table (do NOT set location — S3 Tables manages it).
CREATE TABLE "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."quality_inspections"
WITH (table_type = 'ICEBERG', format = 'PARQUET', write_compression = 'ZSTD') AS
SELECT
  CAST(NULLIF(inspection_id, '')   AS bigint)  AS inspection_id,
  lot_number, product_code, supplier_id, inspection_type,
  CAST(NULLIF(inspection_date, '') AS date)    AS inspection_date,
  result,
  CAST(NULLIF(defect_count, '')    AS integer) AS defect_count,
  CAST(NULLIF(inspected_qty, '')   AS integer) AS inspected_qty
FROM "AwsDataCatalog"."{prefix}_raw"."raw_quality_inspections"
WHERE NULLIF(inspection_date, '') IS NOT NULL;
```

> **⚠️ CSV empty strings break `CAST` — wrap every cast in `NULLIF`.** OpenCSVSerde reads every column as a raw string, so a missing value arrives as the empty string `''`, not NULL. `CAST('' AS date)` (or `AS integer`/`AS bigint`) aborts the **entire** CTAS with `INVALID_CAST_ARGUMENT` — one blank cell fails the whole load. Always cast through `NULLIF(col, '')`. Iceberg partition transforms (`month(date)`, `day(date)`) tolerate the resulting NULLs safely. The Glue Spark default path avoids this entirely: `spark.read.csv(..., inferSchema=true)` reads empty CSV fields as `null` automatically.

For ongoing loads via this fallback, `INSERT INTO` / `MERGE INTO` from the external table — but for any recurring daily batch, use the Glue Spark job instead.

### `athena-views/marts.sql` — materialized mart table (Iceberg)

`CREATE VIEW` is unsupported on the `s3tablescatalog` catalog. Materialize enrichment/aggregation as a **mart table (CTAS)** in the same S3 Tables namespace. The consumption layer reads `mart_*` exactly as it would have read `v_*`:

```sql
-- GRAIN: (inspection_month, supplier_id) — every measure below is valid to SUM across this grain
CREATE TABLE "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."mart_quality_summary"
WITH (table_type = 'ICEBERG', format = 'PARQUET', write_compression = 'ZSTD') AS
SELECT
  DATE_TRUNC('month', inspection_date) AS inspection_month,
  supplier_id,
  COUNT(*)                                            AS total_count,
  SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END)    AS defect_count,
  CAST(SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END) AS DOUBLE)
    / NULLIF(COUNT(*), 0) * 100                        AS defect_rate_pct
FROM "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."quality_inspections"
GROUP BY DATE_TRUNC('month', inspection_date), supplier_id;
```

> Better still, build the mart **inside the Glue 5.x Spark job** (an extra `writeTo` of an aggregated DataFrame to `mart_quality_summary`) so it is typed in code and refreshed on the same schedule as the base table — no separate Athena step.

> 🔴 **Declare the grain** in a `-- GRAIN: (...)` header comment AND in `platform.yaml` (`grain: [...]`, `sum_safe_columns: [...]`). Never put a coarser-grain measure into a finer-grain mart without pre-aggregating — it duplicates on SUM (a material-level count in a `(material × defect_code)` grain mart over-counted 8.3×). See SKILL.md §4 → "Mart grain declaration".

### `athena-views/marts.sql` — single-row KPI summary mart (eliminates grain-induced SUM duplication)

KPI cards aggregate over the WHOLE dataset. If they read a multi-grain mart, SUM/COUNT double- or triple-count. The fix is a dedicated **single-row** mart — one row, every KPI pre-computed with the CORRECT aggregation function. KPI visuals read this; trend/ranking visuals read the grain-level mart. Any aggregation over a 1-row table returns the same (correct) number.

```sql
-- GRAIN: () single row — KPI cards aggregate this; result is identical regardless of agg function
CREATE TABLE "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."mart_kpi_summary"
WITH (table_type = 'ICEBERG', format = 'PARQUET', write_compression = 'ZSTD') AS
SELECT
  -- COUNT(DISTINCT ...) not COUNT(...) — counting non-distinct over-reports (165 vs 20 materials)
  COUNT(DISTINCT CASE WHEN result = 'fail' THEN material_key END) AS defect_materials,
  SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END)                AS total_defects,
  -- delay metrics: filter to the right population, exclude early completions that dilute the average
  COUNT(CASE WHEN is_on_time = false THEN 1 END)                  AS delayed_orders,        -- NOT COUNT(delay_days)
  AVG(CASE WHEN delay_days > 0 THEN delay_days END)               AS avg_delay_days_late,   -- exclude negatives
  -- notifications counted at their NATIVE (material) grain, never summed from a finer-grain mart
  COUNT(DISTINCT notification_id)                                 AS qmel_notifications
FROM "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."base_quality_notifications";
```

> Each KPI's aggregation must encode its real intent: `COUNT(DISTINCT …)` for "how many distinct X", a `WHERE`/`CASE` population filter for "delayed orders" (`is_on_time = false`, not "has a delay value"), and exclusion of out-of-population values (negative/early `delay_days`) so averages aren't diluted. These exact mistakes produced 5 of the 6 wrong KPIs.

### Time travel (unique to Iceberg)

```sql
-- Query as of a specific time
SELECT * FROM "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."quality_inspections"
FOR TIMESTAMP AS OF TIMESTAMP '2026-01-15 00:00:00';

-- View snapshot history
SELECT * FROM "s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."quality_inspections$snapshots";
```

---

## Hive path (opt-in)

### `glue-scripts/ingest-jdbc.py` — JDBC → S3 raw

```python
"""
JDBC ingestion to S3 raw zone.
Reads each table from the source DB and writes Parquet partitioned by ingestion_date.
"""
import sys
from datetime import datetime
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import lit
from awsglue.context import GlueContext
from awsglue.job import Job

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'CONNECTION_NAME', 'DATABASE', 'TABLES', 'TARGET_BUCKET', 'SOURCE_NAME', 'ENGINE',
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

ingestion_date = datetime.utcnow().strftime('%Y-%m-%d')
tables = args['TABLES'].split(',')
# Engine mapping: sqlserver→sqlserver, mysql→mysql, postgresql→postgresql, oracle→oracle
connection_type = args.get('ENGINE', 'sqlserver')

for table in tables:
    table = table.strip()
    print(f"Ingesting {args['DATABASE']}.{table}")
    # useConnectionProperties pulls url/user/password from the named Glue connection
    # (the connection's JDBC URL already encodes the database, so no separate
    # 'database' option is needed here). For engines with schemas, qualify the table
    # as 'schema.table' in dbtable.
    df = glueContext.create_dynamic_frame.from_options(
        connection_type=connection_type,
        connection_options={
            'useConnectionProperties': 'true',
            'connectionName': args['CONNECTION_NAME'],
            'dbtable': table,
        },
    )
    df = df.toDF().withColumn('ingestion_date', lit(ingestion_date))
    output_path = f"s3://{args['TARGET_BUCKET']}/{args['SOURCE_NAME']}/{table}/"
    (df.write
       .mode('overwrite')
       .partitionBy('ingestion_date')
       .parquet(output_path))
    print(f"  → {output_path}")

job.commit()
```

### `glue-scripts/transform.py` — Raw → Curated

```python
"""
Raw → Curated transformation.
Cleans, casts, filters, and writes Parquet+Snappy to curated zone.
Customize the transform_table() function per business domain.
"""
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, trim, to_date, when

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'DATABASE', 'RAW_TABLE', 'CURATED_TABLE', 'TARGET_BUCKET',
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

raw = glueContext.create_dynamic_frame.from_catalog(
    database=args['DATABASE'], table_name=args['RAW_TABLE']
).toDF()

# Domain-specific cleanup — replace with actual transforms
curated = (raw
  .withColumn('inspection_date', to_date(col('inspection_date')))
  .withColumn('supplier_id', trim(col('supplier_id')))
  .dropDuplicates(['inspection_id'])
)

(curated.write
   .mode('overwrite')
   .option('compression', 'snappy')
   .partitionBy('inspection_date')
   .parquet(f"s3://{args['TARGET_BUCKET']}/{args['CURATED_TABLE']}/"))

job.commit()
```

### `athena-views/views.sql` — view DDL (Hive only)

`v_{table}` views work on the Hive path. (On Iceberg, use `mart_*` CTAS instead — see above.)

```sql
-- Enrichment + aggregation view: join codes to human-readable names, derive
-- defect metrics from the per-inspection `result` column. The raw
-- quality_inspections table records ONE row per inspection with a `result` of
-- 'pass' / 'fail' / 'conditional' — there are no pre-aggregated count columns,
-- so total_count / defect_count / defect_rate_pct are derived here.
CREATE OR REPLACE VIEW v_quality_inspections AS
SELECT
  DATE_TRUNC('month', qi.inspection_date) AS inspection_month,
  qi.supplier_id,
  s.supplier_name,
  qi.product_code,
  p.product_name,
  qi.inspection_type,
  CASE qi.inspection_type
    WHEN 'incoming_material' THEN '수입검사'
    WHEN 'in_process' THEN '공정검사'
    WHEN 'final_product' THEN '최종검사'
    WHEN 'packaging' THEN '포장검사'
    ELSE '기타'
  END AS inspection_type_name,
  COUNT(*) AS total_count,
  SUM(CASE WHEN qi.result = 'fail' THEN 1 ELSE 0 END) AS defect_count,
  CAST(SUM(CASE WHEN qi.result = 'fail' THEN 1 ELSE 0 END) AS DOUBLE)
    / NULLIF(COUNT(*), 0) * 100 AS defect_rate_pct
FROM quality_inspections qi
LEFT JOIN suppliers s ON qi.supplier_id = s.supplier_id
LEFT JOIN products p ON qi.product_code = p.product_code
GROUP BY
  DATE_TRUNC('month', qi.inspection_date),
  qi.supplier_id, s.supplier_name,
  qi.product_code, p.product_name,
  qi.inspection_type;
```

Patterns to apply:

- `CASE` statements for code→Korean name enrichment (matches business question phrasing)
- `LEFT JOIN` for dimension lookups, never `INNER JOIN` (avoid silent row drops)
- `NULLIF(denom, 0)` to avoid divide-by-zero
- `DATE_TRUNC('month', date_col) AS month` for time-series rollups

### Named query — one per business question

```sql
-- Q: 거래처별 불량 TOP5 (FY 2025)
SELECT supplier_name, SUM(defect_count) AS total_defects
FROM v_quality_inspections
WHERE inspection_month >= DATE '2025-01-01'
GROUP BY supplier_name
ORDER BY total_defects DESC
LIMIT 5;
```

---

## Helper scripts (both patterns)

### `scripts/run-views.py` — apply CREATE OR REPLACE VIEW / mart CTAS via Athena

Use Python — bash awk-based SQL splitting is fragile with multi-line CASE statements. Works for both `views.sql` (Hive) and `marts.sql` (Iceberg) — it splits on statement boundaries.

```python
# scripts/run-views.py
"""
Apply CREATE OR REPLACE VIEW (Hive views.sql) or CREATE TABLE ... AS SELECT
(Iceberg marts.sql) statements from a .sql file via Athena.
Use Python — bash awk-based SQL splitting is fragile with multi-line CASE statements.
"""
import boto3, re, sys, time


def run_views(workgroup: str, database: str, views_file: str, region: str):
    client = boto3.client('athena', region_name=region)
    with open(views_file) as f:
        sql = f.read()

    # Strip `--` line comments first. The marts.sql / views.sql files carry `-- GRAIN`
    # headers between statements; left in place they end up dangling after the previous
    # statement's `;` (Athena rejects a `;` followed by more content) or split off as an
    # orphan chunk. Athena runs one statement per call, so comments add no value here.
    sql = re.sub(r'--[^\n]*', '', sql)

    # Split on statement boundaries — handle BOTH Hive views (CREATE OR REPLACE VIEW)
    # and Iceberg marts (CREATE TABLE ... AS SELECT). Matching only VIEW here would
    # collapse a marts.sql file into one un-split blob and the name lookup below would
    # fail with AttributeError on the default (Iceberg) path.
    statements = re.split(r'(?=CREATE\s+(?:OR\s+REPLACE\s+VIEW|TABLE)\b)', sql, flags=re.IGNORECASE)
    statements = [s.strip().rstrip(';') for s in statements
                  if re.match(r'CREATE\s+(?:OR\s+REPLACE\s+VIEW|TABLE)\b', s.strip(), re.IGNORECASE)]

    for stmt in statements:
        m = re.search(r'(?:VIEW|TABLE)\s+(\S+)', stmt, re.IGNORECASE)
        obj_name = m.group(1) if m else '(unnamed statement)'
        print(f"Creating: {obj_name}")
        response = client.start_query_execution(
            QueryString=stmt,
            WorkGroup=workgroup,
            QueryExecutionContext={'Database': database},
        )
        exec_id = response['QueryExecutionId']
        while True:
            status = client.get_query_execution(QueryExecutionId=exec_id)
            state = status['QueryExecution']['Status']['State']
            if state in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
                break
            time.sleep(1)
        if state != 'SUCCEEDED':
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'unknown')
            print(f"  FAILED: {reason}")
            sys.exit(1)
        print(f"  OK")


if __name__ == '__main__':
    run_views(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
```

Invoke after deploy:
```bash
python3 scripts/run-views.py {prefix}-workgroup {prefix}_db athena-views/views.sql {region}
```

### `scripts/smoke-test.py` — end-to-end data flow verification

Generate this alongside the CDK app. The smoke test should:
1. Run each named query in the workgroup and assert `rows > 0`.
2. Verify each curated Glue table exists with `partition_count > 0`.
3. Run a sample `SELECT * FROM {table} LIMIT 10` against each curated table to confirm read access from the Athena workgroup with the configured IAM role.
4. Exit non-zero on any failure so it can be wired into CI / a deploy hook.

```bash
python3 scripts/smoke-test.py --prefix {prefix} --region {region}
```

### `athena-views/quality-checks.sql` — post-run data quality (one set per curated table)

```sql
-- A. Row count: target should be within 1% of source
SELECT COUNT(*) AS target_count FROM {prefix}_db.{table};
-- (compare manually with source DB count)

-- B. Null rate on key columns (inspection_id, supplier_id, etc.)
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN {key_column} IS NULL THEN 1 ELSE 0 END) AS nulls,
  ROUND(SUM(CASE WHEN {key_column} IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS null_pct
FROM {prefix}_db.{table};

-- C. Date range — confirms the latest partition loaded
SELECT MIN({date_col}) AS earliest, MAX({date_col}) AS latest
FROM {prefix}_db.{table};

-- D. Duplicate primary key check
SELECT {pk_column}, COUNT(*) AS occurrences
FROM {prefix}_db.{table}
GROUP BY {pk_column}
HAVING COUNT(*) > 1
LIMIT 10;

-- E. Referential integrity (if there are FK relationships)
SELECT a.{fk_column}
FROM {prefix}_db.{table} a
LEFT JOIN {prefix}_db.{dim_table} b ON a.{fk_column} = b.{pk_column}
WHERE b.{pk_column} IS NULL
LIMIT 10;
```
