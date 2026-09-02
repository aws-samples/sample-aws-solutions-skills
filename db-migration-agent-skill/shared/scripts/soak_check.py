#!/usr/bin/env python3
"""Runs the mechanical half of a Phase 7.7 soak day: row counts, checksums, schema
drift, replication lag/errors, and CloudWatch alarm/headroom state. Writes dashboard/
status.json's "soak" object, appends dashboard/activity-log.jsonl, and writes
soak-report-day-N.md from the template. Judgment calls (interpreting a RED day, deciding
what an anomaly means) stay with the agent — this script only reports facts and flags for
review.

Usage: python3 soak_check.py --config dashboard/soak-config.json
Config is written once by the agent during Phase 7.7 setup — see
shared/reference/execution-runbooks.md §Soak automation for the schema and for
how to schedule this (cron or EventBridge Scheduler + Lambda).

RETRY/RE-RUN SAFETY: running this twice for the same calendar day (e.g. cron fired twice,
or you re-ran it by hand) overwrites that day's entry in status.json/activity-log.jsonl in
place — it never appends a duplicate or double-counts the green streak.

SCOPE: homogeneous MySQL-family (mysql/mariadb/aurora-mysql) or PostgreSQL-family
(postgres/postgresql/aurora-postgresql) only — source and target must be the same family.
Heterogeneous soak-checking is not yet supported by this script; a family mismatch (or an
engine string outside both families) raises ValueError with no dashboard/log/report writes
attempted, and this script exits with a clean one-line message (no Python traceback) and
exit code 1 for that case.
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number.
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30
_SPLIT = "___SOAK_CHECK_SPLIT___"

# Confirmed LIVE against a real RDS PostgreSQL instance and a real Aurora PostgreSQL
# cluster (both us-east-1, aurora-postgresql/postgres 16.13): the platform/OS default CA
# trust store does NOT contain the current Amazon RDS root ("Amazon RDS <region> Root CA
# RSA2048 G1") — only the unrelated generic "Amazon Root CA 1-4" (ACM/Trust Services)
# and legacy Starfield roots. `sslmode=verify-full` with no `sslrootcert` therefore fails
# outright (libpq falls back to the compiled-in `~/.postgresql/root.crt`, which normally
# doesn't exist), and even `sslrootcert=system` (explicit OS-store opt-in) still fails
# chain validation for exactly this reason. The tier-1 "neither ssl_ca nor ssl_insecure"
# default below pins this bundled copy of the official AWS RDS/Aurora CA bundle
# (https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem, covers every
# region/algorithm generation) instead of trusting the OS store — this is what actually
# makes the documented "just works against RDS/Aurora" default true. A genuinely
# non-AWS Postgres source signed by a public WebPKI CA should use the explicit `ssl_ca`
# tier instead (point it at that CA, or at the OS store's own file if that's really what
# you need) rather than relying on this tier-1 default.
_DEFAULT_CA_BUNDLE = Path(__file__).resolve().parent.parent / "assets" / "rds-global-bundle.pem"

MYSQL_FAMILY = {"mysql", "mariadb", "aurora-mysql"}
POSTGRES_FAMILY = {"postgres", "postgresql", "aurora-postgresql"}


def _engine_family(engine):
    """Normalizes an engine string to 'mysql' or 'postgres' — MariaDB/Aurora MySQL route
    to the same client path as MySQL instead of silently falling through to the psql path
    (the confirmed bug: anything other than the exact string "mysql" used to fall to
    Postgres). Must match soak_check_lambda.py's MYSQL_FAMILY/POSTGRES_FAMILY exactly —
    these are the literal `engine` strings AWS APIs return (e.g. `aws rds
    describe-db-clusters`), so a family missing "aurora-postgresql" here previously made a
    real Aurora PostgreSQL target look "unsupported" instead of naming the actual problem
    (heterogeneous mismatch) when checked against a non-Postgres-family source. Raises for
    anything outside both families; heterogeneous soak-checking (different SQL dialects on
    each side) is out of scope for this script."""
    e = (engine or "").strip().lower()
    if e in MYSQL_FAMILY:
        return "mysql"
    if e in POSTGRES_FAMILY:
        return "postgres"
    raise ValueError(
        f"Unsupported engine '{engine}' — this script only supports homogeneous "
        "MySQL-family (mysql/mariadb/aurora-mysql) or PostgreSQL-family "
        "(postgres/postgresql/aurora-postgresql) checks. Heterogeneous soak-checking is "
        "not yet supported."
    )


def run_mysql_batch(host, user, password, database, sqls, ssl_ca=None, ssl_insecure=False):
    """Executes every statement in `sqls` in ONE mysql client session (one subprocess
    call/one connection) — required so a consistent-snapshot transaction started as the
    first statement actually covers every statement after it; separate subprocess calls
    are each a brand-new connection with no session continuity. Returns a list of line
    lists, one per input statement, split on a marker row injected after each one.
    Three TLS tiers, never a silent plaintext fallback:
    - ssl_ca given: pin that exact CA certificate as the trust anchor (VERIFY_CA —
      encrypted + chain-verified, no hostname check — on-prem certs frequently carry no
      SAN matching the IP/hostname actually used to reach them). Must be the actual CA
      certificate, not just any certificate the peer happens to present — confirmed
      live: pinning a presented LEAF certificate does not satisfy chain validation if
      the CA that issued it isn't ALSO trusted.
    - ssl_insecure=True (explicit opt-in only, never the default): encrypts but skips
      certificate verification entirely (VERIFY_CA/VERIFY_IDENTITY's weaker sibling,
      REQUIRED) — the real, bounded fallback for a self-signed cert auto-generated by
      the DB engine on a host with no way to retrieve the actual CA file.
    - neither: full verification against the system default CA store (VERIFY_IDENTITY)
      — correct for an RDS/Aurora endpoint's Amazon-issued certificate."""
    script = "; ".join(f"{sql}; SELECT '{_SPLIT}'" for sql in sqls)
    cmd = ["mysql", "-h", host, "-u", user, f"-p{password}"]
    if ssl_ca:
        cmd += [f"--ssl-ca={ssl_ca}", "--ssl-mode=VERIFY_CA"]
    elif ssl_insecure:
        cmd += ["--ssl-mode=REQUIRED"]
    else:
        cmd += ["--ssl-mode=VERIFY_IDENTITY"]
    cmd += ["-N", "-B", database, "-e", script]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    chunks, current = [], []
    for line in out.splitlines():
        if line == _SPLIT:
            chunks.append(current)
            current = []
        else:
            current.append(line)
    return chunks


def run_psql_batch(host, user, password, database, sqls, ssl_ca=None, ssl_insecure=False, port=None):
    """Postgres equivalent of run_mysql_batch — one psql session, one BEGIN ISOLATION
    LEVEL REPEATABLE READ covering every statement. Same three TLS tiers as
    run_mysql_batch, EXCEPT the "neither" (tier 1) default pins the bundled AWS RDS/Aurora
    CA bundle (`_DEFAULT_CA_BUNDLE`), not the OS trust store — confirmed live that the OS
    store lacks the current RDS root; see `_DEFAULT_CA_BUNDLE`'s module-level comment.
    Never plaintext.
    `port`: confirmed live that without an explicit `-p`, psql silently defaults to 5432 —
    connecting to the wrong endpoint (or nothing) instead of erroring clearly, for the
    extremely common case of reaching the DB through an SSM/bastion port-forward tunnel on
    a non-default local port (this skill's own documented Phase 2 access path). `None`
    keeps psql's own 5432 default for a direct, default-port connection."""
    script = "; ".join(f"{sql}; SELECT '{_SPLIT}'" for sql in sqls)
    if ssl_ca:
        env = {"PGPASSWORD": password, "PGSSLMODE": "verify-ca", "PGSSLROOTCERT": ssl_ca}
    elif ssl_insecure:
        env = {"PGPASSWORD": password, "PGSSLMODE": "require"}
    else:
        env = {"PGPASSWORD": password, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(_DEFAULT_CA_BUNDLE)}
    cmd = ["psql", "-h", host]
    if port:
        cmd += ["-p", str(port)]
    cmd += ["-U", user, "-d", database, "-t", "-A", "-c", script]
    # env= replaces the child's whole environment (not merged) — PATH must be carried over
    # explicitly or a bare "psql" argv[0] can fail to resolve on some shells/PATH configs.
    import os as _os
    env["PATH"] = _os.environ.get("PATH", "")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env).stdout
    chunks, current = [], []
    for line in out.splitlines():
        if line == _SPLIT:
            chunks.append(current)
            current = []
        else:
            current.append(line)
    return chunks


def run_batch(family, conn, sqls):
    # conn (cfg["source"]/cfg["target"]) also carries "engine" for the dispatch decision
    # above — strip it here so it isn't passed through as a stray keyword arg.
    conn_args = {k: v for k, v in conn.items()
                 if k in ("host", "user", "password", "database", "ssl_ca", "ssl_insecure")}
    if family == "mysql":
        return run_mysql_batch(sqls=sqls, **conn_args)
    # soak-config.json's documented schema (execution-runbooks.md §Soak automation)
    # includes "port" per side — confirmed live that dropping it here made run_psql_batch
    # silently default to 5432 regardless of what's configured, connecting to the wrong
    # endpoint (or nothing) instead of erroring clearly for the extremely common case of
    # reaching the DB through an SSM/bastion port-forward tunnel on a non-default local
    # port. Threaded through only for postgres here (not added to the shared allowlist
    # above / run_mysql_batch's signature) to stay isolated from that function while it's
    # being edited concurrently elsewhere in this file for an unrelated MySQL-client fix.
    if "port" in conn:
        conn_args["port"] = conn["port"]
    return run_psql_batch(sqls=sqls, **conn_args)


def run_one(family, conn, sql):
    chunks = run_batch(family, conn, [sql])
    return chunks[0] if chunks else []


def cloudwatch_alarms(alarm_names, region):
    """Returns (firing, unknown, check) — check is True/False/None/"not_applicable",
    same 3-state model as soak_check_lambda.py. INSUFFICIENT_DATA or a missing alarm is
    `unknown`, never silently folded into "pass"."""
    if not alarm_names:
        return [], [], "not_applicable"
    cmd = ["aws", "cloudwatch", "describe-alarms", "--alarm-names", *alarm_names,
           "--region", region, "--query", "MetricAlarms[].[AlarmName,StateValue]", "--output", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    states = {name: state for name, state in (json.loads(out) if out.strip() else [])}
    firing = [n for n, s in states.items() if s == "ALARM"]
    unknown = [n for n in alarm_names if states.get(n) in (None, "INSUFFICIENT_DATA")]
    if firing:
        return firing, unknown, False
    if unknown:
        return firing, unknown, None
    return firing, unknown, True


def db_headroom_pct(db_instance_id, region):
    """FreeStorageSpace as % of AllocatedStorage. Returns "not_applicable" for an
    Aurora-family instance — confirmed live against a real Aurora PostgreSQL writer that
    Aurora instances report a placeholder AllocatedStorage (observed: 1) and publish NO
    FreeStorageSpace datapoints at all (Aurora storage auto-scales; there is no fixed
    allocation to measure headroom against), which used to make this permanently return
    None (needs_agent_review) for every Aurora target — the skill's primary target engine
    — keeping state stuck at "active" forever instead of ever reaching "complete". Returns
    None on any OTHER failure to get a real, current datapoint — the caller decides
    "not configured" vs "needs review" for that case."""
    cmd = ["aws", "rds", "describe-db-instances", "--db-instance-identifier", db_instance_id,
           "--region", region, "--query", "DBInstances[0].[AllocatedStorage,Engine]", "--output", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    if not out:
        return None
    try:
        allocated_gb, engine = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if engine and str(engine).lower().startswith("aurora"):
        return "not_applicable"
    if not allocated_gb:
        return None
    cmd = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", "AWS/RDS",
           "--metric-name", "FreeStorageSpace", "--dimensions", f"Name=DBInstanceIdentifier,Value={db_instance_id}",
           "--start-time", (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)).isoformat() + "Z",
           "--end-time", datetime.datetime.utcnow().isoformat() + "Z",
           "--period", "1800", "--statistics", "Average", "--region", region,
           "--query", "Datapoints[0].Average", "--output", "text"]
    free_bytes = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    if not free_bytes or free_bytes == "None":
        return None
    free_gb = float(free_bytes) / (1024 ** 3)
    return round(100 * free_gb / float(allocated_gb), 1)


def measure_replication_lag(cfg, family, source_conn, target_conn):
    """Returns (lag_seconds_or_None, mechanism_or_None) — DMS CloudWatch metrics if a DMS
    task is configured, else SHOW REPLICA STATUS (MySQL-family) / a replay-lag query
    (Postgres) on whichever side is configured as the replica, else (None, None) meaning
    genuinely nothing is configured (the caller treats that as "not_applicable")."""
    region = cfg.get("region", "us-east-1")
    dms_task_id = cfg.get("dms_task_id")
    dms_instance_id = cfg.get("dms_replication_instance_id")
    if dms_task_id and dms_instance_id:
        best = None
        now = datetime.datetime.utcnow()
        for metric in ("CDCLatencyTarget", "CDCLatencySource"):
            cmd = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", "AWS/DMS",
                   "--metric-name", metric, "--dimensions",
                   f"Name=ReplicationInstanceIdentifier,Value={dms_instance_id}",
                   f"Name=ReplicationTaskIdentifier,Value={dms_task_id}",
                   "--start-time", (now - datetime.timedelta(minutes=15)).isoformat() + "Z",
                   "--end-time", now.isoformat() + "Z", "--period", "300", "--statistics", "Maximum",
                   "--region", region, "--query", "Datapoints[].Maximum", "--output", "json"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
            vals = json.loads(out) if out.strip() else []
            if vals:
                worst = max(float(v) for v in vals)
                best = worst if best is None else max(best, worst)
        return best, "dms"

    replica_side = cfg.get("mysql_replica_status_side")
    if family == "mysql" and replica_side:
        conn = cfg["target"] if replica_side == "target" else cfg["source"]
        lines = run_one(family, conn, "SHOW REPLICA STATUS")
        if not lines:
            lines = run_one(family, conn, "SHOW SLAVE STATUS")
        if not lines or not lines[0]:
            return None, "mysql_replica_status"
        fields = lines[0].split("\t")
        val = fields[31] if len(fields) > 31 else None  # Seconds_Behind_Source/Master
        return (float(val), "mysql_replica_status") if val and val != "NULL" else (None, "mysql_replica_status")

    pg_side = cfg.get("pg_replication_lag_side")
    if family == "postgres" and pg_side:
        conn = cfg["target"] if pg_side == "target" else cfg["source"]
        lines = run_one(family, conn, "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))")
        val = lines[0].strip() if lines and lines[0].strip() else None
        return (float(val), "postgres_replica_status") if val else (None, "postgres_replica_status")

    return None, None


def replication_errors(cfg):
    """DMS task stats (TablesErrored/Status/LastFailureMessage) when a DMS task ARN is
    configured; "not_applicable" when nothing is configured for this engagement."""
    dms_task_arn = cfg.get("dms_task_arn")
    if not dms_task_arn:
        return "not_applicable", {}
    region = cfg.get("region", "us-east-1")
    cmd = ["aws", "dms", "describe-replication-tasks", "--filters",
           f"Name=replication-task-arn,Values={dms_task_arn}", "--region", region,
           "--query", "ReplicationTasks[0].{Status:Status,Stats:ReplicationTaskStats,"
                       "LastFailureMessage:LastFailureMessage}", "--output", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    task = json.loads(out) if out.strip() and out.strip() != "null" else None
    if not task:
        return None, {"error": "DMS task not found for configured ARN"}
    tables_errored = (task.get("Stats") or {}).get("TablesErrored", 0)
    status = task.get("Status")
    last_failure = task.get("LastFailureMessage")
    detail = {"status": status, "tables_errored": tables_errored, "last_failure_message": last_failure}
    ok = (tables_errored == 0) and (status == "running") and not last_failure
    return ok, detail


def _column_fingerprint(family, lines):
    """Parses information_schema.columns rows (name, type, nullable, default — tab-
    separated) into name -> {type, nullable, default}, strengthened from a name-only
    comparison."""
    out = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if not name:
            continue
        typ = parts[1] if len(parts) > 1 else ""
        nullable = parts[2] if len(parts) > 2 else ""
        default = parts[3] if len(parts) > 3 and parts[3] != "NULL" else None
        out[name] = {"type": typ, "nullable": nullable, "default": default}
    return out


def run_day(cfg):
    source_family = _engine_family(cfg["source"]["engine"])
    target_family = _engine_family(cfg["target"]["engine"])
    if source_family != target_family:
        raise ValueError(
            f"source engine={cfg['source']['engine']!r} and target engine="
            f"{cfg['target']['engine']!r} normalize to different SQL families — "
            "heterogeneous soak-checking is not yet supported by this script."
        )
    family = source_family
    tables = cfg["tables"]
    checksum_tables = cfg.get("checksum_tables") or tables[:2]

    def _side_sqls(conn_engine_family):
        snapshot_start = ("START TRANSACTION WITH CONSISTENT SNAPSHOT" if conn_engine_family == "mysql"
                           else "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        row_sqls = [f"SELECT COUNT(*) FROM {t}" for t in tables]
        checksum_sqls = ([f"CHECKSUM TABLE {t}" for t in checksum_tables] if conn_engine_family == "mysql"
                          else [f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {t} t" for t in checksum_tables])
        if conn_engine_family == "mysql":
            col_sqls = [f"SELECT column_name, column_type, is_nullable, column_default FROM "
                        f"information_schema.columns WHERE table_schema=DATABASE() AND "
                        f"table_name='{t}' ORDER BY column_name" for t in tables]
        else:
            col_sqls = [f"SELECT column_name, data_type, is_nullable, column_default FROM "
                        f"information_schema.columns WHERE table_name='{t}' ORDER BY column_name" for t in tables]
        return [snapshot_start] + row_sqls + checksum_sqls + col_sqls + ["COMMIT"]

    # ONE batched, one-session call per side — the consistent-snapshot transaction at the
    # top of the script covers every row-count/checksum/column read that follows it in
    # the SAME session, closing the "independent statements can straddle a write" gap.
    # True cross-engine (source vs target) synchronization isn't possible without a
    # distributed transaction spanning two different database servers — that residual
    # skew (the few hundred ms between opening each side's session) is accepted, not
    # eliminated; only the WITHIN-one-side inconsistency is closed here.
    src_sqls = _side_sqls(family)
    tgt_sqls = _side_sqls(family)
    src_chunks = run_batch(family, cfg["source"], src_sqls)
    tgt_chunks = run_batch(family, cfg["target"], tgt_sqls)

    def _unpack(chunks):
        # chunks[0] = snapshot-start's own (empty) result; then N row counts, then M
        # checksums, then N column-lists; chunks[-1] = COMMIT's own (empty) result.
        i = 1
        rows = chunks[i:i + len(tables)]; i += len(tables)
        cks = chunks[i:i + len(checksum_tables)]; i += len(checksum_tables)
        cols = chunks[i:i + len(tables)]; i += len(tables)
        return rows, cks, cols

    src_rows, src_cks, src_cols = _unpack(src_chunks)
    tgt_rows, tgt_cks, tgt_cols = _unpack(tgt_chunks)

    row_check = {"pass": True, "detail": {}}
    for idx, t in enumerate(tables):
        sc = int(src_rows[idx][0]) if src_rows[idx] and src_rows[idx][0] else None
        tc = int(tgt_rows[idx][0]) if tgt_rows[idx] and tgt_rows[idx][0] else None
        row_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc:
            row_check["pass"] = False

    checksum_check = {"pass": True, "detail": {}}
    for idx, t in enumerate(checksum_tables):
        sline = src_cks[idx][0] if src_cks[idx] else None
        tline = tgt_cks[idx][0] if tgt_cks[idx] else None
        sc = sline.split("\t")[-1] if sline else None
        tc = tline.split("\t")[-1] if tline else None
        checksum_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc or sc is None:
            checksum_check["pass"] = False

    drift_check = {"pass": True, "detail": {}}
    for idx, t in enumerate(tables):
        sc, tc = _column_fingerprint(family, src_cols[idx]), _column_fingerprint(family, tgt_cols[idx])
        if sc != tc:
            drift_check["pass"] = False
            mismatched = sorted(k for k in (set(sc) & set(tc)) if sc[k] != tc[k])
            drift_check["detail"][t] = {
                "source_only": sorted(set(sc) - set(tc)), "target_only": sorted(set(tc) - set(sc)),
                "mismatched_attributes": {k: {"source": sc[k], "target": tc[k]} for k in mismatched},
            }

    lag_seconds, lag_mechanism = measure_replication_lag(cfg, family, cfg["source"], cfg["target"])
    firing_alarms, unknown_alarms, alarms_check = cloudwatch_alarms(cfg.get("alarm_names", []), cfg.get("region", "us-east-1"))

    target_db_instance_id = cfg.get("target_db_instance_id")
    if not target_db_instance_id:
        headroom_check, headroom = "not_applicable", None
    else:
        headroom = db_headroom_pct(target_db_instance_id, cfg.get("region", "us-east-1"))
        if headroom == "not_applicable":
            headroom_check, headroom = "not_applicable", None
        else:
            headroom_check = None if headroom is None else (headroom > HEADROOM_THRESHOLD_PCT)

    lag_check = "not_applicable" if lag_mechanism is None else (None if lag_seconds is None else (lag_seconds <= LAG_THRESHOLD_S))
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


def update_status_json(status_path, day_result, n_total):
    """Idempotent per calendar day — see soak_check_lambda.py's update_status_json for
    why: a re-run for a day already in soak.days[] overwrites it in place, and
    consecutive_green is recomputed from days[] itself (trailing green run), never
    incremented, so it can never drift from what's actually on disk."""
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
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
    # Lets the dashboard flag a silently-missed run (host was down, cron didn't fire,
    # script crashed) — a stale soak.days[] entry looks identical to "waiting for
    # tomorrow" unless something records when the last run actually happened.
    soak["last_checked_at"] = datetime.datetime.now().astimezone().isoformat()
    soak["state"] = "complete" if consecutive >= n_total else "active"
    status["soak"] = soak
    status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))


def append_activity_log(log_path, day_result):
    """Idempotent per calendar day — a re-run for a day already logged replaces that
    day's line in place instead of appending a duplicate."""
    entry = {
        "time": datetime.datetime.now().astimezone().isoformat(),
        "phase": "7.7",
        "date": day_result["date"],
        "title": f"Soak day {day_result['date']} — {day_result['overall']}",
        "action": "automated soak_check.py run (row count, checksum, schema drift, alarms, "
                   "headroom, replication lag/errors)",
        # activity-log.jsonl's documented vocabulary is success|in_progress|blocked — map
        # the day verdict onto it (never write "green"/"red" directly; dashboard.js's
        # activity-log renderer doesn't recognize those and used to default to a green
        # checkmark even on a RED day).
        "result": "success" if day_result["overall"] == "green" else "blocked",
        "detail": "needs_agent_review" if day_result["needs_agent_review"] else "all mechanical checks green",
        "files": [],
    }
    existing_lines = log_path.read_text().splitlines() if log_path.exists() else []
    kept = []
    for line in existing_lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if parsed.get("phase") == "7.7" and parsed.get("date") == entry["date"]:
            continue
        kept.append(line)
    kept.append(json.dumps(entry, ensure_ascii=False))
    log_path.write_text("\n".join(kept) + "\n")


def write_soak_report(reports_dir, day_result, day_n, n_total, consecutive_green):
    # shared/templates/soak-report.md has free-text fields (customer visibility,
    # notes) only the agent can fill in; this script writes the mechanical-check
    # facts as a companion file, which the agent then folds into that template.
    checks = day_result["checks"]

    def _mark(v):
        if v is True:
            return "▢ pass"
        if v is False:
            return "▢ FAIL"
        if v == "not_applicable":
            return "▢ n/a"
        return "▢ NEEDS AGENT REVIEW"

    out_path = reports_dir / f"soak-report-day{day_n}.md"
    summary = (
        f"# Soak Report — Day {day_n} of {n_total}\n\n"
        f"Consecutive green counter: {consecutive_green}/{n_total} "
        f"(any RED resets it to 0)\n\n"
        f"## Verdict: {'🟢 GREEN' if day_result['overall'] == 'green' else '🔴 RED'}\n\n"
        f"| Check | Pass |\n|---|:---:|\n"
        + "".join(f"| {k} | {_mark(v)} |\n" for k, v in checks.items())
        + f"\n## Detail\n```json\n{json.dumps(day_result['detail'], indent=2, ensure_ascii=False)}\n```\n"
    )
    out_path.write_text(summary)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text())

    engagement_dir = args.config.parent.parent  # dashboard/soak-config.json -> engagement root
    status_path = args.config.parent / "status.json"
    log_path = args.config.parent / "activity-log.jsonl"

    try:
        day_result = run_day(cfg)
    except ValueError as e:
        # An unsupported/mismatched engine pair (see _engine_family) is an expected,
        # already-diagnosed config problem, not a bug in this script — surface it as a
        # clean one-line message a human or an agent can act on immediately, not a Python
        # traceback. No dashboard/log/report write has happened yet at this point (this
        # is the very first thing run_day checks), so nothing needs cleanup here.
        sys.exit(f"soak_check.py: {e}")
    update_status_json(status_path, day_result, cfg["n_total"])
    append_activity_log(log_path, day_result)

    status = json.loads(status_path.read_text())
    day_n = len(status["soak"]["days"])
    write_soak_report(engagement_dir, day_result, day_n, cfg["n_total"],
                       status["soak"]["consecutive_green"])

    print(f"Day {day_n}: {day_result['overall'].upper()}"
          + (" — needs_agent_review=true, re-invoke the agent to interpret this before the next period" if day_result["needs_agent_review"] else ""))
    sys.exit(0 if not day_result["needs_agent_review"] else 2)


if __name__ == "__main__":
    main()
