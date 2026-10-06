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
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number.
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30
_SPLIT = "___SOAK_CHECK_SPLIT___"
# Whole-batch client timeout (one session per side). The old fixed 60 s could not cover a
# COUNT(*)/checksum over a ~100M-row table; override per run with "batch_timeout_seconds".
BATCH_TIMEOUT_S = 900

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


def _mysql_client_is_mariadb():
    """Detects whether the `mysql` CLI actually on PATH is the MariaDB client rather than
    Oracle MySQL's — confirmed live against a real migration bastion (Amazon Linux)
    running MariaDB 10.5's client under the plain `mysql` binary name, a common shape:
    several distro families ship MariaDB's client by default even when the *server* being
    reached is genuine MySQL/RDS MySQL. MariaDB's client does not recognize `--ssl-mode`
    at all — passing it is a client-side "unknown variable" argument-parse error that
    exits before attempting any connection, so `run_mysql_batch` got zero output and the
    caller saw a confusing `IndexError` many calls later instead of a clear TLS-flag
    error at the source. Not cached across calls — cheap (`--version`, no network) and
    correctness matters more than shaving a subprocess call per batch."""
    try:
        out = subprocess.run(["mysql", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "mariadb" in out.lower()


def _mysql_ssl_args(ssl_ca, ssl_insecure):
    """Same three TLS tiers as run_mysql_batch's docstring, translated to whichever flag
    set the actually-installed client accepts. MySQL client: `--ssl-mode=VERIFY_CA` /
    `REQUIRED` / `VERIFY_IDENTITY`. MariaDB client has no `--ssl-mode`; its nearest
    equivalents are `--ssl-ca=<file>` alone (chain-verify only, no hostname check — same
    shape as VERIFY_CA) and `--ssl` alone (encrypt, no verification — same shape as
    REQUIRED). The tier-1 default (neither given) pins the bundled AWS RDS/Aurora CA
    bundle (`_DEFAULT_CA_BUNDLE`) on BOTH client flavors rather than trusting either
    client's own default CA store — confirmed live against a real bastion (Amazon Linux)
    and a real RDS MySQL endpoint: the OS default trust store does not contain the
    current Amazon RDS root, so relying on it fails chain validation ("self-signed
    certificate in certificate chain") for exactly the "should just work" RDS/Aurora
    case this tier exists for — the same root cause already confirmed and fixed for the
    Postgres path (see `_DEFAULT_CA_BUNDLE`'s module-level comment); a genuinely non-AWS
    MySQL/MariaDB source signed by a public WebPKI CA should use the explicit `ssl_ca`
    tier instead."""
    mariadb = _mysql_client_is_mariadb()
    if ssl_ca:
        return [f"--ssl-ca={ssl_ca}"] if mariadb else [f"--ssl-ca={ssl_ca}", "--ssl-mode=VERIFY_CA"]
    if ssl_insecure:
        return ["--ssl"] if mariadb else ["--ssl-mode=REQUIRED"]
    bundle = str(_DEFAULT_CA_BUNDLE)
    return ([f"--ssl-ca={bundle}", "--ssl-verify-server-cert"] if mariadb
            else [f"--ssl-ca={bundle}", "--ssl-mode=VERIFY_IDENTITY"])


def run_mysql_batch(host, user, password, database, sqls, ssl_ca=None, ssl_insecure=False, port=None,
                    headers=False, timeout=None):
    """Executes every statement in `sqls` in ONE mysql client session (one subprocess
    call/one connection) — required so a consistent-snapshot transaction started as the
    first statement actually covers every statement after it; separate subprocess calls
    are each a brand-new connection with no session continuity. Returns a list of line
    lists, one per input statement, split on a marker row injected after each one.
    `port`: confirmed live (real version-gap pair, source+target both reached through an
    SSM/bastion port-forward tunnel on non-default local ports) that without an explicit
    `-P`, the `mysql` CLI silently defaults to 3306 — connecting to nothing (or the wrong
    endpoint) instead of erroring clearly, for the exact "reach the DB through a bastion
    tunnel on a non-default local port" case this skill's own Phase 2 access path
    documents as routine. This is the same gap already found and fixed for the Postgres
    path (`run_psql_batch`'s `port` param) — soak-config.json's documented schema
    (execution-runbooks.md §Soak automation) includes "port" for BOTH families, but only
    the Postgres branch was actually threading it through; MySQL/MariaDB silently dropped
    it. `None` keeps the mysql client's own 3306 default for a direct, default-port
    connection.
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
    - neither: full verification (VERIFY_IDENTITY), pinned to the bundled AWS RDS/Aurora
      CA bundle rather than the client's own default CA store — correct for an RDS/Aurora
      endpoint's Amazon-issued certificate; confirmed live that the OS default trust
      store lacks the current RDS root (see `_mysql_ssl_args`'s docstring).
    Flag *names* for these three tiers come from `_mysql_ssl_args`, which picks the
    MySQL-client or MariaDB-client spelling — confirmed live that a MariaDB client
    rejects `--ssl-mode` outright (see `_mysql_client_is_mariadb`'s docstring).
    `headers=True` keeps the column-name line (no `-N`) as the first line of each chunk —
    used only for SHOW REPLICA STATUS, whose columns are read BY NAME (see
    LAG_COLUMN_NAMES)."""
    script = "; ".join(f"{sql}; SELECT '{_SPLIT}'" for sql in sqls)
    cmd = ["mysql", "-h", host, "-u", user]
    if port:
        cmd += ["-P", str(port)]
    cmd += _mysql_ssl_args(ssl_ca, ssl_insecure)
    cmd += (["-B"] if headers else ["-N", "-B"]) + [database, "-e", script]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or BATCH_TIMEOUT_S,
                          env={**os.environ, "MYSQL_PWD": password})
    if proc.returncode != 0:
        # A client-side failure here (bad TLS flag, auth failure, unreachable host) used
        # to surface as an empty/short chunk list and a confusing IndexError many calls
        # later in run_day's unpacking — fail loudly at the source instead, with the
        # client's own stderr attached.
        raise RuntimeError(
            f"mysql client exited {proc.returncode} connecting to {host!r} as {user!r}: "
            f"{proc.stderr.strip()}"
        )
    out = proc.stdout
    chunks, current = [], []
    for line in out.splitlines():
        if line == _SPLIT:
            chunks.append(current)
            current = []
        else:
            current.append(line)
    return chunks


def run_psql_batch(host, user, password, database, sqls, ssl_ca=None, ssl_insecure=False, port=None, timeout=None):
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
    cmd = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", host]
    if port:
        cmd += ["-p", str(port)]
    cmd += ["-U", user, "-d", database, "-t", "-A", "-F", "\t", "-c", script]
    # env= replaces the child's whole environment (not merged) — PATH must be carried over
    # explicitly or a bare "psql" argv[0] can fail to resolve on some shells/PATH configs.
    import os as _os
    env["PATH"] = _os.environ.get("PATH", "")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or BATCH_TIMEOUT_S, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"psql client exited {proc.returncode}: {proc.stderr.strip()}")
    out = proc.stdout
    chunks, current = [], []
    for line in out.splitlines():
        if line == _SPLIT:
            chunks.append(current)
            current = []
        else:
            current.append(line)
    return chunks


def run_batch(family, conn, sqls, headers=False, timeout=None):
    # conn (cfg["source"]/cfg["target"]) also carries "engine" for the dispatch decision
    # above — strip it here so it isn't passed through as a stray keyword arg.
    conn_args = {k: v for k, v in conn.items()
                 if k in ("host", "user", "password", "database", "ssl_ca", "ssl_insecure")}
    if "password" in conn:
        raise ValueError("Use password_env, not a password in soak-config.json")
    password_env = conn.get("password_env")
    if not password_env or password_env not in os.environ:
        raise ValueError("Connection requires password_env naming an on-host environment variable")
    conn_args["password"] = os.environ[password_env]
    # soak-config.json's documented schema (execution-runbooks.md §Soak automation)
    # includes "port" per side, for BOTH families — confirmed live (real MySQL 8.0->8.4
    # version-gap pair, both sides reached through an SSM/bastion port-forward tunnel on
    # non-default local ports) that dropping it here made run_mysql_batch's `mysql` CLI
    # silently default to 3306, connecting to nothing instead of erroring clearly, for the
    # exact "reach the DB through a bastion tunnel on a non-default local port" case this
    # skill's own Phase 2 access path documents as routine. This was already fixed for
    # run_psql_batch (Postgres) but the identical MySQL/MariaDB-side gap was left
    # unfixed — the comment that used to be here said it was deliberately isolated from
    # run_mysql_batch to avoid colliding with an unrelated concurrent MySQL-client edit;
    # that edit landed without ever adding port support, so the gap survived. Both
    # families now take it the same way.
    if "port" in conn:
        conn_args["port"] = conn["port"]
    if family == "mysql":
        return run_mysql_batch(sqls=sqls, headers=headers, timeout=timeout, **conn_args)
    return run_psql_batch(sqls=sqls, timeout=timeout, **conn_args)


def run_one(family, conn, sql, headers=False):
    chunks = run_batch(family, conn, [sql], headers=headers)
    return chunks[0] if chunks else []


# Same lookup as soak_check_lambda.py: SHOW REPLICA STATUS (8.0.22+, the only form on 8.4)
# names it Seconds_Behind_Source; SHOW SLAVE STATUS (<8.0.22, MariaDB) Seconds_Behind_Master.
# Read BY NAME — the old positional read (field 32) was verified only on an 8.0 replica.
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


def aws_region(cfg):
    """The region for every `aws` CLI call. Never a hardcoded default — a silent
    us-east-1 fallback is exactly what broke an ap-northeast-2 engagement. Order:
    cfg["region"]; else parsed from an RDS endpoint (`*.<region>.rds.amazonaws.com`) on
    the target, then the source. No match -> ValueError (clean exit in main())."""
    if cfg.get("region"):
        return cfg["region"]
    for side in ("target", "source"):
        m = re.search(r"\.([a-z]{2}(?:-gov)?-[a-z]+-\d)\.rds\.amazonaws\.com$", str((cfg.get(side) or {}).get("host", "")))
        if m:
            return m.group(1)
    raise ValueError(
        'soak-config.json has no "region" and it cannot be derived from an RDS endpoint '
        '(hosts are tunnels/IPs) — add "region": "<the target\'s region, e.g. ap-northeast-2>".'
    )


def _needs_aws(cfg):
    return bool(cfg.get("alarm_names") or cfg.get("target_db_instance_id") or cfg.get("dms_task_arn")
                or (cfg.get("dms_task_id") and cfg.get("dms_replication_instance_id")))


def _aws_cli(cmd, check, iam_action, resource, aws_errors):
    """Run an `aws` CLI call; on failure record {check, iam_action, resource, result,
    message} in aws_errors (same shape as soak_check_lambda.py's detail.aws_errors[]) and
    return "" so the caller's existing None/needs-review path applies — never a pass."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        proc, err, timed_out = None, "aws CLI call timed out after 30s", True
    except (FileNotFoundError, OSError) as e:
        proc, err, timed_out = None, f"could not run the aws CLI: {type(e).__name__}: {e}", False
    else:
        if proc.returncode == 0:
            return proc.stdout
        err, timed_out = (proc.stderr or "").strip(), False
    denied = any(c in err for c in ("AccessDenied", "UnauthorizedOperation", "AuthorizationError", "is not authorized"))
    network = timed_out or any(c in err for c in ("Could not connect to the endpoint", "Connect timeout", "Read timeout"))
    entry = {"check": check, "iam_action": iam_action, "resource": resource,
             "result": "AccessDenied" if denied else "Error",
             "message": (err + (" — network: endpoint unreachable (NAT/VPC endpoint), not IAM" if network else ""))[:1000]}
    print("SOAK_CHECK_AWS_ERROR " + json.dumps(entry, ensure_ascii=False), file=sys.stderr)
    aws_errors.append(entry)
    return ""


def cloudwatch_alarms(alarm_names, region, aws_errors=None):
    """Returns (firing, unknown, check) — check is True/False/None/"not_applicable",
    same 3-state model as soak_check_lambda.py. INSUFFICIENT_DATA or a missing alarm is
    `unknown`, never silently folded into "pass"."""
    if not alarm_names:
        return [], [], "not_applicable"
    cmd = ["aws", "cloudwatch", "describe-alarms", "--alarm-names", *alarm_names,
           "--region", region, "--query", "MetricAlarms[].[AlarmName,StateValue]", "--output", "json"]
    out = _aws_cli(cmd, "alarms", "cloudwatch:DescribeAlarms", ",".join(alarm_names),
                   aws_errors if aws_errors is not None else [])
    states = {name: state for name, state in (json.loads(out) if out.strip() else [])}
    firing = [n for n, s in states.items() if s == "ALARM"]
    unknown = [n for n in alarm_names if states.get(n) in (None, "INSUFFICIENT_DATA")]
    if firing:
        return firing, unknown, False
    if unknown:
        return firing, unknown, None
    return firing, unknown, True


def db_headroom_pct(db_instance_id, region, aws_errors=None):
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
    aws_errors = aws_errors if aws_errors is not None else []
    out = _aws_cli(cmd, "headroom", "rds:DescribeDBInstances", f"db:{db_instance_id}", aws_errors).strip()
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
    free_bytes = _aws_cli(cmd, "headroom", "cloudwatch:GetMetricStatistics",
                          "* (AWS/RDS FreeStorageSpace)", aws_errors).strip()
    if not free_bytes or free_bytes == "None":
        return None
    free_gb = float(free_bytes) / (1024 ** 3)
    return round(100 * free_gb / float(allocated_gb), 1)


# AWS/DMS task metrics carry the dimension ReplicationTaskIdentifier = the task's RESOURCE ID
# (the last ':' segment of its ARN, e.g. arn:aws:dms:<region>:<acct>:task:CPSTBQCAAFB67LEICTHDNETPSU),
# NOT the friendly ReplicationTaskIdentifier ("myproj-fwd-cdc"). The instance dimension
# ReplicationInstanceIdentifier IS the friendly instance identifier. Confirmed live with
# `aws cloudwatch list-metrics --namespace AWS/DMS`: the friendly task name returns ZERO
# datapoints — indistinguishable from "no metrics" unless flagged. KEEP IN SYNC (both scripts).
DMS_DIMENSION_HINT = ("wrong ReplicationTaskIdentifier? use the task ARN's resource-id suffix "
                      "(arn:...:task:<RESOURCE-ID>), not the friendly task name — or the task is not "
                      "running / has emitted no datapoint in the last 15 minutes")


def dms_metric_task_id(task_id, task_arn):
    """(dimension_value_or_None, problem_or_None) for the ReplicationTaskIdentifier dimension.
    An ARN (in either setting) is authoritative: its last ':' segment is the resource id."""
    tid = (task_id or "").strip()
    from_arn = tid.startswith("arn:")
    if from_arn:   # provenance: a value taken from an ARN IS the resource id (custom ones may be lowercase)
        tid = tid.rsplit(":", 1)[-1]
    arn = (task_arn or "").strip()
    arn_id = arn.rsplit(":", 1)[-1] if arn.startswith("arn:") and ":task:" in arn else None
    if arn_id:
        if tid and tid != arn_id:
            return arn_id, (f"DMS_TASK_ID {task_id!r} is not the task's resource id {arn_id!r} (the "
                            "DMS_TASK_ARN suffix) — metrics are queried with the ARN suffix; fix the config")
        return arn_id, None
    if not tid:
        return None, None
    if not from_arn and re.search(r"[a-z-]", tid):
        return tid, (f"DMS_TASK_ID {tid!r} looks like the friendly task name (lowercase/hyphens) — "
                     + DMS_DIMENSION_HINT + ". If a custom ResourceIdentifier really looks like this, "
                     "also set DMS_TASK_ARN so it can be confirmed")
    return tid, None


def measure_replication_lag(cfg, family, source_conn, target_conn, aws_errors=None):
    """Returns (lag_seconds_or_None, mechanism_or_None) — DMS CloudWatch metrics if a DMS
    task is configured, else SHOW REPLICA STATUS (MySQL-family); native PostgreSQL
    logical replication requires manual review, not physical replay age. (None, None) means
    genuinely nothing is configured (the caller treats that as "not_applicable")."""
    aws_errors = aws_errors if aws_errors is not None else []
    dms_task_id = cfg.get("dms_task_id")
    dms_instance_id = cfg.get("dms_replication_instance_id")
    if dms_task_id and dms_instance_id:
        dms_task_id, id_problem = dms_metric_task_id(dms_task_id, cfg.get("dms_task_arn"))
        if id_problem:
            aws_errors.append({"check": "replication_lag", "iam_action": "n/a (config)",
                               "resource": "dms_task_id", "result": "Error", "message": id_problem})
        region = aws_region(cfg)
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
            out = _aws_cli(cmd, "replication_lag", "cloudwatch:GetMetricStatistics",
                           f"* (AWS/DMS {metric})", aws_errors)
            vals = json.loads(out) if out.strip() else []
            if not vals:
                if out.strip():   # the call succeeded but returned no datapoints
                    aws_errors.append({"check": "replication_lag", "iam_action": "cloudwatch:GetMetricStatistics",
                                       "resource": f"AWS/DMS {metric} ReplicationTaskIdentifier={dms_task_id}",
                                       "result": "Error", "message": "no datapoints — " + DMS_DIMENSION_HINT})
                return None, "dms"
            if vals:
                worst = max(float(v) for v in vals)
                best = worst if best is None else max(best, worst)
        return best, "dms"

    replica_side = cfg.get("mysql_replica_status_side")
    if family == "mysql" and replica_side:
        conn = cfg["target"] if replica_side == "target" else cfg["source"]
        try:
            lines = run_one(family, conn, "SHOW REPLICA STATUS", headers=True)
        except RuntimeError:
            lines = []
        if not lines:
            try:
                lines = run_one(family, conn, "SHOW SLAVE STATUS", headers=True)  # pre-8.0.22 / MariaDB
            except RuntimeError:
                lines = []
        # headers=True: lines[0] is the column-name row, lines[1] the (single) status row.
        if len(lines) < 2 or not lines[1]:
            return None, "mysql_replica_status"
        val, _col = lag_from_replica_status(lines[0].split("\t"), lines[1].split("\t"))
        return val, "mysql_replica_status"

    pg_side = cfg.get("pg_replication_lag_side")
    if family == "postgres" and pg_side:
        return None, "postgres_logical_requires_review"

    return None, None


def replication_errors(cfg, aws_errors=None):
    """DMS task stats (TablesErrored/Status/LastFailureMessage) when a DMS task ARN is
    configured; "not_applicable" when nothing is configured for this engagement."""
    dms_task_arn = cfg.get("dms_task_arn")
    if not dms_task_arn:
        return "not_applicable", {}
    region = aws_region(cfg)
    cmd = ["aws", "dms", "describe-replication-tasks", "--filters",
           f"Name=replication-task-arn,Values={dms_task_arn}", "--region", region,
           "--query", "ReplicationTasks[0].{Status:Status,Stats:ReplicationTaskStats,"
                       "LastFailureMessage:LastFailureMessage}", "--output", "json"]
    out = _aws_cli(cmd, "replication_errors", "dms:DescribeReplicationTasks", dms_task_arn,
                   aws_errors if aws_errors is not None else [])
    task = json.loads(out) if out.strip() and out.strip() != "null" else None
    if not task:
        return None, {"error": "DMS task not found for configured ARN"}
    tables_errored = (task.get("Stats") or {}).get("TablesErrored", 0)
    status = task.get("Status")
    last_failure = task.get("LastFailureMessage")
    detail = {"status": status, "tables_errored": tables_errored, "last_failure_message": last_failure}
    ok = (tables_errored == 0) and (status == "running") and not last_failure
    return ok, detail


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


# ── Watermark-bounded comparison (soak under LIVE writes) ──────────────────────────────
# Whole-table COUNT(*)/CHECKSUM TABLE source-vs-target is RED every day while the
# application writes: replication lag means the newest rows (the "tail") are on the
# source but not yet on the target at the instant each side is read. Confirmed live
# (MySQL 8.0 -> RDS 8.4 under app writes): every daily check went RED for that reason
# alone. So, per table, compare only rows up to a WATERMARK both sides already hold —
# pk <= min(source MAX(pk), target MAX(pk)) - margin, or <timestamp column> <= source
# NOW() - N minutes when a timestamp column is configured for that table — and report the
# tail beyond it as informational detail, never as a failure. Tables without a
# single-column integer primary key (and no configured timestamp column) fall back to the
# whole-table comparison, and the detail says so. A mismatch BELOW the watermark is still
# a real failure (RED); it can also mean an UPDATE/DELETE of an old row still in flight —
# the agent reviews it (needs_agent_review), it is never silently passed.
# KEEP IN SYNC with soak_check_lambda.py — scripts/test_soak_scripts.py asserts identical SQL.
_INTEGER_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "int2", "int4", "int8"}
_BINARY_TYPE_HINTS = ("binary", "blob", "bit", "geometry", "point", "linestring", "polygon")
WATERMARK_DEFAULTS = {"enabled": True, "pk_margin": 10000, "timestamp_columns": {}, "timestamp_age_minutes": 15}


def _ident_sql(family, name):
    quote = '`' if family == 'mysql' else '"'
    return quote + name.replace(quote, quote * 2) + quote


def _pk_sql(family, table):
    """Primary-key columns (name, type) in key order — catalog only, no table scan."""
    parts = _table_parts(table)
    name = _sql_literal(family, parts[-1])
    schema = (_sql_literal(family, parts[0]) if len(parts) == 2 else
              "DATABASE()" if family == "mysql" else "current_schema()")
    if family == "mysql":
        return ("SELECT k.column_name, c.data_type FROM information_schema.key_column_usage k "
                "JOIN information_schema.columns c ON c.table_schema=k.table_schema AND "
                "c.table_name=k.table_name AND c.column_name=k.column_name "
                f"WHERE k.table_schema={schema} AND k.table_name={name} AND k.constraint_name='PRIMARY' "
                "ORDER BY k.ordinal_position")
    return ("SELECT attribute.attname, pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) "
            "FROM pg_catalog.pg_index idx JOIN pg_catalog.pg_class relation ON relation.oid=idx.indrelid "
            "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
            "JOIN pg_catalog.pg_attribute attribute ON attribute.attrelid=relation.oid AND attribute.attnum=ANY(idx.indkey) "
            f"WHERE idx.indisprimary AND namespace.nspname={schema} AND relation.relname={name} "
            "ORDER BY array_position(idx.indkey::int2[], attribute.attnum)")


def _single_integer_pk(rows):
    """The PK column name if the key is exactly one integer column, else None."""
    if len(rows) != 1:
        return None
    base = str(rows[0][1]).strip().lower().split("(")[0].split()[0] if rows[0][1] else ""
    return str(rows[0][0]) if base in _INTEGER_TYPES else None


def _max_sql(family, table, column):
    return f"SELECT MAX({_ident_sql(family, column)}) FROM {_table_sql(family, table)}"


def _cutoff_sql(family, minutes):
    """Evaluated on the SOURCE only; the same literal then bounds both sides (the target's
    time_zone must match the source's — Phase 7 already verifies that)."""
    m = int(minutes)
    if family == "mysql":
        return f"SELECT DATE_FORMAT(NOW(6) - INTERVAL {m} MINUTE, '%Y-%m-%d %H:%i:%s.%f')"
    return f"SELECT to_char(now() - interval '{m} minutes', 'YYYY-MM-DD HH24:MI:SS.US')"


def _bound_sql(family, column, op, value):
    literal = str(int(value)) if isinstance(value, int) else _sql_literal(family, str(value))
    return f"{_ident_sql(family, column)} {op} {literal}"


def _count_where_sql(family, table, where):
    return f"SELECT COUNT(*) FROM {_table_sql(family, table)} WHERE {where}"


def _bounded_checksum_sql(family, table, column_types, where):
    """Order-independent fingerprint of the rows matching `where`. MySQL: COUNT + SUM and
    BIT_XOR of CRC32 over a length-prefixed, NULL-marked concatenation of every column in
    name order (CHECKSUM TABLE cannot take a WHERE clause). Fast and non-cryptographic,
    like CHECKSUM TABLE itself. PostgreSQL: the same md5(string_agg) shape as the
    whole-table checksum, restricted by `where`."""
    if family != "mysql":
        return f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {_table_sql(family, table)} t WHERE {where}"
    terms = []
    for col in sorted(column_types):
        q = _ident_sql(family, col)
        typ = str(column_types[col]).lower()
        v = f"HEX({q})" if any(h in typ for h in _BINARY_TYPE_HINTS) else f"CAST({q} AS CHAR)"
        terms.append(f"IFNULL(CONCAT(CHAR_LENGTH({v}), ':', {v}), 'N')")
    row = "CONCAT_WS('|', " + ", ".join(terms) + ")" if terms else "''"
    return (f"SELECT CONCAT(COUNT(*), ':', COALESCE(SUM(CRC32(r)), 0), ':', BIT_XOR(CRC32(r))) "
            f"FROM (SELECT {row} AS r FROM {_table_sql(family, table)} WHERE {where}) w")


def watermark_config(cfg):
    w = dict(WATERMARK_DEFAULTS)
    w.update({k: v for k, v in (cfg.get("watermark") or {}).items() if v is not None})
    return w


def plan_bound(table, wcfg, src_pk_rows, tgt_pk_rows):
    """('timestamp', column) | ('pk', column) | (None, reason) for one table."""
    if not wcfg.get("enabled", True):
        return None, "watermark disabled in config — whole-table comparison"
    ts_col = (wcfg.get("timestamp_columns") or {}).get(table)
    if ts_col:
        return "timestamp", ts_col
    src_pk, tgt_pk = _single_integer_pk(src_pk_rows), _single_integer_pk(tgt_pk_rows)
    if src_pk and src_pk == tgt_pk:
        return "pk", src_pk
    if src_pk != tgt_pk:
        return None, (f"primary key differs between sides (source {src_pk!r}, target {tgt_pk!r}) — "
                      "whole-table comparison")
    return None, ("no single-column integer primary key and no timestamp column configured — "
                  "whole-table comparison; under live writes this can be RED from replication lag alone")


_TIMESTAMP_BASES = {"datetime", "timestamp", "date", "timestamptz"}


def _type_base(typ):
    t = str(typ or "").strip().lower()
    if t.startswith("timestamp"):
        return "timestamptz" if "with time zone" in t else "timestamp"
    return t.split("(")[0].split()[0] if t else ""


def check_timestamp_column(column, src_cols, tgt_cols):
    """(ok, nullable, reason). A configured timestamp watermark column must exist on BOTH
    sides with the same date/time type; `nullable` is True if either side allows NULL —
    then NULL rows are compared explicitly (they have no position relative to a cutoff)."""
    s_attr, t_attr = (src_cols or {}).get(column), (tgt_cols or {}).get(column)
    if not s_attr or not t_attr:
        missing = [side for side, a in (("source", s_attr), ("target", t_attr)) if not a]
        return False, False, f"timestamp column {column!r} not found on {'/'.join(missing)}"
    sb, tb = _type_base(s_attr.get("type")), _type_base(t_attr.get("type"))
    if sb not in _TIMESTAMP_BASES or tb not in _TIMESTAMP_BASES or sb != tb:
        return False, False, (f"timestamp column {column!r} has type {s_attr.get('type')!r} (source) / "
                              f"{t_attr.get('type')!r} (target) — needs the same date/time type on both")
    nullable = "YES" in (str(s_attr.get("nullable")).upper(), str(t_attr.get("nullable")).upper())
    return True, nullable, None


def resolve_mode(table, wcfg, src_pk_rows, tgt_pk_rows, src_cols, tgt_cols):
    """Joint (both-schema) decision: (mode, column_or_reason, nullable)."""
    mode, info = plan_bound(table, wcfg, src_pk_rows, tgt_pk_rows)
    if mode != "timestamp":
        return mode, info, False
    ok, nullable, why = check_timestamp_column(info, src_cols, tgt_cols)
    if not ok:
        return None, why + " — whole-table comparison", False
    return "timestamp", info, nullable


def bound_predicates(family, column, bound, nullable):
    """(le, gt, null_pred_or_None). With a nullable timestamp column the compared set is
    `col <= bound OR col IS NULL` (NULL rows are counted and checksummed, never silently
    dropped), the tail is `col > bound`; together they cover every row."""
    q = _ident_sql(family, column)
    le, gt = _bound_sql(family, column, "<=", bound), _bound_sql(family, column, ">", bound)
    if nullable:
        return f"({le} OR {q} IS NULL)", gt, f"{q} IS NULL"
    return le, gt, None


def _min_sql(family, table, column):
    return f"SELECT MIN({_ident_sql(family, column)}) FROM {_table_sql(family, table)}"


def resolve_pk_watermark(src_max, tgt_max, src_min, margin):
    """(watermark, None) or (None, reason-for-whole-table-fallback)."""
    if src_max is None or tgt_max is None:
        return None, "table empty on at least one side — whole-table comparison"
    wm = min(int(src_max), int(tgt_max)) - int(margin)
    if src_min is None or wm < int(src_min):
        return None, (f"no rows below the watermark (table spans fewer than the {int(margin)}-key margin) "
                      "— whole-table comparison")
    return wm, None


_BOUNDED_NOTE = ("compared rows up to the watermark only; the tail beyond it is informational "
                 "(replication lag), not a failure")


def bounded_verdict(sc, tc, s_tail, t_tail):
    """True/False for the bounded comparison, or None when nothing was below the watermark
    while rows exist (timestamp mode, every row recent) — inconclusive, needs review."""
    if sc is None or tc is None:
        return None
    if sc == 0 and tc == 0 and (s_tail or t_tail):
        return None
    return sc == tc


def combine_checks(values):
    """Per-table True/False/None -> one check value: any False -> False, else any None ->
    None (needs review), else True."""
    values = list(values)
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


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
        default = parts[3] if len(parts) > 3 and parts[3] not in ("NULL", "") else None
        out[name] = {"type": typ, "nullable": nullable, "default": default}
    return out


def run_day(cfg, run_date=None):
    # Pin the verdict date at START: a 23:30Z run that finishes after midnight still records
    # the UTC day it checked (and the next day's run doesn't overwrite it).
    run_date = run_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
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
    if not isinstance(tables, list) or not tables or not all(isinstance(table, str) and table for table in tables):
        raise ValueError("tables must be a nonempty list of table identifiers")
    checksum_tables = cfg.get("checksum_tables") or tables[:2]
    # Resolve the region up front (before touching either database) when any AWS-side
    # check is configured — a missing region is a config error, not a us-east-1 default.
    region = aws_region(cfg) if _needs_aws(cfg) else None
    aws_errors = []

    wcfg = watermark_config(cfg)
    timeout = int(cfg.get("batch_timeout_seconds") or BATCH_TIMEOUT_S)
    union = list(dict.fromkeys(list(tables) + list(checksum_tables)))

    def _both(src_sqls, tgt_sqls):
        # Source and target run concurrently — independent servers, halves wall time.
        with ThreadPoolExecutor(max_workers=2) as executor:
            sf = executor.submit(run_batch, family, cfg["source"], src_sqls, False, timeout) if src_sqls else None
            tf = executor.submit(run_batch, family, cfg["target"], tgt_sqls, False, timeout) if tgt_sqls else None
            return (sf.result() if sf else []), (tf.result() if tf else [])

    def _rows(lines):
        return [line.split("\t") for line in lines if line.strip()]

    def _val(lines):
        v = lines[0].split("\t")[-1] if lines else None
        return None if v in (None, "NULL", "") else v

    # Round 1 — catalog only: primary keys (watermark eligibility) and column types (the
    # bounded checksum's column list). No table scans.
    r1 = [_pk_sql(family, t) for t in union] + [_columns_sql(family, t) for t in union]
    s1, t1 = _both(r1, r1)
    plans, col_types, nullable = {}, {}, {}
    for i, t in enumerate(union):
        s_cols = _column_fingerprint(family, s1[len(union) + i])
        t_cols = _column_fingerprint(family, t1[len(union) + i])
        mode, info, nullable[t] = resolve_mode(t, wcfg, _rows(s1[i]), _rows(t1[i]), s_cols, t_cols)
        plans[t] = (mode, info)
        col_types[t] = {k: v["type"] for k, v in s_cols.items()}

    # Round 2 — watermark inputs: MAX(pk) both sides + MIN(pk) on the source (index
    # endpoint reads), and the source clock for timestamp-bounded tables.
    pk_tables = [t for t in union if plans[t][0] == "pk"]
    ts_needed = any(plans[t][0] == "timestamp" for t in union)
    r2s = ([_max_sql(family, t, plans[t][1]) for t in pk_tables] + [_min_sql(family, t, plans[t][1]) for t in pk_tables]
           + ([_cutoff_sql(family, wcfg["timestamp_age_minutes"])] if ts_needed else []))
    r2t = [_max_sql(family, t, plans[t][1]) for t in pk_tables]
    s2, t2 = _both(r2s, r2t)
    bounds, extras = {}, {}
    for i, t in enumerate(pk_tables):
        smax, smin, tmax = _val(s2[i]), _val(s2[len(pk_tables) + i]), _val(t2[i])
        smax, smin, tmax = [None if v is None else int(v) for v in (smax, smin, tmax)]
        wm, why = resolve_pk_watermark(smax, tmax, smin, wcfg["pk_margin"])
        extras[t] = {"column": plans[t][1], "max_pk": {"source": smax, "target": tmax}, "margin": int(wcfg["pk_margin"])}
        if wm is None:
            plans[t] = (None, why)
        else:
            bounds[t] = extras[t]["watermark"] = wm
    cutoff = _val(s2[2 * len(pk_tables)]) if ts_needed else None
    for t in union:
        if plans[t][0] == "timestamp":
            extras[t] = {"column": plans[t][1], "age_minutes": int(wcfg["timestamp_age_minutes"])}
            if cutoff is None:
                plans[t] = (None, "could not read the source clock — whole-table comparison")
            else:
                bounds[t] = extras[t]["watermark"] = cutoff

    # Round 3 — ONE batched, one-session call per side: the consistent-snapshot transaction
    # at the top covers every row-count/checksum/column read that follows it in the SAME
    # session, closing the "independent statements can straddle a write" gap. True
    # cross-engine (source vs target) synchronization isn't possible without a distributed
    # transaction spanning two servers — that residual skew is accepted; the watermark
    # above is what keeps replication lag on the newest rows from reading as drift.
    keys, sqls, preds = [], [], {}
    for t in union:
        if t in bounds:
            preds[t] = bound_predicates(family, plans[t][1], bounds[t], nullable[t] and plans[t][0] == "timestamp")
    for t in union:   # checksum-only tables get the same bounded count/tail as the Lambda
        if t in bounds:
            le, gt, null_pred = preds[t]
            keys += [("rows", t), ("tail", t)]
            sqls += [_count_where_sql(family, t, le), _count_where_sql(family, t, gt)]
            if null_pred:
                keys.append(("nulls", t)); sqls.append(_count_where_sql(family, t, null_pred))
        else:
            keys.append(("rows", t)); sqls.append(f"SELECT COUNT(*) FROM {_table_sql(family, t)}")
    for t in checksum_tables:
        keys.append(("checksum", t))
        if t in bounds:
            sqls.append(_bounded_checksum_sql(family, t, col_types[t], preds[t][0]))
        elif family == "mysql":
            sqls.append(f"CHECKSUM TABLE {_table_sql(family, t)}")
        else:
            sqls.append(f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {_table_sql(family, t)} t")
    for t in tables:
        keys.append(("cols", t)); sqls.append(_columns_sql(family, t))
    snapshot_start = ("START TRANSACTION WITH CONSISTENT SNAPSHOT" if family == "mysql"
                      else "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    batch = [snapshot_start] + sqls + ["COMMIT"]
    src_chunks, tgt_chunks = _both(batch, batch)
    # chunks[0] = snapshot-start's own (empty) result; chunks[-1] = COMMIT's.
    src_res = {k: src_chunks[1 + i] for i, k in enumerate(keys)}
    tgt_res = {k: tgt_chunks[1 + i] for i, k in enumerate(keys)}

    def _int(v):
        return int(v) if v is not None else None

    row_check, row_oks, row_ok_by, notes = {"pass": True, "detail": {}}, [], {}, {}
    for t in union:
        sc, tc = _int(_val(src_res[("rows", t)])), _int(_val(tgt_res[("rows", t)]))
        if t in bounds:
            tail = {"source": _int(_val(src_res[("tail", t)])) or 0, "target": _int(_val(tgt_res[("tail", t)])) or 0}
            ok = bounded_verdict(sc, tc, tail["source"], tail["target"])
            if ("nulls", t) in src_res:
                extras[t]["null_rows"] = {"source": _int(_val(src_res[("nulls", t)])) or 0,
                                          "target": _int(_val(tgt_res[("nulls", t)])) or 0}
                if extras[t]["null_rows"]["source"] != extras[t]["null_rows"]["target"]:
                    ok = False
            note = _BOUNDED_NOTE if ok is not None else (
                "no rows below the watermark while recent rows exist — inconclusive, needs review "
                "(lower timestamp_age_minutes or use the PK watermark)")
            if ("nulls", t) in src_res:
                note += "; NULL-timestamp rows are included in the compared set and counted separately"
            detail = {"source": sc, "target": tc, "mode": f"{plans[t][0]}_watermark",
                      **extras[t], "tail_rows": tail, "note": note}
            notes[t] = note
        else:
            ok = sc is not None and sc == tc
            detail = {"source": sc, "target": tc, "mode": "whole_table", "note": plans[t][1], **extras.get(t, {})}
        row_ok_by[t] = ok
        if t in tables:
            row_check["detail"][t] = detail
            row_oks.append(ok)
    row_check["pass"] = combine_checks(row_oks)

    checksum_check, ck_oks = {"pass": True, "detail": {}}, []
    for t in checksum_tables:
        sc, tc = _val(src_res[("checksum", t)]), _val(tgt_res[("checksum", t)])
        if t in bounds:
            ok = None if row_ok_by[t] is None else (sc is not None and sc == tc)
            checksum_check["detail"][t] = {"source": sc, "target": tc, "mode": f"{plans[t][0]}_watermark",
                                           "column": plans[t][1], "watermark": bounds[t], "note": notes[t]}
        else:
            ok = sc is not None and sc == tc
            checksum_check["detail"][t] = {"source": sc, "target": tc, "mode": "whole_table", "note": plans[t][1]}
        ck_oks.append(ok)
    checksum_check["pass"] = combine_checks(ck_oks)
    src_cols = [src_res[("cols", t)] for t in tables]
    tgt_cols = [tgt_res[("cols", t)] for t in tables]

    drift_check = {"pass": True, "detail": {}}
    for idx, t in enumerate(tables):
        sc, tc = _column_fingerprint(family, src_cols[idx]), _column_fingerprint(family, tgt_cols[idx])
        if sc != tc or not sc or not tc:
            drift_check["pass"] = False
            mismatched = sorted(k for k in (set(sc) & set(tc)) if sc[k] != tc[k])
            drift_check["detail"][t] = {
                "source_only": sorted(set(sc) - set(tc)), "target_only": sorted(set(tc) - set(sc)),
                "mismatched_attributes": {k: {"source": sc[k], "target": tc[k]} for k in mismatched},
            }

    lag_seconds, lag_mechanism = measure_replication_lag(cfg, family, cfg["source"], cfg["target"], aws_errors)
    firing_alarms, unknown_alarms, alarms_check = cloudwatch_alarms(cfg.get("alarm_names", []), region, aws_errors)

    target_db_instance_id = cfg.get("target_db_instance_id")
    if not target_db_instance_id:
        headroom_check, headroom = "not_applicable", None
    else:
        headroom = db_headroom_pct(target_db_instance_id, region, aws_errors)
        if headroom == "not_applicable":
            headroom_check, headroom = "not_applicable", None
        else:
            headroom_check = None if headroom is None else (headroom > HEADROOM_THRESHOLD_PCT)

    lag_check = "not_applicable" if lag_mechanism is None else (None if lag_seconds is None else (lag_seconds <= LAG_THRESHOLD_S))
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
        detail["aws_errors"] = aws_errors  # additive; same shape as soak_check_lambda.py
    return {
        "date": run_date,
        "checks": checks,
        "detail": detail,
        "overall": "green" if overall_green else "red",
        "needs_agent_review": needs_review,
    }


class StatusShapeError(RuntimeError):
    """status.json cannot be safely updated (not JSON / soak block of the wrong type)."""


def ensure_soak_shape(status, n_total):
    """Make `status` carry the soak block the writer needs, without touching anything else.
    Returns the list of fields it added. Missing `soak`, `soak.days`, `n_total`,
    `consecutive_green`, `state` are created (a dashboard seeded without them is common —
    live, the first scheduled write crashed on a status.json with no `soak.days`); a soak
    block or days list of the WRONG TYPE is never overwritten — StatusShapeError instead.
    KEEP IN SYNC (soak_check.py / soak_check_lambda.py)."""
    if not isinstance(status, dict):
        raise StatusShapeError(f"status.json holds a {type(status).__name__}, not a JSON object")
    added = []
    soak = status.get("soak")
    if soak is None:
        soak = status["soak"] = {}
        added.append("soak")
    elif not isinstance(soak, dict):
        raise StatusShapeError(f"status.json 'soak' is a {type(soak).__name__}, not an object — repair it "
                               "(dashboard_update.py --replace soak) before the soak writer runs")
    days = soak.get("days")
    if days is None:
        soak["days"] = []
        added.append("soak.days")
    elif not isinstance(days, list) or not all(isinstance(d, dict) for d in days):
        raise StatusShapeError("status.json 'soak.days' must be a list of objects — repair it before the "
                               "soak writer runs")
    else:
        # Everything the writer/streak consumes: a real UTC calendar date per day (sorted and
        # compared as dates), `checks` an object, `overall` a string, `needs_agent_review` a bool.
        for d in days:
            date = d.get("date")
            try:
                if not isinstance(date, str) or len(date) != 10:
                    raise ValueError
                datetime.date.fromisoformat(date)
            except ValueError:
                raise StatusShapeError(f"status.json soak.days[].date {date!r} is not a real YYYY-MM-DD date — "
                                       "repair it before the soak writer runs") from None
            if not isinstance(d.get("checks", {}), dict) or not isinstance(d.get("overall", ""), str) \
                    or not isinstance(d.get("needs_agent_review", False), bool):
                raise StatusShapeError(f"status.json soak.days[{date}] has a checks/overall/needs_agent_review "
                                       "field of the wrong type — repair it before the soak writer runs")
    for field, default in (("n_total", n_total), ("consecutive_green", 0), ("state", "active")):
        if field not in soak:
            soak[field] = default
            added.append(f"soak.{field}")
    return added


def _parse_status(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError as e:
        raise StatusShapeError(f"status.json is not valid JSON ({e}) — rebuild it (dashboard_update.py "
                               "refuses to patch a corrupt file) before the soak writer runs") from e


def update_status_json(status_path, day_result, n_total):
    """Idempotent per calendar day — see soak_check_lambda.py's update_status_json for
    why: a re-run for a day already in soak.days[] overwrites it in place, and
    consecutive_green is recomputed from days[] itself (trailing green run), never
    incremented, so it can never drift from what's actually on disk."""
    status = _parse_status(status_path.read_text()) if status_path.exists() else {}
    ensure_soak_shape(status, n_total)
    soak = status["soak"]
    days = soak["days"]
    existing_idx = next((i for i, d in enumerate(days) if d.get("date") == day_result["date"]), None)
    if existing_idx is not None:
        days[existing_idx] = day_result
    else:
        days.append(day_result)
    consecutive = _green_streak(days, max(d["date"] for d in days))   # latest recorded day (see Lambda)
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
    ap.add_argument("--run-date", help="UTC day this run is FOR (YYYY-MM-DD); default: the UTC date when the "
                                       "run STARTS — pinned before the checks, so finishing after midnight "
                                       "does not shift it")
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text())

    engagement_dir = args.config.parent.parent  # dashboard/soak-config.json -> engagement root
    status_path = args.config.parent / "status.json"
    log_path = args.config.parent / "activity-log.jsonl"

    started_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        # Fail fast, before the DB checks, if status.json can't take today's result.
        ensure_soak_shape(_parse_status(status_path.read_text()) if status_path.exists() else {}, cfg["n_total"])
    except StatusShapeError as e:
        sys.exit(f"soak_check.py: {e}")
    try:
        run_date = args.run_date or started_date
        datetime.date.fromisoformat(run_date)
        day_result = run_day(cfg, run_date)
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
