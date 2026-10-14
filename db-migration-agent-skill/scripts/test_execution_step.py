#!/usr/bin/env python3
"""Offline process/failure tests; temporary files only, no database or AWS access."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared/scripts"))
import execution_step as runner


class OperatorCLITests(unittest.TestCase):
    """Forward tests: real CLI/publisher, observable fixture effects, no runner mocks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cli = [sys.executable, runner.__file__]
        self.dash = self.root / "dashboard"
        self.dash.mkdir()
        self.save(self.dash / "status.json", {"mode": "3", "phases": [
            {"id": p, "status": "pending"}
            for p in ["0", "1", "2", "3", "4-5", "6", "7", "7.5", "7.7", "8", "9"]]})
        (self.root / "plan.md").write_text("# Migration\n\n## Phase 6\n")
        (self.root / "approval.md").write_text("A3: approved local fixture")
        (self.root / "fixture.py").write_text(
            "import hashlib,json,os,sys,time\nfrom pathlib import Path\n"
            "stage=sys.argv[1]\n"
            "with Path('launches.txt').open('a') as f: f.write(stage+'\\n')\n"
            "Path(stage+'.ready').write_text(str(os.getpid()))\n"
            "end=time.monotonic()+20\n"
            "while '--hold' in sys.argv and not Path(stage+'.release').exists():\n"
            "    if time.monotonic()>end: raise SystemExit(91)\n"
            "    time.sleep(.02)\n"
            "if '--fail' in sys.argv: raise SystemExit(9)\n"
            "p=Path('observed.txt')\n"
            "if stage=='operation': p.write_text('expected local result')\n"
            "else:\n"
            "    assert p.read_text()=='expected local result'\n"
            "    proof={'verified':True,'detail':'read and compared local contents',"
            "'evidence':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}\n"
            "    Path(os.environ['MIGRATION_ATTEMPT_DIR'],'verification.json').write_text(json.dumps(proof))\n")
        self.spec = {
            "phase": "6", "step": "load", "label": "Local fixture",
            "plan": "plan.md", "plan_section": "Phase 6", "dashboard": "dashboard",
            "effect": "target_write", "scope": "temporary local files",
            "authorization": dict(self.ref("approval.md"), reference="A3"),
            "inputs": [self.ref("fixture.py")],
            "operation": [sys.executable, "fixture.py", "operation"],
            "verification": [sys.executable, "fixture.py", "verification"],
            "interval_seconds": .05, "attention_after_seconds": 15,
        }

    def tearDown(self):
        # Only fixture processes created by this case. Never production job handling.
        for stage in ("operation", "verification"):
            (self.root / (stage + ".release")).touch()
        for started in (self.root / ".execution").glob("*/started.json"):
            pid = self.read(started)["worker_pid"]
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def save(self, path, value):
        # Atomic external edits avoid testing accidental partial JSON writes.
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)

    def read(self, path):
        return json.loads(path.read_text())

    def ref(self, name):
        return {"path": name, "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest()}

    def run_cli(self, *args):
        return subprocess.run(self.cli + list(args), cwd=self.root,
                              capture_output=True, text=True, timeout=12,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def start(self, name="first", spec=None):
        self.save(self.root / (name + ".json"), spec or self.spec)
        return self.run_cli("start", "--workspace", str(self.root), "--attempt", name,
                            "--spec", str(self.root / (name + ".json")))

    def attempt(self, name="first"):
        return self.root / ".execution" / name

    def wait_for(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.02)
        self.fail("fixture did not reach expected observable state")

    def finished(self, name="first"):
        attempt = self.attempt(name)
        self.wait_for(lambda: (attempt / "result.json").exists()
                      or ((attempt / "state.json").exists()
                          and self.read(attempt / "state.json")["status"] == "blocked"))
        return self.run_cli("status", "--attempt-dir", str(attempt))

    def inspection(self):
        (self.root / "inspection.txt").write_text("Inspected fixture processes and local output.")
        path = self.root / "inspection.json"
        self.save(path, {"no_active_job": True, "detail": "No remaining fixture job; recover by inspection",
                         "evidence": [self.ref("inspection.txt")]})
        return path

    def reconcile(self, name="first"):
        return self.run_cli("reconcile", "--attempt-dir", str(self.attempt(name)),
                            "--evidence-file", str(self.inspection()))

    def hold_dashboard(self):
        lock = (self.dash / ".status.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(lock.close)
        return lock

    def test_cli_success_and_missing_completion_checkpoint(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.finished().returncode, 0)
        self.assertEqual((self.root / "launches.txt").read_text().splitlines(),
                         ["operation", "verification"])
        paths = [self.root / "plan.md", self.dash / "status.json",
                 self.dash / "activity-log.jsonl"]
        for path in paths:
            with self.subTest(checkpoint=path.name):
                original = path.read_bytes()
                path.unlink()
                try:
                    result = self.run_cli("status", "--attempt-dir", str(self.attempt()))
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("checkpoint", result.stdout + result.stderr)
                    self.assertNotEqual(self.start("next", dict(self.spec, previous="first")).returncode, 0)
                finally:
                    path.write_bytes(original)
        self.assertEqual(self.run_cli("status", "--attempt-dir", str(self.attempt())).returncode, 0)

    def test_cli_conflicting_activity_outcome_blocks_status_and_advancement(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.finished().returncode, 0)
        path = self.dash / "activity-log.jsonl"
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(entries[-1]["result"], "success")
        entries[-1]["result"] = "blocked"
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()))
        self.assertEqual(status.returncode, 1, status.stdout)
        self.assertNotEqual(self.start("next", dict(self.spec, previous="first")).returncode, 0)

    def test_cli_mode_change_while_launch_checkpoint_waits(self):
        lock = self.hold_dashboard()
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.attempt() / "checkpoint-patch.json").exists())
        state = self.read(self.dash / "status.json")
        state["mode"] = "1"
        self.save(self.dash / "status.json", state)
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.finished().returncode, 1)
        self.assertFalse((self.root / "operation.ready").exists(),
                         "mutation launched after engagement became Mode 1")

    def test_cli_approval_change_while_launch_checkpoint_waits(self):
        lock = self.hold_dashboard()
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.attempt() / "checkpoint-patch.json").exists())
        (self.root / "approval.md").write_text("Withdrawn fixture scope")
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.finished().returncode, 1)
        self.assertFalse((self.root / "operation.ready").exists(),
                         "mutation launched after bound authorization changed")

    def test_cli_input_change_while_launch_checkpoint_waits(self):
        lock = self.hold_dashboard()
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.attempt() / "checkpoint-patch.json").exists())
        with (self.root / "fixture.py").open("a") as stream:
            stream.write("\n# Different reviewed bytes\n")
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.finished().returncode, 1)
        self.assertFalse((self.root / "operation.ready").exists(),
                         "changed script ran before its hash was rechecked")

    def test_cli_budget_expiry_during_launch_checkpoint_never_launches(self):
        lock = self.hold_dashboard()
        self.assertEqual(self.start(spec=dict(self.spec, attention_after_seconds=.1)).returncode, 0)
        self.wait_for(lambda: (self.attempt() / "checkpoint-patch.json").exists())
        time.sleep(.2)
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.finished().returncode, 1)
        self.assertFalse((self.root / "operation.ready").exists(),
                         "mutation launched after its supervision budget expired")

    def test_cli_reconciliation_prevents_delayed_worker_launch(self):
        self.reserve_without_worker()
        self.assertEqual(self.reconcile().returncode, 0)
        delayed = self.run_cli("_worker", "--attempt-dir", str(self.attempt()))
        self.assertEqual(delayed.returncode, 1, delayed.stdout)
        self.assertFalse((self.root / "operation.ready").exists())
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()))
        self.assertEqual(status.returncode, 1)
        self.assertIn("reconciliation", status.stdout)

    def reserve_without_worker(self):
        # Simulate loss of the start process after reservation, before spawning its worker.
        self.save(self.root / "first.json", self.spec)
        script = (
            "import json,sys\nfrom pathlib import Path\n"
            "sys.path.insert(0,sys.argv[1])\nimport execution_step\n"
            "r=Path.cwd()\nexecution_step.reserve(r,r/'.execution','first',"
            "json.loads((r/'first.json').read_text()))\n")
        reserved = subprocess.run([sys.executable, "-c", script, str(Path(runner.__file__).parent)],
                                  cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(reserved.returncode, 0, reserved.stderr)

    def test_cli_failed_reconciliation_publication_still_prevents_delayed_launch(self):
        self.reserve_without_worker()
        (self.root / "plan.md").write_text("# Missing section\n")
        self.assertNotEqual(self.reconcile().returncode, 0)
        self.assertFalse((self.attempt() / "reconciliation.json").exists())
        (self.root / "plan.md").write_text("# Migration\n\n## Phase 6\n")
        self.assertNotEqual(self.run_cli("_worker", "--attempt-dir", str(self.attempt())).returncode, 0)
        self.assertFalse((self.root / "operation.ready").exists())
        self.assertEqual(self.reconcile().returncode, 0)

    def test_cli_publication_failure_reports_cause_and_needs_reconciliation(self):
        self.spec["operation"].append("--hold")
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.root / "operation.ready").exists())
        (self.root / "plan.md").write_text("# Missing requested section\n")
        failed = self.finished()
        self.assertEqual(failed.returncode, 1)
        self.assertFalse((self.attempt() / "result.json").exists())
        self.assertNotEqual(self.reconcile().returncode, 0, "cannot reconcile a live fixture job")
        log = (self.attempt() / "worker.log").read_text()
        self.assertIn("dashboard_update:", log, "publisher diagnostics were discarded")
        (self.root / "operation.release").touch()
        self.wait_for(lambda: (self.root / "observed.txt").exists())
        (self.root / "plan.md").write_text("# Migration\n\n## Phase 6\n")
        self.assertEqual(self.reconcile().returncode, 0)
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()))
        self.assertEqual(status.returncode, 1)
        self.assertIn("reconciliation", status.stdout)

    def test_cli_lost_worker_remains_unknown_until_reconciled_then_recovers(self):
        self.spec["operation"].append("--hold")
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.root / "operation.ready").exists())
        attempt = self.attempt()
        self.wait_for(lambda: self.read(attempt / "state.json").get("job_pid"))
        record = self.read(attempt / "state.json")
        os.kill(record["worker_pid"], signal.SIGKILL)
        # Wait for the killed fixture worker's lock to be released.
        with (attempt / ".worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fcntl.flock(lock, fcntl.LOCK_UN)
        status = self.run_cli("status", "--attempt-dir", str(attempt))
        self.assertEqual(status.returncode, 1, status.stdout)
        self.assertIn("unknown", status.stdout)
        self.assertNotEqual(self.reconcile().returncode, 0)
        recovery = dict(self.spec, step="inspect", previous="first", recovery_for="first",
                        effect="read_only", operation=[sys.executable, "-c", "pass"])
        self.assertNotEqual(self.start("recovery", recovery).returncode, 0)
        self.assertFalse((attempt / "result.json").exists())
        (self.root / "operation.release").touch()
        self.wait_for(lambda: (self.root / "observed.txt").exists())
        # The fixture writes output just before exit; allow its PID to disappear.
        def exited():
            try:
                os.kill(record["job_pid"], 0)
                return False
            except ProcessLookupError:
                return True
        self.wait_for(exited)
        self.assertEqual(self.reconcile().returncode, 0)
        self.assertFalse((attempt / "result.json").exists())
        self.assertEqual(self.start("recovery", recovery).returncode, 0)
        self.assertEqual(self.finished("recovery").returncode, 0)
        retry = dict(self.spec, previous="recovery")
        self.assertEqual(self.start("retry", retry).returncode, 0)
        self.assertEqual(self.finished("retry").returncode, 0)
        self.assertEqual(self.read(attempt / "reconciliation.json")["status"], "blocked")

    def test_cli_verification_is_supervised_and_status_wait_does_not_relaunch(self):
        self.spec["verification"].append("--hold")
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.root / "verification.ready").exists())
        before = (self.dash / "activity-log.jsonl").read_text().count("\n")
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()),
                              "--wait-seconds", ".3")
        self.assertEqual(status.returncode, 3, status.stdout)
        self.assertGreater((self.dash / "activity-log.jsonl").read_text().count("\n"), before)
        self.assertEqual((self.root / "launches.txt").read_text().splitlines(),
                         ["operation", "verification"])
        self.assertNotEqual(self.reconcile().returncode, 0)
        (self.root / "verification.release").touch()
        self.assertEqual(self.finished().returncode, 0)

    def test_cli_changes_during_operation_block_verifier(self):
        for change in ("mode", "approval", "input"):
            with self.subTest(change=change):
                # Each change gets an independent chain and complete process cleanup.
                case = OperatorCLITests()
                case.setUp()
                try:
                    case.spec["operation"].append("--hold")
                    self.assertEqual(case.start().returncode, 0)
                    case.wait_for(lambda: (case.root / "operation.ready").exists())
                    if change == "mode":
                        # A cooperating editor must hold the publisher's lock.
                        with (case.dash / ".status.lock").open("a") as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            state = case.read(case.dash / "status.json")
                            state["mode"] = "1"
                            case.save(case.dash / "status.json", state)
                    else:
                        filename = "approval.md" if change == "approval" else "fixture.py"
                        with (case.root / filename).open("a") as stream:
                            stream.write("\n# changed\n")
                    (case.root / "operation.release").touch()
                    self.assertEqual(case.finished().returncode, 1)
                    self.assertFalse((case.root / "verification.ready").exists())
                finally:
                    case.tearDown()

    def test_cli_competing_starts_only_launch_one_operation(self):
        self.spec["operation"].append("--hold")
        self.save(self.root / "spec.json", self.spec)
        commands = [self.cli + ["start", "--workspace", str(self.root), "--attempt", name,
                               "--spec", str(self.root / "spec.json")]
                    for name in ("alpha", "beta")]
        processes = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) for cmd in commands]
        outputs = [p.communicate(timeout=8) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], outputs)
        winner = ["alpha", "beta"][[p.returncode for p in processes].index(0)]
        self.wait_for(lambda: (self.root / "operation.ready").exists())
        self.assertEqual((self.root / "launches.txt").read_text().splitlines(), ["operation"])
        self.assertNotEqual(self.start(winner).returncode, 0)
        (self.root / "operation.release").touch()
        self.assertEqual(self.finished(winner).returncode, 0)

    def test_cli_failed_operation_recovery_and_retry_preserve_failure(self):
        failed_spec = copy.deepcopy(self.spec)
        failed_spec["operation"].append("--fail")
        self.assertEqual(self.start(spec=failed_spec).returncode, 0)
        self.assertEqual(self.finished().returncode, 1)
        original = (self.attempt() / "result.json").read_bytes()
        self.assertFalse((self.root / "verification.ready").exists())
        self.assertNotEqual(self.start("retry", dict(self.spec, previous="first")).returncode, 0)
        recovery = dict(self.spec, step="inspect", previous="first", recovery_for="first")
        self.assertEqual(self.start("recover", recovery).returncode, 0)
        self.assertEqual(self.finished("recover").returncode, 0)
        self.assertEqual(self.start("retry", dict(self.spec, previous="recover")).returncode, 0)
        self.assertEqual(self.finished("retry").returncode, 0)
        self.assertEqual((self.attempt() / "result.json").read_bytes(), original)
        self.assertNotEqual(self.start("branch", dict(self.spec, previous="recover")).returncode, 0)
        # Closing an old failure must not overwrite checkpoints in an advanced chain.
        self.assertNotEqual(self.reconcile().returncode, 0)
        self.assertEqual(self.finished("retry").returncode, 0)

    def test_cli_attention_keeps_observing_but_never_verifies_late_exit(self):
        self.spec["operation"].append("--hold")
        self.spec["attention_after_seconds"] = .5
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.root / "operation.ready").exists())
        self.wait_for(lambda: self.read(self.attempt() / "state.json").get("attention_required"))
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()))
        self.assertEqual(status.returncode, 1)
        self.assertFalse((self.attempt() / "result.json").exists())
        self.assertNotEqual(self.reconcile().returncode, 0)
        (self.root / "operation.release").touch()
        self.wait_for(lambda: (self.attempt() / "result.json").exists())
        self.assertEqual(self.finished().returncode, 1)
        self.assertFalse((self.root / "verification.ready").exists())

    def test_cli_verified_but_unpublishable_patch_never_completes(self):
        with (self.root / "fixture.py").open("a") as stream:
            stream.write(
                "\nif stage=='verification':\n"
                "    proof['dashboard_patch']={'migration_objects':{'tables':{'total':float('nan')}}}\n"
                "    Path(os.environ['MIGRATION_ATTEMPT_DIR'],'verification.json').write_text(json.dumps(proof))\n")
        self.spec["inputs"] = [self.ref("fixture.py")]
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.finished().returncode, 1)
        record = self.read(self.attempt() / "result.json")
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["observed_exit_code"], 0)
        self.assertTrue(self.read(self.attempt() / "verification.json")["verified"])
        self.assertNotEqual(self.start("next", dict(self.spec, previous="first")).returncode, 0)

    def test_cli_unsupported_effects_never_launch(self):
        for effect in ("source_write", "decommission"):
            with self.subTest(effect=effect):
                result = self.start(effect, dict(self.spec, effect=effect))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("effect", result.stderr)
        self.assertFalse((self.root / "operation.ready").exists())

    def test_cli_rehearsal_approval_cannot_authorize_live_cutover(self):
        spec = copy.deepcopy(self.spec)
        spec["effect"] = "cutover"
        spec["authorization"]["reference"] = "A4b: approved rehearsal"
        result = self.start(spec=spec)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("A4", result.stderr)
        self.assertFalse((self.root / "operation.ready").exists())

    def test_cli_result_persistence_failure_requires_local_repair_and_reconciliation(self):
        with (self.root / "fixture.py").open("a") as stream:
            stream.write(
                "\nif stage=='verification':\n"
                "    Path(os.environ['MIGRATION_ATTEMPT_DIR'],'result.json').mkdir()\n")
        self.spec["inputs"] = [self.ref("fixture.py")]
        self.assertEqual(self.start().returncode, 0)
        self.wait_for(lambda: (self.attempt() / "result.json").is_dir())
        with (self.attempt() / ".worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.run_cli("status", "--attempt-dir", str(self.attempt())).returncode, 1)
        self.assertNotEqual(self.start("next", dict(self.spec, previous="first")).returncode, 0)
        (self.attempt() / "result.json").rmdir()
        self.assertEqual(self.reconcile().returncode, 0)
        self.assertFalse((self.attempt() / "result.json").exists())
        status = self.run_cli("status", "--attempt-dir", str(self.attempt()))
        self.assertEqual(status.returncode, 1)
        self.assertIn("reconciliation", status.stdout)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.dash = self.root / "dashboard"
        self.dash.mkdir()
        runner.write(self.dash / "status.json", {"mode": "3", "phases": [
            {"id": phase, "status": "pending"} for phase in runner.dashboard.PHASE_IDS]})
        (self.root / "migration-plan.md").write_text("# Migration\n\n## Phase 6\n")
        (self.root / "authorizations.md").write_text("A3 test fixture: accepted target test scope")
        (self.root / "operation.py").write_text(
            "from pathlib import Path\nPath('observed.txt').write_text('observed fixture result')\n")
        (self.root / "verify.py").write_text(
            "import os,json,hashlib\nfrom pathlib import Path\n"
            "p=Path('observed.txt')\nassert p.read_text()=='observed fixture result'\n"
            "data={'verified':True,'detail':'fixture contents verified','evidence':["
            "{'path':'observed.txt','sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}\n"
            "Path(os.environ['MIGRATION_ATTEMPT_DIR'],'verification.json').write_text(json.dumps(data))\n")
        self.state_root = self.root / ".execution"
        self.spec = {
            "phase": "6", "step": "load", "label": "Load fixture",
            "plan": "migration-plan.md", "plan_section": "Phase 6", "dashboard": "dashboard",
            "effect": "target_write", "scope": "temporary local fixture",
            "authorization": dict(self.ref("authorizations.md"), reference="A3"),
            "inputs": [self.ref("operation.py"), self.ref("verify.py")],
            "operation": [sys.executable, "operation.py"],
            "verification": [sys.executable, "verify.py"],
            "interval_seconds": 0.05, "attention_after_seconds": 30,
        }

    def tearDown(self):
        self.temp.cleanup()

    def ref(self, path):
        return {"path": path, "sha256": hashlib.sha256((self.root / path).read_bytes()).hexdigest()}

    def execute(self, name="first", spec=None):
        attempt = runner.reserve(self.root, self.state_root, name, spec or self.spec)
        with mock.patch.dict(os.environ, MIGRATION_ATTEMPT_DIR=str(attempt)):
            runner.worker(attempt)
        return attempt, runner.load(attempt / "state.json")

    def test_operation_and_verification_publish_real_completion(self):
        attempt, record = self.execute()
        self.assertEqual(record["status"], "done")
        runner.checkpoint_exists(self.root, self.spec, record)
        self.assertEqual(runner.status(attempt)[1], 0)
        self.assertEqual(os.stat(attempt / "operation.log").st_mode & 0o777, 0o600)

    def test_unicode_line_separators_in_evidence_preserve_checkpoint(self):
        verifier = self.root / "verify.py"
        verifier.write_text(verifier.read_text().replace(
            "fixture contents verified", "한글\\u2028evidence\\u2029verified"))
        self.spec["inputs"] = [self.ref("operation.py"), self.ref("verify.py")]
        attempt, record = self.execute()
        self.assertEqual(record["status"], "done")
        observed, code = runner.status(attempt)
        self.assertEqual(code, 0, observed)
        next_spec = dict(self.spec, previous="first", step="next")
        runner.reserve(self.root, self.state_root, "second", next_spec)

    def test_mode_one_blocks_target_writes_before_launch(self):
        state = runner.load(self.dash / "status.json")
        state["mode"] = "1"
        runner.write(self.dash / "status.json", state)
        with self.assertRaisesRegex(runner.StepError, "Mode 1"):
            runner.reserve(self.root, self.state_root, "first", self.spec)
        self.assertFalse((self.root / "observed.txt").exists())

    def test_mode_two_blocks_live_cutover_and_rollback(self):
        state = runner.load(self.dash / "status.json")
        state["mode"] = "2"
        runner.write(self.dash / "status.json", state)
        for effect in ("cutover", "rollback"):
            with self.subTest(effect=effect), self.assertRaisesRegex(runner.StepError, "Mode 3"):
                runner.validate_spec(self.root, dict(self.spec, effect=effect))

    def test_reviewed_script_change_blocks_before_launch(self):
        (self.root / "operation.py").write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(runner.StepError, "input changed"):
            runner.reserve(self.root, self.state_root, "first", self.spec)

    def test_authorization_record_change_blocks_before_launch(self):
        (self.root / "authorizations.md").write_text("different scope")
        with self.assertRaisesRegex(runner.StepError, "input changed"):
            runner.reserve(self.root, self.state_root, "first", self.spec)

    def test_worker_rechecks_script_after_reservation(self):
        attempt = runner.reserve(self.root, self.state_root, "first", self.spec)
        (self.root / "operation.py").write_text("raise SystemExit(0)")
        runner.worker(attempt)
        self.assertEqual(runner.load(attempt / "result.json")["status"], "blocked")
        self.assertFalse((self.root / "observed.txt").exists())

    def test_same_attempt_cannot_restart(self):
        attempt, _ = self.execute()
        before = (self.root / "observed.txt").stat().st_mtime_ns
        with self.assertRaisesRegex(runner.StepError, "already started"):
            runner.worker(attempt)
        self.assertEqual(before, (self.root / "observed.txt").stat().st_mtime_ns)

    def test_zero_exit_without_verification_evidence_is_blocked(self):
        spec = copy.deepcopy(self.spec)
        spec["verification"] = [sys.executable, "-c", "pass"]
        attempt, state = self.execute(spec=spec)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(runner.status(attempt)[1], 1)

    def test_nonzero_operation_never_runs_verifier(self):
        spec = copy.deepcopy(self.spec)
        spec["operation"] = [sys.executable, "-c", "raise SystemExit(8)"]
        attempt, state = self.execute(spec=spec)
        self.assertEqual(state["status"], "blocked")
        self.assertFalse((attempt / "verification.log").exists())

    def test_evidence_hash_mismatch_is_blocked(self):
        (self.root / "verify.py").write_text(
            "import os,json\nfrom pathlib import Path\n"
            "Path(os.environ['MIGRATION_ATTEMPT_DIR'],'verification.json').write_text(json.dumps("
            "{'verified':True,'detail':'looks fine','evidence':[{'path':'observed.txt','sha256':'wrong'}]}))")
        self.spec["inputs"][1] = self.ref("verify.py")
        _, state = self.execute()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("input changed", state["error"])

    def test_failed_attempt_requires_separate_recovery(self):
        bad = dict(self.spec, operation=[sys.executable, "-c", "raise SystemExit(8)"])
        self.execute(spec=bad)
        with self.assertRaisesRegex(runner.StepError, "reviewed recovery"):
            runner.reserve(self.root, self.state_root, "retry", dict(self.spec, previous="first"))
        with self.assertRaisesRegex(runner.StepError, "separate step"):
            runner.reserve(self.root, self.state_root, "retry",
                           dict(self.spec, previous="first", recovery_for="first"))

    def test_recovery_then_new_attempt_preserves_failure(self):
        bad = dict(self.spec, operation=[sys.executable, "-c", "raise SystemExit(8)"])
        failed, _ = self.execute(spec=bad)
        recovery = dict(self.spec, step="investigate", previous="first", recovery_for="first",
                        effect="read_only")
        self.execute("recover", recovery)
        _, state = self.execute("retry", dict(self.spec, previous="recover"))
        self.assertEqual(state["status"], "done")
        self.assertEqual(runner.load(failed / "result.json")["status"], "blocked")

    def test_stale_predecessor_cannot_branch(self):
        self.execute()
        with self.assertRaisesRegex(runner.StepError, "latest attempt"):
            runner.reserve(self.root, self.state_root, "next", self.spec)

    def test_missing_predecessor_plan_checkpoint_blocks(self):
        self.execute()
        (self.root / "migration-plan.md").write_text("# Plan\n## Phase 6\n")
        with self.assertRaisesRegex(runner.StepError, "plan checkpoint"):
            runner.reserve(self.root, self.state_root, "next", dict(self.spec, previous="first"))

    def test_failed_initial_publisher_never_launches_or_releases_attempt(self):
        with mock.patch.object(runner, "publish", side_effect=runner.StepError("disk unavailable")):
            attempt, state = self.execute()
        self.assertEqual(state["status"], "blocked")
        self.assertFalse((self.root / "observed.txt").exists())
        with self.assertRaises(runner.StepError):
            runner.reserve(self.root, self.state_root, "first", self.spec)
        self.assertTrue((attempt / "started.json").exists())

    def test_attention_budget_never_turns_into_success(self):
        spec = dict(self.spec, attention_after_seconds=0.05,
                    operation=[sys.executable, "-c", "import time; time.sleep(.15)"])
        attempt, record = self.execute(spec=spec)
        self.assertTrue(record["attention_required"])
        self.assertEqual(record["status"], "blocked")
        self.assertFalse((attempt / "verification.log").exists())

    def test_unknown_result_cannot_start_next_step(self):
        runner.reserve(self.root, self.state_root, "first", self.spec)
        with self.assertRaisesRegex(runner.StepError, "no terminal result"):
            runner.reserve(self.root, self.state_root, "next", dict(self.spec, previous="first"))

    def test_path_traversal_and_symlink_rejected(self):
        (self.root / "alias").symlink_to(self.root / "operation.py")
        for path in ("../operation.py", "/tmp/file", "alias"):
            with self.subTest(path=path), self.assertRaises(runner.StepError):
                runner.check_ref(self.root, {"path": path, "sha256": "anything"})

    def test_reconciliation_is_blocked_and_preserves_original_failure(self):
        with mock.patch.object(runner, "publish", side_effect=runner.StepError("disk unavailable")):
            attempt, old = self.execute()
        (self.root / "inspection.txt").write_text("No process launched; dashboard repaired.")
        evidence = self.root / "reconcile.json"
        runner.write(evidence, {"no_active_job": True, "detail": "No job launched; fix verified",
                               "evidence": [self.ref("inspection.txt")]})
        closed = runner.reconcile(attempt, evidence)
        self.assertEqual(closed["status"], "blocked")
        self.assertEqual(runner.load(attempt / "result.json"), old)
        runner.reserve(self.root, self.state_root, "recovery", dict(
            self.spec, step="repair", previous="first", recovery_for="first"))

    def test_detached_worker_survives_start_command_and_status_does_not_relaunch(self):
        spec = self.root / "spec.json"
        runner.write(spec, self.spec)
        command = [sys.executable, runner.__file__, "start", "--workspace", str(self.root),
                   "--attempt", "detached", "--spec", str(spec)]
        started = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(started.returncode, 0, started.stderr)
        attempt = self.state_root / "detached"
        record, code = runner.status(attempt, 10)
        self.assertEqual(code, 0, record)
        before = (self.root / "observed.txt").stat().st_mtime_ns
        self.assertEqual(runner.status(attempt)[1], 0)
        self.assertEqual(before, (self.root / "observed.txt").stat().st_mtime_ns)

    def test_verification_launch_error_does_not_inherit_operation_result(self):
        spec = dict(self.spec, verification=["/nonexistent/migration-verifier"])
        attempt, state = self.execute(spec=spec)
        self.assertEqual(state["status"], "blocked")
        self.assertIsNone(state["observed_exit_code"])
        self.assertEqual(runner.status(attempt)[1], 1)

    def test_reconciliation_cannot_override_done_result_when_final_state_write_failed(self):
        attempt = runner.reserve(self.root, self.state_root, "first", self.spec)
        original_write = runner.write

        def fail_final_state(path, value):
            if Path(path).name == "state.json" and value.get("status") == "done":
                raise OSError("fixture final state write failure")
            original_write(path, value)

        with mock.patch.object(runner, "write", side_effect=fail_final_state), \
                mock.patch.dict(os.environ, MIGRATION_ATTEMPT_DIR=str(attempt)):
            with self.assertRaisesRegex(OSError, "final state"):
                runner.worker(attempt)
        original_result = (attempt / "result.json").read_bytes()
        self.assertEqual(runner.status(attempt)[1], 0)
        (self.root / "inspection.txt").write_text("Fixture finished; no active process.")
        evidence = self.root / "reconcile.json"
        runner.write(evidence, {"no_active_job": True, "detail": "No active fixture job",
                               "evidence": [self.ref("inspection.txt")]})
        with self.assertRaisesRegex(runner.StepError, "successful"):
            runner.reconcile(attempt, evidence)
        self.assertEqual((attempt / "result.json").read_bytes(), original_result)
        self.assertEqual(runner.status(attempt)[1], 0)


if __name__ == "__main__":
    unittest.main()
