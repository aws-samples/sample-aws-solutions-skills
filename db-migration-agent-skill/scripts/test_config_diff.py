#!/usr/bin/env python3
"""Tests for shared/scripts/config_diff.py — fail-closed: a value is printed only when its key is a
connection field AND the value passes that field's strict pattern; everything else is
changed/unchanged/added/removed; anything ambiguous makes the whole file opaque. Includes the
review-11 and review-12 reproductions.

Run: python3 -B -m unittest discover -s scripts -p 'test_config_diff.py'
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "shared" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import config_diff as cd  # noqa: E402

FAKES = ("s3cretPW", "AKIAXXXXSECRET", "-----BEGIN", "jsonPW123", "yamlPW456", "multiLinePW",
         "secondUrlPW", "firstUrlPW", "tok_abcdef", "dsnFake", "queryFake", "anchorFake",
         "oldFake", "newFake", "escapedFake", "litFake", "nestFake", "keyFake")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_diff(self, ext, live, staged, *extra):
        a, b = self.d / f"live.{ext}", self.d / f"staged.{ext}"
        a.write_text(live)
        b.write_text(staged)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cd.main([str(a), str(b), *extra])
        text = out.getvalue() + err.getvalue()
        for fake in FAKES:
            self.assertNotIn(fake, text)
        return rc, out.getvalue()


class EnvTests(Base):
    def test_connection_fields_print_secrets_never(self):
        live = ("REDIS_HOST=old.example\nREDIS_PORT=6379\nREDIS_PASSWORD=s3cretPW\nAPI_KEY=AKIAXXXXSECRET\n"
                "SERVICE_TOKEN=tok_abcdef\nREDIS_TLS=false\nREDIS_USER=app\nREDIS_DB=0\n")
        staged = live.replace("old.example", "new.example").replace("REDIS_TLS=false", "REDIS_TLS=true")
        rc, out = self.run_diff("env", live, staged)
        self.assertEqual(rc, 0)
        self.assertIn("REDIS_HOST: changed  old.example -> new.example", out)
        self.assertIn("REDIS_TLS: changed  false -> true", out)
        self.assertIn("REDIS_PORT: unchanged  6379", out)
        self.assertIn("REDIS_USER: unchanged  app", out)
        self.assertIn("REDIS_DB: unchanged  0", out)
        for key in ("REDIS_PASSWORD", "API_KEY", "SERVICE_TOKEN"):
            self.assertIn(f"{key}: unchanged\n", out + "\n")

    def test_review12_allow_listed_keys_with_unsafe_values_are_not_printed(self):
        live = ('DATABASE="host=old password=dsnFake"\nREDIS_ENDPOINT=h.example?password=queryFake\n'
                "APP_SERVER=user:keyFake@h\nREDIS_PORT=70000\nREDIS_USER=a=b\n")
        staged = live.replace("old", "new")
        rc, out = self.run_diff("env", live, staged)
        self.assertIn("DATABASE: changed", out)
        self.assertIn("REDIS_ENDPOINT: unchanged", out)   # ambiguous unquoted? -> no: value has no spaces
        self.assertIn("APP_SERVER: unchanged", out)
        self.assertIn("REDIS_PORT: unchanged\n", out + "\n")
        self.assertIn("REDIS_USER: unchanged\n", out + "\n")

    def test_urls_show_only_scheme_host_port_path(self):
        live = "CACHE_URL=rediss://app:firstUrlPW@cache.example:6380/0?password=queryFake#frag\n"
        staged = "CACHE_URL=rediss://app:secondUrlPW@cache2.example:6380/0\n"
        rc, out = self.run_diff("env", live, staged)
        self.assertIn("CACHE_URL: changed  rediss://cache.example:6380 -> rediss://cache2.example:6380", out)

    def test_review13_url_paths_are_never_shown(self):
        live = "WEBHOOK_URL=https://hooks.example.com/services/TFAKE/BFAKE/s3cretPW\n"
        staged = "WEBHOOK_URL=https://hooks.example.com/services/TFAKE/BFAKE/multiLinePW\n"
        rc, out = self.run_diff("env", live, staged)
        self.assertIn("WEBHOOK_URL: changed  https://hooks.example.com -> https://hooks.example.com", out)
        self.assertNotIn("TFAKE", out)

    def test_long_or_odd_env_key_names_are_labelled(self):
        live = "A" * 65 + "=1\n"
        rc, out = self.run_diff("env", live, live)
        self.assertIn("<key #1>: unchanged", out)

    def test_two_urls_in_one_value_are_not_shown(self):
        live = 'CACHE_URL="redis://:firstUrlPW@a:6379,rediss://app:secondUrlPW@b:6380"\n'
        rc, out = self.run_diff("env", live, live)
        self.assertIn("CACHE_URL: unchanged\n", out + "\n")

    def test_review12_escaped_quote_change_is_detected(self):
        live = 'DB_PASSWORD="prefix\\"oldFake"\n'
        staged = 'DB_PASSWORD="prefix\\"newFake"\n'
        rc, out = self.run_diff("env", live, staged)
        self.assertIn("DB_PASSWORD: changed", out)

    def test_review12_multiline_secret_with_escaped_quote_makes_file_opaque(self):
        live = 'DB_PASSWORD="multiLinePW \\"\nDB_HOST=escapedFake"\nREDIS_PORT=6379\n'
        rc, out = self.run_diff("env", live, live)
        self.assertIn("opaque", out)
        self.assertNotIn("DB_HOST", out)

    def test_ambiguous_constructs_make_the_whole_file_opaque(self):
        for bad in ('A="unterminated\n', "A=x \\\nmultiLinePW\n", 'A="x" trailing\n', "A=x\nA=y\n",
                    "not a kv line\n", "A=`cmd`\n", "A=two words\n"):
            rc, out = self.run_diff("env", bad, bad + "B=1\n")
            self.assertIn("opaque", out, bad)
            self.assertIn("changed", out, bad)


class JsonYamlTests(Base):
    def test_json_shows_only_exact_top_level_connection_keys(self):
        live = ('{"host": "old", "port": 6379, "tls": true, "password": "jsonPW123", '
                '"redis": {"host": "nested", "password": "nestFake"}, "url": "rediss://u:jsonPW123@h:6380/0"}')
        staged = live.replace('"old"', '"new"')
        rc, out = self.run_diff("json", live, staged)
        self.assertIn("host: changed  old -> new", out)
        self.assertIn("port: unchanged  6379", out)
        self.assertIn("tls: unchanged  true", out)
        self.assertIn("url: unchanged  rediss://h:6380", out)
        self.assertIn("other content: unchanged", out)
        self.assertNotIn("password", out)
        self.assertNotIn("redis:", out)
        self.assertNotIn("nested", out)

    def test_review13_nested_passwords_and_token_keys_are_never_named(self):
        live = '{"passwords": {"database": "s3cretPW"}, "api_keys": {"tok_abcdef": {"scope": "x"}}, "port": 1}'
        staged = live.replace("s3cretPW", "multiLinePW")
        rc, out = self.run_diff("json", live, staged)
        self.assertIn("other content: changed (1 other entries changed)", out)
        for name in ("passwords", "database", "api_keys", "scope"):
            self.assertNotIn(name, out)

    def test_review13_type_and_container_changes_are_detected(self):
        for a, b in (('{"x": false}', '{"x": "false"}'), ('{"x": null}', '{"x": "null"}'),
                     ('{"x": {}}', '{"x": []}'), ('{"tls": false}', '{"tls": "false"}')):
            rc, out = self.run_diff("json", a, b)
            self.assertRegex(out, r"other content: changed|tls: changed", (a, b))
        rc, out = self.run_diff("json", '{"x": {}}', '{"x": {}}')
        self.assertIn("other content: unchanged", out)

    def test_review12_dotted_literal_and_nested_paths_do_not_collide(self):
        live = '{"db.password": "litFake", "db": {"password": "nestFake"}}'
        staged = '{"db.password": "litFake2", "db": {"password": "nestFake"}}'
        rc, out = self.run_diff("json", live, staged)
        self.assertIn("other content: changed (1 other entries changed)", out)
        self.assertNotIn("db.password", out)

    def test_duplicate_json_keys_and_non_object_top_level_are_opaque(self):
        for doc in ('{"password": "jsonPW123", "password": "x"}', '["jsonPW123"]'):
            rc, out = self.run_diff("json", doc, doc)
            self.assertIn("opaque", out)

    def test_review12_yaml_is_always_opaque(self):
        live = "s: &a anchorFake\nredis:\n  host: *a\n  password: yamlPW456\n"
        staged = live.replace("yamlPW456", "yamlPW457")
        rc, out = self.run_diff("yaml", live, staged)
        self.assertIn("staged.yaml: changed (opaque", out)
        rc, out = self.run_diff("yml", live, live)
        self.assertIn("unchanged (opaque", out)


class ErrorTests(Base):
    def test_errors_never_echo_content(self):
        a = self.d / "live.env"
        a.write_bytes(b"A=\xff\xfe s3cretPW\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cd.main([str(a), str(self.d / "missing.env")])
        self.assertEqual(rc, 2)
        self.assertNotIn("s3cretPW", out.getvalue() + err.getvalue())
        b = self.d / "staged.env"
        b.write_bytes(b"A=\xff s3cretPW\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cd.main([str(a), str(b)])
        self.assertEqual(rc, 0)
        self.assertNotIn("s3cretPW", out.getvalue() + err.getvalue())
        self.assertIn("opaque", out.getvalue())

    def test_internal_error_is_generic(self):
        a = self.d / "live.env"
        a.write_text("A=1\n")
        out, err = io.StringIO(), io.StringIO()
        orig = cd.compare
        cd.compare = lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("s3cretPW leaked?"))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cd.main([str(a), str(a)])
        finally:
            cd.compare = orig
        self.assertEqual(rc, 2)
        self.assertNotIn("s3cretPW", out.getvalue() + err.getvalue())


class PatternTests(unittest.TestCase):
    def test_value_gates(self):
        ok = [("host", "db.example.com"), ("host", "10.0.0.1"), ("host", "[2001:db8::1]"), ("port", "6379"),
              ("tls", "true"), ("sslmode", "verify-full"), ("index", "12"), ("dbname", "shop"), ("user", "app.svc@x")]
        bad = [("host", "a b"), ("host", "a=b"), ("host", "h?x=1"), ("host", "u@h"), ("host", "h/x"), ("host", "h:1"),
               ("host", "999.1.1.1"), ("port", "0"), ("port", "65536"), ("tls", "maybe"), ("index", "1000"),
               ("dbname", "host=x"), ("user", "a=b"), ("user", "x" * 65), ("url", "redis://a,b")]
        for t, v in ok:
            self.assertEqual(cd.safe_display(t, v), v, (t, v))
        for t, v in bad:
            self.assertIsNone(cd.safe_display(t, v), (t, v))
        self.assertIsNone(cd.field_type("DB_PASSWORD_HOST"), "secret word wins")
        self.assertEqual(cd.field_type("REDIS_SSL_MODE"), "sslmode")


if __name__ == "__main__":
    unittest.main()
