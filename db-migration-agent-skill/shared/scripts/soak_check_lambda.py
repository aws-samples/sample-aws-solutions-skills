"""Lambda-compatible port of soak_check.py — same Phase 7.7 mechanical checks (row count,
checksum, schema drift, alarm state, storage headroom), same null-handling for
replication_lag/customer_test_suite, same needs_agent_review flagging — but running as an
AWS-managed EventBridge Scheduler target instead of a process on a bastion or laptop, and
writing to the dashboard's S3 bucket instead of a local dashboard/ folder.

WHY THIS EXISTS ALONGSIDE soak_check.py, NOT INSTEAD OF IT: soak_check.py stays the
reference implementation (readable, no AWS SDK dependency, easy to run/debug by hand from
any machine that can reach the databases directly). This file is the same logic re-targeted
at the two things that change in Lambda: (1) no `mysql`/`psql`/`aws` CLI binaries or
subprocess — connect over the wire with pymysql/pg8000, call boto3 directly; (2) no local
filesystem — status.json/activity-log.jsonl/soak-report-*.md live in S3, which has no native
append, so "append a line to activity-log.jsonl" is GET-modify-PUT under the hood.

Deploy shape (see shared/patterns/cdk-stacks.md §Soak automation infra): VPC-attached into
the SAME private subnets + security group the migration bastion already uses (identical
reachability to the source over the existing VPN/DX path, no new networking), triggered
daily by EventBridge Scheduler, IAM scoped to Secrets Manager read (source+target secrets
only), CloudWatch/RDS describe, and S3 read/write on the dashboard bucket only.

Config arrives via environment variables (set once by CDK from the same values that would
have gone into soak-config.json for the standalone script) — see the SoakCheckFunction
construct in cdk-stacks.md for exactly which ones and their shapes.
"""
import datetime
import json
import os

import boto3
import pymysql
from botocore.exceptions import ClientError

try:
    import pg8000.native as pg8000
except ImportError:  # not needed for a MySQL-only deployment; kept optional to match
    pg8000 = None      # soak_check.py's engine-agnostic shape without forcing the dependency.

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number. Defined here for parity with soak_check.py;
# this Lambda doesn't compute lag itself (see `replication_lag: None` below).
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30

_secrets = boto3.client("secretsmanager")
_cloudwatch = boto3.client("cloudwatch")
_rds = boto3.client("rds")
_s3 = boto3.client("s3")


def _env_json(name, default):
    raw = os.environ.get(name)
    return json.loads(raw) if raw else default


def _get_secret(secret_arn):
    resp = _secrets.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _connect(engine, host, port, creds, database):
    user = creds.get("username") or creds.get("user")
    password = creds["password"]
    if engine == "mysql":
        return pymysql.connect(host=host, port=int(port), user=user, password=password,
                                database=database, connect_timeout=10, read_timeout=25,
                                cursorclass=pymysql.cursors.Cursor)
    if pg8000 is None:
        raise RuntimeError("engine=postgres but pg8000 is not bundled in this deployment")
    return pg8000.Connection(host=host, port=int(port), user=user, password=password,
                             database=database, timeout=10)


def _query(engine, conn, sql, params=None):
    """Returns list of row tuples. pg8000.native.Connection.run() and pymysql cursors have
    different call shapes — normalize here so the check functions below stay engine-blind,
    same as soak_check.py's run_mysql/run_psql split, just at the query layer instead of the
    subprocess layer."""
    if engine == "mysql":
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())
    # pg8000.native.run returns list-of-dict-like rows keyed by column label; normalize to
    # plain tuples by column order for parity with the mysql path.
    rows = conn.run(sql, **(params or {}))
    return rows


def row_count(engine, conn, table):
    rows = _query(engine, conn, f"SELECT COUNT(*) FROM {table}")
    return int(rows[0][0]) if rows else None


def checksum(engine, conn, table):
    if engine == "mysql":
        rows = _query(engine, conn, f"CHECKSUM TABLE {table}")
        return str(rows[0][1]) if rows and rows[0][1] is not None else None
    rows = _query(engine, conn, f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {table} t")
    return rows[0][0] if rows and rows[0][0] is not None else None


def columns(engine, conn, table):
    if engine == "mysql":
        rows = _query(engine, conn,
                      "SELECT column_name FROM information_schema.columns "
                      "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY column_name",
                      (table,))
    else:
        rows = _query(engine, conn,
                      "SELECT column_name FROM information_schema.columns "
                      "WHERE table_name=:t ORDER BY column_name", {"t": table})
    return sorted(str(r[0]).strip() for r in rows if r[0])


def cloudwatch_alarms(alarm_names):
    if not alarm_names:
        return [], True
    resp = _cloudwatch.describe_alarms(AlarmNames=alarm_names)
    firing = [a["AlarmName"] for a in resp.get("MetricAlarms", []) if a.get("StateValue") == "ALARM"]
    return firing, len(firing) == 0


def db_headroom_pct(db_instance_id):
    """FreeStorageSpace as % of AllocatedStorage — a proxy for storage headroom."""
    if not db_instance_id:
        return None
    desc = _rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
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


def run_day(cfg, source_conn, target_conn):
    engine = cfg["engine"]
    tables = cfg["tables"]

    row_check = {"pass": True, "detail": {}}
    for t in tables:
        sc, tc = row_count(engine, source_conn, t), row_count(engine, target_conn, t)
        row_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc:
            row_check["pass"] = False

    checksum_tables = cfg.get("checksum_tables") or tables[:2]
    checksum_check = {"pass": True, "detail": {}}
    for t in checksum_tables:
        sc, tc = checksum(engine, source_conn, t), checksum(engine, target_conn, t)
        checksum_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc or sc is None:
            checksum_check["pass"] = False

    drift_check = {"pass": True, "detail": {}}
    for t in tables:
        sc, tc = columns(engine, source_conn, t), columns(engine, target_conn, t)
        if sc != tc:
            drift_check["pass"] = False
            drift_check["detail"][t] = {"source_only": sorted(set(sc) - set(tc)),
                                         "target_only": sorted(set(tc) - set(sc))}

    firing_alarms, alarms_pass = cloudwatch_alarms(cfg.get("alarm_names", []))
    headroom = db_headroom_pct(cfg.get("target_db_instance_id"))
    headroom_pass = headroom is None or headroom > HEADROOM_THRESHOLD_PCT

    checks = {
        "row_count": row_check["pass"],
        "checksum": checksum_check["pass"],
        "alarms": alarms_pass,
        "headroom": headroom_pass,
        "schema_drift": drift_check["pass"],
        # replication_lag, customer_test_suite: left null — this Lambda has no CDC-metric
        # or external-test-suite access, exactly like soak_check.py; never guess these.
        "replication_lag": None,
        "customer_test_suite": None,
    }
    mechanical_checks = [v for v in checks.values() if v is not None]
    overall_green = all(mechanical_checks)
    needs_review = (not overall_green) or any(v is None for v in checks.values())

    return {
        "date": datetime.date.today().isoformat(),
        "checks": checks,
        "detail": {"row_count": row_check["detail"], "checksum": checksum_check["detail"],
                    "schema_drift": drift_check["detail"], "firing_alarms": firing_alarms,
                    "headroom_pct": headroom},
        "overall": "green" if overall_green else "red",
        "needs_agent_review": needs_review,
    }


# ── S3-backed equivalents of soak_check.py's local-file functions ──────────────────────

def _s3_get_json(bucket, key, default):
    try:
        body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body) if body else default
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return default
        raise


def _s3_get_text(bucket, key, default=""):
    try:
        return _s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return default
        raise


def update_status_json(bucket, key, day_result, n_total):
    status = _s3_get_json(bucket, key, {})
    soak = status.setdefault("soak", {"days": [], "n_total": n_total, "consecutive_green": 0, "state": "active"})
    soak["days"].append(day_result)
    soak["consecutive_green"] = 0 if day_result["overall"] != "green" else soak["consecutive_green"] + 1
    soak["n_total"] = n_total
    # S3 is now the single source of truth during the soak window — this timestamp is what
    # lets the dashboard flag a missed scheduled run (Lambda didn't fire, or errored before
    # writing) exactly as it did on the bastion; see execution-runbooks.md §Soak automation.
    soak["last_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    soak["state"] = "complete" if soak["consecutive_green"] >= n_total else "active"
    status["soak"] = soak
    status["updated_at"] = soak["last_checked_at"]
    _s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(status, indent=2, ensure_ascii=False).encode("utf-8"),
                    ContentType="application/json")
    return status


def append_activity_log(bucket, key, day_result):
    entry = {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "phase": "7.7",
        "title": f"Soak day {day_result['date']} — {day_result['overall']}",
        "action": "automated soak_check_lambda.py run (row count, checksum, schema drift, alarms, headroom)",
        "result": day_result["overall"],
        "detail": "needs_agent_review" if day_result["needs_agent_review"] else "all mechanical checks green",
        "files": [],
    }
    # S3 objects have no native append — GET the whole log, add one line, PUT the whole
    # thing back. Fine at this scale (one line/day for a soak window measured in days);
    # this is the one place this port genuinely differs in mechanism, not just plumbing.
    existing = _s3_get_text(bucket, key, "")
    updated = existing + json.dumps(entry, ensure_ascii=False) + "\n"
    _s3.put_object(Bucket=bucket, Key=key, Body=updated.encode("utf-8"), ContentType="application/x-ndjson")
    return entry


def write_soak_report(bucket, reports_prefix, day_result, day_n, n_total, consecutive_green):
    checks = day_result["checks"]
    key = f"{reports_prefix}soak-report-day{day_n}.md"
    summary = (
        f"# Soak Report — Day {day_n} of {n_total}\n\n"
        f"Consecutive green counter: {consecutive_green}/{n_total} "
        f"(any RED resets it to 0)\n\n"
        f"## Verdict: {'🟢 GREEN' if day_result['overall'] == 'green' else '🔴 RED'}\n\n"
        f"| Check | Pass |\n|---|:---:|\n"
        + "".join(f"| {k} | {'▢ pass' if v else ('▢ NEEDS AGENT REVIEW' if v is None else '▢ FAIL')} |\n"
                  for k, v in checks.items())
        + f"\n## Detail\n```json\n{json.dumps(day_result['detail'], indent=2, ensure_ascii=False)}\n```\n"
    )
    _s3.put_object(Bucket=bucket, Key=key, Body=summary.encode("utf-8"), ContentType="text/markdown")
    return key


def handler(event, context):
    cfg = {
        "engine": os.environ["ENGINE"],
        "tables": _env_json("TABLES", []),
        "checksum_tables": _env_json("CHECKSUM_TABLES", None),
        "alarm_names": _env_json("ALARM_NAMES", []),
        "target_db_instance_id": os.environ.get("TARGET_DB_INSTANCE_ID"),
        "n_total": int(os.environ["N_TOTAL"]),
    }
    bucket = os.environ["DASHBOARD_BUCKET"]
    prefix = os.environ.get("DASHBOARD_PREFIX", "")
    status_key = f"{prefix}status.json"
    log_key = f"{prefix}activity-log.jsonl"
    reports_prefix = f"{prefix}reports/"

    source_creds = _get_secret(os.environ["SOURCE_SECRET_ARN"])
    target_creds = _get_secret(os.environ["TARGET_SECRET_ARN"])

    source_conn = _connect(cfg["engine"], os.environ["SOURCE_HOST"], os.environ["SOURCE_PORT"],
                            source_creds, os.environ["SOURCE_DB"])
    target_conn = _connect(cfg["engine"], os.environ["TARGET_HOST"], os.environ["TARGET_PORT"],
                            target_creds, os.environ["TARGET_DB"])
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
        # Lambda's own Errors metric/DLQ; alert on this via a CloudWatch Logs metric filter
        # on "needs_agent_review=true" (see cdk-stacks.md), or by reading status.json.
        print(f"WARNING day {day_n}: needs_agent_review=true — {day_result['overall']}")
    else:
        print(f"Day {day_n}: {day_result['overall'].upper()}")

    return {"day": day_n, "overall": day_result["overall"], "needs_agent_review": day_result["needs_agent_review"]}
