"""Lambda-compatible port of soak_check.py — same Phase 7.7 mechanical checks (row count,
checksum, schema drift, alarm state, storage headroom, replication lag, replication
errors), same 3-state handling for replication_lag/customer_test_suite (True/False/
"not_applicable"/None — see run_day's docstring), same needs_agent_review flagging — but
running as an AWS-managed EventBridge Scheduler target instead of a process on a bastion
or laptop, and writing to the dashboard's S3 bucket instead of a local dashboard/ folder.

WHY THIS EXISTS ALONGSIDE soak_check.py, NOT INSTEAD OF IT: soak_check.py stays the
reference implementation (readable, no AWS SDK dependency, easy to run/debug by hand from
any machine that can reach the databases directly). This file is the same logic re-targeted
at the two things that change in Lambda: (1) no `mysql`/`psql`/`aws` CLI binaries or
subprocess — connect over the wire with pymysql/pg8000, call boto3 directly; (2) no local
filesystem — status.json/activity-log.jsonl/soak-report-*.md live in S3, which has no native
append, so "append a line to activity-log.jsonl" is GET-modify-PUT under the hood.

SCOPE: homogeneous MySQL-family (mysql/mariadb/aurora-mysql) or PostgreSQL-family
(postgres/postgresql/aurora-postgresql) only — source and target must normalize to the
SAME family. Heterogeneous soak-checking (e.g. MySQL source, PostgreSQL target) is not
yet supported by this script; `handler()` checks this FIRST, from the SOURCE_ENGINE/
TARGET_ENGINE environment variables alone, and raises a clear error before reading either
secret or opening either database connection — rather than silently running the wrong SQL
dialect against one side, or spending a live Secrets Manager read + DB connection on an
invocation that was already going to fail.

RETRY SAFETY: EventBridge Scheduler's retry policy is at-least-once, not exactly-once — a
retried invocation for a day already recorded in status.json/activity-log.jsonl overwrites
that day's entry in place instead of appending a duplicate or double-incrementing the
green streak (see update_status_json/append_activity_log below).

Deploy shape (see shared/patterns/cdk-stacks.md §soak-stack.ts): VPC-attached into
the SAME private subnets + security group the migration bastion already uses (identical
reachability to the source over the existing VPN/DX path, no new networking), triggered
daily by EventBridge Scheduler, IAM scoped to Secrets Manager read (a DEDICATED read-only
credential per side — never the admin/master secret), CloudWatch/RDS/DMS describe, and S3
read/write on the dashboard bucket only.

Config arrives via environment variables (set once by CDK from the same values that would
have gone into soak-config.json for the standalone script) — see the SoakCheckFunction
construct in cdk-stacks.md for exactly which ones and their shapes. Optional variables may
be present but EMPTY (CDK sets `?? ''`) — an empty value means "not configured".

PREFLIGHT MODE: invoke with the event `{"mode": "preflight"}` after the first deploy, while
the schedule is still DISABLED. It runs the same probes a normal run needs — secrets, a DB
connect plus SELECT/catalog/checksum-shaped probes on EVERY configured table on both sides
(built from the same SQL helpers as the run), replica status, all alarms, both DMS metrics,
RDS describe, and every S3 key — WITHOUT writing anything (writes are proven with a
wrong-ETag conditional PUT: 412 = allowed). Each row
{check, iam_action, resource, result: PASS|AccessDenied|Error|UNVERIFIED|SKIPPED, message[, kms_key]}
is logged as `SOAK_PREFLIGHT_ROW {json}` the moment it completes; probes are skipped (not
hung) when the function's remaining time runs low. Fix every non-PASS row in ONE change,
redeploy at most ONCE, re-run, then enable the schedule (cdk-stacks.md §soak-stack.ts
"Deploy-once workflow"). Never redeploy on a guess.

ERROR HANDLING: every AWS call goes through `_aws()`, which turns botocore errors into an
`AwsCallError` carrying the exact IAM action and resource. Calls whose failure the four-state
model can absorb (alarms, headroom metrics, DMS lag/errors) record the failure in
detail.aws_errors[] and set that check to None (needs_agent_review) — never a pass. Calls
the run cannot proceed without (Secrets Manager, S3 reads/writes) log one
`SOAK_CHECK_ERROR {json}` line and fail the invocation. A connect/read TIMEOUT on an AWS
endpoint is reported as a network problem (no NAT / VPC endpoint), not as IAM.
"""
import datetime
import json
import os
import random
import re
import ssl
import time
from pathlib import Path

import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

try:
    import pg8000.native as pg8000
except ImportError:  # not needed for a MySQL-only deployment; kept optional to match
    pg8000 = None      # soak_check.py's engine-agnostic shape without forcing the dependency.

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number.
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30

# Confirmed LIVE against a real RDS PostgreSQL instance and a real Aurora PostgreSQL
# cluster (us-east-1, postgres/aurora-postgresql 16.13): ssl.create_default_context()'s
# platform default trust store does NOT contain the current Amazon RDS root ("Amazon RDS
# <region> Root CA RSA2048 G1") on Amazon Linux 2023 — only the unrelated generic "Amazon
# Root CA 1-4" (ACM/Trust Services roots) and legacy Starfield roots. Tier 1 (no ca_path)
# therefore pins this bundled copy of the official AWS RDS/Aurora CA bundle
# (https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem, covers every
# region/algorithm generation) instead of the OS default — see _tls_context. Packaged as
# a sibling file of this handler in the Lambda deployment asset (see cdk-stacks.md
# §soak-stack.ts bundling step) — NOT shared/assets/ (this file gets flat-copied into
# lambda/soak-check/, unlike soak_check.py which runs in place from the skill tree).
_DEFAULT_CA_BUNDLE = str(Path(__file__).resolve().parent / "rds-global-bundle.pem")

# Short connect/read timeouts: a VPC-attached Lambda with no NAT/VPC endpoint otherwise
# hangs on botocore's 60s default connect timeout x retries until the FUNCTION times out,
# which is indistinguishable from "something is wrong with IAM" in the logs. Failing fast
# lets _aws() name the unreachable endpoint instead.
_BOTO_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 3, "mode": "standard"})
_secrets = boto3.client("secretsmanager", config=_BOTO_CONFIG)
_cloudwatch = boto3.client("cloudwatch", config=_BOTO_CONFIG)
_rds = boto3.client("rds", config=_BOTO_CONFIG)
_s3 = boto3.client("s3", config=_BOTO_CONFIG)
_dms = boto3.client("dms", config=_BOTO_CONFIG)

# Preflight gets its own, tighter clients: every probe must finish (or fail) well inside the
# function timeout so the table is always returned — 2 attempts total, 3s connect, 5s read.
_PREFLIGHT_BOTO_CONFIG = Config(connect_timeout=3, read_timeout=5,
                                retries={"total_max_attempts": 2, "mode": "standard"})
_PF = None


class _Clients:
    def __init__(self, config):
        self.secrets = boto3.client("secretsmanager", config=config)
        self.cloudwatch = boto3.client("cloudwatch", config=config)
        self.rds = boto3.client("rds", config=config)
        self.s3 = boto3.client("s3", config=config)
        self.dms = boto3.client("dms", config=config)


def _preflight_clients():
    global _PF
    if _PF is None:
        _PF = _Clients(_PREFLIGHT_BOTO_CONFIG)
    return _PF

# Error codes the services in play return for an IAM/KMS/resource-policy denial.
_ACCESS_DENIED_CODES = {
    "AccessDenied", "AccessDeniedException", "AccessDeniedFault", "UnauthorizedOperation",
    "AuthorizationError", "KMS.AccessDeniedException", "KMSAccessDeniedException",
    "DecryptionFailure", "InvalidClientTokenId",
}
_NETWORK_ERRORS = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)


class AwsCallError(RuntimeError):
    """A failed AWS API call, carrying exactly what an operator needs to fix it."""

    def __init__(self, check, iam_action, resource, exc):
        self.check, self.iam_action, self.resource = check, iam_action, resource
        self.kms_key = None
        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {})
            self.code = err.get("Code") or ""
            msg = err.get("Message") or str(exc)
            low = msg.lower()
            kms = "kms" in low or self.code.startswith("KMS") or self.code == "DecryptionFailure"
            denied = self.code in _ACCESS_DENIED_CODES or (kms and ("denied" in low or "not allowed" in low
                                                                       or "not authorized" in low))
            self.result = "AccessDenied" if denied else "Error"
            if self.result == "AccessDenied" and kms:
                # Name the KMS action actually denied: parse it from the AWS message when
                # present, else infer from the operation (reads decrypt, writes generate a
                # data key). The key ARN is reported separately from the S3/secret resource.
                m = re.search(r"perform:\s*(kms:[A-Za-z*]+)", msg)
                self.iam_action = m.group(1) if m else ("kms:GenerateDataKey" if "PutObject" in iam_action else "kms:Decrypt")
                k = re.search(r"(arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(?:key|alias)/[A-Za-z0-9/_-]+)", msg)
                self.kms_key = k.group(1) if k else ("the customer-managed key (ARN not in the error — "
                                                     "see the secret's / bucket's encryption settings)")
            self.message = f"{self.code}: {msg}"
        elif isinstance(exc, _NETWORK_ERRORS):
            self.code, self.result = type(exc).__name__, "Error"
            self.message = (f"network: {exc} — the Lambda's subnets cannot reach this AWS endpoint. "
                            "Add a NAT route or a VPC interface endpoint (S3: gateway endpoint); "
                            "this is NOT an IAM problem.")
        else:
            self.code, self.result = type(exc).__name__, "Error"
            self.message = f"{type(exc).__name__}: {exc}"
        super().__init__(f"{self.result} {self.iam_action} on {self.resource}: {self.message}")

    def as_dict(self):
        d = {"check": self.check, "iam_action": self.iam_action, "resource": self.resource,
             "result": self.result, "message": self.message}
        if self.kms_key:
            d["kms_key"] = self.kms_key  # the S3/secret resource stays in "resource"
        return d


def _aws(check, iam_action, resource, fn, **kwargs):
    try:
        return fn(**kwargs)
    except (ClientError, BotoCoreError) as e:
        raise AwsCallError(check, iam_action, resource, e) from e


def _record(aws_errors, err):
    """Non-fatal AWS failure: keep it visible (logged + detail.aws_errors[]) — the caller
    turns the check into None/needs_agent_review, never into a pass."""
    print("SOAK_CHECK_AWS_ERROR " + json.dumps(err.as_dict(), ensure_ascii=False))
    if aws_errors is not None:
        aws_errors.append(err.as_dict())


def _s3_arn(bucket, key):
    return f"arn:aws:s3:::{bucket}/{key}"

MYSQL_FAMILY = {"mysql", "mariadb", "aurora-mysql"}
POSTGRES_FAMILY = {"postgres", "postgresql", "aurora-postgresql"}


def _engine_family(engine):
    """Normalizes an engine string to 'mysql' or 'postgres' so MariaDB routes to the
    MySQL client path instead of silently falling through to Postgres (the confirmed
    bug: anything other than the exact string "mysql" used to fall to the pg8000 path).
    Raises rather than guessing for anything outside the two supported families —
    heterogeneous soak-checking (different SQL dialects on each side) is out of scope
    for this script; see the module docstring."""
    e = (engine or "").strip().lower()
    if e in MYSQL_FAMILY:
        return "mysql"
    if e in POSTGRES_FAMILY:
        return "postgres"
    raise ValueError(
        f"Unsupported engine '{engine}' for soak automation — this script only supports "
        "homogeneous MySQL-family (mysql/mariadb/aurora-mysql) or PostgreSQL-family "
        "(postgres/postgresql/aurora-postgresql) checks. Heterogeneous soak-checking "
        "(different SQL dialects on each side) is not yet supported."
    )


def _env_json(name, default):
    raw = os.environ.get(name)
    return json.loads(raw) if raw else default


def _get_secret(secret_arn, check="secret", client=None):
    resp = _aws(check, "secretsmanager:GetSecretValue", secret_arn,
                (client or _secrets).get_secret_value, SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _tls_context(ca_path=None, insecure=False):
    """Always negotiate TLS, never allow a silent plaintext fallback. Three tiers:
    1. No ca_path, insecure=False (the common case: an RDS/Aurora endpoint with an
       Amazon-issued cert) — full sslmode=verify-full equivalent: pins the bundled AWS
       RDS/Aurora CA bundle (`_DEFAULT_CA_BUNDLE`) PLUS hostname verification. NOT the
       platform default trust store — confirmed live that the OS store lacks the current
       RDS root (see `_DEFAULT_CA_BUNDLE`'s module-level comment); a genuinely non-AWS
       Postgres/MySQL source signed by a public WebPKI CA should pass its own ca_path
       (tier 2) rather than rely on this default.
    2. ca_path given — pins that specific CA certificate as the trust anchor; still
       CERT_REQUIRED (fully encrypted and chain-verified against it), hostname
       verification skipped (pinning the exact CA already achieves the security goal,
       and on-prem certs frequently carry no SAN matching the IP/hostname used to reach
       them). Confirmed live while building this: pinning the presented LEAF certificate
       (e.g. via `ssl.getpeercert`) does NOT satisfy this — OpenSSL still rejects the
       self-signed CA that issued it if that CA itself isn't the file being trusted, so
       ca_path must be the actual CA certificate, not just any certificate the peer
       happens to present.
    3. insecure=True (explicit opt-in ONLY, never the default) — encrypts the session
       but skips certificate verification entirely (CERT_NONE). Real, bounded fallback
       for exactly the case tier 2 can't cover: a self-signed cert auto-generated by the
       DB engine on a host with no shell/filesystem access to retrieve the actual CA
       file (confirmed live against exactly this: MySQL 8.0's auto-generated per-install
       CA, on-prem-style, unreachable except over the DB port itself). Still strictly
       better than the original bug (no TLS negotiated at all) — it must be turned on
       explicitly per-side, never silently, and every other scenario should prefer tier
       1 or 2 over this."""
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=ca_path or _DEFAULT_CA_BUNDLE)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = ca_path is None
    return ctx


def _connect(family, host, port, creds, database, ssl_ca_path=None, ssl_insecure=False,
             connect_timeout=10, read_timeout=25):
    user = creds.get("username") or creds.get("user")
    password = creds["password"]
    tls = _tls_context(ssl_ca_path, ssl_insecure)
    if family == "mysql":
        return pymysql.connect(host=host, port=int(port), user=user, password=password,
                                database=database, connect_timeout=connect_timeout, read_timeout=read_timeout,
                                cursorclass=pymysql.cursors.Cursor, ssl=tls)
    if pg8000 is None:
        raise RuntimeError("engine=postgres but pg8000 is not bundled in this deployment")
    return pg8000.Connection(host=host, port=int(port), user=user, password=password,
                             database=database, timeout=connect_timeout, ssl_context=tls)


def _query(family, conn, sql, params=None):
    """Returns list of row tuples. pg8000.native.Connection.run() and pymysql cursors have
    different call shapes — normalize here so the check functions below stay engine-blind,
    same as soak_check.py's run_mysql/run_psql split, just at the query layer instead of the
    subprocess layer."""
    if family == "mysql":
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    # pg8000.native.run returns list-of-dict-like rows keyed by column label; normalize to
    # plain tuples by column order for parity with the mysql path.
    rows = conn.run(sql, **(params or {}))
    return rows


def _query_named(conn, sql):
    """MySQL-family only: (column_names, rows) — for result sets whose column ORDER is not
    stable across versions (SHOW REPLICA STATUS)."""
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        return cols, list(cur.fetchall())


# SHOW REPLICA STATUS (8.0.22+, the only form on 8.4) names it Seconds_Behind_Source;
# SHOW SLAVE STATUS (<8.0.22, MariaDB) names it Seconds_Behind_Master. Looked up BY NAME:
# the old positional read (index 32) was verified only against an 8.0 replica, and the
# column list is not a stable contract across 8.0/8.4/MariaDB.
LAG_COLUMN_NAMES = ("Seconds_Behind_Source", "Seconds_Behind_Master")


def lag_from_replica_status(cols, row):
    """Returns (seconds_or_None, column_name_or_None). None seconds with a found column
    means NULL (SQL/applier thread not running) — needs review, not zero lag."""
    for name in LAG_COLUMN_NAMES:
        if name in cols:
            idx = cols.index(name)
            val = row[idx] if idx < len(row) else None
            if val is None or str(val).upper() == "NULL" or str(val) == "":
                return None, name
            return float(val), name
    return None, None


def _start_consistent_read(family, conn):
    """Best-effort single-instant read for everything this connection checks this run —
    row-count and checksum are otherwise independent statements that can straddle a write
    landing on the table between them, producing a false mismatch that has nothing to do
    with real replication drift. True snapshot isolation is only guaranteed WITHIN one
    engine's own connection — there is no distributed transaction spanning source+target,
    so a small residual source-vs-target skew (the time between opening each connection)
    is accepted, not eliminated; this only removes the WITHIN-one-side inconsistency."""
    if family == "mysql":
        _query(family, conn, "START TRANSACTION WITH CONSISTENT SNAPSHOT")
    else:
        conn.run("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def _end_consistent_read(family, conn):
    # Release promptly — an open REPEATABLE READ/consistent-snapshot transaction held
    # open longer than necessary can pin resources (e.g. hold back VACUUM on Postgres).
    try:
        if family == "mysql":
            _query(family, conn, "COMMIT")
        else:
            conn.run("COMMIT")
    except Exception:
        pass


def _table_parts(table):
    pattern = r'\s*(?:"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|([^.`"\s][^.`"]*?))\s*(\.|$)'
    parts = []
    offset = 0
    for match in re.finditer(pattern, table):
        if match.start() != offset:
            raise ValueError(f"Invalid table identifier: {table!r}")
        quoted, backtick, plain, separator = match.groups()
        parts.append(quoted.replace('""', '"') if quoted is not None else
                     backtick.replace('``', '`') if backtick is not None else plain.strip())
        offset = match.end()
    if offset != len(table) or not 1 <= len(parts) <= 2 or separator == '.':
        raise ValueError(f"Invalid table identifier: {table!r}")
    return parts


def _table_sql(family, table):
    quote = '`' if family == 'mysql' else '"'
    return '.'.join(quote + part.replace(quote, quote * 2) + quote for part in _table_parts(table))


def _sql_literal(family, value):
    if family == "mysql":
        return "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _columns_sql(family, table):
    parts = _table_parts(table)
    name = _sql_literal(family, parts[-1])
    schema = (_sql_literal(family, parts[0]) if len(parts) == 2 else
              "DATABASE()" if family == "mysql" else "current_schema()")
    if family == "mysql":
        return ("SELECT column_name, column_type, is_nullable, column_default FROM "
                f"information_schema.columns WHERE table_schema={schema} AND table_name={name} ORDER BY column_name")
    return ("SELECT attribute.attname, pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
            "CASE WHEN attribute.attnotnull THEN 'NO' ELSE 'YES' END, "
            "pg_catalog.pg_get_expr(defaults.adbin, defaults.adrelid) "
            "FROM pg_catalog.pg_attribute attribute JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid "
            "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_attrdef defaults ON defaults.adrelid=attribute.attrelid AND defaults.adnum=attribute.attnum "
            f"WHERE namespace.nspname={schema} AND relation.relname={name} "
            "AND attribute.attnum>0 AND NOT attribute.attisdropped ORDER BY attribute.attname")


def _green_streak(days, current_date):
    days.sort(key=lambda day: day["date"])
    expected = datetime.date.fromisoformat(current_date)
    consecutive = 0
    for day in reversed(days):
        if (datetime.date.fromisoformat(day["date"]) != expected
                or day.get("overall") != "green" or day.get("needs_agent_review")
                or any(value is not True and value != "not_applicable"
                       for value in day.get("checks", {}).values())):
            break
        consecutive += 1
        expected -= datetime.timedelta(days=1)
    return consecutive


# Single source of truth for the per-table SQL — the normal run AND preflight build their
# statements from these, so preflight probes exactly the objects/privileges a run touches.
def _row_count_sql(family, table):
    return f"SELECT COUNT(*) FROM {_table_sql(family, table)}"


def _checksum_sql(family, table, probe=False):
    """probe=True: same statement shape and privilege requirement, bounded cost — MySQL
    `CHECKSUM TABLE ... QUICK` (no scan on InnoDB), Postgres the same md5(string_agg) over
    a one-row subquery."""
    if family == "mysql":
        return f"CHECKSUM TABLE {_table_sql(family, table)}" + (" QUICK" if probe else "")
    src = f"(SELECT * FROM {_table_sql(family, table)} LIMIT 1)" if probe else _table_sql(family, table)
    return f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {src} t"


def _select_probe_sql(family, table):
    """Same table + SELECT privilege as _row_count_sql, without a full count."""
    return f"SELECT 1 FROM {_table_sql(family, table)} LIMIT 1"


def row_count(family, conn, table):
    rows = _query(family, conn, _row_count_sql(family, table))
    return int(rows[0][0]) if rows else None


def checksum(family, conn, table):
    rows = _query(family, conn, _checksum_sql(family, table))
    if family == "mysql":
        return str(rows[0][1]) if rows and rows[0][1] is not None else None
    return rows[0][0] if rows and rows[0][0] is not None else None


def columns(family, conn, table):
    """Column fingerprint keyed by name -> {type, nullable, default} — strengthened from
    a name-only comparison (the confirmed gap: two tables with identically-named columns
    of different types/nullability/defaults used to report no drift at all)."""
    rows = _query(family, conn, _columns_sql(family, table))
    return {
        str(r[0]).strip(): {"type": str(r[1]), "nullable": str(r[2]),
                             "default": (str(r[3]) if r[3] is not None else None)}
        for r in rows if r[0]
    }


def cloudwatch_alarms(alarm_names, aws_errors=None):
    """Returns (firing_names, unknown_names, check) where check is True (all OK), False
    (something is actually firing), None (nothing firing but at least one alarm came back
    INSUFFICIENT_DATA or wasn't found at all — needs_agent_review, NOT a silent pass), or
    "not_applicable" (no alarms configured for this engagement)."""
    if not alarm_names:
        return [], [], "not_applicable"
    try:
        resp = _aws("alarms", "cloudwatch:DescribeAlarms", ",".join(alarm_names),
                    _cloudwatch.describe_alarms, AlarmNames=alarm_names)
    except AwsCallError as e:
        _record(aws_errors, e)
        return [], list(alarm_names), None  # API call itself failed -> needs review, not a crash
    states = {a["AlarmName"]: a.get("StateValue") for a in resp.get("MetricAlarms", [])}
    firing = [n for n, s in states.items() if s == "ALARM"]
    unknown = [n for n in alarm_names if states.get(n) in (None, "INSUFFICIENT_DATA")]
    if firing:
        return firing, unknown, False
    if unknown:
        return firing, unknown, None
    return firing, unknown, True


def db_headroom_pct(db_instance_id, aws_errors=None):
    """FreeStorageSpace as % of AllocatedStorage — a proxy for storage headroom. Returns
    "not_applicable" for an Aurora-family instance — confirmed live against a real Aurora
    PostgreSQL writer that Aurora instances report a placeholder AllocatedStorage
    (observed: 1) and publish NO FreeStorageSpace datapoints at all (Aurora storage
    auto-scales; there is no fixed allocation to measure headroom against), which used to
    make this permanently return None (needs_agent_review) for every Aurora target — the
    skill's primary target engine — keeping state stuck at "active" forever instead of
    ever reaching "complete". Returns None on any OTHER failure to get a real, current
    datapoint (missing instance, missing metric, INSUFFICIENT_DATA) — the caller decides
    whether that means "not configured" (excluded) or "needs review" (missing data must
    never look like a silent pass, and must never crash the whole invocation either —
    DBInstanceNotFound is exactly the kind of thing that should surface as "needs
    review", not an unhandled exception)."""
    try:
        desc = _aws("headroom", "rds:DescribeDBInstances", f"db:{db_instance_id}",
                    _rds.describe_db_instances, DBInstanceIdentifier=db_instance_id)
    except AwsCallError as e:
        _record(aws_errors, e)
        return None
    instances = desc.get("DBInstances", [])
    if not instances:
        return None
    engine = instances[0].get("Engine") or ""
    if engine.lower().startswith("aurora"):
        return "not_applicable"
    allocated_gb = instances[0].get("AllocatedStorage")
    if not allocated_gb:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        stats = _aws("headroom", "cloudwatch:GetMetricStatistics", "* (AWS/RDS FreeStorageSpace)",
                     _cloudwatch.get_metric_statistics,
                     Namespace="AWS/RDS", MetricName="FreeStorageSpace",
                     Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
                     StartTime=now - datetime.timedelta(minutes=30), EndTime=now,
                     Period=1800, Statistics=["Average"])
    except AwsCallError as e:
        _record(aws_errors, e)
        return None
    points = stats.get("Datapoints", [])
    if not points:
        return None
    free_gb = float(points[0]["Average"]) / (1024 ** 3)
    return round(100 * free_gb / float(allocated_gb), 1)


def measure_replication_lag(cfg, family, source_conn, target_conn, aws_errors=None):
    """Returns (lag_seconds_or_None, mechanism_or_None). mechanism is one of
    "dms"/"mysql_replica_status"/"postgres_logical_requires_review"/None (nothing configured for
    this engagement — the caller treats that as "not_applicable", not "unknown")."""
    dms_task_id = cfg.get("dms_task_id")
    dms_instance_id = cfg.get("dms_replication_instance_id")
    if dms_task_id and dms_instance_id:
        now = datetime.datetime.now(datetime.timezone.utc)
        best = None
        for metric in ("CDCLatencyTarget", "CDCLatencySource"):
            try:
                stats = _aws("replication_lag", "cloudwatch:GetMetricStatistics", f"* (AWS/DMS {metric})",
                             _cloudwatch.get_metric_statistics,
                             Namespace="AWS/DMS", MetricName=metric,
                             Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": dms_instance_id},
                                         {"Name": "ReplicationTaskIdentifier", "Value": dms_task_id}],
                             StartTime=now - datetime.timedelta(minutes=15), EndTime=now,
                             Period=300, Statistics=["Maximum"])
            except AwsCallError as e:
                _record(aws_errors, e)
                return None, "dms"
            points = stats.get("Datapoints", [])
            if not points:
                return None, "dms"
            if points:
                worst = max(float(p["Maximum"]) for p in points)
                best = worst if best is None else max(best, worst)
        # Missing either required metric cannot establish the lag bound.
        return best, "dms"

    replica_side = cfg.get("mysql_replica_status_side")  # "source" or "target"
    if family == "mysql" and replica_side:
        conn = target_conn if replica_side == "target" else source_conn
        try:
            cols, rows = _query_named(conn, "SHOW REPLICA STATUS")
        except Exception:
            try:
                cols, rows = _query_named(conn, "SHOW SLAVE STATUS")  # pre-8.0.22 / MariaDB syntax
            except Exception:
                return None, "mysql_replica_status"
        if not rows:
            return None, "mysql_replica_status"
        # By column NAME (see LAG_COLUMN_NAMES) — not position.
        val, _col = lag_from_replica_status(cols, rows[0])
        return (val, "mysql_replica_status")

    pg_side = cfg.get("pg_replication_lag_side")  # "source" or "target"
    if family == "postgres" and pg_side:
        return None, "postgres_logical_requires_review"

    return None, None


def replication_errors(cfg, aws_errors=None):
    """Returns (check, detail) where check is True/False/None/"not_applicable" — pulls
    DMS task stats (TablesErrored, task Status, LastFailureMessage) when a DMS task ARN
    is configured; "not_applicable" when nothing is configured (no DMS task in play for
    this engagement — e.g. native logical/binlog replication with no equivalent error
    surface wired yet)."""
    dms_task_arn = cfg.get("dms_task_arn")
    if not dms_task_arn:
        return "not_applicable", {}
    try:
        resp = _aws("replication_errors", "dms:DescribeReplicationTasks", dms_task_arn,
                    _dms.describe_replication_tasks,
                    Filters=[{"Name": "replication-task-arn", "Values": [dms_task_arn]}])
    except AwsCallError as e:
        _record(aws_errors, e)
        return None, {"error": str(e)}
    tasks = resp.get("ReplicationTasks", [])
    if not tasks:
        return None, {"error": "DMS task not found for configured ARN"}
    task = tasks[0]
    stats = task.get("ReplicationTaskStats", {}) or {}
    tables_errored = stats.get("TablesErrored", 0)
    status = task.get("Status")
    last_failure = task.get("LastFailureMessage")
    detail = {"status": status, "tables_errored": tables_errored, "last_failure_message": last_failure}
    ok = (tables_errored == 0) and (status == "running") and not last_failure
    return ok, detail


def run_day(cfg, source_conn, target_conn):
    """checks{} four-state model:
      True/False  — measured and passed/failed.
      None        — missing/INSUFFICIENT_DATA/unreachable, or pending full-period evidence
                    review — needs_agent_review,
                    never a silent pass.
      "not_applicable" — genuinely nothing configured for this check on this engagement
                    (e.g. no DMS task, no customer test suite per Q18) — excluded from
                    BOTH the green calculation AND needs_agent_review, so a clean day can
                    actually reach `state: complete` instead of being permanently stuck.
    """
    source_family = _engine_family(cfg["source_engine"])
    target_family = _engine_family(cfg["target_engine"])
    if source_family != target_family:
        raise ValueError(
            f"source_engine={cfg['source_engine']!r} and target_engine={cfg['target_engine']!r} "
            "normalize to different SQL families — heterogeneous soak-checking is not yet "
            "supported by this script (see module docstring)."
        )
    family = source_family
    tables = cfg["tables"]
    if not isinstance(tables, list) or not tables or not all(isinstance(table, str) and table for table in tables):
        raise ValueError("tables must be a nonempty list of table identifiers")

    aws_errors = []
    _start_consistent_read(family, source_conn)
    _start_consistent_read(family, target_conn)
    try:
        row_check = {"pass": True, "detail": {}}
        for t in tables:
            sc, tc = row_count(family, source_conn, t), row_count(family, target_conn, t)
            row_check["detail"][t] = {"source": sc, "target": tc}
            if sc != tc or sc is None:
                row_check["pass"] = False

        checksum_tables = cfg.get("checksum_tables") or tables[:2]
        checksum_check = {"pass": True, "detail": {}}
        for t in checksum_tables:
            sc, tc = checksum(family, source_conn, t), checksum(family, target_conn, t)
            checksum_check["detail"][t] = {"source": sc, "target": tc}
            if sc != tc or sc is None:
                checksum_check["pass"] = False

        drift_check = {"pass": True, "detail": {}}
        for t in tables:
            sc, tc = columns(family, source_conn, t), columns(family, target_conn, t)
            if sc != tc or not sc or not tc:
                drift_check["pass"] = False
                mismatched = sorted(k for k in (set(sc) & set(tc)) if sc[k] != tc[k])
                drift_check["detail"][t] = {
                    "source_only": sorted(set(sc) - set(tc)),
                    "target_only": sorted(set(tc) - set(sc)),
                    "mismatched_attributes": {k: {"source": sc[k], "target": tc[k]} for k in mismatched},
                }

        lag_seconds, lag_mechanism = measure_replication_lag(cfg, family, source_conn, target_conn, aws_errors)
    finally:
        _end_consistent_read(family, source_conn)
        _end_consistent_read(family, target_conn)

    firing_alarms, unknown_alarms, alarms_check = cloudwatch_alarms(cfg.get("alarm_names", []), aws_errors)

    target_db_instance_id = cfg.get("target_db_instance_id")
    if not target_db_instance_id:
        headroom_check, headroom = "not_applicable", None
    else:
        headroom = db_headroom_pct(target_db_instance_id, aws_errors)
        if headroom == "not_applicable":
            headroom_check, headroom = "not_applicable", None
        else:
            headroom_check = None if headroom is None else (headroom > HEADROOM_THRESHOLD_PCT)

    if lag_mechanism is None:
        lag_check = "not_applicable"
    else:
        lag_check = None if lag_seconds is None else (lag_seconds <= LAG_THRESHOLD_S)

    repl_errors_check, repl_errors_detail = replication_errors(cfg, aws_errors)

    customer_test_suite_check = None if cfg.get("customer_test_suite_provided") else "not_applicable"

    checks = {
        "row_count": row_check["pass"],
        "checksum": checksum_check["pass"],
        "alarms": alarms_check,
        "headroom": headroom_check,
        "schema_drift": drift_check["pass"],
        "replication_lag": lag_check,
        "replication_errors": repl_errors_check,
        "customer_test_suite": customer_test_suite_check,
        "period_evidence": None,
    }
    measured = [value for value in checks.values() if value != "not_applicable"]
    overall_green = bool(measured) and all(value is True for value in measured)
    needs_review = (not overall_green) or any(v is None for v in checks.values())

    detail = {"row_count": row_check["detail"], "checksum": checksum_check["detail"],
              "schema_drift": drift_check["detail"], "firing_alarms": firing_alarms,
              "unknown_alarms": unknown_alarms, "headroom_pct": headroom,
              "replication_lag_seconds": lag_seconds, "replication_lag_mechanism": lag_mechanism,
              "replication_errors": repl_errors_detail}
    if aws_errors:
        # Additive key, present only when an AWS call failed: names the exact IAM action +
        # resource so the fix is one targeted change (see module docstring).
        detail["aws_errors"] = aws_errors
    return {
        "date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "checks": checks,
        "detail": detail,
        "overall": "green" if overall_green else "red",
        "needs_agent_review": needs_review,
    }


# ── S3-backed equivalents of soak_check.py's local-file functions ──────────────────────

_CAS_MAX_ATTEMPTS = 8  # genuinely concurrent writers to the SAME key are rare (this
                        # Lambda runs once daily) — a bounded handful of retries, not
                        # unbounded backoff. A small random jitter between attempts
                        # (below) keeps a burst of simultaneous retriers from repeatedly
                        # colliding on the exact same S3 request round-trip.


def _cas_jitter_sleep():
    time.sleep(random.uniform(0.05, 0.25))


def _s3_get_with_etag(bucket, key, client=None):
    """Returns (raw_bytes_or_None, etag_or_None). etag=None means the object doesn't
    exist yet — the caller conditions its PUT on IfNoneMatch:'*' in that case instead of
    IfMatch, so two invocations racing to create the object for the first time can't
    both "succeed" and one silently clobber the other either."""
    try:
        resp = (client or _s3).get_object(Bucket=bucket, Key=key)
        return resp["Body"].read(), resp["ETag"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None
        err = AwsCallError("s3_read", "s3:GetObject", _s3_arn(bucket, key), e)
        if err.result == "AccessDenied":
            err.message += (" (note: without s3:ListBucket, S3 also answers AccessDenied — not "
                            "NoSuchKey — for an object that does not exist yet)")
        raise err from e
    except BotoCoreError as e:
        raise AwsCallError("s3_read", "s3:GetObject", _s3_arn(bucket, key), e) from e


def _s3_put_conditional(bucket, key, body_bytes, content_type, etag):
    kwargs = {"Bucket": bucket, "Key": key, "Body": body_bytes, "ContentType": content_type}
    kwargs["IfNoneMatch" if etag is None else "IfMatch"] = "*" if etag is None else etag
    _s3.put_object(**kwargs)


def _is_precondition_failed(e):
    # Confirmed live under a genuinely concurrent write burst: S3 doesn't always return
    # the classic 412 "PreconditionFailed" for a conditional-write loss — it can also
    # return HTTP 409 with error code "ConditionalRequestConflict" for the same "someone
    # else won the race" situation. Treat both as retryable; anything else is a real error.
    code = e.response.get("Error", {}).get("Code")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412)


def update_status_json(bucket, key, day_result, n_total):
    """Idempotent per calendar day AND safe against a genuinely concurrent writer to the
    same status.json — EventBridge Scheduler retries are at-least-once (handled by the
    per-date overwrite-in-place below), but a bare GET-modify-PUT is ALSO non-atomic if
    two invocations happen to overlap in wall-clock time: both read the same starting
    version, both compute their own "new" version, and whichever PUTs last silently wins,
    discarding the other's write. `IfMatch`/`IfNoneMatch` (optimistic concurrency via
    ETag) closes that gap: a PUT that lost the race to a genuinely concurrent writer gets
    HTTP 412, and this function re-reads the fresh version and reapplies the same
    mutation against it, rather than either blindly overwriting or silently losing data.

    consecutive_green is recomputed from days[] itself on every attempt (the trailing run
    of "green" entries), never incremented — that's what makes overwriting a day (retry,
    concurrent-write reapplication, or a correction) always self-consistent instead of
    drifting from what days[] actually contains."""
    for attempt in range(_CAS_MAX_ATTEMPTS):
        raw, etag = _s3_get_with_etag(bucket, key)
        status = json.loads(raw) if raw else {}
        soak = status.setdefault("soak", {"days": [], "n_total": n_total, "consecutive_green": 0, "state": "active"})
        days = soak["days"]
        existing_idx = next((i for i, d in enumerate(days) if d.get("date") == day_result["date"]), None)
        if existing_idx is not None:
            days[existing_idx] = day_result
        else:
            days.append(day_result)
        consecutive = _green_streak(days, datetime.datetime.now(datetime.timezone.utc).date().isoformat())
        soak["consecutive_green"] = consecutive
        soak["n_total"] = n_total
        # S3 is now the single source of truth during the soak window — this timestamp is
        # what lets the dashboard flag a missed scheduled run (Lambda didn't fire, or
        # errored before writing) — see execution-runbooks.md §Soak automation.
        soak["last_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        soak["state"] = "complete" if consecutive >= n_total else "active"
        status["soak"] = soak
        status["updated_at"] = soak["last_checked_at"]
        try:
            _s3_put_conditional(bucket, key, json.dumps(status, indent=2, ensure_ascii=False).encode("utf-8"),
                                 "application/json", etag)
            return status
        except ClientError as e:
            if _is_precondition_failed(e) and attempt < _CAS_MAX_ATTEMPTS - 1:
                _cas_jitter_sleep()
                continue
            raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
        except BotoCoreError as e:
            raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
    raise RuntimeError(f"Could not write {key} after {_CAS_MAX_ATTEMPTS} attempts (concurrent writers)")


def append_activity_log(bucket, key, day_result):
    """Idempotent per calendar day (same reasoning as update_status_json — a retried
    invocation for a day already logged replaces that day's line in place instead of
    appending a second one, matched on phase=="7.7" AND date==today so this never
    touches lines the agent wrote by hand for other phases) AND safe against a
    concurrent writer via the same ETag-conditional retry loop — S3 objects have no
    native append, so "append a line" is GET-modify-PUT under the hood, and that's
    non-atomic without this."""
    entry = {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "phase": "7.7",
        "date": day_result["date"],
        "title": f"Soak day {day_result['date']} — {day_result['overall']}",
        "action": "automated soak_check_lambda.py run (row count, checksum, schema drift, "
                   "alarms, headroom, replication lag/errors)",
        # activity-log.jsonl's documented vocabulary is success|in_progress|blocked
        # (dashboard.md) — map the day verdict onto it rather than writing "green"/"red"
        # (which dashboard.js's activity-log renderer doesn't recognize and used to
        # silently default to a green checkmark even on a RED day).
        "result": "success" if day_result["overall"] == "green" else "blocked",
        "detail": "needs_agent_review" if day_result["needs_agent_review"] else "all mechanical checks green",
        "files": [],
    }
    for attempt in range(_CAS_MAX_ATTEMPTS):
        raw, etag = _s3_get_with_etag(bucket, key)
        existing = raw.decode("utf-8") if raw else ""
        kept = []
        for line in existing.split("\n"):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)  # never silently drop a line this script doesn't understand
                continue
            if parsed.get("phase") == "7.7" and parsed.get("date") == entry["date"]:
                continue  # superseded by the fresh entry below (retry of the same day)
            kept.append(line)
        kept.append(json.dumps(entry, ensure_ascii=False))
        updated = "\n".join(kept) + "\n"
        try:
            _s3_put_conditional(bucket, key, updated.encode("utf-8"), "application/x-ndjson", etag)
            return entry
        except ClientError as e:
            if _is_precondition_failed(e) and attempt < _CAS_MAX_ATTEMPTS - 1:
                _cas_jitter_sleep()
                continue
            raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
        except BotoCoreError as e:
            raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
    raise RuntimeError(f"Could not write {key} after {_CAS_MAX_ATTEMPTS} attempts (concurrent writers)")


def write_soak_report(bucket, reports_prefix, day_result, day_n, n_total, consecutive_green):
    checks = day_result["checks"]

    def _mark(v):
        if v is True:
            return "▢ pass"
        if v is False:
            return "▢ FAIL"
        if v == "not_applicable":
            return "▢ n/a"
        return "▢ NEEDS AGENT REVIEW"

    key = f"{reports_prefix}soak-report-day{day_n}.md"
    summary = (
        f"# Soak Report — Day {day_n} of {n_total}\n\n"
        f"Consecutive green counter: {consecutive_green}/{n_total} "
        f"(any RED resets it to 0)\n\n"
        f"## Verdict: {'🟢 GREEN' if day_result['overall'] == 'green' else '🔴 RED'}\n\n"
        f"| Check | Pass |\n|---|:---:|\n"
        + "".join(f"| {k} | {_mark(v)} |\n" for k, v in checks.items())
        + f"\n## Detail\n```json\n{json.dumps(day_result['detail'], indent=2, ensure_ascii=False)}\n```\n"
    )
    _aws("s3_write", "s3:PutObject", _s3_arn(bucket, key), _s3.put_object,
         Bucket=bucket, Key=key, Body=summary.encode("utf-8"), ContentType="text/markdown")
    return key


def _env(name):
    """Optional env var: CDK sets `?? ''`, so empty string == not configured."""
    return os.environ.get(name) or None


def _load_config():
    cfg = {
        "source_engine": os.environ["SOURCE_ENGINE"],
        "target_engine": os.environ["TARGET_ENGINE"],
        "tables": json.loads(os.environ["TABLES"]),
        "checksum_tables": _env_json("CHECKSUM_TABLES", None),
        "alarm_names": _env_json("ALARM_NAMES", []),
        "target_db_instance_id": _env("TARGET_DB_INSTANCE_ID"),
        "dms_task_id": _env("DMS_TASK_ID"),
        "dms_replication_instance_id": _env("DMS_REPLICATION_INSTANCE_ID"),
        "dms_task_arn": _env("DMS_TASK_ARN"),
        "mysql_replica_status_side": _env("MYSQL_REPLICA_STATUS_SIDE"),
        "pg_replication_lag_side": _env("PG_REPLICATION_LAG_SIDE"),
        "customer_test_suite_provided": os.environ.get("CUSTOMER_TEST_SUITE_PROVIDED", "false").lower() == "true",
        "n_total": int(os.environ["N_TOTAL"]),
    }
    # Checked FIRST, before any Secrets Manager read or DB connection attempt — a
    # misconfigured heterogeneous pair (or an engine string outside both supported
    # families) is a config error diagnosable from the environment variables alone; there
    # is no reason to spend a GetSecretValue call or open a live connection to either
    # database (using a real, dedicated credential) before failing on it. run_day() below
    # still re-checks this itself (defense-in-depth for any other/direct caller), but by
    # the time this invocation would have reached that check, it would have already
    # touched two live databases for nothing.
    source_family = _engine_family(cfg["source_engine"])
    target_family = _engine_family(cfg["target_engine"])
    if source_family != target_family:
        raise ValueError(
            f"source_engine={cfg['source_engine']!r} and target_engine={cfg['target_engine']!r} "
            "normalize to different SQL families — heterogeneous soak-checking is not yet "
            "supported by this script (see module docstring). No secret was read and no "
            "database connection was attempted for this invocation."
        )

    if not isinstance(cfg["tables"], list) or not cfg["tables"]:
        raise ValueError("TABLES must be a nonempty list of table identifiers")
    for table in cfg["tables"]:
        _table_parts(table)
    cfg["family"] = source_family
    return cfg


def _side_settings(side):
    """Connection settings for "SOURCE"/"TARGET". Source and target very often have
    DIFFERENT trust anchors — an on-prem/legacy source with a self-signed or private-CA
    certificate vs. an RDS/Aurora target with an Amazon-issued one — so CA path and the
    opt-in skip-verify flag are independent per side, never one shared path. Leaving the CA
    unset falls back to the bundled AWS RDS/Aurora CA bundle for that side (still full TLS,
    chain-verified — see _tls_context/_DEFAULT_CA_BUNDLE — NOT the platform default trust
    store, which is confirmed live to lack the current RDS root). Skip-verify is the
    explicit, per-side, opt-in-only "encrypt but don't verify" fallback — see
    _tls_context's tier 3 docstring. Never defaults to true."""
    return {
        "secret_arn": os.environ[f"{side}_SECRET_ARN"],
        "host": os.environ[f"{side}_HOST"], "port": os.environ[f"{side}_PORT"],
        "database": os.environ[f"{side}_DB"],
        "ssl_ca_path": _env(f"{side}_SSL_CA_PATH"),
        "ssl_insecure": os.environ.get(f"{side}_TLS_SKIP_VERIFY", "false").lower() == "true",
    }


class _Unverified(Exception):
    """A probe that could not prove the permission without mutating data."""


# Stop starting new probes when less than this is left: one probe is bounded by the
# preflight client config (2 x (3s connect + 5s read)) or the DB timeouts (5s + 10s).
_PREFLIGHT_RESERVE_MS = 20000
# A deliberately wrong ETag: a conditional PUT with it must be rejected (412/409) by S3
# AFTER authorization — so 412 proves s3:PutObject is allowed, without writing anything.
_WRONG_ETAG = '"00000000000000000000000000000000"'


def _preflight_row(check, iam_action, resource, fn):
    """Run one probe; never raises. Returns (row, value)."""
    try:
        value = fn()
        return {"check": check, "iam_action": iam_action, "resource": resource,
                "result": "PASS", "message": "ok"}, value
    except AwsCallError as e:
        row = e.as_dict()
        row["check"] = check
        return row, None
    except _Unverified as e:
        return {"check": check, "iam_action": iam_action, "resource": resource,
                "result": "UNVERIFIED", "message": str(e)}, None
    except Exception as e:  # DB driver / TLS / socket errors — reported, not raised
        return {"check": check, "iam_action": iam_action, "resource": resource,
                "result": "Error", "message": f"{type(e).__name__}: {e}"}, None


class _Preflight:
    def __init__(self, context):
        self.rows, self.context = [], context

    def _budget_ok(self):
        remaining = getattr(self.context, "get_remaining_time_in_millis", None)
        return remaining is None or remaining() > _PREFLIGHT_RESERVE_MS

    def emit(self, row):
        self.rows.append(row)
        # One line per probe AS IT COMPLETES — survives even if the function is killed later.
        print("SOAK_PREFLIGHT_ROW " + json.dumps(row, ensure_ascii=False), flush=True)

    def run(self, check, action, resource, fn):
        if not self._budget_ok():
            self.emit({"check": check, "iam_action": action, "resource": resource, "result": "SKIPPED",
                       "message": "time budget: too close to the function timeout — fix earlier rows, "
                                  "re-run preflight (or raise the function timeout)"})
            return False, None
        row, value = _preflight_row(check, action, resource, fn)
        self.emit(row)
        return row["result"] == "PASS", value


def _pf_s3_read(pf_s3, bucket, key):
    """(body, etag, sse_kms_key) — body/etag None if the key is absent."""
    try:
        resp = pf_s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None, None
        raise AwsCallError("s3_read", "s3:GetObject", _s3_arn(bucket, key), e) from e
    except BotoCoreError as e:
        raise AwsCallError("s3_read", "s3:GetObject", _s3_arn(bucket, key), e) from e
    kms_key = resp.get("SSEKMSKeyId") if resp.get("ServerSideEncryption") == "aws:kms" else None
    return resp["Body"].read(), resp["ETag"], kms_key


def _pf_s3_write_probe(pf_s3, bucket, key, body, content_type, absent_ok):
    """Prove s3:PutObject on `key` WITHOUT changing it: conditional PUT with a wrong ETag.
    412/409 = authorized (precondition evaluated after authz); 404 on an absent key with
    absent_ok = authorized; AccessDenied = missing grant. If S3 ever accepted it, the body is
    the object's current bytes, so content is unchanged (only a new version)."""
    try:
        pf_s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, IfMatch=_WRONG_ETAG)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if _is_precondition_failed(e) or (absent_ok and code in ("NoSuchKey", "404")):
            return code
        raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
    except BotoCoreError as e:
        raise AwsCallError("s3_write", "s3:PutObject", _s3_arn(bucket, key), e) from e
    raise RuntimeError("S3 accepted a PUT with a deliberately wrong IfMatch ETag (content rewritten "
                       "byte-identical) — conditional writes are not being enforced; the CAS logic "
                       "depends on them. Investigate before enabling the schedule.")


def run_preflight(cfg, bucket, prefix, context=None):
    """Exercise every AWS call + DB access a normal run makes — same table list, same SQL
    builders (_select_probe_sql/_columns_sql/_checksum_sql), both DMS metrics, all alarms,
    every S3 key — WITHOUT writing anything: writes are proven with a wrong-ETag conditional
    PUT (412 = allowed). KMS: reads exercise kms:Decrypt; a non-mutating preflight cannot
    exercise kms:GenerateDataKey for writes — that row says so when the bucket is SSE-KMS."""
    family = cfg["family"]
    pf_clients = _preflight_clients()
    pf = _Preflight(context)
    not_configured = []
    tables = cfg["tables"]
    checksum_tables = cfg.get("checksum_tables") or tables[:2]
    now = datetime.datetime.now(datetime.timezone.utc)

    for side in ("SOURCE", "TARGET"):
        sl = side.lower()
        st = _side_settings(side)
        ok, creds = pf.run(f"{sl}_secret", "secretsmanager:GetSecretValue (+kms:Decrypt if CMK)", st["secret_arn"],
                           lambda st=st, sl=sl: _get_secret(st["secret_arn"], f"{sl}_secret", pf_clients.secrets))
        endpoint = f"{st['host']}:{st['port']}/{st['database']}"
        if not ok:
            pf.emit({"check": f"{sl}_db_connect", "iam_action": "n/a (DB login; network: SG/route to DB)",
                     "resource": endpoint, "result": "SKIPPED",
                     "message": "secret could not be read — fix the row above first"})
            continue
        ok, conn = pf.run(f"{sl}_db_connect", "n/a (DB login + TLS; network: SG/route to DB)", endpoint,
                          lambda st=st, creds=creds: _connect(family, st["host"], st["port"], creds, st["database"],
                                                              st["ssl_ca_path"], st["ssl_insecure"],
                                                              connect_timeout=5, read_timeout=10))
        if not ok:
            continue
        try:
            for t in tables:
                def probe_table(t=t, conn=conn):
                    _query(family, conn, _select_probe_sql(family, t))
                    if not _query(family, conn, _columns_sql(family, t)):
                        raise RuntimeError("no columns visible in the catalog — table missing or no "
                                           "privilege on it (schema-drift check would fail)")
                pf.run(f"{sl}_db_select {t}", "n/a (DB grant: SELECT)", f"{endpoint} {t}", probe_table)
            for t in checksum_tables:
                pf.run(f"{sl}_db_checksum {t}", "n/a (DB grant: SELECT)", f"{endpoint} {t}",
                       lambda t=t, conn=conn: _query(family, conn, _checksum_sql(family, t, probe=True)))
            if family == "mysql" and cfg.get("mysql_replica_status_side") == sl:
                def probe_replica(conn=conn):
                    try:
                        cols, rrows = _query_named(conn, "SHOW REPLICA STATUS")
                    except Exception:
                        cols, rrows = _query_named(conn, "SHOW SLAVE STATUS")
                    if not rrows:
                        raise RuntimeError("SHOW REPLICA STATUS returned no rows — replication not configured "
                                           "on this side, or the user lacks REPLICATION CLIENT")
                    if lag_from_replica_status(cols, rrows[0])[1] is None:
                        raise RuntimeError(f"no lag column ({'/'.join(LAG_COLUMN_NAMES)}) in replica status")
                pf.run(f"{sl}_db_replica_status", "n/a (DB grant: REPLICATION CLIENT)", endpoint, probe_replica)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if cfg.get("alarm_names"):
        names = cfg["alarm_names"]

        def probe_alarms():
            resp = _aws("alarms", "cloudwatch:DescribeAlarms", ",".join(names),
                        pf_clients.cloudwatch.describe_alarms, AlarmNames=names)
            found = {a["AlarmName"] for a in resp.get("MetricAlarms", [])}
            missing = [n for n in names if n not in found]
            if missing:
                raise RuntimeError(f"alarm(s) not found: {missing} — the normal run would report them unknown")
        pf.run("alarms", "cloudwatch:DescribeAlarms", ",".join(names), probe_alarms)
    else:
        not_configured.append("alarms (ALARM_NAMES empty)")

    db_id = cfg.get("target_db_instance_id")
    if db_id:
        pf.run("headroom_describe", "rds:DescribeDBInstances", f"db:{db_id}",
               lambda: _aws("headroom_describe", "rds:DescribeDBInstances", f"db:{db_id}",
                            pf_clients.rds.describe_db_instances, DBInstanceIdentifier=db_id))
        pf.run("headroom_metric", "cloudwatch:GetMetricStatistics", "* (AWS/RDS FreeStorageSpace)",
               lambda: _aws("headroom_metric", "cloudwatch:GetMetricStatistics", "*",
                            pf_clients.cloudwatch.get_metric_statistics, Namespace="AWS/RDS",
                            MetricName="FreeStorageSpace",
                            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                            StartTime=now - datetime.timedelta(minutes=30), EndTime=now,
                            Period=1800, Statistics=["Average"]))
    else:
        not_configured.append("headroom (TARGET_DB_INSTANCE_ID empty)")

    if cfg.get("dms_task_id") and cfg.get("dms_replication_instance_id"):
        dims = [{"Name": "ReplicationInstanceIdentifier", "Value": cfg["dms_replication_instance_id"]},
                {"Name": "ReplicationTaskIdentifier", "Value": cfg["dms_task_id"]}]
        for metric in ("CDCLatencyTarget", "CDCLatencySource"):  # same pair as measure_replication_lag
            pf.run(f"dms_lag_metric {metric}", "cloudwatch:GetMetricStatistics", f"* (AWS/DMS {metric})",
                   lambda metric=metric: _aws("dms_lag_metric", "cloudwatch:GetMetricStatistics", "*",
                                              pf_clients.cloudwatch.get_metric_statistics, Namespace="AWS/DMS",
                                              MetricName=metric, Dimensions=dims,
                                              StartTime=now - datetime.timedelta(minutes=15), EndTime=now,
                                              Period=300, Statistics=["Maximum"]))
    else:
        not_configured.append("dms lag metrics (DMS_TASK_ID/DMS_REPLICATION_INSTANCE_ID empty)")
    if cfg.get("dms_task_arn"):
        arn = cfg["dms_task_arn"]
        pf.run("dms_task", "dms:DescribeReplicationTasks", arn,
               lambda: _aws("dms_task", "dms:DescribeReplicationTasks", arn, pf_clients.dms.describe_replication_tasks,
                            Filters=[{"Name": "replication-task-arn", "Values": [arn]}]))
    else:
        not_configured.append("dms replication errors (DMS_TASK_ARN empty)")

    # S3: every key the normal run reads/writes. Never creates or changes anything.
    for key, ctype in ((f"{prefix}status.json", "application/json"),
                       (f"{prefix}activity-log.jsonl", "application/x-ndjson")):
        ok, got = pf.run(f"s3_get {key}", "s3:GetObject (+kms:Decrypt if CMK)", _s3_arn(bucket, key),
                         lambda key=key: _pf_s3_read(pf_clients.s3, bucket, key))

        def probe_write(key=key, ctype=ctype, got=got, read_ok=ok):
            if not read_ok:
                raise _Unverified("read failed — fix the s3_get row first")
            body, etag, kms_key = got
            if etag is None:
                raise _Unverified("key absent — upload the initial dashboard files, then re-run "
                                  "(preflight never creates it)")
            code = _pf_s3_write_probe(pf_clients.s3, bucket, key, body, ctype, absent_ok=False)
            if kms_key:
                raise _Unverified(f"s3:PutObject authorized ({code}); bucket is SSE-KMS ({kms_key}) — "
                                  "kms:GenerateDataKey is only exercised by a real write: check the first "
                                  "scheduled run's log")
        pf.run(f"s3_put {key}", "s3:PutObject (conditional; +kms:GenerateDataKey if CMK)", _s3_arn(bucket, key),
               probe_write)
    report_key = f"{prefix}reports/soak-report-day1.md"
    pf.run(f"s3_put {prefix}reports/*", "s3:PutObject", _s3_arn(bucket, f"{prefix}reports/*"),
           lambda: _pf_s3_write_probe(pf_clients.s3, bucket, report_key, b"", "text/markdown", absent_ok=True))

    rows = pf.rows
    passed = all(r["result"] == "PASS" for r in rows)
    print("SOAK_PREFLIGHT " + ("ALL PASS" if passed else
                               "GAPS FOUND — fix every non-PASS row in one change, redeploy once (if needed), re-run"))
    for r in rows:
        print(f"  {r['result']:<12} {r['check']:<36} {r['iam_action']:<45} {r['resource']}"
              + ("" if r["result"] == "PASS" else f"\n               -> {r['message']}")
              + (f"\n               -> KMS key: {r['kms_key']}" if r.get("kms_key") else ""))
    return {"mode": "preflight", "ok": passed, "checks": rows, "not_configured": not_configured}


def handler(event, context):
    cfg = _load_config()
    bucket = os.environ["DASHBOARD_BUCKET"]
    prefix = os.environ.get("DASHBOARD_PREFIX", "")
    if isinstance(event, dict) and event.get("mode") == "preflight":
        return run_preflight(cfg, bucket, prefix, context)
    try:
        return _run_normal(cfg, bucket, prefix)
    except AwsCallError as e:
        # Fatal AWS failure (secret read, S3 read/write): one structured line naming the
        # exact IAM action + resource, then fail the invocation (Lambda Errors metric /
        # alarm) — never a silent pass, never a guess.
        print("SOAK_CHECK_ERROR " + json.dumps(e.as_dict(), ensure_ascii=False))
        raise


def _run_normal(cfg, bucket, prefix):
    family = cfg["family"]
    status_key = f"{prefix}status.json"
    log_key = f"{prefix}activity-log.jsonl"
    reports_prefix = f"{prefix}reports/"
    src, tgt = _side_settings("SOURCE"), _side_settings("TARGET")

    # Dedicated, SELECT-only credentials for THIS Lambda — never the target's admin/master
    # secret (see engagement-safety.md §IAM guardrails; cdk-stacks.md §soak-stack.ts for how
    # the read-only DB user itself is provisioned).
    source_creds = _get_secret(src["secret_arn"], "source_secret")
    target_creds = _get_secret(tgt["secret_arn"], "target_secret")

    source_conn = _connect(family, src["host"], src["port"], source_creds, src["database"],
                            src["ssl_ca_path"], src["ssl_insecure"])
    target_conn = _connect(family, tgt["host"], tgt["port"], target_creds, tgt["database"],
                            tgt["ssl_ca_path"], tgt["ssl_insecure"])
    try:
        day_result = run_day(cfg, source_conn, target_conn)
    finally:
        try:
            source_conn.close()
        except Exception:
            pass
        try:
            target_conn.close()
        except Exception:
            pass

    status = update_status_json(bucket, status_key, day_result, cfg["n_total"])
    append_activity_log(bucket, log_key, day_result)
    day_n = len(status["soak"]["days"])
    write_soak_report(bucket, reports_prefix, day_result, day_n, cfg["n_total"], status["soak"]["consecutive_green"])

    if day_result["needs_agent_review"]:
        # Not a Lambda failure — a legitimate soak result the agent/customer needs to look
        # at. Logged at WARNING (not raised) so a normal RED/needs-review day doesn't trip
        # Lambda's own Errors metric/DLQ; alert on this via the CloudWatch Logs metric
        # filter on "needs_agent_review=true" wired in cdk-stacks.md §soak-stack.ts, or by
        # reading status.json.
        print(f"WARNING day {day_n}: needs_agent_review=true — {day_result['overall']}")
    else:
        print(f"Day {day_n}: {day_result['overall'].upper()}")

    return {"day": day_n, "overall": day_result["overall"], "needs_agent_review": day_result["needs_agent_review"]}
