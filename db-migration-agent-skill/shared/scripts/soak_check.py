#!/usr/bin/env python3
"""Runs the mechanical half of a Phase 7.7 soak day: row counts, checksums, schema
drift, and CloudWatch alarm/headroom state. Writes dashboard/status.json's "soak"
object, appends dashboard/activity-log.jsonl, and writes soak-report-day-N.md from
the template. Judgment calls (interpreting a RED day, deciding what an anomaly
means) stay with the agent — this script only reports facts and flags for review.

Usage: python3 soak_check.py --config dashboard/soak-config.json
Config is written once by the agent during Phase 7.7 setup — see
shared/reference/execution-runbooks.md §Soak automation for the schema and for
how to schedule this (cron or EventBridge Scheduler + Lambda).
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

# Matches the CDCLatencySource/Target warning threshold used elsewhere in this skill.
# AWS gives no CDC latency SLA (see dms-best-practices.md) — this is a soft, tunable
# gate, not an AWS-blessed hard number. This script doesn't compute lag itself (see
# `replication_lag: None` below); it's defined here for whoever wires that metric in.
LAG_THRESHOLD_S = 30
HEADROOM_THRESHOLD_PCT = 30


def run_mysql(host, user, password, database, sql):
    cmd = ["mysql", "-h", host, "-u", user, f"-p{password}", "-N", "-B", database, "-e", sql]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()


def run_psql(host, user, password, database, sql):
    env = {"PGPASSWORD": password}
    cmd = ["psql", "-h", host, "-U", user, "-d", database, "-t", "-A", "-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env).stdout.strip()


def row_count(engine, conn, table):
    sql = f"SELECT COUNT(*) FROM {table}"
    out = run_mysql(**conn, sql=sql) if engine == "mysql" else run_psql(**conn, sql=sql)
    return int(out.splitlines()[-1]) if out else None


def checksum(engine, conn, table):
    if engine == "mysql":
        out = run_mysql(**conn, sql=f"CHECKSUM TABLE {table}")
        return out.split()[-1] if out else None
    sql = f"SELECT md5(string_agg(t.*::text, '' ORDER BY t.*)) FROM {table} t"
    out = run_psql(**conn, sql=sql)
    return out or None


def columns(engine, conn, table):
    if engine == "mysql":
        sql = f"SELECT column_name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{table}' ORDER BY column_name"
        out = run_mysql(**conn, sql=sql)
    else:
        sql = f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' ORDER BY column_name"
        out = run_psql(**conn, sql=sql)
    return sorted(l.strip() for l in out.splitlines() if l.strip())


def cloudwatch_alarms(alarm_names, region):
    if not alarm_names:
        return [], True
    cmd = ["aws", "cloudwatch", "describe-alarms", "--alarm-names", *alarm_names,
           "--region", region, "--query", "MetricAlarms[?StateValue=='ALARM'].AlarmName", "--output", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    firing = json.loads(out) if out.strip() else []
    return firing, len(firing) == 0


def db_headroom_pct(db_instance_id, region):
    """FreeStorageSpace as % of AllocatedStorage — a proxy for storage headroom."""
    cmd = ["aws", "rds", "describe-db-instances", "--db-instance-identifier", db_instance_id,
           "--region", region, "--query", "DBInstances[0].AllocatedStorage", "--output", "text"]
    allocated_gb = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    if not allocated_gb or allocated_gb == "None":
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


def run_day(cfg):
    engine = cfg["engine"]
    src, tgt = cfg["source"], cfg["target"]
    tables = cfg["tables"]

    row_check = {"pass": True, "detail": {}}
    for t in tables:
        sc, tc = row_count(engine, src, t), row_count(engine, tgt, t)
        row_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc:
            row_check["pass"] = False

    checksum_tables = cfg.get("checksum_tables", tables[:2])
    checksum_check = {"pass": True, "detail": {}}
    for t in checksum_tables:
        sc, tc = checksum(engine, src, t), checksum(engine, tgt, t)
        checksum_check["detail"][t] = {"source": sc, "target": tc}
        if sc != tc or sc is None:
            checksum_check["pass"] = False

    drift_check = {"pass": True, "detail": {}}
    for t in tables:
        sc, tc = columns(engine, src, t), columns(engine, tgt, t)
        if sc != tc:
            drift_check["pass"] = False
            drift_check["detail"][t] = {"source_only": sorted(set(sc) - set(tc)), "target_only": sorted(set(tc) - set(sc))}

    firing_alarms, alarms_pass = cloudwatch_alarms(cfg.get("alarm_names", []), cfg.get("region", "us-east-1"))
    headroom = db_headroom_pct(cfg["target_db_instance_id"], cfg.get("region", "us-east-1")) if cfg.get("target_db_instance_id") else None
    headroom_pass = headroom is None or headroom > HEADROOM_THRESHOLD_PCT

    checks = {
        "row_count": row_check["pass"],
        "checksum": checksum_check["pass"],
        "alarms": alarms_pass,
        "headroom": headroom_pass,
        "schema_drift": drift_check["pass"],
        # replication_lag, customer_test_suite: left null — need a live CDC metric
        # query or an external test-suite result the script has no access to; the
        # agent fills these in on review, this script never guesses them.
        "replication_lag": None,
        "customer_test_suite": None,
    }
    mechanical_checks = [v for k, v in checks.items() if v is not None]
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


def update_status_json(status_path, day_result, n_total):
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    soak = status.setdefault("soak", {"days": [], "n_total": n_total, "consecutive_green": 0, "state": "active"})
    soak["days"].append(day_result)
    soak["consecutive_green"] = 0 if day_result["overall"] != "green" else soak["consecutive_green"] + 1
    soak["n_total"] = n_total
    # Lets the dashboard flag a silently-missed run (host was down, cron didn't fire,
    # script crashed) — a stale soak.days[] entry looks identical to "waiting for
    # tomorrow" unless something records when the last run actually happened.
    soak["last_checked_at"] = datetime.datetime.now().astimezone().isoformat()
    soak["state"] = "complete" if soak["consecutive_green"] >= n_total else "active"
    status["soak"] = soak
    status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))


def append_activity_log(log_path, day_result):
    entry = {
        "time": datetime.datetime.now().astimezone().isoformat(),
        "phase": "7.7",
        "title": f"Soak day {day_result['date']} — {day_result['overall']}",
        "action": "automated soak_check.py run (row count, checksum, schema drift, alarms, headroom)",
        "result": day_result["overall"],
        "detail": "needs_agent_review" if day_result["needs_agent_review"] else "all mechanical checks green",
        "files": [],
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_soak_report(reports_dir, day_result, day_n, n_total, consecutive_green):
    # shared/templates/soak-report.md has free-text fields (customer visibility,
    # notes) only the agent can fill in; this script writes the mechanical-check
    # facts as a companion file, which the agent then folds into that template.
    checks = day_result["checks"]
    out_path = reports_dir / f"soak-report-day{day_n}.md"
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

    day_result = run_day(cfg)
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
