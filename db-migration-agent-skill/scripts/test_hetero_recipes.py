#!/usr/bin/env python3
"""Live proofs for the MySQL <-> PostgreSQL recipes in shared/reference/mysql-postgresql.md.

Every test runs against real mysql:8.0 and postgres:16 containers (random loopback ports) and
pins one statement the reference makes, so a wrong recipe fails here instead of in a customer
run. They exercise the database engines, NOT AWS DMS: where DMS behaviour matters, the test
replays the value shape the DMS documentation (and the dry runs) show — e.g. PostgreSQL
BOOLEAN arriving as the text 'true'/'false' (DMS type CHAR(5)) — through the same MySQL load
path (LOAD DATA LOCAL) a DMS MySQL target uses.

  - partial unique index -> generated column + UNIQUE: rows OUTSIDE the predicate must be
    allowed to share the key (generated key NULL for them); the boolean mapped through an
    integer cast of 'true'/'false' (the int1 change-data-type defect of the dry run) silently
    drops exactly the rows outside the predicate under LOAD DATA LOCAL (IGNORE semantics)
  - assessment queries A10 (MySQL) / P9 (PostgreSQL) and the §4.7 post-load non-null generators: LOB-class columns on tables without a
    primary or unique key
  - citext compared with a text-typed value is case-sensitive; MySQL ENUM sorts by index
  - GENERATED ALWAYS AS IDENTITY rejects explicit ids (DMS apply); BY DEFAULT accepts them
  - PostgreSQL triggers fire for an apply session unless session_replication_role = replica;
    a MySQL trigger fires for every writer, so the guard must test USER(), not CURRENT_USER()
  - MySQL cascades are not binlogged (trigger-written child rows are) and the replica role skips
    PG FK actions, so the §10 R1 orphan check finds the children a reverse apply leaves behind
  - super_read_only blocks ALTER USER even for root; the fence that keeps the reverse target
    writable for DMS is ACCOUNT LOCK on the application accounts

Tests SKIP (never fail) when Docker, the images or the drivers are unavailable; set
CEC_SKIP_DOCKER=1 to skip them explicitly. Containers are always removed.
Run: python3 -B -m unittest discover -s scripts -p 'test_hetero_recipes.py'
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "shared" / "reference" / "mysql-postgresql.md"


def _docker_skip_reason() -> str | None:
    if os.environ.get("CEC_SKIP_DOCKER"):
        return "CEC_SKIP_DOCKER set"
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
        import pg8000  # noqa: F401
        import pymysql  # noqa: F401
    except ImportError as e:
        return f"python driver missing: {e}"
    return None


SKIP = _docker_skip_reason()


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Env:
    started = False
    tmp: Path | None = None
    mysql_id = pg_id = ""
    mysql_port = pg_port = 0
    pw = secrets.token_hex(12)


def _port(cid: str, port: int) -> int:
    return int(_run(["docker", "port", cid, f"{port}/tcp"]).stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _insecure() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def my(user: str = "root", pw: str | None = None, db: str | None = None, local_infile: bool = False):
    import pymysql
    return pymysql.connect(host="127.0.0.1", port=Env.mysql_port, user=user, password=pw or Env.pw, database=db,
                           charset="utf8mb4", autocommit=True, connect_timeout=5, local_infile=local_infile)


def pg(db: str = "postgres", user: str = "postgres", pw: str | None = None):
    import pg8000.native as pgn
    return pgn.Connection(host="127.0.0.1", port=Env.pg_port, user=user, password=pw or Env.pw, database=db,
                          timeout=15)


def setUpModule() -> None:
    if SKIP:
        return
    Env.tmp = Path(tempfile.mkdtemp(prefix="hetero-recipes-"))
    envfile = Env.tmp / "db.env"
    envfile.write_text(f"MYSQL_ROOT_PASSWORD={Env.pw}\nPOSTGRES_PASSWORD={Env.pw}\n")
    envfile.chmod(0o600)
    tag = secrets.token_hex(4)
    Env.started = True
    Env.mysql_id = _run(["docker", "run", "-d", "--rm", "--name", f"hetero-recipes-my-{tag}", "--env-file", str(envfile),
                         "-p", "127.0.0.1::3306", "mysql:8.0", "--local-infile=1",
                         "--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci"]).stdout.strip()
    Env.pg_id = _run(["docker", "run", "-d", "--rm", "--name", f"hetero-recipes-pg-{tag}", "--env-file", str(envfile),
                      "-p", "127.0.0.1::5432", "postgres:16"]).stdout.strip()
    if not Env.mysql_id or not Env.pg_id:
        raise RuntimeError("could not start containers")
    Env.mysql_port, Env.pg_port = _port(Env.mysql_id, 3306), _port(Env.pg_id, 5432)
    deadline = time.time() + 180
    while True:
        try:
            my().close()
            pg().close()
            break
        except Exception:  # noqa: BLE001 - containers still starting
            if time.time() > deadline:
                raise
            time.sleep(2)
    with my() as c, c.cursor() as cur:
        cur.execute("CREATE DATABASE billing")
    con = pg()
    con.run("CREATE DATABASE ledger")
    con.close()


def tearDownModule() -> None:
    if not Env.started:
        return
    for cid in (Env.mysql_id, Env.pg_id):
        if cid:
            _run(["docker", "rm", "-f", cid])
    if Env.tmp:
        shutil.rmtree(Env.tmp, ignore_errors=True)


def _ref_sql(label: str) -> str:
    """The reference's own query block, so the test proves what the document says."""
    text = REF.read_text()
    m = re.search(rf"^-- {re.escape(label)}\..*?\n(?=-- [A-Z]\d+\.|```)", text, re.S | re.M)
    if not m:
        raise AssertionError(f"{label} query not found in {REF.name}")
    return "\n".join(l for l in m.group(0).splitlines() if not l.lstrip().startswith("--")).strip()


# The recipe from mysql-postgresql.md §4.3 (partial unique index): the generated key is NULL
# whenever the predicate is false, and MySQL UNIQUE permits any number of NULLs.
INVOICES_MYSQL = """CREATE TABLE billing.invoices_{s} (
  id BIGINT PRIMARY KEY,
  account_id BIGINT NOT NULL,
  invoice_no VARCHAR(32) NOT NULL,
  is_void TINYINT(1) NOT NULL,
  open_invoice_no VARCHAR(32) GENERATED ALWAYS AS (CASE WHEN is_void = 0 THEN invoice_no END) STORED,
  UNIQUE KEY uq_invoices_open_no (account_id, open_invoice_no))"""


@unittest.skipIf(SKIP, SKIP or "")
class PartialUniqueIndexRecipe(unittest.TestCase):
    """OBS-18: PG `UNIQUE (account_id, invoice_no) WHERE NOT is_void` on MySQL."""

    def _rows(self, n_accounts: int = 50, open_first: bool = True) -> list[tuple[int, int, str, bool]]:
        """Per account: one OPEN invoice and three VOID invoices sharing its number. ids
        ascend in load order; `open_first` puts the open row first (lowest id)."""
        rows, rid = [], 0
        for acct in range(1, n_accounts + 1):
            flags = [False, True, True, True] if open_first else [True, True, True, False]
            for void in flags:
                rid += 1
                rows.append((rid, acct, f"INV-{acct}", void))
        return rows

    def test_recipe_allows_rows_outside_the_predicate(self):
        with my(db="billing") as c, c.cursor() as cur:
            cur.execute(INVOICES_MYSQL.format(s="direct"))
            cur.executemany("INSERT INTO billing.invoices_direct (id, account_id, invoice_no, is_void) VALUES (%s,%s,%s,%s)",
                            [(i, a, n, int(v)) for i, a, n, v in self._rows(5)])
            cur.execute("SELECT COUNT(*), SUM(open_invoice_no IS NULL), SUM(is_void) FROM billing.invoices_direct")
            self.assertEqual(tuple(int(x) for x in cur.fetchone()), (20, 15, 15))
            import pymysql
            with self.assertRaises(pymysql.err.IntegrityError):   # a second OPEN row with the key is still rejected
                cur.execute("INSERT INTO billing.invoices_direct (id, account_id, invoice_no, is_void) VALUES (999, 1, 'INV-1', 0)")
            cur.execute("INSERT INTO billing.invoices_direct (id, account_id, invoice_no, is_void) VALUES (998, 1, 'INV-1', 1)")

    def test_postgres_source_accepts_the_same_rows(self):
        con = pg("ledger")
        try:
            con.run("CREATE TABLE inv_src (id bigint PRIMARY KEY, account_id bigint NOT NULL, invoice_no text NOT NULL, is_void boolean NOT NULL)")
            con.run("CREATE UNIQUE INDEX uq_open ON inv_src (account_id, invoice_no) WHERE NOT is_void")
            for r in self._rows(5):
                con.run("INSERT INTO inv_src VALUES (:i, :a, :n, :v)", i=r[0], a=r[1], n=r[2], v=r[3])
            self.assertEqual(con.run("SELECT count(*) FROM inv_src")[0][0], 20)
        finally:
            con.close()

    def _load(self, table: str, bool_expr: str, rows: list[tuple[int, int, str, bool]]) -> tuple[int, int, set[int]]:
        """LOAD DATA LOCAL (the DMS MySQL-target full-load path) with booleans as DMS sends them:
        the PostgreSQL text form 'true'/'false' (DMS type CHAR(5)). Returns (rows, sum(is_void),
        surviving ids)."""
        csv = Env.tmp / f"{table}.csv"
        csv.write_text("".join(f"{i},{a},{n},{'true' if v else 'false'}\n" for i, a, n, v in rows))
        with my(db="billing", local_infile=True) as c, c.cursor() as cur:
            cur.execute(INVOICES_MYSQL.replace("invoices_{s}", table))
            cur.execute(f"LOAD DATA LOCAL INFILE '{csv}' INTO TABLE billing.{table} FIELDS TERMINATED BY ',' "
                        f"(id, account_id, invoice_no, @v) SET is_void = {bool_expr}")
            cur.execute(f"SELECT COUNT(*), COALESCE(SUM(is_void), 0) FROM billing.{table}")
            n, v = (int(x) for x in cur.fetchone())
            cur.execute(f"SELECT id FROM billing.{table}")
            return n, v, {int(r[0]) for r in cur.fetchall()}

    def test_integer_cast_of_text_boolean_silently_drops_the_void_rows(self):
        # The dry-run defect: 'true' -> integer 0 (int1 change-data-type on DMS's CHAR(5)
        # value). Every void row now claims the open key, collides, and LOCAL = IGNORE drops it
        # with a warning only — no error, the load "succeeds". With the open invoice loaded
        # first (lowest id, as in the run's data), the survivors are exactly the rows INSIDE the
        # predicate and every dropped row is one OUTSIDE it.
        rows = self._rows(50, open_first=True)
        open_ids = {r[0] for r in rows if not r[3]}
        void_ids = {r[0] for r in rows if r[3]}
        got_rows, got_voids, survivors = self._load("invoices_int_cast", "CAST(@v AS SIGNED)", rows)
        self.assertEqual(got_voids, 0, "every boolean became 0")
        self.assertEqual(survivors, open_ids, "survivors are exactly the open (inside-predicate) invoices")
        self.assertEqual({r[0] for r in rows} - survivors, void_ids, "dropped = exactly the void (outside-predicate) rows")

    def test_which_rows_vanish_depends_on_load_order(self):
        # Same defect, void rows loaded first: still short by the void count, but the FIRST row
        # per key survives — a formerly-void invoice now looks open and the real open invoice is
        # gone. Count loss is order-independent; WHICH rows vanish is not, so the proof query
        # must compare per-key predicate membership, never the count alone.
        rows = self._rows(50, open_first=False)
        open_ids = {r[0] for r in rows if not r[3]}
        first_void_per_acct = {min(r[0] for r in rows if r[1] == a and r[3]) for a in range(1, 51)}
        got_rows, _, survivors = self._load("invoices_int_cast_rev", "CAST(@v AS SIGNED)", rows)
        self.assertEqual(len(rows) - got_rows, sum(1 for r in rows if r[3]))
        self.assertEqual(survivors, first_void_per_acct)
        self.assertFalse(survivors & open_ids, "every real open invoice was dropped")

    def test_boolean_conversion_keeps_every_row(self):
        rows = self._rows(50)
        got_rows, got_voids, survivors = self._load("invoices_bool", "(@v = 'true')", rows)
        self.assertEqual((got_rows, got_voids), (len(rows), sum(1 for r in rows if r[3])))
        self.assertEqual(survivors, {r[0] for r in rows})


@unittest.skipIf(SKIP, SKIP or "")
class LobWithoutKeyAssessment(unittest.TestCase):
    """OBS-30/10: the reference's A10 / P9 queries list exactly the tables DMS will strip LOBs from."""

    def test_mysql_a10_lists_lob_tables_without_pk_or_uk(self):
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE lobs")
            for ddl in ("CREATE TABLE lobs.audit_log (actor VARCHAR(64), payload JSON, created_at DATETIME)",
                        "CREATE TABLE lobs.notes (body LONGTEXT, img MEDIUMBLOB)",
                        "CREATE TABLE lobs.short_text (body TEXT)",
                        "CREATE TABLE lobs.with_pk (id INT PRIMARY KEY, payload JSON)",
                        "CREATE TABLE lobs.with_uk (code VARCHAR(10) NOT NULL, doc LONGTEXT, UNIQUE KEY (code))",
                        "CREATE TABLE lobs.no_lob (a INT, b VARCHAR(10))"):
                cur.execute(ddl)
            cur.execute(_ref_sql("A10").replace("NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')",
                                                "= 'lobs'"))
            got = {r[1]: r for r in cur.fetchall()}
        self.assertEqual(set(got), {"audit_log", "notes", "short_text"})
        self.assertIn("payload json", got["audit_log"][2].lower())
        self.assertEqual(int(got["audit_log"][3]), 1)     # JSON is a DMS LOB (CLOB)
        self.assertEqual(int(got["short_text"][3]), 0)    # TEXT maps to WSTRING per the DMS doc: check, not blocker

    def test_postgres_p9_lists_lob_tables_without_pk_or_uk(self):
        con = pg("ledger")
        try:
            for ddl in ("CREATE SCHEMA billing",
                        "CREATE TABLE billing.payment_events (invoice_id bigint, kind text, payload jsonb)",
                        "CREATE TABLE billing.tags (labels text[])",
                        "CREATE TABLE billing.keyed (id bigint PRIMARY KEY, payload jsonb)",
                        "CREATE TABLE billing.unique_keyed (code varchar(10) NOT NULL UNIQUE, body text)",
                        "CREATE TABLE billing.narrow (a int, b varchar(10))",
                        "CREATE TABLE billing.free_text (note varchar)"):
                con.run(ddl)
            rows = con.run(_ref_sql("P9").replace("NOT IN ('pg_catalog', 'information_schema')", "= 'billing'"))
        finally:
            con.close()
        got = {r[1]: r for r in rows}
        self.assertEqual(set(got), {"payment_events", "tags", "free_text"})
        self.assertIn("payload jsonb", got["payment_events"][2])
        self.assertIn("kind text", got["payment_events"][2])
        # confirmed blockers vs conditional checks (unbounded varchar = WSTRING by default)
        self.assertEqual({t: r[4] for t, r in got.items()}, {"payment_events": True, "tags": True, "free_text": False})


    def test_post_load_non_null_generators_cover_the_assessed_inventory(self):
        # Review r6 #7: the generators run on the SOURCE over the A10/P9 inventory (blockers and
        # conditional checks, varchar included) and emit a runnable statement for each side.
        block = REF.read_text().split("Mandatory post-full-load quick check", 1)[1].split("```sql", 1)[1].split("```", 1)[0]
        my_gen, pg_gen = (part.strip() for part in block.split("-- PostgreSQL source"))
        strip = lambda q: "\n".join(l for l in q.splitlines() if not l.lstrip().startswith("--")).strip().rstrip(";")
        my_gen = strip(my_gen).replace("IN ('shop')", "IN ('lobs2')")
        pg_gen = strip("-- " + pg_gen).replace("IN ('shop')", "IN ('lobs2')")
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE lobs2")
            cur.execute("CREATE TABLE lobs2.audit_log (actor VARCHAR(64), payload JSON, note TEXT)")
            cur.execute("INSERT INTO lobs2.audit_log VALUES ('a', '{}', 'x'), ('b', NULL, NULL)")
            cur.execute("CREATE TABLE lobs2.keyed (id INT PRIMARY KEY, payload JSON)")
            cur.execute("INSERT INTO lobs2.keyed VALUES (1, '{}'), (2, NULL)")
            # Review r7 #2: the surrogate-key remedy must not drop the repaired table from the check
            cur.execute("ALTER TABLE lobs2.audit_log ADD COLUMN id BIGINT AUTO_INCREMENT PRIMARY KEY")
            cur.execute("CREATE TABLE lobs2.payment_events (kind TEXT, payload JSON, tags TEXT, memo TEXT)")
            cur.execute("INSERT INTO lobs2.payment_events VALUES ('paid', '{}', '{a}', 'm'), (NULL, NULL, NULL, NULL)")
            cur.execute(my_gen)
            my_rows = cur.fetchall()
            my_src = {r[0]: tuple(int(x) for x in self._one(cur, r[2])) for r in my_rows}
            pg_tgt_for_my = {r[0]: r[3] for r in my_rows}
        con = pg("ledger")
        try:
            con.run("CREATE SCHEMA lobs2")
            con.run("CREATE TABLE lobs2.audit_log (actor varchar(64), payload jsonb, note text)")
            con.run("INSERT INTO lobs2.audit_log VALUES ('a', '{}', 'x'), ('b', NULL, NULL)")
            con.run("ALTER TABLE lobs2.audit_log ADD COLUMN id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
            con.run("CREATE TABLE lobs2.keyed (id int PRIMARY KEY, payload jsonb)")
            con.run("INSERT INTO lobs2.keyed VALUES (1, '{}'), (2, NULL)")
            con.run("CREATE TABLE lobs2.payment_events (kind text, payload jsonb, tags text[], memo varchar)")
            con.run("INSERT INTO lobs2.payment_events VALUES ('paid', '{}', '{a}', 'm'), (NULL, NULL, NULL, NULL)")
            pg_from_my = {col: tuple(con.run(stmt)[0]) for col, stmt in pg_tgt_for_my.items()}
            pg_rows = con.run(pg_gen)
            pg_src = {r[0]: tuple(con.run(r[2])[0]) for r in pg_rows}
        finally:
            con.close()
        with my() as c, c.cursor() as cur:
            my_from_pg = {r[0]: tuple(int(x) for x in self._one(cur, r[3])) for r in pg_rows}
        self.assertEqual(set(my_src), {"lobs2.audit_log.payload", "lobs2.audit_log.note", "lobs2.keyed.payload",
                                       "lobs2.payment_events.kind", "lobs2.payment_events.payload",
                                       "lobs2.payment_events.tags", "lobs2.payment_events.memo"},
                         "every LOB-class column regardless of current keys (repaired audit_log stays); TEXT included")
        self.assertEqual(my_src, pg_from_my, "each generated target statement runs on PG and sees the same counts")
        self.assertIn("lobs2.payment_events.memo", pg_src, "unbounded varchar (conditional check) is covered")
        self.assertEqual(set(pg_src), {"lobs2.audit_log.note", "lobs2.audit_log.payload", "lobs2.keyed.payload",
                                       "lobs2.payment_events.kind", "lobs2.payment_events.payload",
                                       "lobs2.payment_events.tags", "lobs2.payment_events.memo"})
        self.assertTrue(all(v == (2, 1) for v in list(pg_src.values()) + list(my_from_pg.values())))

    @staticmethod
    def _one(cur, stmt: str):
        cur.execute(stmt)
        return cur.fetchone()


@unittest.skipIf(SKIP, SKIP or "")
class ApplicationSemantics(unittest.TestCase):
    """OBS-19: the two app-checklist rows added from the mysql2pg run."""

    def test_citext_against_text_typed_value_is_case_sensitive(self):
        con = pg("ledger")
        try:
            con.run("CREATE EXTENSION IF NOT EXISTS citext")
            con.run("CREATE TABLE cust (email citext PRIMARY KEY)")
            con.run("INSERT INTO cust VALUES ('larry@example.com')")
            q = "SELECT count(*) FROM cust WHERE email = {}"
            self.assertEqual(con.run(q.format("'LARRY@example.com'"))[0][0], 1)              # untyped literal -> citext
            self.assertEqual(con.run(q.format("CAST(:e AS citext)"), e="LARRY@example.com")[0][0], 1)
            self.assertEqual(con.run(q.format("CAST(:e AS text)"), e="LARRY@example.com")[0][0], 0)
            self.assertEqual(con.run(q.format("CAST(:e AS varchar)"), e="LARRY@example.com")[0][0], 0)
            self.assertEqual(con.run("SELECT count(*) FROM cust WHERE email LIKE CAST(:e AS text)", e="LARRY%")[0][0], 1)
        finally:
            con.close()

    def test_enum_order_by_is_definition_order_in_mysql(self):
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS sem")
            cur.execute("CREATE TABLE sem.o (s ENUM('pending','paid','shipped','delivered','cancelled'))")
            cur.execute("INSERT INTO sem.o VALUES ('cancelled'),('paid'),('pending'),('shipped'),('delivered')")
            cur.execute("SELECT s FROM sem.o ORDER BY s")
            mysql_order = [r[0] for r in cur.fetchall()]
        con = pg("ledger")
        try:
            con.run("CREATE TABLE o (s varchar(16) CHECK (s IN ('pending','paid','shipped','delivered','cancelled')))")
            con.run("INSERT INTO o VALUES ('cancelled'),('paid'),('pending'),('shipped'),('delivered')")
            pg_order = [r[0] for r in con.run("SELECT s FROM o ORDER BY s COLLATE \"C\"")]
            fixed = [r[0] for r in con.run("SELECT s FROM o ORDER BY array_position("
                                           "ARRAY['pending','paid','shipped','delivered','cancelled']::varchar[], s)")]
        finally:
            con.close()
        self.assertEqual(mysql_order, ["pending", "paid", "shipped", "delivered", "cancelled"])
        self.assertEqual(pg_order, sorted(mysql_order))
        self.assertEqual(fixed, mysql_order)


@unittest.skipIf(SKIP, SKIP or "")
class ReverseApplyPrerequisites(unittest.TestCase):
    """OBS-16/23/24/3: identity mode, trigger behaviour under apply, and the freeze fence."""

    def test_generated_always_identity_rejects_explicit_ids(self):
        import pg8000
        con = pg("ledger")
        try:
            con.run("CREATE TABLE ga (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text)")
            con.run("CREATE TABLE gbd (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v text)")
            with self.assertRaises(pg8000.exceptions.DatabaseError):
                con.run("INSERT INTO ga (id, v) VALUES (42, 'x')")       # what a DMS apply INSERT does
            con.run("INSERT INTO gbd (id, v) VALUES (42, 'x')")
            con.run("ALTER TABLE ga ALTER COLUMN id SET GENERATED BY DEFAULT")   # the A2 remedy (catalog-only)
            con.run("INSERT INTO ga (id, v) VALUES (42, 'x')")
            self.assertEqual(con.run("SELECT count(*) FROM ga")[0][0], 1)
        finally:
            con.close()

    def test_postgres_trigger_fires_for_apply_unless_replica_role(self):
        con = pg("ledger")
        try:
            con.run("CREATE TABLE inv (id bigint PRIMARY KEY, status text)")
            con.run("CREATE TABLE inv_audit (id bigint, status text)")
            con.run("CREATE FUNCTION f_audit() RETURNS trigger LANGUAGE plpgsql AS "
                    "$$BEGIN INSERT INTO inv_audit VALUES (NEW.id, NEW.status); RETURN NEW; END$$")
            con.run("CREATE TRIGGER t_audit AFTER INSERT OR UPDATE ON inv FOR EACH ROW EXECUTE FUNCTION f_audit()")
            con.run("INSERT INTO inv VALUES (1, 'open')")                  # default role 'origin': fires
            con.run("SET session_replication_role = replica")             # AfterConnectScript on the DMS endpoint
            con.run("INSERT INTO inv VALUES (2, 'open')")
            con.run("UPDATE inv SET status = 'paid' WHERE id = 1")
            self.assertEqual(con.run("SELECT count(*) FROM inv_audit")[0][0], 1)
            con.run("RESET session_replication_role")
        finally:
            con.close()

    def test_mysql_trigger_guard_must_use_user_not_current_user(self):
        import pymysql
        rev_pw = secrets.token_hex(8)
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS shop")
            cur.execute("CREATE TABLE shop.orders (id INT PRIMARY KEY, status VARCHAR(16))")
            cur.execute("CREATE TABLE shop.audit_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, order_id INT, actor VARCHAR(128))")
            cur.execute(f"CREATE USER 'rev_writer'@'%' IDENTIFIED BY '{rev_pw}'")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'rev_writer'@'%'")
            # CURRENT_USER() inside a trigger is the DEFINER (root) for every writer, so it can't
            # tell the reverse writer apart; USER() is the connecting client.
            cur.execute("CREATE TRIGGER shop.trg_audit AFTER UPDATE ON shop.orders FOR EACH ROW BEGIN "
                        "IF SUBSTRING_INDEX(USER(), '@', 1) <> 'rev_writer' THEN "
                        "INSERT INTO shop.audit_log (order_id, actor) VALUES (NEW.id, CURRENT_USER()); END IF; END")
            cur.execute("INSERT INTO shop.orders VALUES (1, 'pending'), (2, 'pending')")
            cur.execute("UPDATE shop.orders SET status = 'paid' WHERE id = 1")          # app-like writer: audited
        with my("rev_writer", rev_pw) as c, c.cursor() as cur:
            cur.execute("UPDATE shop.orders SET status = 'paid' WHERE id = 2")          # reverse apply: not audited
        with my() as c, c.cursor() as cur:
            cur.execute("SELECT order_id, actor FROM shop.audit_log")
            rows = cur.fetchall()
        self.assertEqual([r[0] for r in rows], [1])
        self.assertTrue(rows[0][1].startswith("root@"), "CURRENT_USER() reports the definer, not the writer")
        self.assertIsNotNone(pymysql)

    def test_super_read_only_blocks_alter_user_and_account_lock_is_the_fence(self):
        import pymysql
        app_pw, rev_pw = secrets.token_hex(8), secrets.token_hex(8)
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS fence")
            cur.execute("CREATE TABLE fence.t (id INT PRIMARY KEY)")
            for u, p in (("app", app_pw), ("dms_rev", rev_pw)):
                cur.execute(f"CREATE USER '{u}'@'%' IDENTIFIED BY '{p}'")
                cur.execute(f"GRANT SELECT, INSERT ON fence.* TO '{u}'@'%'")
            cur.execute("SET GLOBAL super_read_only = ON")
            try:
                with self.assertRaises(pymysql.err.OperationalError) as e:   # ERROR 1290, even for root
                    cur.execute("ALTER USER 'app'@'%' ACCOUNT LOCK")
                self.assertEqual(e.exception.args[0], 1290)
            finally:
                cur.execute("SET GLOBAL read_only = OFF")                   # also clears super_read_only
            cur.execute("ALTER USER 'app'@'%' ACCOUNT LOCK")                 # the fence: app accounts only
        with self.assertRaises(pymysql.err.OperationalError):
            my("app", app_pw).close()                                       # new app logins refused (3118)
        with my("dms_rev", rev_pw) as c, c.cursor() as cur:
            cur.execute("INSERT INTO fence.t VALUES (1)")                   # the reverse writer still writes
        with my() as c, c.cursor() as cur:
            cur.execute("ALTER USER 'app'@'%' ACCOUNT UNLOCK")


@unittest.skipIf(SKIP, SKIP or "")
class MySQLTargetSqlMode(unittest.TestCase):
    """OBS-35: what an explicit strict sql_mode does and does not protect on a MySQL target."""

    STRICT = "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"

    def setUp(self):
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS sm")
            cur.execute("DROP TABLE IF EXISTS sm.t")
            cur.execute("CREATE TABLE sm.t (id INT PRIMARY KEY, code VARCHAR(3), at DATETIME(6))")

    def _rows(self, cur):
        cur.execute("SELECT id, code, CAST(at AS CHAR) FROM sm.t ORDER BY id")
        return [tuple(r) for r in cur.fetchall()]

    def test_default_is_the_documented_strict_set(self):
        with my() as c, c.cursor() as cur:
            cur.execute("SELECT @@GLOBAL.sql_mode")
            self.assertEqual(cur.fetchone()[0], self.STRICT)

    def test_strict_insert_fails_where_non_strict_silently_adjusts(self):
        import pymysql
        with my() as c, c.cursor() as cur:
            cur.execute(f"SET SESSION sql_mode = '{self.STRICT}'")
            for bad in ("(1, 'ABCDEF', NULL)", "(2, 'x', 'infinity')", "(3, 'x', '10000-01-01 00:00:00')"):
                with self.assertRaises(pymysql.err.MySQLError):
                    cur.execute(f"INSERT INTO sm.t VALUES {bad}")      # CDC-apply shape: rejected
            cur.execute("INSERT INTO sm.t VALUES (9, 'x', '0999-12-31 00:00:00')")   # unsupported range, yet accepted
            cur.execute("SET SESSION sql_mode = ''")                   # what the dry-run group left
            cur.execute("INSERT INTO sm.t VALUES (1, 'ABCDEF', NULL), (2, 'x', 'infinity'), (3, 'x', '0999-12-31 00:00:00')")
            self.assertEqual(self._rows(cur), [(1, "ABC", None), (2, "x", "0000-00-00 00:00:00.000000"),
                                               (3, "x", "0999-12-31 00:00:00.000000"),
                                               (9, "x", "0999-12-31 00:00:00.000000")])

    def test_load_data_local_is_non_restrictive_even_under_strict_mode(self):
        csv = Env.tmp / "sm.csv"
        csv.write_text("1,ABCDEF,2026-01-01 00:00:00\n2,x,infinity\n")
        with my(local_infile=True) as c, c.cursor() as cur:
            cur.execute(f"SET SESSION sql_mode = '{self.STRICT}'")
            cur.execute(f"LOAD DATA LOCAL INFILE '{csv}' INTO TABLE sm.t FIELDS TERMINATED BY ','")   # no error
            self.assertEqual(self._rows(cur), [(1, "ABC", "2026-01-01 00:00:00.000000"),
                                               (2, "x", "0000-00-00 00:00:00.000000")])


@unittest.skipIf(SKIP, SKIP or "")
class FractionalSeconds(unittest.TestCase):
    """OBS-52: PG now() keeps microseconds; MySQL NOW()/CURRENT_TIMESTAMP default to fsp 0."""

    def test_mysql_now_precision_and_the_schema_side_fixes(self):
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS fsp")
            cur.execute("CREATE TABLE fsp.t (id INT PRIMARY KEY, at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), "
                        "plain DATETIME NULL)")
            cur.execute("INSERT INTO fsp.t (id) VALUES (1)")                       # app omits the column
            cur.execute("INSERT INTO fsp.t (id, at) VALUES (2, NOW())")            # app passes NOW(): seconds only
            cur.execute("INSERT INTO fsp.t (id, at) VALUES (3, NOW(6))")
            cur.execute("INSERT INTO fsp.t (id, at, plain) VALUES (4, '2026-10-28 18:00:00.654321', '2026-10-28 18:00:00.654321')")
            cur.execute("SELECT id, MICROSECOND(at), MICROSECOND(plain) FROM fsp.t ORDER BY id")
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            # a BEFORE INSERT trigger is the last resort when the app passes NOW() explicitly
            cur.execute("CREATE TABLE fsp.t2 (id INT PRIMARY KEY, at DATETIME(6) NOT NULL)")
            cur.execute("CREATE TRIGGER fsp.t2_bi BEFORE INSERT ON fsp.t2 FOR EACH ROW "
                        "SET NEW.at = IF(MICROSECOND(NEW.at) = 0 AND NEW.at >= NOW() - INTERVAL 1 SECOND, NOW(6), NEW.at)")
            cur.execute("INSERT INTO fsp.t2 VALUES (1, NOW())")
            cur.execute("SELECT MICROSECOND(at) FROM fsp.t2")
            trig = cur.fetchone()[0]
        con = pg("ledger")
        try:
            pg_us = con.run("SELECT extract(microseconds FROM now())::bigint % 1000000 <> 0 "
                            "OR extract(microseconds FROM clock_timestamp())::bigint % 1000000 <> 0")[0][0]
        finally:
            con.close()
        self.assertNotEqual(rows[1][0], 0, "DEFAULT CURRENT_TIMESTAMP(6) keeps microseconds when the column is omitted")
        self.assertEqual(rows[2][0], 0, "NOW() is fsp 0: microseconds lost even in a DATETIME(6) column")
        self.assertNotEqual(rows[3][0], 0)
        self.assertEqual(rows[4], (654321, 0), "DATETIME(6) keeps a client value; DATETIME(0) rounds it away")
        self.assertNotEqual(trig, 0, "the trigger restores microseconds for NOW()-valued inserts")
        self.assertTrue(pg_us, "PostgreSQL now() carries microseconds")


def _ref_indented(label: str) -> str:
    """A §10 query block (indented inside a list item in the reference), comments stripped."""
    text = REF.read_text()
    m = re.search(rf"^\s*-- {re.escape(label)}\..*?\n(?=\s*```)", text, re.S | re.M)
    if not m:
        raise AssertionError(f"{label} query not found")
    return "\n".join(l.strip() for l in m.group(0).splitlines() if not l.strip().startswith("--")).rstrip(";")


def _ref_r1() -> str:
    return _ref_indented("R1")


@unittest.skipIf(SKIP, SKIP or "")
class ReverseCascade(unittest.TestCase):
    """Review r5 #1: MySQL cascades vs a PostgreSQL rollback side."""

    def _table_maps(self, cur, sql: str) -> set[str]:
        cur.execute("FLUSH BINARY LOGS")
        cur.execute("SHOW BINARY LOGS")
        last = cur.fetchall()[-1][0]
        cur.execute(sql)
        cur.execute(f"SHOW BINLOG EVENTS IN '{last}'")
        # SHOW BINLOG EVENTS: Log_name, Pos, Event_type, Server_id, End_log_pos, Info
        return {m.group(1) for r in cur.fetchall() if r[2] == "Table_map"
                for m in [re.search(r"\((\w+\.\w+)\)", r[5])] if m}

    def test_cascade_children_are_not_binlogged_but_trigger_deletes_are(self):
        with my() as c, c.cursor() as cur:
            cur.execute("SELECT @@log_bin, @@binlog_format")
            self.assertEqual(tuple(cur.fetchone()), (1, "ROW"))
            cur.execute("CREATE DATABASE casc")
            cur.execute("CREATE TABLE casc.orders (id INT PRIMARY KEY)")
            cur.execute("CREATE TABLE casc.items (id INT PRIMARY KEY, order_id INT, "
                        "FOREIGN KEY (order_id) REFERENCES casc.orders (id) ON DELETE CASCADE)")
            cur.execute("CREATE TABLE casc.orders2 (id INT PRIMARY KEY)")
            cur.execute("CREATE TABLE casc.items2 (id INT PRIMARY KEY, order_id INT, "
                        "FOREIGN KEY (order_id) REFERENCES casc.orders2 (id) ON DELETE RESTRICT)")
            cur.execute("CREATE TRIGGER casc.orders2_bd BEFORE DELETE ON casc.orders2 FOR EACH ROW "
                        "DELETE FROM casc.items2 WHERE order_id = OLD.id")
            cur.execute("INSERT INTO casc.orders VALUES (1)")
            cur.execute("INSERT INTO casc.items VALUES (10, 1), (11, 1)")
            cur.execute("INSERT INTO casc.orders2 VALUES (1)")
            cur.execute("INSERT INTO casc.items2 VALUES (10, 1), (11, 1)")
            cascade = self._table_maps(cur, "DELETE FROM casc.orders WHERE id = 1")
            trigger = self._table_maps(cur, "DELETE FROM casc.orders2 WHERE id = 1")
            cur.execute("SELECT (SELECT COUNT(*) FROM casc.items), (SELECT COUNT(*) FROM casc.items2)")
            self.assertEqual(tuple(cur.fetchone()), (0, 0), "both variants delete the children locally")
        self.assertEqual(cascade, {"casc.orders"}, "the cascaded child delete is NOT in the binlog")
        self.assertEqual(trigger, {"casc.orders2", "casc.items2"}, "trigger-written child rows ARE binlogged")

    def test_replica_role_skips_pg_fk_actions_and_r1_finds_the_orphans(self):
        con = pg("ledger")
        try:
            con.run("CREATE SCHEMA casc")
            con.run("CREATE TABLE casc.orders (id int PRIMARY KEY)")
            con.run("CREATE TABLE casc.items (id int PRIMARY KEY, order_id int REFERENCES casc.orders (id) ON DELETE CASCADE)")
            con.run("CREATE TABLE casc.notes (id int PRIMARY KEY, order_id int REFERENCES casc.orders (id))")
            con.run("INSERT INTO casc.orders VALUES (1), (2)")
            con.run("INSERT INTO casc.items VALUES (10, 1), (11, 1), (12, 2)")
            con.run("INSERT INTO casc.notes VALUES (20, 1), (21, NULL)")
            con.run("SET session_replication_role = replica")     # what the reverse DMS session runs with
            con.run("DELETE FROM casc.orders WHERE id = 1")      # a reverse-applied parent DELETE
            con.run("RESET session_replication_role")
            gen = _ref_r1().replace("IN ('shop')", "IN ('casc')")
            got = {}
            for (stmt,) in con.run(gen):
                fk, n = con.run(stmt)[0]
                got[fk.split(".")[1]] = n          # fk = "<schema>.<table>.<constraint>"
        finally:
            con.close()
        self.assertEqual(got, {"items": 2, "notes": 1}, "cascade did not fire; NULL FKs are not orphans")

    def test_r1_misses_a_recreated_parent_key(self):
        # Review r6 #1: parent deleted on MySQL (cascade not replicated) and re-created with the
        # same key: the stale child keeps a valid parent on PG, so R1 reports 0 — only a keyed
        # compare of the child table against MySQL shows the difference.
        con = pg("ledger")
        try:
            con.run("CREATE SCHEMA casc2")
            con.run("CREATE TABLE casc2.orders (id int PRIMARY KEY)")
            con.run("CREATE TABLE casc2.items (id int PRIMARY KEY, order_id int REFERENCES casc2.orders (id) ON DELETE CASCADE)")
            con.run("INSERT INTO casc2.orders VALUES (1)")
            con.run("INSERT INTO casc2.items VALUES (10, 1)")
            con.run("SET session_replication_role = replica")
            con.run("DELETE FROM casc2.orders WHERE id = 1")     # reverse-applied parent delete
            con.run("INSERT INTO casc2.orders VALUES (1)")       # reverse-applied re-insert, same key
            con.run("RESET session_replication_role")
            orphans = sum(con.run(stmt)[0][1] for (stmt,) in con.run(_ref_r1().replace("IN ('shop')", "IN ('casc2')")))
            pg_items = {r[0] for r in con.run("SELECT id FROM casc2.items")}
        finally:
            con.close()
        mysql_items: set[int] = set()   # on MySQL the cascade removed item 10 before the re-insert
        self.assertEqual(orphans, 0, "R1 sees nothing")
        self.assertEqual(pg_items - mysql_items, {10}, "the keyed compare finds the stale child")

    def test_r2_lists_cascade_reachable_child_tables_transitively(self):
        with my() as c, c.cursor() as cur:
            cur.execute("CREATE DATABASE casc3")
            for ddl in ("CREATE TABLE casc3.customers (id INT PRIMARY KEY)",
                        "CREATE TABLE casc3.orders (id INT PRIMARY KEY, cid INT, FOREIGN KEY (cid) REFERENCES casc3.customers (id) ON DELETE CASCADE)",
                        "CREATE TABLE casc3.items (id INT PRIMARY KEY, oid INT, FOREIGN KEY (oid) REFERENCES casc3.orders (id) ON DELETE CASCADE)",
                        "CREATE TABLE casc3.notes (id INT PRIMARY KEY, oid INT, FOREIGN KEY (oid) REFERENCES casc3.orders (id) ON DELETE SET NULL)",
                        "CREATE TABLE casc3.audits (id INT PRIMARY KEY, iid INT, FOREIGN KEY (iid) REFERENCES casc3.items (id) ON DELETE RESTRICT)",
                        "CREATE TABLE casc3.lone (id INT PRIMARY KEY, cid INT, FOREIGN KEY (cid) REFERENCES casc3.customers (id))"):
                cur.execute(ddl)
            cur.execute(_ref_indented("R2").replace("IN ('shop')", "IN ('casc3')"))
            got = {r[1] for r in cur.fetchall()}
        self.assertEqual(got, {"orders", "items", "notes"}, "transitive CASCADE/SET NULL children; RESTRICT/NO ACTION stop")


if __name__ == "__main__":
    unittest.main()
