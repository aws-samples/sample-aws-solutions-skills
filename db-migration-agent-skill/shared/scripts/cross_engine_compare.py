#!/usr/bin/env python3
"""Cross-engine (MySQL-family <-> PostgreSQL-family) table comparison: row counts plus a
canonical, normalized hash per row and per PK-ordered chunk, so a MySQL table and its
PostgreSQL copy (or the reverse) can be compared even though the two engines render the
same data differently (TINYINT(1) 1 vs boolean t, DATETIME vs timestamptz, JSON key order,
DECIMAL scale, UUID case, ...). Plain CHECKSUM TABLE / md5(string_agg) results can never
match across engines — this module normalizes every value to one documented canonical
text form first (see `Normalizer` and shared/reference/mysql-postgresql.md §Validation).

Used three ways:
  1. CLI — Phase 7 validation of a heterogeneous pair:
       python3 cross_engine_compare.py --config compare-config.json --out report.json
     The config has the same `source`/`target` shape as soak-config.json (engine, host,
     port, user, database, password_env | secret_arn, ssl_ca, ssl_insecure) plus an
     optional `cross_engine` options object (DEFAULT_OPTIONS below).
     --workers N: parallel PK-range worker processes (each range in its own snapshot pair —
     per-range snapshots, NOT a single consistent cut: a MATCH is proof only with
     --writes-frozen (cutover freeze); on live data an all-ranges MATCH is NEEDS_REVIEW);
     --writes-frozen: the caller asserts writes are stopped on both sides for the run;
     --progress-file: atomically rewritten JSON progress for the dashboard (+ stderr lines).
  2. Library — soak_check.py / soak_check_lambda.py call `soak_checks()` for a
     mysql<->postgres pair (Phase 7.7), with the same watermark concept as their
     homogeneous path (pk <= min(source MAX, target MAX) - margin).
  3. Preflight — soak_check_lambda.py's preflight runs `probe_sqls()`: exactly the
     catalog/count/select statements a run issues, under `1=0`.

SAFETY: read-only. Every statement is checked against a SELECT/SET/transaction-control
allow-list before it is sent, and each side runs inside a READ ONLY transaction.
Credentials come only from an environment variable named in the config (password_env)
or Secrets Manager (secret_arn) — never argv, never the config file itself. Runtime is
bounded (max_seconds; chunked keyset reads; multiset_max_rows for key-less tables); a
comparison that runs out of budget is NEEDS_REVIEW, never MATCH.

Verdicts use the soak scripts' four-state model: MATCH -> True, MISMATCH -> False,
NEEDS_REVIEW -> None, NOT_APPLICABLE -> "not_applicable".

Drivers: pymysql for MySQL; pg8000 (native or DB-API), psycopg (3) or psycopg2 for
PostgreSQL — whichever is importable (or `pg_driver`). Python 3.9+, no other deps.
KEEP the soak-facing API (`SqlSide`, `soak_checks`, `probe_sqls`, `pk_watermark`) stable:
soak_check.py and soak_check_lambda.py import it (the Lambda asset copies this file flat
next to the handler — cdk-stacks.md §soak-stack.ts).
"""
import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
import re
import ssl
import sys
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path

VERSION = 3   # v2: REPEATABLE READ before the MySQL snapshot, workers, progress, 0101 zero-date finding
#              v3: per-range consistency + writes_frozen, range_errors, coordinator deadline, paired-only 0101
MYSQL_FAMILY = {"mysql", "mariadb", "aurora-mysql"}
POSTGRES_FAMILY = {"postgres", "postgresql", "aurora-postgresql"}
VERDICT_CHECK = {"MATCH": True, "MISMATCH": False, "NEEDS_REVIEW": None, "NOT_APPLICABLE": "not_applicable"}

DEFAULT_OPTIONS = {
    # Zone naive values (MySQL DATETIME, PG timestamp without time zone) are interpreted in
    # before conversion to a UTC instant. "UTC", a fixed offset "+09:00", or an IANA name.
    "naive_timezone": "UTC",
    "timestamp_precision": 6,        # fractional-second digits kept (0-6); truncation, not rounding
    "bool_policy": "strict",         # strict: boolean vs integer/text column pairs are a type MISMATCH
    "tinyint1_as_boolean": True,     # MySQL TINYINT(1)/BOOL is the boolean kind
    "bit1_as_boolean": True,         # MySQL BIT(1) is the boolean kind
    # MySQL 0000-00-00 dates: "null" (equal to a target NULL — what pgloader writes) | "distinct".
    # DMS 3.6.1 wrote 0101-01-01 on Aurora PG 17 instead: paired per key, that is its own
    # finding (zero_date_as_0101), never equal to anything.
    "zero_date": "null",
    "text_casefold": False,          # compare text case-insensitively (collation-sensitive checks)
    "text_rstrip": False,            # ignore trailing spaces on VARCHAR/TEXT (PAD SPACE collations)
    "float4_digits": 7,
    "float8_digits": 15,
    "strict_types": True,            # a type finding of severity "mismatch" fails the table
    "chunk_size": 5000,
    "max_seconds": 600,
    "multiset_max_rows": 200000,     # key-less tables: compared as a multiset, bounded
    "max_examples": 20,
    # CLI only (needs the connection config): >1 = parallel PK-range workers, one process and
    # one snapshot pair PER RANGE — exact while writes are frozen, but on live data
    # the ranges' snapshots differ by the scan time (see main() --help and §9).
    "workers": 1,
    # The caller asserts that writes are stopped on BOTH sides for the whole run (cutover
    # freeze). Recorded in the report; with workers > 1 it is the only way a MATCH counts as
    # proof, because each range is read in its own snapshot pair (not a single cut).
    "writes_frozen": False,
    "include_values": False,         # report canonical values of differing columns (may be PII)
    "pg_driver": None,               # None = auto (pg8000, psycopg, psycopg2)
    # PG TIME WITH TIME ZONE vs an unzoned TIME/INTERVAL: None (default) = not comparable ->
    # NEEDS_REVIEW + finding; "utc" = compare the UTC wall time (unzoned side read as UTC);
    # "wallclock" = drop the offset (explicitly accepts the offset loss DMS performs).
    "timetz_policy": None,
    "tables": {},                    # per-table overrides, see _table_options
}
_TABLE_OPTION_KEYS = {"target", "columns", "kinds", "key", "ignore_columns", "casefold_columns", "skip",
                      "key_range", "tail_keys"}
_KINDS = {"int", "decimal", "float4", "float8", "bool", "text", "char", "enum", "set", "json", "uuid",
          "binary", "bits", "date", "datetime", "timestamp_utc", "time", "timetz", "interval", "array", "unknown",
          "unsupported"}
_HIGH = decimal.Context(prec=400)
_DEFAULT_CA_BUNDLES = (Path(__file__).resolve().parent.parent / "assets" / "rds-global-bundle.pem",
                       Path(__file__).resolve().parent / "rds-global-bundle.pem")


class CompareError(ValueError):
    """Configuration / schema problem the caller should surface as a clean one-line message."""


class _Budget(Exception):
    pass


class _OrderError(Exception):
    pass


def engine_family(engine):
    e = (engine or "").strip().lower()
    if e in MYSQL_FAMILY:
        return "mysql"
    if e in POSTGRES_FAMILY:
        return "postgres"
    raise CompareError(f"Unsupported engine '{engine}' — cross_engine_compare supports MySQL-family "
                       "(mysql/mariadb/aurora-mysql) and PostgreSQL-family (postgres/postgresql/aurora-postgresql).")


def merge_options(options):
    opts = dict(DEFAULT_OPTIONS)
    unknown = sorted(set(options or {}) - set(DEFAULT_OPTIONS))
    if unknown:
        raise CompareError(f"unknown cross_engine option(s): {unknown}")
    if "writes_frozen" in (options or {}) and not isinstance(options["writes_frozen"], bool):
        # An assertion that turns per-range snapshots into proof must be unambiguous: only a
        # JSON true/false is accepted ("false", 0, null, ... are configuration errors).
        raise CompareError(f"writes_frozen must be a JSON boolean (true/false), got {options['writes_frozen']!r} "
                           f"({type(options['writes_frozen']).__name__})")
    opts.update({k: v for k, v in (options or {}).items() if v is not None})
    if opts["bool_policy"] not in ("strict", "numeric"):
        raise CompareError("bool_policy must be 'strict' or 'numeric'")
    if opts["zero_date"] not in ("null", "distinct"):
        raise CompareError("zero_date must be 'null' or 'distinct'")
    if opts["timetz_policy"] not in (None, "utc", "wallclock"):
        raise CompareError("timetz_policy must be null, 'utc' or 'wallclock'")
    if not 1 <= int(opts["workers"]) <= 64:
        raise CompareError("workers must be 1-64")
    if not 0 <= int(opts["timestamp_precision"]) <= 6:
        raise CompareError("timestamp_precision must be 0-6")
    for t, to in (opts["tables"] or {}).items():
        bad = sorted(set(to or {}) - _TABLE_OPTION_KEYS)
        if bad:
            raise CompareError(f"unknown option(s) {bad} for table {t!r}")
        for col, kind in ((to or {}).get("kinds") or {}).items():
            if kind not in _KINDS:
                raise CompareError(f"unknown kind {kind!r} for {t}.{col}")
        _validate_scope(t, to or {})
    _tzinfo(opts["naive_timezone"])  # validate early
    return opts


def _validate_scope(table, to):
    """Per-table partial-scan options (in-freeze quick checks): key_range [lo, hi] = keys in
    (lo, hi] on the single integer key (null = unbounded), or tail_keys N = the last N key values
    by the source's MAX(key) at snapshot time. Mutually exclusive; JSON integers only."""
    kr, tk = to.get("key_range"), to.get("tail_keys")
    if kr is not None and tk is not None:
        raise CompareError(f"table {table!r}: key_range and tail_keys are mutually exclusive")
    if kr is not None:
        if (not isinstance(kr, list) or len(kr) != 2
                or any(b is not None and (isinstance(b, bool) or not isinstance(b, int)) for b in kr)):
            raise CompareError(f"table {table!r}: key_range must be [lo, hi] of JSON integers or null (meaning (lo, hi])")
        if kr[0] is not None and kr[1] is not None and kr[0] >= kr[1]:
            raise CompareError(f"table {table!r}: key_range lo must be < hi")
    if tk is not None and (isinstance(tk, bool) or not isinstance(tk, int) or tk < 1):
        raise CompareError(f"table {table!r}: tail_keys must be a positive JSON integer")


def scope_range(src_side, plan, opts):
    """(lo, hi] for a table with key_range / tail_keys, else None. Read inside the snapshot."""
    to = _table_options(opts, plan.table)
    if to.get("key_range") is None and to.get("tail_keys") is None:
        return None
    if not parallel_eligible(plan):
        raise CompareError("key_range / tail_keys need one integer key column on both sides")
    if to.get("key_range") is not None:
        return tuple(to["key_range"])
    hi = src_side.query(minmax_sql(src_side.dialect, plan.src, plan.key[0].src.name, "MAX"))[0][0]
    if hi is None:
        return (None, None)
    return (int(hi) - int(to["tail_keys"]), None)   # upper end open: keys above the source MAX are compared too


def _mark_partial(rep, key_range, to):
    rng = [None if b is None else str(b) for b in key_range]
    rep["coverage"] = {"partial": True, "key_range": rng, "tail_keys": to.get("tail_keys")}
    rep["notes"].append(f"PARTIAL scan: only keys in ({rng[0]}, {rng[1]}] were compared — evidence for that key "
                        "range only, never a full-table result")
    return rep


# ── Identifiers and literals ────────────────────────────────────────────────────────────
# Same parsing as soak_check*.py _table_parts (test_cross_engine_compare.py asserts parity).
def table_parts(table):
    pattern = r'\s*(?:"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|([^.`"\s][^.`"]*?))\s*(\.|$)'
    parts, offset, separator = [], 0, None
    for match in re.finditer(pattern, table):
        if match.start() != offset:
            raise CompareError(f"Invalid table identifier: {table!r}")
        quoted, backtick, plain, separator = match.groups()
        parts.append(quoted.replace('""', '"') if quoted is not None else
                     backtick.replace('``', '`') if backtick is not None else plain.strip())
        offset = match.end()
    if offset != len(table) or not 1 <= len(parts) <= 2 or separator == '.':
        raise CompareError(f"Invalid table identifier: {table!r}")
    return parts


def qident(dialect, name):
    q = '`' if dialect == "mysql" else '"'
    return q + str(name).replace(q, q * 2) + q


def qtable(dialect, schema, name):
    return f"{qident(dialect, schema)}.{qident(dialect, name)}"


def str_literal(dialect, value):
    if dialect == "mysql":
        return "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def literal(dialect, value):
    """Side-native SQL literal for a key/bound value as the driver returned it."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return ("1" if value else "0") if dialect == "mysql" else ("TRUE" if value else "FALSE")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        h = bytes(value).hex()
        return f"X'{h}'" if dialect == "mysql" else f"decode('{h}', 'hex')"
    if isinstance(value, dt.datetime):
        return "'" + value.isoformat(sep=" ", timespec="microseconds") + "'"
    if isinstance(value, (dt.date, dt.time)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, uuid.UUID):
        return f"'{value}'"
    return str_literal(dialect, str(value))


_READ_ONLY = re.compile(r"^\s*(SELECT|WITH|SHOW|SET\s+(SESSION\s+)?(time_zone|TimeZone|max_execution_time|max_statement_time"
                        r"|statement_timeout)\b|SET\s+SESSION\s+TRANSACTION\s+ISOLATION\s+LEVEL\s+REPEATABLE\s+READ\s*$"
                        r"|START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK)\b",
                        re.IGNORECASE)
# MySQL-family snapshot: `START TRANSACTION WITH CONSISTENT SNAPSHOT` is IGNORED unless the
# session isolation is REPEATABLE READ (MySQL 8.0/8.4 manual, §15.3.1: "For all other isolation
# levels, the WITH CONSISTENT SNAPSHOT clause is ignored") — a server/parameter group with
# transaction_isolation=READ-COMMITTED turns the "snapshot" into a moving view (dry run 5:
# 78 spurious money mismatches). SESSION scope also applies on MariaDB and the Aurora MySQL
# writer (Aurora readers are always REPEATABLE READ). Shared with soak_check*.py.
MYSQL_SNAPSHOT_ISOLATION = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"


_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`")


def assert_read_only(sql):
    """Allow-list: one SELECT/SHOW/WITH, session time-zone SET, or transaction control —
    nothing else, and never a second statement (';' outside quoted literals/identifiers)."""
    bare = _QUOTED.sub("''", sql).strip().rstrip(";")
    dml = re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE\s+INTO|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|CALL|DO)\b",
                    bare, re.IGNORECASE)
    if dml and not re.match(r"^\s*(SET|START|BEGIN|COMMIT|ROLLBACK)\b", bare, re.IGNORECASE):
        raise CompareError(f"refusing non-read-only statement: {sql[:80]!r}")
    if not _READ_ONLY.match(sql) or ";" in bare or re.search(r"\b(INTO\s+OUTFILE|INTO\s+DUMPFILE|FOR\s+UPDATE)\b",
                                                                bare, re.IGNORECASE):
        raise CompareError(f"refusing non-read-only statement: {sql[:80]!r}")


# ── Catalog SQL ─────────────────────────────────────────────────────────────────────────
def resolve_table_sql(dialect, schema, table):
    s = str_literal(dialect, schema) if schema is not None else ("DATABASE()" if dialect == "mysql" else "current_schema()")
    t = str_literal(dialect, table)
    if dialect == "mysql":
        return ("SELECT table_schema, table_name FROM information_schema.tables "
                f"WHERE LOWER(table_schema)=LOWER({s}) AND LOWER(table_name)=LOWER({t})")
    return ("SELECT n.nspname, c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE c.relkind IN ('r','p','v','m','f') AND lower(n.nspname)=lower({s}) AND lower(c.relname)=lower({t})")


def catalog_sql(dialect, schema, table):
    s, t = str_literal(dialect, schema), str_literal(dialect, table)
    if dialect == "mysql":
        return ("SELECT column_name, data_type, column_type, numeric_precision, numeric_scale, datetime_precision, "
                f"is_nullable FROM information_schema.columns WHERE table_schema={s} AND table_name={t} "
                "ORDER BY ordinal_position")
    return ("SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), ty.typtype, ty.typcategory, "
            "CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, et.typname "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_type ty ON ty.oid=a.atttypid "
            "LEFT JOIN pg_catalog.pg_type et ON et.oid=ty.typelem AND ty.typcategory='A' "
            f"WHERE n.nspname={s} AND c.relname={t} AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum")


def pk_sql(dialect, schema, table):
    s, t = str_literal(dialect, schema), str_literal(dialect, table)
    if dialect == "mysql":
        return ("SELECT column_name FROM information_schema.key_column_usage "
                f"WHERE table_schema={s} AND table_name={t} AND constraint_name='PRIMARY' ORDER BY ordinal_position")
    return ("SELECT a.attname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=ANY(i.indkey) "
            f"WHERE i.indisprimary AND n.nspname={s} AND c.relname={t} "
            "ORDER BY array_position(i.indkey::int2[], a.attnum)")


# ── Column classification ───────────────────────────────────────────────────────────────
class Col:
    __slots__ = ("name", "dialect", "kind", "raw_type", "nullable", "scale", "precision", "fsp", "bits",
                 "unsigned", "base", "phys")

    def __init__(self, name, dialect, kind, raw_type, nullable, scale=None, precision=None, fsp=None,
                 bits=None, unsigned=False, base=None, phys=None):
        self.name, self.dialect, self.kind, self.raw_type, self.nullable = name, dialect, kind, raw_type, nullable
        self.scale, self.precision, self.fsp, self.bits, self.unsigned = scale, precision, fsp, bits, unsigned
        # `kind` is the comparison kind (a `kinds` override may change it); `phys` is always the
        # catalog type's kind — it decides how the DRIVER represents the value (JSON text vs
        # parsed, array literal, naive vs UTC timestamps) and how the engine orders keys.
        self.base, self.phys = base, phys or kind

    def as_dict(self):
        return {"name": self.name, "type": self.raw_type, "kind": self.kind, "nullable": self.nullable}


_MYSQL_INT_MAX = {"tinyint": 127, "smallint": 32767, "mediumint": 8388607, "int": 2147483647, "integer": 2147483647,
                  "bigint": 9223372036854775807, "year": 9999}
_PG_INT_MAX = {"smallint": 32767, "integer": 2147483647, "bigint": 9223372036854775807}


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _strs(row, n):
    vals = [v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v for v in list(row)]
    return (vals + [None] * n)[:n]


def classify_mysql(row, opts):
    name, data_type, column_type, nprec, nscale, dprec, nullable = _strs(row, 7)
    d = str(data_type or "").lower()
    ct = str(column_type or d).lower()
    unsigned = "unsigned" in ct
    kw = {"nullable": str(nullable).upper() == "YES", "unsigned": unsigned, "base": d}
    if d == "tinyint" and opts["tinyint1_as_boolean"] and ct.startswith("tinyint(1)"):
        return Col(name, "mysql", "bool", ct, **kw)
    if d in ("tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"):
        return Col(name, "mysql", "int", ct, **kw)
    if d in ("decimal", "numeric"):
        return Col(name, "mysql", "decimal", ct, scale=_int_or_none(nscale), precision=_int_or_none(nprec), **kw)
    if d == "float":
        return Col(name, "mysql", "float4", ct, **kw)
    if d in ("double", "real"):
        return Col(name, "mysql", "float8", ct, **kw)
    if d == "bit":
        n = _int_or_none(nprec) or 1
        if n == 1 and opts["bit1_as_boolean"]:
            return Col(name, "mysql", "bool", ct, bits=1, **kw)
        return Col(name, "mysql", "bits", ct, bits=n, **kw)
    if d == "char":
        return Col(name, "mysql", "char", ct, **kw)
    if d in ("varchar", "tinytext", "text", "mediumtext", "longtext"):
        return Col(name, "mysql", "text", ct, **kw)
    if d == "enum":
        return Col(name, "mysql", "enum", ct, **kw)
    if d == "set":
        return Col(name, "mysql", "set", ct, **kw)
    if d == "json":
        return Col(name, "mysql", "json", ct, **kw)
    if d in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        return Col(name, "mysql", "binary", ct, **kw)
    if d == "date":
        return Col(name, "mysql", "date", ct, **kw)
    if d == "datetime":
        return Col(name, "mysql", "datetime", ct, fsp=_int_or_none(dprec) or 0, **kw)
    if d == "timestamp":
        return Col(name, "mysql", "timestamp_utc", ct, fsp=_int_or_none(dprec) or 0, **kw)
    if d == "time":
        return Col(name, "mysql", "time", ct, fsp=_int_or_none(dprec) or 0, **kw)
    if d in ("geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon",
             "geometrycollection", "geomcollection"):
        return Col(name, "mysql", "unsupported", ct, **kw)
    return Col(name, "mysql", "unknown", ct, **kw)


def classify_pg(row, opts):
    name, fmt, typtype, typcat, nullable, elem = _strs(row, 6)
    f = str(fmt or "").lower()
    f = re.sub(r'^(?:"[^"]+"|[a-z0-9_]+)\.', "", f)   # schema-qualified (e.g. public.citext)
    kw = {"nullable": str(nullable).upper() == "YES"}
    m = re.match(r"([a-z0-9_ ]+?)(?:\((\d+)(?:,\s*(\d+))?\))?( with(?:out)? time zone)?(\[\])?$", f)
    base = m.group(1).strip() if m else f
    p1, p2 = (_int_or_none(m.group(2)), _int_or_none(m.group(3))) if m else (None, None)
    kw["base"] = base
    if str(typcat or "") == "A" or f.endswith("[]"):
        return Col(name, "postgres", "array", f, **kw)
    if str(typtype or "") == "e":
        return Col(name, "postgres", "enum", f, **kw)
    if base == "boolean":
        return Col(name, "postgres", "bool", f, **kw)
    if base in ("smallint", "integer", "bigint"):
        return Col(name, "postgres", "int", f, **kw)
    if base == "numeric":
        return Col(name, "postgres", "decimal", f, precision=p1, scale=(p2 or 0) if p1 is not None else None, **kw)
    if base == "real":
        return Col(name, "postgres", "float4", f, **kw)
    if base == "double precision":
        return Col(name, "postgres", "float8", f, **kw)
    if base in ("character", "bpchar"):
        return Col(name, "postgres", "char", f, **kw)
    if base in ("character varying", "text", "citext", "name"):
        return Col(name, "postgres", "text", f, **kw)
    if base in ("json", "jsonb"):
        return Col(name, "postgres", "json", f, **kw)
    if base == "uuid":
        return Col(name, "postgres", "uuid", f, **kw)
    if base == "bytea":
        return Col(name, "postgres", "binary", f, **kw)
    if base in ("bit", "bit varying"):
        return Col(name, "postgres", "bits", f, bits=p1 or 1, **kw)
    if base == "date":
        return Col(name, "postgres", "date", f, **kw)
    if base == "timestamp":
        kind = "timestamp_utc" if "with time zone" in f else "datetime"
        return Col(name, "postgres", kind, f, fsp=6 if p1 is None else p1, **kw)
    if base == "time":
        kind = "timetz" if "with time zone" in f else "time"
        return Col(name, "postgres", kind, f, fsp=6 if p1 is None else p1, **kw)
    if base == "interval":
        return Col(name, "postgres", "interval", f, **kw)
    if base in ("inet", "cidr", "macaddr", "macaddr8", "money", "xml"):
        return Col(name, "postgres", "text", f, **kw)
    if base in ("point", "line", "lseg", "box", "path", "polygon", "circle", "geometry", "geography"):
        return Col(name, "postgres", "unsupported", f, **kw)
    return Col(name, "postgres", "unknown", f, **kw)


def _with_kind(col, kind):
    c = Col(col.name, col.dialect, kind, col.raw_type, col.nullable, col.scale, col.precision, col.fsp,
            col.bits, col.unsigned, col.base, phys=col.phys)
    return c


def _int_max(col):
    if col.kind != "int":
        return None
    if col.dialect == "mysql":
        m = _MYSQL_INT_MAX.get(col.base)
        return None if m is None else (2 * m + 1 if col.unsigned and col.base != "year" else m)
    return _PG_INT_MAX.get(col.base)


# ── Pair resolution ─────────────────────────────────────────────────────────────────────
_NUM, _FLT, _TEXTISH, _TS = {"int", "decimal"}, {"float4", "float8"}, {"text", "char", "enum"}, {"datetime", "timestamp_utc"}


class ColPlan:
    __slots__ = ("src", "tgt", "as_kind", "findings", "casefold", "bits")

    def __init__(self, src, tgt, as_kind, findings, casefold=False, bits=None):
        self.src, self.tgt, self.as_kind, self.findings, self.casefold, self.bits = src, tgt, as_kind, findings, casefold, bits


def resolve_pair(s, t, opts, forced=False):
    """(as_kind, findings[]) for one mapped column pair. Severity: "mismatch" (a type-mapping
    defect — fails the table when strict_types), "risk" (data can be lost by future values),
    "warning" (semantics differ; values decide), "info"."""
    ks, kt = s.kind, t.kind
    strict = "mismatch" if opts["strict_types"] and not forced else "warning"
    out = []

    def f(sev, msg):
        out.append({"severity": sev, "message": msg})

    if "unsupported" in (ks, kt):
        f("unsupported", f"{s.raw_type} / {t.raw_type}: no canonical cross-engine form (spatial/geometric) — "
                         "ignore_columns it and validate with an app-level check")
        return "unsupported", out
    if ks in _NUM and kt in _NUM:
        smax, tmax = _int_max(s), _int_max(t)
        if smax is not None and tmax is not None and smax > tmax:
            f("risk", f"integer range: {s.raw_type} max {smax} > {t.raw_type} max {tmax} — values above it cannot load "
                      "(e.g. BIGINT UNSIGNED -> BIGINT, the DMS default); use NUMERIC(20)/a wider type")
        if ks == "decimal" and kt == "decimal":
            if s.scale is None and t.scale is not None:
                f("risk", f"unbounded {s.raw_type} -> {t.raw_type}: digits beyond scale {t.scale} are cut "
                          "(DMS default for unbounded NUMERIC is NUMERIC(28,6))")
            elif s.scale is not None and t.scale is not None and t.scale < s.scale:
                f("risk", f"scale narrows {s.scale} -> {t.scale}: truncation")
        elif kt == "int" and ks == "decimal" and (s.scale or 0) > 0:
            f("risk", f"{s.raw_type} -> {t.raw_type}: fractional part lost")
        return "numeric", out
    if ks in _FLT and kt in _FLT:
        d = min(opts["float4_digits"] if k == "float4" else opts["float8_digits"] for k in (ks, kt))
        if ks != kt:
            f("warning", f"{s.raw_type} vs {t.raw_type}: compared at {d} significant digits")
        return f"float:{d}", out
    if (ks in _FLT and kt in _NUM) or (ks in _NUM and kt in _FLT):
        k = ks if ks in _FLT else kt
        d = opts["float4_digits"] if k == "float4" else opts["float8_digits"]
        f("warning", f"approximate vs exact numeric ({s.raw_type} / {t.raw_type}): compared at {d} significant digits")
        return f"float:{d}", out
    if ks == "bool" and kt == "bool":
        return "bool", out
    if "bool" in (ks, kt) and ({ks, kt} - {"bool"}) <= (_NUM | {"bits"}):
        sev = strict if opts["bool_policy"] == "strict" else "info"
        f(sev, f"boolean vs integer ({s.raw_type} / {t.raw_type}) — e.g. the DMS default TINYINT(1) -> SMALLINT; "
               "apps using true/false break. Pre-create BOOLEAN (or set bool_policy=numeric to accept 0/1)")
        return "bool_numeric", out
    if "bool" in (ks, kt) and ({ks, kt} - {"bool"}) <= _TEXTISH:
        f(strict, f"boolean stored as text ({s.raw_type} / {t.raw_type}) — the DMS default PG BOOLEAN -> "
                  "VARCHAR(5) 'true'/'false'; pre-create TINYINT(1) and convert")
        return "bool", out
    if ks in _TEXTISH and kt in _TEXTISH:
        if "enum" in (ks, kt) and ks != kt:
            f("warning", f"ENUM vs text ({s.raw_type} / {t.raw_type}): allowed-value constraint not enforced on one side")
        return ("char" if "char" in (ks, kt) else "text"), out
    if "set" in (ks, kt) and ({ks, kt} - {"set"}) <= (_TEXTISH | {"array", "set"}):
        if ({ks, kt} - {"set"}) & _TEXTISH:
            f("warning", f"SET vs text ({s.raw_type} / {t.raw_type}): membership constraint not enforced")
        return "set", out
    if ks == "json" and kt == "json":
        return "json", out
    if {ks, kt} <= {"json", "array"}:
        return "json", out
    if "json" in (ks, kt) and ({ks, kt} - {"json"}) <= _TEXTISH:
        f(strict, f"JSON stored as text ({s.raw_type} / {t.raw_type}) — the DMS default JSON -> TEXT / JSONB -> "
                  "LONGTEXT; pre-create JSON/JSONB (or force kind 'json' to accept)")
        return "json", out
    if "array" in (ks, kt) and ({ks, kt} - {"array"}) <= _TEXTISH:
        f(strict, f"array stored as text ({s.raw_type} / {t.raw_type}) — the DMS default ARRAY -> TEXT; "
                  "pre-create JSON (MySQL) and convert")
        return "json", out
    if "uuid" in (ks, kt) and ({ks, kt} - {"uuid"}) <= (_TEXTISH | {"binary", "uuid"}):
        return "uuid", out
    if ks == "binary" and kt == "binary":
        return "binary", out
    if {ks, kt} <= {"bits", "binary"}:
        return "bits", out
    if ks == "date" and kt == "date":
        return "date", out
    if "date" in (ks, kt) and ({ks, kt} - {"date"}) <= _TS:
        f("warning", f"date vs timestamp ({s.raw_type} / {t.raw_type}): compared as timestamps at midnight")
        return "ts", out
    if ks in _TS and kt in _TS:
        if ks != kt:
            f("warning", f"naive vs time-zone-aware ({s.raw_type} / {t.raw_type}): compared as UTC instants, naive "
                         f"values read in naive_timezone={opts['naive_timezone']}")
        if s.fsp is not None and t.fsp is not None and t.fsp < s.fsp:
            f("risk", f"fractional seconds narrow {s.fsp} -> {t.fsp}")
        return "ts", out
    if "timetz" in (ks, kt):
        if ks == kt:
            return "timetz", out          # both zoned: compared as UTC wall time
        if ({ks, kt} - {"timetz"}) <= {"time", "interval"}:
            pol = opts["timetz_policy"]
            if pol == "utc":
                f("warning", f"TIME WITH TIME ZONE vs {s.raw_type if ks != 'timetz' else t.raw_type}: compared as UTC "
                             "wall time (timetz_policy=utc; the unzoned side is read as UTC)")
                return "time:utc", out
            if pol == "wallclock":
                f("warning", "TIME WITH TIME ZONE vs unzoned TIME: offset ignored (timetz_policy=wallclock — the "
                             "offset loss DMS performs, TIMETZ -> TIME, accepted)")
                return "time:wallclock", out
            f("unsupported", f"TIME WITH TIME ZONE vs {s.raw_type if ks != 'timetz' else t.raw_type}: the offset is "
                             "lost on the unzoned side (DMS maps TIMETZ -> TIME) — decide the conversion and set "
                             "cross_engine timetz_policy ('utc' or 'wallclock')")
            return "unsupported", out
    if {ks, kt} <= {"time", "interval"}:
        return "time", out
    if ks == kt == "array":
        return "json", out
    f(strict if ks != kt else "warning", f"incompatible kinds {ks} ({s.raw_type}) / {kt} ({t.raw_type}) — compared as text")
    return "str", out


# ── Normalizer ──────────────────────────────────────────────────────────────────────────
def _tzinfo(name):
    if name in (None, "", "UTC", "utc", "Z"):
        return dt.timezone.utc
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", str(name))
    if m:
        delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return dt.timezone(delta if m.group(1) == "+" else -delta)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception as e:  # ZoneInfoNotFoundError, missing tzdata
        raise CompareError(f"naive_timezone {name!r} is not a known zone ({type(e).__name__}); use UTC or +HH:MM") from e


def dec_str(v):
    """Canonical exact number: no exponent, no trailing zeros, '-0' -> '0'."""
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, float):
        v = repr(v)
    if isinstance(v, (bytes, bytearray)):
        v = int.from_bytes(v, "big")
    d = v if isinstance(v, Decimal) else Decimal(str(v).strip())
    if d.is_nan():
        return "NaN"
    if d.is_infinite():
        return "Infinity" if d > 0 else "-Infinity"
    if d == 0:
        return "0"
    return format(d.normalize(_HIGH), "f")


_JSON_ESC = re.compile(r'[\x00-\x1f"\\]')


def _jstr(s):
    """json.dumps(s, ensure_ascii=False) without the encoder call for the common case (no
    character json escapes: control chars, quote, backslash) — identical output."""
    return '"' + s + '"' if _JSON_ESC.search(s) is None else json.dumps(s, ensure_ascii=False)


def _kv_key(kv):
    return str(kv[0])


def _json_canon(o):
    t = type(o)
    if t is str:
        return _jstr(o)
    if t is int:
        return str(o)
    if t is dict:
        return "{" + ",".join(_jstr(k if type(k) is str else str(k)) + ":" + _json_canon(v)
                              for k, v in sorted(o.items(), key=_kv_key)) + "}"
    if t is list:
        return "[" + ",".join([_json_canon(x) for x in o]) + "]"
    if o is None:
        return "null"
    if o is True:
        return "true"
    if o is False:
        return "false"
    if isinstance(o, (int, float, Decimal)):
        return dec_str(o)
    if isinstance(o, str):
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _json_canon(v)
                              for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))) + "}"
    if isinstance(o, (list, tuple)):
        return "[" + ",".join(_json_canon(x) for x in o) + "]"
    if isinstance(o, (bytes, bytearray, memoryview)):
        return json.dumps(bytes(o).hex())
    if isinstance(o, dt.datetime):
        return json.dumps(o.isoformat(sep=" "))
    if isinstance(o, (dt.date, dt.time)):
        return json.dumps(o.isoformat())
    return json.dumps(str(o), ensure_ascii=False)


def parse_pg_array(text):
    """One-dimensional PostgreSQL array literal -> list of str/None. Drivers return arrays of
    types they don't know (e.g. pgloader's enum arrays for MySQL SET) as this raw text."""
    s = text.strip()
    if not (s.startswith("{") and s.endswith("}")):
        raise ValueError(f"not an array literal: {text!r}")
    out, i, body = [], 0, s[1:-1]
    if body == "":
        return out
    while i <= len(body):
        if i < len(body) and body[i] == '"':
            i += 1
            buf = []
            while body[i] != '"':
                if body[i] == "\\":
                    i += 1
                buf.append(body[i])
                i += 1
            out.append("".join(buf))
            i += 1
        else:
            j = body.find(",", i)
            j = len(body) if j < 0 else j
            tok = body[i:j].strip()
            out.append(None if tok.upper() == "NULL" else tok)
            i = j
        if i < len(body) and body[i] != ",":
            raise ValueError(f"bad array literal: {text!r}")
        i += 1
    return out


_TIMETZ_TEXT = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})(\.\d+)?(?:([+-])(\d{2}):?(\d{2})?)?$")
_ZERO_DATE = re.compile(r"^\d{4}-(00-\d{2}|\d{2}-00)|^0000-00-00")
_BOOL_TRUE, _BOOL_FALSE = {"1", "t", "true", "y", "yes", "on"}, {"0", "f", "false", "n", "no", "off"}
# What DMS actually wrote for MySQL zero dates on Aurora PostgreSQL 17 (DMS 3.6.1, dry run 5):
# 0101-01-01 [00:00:00] — not NULL. Reported as its own finding class, never equal to NULL.
_SENTINEL_0101 = re.compile(r"^0101-01-01(?:[ T]00:00:00(?:\.0+)?)?(?:Z|[+-]00(?::?00)?)?$")
ZERO_DATE_0101_MESSAGE = ("MySQL zero date (0000-00-00) landed as 0101-01-01 on the target for the listed keys (paired "
                          "per key) — DMS behaviour observed on Aurora PostgreSQL 17 / DMS 3.6.1 (not NULL as often "
                          "assumed). Remediate PER KEY from the reported keys (or a key list re-derived from the source's "
                          "zero dates) — never a blanket UPDATE on the value, which would also erase genuine 0101-01-01 "
                          "dates — after full load AND again right before cutover (CDC UPDATEs of rows still holding the "
                          "zero date re-apply the after-image), then re-validate")
ZERO_DATE_INCONCLUSIVE_MESSAGE = ("source zero dates and target 0101-01-01 values were both seen in a key-less (multiset) "
                                  "comparison, so they cannot be paired by key — INCONCLUSIVE, not a zero-date finding: "
                                  "add a `key` for this table and re-run to get paired evidence; do not run a blanket "
                                  "UPDATE on the value")


def is_zero_date_raw(v):
    """A MySQL zero/partial-zero date as drivers return it (pymysql: the text)."""
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("ascii", "replace")
    return isinstance(v, str) and _ZERO_DATE.match(v.strip()) is not None


def is_0101_sentinel(v):
    """0101-01-01 at midnight (date, naive/UTC datetime, or text)."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None and v.utcoffset():
            v = v.astimezone(dt.timezone.utc)
        return (v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond) == (101, 1, 1, 0, 0, 0, 0)
    if isinstance(v, dt.date):
        return (v.year, v.month, v.day) == (101, 1, 1)
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("ascii", "replace")
    return isinstance(v, str) and _SENTINEL_0101.match(v.strip()) is not None


class Normalizer:
    """Canonical text per value. NULL stays None (hashed with its own marker). Forms:
    numeric -> exact decimal text (1.500 -> '1.5'); float:N -> N significant digits;
    bool -> 'true'/'false' (other integers -> '!bool:<n>', never equal to a boolean);
    bool_numeric -> '1'/'0'; text -> exact (optional casefold / rstrip; CHAR always rstrip);
    set -> sorted unique members joined by ','; json -> key-sorted compact JSON with
    canonical numbers; uuid -> lowercase hyphenated; binary -> lowercase hex; bits ->
    zero-padded bit string; date -> YYYY-MM-DD; ts -> UTC 'YYYY-MM-DD HH:MM:SS[.ffffff]'
    (naive values read in naive_timezone; MySQL TIMESTAMP is read with session time_zone
    +00:00, i.e. already UTC); time -> [-]HH:MM:SS[.ffffff]; MySQL zero dates -> NULL
    (zero_date=null) or '!zero:<text>' (distinct), counted per side."""

    def __init__(self, opts):
        self.opts = opts
        self.tz = _tzinfo(opts["naive_timezone"])
        self.precision = int(opts["timestamp_precision"])
        self.zero_dates = Counter()
        self.sentinels_0101 = Counter()     # 0101-01-01 values seen per side (DMS zero-date output)

    def canon(self, v, col, as_kind, side="source", casefold=False, bits=None, nofold=False):
        if v is None:
            return None
        if isinstance(v, memoryview):
            v = v.tobytes()
        k = as_kind
        try:
            if k == "numeric":
                return dec_str(v)
            if k.startswith("float:"):
                return self._float(v, int(k.split(":")[1]))
            if k == "bool":
                return self._bool(v)
            if k == "bool_numeric":
                b = self._bool(v) if not isinstance(v, (int, Decimal)) or isinstance(v, bool) else None
                if b is None:
                    return dec_str(v)
                return {"true": "1", "false": "0"}.get(b, b)
            if k in ("text", "char", "str"):
                s = self._text(v)
                if k == "char" or self.opts["text_rstrip"]:
                    s = s.rstrip(" ")
                fold = (casefold or self.opts["text_casefold"]) and k != "str" and not nofold
                return s.casefold() if fold else s
            if k == "set":
                return self._set(v, casefold, col)
            if k == "json":
                return self._json(v, col)
            if k == "uuid":
                return self._uuid(v)
            if k == "binary":
                return bytes(v).hex() if isinstance(v, (bytes, bytearray)) else self._binary_text(v)
            if k == "bits":
                return self._bits(v, bits or col.bits or 1)
            if k == "date":
                if is_0101_sentinel(v):
                    self.sentinels_0101[side] += 1
                return self._date(v, side)
            if k == "ts":
                if is_0101_sentinel(v):
                    self.sentinels_0101[side] += 1
                return self._ts(v, col, side)
            if k == "time":
                return self._time(v)
            if k in ("timetz", "time:utc"):
                return self._time(v, "utc")
            if k == "time:wallclock":
                return self._time(v, "wallclock")
        except (ValueError, TypeError, ArithmeticError, OverflowError) as e:
            return f"!{k}:{type(e).__name__}:{v!r}"
        return self._text(v)

    def compile(self, col, as_kind, side="source", casefold=False, bits=None, nofold=False):
        """canon() specialised for one column: exact-type fast paths for the common driver
        types (int, Decimal, str, bool, date, datetime) and canon() for everything else.
        Output is identical to canon() for every value (test_cross_engine_compare asserts it)."""
        canon = self.canon

        def slow(v):
            return canon(v, col, as_kind, side, casefold, bits, nofold)
        k = as_kind
        if k == "numeric":
            def f(v):
                t = type(v)
                if t is int:
                    return str(v)
                if t is Decimal and v.is_finite():
                    return "0" if not v else format(v.normalize(_HIGH), "f")
                return slow(v)
            return f
        if k in ("text", "char", "str"):
            rstrip = k == "char" or bool(self.opts["text_rstrip"])
            fold = bool((casefold or self.opts["text_casefold"]) and k != "str" and not nofold)

            def f(v):
                if type(v) is str:
                    if rstrip:
                        v = v.rstrip(" ")
                    return v.casefold() if fold else v
                return slow(v)
            return f
        if k == "bool":
            def f(v):
                if v is True:
                    return "true"
                if v is False:
                    return "false"
                if type(v) is int and v in (0, 1):
                    return "true" if v else "false"
                return slow(v)
            return f
        if k == "date":
            def f(v):
                if type(v) is dt.date and v.year != 101:
                    return v.isoformat()
                return slow(v)
            return f
        if k == "ts":
            naive_ok = col.phys == "timestamp_utc" or self.tz is dt.timezone.utc
            cut = 19 if self.precision == 0 else 20 + self.precision
            zero = dt.timedelta(0)

            def f(v):
                if type(v) is dt.datetime and v.year != 101:
                    if v.tzinfo is None:
                        if naive_ok:
                            return v.isoformat(" ", "microseconds")[:cut]
                    elif v.utcoffset() == zero:
                        return v.replace(tzinfo=None).isoformat(" ", "microseconds")[:cut]
                return slow(v)
            return f
        return slow

    @staticmethod
    def _text(v):
        if isinstance(v, (bytes, bytearray)):
            try:
                return bytes(v).decode("utf-8")
            except UnicodeDecodeError:
                return "!bytes:" + bytes(v).hex()
        return str(v)

    @staticmethod
    def _float(v, digits):
        f = float(v)
        if f != f:
            return "nan"
        if f in (float("inf"), float("-inf")):
            return "inf" if f > 0 else "-inf"
        return format(f, f".{digits}g")

    @staticmethod
    def _bool(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (bytes, bytearray)):
            v = int.from_bytes(v, "big")
        if isinstance(v, (int, Decimal)):
            return "true" if v == 1 else "false" if v == 0 else f"!bool:{dec_str(v)}"
        s = str(v).strip().lower()
        return "true" if s in _BOOL_TRUE else "false" if s in _BOOL_FALSE else f"!bool:{s}"

    def _set(self, v, casefold, col):
        if isinstance(v, str) and col.dialect == "postgres" and col.phys == "array":
            v = parse_pg_array(v)
        if isinstance(v, (set, frozenset, list, tuple)):
            items = [self._text(x) for x in v if x is not None]
        else:
            s = self._text(v)
            items = [] if s == "" else s.split(",")
        if casefold or self.opts["text_casefold"]:
            items = [x.casefold() for x in items]
        return ",".join(sorted(set(items)))

    def _json(self, v, col):
        # MySQL drivers return JSON as text; PostgreSQL drivers return json/jsonb already parsed
        # (a str there is a JSON string scalar). Text-typed columns hold JSON text on either side.
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8")
        if isinstance(v, str) and col.dialect == "postgres" and col.phys == "array":
            v = parse_pg_array(v)
        if isinstance(v, str) and (col.dialect == "mysql" or col.phys in _TEXTISH):
            try:
                v = json.loads(v, parse_float=Decimal)
            except ValueError:
                return "!json:" + v
        return _json_canon(v)

    @staticmethod
    def _uuid(v):
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, (bytes, bytearray)) and len(v) == 16:
            return str(uuid.UUID(bytes=bytes(v)))
        s = Normalizer._text(v).strip()
        try:
            return str(uuid.UUID(s))
        except ValueError:
            return "!uuid:" + s

    @staticmethod
    def _binary_text(v):
        s = str(v)
        return s[2:].lower() if s.startswith("\\x") else s.encode("utf-8").hex()

    @staticmethod
    def _bits(v, n):
        if isinstance(v, (bytes, bytearray)):
            iv = int.from_bytes(v, "big")
        elif isinstance(v, bool):
            iv = int(v)
        elif isinstance(v, int):
            iv = v
        else:
            s = str(v).strip()
            iv = int(s, 2) if s else 0
        return format(iv, f"0{int(n)}b")

    def _zero(self, s, side):
        self.zero_dates[side] += 1
        return None if self.opts["zero_date"] == "null" else "!zero:" + s

    def _date(self, v, side):
        if isinstance(v, dt.datetime):
            return v.date().isoformat()
        if isinstance(v, dt.date):
            return v.isoformat()
        s = self._text(v).strip()
        if _ZERO_DATE.match(s):
            return self._zero(s, side)
        return dt.date.fromisoformat(s[:10]).isoformat()

    def _ts(self, v, col, side):
        if isinstance(v, (str, bytes, bytearray)):
            s = self._text(v).strip()
            if _ZERO_DATE.match(s):
                return self._zero(s, side)
            try:
                v = dt.datetime.fromisoformat(s)
            except ValueError:
                return "!ts:" + s          # e.g. PostgreSQL 'infinity'
        if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
            v = dt.datetime(v.year, v.month, v.day)
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        elif col.phys != "timestamp_utc":   # naive DATETIME / timestamp (or a date): naive_timezone
            v = v.replace(tzinfo=self.tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
        out = v.strftime("%Y-%m-%d %H:%M:%S")
        if v.year < 1000:
            out = f"{v.year:04d}" + out[out.index("-"):]
        if self.precision:
            out += "." + f"{v.microsecond:06d}"[:self.precision]
        return out

    def _time(self, v, zone=None):
        """zone None: naive time; "utc": shift a zoned time to UTC (unzoned values taken as UTC);
        "wallclock": drop any offset."""
        if isinstance(v, (str, bytes, bytearray)):
            m = _TIMETZ_TEXT.match(self._text(v).strip())
            if not m:
                return self._text(v)
            off = None
            if m.group(5):
                sign = 1 if m.group(5) == "+" else -1
                off = dt.timezone(sign * dt.timedelta(hours=int(m.group(6)), minutes=int(m.group(7) or 0)))
            v = dt.time(int(m.group(1)) % 24, int(m.group(2)), int(m.group(3)),
                        int(((m.group(4) or ".0")[1:] + "000000")[:6]), tzinfo=off)
        if isinstance(v, dt.timedelta):
            us = (v.days * 86400 + v.seconds) * 1_000_000 + v.microseconds
        elif isinstance(v, dt.time):
            us = ((v.hour * 60 + v.minute) * 60 + v.second) * 1_000_000 + v.microsecond
            if v.tzinfo is not None and zone == "utc":
                off = v.tzinfo.utcoffset(None)
                us = (us - int(off.total_seconds() * 1_000_000)) % (86400 * 1_000_000)
        else:
            return self._text(v)
        sign = "-" if us < 0 else ""
        us = abs(us)
        h, rem = divmod(us, 3_600_000_000)
        m, rem = divmod(rem, 60_000_000)
        s, frac = divmod(rem, 1_000_000)
        out = f"{sign}{h:02d}:{m:02d}:{s:02d}"
        if self.precision:
            out += "." + f"{frac:06d}"[:self.precision]
        return out


def row_digest(values):
    """sha256 over b"\x00N;" (NULL) or b"\x01<utf-8 byte length>:<utf-8 bytes>" per value —
    built as one str and encoded once (same bytes as one update per value)."""
    parts = []
    add = parts.append
    for c in values:
        if c is None:
            add("\x00N;")
        else:
            add(f"\x01{len(c) if c.isascii() else len(c.encode('utf-8', 'surrogatepass'))}:{c}")
    return hashlib.sha256("".join(parts).encode("utf-8", "surrogatepass")).digest()


# ── Sides ───────────────────────────────────────────────────────────────────────────────
class SqlSide:
    """One database connection + dialect. `runner(sql) -> rows` overrides execution (the
    Lambda preflight passes its budget-checked query function)."""

    def __init__(self, dialect, conn, label="source", runner=None):
        if dialect not in ("mysql", "postgres"):
            raise CompareError(f"dialect must be mysql or postgres, not {dialect!r}")
        self.dialect, self.conn, self.label, self.runner = dialect, conn, label, runner
        self.queries = 0

    def query(self, sql):
        assert_read_only(sql)
        self.queries += 1
        if self.runner is not None:
            return [tuple(r) for r in self.runner(sql)]
        conn = self.conn
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            try:
                cur.execute(sql)
                rows = cur.fetchall() if cur.description else []
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            return [tuple(r) for r in rows]
        return [tuple(r) for r in (conn.run(sql) or [])]    # pg8000.native

    def begin(self, statement_timeout_s=None):
        """UTC session time zone (MySQL TIMESTAMP / PG timestamptz come back as UTC), a
        server-side per-statement timeout (independent of the driver: MySQL
        max_execution_time for SELECTs, MariaDB max_statement_time, PostgreSQL
        statement_timeout), then one READ ONLY consistent-snapshot transaction — on MySQL
        after forcing the session to REPEATABLE READ (see MYSQL_SNAPSHOT_ISOLATION)."""
        if self.dialect == "postgres" and hasattr(self.conn, "autocommit"):
            try:
                self.conn.autocommit = True   # DB-API drivers: our explicit BEGIN must start the txn
            except Exception:
                pass
        ms = None if statement_timeout_s is None else max(1, int(float(statement_timeout_s) * 1000))
        if self.dialect == "mysql":
            self.query("SET time_zone = '+00:00'")
            if ms is not None:
                try:
                    self.query(f"SET SESSION max_execution_time = {ms}")
                except CompareError:
                    raise
                except Exception:          # MariaDB has max_statement_time (seconds) instead
                    self.query(f"SET SESSION max_statement_time = {ms / 1000.0:.3f}")
            self.query(MYSQL_SNAPSHOT_ISOLATION)   # before START: the snapshot needs REPEATABLE READ
            self.query("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        else:
            self.query("SET TimeZone = 'UTC'")
            if ms is not None:
                self.query(f"SET statement_timeout = {ms}")
            self.query("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

    def end(self):
        try:
            self.query("COMMIT")
        except Exception:
            pass


def make_side(dialect, conn, label="source", runner=None):
    return conn if hasattr(conn, "query") and hasattr(conn, "dialect") else SqlSide(dialect, conn, label, runner)


class SideTable:
    def __init__(self, ident, schema, name, cols, pk):
        self.ident, self.schema, self.name, self.cols, self.pk = ident, schema, name, cols, pk

    def as_dict(self):
        return {"identifier": self.ident, "schema": self.schema, "table": self.name}


def side_table(side, ident, opts):
    parts = table_parts(ident)
    schema, name = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    cands = [tuple(r) for r in side.query(resolve_table_sql(side.dialect, schema, name))]
    if not cands:
        return None
    exact = [c for c in cands if c[1] == name and (schema is None or c[0] == schema)]
    if len(exact) == 1:
        cands = exact
    elif len(cands) > 1:
        raise CompareError(f"{side.label}: {ident!r} matches several tables case-insensitively: {cands} — quote it exactly")
    sch, tab = str(cands[0][0]), str(cands[0][1])
    classify = classify_mysql if side.dialect == "mysql" else classify_pg
    cols = [classify(r, opts) for r in side.query(catalog_sql(side.dialect, sch, tab))]
    pk = [str(r[0]) for r in side.query(pk_sql(side.dialect, sch, tab))]
    return SideTable(ident, sch, tab, cols, pk)


# ── Plan ────────────────────────────────────────────────────────────────────────────────
_KEYABLE = {"int", "decimal", "date", "datetime", "timestamp_utc", "text", "char", "enum", "uuid", "binary"}


class Plan:
    def __init__(self, table, src, tgt):
        self.table, self.src, self.tgt = table, src, tgt
        self.cols, self.key, self.findings, self.source_only, self.target_only = [], [], [], [], []
        self.unsupported, self.ignored, self.mode, self.note = [], [], None, None

    def type_mismatch(self):
        return any(f["severity"] == "mismatch" for f in self.findings)


def _table_options(opts, table):
    return (opts.get("tables") or {}).get(table) or {}


def build_plan(table, src, tgt, opts):
    to = _table_options(opts, table)
    plan = Plan(table, src, tgt)
    ignore = {c.lower() for c in (to.get("ignore_columns") or [])}
    explicit = {k.lower(): v for k, v in (to.get("columns") or {}).items()}
    kinds = {k.lower(): v for k, v in (to.get("kinds") or {}).items()}
    casefold_cols = {c.lower() for c in (to.get("casefold_columns") or [])}
    tgt_by_lower = {}
    for c in tgt.cols:
        tgt_by_lower.setdefault(c.name.lower(), []).append(c)
    used = set()
    for s in src.cols:
        lname = s.name.lower()
        if lname in ignore:
            plan.ignored.append(s.name)
            continue
        if lname in explicit:
            want = explicit[lname]
            if want is None:
                plan.ignored.append(s.name)
                continue
            match = [c for c in tgt.cols if c.name == want] or tgt_by_lower.get(str(want).lower(), [])
        else:
            match = [c for c in tgt_by_lower.get(lname, []) if c.name == s.name] or tgt_by_lower.get(lname, [])
        if len(match) != 1:
            plan.source_only.append(s.name)
            if len(match) > 1:
                plan.findings.append({"column": s.name, "severity": "mismatch",
                                      "message": f"ambiguous target columns {[c.name for c in match]} — map explicitly"})
            continue
        t = match[0]
        used.add(t.name)
        forced = kinds.get(lname)
        sk = _with_kind(s, forced) if forced else s
        tk = _with_kind(t, forced) if forced else t
        as_kind, findings = resolve_pair(sk, tk, opts, forced=bool(forced))
        if s.nullable != t.nullable:
            findings.append({"severity": "warning", "message": f"nullability differs (source {'NULL' if s.nullable else 'NOT NULL'}, "
                                                               f"target {'NULL' if t.nullable else 'NOT NULL'})"})
        for fd in findings:
            plan.findings.append({"column": s.name, "target_column": t.name, **fd})
        if as_kind == "unsupported":
            plan.unsupported.append(s.name)
            continue
        bits = max(s.bits or 0, t.bits or 0) or None
        plan.cols.append(ColPlan(sk, tk, as_kind, findings, casefold=lname in casefold_cols, bits=bits))
    plan.target_only = [c.name for c in tgt.cols if c.name not in used and c.name.lower() not in ignore]
    for name in plan.source_only:
        plan.findings.append({"column": name, "severity": "mismatch", "message": "column missing on target"})
    for name in plan.target_only:
        plan.findings.append({"column": name, "severity": "warning", "message": "extra column on target (not compared)"})
    # Key: explicit option, else the source PK (must be mapped and orderable on both sides).
    key_names = to.get("key") or src.pk
    by_src = {cp.src.name.lower(): cp for cp in plan.cols}
    key = []
    for k in key_names or []:
        cp = by_src.get(str(k).lower())
        if cp is None or cp.src.phys not in _KEYABLE or cp.tgt.phys not in _KEYABLE:
            key = []
            plan.note = (f"key column {k!r} is not a mapped, orderable column on both sides — multiset comparison")
            break
        key.append(cp)
    plan.key = key
    if key and not tgt.pk:
        plan.findings.append({"severity": "warning", "message": "target has no PRIMARY KEY (DMS CDC/validation need one); "
                                                               "ordered by the mapped source key"})
    plan.mode = "key_chunks" if key else "multiset"
    if not key and plan.note is None:
        plan.note = "no primary key (and no `key` option) — multiset comparison, bounded by multiset_max_rows"
    return plan


# ── SQL builders used by run AND preflight ──────────────────────────────────────────────
def key_expr(dialect, col):
    q = qident(dialect, col.name)
    if col.phys in ("text", "char", "enum") or (col.phys == "uuid" and dialect == "mysql"):
        return f"CAST({q} AS BINARY)" if dialect == "mysql" else f"({q})::text COLLATE \"C\""
    return q


def _key_lit(dialect, col, v):
    if col.phys in ("text", "char", "enum") or (col.phys == "uuid" and dialect == "mysql"):
        if dialect == "mysql":   # compared against CAST(col AS BINARY): the raw bytes
            raw = bytes(v) if isinstance(v, (bytes, bytearray, memoryview)) else str(v).encode("utf-8")
            return f"X'{raw.hex()}'"
        return str_literal(dialect, str(v))
    return literal(dialect, v)


def bound_sql(dialect, column, bound, tail=False):
    """bound = {"column", "op": "<=", "value", "include_nulls"} with column = this side's name."""
    q = qident(dialect, column)
    lit = literal(dialect, bound["value"])
    if tail:
        return f"{q} > {lit}"
    if bound.get("include_nulls"):
        return f"({q} <= {lit} OR {q} IS NULL)"
    return f"{q} <= {lit}"


def select_sql(dialect, side_table_, cols, key_cols, where, after=None, limit=None):
    sel = ", ".join(qident(dialect, c.name) for c in cols)
    conds = list(where)
    if after is not None and key_cols:
        exprs = [key_expr(dialect, c) for c in key_cols]
        lits = [_key_lit(dialect, c, v) for c, v in zip(key_cols, after)]
        conds.append(f"({', '.join(exprs)}) > ({', '.join(lits)})" if len(exprs) > 1 else f"{exprs[0]} > {lits[0]}")
    sql = f"SELECT {sel} FROM {qtable(dialect, side_table_.schema, side_table_.name)}"
    if conds:
        sql += " WHERE " + " AND ".join(f"({c})" for c in conds)
    if key_cols:
        sql += " ORDER BY " + ", ".join(key_expr(dialect, c) for c in key_cols)
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def count_sql(dialect, side_table_, where):
    sql = f"SELECT COUNT(*) FROM {qtable(dialect, side_table_.schema, side_table_.name)}"
    return sql + (" WHERE " + " AND ".join(f"({c})" for c in where) if where else "")


def minmax_sql(dialect, side_table_, column, fn):
    return f"SELECT {fn}({qident(dialect, column)}) FROM {qtable(dialect, side_table_.schema, side_table_.name)}"


def _side_bound(plan, bound, side):
    if not bound:
        return None
    cp = next((c for c in plan.cols if c.src.name == bound["column"]), None)
    if cp is None:
        raise CompareError(f"bound column {bound['column']!r} is not a mapped column of {plan.table}")
    return {**bound, "column": cp.src.name if side == "source" else cp.tgt.name}


def probe_sqls(plan, side, dialect, bound=None):
    """Every statement shape a run issues for this table on one side, made zero-row with
    1=0 — for preflight parity. side = 'source' | 'target'."""
    st = plan.src if side == "source" else plan.tgt
    cols = [cp.src if side == "source" else cp.tgt for cp in plan.cols]
    key = [cp.src if side == "source" else cp.tgt for cp in plan.key]
    b = _side_bound(plan, bound, side)
    where = ([bound_sql(dialect, b["column"], b)] if b else []) + ["1=0"]
    out = [count_sql(dialect, st, where), select_sql(dialect, st, cols, key, where, limit=1)]
    if b:
        out.append(count_sql(dialect, st, [bound_sql(dialect, b["column"], b, tail=True), "1=0"]))
        if b.get("include_nulls"):
            out.append(count_sql(dialect, st, [f"{qident(dialect, b['column'])} IS NULL", "1=0"]))
    return out   # watermark endpoint reads (MAX/MIN, source clock): watermark_ops(), run as-is


# ── Comparison ──────────────────────────────────────────────────────────────────────────
def pk_watermark(src_max, tgt_max, src_min, margin):
    """Same semantics as soak_check*.py resolve_pk_watermark (tests assert parity)."""
    if src_max is None or tgt_max is None:
        return None, "table empty on at least one side — whole-table comparison"
    wm = min(int(src_max), int(tgt_max)) - int(margin)
    if src_min is None or wm < int(src_min):
        return None, (f"no rows below the watermark (table spans fewer than the {int(margin)}-key margin) "
                      "— whole-table comparison")
    return wm, None


def _short(c):
    return None if c is None else hashlib.sha256(c.encode("utf-8", "surrogatepass")).hexdigest()[:12]


class _Acc:
    def __init__(self, opts):
        self.opts = opts
        self.missing, self.extra, self.different = [], [], []
        self.n_missing = self.n_extra = self.n_diff = 0
        self.diff_columns = Counter()

    def full(self, lst):
        return len(lst) >= int(self.opts["max_examples"])


def _stream(side, st, cols, key_cols, where, chunk, deadline, key_idx):
    last = None
    while True:
        if time.monotonic() > deadline:
            raise _Budget()
        rows = side.query(select_sql(side.dialect, st, cols, key_cols, where, after=last, limit=chunk))
        if time.monotonic() > deadline:   # a fetch that returned after the budget is not trusted
            raise _Budget()
        for r in rows:
            yield r
        if len(rows) < chunk:
            return
        last = [rows[-1][i] for i in key_idx]


def _canon_key(norm, plan, row, side, key_idx):
    out = []
    for cp, i in zip(plan.key, key_idx):
        col = cp.src if side == "source" else cp.tgt
        v = row[i]
        if cp.as_kind == "numeric":
            out.append(Decimal(dec_str(v)))
        else:
            c = norm.canon(v, col, cp.as_kind, side, nofold=True)   # keys: exact, as the engines order them
            out.append("" if c is None else c)
    return tuple(out)


def _canon_row(norm, plan, row, side):
    vals = []
    for cp, v in zip(plan.cols, row):
        col = cp.src if side == "source" else cp.tgt
        vals.append(norm.canon(v, col, cp.as_kind, side, casefold=cp.casefold, bits=cp.bits))
    return vals


def _row_fns(norm, plan, side):
    """Per-column compiled canonicalizers (Normalizer.compile) — same output as _canon_row."""
    return [norm.compile(cp.src if side == "source" else cp.tgt, cp.as_kind, side, casefold=cp.casefold, bits=cp.bits)
            for cp in plan.cols]


def _key_fns(norm, plan, side):
    """Per-key-column canonicalizers — same ordering/equality as _canon_key: numeric keys as
    exact numbers (int when integral: int and Decimal compare exactly), others exact text."""
    out = []
    for cp in plan.key:
        col = cp.src if side == "source" else cp.tgt
        if cp.as_kind == "numeric":
            def f(v):
                t = type(v)
                if t is int:
                    return v
                if t is Decimal and v.is_finite() and v == v.to_integral_value():
                    return int(v)
                return Decimal(dec_str(v))
        else:
            c = norm.compile(col, cp.as_kind, side, nofold=True)

            def f(v, c=c):
                r = c(v)
                return "" if r is None else r
        out.append(f)
    return out


def _range_conds(dialect, col, key_range):
    """(lo, hi] on one integer key column; None = unbounded on that end."""
    lo, hi = key_range
    q = qident(dialect, col.name)
    conds = []
    if lo is not None:
        conds.append(f"{q} > {int(lo)}")
    if hi is not None:
        conds.append(f"{q} <= {int(hi)}")
    return conds


def row_estimate_sql(dialect, st):
    s, t = str_literal(dialect, st.schema), str_literal(dialect, st.name)
    if dialect == "mysql":
        return f"SELECT table_rows FROM information_schema.tables WHERE table_schema={s} AND table_name={t}"
    return ("SELECT c.reltuples FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE n.nspname={s} AND c.relname={t}")


def row_estimate(side, st):
    """Catalog row estimate (InnoDB table_rows / PG reltuples) for progress ETA only — never
    used for a verdict. None when unknown."""
    try:
        rows = side.query(row_estimate_sql(side.dialect, st))
        v = float(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else -1
        return int(v) if v >= 0 else None
    except CompareError:
        raise
    except Exception:
        return None


class Progress:
    """Rows compared / estimate / rows per s / ETA -> one stderr line every `interval` s and
    (optional) an atomically replaced JSON file the agent can mirror to the dashboard."""

    def __init__(self, stream=None, path=None, interval=15.0, workers=1, tables_total=0, clock=time.monotonic):
        self.stream, self.path, self.interval, self.workers = stream, Path(path) if path else None, float(interval), workers
        self.tables_total, self.tables_done, self.clock = tables_total, 0, clock
        self.table, self.rows, self.estimate, self.t0, self.last = None, 0, None, clock(), None
        self.run_t0 = clock()

    def start_table(self, table, estimate):
        self.table, self.rows, self.estimate, self.t0 = table, 0, estimate, self.clock()
        self.emit(force=True)

    def add(self, n):
        self.rows += int(n)
        self.emit()

    def end_table(self):
        self.tables_done += 1
        self.emit(force=True)

    def snapshot(self, state="running"):
        el = max(self.clock() - self.t0, 1e-9)
        rps = self.rows / el if self.rows else 0.0
        eta = None
        if self.estimate and rps and self.rows < self.estimate:
            eta = int((self.estimate - self.rows) / rps)
        return {"tool": "cross_engine_compare", "state": state, "table": self.table, "tables_done": self.tables_done,
                "tables_total": self.tables_total, "rows_compared": self.rows, "rows_total_estimate": self.estimate,
                "rows_per_s": round(rps, 1), "eta_s": eta, "elapsed_s": round(self.clock() - self.run_t0, 1),
                "workers": self.workers, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

    def emit(self, force=False, state="running"):
        now = self.clock()
        if not force and self.last is not None and now - self.last < self.interval:
            return
        self.last = now
        snap = self.snapshot(state)
        if self.stream is not None:
            est = f"~{snap['rows_total_estimate']:,}" if snap["rows_total_estimate"] else "?"
            eta = f"{snap['eta_s']}s" if snap["eta_s"] is not None else "?"
            print(f"progress {snap['table']}: {snap['rows_compared']:,} / {est} rows, {snap['rows_per_s']:,.0f} rows/s, "
                  f"ETA {eta}, table {snap['tables_done'] + (0 if state == 'done' else 1)}/{snap['tables_total']}, "
                  f"workers {snap['workers']}", file=self.stream, flush=True)
        if self.path is not None:
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snap) + "\n")
            os.replace(tmp, self.path)      # atomic: a reader never sees a half-written file

    def finish(self, overall):
        self.table = None
        snap_state = "done"
        self.emit(force=True, state=snap_state)
        if self.path is not None:
            d = self.snapshot(snap_state)
            d["overall"] = overall
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d) + "\n")
            os.replace(tmp, self.path)


def compare_table(src_side, tgt_side, plan, opts, bound=None, deadline=None, key_range=None, progress=None):
    """Compares one planned table. Returns the per-table report dict. key_range = (lo, hi]
    on the single integer key (parallel workers); progress = callable(rows_compared_delta)."""
    norm = Normalizer(opts)
    deadline = deadline if deadline is not None else time.monotonic() + float(opts["max_seconds"])
    rep = {"target_table": plan.tgt.as_dict(), "source_table": plan.src.as_dict(), "mode": plan.mode,
           "key": [cp.src.name for cp in plan.key], "columns": [
               {"source": cp.src.name, "target": cp.tgt.name, "source_type": cp.src.raw_type,
                "target_type": cp.tgt.raw_type, "compared_as": cp.as_kind} for cp in plan.cols],
           "type_findings": plan.findings, "ignored_columns": plan.ignored, "unsupported_columns": plan.unsupported,
           "notes": [plan.note] if plan.note else []}
    sb, tb = _side_bound(plan, bound, "source"), _side_bound(plan, bound, "target")
    if bound:
        rep["bound"] = {"column": bound["column"], "op": "<=", "value": str(bound["value"]),
                        "include_nulls": bool(bound.get("include_nulls"))}
    s_where = [bound_sql(src_side.dialect, sb["column"], sb)] if sb else []
    t_where = [bound_sql(tgt_side.dialect, tb["column"], tb)] if tb else []
    if key_range is not None:
        if len(plan.key) != 1:
            raise CompareError("key_range needs exactly one key column")
        s_where += _range_conds(src_side.dialect, plan.key[0].src, key_range)
        t_where += _range_conds(tgt_side.dialect, plan.key[0].tgt, key_range)
    s_cols = [cp.src for cp in plan.cols]
    t_cols = [cp.tgt for cp in plan.cols]
    acc = _Acc(opts)
    s_hash, t_hash = hashlib.sha256(), hashlib.sha256()
    s_n = t_n = 0
    partial = order_error = None
    include_values = bool(opts["include_values"])
    max_ex = int(opts["max_examples"])
    zero_0101, zero_0101_keys = Counter(), {}
    date_idx = [i for i, cp in enumerate(plan.cols) if cp.as_kind in ("date", "ts") and cp.src.dialect == "mysql"]
    s_fns, t_fns = _row_fns(norm, plan, "source"), _row_fns(norm, plan, "target")

    def canon_s(row):
        return [None if v is None else f(v) for f, v in zip(s_fns, row)]

    def canon_t(row):
        return [None if v is None else f(v) for f, v in zip(t_fns, row)]
    pending = [0]

    def tick(n=1):
        if progress is not None:
            pending[0] += n
            if pending[0] >= 2048:
                progress(pending[0])
                pending[0] = 0

    def record_diff(key, sv, tv, s_raw=None, t_raw=None):
        acc.n_diff += 1
        cols = [cp.src.name for cp, a, b in zip(plan.cols, sv, tv) if a != b]
        acc.diff_columns.update(cols)
        if s_raw is not None and date_idx:
            for i in date_idx:
                if sv[i] != tv[i] and is_zero_date_raw(s_raw[i]) and is_0101_sentinel(t_raw[i]):
                    c = plan.cols[i].src.name
                    zero_0101[c] += 1
                    ks = zero_0101_keys.setdefault(c, [])
                    if len(ks) < max_ex:
                        ks.append([str(k) for k in key])
        if not acc.full(acc.different):
            item = {"key": [str(k) for k in key], "columns": cols}
            item["values" if include_values else "value_hashes"] = {
                cp.src.name: ({"source": a, "target": b} if include_values else {"source": _short(a), "target": _short(b)})
                for cp, a, b in zip(plan.cols, sv, tv) if a != b}
            acc.different.append(item)

    chunks, chunk_size = [], int(opts["chunk_size"])
    if plan.mode == "key_chunks":
        key_idx = [plan.cols.index(cp) for cp in plan.key]
        s_key_cols = [cp.src for cp in plan.key]
        t_key_cols = [cp.tgt for cp in plan.key]
        s_it = _stream(src_side, plan.src, s_cols, s_key_cols, s_where, chunk_size, deadline, key_idx)
        t_it = _stream(tgt_side, plan.tgt, t_cols, t_key_cols, t_where, chunk_size, deadline, key_idx)
        cur = {"first": None, "last": None, "s": hashlib.sha256(), "t": hashlib.sha256(), "sn": 0, "tn": 0, "n": 0}

        def close_chunk():
            if cur["n"]:
                sd, td = cur["s"].hexdigest()[:16], cur["t"].hexdigest()[:16]
                chunks.append({"first_key": [str(k) for k in cur["first"]], "last_key": [str(k) for k in cur["last"]],
                               "rows": {"source": cur["sn"], "target": cur["tn"]},
                               "hash": {"source": sd, "target": td}, "match": sd == td and cur["sn"] == cur["tn"]})
            cur.update({"first": None, "s": hashlib.sha256(), "t": hashlib.sha256(), "sn": 0, "tn": 0, "n": 0})

        def bump(key):
            if cur["first"] is None:
                cur["first"] = key
            cur["last"] = key
            cur["n"] += 1
            if cur["n"] >= chunk_size:
                close_chunk()

        sk_fns, tk_fns = _key_fns(norm, plan, "source"), _key_fns(norm, plan, "target")

        def key_s(row):
            return tuple([f(row[i]) for f, i in zip(sk_fns, key_idx)])

        def key_t(row):
            return tuple([f(row[i]) for f, i in zip(tk_fns, key_idx)])
        try:
            s_row, t_row = next(s_it, None), next(t_it, None)
            prev_s = prev_t = None
            merged = 0
            while s_row is not None or t_row is not None:
                merged += 1
                if merged % 512 == 0 and time.monotonic() > deadline:
                    raise _Budget()
                tick()
                sk = key_s(s_row) if s_row is not None else None
                tk = key_t(t_row) if t_row is not None else None
                if sk is not None and prev_s is not None and not sk > prev_s:
                    raise _OrderError(f"source rows not in normalized key order at {list(map(str, sk))}")
                if tk is not None and prev_t is not None and not tk > prev_t:
                    raise _OrderError(f"target rows not in normalized key order at {list(map(str, tk))}")
                if tk is None or (sk is not None and sk < tk):
                    sv = canon_s(s_row)
                    d = row_digest(sv)
                    s_hash.update(d); cur["s"].update(d); cur["sn"] += 1; s_n += 1
                    acc.n_missing += 1
                    if not acc.full(acc.missing):
                        acc.missing.append([str(k) for k in sk])
                    bump(sk); prev_s = sk
                    s_row = next(s_it, None)
                elif sk is None or tk < sk:
                    tv = canon_t(t_row)
                    d = row_digest(tv)
                    t_hash.update(d); cur["t"].update(d); cur["tn"] += 1; t_n += 1
                    acc.n_extra += 1
                    if not acc.full(acc.extra):
                        acc.extra.append([str(k) for k in tk])
                    bump(tk); prev_t = tk
                    t_row = next(t_it, None)
                else:
                    sv, tv = canon_s(s_row), canon_t(t_row)
                    ds, dt_ = row_digest(sv), row_digest(tv)
                    s_hash.update(ds); t_hash.update(dt_); cur["s"].update(ds); cur["t"].update(dt_)
                    cur["sn"] += 1; cur["tn"] += 1; s_n += 1; t_n += 1
                    if ds != dt_:
                        record_diff(sk, sv, tv, s_row, t_row)
                    bump(sk); prev_s, prev_t = sk, tk
                    s_row, t_row = next(s_it, None), next(t_it, None)
            close_chunk()
        except _Budget:
            partial = (f"time budget ({opts['max_seconds']} s) exhausted after {s_n} source / {t_n} target rows "
                       f"(last key {list(map(str, cur['last'])) if cur['last'] else None}) — partial, not a MATCH")
        except _OrderError as e:
            order_error = (f"{e} — the engines order this key differently from the normalizer (collation/type); "
                           "set an integer `key` for the table or compare it as a multiset")
    else:
        limit = int(opts["multiset_max_rows"])
        s_cnt = int(src_side.query(count_sql(src_side.dialect, plan.src, s_where))[0][0])
        t_cnt = int(tgt_side.query(count_sql(tgt_side.dialect, plan.tgt, t_where))[0][0])
        s_n, t_n = s_cnt, t_cnt
        if max(s_cnt, t_cnt) > limit:
            partial = (f"multiset mode limited to {limit} rows (source {s_cnt}, target {t_cnt}) — add a `key` "
                       "(unique, NOT NULL column) for this table; not compared")
        elif time.monotonic() > deadline:
            partial = "time budget exhausted before this table — not compared"
        else:
          try:
            def fetch(side, st, cols, where):
                rows = side.query(select_sql(side.dialect, st, cols, [], where, limit=limit + 1))
                if time.monotonic() > deadline:
                    raise _Budget()
                return rows

            def digest_all(rows, side):
                c = Counter()
                canon = canon_s if side == "source" else canon_t
                for i, r in enumerate(rows, 1):
                    if i % 512 == 0 and time.monotonic() > deadline:
                        raise _Budget()
                    if side == "source":
                        tick()
                    c[row_digest(canon(r))] += 1
                return c
            s_rows = fetch(src_side, plan.src, s_cols, s_where)
            t_rows = fetch(tgt_side, plan.tgt, t_cols, t_where)
            sc, tc = digest_all(s_rows, "source"), digest_all(t_rows, "target")
            for d in sorted(sc):
                s_hash.update(d * sc[d])
            for d in sorted(tc):
                t_hash.update(d * tc[d])
            only_s, only_t = sc - tc, tc - sc
            acc.n_missing, acc.n_extra = sum(only_s.values()), sum(only_t.values())
            acc.missing = [{"row_hash": d.hex()[:16], "count": n} for d, n in list(only_s.items())[:max_ex]]
            acc.extra = [{"row_hash": d.hex()[:16], "count": n} for d, n in list(only_t.items())[:max_ex]]
            s_n, t_n = len(s_rows), len(t_rows)
          except _Budget:
            partial = (f"time budget ({opts['max_seconds']} s) exhausted during the multiset comparison — "
                       "partial, not a MATCH")
    if progress is not None and pending[0]:
        progress(pending[0])
    rep["rows"] = {"source": s_n, "target": t_n}
    if sb:
        rep["tail_rows"] = {
            "source": int(src_side.query(count_sql(src_side.dialect, plan.src, [bound_sql(src_side.dialect, sb["column"], sb, tail=True)]))[0][0]),
            "target": int(tgt_side.query(count_sql(tgt_side.dialect, plan.tgt, [bound_sql(tgt_side.dialect, tb["column"], tb, tail=True)]))[0][0])}
    rep["digest"] = {"source": s_hash.hexdigest(), "target": t_hash.hexdigest()}
    rep["mismatches"] = {"missing_on_target": acc.n_missing, "extra_on_target": acc.n_extra,
                         "different_rows": acc.n_diff, "differing_columns": dict(acc.diff_columns),
                         "examples": {"missing_on_target": acc.missing, "extra_on_target": acc.extra,
                                      "different": acc.different}}
    if chunks:
        rep["chunks"] = {"size": chunk_size, "count": len(chunks), "mismatched": [c for c in chunks if not c["match"]][:max_ex]}
    if norm.zero_dates:
        rep["zero_dates"] = {"source": norm.zero_dates.get("source", 0), "target": norm.zero_dates.get("target", 0),
                             "policy": opts["zero_date"]}
    if norm.sentinels_0101:
        rep["zero_date_sentinels"] = {"value": "0101-01-01", "source": norm.sentinels_0101.get("source", 0),
                                      "target": norm.sentinels_0101.get("target", 0)}
    _zero_date_findings(rep, zero_0101, zero_0101_keys, keyed=plan.mode == "key_chunks", max_ex=max_ex)
    if not partial and time.monotonic() > deadline:
        partial = (f"comparison finished after the time budget ({opts['max_seconds']} s) — not accepted as a MATCH; "
                   "raise max_seconds or narrow the scope")
    schema_bad = plan.source_only or (opts["strict_types"] and plan.type_mismatch())
    # Row differences found before an ordering error are artifacts of the misaligned merge.
    values_bad = (acc.n_missing or acc.n_extra or acc.n_diff) and not order_error
    if partial:
        rep["notes"].append(partial)
    if order_error:
        rep["notes"].append(order_error)
    if schema_bad or values_bad:
        verdict = "MISMATCH"
    elif partial or order_error or plan.unsupported:
        verdict = "NEEDS_REVIEW"
        if plan.unsupported:
            rep["notes"].append(f"unsupported column(s) not compared: {plan.unsupported}")
    elif sb and s_n == 0 and t_n == 0 and (rep["tail_rows"]["source"] or rep["tail_rows"]["target"]):
        verdict = "NEEDS_REVIEW"
        rep["notes"].append("no rows below the bound while newer rows exist — inconclusive")
    else:
        verdict = "MATCH"
    rep["verdict"], rep["check"] = verdict, VERDICT_CHECK[verdict]
    return rep


def _zero_date_findings(rep, zero_0101, keys=None, keyed=True, max_ex=20):
    """Explicit finding class for MySQL zero dates that became 0101-01-01 on the target —
    ONLY from paired per-key evidence (the same key's source value is a zero date and its
    target value 0101-01-01; those rows are already MISMATCH). Aggregate counters never
    create a finding: they can't tell a converted zero date from a genuine 0101-01-01 row.
    A key-less (multiset) table with differences and both observations is reported as
    `zero_date_inconclusive` — a note, not a finding, never an UPDATE recommendation."""
    keys = keys or {}
    findings = [{"column": c, "rows": n, "class": "zero_date_as_0101", "evidence": "paired_by_key",
                 "keys": keys.get(c, [])[:max_ex], "message": ZERO_DATE_0101_MESSAGE}
                for c, n in sorted(zero_0101.items())]
    if findings:
        rep["zero_date_findings"] = findings
        rep["mismatches"]["zero_date_as_0101"] = {f["column"]: f["rows"] for f in findings}
        rep["notes"].append(f"{sum(f['rows'] for f in findings)} row(s): " + ZERO_DATE_0101_MESSAGE)
        return
    zs, zd, mm = rep.get("zero_date_sentinels") or {}, rep.get("zero_dates") or {}, rep.get("mismatches") or {}
    differs = mm.get("missing_on_target") or mm.get("extra_on_target") or mm.get("different_rows")
    if not keyed and differs and zs.get("target") and zd.get("source"):
        rep["zero_date_inconclusive"] = {"source_zero_dates": zd["source"], "target_0101_values": zs["target"],
                                         "evidence": "unpaired (multiset)", "message": ZERO_DATE_INCONCLUSIVE_MESSAGE}
        rep["notes"].append(ZERO_DATE_INCONCLUSIVE_MESSAGE)


def plan_table(src_side, tgt_side, table, opts):
    """(plan, None) or (None, report-with-verdict) when the table cannot be planned."""
    to = _table_options(opts, table)
    if to.get("skip"):
        return None, {"verdict": "NOT_APPLICABLE", "check": "not_applicable", "notes": [f"skipped: {to['skip']}"]}
    src = side_table(src_side, table, opts)
    tgt = side_table(tgt_side, to.get("target") or table, opts)
    missing = [lbl for lbl, st in (("source", src), ("target", tgt)) if st is None]
    if missing:
        return None, {"verdict": "MISMATCH", "check": False, "notes": [f"table not found on {'/'.join(missing)}"]}
    return build_plan(table, src, tgt, opts), None


def compare_tables(src_side, tgt_side, tables, options=None, bounds=None, progress=None):
    """Full report for `tables` (no watermark unless `bounds` {table: bound} is given), all in
    ONE snapshot pair. `workers` is ignored here (it needs connection config — see
    compare_tables_parallel / the CLI). progress: a Progress (or None)."""
    opts = merge_options(options)
    started = dt.datetime.now(dt.timezone.utc)
    deadline = time.monotonic() + float(opts["max_seconds"])
    report = {"tool": "cross_engine_compare", "version": VERSION, "started_at": started.isoformat(),
              "source": {"dialect": src_side.dialect}, "target": {"dialect": tgt_side.dialect},
              "options": {k: v for k, v in opts.items() if k != "tables"}, "tables": {}}
    src_side.begin(opts["max_seconds"])
    tgt_side.begin(opts["max_seconds"])
    try:
        for t in tables:
            try:
                plan, early = plan_table(src_side, tgt_side, t, opts)
                if early is not None:
                    report["tables"][t] = early
                    continue
                if time.monotonic() > deadline:
                    report["tables"][t] = {"verdict": "NEEDS_REVIEW", "check": None,
                                           "notes": ["time budget exhausted before this table — not compared"]}
                    continue
                if progress is not None:
                    progress.start_table(t, row_estimate(src_side, plan.src))
                kr = scope_range(src_side, plan, opts)
                report["tables"][t] = compare_table(src_side, tgt_side, plan, opts, (bounds or {}).get(t), deadline,
                                                    key_range=kr,
                                                    progress=progress.add if progress is not None else None)
                report["tables"][t]["consistency"] = "single_snapshot"
                if kr is not None:
                    _mark_partial(report["tables"][t], kr, _table_options(opts, t))
            except CompareError as e:
                report["tables"][t] = {"verdict": "NEEDS_REVIEW", "check": None, "notes": [str(e)]}
            finally:
                if progress is not None and progress.table == t:
                    progress.end_table()
    finally:
        src_side.end()
        tgt_side.end()
    return _finish_report(report)


def _finish_report(report):
    verdicts = [r["verdict"] for r in report["tables"].values()]
    report["overall"] = ("MISMATCH" if "MISMATCH" in verdicts else "NEEDS_REVIEW" if "NEEDS_REVIEW" in verdicts
                         else "MATCH")
    # single_snapshot: every compared table was read in one snapshot pair (one consistent cut
    # per table); per_range_snapshots: at least one table was split into ranges, each in its
    # own snapshot pair — not a single cut (see PARALLEL_NOTE / writes_frozen).
    report["consistency"] = ("per_range_snapshots" if any(r.get("consistency") == "per_range_snapshots"
                                                         for r in report["tables"].values()) else "single_snapshot")
    report["writes_frozen"] = bool((report.get("options") or {}).get("writes_frozen"))
    report["coverage"] = ("partial" if any((r.get("coverage") or {}).get("partial") for r in report["tables"].values())
                          else "full")
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


# ── Parallel PK-range workers (CLI) ─────────────────────────────────────────────────────
_W = {}
PARALLEL_NOTE = ("parallel: {workers} worker processes over {ranges} integer-key ranges, EACH RANGE IN ITS OWN snapshot "
                 "pair (separate transactions opened up to the scan time apart) — per-range snapshots, NOT a single "
                 "consistent cut. Exact only while writes are frozen on both sides (--writes-frozen). On live data a "
                 "row changed between two ranges' snapshots can show as a difference (re-check the reported keys), "
                 "and a row whose key moved from a not-yet-read range into an already-read one can be MISSED")
PARALLEL_UNPROVEN_NOTE = ("every range matched, but per-range snapshots are not a single cut and writes were not declared "
                          "frozen — a row whose key moved between ranges during the scan can be missed, so this is NOT a "
                          "MATCH-as-proof (NEEDS_REVIEW): re-run under a write freeze with --writes-frozen, or with "
                          "--workers 1 (one snapshot pair), and reconcile any differing keys per key")
# Seconds the coordinator waits past the budget for workers to hand back their own partial
# (budget-exhausted) reports before it terminates them.
PARALLEL_GRACE_S = 5.0


def parallel_eligible(plan):
    """One integer key column on both sides (keyset ranges by value)."""
    return (plan.mode == "key_chunks" and len(plan.key) == 1 and plan.key[0].src.phys == "int"
            and plan.key[0].tgt.phys in ("int", "decimal") and (plan.key[0].tgt.scale or 0) == 0)


def key_ranges(lo, hi, parts):
    """(lo, hi] pairs covering EVERYTHING: first unbounded below, last unbounded above, so rows
    outside [lo, hi] at range-scan time are still compared. lo/hi = min/max key seen."""
    lo, hi = int(lo), int(hi)
    parts = max(1, min(int(parts), hi - lo + 1))
    cuts = [lo - 1 + ((hi - lo + 1) * i) // parts for i in range(1, parts)]
    bounds = [None] + cuts + [None]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _worker_init(cfg, options, queue):
    sys.dont_write_bytecode = True
    _W["opts"] = merge_options(options)
    _W["q"] = queue
    _W["src"] = connect_side(cfg["source"], "source", _W["opts"]["pg_driver"])
    _W["tgt"] = connect_side(cfg["target"], "target", _W["opts"]["pg_driver"])


def _worker_range(table, key_range, wall_deadline):
    src, tgt, opts, q = _W["src"], _W["tgt"], _W["opts"], _W["q"]
    remaining = wall_deadline - time.time()
    if remaining <= 0:
        return range_error(key_range, "time budget exhausted before this range started — not compared")
    deadline = time.monotonic() + remaining
    src.begin(remaining)
    tgt.begin(remaining)
    try:
        plan, early = plan_table(src, tgt, table, opts)
        if early is not None:
            return early
        return compare_table(src, tgt, plan, opts, deadline=deadline, key_range=key_range,
                             progress=(lambda n: q.put((table, n))) if q is not None else None)
    finally:
        src.end()
        tgt.end()


def range_error(key_range, error):
    """The per-range stand-in report for a range that produced no evidence (worker exception,
    broken pool, coordinator deadline). Never a MATCH; other ranges' evidence is kept."""
    return {"verdict": "NEEDS_REVIEW", "check": None,
            "range_error": {"range": [None if b is None else str(b) for b in key_range], "error": str(error)},
            "notes": [f"range ({key_range[0]}, {key_range[1]}] not compared: {error}"]}


def merge_range_reports(parts, ranges, workers, writes_frozen=False):
    """One table report from per-range reports (in key order): sums, examples in key order
    truncated to max_examples — the same mismatch set and counts as one sequential scan. The
    digest is sha256 over the ordered range digests (digest_scheme "ranges"). Ranges without
    evidence are listed in `range_errors`; MISMATCH takes precedence over NEEDS_REVIEW, and an
    all-ranges MATCH is MATCH-as-proof only with writes_frozen (or a single range)."""
    errors = [p["range_error"] for p in parts if p.get("range_error")]
    multi = len(ranges) > 1
    consistency = "per_range_snapshots" if multi else "single_snapshot"
    full = [p for p in parts if "mismatches" in p]
    if not full:
        verdicts = [p["verdict"] for p in parts]
        v = "MISMATCH" if "MISMATCH" in verdicts else "NEEDS_REVIEW" if "NEEDS_REVIEW" in verdicts else "MATCH"
        if v == "MATCH" and multi and not writes_frozen:
            v = "NEEDS_REVIEW"
        out = {"verdict": v, "check": VERDICT_CHECK[v], "consistency": consistency,
               "notes": [n for p in parts for n in p.get("notes", [])]}
        if errors:
            out["range_errors"] = errors
        return out
    base = full[0]
    rep = {k: base[k] for k in ("target_table", "source_table", "mode", "key", "columns", "type_findings",
                                "ignored_columns", "unsupported_columns") if k in base}
    common = set(base.get("notes", []))
    for p in full[1:]:
        common &= set(p.get("notes", []))
    base_notes = [n for n in base.get("notes", []) if n in common and not n.endswith(ZERO_DATE_0101_MESSAGE)]
    notes = []
    rows = {"source": 0, "target": 0}
    mm = {"missing_on_target": 0, "extra_on_target": 0, "different_rows": 0}
    diff_cols, z0101, zd, zs = Counter(), Counter(), Counter(), Counter()
    z_keys = {}
    policy = None
    ex = {"missing_on_target": [], "extra_on_target": [], "different": []}
    s_h, t_h = hashlib.sha256(), hashlib.sha256()
    chunk_count, chunk_bad, chunk_size = 0, [], None
    verdicts = []
    for p, rg in zip(parts, ranges):
        verdicts.append(p["verdict"])
        tag = f"range ({rg[0]}, {rg[1]}]"
        for n in p.get("notes", []):
            if n not in base_notes and not n.endswith(ZERO_DATE_0101_MESSAGE) and n not in notes:
                notes.append(f"{tag}: {n}")
        if "mismatches" not in p:
            continue
        rows["source"] += p["rows"]["source"]
        rows["target"] += p["rows"]["target"]
        for k in mm:
            mm[k] += p["mismatches"][k]
        diff_cols.update(p["mismatches"]["differing_columns"])
        z0101.update(p["mismatches"].get("zero_date_as_0101") or {})
        for f in p.get("zero_date_findings") or []:
            z_keys.setdefault(f["column"], []).extend(f.get("keys") or [])
        for k in ex:
            ex[k].extend(p["mismatches"]["examples"][k])
        s_h.update(f"{rg}:{p['digest']['source']};".encode())
        t_h.update(f"{rg}:{p['digest']['target']};".encode())
        if "chunks" in p:
            chunk_size = p["chunks"]["size"]
            chunk_count += p["chunks"]["count"]
            chunk_bad.extend(p["chunks"]["mismatched"])
        for side in ("source", "target"):
            zd[side] += (p.get("zero_dates") or {}).get(side, 0)
            zs[side] += (p.get("zero_date_sentinels") or {}).get(side, 0)
        policy = (p.get("zero_dates") or {}).get("policy") or policy
    rep["notes"] = base_notes + [PARALLEL_NOTE.format(workers=workers, ranges=len(ranges))] + notes
    rep["parallel"] = {"workers": workers, "ranges": len(ranges), "snapshots": "one per range",
                       "writes_frozen": bool(writes_frozen), "ranges_without_evidence": len(errors)}
    rep["consistency"] = consistency
    if errors:
        rep["range_errors"] = errors
    rep["rows"] = rows
    rep["digest"] = {"source": s_h.hexdigest(), "target": t_h.hexdigest()}
    rep["digest_scheme"] = "ranges"
    rep["mismatches"] = {**mm, "differing_columns": dict(diff_cols), "examples": ex}
    if chunk_size is not None:
        rep["chunks"] = {"size": chunk_size, "count": chunk_count, "mismatched": chunk_bad}
    if zd:
        rep["zero_dates"] = {"source": zd["source"], "target": zd["target"], "policy": policy}
    if zs:
        rep["zero_date_sentinels"] = {"value": "0101-01-01", "source": zs["source"], "target": zs["target"]}
    _zero_date_findings(rep, z0101, z_keys, keyed=True, max_ex=10 ** 9)
    v = "MISMATCH" if "MISMATCH" in verdicts else "NEEDS_REVIEW" if "NEEDS_REVIEW" in verdicts else "MATCH"
    if v == "MATCH" and multi and not writes_frozen:
        v = "NEEDS_REVIEW"
        rep["unproven_verdict"] = "MATCH"
        rep["notes"].append(PARALLEL_UNPROVEN_NOTE)
    rep["verdict"], rep["check"] = v, VERDICT_CHECK[v]
    return rep


def _truncate_examples(rep, max_ex):
    if "mismatches" in rep:
        for k, lst in rep["mismatches"]["examples"].items():
            del lst[max_ex:]
        if "chunks" in rep:
            del rep["chunks"]["mismatched"][max_ex:]
    for f in rep.get("zero_date_findings") or []:
        del f["keys"][max_ex:]
    return rep


def collect_range_results(futs, ranges, deadline, queue=None, progress=None, grace=PARALLEL_GRACE_S,
                          poll=0.5, clock=time.monotonic):
    """Waits for the range futures without trusting any single one: each result is collected
    independently (an exception becomes that range's range_error, other ranges keep their
    evidence), progress is drained and a timed heartbeat emitted every `poll` s even when no
    worker reports, and the wait ends at deadline + grace. Returns (parts, timed_out) — parts
    in range order; ranges still running at the end are range_errors and their futures are
    cancelled (the caller terminates the pool's processes when timed_out)."""
    from concurrent.futures import wait, FIRST_COMPLETED
    pending = set(futs)
    timed_out = False
    while pending:
        left = deadline + grace - clock()
        if left <= 0:
            timed_out = True
            break
        done, pending = wait(pending, timeout=min(poll, left), return_when=FIRST_COMPLETED)
        if queue is not None:
            _drain(queue, progress, timeout=0)
        if progress is not None:
            progress.emit()                      # heartbeat: interval-gated, independent of worker messages
    if queue is not None:
        _drain(queue, progress, timeout=0)
    parts = []
    for f, rg in zip(futs, ranges):
        if not f.done():
            f.cancel()
            parts.append(range_error(rg, "time budget exhausted — the coordinator stopped waiting for this range "
                                         "(worker terminated); not compared"))
            continue
        try:
            parts.append(f.result())
        except Exception as e:                    # worker connect/driver failure, BrokenProcessPool, ...
            parts.append(range_error(rg, f"worker failed ({type(e).__name__}: {e})"))
    return parts, timed_out


def submit_and_collect(pool, fn, table, ranges, wall_deadline, deadline, queue=None, progress=None, **collect_kw):
    """Submits the ranges one at a time: a submit() failure (BrokenProcessPool after a worker
    died, a shut-down pool, ...) stops further submissions but keeps every future already
    submitted — their completed evidence is collected as usual — and each range that was never
    submitted becomes a range_error. Returns (parts in range order, pool_unusable)."""
    futs, submitted, failure = [], [], None
    for rg in ranges:
        try:
            futs.append(pool.submit(fn, table, rg, wall_deadline))
        except Exception as e:                    # BrokenProcessPool, RuntimeError (shutdown), pickling ...
            failure = e
            break
        submitted.append(rg)
    got, timed_out = collect_range_results(futs, submitted, deadline, queue, progress, **collect_kw)
    by_range = dict(zip(submitted, got))
    parts = [by_range[rg] if rg in by_range else
             range_error(rg, f"not submitted: worker pool failed ({type(failure).__name__}: {failure})")
             for rg in ranges]
    unusable = (timed_out or failure is not None
                or any(p.get("range_error") and "Broken" in p["range_error"]["error"] for p in got))
    return parts, unusable


def _kill_pool(pool):
    """Stops a ProcessPoolExecutor whose running futures can't be cancelled: no new work,
    then terminate its worker processes (they hold read-only snapshots only)."""
    procs = list((getattr(pool, "_processes", None) or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for pr in procs:
        try:
            pr.terminate()
        except Exception:
            pass
    for pr in procs:
        try:
            pr.join(timeout=5)
        except Exception:
            pass


def compare_tables_parallel(cfg, tables, options=None, progress=None, src_side=None, tgt_side=None):
    """compare_tables with `workers` processes for tables with one integer key (others run
    sequentially in the coordinator's snapshot). Each worker opens its own connections from
    `cfg` (password_env / secret_arn are resolved in the worker) and reads each range in a
    new snapshot pair — see PARALLEL_NOTE for what that means on live data."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    opts = merge_options(options)
    workers = int(opts["workers"])
    own = src_side is None
    if own:
        src_side = connect_side(cfg["source"], "source", opts["pg_driver"])
        try:
            tgt_side = connect_side(cfg["target"], "target", opts["pg_driver"])
        except Exception:
            _close(src_side)
            raise
    started = dt.datetime.now(dt.timezone.utc)
    wall_deadline = time.time() + float(opts["max_seconds"])
    deadline = time.monotonic() + float(opts["max_seconds"])
    report = {"tool": "cross_engine_compare", "version": VERSION, "started_at": started.isoformat(),
              "source": {"dialect": src_side.dialect}, "target": {"dialect": tgt_side.dialect},
              "options": {k: v for k, v in opts.items() if k != "tables"}, "tables": {}}
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    pool = None
    src_side.begin(opts["max_seconds"])
    tgt_side.begin(opts["max_seconds"])
    try:
        for t in tables:
            try:
                plan, early = plan_table(src_side, tgt_side, t, opts)
                if early is not None:
                    report["tables"][t] = early
                    continue
                if time.monotonic() > deadline:
                    report["tables"][t] = {"verdict": "NEEDS_REVIEW", "check": None,
                                           "notes": ["time budget exhausted before this table — not compared"]}
                    continue
                if progress is not None:
                    progress.start_table(t, row_estimate(src_side, plan.src))
                try:
                    kr = scope_range(src_side, plan, opts)
                    if kr is not None:   # a partial in-freeze check: one snapshot, no ranges
                        r = compare_table(src_side, tgt_side, plan, opts, deadline=deadline, key_range=kr,
                                          progress=progress.add if progress is not None else None)
                        r["consistency"] = "single_snapshot"
                        report["tables"][t] = _mark_partial(r, kr, _table_options(opts, t))
                        continue
                    if not parallel_eligible(plan):
                        r = compare_table(src_side, tgt_side, plan, opts, deadline=deadline,
                                          progress=progress.add if progress is not None else None)
                        r["notes"].append(f"workers={workers} not used for this table: parallel ranges need one integer "
                                          "key column on both sides — compared sequentially")
                        r["consistency"] = "single_snapshot"
                        report["tables"][t] = r
                        continue
                    k = plan.key[0]
                    mins = [src_side.query(minmax_sql(src_side.dialect, plan.src, k.src.name, "MIN"))[0][0],
                            tgt_side.query(minmax_sql(tgt_side.dialect, plan.tgt, k.tgt.name, "MIN"))[0][0]]
                    maxs = [src_side.query(minmax_sql(src_side.dialect, plan.src, k.src.name, "MAX"))[0][0],
                            tgt_side.query(minmax_sql(tgt_side.dialect, plan.tgt, k.tgt.name, "MAX"))[0][0]]
                    mins, maxs = [int(v) for v in mins if v is not None], [int(v) for v in maxs if v is not None]
                    ranges = key_ranges(min(mins), max(maxs), workers * 4) if mins else [(None, None)]
                    if pool is None:
                        pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                                                   initargs=(cfg, options, queue))
                    parts, unusable = submit_and_collect(pool, _worker_range, t, ranges, wall_deadline, deadline,
                                                         queue, progress)
                    if unusable:
                        _kill_pool(pool)
                        pool = None
                    report["tables"][t] = _truncate_examples(
                        merge_range_reports(parts, ranges, workers, bool(opts["writes_frozen"])), int(opts["max_examples"]))
                finally:
                    if progress is not None:
                        progress.end_table()
            except CompareError as e:
                report["tables"][t] = {"verdict": "NEEDS_REVIEW", "check": None, "notes": [str(e)]}
    finally:
        src_side.end()
        tgt_side.end()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        queue.close()
        if own:
            _close(src_side)
            _close(tgt_side)
    return _finish_report(report)


def _drain(queue, progress, timeout):
    import queue as _q
    end = time.monotonic() + timeout
    while True:
        try:
            _t, n = queue.get(timeout=max(0.0, end - time.monotonic())) if timeout else queue.get_nowait()
        except (_q.Empty, OSError, ValueError):
            return
        if progress is not None:
            progress.add(n)


# ── Soak integration (soak_check.py / soak_check_lambda.py) ─────────────────────────────
_SOAK_NOTE = ("compared rows up to the watermark only; the tail beyond it is informational "
              "(replication lag), not a failure")


def utc_now_sql(dialect):
    return "SELECT UTC_TIMESTAMP(6)" if dialect == "mysql" else "SELECT (now() AT TIME ZONE 'UTC')"


def cross_family_lag_problem(cfg):
    """The one replication-lag config rule for a cross-family soak, shared by the normal runs
    (soak_check.py / soak_check_lambda.py) and the Lambda preflight: DMS metrics are the only
    lag source; a native replica-status / pg side without a DMS task cannot be measured."""
    if not (cfg.get("dms_task_id") and cfg.get("dms_replication_instance_id")) and (
            cfg.get("mysql_replica_status_side") or cfg.get("pg_replication_lag_side")):
        return ("native replication cannot run between MySQL and PostgreSQL — configure the DMS task "
                "(dms_task_id + dms_replication_instance_id / DMS_TASK_ID + DMS_REPLICATION_INSTANCE_ID) "
                "for replication_lag and clear mysql_replica_status_side / pg_replication_lag_side")
    return None


def watermark_ops(plan, wcfg):
    """(mode, cp, ops, reason) — the watermark the soak run selects for this table and the
    EXACT endpoint statements it issues to resolve it: ops = {"source": [...], "target": [...]}.
    mode "timestamp" (configured naive DATETIME/timestamp or DATE column on both sides; source
    UTC clock), "pk" (single integer source key; MAX+MIN on the source, MAX on the target —
    which may be NUMERIC there), or None (whole table; reason says why). The run and the Lambda
    preflight both use this, so preflight probes what the run executes."""
    none = {"source": [], "target": []}
    if not wcfg.get("enabled", True):
        return None, None, none, "watermark disabled in config — whole-table comparison"
    sd = plan.src.cols[0].dialect if plan.src.cols else "mysql"
    td = plan.tgt.cols[0].dialect if plan.tgt.cols else "postgres"
    ts_col = (wcfg.get("timestamp_columns") or {}).get(plan.table)
    if ts_col:
        cp = next((c for c in plan.cols if c.src.name == ts_col), None)
        if cp is None:
            return None, None, none, f"timestamp column {ts_col!r} not found on both sides — whole-table comparison"
        if not ({cp.src.kind, cp.tgt.kind} <= {"datetime"} or {cp.src.kind, cp.tgt.kind} <= {"date"}):
            return None, None, none, (f"timestamp column {ts_col!r} is {cp.src.raw_type} / {cp.tgt.raw_type} — the "
                                      "cross-engine timestamp watermark needs a naive DATETIME/timestamp (or DATE) on "
                                      "both sides — whole-table comparison")
        return "timestamp", cp, {"source": [utc_now_sql(sd)], "target": []}, None
    if len(plan.key) == 1 and plan.key[0].src.kind == "int" and plan.key[0].tgt.kind in ("int", "decimal"):
        cp = plan.key[0]
        return "pk", cp, {"source": [minmax_sql(sd, plan.src, cp.src.name, "MAX"), minmax_sql(sd, plan.src, cp.src.name, "MIN")],
                          "target": [minmax_sql(td, plan.tgt, cp.tgt.name, "MAX")]}, None
    return None, None, none, ("no single-column integer primary key and no timestamp column configured — whole-table "
                              "comparison; under live writes this can be RED from replication lag alone")


def soak_probe_bound(plan, wcfg):
    """(dummy_bound | None, message, ops) — the bound SHAPE a soak run would use, plus the
    watermark statements it would run (watermark_ops), without reading data. Raises for a
    configured but unusable timestamp column, like the homogeneous preflight."""
    mode, cp, ops, why = watermark_ops(plan, wcfg)
    ts_col = (wcfg.get("timestamp_columns") or {}).get(plan.table)
    if ts_col and wcfg.get("enabled", True) and mode != "timestamp":
        raise CompareError(f"configured {why}")
    if mode == "timestamp":
        value = "1970-01-01" if cp.src.kind == "date" else "1970-01-01 00:00:00"
        return ({"column": ts_col, "op": "<=", "value": value, "include_nulls": cp.src.nullable or cp.tgt.nullable},
                f"timestamp_watermark on {ts_col}", ops)
    if mode == "pk":
        return ({"column": cp.src.name, "op": "<=", "value": 0, "include_nulls": False},
                f"pk_watermark on {cp.src.name}", ops)
    return None, "whole-table comparison — " + (plan.note or why), ops


def _soak_bound(src_side, tgt_side, plan, wcfg, opts, table):
    """(bound | None, extras, reason). Same concept as the homogeneous watermark: a
    configured timestamp column (naive DATETIME/timestamp or DATE on BOTH sides; cutoff =
    source UTC clock - age, expressed in naive_timezone), else a single integer key.
    The statements come from watermark_ops (shared with the Lambda preflight)."""
    mode, cp, ops, why = watermark_ops(plan, wcfg)
    if mode is None:
        return None, {}, why
    if mode == "timestamp":
        ts_col = cp.src.name
        now = src_side.query(ops["source"][0])[0][0]
        if isinstance(now, str):
            now = dt.datetime.fromisoformat(now)
        cutoff = (now.replace(tzinfo=dt.timezone.utc) - dt.timedelta(minutes=int(wcfg["timestamp_age_minutes"])))
        cutoff = cutoff.astimezone(_tzinfo(opts["naive_timezone"])).replace(tzinfo=None)
        value = cutoff.date().isoformat() if cp.src.kind == "date" else cutoff.isoformat(sep=" ", timespec="microseconds")
        nullable = cp.src.nullable or cp.tgt.nullable
        return ({"column": ts_col, "op": "<=", "value": value, "include_nulls": nullable},
                {"column": ts_col, "age_minutes": int(wcfg["timestamp_age_minutes"]), "watermark": value,
                 "naive_timezone": opts["naive_timezone"]}, "timestamp")
    if mode == "pk":
        smax = src_side.query(ops["source"][0])[0][0]
        smin = src_side.query(ops["source"][1])[0][0]
        tmax = tgt_side.query(ops["target"][0])[0][0]
        smax, smin, tmax = [None if v is None else int(v) for v in (smax, smin, tmax)]
        wm, why = pk_watermark(smax, tmax, smin, wcfg["pk_margin"])
        extras = {"column": cp.src.name, "max_pk": {"source": smax, "target": tmax}, "margin": int(wcfg["pk_margin"])}
        if wm is None:
            return None, extras, why
        extras["watermark"] = wm
        return {"column": cp.src.name, "op": "<=", "value": wm, "include_nulls": False}, extras, "pk"
    return None, {}, ("no single-column integer primary key and no timestamp column configured — whole-table "
                      "comparison; under live writes this can be RED from replication lag alone")


def _count(side, st, where):
    return int(side.query(count_sql(side.dialect, st, where))[0][0])


def soak_checks(src_side, tgt_side, tables, checksum_tables, wcfg, options=None):
    """(row_check, checksum_check, drift_check) in the soak scripts' shape for a
    mysql<->postgres pair. Opens/closes the READ ONLY snapshot on both sides itself."""
    opts = merge_options(options)
    deadline = time.monotonic() + float(opts["max_seconds"])
    pair = f"{src_side.dialect}->{tgt_side.dialect}"
    row_check, checksum_check, drift_check = {"pass": True, "detail": {}}, {"pass": True, "detail": {}}, {"pass": True, "detail": {}}
    row_oks, ck_oks = [], []
    union = list(dict.fromkeys(list(tables) + list(checksum_tables)))
    src_side.begin(opts["max_seconds"])
    tgt_side.begin(opts["max_seconds"])
    try:
        for t in union:
            try:
                plan, early = plan_table(src_side, tgt_side, t, opts)
            except CompareError as e:
                plan, early = None, {"verdict": "NEEDS_REVIEW", "check": None, "notes": [str(e)]}
            if early is not None:
                note = "; ".join(early.get("notes") or [])
                if t in tables:
                    row_check["detail"][t] = {"source": None, "target": None, "mode": "whole_table", "note": note,
                                              "comparison": "cross_engine_normalized", "engines": pair,
                                              "match": early["check"]}
                    row_oks.append(early["check"])
                    if early["check"] is not True and early["check"] != "not_applicable":
                        drift_check["pass"] = False
                        drift_check["detail"][t] = {"source_only": [], "target_only": [], "mismatched_attributes": {},
                                                    "note": note}
                if t in checksum_tables:
                    checksum_check["detail"][t] = {"source": None, "target": None, "mode": "whole_table", "note": note,
                                                   "comparison": "cross_engine_normalized", "match": early["check"]}
                    ck_oks.append(early["check"])
                continue
            bound, extras, mode_or_why = _soak_bound(src_side, tgt_side, plan, wcfg, opts, t)
            mode = mode_or_why if bound else None
            sb, tb = _side_bound(plan, bound, "source"), _side_bound(plan, bound, "target")
            if bound:
                s_le = [bound_sql(src_side.dialect, sb["column"], sb)]
                t_le = [bound_sql(tgt_side.dialect, tb["column"], tb)]
                sc, tc = _count(src_side, plan.src, s_le), _count(tgt_side, plan.tgt, t_le)
                tail = {"source": _count(src_side, plan.src, [bound_sql(src_side.dialect, sb["column"], sb, tail=True)]),
                        "target": _count(tgt_side, plan.tgt, [bound_sql(tgt_side.dialect, tb["column"], tb, tail=True)])}
                ok = None if (sc == 0 and tc == 0 and (tail["source"] or tail["target"])) else (sc == tc)
                note = _SOAK_NOTE if ok is not None else (
                    "no rows below the watermark while recent rows exist — inconclusive, needs review "
                    "(lower timestamp_age_minutes or use the PK watermark)")
                if bound.get("include_nulls"):
                    q_s, q_t = qident(src_side.dialect, sb["column"]), qident(tgt_side.dialect, tb["column"])
                    extras["null_rows"] = {"source": _count(src_side, plan.src, [f"{q_s} IS NULL"]),
                                           "target": _count(tgt_side, plan.tgt, [f"{q_t} IS NULL"])}
                    if extras["null_rows"]["source"] != extras["null_rows"]["target"]:
                        ok = False
                    note += "; NULL-timestamp rows are included in the compared set and counted separately"
                detail = {"source": sc, "target": tc, "mode": f"{mode}_watermark", **extras, "tail_rows": tail, "note": note}
            else:
                sc, tc = _count(src_side, plan.src, []), _count(tgt_side, plan.tgt, [])
                ok = sc == tc
                note = mode_or_why
                detail = {"source": sc, "target": tc, "mode": "whole_table", "note": note, **extras}
            detail.update({"comparison": "cross_engine_normalized", "engines": pair, "match": ok})
            if t in tables:
                row_check["detail"][t] = detail
                row_oks.append(ok)
                problems = {}
                for fd in plan.findings:
                    if fd.get("column") and (fd["severity"] == "mismatch" or "nullability" in fd["message"]):
                        cp = next((c for c in plan.cols if c.src.name == fd["column"]), None)
                        if cp is not None:
                            problems[fd["column"]] = {"source": cp.src.as_dict(), "target": cp.tgt.as_dict(),
                                                      "finding": fd["message"]}
                if plan.source_only or plan.target_only or problems:
                    drift_check["pass"] = False
                    drift_check["detail"][t] = {"source_only": sorted(plan.source_only),
                                                "target_only": sorted(plan.target_only),
                                                "mismatched_attributes": problems}
            if t in checksum_tables:
                rep = compare_table(src_side, tgt_side, plan, opts, bound, deadline)
                ck_ok = None if ok is None else rep["check"]
                checksum_check["detail"][t] = {
                    "source": rep["digest"]["source"], "target": rep["digest"]["target"],
                    "mode": f"{mode}_watermark" if bound else "whole_table", "column": extras.get("column"),
                    "watermark": extras.get("watermark"), "note": note, "comparison": "cross_engine_normalized",
                    "engines": pair, "verdict": rep["verdict"], "rows": rep["rows"], "mismatches": rep["mismatches"],
                    "type_findings": [fd for fd in rep["type_findings"] if fd["severity"] in ("mismatch", "risk", "unsupported")],
                    "notes": rep["notes"], "match": ck_ok}
                ck_oks.append(ck_ok)
    finally:
        src_side.end()
        tgt_side.end()
    row_check["pass"] = _combine(row_oks)
    checksum_check["pass"] = _combine(ck_oks)
    return row_check, checksum_check, drift_check


def _combine(values):
    values = [v for v in values if v != "not_applicable"]
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


# ── Connections (CLI and soak_check.py standalone) ──────────────────────────────────────
def _default_ca():
    for p in _DEFAULT_CA_BUNDLES:
        if p.exists():
            return str(p)
    raise CompareError("bundled RDS CA bundle (rds-global-bundle.pem) not found next to this script or in "
                       "../assets — set ssl_ca explicitly")


def tls_context(ssl_ca=None, insecure=False):
    """Same three tiers as soak_check*.py: default = verify-full against the bundled AWS RDS CA
    bundle; ssl_ca = pin that CA (no hostname check); ssl_insecure = encrypt, no verification
    (explicit opt-in only). Never plaintext."""
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=ssl_ca or _default_ca())
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = ssl_ca is None
    return ctx


def resolve_credentials(side_cfg, label):
    if "password" in side_cfg:
        raise CompareError(f"{label}: use password_env or secret_arn, never a password in the config file")
    user = side_cfg.get("user")
    if side_cfg.get("secret_arn"):
        try:
            import boto3
        except ImportError as e:
            raise CompareError(f"{label}: secret_arn needs boto3") from e
        region = side_cfg.get("region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        sec = json.loads(boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=side_cfg["secret_arn"])["SecretString"])
        return user or sec.get("username") or sec.get("user"), sec["password"]
    env = side_cfg.get("password_env")
    if not env or env not in os.environ:
        raise CompareError(f"{label}: password_env must name an environment variable that is set on this host")
    return user, os.environ[env]


def connect_side(side_cfg, label, pg_driver=None, connect_timeout=10, read_timeout=300):
    dialect = engine_family(side_cfg.get("engine"))
    user, password = resolve_credentials(side_cfg, label)
    host, db = side_cfg["host"], side_cfg["database"]
    ssl_ca, insecure = side_cfg.get("ssl_ca"), bool(side_cfg.get("ssl_insecure"))
    if dialect == "mysql":
        try:
            import pymysql
        except ImportError as e:
            raise CompareError("MySQL side needs pymysql (pip install pymysql)") from e
        conn = pymysql.connect(host=host, port=int(side_cfg.get("port") or 3306), user=user, password=password,
                               database=db, connect_timeout=connect_timeout, read_timeout=read_timeout,
                               charset="utf8mb4", ssl=tls_context(ssl_ca, insecure))
        return SqlSide("mysql", conn, label)
    port = int(side_cfg.get("port") or 5432)
    order = [pg_driver or side_cfg.get("pg_driver")] if (pg_driver or side_cfg.get("pg_driver")) else ["pg8000", "psycopg", "psycopg2"]
    errors = []
    for drv in order:
        try:
            if drv == "pg8000":
                import pg8000.native as pgn
                conn = pgn.Connection(host=host, port=port, user=user, password=password, database=db,
                                      timeout=connect_timeout, ssl_context=tls_context(ssl_ca, insecure))
                sock = getattr(conn, "_usock", None)
                if sock is not None and hasattr(sock, "settimeout"):
                    sock.settimeout(read_timeout)
                return SqlSide("postgres", conn, label)
            if drv in ("psycopg", "psycopg2"):
                mod = __import__(drv)
                mode = "require" if insecure else ("verify-ca" if ssl_ca else "verify-full")
                # psycopg ignores socket read timeouts: bound every statement server-side instead
                kw = {"host": host, "port": port, "user": user, "password": password, "dbname": db,
                      "connect_timeout": connect_timeout, "sslmode": mode,
                      "options": f"-c statement_timeout={int(read_timeout * 1000)}"}
                if not insecure:
                    kw["sslrootcert"] = ssl_ca or _default_ca()
                return SqlSide("postgres", mod.connect(**kw), label)
            raise CompareError(f"unknown pg_driver {drv!r}")
        except ImportError as e:
            errors.append(f"{drv}: {e}")
    raise CompareError("PostgreSQL side needs pg8000, psycopg or psycopg2: " + "; ".join(errors))


def _close(side):
    try:
        side.conn.close()
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Normalized MySQL <-> PostgreSQL table comparison (read-only).",
        epilog="Throughput (1M mixed-type rows, MySQL 8.0 -> PostgreSQL 16 in local Docker, 8 vCPU Graviton, "
               "pg8000): ~25.7k rows/s with --workers 1 (16.7k before the v2 fast paths), ~98k rows/s with "
               "--workers 4. Small hosts are slower (dry run 5: ~7k rows/s on a t4g.small). Budget Phase 7 as "
               "rows / rows_per_s from the progress output; see mysql-postgresql.md §9 for the in-window quick "
               "check vs post-switch full compare split.")
    ap.add_argument("--config", required=True, type=Path,
                    help="JSON with source/target (soak-config.json shape), tables, optional cross_engine options")
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--tables", help="comma-separated subset of the configured tables")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--include-values", action="store_true", help="report canonical values of differing columns")
    ap.add_argument("--workers", type=int,
                    help="parallel PK-range worker processes (default 1). Each range is read in ITS OWN snapshot "
                         "pair — per-range snapshots, not a single cut: exact only while writes are frozen "
                         "(--writes-frozen); on LIVE data rows written between the ranges' snapshots can show as "
                         "differences or be missed, so an all-ranges MATCH is NEEDS_REVIEW unless --writes-frozen. "
                         "Tables without one integer key are compared sequentially (one snapshot)")
    ap.add_argument("--writes-frozen", action="store_true",
                    help="assert that writes are stopped on both sides for the whole run (cutover freeze); recorded "
                         "in the report. With --workers > 1 it is required for a MATCH to count as proof — without it "
                         "an all-ranges MATCH is NEEDS_REVIEW (per-range snapshots are not a single cut)")
    ap.add_argument("--progress-file", type=Path,
                    help="atomically rewritten JSON progress (table, rows_compared, rows_total_estimate, rows_per_s, "
                         "eta_s, updated_at, workers, state) for the dashboard")
    ap.add_argument("--progress-interval", type=float, default=15.0, help="seconds between progress updates (15)")
    ap.add_argument("--no-progress", action="store_true", help="no progress lines on stderr")
    args = ap.parse_args(argv)
    try:
        cfg = json.loads(args.config.read_text())
        options = dict(cfg.get("cross_engine") or {})
        if args.max_seconds:
            options["max_seconds"] = args.max_seconds
        if args.include_values:
            options["include_values"] = True
        if args.workers:
            options["workers"] = args.workers
        if args.writes_frozen:
            options["writes_frozen"] = True
        opts = merge_options(options)
        tables = cfg.get("tables") or []
        if args.tables:
            tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        if not tables:
            raise CompareError("tables must be a nonempty list")
        src = connect_side(cfg["source"], "source", opts["pg_driver"])
        try:
            tgt = connect_side(cfg["target"], "target", opts["pg_driver"])
        except Exception:
            _close(src)
            raise
    except (CompareError, KeyError, ValueError, OSError) as e:
        print(f"cross_engine_compare.py: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    progress = Progress(stream=None if args.no_progress else sys.stderr, path=args.progress_file,
                        interval=args.progress_interval, workers=int(opts["workers"]), tables_total=len(tables))
    try:
        if int(opts["workers"]) > 1:
            report = compare_tables_parallel(cfg, tables, options, progress, src, tgt)
        else:
            report = compare_tables(src, tgt, tables, options, progress=progress)
    finally:
        _close(src)
        _close(tgt)
    progress.finish(report["overall"])
    report["source"].update({"engine": cfg["source"].get("engine"), "host": cfg["source"].get("host"),
                             "database": cfg["source"].get("database")})
    report["target"].update({"engine": cfg["target"].get("engine"), "host": cfg["target"].get("host"),
                             "database": cfg["target"].get("database")})
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    else:
        print(text)
    for t, r in report["tables"].items():
        print(f"{r['verdict']:<14} {t}", file=sys.stderr)
    return {"MATCH": 0, "MISMATCH": 1, "NEEDS_REVIEW": 2}[report["overall"]]


if __name__ == "__main__":
    sys.exit(main())
