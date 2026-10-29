#!/usr/bin/env python3
"""MySQL-family consistent-snapshot isolation (dry run 5, OBS-29).

`START TRANSACTION WITH CONSISTENT SNAPSHOT` is ignored by MySQL unless the session runs
REPEATABLE READ ("For all other isolation levels, the WITH CONSISTENT SNAPSHOT clause is
ignored" — https://dev.mysql.com/doc/refman/8.0/en/commit.html). A target parameter group
with transaction_isolation=READ-COMMITTED turned the compare's MySQL "snapshot" into a moving
view while the PostgreSQL side stayed pinned -> 78 spurious money mismatches. All three
readers (cross_engine_compare.SqlSide.begin, soak_check_lambda._start_consistent_read,
soak_check.py's MySQL batch) now issue `SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE
READ` first.

Unit tests: the exact statement sequence of each reader, constant parity, and the read-only
allow-list (exactly this SET is allowed, nothing broader).

Integration tests (Docker): a mysql:8.0 server started with
--transaction-isolation=READ-COMMITTED and a postgres:16 server; a writer on separate
connections commits mid-read. Each reader keeps a stable view with the fix, and the same
sequence WITHOUT the SET demonstrably moves (proving the test would catch the old code).
SKIP only when Docker/images/drivers are unavailable (or SNAPISO_SKIP_DOCKER=1). Container
names: $SNAPISO_NAME_PREFIX (default "snapiso-it") + random suffix; random loopback ports.

Run: python3 -B scripts/test_snapshot_isolation.py
"""
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
sys.dont_write_bytecode = True
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import cross_engine_compare as cec  # noqa: E402
import soak_check  # noqa: E402
import soak_check_lambda as L  # noqa: E402

RR = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"


class RecordingConn:
    """pymysql-shaped connection that records statements."""

    def __init__(self):
        self.executed = []

    def cursor(self):
        conn = self

        class C:
            description = None

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                conn.executed.append(sql)

            def fetchall(s):
                return []

            def close(s):
                pass
        return C()


class StatementSequenceTests(unittest.TestCase):
    def test_constants_identical_in_all_three_scripts(self):
        self.assertEqual(cec.MYSQL_SNAPSHOT_ISOLATION, RR)
        self.assertEqual(L.MYSQL_SNAPSHOT_ISOLATION, RR)
        self.assertEqual(soak_check.MYSQL_SNAPSHOT_ISOLATION, RR)

    def test_cross_engine_mysql_side_sets_rr_before_snapshot(self):
        conn = RecordingConn()
        cec.SqlSide("mysql", conn).begin(30)
        ex = conn.executed
        self.assertIn(RR, ex)
        start = ex.index("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        self.assertLess(ex.index(RR), start)

    def test_cross_engine_pg_side_unchanged(self):
        class PgConn:
            autocommit = False

            def __init__(self):
                self.executed = []

            def run(self, sql):
                self.executed.append(sql)
                return []
        conn = PgConn()
        cec.SqlSide("postgres", conn).begin(5)
        self.assertEqual(conn.executed[-1], "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertNotIn(RR, conn.executed)

    def test_lambda_start_consistent_read_sets_rr_first(self):
        conn = RecordingConn()
        L._start_consistent_read("mysql", conn)
        self.assertEqual(conn.executed, [RR, "START TRANSACTION WITH CONSISTENT SNAPSHOT"])

    def test_standalone_batch_starts_with_rr_then_snapshot(self):
        seen = []

        def fake(family, conn, sqls, headers=False, timeout=None):
            seen.append(list(sqls))
            out = []
            for q in sqls:
                up = q.upper()
                if up.startswith("SELECT COUNT(*)"):
                    out.append(["10"])
                elif up.startswith("CHECKSUM TABLE"):
                    out.append(["shop.orders\t123"])
                elif up.startswith("SELECT COLUMN_NAME"):
                    out.append(["id\tbigint\tNO\tNULL"])
                else:
                    out.append([])
            return out
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
               "tables": ["shop.orders"], "checksum_tables": ["shop.orders"], "watermark": {"enabled": False}}
        with mock.patch.object(soak_check, "run_batch", side_effect=fake):
            day = soak_check.run_day(cfg)
        batches = [b for b in seen if any("CONSISTENT SNAPSHOT" in q for q in b)]
        self.assertEqual(len(batches), 2)                      # one per side
        for b in batches:
            self.assertEqual(b[:2], [RR, "START TRANSACTION WITH CONSISTENT SNAPSHOT"])
            self.assertEqual(b[-1], "COMMIT")
        # results are still mapped to the right statements after the extra leading SET
        self.assertEqual(day["detail"]["row_count"]["shop.orders"]["source"], 10)
        self.assertIs(day["checks"]["row_count"], True)
        self.assertIs(day["checks"]["checksum"], True)

    def test_allow_list_accepts_exactly_this_set(self):
        cec.assert_read_only(RR)
        cec.assert_read_only(RR.lower())
        for bad in ("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
                    "SET SESSION TRANSACTION READ WRITE",
                    "SET GLOBAL TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE",
                    RR + "; DROP TABLE x",
                    RR + ", READ WRITE",
                    "SET SESSION sql_log_bin = 0"):
            with self.assertRaises(cec.CompareError, msg=bad):
                cec.assert_read_only(bad)


# ── Docker integration ──────────────────────────────────────────────────────────────────
def _docker_problem():
    if os.environ.get("SNAPISO_SKIP_DOCKER"):
        return "SNAPISO_SKIP_DOCKER set"
    if not shutil.which("docker"):
        return "docker not installed"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return "docker daemon not reachable"
        for img in ("mysql:8.0", "postgres:16"):
            if subprocess.run(["docker", "image", "inspect", img], capture_output=True, timeout=20).returncode != 0:
                return f"image {img} not present locally"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker unavailable: {e}"
    try:
        import pymysql  # noqa: F401
        import pg8000  # noqa: F401
    except ImportError as e:
        return f"python driver missing: {e}"
    return None


DOCKER_SKIP = _docker_problem()
N_ROWS = 2000


class Env:
    started = False
    my_id = pg_id = None
    my_port = pg_port = None
    tmp = None
    pw = secrets.token_hex(12)


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _port(cid, port):
    return int(_run(["docker", "port", cid, f"{port}/tcp"]).stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def my(db=None, autocommit=True):
    import pymysql
    return pymysql.connect(host="127.0.0.1", port=Env.my_port, user="root", password=Env.pw, database=db,
                           autocommit=autocommit, connect_timeout=5)


def pg():
    import pg8000.native as pgn
    return pgn.Connection(host="127.0.0.1", port=Env.pg_port, user="postgres", password=Env.pw,
                          database="postgres", timeout=20)


def start_env():
    if Env.started:
        return
    import tempfile
    Env.tmp = Path(tempfile.mkdtemp(prefix="snapiso-"))
    envfile = Env.tmp / "db.env"
    envfile.write_text(f"MYSQL_ROOT_PASSWORD={Env.pw}\nPOSTGRES_PASSWORD={Env.pw}\n")
    envfile.chmod(0o600)
    prefix = os.environ.get("SNAPISO_NAME_PREFIX", "snapiso-it")
    sfx = secrets.token_hex(3)
    Env.started = True
    Env.my_id = _run(["docker", "run", "-d", "--rm", "--name", f"{prefix}-my-{sfx}", "--env-file", str(envfile),
                      "-p", "127.0.0.1::3306", "mysql:8.0", "--transaction-isolation=READ-COMMITTED"]).stdout.strip()
    Env.pg_id = _run(["docker", "run", "-d", "--rm", "--name", f"{prefix}-pg-{sfx}", "--env-file", str(envfile),
                      "-p", "127.0.0.1::5432", "postgres:16"]).stdout.strip()
    if not Env.my_id or not Env.pg_id:
        raise RuntimeError("could not start containers")
    Env.my_port, Env.pg_port = _port(Env.my_id, 3306), _port(Env.pg_id, 5432)
    deadline = time.time() + 180
    while True:
        try:
            my().close()
            pg().close()
            break
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(2)
    m = my()
    with m.cursor() as c:
        c.execute("CREATE DATABASE ledger")
        c.execute("CREATE TABLE ledger.accounts (id BIGINT NOT NULL PRIMARY KEY, balance DECIMAL(20,6) NOT NULL)")
        c.executemany("INSERT INTO ledger.accounts VALUES (%s, %s)",
                      [(i, Decimal(i) / Decimal(4)) for i in range(1, N_ROWS + 1)])
    m.close()


def stop_env():
    for cid in (Env.my_id, Env.pg_id):
        if cid:
            _run(["docker", "rm", "-f", cid])
    if Env.tmp:
        shutil.rmtree(Env.tmp, ignore_errors=True)


def tearDownModule():
    if Env.started:
        stop_env()


def reset_pg_and_mysql():
    """Same data on both engines (the 'replicated' state before the scan)."""
    m = my()
    with m.cursor() as c:
        c.execute("UPDATE ledger.accounts SET balance = id / 4")
    m.close()
    p = pg()
    p.run("CREATE SCHEMA IF NOT EXISTS ledger")
    p.run("DROP TABLE IF EXISTS ledger.accounts")
    p.run("CREATE TABLE ledger.accounts (id BIGINT NOT NULL PRIMARY KEY, balance NUMERIC(20,6) NOT NULL)")
    p.run(f"INSERT INTO ledger.accounts SELECT i, i::numeric / 4 FROM generate_series(1, {N_ROWS}) i")
    p.close()


def write_both():
    """The app writes AND replication applies them (zero lag): both engines change after the
    compare's snapshots were taken — a correct snapshot compare must still see equal data."""
    m = my()
    with m.cursor() as c:
        c.execute("UPDATE ledger.accounts SET balance = balance + 1")
    m.close()
    p = pg()
    p.run("UPDATE ledger.accounts SET balance = balance + 1")
    p.close()


@unittest.skipIf(DOCKER_SKIP, f"integration skipped: {DOCKER_SKIP}")
class ReadCommittedServerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_env()

    def test_00_server_default_really_is_read_committed(self):
        m = my()
        with m.cursor() as c:
            c.execute("SELECT @@GLOBAL.transaction_isolation, @@SESSION.transaction_isolation")
            self.assertEqual(c.fetchall()[0], ("READ-COMMITTED", "READ-COMMITTED"))
        m.close()

    def _view_moves(self, start_statements):
        """Opens a 'snapshot' with start_statements, reads, lets a writer thread commit on a
        separate connection, reads again. True if the second read saw the write."""
        reader = my("ledger", autocommit=True)
        with reader.cursor() as c:
            for s in start_statements:
                c.execute(s)
            c.execute("SELECT SUM(balance), MAX(id) FROM accounts")
            before = c.fetchall()[0]

            def writer():
                w = my("ledger")
                with w.cursor() as wc:
                    wc.execute("UPDATE accounts SET balance = balance + 1 WHERE id <= 10")
                    wc.execute(f"INSERT INTO accounts VALUES ({N_ROWS + 1000 + int(time.time() * 1000) % 100000}, 5)")
                w.close()
            th = threading.Thread(target=writer)
            th.start()
            th.join(30)
            c.execute("SELECT SUM(balance), MAX(id) FROM accounts")
            after = c.fetchall()[0]
            c.execute("COMMIT")
        reader.close()
        return before != after

    def test_old_sequence_is_a_moving_view_under_read_committed(self):
        # what all three readers did before the fix
        self.assertTrue(self._view_moves(["START TRANSACTION WITH CONSISTENT SNAPSHOT"]))
        self.assertTrue(self._view_moves(["START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"]))

    def test_rr_first_pins_the_view(self):
        self.assertFalse(self._view_moves([RR, "START TRANSACTION WITH CONSISTENT SNAPSHOT"]))
        self.assertFalse(self._view_moves([RR, "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"]))

    def test_lambda_reader_is_pinned(self):
        reader = my("ledger")
        try:
            L._start_consistent_read("mysql", reader)
            before = L._query("mysql", reader, "SELECT SUM(balance), COUNT(*) FROM accounts")[0]
            th = threading.Thread(target=lambda: write_one())
            th.start()
            th.join(30)
            after = L._query("mysql", reader, "SELECT SUM(balance), COUNT(*) FROM accounts")[0]
            L._end_consistent_read("mysql", reader)
            fresh = L._query("mysql", reader, "SELECT SUM(balance), COUNT(*) FROM accounts")[0]
        finally:
            reader.close()
        self.assertEqual(before, after)        # stable inside the run
        self.assertNotEqual(before, fresh)     # ...and the write really happened

    def test_standalone_mysql_batch_is_pinned(self):
        if not shutil.which("mysql"):
            self.skipTest("mysql client not installed")   # soak_check.py's own prerequisite
        os.environ["SNAPISO_PW"] = Env.pw
        conn = {"engine": "mysql", "host": "127.0.0.1", "port": Env.my_port, "user": "root",
                "database": "ledger", "password_env": "SNAPISO_PW", "ssl_insecure": True}
        q = "SELECT SUM(balance) FROM accounts"

        def batch(start):
            th = threading.Timer(1.0, write_one)
            th.start()
            try:
                out = soak_check.run_batch("mysql", conn, start + [q, "SELECT SLEEP(2.5)", q, "COMMIT"])
            finally:
                th.join(30)
            n = len(start)
            return out[n][0], out[n + 2][0]
        a, b = batch(soak_check.mysql_snapshot_start())
        self.assertEqual(a, b)
        a, b = batch(["START TRANSACTION WITH CONSISTENT SNAPSHOT"])    # old batch head
        self.assertNotEqual(a, b)

    def _cross_engine(self, drop_rr):
        """MySQL (READ-COMMITTED server) -> PostgreSQL compare with a write committed on BOTH
        engines after the first source chunk: equal data at snapshot time, equal data after."""
        import pymysql
        import pg8000.native as pgn
        reset_pg_and_mysql()
        fired = []

        class Src(cec.SqlSide):
            def query(self, sql):
                if drop_rr and sql == cec.MYSQL_SNAPSHOT_ISOLATION:
                    return []                                   # emulate the pre-fix begin()
                rows = super().query(sql)
                if " LIMIT " in sql and "information_schema" not in sql and not fired:   # first data chunk
                    fired.append(1)
                    th = threading.Thread(target=write_both)    # separate connections, committed
                    th.start()
                    th.join(60)
                return rows
        s = Src("mysql", pymysql.connect(host="127.0.0.1", port=Env.my_port, user="root", password=Env.pw,
                                         database="ledger", autocommit=True), "source")
        t = cec.SqlSide("postgres", pgn.Connection(host="127.0.0.1", port=Env.pg_port, user="postgres",
                                                   password=Env.pw, database="postgres"), "target")
        try:
            rep = cec.compare_tables(s, t, ["ledger.accounts"], {"chunk_size": 100, "max_seconds": 120})
        finally:
            s.conn.close()
            t.conn.close()
        self.assertTrue(fired)
        return rep["tables"]["ledger.accounts"]

    def test_cross_engine_no_false_mismatch_with_fix(self):
        r = self._cross_engine(drop_rr=False)
        self.assertEqual(r["verdict"], "MATCH", r["mismatches"])
        self.assertEqual(r["rows"], {"source": N_ROWS, "target": N_ROWS})

    def test_cross_engine_old_begin_reports_spurious_mismatches(self):
        r = self._cross_engine(drop_rr=True)
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["differing_columns"].get("balance"), N_ROWS - 100)


def write_one():
    m = my("ledger")
    with m.cursor() as c:
        c.execute("UPDATE accounts SET balance = balance + 1 WHERE id = 1")
    m.close()


if __name__ == "__main__":
    unittest.main()
