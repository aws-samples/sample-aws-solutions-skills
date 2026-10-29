#!/usr/bin/env python3
"""Bounded, fully detached publisher: keeps the S3 copy of dashboard/status.json and
dashboard/activity-log.jsonl in step with the local files, so a presigned dashboard link
shows current progress OUTSIDE the soak window — then exits by itself.

WHY (live, OBS-48): an agent started `while true; do aws s3 cp …; sleep 60; done &` from its
own tool shell to keep the presigned dashboard current. A headless `claude -p` turn waits for
child processes still attached to its shell, so the turn never ended — 77+ minutes of no
output. A publisher that must outlive the turn has to be (a) fully detached from the agent's
shell — new session, stdin from /dev/null, stdout/stderr to its own log — and (b) bounded:
it stops at --until (the end of the window it serves), not never.

Usage (from the engagement directory):
    # start: returns at once, prints the PID, log, pidfile and the stop command
    python3 -B shared/scripts/dashboard_s3_publish.py --dir dashboard --bucket <bucket> \\
        --region <bucket-region> --until 2026-10-29T06:00:00Z --detach
    # one push now (no loop), e.g. right after a dashboard_update.py checkpoint
    python3 -B shared/scripts/dashboard_s3_publish.py --dir dashboard --bucket <bucket> --once
    # stop (also part of Phase 9 cleanup)
    python3 -B shared/scripts/dashboard_s3_publish.py --dir dashboard --stop

What it publishes: status.json and activity-log.jsonl only — never index.html (the bucket's
copy is the presigned-URL-materialized one, generate_presigned_urls.py), never .status.lock.
A file is uploaded only when its content changed since the last upload.

SOAK WINDOW GUARD (fails closed): during the soak the bucket's status.json is the LIVE copy
that the soak Lambda reads and writes (dashboard.md §The update rule); uploading the local file
over it would erase the Lambda's results. EVERY cycle that has anything to publish — including a
log-only cycle — runs, in this order: (1) read the activity log's ETag, THEN read status.json
(body + ETag) and STOP (exit 3) if it carries an active soak (`soak.state == "active"`), is
malformed, or cannot be read — only "no such key" is benign; (2) put status.json (if it changed)
with `IfMatch=<its ETag from step 1>` (or `IfNoneMatch='*'` when absent) — ANY conflict or error
abandons the whole cycle and the log is not touched; (3) put the log with `IfMatch=<the log ETag
read BEFORE the guard>` (or `IfNoneMatch='*'`) — a conflict abandons the cycle. Because the log
ETag is taken before the status guard, evidence appended at any point after it (e.g. by a soak
activated after the guard read) changes the ETag and the log write fails (412) instead of
overwriting it; the next cycle re-reads and stops on the active soak.
Still: STOP the publisher and confirm the stop BEFORE seeding or activating a soak
(execution-runbooks.md §soak) — the guard is a backstop, not the procedure.

DEADLINE (the guarantee, precisely): (a) no new S3 call starts unless now + CALL_BUDGET_S < --until
(checked before every cycle and every call); (b) an S3 call in flight — including a slow streaming
body read, which socket timeouts do not bound — is INTERRUPTED at --until: before each call the main
thread arms signal.setitimer(ITIMER_REAL, <seconds left>) and SIGALRM raises DeadlineExceeded inside
the call; SIGTERM likewise raises Terminated inside an in-flight call (and stops the loop); (c) a
watchdog thread hard-exits the process (os._exit(4)) at --until + WATCHDOG_GRACE_S (30 s) whatever
happens — it starts BEFORE any boto3/botocore session, client, credential or bucket-region lookup,
and that start-up (region discovery included, with the same short-timeout config) also runs under
the SIGALRM deadline. botocore also runs with 5 s connect / 5 s read timeouts and no retries.
An INTERRUPTED PutObject has an UNKNOWN outcome: S3 may already have committed it before the
interruption. Recovery therefore never assumes either way: the next run re-reads BOTH objects (and
their ETags) and treats status and log as possibly reflecting different cycles.

PID FILE / STOP: the pidfile records the PID, a random run token (also passed to the detached
child as --run-token) and the process start time (/proc/<pid>/stat field 22). --stop signals only
a process whose cmdline contains this script's path AND that token AND whose start time matches;
PIDs <= 1, legacy/malformed pidfiles, or no /proc mean "cannot verify ownership" -> refuse to
signal (exit 1). A PID that no longer exists, or was reused (start time differs), is stale: the
pidfile is removed and nothing is signalled. It never signals a process group.

Credentials: the standard boto3 chain of the host it runs on (never argv, never a file it
writes); it needs s3:GetObject and s3:PutObject on the two keys (plus kms:Decrypt /
kms:GenerateDataKey for a CMK-encrypted bucket) — the same identity generate_presigned_urls.py uses. Temporary credentials end the publisher's usefulness when they expire: it logs the
error and exits non-zero rather than spinning.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PUBLISHED = ("status.json", "activity-log.jsonl")
CONTENT_TYPES = {"status.json": "application/json", "activity-log.jsonl": "application/x-ndjson"}
MAX_WINDOW = datetime.timedelta(days=8)   # a publisher serves one window, never "forever"
CONNECT_TIMEOUT_S, READ_TIMEOUT_S, MAX_ATTEMPTS = 5, 5, 1   # one attempt: no retries
CALL_BUDGET_S = 15   # >= one call's worst case (connect + read + margin); see DEADLINE above
WATCHDOG_GRACE_S = 30   # hard os._exit backstop after --until
_STOP = {"flag": False}


class PublishError(Exception):
    """Configuration or S3 problem: logged, publisher exits non-zero."""


class SoakActive(PublishError):
    """The bucket copy is (or may be) the soak Lambda's live copy: never overwrite it."""


class DeadlineReached(PublishError):
    """--until reached (or too close to start another S3 call): stop cleanly."""


class Stopped(PublishError):
    """SIGTERM/SIGINT received: stop cleanly between calls."""


class DeadlineExceeded(BaseException):
    """SIGALRM at --until: raised INSIDE an in-flight S3 call. BaseException so no SDK
    `except Exception` can swallow it."""


class Terminated(BaseException):
    """SIGTERM/SIGINT: raised inside an in-flight S3 call (or a wait) so the stop is prompt."""


def _on_alarm(_signum, _frame):
    raise DeadlineExceeded("--until reached during an S3 call")


def _timed(until: datetime.datetime | None, fn):
    """Run one S3 call (incl. its body read) under an absolute deadline: SIGALRM at --until.
    Main thread only (signals); elsewhere — or without --until — the call runs untimed."""
    if until is None or threading.current_thread() is not threading.main_thread():
        return fn()
    remaining = (until - utcnow()).total_seconds()
    if remaining <= 0:
        raise DeadlineExceeded("--until reached")
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def start_watchdog(until: datetime.datetime, grace: float | None = None) -> threading.Thread:
    """Hard backstop: os._exit(4) at until + grace, whatever the main thread is doing."""
    hard = until + datetime.timedelta(seconds=WATCHDOG_GRACE_S if grace is None else grace)

    def run():
        while True:
            left = (hard - utcnow()).total_seconds()
            if left <= 0:
                os.write(2, b"dashboard_s3_publish: watchdog: --until + grace passed; hard exit\n")
                os._exit(4)
            time.sleep(min(left, 1.0))
    t = threading.Thread(target=run, name="publisher-watchdog", daemon=True)
    t.start()
    return t


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_until(text: str, now: datetime.datetime | None = None) -> datetime.datetime:
    try:
        until = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublishError(f"--until {text!r} is not an ISO-8601 time") from exc
    if until.tzinfo is None:
        raise PublishError("--until needs an explicit zone (use UTC, e.g. 2026-10-29T06:00:00Z)")
    now = now or utcnow()
    if until <= now:
        raise PublishError(f"--until {text} is not in the future")
    if until - now > MAX_WINDOW:
        raise PublishError(f"--until is more than {MAX_WINDOW.days} days away — a publisher serves one bounded window")
    return until


def _error_code(exc) -> str | None:
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
    return str(code) if code is not None else (type(exc).__name__ if type(exc).__name__ == "NoSuchKey" else None)


_ABSENT = ("NoSuchKey", "404", "NotFound")
_CONFLICT = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")


def check_time(until: datetime.datetime | None, budget: float) -> None:
    """Raise before any work that could run past --until, or after a stop signal."""
    if _STOP["flag"]:
        raise Stopped("stop signal received")
    if until is not None and (until - utcnow()).total_seconds() <= budget:
        raise DeadlineReached(f"window ends {until.isoformat()} — not starting another S3 call")


def read_state(client, bucket: str, prefix: str, until, budget: float) -> dict[str, str | None]:
    """ETag per published key read THIS cycle (None = object absent). The log ETag is read FIRST,
    then the status guard — so anything appended to the log after this point fails the log's
    IfMatch. Fails closed: an unreadable or malformed status.json, or one with an active soak,
    raises SoakActive."""
    etags: dict[str, str | None] = {}
    check_time(until, budget)
    lkey = f"{prefix}activity-log.jsonl"
    try:
        etags["activity-log.jsonl"] = _timed(until, lambda: client.head_object(Bucket=bucket, Key=lkey)).get("ETag")
        if etags["activity-log.jsonl"] is None:
            raise SoakActive(f"s3://{bucket}/{lkey} returned no ETag — failing closed")
    except SoakActive:
        raise
    except Exception as exc:  # noqa: BLE001 - only "absent" is benign
        if _error_code(exc) not in _ABSENT:
            raise SoakActive(f"cannot read s3://{bucket}/{lkey} ({_error_code(exc) or type(exc).__name__}) "
                             "— failing closed") from None
        etags["activity-log.jsonl"] = None
    check_time(until, budget)
    key = f"{prefix}status.json"

    def _get():
        obj = client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read(), obj.get("ETag")      # the body read is inside the timed region
    try:
        body, etags["status.json"] = _timed(until, _get)
    except Exception as exc:  # noqa: BLE001 - only "absent" is benign
        if _error_code(exc) not in _ABSENT:
            raise SoakActive(f"cannot read s3://{bucket}/{key} for the soak guard ({_error_code(exc) or type(exc).__name__}) "
                             "— failing closed") from None
        body, etags["status.json"] = None, None
    if body is not None:
        try:
            doc = json.loads(body)
        except ValueError:
            raise SoakActive(f"s3://{bucket}/{key} is not valid JSON — failing closed (never overwrite what "
                             "cannot be read)") from None
        if not isinstance(doc, dict):
            raise SoakActive(f"s3://{bucket}/{key} is not a JSON object — failing closed") from None
        soak = doc.get("soak")
        if soak is not None and not isinstance(soak, dict):
            raise SoakActive(f"s3://{bucket}/{key} has a malformed soak section — failing closed")
        if isinstance(soak, dict) and soak.get("state") == "active":
            raise SoakActive(f"s3://{bucket}/{key} carries an active soak — the bucket copy is the soak Lambda's "
                             "live copy; stopping (read/modify/write it instead, dashboard.md)")
        if etags["status.json"] is None:
            raise SoakActive(f"s3://{bucket}/{key} returned no ETag — cannot write conditionally; failing closed")
    return etags


def _pending(directory: Path, last: dict[str, str], log) -> dict[str, tuple[bytes, str]]:
    """Local files whose content changed since the last successful upload (name -> (data, sha))."""
    out = {}
    for name in PUBLISHED:
        path = directory / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if name == "status.json":
            try:
                json.loads(data)
            except ValueError:
                log(f"skip {name}: not valid JSON right now (a writer may be mid-update)")
                continue
        digest = hashlib.sha256(data).hexdigest()
        if last.get(name) != digest:
            out[name] = (data, digest)
    return out


def publish_once(client, bucket: str, prefix: str, directory: Path, last: dict[str, str],
                 dry_run: bool = False, log=print, until: datetime.datetime | None = None) -> list[str]:
    """One cycle: (1) read the log ETag, then status (guard) + its ETag, (2) conditional put of
    status.json, (3) conditional put of the log (IfMatch on the ETag read before the guard). Any conflict abandons the rest of the cycle (nothing is
    overwritten; the next cycle re-reads). Returns the names uploaded. Raises SoakActive (fail
    closed), DeadlineReached/Stopped near --until or on a stop signal."""
    budget = 0.0 if dry_run else float(CALL_BUDGET_S)
    pending = _pending(directory, last, log)
    if not pending:
        return []
    etags = {} if dry_run else read_state(client, bucket, prefix, until, budget)   # step 1
    uploaded = []
    for name in PUBLISHED:                                                          # steps 2, 3
        if name not in pending:
            continue
        data, digest = pending[name]
        check_time(until, budget)
        if not dry_run:
            cond = {"IfMatch": etags[name]} if etags.get(name) else {"IfNoneMatch": "*"}
            try:
                _timed(until, lambda: client.put_object(Bucket=bucket, Key=f"{prefix}{name}", Body=data,
                                                        ContentType=CONTENT_TYPES[name], CacheControl="no-store",
                                                        **cond))
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) in _CONFLICT:
                    log(f"abandon cycle: s3 {name} changed since it was read — nothing more is written; "
                        "re-reading next cycle")
                    return uploaded
                raise                                   # any other error: abandon (the loop exits)
        last[name] = digest
        uploaded.append(name)
        log(f"{'would upload' if dry_run else 'uploaded'} {name} ({len(data)} bytes, sha256 {digest[:12]})")
    return uploaded


def run_loop(client, args, until: datetime.datetime, log) -> int:
    _STOP["flag"] = False

    def _term(_signum, _frame):
        _STOP["flag"] = True
        raise Terminated("stop signal")             # interrupts an in-flight call or a wait
    previous = {sig: signal.signal(sig, _term) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return _loop(client, args, until, log)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _loop(client, args, until: datetime.datetime, log) -> int:
    last: dict[str, str] = {}
    budget = 0.0 if args.dry_run else float(CALL_BUDGET_S)
    log(f"publisher started (pid {os.getpid()}), interval {args.interval}s, until {until.isoformat()}")
    reason = None
    while True:
        try:
            check_time(until, budget)                       # before each cycle
            publish_once(client, args.bucket, args.prefix, Path(args.dir), last, args.dry_run, log, until)
            wait_end = time.monotonic() + max(0.0, min(args.interval, (until - utcnow()).total_seconds()))
            while not _STOP["flag"] and time.monotonic() < wait_end:
                time.sleep(min(0.2, max(0.0, wait_end - time.monotonic())))
            if _STOP["flag"]:
                raise Stopped("stop signal received")
        except DeadlineReached:
            reason = f"window ended ({until.isoformat()})"
            break
        except DeadlineExceeded:
            reason = f"window ended ({until.isoformat()}) — an in-flight S3 call was interrupted"
            break
        except (Stopped, Terminated):
            reason = "signal"
            break
        except SoakActive as exc:
            log(f"STOP: {exc}")
            return 3
        except PublishError as exc:
            log(f"ERROR: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 - e.g. expired credentials: log and exit, never spin
            log(f"ERROR: S3 call failed ({_error_code(exc) or type(exc).__name__})")
            return 1
    log(f"publisher stopped: {reason}")
    return 0


def pidfile_path(args) -> Path:
    return Path(args.pidfile) if args.pidfile else Path(args.dir) / ".publisher.pid"


def _proc_start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time in clock ticks); None if unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = stat[stat.rfind(")") + 2:].split()   # fields after "(comm)" start at field 3
    return rest[19] if len(rest) > 19 else None


def _read_record(pidfile: Path) -> dict | None:
    """The pidfile record, or None if missing/legacy/malformed (ownership then can't be shown)."""
    try:
        rec = json.loads(pidfile.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    pid = rec.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(rec.get("token"), str) \
            or not isinstance(rec.get("start_time"), str) or len(rec["token"]) < 16:
        return None
    return rec


def ownership(rec: dict) -> tuple[str, str]:
    """('ours'|'stale'|'unverified', reason). Only 'ours' may be signalled."""
    pid = rec["pid"]
    if pid <= 1:
        return "unverified", f"pid {pid} is not a valid publisher pid (<= 1)"
    if not Path("/proc/self/stat").exists():
        return "unverified", "/proc is not available — cannot verify ownership"
    if not Path(f"/proc/{pid}").exists():
        return "stale", f"pid {pid} no longer exists"
    start = _proc_start_time(pid)
    if start is None:
        return "unverified", f"cannot read /proc/{pid}/stat"
    if start != rec["start_time"]:
        return "stale", f"pid {pid} was reused (start time differs)"
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return "unverified", f"cannot read /proc/{pid}/cmdline"
    script = str(Path(__file__).resolve()).encode()
    token = rec["token"].encode()
    has_token = any(a == b"--run-token" and i + 1 < len(args) and args[i + 1] == token for i, a in enumerate(args))
    if script not in args or not has_token:
        return "unverified", f"pid {pid} is not this publisher (script path / run token do not match)"
    return "ours", f"pid {pid}"


def detach(args, argv: list[str]) -> int:
    """Re-run this script fully detached: new session, stdin /dev/null, own log. Returns at once."""
    pidfile = pidfile_path(args)
    if pidfile.exists():
        rec = _read_record(pidfile)
        if rec is None:
            raise PublishError(f"{pidfile} exists but cannot be verified (legacy/malformed) — check the process and "
                               "remove the file by hand before starting another publisher")
        state, why = ownership(rec)
        if state == "ours":
            raise PublishError(f"a publisher is already running ({why}, {pidfile}) — stop it first")
        if state == "unverified":
            raise PublishError(f"{pidfile}: {why} — refusing to start a second publisher")
        pidfile.unlink()
    log_path = Path(args.log) if args.log else Path(args.dir) / ".publisher.log"
    token = secrets.token_hex(16)
    child_argv = [a for a in argv if a != "--detach"] + ["--run-token", token]
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), *child_argv],
                                stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    finally:
        os.close(fd)
    start = _proc_start_time(proc.pid)
    if start is None:
        proc.terminate()   # our own child, by its exact PID
        raise PublishError("cannot read the new publisher's start time from /proc — stopped it; ownership could "
                           "not be recorded")
    fdp = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fdp, "w") as fh:
        json.dump({"pid": proc.pid, "token": token, "start_time": start,
                   "script": str(Path(__file__).resolve())}, fh)
    stop_cmd = (f"python3 -B {Path(__file__).resolve()} --dir {args.dir} --stop"
                + (f" --pidfile {args.pidfile}" if args.pidfile else ""))
    print(json.dumps({"pid": proc.pid, "pidfile": str(pidfile), "log": str(log_path),
                      "until": args.until, "stop": stop_cmd,
                      "note": "detached (own session, stdin /dev/null): it does not hold the agent's turn open"}))
    return 0


def stop(args) -> int:
    """Signal the recorded publisher only if its ownership is proven; never a process group."""
    pidfile = pidfile_path(args)
    if not pidfile.exists():
        print(f"no publisher pidfile at {pidfile} — nothing to stop")
        return 0
    rec = _read_record(pidfile)
    if rec is None:
        print(f"{pidfile} is legacy/malformed — cannot verify ownership; NOT signalling anything. Check the "
              "process yourself, stop it by its exact PID, then remove the file.", file=sys.stderr)
        return 1
    state, why = ownership(rec)
    if state == "stale":
        pidfile.unlink(missing_ok=True)
        print(f"publisher not running ({why}) — stale pidfile removed, nothing signalled")
        return 0
    if state != "ours":
        print(f"refusing to signal: {why}", file=sys.stderr)
        return 1
    pid = rec["pid"]
    os.kill(pid, signal.SIGTERM)          # pid > 1, verified: never a group or broadcast
    for _ in range(300):                  # SIGTERM interrupts an in-flight call; 30 s is a generous cap
        if ownership(rec)[0] != "ours":
            break
        time.sleep(0.1)
    else:
        print(f"publisher pid {pid} did not exit within 30 s after SIGTERM", file=sys.stderr)
        return 1
    pidfile.unlink(missing_ok=True)
    print(f"stopped publisher pid {pid}")
    return 0


def _short_config():
    from botocore.config import Config
    return Config(signature_version="s3v4", connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                  retries={"total_max_attempts": MAX_ATTEMPTS, "mode": "standard"})   # 1 attempt: no retries


def _discover_region(bucket: str) -> str:
    """The bucket's real region (generate_presigned_urls.py logic) via a SHORT-timeout probe client."""
    import boto3
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from generate_presigned_urls import resolve_bucket_region   # same bucket-region logic
    try:
        return resolve_bucket_region(bucket, probe_client=boto3.client("s3", config=_short_config()))
    except SystemExit:
        raise PublishError("could not determine the bucket's region — pass --region") from None


def make_client(bucket: str, region: str | None, until: datetime.datetime | None = None):
    """Region discovery and client/credential setup run under the same absolute deadline as every
    S3 call (SIGALRM at --until; the watchdog is already running when this is called)."""
    def build():
        import boto3
        return boto3.client("s3", region_name=region or _discover_region(bucket), config=_short_config())
    return _timed(until, build)


def conditional_writes_supported() -> bool:
    """True when the installed botocore models PutObject IfMatch/IfNoneMatch (S3 conditional writes)."""
    try:
        import botocore.session
        members = botocore.session.get_session().get_service_model("s3").operation_model("PutObject").input_shape.members
    except Exception:  # noqa: BLE001
        return False
    return "IfMatch" in members and "IfNoneMatch" in members


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default="dashboard", help="local dashboard directory (default: dashboard)")
    ap.add_argument("--bucket")
    ap.add_argument("--prefix", default="", help='key prefix inside the bucket (e.g. "" or "eng/")')
    ap.add_argument("--region", help="the bucket's region (detected from S3 when omitted)")
    ap.add_argument("--until", help="UTC ISO-8601 end of the window to serve (required for the loop; max 8 days)")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between change checks (default 30)")
    ap.add_argument("--once", action="store_true", help="publish changed files once and exit")
    ap.add_argument("--detach", action="store_true", help="start the loop fully detached and return at once")
    ap.add_argument("--stop", action="store_true", help="stop the detached publisher (pidfile)")
    ap.add_argument("--pidfile", help="default <dir>/.publisher.pid")
    ap.add_argument("--log", help="detached log file (default <dir>/.publisher.log, mode 0600)")
    ap.add_argument("--dry-run", action="store_true", help="no S3 calls; log what would be uploaded")
    ap.add_argument("--run-token", help=argparse.SUPPRESS)   # set by --detach; identifies the process for --stop
    args = ap.parse_args(argv)

    def log(msg):
        print(f"{utcnow().isoformat(timespec='seconds')} {msg}", flush=True)
    try:
        if args.stop:
            return stop(args)
        if not args.bucket:
            raise PublishError("--bucket is required")
        if not Path(args.dir).is_dir():
            raise PublishError(f"--dir {args.dir} is not a directory")
        if not args.once:
            if not args.until:
                raise PublishError("--until is required for the loop — a publisher serves one bounded window")
            until = parse_until(args.until)
            if not args.interval >= 1 and not args.dry_run:
                raise PublishError("--interval must be at least 1 second")
            if args.detach:
                return detach(args, argv)
        if not args.once:
            # hard backstop BEFORE any boto3/botocore session, client, credential or region lookup
            start_watchdog(until)       # os._exit(4) at --until + WATCHDOG_GRACE_S
        if not args.dry_run and not conditional_writes_supported():
            raise SoakActive("the installed boto3/botocore does not support S3 conditional writes (PutObject "
                             "IfMatch/IfNoneMatch) — upgrade boto3; refusing to publish unconditionally")
        try:
            client = None if args.dry_run else make_client(args.bucket, args.region,
                                                           None if args.once else until)
        except DeadlineExceeded:
            log(f"publisher stopped: window ended ({until.isoformat()}) during start-up")
            return 0
        if args.once:
            try:
                publish_once(client, args.bucket, args.prefix, Path(args.dir), {}, args.dry_run, log)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, PublishError):
                    raise
                raise PublishError(f"S3 call failed ({_error_code(exc) or type(exc).__name__})") from None
            return 0
        return run_loop(client, args, until, log)
    except SoakActive as exc:
        print(f"dashboard_s3_publish: STOP: {exc}", file=sys.stderr)
        return 3
    except PublishError as exc:
        print(f"dashboard_s3_publish: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
