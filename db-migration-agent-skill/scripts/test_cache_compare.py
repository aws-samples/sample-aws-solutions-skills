#!/usr/bin/env python3
"""Tests for shared/scripts/cache_compare.py.

Unit tests use in-memory fakes (no network, no redis-py needed). Integration tests run
real engines in Docker — redis:7.2, valkey/valkey:8.1, memcached:1.6 — and are SKIPPED
(never failed) when Docker, the images, or redis-py are unavailable. Containers and the
Docker network are always removed. Run: python3 -B scripts/test_cache_compare.py
"""
import contextlib
import io
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
import cache_compare as cc  # noqa: E402
import dashboard_update as du  # noqa: E402

FAKE_CLOCK = [0.0]   # advanced by FakeNode.delay; used as the comparison clock in budget tests
FIXTURE = ROOT / "scripts" / "fixtures" / "engagements" / "synthetic" / "dashboard"


# ----------------------------------------------------------------------------- fakes

class FakeResponseError(Exception):
    pass


class FakeNode:
    """One fake server. `data[db][key] = (type, value, pttl)`; `slots` = owned slot range
    (None = standalone)."""

    def __init__(self, host, port, data=None, slots=None, cluster=None, maxmemory=0, used=1000):
        self.host, self.port = host, port
        self.multi_db = slots is None          # cluster nodes refuse SELECT unless multi_db (Valkey 9)
        self.delay = 0                         # simulated seconds per read (budget tests)
        self.memory_denied = False
        self.data = data if data is not None else {0: {}}
        self.slots, self.cluster = slots, cluster
        self.maxmemory, self.used = maxmemory, used
        self.replication = {"role": "master"}
        self.reads = 0
        self.gained, self.lost = set(), set()   # slots moved by a simulated reshard

    def owns_slot(self, slot):
        return slot in self.gained or (slot not in self.lost and self.slots[0] <= slot <= self.slots[1])

    def owns(self, key):
        return self.slots is None or self.owns_slot(cc.key_slot(key))


class FakeClient:
    def __init__(self, node, db):
        self.node, self.db = node, db

    # --- helpers
    def _select_ok(self):
        if self.db != 0 and not self.node.multi_db:
            raise FakeResponseError("ERR SELECT is not allowed in cluster mode")

    def _entry(self, key):
        self._select_ok()
        if self.node.delay:
            FAKE_CLOCK[0] += self.node.delay
        key = key if isinstance(key, bytes) else str(key).encode()
        if not self.node.owns(key):
            owner = next(n for n in self.node.cluster if n.owns(key))
            raise FakeResponseError(f"MOVED {cc.key_slot(key)} {owner.host}:{owner.port}")
        self.node.reads += 1
        return self.node.data.get(self.db, {}).get(key)

    def close(self):
        pass

    def client_setname(self, name):
        return True

    def info(self, section):
        if section == "server":
            return {"redis_version": "7.2.4", "server_name": "valkey", "valkey_version": "8.1.0"}
        if section == "cluster":
            return {"cluster_enabled": 1 if self.node.slots is not None else 0}
        if section == "keyspace":
            out = {}
            for db, keys in self.node.data.items():
                if keys:
                    out[f"db{db}"] = {"keys": len(keys), "expires": sum(1 for v in keys.values() if v[2] >= 0),
                                      "avg_ttl": 0}
            return out
        if section == "memory":
            if self.node.memory_denied:
                raise FakeResponseError("NOPERM this user has no permissions to run the 'info' command")
            return {"used_memory": self.node.used, "maxmemory": self.node.maxmemory, "maxmemory_policy": "noeviction"}
        if section == "replication":
            return dict(self.node.replication)
        return {}

    def execute_command(self, *args):
        if args[0] == "XRANGE":   # raw reply: [[id, [f, v, f, v ...]]] — duplicates preserved
            _, key, start, _end, _, count = args
            return [[eid, [x for kv in fields.items() for x in kv] if isinstance(fields, dict) else list(fields)]
                    for eid, fields in self.xrange(key, min=start, count=count)]
        assert args == ("CLUSTER", "SLOTS"), args
        rows = []
        for n in self.node.cluster:
            start = None
            for slot in range(16385):
                owned = slot < 16384 and n.owns_slot(slot)
                if owned and start is None:
                    start = slot
                elif not owned and start is not None:
                    rows.append([start, slot - 1, [n.host.encode(), n.port, b"id"]])
                    start = None
        return rows

    def scan(self, cursor=0, count=10):
        self._select_ok()
        keys = sorted(self.node.data.get(self.db, {}))
        nxt = cursor + count
        return (0 if nxt >= len(keys) else nxt), keys[cursor:nxt]

    def exists(self, key):
        return 1 if self._entry(key) else 0

    def type(self, key):
        e = self._entry(key)
        return (e[0] if e else "none").encode()

    def pttl(self, key):
        e = self._entry(key)
        return -2 if not e else e[2]

    def strlen(self, key):
        return len(self._entry(key)[1])

    def get(self, key):
        return self._entry(key)[1]

    def getrange(self, key, a, b):
        return self._entry(key)[1][a:b + 1]

    def llen(self, key):
        return len(self._entry(key)[1])

    def lrange(self, key, a, b):
        return self._entry(key)[1][a:b + 1]

    def hlen(self, key):
        return len(self._entry(key)[1])

    def hscan(self, key, cursor, count=10):
        items = sorted(self._entry(key)[1].items())
        part = items[cursor:cursor + count]
        nxt = cursor + count
        return (0 if nxt >= len(items) else nxt), dict(part)

    def scard(self, key):
        return len(self._entry(key)[1])

    def sscan(self, key, cursor, count=10):
        items = sorted(self._entry(key)[1])
        nxt = cursor + count
        return (0 if nxt >= len(items) else nxt), items[cursor:nxt]

    def zcard(self, key):
        return len(self._entry(key)[1])

    def zscan(self, key, cursor, count=10):
        items = sorted(self._entry(key)[1].items())
        nxt = cursor + count
        return (0 if nxt >= len(items) else nxt), items[cursor:nxt]

    def xlen(self, key):
        return len(self._entry(key)[1]["entries"])

    def xrange(self, key, min="-", max="+", count=None):
        def _id(s):
            ms, _, seq = (s.decode() if isinstance(s, bytes) else s).partition("-")
            return int(ms), int(seq or 0)
        entries = self._entry(key)[1]["entries"]
        if min != "-":
            entries = [e for e in entries if _id(e[0]) >= _id(min)]
        return entries[:count] if count else entries

    def xinfo_groups(self, key):
        return [dict(g) for g in self._entry(key)[1]["groups"]]


def make_dataset():
    big_hash = {f"f{i}".encode(): f"v{i}".encode() for i in range(300)}
    return {
        0: {
            b"s:1": ("string", b"hello", -1),
            b"s:ttl": ("string", b"expiring", 600000),
            b"s:big": ("string", b"x" * (cc.STRING_CHUNK_BYTES + 17), -1),
            b"l:1": ("list", [b"a", b"b", b"c"], -1),
            b"h:1": ("hash", {b"name": b"kim", b"age": b"30"}, 120000),
            b"h:big": ("hash", big_hash, -1),
            b"set:1": ("set", {b"x", b"y", b"z"}, -1),
            b"z:1": ("zset", {b"alice": 1.5, b"bob": 2.0}, -1),
            b"x:1": ("stream", {"entries": [(b"1-0", {b"k": b"v"}), (b"2-0", {b"k": b"w"})],
                                "groups": [{"name": b"g1", "last-delivered-id": b"1-0"}]}, -1),
            **{f"k:{i}".encode(): ("string", str(i).encode(), -1) for i in range(40)},
        },
        3: {b"db3:only": ("string", b"three", -1)},
    }


def clone(data):
    import copy
    return copy.deepcopy(data)


def standalone(host, data, **kw):
    node = FakeNode(host, 6379, data, **kw)
    return node, (lambda h, p, db: FakeClient(node, db)), FakeResponseError


def fake_cluster(data, n=3, host_prefix="10.0.0.", multi_db=False):
    """Splits `data` across n fake primaries by real hash slot (db0 only unless multi_db,
    which models Valkey 9 cluster-databases)."""
    bounds = [(i * 16384 // n, (i + 1) * 16384 // n - 1) for i in range(n)]
    nodes = [FakeNode(f"{host_prefix}{i + 1}", 6379, {0: {}}, slots=b) for i, b in enumerate(bounds)]
    for n_ in nodes:
        n_.cluster = nodes
        n_.multi_db = multi_db
    for db, keys in data.items():
        if db != 0 and not multi_db:
            continue
        for key, val in keys.items():
            next(x for x in nodes if x.owns(key)).data.setdefault(db, {})[key] = val
    registry = {(x.host, x.port): x for x in nodes}
    return nodes, (lambda h, p, db: FakeClient(registry[(h, p)], db)), FakeResponseError


def base_cfg(**over):
    cfg = dict(cc.DEFAULTS)
    cfg.update({"source": {"host": "src", "port": 6379}, "target": {"host": "tgt", "port": 6379},
                "recheck_delay_seconds": 0, "sample_size": 1000})
    cfg.update(over)
    return cfg


def run_fake(cfg, src_factory, tgt_factory):
    return cc.run(cfg, mock.Mock(dashboard=None, record=None), factories={"source": src_factory, "target": tgt_factory})


# ----------------------------------------------------------------------------- unit tests

class PrimitivesTest(unittest.TestCase):
    def test_crc16_and_slots_match_the_cluster_spec(self):
        self.assertEqual(cc._crc16(b"123456789"), 0x31C3)        # cluster-spec reference value
        self.assertEqual(cc.key_slot("foo"), 12182)
        self.assertEqual(cc.key_slot("{user1000}.following"), cc.key_slot("{user1000}.followers"))
        self.assertEqual(cc.key_slot("foo{}{bar}"), cc._crc16(b"foo{}{bar}") % 16384)   # empty tag ignored
        self.assertEqual(cc.key_slot("foo{{bar}}zap"), cc._crc16(b"{bar") % 16384)

    def test_four_state_combine(self):
        self.assertIs(cc.combine([True, "not_applicable"]), True)
        self.assertIs(cc.combine([True, None]), None)
        self.assertIs(cc.combine([None, False]), False)
        self.assertEqual(cc.combine(["not_applicable"]), "not_applicable")

    def test_bottom_k_is_deterministic_and_seeded(self):
        keys = [f"key:{i}".encode() for i in range(500)]
        a, b, c = cc._BottomK(25, "seed-1"), cc._BottomK(25, "seed-1"), cc._BottomK(25, "seed-2")
        for k in keys:
            a.offer(k)
            c.offer(k)
        for k in reversed(keys + keys[:50]):     # different order + duplicates (SCAN may repeat)
            b.offer(k)
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual(len(a.keys()), 25)
        self.assertNotEqual(set(a.keys()), set(c.keys()))


class ConfigAndCredentialsTest(unittest.TestCase):
    def _write(self, d, obj, name="cfg.json"):
        p = Path(d) / name
        p.write_text(json.dumps(obj))
        return str(p)

    def test_inline_password_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, {"source": {"host": "a", "password": "x"}, "target": {"host": "b"}})
            with self.assertRaisesRegex(cc.ConfigError, "never stored in the config"):
                cc.load_config(path, {})

    def test_no_credential_flag_exists_on_the_command_line(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cc.parse_args(["--help"])
        self.assertNotRegex(out.getvalue().lower(), r"--\S*(pass|token|secret)")

    def test_password_env_and_secrets_file_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            sec = Path(d) / "secrets.json"
            sec.write_text(json.dumps({"target": {"username": "ro", "password": "from-file"}}))
            os.chmod(sec, 0o644)
            cfg = base_cfg(secrets_file=str(sec))
            cfg["source"]["password_env"] = "CC_TEST_PW"
            with mock.patch.dict(os.environ, {"CC_TEST_PW": "from-env"}):
                with self.assertRaisesRegex(cc.ConfigError, "group/world accessible"):
                    cc.resolve_credentials(cfg, "target")
                os.chmod(sec, 0o600)
                self.assertEqual(cc.resolve_credentials(cfg, "source"), (None, "from-env"))
                self.assertEqual(cc.resolve_credentials(cfg, "target"), ("ro", "from-file"))
            with self.assertRaisesRegex(cc.ConfigError, "not set"):
                cc.resolve_credentials(cfg, "source")

    def test_memcached_cannot_be_compared_with_redis(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, {"source": {"host": "a", "engine": "memcached"}, "target": {"host": "b"}})
            with self.assertRaisesRegex(cc.ConfigError, "memcached can only"):
                cc.load_config(path, {})


class CompareFakeTest(unittest.TestCase):
    def test_identical_standalone_datasets_match(self):
        _, sf, se = standalone("src", make_dataset())
        _, tf, te = standalone("tgt", clone(make_dataset()))
        report = run_fake(base_cfg(), (sf, se), (tf, te))
        self.assertIs(report["verdict"], True, report)
        self.assertEqual(report["checks"]["key_count"], True)
        self.assertEqual(report["checks"]["sample_match"], True)
        self.assertEqual(report["checks"]["replication_link"], "not_applicable")
        self.assertEqual([k["db"] for k in report["keyspaces"]], [0, 3])
        self.assertEqual(sum(k["sampled"] for k in report["keyspaces"]), len(make_dataset()[0]) + 1)
        self.assertEqual(report["mismatches"], [])

    def test_exact_mismatches_are_reported(self):
        src = make_dataset()
        tgt = clone(src)
        tgt[0][b"s:1"] = ("string", b"HELLO", -1)                       # value
        tgt[0][b"s:ttl"] = ("string", b"expiring", 10_000)              # TTL far outside tolerance
        tgt[0][b"h:1"] = ("hash", {b"name": b"kim", b"age": b"30"}, -1)  # TTL dropped
        del tgt[0][b"l:1"]                                              # missing
        tgt[0][b"extra"] = ("string", b"new", -1)                       # extra
        tgt[0][b"set:1"] = ("list", [b"x"], -1)                         # type
        tgt[0][b"z:1"] = ("zset", {b"alice": 1.5, b"bob": 2.5}, -1)       # score
        tgt[0][b"x:1"][1]["groups"][0]["last-delivered-id"] = b"2-0"     # stream group
        tgt[3][b"db3:only"] = ("string", b"THREE", -1)                  # second logical db
        _, sf, se = standalone("src", src)
        _, tf, te = standalone("tgt", tgt)
        report = run_fake(base_cfg(full_scan=True), (sf, se), (tf, te))
        got = {(m["db"], m["key"], m["kind"]) for m in report["mismatches"]}
        self.assertEqual(got, {(0, "s:1", "value"), (0, "s:ttl", "ttl"), (0, "h:1", "ttl"),
                               (0, "l:1", "missing_on_target"), (0, "extra", "extra_on_target"),
                               (0, "set:1", "type"), (0, "z:1", "value"), (0, "x:1", "stream_groups"),
                               (3, "db3:only", "value")})
        self.assertIs(report["verdict"], False)
        self.assertIs(report["checks"]["key_count"], True)   # same count: one missing + one extra
        sampled = run_fake(base_cfg(), (sf, se), (tf, te))   # sample >= dbsize finds the same set
        self.assertEqual({(m["db"], m["key"], m["kind"]) for m in sampled["mismatches"]}, got)

    def test_ttl_within_tolerance_is_a_match(self):
        src = make_dataset()
        tgt = clone(src)
        tgt[0][b"s:ttl"] = ("string", b"expiring", 600000 - 3000)
        _, sf, se = standalone("src", src)
        _, tf, te = standalone("tgt", tgt)
        self.assertIs(run_fake(base_cfg(ttl_tolerance_ms=5000), (sf, se), (tf, te))["verdict"], True)
        self.assertIs(run_fake(base_cfg(ttl_tolerance_ms=1000), (sf, se), (tf, te))["verdict"], False)

    def test_transient_mismatch_resolves_on_recheck(self):
        src = make_dataset()
        tgt_node, tf, te = standalone("tgt", clone(src))
        tgt_node.data[0][b"s:1"] = ("string", b"stale", -1)
        _, sf, se = standalone("src", src)
        real_sleep = []

        def catch_up(seconds):   # replication applies the write before the re-read
            real_sleep.append(seconds)
            tgt_node.data[0][b"s:1"] = src[0][b"s:1"]
        with mock.patch.object(cc.time, "sleep", catch_up):
            report = cc.run(base_cfg(recheck_delay_seconds=0.5), mock.Mock(dashboard=None, record=None),
                            factories={"source": (sf, se), "target": (tf, te)})
        self.assertIs(report["verdict"], True)
        self.assertEqual([m["key"] for m in report["transient_mismatches"]], ["s:1"])
        self.assertEqual(real_sleep, [0.5])

    def test_cluster_target_routes_by_slot_and_follows_moved(self):
        data = {0: make_dataset()[0]}
        _, sf, se = standalone("src", data)
        nodes, tf, te = fake_cluster(clone(data))
        report = run_fake(base_cfg(target={"host": "10.0.0.2", "port": 6379}), (sf, se), (tf, te))
        self.assertIs(report["verdict"], True, report)
        self.assertEqual(report["target"]["primaries"], 3)
        self.assertTrue(all(n.reads for n in nodes), "every primary must be read")
        # Reshard after discovery: the stale slot map gets MOVED, refreshes once, still reads.
        side = cc.RedisSide("target", {"host": "10.0.0.1", "port": 6379}, tf, te).discover()
        moved = next(k for k in data[0] if nodes[0].owns(k))
        slot = cc.key_slot(moved)
        nodes[0].lost.add(slot)
        nodes[1].gained.add(slot)
        nodes[1].data[0][moved] = nodes[0].data[0].pop(moved)
        self.assertEqual(side.slots[slot], ("10.0.0.1", 6379))
        self.assertEqual(cc.read_key(side, moved, 0, 1000, 100)["type"], data[0][moved][0])
        self.assertEqual(side.slots[slot], ("10.0.0.2", 6379), "slot map refreshed after MOVED")

    def test_cluster_source_against_standalone_target(self):
        data = {0: make_dataset()[0]}
        _, sf, se = fake_cluster(data)
        tgt = clone(data)
        tgt[0][b"k:7"] = ("string", b"changed", -1)
        _, tf, te = standalone("tgt", tgt)
        report = run_fake(base_cfg(source={"host": "10.0.0.3", "port": 6379}), (sf, se), (tf, te))
        self.assertEqual(report["source"]["primaries"], 3)
        self.assertEqual([(m["key"], m["kind"]) for m in report["mismatches"]], [("k:7", "value")])

    def test_multi_db_source_into_cluster_target_is_flagged(self):
        src = make_dataset()
        _, sf, se = standalone("src", src)
        _, tf, te = fake_cluster(clone(src))     # db3 has nowhere to go
        report = run_fake(base_cfg(target={"host": "10.0.0.1", "port": 6379}), (sf, se), (tf, te))
        self.assertIs(report["checks"]["key_count"], False)
        self.assertTrue(any("cluster-mode" in l for l in report["limitations"]))

    def test_budget_exhaustion_is_null_not_a_pass(self):
        _, sf, se = standalone("src", make_dataset())
        _, tf, te = standalone("tgt", clone(make_dataset()))
        report = run_fake(base_cfg(time_budget_seconds=0), (sf, se), (tf, te))
        self.assertIsNone(report["checks"]["sample_match"])
        self.assertIsNone(report["verdict"])
        self.assertTrue(any("time budget" in l for l in report["limitations"]))

    def test_large_collections_fall_back_to_cardinality(self):
        _, sf, se = standalone("src", make_dataset())
        tgt = clone(make_dataset())
        tgt[0][b"h:big"][1][b"f1"] = b"different"
        _, tf, te = standalone("tgt", tgt)
        report = run_fake(base_cfg(max_elements=100), (sf, se), (tf, te))
        self.assertIsNone(report["verdict"])       # contents not compared -> unknown, never a pass
        self.assertIsNone(report["checks"]["sample_match"])
        self.assertEqual([u["key"] for u in report["uncompared_keys"]], ["h:big"])
        self.assertTrue(any("max_elements" in l for l in report["limitations"]))
        self.assertIs(run_fake(base_cfg(max_elements=1000), (sf, se), (tf, te))["verdict"], False)

    def test_module_types_are_unsupported_and_need_review(self):
        src = {0: {b"json:1": ("ReJSON-RL", b"{}", -1)}}
        _, sf, se = standalone("src", src)
        _, tf, te = standalone("tgt", clone(src))
        report = run_fake(base_cfg(), (sf, se), (tf, te))
        self.assertIsNone(report["checks"]["sample_match"])
        self.assertIsNone(report["verdict"])

    def test_replication_and_headroom_checks(self):
        _, sf, se = standalone("src", make_dataset())
        tnode, tf, te = standalone("tgt", clone(make_dataset()), maxmemory=10_000, used=9_000)
        tnode.replication = {"role": "slave", "master_link_status": "down"}
        report = run_fake(base_cfg(expect_replication=True), (sf, se), (tf, te))
        self.assertIs(report["checks"]["replication_link"], False)
        self.assertIs(report["checks"]["headroom"], False)      # 10 % < 30 % threshold
        self.assertEqual(report["headroom_pct"], 10.0)
        tnode.replication = {"role": "slave", "master_link_status": "up"}
        tnode.used = 1_000
        report = run_fake(base_cfg(expect_replication=True), (sf, se), (tf, te))
        self.assertIs(report["checks"]["replication_link"], True)
        self.assertIs(report["verdict"], True)

    def test_unreachable_side_is_null(self):
        def broken(h, p, db):
            raise ConnectionRefusedError("refused")
        _, sf, se = standalone("src", make_dataset())
        report = run_fake(base_cfg(), (sf, se), (broken, FakeResponseError))
        self.assertIsNone(report["verdict"])
        self.assertTrue(report["errors"])


class ReviewRegressionTest(unittest.TestCase):
    """Findings from the 2026-10-28 review: unknowns must never surface as passes."""

    def test_error_after_sampling_caps_the_verdict(self):
        _, sf, se = standalone("src", make_dataset())
        tnode, tf, te = standalone("tgt", clone(make_dataset()))
        tnode.memory_denied = True
        report = run_fake(base_cfg(), (sf, se), (tf, te))
        self.assertIs(report["checks"]["sample_match"], True)
        self.assertIsNone(report["checks"]["headroom"])
        self.assertIsNone(report["verdict"])
        self.assertTrue(any("INFO" in e or "NOPERM" in e for e in report["errors"]))

    def test_configured_alarms_start_unknown_for_both_engines(self):
        with mock.patch.object(cc, "alarms_check", side_effect=RuntimeError("throttled")):
            _, sf, se = standalone("src", make_dataset())
            _, tf, te = standalone("tgt", clone(make_dataset()))
            report = run_fake(base_cfg(alarm_names=["a"], region="ap-northeast-2"), (sf, se), (tf, te))
            self.assertIsNone(report["checks"]["alarms"])
            self.assertIsNone(report["verdict"])
            cfg = base_cfg(alarm_names=["a"], region="ap-northeast-2")
            cfg["source"]["engine"] = cfg["target"]["engine"] = "memcached"
            stats = {"curr_items": "1", "get_hits": "1", "get_misses": "0"}
            with mock.patch.object(cc, "memcached_stats", return_value=stats):
                report = cc.run(cfg, mock.Mock(dashboard=None, record=None))
        self.assertIsNone(report["checks"]["alarms"])
        self.assertIsNone(report["verdict"])
        with mock.patch.object(cc, "alarms_check", return_value=(False, {"firing_alarms": ["a"]})), \
                mock.patch.object(cc, "memcached_stats", return_value=stats):
            self.assertIs(cc.run(cfg, mock.Mock(dashboard=None, record=None))["verdict"], False)

    def test_multi_db_cluster_source_is_compared(self):
        data = make_dataset()
        _, sf, se = fake_cluster(data, multi_db=True)              # Valkey 9 cluster-databases
        tgt = clone(data)
        tgt[3][b"db3:only"] = ("string", b"changed", -1)
        _, tf, te = standalone("tgt", tgt)
        report = run_fake(base_cfg(source={"host": "10.0.0.1", "port": 6379}), (sf, se), (tf, te))
        self.assertEqual([(m["db"], m["key"]) for m in report["mismatches"]], [(3, "db3:only")])

    def test_cluster_db_that_cannot_be_selected_is_unknown(self):
        data = make_dataset()
        nodes, sf, se = fake_cluster(data, multi_db=True)
        for n in nodes:
            n.multi_db = False                                     # INFO shows db3 but SELECT is refused
        _, tf, te = standalone("tgt", clone(data))
        report = run_fake(base_cfg(source={"host": "10.0.0.1", "port": 6379}), (sf, se), (tf, te))
        ks3 = next(k for k in report["keyspaces"] if k["db"] == 3)
        self.assertIsNone(ks3["sample_match"])
        self.assertIsNone(report["verdict"])

    def test_unsupported_type_is_not_published_as_validated(self):
        src = {0: {b"json:1": ("ReJSON-RL", b"{}", -1), b"s": ("string", b"x", -1)}}
        _, sf, se = standalone("src", src)
        _, tf, te = standalone("tgt", clone(src))
        report = run_fake(base_cfg(), (sf, se), (tf, te))
        patch, status, _ = cc.validation_patch(mock.Mock(phase="7", step_id="x"), report)
        item = patch["migration_objects"]["keyspaces"]["items"][0]
        self.assertEqual((item["status"], item["comparison"], status), ("loaded", "unknown", "blocked"))
        self.assertEqual(patch["migration_objects"]["keyspaces"]["validated"], 0)

    def test_stream_duplicate_fields_are_digested(self):
        src = {0: {b"x": ("stream", {"entries": [(b"1-0", [b"f", b"first", b"f", b"last"])], "groups": []}, -1)}}
        tgt = {0: {b"x": ("stream", {"entries": [(b"1-0", [b"f", b"last"])], "groups": []}, -1)}}
        _, sf, se = standalone("src", src)
        _, tf, te = standalone("tgt", tgt)
        report = run_fake(base_cfg(), (sf, se), (tf, te))
        self.assertEqual([(m["key"], m["kind"]) for m in report["mismatches"]], [("x", "value")])

    def test_slow_reads_cannot_finish_as_success(self):
        _, sf, se = standalone("src", make_dataset())
        tnode, tf, te = standalone("tgt", clone(make_dataset()))
        tnode.delay = 0.5                        # each target read costs 0.5 simulated s
        FAKE_CLOCK[0] = 0.0
        cfg = base_cfg(time_budget_seconds=5)
        src_side = cc.RedisSide("source", cfg["source"], sf, se).discover()
        tgt_side = cc.RedisSide("target", cfg["target"], tf, te).discover()
        result = cc.compare_redis(cfg, src_side, tgt_side, clock=lambda: FAKE_CLOCK[0], sleep=lambda s: None)
        self.assertIsNone(result["checks"]["sample_match"])
        self.assertFalse(result["complete"])
        self.assertTrue(any("time budget" in l for l in result["limitations"]))
        tnode.delay = 0.0

    def test_budget_expiring_during_rechecks_is_reported(self):
        src = make_dataset()
        tgt = clone(src)
        tgt[0][b"s:1"] = ("string", b"other", -1)
        _, sf, se = standalone("src", src)
        tnode, tf, te = standalone("tgt", tgt)
        cfg = base_cfg(time_budget_seconds=10)
        FAKE_CLOCK[0] = 0.0
        src_side = cc.RedisSide("source", cfg["source"], sf, se).discover()
        tgt_side = cc.RedisSide("target", cfg["target"], tf, te).discover()

        def slow_sleep(seconds):
            FAKE_CLOCK[0] += 60                  # the recheck delay blows the budget
        result = cc.compare_redis(cfg, src_side, tgt_side, clock=lambda: FAKE_CLOCK[0], sleep=slow_sleep)
        self.assertIs(result["checks"]["sample_match"], False)   # mismatch kept, not silently cleared
        self.assertTrue(any("re-reads" in l or "exceeded" in l for l in result["limitations"]))


class MemcachedTest(unittest.TestCase):
    def test_stats_comparison_is_not_a_data_comparison(self):
        s = {"curr_items": "1000", "get_hits": "900", "get_misses": "100", "version": "1.6.22"}
        t = {"curr_items": "100", "get_hits": "10", "get_misses": "90", "version": "1.6.22"}
        rep = cc.compare_memcached(base_cfg(), s, t)
        self.assertEqual(rep["checks"], {"data_match": "not_applicable"})
        rep = cc.compare_memcached(base_cfg(memcached_min_items_ratio=0.8, memcached_max_hit_ratio_drop=0.1,
                                            memcached_min_gets=50), s, t)
        self.assertIs(rep["checks"]["warm_items"], False)
        self.assertIs(rep["checks"]["warm_hit_ratio"], False)
        rep = cc.compare_memcached(base_cfg(memcached_max_hit_ratio_drop=0.1, memcached_min_gets=5000), s, t)
        self.assertIsNone(rep["checks"]["warm_hit_ratio"])   # not enough traffic yet

    def test_stats_over_a_socket(self):
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(b"STAT pid 1\r\nSTAT curr_items 42\r\nSTAT version 1.6.22\r\nEND\r\n")
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            stats = cc.memcached_stats({"host": "127.0.0.1", "port": server.server_address[1]}, 5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(stats["curr_items"], "42")


class DashboardHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dash = Path(self.tmp.name) / "dashboard"
        shutil.copytree(FIXTURE, self.dash)
        self.plan = Path(self.tmp.name) / "migration-plan.md"
        self.plan.write_text("# plan\n\n## Phase 7\n\n## Phase 7.7\n")
        self.cfgp = Path(self.tmp.name) / "cfg.json"
        self.cfgp.write_text(json.dumps({"source": {"host": "src"}, "target": {"host": "tgt"},
                                         "recheck_delay_seconds": 0}))

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, tgt_data, *extra):
        src_factory = standalone("src", make_dataset())[1:]
        tgt_factory = standalone("tgt", tgt_data)[1:]

        def factory(cfg, side, username, password):
            return src_factory if side == "source" else tgt_factory
        with mock.patch.object(cc, "_redis_factory", factory), \
                contextlib.redirect_stderr(io.StringIO()):
            return cc.main(["--config", str(self.cfgp), "--report", str(Path(self.tmp.name) / "r.json"),
                            "--dashboard", str(self.dash), "--plan", str(self.plan), *extra])

    def test_validation_publishes_step_and_keyspaces(self):
        rc = self._main(clone(make_dataset()), "--record", "validation", "--plan-section", "Phase 7")
        self.assertEqual(rc, 0)
        st = json.loads((self.dash / "status.json").read_text())
        step = next(s for p in st["phases"] if p["id"] == "7" for s in p["steps"] if s["id"] == "cache-compare")
        self.assertEqual(step["status"], "done")
        ks = st["migration_objects"]["keyspaces"]
        self.assertEqual((ks["total"], ks["validated"]), (2, 2))
        self.assertEqual(ks["items"][0]["name"], "db0")
        self.assertIn("tables", st["migration_objects"], "existing object types must be preserved")
        log = (self.dash / "activity-log.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(l)["result"] for l in log[-2:]], ["in_progress", "success"])
        self.assertIn("cache_compare cache-compare", self.plan.read_text())
        self.assertEqual(oct(os.stat(Path(self.tmp.name) / "r.json").st_mode & 0o777), "0o600")

    def test_validation_mismatch_blocks_the_step(self):
        tgt = clone(make_dataset())
        tgt[0][b"s:1"] = ("string", b"other", -1)
        self.assertEqual(self._main(tgt, "--record", "validation"), 1)
        st = json.loads((self.dash / "status.json").read_text())
        step = next(s for p in st["phases"] if p["id"] == "7" for s in p["steps"] if s["id"] == "cache-compare")
        self.assertEqual(step["status"], "blocked")
        self.assertEqual(st["migration_objects"]["keyspaces"]["items"][0]["status"], "loaded")

    def test_soak_day_is_recorded_and_never_self_greens(self):
        rc = self._main(clone(make_dataset()), "--record", "soak", "--soak-n-total", "3", "--run-date", "2026-10-28")
        self.assertEqual(rc, 0)   # the comparison matched ...
        st = json.loads((self.dash / "status.json").read_text())
        day = next(d for d in st["soak"]["days"] if d["date"] == "2026-10-28")
        self.assertEqual(day["checks"]["sample_match"], True)
        self.assertIsNone(day["checks"]["period_evidence"])          # ... but full-period review is manual
        self.assertEqual((day["overall"], day["needs_agent_review"]), ("red", True))
        self.assertEqual(st["soak"]["consecutive_green"], 0)
        self.assertEqual(day["detail"]["sampler"], "cache_compare")
        # Re-running the same UTC day replaces that day, never duplicates it, and keeps the
        # literal null (merge-patch would have deleted it).
        self._main(clone(make_dataset()), "--record", "soak", "--soak-n-total", "3", "--run-date", "2026-10-28")
        st = json.loads((self.dash / "status.json").read_text())
        days = [d for d in st["soak"]["days"] if d["date"] == "2026-10-28"]
        self.assertEqual(len(days), 1)
        self.assertIn("period_evidence", days[0]["checks"])
        self.assertIsNone(days[0]["checks"]["period_evidence"])

    def test_soak_publication_into_a_seeded_soak_keeps_nulls(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(du.main(["--dashboard", str(self.dash), "--seed-soak", "2"]), 0)
        for date in ("2026-10-27", "2026-10-28"):
            self.assertEqual(self._main(clone(make_dataset()), "--record", "soak", "--run-date", date), 0)
        st = json.loads((self.dash / "status.json").read_text())
        self.assertEqual([d["date"] for d in st["soak"]["days"]], ["2026-10-27", "2026-10-28"])
        self.assertTrue(all(d["checks"]["period_evidence"] is None for d in st["soak"]["days"]))
        self.assertEqual((st["soak"]["consecutive_green"], st["soak"]["state"]), (0, "active"))

    def test_streak_counts_only_consecutive_reviewed_green_days(self):
        g = {"checks": {"key_count": True, "period_evidence": True}, "overall": "green", "needs_agent_review": False}
        days = [dict(g, date="2026-10-26"), dict(g, date="2026-10-27"), dict(g, date="2026-10-28")]
        self.assertEqual(du.green_streak(days, "2026-10-28"), 3)
        days[1] = dict(g, date="2026-10-27", checks={"key_count": None})
        self.assertEqual(du.green_streak(days, "2026-10-28"), 1)


# ----------------------------------------------------------------------------- integration

def _docker_ok(need_redis_py=True):
    if not shutil.which("docker"):
        return "docker CLI not found"
    try:
        out = subprocess.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return f"docker unavailable: {e}"
    if out.returncode != 0:
        return f"docker unavailable: {out.stderr.strip()[:120]}"
    missing = {"redis:7.2", "valkey/valkey:8.1", "memcached:1.6"} - set(out.stdout.split())
    if missing:
        return f"images not pulled locally: {sorted(missing)}"
    if need_redis_py:
        try:
            import redis  # noqa: F401
        except ImportError:
            return "redis-py not installed (pip install redis)"
    return None


DOCKER_SKIP = _docker_ok()
DOCKER_SKIP_MEMCACHED = _docker_ok(need_redis_py=False)   # memcached mode is stdlib-only


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=check).stdout.strip()


class DockerEnv:
    def __init__(self):
        self.tag = "cctest-" + uuid.uuid4().hex[:8]
        self.network = self.tag
        self.containers = []
        _docker("network", "create", self.network)

    def run(self, name, image, *cmd, publish=None):
        cname = f"{self.tag}-{name}"
        args = ["run", "-d", "--rm", "--name", cname, "--network", self.network]
        if publish:
            args += ["-p", f"127.0.0.1::{publish}"]
        _docker(*args, image, *cmd)
        self.containers.append(cname)
        return cname

    def port(self, cname, port):
        return int(_docker("port", cname, str(port)).splitlines()[0].rsplit(":", 1)[1])

    def ip(self, cname):
        return _docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cname)

    def close(self):
        for c in self.containers:
            _docker("rm", "-f", c, check=False)
        _docker("network", "rm", self.network, check=False)


def _wait(fn, timeout=30, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if fn():
                return
        except Exception as e:   # noqa: BLE001 — engines are still starting
            last = e
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what} ({last})")


def populate(client_for_db, big=True):
    """Mixed types + TTLs in db0 and db2 (+ large keys). client_for_db(db) -> redis client."""
    c = client_for_db(0)
    for i in range(200):
        c.set(f"str:{i}", f"value-{i}")
    for i in range(0, 200, 4):
        c.expire(f"str:{i}", 3600 + i)
    c.rpush("list:1", *[f"e{i}" for i in range(50)])
    c.hset("hash:1", mapping={f"f{i}": f"v{i}" for i in range(30)})
    c.expire("hash:1", 7200)
    c.sadd("set:1", *[f"m{i}" for i in range(40)])
    c.zadd("zset:1", {f"p{i}": i * 1.25 for i in range(40)})
    for i in range(30):
        c.xadd("stream:1", {"n": str(i)}, id=f"{1700000000000 + i}-0")   # explicit IDs: comparable everywhere
    c.xgroup_create("stream:1", "workers", id="0")
    if big:
        c.set("big:string", b"\x00\x01payload" * 200_000)                       # ~1.8 MiB → GETRANGE chunks
        c.hset("big:hash", mapping={f"field:{i}": f"val:{i}" for i in range(20_000)})
    if client_for_db(2) is not None:
        d2 = client_for_db(2)
        for i in range(25):
            d2.set(f"db2:{i}", f"two-{i}")
        d2.pexpire("db2:0", 900_000)


def _cfg_file(d, src, tgt, **extra):
    p = Path(d) / f"cfg-{uuid.uuid4().hex[:6]}.json"
    p.write_text(json.dumps({"source": src, "target": tgt, "recheck_delay_seconds": 0.2, **extra}))
    return str(p)


def _run_main(cfg_path, *extra):
    report = Path(cfg_path).with_suffix(".report.json")
    with contextlib.redirect_stderr(io.StringIO()):
        rc = cc.main(["--config", cfg_path, "--report", str(report), *extra])
    return rc, json.loads(report.read_text())


@unittest.skipIf(DOCKER_SKIP, DOCKER_SKIP or "")
class RedisToValkeyIntegrationTest(unittest.TestCase):
    """redis:7.2 (source) → valkey/valkey:8.1 (target, AUTH via password_env) by REPLICAOF."""

    @classmethod
    def setUpClass(cls):
        import redis
        cls.redis = redis
        cls.env = DockerEnv()
        cls.addClassCleanup(cls.env.close)
        cls.src_name = cls.env.run("src", "redis:7.2", "redis-server", "--protected-mode", "no", publish=6379)
        cls.tgt_name = cls.env.run("tgt", "valkey/valkey:8.1", "valkey-server", "--requirepass", "tgt-secret-123",
                                   "--protected-mode", "no", publish=6379)
        cls.src_port, cls.tgt_port = cls.env.port(cls.src_name, 6379), cls.env.port(cls.tgt_name, 6379)
        cls.src = staticmethod(lambda db=0: redis.Redis(host="127.0.0.1", port=cls.src_port, db=db))
        cls.tgt = staticmethod(lambda db=0: redis.Redis(host="127.0.0.1", port=cls.tgt_port, db=db,
                                                        password="tgt-secret-123"))
        _wait(lambda: cls.src().ping() and cls.tgt().ping(), what="engines up")
        populate(lambda db: cls.src(db))
        cls.tgt().execute_command("REPLICAOF", cls.src_name, "6379")

        def synced():
            r, s = cls.tgt().info("replication"), cls.src().info("replication")
            return (r.get("master_link_status") == "up" and r.get("slave_repl_offset") == s.get("master_repl_offset")
                    and {d: v["keys"] for d, v in cls.tgt().info("keyspace").items()}
                    == {d: v["keys"] for d, v in cls.src().info("keyspace").items()})
        _wait(synced, 60, "valkey replica in sync with redis 7.2")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.src_ep = {"host": "127.0.0.1", "port": cls.src_port, "engine": "redis"}
        cls.tgt_ep = {"host": "127.0.0.1", "port": cls.tgt_port, "engine": "valkey", "password_env": "CC_IT_TGT_PW"}

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CC_IT_TGT_PW": "tgt-secret-123", "CC_IT_SRC_PW": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_1_replicated_copy_matches(self):
        info = self.tgt().info("server")
        self.assertIn("valkey_version", info)
        # The documented least-privilege ACL (cache_compare.py docstring) must be enough.
        self.src().execute_command("ACL", "SETUSER", "cache_ro", "reset", "on", ">ro-pass-1234567890abc", *cc.READ_ONLY_ACL)
        src_ep = dict(self.src_ep, username="cache_ro", password_env="CC_IT_SRC_PW")
        os.environ["CC_IT_SRC_PW"] = "ro-pass-1234567890abc"
        cfg = _cfg_file(self.tmp.name, src_ep, self.tgt_ep, expect_replication=True, sample_size=200)
        rc, rep = _run_main(cfg)
        self.assertEqual(rc, 0, rep)
        self.assertIs(rep["checks"]["replication_link"], True)
        self.assertEqual(rep["checks"]["key_count"], True)
        self.assertEqual({k["db"] for k in rep["keyspaces"]}, {0, 2})
        self.assertEqual(rep["source"]["server_name"], "redis")
        self.assertEqual(rep["target"]["server_name"], "valkey")
        rc, rep = _run_main(cfg, "--full-scan")
        self.assertEqual(rc, 0, rep["mismatches"])
        self.assertEqual(sum(k["sampled"] for k in rep["keyspaces"]), self.src().dbsize() + self.src(2).dbsize())

    def test_2_mutations_are_reported_exactly(self):
        t = self.tgt()
        t.execute_command("REPLICAOF", "NO", "ONE")
        _wait(lambda: self.tgt().info("replication")["role"] == "master", what="promotion")
        t.set("str:1", "tampered")                    # value
        t.expire("str:2", 99_999)                     # TTL added
        t.persist("str:4")                            # TTL removed
        t.delete("list:1")                            # missing on target
        t.set("only:on:target", "x")                  # extra on target
        t.hset("big:hash", "field:777", "changed")    # value inside the large key
        self.tgt(2).set("db2:5", "changed")           # second logical db
        # Same stream ID, duplicate field names on one side only (redis-py's dict view would hide it).
        self.src().execute_command("XADD", "dup:stream", "1-0", "f", "first", "f", "last")
        t.execute_command("XADD", "dup:stream", "1-0", "f", "last")
        cfg = _cfg_file(self.tmp.name, self.src_ep, self.tgt_ep)
        rc, rep = _run_main(cfg, "--full-scan")
        self.assertEqual(rc, 1)
        got = {(m["db"], m["key"], m["kind"]) for m in rep["mismatches"]}
        self.assertEqual(got, {(0, "str:1", "value"), (0, "str:2", "ttl"), (0, "str:4", "ttl"),
                               (0, "list:1", "missing_on_target"), (0, "only:on:target", "extra_on_target"),
                               (0, "big:hash", "value"), (2, "db2:5", "value"), (0, "dup:stream", "value")})
        rc, sampled = _run_main(cfg, "--sample-size", "100000")
        self.assertEqual({(m["db"], m["key"], m["kind"]) for m in sampled["mismatches"]}, got)


@unittest.skipIf(DOCKER_SKIP, DOCKER_SKIP or "")
class ClusterIntegrationTest(unittest.TestCase):
    """3-node valkey 8.1 cluster vs standalone redis 7.2, compared in both directions."""

    @classmethod
    def setUpClass(cls):
        import redis
        cls.env = DockerEnv()
        cls.addClassCleanup(cls.env.close)
        names = [cls.env.run(f"c{i}", "valkey/valkey:8.1", "valkey-server", "--cluster-enabled", "yes",
                             "--protected-mode", "no") for i in range(3)]
        cls.sa = cls.env.run("sa", "redis:7.2", "redis-server", "--protected-mode", "no")
        ips = [cls.env.ip(n) for n in names]
        cls.sa_ip = cls.env.ip(cls.sa)
        try:   # the cluster announces container IPs: they must be routable from this host (Linux bridge)
            socket.create_connection((ips[0], 6379), timeout=3).close()
        except OSError:
            raise unittest.SkipTest("container IPs not routable from this host (e.g. Docker Desktop) — "
                                    "cluster test needs announced addresses reachable")
        _wait(lambda: redis.Redis(host=ips[0]).ping(), what="cluster nodes up")
        _docker("exec", names[0], "valkey-cli", "--cluster", "create", *[f"{ip}:6379" for ip in ips],
                "--cluster-replicas", "0", "--cluster-yes")
        _wait(lambda: all(redis.Redis(host=ip).info("cluster") and
                          redis.Redis(host=ip).execute_command("CLUSTER", "INFO").decode().count("cluster_state:ok")
                          for ip in ips), 60, "cluster_state:ok")
        from redis.cluster import RedisCluster
        cls.rc = RedisCluster(host=ips[0], port=6379)
        cls.addClassCleanup(cls.rc.close)
        _wait(lambda: cls.rc.set("probe", "1") and cls.rc.delete("probe") is not None, 30, "cluster writable")
        cls.sa_client = redis.Redis(host=cls.sa_ip)
        populate(lambda db: cls.rc if db == 0 else None, big=False)
        populate(lambda db: cls.sa_client if db == 0 else None, big=False)
        cls.ips = ips
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)

    def test_cluster_both_directions(self):
        sa = {"host": self.sa_ip, "port": 6379}
        cl = {"host": self.ips[1], "port": 6379, "cluster": "auto"}
        rc, rep = _run_main(_cfg_file(self.tmp.name, sa, cl, ttl_tolerance_ms=10_000), "--full-scan")
        self.assertEqual(rc, 0, rep.get("mismatches") or rep.get("errors"))
        self.assertEqual((rep["target"]["cluster"], rep["target"]["primaries"]), (True, 3))
        self.rc.set("str:9", "cluster-changed")
        for direction in ((sa, cl), (cl, sa)):
            rc, rep = _run_main(_cfg_file(self.tmp.name, *direction, ttl_tolerance_ms=10_000), "--full-scan")
            self.assertEqual(rc, 1)
            self.assertEqual([(m["key"], m["kind"]) for m in rep["mismatches"]], [("str:9", "value")])


@unittest.skipIf(DOCKER_SKIP_MEMCACHED, DOCKER_SKIP_MEMCACHED or "")
class MemcachedIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = DockerEnv()
        cls.addClassCleanup(cls.env.close)
        cls.a = cls.env.run("mca", "memcached:1.6", publish=11211)
        cls.b = cls.env.run("mcb", "memcached:1.6", publish=11211)
        cls.pa, cls.pb = cls.env.port(cls.a, 11211), cls.env.port(cls.b, 11211)
        _wait(lambda: cc.memcached_stats({"host": "127.0.0.1", "port": cls.pa}, 2) and
              cc.memcached_stats({"host": "127.0.0.1", "port": cls.pb}, 2), what="memcached up")
        with socket.create_connection(("127.0.0.1", cls.pa), timeout=5) as s:
            for i in range(100):
                s.sendall(f"set k{i} 0 0 3\r\nabc\r\n".encode())
                s.recv(64)
            for i in range(50):
                s.sendall(f"get k{i}\r\n".encode())
                s.recv(256)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)

    def test_stats_mode(self):
        a = {"host": "127.0.0.1", "port": self.pa, "engine": "memcached"}
        b = {"host": "127.0.0.1", "port": self.pb, "engine": "memcached"}
        rc, rep = _run_main(_cfg_file(self.tmp.name, a, b))
        self.assertEqual((rc, rep["verdict"]), (0, "not_applicable"))
        self.assertEqual(rep["source"]["curr_items"], "100")
        self.assertIn("not a data comparison", rep["limitations"][0])
        rc, rep = _run_main(_cfg_file(self.tmp.name, a, b, memcached_min_items_ratio=0.9))
        self.assertEqual((rc, rep["checks"]["warm_items"]), (1, False))


if __name__ == "__main__":
    unittest.main(verbosity=1)
