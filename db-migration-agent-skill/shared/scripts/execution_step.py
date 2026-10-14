#!/usr/bin/env python3
"""Run one reviewed operation and its verifier, with persistent checkpoints.

Local POSIX helper; no AWS calls, credential handling, automatic retries or rollback.
The record binds reviewed inputs, not the identity/authority of a human approver.
Use IAM/database permissions to enforce access. See reference/execution-control.md.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

import dashboard_update as dashboard


class StepError(Exception):
    pass


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
EFFECTS = {"read_only", "target_write", "rehearsal", "cutover", "rollback"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(path):
    def reject_constant(value):
        raise StepError(f"non-finite JSON number {value} in {path}")

    with open(path, encoding="utf-8") as stream:
        value = json.load(stream, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise StepError(f"expected an object: {path}")
    return value


def write(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".execution-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if load(path) != value:
            raise StepError(f"record read-back failed: {path}")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise StepError("attempt/step identifiers must be short path-free names")
    return value


def local(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise StepError("workspace paths must be nonempty relative paths")
    parts = Path(relative).parts
    if ".." in parts:
        raise StepError("parent traversal is not allowed")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise StepError(f"symlinks are not allowed: {relative}")
    if not path.resolve().is_relative_to(root):
        raise StepError("path escapes workspace")
    return path


def check_ref(root, ref):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise StepError("each input binding needs path and sha256")
    path = local(root, ref["path"])
    if not path.is_file():
        raise StepError(f"bound input is not a file: {ref['path']}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != ref["sha256"]:
        raise StepError(f"reviewed input changed: {ref['path']}")


def validate_spec(root, spec):
    required = {"phase", "step", "label", "plan", "plan_section", "dashboard",
                "effect", "scope", "authorization", "inputs", "operation", "verification"}
    optional = {"previous", "recovery_for", "interval_seconds", "attention_after_seconds"}
    if not required <= spec.keys() or spec.keys() - required - optional:
        raise StepError("spec has missing or unsupported fields")
    identifier(spec["step"])
    if spec["phase"] not in dashboard.PHASE_IDS or spec["effect"] not in EFFECTS:
        raise StepError("unknown phase or operation effect")
    for field in ("label", "plan_section", "scope"):
        if not isinstance(spec[field], str) or not spec[field].strip():
            raise StepError(f"{field} must describe this reviewed operation")
    for field in ("operation", "verification"):
        cmd = spec[field]
        if not isinstance(cmd, list) or not cmd or not all(
                isinstance(arg, str) and arg and "\0" not in arg for arg in cmd):
            raise StepError(f"{field} must be a nonempty argv array")
    for key, default, minimum, maximum in (
            ("interval_seconds", 60, 0.05, 60),
            ("attention_after_seconds", 3600, 0.05, 604800)):
        value = spec.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                math.isfinite(value) and minimum <= value <= maximum):
            raise StepError(f"{key} must be finite in [{minimum}, {maximum}]")
    if not isinstance(spec["inputs"], list) or not spec["inputs"]:
        raise StepError("bind the reviewed operation/verifier files in inputs")
    auth = spec["authorization"]
    if not isinstance(auth, dict) or set(auth) != {"path", "sha256", "reference"}:
        raise StepError("authorization needs path, sha256 and the accepted block reference")
    if not isinstance(auth["reference"], str) or not auth["reference"].strip():
        raise StepError("authorization reference cannot be empty")
    check_ref(root, {key: auth[key] for key in ("path", "sha256")})
    for ref in spec["inputs"]:
        check_ref(root, ref)
    local(root, spec["plan"]).read_text(encoding="utf-8")
    status = load(local(root, spec["dashboard"]) / "status.json")
    dashboard.validate_status(status)
    mode = str(status.get("mode", ""))
    if mode not in {"1", "2", "3"}:
        raise StepError("record the engagement mode before execution")
    if mode == "1" and spec["effect"] != "read_only":
        raise StepError("Mode 1 permits only read_only operations")
    if mode != "3" and spec["effect"] in {"cutover", "rollback"}:
        raise StepError("live cutover/rollback requires Mode 3")
    for effect, tag in (("cutover", "A4"), ("rollback", "A5")):
        if spec["effect"] == effect and not re.match(rf"^{tag}(?:$|[- :])", auth["reference"]):
            raise StepError(f"{effect} requires its own {tag} reference (A4b is not A4)")
    return spec


def publish(root, attempt, spec, status, detail, patch=None):
    """Use the existing validated publisher with a bounded subprocess wait."""
    token = uuid.uuid4().hex
    marker = f"[execution {attempt.name} {token}]"
    body = patch or {}
    # These fields describe this process only. Never set gates/approvals from an exit code.
    body = dashboard.merge_patch(body, {
        "current_phase": spec["phase"], "current_activity": detail,
        "phases": [{"id": spec["phase"], "status": "in_progress", "steps": [{
            "id": spec["step"], "label": spec["label"], "status": status,
            "detail": detail, "execution_attempt": attempt.name, "checkpoint_id": token,
        }]}],
    })
    patch_path = attempt / "checkpoint-patch.json"
    write(patch_path, body)
    result = {"done": "success", "blocked": "blocked", "in_progress": "in_progress"}[status]
    args = [sys.executable, str(Path(dashboard.__file__).resolve()),
            "--dashboard", str(local(root, spec["dashboard"])),
            "--plan", str(local(root, spec["plan"])), "--plan-section", spec["plan_section"],
            "--plan-line", f"{marker} {detail}", "--patch-file", str(patch_path),
            "--log-title", spec["label"], "--log-result", result,
            "--log-detail", f"{marker} {detail}", "--lock-timeout", "5"]
    # Inherit the worker's private log so failures retain the publisher's diagnosis.
    done = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=None, timeout=15)
    if done.returncode:
        raise StepError(f"checkpoint failed (exit {done.returncode}); inspect worker.log")
    return token


def checkpoint_exists(root, spec, record):
    token = record.get("checkpoint_id")
    if not token:
        raise StepError("predecessor has no confirmed completion checkpoint")
    status = load(local(root, spec["dashboard"]) / "status.json")
    dashboard.validate_status(status)
    phases = [p for p in status.get("phases", []) if p.get("id") == spec["phase"]]
    steps = [s for p in phases for s in p.get("steps", []) if s.get("id") == spec["step"]]
    if len(steps) != 1 or any(steps[0].get(k) != v for k, v in {
            "status": record["status"], "execution_attempt": record["attempt"],
            "checkpoint_id": token}.items()):
        raise StepError("predecessor dashboard checkpoint is missing or changed")
    marker = f"[execution {record['attempt']} {token}]"
    if marker not in local(root, spec["plan"]).read_text(encoding="utf-8"):
        raise StepError("predecessor plan checkpoint is missing")
    lines = (local(root, spec["dashboard"]) / "activity-log.jsonl").read_text(
        encoding="utf-8").split("\n")
    entries = [json.loads(line) for line in lines if line.strip()]
    matches = [entry for entry in entries if isinstance(entry, dict)
               and isinstance(entry.get("detail"), str) and marker in entry["detail"]]
    expected = {"done": "success", "blocked": "blocked"}[record["status"]]
    if (len(matches) != 1 or matches[0].get("result") != expected
            or matches[0].get("phase") != spec["phase"]):
        raise StepError("predecessor activity checkpoint is missing or changed")


def predecessor(root, state_root, spec):
    head = load(state_root / "head.json") if (state_root / "head.json").exists() else {}
    previous = spec.get("previous")
    if previous != head.get("attempt"):
        raise StepError("previous must name the latest attempt; stale branches are not allowed")
    if not previous:
        if spec.get("recovery_for"):
            raise StepError("recovery needs a failed predecessor")
        return
    prev_dir = state_root / identifier(previous)
    result_path = (prev_dir / "reconciliation.json" if (prev_dir / "reconciliation.json").exists()
                   else prev_dir / "result.json")
    record = load(result_path) if result_path.exists() else None
    if not record:
        raise StepError("predecessor has no terminal result; inspect/reconcile the same job")
    if record["status"] == "done":
        if spec.get("recovery_for"):
            raise StepError("successful predecessor does not need recovery")
    elif record["status"] == "blocked" and spec.get("recovery_for") == previous:
        if spec["step"] == load(prev_dir / "spec.json")["step"]:
            raise StepError("recovery must be a separate step, not a replay of the failed operation")
    else:
        raise StepError("failed predecessor requires a reviewed recovery step")
    checkpoint_exists(root, load(prev_dir / "spec.json"), record)


def reserve(root, state_root, name, spec):
    identifier(name)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with dashboard._Lock(state_root / ".execution.lock", 5):
        validate_spec(root, spec)
        predecessor(root, state_root, spec)
        attempt = state_root / name
        # Reserve before any checkpoint/launch; a failed publication never frees the name.
        attempt.mkdir(mode=0o700)
        write(attempt / "spec.json", spec)
        write(attempt / "state.json", {"attempt": name, "status": "reserved",
                                     "updated_at": now(), "workspace": str(root)})
        write(state_root / "head.json", {"attempt": name})
    return attempt


def observe_command(root, attempt, spec, record, cmd, stage, deadline):
    validate_spec(root, spec)  # recheck input hashes/mode before each actual launch
    record.update(stage=stage, status="in_progress", updated_at=now(),
                  job_pid=None, observed_exit_code=None)
    record["checkpoint_id"] = publish(root, attempt, spec, "in_progress", f"{stage}: starting")
    write(attempt / "state.json", record)
    with open(attempt / f"{stage}.log", "xb") as output:
        os.chmod(output.name, 0o600)
        # Publication can wait on another writer. Check again after that wait,
        # immediately before launch; its earlier validation is no longer current.
        validate_spec(root, spec)
        if time.monotonic() >= deadline:
            record["attention_required"] = True
            raise StepError("execution budget exceeded before launch; reconcile before further work")
        process = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        record.update(job_pid=process.pid, updated_at=now())
        write(attempt / "state.json", record)
        while True:
            code = process.poll()
            expired = time.monotonic() >= deadline
            if expired:
                record["attention_required"] = True
            detail = (f"{stage}: exit {code}" if code is not None else
                      f"{stage}: process running; database result not yet verified")
            if record.get("attention_required"):
                detail += "; execution budget exceeded — reconcile before further work"
            record.update(updated_at=now(), observed_exit_code=code)
            record["checkpoint_id"] = publish(root, attempt, spec, "in_progress", detail)
            write(attempt / "state.json", record)
            if code is not None:
                if code != 0 or record.get("attention_required"):
                    raise StepError(detail)
                return
            time.sleep(spec.get("interval_seconds", 60))


def worker(attempt):
    """A detached worker survives the caller's finite observation turn."""
    os.umask(0o077)
    with dashboard._Lock(attempt / ".worker.lock", 0):
        initial = load(attempt / "state.json")
        root = Path(initial["workspace"])
        spec = load(attempt / "spec.json")
        if ((attempt / "started.json").exists()
                or initial["status"] != "reserved"
                or (attempt / "reconciliation.json").exists()
                or (attempt / "result.json").exists()):
            raise StepError("attempt already started or closed; never relaunch it")
        write(attempt / "started.json", {"worker_pid": os.getpid(), "at": now()})
        record = dict(initial, worker_pid=os.getpid())
        deadline = time.monotonic() + spec.get("attention_after_seconds", 3600)
        try:
            for command, stage in (("operation", "operation"), ("verification", "verification")):
                observe_command(root, attempt, spec, record, spec[command], stage, deadline)
            # Verification produces observed facts, not just an exit code.
            evidence = load(attempt / "verification.json")
            if (not {"verified", "detail", "evidence"} <= evidence.keys()
                    or evidence.keys() - {"verified", "detail", "evidence", "dashboard_patch"}
                    or evidence["verified"] is not True):
                raise StepError("verifier must supply verified=true, detail and bound evidence")
            if not isinstance(evidence["detail"], str) or not evidence["detail"].strip():
                raise StepError("verification detail is missing")
            if not isinstance(evidence["evidence"], list) or not evidence["evidence"]:
                raise StepError("verification evidence is missing")
            for ref in evidence["evidence"]:
                check_ref(root, ref)
            patch = evidence.get("dashboard_patch", {})
            if not isinstance(patch, dict) or patch.keys() - {"migration_objects"}:
                raise StepError("verifier patches may update only observed migration_objects")
            record["verification"] = evidence
            record["checkpoint_id"] = publish(root, attempt, spec, "done", evidence["detail"], patch)
            record.update(status="done", updated_at=now(), job_pid=None)
        except Exception as exc:
            # A later dashboard repair cannot change this failed attempt into success.
            record.update(status="blocked", error=str(exc), updated_at=now())
            try:
                record["checkpoint_id"] = publish(root, attempt, spec, "blocked",
                                                  f"blocked: {exc}; reconcile before retry")
            except Exception as publication:
                record["checkpoint_id"] = None
                record["publication_error"] = str(publication)
        # Do not claim a terminal result if the command may still be running.
        # Such attempts require external read-only reconciliation, never a new launch.
        if record.get("observed_exit_code") is None and record.get("job_pid"):
            record["job_state"] = "unknown"
        else:
            write(attempt / "result.json", record)
        write(attempt / "state.json", record)


def status(attempt, wait_seconds=0):
    end = time.monotonic() + wait_seconds
    while True:
        path = (attempt / "reconciliation.json" if (attempt / "reconciliation.json").exists()
                else attempt / "result.json")
        record = load(path if path.exists() else attempt / "state.json")
        if path.exists():
            if record["status"] == "done":
                try:
                    checkpoint_exists(Path(record["workspace"]), load(attempt / "spec.json"), record)
                except (StepError, dashboard.UpdateError, OSError, ValueError, TypeError, KeyError) as exc:
                    return dict(record, observation=f"completion checkpoint unconfirmed: {exc}"), 1
            return record, 0 if record["status"] == "done" else 1
        if record.get("status") == "blocked" or record.get("attention_required"):
            return record, 1
        if (attempt / "started.json").exists():
            # A PID can be reused; the worker's lifetime lock is the local
            # supervision signal. A fresh timestamp alone proves no liveness.
            with (attempt / ".worker.lock").open("rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    # The worker may have finished since the initial read.
                    if ((attempt / "result.json").exists()
                            or (attempt / "reconciliation.json").exists()):
                        continue
                    return dict(record, observation="worker lost; job outcome unknown; do not relaunch"), 1
        updated = datetime.datetime.fromisoformat(record["updated_at"])
        age = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
        spec = load(attempt / "spec.json")
        if age > spec.get("interval_seconds", 60) + 35:
            return dict(record, observation="stale; job outcome unknown; do not relaunch"), 1
        if time.monotonic() >= end:
            detail = ("worker launch pending; observe this attempt again"
                      if record["status"] == "reserved" else
                      "same worker continues; observe this attempt again")
            return dict(record, observation=detail), 3
        time.sleep(min(0.1, max(0, end - time.monotonic())))


def reconcile(attempt, evidence_path):
    """Close an inspected failed/unknown attempt as BLOCKED, never as success."""
    with dashboard._Lock(attempt.parent / ".execution.lock", 5), \
            dashboard._Lock(attempt / ".worker.lock", 0):
        if load(attempt.parent / "head.json").get("attempt") != attempt.name:
            raise StepError("reconcile only the latest attempt; this chain has already advanced")
        if (attempt / "reconciliation.json").exists():
            raise StepError("attempt already reconciled")
        result_path = attempt / "result.json"
        record = load(result_path if result_path.exists() else attempt / "state.json")
        if record["status"] == "done":
            raise StepError("successful attempts do not need reconciliation")
        pid = record.get("job_pid")
        if pid and record.get("observed_exit_code") is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise StepError("local job may still exist; inspect it before reconciliation")
        root = Path(record["workspace"])
        evidence = load(evidence_path)
        if set(evidence) != {"no_active_job", "detail", "evidence"} or evidence["no_active_job"] is not True:
            raise StepError("reconciliation must establish no active local or remote job")
        if not isinstance(evidence["detail"], str) or not evidence["detail"].strip():
            raise StepError("reconciliation needs the observed outcome and recovery decision")
        if not isinstance(evidence["evidence"], list) or not evidence["evidence"]:
            raise StepError("reconciliation needs bound observation files")
        for ref in evidence["evidence"]:
            check_ref(root, ref)
        spec = load(attempt / "spec.json")
        # Seal even an unstarted reservation before publication. A delayed worker
        # must not launch it if publication fails or the reconciler disappears.
        record.update(status="blocked", updated_at=now(), reconciliation=evidence,
                      job_pid=None, checkpoint_id=None)
        write(attempt / "state.json", record)
        record["checkpoint_id"] = publish(root, attempt, spec, "blocked", evidence["detail"])
        write(attempt / "reconciliation.json", record)
        return record


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("--workspace", required=True)
    start.add_argument("--state-root", default=".execution")
    start.add_argument("--attempt", required=True)
    start.add_argument("--spec", required=True)
    view = sub.add_parser("status")
    view.add_argument("--attempt-dir", required=True)
    view.add_argument("--wait-seconds", type=float, default=0)
    recovery = sub.add_parser("reconcile")
    recovery.add_argument("--attempt-dir", required=True)
    recovery.add_argument("--evidence-file", required=True)
    internal = sub.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("--attempt-dir", required=True)
    args = ap.parse_args(argv)
    try:
        if args.action == "reconcile":
            print(json.dumps(reconcile(Path(args.attempt_dir).resolve(), args.evidence_file),
                             ensure_ascii=False, indent=2))
            return 0
        if args.action == "status":
            if not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 60:
                raise StepError("wait-seconds must be between 0 and 60")
            record, code = status(Path(args.attempt_dir).resolve(), args.wait_seconds)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return code
        if args.action == "_worker":
            worker(Path(args.attempt_dir).resolve())
            return 0
        root = Path(args.workspace).resolve(strict=True)
        state_root = local(root, args.state_root)
        attempt = reserve(root, state_root, args.attempt, load(args.spec))
        with open(attempt / "worker.log", "xb") as log:
            os.chmod(log.name, 0o600)
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker",
                              "--attempt-dir", str(attempt)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                             env=dict(os.environ, MIGRATION_ATTEMPT_DIR=str(attempt)))
        print(json.dumps({"attempt_dir": str(attempt), "next": "status (do not start again)"}))
        return 0
    except (StepError, dashboard.UpdateError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"execution_step: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
