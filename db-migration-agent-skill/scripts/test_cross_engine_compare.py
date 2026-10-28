#!/usr/bin/env python3
"""Tests for shared/scripts/cross_engine_compare.py.

Unit tests (no database): the normalizer for every type on both engines (values shaped the
way pymysql / pg8000 / psycopg return them), column-pair resolution, and the comparison
engine against in-memory sides.

Integration tests (class DockerIntegration*): real mysql:8.0 and postgres:16 containers on
random loopback ports. MySQL -> PostgreSQL via a scripted load that mirrors the
recommended pre-created schema, one that mirrors DMS default mappings, one with
deliberately wrong conversions, and pgloader (a v3.6.9 source build, image
dbskill-pgloader:3.6.9-src, built once from the inline Dockerfile and reused — the Docker Hub
image is amd64-only and Debian's 3.6.7 package can't read MySQL 8); PostgreSQL -> MySQL via scripted loads. The tool must report MATCH for
correct conversions and pinpoint MISMATCH for wrong ones; soak_check.py (CLI) and
soak_check_lambda.run_day are run in cross-family mode against the same containers.
Integration tests SKIP (never fail) when Docker or the images are unavailable; set
CEC_SKIP_DOCKER=1 to skip them explicitly. Containers/networks are always removed.

Run: python3 -B scripts/test_cross_engine_compare.py
"""
import datetime as dt
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
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

UTC = dt.timezone.utc
OPTS = cec.merge_options({})


def mcol(row):
    return cec.classify_mysql(row, OPTS)


def pcol(row):
    return cec.classify_pg(row, OPTS)


# ── In-memory side: answers exactly the SQL shapes cross_engine_compare issues ──────────
_HEX = re.compile(r"X'([0-9a-f]*)'")
_ELIT = re.compile(r"E'((?:[^']|'')*)'")


class MemSide(cec.SqlSide):
    """One engine holding tables {(schema, name): {"catalog": [...], "pk": [...], "rows": [dict]}}.
    Supports single-integer-key keyset reads and `<= n` / `> n` bounds — enough for the
    engine logic; real SQL is exercised by the Docker integration tests."""

    def __init__(self, dialect, tables, label="source", shuffle=False):
        super().__init__(dialect, None, label)
        self.tables, self.shuffle, self.log = tables, shuffle, []

    def _lits(self, sql):
        if self.dialect == "mysql":
            return [bytes.fromhex(h).decode() for h in _HEX.findall(sql)]
        return [x.replace("''", "'") for x in _ELIT.findall(sql)]

    def _table(self, sql):
        q = '`' if self.dialect == "mysql" else '"'
        m = re.search(rf"FROM {q}([^{q}]+){q}\.{q}([^{q}]+){q}", sql)
        return self.tables[(m.group(1), m.group(2))]

    def query(self, sql):
        cec.assert_read_only(sql)
        self.queries += 1
        self.log.append(sql)
        up = sql.lstrip().upper()
        if up.startswith(("SET", "START", "BEGIN", "COMMIT")):
            return []
        if "information_schema.tables" in sql or "relkind IN" in sql:
            lits = [x.lower() for x in self._lits(sql)]
            return [(s, n) for (s, n) in self.tables if n.lower() in lits and (s.lower() in lits or len(lits) == 1)]
        if "information_schema.columns" in sql or "format_type" in sql:
            lits = self._lits(sql)
            return list(self.tables[(lits[0], lits[1])]["catalog"])
        if "key_column_usage" in sql or "indisprimary" in sql:
            lits = self._lits(sql)
            return [(c,) for c in self.tables[(lits[0], lits[1])]["pk"]]
        if "UTC_TIMESTAMP" in up or "AT TIME ZONE 'UTC'" in up:
            return [(dt.datetime(2026, 10, 5, 12, 0, 0),)]
        t = self._table(sql)
        rows = list(t["rows"])
        if "1=0" in sql:
            rows = []
        for op, val in re.findall(r"[`\"]\w+[`\"] (<=|>) (-?\d+)(?![\d'])", sql):
            key = t["pk"][0] if t["pk"] else None
            col = re.search(rf"[`\"](\w+)[`\"] {re.escape(op)} {val}", sql).group(1)
            if op == "<=":
                rows = [r for r in rows if r[col] is not None and r[col] <= int(val)]
            else:
                rows = [r for r in rows if r[col] is not None and r[col] > int(val)]
            del key
        if up.startswith("SELECT COUNT(*)"):
            return [(len(rows),)]
        m = re.match(r"SELECT (MAX|MIN)\([`\"](\w+)[`\"]\)", sql)
        if m:
            vals = [r[m.group(2)] for r in rows if r[m.group(2)] is not None]
            return [((max if m.group(1) == "MAX" else min)(vals) if vals else None,)]
        cols = re.findall(r"[`\"](\w+)[`\"]", sql.split(" FROM ")[0])
        if t["pk"] and " ORDER BY " in sql:
            rows.sort(key=lambda r: r[t["pk"][0]])
            if self.shuffle:
                rows.reverse()
        lim = re.search(r"LIMIT (\d+)$", sql)
        if lim:
            rows = rows[:int(lim.group(1))]
        return [tuple(r[c] for c in cols) for r in rows]


def mysql_table(rows, pk=("id",), catalog=None):
    catalog = catalog or [("id", "bigint", "bigint unsigned", 20, 0, None, "NO"),
                          ("flag", "tinyint", "tinyint(1)", 3, 0, None, "NO"),
                          ("price", "decimal", "decimal(20,6)", 20, 6, None, "NO"),
                          ("created", "datetime", "datetime(6)", None, None, 6, "YES"),
                          ("doc", "json", "json", None, None, None, "YES"),
                          ("email", "varchar", "varchar(100)", None, None, None, "NO")]
    return {("shop", "items"): {"catalog": catalog, "pk": list(pk), "rows": rows}}


def pg_table(rows, pk=("id",), catalog=None):
    catalog = catalog or [("id", "numeric(20,0)", "b", "N", "NO", None),
                          ("flag", "boolean", "b", "B", "NO", None),
                          ("price", "numeric(20,6)", "b", "N", "NO", None),
                          ("created", "timestamp(6) without time zone", "b", "D", "YES", None),
                          ("doc", "jsonb", "b", "U", "YES", None),
                          ("email", "text", "b", "S", "NO", None)]
    return {("shop", "items"): {"catalog": catalog, "pk": list(pk), "rows": rows}}


def src_rows(n=30):
    return [{"id": i, "flag": i % 2, "price": Decimal(f"{i}.125000"),
             "created": dt.datetime(2026, 1, 1, 0, 0) + dt.timedelta(minutes=i, microseconds=i),
             "doc": json.dumps({"b": i, "a": [1, 2.5]}), "email": f"user{i}@Example.com"} for i in range(1, n + 1)]


def tgt_rows(n=30):
    return [{"id": Decimal(i), "flag": bool(i % 2), "price": Decimal(f"{i}.125"),
             "created": dt.datetime(2026, 1, 1, 0, 0) + dt.timedelta(minutes=i, microseconds=i),
             "doc": {"a": [1, Decimal("2.5")], "b": i}, "email": f"user{i}@Example.com"} for i in range(1, n + 1)]


def compare(src, tgt, options=None, table="shop.items", bound=None):
    s, t = MemSide("mysql", src, "source"), MemSide("postgres", tgt, "target")
    opts = cec.merge_options(options)
    plan, early = cec.plan_table(s, t, table, opts)
    if early:
        return early
    return cec.compare_table(s, t, plan, opts, bound)


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.n = cec.Normalizer(OPTS)

    def c(self, v, col, kind, **kw):
        return self.n.canon(v, col, kind, **kw)

    def test_integers_and_unsigned_bigint(self):
        m = mcol(("id", "bigint", "bigint unsigned", 20, 0, None, "NO"))
        p = pcol(("id", "numeric(20,0)", "b", "N", "NO", None))
        self.assertEqual(self.c(18446744073709551615, m, "numeric"), self.c(Decimal("18446744073709551615"), p, "numeric"))
        self.assertEqual(self.c(5, m, "numeric"), "5")
        self.assertEqual(self.c(True, m, "numeric"), "1")

    def test_decimal_scale_aware(self):
        m = mcol(("p", "decimal", "decimal(20,6)", 20, 6, None, "NO"))
        self.assertEqual(self.c(Decimal("1.500000"), m, "numeric"), "1.5")
        self.assertEqual(self.c(Decimal("-0.000"), m, "numeric"), "0")
        self.assertEqual(self.c(Decimal("1E+2"), m, "numeric"), "100")
        self.assertNotEqual(self.c(Decimal("0.611111104488373"), m, "numeric"), self.c(Decimal("0.611111"), m, "numeric"))
        self.assertEqual(self.c(Decimal("123456789012345678901234567890.123456789"), m, "numeric"),
                         "123456789012345678901234567890.123456789")

    def test_float_digits(self):
        m = mcol(("f", "float", "float", 12, None, None, "YES"))
        self.assertEqual(self.c(0.10000000149011612, m, "float:7"), self.c(0.1, m, "float:7"))
        self.assertEqual(self.c(float("nan"), m, "float:15"), "nan")

    def test_booleans_both_engines(self):
        m = mcol(("f", "tinyint", "tinyint(1)", 3, 0, None, "NO"))
        p = pcol(("f", "boolean", "b", "B", "NO", None))
        self.assertEqual(m.kind, "bool")
        self.assertEqual(self.c(1, m, "bool"), self.c(True, p, "bool"))
        self.assertEqual(self.c(0, m, "bool"), "false")
        self.assertEqual(self.c(2, m, "bool"), "!bool:2")          # TINYINT(1) holding 2 never equals a boolean
        bit1 = mcol(("b", "bit", "bit(1)", 1, None, None, "YES"))
        self.assertEqual(self.c(b"\x01", bit1, "bool"), "true")
        txt = mcol(("a", "varchar", "varchar(5)", None, None, None, "NO"))
        self.assertEqual(self.c("true", txt, "bool"), "true")     # PG BOOLEAN -> VARCHAR(5) 'true'
        self.assertEqual(self.c(True, p, "bool_numeric"), "1")
        self.assertEqual(self.c(1, m, "bool_numeric"), "1")

    def test_text_exact_casefold_and_char(self):
        m = mcol(("e", "varchar", "varchar(10)", None, None, None, "NO"))
        self.assertNotEqual(self.c("Ab", m, "text"), self.c("ab", m, "text"))
        self.assertEqual(self.c("Ab", m, "text", casefold=True), self.c("ab", m, "text", casefold=True))
        p = pcol(("c", "character(5)", "b", "S", "NO", None))
        self.assertEqual(self.c("ab   ", p, "char"), self.c("ab", m, "char"))
        self.assertEqual(self.c("한글 é", m, "text"), "한글 é")

    def test_enum_set_and_array(self):
        e = mcol(("s", "enum", "enum('new','paid')", None, None, None, "NO"))
        self.assertEqual(e.kind, "enum")
        self.assertEqual(self.c("paid", e, "text"), "paid")
        s = mcol(("t", "set", "set('a','b','c')", None, None, None, "YES"))
        p = pcol(("t", "text[]", "b", "A", "YES", "text"))
        self.assertEqual(p.kind, "array")
        self.assertEqual(self.c("c,a", s, "set"), self.c(["a", "c"], p, "set"))
        self.assertEqual(self.c("", s, "set"), "")
        pj = mcol(("l", "json", "json", None, None, None, "YES"))
        self.assertEqual(self.c(["x", "y"], p, "json"), self.c('["x", "y"]', pj, "json"))

    def test_pg_array_literal_from_unknown_array_types(self):
        # pg8000 returns arrays of types it doesn't know (pgloader's enum array for a SET) as text
        p = pcol(("t", "items_tags[]", "b", "A", "YES", "items_tags"))
        s = mcol(("t", "set", "set('a','b','c')", None, None, None, "YES"))
        self.assertEqual(self.c("{b,a}", p, "set"), self.c("a,b", s, "set"))
        self.assertEqual(self.c("{}", p, "set"), self.c("", s, "set"))
        self.assertEqual(cec.parse_pg_array('{"a,b",NULL,"q\\"x"}'), ["a,b", None, 'q"x'])
        self.assertEqual(self.c('{x,"y z"}', p, "json"), '["x","y z"]')

    def test_json_canonical_both_engines(self):
        m = mcol(("d", "json", "json", None, None, None, "YES"))
        p = pcol(("d", "jsonb", "b", "U", "YES", None))
        self.assertEqual(self.c('{"b": 1, "a": [1, 2.50], "c": null}', m, "json"),
                         self.c({"c": None, "a": [1, Decimal("2.5")], "b": 1}, p, "json"))
        self.assertEqual(self.c('"abc"', m, "json"), self.c("abc", p, "json"))   # PG drivers parse; "abc" is a scalar
        t = pcol(("d", "text", "b", "S", "YES", None))
        self.assertEqual(self.c('{"a":1}', t, "json"), self.c({"a": 1}, p, "json"))
        self.assertTrue(self.c("not json", m, "json").startswith("!json:"))

    def test_uuid_binary_bits(self):
        p = pcol(("u", "uuid", "b", "U", "NO", None))
        m = mcol(("u", "char", "char(36)", None, None, None, "NO"))
        u = uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        self.assertEqual(self.c(u, p, "uuid"), self.c("A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11", m, "uuid"))
        self.assertEqual(self.c(u.bytes, m, "uuid"), str(u))
        b = mcol(("b", "varbinary", "varbinary(16)", None, None, None, "YES"))
        self.assertEqual(self.c(b"\x00\xff", b, "binary"), self.c(memoryview(b"\x00\xff"), b, "binary"))
        self.assertEqual(self.c(b"\x00\xff", b, "binary"), "00ff")
        bits = mcol(("x", "bit", "bit(4)", 4, None, None, "YES"))
        pbits = pcol(("x", "bit(4)", "b", "V", "YES", None))
        self.assertEqual(self.c(b"\x05", bits, "bits", bits=4), self.c("0101", pbits, "bits", bits=4))

    def test_dates_and_zero_dates(self):
        m = mcol(("d", "date", "date", None, None, None, "YES"))
        p = pcol(("d", "date", "b", "D", "YES", None))
        self.assertEqual(self.c(dt.date(2026, 1, 2), m, "date"), self.c(dt.date(2026, 1, 2), p, "date"))
        self.assertIsNone(self.c("0000-00-00", m, "date"))           # what DMS/pgloader write: NULL
        self.assertIsNone(self.c("2026-00-10", m, "date"))           # partial zero date
        self.assertEqual(self.n.zero_dates["source"], 2)
        n2 = cec.Normalizer(cec.merge_options({"zero_date": "distinct"}))
        self.assertEqual(n2.canon("0000-00-00", m, "date"), "!zero:0000-00-00")

    def test_timestamps_naive_vs_aware(self):
        mdt = mcol(("t", "datetime", "datetime(6)", None, None, 6, "YES"))
        mts = mcol(("t", "timestamp", "timestamp(3)", None, None, 3, "YES"))
        ptz = pcol(("t", "timestamp(3) with time zone", "b", "D", "YES", None))
        pnaive = pcol(("t", "timestamp(6) without time zone", "b", "D", "YES", None))
        self.assertEqual((mts.kind, ptz.kind, pnaive.kind), ("timestamp_utc", "timestamp_utc", "datetime"))
        aware = dt.datetime(2026, 1, 1, 0, 0, 0, 123000, tzinfo=UTC)
        # MySQL TIMESTAMP read with session time_zone=+00:00 is naive UTC
        self.assertEqual(self.c(dt.datetime(2026, 1, 1, 0, 0, 0, 123000), mts, "ts"), self.c(aware, ptz, "ts"))
        # naive DATETIME interpreted in naive_timezone
        kst = cec.Normalizer(cec.merge_options({"naive_timezone": "+09:00"}))
        self.assertEqual(kst.canon(dt.datetime(2026, 1, 1, 9, 0, 0, 123000), mdt, "ts"), self.c(aware, ptz, "ts"))
        self.assertNotEqual(self.c(dt.datetime(2026, 1, 1, 9, 0, 0, 123000), mdt, "ts"), self.c(aware, ptz, "ts"))
        self.assertEqual(self.c(dt.datetime(2026, 1, 1, 9, 0), mdt, "ts"), "2026-01-01 09:00:00.000000")
        p0 = cec.Normalizer(cec.merge_options({"timestamp_precision": 0}))
        self.assertEqual(p0.canon(dt.datetime(2026, 1, 1, 9, 0, 0, 999999), pnaive, "ts"), "2026-01-01 09:00:00")
        self.assertIsNone(self.c("0000-00-00 00:00:00", mdt, "ts"))
        self.assertEqual(self.c("infinity", pnaive, "ts"), "!ts:infinity")

    # #5 — TIME WITH TIME ZONE is not an unzoned TIME
    def test_timetz_requires_a_policy(self):
        p = pcol(("t", "time with time zone", "b", "D", "YES", None))
        m = mcol(("t", "time", "time", None, None, 0, "YES"))
        self.assertEqual(p.kind, "timetz")
        kind, f = cec.resolve_pair(p, m, OPTS)
        self.assertEqual((kind, f[0]["severity"]), ("unsupported", "unsupported"))
        noon_kst = dt.time(12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        utc = cec.Normalizer(cec.merge_options({"timetz_policy": "utc"}))
        kind, _ = cec.resolve_pair(p, m, utc.opts)
        self.assertEqual(utc.canon(noon_kst, p, kind), utc.canon(dt.timedelta(hours=3), m, kind))
        self.assertNotEqual(utc.canon(noon_kst, p, kind), utc.canon(dt.timedelta(hours=12), m, kind))
        self.assertEqual(utc.canon("12:00:00+09", p, kind), "03:00:00.000000")      # text form from a driver
        wall = cec.Normalizer(cec.merge_options({"timetz_policy": "wallclock"}))
        kind, _ = cec.resolve_pair(p, m, wall.opts)
        self.assertEqual(wall.canon(noon_kst, p, kind), wall.canon(dt.timedelta(hours=12), m, kind))
        kind, _ = cec.resolve_pair(p, p, OPTS)                                      # both zoned: UTC
        self.assertEqual(self.c(noon_kst, p, kind), self.c(dt.time(4, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))), p, kind))

    def test_time_and_interval(self):
        m = mcol(("d", "time", "time(3)", None, None, 3, "YES"))
        p = pcol(("d", "interval", "b", "T", "YES", None))
        self.assertEqual(self.c(dt.timedelta(hours=25, minutes=1, milliseconds=5), m, "time"),
                         self.c(dt.timedelta(days=1, hours=1, minutes=1, milliseconds=5), p, "time"))
        self.assertEqual(self.c(dt.time(1, 2, 3), p, "time"), "01:02:03.000000")
        self.assertEqual(self.c(-dt.timedelta(minutes=1), m, "time"), "-00:01:00.000000")

    def test_null_vs_empty_hash_differently(self):
        self.assertNotEqual(cec.row_digest([None]), cec.row_digest([""]))
        self.assertNotEqual(cec.row_digest(["a", "bc"]), cec.row_digest(["ab", "c"]))


class ClassifyAndPairTests(unittest.TestCase):
    def pair(self, s, t, **opts):
        return cec.resolve_pair(s, t, cec.merge_options(opts))

    def test_classify_pg_types(self):
        for fmt, kind in (("numeric(20,6)", "decimal"), ("numeric", "decimal"), ("character varying(10)", "text"),
                          ("double precision", "float8"), ("real", "float4"), ("public.citext", "text"),
                          ("timestamp with time zone", "timestamp_utc"), ("time without time zone", "time"),
                          ("bytea", "binary"), ("uuid", "uuid"), ("point", "unsupported")):
            self.assertEqual(pcol(("c", fmt, "b", "X", "YES", None)).kind, kind, fmt)
        self.assertIsNone(pcol(("c", "numeric", "b", "N", "YES", None)).scale)
        self.assertEqual(pcol(("c", "numeric(20)", "b", "N", "YES", None)).scale, 0)
        self.assertEqual(pcol(("c", "mood", "e", "E", "YES", None)).kind, "enum")

    def test_classify_mysql_types(self):
        self.assertEqual(mcol(("c", "tinyint", "tinyint", 3, 0, None, "NO")).kind, "int")
        self.assertEqual(mcol(("c", "tinyint", "tinyint(1)", 3, 0, None, "NO")).kind, "bool")
        o = cec.merge_options({"tinyint1_as_boolean": False})
        self.assertEqual(cec.classify_mysql(("c", "tinyint", "tinyint(1)", 3, 0, None, "NO"), o).kind, "int")
        self.assertEqual(mcol((b"c", b"geometry", b"geometry", None, None, None, b"YES")).kind, "unsupported")

    def test_tinyint1_to_smallint_policy(self):
        s = mcol(("f", "tinyint", "tinyint(1)", 3, 0, None, "NO"))
        t = pcol(("f", "smallint", "b", "N", "NO", None))
        kind, f = self.pair(s, t)
        self.assertEqual((kind, f[0]["severity"]), ("bool_numeric", "mismatch"))
        kind, f = self.pair(s, t, bool_policy="numeric")
        self.assertEqual((kind, f[0]["severity"]), ("bool_numeric", "info"))

    def test_dms_default_findings(self):
        cases = [
            (pcol(("a", "boolean", "b", "B", "NO", None)), mcol(("a", "varchar", "varchar(5)", None, None, None, "NO")), "mismatch"),
            (mcol(("d", "json", "json", None, None, None, "YES")), pcol(("d", "text", "b", "S", "YES", None)), "mismatch"),
            (pcol(("l", "text[]", "b", "A", "YES", "text")), mcol(("l", "longtext", "longtext", None, None, None, "YES")), "mismatch"),
            (mcol(("b", "bigint", "bigint unsigned", 20, 0, None, "YES")), pcol(("b", "bigint", "b", "N", "YES", None)), "risk"),
            (pcol(("r", "numeric", "b", "N", "YES", None)), mcol(("r", "decimal", "decimal(28,6)", 28, 6, None, "YES")), "risk"),
            (mcol(("g", "geometry", "geometry", None, None, None, "YES")), pcol(("g", "bytea", "b", "U", "YES", None)), "unsupported"),
        ]
        for s, t, sev in cases:
            _, f = self.pair(s, t)
            self.assertEqual(f[0]["severity"], sev, (s.raw_type, t.raw_type, f))

    def test_expected_target_types_are_clean(self):
        cases = [(pcol(("u", "uuid", "b", "U", "NO", None)), mcol(("u", "char", "char(36)", None, None, None, "NO"))),
                 (mcol(("i", "int", "int unsigned", 10, 0, None, "NO")), pcol(("i", "bigint", "b", "N", "NO", None))),
                 (mcol(("t", "set", "set('a')", None, None, None, "YES")), pcol(("t", "text[]", "b", "A", "YES", "text")))]
        for s, t in cases:
            _, f = self.pair(s, t)
            self.assertFalse([x for x in f if x["severity"] in ("mismatch", "risk")], f)


class EngineTests(unittest.TestCase):
    def test_correct_conversion_matches(self):
        r = compare(mysql_table(src_rows()), pg_table(tgt_rows()))
        self.assertEqual(r["verdict"], "MATCH", r)
        self.assertIs(r["check"], True)
        self.assertEqual(r["rows"], {"source": 30, "target": 30})
        self.assertEqual(r["digest"]["source"], r["digest"]["target"])

    def test_missing_extra_and_changed_rows_are_pinpointed(self):
        t = tgt_rows()
        del t[4]                                 # id 5 missing
        next(r for r in t if r["id"] == 10)["price"] = Decimal("10.12")   # id 10 truncated price
        t.append({**t[0], "id": Decimal(99)})    # extra id 99
        r = compare(mysql_table(src_rows()), pg_table(t))
        self.assertEqual(r["verdict"], "MISMATCH")
        mm = r["mismatches"]
        self.assertEqual(mm["examples"]["missing_on_target"], [["5"]])
        self.assertEqual(mm["examples"]["extra_on_target"], [["99"]])
        self.assertEqual(mm["examples"]["different"][0]["key"], ["10"])
        self.assertEqual(mm["examples"]["different"][0]["columns"], ["price"])
        self.assertNotIn("values", mm["examples"]["different"][0])   # values redacted by default
        r2 = compare(mysql_table(src_rows()), pg_table(t), {"include_values": True})
        self.assertEqual(r2["mismatches"]["examples"]["different"][0]["values"]["price"],
                         {"source": "10.125", "target": "10.12"})

    def test_strict_type_mismatch_without_value_difference(self):
        cat = pg_table([])[("shop", "items")]["catalog"]
        cat = [("flag", "smallint", "b", "N", "NO", None) if c[0] == "flag" else c for c in cat]
        t = [{**r, "flag": int(r["flag"])} for r in tgt_rows()]
        r = compare(mysql_table(src_rows()), pg_table(t, catalog=cat))
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["different_rows"], 0)
        self.assertTrue(any(f["severity"] == "mismatch" and f["column"] == "flag" for f in r["type_findings"]))
        self.assertEqual(compare(mysql_table(src_rows()), pg_table(t, catalog=cat), {"bool_policy": "numeric"})["verdict"],
                         "MATCH")

    def test_timezone_shift_detected_and_naive_timezone_option(self):
        cat = [("created", "timestamp(6) with time zone", "b", "D", "YES", None) if c[0] == "created" else c
               for c in pg_table([])[("shop", "items")]["catalog"]]
        kst = dt.timezone(dt.timedelta(hours=9))
        t = [{**r, "created": r["created"].replace(tzinfo=kst)} for r in tgt_rows()]   # loaded in a +09:00 session
        r = compare(mysql_table(src_rows()), pg_table(t, catalog=cat))
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["differing_columns"], {"created": 30})
        self.assertEqual(compare(mysql_table(src_rows()), pg_table(t, catalog=cat),
                                 {"naive_timezone": "+09:00"})["verdict"], "MATCH")

    def test_chunks_identify_the_mismatching_range(self):
        t = tgt_rows(100)
        t[57]["email"] = "USER58@example.com"
        r = compare(mysql_table(src_rows(100)), pg_table(t), {"chunk_size": 10})
        self.assertEqual(r["chunks"]["count"], 10)
        self.assertEqual([c["first_key"] for c in r["chunks"]["mismatched"]], [["51"]])
        self.assertEqual(compare(mysql_table(src_rows(100)), pg_table(t), {"text_casefold": True})["verdict"], "MATCH")

    def test_budget_exhaustion_is_needs_review_never_match(self):
        r = compare(mysql_table(src_rows()), pg_table(tgt_rows()), {"max_seconds": 0})
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")
        self.assertIsNone(r["check"])

    def test_ordering_disagreement_is_needs_review(self):
        s, t = MemSide("mysql", mysql_table(src_rows()), "source", shuffle=True), MemSide("postgres", pg_table(tgt_rows()), "target")
        plan, _ = cec.plan_table(s, t, "shop.items", OPTS)
        r = cec.compare_table(s, t, plan, OPTS)
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")
        self.assertIn("order", " ".join(r["notes"]))

    def test_no_pk_multiset(self):
        rows = [{"id": i % 3, "flag": 1, "price": Decimal("1"), "created": None, "doc": None, "email": "a"} for i in range(9)]
        trows = [{"id": Decimal(i % 3), "flag": True, "price": Decimal("1.0"), "created": None, "doc": None, "email": "a"}
                 for i in range(9)]
        r = compare(mysql_table(rows, pk=()), pg_table(trows, pk=()))
        self.assertEqual((r["mode"], r["verdict"]), ("multiset", "MATCH"))
        r = compare(mysql_table(rows, pk=()), pg_table(trows[:-1], pk=()))
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["missing_on_target"], 1)
        r = compare(mysql_table(rows, pk=()), pg_table(trows, pk=()), {"multiset_max_rows": 5})
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")

    def test_timetz_vs_time_table_is_needs_review_without_policy(self):
        cat_p = pg_table([])[("shop", "items")]["catalog"] + [("at", "time with time zone", "b", "D", "YES", None)]
        cat_m = mysql_table([])[("shop", "items")]["catalog"] + [("at", "time", "time", None, None, 0, "YES")]
        kst = dt.timezone(dt.timedelta(hours=9))
        src = [{**r, "at": dt.timedelta(hours=12)} for r in src_rows()]
        tgt = [{**r, "at": dt.time(12, 0, tzinfo=kst)} for r in tgt_rows()]
        r = compare(mysql_table(src, catalog=cat_m), pg_table(tgt, catalog=cat_p))
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")                    # was a silent MATCH
        self.assertIn("at", r["unsupported_columns"])
        self.assertEqual(compare(mysql_table(src, catalog=cat_m), pg_table(tgt, catalog=cat_p),
                                 {"timetz_policy": "utc"})["verdict"], "MISMATCH")   # 12:00+09 is 03:00 UTC
        self.assertEqual(compare(mysql_table(src, catalog=cat_m), pg_table(tgt, catalog=cat_p),
                                 {"timetz_policy": "wallclock"})["verdict"], "MATCH")

    def test_bound_and_tail(self):
        t = tgt_rows(25)                      # target lags 5 rows
        r = compare(mysql_table(src_rows()), pg_table(t), bound={"column": "id", "op": "<=", "value": 20})
        self.assertEqual(r["verdict"], "MATCH")
        self.assertEqual(r["rows"], {"source": 20, "target": 20})
        self.assertEqual(r["tail_rows"], {"source": 10, "target": 5})

    def test_missing_column_missing_table_and_skip(self):
        cat = [c for c in pg_table([])[("shop", "items")]["catalog"] if c[0] != "doc"]
        t = [{k: v for k, v in r.items() if k != "doc"} for r in tgt_rows()]
        r = compare(mysql_table(src_rows()), pg_table(t, catalog=cat))
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertIn({"column": "doc", "severity": "mismatch", "message": "column missing on target"}, r["type_findings"])
        r = compare(mysql_table(src_rows()), {("shop", "other"): pg_table([])[("shop", "items")]})
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertIn("not found on target", r["notes"][0])
        r = compare(mysql_table(src_rows()), pg_table(tgt_rows()), {"tables": {"shop.items": {"skip": "archived"}}})
        self.assertEqual(r["check"], "not_applicable")

    def test_column_and_table_mapping_overrides(self):
        cat = [(("Email_Addr",) + tuple(c[1:])) if c[0] == "email" else c for c in pg_table([])[("shop", "items")]["catalog"]]
        t = [{("Email_Addr" if k == "email" else k): v for k, v in r.items()} for r in tgt_rows()]
        tables = {("public", "items2"): pg_table(t, catalog=cat)[("shop", "items")]}
        r = compare(mysql_table(src_rows()), tables,
                    {"tables": {"shop.items": {"target": "public.items2", "columns": {"email": "Email_Addr"}}}})
        self.assertEqual(r["verdict"], "MATCH", r)

    def test_case_insensitive_auto_mapping(self):
        cat = [(c[0].upper(),) + tuple(c[1:]) for c in pg_table([])[("shop", "items")]["catalog"]]
        t = [{k.upper(): v for k, v in r.items()} for r in tgt_rows()]
        tables = pg_table(t, catalog=cat, pk=("ID",))
        r = compare(mysql_table(src_rows()), tables)
        self.assertEqual(r["verdict"], "MATCH", r)

    def test_read_only_guard(self):
        for bad in ("UPDATE t SET a=1", "SELECT 1; DROP TABLE t", "DELETE FROM t", "SELECT * FROM t FOR UPDATE",
                    "SELECT * INTO OUTFILE '/tmp/x' FROM t", "SET GLOBAL read_only=0",
                    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "SELECT 1 FROM t WHERE 1=1 OR CALL"):
            with self.assertRaises(cec.CompareError, msg=bad):
                cec.assert_read_only(bad)
        cec.assert_read_only("SELECT 'a;b' FROM t WHERE x = E'c;d'")
        cec.assert_read_only("SET time_zone = '+00:00'")

    def test_options_validation(self):
        for bad in ({"nope": 1}, {"bool_policy": "maybe"}, {"tables": {"t": {"colums": {}}}},
                    {"naive_timezone": "Mars/Base"}, {"tables": {"t": {"kinds": {"c": "blob"}}}}):
            with self.assertRaises(cec.CompareError):
                cec.merge_options(bad)

    def test_parity_with_soak_helpers(self):
        for ident in ("shop.orders", "orders", '"a.b".c', "`x`.`y`"):
            self.assertEqual(cec.table_parts(ident), soak_check._table_parts(ident))
        for case in ((100, 90, 1, 10), (5, 5, 1, 10), (None, 5, 1, 10), (100, 100, 50, 0)):
            self.assertEqual(cec.pk_watermark(*case), soak_check.resolve_pk_watermark(*case))

    def test_probe_sqls_are_zero_row_and_read_only(self):
        s, t = MemSide("mysql", mysql_table(src_rows()), "source"), MemSide("postgres", pg_table(tgt_rows()), "target")
        plan, _ = cec.plan_table(s, t, "shop.items", OPTS)
        for side, dialect in (("source", "mysql"), ("target", "postgres")):
            sqls = cec.probe_sqls(plan, side, dialect, {"column": "id", "op": "<=", "value": 0})
            self.assertTrue(all("1=0" in q for q in sqls), sqls)          # every data statement, no exemptions
            for q in sqls:
                cec.assert_read_only(q)

    def test_watermark_ops_cover_a_numeric_target_key(self):
        s, t = MemSide("mysql", mysql_table(src_rows()), "source"), MemSide("postgres", pg_table(tgt_rows()), "target")
        plan, _ = cec.plan_table(s, t, "shop.items", OPTS)       # target id is NUMERIC(20,0)
        mode, cp, ops, _ = cec.watermark_ops(plan, {"enabled": True, "timestamp_columns": {}})
        self.assertEqual(mode, "pk")
        self.assertEqual(ops["source"], ["SELECT MAX(`id`) FROM `shop`.`items`", "SELECT MIN(`id`) FROM `shop`.`items`"])
        self.assertEqual(ops["target"], ['SELECT MAX("id") FROM "shop"."items"'])

    # #16 — the budget bounds reads AND normalization; drivers get server-side statement timeouts
    def test_final_fetch_past_deadline_is_not_a_match(self):
        clock = [0.0]

        class Slow(MemSide):
            def query(self, sql):
                rows = super().query(sql)
                if " ORDER BY " in sql:
                    clock[0] += 100.0            # the (single, final) chunk fetch takes 100 s
                return rows
        s, t = Slow("mysql", mysql_table(src_rows()), "source"), Slow("postgres", pg_table(tgt_rows()), "target")
        opts = cec.merge_options({"max_seconds": 1})
        with mock.patch.object(cec.time, "monotonic", lambda: clock[0]):
            plan, _ = cec.plan_table(s, t, "shop.items", opts)
            r = cec.compare_table(s, t, plan, opts)
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")
        self.assertIn("time budget", " ".join(r["notes"]))

    def test_slow_multiset_normalization_is_not_a_match(self):
        clock = [0.0]
        rows = [{"id": i, "flag": 1, "price": Decimal("1"), "created": None, "doc": None, "email": f"a{i}"} for i in range(1200)]
        trows = [{"id": Decimal(i), "flag": True, "price": Decimal("1"), "created": None, "doc": None, "email": f"a{i}"}
                 for i in range(1200)]
        real = cec.Normalizer.canon

        def slow_canon(self, *a, **k):
            clock[0] += 0.01
            return real(self, *a, **k)
        opts = cec.merge_options({"max_seconds": 5, "multiset_max_rows": 5000})
        s, t = MemSide("mysql", mysql_table(rows, pk=()), "source"), MemSide("postgres", pg_table(trows, pk=()), "target")
        with mock.patch.object(cec.time, "monotonic", lambda: clock[0]), mock.patch.object(cec.Normalizer, "canon", slow_canon):
            plan, _ = cec.plan_table(s, t, "shop.items", opts)
            r = cec.compare_table(s, t, plan, opts)
        self.assertEqual((r["mode"], r["verdict"]), ("multiset", "NEEDS_REVIEW"))
        self.assertIn("multiset", " ".join(r["notes"]))

    def test_statement_timeouts_are_set_server_side(self):
        s, t = MemSide("mysql", mysql_table(src_rows()), "source"), MemSide("postgres", pg_table(tgt_rows()), "target")
        cec.compare_tables(s, t, ["shop.items"], {"max_seconds": 42})
        self.assertIn("SET SESSION max_execution_time = 42000", s.log)
        self.assertIn("SET statement_timeout = 42000", t.log)

        class MariaDb(MemSide):
            def query(self, sql):
                if "max_execution_time" in sql:
                    raise RuntimeError("Unknown system variable 'max_execution_time'")
                return super().query(sql)
        m = MariaDb("mysql", mysql_table(src_rows()), "source")
        m.begin(7)
        self.assertIn("SET SESSION max_statement_time = 7.000", m.log)

    def test_psycopg_connections_get_a_statement_timeout(self):
        captured = {}
        fake = mock.MagicMock()
        fake.connect.side_effect = lambda **kw: captured.update(kw) or mock.MagicMock()
        cfg = {"engine": "postgres", "host": "h", "database": "d", "user": "u", "password_env": "CEC_T_PW",
               "ssl_insecure": True}
        with mock.patch.dict(sys.modules, {"psycopg": fake}), mock.patch.dict(os.environ, {"CEC_T_PW": "x"}):
            cec.connect_side(cfg, "target", "psycopg", read_timeout=30)
        self.assertEqual(captured["options"], "-c statement_timeout=30000")

    # #15 — a `kinds` override changes the comparison, not how the driver represents the value
    def test_kinds_override_json_in_pg_text(self):
        cat = [("doc", "text", "b", "S", "YES", None) if c[0] == "doc" else c for c in pg_table([])[("shop", "items")]["catalog"]]
        t = [{**r, "doc": json.dumps({"a": [1, 2.50], "b": int(r["id"])}, indent=1)} for r in tgt_rows()]   # JSON text, other key order
        r = compare(mysql_table(src_rows()), pg_table(t, catalog=cat), {"tables": {"shop.items": {"kinds": {"doc": "json"}}}})
        self.assertEqual(r["verdict"], "MATCH", r["mismatches"])
        self.assertEqual(r["mismatches"]["different_rows"], 0)
        r = compare(mysql_table(src_rows()), pg_table(t, catalog=cat))                  # without the override
        self.assertEqual(r["verdict"], "MISMATCH")                                      # JSON-as-text is a type defect
        self.assertEqual(r["mismatches"]["different_rows"], 0)


class SoakChecksTests(unittest.TestCase):
    WCFG = {"enabled": True, "pk_margin": 5, "timestamp_columns": {}, "timestamp_age_minutes": 15}

    def run_checks(self, src, tgt, **opts):
        s, t = MemSide("mysql", src, "source"), MemSide("postgres", tgt, "target")
        return cec.soak_checks(s, t, ["shop.items"], ["shop.items"], self.WCFG, opts)

    def test_lagging_target_is_green_below_watermark(self):
        rows, ck, drift = self.run_checks(mysql_table(src_rows(30)), pg_table(tgt_rows(28)))
        d = rows["detail"]["shop.items"]
        self.assertEqual((d["mode"], d["watermark"], d["tail_rows"]), ("pk_watermark", 23, {"source": 7, "target": 5}))
        self.assertIs(rows["pass"], True)
        self.assertIs(ck["pass"], True)
        self.assertIs(drift["pass"], True)
        self.assertEqual(ck["detail"]["shop.items"]["comparison"], "cross_engine_normalized")

    def test_changed_old_row_is_red(self):
        t = tgt_rows(30)
        t[2]["price"] = Decimal("3.1")
        rows, ck, _ = self.run_checks(mysql_table(src_rows(30)), pg_table(t))
        self.assertIs(rows["pass"], True)
        self.assertIs(ck["pass"], False)
        self.assertEqual(ck["detail"]["shop.items"]["mismatches"]["examples"]["different"][0]["key"], ["3"])

    def test_type_mapping_defect_fails_drift(self):
        cat = [("flag", "smallint", "b", "N", "NO", None) if c[0] == "flag" else c for c in pg_table([])[("shop", "items")]["catalog"]]
        t = [{**r, "flag": int(r["flag"])} for r in tgt_rows()]
        _, ck, drift = self.run_checks(mysql_table(src_rows()), pg_table(t, catalog=cat))
        self.assertIs(drift["pass"], False)
        self.assertIn("flag", drift["detail"]["shop.items"]["mismatched_attributes"])
        self.assertIs(ck["pass"], False)

    def test_timestamp_watermark_cross_engine(self):
        wcfg = {**self.WCFG, "timestamp_columns": {"shop.items": "created"}}
        s, t = MemSide("mysql", mysql_table(src_rows()), "source"), MemSide("postgres", pg_table(tgt_rows()), "target")
        rows, ck, _ = cec.soak_checks(s, t, ["shop.items"], [], wcfg, {})
        d = rows["detail"]["shop.items"]
        self.assertEqual(d["mode"], "timestamp_watermark")
        self.assertEqual(d["watermark"], "2026-10-05 11:45:00.000000")
        self.assertIn("null_rows", d)


# ── Docker integration ──────────────────────────────────────────────────────────────────
def _docker_ok():
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
        import pymysql  # noqa: F401
        import pg8000  # noqa: F401
    except ImportError as e:
        return f"python driver missing: {e}"
    return None


DOCKER_SKIP = _docker_ok()


def _insecure_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Env:
    """Containers + network for the whole module; torn down in tearDownModule."""
    started = False
    tmp = net = mysql_id = pg_id = None
    mysql_port = pg_port = None
    pw = secrets.token_hex(12)
    ro_pw = secrets.token_hex(12)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 120), **kw)


def _port(cid, port):
    out = _run(["docker", "port", cid, f"{port}/tcp"]).stdout.strip().splitlines()[0]
    return int(out.rsplit(":", 1)[1])


def start_env():
    if Env.started:
        return
    Env.tmp = Path(tempfile.mkdtemp(prefix="cec-it-"))
    envfile = Env.tmp / "db.env"
    envfile.write_text(f"MYSQL_ROOT_PASSWORD={Env.pw}\nPOSTGRES_PASSWORD={Env.pw}\n")
    envfile.chmod(0o600)
    Env.net = f"cec-it-{secrets.token_hex(4)}"
    _run(["docker", "network", "create", Env.net])
    Env.started = True
    Env.mysql_id = _run(["docker", "run", "-d", "--rm", "--network", Env.net, "--network-alias", "mysqlsrc",
                         "--env-file", str(envfile), "-p", "127.0.0.1::3306", "mysql:8.0",
                         "--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci"]).stdout.strip()
    Env.pg_id = _run(["docker", "run", "-d", "--rm", "--network", Env.net, "--network-alias", "pgtgt",
                      "--env-file", str(envfile), "-p", "127.0.0.1::5432", "postgres:16",
                      "-c", "ssl=on", "-c", "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
                      "-c", "ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"]).stdout.strip()
    if not Env.mysql_id or not Env.pg_id:
        raise RuntimeError("could not start containers")
    Env.mysql_port, Env.pg_port = _port(Env.mysql_id, 3306), _port(Env.pg_id, 5432)
    deadline = time.time() + 180
    while True:
        try:
            my("root").close()
            pg("postgres", "postgres").close()
            break
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(2)


def stop_env():
    for cid in (Env.mysql_id, Env.pg_id):
        if cid:
            _run(["docker", "rm", "-f", cid])
    if Env.net:
        _run(["docker", "network", "rm", Env.net])
    if Env.tmp:
        shutil.rmtree(Env.tmp, ignore_errors=True)


def tearDownModule():
    if Env.started:
        stop_env()


def my(user, db=None, pw=None):
    import pymysql
    return pymysql.connect(host="127.0.0.1", port=Env.mysql_port, user=user, password=pw or Env.pw, database=db,
                           ssl=_insecure_ctx(), charset="utf8mb4", autocommit=True, connect_timeout=5)


def pg(user, db, pw=None):
    import pg8000.native as pgn
    return pgn.Connection(host="127.0.0.1", port=Env.pg_port, user=user, password=pw or Env.pw, database=db,
                          ssl_context=_insecure_ctx(), timeout=10)


def my_exec(conn, sqls, params=None):
    with conn.cursor() as cur:
        for s in sqls:
            cur.execute(s)


def side_cfg(engine, db, user="cmp_ro", pw_env="CEC_IT_RO_PW"):
    return {"engine": engine, "host": "127.0.0.1", "port": Env.mysql_port if engine == "mysql" else Env.pg_port,
            "user": user, "database": db, "password_env": pw_env, "ssl_insecure": True}


MYSQL_SRC_DDL = [
    "CREATE DATABASE shop", "CREATE DATABASE app_ok", "CREATE DATABASE app_dms",
    """CREATE TABLE shop.items (
        id BIGINT UNSIGNED NOT NULL PRIMARY KEY, big BIGINT UNSIGNED NULL, flag TINYINT(1) NOT NULL,
        status ENUM('new','paid','shipped') NOT NULL, tags SET('a','b','c') NULL, doc JSON NULL,
        created DATETIME(6) NULL, updated TIMESTAMP(3) NULL, born DATE NULL, price DECIMAL(20,6) NOT NULL,
        ratio DOUBLE NULL, email VARCHAR(100) NOT NULL, note TEXT NULL, blobv VARBINARY(16) NULL, dur TIME(3) NULL,
        UNIQUE KEY uq_email (email)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    "CREATE TABLE shop.events (msg VARCHAR(50) NULL, at DATETIME NULL)",
    """CREATE TABLE app_ok.accounts (id BIGINT NOT NULL PRIMARY KEY, active TINYINT(1) NOT NULL, ext_id CHAR(36) NOT NULL,
        profile JSON NULL, labels JSON NULL, seen DATETIME(6) NULL, amount DECIMAL(20,6) NULL, raw DECIMAL(38,15) NULL,
        handle VARCHAR(100) COLLATE utf8mb4_bin NOT NULL, note VARCHAR(200) NULL, UNIQUE KEY uq_handle (handle))""",
    """CREATE TABLE app_dms.accounts (id BIGINT NOT NULL PRIMARY KEY, active VARCHAR(5) NOT NULL, ext_id VARCHAR(36) NOT NULL,
        profile LONGTEXT NULL, labels LONGTEXT NULL, seen DATETIME(6) NULL, amount DECIMAL(20,6) NULL, raw DECIMAL(28,6) NULL,
        handle VARCHAR(100) NOT NULL, note VARCHAR(200) NULL, UNIQUE KEY uq_handle (handle))""",
]

PG_TGT_SCHEMA = """
  id NUMERIC(20) NOT NULL PRIMARY KEY, big NUMERIC(20), flag BOOLEAN NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('new','paid','shipped')), tags TEXT[], doc JSONB,
  created {created}, updated TIMESTAMPTZ(3), born DATE, price {price} NOT NULL, ratio DOUBLE PRECISION,
  email {email} NOT NULL UNIQUE, note TEXT, blobv BYTEA, dur INTERVAL"""

PG_DMS_SCHEMA = """
  id BIGINT NOT NULL PRIMARY KEY, big BIGINT, flag SMALLINT NOT NULL, status VARCHAR(7) NOT NULL, tags VARCHAR(5),
  doc TEXT, created TIMESTAMP(6), updated TIMESTAMP(3), born DATE, price NUMERIC(20,6) NOT NULL, ratio DOUBLE PRECISION,
  email VARCHAR(100) NOT NULL, note TEXT, blobv BYTEA, dur VARCHAR(20)"""

ITEM_COLS = ["id", "big", "flag", "status", "tags", "doc", "created", "updated", "born", "price", "ratio", "email",
             "note", "blobv", "dur"]


def item_rows(n=60):
    out = []
    for i in range(1, n + 1):
        out.append({
            "id": i, "big": 18446744073709551615 if i == 7 else i * 1000, "flag": i % 2,
            "status": ("new", "paid", "shipped")[i % 3], "tags": ("a,c", "", None, "b")[i % 4],
            "doc": None if i % 10 == 0 else json.dumps({"b": i, "a": [1, 2.5], "s": "x" * (i % 3)}),
            "created": dt.datetime(2026, 1, 1) + dt.timedelta(minutes=i, microseconds=i * 7),
            "updated": dt.datetime(2026, 2, 1, 0, 0, 0, 123000) + dt.timedelta(minutes=i),
            "born": "0000-00-00" if i == 5 else (None if i == 6 else dt.date(1990, 1, 1) + dt.timedelta(days=i)),
            "price": (Decimal(i) / Decimal(7)).quantize(Decimal("0.000001")), "ratio": i / 3.0,
            "email": f"User{i}@Example.com", "note": f"héllo 한글 {i}" if i % 5 else None,
            "blobv": bytes([i, 255 - i]), "dur": dt.timedelta(hours=i, milliseconds=i)})
    return out


def account_rows(n=40):
    out = []
    for i in range(1, n + 1):
        handle = {3: "Bob", 4: "bob"}.get(i, f"user{i}")
        out.append({"active": i % 3 != 0, "ext_id": uuid.UUID(int=i * 7919), "profile": {"tier": i % 4, "tags": ["x", "y"][: i % 3]},
                    "labels": ["a", "b"] if i % 2 else [], "seen": dt.datetime(2026, 3, 1, tzinfo=UTC) + dt.timedelta(seconds=i * 61),
                    "amount": (Decimal(i) / Decimal(3)).quantize(Decimal("0.000001")),
                    "raw": Decimal("0.611111104488373") if i % 5 == 0 else (Decimal(i) / Decimal(9)).quantize(Decimal("1e-15")),
                    "handle": handle, "note": None if i % 7 == 0 else f"n{i}"})
    return out


def seed():
    """Source data, users, and the scripted target loads (once per module)."""
    if getattr(Env, "seeded", False):
        return
    m = my("root")
    my_exec(m, MYSQL_SRC_DDL)
    with m.cursor() as cur:
        cur.execute("SET SESSION sql_mode = ''")         # legacy data: allow the zero date
        cur.execute("SET time_zone = '+00:00'")
        for r in item_rows():
            cur.execute(f"INSERT INTO shop.items ({','.join(ITEM_COLS)}) VALUES ({','.join(['%s'] * len(ITEM_COLS))})",
                        [r[c] for c in ITEM_COLS])
        for i in range(10):
            cur.execute("INSERT INTO shop.events VALUES (%s, %s)", ("dup" if i < 4 else f"m{i}", None if i == 9 else
                                                                    dt.datetime(2026, 5, 1, 0, 0, i)))
        cur.execute(f"CREATE USER cmp_ro IDENTIFIED BY '{Env.ro_pw}'")
        cur.execute("GRANT SELECT ON shop.* TO cmp_ro")
        cur.execute("GRANT SELECT ON app_ok.* TO cmp_ro")
        cur.execute("GRANT SELECT ON app_dms.* TO cmp_ro")
        # pgloader load login: its plugin must match the server's default_authentication_plugin
        # (caching_sha2_password on MySQL 8.0 community) — see mysql-postgresql.md §11.
        cur.execute(f"CREATE USER pgl IDENTIFIED WITH caching_sha2_password BY '{Env.pw}'")
        cur.execute("GRANT SELECT, SHOW VIEW ON shop.* TO pgl")
    m.close()
    p = pg("postgres", "postgres")
    p.run("CREATE DATABASE tgt")
    p.run("CREATE DATABASE appsrc")
    p.run(f"CREATE ROLE cmp_ro LOGIN PASSWORD '{Env.ro_pw}'")
    p.close()
    t = pg("postgres", "tgt")
    t.run("CREATE EXTENSION citext")
    for schema in ("shop", "shop_dms", "shop_bad", "pgl"):
        t.run(f"CREATE SCHEMA {schema}")
    t.run("CREATE TABLE shop.items (" + PG_TGT_SCHEMA.format(created="TIMESTAMP(6)", price="NUMERIC(20,6)", email="CITEXT") + ")")
    t.run("CREATE TABLE shop.events (msg VARCHAR(50), at TIMESTAMP(0))")
    t.run("CREATE TABLE shop_dms.items (" + PG_DMS_SCHEMA + ")")
    t.run("CREATE TABLE shop_bad.items (" + PG_TGT_SCHEMA.format(created="TIMESTAMPTZ(6)", price="NUMERIC(20,2)", email="TEXT") + ")")
    # Read the MySQL source the way a loader would (driver values), convert, insert.
    src = my("root", "shop")
    with src.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")
        cur.execute(f"SELECT {','.join(ITEM_COLS)} FROM items ORDER BY id")
        rows = [dict(zip(ITEM_COLS, r)) for r in cur.fetchall()]
        cur.execute("SELECT msg, at FROM events")
        events = cur.fetchall()
    src.close()

    def zero_to_none(v):
        return None if isinstance(v, str) else v

    ins = ("INSERT INTO {s}.items (" + ",".join(ITEM_COLS) + ") VALUES (" +
           ",".join(f":{c}" for c in ITEM_COLS) + ")")
    kst = dt.timezone(dt.timedelta(hours=9))
    for r in rows:
        good = dict(r, flag=bool(r["flag"]), tags=None if r["tags"] is None else ([x for x in r["tags"].split(",") if x]),
                    doc=r["doc"], updated=r["updated"].replace(tzinfo=UTC) if r["updated"] else None,
                    born=zero_to_none(r["born"]), id=Decimal(r["id"]), big=Decimal(r["big"]))
        t.run(ins.format(s="shop").replace(":doc", "CAST(:doc AS jsonb)"), **good)
        bad = dict(good, created=r["created"].replace(tzinfo=kst), email=r["email"].lower())   # wrong session TZ, folded
        t.run(ins.format(s="shop_bad").replace(":doc", "CAST(:doc AS jsonb)"), **bad)
        if r["big"] > 9223372036854775807:
            continue          # DMS default BIGINT UNSIGNED -> BIGINT: this row cannot load
        dms = dict(r, flag=int(r["flag"]), updated=r["updated"], born=zero_to_none(r["born"]),
                   dur=None if r["dur"] is None else cec.Normalizer(cec.merge_options({"timestamp_precision": 3}))._time(r["dur"]))
        t.run(ins.format(s="shop_dms"), **dms)
    for msg, at in events:
        t.run("INSERT INTO shop.events VALUES (:m, :a)", m=msg, a=at)
    for schema in ("shop", "shop_dms", "shop_bad", "pgl"):
        t.run(f"GRANT USAGE ON SCHEMA {schema} TO cmp_ro")
        t.run(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO cmp_ro")
        t.run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO cmp_ro")
    t.close()
    # PostgreSQL source -> MySQL targets
    a = pg("postgres", "appsrc")
    a.run("""CREATE TABLE public.accounts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, active BOOLEAN NOT NULL,
             ext_id UUID NOT NULL, profile JSONB, labels TEXT[], seen TIMESTAMPTZ(6), amount NUMERIC(20,6), raw NUMERIC,
             handle TEXT NOT NULL UNIQUE, note VARCHAR(200))""")
    for r in account_rows():
        a.run("INSERT INTO accounts (active, ext_id, profile, labels, seen, amount, raw, handle, note) VALUES "
              "(:active, CAST(:ext_id AS uuid), CAST(:profile AS jsonb), :labels, :seen, :amount, :raw, :handle, :note)",
              **dict(r, ext_id=str(r["ext_id"]), profile=json.dumps(r["profile"])))
    a.run("GRANT USAGE ON SCHEMA public TO cmp_ro")
    a.run("GRANT SELECT ON ALL TABLES IN SCHEMA public TO cmp_ro")
    src_rows_ = a.run("SELECT id, active, ext_id, profile, labels, seen, amount, raw, handle, note FROM accounts ORDER BY id")
    a.close()
    m = my("root")
    with m.cursor() as cur:
        for r in src_rows_:
            id_, active, ext_id, profile, labels, seen, amount, raw, handle, note = r
            seen_utc = seen.astimezone(UTC).replace(tzinfo=None) if seen else None
            cur.execute("INSERT INTO app_ok.accounts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (id_, int(active), str(ext_id), json.dumps(profile), json.dumps(labels), seen_utc, amount, raw,
                         handle, note))
            labels_txt = "{" + ",".join(labels) + "}"
            try:   # DMS default mappings: 'true'/'false', PG array literal text, NUMERIC(28,6), ci unique
                cur.execute("INSERT INTO app_dms.accounts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (id_, "true" if active else "false", str(ext_id), json.dumps(profile), labels_txt, seen_utc,
                             amount, Decimal(raw).quantize(Decimal("0.000001"), rounding="ROUND_DOWN"), handle, note))
            except Exception as e:  # pymysql IntegrityError: 'bob' collides with 'Bob' under _ci
                if "Duplicate" not in str(e):
                    raise
    m.close()
    Env.seeded = True


@unittest.skipIf(DOCKER_SKIP, f"integration skipped: {DOCKER_SKIP}")
class DockerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_env()
        seed()
        os.environ["CEC_IT_RO_PW"] = Env.ro_pw
        os.environ["CEC_IT_ROOT_PW"] = Env.pw

    def cmp(self, src_engine, src_db, tgt_engine, tgt_db, tables, options=None, pg_driver=None):
        s = cec.connect_side(side_cfg(src_engine, src_db), "source", pg_driver)
        t = cec.connect_side(side_cfg(tgt_engine, tgt_db), "target", pg_driver)
        try:
            return cec.compare_tables(s, t, tables, options)
        finally:
            s.conn.close()
            t.conn.close()

    # MySQL -> PostgreSQL
    def test_mysql_to_pg_correct_conversion_matches(self):
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items", "shop.events"])
        items = rep["tables"]["shop.items"]
        self.assertEqual(items["verdict"], "MATCH", json.dumps(items, indent=1, default=str)[:3000])
        self.assertEqual(items["rows"], {"source": 60, "target": 60})
        self.assertEqual(items["zero_dates"]["source"], 1)
        self.assertEqual(rep["tables"]["shop.events"]["mode"], "multiset")
        self.assertEqual(rep["tables"]["shop.events"]["verdict"], "MATCH")
        self.assertEqual(rep["overall"], "MATCH")

    def test_mysql_to_pg_dms_default_mappings_are_flagged(self):
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items"],
                       {"tables": {"shop.items": {"target": "shop_dms.items"}}})
        r = rep["tables"]["shop.items"]
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["examples"]["missing_on_target"], [["7"]])    # BIGINT UNSIGNED overflow
        sev = {(f["column"], f["severity"]) for f in r["type_findings"] if "column" in f}
        for expected in (("flag", "mismatch"), ("doc", "mismatch"), ("big", "risk"), ("dur", "mismatch")):
            self.assertIn(expected, sev)

    def test_mysql_to_pg_wrong_conversions_are_pinpointed(self):
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items"],
                       {"tables": {"shop.items": {"target": "shop_bad.items"}}, "max_examples": 100})
        r = rep["tables"]["shop.items"]
        self.assertEqual(r["verdict"], "MISMATCH")
        cols = r["mismatches"]["differing_columns"]
        self.assertEqual(cols["created"], 60)       # +09:00 load session shifted every instant
        self.assertEqual(cols["email"], 60)         # case-folded values
        self.assertGreater(cols["price"], 40)       # NUMERIC(20,2) truncation
        self.assertIn(("price", "risk"), {(f.get("column"), f["severity"]) for f in r["type_findings"]})
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items"],
                       {"tables": {"shop.items": {"target": "shop_bad.items", "casefold_columns": ["email"]}},
                        "naive_timezone": "+09:00"})
        self.assertEqual(set(rep["tables"]["shop.items"]["mismatches"]["differing_columns"]), {"price"})

    def test_psycopg2_driver_path(self):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            self.skipTest("psycopg2 not installed")
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items"], pg_driver="psycopg2")
        self.assertEqual(rep["tables"]["shop.items"]["verdict"], "MATCH")

    def test_psycopg3_driver_path(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg (3) not installed")
        rep = self.cmp("postgres", "appsrc", "mysql", "app_ok", ["public.accounts"],
                       {"tables": {"public.accounts": {"target": "app_ok.accounts"}}}, pg_driver="psycopg")
        self.assertEqual(rep["tables"]["public.accounts"]["verdict"], "MATCH")

    def test_cli_end_to_end(self):
        cfg = Env.tmp / "cli.json"
        out = Env.tmp / "cli-report.json"
        cfg.write_text(json.dumps({"source": side_cfg("mysql", "shop"), "target": side_cfg("postgres", "tgt"),
                                   "tables": ["shop.items"]}))
        p = _run([sys.executable, "-B", str(ROOT / "shared/scripts/cross_engine_compare.py"), "--config", str(cfg),
                  "--out", str(out)], env={**os.environ})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(Env.ro_pw, p.stdout + p.stderr + out.read_text())
        self.assertEqual(json.loads(out.read_text())["overall"], "MATCH")
        cfg.write_text(json.dumps({"source": {**side_cfg("mysql", "shop"), "password": "x"},
                                   "target": side_cfg("postgres", "tgt"), "tables": ["shop.items"]}))
        p = _run([sys.executable, "-B", str(ROOT / "shared/scripts/cross_engine_compare.py"), "--config", str(cfg)])
        self.assertEqual(p.returncode, 3)
        self.assertIn("never a password", p.stderr)

    def test_compare_session_is_read_only(self):
        s = cec.connect_side(side_cfg("mysql", "shop", user="root", pw_env="CEC_IT_ROOT_PW"), "source")
        t = cec.connect_side(side_cfg("postgres", "tgt", user="postgres", pw_env="CEC_IT_ROOT_PW"), "target")
        try:
            s.begin()
            t.begin()
            with self.assertRaises(cec.CompareError):
                s.query("DELETE FROM shop.items")
            # even bypassing the guard, the transaction itself refuses writes
            with s.conn.cursor() as cur, self.assertRaises(Exception):
                cur.execute("DELETE FROM shop.items WHERE id = 1")
            with self.assertRaises(Exception):
                t.conn.run("DELETE FROM shop.items WHERE id = 1")
        finally:
            s.conn.close()
            t.conn.close()

    # pgloader (MySQL -> PostgreSQL, offline). dimitri/pgloader is amd64-only and Debian's
    # pgloader 3.6.7 package carries a QMyND that cannot read MySQL 8 (collation ids 76/255,
    # pgloader issues #877/#1592), so build v3.6.9 from source (native arm64/amd64), once.
    PGLOADER_IMAGE = "dbskill-pgloader:3.6.9-src"
    PGLOADER_DOCKERFILE = """FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends sbcl make curl git ca-certificates \\
    libsqlite3-dev libssl-dev freetds-dev unzip gawk zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v3.6.9 https://github.com/dimitri/pgloader.git /src
WORKDIR /src
RUN make save
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libsqlite3-0 libssl3 freetds-bin libzstd1 \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/pgloader /usr/local/bin/pgloader
"""
    # The CAST rules that made pgloader's output MATCH (mysql-postgresql.md §11).
    PGLOADER_CASTS = """SET PostgreSQL PARAMETERS timezone TO 'UTC'
CAST type bigint when unsigned to numeric drop typemod,
     type date to date drop default using zero-dates-to-null,
     type datetime to timestamp drop default drop not null using zero-dates-to-null,
     type time to interval drop typemod"""

    def pgloader_image(self):
        if getattr(Env, "pgl_image", None) is not None:
            return Env.pgl_image or self.skipTest(Env.pgl_skip)
        Env.pgl_image, Env.pgl_skip = "", None
        if _run(["docker", "image", "inspect", self.PGLOADER_IMAGE]).returncode != 0:
            d = Env.tmp / "pgloader-build"
            d.mkdir(exist_ok=True)
            (d / "Dockerfile").write_text(self.PGLOADER_DOCKERFILE)
            b = _run(["docker", "build", "-t", self.PGLOADER_IMAGE, str(d)], timeout=1800)
            if b.returncode != 0:
                Env.pgl_skip = "could not build the pgloader image (network?): " + (b.stderr.strip().splitlines() or ["?"])[-1]
                self.skipTest(Env.pgl_skip)
        v = _run(["docker", "run", "--rm", self.PGLOADER_IMAGE, "pgloader", "--version"], timeout=60)
        if v.returncode != 0:
            Env.pgl_skip = "pgloader image cannot run on this host: " + (v.stderr.strip().splitlines() or ["?"])[-1]
            self.skipTest(Env.pgl_skip)
        Env.pgl_image = self.PGLOADER_IMAGE
        return Env.pgl_image

    def pgloader_load(self, schema, casts):
        image = self.pgloader_image()
        load = Env.tmp / f"{schema}.load"
        # Credentials via MYSQL_PWD / PGPASSWORD (env-file), never in the load file or argv.
        load.write_text(f"""LOAD DATABASE
  FROM mysql://pgl@mysqlsrc:3306/shop
  INTO postgresql://postgres@pgtgt:5432/tgt
WITH include drop, create tables, create indexes, reset sequences, foreign keys, downcase identifiers
{casts}
INCLUDING ONLY TABLE NAMES MATCHING 'items', 'events'
ALTER SCHEMA 'shop' RENAME TO '{schema}'
;
""")
        envf = Env.tmp / "pgl.env"
        envf.write_text(f"MYSQL_PWD={Env.pw}\nPGPASSWORD={Env.pw}\n")
        envf.chmod(0o600)
        p = _run(["docker", "run", "--rm", "--network", Env.net, "--env-file", str(envf), "-v", f"{load}:/m.load:ro",
                  image, "pgloader", "/m.load"], timeout=600)
        # pgloader exits 0 even when a table's COPY failed — success must be judged from the data.
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        t = pg("postgres", "tgt")
        t.run(f"GRANT USAGE ON SCHEMA {schema} TO cmp_ro")
        t.run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO cmp_ro")
        types = {r[0]: r[1] for r in t.run(f"SELECT column_name, udt_name FROM information_schema.columns "
                                           f"WHERE table_schema = '{schema}' AND table_name = 'items'")}
        t.close()
        rep = self.cmp("mysql", "shop", "postgres", "tgt", ["shop.items", "shop.events"],
                       {"tables": {"shop.items": {"target": f"{schema}.items"}, "shop.events": {"target": f"{schema}.events"}}})
        return p.stdout + p.stderr, types, rep

    def test_pgloader_default_casts(self):
        log, types, rep = self.pgloader_load("pgl_default", "SET PostgreSQL PARAMETERS timezone TO 'UTC'")
        # What pgloader 3.6.9 maps by default for MySQL 8.0:
        self.assertEqual(types["flag"], "bool")             # TINYINT(1) -> boolean (good)
        self.assertEqual(types["status"], "items_status")   # ENUM -> CREATE TYPE (good)
        self.assertEqual(types["tags"], "_items_tags")      # SET -> array of an enum type (good)
        self.assertEqual(types["doc"], "json")              # JSON -> json (jsonb needs a CAST)
        self.assertEqual(types["created"], "timestamptz")   # DATETIME -> timestamptz (session-zone dependent)
        self.assertEqual(types["updated"], "timestamptz")   # TIMESTAMP -> timestamptz (good)
        self.assertEqual(types["big"], "int8")              # BIGINT UNSIGNED -> bigint (overflows)
        self.assertEqual(types["dur"], "time")              # TIME -> time (fails beyond 24 h)
        self.assertIn("Database error", log)                # ...and still exit code 0
        items = rep["tables"]["shop.items"]
        self.assertEqual(items["verdict"], "MISMATCH")
        self.assertEqual(items["rows"]["target"], 0)        # one bad value aborts the table's COPY batch
        self.assertEqual(rep["tables"]["shop.events"]["verdict"], "MATCH")   # no PK, multiset

    def test_pgloader_with_cast_rules_matches(self):
        _, types, rep = self.pgloader_load("pgl_cast", self.PGLOADER_CASTS)
        self.assertEqual((types["big"], types["created"], types["dur"]), ("numeric", "timestamp", "interval"))
        for t in ("shop.items", "shop.events"):
            self.assertEqual(rep["tables"][t]["verdict"], "MATCH", json.dumps(rep["tables"][t], default=str)[:3000])
        self.assertEqual(rep["tables"]["shop.items"]["rows"], {"source": 60, "target": 60})
        self.assertEqual(rep["tables"]["shop.items"]["zero_dates"]["source"], 1)

    # PostgreSQL -> MySQL
    def test_pg_to_mysql_correct_conversion_matches(self):
        rep = self.cmp("postgres", "appsrc", "mysql", "app_ok", ["public.accounts"],
                       {"tables": {"public.accounts": {"target": "app_ok.accounts"}}})
        r = rep["tables"]["public.accounts"]
        self.assertEqual(r["verdict"], "MATCH", json.dumps(r, indent=1, default=str)[:3000])
        self.assertIn(("raw", "risk"), {(f.get("column"), f["severity"]) for f in r["type_findings"]})

    def test_pg_to_mysql_dms_defaults_and_ci_unique_collision(self):
        rep = self.cmp("postgres", "appsrc", "mysql", "app_dms", ["public.accounts"],
                       {"tables": {"public.accounts": {"target": "app_dms.accounts"}}, "max_examples": 50})
        r = rep["tables"]["public.accounts"]
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["mismatches"]["examples"]["missing_on_target"], [["4"]])   # 'bob' vs 'Bob' under _ci
        cols = r["mismatches"]["differing_columns"]
        self.assertEqual(cols.get("raw"), 35)        # NUMERIC -> DECIMAL(28,6): every value with > 6 decimals
        self.assertEqual(cols.get("labels"), 39)     # PG array literal '{a,b}' stored as text is not JSON
        self.assertNotIn("active", cols)             # 'true'/'false' text equals the boolean... the TYPE is the defect
        self.assertNotIn("profile", cols)
        sev = {(f.get("column"), f["severity"]) for f in r["type_findings"]}
        for expected in (("active", "mismatch"), ("labels", "mismatch"), ("profile", "mismatch")):
            self.assertIn(expected, sev)

    def test_pg_timetz_to_mysql_time_live(self):
        a = pg("postgres", "appsrc")
        a.run("CREATE TABLE IF NOT EXISTS public.shifts (id INT PRIMARY KEY, at TIME WITH TIME ZONE)")
        a.run("TRUNCATE public.shifts")
        a.run("INSERT INTO public.shifts VALUES (1, '12:00:00+09'), (2, '23:30:00-05'), (3, NULL)")
        a.run("GRANT SELECT ON public.shifts TO cmp_ro")
        a.close()
        m = my("root")
        my_exec(m, ["CREATE TABLE IF NOT EXISTS app_ok.shifts (id INT PRIMARY KEY, at TIME NULL)", "DELETE FROM app_ok.shifts",
                    "INSERT INTO app_ok.shifts VALUES (1, '03:00:00'), (2, '04:30:00'), (3, NULL)"])   # UTC wall times
        m.close()
        opts = {"tables": {"public.shifts": {"target": "app_ok.shifts"}}}
        for drv in ("pg8000", "psycopg"):
            r = self.cmp("postgres", "appsrc", "mysql", "app_ok", ["public.shifts"], opts, pg_driver=drv)
            self.assertEqual(r["tables"]["public.shifts"]["verdict"], "NEEDS_REVIEW", drv)     # no silent MATCH
            r = self.cmp("postgres", "appsrc", "mysql", "app_ok", ["public.shifts"], {**opts, "timetz_policy": "utc"},
                         pg_driver=drv)
            self.assertEqual(r["tables"]["public.shifts"]["verdict"], "MATCH", (drv, r["tables"]["public.shifts"]))
            r = self.cmp("postgres", "appsrc", "mysql", "app_ok", ["public.shifts"], {**opts, "timetz_policy": "wallclock"},
                         pg_driver=drv)
            self.assertEqual(r["tables"]["public.shifts"]["verdict"], "MISMATCH", drv)          # offset matters

    # soak scripts in cross-family mode against the same containers
    def soak_dir(self, target_db, target_tables=None):
        d = Path(tempfile.mkdtemp(dir=Env.tmp))
        (d / "dashboard").mkdir()
        cfg = {"source": side_cfg("mysql", "shop"), "target": side_cfg("postgres", target_db),
               "tables": ["shop.items", "shop.events"], "checksum_tables": ["shop.items"], "n_total": 3,
               "watermark": {"enabled": True, "pk_margin": 5}, "cross_engine": {"tables": target_tables or {}}}
        (d / "dashboard" / "soak-config.json").write_text(json.dumps(cfg))
        return d

    def test_soak_check_cli_cross_family(self):
        d = self.soak_dir("tgt")
        p = _run([sys.executable, "-B", str(ROOT / "shared/scripts/soak_check.py"), "--config",
                  str(d / "dashboard" / "soak-config.json"), "--run-date", "2026-10-28"], env={**os.environ})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)     # period_evidence stays null -> needs review
        day = json.loads((d / "dashboard" / "status.json").read_text())["soak"]["days"][0]
        self.assertEqual({k: day["checks"][k] for k in ("row_count", "checksum", "schema_drift")},
                         {"row_count": True, "checksum": True, "schema_drift": True}, json.dumps(day["detail"], default=str)[:3000])
        items = day["detail"]["row_count"]["shop.items"]
        self.assertEqual((items["mode"], items["watermark"], items["comparison"]), ("pk_watermark", 55, "cross_engine_normalized"))
        self.assertEqual(day["detail"]["row_count"]["shop.events"]["mode"], "whole_table")
        self.assertTrue((d / "soak-report-day1.md").exists())
        self.assertNotIn(Env.ro_pw, (d / "dashboard" / "status.json").read_text())
        # same pair against the DMS-default target: RED with the reasons
        d2 = self.soak_dir("tgt", {"shop.items": {"target": "shop_dms.items"}})
        p = _run([sys.executable, "-B", str(ROOT / "shared/scripts/soak_check.py"), "--config",
                  str(d2 / "dashboard" / "soak-config.json"), "--run-date", "2026-10-28"], env={**os.environ})
        day = json.loads((d2 / "dashboard" / "status.json").read_text())["soak"]["days"][0]
        self.assertEqual((day["checks"]["checksum"], day["checks"]["schema_drift"], day["overall"]), (False, False, "red"))
        self.assertIn("flag", day["detail"]["schema_drift"]["shop.items"]["mismatched_attributes"])

    def test_lambda_run_day_cross_family_matches_standalone(self):
        import soak_check_lambda as L
        cfg = {"source_engine": "mysql", "target_engine": "aurora-postgresql", "tables": ["shop.items", "shop.events"],
               "checksum_tables": ["shop.items"], "watermark": {"pk_margin": 5}, "cross_engine": {}, "family": "mysql"}
        sconn = my("cmp_ro", "shop", Env.ro_pw)
        tconn = pg("cmp_ro", "tgt", Env.ro_pw)    # pg8000.native, as the Lambda connects
        try:
            lam = L.run_day(cfg, sconn, tconn, "2026-10-28")
        finally:
            sconn.close()
            tconn.close()
        d = self.soak_dir("tgt")
        std = soak_check.run_day(json.loads((d / "dashboard" / "soak-config.json").read_text()), "2026-10-28")
        self.assertEqual(lam["checks"], std["checks"])
        self.assertEqual(lam["detail"]["row_count"], std["detail"]["row_count"])
        self.assertEqual(lam["detail"]["checksum"]["shop.items"]["source"], std["detail"]["checksum"]["shop.items"]["source"])
        self.assertIs(lam["checks"]["checksum"], True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
