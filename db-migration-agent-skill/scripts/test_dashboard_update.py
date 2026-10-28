#!/usr/bin/env python3
"""Offline tests for shared/scripts/dashboard_update.py — temp directories only.

Run: python3 -B scripts/test_dashboard_update.py [ENGAGEMENTS_DIR ...]
Defaults to fixtures/engagements, a public synthetic test snapshot (not migration history).
Optional ENGAGEMENTS_DIR args override that fixture collection: every
*/dashboard/status.json found is COPIED to a temp dir and patched there — the originals
are never written.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared" / "scripts"))
import dashboard_update as du  # noqa: E402

EXTRA_DIRS = [Path(__file__).resolve().parent / "fixtures" / "engagements"]
if __name__ == "__main__":
    EXTRA_DIRS = [Path(a) for a in sys.argv[1:]] or EXTRA_DIRS
    del sys.argv[1:]


def seed_status():
    return {
        "engagement": "demo", "updated_at": "2026-10-05T00:00:00+00:00", "mode": "3", "lang": "ko",
        "overall_progress_pct": 0, "current_phase": "0", "current_activity": "",
        "phases": [{"id": i, "name": f"P{i}", "status": "pending", "done": 0, "total": 1} for i in du.PHASE_IDS],
        "cutover_gates": [{"key": k, "label": k, "met": False, "detail": ""} for k in sorted(du.GATE_KEYS)],
        "cutover_ready": False,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dash = self.tmp / "dashboard"
        self.dash.mkdir()
        (self.dash / "status.json").write_text(json.dumps(seed_status()))
        (self.dash / "activity-log.jsonl").write_text("")
        self.plan = self.tmp / "migration-plan.md"
        self.plan.write_text("# Plan\n\n## Phase 6 — Execute\n\n- existing line\n\n## Phase 7 — Validate\n\ntext\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_du(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = du.main(["--dashboard", str(self.dash), "--plan", str(self.plan), *argv])
        return rc, out.getvalue(), err.getvalue()

    def status(self):
        return json.loads((self.dash / "status.json").read_text())


class MergeTests(Base):
    def test_keyed_phase_update_in_place_never_duplicates(self):
        rc, out, err = self.run_du("--patch", json.dumps({
            "current_phase": "6",
            "phases": [{"id": "6", "status": "in_progress",
                        "steps": [{"id": "load-orders", "label": "orders", "status": "in_progress"}]}]}))
        self.assertEqual(rc, 0, err)
        st = self.status()
        self.assertEqual(len(st["phases"]), 11)
        p6 = next(p for p in st["phases"] if p["id"] == "6")
        self.assertEqual((p6["status"], p6["name"]), ("in_progress", "P6"))   # other fields kept
        # second checkpoint: step merged by id, label preserved
        rc, _, err = self.run_du("--patch", json.dumps({"phases": [{"id": "6", "steps": [
            {"id": "load-orders", "status": "done", "detail": "29,900,000 rows"}]}]}))
        self.assertEqual(rc, 0, err)
        step = next(p for p in self.status()["phases"] if p["id"] == "6")["steps"][0]
        self.assertEqual(step, {"id": "load-orders", "label": "orders", "status": "done", "detail": "29,900,000 rows"})

    def test_null_deletes_and_delete_marker_removes_item(self):
        self.run_du("--patch", json.dumps({"customer_actions": [{"id": "A2", "title": "t", "status": "pending"},
                                                                {"id": "A3", "title": "u", "status": "pending"}]}))
        rc, _, err = self.run_du("--patch", json.dumps({"current_activity": None,
                                                        "customer_actions": [{"id": "A2", "_delete": True}]}))
        self.assertEqual(rc, 0, err)
        st = self.status()
        self.assertNotIn("current_activity", st)
        self.assertEqual([a["id"] for a in st["customer_actions"]], ["A3"])

    def test_gate_merge_by_key(self):
        rc, _, err = self.run_du("--patch", json.dumps({"cutover_gates": [{"key": "rehearsal", "detail": "waiver"}]}))
        self.assertEqual(rc, 0, err)
        gates = self.status()["cutover_gates"]
        self.assertEqual(len(gates), 6)
        self.assertEqual(next(g for g in gates if g["key"] == "rehearsal")["detail"], "waiver")

    def test_updated_at_bumped(self):
        self.run_du("--patch", "{}")
        self.assertNotEqual(self.status()["updated_at"], "2026-10-05T00:00:00+00:00")


class ProgressTests(unittest.TestCase):
    def test_equal_phase_weighting_is_deterministic(self):
        phases = [{"id": i, "status": "pending", "done": 0, "total": 18 if i == "1" else 1} for i in du.PHASE_IDS]
        for p in phases[:3]:
            p.update(status="done", done=p["total"])
        phases[3].update(status="in_progress", done=0)
        # phase 3/11 in progress -> 27%, not the 62% a raw sum(done)/sum(total) produced live
        self.assertEqual(du.progress_pct(phases), 27)
        phases[3].update(status="in_progress", done=1, total=2)
        self.assertEqual(du.progress_pct(phases), 32)

    def test_cli_recomputes_unless_kept(self):
        t = Base("setUp")
        t.setUp()
        try:
            t.run_du("--patch", json.dumps({"overall_progress_pct": 91, "phases": [{"id": "0", "status": "done"}]}))
            self.assertEqual(t.status()["overall_progress_pct"], 9)
            t.run_du("--keep-progress", "--patch", json.dumps({"overall_progress_pct": 50}))
            self.assertEqual(t.status()["overall_progress_pct"], 50)
        finally:
            t.tearDown()


class ValidationTests(Base):
    def test_corrupt_file_is_refused_untouched(self):
        bad = '{"phases": [ {"id": "0"} "cutover_gates": []}'
        (self.dash / "status.json").write_text(bad)
        rc, _, err = self.run_du("--patch", "{}", "--log-title", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", err)
        self.assertEqual((self.dash / "status.json").read_text(), bad)
        self.assertEqual((self.dash / "activity-log.jsonl").read_text(), "")

    def test_invalid_result_writes_nothing(self):
        before = (self.dash / "status.json").read_text()
        for patch in ({"phases": [{"id": "12", "status": "pending"}]},          # 12th phase
                      {"phases": [{"id": "6", "status": "blocked"}]},           # phase vocabulary
                      {"phases": [{"id": "6", "steps": [{"id": "a", "status": "ok"}]}]},
                      {"cutover_ready": True}):                                 # gates unmet
            rc, _, err = self.run_du("--patch", json.dumps(patch), "--log-title", "x",
                                     "--plan-section", "Phase 6", "--plan-line", "y")
            self.assertEqual(rc, 1, patch)
            self.assertEqual((self.dash / "status.json").read_text(), before)
        self.assertEqual((self.dash / "activity-log.jsonl").read_text(), "")
        self.assertNotIn("y\n", self.plan.read_text())

    def test_bad_log_result_rejected(self):
        rc, _, err = self.run_du("--log-json", json.dumps({"title": "x", "result": "done"}))
        self.assertEqual(rc, 1)
        self.assertIn("step status", err)

    def test_missing_plan_section_rejected_before_writing(self):
        before = (self.dash / "status.json").read_text()
        rc, _, err = self.run_du("--patch", "{}", "--plan-section", "Phase 42", "--plan-line", "z")
        self.assertEqual(rc, 1)
        self.assertEqual((self.dash / "status.json").read_text(), before)

    def test_repair_duplicated_phases_with_replace(self):
        st = seed_status()
        st["phases"].append(dict(st["phases"][5]))  # the live duplicate-phase bug
        (self.dash / "status.json").write_text(json.dumps(st))
        rc, _, err = self.run_du("--patch", json.dumps({"phases": [{"id": "6", "status": "done"}]}))
        self.assertEqual(rc, 1)          # merging cannot fix 12 entries
        full = seed_status()["phases"]
        rc, _, err = self.run_du("--replace", "phases", "--patch", json.dumps({"phases": full}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.status()["phases"]), 11)


class LogAndPlanTests(Base):
    def test_one_command_writes_all_three(self):
        rc, out, err = self.run_du(
            "--patch", json.dumps({"current_phase": "6", "current_activity": "loading"}),
            "--log-title", "load-orders check", "--log-action", "probe", "--log-result", "in_progress",
            "--log-detail", "4m", "--plan-section", "Phase 6", "--plan-line", "load-orders running")
        self.assertEqual(rc, 0, err)
        self.assertIn("log+1 plan+1", out)
        entries = [json.loads(l) for l in (self.dash / "activity-log.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["phase"], entries[0]["result"], entries[0]["time"]),
                         ("6", "in_progress", self.status()["updated_at"]))
        plan = self.plan.read_text()
        sec6 = plan.split("## Phase 6")[1].split("## Phase 7")[0]
        self.assertIn("load-orders running", sec6)
        self.assertIn("- existing line", sec6)
        self.assertLess(sec6.index("existing line"), sec6.index("load-orders running"))

    def test_log_without_trailing_newline_is_not_glued(self):
        (self.dash / "activity-log.jsonl").write_text('{"time":"t","phase":"0","title":"a","result":"success"}')
        self.run_du("--log-title", "b", "--log-result", "success")
        lines = (self.dash / "activity-log.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["title"] for l in lines], ["a", "b"])

    def test_no_temp_files_left(self):
        self.run_du("--patch", "{}")
        self.assertEqual(sorted(p.name for p in self.dash.iterdir()), [".status.lock", "activity-log.jsonl", "status.json"])


class ReadinessTests(Base):
    def test_ready_needs_all_six_gates_present_and_met(self):
        st = seed_status()
        st["cutover_gates"] = [g for g in st["cutover_gates"] if g["key"] != "soak"]   # legacy: 5 gates
        for g in st["cutover_gates"]:
            g["met"] = True
        (self.dash / "status.json").write_text(json.dumps(st))
        rc, _, err = self.run_du("--patch", json.dumps({"cutover_ready": True}))
        self.assertEqual(rc, 1)
        self.assertIn("six cutover gates", err)
        rc, _, err = self.run_du("--patch", json.dumps({"current_activity": "x"}))   # legacy + false stays valid
        self.assertEqual(rc, 0, err)
        rc, _, err = self.run_du("--patch", json.dumps({"cutover_gates": [{"key": "soak", "label": "s", "met": True}],
                                                        "cutover_ready": True}))
        self.assertEqual(rc, 0, err)

    def test_ready_with_no_gates_is_rejected(self):
        st = seed_status()
        del st["cutover_gates"]
        (self.dash / "status.json").write_text(json.dumps(st))
        rc, _, _ = self.run_du("--patch", json.dumps({"cutover_ready": True}))
        self.assertEqual(rc, 1)


@unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
class IoFailureTests(Base):
    ARGS = ("--patch", json.dumps({"current_activity": "new"}), "--log-title", "t",
            "--plan-section", "Phase 6", "--plan-line", "NEWLINE")

    def snapshot(self):
        return ((self.dash / "status.json").read_text(), (self.dash / "activity-log.jsonl").read_text(),
                self.plan.read_text())

    def assert_unchanged(self, before):
        self.assertEqual(self.snapshot(), before)

    def test_permission_denied_on_each_target(self):
        for target in (self.dash / "activity-log.jsonl", self.plan, self.dash / "status.json"):
            before = self.snapshot()
            os.chmod(target, 0o444)
            try:
                rc, _, err = self.run_du(*self.ARGS)
            finally:
                os.chmod(target, 0o644)
            self.assertEqual(rc, 1, target)
            self.assertIn("not writable", err)
            self.assert_unchanged(before)

    def test_read_only_dashboard_dir(self):
        before = self.snapshot()
        (self.dash / ".status.lock").touch()
        os.chmod(self.dash, 0o555)
        try:
            rc, _, err = self.run_du(*self.ARGS)
        finally:
            os.chmod(self.dash, 0o755)
        self.assertEqual(rc, 1)
        self.assert_unchanged(before)

    def test_failure_at_status_replace_rolls_back_log_and_plan(self):
        before = self.snapshot()
        real = du._atomic_write

        def failing(path, text):
            if Path(path).name == "status.json" and "new" in text:
                raise PermissionError(13, "Permission denied (injected)")
            return real(path, text)
        with mock.patch.object(du, "_atomic_write", side_effect=failing):
            rc, _, err = self.run_du(*self.ARGS)
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)
        self.assert_unchanged(before)

    def test_readback_mismatch_rolls_back_everything(self):
        before = self.snapshot()
        real = du._load_json
        calls = {"n": 0}

        def lying(path):
            calls["n"] += 1
            v = real(path)
            return dict(v, current_activity="tampered") if calls["n"] > 1 else v
        with mock.patch.object(du, "_load_json", side_effect=lying):
            rc, _, err = self.run_du(*self.ARGS)
        self.assertEqual(rc, 1)
        self.assertIn("read-back", err)
        self.assert_unchanged(before)


class ConcurrencyTests(Base):
    def test_two_processes_lose_no_updates(self):
        script = str(Path(du.__file__))
        code = ("import json,subprocess,sys\n"
                "for i in range(25):\n"
                "  p={'phases':[{'id':'6','steps':[{'id':f'{sys.argv[1]}-{i}','label':'x','status':'done'}]}]}\n"
                "  r=subprocess.run([sys.executable,'-B',sys.argv[2],'--dashboard',sys.argv[3],'--lock-timeout','60',"
                "'--patch',json.dumps(p),'--log-title',f'{sys.argv[1]}-{i}','--log-result','success'],capture_output=True)\n"
                "  assert r.returncode==0, r.stderr\n")
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, w, script, str(self.dash)]) for w in ("a", "b")]
        self.assertEqual([p.wait(timeout=120) for p in procs], [0, 0])
        steps = next(p for p in self.status()["phases"] if p["id"] == "6")["steps"]
        self.assertEqual(len(steps), 50)
        lines = (self.dash / "activity-log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 50)
        self.assertEqual(len({json.loads(l)["title"] for l in lines}), 50)



class SoakAndCutoverBlockTests(Base):
    def test_seed_soak_fills_only_missing_fields(self):
        rc, _, err = self.run_du("--seed-soak", "3")
        self.assertEqual(rc, 0, err)
        soak = self.status()["soak"]
        self.assertEqual((soak["days"], soak["n_total"], soak["consecutive_green"], soak["state"]), ([], 3, 0, "active"))
        started = soak["started_at"]
        self.run_du("--patch", json.dumps({"soak": {"days": [{"date": "2026-10-06", "overall": "green"}]}}))
        self.run_du("--seed-soak", "7")
        soak = self.status()["soak"]
        self.assertEqual((soak["n_total"], soak["started_at"], len(soak["days"])), (3, started, 1))

    def test_days_merge_by_date(self):
        self.run_du("--seed-soak", "3")
        self.run_du("--patch", json.dumps({"soak": {"days": [{"date": "2026-10-06", "overall": "red"}]}}))
        self.run_du("--patch", json.dumps({"soak": {"days": [{"date": "2026-10-06", "period_evidence": True},
                                                             {"date": "2026-10-07", "overall": "green"}]}}))
        days = self.status()["soak"]["days"]
        self.assertEqual(days, [{"date": "2026-10-06", "overall": "red", "period_evidence": True},
                                {"date": "2026-10-07", "overall": "green"}])

    def _day(self, date, green, **checks):
        return {"date": date, "overall": "green" if green else "red", "needs_agent_review": not green,
                "checks": {"key_count": True, "period_evidence": True if green else None, **checks}}

    def _write_day(self, day):
        f = self.tmp / "day.json"
        f.write_text(json.dumps(day))
        return self.run_du("--soak-day-file", str(f))

    def test_soak_day_replacement_preserves_nulls_and_recomputes_streak(self):
        self.assertEqual(self._write_day(self._day("2026-10-06", True))[0], 1, "n_total must be seeded first")
        self.run_du("--seed-soak", "2")
        rc, _, err = self._write_day(self._day("2026-10-06", False))
        self.assertEqual(rc, 0, err)
        soak = self.status()["soak"]
        self.assertIsNone(soak["days"][0]["checks"]["period_evidence"], "literal null must persist")
        self.assertEqual((soak["consecutive_green"], soak["state"]), (0, "active"))
        self._write_day(self._day("2026-10-06", False))          # re-run of the same day
        soak = self.status()["soak"]
        self.assertEqual(len(soak["days"]), 1)
        self.assertIn("period_evidence", soak["days"][0]["checks"])
        self._write_day(self._day("2026-10-06", True))
        self._write_day(self._day("2026-10-07", True))
        soak = self.status()["soak"]
        self.assertEqual((soak["consecutive_green"], soak["state"]), (2, "complete"))
        self.assertTrue(soak["last_checked_at"])

    def test_delayed_historical_day_cannot_complete_over_a_red_latest_day(self):
        self.run_du("--seed-soak", "2")
        self._write_day(self._day("2026-10-08", False))        # latest day is red
        self._write_day(self._day("2026-10-06", True))          # delayed older samples arrive later
        self._write_day(self._day("2026-10-07", True))
        soak = self.status()["soak"]
        self.assertEqual((soak["consecutive_green"], soak["state"]), (0, "active"))
        self.assertEqual([d["date"] for d in soak["days"]], ["2026-10-06", "2026-10-07", "2026-10-08"])

    def test_soak_day_shape_is_validated(self):
        self.run_du("--seed-soak", "2")
        for bad in ({"date": "2026-02-30", "checks": {}, "overall": "red", "needs_agent_review": True},
                    {"date": "2026-10-06", "overall": "red", "needs_agent_review": True},
                    {"date": "2026-10-06", "checks": {}, "overall": "red", "needs_agent_review": "yes"}):
            self.assertEqual(self._write_day(bad)[0], 1, bad)
        self.assertEqual(self.status()["soak"]["days"], [])

    def test_malformed_soak_rejected(self):
        for patch in ({"soak": []}, {"soak": {"days": {}}}, {"soak": {"days": [{"overall": "green"}]}},
                      {"soak": {"n_total": 0}}, {"cutover": "done"}):
            rc, _, _ = self.run_du("--patch", json.dumps(patch))
            self.assertEqual(rc, 1, patch)

    def test_cutover_block(self):
        rc, _, err = self.run_du("--patch", json.dumps({"cutover": {
            "completed_at": "2026-10-06T14:03:01Z", "measured_write_pause_seconds": 45.3,
            "target_endpoint": "db.example", "rollback_window_ends": "2026-10-13T14:03:00Z",
            "rollback_path_state": "reverse DMS running, applying"}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status()["cutover"]["measured_write_pause_seconds"], 45.3)



class AppendFaultTests(Base):
    ARGS = ("--patch", json.dumps({"current_activity": "new"}), "--log-title", "t",
            "--plan-section", "Phase 6", "--plan-line", "NEWLINE")

    def snapshot(self):
        return ((self.dash / "status.json").read_text(), (self.dash / "activity-log.jsonl").read_text(),
                self.plan.read_text())

    def test_fsync_failure_after_bytes_written_is_rolled_back(self):
        (self.dash / "activity-log.jsonl").write_text('{"title":"old","result":"success"}\n')
        before = self.snapshot()
        real = os.fsync
        calls = {"n": 0}

        def failing(fd):
            calls["n"] += 1
            if calls["n"] == 1:            # the log append's fsync — bytes are already written
                raise OSError(5, "I/O error (injected at fsync)")
            return real(fd)
        with mock.patch.object(du.os, "fsync", side_effect=failing):
            rc, _, err = self.run_du(*self.ARGS)
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)
        self.assertEqual(self.snapshot(), before)

    def test_write_failure_on_new_log_removes_partial_file(self):
        (self.dash / "activity-log.jsonl").unlink()
        before_status, before_plan = (self.dash / "status.json").read_text(), self.plan.read_text()
        real_open = open

        class Boom:
            def __init__(self, f):
                self.f = f
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.f.close()
            def write(self, data):
                self.f.write(data[:5])     # partial bytes reach the file
                raise OSError(28, "No space left on device (injected)")

        def fake_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            return Boom(f) if str(path).endswith("activity-log.jsonl") and mode == "a" else f
        with mock.patch("builtins.open", side_effect=fake_open):
            rc, _, err = self.run_du(*self.ARGS)
        self.assertEqual(rc, 1)
        self.assertFalse((self.dash / "activity-log.jsonl").exists())
        self.assertEqual(((self.dash / "status.json").read_text(), self.plan.read_text()), (before_status, before_plan))

    def test_failure_inside_plan_replace_after_rename_is_rolled_back(self):
        before = self.snapshot()
        real = du.os.replace

        def replace_then_fail(src, dst):
            real(src, dst)
            if str(dst).endswith("migration-plan.md"):
                raise OSError(5, "I/O error after rename (injected)")
        with mock.patch.object(du.os, "replace", side_effect=replace_then_fail):
            rc, _, err = self.run_du(*self.ARGS)
        self.assertEqual(rc, 1)
        self.assertEqual(self.snapshot(), before)


class FieldValidationTests(Base):
    def test_bad_soak_dates_rejected(self):
        for d in ("2026-10-99", "2026-02-30", "20261006", 20261006, None):
            rc, _, _ = self.run_du("--patch", json.dumps({"soak": {"days": [{"date": d}]}}))
            self.assertEqual(rc, 1, d)
        rc, _, err = self.run_du("--patch", json.dumps({"soak": {"days": [{"date": "2026-10-06"}]}}))
        self.assertEqual(rc, 0, err)

    def test_bad_cutover_fields_rejected(self):
        bad = [{"completed_at": "pending"}, {"completed_at": True}, {"completed_at": "2026-10-06T14:03:01+09:00"},
               {"rollback_window_ends": "next week"}, {"measured_write_pause_seconds": -1},
               {"measured_write_pause_seconds": "45s"}, {"measured_write_pause_seconds": True},
               {"target_endpoint": 5}, {"rollback_path_state": ""}]
        for c in bad:
            rc, _, _ = self.run_du("--patch", json.dumps({"cutover": c}))
            self.assertEqual(rc, 1, c)
        rc, _, err = self.run_du("--patch", '{"cutover": {"measured_write_pause_seconds": NaN}}')
        self.assertEqual(rc, 1)
        self.assertIn("finite", err)
        rc, _, err = self.run_du("--patch", json.dumps({"cutover": {"completed_at": "2026-10-06T14:03:01Z",
                                                                    "measured_write_pause_seconds": 0}}))
        self.assertEqual(rc, 0, err)


class FailureHandlingTests(Base):
    ARGS = ("--patch", json.dumps({"current_activity": "new"}), "--log-title", "demo",
            "--plan-section", "Phase 6", "--plan-line", "NEWLINE")

    def paths(self):
        return self.dash / "status.json", self.dash / "activity-log.jsonl", self.plan

    def snapshot(self):
        return tuple(p.read_bytes() if p.exists() else None for p in self.paths())

    def assert_failed_unchanged(self, result, before):
        rc, out, err = result
        self.assertEqual(rc, 1, err)
        self.assertEqual(out, "")
        self.assertIn("FAILED", err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.dash.iterdir()),
                         [".status.lock", "activity-log.jsonl", "status.json"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["dashboard", "migration-plan.md"])

    def test_persistent_read_failure_does_not_prevent_rollback(self):
        real_write, real_read, real_bytes = du._atomic_write, Path.read_text, Path.read_bytes
        for target in (self.paths()[0], self.plan):
            with self.subTest(target=target.name):
                before = self.snapshot()
                committed = False

                def write(path, text):
                    nonlocal committed
                    real_write(path, text)
                    if Path(path) == self.paths()[0]:
                        committed = True

                def read(path, *args, **kwargs):
                    if committed and path == target:
                        raise OSError(5, "synthetic persistent read failure")
                    return real_read(path, *args, **kwargs)

                def read_bytes(path):
                    if committed and path == target:
                        raise OSError(5, "synthetic persistent read failure")
                    return real_bytes(path)

                with mock.patch.object(du, "_atomic_write", side_effect=write), \
                        mock.patch.object(Path, "read_text", read), \
                        mock.patch.object(Path, "read_bytes", read_bytes):
                    result = self.run_du(*self.ARGS)
                self.assert_failed_unchanged(result, before)
                self.assertIn("rolled back", result[2])
                self.assertNotIn("ROLLBACK INCOMPLETE", result[2])

    def test_empty_log_readback_rolls_back(self):
        before = self.snapshot()
        real = Path.read_text

        def read(path, *args, **kwargs):
            return "" if path == self.paths()[1] else real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read):
            result = self.run_du(*self.ARGS)
        self.assert_failed_unchanged(result, before)
        self.assertIn("read-back", result[2])

    def test_invalid_utf8_in_existing_log_rolls_back(self):
        self.paths()[1].write_bytes(b"\xff\n")
        before = self.snapshot()
        self.assert_failed_unchanged(self.run_du(*self.ARGS), before)

    def test_rollback_restores_original_bytes_including_crlf(self):
        status, log, plan = self.paths()
        status.write_bytes((json.dumps(seed_status(), indent=2) + "\n").replace("\n", "\r\n").encode("utf-8"))
        plan.write_bytes(plan.read_bytes().replace(b"\n", b"\r\n"))
        log.write_bytes('{"title":"데모"}'.encode("utf-8"))  # byte size, no final newline
        before = self.snapshot()
        real = du._load_json
        calls = 0

        def load(path):
            nonlocal calls
            calls += 1
            value = real(path)
            return dict(value, current_activity="synthetic mismatch") if calls > 1 else value

        with mock.patch.object(du, "_load_json", side_effect=load):
            result = self.run_du(*self.ARGS)
        self.assert_failed_unchanged(result, before)

    def test_failed_restore_still_attempts_other_files(self):
        real_write, real_load = du._atomic_write, du._load_json
        for target_index in (0, 2):
            with self.subTest(target=self.paths()[target_index].name):
                before = self.snapshot()
                calls = 0

                def write(path, text):
                    if Path(path) == self.paths()[target_index] and text.encode("utf-8") == before[target_index]:
                        raise OSError(5, "synthetic restore failure")
                    return real_write(path, text)

                def load(path):
                    nonlocal calls
                    calls += 1
                    value = real_load(path)
                    return dict(value, current_activity="synthetic mismatch") if calls > 1 else value

                with mock.patch.object(du, "_atomic_write", side_effect=write), \
                        mock.patch.object(du, "_load_json", side_effect=load):
                    rc, _, err = self.run_du(*self.ARGS)
                self.assertEqual(rc, 1)
                self.assertIn("ROLLBACK INCOMPLETE", err)
                self.assertIn(self.paths()[target_index].name, err)
                self.assertNotIn("write failed, rolled back", err)
                after = self.snapshot()
                for n in range(3):
                    if n == target_index:
                        self.assertNotEqual(after[n], before[n])
                    else:
                        self.assertEqual(after[n], before[n])
                for path, data in zip(self.paths(), before):
                    path.write_bytes(data)

    def test_fsync_failure_at_each_write_rolls_back(self):
        real = du.os.fsync
        for fail_at in (1, 2, 3):
            with self.subTest(fsync=fail_at):
                before = self.snapshot()
                calls = 0

                def fsync(fd):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(5, "synthetic fsync failure")
                    return real(fd)

                with mock.patch.object(du.os, "fsync", side_effect=fsync):
                    result = self.run_du(*self.ARGS)
                self.assert_failed_unchanged(result, before)

    def test_replace_failure_before_and_after_each_rename_rolls_back(self):
        real = du.os.replace
        for target in (self.plan, self.paths()[0]):
            for after_rename in (False, True):
                with self.subTest(target=target.name, after_rename=after_rename):
                    before = self.snapshot()
                    failed = False

                    def replace(src, dst):
                        nonlocal failed
                        if Path(dst) == target and not failed:
                            failed = True
                            if after_rename:
                                real(src, dst)
                            raise OSError(5, "synthetic rename failure")
                        return real(src, dst)

                    with mock.patch.object(du.os, "replace", side_effect=replace):
                        result = self.run_du(*self.ARGS)
                    self.assert_failed_unchanged(result, before)

    def test_precommit_read_errors_return_failure_without_writes(self):
        real_text, real_bytes = Path.read_text, Path.read_bytes
        for target in (self.paths()[0], self.plan):
            with self.subTest(target=target.name):
                before = self.snapshot()

                def read_text(path, *args, **kwargs):
                    if path == target:
                        raise OSError(5, "synthetic precommit read failure")
                    return real_text(path, *args, **kwargs)

                def read_bytes(path):
                    if path == target:
                        raise OSError(5, "synthetic precommit read failure")
                    return real_bytes(path)

                with mock.patch.object(Path, "read_text", read_text), \
                        mock.patch.object(Path, "read_bytes", read_bytes):
                    result = self.run_du(*self.ARGS)
                self.assert_failed_unchanged(result, before)

    def test_malformed_payloads_return_failure_without_writes(self):
        bad_patches = [
            {"phases": 3}, {"phases": [None]}, {"phases": [{"id": []}]},
            {"phases": [{"id": "6", "status": []}]},
            {"phases": [{"id": "6", "steps": [None]}]},
            {"phases": [{"id": "6", "steps": [{"id": [], "status": "pending"}]}]},
            {"phases": [{"id": "6", "status": "in_progress", "done": "invalid"}]},
            {"cutover_gates": [None]}, {"cutover_gates": [{"key": {}}]},
        ]
        for patch in bad_patches:
            with self.subTest(patch=patch):
                before = self.snapshot()
                self.assert_failed_unchanged(
                    self.run_du(*self.ARGS, "--patch", json.dumps(patch)), before)

    def test_malformed_existing_structure_can_be_repaired_with_replace(self):
        current = seed_status()
        current["phases"] = 3
        self.paths()[0].write_text(json.dumps(current))
        rc, _, err = self.run_du("--replace", "phases", "--patch",
                                 json.dumps({"phases": seed_status()["phases"]}))
        self.assertEqual(rc, 0, err)
        self.assertIn("warning", err)
        self.assertEqual(self.status()["phases"], seed_status()["phases"])

    def test_malformed_log_json_returns_failure_without_writes(self):
        for raw in ("{", "[]", '{"title":"demo","result":[]}',
                    '{"title":"demo","detail":NaN}', '{"title":"demo","detail":Infinity}'):
            with self.subTest(log_json=raw):
                before = self.snapshot()
                self.assert_failed_unchanged(self.run_du(*self.ARGS, "--log-json", raw), before)

    def test_unencodable_payloads_leave_no_partial_checkpoint(self):
        for args in (("--patch", '{"current_activity":"\\ud800"}'),
                     ("--log-json", '{"title":"demo","detail":"\\ud800"}'),
                     ("--plan-line", "\ud800")):
            with self.subTest(field=args[0]):
                before = self.snapshot()
                self.assert_failed_unchanged(self.run_du(*self.ARGS, *args), before)

    def test_unicode_line_separators_in_log_are_valid(self):
        title = "demo\u2028line\u2029next\u0085line"
        rc, _, err = self.run_du(*self.ARGS, "--log-title", title)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(self.paths()[1].read_text())["title"], title)


class RealDashboardCopyTests(unittest.TestCase):
    """Patch temporary COPIES of synthetic or explicitly supplied engagement dashboards."""

    def test_real_copies(self):
        files = []
        for directory in EXTRA_DIRS:
            found = sorted(directory.glob("*/dashboard/status.json"))
            self.assertTrue(found, f"No engagement fixtures found in {directory} (expected */dashboard/status.json)")
            files.extend(found)
        for f in files:
            with self.subTest(fixture=str(f)), tempfile.TemporaryDirectory() as tmp:
                original = f.read_bytes()
                original_status = json.loads(original)
                source_log = f.with_name("activity-log.jsonl")
                original_log = source_log.read_bytes() if source_log.exists() else None
                source_files = sorted(p.name for p in f.parent.iterdir())
                dash = Path(tmp) / "dashboard"
                dash.mkdir()
                shutil.copy(f, dash / "status.json")
                (dash / "activity-log.jsonl").write_bytes(original_log or b"")
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = du.main(["--dashboard", str(dash), "--patch",
                                  json.dumps({"current_activity": "copy test"}),
                                  "--log-title", "Synthetic copy test (temporary dashboard only)",
                                  "--log-result", "in_progress"])
                self.assertEqual(rc, 0, f"{f}: {err.getvalue()}")
                st = json.loads((dash / "status.json").read_text())
                self.assertEqual(len(st["phases"]), 11, f)
                self.assertEqual(st["current_activity"], "copy test")
                self.assertCountEqual([p["id"] for p in st["phases"]], du.PHASE_IDS)
                self.assertCountEqual([g["key"] for g in st["cutover_gates"]], du.GATE_KEYS)
                # Only the requested activity and the updater's timestamp/progress may change.
                changed_fields = {"current_activity", "updated_at", "overall_progress_pct"}
                self.assertEqual({k: v for k, v in st.items() if k not in changed_fields},
                                 {k: v for k, v in original_status.items() if k not in changed_fields})
                copied_log = (dash / "activity-log.jsonl").read_bytes()
                prefix = original_log or b""
                if prefix and not prefix.endswith(b"\n"):
                    prefix += b"\n"
                self.assertTrue(copied_log.startswith(prefix))
                appended = copied_log[len(prefix):].splitlines()
                self.assertEqual(len(appended), 1)
                event = json.loads(appended[0])
                self.assertEqual(event["title"], "Synthetic copy test (temporary dashboard only)")
                self.assertEqual(event["result"], "in_progress")
                self.assertEqual(event["time"], st["updated_at"])
                self.assertEqual(f.read_bytes(), original, "Source fixture was modified")
                self.assertEqual(source_log.read_bytes() if source_log.exists() else None, original_log)
                self.assertEqual(sorted(p.name for p in f.parent.iterdir()), source_files)


if __name__ == "__main__":
    unittest.main(verbosity=1)
