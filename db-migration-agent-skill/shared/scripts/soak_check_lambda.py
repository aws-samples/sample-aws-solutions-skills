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
yet supported by this script; it raises a clear error at startup rather than silently
running the wrong SQL dialect against one side.

RETRY SAFETY: EventBridge Scheduler's retry policy is at-least-once, not exactly-once — a
retried invocation for a day already recorded in status.json/activity-log.jsonl overwrites
that day's entry in place instead of appending a duplicate or double-incrementing the
green streak (see update_status_json/append_activity_log below).

Deploy shape (see shared/patterns/cdk-stacks.md §Soak automation infra): VPC-attached into
the SAME private subnets + security group the migration bastion already uses (identical
reachability to the source over the existing VPN/DX path, no new networking), triggered
daily by EventBridge Scheduler, IAM scoped to Secrets Manager read (a DEDICATED read-only
credential per side — never the admin/master secret), CloudWatch/RDS/DMS describe, and S3
read/write on the dashboard bucket only.

Config arrives via environment variables (set once by CDK from the same values that would
have gone into soak-config.json for the standalone script) — see the SoakCheckFunction
construct in cdk-stacks.md for exactly which ones and their shapes.
"""
import datetime
import json
import os
import random
import ssl
import time

import boto3
import pymysql
from botocore.exceptions import ClientError

try:
    import pg8000.native as pg8000
except ImportError:  # not needed for a MySQL-only deployment; kept optional to match
    pg8000 = None      # soak_check.py's engine-agnostic shape without forcing the dependency.

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number.
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30

_secrets = boto3.client("secretsmanager")
_cloudwatch = boto3.client("cloudwatch")
_rds = boto3.client("rds")
_s3 = boto3.client("s3")
_dms = boto3.client("dms")

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


def _get_secret(secret_arn):
    resp = _secrets.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _tls_context(ca_path=None, insecure=False):
    """Always negotiate TLS, never allow a silent plaintext fallback. Three tiers:
    1. No ca_path, insecure=False (the common case: an RDS/Aurora endpoint with an
       Amazon-issued cert) — full sslmode=verify-full equivalent: the platform default
       trust store (already trusts the public roots those certs chain to) PLUS hostname
       verification.
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
    ctx = ssl.create_default_context(cafile=ca_path) if ca_path else ssl.create_default_context()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = ca_path is None
    return ctx


def _connect(family, host, port, creds, database, ssl_ca_path=None, ssl_insecure=False):
    user = creds.get("username") or creds.get("user")
    password = creds["password"]
    tls = _tls_context(ssl_ca_path, ssl_insecure)
    if family == "mysql":
        return pymysql.connect(host=host, port=int(port), user=user, password=password,
                                database=database, connect_timeout=10, read_timeout=25,
                                cursorclass=pymysql.cursors.Cursor, ssl=tls)
    if pg8000 is None:
        raise RuntimeError("engine=postgres but pg8000 is not bundled in this deployment")
    return pg8000.Connection(host=host, port=int(port), user=user, password=password,
                             database=database, timeout=10, ssl_context=tls)


def _query(family, conn, sql, params=None):
    """Returns list of row tuples. pg8000.native.Connection.run() and pymysql cursors have
    different call shapes — normalize here so the check functions below stay engine-blind,
    same as soak_check.py's run_mysql/run_psql split, just at the query layer instead of the
    subprocess layer."""
    if family == "mysql":
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())
    # pg8000.native.run returns list-of-dict-like rows keyed by column label; normalize to
    # plain tuples by column order for parity with the mysql path.
    rows = conn.run(sql, **(params or {}))
    return rows


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


def row_count(family, conn, table):
    rows = _query(family, conn, f"SELECT COUNT(*) FROM {table}")
    return int(rows[0][0]) if rows else None


def checksum(family, conn, table):
    if family == "mysql":
        rows = _query(family, conn, f"CHECKSUM TABLE {table}")
        return str(rows[0][1]) if rows and rows[0][1] is not None else None
    rows = _query(family, conn, f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {table} t")
    return rows[0][0] if rows and rows[0][0] is not None else None


def columns(family, conn, table):
    """Column fingerprint keyed by name -> {type, nullable, default} — strengthened from
    a name-only comparison (the confirmed gap: two tables with identically-named columns
    of different types/nullability/defaults used to report no drift at all)."""
    if family == "mysql":
        rows = _query(family, conn,
                      "SELECT column_name, column_type, is_nullable, column_default FROM information_schema.columns "
                      "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY column_name",
                      (table,))
    else:
        rows = _query(family, conn,
                      "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                      "WHERE table_name=:t ORDER BY column_name", {"t": table})
    return {
        str(r[0]).strip(): {"type": str(r[1]), "nullable": str(r[2]),
                             "default": (str(r[3]) if r[3] is not None else None)}
        for r in rows if r[0]
    }


def cloudwatch_alarms(alarm_names):
    """Returns (firing_names, unknown_names, check) where check is True (all OK), False
    (something is actually firing), None (nothing firing but at least one alarm came back
    INSUFFICIENT_DATA or wasn't found at all — needs_agent_review, NOT a silent pass), or
    "not_applicable" (no alarms configured for this engagement)."""
    if not alarm_names:
        return [], [], "not_applicable"
    try:
        resp = _cloudwatch.describe_alarms(AlarmNames=alarm_names)
    except ClientError:
        return [], list(alarm_names), None  # API call itself failed -> needs review, not a crash
    states = {a["AlarmName"]: a.get("StateValue") for a in resp.get("MetricAlarms", [])}
    firing = [n for n, s in states.items() if s == "ALARM"]
    unknown = [n for n in alarm_names if states.get(n) in (None, "INSUFFICIENT_DATA")]
    if firing:
        return firing, unknown, False
    if unknown:
        return firing, unknown, None
    return firing, unknown, True


def db_headroom_pct(db_instance_id):
    """FreeStorageSpace as % of AllocatedStorage — a proxy for storage headroom. Returns
    None on ANY failure to get a real, current datapoint (missing instance, missing
    metric, INSUFFICIENT_DATA) — the caller decides whether that means "not configured"
    (excluded) or "needs review" (missing data must never look like a silent pass, and
    must never crash the whole invocation either — DBInstanceNotFound is exactly the
    kind of thing that should surface as "needs review", not an unhandled exception)."""
    try:
        desc = _rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
    except ClientError:
        return None
    instances = desc.get("DBInstances", [])
    if not instances:
        return None
    allocated_gb = instances[0].get("AllocatedStorage")
    if not allocated_gb:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    stats = _cloudwatch.get_metric_statistics(
        Namespace="AWS/RDS", MetricName="FreeStorageSpace",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
        StartTime=now - datetime.timedelta(minutes=30), EndTime=now,
        Period=1800, Statistics=["Average"],
    )
    points = stats.get("Datapoints", [])
    if not points:
        return None
    free_gb = float(points[0]["Average"]) / (1024 ** 3)
    return round(100 * free_gb / float(allocated_gb), 1)


def measure_replication_lag(cfg, family, source_conn, target_conn):
    """Returns (lag_seconds_or_None, mechanism_or_None). mechanism is one of
    "dms"/"mysql_replica_status"/"postgres_replica_status"/None (nothing configured for
    this engagement — the caller treats that as "not_applicable", not "unknown")."""
    dms_task_id = cfg.get("dms_task_id")
    dms_instance_id = cfg.get("dms_replication_instance_id")
    if dms_task_id and dms_instance_id:
        now = datetime.datetime.now(datetime.timezone.utc)
        best = None
        for metric in ("CDCLatencyTarget", "CDCLatencySource"):
            stats = _cloudwatch.get_metric_statistics(
                Namespace="AWS/DMS", MetricName=metric,
                Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": dms_instance_id},
                            {"Name": "ReplicationTaskIdentifier", "Value": dms_task_id}],
                StartTime=now - datetime.timedelta(minutes=15), EndTime=now,
                Period=300, Statistics=["Maximum"],
            )
            points = stats.get("Datapoints", [])
            if points:
                worst = max(float(p["Maximum"]) for p in points)
                best = worst if best is None else max(best, worst)
        # points missing/INSUFFICIENT_DATA for BOTH metrics -> None, not a guess of 0.
        return best, "dms"

    replica_side = cfg.get("mysql_replica_status_side")  # "source" or "target"
    if family == "mysql" and replica_side:
        conn = target_conn if replica_side == "target" else source_conn
        try:
            rows = _query(family, conn, "SHOW REPLICA STATUS")
        except Exception:
            try:
                rows = _query(family, conn, "SHOW SLAVE STATUS")  # pre-8.0.22 syntax
            except Exception:
                return None, "mysql_replica_status"
        if not rows:
            return None, "mysql_replica_status"
        # Column position (not name) is the portable way to read this across the
        # SHOW REPLICA/SLAVE STATUS renames between MySQL versions.
        row = rows[0]
        # Seconds_Behind_Source (8.0.22+) / Seconds_Behind_Master (older) is column 32
        # (1-indexed) in both forms; guard against a shorter row defensively.
        idx = 31
        val = row[idx] if len(row) > idx else None
        return (float(val), "mysql_replica_status") if val is not None else (None, "mysql_replica_status")

    pg_side = cfg.get("pg_replication_lag_side")  # "source" or "target"
    if family == "postgres" and pg_side:
        conn = target_conn if pg_side == "target" else source_conn
        try:
            rows = _query(family, conn,
                          "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))")
        except Exception:
            return None, "postgres_replica_status"
        val = rows[0][0] if rows else None
        return (float(val), "postgres_replica_status") if val is not None else (None, "postgres_replica_status")

    return None, None


def replication_errors(cfg):
    """Returns (check, detail) where check is True/False/None/"not_applicable" — pulls
    DMS task stats (TablesErrored, task Status, LastFailureMessage) when a DMS task ARN
    is configured; "not_applicable" when nothing is configured (no DMS task in play for
    this engagement — e.g. native logical/binlog replication with no equivalent error
    surface wired yet)."""
    dms_task_arn = cfg.get("dms_task_arn")
    if not dms_task_arn:
        return "not_applicable", {}
    try:
        resp = _dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [dms_task_arn]}])
    except ClientError as e:
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
    """checks{} 3-state model (replaces the old always-null placeholders):
      True/False  — measured and passed/failed.
      None        — SHOULD be measurable (something IS configured for it) but the data
                    came back missing/INSUFFICIENT_DATA/unreachable — needs_agent_review,
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

    _start_consistent_read(family, source_conn)
    _start_consistent_read(family, target_conn)
    try:
        row_check = {"pass": True, "detail": {}}
        for t in tables:
            sc, tc = row_count(family, source_conn, t), row_count(family, target_conn, t)
            row_check["detail"][t] = {"source": sc, "target": tc}
            if sc != tc:
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
            if sc != tc:
                drift_check["pass"] = False
                mismatched = sorted(k for k in (set(sc) & set(tc)) if sc[k] != tc[k])
                drift_check["detail"][t] = {
                    "source_only": sorted(set(sc) - set(tc)),
                    "target_only": sorted(set(tc) - set(sc)),
                    "mismatched_attributes": {k: {"source": sc[k], "target": tc[k]} for k in mismatched},
                }

        lag_seconds, lag_mechanism = measure_replication_lag(cfg, family, source_conn, target_conn)
    finally:
        _end_consistent_read(family, source_conn)
        _end_consistent_read(family, target_conn)

    firing_alarms, unknown_alarms, alarms_check = cloudwatch_alarms(cfg.get("alarm_names", []))

    target_db_instance_id = cfg.get("target_db_instance_id")
    if not target_db_instance_id:
        headroom_check, headroom = "not_applicable", None
    else:
        headroom = db_headroom_pct(target_db_instance_id)
        headroom_check = None if headroom is None else (headroom > HEADROOM_THRESHOLD_PCT)

    if lag_mechanism is None:
        lag_check = "not_applicable"
    else:
        lag_check = None if lag_seconds is None else (lag_seconds <= LAG_THRESHOLD_S)

    repl_errors_check, repl_errors_detail = replication_errors(cfg)

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
    }
    # Green/complete calc only ever considers checks that were actually measured this
    # run — "not_applicable" (nothing configured) is excluded exactly like a check that
    # doesn't exist for this engagement, never counted as either a pass or a review flag.
    measured = [v for v in checks.values() if isinstance(v, bool)]
    overall_green = all(measured) if measured else False
    needs_review = (not overall_green) or any(v is None for v in checks.values())

    return {
        "date": datetime.date.today().isoformat(),
        "checks": checks,
        "detail": {"row_count": row_check["detail"], "checksum": checksum_check["detail"],
                    "schema_drift": drift_check["detail"], "firing_alarms": firing_alarms,
                    "unknown_alarms": unknown_alarms, "headroom_pct": headroom,
                    "replication_lag_seconds": lag_seconds, "replication_lag_mechanism": lag_mechanism,
                    "replication_errors": repl_errors_detail},
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


def _s3_get_with_etag(bucket, key):
    """Returns (raw_bytes_or_None, etag_or_None). etag=None means the object doesn't
    exist yet — the caller conditions its PUT on IfNoneMatch:'*' in that case instead of
    IfMatch, so two invocations racing to create the object for the first time can't
    both "succeed" and one silently clobber the other either."""
    try:
        resp = _s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read(), resp["ETag"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None
        raise


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
        consecutive = 0
        for d in reversed(days):
            if d.get("overall") == "green":
                consecutive += 1
            else:
                break
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
            raise
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
            raise
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
    _s3.put_object(Bucket=bucket, Key=key, Body=summary.encode("utf-8"), ContentType="text/markdown")
    return key


def handler(event, context):
    cfg = {
        "source_engine": os.environ["SOURCE_ENGINE"],
        "target_engine": os.environ["TARGET_ENGINE"],
        "tables": _env_json("TABLES", []),
        "checksum_tables": _env_json("CHECKSUM_TABLES", None),
        "alarm_names": _env_json("ALARM_NAMES", []),
        "target_db_instance_id": os.environ.get("TARGET_DB_INSTANCE_ID"),
        "dms_task_id": os.environ.get("DMS_TASK_ID"),
        "dms_replication_instance_id": os.environ.get("DMS_REPLICATION_INSTANCE_ID"),
        "dms_task_arn": os.environ.get("DMS_TASK_ARN"),
        "mysql_replica_status_side": os.environ.get("MYSQL_REPLICA_STATUS_SIDE"),
        "pg_replication_lag_side": os.environ.get("PG_REPLICATION_LAG_SIDE"),
        "customer_test_suite_provided": os.environ.get("CUSTOMER_TEST_SUITE_PROVIDED", "false").lower() == "true",
        "n_total": int(os.environ["N_TOTAL"]),
    }
    bucket = os.environ["DASHBOARD_BUCKET"]
    prefix = os.environ.get("DASHBOARD_PREFIX", "")
    status_key = f"{prefix}status.json"
    log_key = f"{prefix}activity-log.jsonl"
    reports_prefix = f"{prefix}reports/"
    # Source and target very often have DIFFERENT trust anchors — an on-prem/legacy
    # source with a self-signed or private-CA certificate vs. an RDS/Aurora target with
    # an Amazon-issued one — so these are two independent, optional settings, never one
    # shared path. Leaving either unset falls back to the platform default trust store
    # (still full TLS, just not pinned to a specific CA) for that side.
    source_ssl_ca_path = os.environ.get("SOURCE_SSL_CA_PATH")
    target_ssl_ca_path = os.environ.get("TARGET_SSL_CA_PATH")
    # Explicit, per-side, opt-in-only "encrypt but don't verify" fallback — see
    # _tls_context's tier 3 docstring. Never defaults to true.
    source_ssl_insecure = os.environ.get("SOURCE_TLS_SKIP_VERIFY", "false").lower() == "true"
    target_ssl_insecure = os.environ.get("TARGET_TLS_SKIP_VERIFY", "false").lower() == "true"

    # Dedicated, SELECT-only credentials for THIS Lambda — never the target's admin/master
    # secret (see engagement-safety.md §IAM guardrails; cdk-stacks.md §soak-stack.ts for how
    # the read-only DB user itself is provisioned).
    source_creds = _get_secret(os.environ["SOURCE_SECRET_ARN"])
    target_creds = _get_secret(os.environ["TARGET_SECRET_ARN"])

    source_family = _engine_family(cfg["source_engine"])
    target_family = _engine_family(cfg["target_engine"])
    source_conn = _connect(source_family, os.environ["SOURCE_HOST"], os.environ["SOURCE_PORT"],
                            source_creds, os.environ["SOURCE_DB"], source_ssl_ca_path, source_ssl_insecure)
    target_conn = _connect(target_family, os.environ["TARGET_HOST"], os.environ["TARGET_PORT"],
                            target_creds, os.environ["TARGET_DB"], target_ssl_ca_path, target_ssl_insecure)
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
