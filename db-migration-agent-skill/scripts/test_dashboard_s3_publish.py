#!/usr/bin/env python3
"""Tests for shared/scripts/dashboard_s3_publish.py (OBS-48): bounded, fully detached publisher.

Run: python3 -B -m unittest discover -s scripts -p 'test_dashboard_s3_publish.py'
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "shared" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import dashboard_s3_publish as pub  # noqa: E402

SCRIPT = SCRIPTS / "dashboard_s3_publish.py"


def _client_error(code):
    err = Exception(code)
    err.response = {"Error": {"Code": code}}
    return err


class FakeS3:
    """S3 with ETags and conditional PutObject (IfMatch / IfNoneMatch='*')."""

    def __init__(self, remote_status=None, prefix=""):
        self.objects, self.etag_n, self.puts, self.calls = {}, 0, [], []
        self.before_put = None          # hook: runs between the cycle's read and its write
        self.after_get = None           # hook: runs right after a status GET returns (after the guard read)
        if remote_status is not None:
            self._store(f"{prefix}status.json", remote_status if isinstance(remote_status, bytes)
                        else json.dumps(remote_status).encode())

    def _store(self, key, body):
        self.etag_n += 1
        self.objects[key] = (body, f'"e{self.etag_n}"')

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        body, etag = self.objects[Key]
        out = {"Body": io.BytesIO(body), "ETag": etag}
        if self.after_get:
            hook, self.after_get = self.after_get, None
            hook(self)
        return out

    def head_object(self, Bucket, Key):
        self.calls.append(("head", Key))
        if Key not in self.objects:
            raise _client_error("404")
        return {"ETag": self.objects[Key][1]}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl, IfMatch=None, IfNoneMatch=None):
        self.calls.append(("put", Key))
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook(self)
        cur = self.objects.get(Key)
        if IfNoneMatch == "*" and cur is not None:
            raise _client_error("PreconditionFailed")
        if IfMatch is not None and (cur is None or cur[1] != IfMatch):
            raise _client_error("PreconditionFailed")
        if IfMatch is None and IfNoneMatch is None:
            raise AssertionError("unconditional write")
        self._store(Key, Body)
        self.puts.append(Key)


class PublishOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "status.json").write_text(json.dumps({"phases": []}))
        (self.dir / "activity-log.jsonl").write_text('{"title": "x"}\n')
        (self.dir / "index.html").write_text("<html>")
        (self.dir / ".status.lock").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def once(self, s3, prefix="p/", last=None):
        return pub.publish_once(s3, "b", prefix, self.dir, {} if last is None else last, log=lambda m: None)

    def test_uploads_only_the_data_files_conditionally_and_only_when_changed(self):
        s3, last = FakeS3(), {}
        self.assertEqual(self.once(s3, last=last), ["status.json", "activity-log.jsonl"])
        self.assertEqual(set(s3.objects), {"p/status.json", "p/activity-log.jsonl"}, "never index.html or the lock")
        self.assertEqual(self.once(s3, last=last), [], "unchanged: no upload")
        (self.dir / "activity-log.jsonl").write_text('{"title": "x"}\n{"title": "y"}\n')
        self.assertEqual(self.once(s3, last=last), ["activity-log.jsonl"], "IfMatch on the ETag read this cycle")

    def test_active_soak_in_the_bucket_stops_before_any_write(self):
        s3 = FakeS3(remote_status={"soak": {"state": "active", "days": []}}, prefix="p/")
        with self.assertRaises(pub.SoakActive):
            self.once(s3)
        self.assertEqual(s3.puts, [])

    def test_soak_activated_between_read_and_write_is_never_overwritten(self):
        # review r12 #3: activation + a green result land after the guard read, before the write
        s3 = FakeS3(remote_status={"phases": []}, prefix="p/")
        live = json.dumps({"soak": {"state": "active", "days": [{"date": "2026-10-28", "overall": "green"}]}}).encode()
        s3.before_put = lambda c: c._store("p/status.json", live)
        self.assertNotIn("status.json", self.once(s3), "412 -> skipped, not overwritten")
        self.assertEqual(s3.objects["p/status.json"][0], live, "the soak evidence survives")
        with self.assertRaises(pub.SoakActive):          # next cycle re-reads and stops
            self.once(s3)

    def test_malformed_or_unreadable_remote_status_fails_closed(self):
        for body in (b'{"soak": {"state": "act', b'["not", "an", "object"]', b'{"soak": "active"}'):
            s3 = FakeS3(remote_status=body, prefix="p/")
            with self.assertRaises(pub.SoakActive, msg=body):
                self.once(s3)
            self.assertEqual(s3.puts, [], body)
        s3 = FakeS3()
        s3.get_object = lambda **kw: (_ for _ in ()).throw(_client_error("AccessDenied"))
        with self.assertRaises(pub.SoakActive):
            self.once(s3)
        self.assertEqual(s3.puts, [])

    def test_absent_objects_are_created_with_if_none_match(self):
        s3 = FakeS3()
        self.assertEqual(self.once(s3), ["status.json", "activity-log.jsonl"], "created with IfNoneMatch='*'")
        s3 = FakeS3()
        s3.before_put = lambda c: c._store("p/status.json", b"{}")   # someone created it meanwhile
        self.assertEqual(self.once(s3), [], "status conflict abandons the whole cycle")
        self.assertEqual(s3.objects["p/status.json"][0], b"{}")
        self.assertNotIn("p/activity-log.jsonl", s3.objects, "the log is not touched after a status conflict")

    def test_review13_race_status_conflict_never_lets_the_log_be_replaced(self):
        # read inactive status -> another writer activates soak AND appends log evidence ->
        # the status put fails its condition -> the log must not be written at all
        s3 = FakeS3(remote_status={"phases": []}, prefix="p/")
        s3._store("p/activity-log.jsonl", b'{"title": "old"}\n')
        evidence = b'{"title": "old"}\n{"title": "soak interval 1 green"}\n'

        def writer(c):
            c._store("p/status.json", json.dumps({"soak": {"state": "active"}}).encode())
            c._store("p/activity-log.jsonl", evidence)
        s3.before_put = writer
        self.assertEqual(self.once(s3), [])
        self.assertEqual(s3.objects["p/activity-log.jsonl"][0], evidence, "soak evidence survives")
        self.assertEqual([k for op, k in s3.calls if op == "put"], ["p/status.json"], "no log put attempted")
        with self.assertRaises(pub.SoakActive):
            self.once(s3)

    def test_log_only_change_still_reads_the_status_guard_first(self):
        s3, last = FakeS3(), {}
        self.once(s3, last=last)
        (self.dir / "activity-log.jsonl").write_text('{"title": "x"}\n{"title": "y"}\n')
        s3.calls.clear()
        self.assertEqual(self.once(s3, last=last), ["activity-log.jsonl"])
        self.assertEqual(s3.calls[:2], [("head", "p/activity-log.jsonl"), ("get", "p/status.json")],
                         "log ETag first, then the status guard, before any write")
        s3._store("p/status.json", json.dumps({"soak": {"state": "active"}}).encode())
        (self.dir / "activity-log.jsonl").write_text('{"title": "z"}\n')
        with self.assertRaises(pub.SoakActive):
            self.once(s3, last=last)

    def test_review14_log_only_cycle_never_overwrites_evidence_appended_after_the_guard(self):
        # reviewer reproduction: log-only cycle; the status read shows no soak -> a soak activates
        # and appends green evidence -> the publisher must NOT replace the log
        s3, last = FakeS3(), {}
        self.once(s3, last=last)                                   # status + log published
        (self.dir / "activity-log.jsonl").write_text('{"title": "x"}\n{"title": "local only"}\n')
        evidence = b'{"title": "x"}\n{"title": "soak interval 1 green"}\n'

        def soak_starts(c):
            c._store("p/status.json", json.dumps({"soak": {"state": "active"}}).encode())
            c._store("p/activity-log.jsonl", evidence)
        s3.after_get = soak_starts
        self.assertEqual(self.once(s3, last=last), [], "log-only cycle abandoned on the 412")
        self.assertEqual(s3.objects["p/activity-log.jsonl"][0], evidence, "soak evidence survives")
        with self.assertRaises(pub.SoakActive):                     # next cycle stops on the soak
            self.once(s3, last=last)

    def test_invalid_local_status_is_skipped_not_uploaded(self):
        (self.dir / "status.json").write_text('{"phases": [')
        self.assertEqual(self.once(FakeS3(), prefix=""), ["activity-log.jsonl"])

    def test_until_is_required_bounded_and_zoned(self):
        now = datetime.datetime(2026, 10, 28, 20, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(pub.parse_until("2026-10-29T06:00:00Z", now).hour, 6)
        for bad in ("2026-10-28T19:00:00Z", "2026-11-28T00:00:00Z", "2026-10-29T06:00:00", "tomorrow"):
            with self.assertRaises(pub.PublishError, msg=bad):
                pub.parse_until(bad, now)
        self.assertEqual(pub.main(["--dir", str(self.dir), "--bucket", "b"]), 2, "the loop needs --until")

    def test_installed_botocore_supports_conditional_writes(self):
        self.assertTrue(pub.conditional_writes_supported())


class DeadlineTests(unittest.TestCase):
    """review r12 #5: --until bounds every cycle and every S3 call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "status.json").write_text("{}")
        self._budget = pub.CALL_BUDGET_S

    def tearDown(self):
        pub.CALL_BUDGET_S = self._budget
        self.tmp.cleanup()

    def test_no_s3_call_starts_at_or_after_until_minus_budget(self):
        pub.CALL_BUDGET_S = 0.5
        s3, stamps = FakeS3(), []
        n = {"i": 0}
        orig_put = s3.put_object

        def put(**kw):
            stamps.append(pub.utcnow())
            n["i"] += 1
            (self.dir / "status.json").write_text(json.dumps({"n": n["i"]}))   # keep producing changes
            return orig_put(**kw)
        s3.put_object = put
        until = pub.utcnow() + datetime.timedelta(seconds=2)
        args = argparse.Namespace(bucket="b", prefix="", dir=str(self.dir), dry_run=False, interval=1.0)
        started = time.monotonic()
        self.assertEqual(pub.run_loop(s3, args, until, lambda m: None), 0)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(stamps, "at least one cycle ran")
        for t in stamps:
            self.assertLessEqual((t - until).total_seconds(), -pub.CALL_BUDGET_S + 0.05,
                                 "an upload started inside the final call budget or after --until")

    def test_make_client_uses_short_timeouts_and_no_retries(self):
        c = pub.make_client("b", "ap-northeast-2")
        cfg = c.meta.config
        self.assertEqual((cfg.connect_timeout, cfg.read_timeout, cfg.retries["total_max_attempts"]), (5, 5, 1))
        self.assertGreaterEqual(pub.CALL_BUDGET_S, 15)
        self.assertLessEqual(pub.CONNECT_TIMEOUT_S + pub.READ_TIMEOUT_S, pub.CALL_BUDGET_S)

    def test_review13_eleven_seconds_left_starts_no_call(self):
        s3 = FakeS3()
        until = pub.utcnow() + datetime.timedelta(seconds=11)
        with self.assertRaises(pub.DeadlineReached):
            pub.publish_once(s3, "b", "", self.dir, {}, log=lambda m: None, until=until)
        self.assertEqual(s3.calls, [], "no S3 call started with less than CALL_BUDGET_S left")
        until = pub.utcnow() + datetime.timedelta(seconds=pub.CALL_BUDGET_S + 30)
        self.assertEqual(pub.publish_once(s3, "b", "", self.dir, {}, log=lambda m: None, until=until), ["status.json"])


WRAPPER = r"""
import io, json, os, sys, time
sys.path.insert(0, os.environ["PUB_SCRIPTS"])
import dashboard_s3_publish as pub
MODE, TOTAL = os.environ["SLOW_MODE"], float(os.environ["SLOW_SECONDS"])

def slow():
    end = time.monotonic() + TOTAL            # a streaming read: many small blocking reads
    while time.monotonic() < end:
        time.sleep(0.05)

class Body:
    def read(self):
        slow()
        return b"{}"

class Slow:
    def head_object(self, Bucket, Key):
        e = Exception("404"); e.response = {"Error": {"Code": "404"}}; raise e
    def get_object(self, Bucket, Key):
        if MODE == "read":
            return {"Body": Body(), "ETag": '"e1"'}
        return {"Body": io.BytesIO(b"{}"), "ETag": '"e1"'}
    def put_object(self, **kw):
        if MODE == "put":
            slow()

if MODE == "region":                     # slow bucket-region discovery during start-up
    pub._discover_region = lambda bucket: (slow(), "ap-northeast-2")[1]
else:
    pub.make_client = lambda bucket, region, until=None: Slow()
pub.conditional_writes_supported = lambda: True
if os.environ.get("CALL_BUDGET_S"):
    pub.CALL_BUDGET_S = float(os.environ["CALL_BUDGET_S"])
if os.environ.get("WATCHDOG_GRACE_S"):
    pub.WATCHDOG_GRACE_S = float(os.environ["WATCHDOG_GRACE_S"])
if os.environ.get("NO_ITIMER"):
    pub.signal.setitimer = lambda *a, **k: 0.0   # simulate a call the alarm cannot interrupt
sys.exit(pub.main(sys.argv[1:]))
"""


class AbsoluteDeadlineTests(unittest.TestCase):
    """review r14 #3: an in-flight call (incl. a slow streaming read) is interrupted at --until;
    SIGTERM interrupts it too; a watchdog hard-exits at --until + grace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "status.json").write_text('{"phases": []}')
        self.wrapper = self.dir / "slow_wrapper.py"
        self.wrapper.write_text(WRAPPER)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, until, mode, seconds, region=True, **env):
        e = dict(os.environ, PUB_SCRIPTS=str(SCRIPTS), SLOW_MODE=mode, SLOW_SECONDS=str(seconds),
                 **{k: str(v) for k, v in env.items()})
        return subprocess.Popen([sys.executable, "-B", str(self.wrapper), "--dir", str(self.dir), "--bucket", "b",
                                 *(["--region", "ap-northeast-2"] if region else []),
                                 "--until", until.isoformat().replace("+00:00", "Z"),
                                 "--interval", "1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=e)

    def test_review15_slow_region_discovery_at_startup_is_bounded(self):
        # Without --region, start-up discovers the bucket region; that must already run under the
        # deadline (alarm) with the watchdog started, so a 40 s discovery cannot outlive --until.
        until = pub.utcnow() + datetime.timedelta(seconds=4)
        proc = self.start(until, "region", 40, region=False)
        out, _ = proc.communicate(timeout=60)
        late = (pub.utcnow() - until).total_seconds()
        self.assertLess(late, 2.0, f"exited {late:.1f}s after --until during start-up: {out}")
        self.assertLess(late, pub.WATCHDOG_GRACE_S)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("during start-up", out)

    def test_review14_slow_streaming_read_is_interrupted_at_until(self):
        until = pub.utcnow() + datetime.timedelta(seconds=16)       # > CALL_BUDGET_S (15): the read starts
        proc = self.start(until, "read", 24)
        out, _ = proc.communicate(timeout=60)
        late = (pub.utcnow() - until).total_seconds()
        self.assertEqual(proc.returncode, 0, out)
        self.assertLess(late, 2.0, f"exited {late:.1f}s after --until (a 24 s read was in flight)")
        self.assertIn("in-flight S3 call was interrupted", out)

    def test_slow_put_is_interrupted_at_until(self):
        until = pub.utcnow() + datetime.timedelta(seconds=3)
        proc = self.start(until, "put", 24, CALL_BUDGET_S=1)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertLess((pub.utcnow() - until).total_seconds(), 2.0, out)

    def test_sigterm_interrupts_an_in_flight_call(self):
        until = pub.utcnow() + datetime.timedelta(minutes=10)
        proc = self.start(until, "put", 24)
        try:
            time.sleep(2.5)                                         # the slow put is in flight now
            t0 = time.monotonic()
            proc.send_signal(15)                                    # our own child, by its PID
            out, _ = proc.communicate(timeout=10)
            self.assertLess(time.monotonic() - t0, 2.0, out)
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("publisher stopped: signal", out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_watchdog_hard_exits_when_the_alarm_cannot_interrupt(self):
        until = pub.utcnow() + datetime.timedelta(seconds=2.5)
        proc = self.start(until, "read", 24, CALL_BUDGET_S=1, WATCHDOG_GRACE_S=1, NO_ITIMER=1)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 4, out)
        self.assertLess((pub.utcnow() - until).total_seconds(), 1 + 2.0, out)
        self.assertIn("watchdog", out)


class PidOwnershipTests(unittest.TestCase):
    """review r12 #2: never signal a process whose ownership is not proven; never a group."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pidfile = self.dir / ".publisher.pid"

    def tearDown(self):
        self.tmp.cleanup()

    def stop(self):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--stop"],
                              capture_output=True, text=True, timeout=20)

    def test_invalid_pids_and_legacy_pidfiles_are_never_signalled(self):
        sent = []
        orig = os.kill
        os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            for content in ("0\n", "-1\n", "1\n", json.dumps({"pid": 0, "token": "x" * 32, "start_time": "1"}),
                            json.dumps({"pid": -1, "token": "x" * 32, "start_time": "1"}),
                            json.dumps({"pid": 1, "token": "x" * 32, "start_time": "1"}), "garbage"):
                self.pidfile.write_text(content)
                rc = pub.main(["--dir", str(self.dir), "--stop"])
                self.assertEqual(rc, 1, content)
                self.assertTrue(self.pidfile.exists(), "unverifiable pidfile left for a human")
        finally:
            os.kill = orig
        self.assertEqual(sent, [], "nothing signalled")

    def test_foreign_live_process_is_refused_and_reused_pid_is_stale(self):
        foreign = subprocess.Popen(["sleep", "30"])      # started by this test; killed by its PID below
        try:
            start = pub._proc_start_time(foreign.pid)
            self.pidfile.write_text(json.dumps({"pid": foreign.pid, "token": "a" * 32, "start_time": start}))
            res = self.stop()
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("not this publisher", res.stderr)
            self.assertIsNone(foreign.poll(), "the foreign process is untouched")
            self.pidfile.write_text(json.dumps({"pid": foreign.pid, "token": "a" * 32, "start_time": "1"}))
            res = self.stop()                          # same PID, different start time = reused
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("reused", res.stdout)
            self.assertFalse(self.pidfile.exists())
            self.assertIsNone(foreign.poll(), "still untouched")
        finally:
            foreign.kill()
            foreign.wait(timeout=10)

    def test_dead_pid_is_stale_and_cleaned(self):
        gone = subprocess.Popen(["true"])
        gone.wait(timeout=10)
        self.pidfile.write_text(json.dumps({"pid": gone.pid, "token": "b" * 32, "start_time": "123"}))
        res = self.stop()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(self.pidfile.exists())


class DetachTests(unittest.TestCase):
    """The OBS-48 failure: a background job attached to the agent's shell holds the turn open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "status.json").write_text("{}")

    def tearDown(self):
        subprocess.run([sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--stop"],
                       capture_output=True, timeout=20)
        self.tmp.cleanup()

    def test_detach_returns_at_once_in_its_own_session_and_stops_cleanly(self):
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        started = time.monotonic()
        # capture_output keeps pipes open: a child that inherited them would block communicate()
        res = subprocess.run([sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--bucket", "b",
                              "--until", until, "--interval", "0.2", "--dry-run", "--detach"],
                             capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - started, 10, "the starter must not wait for the publisher")
        self.assertEqual(res.returncode, 0, res.stderr)
        info = json.loads(res.stdout)
        pid = info["pid"]
        self.assertTrue(Path(f"/proc/{pid}").exists(), "publisher running")
        self.assertEqual(os.getsid(pid), pid, "session leader of its own session")
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        self.assertEqual(os.readlink(f"/proc/{pid}/fd/0"), "/dev/null", "stdin detached")
        self.assertEqual(oct(os.stat(info["log"]).st_mode & 0o777), "0o600")
        self.assertIn("--stop", info["stop"])
        rec = json.loads(Path(info["pidfile"]).read_text())
        self.assertEqual(rec["pid"], pid)
        self.assertGreaterEqual(len(rec["token"]), 32)
        self.assertIn(rec["token"].encode(), Path(f"/proc/{pid}/cmdline").read_bytes(), "token in the child argv")
        t0 = time.monotonic()
        stop = subprocess.run([sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--stop"],
                              capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - t0, 3, "SIGTERM -> exit within a bounded time")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(Path(info["pidfile"]).exists())
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists() or "zombie" in self._state(pid):
                break
            time.sleep(0.1)
        self.assertTrue(not Path(f"/proc/{pid}").exists() or "zombie" in self._state(pid))

    @staticmethod
    def _state(pid):
        try:
            return Path(f"/proc/{pid}/status").read_text().lower()
        except OSError:
            return "gone"

    def test_negative_control_a_plain_background_job_holds_the_caller(self):
        # What OBS-48 did: `cmd &` from the tool shell keeps the shell's output pipes open, so a
        # caller waiting for them (a headless turn) never finishes. setsid + redirects do not.
        # Only the PIDs this test started are ever killed (never by pattern: other sessions'
        # processes run on the same machine).
        pids = []
        try:
            attached = subprocess.Popen(["bash", "-c", "sleep 31.7 & echo $!"], stdout=subprocess.PIPE, text=True)
            pids.append(int(attached.stdout.readline().strip()))
            with self.assertRaises(subprocess.TimeoutExpired):
                attached.communicate(timeout=3)          # the backgrounded child still holds stdout
            pidfile = self.dir / "detached.pid"   # the job records its own PID (setsid may fork)
            res = subprocess.run(["bash", "-c", f"setsid nohup sh -c 'echo $$ > {pidfile}; exec sleep 31.7' "
                                  ">/dev/null 2>&1 </dev/null & echo started"],
                                 capture_output=True, text=True, timeout=3)   # returns at once
            self.assertEqual(res.stdout.strip(), "started")
            for _ in range(50):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                time.sleep(0.1)
            pids.append(int(pidfile.read_text().strip()))
            self.assertEqual(os.getsid(pids[-1]), pids[-1], "the detached job leads its own session")
        finally:
            for pid in pids:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
            if "attached" in locals():
                attached.communicate(timeout=10)

    def test_loop_ends_by_itself_at_until(self):
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        started = time.monotonic()
        res = subprocess.run([sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--bucket", "b",
                              "--until", until, "--interval", "0.2", "--dry-run"],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn("window ended", res.stdout)

    def test_second_start_is_refused_while_one_runs(self):
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        args = [sys.executable, "-B", str(SCRIPT), "--dir", str(self.dir), "--bucket", "b", "--until", until,
                "--interval", "0.2", "--dry-run", "--detach"]
        self.assertEqual(subprocess.run(args, capture_output=True, timeout=20).returncode, 0)
        second = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already running", second.stderr)


if __name__ == "__main__":
    unittest.main()
