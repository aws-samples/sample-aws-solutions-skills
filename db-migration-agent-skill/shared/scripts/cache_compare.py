#!/usr/bin/env python3
"""Read-only source/target comparison for cache migrations (Redis OSS / Valkey →
ElastiCache, ElastiCache Redis OSS → Valkey; Memcached stats). Reference:
shared/reference/elasticache-migration.md §Validation.

WHAT IT CHECKS (Redis OSS / Valkey, standalone or cluster mode on EITHER side):
  key_count        INFO keyspace per logical db (summed over every primary of a cluster),
                   within an optional tolerance.
  sample_match     A deterministic sample of keys (bottom-k by a keyed hash of the key
                   name, so the same seed selects the same keys on both sides and across
                   runs, independent of SCAN order) — or every key with --full-scan —
                   compared for TYPE, TTL (within tolerance) and a per-type value digest:
                   string (bytes), list (ordered), hash / set (order-independent, sorted),
                   zset (member → score, sorted), stream (entries in ID order with their raw,
                   ordered field/value pairs — duplicate field names kept — + consumer
                   group names/last-delivered-id). Keys sampled on the target are checked
                   on the source too (extras). Mismatches are re-read once after a short
                   delay; ones that resolve are reported as transient, not as failures.
                   Module types and collections over max_elements (type/TTL/cardinality
                   only) are UNKNOWN (null), never a pass. Non-zero dbs on a cluster side
                   are compared where SELECT works (Valkey 9 cluster-databases), else unknown.
  replication_link Only with "expect_replication": every target primary that is a
                   replica reports master_link_status:up (online migration / REPLICAOF).
  alarms           Only with "alarm_names": CloudWatch alarm state via the aws CLI.
  headroom         Target used_memory vs maxmemory per primary (not_applicable when
                   maxmemory is 0/hidden, e.g. Serverless).
  Memory totals are reported for information only — encodings differ across versions.

MEMCACHED MODE ("engine": "memcached" on both sides) is explicitly NOT a data
comparison: memcached cannot enumerate keys. It compares `stats` (curr_items, hit ratio,
evictions, limits) and, only when thresholds are configured, reports a cache-warmth
check. Its data verdict is always "not_applicable".

VERDICTS use the soak scripts' four states: true / false / null (configured but not
established — unreachable, time budget exhausted (checked inside every chunked read and
re-read), unsupported type, any recorded error) / "not_applicable". Every configured check
starts as null; an error in any later step can never leave a passing verdict. Alarms are
evaluated for both engines. The dashboard gets a four-state result per keyspace, and soak
days are published through dashboard_update.py --soak-day-file (verbatim day, streak/state
recomputed inside its lock).

READ-ONLY: only INFO, CLUSTER SLOTS, SCAN/HSCAN/SSCAN/ZSCAN, TYPE, PTTL, *LEN/*CARD,
GET/GETRANGE, LRANGE, XRANGE, XINFO GROUPS, CLIENT SETNAME (best effort, so the
comparison is identifiable in CLIENT LIST) and memcached `stats`. No KEYS, no MONITOR,
no DEBUG, no CONFIG, no writes. Use a dedicated read-only ACL user where possible —
READ_ONLY_ACL below is the tested least-privilege rule set
("on ~* &* -@all +@read +info +ping +select +cluster|slots +cluster|info +client|setname
+client|setinfo -keys").

CREDENTIALS NEVER IN ARGV (hard constraint 4). Per side: "password_env" (env var name),
or the customer-managed "secrets_file" (JSON {"source": {"username":..,"password":..},
"target": {...}}, must be chmod 600 or stricter), or "password_secret_id" (Secrets
Manager, fetched on this host via boto3 or the aws CLI; "password_secret_key" selects the
JSON field). A "password"/"auth_token" key inside --config is refused.

Usage:
  python3 cache_compare.py --config cache-compare.json [--report report.json]
      [--sample-size 1000] [--seed S] [--full-scan] [--time-budget 600]
      [--dashboard dashboard --record validation|soak [--phase 7] [--step-id cache-compare]
       [--soak-n-total N] [--plan migration-plan.md --plan-section "Phase 7"]]

Config (JSON; every field optional unless noted):
  {"source": {"host": "10.0.1.20", "port": 6379, "engine": "redis",       (host required)
              "tls": false, "tls_ca": null, "tls_insecure": false, "tls_check_hostname": true,
              "username": null, "password_env": "SRC_CACHE_PASSWORD",
              "cluster": "auto", "address_map": {"10.0.1.21:6379": "127.0.0.1:16379"}},
   "target": {... same ...},
   "secrets_file": null, "dbs": null, "db_map": {"1": 1},
   "sample_size": 1000, "seed": "cache-compare", "full_scan": false,
   "ttl_tolerance_ms": 5000, "key_count_tolerance": 0, "key_count_tolerance_pct": 0.0,
   "max_elements": 100000, "chunk": 1000, "scan_count": 1000,
   "time_budget_seconds": 600, "socket_timeout_seconds": 10,
   "recheck_mismatches": true, "recheck_delay_seconds": 1.0,
   "expect_replication": false, "alarm_names": [], "region": null,
   "headroom_threshold_pct": 30, "customer_test_suite_provided": false,
   "memcached_min_items_ratio": null, "memcached_max_hit_ratio_drop": null,
   "memcached_min_gets": 1000, "redact_keys": false}

Exit status: 0 verdict true or "not_applicable"; 1 false; 3 null (needs review);
2 bad arguments/config; 4 dashboard publication failed (the comparison result is still
written to --report when given).

Requires redis-py (`pip install redis`) for Redis/Valkey mode only; memcached mode is
stdlib-only. Python 3.9+.
"""
import argparse
import datetime
import hashlib
import heapq
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path

TOOL_VERSION = 1
HERE = Path(__file__).resolve().parent
DASHBOARD_UPDATE = HERE / "dashboard_update.py"
STRING_CHUNK_BYTES = 1024 * 1024
KEY_DISPLAY_MAX = 200
CHECK_STATES = (True, False, None, "not_applicable")
FORBIDDEN_CONFIG_KEYS = {"password", "auth_token", "authtoken", "token", "secret"}
# Least-privilege ACL rules this script needs (tested against redis 7.2 in
# scripts/test_cache_compare.py): ACL SETUSER cache_ro on >… ~* &* <these rules>.
# On ElastiCache create it as an RBAC user with the same access string.
READ_ONLY_ACL = ("~*", "&*", "-@all", "+@read", "+info", "+ping", "+select", "+cluster|slots",
                 "+cluster|info", "+client|setname", "+client|setinfo", "-keys")

DEFAULTS = {
    "sample_size": 1000, "seed": "cache-compare", "full_scan": False,
    "ttl_tolerance_ms": 5000, "key_count_tolerance": 0, "key_count_tolerance_pct": 0.0,
    "max_elements": 100000, "chunk": 1000, "scan_count": 1000,
    "time_budget_seconds": 600, "socket_timeout_seconds": 10,
    "recheck_mismatches": True, "recheck_delay_seconds": 1.0,
    "expect_replication": False, "alarm_names": [], "region": None,
    "headroom_threshold_pct": 30, "customer_test_suite_provided": False,
    "memcached_min_items_ratio": None, "memcached_max_hit_ratio_drop": None,
    "memcached_min_gets": 1000, "redact_keys": False, "dbs": None, "db_map": {},
    "progress_interval_seconds": 240,
}


class ConfigError(Exception):
    pass


class BudgetExhausted(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def _crc16(data):
    """CRC16-XMODEM, the Redis Cluster key-slot hash (cluster-spec)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def key_slot(key):
    """Hash slot of a key, honouring {hash tags} exactly as the cluster spec defines."""
    if isinstance(key, str):
        key = key.encode()
    start = key.find(b"{")
    if start != -1:
        end = key.find(b"}", start + 1)
        if end != -1 and end != start + 1:
            key = key[start + 1:end]
    return _crc16(key) % 16384


def combine(values):
    """Four-state AND: any False -> False; else any None -> None; else True when at least
    one value was measured; "not_applicable" when nothing applied."""
    values = [v for v in values if v != "not_applicable"]
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True if values else "not_applicable"


def _b(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, float):
        return repr(value).encode()
    return str(value).encode()


def _lp(h, value):
    """Length-prefixed update so element boundaries can never be ambiguous."""
    data = _b(value)
    h.update(len(data).to_bytes(8, "big"))
    h.update(data)


def display_key(key, redact=False):
    raw = key if isinstance(key, bytes) else str(key).encode()
    if redact:
        return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    text = raw.decode("utf-8", "backslashreplace")
    return text if len(text) <= KEY_DISPLAY_MAX else text[:KEY_DISPLAY_MAX] + "…"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- config

def _reject_inline_secrets(obj, path="config"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_CONFIG_KEYS:
                raise ConfigError(f"{path}.{k}: credentials are never stored in the config file — use "
                                  "password_env, a customer-managed secrets_file, or password_secret_id")
            _reject_inline_secrets(v, f"{path}.{k}")


def load_config(path, overrides):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a JSON object")
    _reject_inline_secrets(cfg)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    for k, v in overrides.items():
        if v is not None:
            merged[k] = v
    for side in ("source", "target"):
        ep = merged.get(side)
        if not isinstance(ep, dict) or not ep.get("host"):
            raise ConfigError(f"{side}.host is required")
        ep.setdefault("port", 11211 if (ep.get("engine") or "").lower() == "memcached" else 6379)
    engines = {(merged[s].get("engine") or "redis").lower() for s in ("source", "target")}
    if "memcached" in engines and engines != {"memcached"}:
        raise ConfigError("memcached can only be compared with memcached (stats mode); "
                          "Redis/Valkey <-> Memcached has no meaningful comparison")
    if int(merged["sample_size"]) < 1 and not merged["full_scan"]:
        raise ConfigError("sample_size must be >= 1 (or use --full-scan)")
    return merged


def _secrets_file_entry(cfg, side):
    path = cfg.get("secrets_file")
    if not path:
        return {}
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        raise ConfigError(f"secrets_file {path}: {e}") from e
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"secrets_file {path} is group/world accessible (mode {oct(st.st_mode & 0o777)}); "
                          "chmod 600 it — the customer manages this file, the agent never generates it")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"secrets_file {path} is not readable JSON: {e}") from e
    entry = data.get(side) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def _secret_from_secrets_manager(secret_id, key, region):
    try:
        import boto3  # optional
        kwargs = {"region_name": region} if region else {}
        raw = boto3.client("secretsmanager", **kwargs).get_secret_value(SecretId=secret_id)["SecretString"]
    except ImportError:
        cmd = ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
               "--query", "SecretString", "--output", "text"] + (["--region", region] if region else [])
        try:
            raw = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            raise ConfigError(f"Secrets Manager fetch for {secret_id} failed: {e}") from e
    except Exception as e:  # botocore errors vary by version; surface the message, never the value
        raise ConfigError(f"Secrets Manager fetch for {secret_id} failed: {type(e).__name__}: {e}") from e
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw   # plain-string secret
    if not isinstance(obj, dict) or key not in obj:
        keys = sorted(obj) if isinstance(obj, dict) else []
        raise ConfigError(f"secret {secret_id} has no field {key!r} (fields: {keys}); set password_secret_key")
    return obj[key]


def resolve_credentials(cfg, side):
    """(username, password) for a side, from secrets_file > password_env > Secrets Manager."""
    ep = cfg[side]
    entry = _secrets_file_entry(cfg, side)
    username = entry.get("username") or ep.get("username")
    password = entry.get("password")
    if password is None and ep.get("password_env"):
        password = os.environ.get(ep["password_env"])
        if password is None:
            raise ConfigError(f"{side}.password_env={ep['password_env']} is not set in this environment")
    if password is None and ep.get("password_secret_id"):
        password = _secret_from_secrets_manager(ep["password_secret_id"], ep.get("password_secret_key", "password"),
                                                ep.get("region") or cfg.get("region"))
    return username, password


# --------------------------------------------------------------------------- redis side

def _redis_module():
    try:
        import redis  # noqa: F401
        return redis
    except ImportError as e:
        raise ConfigError("Redis/Valkey mode needs redis-py on this host: pip install redis") from e


def _is_redirect(exc):
    # redis-py raises MovedError/AskError with the prefix stripped from the message;
    # other clients (and the test fakes) surface a ResponseError("MOVED <slot> <host:port>").
    msg = str(exc)
    return type(exc).__name__ in ("MovedError", "AskError") or msg.startswith(("MOVED ", "ASK "))


class RedisSide:
    """One endpoint: a standalone node or every primary of a cluster. `factory(host, port,
    db)` returns a redis-py-compatible client; tests inject fakes."""

    def __init__(self, label, ep, factory, response_error):
        self.label = label
        self.ep = ep
        self.factory = factory
        self.ResponseError = response_error
        self._clients = {}
        self.address_map = {k: v for k, v in (ep.get("address_map") or {}).items()}
        self.cluster = False
        self.slots = None
        self.primaries = [(ep["host"], int(ep["port"]))]
        self.server = {}

    def _addr(self, host, port):
        mapped = self.address_map.get(f"{host}:{port}")
        if mapped:
            h, _, p = mapped.rpartition(":")
            return h, int(p)
        return host, int(port)

    def client(self, node, db=0):
        key = (node, db)
        if key not in self._clients:
            host, port = self._addr(*node)
            c = self.factory(host, port, db)
            # Raw XRANGE reply: redis-py's parser turns stream fields into a dict, which
            # collapses duplicate field names (XADD allows them) — digest the real pairs.
            if hasattr(c, "set_response_callback"):
                c.set_response_callback("XRANGE", lambda r, **kw: r)
            try:
                c.client_setname("cache_compare")
            except Exception:   # noqa: BLE001 — naming is best effort (may be restricted)
                pass
            self._clients[key] = c
        return self._clients[key]

    def close(self):
        for c in self._clients.values():
            try:
                c.close()
            except Exception:   # noqa: BLE001
                pass

    def discover(self):
        seed = (self.ep["host"], int(self.ep["port"]))
        c = self.client(seed, 0)
        self.server = c.info("server") or {}
        want = self.ep.get("cluster", "auto")
        enabled = False
        if want is True:
            enabled = True
        elif want in ("auto", None):
            try:
                enabled = int((c.info("cluster") or {}).get("cluster_enabled", 0)) == 1
            except self.ResponseError:
                enabled = False
        if enabled:
            self.cluster = True
            self.refresh_slots()
        return self

    def refresh_slots(self):
        seed = (self.ep["host"], int(self.ep["port"]))
        rows = self.client(seed, 0).execute_command("CLUSTER", "SLOTS")
        table = [None] * 16384
        primaries = []
        for row in rows:
            start, end, master = int(row[0]), int(row[1]), row[2]
            host = master[0].decode() if isinstance(master[0], bytes) else str(master[0])
            if not host:   # an empty host means "the node you asked"
                host = seed[0]
            node = (host, int(master[1]))
            if node not in primaries:
                primaries.append(node)
            for s in range(start, end + 1):
                table[s] = node
        if any(n is None for n in table):
            missing = sum(1 for n in table if n is None)
            raise RuntimeError(f"{self.label}: cluster slot map is incomplete ({missing} unassigned slots)")
        self.slots, self.primaries = table, primaries

    def describe(self):
        s = self.server
        return {"endpoint": f"{self.ep['host']}:{self.ep['port']}",
                "server_name": _str(s.get("server_name")) or "redis",
                "version": _str(s.get("valkey_version") or s.get("redis_version")),
                "redis_version": _str(s.get("redis_version")),
                "cluster": self.cluster, "primaries": len(self.primaries)}

    def keyspace(self):
        """{db: {"keys": n, "expires": n}} summed over every primary."""
        out = {}
        for node in self.primaries:
            info = self.client(node, 0).info("keyspace") or {}
            for name, val in info.items():
                if not str(name).startswith("db"):
                    continue
                db = int(str(name)[2:])
                if isinstance(val, str):   # tolerate unparsed "keys=1,expires=0,avg_ttl=0"
                    val = dict(kv.split("=") for kv in val.split(","))
                agg = out.setdefault(db, {"keys": 0, "expires": 0})
                agg["keys"] += int(val.get("keys", 0))
                agg["expires"] += int(val.get("expires", 0))
        return out

    def memory(self):
        nodes = []
        for node in self.primaries:
            m = self.client(node, 0).info("memory") or {}
            nodes.append({"node": f"{node[0]}:{node[1]}", "used_memory": _int(m.get("used_memory")),
                          "maxmemory": _int(m.get("maxmemory")),
                          "maxmemory_policy": _str(m.get("maxmemory_policy"))})
        return nodes

    def replication(self):
        nodes = []
        for node in self.primaries:
            r = self.client(node, 0).info("replication") or {}
            nodes.append({"node": f"{node[0]}:{node[1]}", "role": _str(r.get("role")),
                          "master_link_status": _str(r.get("master_link_status")),
                          "master_repl_offset": _int(r.get("master_repl_offset")),
                          "slave_repl_offset": _int(r.get("slave_repl_offset")),
                          "master_last_io_seconds_ago": _int(r.get("master_last_io_seconds_ago"))})
        return nodes

    def scan(self, db, count, deadline, clock=time.monotonic):
        """Yields every key name of a logical db (all primaries). Raises BudgetExhausted.
        A non-zero db on a cluster works where the engine has cluster-databases (Valkey
        9.0+); elsewhere SELECT is refused and the caller records the db as unknown."""
        for node in self.primaries:
            c = self.client(node, db)
            cursor = 0
            while True:
                if clock() > deadline:
                    raise BudgetExhausted()
                cursor, keys = c.scan(cursor=cursor, count=count)
                for k in keys:
                    yield k
                if int(cursor) == 0:
                    break

    def _route(self, key, db):
        if not self.cluster:
            return self.client(self.primaries[0], db)
        return self.client(self.slots[key_slot(key)], db)

    def call(self, key, db, fn):
        """Runs fn(client) on the node owning `key`; one slot-map refresh on MOVED/ASK."""
        try:
            return fn(self._route(key, db))
        except self.ResponseError as e:
            if not (self.cluster and _is_redirect(e)):
                raise
            self.refresh_slots()
            return fn(self._route(key, db))


def _str(v):
    if v is None:
        return None
    return v.decode() if isinstance(v, bytes) else str(v)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Budget:
    """Wall-clock budget shared by every read, including per-chunk collection reads and
    mismatch re-reads. `check()` raises BudgetExhausted once the deadline has passed."""

    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + float(seconds)

    def expired(self):
        return self.clock() > self.deadline

    def check(self):
        if self.expired():
            raise BudgetExhausted()


def _stream_entries(raw):
    """Normalise XRANGE output to [(id, [field, value, field, value, ...])] preserving the
    stored order and duplicate field names. The client is configured to return the raw
    reply (RedisSide.client); a dict (lossy — duplicates collapsed) is only accepted from
    test fakes that cannot express duplicates anyway."""
    out = []
    for entry in raw or []:
        eid, fields = entry[0], entry[1]
        if isinstance(fields, dict):
            flat = [x for kv in fields.items() for x in kv]
        else:
            flat = list(fields or [])
        out.append((eid, flat))
    return out


def read_key(side, key, db, max_elements, chunk, budget=None):
    """Type, PTTL and a value digest. Never returns the value itself. `partial` = the
    contents were NOT compared (over max_elements) — the caller treats that as unknown."""
    check = budget.check if budget else (lambda: None)
    check()
    t = _str(side.call(key, db, lambda c: c.type(key)))
    pttl = int(side.call(key, db, lambda c: c.pttl(key)))
    out = {"type": t, "pttl": pttl, "digest": None, "card": None, "partial": False, "meta": None}
    if t == "none":
        return out
    h = hashlib.sha256(t.encode())
    if t == "string":
        n = int(side.call(key, db, lambda c: c.strlen(key)))
        out["card"] = n
        if n <= STRING_CHUNK_BYTES:
            _lp(h, side.call(key, db, lambda c: c.get(key)))
        else:
            for off in range(0, n, STRING_CHUNK_BYTES):
                check()
                part = side.call(key, db, lambda c, o=off: c.getrange(key, o, o + STRING_CHUNK_BYTES - 1))
                h.update(_b(part))
    elif t == "list":
        n = int(side.call(key, db, lambda c: c.llen(key)))
        out["card"] = n
        if n > max_elements:
            out["partial"] = True
        else:
            for start in range(0, n, chunk):
                check()
                for item in side.call(key, db, lambda c, s=start: c.lrange(key, s, s + chunk - 1)):
                    _lp(h, item)
    elif t in ("hash", "set", "zset"):
        n = int(side.call(key, db, {"hash": lambda c: c.hlen(key), "set": lambda c: c.scard(key),
                                    "zset": lambda c: c.zcard(key)}[t]))
        out["card"] = n
        if n > max_elements:
            out["partial"] = True
        else:
            # *SCAN may repeat elements; collecting into a dict/set de-duplicates them.
            seen = {}
            cursor = 0
            while True:
                check()
                if t == "hash":
                    cursor, part = side.call(key, db, lambda c, cu=cursor: c.hscan(key, cu, count=chunk))
                    seen.update(part)
                elif t == "set":
                    cursor, part = side.call(key, db, lambda c, cu=cursor: c.sscan(key, cu, count=chunk))
                    seen.update({m: None for m in part})
                else:
                    cursor, part = side.call(key, db, lambda c, cu=cursor: c.zscan(key, cu, count=chunk))
                    seen.update({m: float(s) for m, s in part})
                if int(cursor) == 0:
                    break
            for member in sorted(seen, key=_b):
                _lp(h, member)
                if t != "set":
                    _lp(h, seen[member])
    elif t == "stream":
        n = int(side.call(key, db, lambda c: c.xlen(key)))
        out["card"] = n
        if n > max_elements:
            out["partial"] = True
        else:
            start = "-"
            while True:
                check()
                entries = _stream_entries(side.call(
                    key, db, lambda c, s=start: c.execute_command("XRANGE", key, s, "+", "COUNT", chunk)))
                for eid, flat in entries:
                    # Entry boundary + field count, then the stored (ordered, duplicate-
                    # preserving) field/value pairs.
                    _lp(h, eid)
                    _lp(h, len(flat))
                    for x in flat:
                        _lp(h, x)
                if len(entries) < chunk:
                    break
                ms, _, seq = _str(entries[-1][0]).partition("-")
                start = f"{ms}-{int(seq) + 1}"
        try:
            groups = side.call(key, db, lambda c: c.xinfo_groups(key))
            out["meta"] = sorted((_str(g.get("name")), _str(g.get("last-delivered-id"))) for g in groups)
        except side.ResponseError:
            out["meta"] = None
    else:
        out["unsupported"] = True   # module types (e.g. ReJSON-RL) — cannot exist on ElastiCache anyway
        return out
    out["digest"] = h.hexdigest()
    return out


def classify(src, tgt, ttl_tolerance_ms):
    """None when the two reads agree; "uncompared" when type/TTL/cardinality agree but the
    contents were not read (over max_elements); otherwise a mismatch kind."""
    if src["type"] == "none" and tgt["type"] == "none":
        return None
    if src["type"] == "none":
        return "extra_on_target"
    if tgt["type"] == "none":
        return "missing_on_target"
    if src["type"] != tgt["type"]:
        return "type"
    if src.get("unsupported") or tgt.get("unsupported"):
        return "unsupported_type"
    partial = src["partial"] or tgt["partial"]
    if partial:
        if src["card"] != tgt["card"]:
            return "value"
    elif src["digest"] != tgt["digest"]:
        return "value"
    if src.get("meta") != tgt.get("meta"):
        return "stream_groups"
    sp, tp = src["pttl"], tgt["pttl"]
    if (sp < 0) != (tp < 0):
        return "ttl"
    if sp >= 0 and abs(sp - tp) > ttl_tolerance_ms:
        return "ttl"
    return "uncompared" if partial else None


UNKNOWN_KINDS = ("unsupported_type", "uncompared")


class _BottomK:
    """The k keys with the smallest keyed-hash value — a deterministic sample for a given
    seed and keyspace, independent of SCAN order or which side is scanned."""

    def __init__(self, k, seed):
        self.k, self.seed, self.heap = k, hashlib.sha256(seed.encode()).digest()[:16], []
        self._seen = set()

    def rank(self, key):
        return int.from_bytes(hashlib.blake2b(_b(key), key=self.seed, digest_size=8).digest(), "big")

    def offer(self, key):
        if key in self._seen:   # SCAN may return a key twice
            return
        r = self.rank(key)
        if len(self.heap) < self.k:
            heapq.heappush(self.heap, (-r, key))
            self._seen.add(key)
        elif -self.heap[0][0] > r:
            _, old = heapq.heapreplace(self.heap, (-r, key))
            self._seen.discard(old)
            self._seen.add(key)

    def keys(self):
        return sorted((k for _, k in self.heap), key=self.rank)


def keyspace_result(ks):
    """Four-state per-db result published to the dashboard: False on any persistent
    mismatch or count mismatch, None when anything was not compared, True only when the
    counts match and every compared key matched."""
    return combine([ks["key_count_match"], ks["sample_match"]])


def compare_redis(cfg, source, target, progress=None, clock=time.monotonic, sleep=None):
    sleep = sleep or time.sleep
    started = clock()
    budget = Budget(cfg["time_budget_seconds"], clock)
    limitations, errors = [], []
    redact = bool(cfg.get("redact_keys"))
    tol_ms = int(cfg["ttl_tolerance_ms"])
    max_el, chunk, scan_count = int(cfg["max_elements"]), int(cfg["chunk"]), int(cfg["scan_count"])
    db_map = {int(k): int(v) for k, v in (cfg.get("db_map") or {}).items()}
    src_ks, tgt_ks = source.keyspace(), target.keyspace()
    wanted = cfg.get("dbs")
    dbs = sorted(set(src_ks) | {d for d in tgt_ks if d not in db_map.values()})
    if wanted is not None:
        dbs = [d for d in dbs if d in {int(x) for x in wanted}]

    keyspaces, mismatches, transient, unknown = [], [], [], []
    budget_hit = False
    last_progress = [clock()]

    def _progress(msg):
        if progress and clock() - last_progress[0] >= float(cfg["progress_interval_seconds"]):
            last_progress[0] = clock()
            progress(msg)

    def _record(db, key, kind, s, t, direction):
        return {"db": db, "key": display_key(key, redact), "_raw": key, "kind": kind, "direction": direction,
                "source": {"type": s["type"], "pttl": s["pttl"], "card": s["card"]},
                "target": {"type": t["type"], "pttl": t["pttl"], "card": t["card"]}}

    for db in dbs:
        tdb = db_map.get(db, db)
        s_keys = src_ks.get(db, {}).get("keys", 0)
        t_keys = tgt_ks.get(tdb, {}).get("keys", 0)
        tol = max(int(cfg["key_count_tolerance"]), int(round(float(cfg["key_count_tolerance_pct"]) / 100.0 * s_keys)))
        entry = {"db": db, "target_db": tdb, "source_keys": s_keys, "target_keys": t_keys,
                 "source_expires": src_ks.get(db, {}).get("expires", 0),
                 "target_expires": tgt_ks.get(tdb, {}).get("expires", 0),
                 "key_count_match": abs(s_keys - t_keys) <= tol, "sampled": 0, "mismatches": 0,
                 "uncompared": 0, "scan_complete": True}
        keyspaces.append(entry)
        if budget_hit:
            entry["scan_complete"] = False
            entry["note"] = "not compared — time budget exhausted earlier"
            continue
        if target.cluster and tdb != 0 and tdb not in tgt_ks and s_keys:
            limitations.append(f"db{db}: target is cluster-mode and reports no db{tdb} — only db0 exists unless "
                               "the engine has cluster-databases (Valkey 9.0+)")
        compared = 0

        def _compare(key, direction):
            nonlocal compared
            s = read_key(source, key, db, max_el, chunk, budget)
            t = read_key(target, key, tdb, max_el, chunk, budget)
            compared += 1
            kind = classify(s, t, tol_ms)
            if kind in UNKNOWN_KINDS:
                entry["uncompared"] += 1
                unknown.append(_record(db, key, kind, s, t, direction))
            elif kind:
                mismatches.append(_record(db, key, kind, s, t, direction))

        try:
            if cfg["full_scan"]:
                for key in source.scan(db, scan_count, budget.deadline, clock):
                    _compare(key, "source->target")
                    _progress(f"db{db}: {compared} keys compared (full scan)")
                for key in target.scan(tdb, scan_count, budget.deadline, clock):
                    budget.check()
                    if int(source.call(key, db, lambda c, k=key: c.exists(k))) == 0:
                        t = read_key(target, key, tdb, max_el, chunk, budget)
                        if t["type"] != "none":
                            s = {"type": "none", "pttl": -2, "card": None}
                            mismatches.append(_record(db, key, "extra_on_target", s, t, "target->source"))
            else:
                k = int(cfg["sample_size"])
                src_sample, tgt_sample = _BottomK(k, cfg["seed"]), _BottomK(k, cfg["seed"])
                n = 0
                for key in source.scan(db, scan_count, budget.deadline, clock):
                    src_sample.offer(key)
                    n += 1
                    if n % 10000 == 0:
                        _progress(f"db{db}: {n} source key names scanned for the sample")
                for key in target.scan(tdb, scan_count, budget.deadline, clock):
                    tgt_sample.offer(key)
                chosen = src_sample.keys()
                for key in chosen:
                    _compare(key, "source->target")
                    _progress(f"db{db}: {compared}/{len(chosen)} sampled keys compared")
                in_src = set(chosen)
                for key in tgt_sample.keys():
                    if key not in in_src:
                        _compare(key, "target->source")
        except BudgetExhausted:
            budget_hit = True
            entry["scan_complete"] = False
            limitations.append(f"db{db}: time budget ({cfg['time_budget_seconds']} s) exhausted — "
                               "comparison incomplete")
        except Exception as e:   # noqa: BLE001 — e.g. SELECT refused on a cluster node: unknown, not skipped
            entry["scan_complete"] = False
            errors.append(f"db{db}: {type(e).__name__}: {e}")
            limitations.append(f"db{db}: keys could not be compared ({type(e).__name__}) — result unknown")
        entry["sampled"] = compared

    # Re-read mismatches once: replication/expiry churn between the two reads is transient.
    rechecked = True
    if mismatches and cfg.get("recheck_mismatches", True):
        try:
            budget.check()
            sleep(float(cfg["recheck_delay_seconds"]))
        except BudgetExhausted:
            rechecked = False
        still = []
        for m in mismatches:
            if not rechecked or budget.expired():
                rechecked = False
                still.append(m)
                continue
            db, tdb = m["db"], db_map.get(m["db"], m["db"])
            try:
                s = read_key(source, m["_raw"], db, max_el, chunk, budget)
                t = read_key(target, m["_raw"], tdb, max_el, chunk, budget)
                kind = classify(s, t, tol_ms)
            except BudgetExhausted:
                rechecked, kind = False, m["kind"]
            except Exception as e:   # noqa: BLE001 — a failed re-read keeps the mismatch
                errors.append(f"recheck {m['key']}: {type(e).__name__}: {e}")
                kind = m["kind"]
            if kind is None:
                m["resolved_on_recheck"] = True
                transient.append(m)
            else:
                still.append(m)
        mismatches = still
        if not rechecked:
            limitations.append("time budget exhausted during mismatch re-reads — remaining mismatches not rechecked")
    for m in mismatches + transient + unknown:
        m.pop("_raw", None)

    # Final expiry check: work that finished after the deadline is not a clean success.
    if budget.expired() and not budget_hit:
        budget_hit = True
        limitations.append(f"time budget ({cfg['time_budget_seconds']} s) exceeded before the comparison finished")
        for ks in keyspaces:
            ks["scan_complete"] = False

    for ks in keyspaces:
        ks["mismatches"] = sum(1 for m in mismatches if m["db"] == ks["db"])
        if ks["mismatches"]:
            ks["sample_match"] = False
        elif not ks["scan_complete"] or ks["uncompared"]:
            ks["sample_match"] = None
        elif ks["source_keys"] == 0 and ks["target_keys"] == 0:
            ks["sample_match"] = "not_applicable"
        elif ks["sampled"] == 0:
            ks["sample_match"] = None
        else:
            ks["sample_match"] = True
        ks["result"] = keyspace_result(ks)

    total_src = sum(v["keys"] for v in src_ks.values())
    total_tgt = sum(v["keys"] for v in tgt_ks.values())
    key_count = combine([ks["key_count_match"] for ks in keyspaces]) if keyspaces else True
    n_unsupported = sum(1 for u in unknown if u["kind"] == "unsupported_type")
    n_uncompared = sum(1 for u in unknown if u["kind"] == "uncompared")
    if n_unsupported:
        limitations.append(f"{n_unsupported} sampled key(s) have a module/unsupported type — not comparable; "
                           "modules are not available on ElastiCache (blocker catalog)")
    if n_uncompared:
        limitations.append(f"{n_uncompared} key(s) exceed max_elements={max_el}: type, TTL and cardinality agree "
                           "but contents were NOT compared — unknown (raise max_elements to digest them)")
    if keyspaces:
        sample_match = combine([ks["sample_match"] for ks in keyspaces])
    else:
        sample_match = "not_applicable"
    if sample_match is True and errors:
        sample_match = None
    return {"keyspaces": keyspaces, "mismatches": mismatches, "transient_mismatches": transient,
            "uncompared_keys": unknown,
            "checks": {"key_count": key_count, "sample_match": sample_match},
            "complete": not budget_hit and all(ks["scan_complete"] for ks in keyspaces),
            "limitations": limitations, "errors": errors,
            "elapsed_seconds": round(clock() - started, 3),
            "totals": {"source_keys": total_src, "target_keys": total_tgt}}


def replication_check(cfg, source, target):
    if not cfg.get("expect_replication"):
        return "not_applicable", None
    nodes = target.replication()
    replicas = [n for n in nodes if n["role"] in ("slave", "replica")]
    if not replicas:
        return False, {"target": nodes, "note": "expect_replication set but no target primary is a replica"}
    ok = all(n["master_link_status"] == "up" for n in replicas)
    detail = {"target": nodes}
    try:
        src = source.replication()
        detail["source"] = src
        if not source.cluster and not target.cluster and len(replicas) == 1 and src[0]["master_repl_offset"] is not None \
                and replicas[0]["slave_repl_offset"] is not None:
            detail["offset_lag_bytes"] = src[0]["master_repl_offset"] - replicas[0]["slave_repl_offset"]
    except Exception as e:   # noqa: BLE001 — source INFO may be restricted; link status still decides
        detail["source_error"] = f"{type(e).__name__}: {e}"
    return ok, detail


def headroom_check(cfg, target):
    nodes = target.memory()
    pcts = [100.0 * (1 - n["used_memory"] / n["maxmemory"]) for n in nodes
            if n["maxmemory"] and n["used_memory"] is not None]
    if not pcts:
        return "not_applicable", None, nodes
    worst = round(min(pcts), 1)
    return worst > float(cfg["headroom_threshold_pct"]), worst, nodes


def alarms_check(cfg):
    names = cfg.get("alarm_names") or []
    if not names:
        return "not_applicable", {}
    region = cfg.get("region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return None, {"error": "alarm_names set but no region (config region / AWS_REGION)"}
    cmd = ["aws", "cloudwatch", "describe-alarms", "--alarm-names", *names, "--region", region,
           "--query", "MetricAlarms[].[AlarmName,StateValue]", "--output", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
        states = {n: s for n, s in json.loads(out or "[]")}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, {"error": f"cloudwatch:DescribeAlarms failed: {type(e).__name__}"}
    firing = [n for n, s in states.items() if s == "ALARM"]
    unknown = [n for n in names if states.get(n) in (None, "INSUFFICIENT_DATA")]
    detail = {"firing_alarms": firing, "unknown_alarms": unknown}
    return (False if firing else None if unknown else True), detail


# --------------------------------------------------------------------------- memcached

def memcached_stats(ep, timeout, password=None, username=None, command=b"stats"):
    """`stats` over the text protocol (stdlib). TLS when ep.tls (ElastiCache Memcached
    1.6.12+ in-transit encryption; mandatory on Serverless)."""
    raw = socket.create_connection((ep["host"], int(ep["port"])), timeout=timeout)
    try:
        if ep.get("tls"):
            ctx = ssl.create_default_context(cafile=ep.get("tls_ca") or None)
            if ep.get("tls_insecure"):
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=ep["host"])
        raw.sendall(command + b"\r\n")
        buf = b""
        while not (buf.endswith(b"END\r\n") or b"ERROR" in buf):
            part = raw.recv(65536)
            if not part:
                break
            buf += part
    finally:
        raw.close()
    out = {}
    for line in buf.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "STAT":
            out[parts[1]] = parts[2]
    if not out:
        raise RuntimeError(f"no stats returned by {ep['host']}:{ep['port']}: {buf[:80]!r}")
    return out


def _hit_ratio(st):
    hits, misses = _int(st.get("get_hits")) or 0, _int(st.get("get_misses")) or 0
    return (hits / (hits + misses), hits + misses) if hits + misses else (None, 0)


def compare_memcached(cfg, src_stats, tgt_stats):
    fields = ("version", "curr_items", "total_items", "bytes", "limit_maxbytes", "evictions",
              "curr_connections", "get_hits", "get_misses", "uptime")
    s_hr, s_gets = _hit_ratio(src_stats)
    t_hr, t_gets = _hit_ratio(tgt_stats)
    report = {"source": {f: src_stats.get(f) for f in fields}, "target": {f: tgt_stats.get(f) for f in fields},
              "hit_ratio": {"source": s_hr, "target": t_hr, "source_gets": s_gets, "target_gets": t_gets}}
    checks = {"data_match": "not_applicable"}
    ratio = cfg.get("memcached_min_items_ratio")
    if ratio is not None:
        s_items, t_items = _int(src_stats.get("curr_items")) or 0, _int(tgt_stats.get("curr_items")) or 0
        checks["warm_items"] = True if s_items == 0 else (t_items >= float(ratio) * s_items)
    drop = cfg.get("memcached_max_hit_ratio_drop")
    if drop is not None:
        min_gets = int(cfg.get("memcached_min_gets") or 0)
        if s_hr is None or t_hr is None or t_gets < min_gets or s_gets < min_gets:
            checks["warm_hit_ratio"] = None   # not enough traffic on one side yet
        else:
            checks["warm_hit_ratio"] = t_hr >= s_hr - float(drop)
    report["checks"] = checks
    report["limitations"] = ["Memcached cannot enumerate keys: this is a stats comparison (warmth/capacity), "
                             "not a data comparison. A cache has no data to migrate — it is re-warmed."]
    return report


# --------------------------------------------------------------------------- dashboard

def _dashboard_update(args, patch, title, result, detail):
    cmd = [sys.executable, "-B", str(DASHBOARD_UPDATE), "--dashboard", args.dashboard, "--patch-file", "-",
           "--log-title", title, "--log-action", "cache_compare.py", "--log-result", result,
           "--log-detail", detail[:500]]
    if args.plan_section:
        cmd += ["--plan", args.plan, "--plan-section", args.plan_section, "--plan-line", f"{title}: {detail[:300]}"]
    try:
        proc = subprocess.run(cmd, input=json.dumps(patch), capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError("dashboard_update.py did not finish within 15 s")
    if proc.returncode != 0:
        raise RuntimeError(f"dashboard_update.py failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _verdict_word(v):
    return {True: "match", False: "MISMATCH", None: "needs review", "not_applicable": "not applicable"}[v]


def validation_patch(args, report):
    v = report["verdict"]
    status = "done" if v in (True, "not_applicable") else "blocked"
    detail = _summary(report)
    patch = {"current_activity": f"cache_compare: {_verdict_word(v)}",
             "phases": [{"id": args.phase, "status": "in_progress",
                         "steps": [{"id": args.step_id, "status": status, "detail": detail}]}]}
    if report["mode"] == "redis" and report.get("keyspaces"):
        items = []
        for ks in report["keyspaces"]:
            res = ks.get("result")
            # Merge-patch drops nulls, so the four-state result is also published as a word.
            items.append({"name": f"db{ks['db']}", "keys_source": ks["source_keys"], "keys_target": ks["target_keys"],
                          "sampled": ks["sampled"], "mismatches": ks["mismatches"],
                          "key_count_match": ks["key_count_match"], "sample_match": ks["sample_match"],
                          "comparison": {True: "match", False: "mismatch", None: "unknown",
                                         "not_applicable": "not_applicable"}[res],
                          "status": "validated" if res is True else "loaded"})
        patch["migration_objects"] = {"keyspaces": {"total": len(items),
                                                    "validated": sum(1 for i in items if i["status"] == "validated"),
                                                    "items": items}}
    return patch, status, detail


def soak_day(cfg, report, run_date):
    checks = dict(report["checks"])
    checks["customer_test_suite"] = None if cfg.get("customer_test_suite_provided") else "not_applicable"
    checks["period_evidence"] = None   # full-period CloudWatch/log review is manual, as for the soak scripts
    measured = [v for v in checks.values() if v != "not_applicable"]
    green = bool(measured) and all(v is True for v in measured) and not report.get("errors")
    detail = {"sampler": "cache_compare", "keyspaces": report.get("keyspaces"),
              "persistent_mismatches": len(report.get("mismatches") or []),
              "transient_mismatches": len(report.get("transient_mismatches") or []),
              "uncompared_keys": len(report.get("uncompared_keys") or []),
              "headroom_pct": report.get("headroom_pct"), "limitations": report.get("limitations"),
              "errors": report.get("errors") or []}
    if report.get("replication") is not None:
        detail["replication"] = report["replication"]
    return {"date": run_date, "checks": checks, "detail": detail,
            "overall": "green" if green else "red",
            "needs_agent_review": (not green) or any(v is None for v in checks.values())}


def publish_soak_day(args, cfg, report, run_date):
    """The day is replaced verbatim and the streak/state recomputed by dashboard_update.py
    INSIDE its lock (--soak-day-file), from the current status — never from a stale read
    here, and without merge-patch deleting literal nulls."""
    day = soak_day(cfg, report, run_date)
    seed = ["--seed-soak", str(args.soak_n_total)] if args.soak_n_total else []
    cmd = [sys.executable, "-B", str(DASHBOARD_UPDATE), "--dashboard", args.dashboard, "--patch", "{}",
           *seed, "--soak-day-file", "-",
           "--log-title", f"Soak day {run_date} — {day['overall']} (cache_compare)", "--log-action", "cache_compare.py",
           "--log-result", "success" if day["overall"] == "green" else "blocked",
           "--log-detail", "needs_agent_review" if day["needs_agent_review"] else "all checks green"]
    if args.plan_section:
        cmd += ["--plan", args.plan, "--plan-section", args.plan_section,
                "--plan-line", f"Soak day {run_date}: {day['overall']} — {_summary(report)[:250]}"]
    try:
        proc = subprocess.run(cmd, input=json.dumps(day, default=str), capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError("dashboard_update.py did not finish within 15 s")
    if proc.returncode != 0:
        raise RuntimeError(f"dashboard_update.py failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return day


def _summary(report):
    if report["mode"] == "memcached":
        s, t = report.get("source") or {}, report.get("target") or {}
        return (f"memcached stats (not a data comparison): curr_items {s.get('curr_items')} -> "
                f"{t.get('curr_items')}; verdict {_verdict_word(report['verdict'])}")
    tot = report.get("totals", {})
    return (f"{_verdict_word(report['verdict'])}: keys {tot.get('source_keys')} -> {tot.get('target_keys')}; "
            f"{sum(k['sampled'] for k in report.get('keyspaces', []))} keys compared, "
            f"{len(report.get('mismatches') or [])} persistent / {len(report.get('transient_mismatches') or [])} "
            f"transient mismatches; checks {json.dumps(report['checks'])}")


# --------------------------------------------------------------------------- main

def _redis_factory(cfg, side, username, password):
    redis = _redis_module()
    ep = cfg[side]
    timeout = float(cfg["socket_timeout_seconds"])

    def make(host, port, db):
        kwargs = dict(host=host, port=port, db=db, username=username, password=password,
                      socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=False)
        if ep.get("tls"):
            kwargs.update(ssl=True, ssl_ca_certs=ep.get("tls_ca") or None,
                          ssl_cert_reqs="none" if ep.get("tls_insecure") else "required",
                          ssl_check_hostname=bool(ep.get("tls_check_hostname", True)) and not ep.get("tls_insecure"))
        return redis.Redis(**kwargs)
    return make, redis.exceptions.ResponseError


def _guard(report, check, fn):
    """Runs one check; any failure records the error and leaves the check unknown (null) —
    an error after sampling can never leave a passing verdict behind."""
    try:
        return fn()
    except ConfigError:
        raise
    except Exception as e:   # noqa: BLE001
        report["checks"][check] = None
        report["errors"].append(f"{check}: {type(e).__name__}: {e}")
        return None


def run(cfg, args, factories=None):
    """Returns the report dict. `factories` = {"source": (factory, ResponseError), ...}
    lets tests inject fakes. Every configured check starts as unknown (null) and only a
    successful measurement replaces it; any recorded error caps the verdict at null."""
    started_at = _now_iso()
    mode = "memcached" if (cfg["source"].get("engine") or "").lower() == "memcached" else "redis"
    report = {"tool": "cache_compare", "tool_version": TOOL_VERSION, "generated_at": started_at, "mode": mode,
              "settings": {k: cfg[k] for k in ("sample_size", "seed", "full_scan", "ttl_tolerance_ms",
                                               "key_count_tolerance", "key_count_tolerance_pct", "max_elements",
                                               "time_budget_seconds", "expect_replication")},
              "errors": [], "limitations": []}
    alarms_configured = bool(cfg.get("alarm_names"))
    if mode == "memcached":
        report["checks"] = {"data_match": "not_applicable", "reachable": None,
                            "alarms": None if alarms_configured else "not_applicable"}
        timeout = float(cfg["socket_timeout_seconds"])
        stats = _guard(report, "reachable", lambda: (memcached_stats(cfg["source"], timeout),
                                                     memcached_stats(cfg["target"], timeout)))
        if stats:
            # Reachability is a precondition, not evidence: a stats-only run with no warmth
            # thresholds stays "not_applicable" (memcached has no data comparison).
            report["checks"].pop("reachable")
            report["reachable"] = True
            mc = compare_memcached(cfg, *stats)
            report["checks"].update(mc.pop("checks"))
            report.update(mc)
        else:
            report.setdefault("source", {})
            report.setdefault("target", {})
    else:
        report["checks"] = {"key_count": None, "sample_match": None,
                            "replication_link": None if cfg.get("expect_replication") else "not_applicable",
                            "headroom": None, "alarms": None if alarms_configured else "not_applicable"}
        sides = {}
        try:
            for side in ("source", "target"):
                if factories:
                    factory, rerr = factories[side]
                else:
                    username, password = resolve_credentials(cfg, side)
                    factory, rerr = _redis_factory(cfg, side, username, password)
                sides[side] = RedisSide(side, cfg[side], factory, rerr)
                if not _guard(report, "key_count", lambda s=sides[side]: s.discover()):
                    report["checks"]["sample_match"] = None
                    break
            else:
                report["source"], report["target"] = sides["source"].describe(), sides["target"].describe()
                progress = None
                if getattr(args, "dashboard", None) and getattr(args, "record", None) == "validation":
                    def progress(msg):
                        _dashboard_update(args, {"current_activity": f"cache_compare: {msg}", "phases": [
                            {"id": args.phase, "status": "in_progress",
                             "steps": [{"id": args.step_id, "status": "in_progress",
                                        "detail": f"{msg} (checked {_now_iso()})"}]}]},
                            "cache_compare progress", "in_progress", msg)
                result = _guard(report, "sample_match",
                                lambda: compare_redis(cfg, sides["source"], sides["target"], progress=progress))
                if result:
                    report["checks"].update(result.pop("checks"))
                    report["errors"].extend(result.pop("errors"))
                    report["limitations"].extend(result.pop("limitations"))
                    report.update(result)
                else:
                    report["checks"]["key_count"] = None
                repl = _guard(report, "replication_link", lambda: replication_check(cfg, sides["source"], sides["target"]))
                if repl:
                    report["checks"]["replication_link"], report["replication"] = repl
                head = _guard(report, "headroom", lambda: headroom_check(cfg, sides["target"]))
                if head:
                    report["checks"]["headroom"], report["headroom_pct"], mem_target = head
                    mem_source = _guard(report, "memory_info", lambda: sides["source"].memory())
                    report["checks"].pop("memory_info", None)   # informational: error recorded, no check
                    report["memory"] = {"source": mem_source, "target": mem_target,
                                        "note": "informational — encodings differ across engines/versions, "
                                                "never a pass/fail"}
        finally:
            for s in sides.values():
                s.close()
    if alarms_configured:
        alarms = _guard(report, "alarms", lambda: alarms_check(cfg))
        if alarms:
            report["checks"]["alarms"], alarm_detail = alarms
            if alarm_detail:
                report["alarms"] = alarm_detail
    verdict = combine(report["checks"].values())
    if report["errors"] and verdict in (True, "not_applicable"):
        verdict = None
    report["verdict"] = verdict
    return report


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Read-only Redis/Valkey (or Memcached stats) source/target comparison.")
    ap.add_argument("--config", required=True, help="JSON config (no credentials inside — see module docstring)")
    ap.add_argument("--report", help="write the JSON report here (mode 600); default: stdout")
    ap.add_argument("--sample-size", type=int)
    ap.add_argument("--seed")
    ap.add_argument("--full-scan", action="store_true", default=None, help="compare every key (small datasets)")
    ap.add_argument("--time-budget", type=float, dest="time_budget_seconds")
    ap.add_argument("--redact-keys", action="store_true", default=None, help="hash key names in the report")
    ap.add_argument("--dashboard", help="dashboard directory to publish to via dashboard_update.py")
    ap.add_argument("--record", choices=("validation", "soak"), help="what the dashboard entry is")
    ap.add_argument("--phase", default=None, help="phase id for --record validation (default 7)")
    ap.add_argument("--step-id", default="cache-compare")
    ap.add_argument("--soak-n-total", type=int)
    ap.add_argument("--run-date", help="UTC date for --record soak (default: today, pinned at start)")
    ap.add_argument("--plan", default="migration-plan.md")
    ap.add_argument("--plan-section")
    args = ap.parse_args(argv)
    if bool(args.dashboard) != bool(args.record):
        ap.error("--dashboard and --record go together")
    args.phase = args.phase or "7"
    return args


def main(argv=None):
    args = parse_args(argv)
    run_date = args.run_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        cfg = load_config(args.config, {"sample_size": args.sample_size, "seed": args.seed,
                                        "full_scan": args.full_scan, "time_budget_seconds": args.time_budget_seconds,
                                        "redact_keys": args.redact_keys})
        if args.dashboard and args.record == "validation":
            _dashboard_update(args, {"current_activity": "cache_compare: starting",
                                     "phases": [{"id": args.phase, "status": "in_progress", "steps": [
                                         {"id": args.step_id, "status": "in_progress",
                                          "detail": f"comparing {cfg['source']['host']} -> {cfg['target']['host']} "
                                                    f"(started {_now_iso()})"}]}]},
                              "cache_compare start", "in_progress", "comparison started")
        report = run(cfg, args)
    except ConfigError as e:
        print(f"cache_compare: config error — {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"cache_compare: {e}", file=sys.stderr)
        return 4
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.report:
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    print(f"cache_compare: verdict={report['verdict']!r} — {_summary(report)}", file=sys.stderr)
    if args.dashboard:
        try:
            if args.record == "validation":
                patch, status, detail = validation_patch(args, report)
                _dashboard_update(args, patch, f"cache_compare {args.step_id}",
                                  "success" if status == "done" else "blocked", detail)
            else:
                publish_soak_day(args, cfg, report, run_date)
        except RuntimeError as e:
            print(f"cache_compare: dashboard publication FAILED — {e}", file=sys.stderr)
            return 4
    v = report["verdict"]
    return 0 if v in (True, "not_applicable") else 1 if v is False else 3


if __name__ == "__main__":
    sys.exit(main())
