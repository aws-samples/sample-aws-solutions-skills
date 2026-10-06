#!/usr/bin/env python3
"""One command that keeps migration-plan.md, dashboard/status.json and
dashboard/activity-log.jsonl in step (hard constraint 1: "one habit, not two").

WHY: hand-editing a large status.json corrupted it in a live run (a missing `],`), the plan
lagged the dashboard by 40 minutes in another, and agents went silent for 8-15 minutes
because a "quick" dashboard edit felt like a separate chore. This helper makes the
checkpoint one mechanical call that either fully succeeds (all writes persisted and read
back) or exits non-zero having changed nothing it could not validate.

What one invocation does, in this order (all under an exclusive flock on
dashboard/.status.lock, so concurrent writers using this helper serialize):
  1. Reads the CURRENT status.json and refuses to touch it if it is not valid JSON (rebuild
     it from the plan; never "repair" by guessing). Pre-existing schema damage (e.g. a
     duplicated phase) is reported; repair it with `--replace phases` and a full array.
  2. Applies a JSON merge patch (RFC 7396: objects merge recursively, `null` deletes a key,
     scalars/other arrays replace) with ONE extension that prevents the duplicate-phase
     bug: arrays of objects keyed by `id` (phases, steps, customer_actions, risks, gate
     items), `key` (cutover_gates) or `name` (migration_objects items) merge ITEM BY KEY —
     a patch item updates the existing item with that key in place (recursively), unknown
     keys are appended, `{"id": "x", "_delete": true}` removes one. So
     `{"phases": [{"id": "6", "status": "in_progress"}]}` updates phase 6 only.
  3. Sets `updated_at` (UTC now) and recomputes `overall_progress_pct` (definition:
     dashboard.md §Field notes — equal weight per phase) unless --keep-progress.
  4. Validates the result (11 distinct phase ids if phases exist, status vocabularies,
     distinct gate keys, cutover_ready true only with all six gates present and met).
  5. Checks every target file is writable, then commits: appends the activity-log line,
     replaces migration-plan.md (temp file + fsync + rename), and replaces status.json
     LAST the same way.
  6. Reads back the complete payload (status.json equals the merged result; the log ends
     with the exact line; the plan equals the staged text).
GUARANTEE: on any detected failure (validation, permission/I-O error, read-back mismatch)
it rolls back what it wrote — log truncated to its prior size, plan and status.json
restored — and exits 1, so the three files stay in step. Each file is individually
crash-safe (status.json/plan are never half-written), but the three files together are not
crash-atomic: if the process is killed between steps, a log and/or plan line can exist
without its status.json update (never the reverse) — re-run the same checkpoint. The lock
only coordinates writers that use this helper.
Every step is local-file only and bounded (no network); total run time is milliseconds.
During the Phase 7.7 soak the LIVE copy is in S3 — do not run this against a stale local
copy and upload it over the Lambda's data (dashboard.md §The update rule); exclude
`.status.lock` when uploading the dashboard folder.

Usage (typical in-progress checkpoint; the patch can also come from --patch-file or stdin "-"):
  python3 dashboard_update.py --dashboard dashboard \
    --patch '{"current_phase":"6","current_activity":"Table 3/4 loading: orders — 4m elapsed",
              "phases":[{"id":"6","status":"in_progress","steps":[{"id":"load-orders",
              "status":"in_progress","detail":"pipe running, checked 17:58Z"}]}]}' \
    --log-title "load-orders check" --log-action "probe loader PID" --log-result in_progress \
    --log-detail "running, 4m elapsed" \
    --plan migration-plan.md --plan-section "Phase 6" --plan-line "17:58Z load-orders running (4m)"

Exit status: 0 = everything written and read back; 1 = validation/IO failure (message on
stderr, nothing written past the failing step); 2 = bad arguments.
"""
import argparse
import datetime
import json
import math
import os
import sys
import tempfile
from pathlib import Path

PHASE_IDS = ["0", "1", "2", "3", "4-5", "6", "7", "7.5", "7.7", "8", "9"]
PHASE_STATUSES = {"done", "in_progress", "pending"}
STEP_STATUSES = {"pending", "in_progress", "done", "blocked"}
GATE_KEYS = {"client_inventory", "validation", "soak", "rehearsal", "runbook", "approvals"}
LOG_RESULTS = {"success", "in_progress", "blocked"}
_ITEM_KEYS = ("id", "key", "name", "date")   # "date": soak.days[] merge by UTC day


class UpdateError(Exception):
    pass


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _item_key(items):
    """The identity field shared by every element of a list of dicts, or None."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for k in _ITEM_KEYS:
        if all(k in i for i in items):
            return k
    return None


def merge_patch(target, patch):
    """RFC 7396 merge patch plus keyed-array merging (see module docstring)."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = _merge_list(out[k], v)
        elif isinstance(v, dict):
            out[k] = merge_patch(out.get(k), v)
        else:
            out[k] = v
    return out


def _merge_list(current, patch_items):
    key = _item_key(patch_items)
    if key is None or (current and _item_key(current) != key):
        return [p for p in patch_items if not (isinstance(p, dict) and p.get("_delete"))]
    result = list(current)
    index = {item[key]: n for n, item in enumerate(result)}
    for p in patch_items:
        ident = p[key]
        if p.get("_delete"):
            if ident in index:
                result[index[ident]] = None
            continue
        body = {k: v for k, v in p.items() if k != "_delete"}
        if ident in index and result[index[ident]] is not None:
            result[index[ident]] = merge_patch(result[index[ident]], body)
        else:
            index[ident] = len(result)
            result.append(merge_patch({}, body))
    return [r for r in result if r is not None]


def progress_pct(phases):
    """Deterministic overall_progress_pct (dashboard.md §Field notes): each of the 11 phases
    weighs 1/11; a `done` phase counts 1, an `in_progress` phase counts done/total (capped
    to [0, 1]; 0 when total is 0/missing), a `pending` phase 0. Rounded to an integer."""
    if not phases:
        return 0
    score = 0.0
    for ph in phases:
        status = ph.get("status")
        if status == "done":
            score += 1.0
        elif status == "in_progress":
            total = ph.get("total") or 0
            done = ph.get("done") or 0
            score += min(1.0, max(0.0, done / total)) if total else 0.0
    return int(round(100 * score / len(phases)))


def _is_calendar_date(v):
    if not isinstance(v, str) or len(v) != 10:
        return False
    try:
        datetime.date.fromisoformat(v)
        return True
    except ValueError:
        return False


def _is_utc_timestamp(v):
    if not isinstance(v, str) or "T" not in v:
        return False
    try:
        ts = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts.utcoffset() == datetime.timedelta(0)


def validate_status(st):
    if not isinstance(st, dict):
        raise UpdateError("status.json must be a JSON object")
    phases = st.get("phases")
    if phases is not None:
        ids = [p.get("id") for p in phases if isinstance(p, dict)]
        if len(phases) != 11 or len(set(ids)) != 11 or set(ids) != set(PHASE_IDS):
            raise UpdateError(f"phases[] must hold exactly the 11 phase ids {PHASE_IDS} once each; got {ids}")
        for p in phases:
            if p.get("status") not in PHASE_STATUSES:
                raise UpdateError(f"phase {p.get('id')}: status {p.get('status')!r} not in {sorted(PHASE_STATUSES)}")
            steps = p.get("steps") or []
            step_ids = [s.get("id") for s in steps]
            if len(step_ids) != len(set(step_ids)):
                raise UpdateError(f"phase {p.get('id')}: duplicate step ids {step_ids}")
            for s in steps:
                if s.get("status") not in STEP_STATUSES:
                    raise UpdateError(f"phase {p.get('id')} step {s.get('id')}: status {s.get('status')!r} "
                                      f"not in {sorted(STEP_STATUSES)}")
    gates = st.get("cutover_gates")
    if gates is not None:
        keys = [g.get("key") for g in gates]
        if len(keys) != len(set(keys)) or not set(keys) <= GATE_KEYS:
            raise UpdateError(f"cutover_gates[] keys must be distinct and from {sorted(GATE_KEYS)}; got {keys}")
        for g in gates:
            if not isinstance(g.get("met"), bool):
                raise UpdateError(f"gate {g.get('key')}: met must be true/false")
    soak = st.get("soak")
    if soak is not None:
        if not isinstance(soak, dict):
            raise UpdateError("soak must be an object (see dashboard.md §Phase 7.7 minimal soak block)")
        days = soak.get("days")
        if days is not None:
            if not isinstance(days, list) or not all(isinstance(d, dict) for d in days):
                raise UpdateError("soak.days must be a list of objects with a 'date' (YYYY-MM-DD, UTC)")
            for d in days:
                if not _is_calendar_date(d.get("date")):
                    raise UpdateError(f"soak.days[].date {d.get('date')!r} is not a real YYYY-MM-DD calendar date")
                if "checks" in d and not isinstance(d["checks"], dict):
                    raise UpdateError(f"soak.days[{d['date']}].checks must be an object")
        if not soak.get("waived") and "n_total" in soak and not (isinstance(soak["n_total"], int) and soak["n_total"] > 0):
            raise UpdateError("soak.n_total must be a positive integer (consecutive green UTC days required)")
    cut = st.get("cutover")
    if cut is not None:
        if not isinstance(cut, dict):
            raise UpdateError("cutover must be an object (dashboard.md §Phase 8–9 cutover block)")
        for field in ("completed_at", "rollback_window_ends"):
            if field in cut and not _is_utc_timestamp(cut[field]):
                raise UpdateError(f"cutover.{field} {cut[field]!r} must be an ISO-8601 UTC timestamp "
                                  "(e.g. 2026-10-06T14:03:01Z)")
        for field in ("target_endpoint", "rollback_path_state"):
            if field in cut and not (isinstance(cut[field], str) and cut[field].strip()):
                raise UpdateError(f"cutover.{field} must be a non-empty string")
        pause = cut.get("measured_write_pause_seconds")
        if "measured_write_pause_seconds" in cut and not (
                isinstance(pause, (int, float)) and not isinstance(pause, bool)
                and math.isfinite(pause) and pause >= 0):
            raise UpdateError("cutover.measured_write_pause_seconds must be a finite, non-negative number "
                              "(a measurement — omit it if not measured)")
    if "cutover_ready" in st and not isinstance(st["cutover_ready"], bool):
        raise UpdateError("cutover_ready must be a boolean (agent-computed AND over gates)")
    if st.get("cutover_ready") is True:
        # Readiness needs ALL six gates present, distinct and met — an incomplete legacy
        # gate list stays valid only with cutover_ready false.
        keys = [g.get("key") for g in (gates or [])]
        if sorted(keys) != sorted(GATE_KEYS) or not all(g.get("met") is True for g in gates):
            raise UpdateError("cutover_ready is true but the six cutover gates are not all present and met")
    return st


def _atomic_write(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UpdateError(f"{path} does not exist — scaffold the dashboard first (dashboard.md)")
    except json.JSONDecodeError as e:
        raise UpdateError(f"{path} is not valid JSON ({e}) — refusing to touch it; rebuild it from "
                          "migration-plan.md (do not hand-patch), then re-run")


def plan_insert(text, section, line):
    """Insert `line` at the end of the Markdown section whose heading contains `section`
    (before the next heading of the same or a higher level)."""
    lines = text.splitlines()
    start = level = None
    for n, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("#") and section.lower() in stripped.lower():
            start, level = n, len(stripped) - len(stripped.lstrip("#"))
            break
    if start is None:
        raise UpdateError(f"migration-plan.md has no heading containing {section!r}")
    end = len(lines)
    for n in range(start + 1, len(lines)):
        stripped = lines[n].lstrip()
        if stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= level:
                end = n
                break
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n"


def build_log_entry(args, status, now):
    if args.log_json:
        entry = json.loads(args.log_json)
        if not isinstance(entry, dict):
            raise UpdateError("--log-json must be a JSON object")
    elif args.log_title:
        entry = {"title": args.log_title}
        for field in ("action", "result", "detail"):
            val = getattr(args, f"log_{field}")
            if val is not None:
                entry[field] = val
        if args.log_files:
            entry["files"] = args.log_files
    else:
        return None
    entry.setdefault("time", now)
    entry.setdefault("phase", status.get("current_phase", ""))
    entry.setdefault("result", "in_progress")
    if entry["result"] not in LOG_RESULTS:
        raise UpdateError(f"log result {entry['result']!r} not in {sorted(LOG_RESULTS)} "
                          "(`done` is a step status, not a log result)")
    if not entry.get("title"):
        raise UpdateError("log entry needs a title")
    return entry


def _writable_file(path):
    """A file we will modify in place (append) or replace: it, or its directory, must be writable."""
    path = Path(path)
    if path.exists():
        if not os.access(path, os.W_OK):
            raise UpdateError(f"{path} is not writable")
    if not os.access(path.parent, os.W_OK):
        raise UpdateError(f"directory {path.parent} is not writable (needed for the atomic replace / create)")


class _Lock:
    """Exclusive flock on dashboard/.status.lock for the whole read-merge-write-readback, so
    two writers using this helper serialize instead of losing each other's update."""

    def __init__(self, path, timeout):
        self.path, self.timeout, self.fd = Path(path), timeout, None

    def __enter__(self):
        import fcntl
        import time
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise UpdateError(f"cannot open lock file {self.path}: {e}")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise UpdateError(f"timed out after {self.timeout}s waiting for {self.path}")
                time.sleep(0.02)

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def _file_size(path):
    path = Path(path)
    return path.stat().st_size if path.exists() else None


def _append_line(path, line, prior):
    """Append one line to a file whose size before this run was `prior` (None = absent).
    The caller records `prior` and marks the log for rollback BEFORE calling this, so a
    failure after some bytes are written (write/flush/fsync) is still truncated away."""
    path = Path(path)
    prefix = ""
    if prior:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            prefix = "" if f.read(1) == b"\n" else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _truncate(path, prior):
    path = Path(path)
    if prior is None:          # the log did not exist before this run
        if path.exists():
            path.unlink()
    else:
        with open(path, "r+b") as f:
            f.truncate(prior)
            f.flush()
            os.fsync(f.fileno())


def run(args):
    dash = Path(args.dashboard)
    status_path, log_path = dash / "status.json", dash / "activity-log.jsonl"
    if args.patch_file:
        raw = sys.stdin.read() if args.patch_file == "-" else Path(args.patch_file).read_text(encoding="utf-8")
    else:
        raw = args.patch or "{}"
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpdateError(f"patch is not valid JSON: {e}")
    if not isinstance(patch, dict):
        raise UpdateError("patch must be a JSON object")
    if (args.plan_section is None) != (args.plan_line is None):
        raise UpdateError("--plan-section and --plan-line go together")
    if not dash.is_dir():
        raise UpdateError(f"{dash} is not a directory — scaffold the dashboard first (dashboard.md)")

    with _Lock(dash / ".status.lock", args.lock_timeout):
        # 1. read + validate the current state (refuse a corrupt file)
        old_status_text = status_path.read_text(encoding="utf-8") if status_path.exists() else None
        current = _load_json(status_path)
        if not isinstance(current, dict):
            raise UpdateError("status.json must hold a JSON object")
        try:
            validate_status(current)
        except UpdateError as e:   # pre-existing schema damage: say so; the patch must repair it
            print(f"dashboard_update: warning — current status.json: {e}", file=sys.stderr)
        now = _now()
        for key in args.replace or []:
            if key not in patch:
                raise UpdateError(f"--replace {key}: the patch has no top-level {key!r}")
        # 2-4. stage everything in memory and validate
        updated = merge_patch({k: v for k, v in current.items() if k not in (args.replace or [])}, patch)
        if args.seed_soak is not None:
            # Minimal soak block the soak writers need (dashboard.md §Phase 7.7); fills only
            # MISSING fields, never overwrites recorded days or the scheduler's results.
            soak = updated.setdefault("soak", {})
            if not isinstance(soak, dict):
                raise UpdateError("--seed-soak: existing soak is not an object; use --replace soak")
            for field, default in (("days", []), ("n_total", args.seed_soak), ("consecutive_green", 0),
                                   ("state", "active"), ("started_at", now)):
                soak.setdefault(field, default)
        updated["updated_at"] = now
        if not args.keep_progress and isinstance(updated.get("phases"), list):
            updated["overall_progress_pct"] = progress_pct(updated["phases"])
        validate_status(updated)
        try:
            status_text = json.dumps(updated, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except ValueError as e:
            raise UpdateError(f"status.json would contain NaN/Infinity, which is not JSON: {e}")
        entry = build_log_entry(args, updated, now)
        log_line = json.dumps(entry, ensure_ascii=False) if entry is not None else None
        plan_path = Path(args.plan)
        old_plan_text = plan_text = plan_line = None
        if args.plan_section is not None:
            if not plan_path.exists():
                raise UpdateError(f"{plan_path} does not exist")
            plan_line = args.plan_line if args.plan_line.lstrip().startswith(("-", "|", "*")) else f"- {now} — {args.plan_line}"
            old_plan_text = plan_path.read_text(encoding="utf-8")
            plan_text = plan_insert(old_plan_text, args.plan_section, plan_line)
        # 5. preflight writability of every target before touching any of them
        _writable_file(status_path)
        if log_line is not None:
            _writable_file(log_path)
        if plan_text is not None:
            _writable_file(plan_path)
        # 6. commit: append-only log, then plan, then status.json LAST; roll back on failure
        # Rollback eligibility is recorded BEFORE each write starts: a write that fails half
        # way (bytes appended, then flush/fsync raises; rename done, then fsync raises) is
        # still undone. Undoing an untouched file is a no-op (same size / same text).
        log_prior, log_done, plan_done, status_done = None, False, False, False
        try:
            if log_line is not None:
                log_prior = _file_size(log_path)
                log_done = True
                _append_line(log_path, log_line, log_prior)
            if plan_text is not None:
                plan_done = True
                _atomic_write(plan_path, plan_text)
            status_done = True
            _atomic_write(status_path, status_text)
            # 7. read back the COMPLETE committed payload
            if _load_json(status_path) != updated:
                raise UpdateError("status.json read-back differs from what was written")
            if log_line is not None and log_path.read_text(encoding="utf-8").splitlines()[-1] != log_line:
                raise UpdateError("activity-log.jsonl read-back does not end with the appended line")
            if plan_text is not None and plan_path.read_text(encoding="utf-8") != plan_text:
                raise UpdateError("migration-plan.md read-back differs from what was written")
        except (OSError, UpdateError) as e:
            problems = []
            for done, undo, what in (
                    (status_done and old_status_text is not None and status_path.read_text(encoding="utf-8") != old_status_text,
                     lambda: _atomic_write(status_path, old_status_text), "status.json"),
                    (plan_done and plan_path.read_text(encoding="utf-8") != old_plan_text,
                     lambda: _atomic_write(plan_path, old_plan_text), "migration-plan.md"),
                    (log_done, lambda: _truncate(log_path, log_prior), "activity-log.jsonl")):
                if done:
                    try:
                        undo()
                    except OSError as re:
                        problems.append(f"{what}: {re}")
            msg = f"write failed, rolled back ({type(e).__name__}: {e})"
            if problems:
                msg += f"; ROLLBACK INCOMPLETE — fix by hand: {'; '.join(problems)}"
            raise UpdateError(msg)
    print(f"dashboard_update: ok updated_at={now} current_phase={updated.get('current_phase')} "
          f"progress={updated.get('overall_progress_pct')}%"
          + (" log+1" if log_line is not None else "") + (" plan+1" if plan_text is not None else ""))
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Merge-patch dashboard/status.json, append one activity-log "
                                             "line and one migration-plan.md line — validated, atomic, read back.")
    ap.add_argument("--dashboard", default="dashboard", help="dashboard directory (default: dashboard)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--patch", help="JSON merge patch (string)")
    g.add_argument("--patch-file", help="file holding the JSON merge patch, or - for stdin")
    ap.add_argument("--replace", action="append", metavar="KEY",
                    help="replace this top-level key wholesale with the patch's value instead of "
                         "merging (e.g. to repair a phases[] array that already has duplicates)")
    ap.add_argument("--seed-soak", type=int, metavar="N_TOTAL",
                    help="add the minimal soak block (days [], n_total, consecutive_green 0, state active, "
                         "started_at now) — only missing fields are filled")
    ap.add_argument("--keep-progress", action="store_true",
                    help="do not recompute overall_progress_pct from phases[]")
    ap.add_argument("--log-json", help="full activity-log entry as JSON (time/phase defaulted)")
    ap.add_argument("--log-title")
    ap.add_argument("--log-action")
    ap.add_argument("--log-result", choices=sorted(LOG_RESULTS))
    ap.add_argument("--log-detail")
    ap.add_argument("--log-files", nargs="*")
    ap.add_argument("--lock-timeout", type=float, default=10.0,
                    help="seconds to wait for dashboard/.status.lock (default 10)")
    ap.add_argument("--plan", default="migration-plan.md")
    ap.add_argument("--plan-section", help="text of the plan heading to append under, e.g. 'Phase 6'")
    ap.add_argument("--plan-line", help="line to append (prefixed '- <UTC time> — ' unless it is a list/table row)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except UpdateError as e:
        print(f"dashboard_update: FAILED — {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
