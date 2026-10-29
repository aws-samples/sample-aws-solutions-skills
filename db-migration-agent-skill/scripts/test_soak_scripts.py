#!/usr/bin/env python3
"""Offline unit tests for shared/scripts/soak_check_lambda.py and soak_check.py — no AWS,
no database. Uses botocore Stubber for every AWS call and fake DB connections/cursors.

Run: python3 scripts/test_soak_scripts.py   (needs boto3 + pymysql importable)
"""
import contextlib
import datetime
import io
import json
import re
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared" / "scripts"))

from botocore.exceptions import EndpointConnectionError  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

import boto3  # noqa: E402
import generate_presigned_urls as presign  # noqa: E402
import soak_check  # noqa: E402
import pymysql  # noqa: E402
import soak_check_lambda as L  # noqa: E402

# The live-table re-check (apply_rechecks) waits recheck.delay_seconds between re-reads; tests
# never sleep — RecheckTests asserts the configured delays explicitly.
soak_check.RECHECK_SLEEP = L.RECHECK_SLEEP = lambda seconds: None
from decimal import Decimal  # noqa: E402

# A SHOW REPLICA STATUS column list shaped like 8.4 (Seconds_Behind_Source deliberately NOT
# at index 32) and like an older SHOW SLAVE STATUS.
COLS_84 = ["Replica_IO_State", "Source_Host", "Source_User", "Source_Port", "Seconds_Behind_Source", "Last_Error"]
COLS_57 = ["Slave_IO_State", "Master_Host"] + [f"c{i}" for i in range(30)] + ["Seconds_Behind_Master"]

SRC_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:soak-src-AbCdEf"
TGT_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:soak-tgt-AbCdEf"
TASK_ARN = "arn:aws:dms:ap-northeast-2:111122223333:task:ABC"
BUCKET = "soak-dash-bucket"

ENV = {
    "SOURCE_ENGINE": "mysql", "TARGET_ENGINE": "mysql", "TABLES": '["shop.orders", "shop.payments"]',
    "ALARM_NAMES": '["tgt-cpu"]', "TARGET_DB_INSTANCE_ID": "tgt-db",
    "DMS_TASK_ID": "ABC", "DMS_REPLICATION_INSTANCE_ID": "ri-1", "DMS_TASK_ARN": TASK_ARN,
    "MYSQL_REPLICA_STATUS_SIDE": "target", "PG_REPLICATION_LAG_SIDE": "",
    "N_TOTAL": "3", "DASHBOARD_BUCKET": BUCKET, "DASHBOARD_PREFIX": "",
    "SOURCE_SECRET_ARN": SRC_ARN, "TARGET_SECRET_ARN": TGT_ARN,
    "SOURCE_HOST": "10.0.0.5", "SOURCE_PORT": "3306", "SOURCE_DB": "shop",
    "TARGET_HOST": "tgt.abc.ap-northeast-2.rds.amazonaws.com", "TARGET_PORT": "3306", "TARGET_DB": "shop",
}


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        for t in self.conn.denied:
            if t in sql:
                raise pymysql.err.OperationalError(1142, f"SELECT command denied to user for table '{t}'")
        up = sql.strip().upper()
        if up.startswith("SHOW REPLICA STATUS"):
            self.description = [(c,) for c in self.conn.cols]
            self._rows = [self.conn.row] if self.conn.row else []
        elif up.startswith("SELECT 1"):
            self.description, self._rows = [("1",)], [(1,)]
        elif up.startswith("SELECT COLUMN_NAME"):
            self.description, self._rows = [("column_name",)], [("id", "int", "NO", None)]
        elif up.startswith("CHECKSUM TABLE"):
            self.description, self._rows = [("Table",), ("Checksum",)], [("t", None)]
        else:
            self.description, self._rows = [("x",)], []

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, cols=COLS_84, row=("Waiting", "h", "u", 3306, 7, ""), denied=()):
        self.cols, self.row, self.executed, self.denied = cols, row, [], tuple(denied)

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def body(b):
    return StreamingBody(io.BytesIO(b), len(b))


class LagLookupTests(unittest.TestCase):
    def test_84_name_lookup_not_position(self):
        self.assertEqual(L.lag_from_replica_status(COLS_84, ("", "", "", 0, 7, "")), (7.0, "Seconds_Behind_Source"))

    def test_master_name(self):
        row = ["x"] * 32 + ["12"]
        self.assertEqual(L.lag_from_replica_status(COLS_57, row), (12.0, "Seconds_Behind_Master"))

    def test_null_is_none_not_zero(self):
        self.assertEqual(L.lag_from_replica_status(COLS_84, ("", "", "", 0, None, "")), (None, "Seconds_Behind_Source"))
        self.assertEqual(soak_check.lag_from_replica_status(COLS_84, ["", "", "", "0", "NULL", ""]), (None, "Seconds_Behind_Source"))

    def test_missing_column(self):
        self.assertEqual(L.lag_from_replica_status(["a", "b"], (1, 2)), (None, None))

    def test_lambda_measure_uses_cursor_description(self):
        conn = FakeConn()
        cfg = {"mysql_replica_status_side": "target"}
        self.assertEqual(L.measure_replication_lag(cfg, "mysql", FakeConn(), conn), (7.0, "mysql_replica_status"))

    def test_standalone_measure_uses_header_line(self):
        out = ["\t".join(COLS_84), "\t".join(["Waiting", "h", "u", "3306", "4", ""])]
        with mock.patch.object(soak_check, "run_one", return_value=out) as ro:
            got = soak_check.measure_replication_lag({"mysql_replica_status_side": "target", "target": {}, "source": {}},
                                                     "mysql", None, None)
        self.assertEqual(got, (4.0, "mysql_replica_status"))
        self.assertTrue(ro.call_args.kwargs.get("headers"))

    def test_scripts_agree(self):
        for cols, row in ((COLS_84, ["a", "b", "c", "1", "9", ""]), (COLS_57, ["x"] * 32 + ["3"])):
            self.assertEqual(L.lag_from_replica_status(cols, row), soak_check.lag_from_replica_status(cols, row))


class RegionTests(unittest.TestCase):
    def test_explicit(self):
        self.assertEqual(soak_check.aws_region({"region": "ap-northeast-2"}), "ap-northeast-2")

    def test_derived_from_rds_endpoint(self):
        cfg = {"target": {"host": "db.abc123.ap-northeast-2.rds.amazonaws.com"}, "source": {"host": "127.0.0.1"}}
        self.assertEqual(soak_check.aws_region(cfg), "ap-northeast-2")

    def test_missing_fails_clearly_never_us_east_1(self):
        with self.assertRaises(ValueError):
            soak_check.aws_region({"target": {"host": "127.0.0.1"}, "source": {"host": "10.0.0.1"}})


class Stubbed(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        pf = L._preflight_clients()
        self.stubs = {n: Stubber(getattr(L, n)) for n in ("_secrets", "_cloudwatch", "_rds", "_s3", "_dms")}
        self.stubs.update({f"pf_{n}": Stubber(getattr(pf, n)) for n in ("secrets", "cloudwatch", "rds", "s3", "dms")})
        for s in self.stubs.values():
            s.activate()

    def tearDown(self):
        for s in self.stubs.values():
            s.deactivate()
        self.env.stop()

    def secret_ok(self, arn, key="pf_secrets"):
        self.stubs[key].add_response("get_secret_value",
                                     {"SecretString": json.dumps({"username": "ro", "password": "p"})},
                                     {"SecretId": arn})

    def pf_aws_ok(self, dms_metrics=("CDCLatencyTarget", "CDCLatencySource"), dms_points=None):
        dms_points = [{"Maximum": 1.0, "Timestamp": datetime.datetime(2026, 10, 5)}] if dms_points is None else dms_points
        cw = self.stubs["pf_cloudwatch"]
        cw.add_response("describe_alarms", {"MetricAlarms": [{"AlarmName": "tgt-cpu", "StateValue": "OK"}]})
        cw.add_response("get_metric_statistics", {"Datapoints": []})  # RDS FreeStorageSpace
        for m in dms_metrics:
            cw.add_response("get_metric_statistics", {"Datapoints": dms_points},
                            {"Namespace": "AWS/DMS", "MetricName": m, "Dimensions": ANY, "StartTime": ANY,
                             "EndTime": ANY, "Period": 300, "Statistics": ["Maximum"]})
        self.stubs["pf_rds"].add_response("describe_db_instances", {"DBInstances": [{"Engine": "mysql", "AllocatedStorage": 100}]})
        self.stubs["pf_dms"].add_response("describe_replication_tasks", {"ReplicationTasks": []})

    def s3_get(self, key, data=b"{}", etag='"e1"', extra=None):
        resp = {"Body": body(data), "ETag": etag}
        resp.update(extra or {})
        self.stubs["pf_s3"].add_response("get_object", resp, {"Bucket": BUCKET, "Key": key})

    def s3_put_probe(self, key, code, status, data=b"{}", ctype="application/json"):
        self.stubs["pf_s3"].add_client_error(
            "put_object", code, code, status,
            expected_params={"Bucket": BUCKET, "Key": key, "Body": data, "ContentType": ctype, "IfMatch": L._WRONG_ETAG})

    def reports_probe(self, code="NoSuchKey", status=404):
        self.stubs["pf_s3"].add_client_error("put_object", code, code, status, expected_params={
            "Bucket": BUCKET, "Key": "reports/soak-report-day1.md", "Body": b"", "ContentType": "text/markdown",
            "IfMatch": L._WRONG_ETAG})


class FakeContext:
    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


class PreflightTests(Stubbed):
    def run_pf(self, conns=None, context=None):
        conns = conns or [FakeConn(), FakeConn()]
        with mock.patch.object(L, "_connect", side_effect=conns), mock.patch("builtins.print"):
            out = L.handler({"mode": "preflight"}, context)
        for s in self.stubs.values():
            s.assert_no_pending_responses()
        return out, {r["check"]: r for r in out["checks"]}

    def test_happy_path_all_pass_and_nothing_written(self):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        out, rows = self.run_pf()
        self.assertTrue(out["ok"], [r for r in out["checks"] if r["result"] != "PASS"])
        for t in ("shop.orders", "shop.payments"):  # EVERY table, both sides
            for side in ("source", "target"):
                self.assertEqual(rows[f"{side}_db_select {t}"]["result"], "PASS")
                self.assertEqual(rows[f"{side}_db_checksum {t}"]["result"], "PASS")
        self.assertIn("dms_lag_metric CDCLatencyTarget", rows)
        self.assertIn("dms_lag_metric CDCLatencySource", rows)
        self.assertEqual(rows["target_db_replica_status"]["result"], "PASS")
        # Watermark parity: every table probed on both sides, mode reported in the message.
        for t in ("shop.orders", "shop.payments"):
            for side in ("source", "target"):
                row = rows[f"{side}_db_watermark {t}"]
                self.assertEqual(row["result"], "PASS")
                self.assertIn("whole-table comparison", row["message"])  # FakeConn exposes no PK

    def test_second_table_denied_and_status_put_denied_are_not_ok(self):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "AccessDenied", 403)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe("PreconditionFailed", 412)
        out, rows = self.run_pf([FakeConn(), FakeConn(denied=["payments"])])
        self.assertFalse(out["ok"])
        self.assertEqual(rows["source_db_select shop.payments"]["result"], "PASS")
        self.assertEqual(rows["target_db_select shop.payments"]["result"], "Error")
        self.assertIn("denied", rows["target_db_select shop.payments"]["message"])
        self.assertEqual(rows["target_db_select shop.orders"]["result"], "PASS")
        self.assertEqual(rows["s3_put status.json"]["result"], "AccessDenied")
        self.assertEqual(rows["s3_put status.json"]["iam_action"], "s3:PutObject")
        self.assertEqual(rows["s3_put status.json"]["resource"], f"arn:aws:s3:::{BUCKET}/status.json")
        self.assertEqual(rows["s3_put activity-log.jsonl"]["result"], "PASS")

    def test_kms_secret_denial_names_kms_action_and_key_separately(self):
        self.secret_ok(SRC_ARN)
        self.stubs["pf_secrets"].add_client_error(
            "get_secret_value", "AccessDeniedException", "Access to KMS is not allowed", 400,
            expected_params={"SecretId": TGT_ARN})
        self.pf_aws_ok()
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        out, rows = self.run_pf([FakeConn()])
        r = rows["target_secret"]
        self.assertEqual((r["result"], r["iam_action"], r["resource"]), ("AccessDenied", "kms:Decrypt", TGT_ARN))
        self.assertIn("kms_key", r)
        self.assertEqual(rows["target_db_connect"]["result"], "SKIPPED")

    def test_absent_key_is_unverified_not_created(self):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.stubs["pf_s3"].add_client_error("get_object", "NoSuchKey", "x", 404,
                                             expected_params={"Bucket": BUCKET, "Key": "status.json"})
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        out, rows = self.run_pf()  # no put_object stub for status.json: any create attempt would fail the stub
        self.assertEqual(rows["s3_put status.json"]["result"], "UNVERIFIED")
        self.assertFalse(out["ok"])

    def test_sse_kms_write_is_flagged_unverified(self):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        kms = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "arn:aws:kms:ap-northeast-2:111122223333:key/k1"}
        self.s3_get("status.json", extra=kms)
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"', extra=kms)
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        out, rows = self.run_pf()
        self.assertEqual(rows["s3_put status.json"]["result"], "UNVERIFIED")
        self.assertIn("kms:GenerateDataKey", rows["s3_put status.json"]["message"])

    def test_time_budget_skips_instead_of_timing_out(self):
        out, rows = self.run_pf(context=FakeContext(1000))  # no stubbed responses: nothing may be called
        self.assertTrue(rows)
        self.assertTrue(all(r["result"] == "SKIPPED" for r in out["checks"]), out["checks"])
        self.assertFalse(out["ok"])

    def test_preflight_clients_are_tight(self):
        cfg = L._PREFLIGHT_BOTO_CONFIG
        self.assertEqual((cfg.connect_timeout, cfg.read_timeout, cfg.retries["total_max_attempts"]), (3, 5, 2))

    def test_network_timeout_is_not_reported_as_iam(self):
        with mock.patch.object(L._secrets, "get_secret_value",
                               side_effect=EndpointConnectionError(endpoint_url="https://secretsmanager.ap-northeast-2.amazonaws.com")):
            row, _ = L._preflight_row("source_secret", "secretsmanager:GetSecretValue", SRC_ARN,
                                      lambda: L._get_secret(SRC_ARN))
        self.assertEqual(row["result"], "Error")
        self.assertIn("NOT an IAM problem", row["message"])


class KmsMappingTests(unittest.TestCase):
    def err(self, code, msg):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": msg}}, "Op")

    def test_parsed_action_and_key_from_message(self):
        e = L.AwsCallError("s3_write", "s3:PutObject", "arn:aws:s3:::b/status.json", self.err(
            "AccessDenied", "User: arn:aws:sts::1:assumed-role/r/f is not authorized to perform: kms:GenerateDataKey "
                            "on resource: arn:aws:kms:ap-northeast-2:111122223333:key/abcd-1234 because no policy"))
        d = e.as_dict()
        self.assertEqual(d["iam_action"], "kms:GenerateDataKey")
        self.assertEqual(d["kms_key"], "arn:aws:kms:ap-northeast-2:111122223333:key/abcd-1234")
        self.assertEqual(d["resource"], "arn:aws:s3:::b/status.json")

    def test_inferred_write_vs_read(self):
        w = L.AwsCallError("s3_write", "s3:PutObject", "r", self.err("AccessDenied", "KMS access denied"))
        r = L.AwsCallError("secret", "secretsmanager:GetSecretValue", "r", self.err("AccessDeniedException", "Access to KMS is not allowed"))
        self.assertEqual(w.iam_action, "kms:GenerateDataKey")
        self.assertEqual(r.iam_action, "kms:Decrypt")

    def test_plain_s3_denial_keeps_s3_action(self):
        e = L.AwsCallError("s3_write", "s3:PutObject", "r", self.err("AccessDenied", "Access Denied"))
        self.assertEqual((e.iam_action, e.kms_key), ("s3:PutObject", None))


class PresignFailureTests(unittest.TestCase):
    def test_redaction_of_signing_material(self):
        raw = ("<Error><Code>SignatureDoesNotMatch</Code><Message>bad sig X-Amz-Signature=deadbeef "
               "key ASIAABCDEFGHIJKLMNOP</Message><StringToSign>AWS4-HMAC-SHA256 secret-sts</StringToSign>"
               "<CanonicalRequest>GET /x X-Amz-Credential=ASIAABCDEFGHIJKLMNOP%2F&amp;X-Amz-Security-Token=FwoGZXIvYXdz"
               "</CanonicalRequest><Region>ap-northeast-2</Region></Error>")
        out = presign.summarize_s3_error(403, raw)
        self.assertIn("HTTP 403", out)
        self.assertIn("Code=SignatureDoesNotMatch", out)
        self.assertIn("Region=ap-northeast-2", out)
        for leaked in ("deadbeef", "ASIAABCDEFGHIJKLMNOP", "FwoGZXIvYXdz", "StringToSign", "secret-sts", "CanonicalRequest"):
            self.assertNotIn(leaked, out)

    def test_failed_verify_suppresses_customer_link(self):
        client = mock.MagicMock()
        client.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f"https://b.example/{Params['Key']}?X-Amz-Signature=s"
        verdict = lambda url, timeout=20: ((False, 400, "HTTP 400 Code=AuthorizationQueryParametersError")
                                           if "status.json" in url else (True, 200, ""))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(presign, "resolve_bucket_region", return_value="ap-northeast-2"), \
             mock.patch.object(presign, "make_signing_client", return_value=client), \
             mock.patch.object(presign, "verify_url", side_effect=verdict), \
             mock.patch.object(sys, "argv", ["x", "--bucket", "b", "--expires-seconds", "60"]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                presign.main()
        self.assertNotIn(cm.exception.code, (0, None))
        self.assertNotIn("CUSTOMER LINK", out.getvalue())
        self.assertIn("AuthorizationQueryParametersError", err.getvalue())

    def test_region_mismatch_exits(self):
        with mock.patch.object(presign, "resolve_bucket_region", return_value="ap-northeast-2"), \
             mock.patch.object(sys, "argv", ["x", "--bucket", "b", "--region", "us-east-1"]):
            with self.assertRaises(SystemExit) as cm:
                presign.main()
        self.assertIn("does not match", str(cm.exception.code))


class StandaloneAwsCliTests(unittest.TestCase):
    def test_timeout_is_captured_as_needs_review(self):
        errs = []
        with mock.patch.object(soak_check.subprocess, "run", side_effect=subprocess.TimeoutExpired(["aws"], 30)), \
             contextlib.redirect_stderr(io.StringIO()):
            out = soak_check._aws_cli(["aws"], "alarms", "cloudwatch:DescribeAlarms", "a", errs)
        self.assertEqual(out, "")
        self.assertEqual((errs[0]["result"], errs[0]["iam_action"]), ("Error", "cloudwatch:DescribeAlarms"))
        self.assertIn("timed out", errs[0]["message"])
        self.assertIn("not IAM", errs[0]["message"])

    def test_missing_cli_is_captured(self):
        errs = []
        with mock.patch.object(soak_check.subprocess, "run", side_effect=FileNotFoundError("aws")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(soak_check._aws_cli(["aws"], "x", "rds:DescribeDBInstances", "db:x", errs), "")
        self.assertIn("could not run the aws CLI", errs[0]["message"])

    def test_alarms_none_on_cli_failure(self):
        errs = []
        with mock.patch.object(soak_check.subprocess, "run", side_effect=subprocess.TimeoutExpired(["aws"], 30)), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(soak_check.cloudwatch_alarms(["a"], "ap-northeast-2", errs)[2])


class NormalModeTests(Stubbed):
    def test_metric_access_denied_becomes_needs_review_with_action(self):
        self.stubs["_rds"].add_response("describe_db_instances", {"DBInstances": [{"Engine": "mysql", "AllocatedStorage": 100}]})
        self.stubs["_cloudwatch"].add_client_error("get_metric_statistics", "AccessDenied", "nope", 403)
        errs = []
        self.assertIsNone(L.db_headroom_pct("tgt-db", errs))
        self.assertEqual(errs[0]["iam_action"], "cloudwatch:GetMetricStatistics")
        self.assertEqual(errs[0]["result"], "AccessDenied")

    def test_alarm_denied_is_none_not_pass(self):
        self.stubs["_cloudwatch"].add_client_error("describe_alarms", "AccessDenied", "nope", 403)
        errs = []
        firing, unknown, check = L.cloudwatch_alarms(["a"], errs)
        self.assertIsNone(check)
        self.assertEqual(errs[0]["resource"], "a")

    def test_s3_put_denied_raises_with_action_and_resource(self):
        self.stubs["_s3"].add_response("get_object", {"Body": body(b"{}"), "ETag": '"e"'})
        self.stubs["_s3"].add_client_error("put_object", "AccessDenied", "denied", 403)
        day = {"date": datetime.date.today().isoformat(), "overall": "green", "checks": {}, "needs_agent_review": False}
        with self.assertRaises(L.AwsCallError) as cm:
            L.update_status_json(BUCKET, "status.json", day, 3)
        self.assertEqual(cm.exception.iam_action, "s3:PutObject")
        self.assertEqual(cm.exception.resource, f"arn:aws:s3:::{BUCKET}/status.json")

    def test_cas_retry_still_works(self):
        self.stubs["_s3"].add_response("get_object", {"Body": body(b"{}"), "ETag": '"e1"'})
        self.stubs["_s3"].add_client_error("put_object", "PreconditionFailed", "lost race", 412)
        self.stubs["_s3"].add_response("get_object", {"Body": body(b"{}"), "ETag": '"e2"'})
        self.stubs["_s3"].add_response("put_object", {}, {"Bucket": BUCKET, "Key": "status.json", "Body": ANY,
                                                          "ContentType": "application/json", "IfMatch": '"e2"'})
        day = {"date": datetime.date.today().isoformat(), "overall": "green", "checks": {}, "needs_agent_review": False}
        with mock.patch.object(L, "_cas_jitter_sleep"):
            st = L.update_status_json(BUCKET, "status.json", day, 3)
        self.assertEqual(len(st["soak"]["days"]), 1)

    def test_handler_secret_denied_logs_and_raises(self):
        self.stubs["_secrets"].add_client_error("get_secret_value", "AccessDeniedException", "not authorized", 400)
        with self.assertRaises(L.AwsCallError), mock.patch("builtins.print") as pr:
            L.handler({}, None)
        logged = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("SOAK_CHECK_ERROR", logged)
        self.assertIn("secretsmanager:GetSecretValue", logged)
        self.assertIn(SRC_ARN, logged)

    def test_cas_409_conflict_is_retried(self):
        self.stubs["_s3"].add_response("get_object", {"Body": body(b"{}"), "ETag": '"e1"'})
        self.stubs["_s3"].add_client_error("put_object", "ConditionalRequestConflict", "conflict", 409)
        self.stubs["_s3"].add_response("get_object", {"Body": body(b"{}"), "ETag": '"e2"'})
        self.stubs["_s3"].add_response("put_object", {}, {"Bucket": BUCKET, "Key": "status.json", "Body": ANY,
                                                          "ContentType": "application/json", "IfMatch": '"e2"'})
        day = {"date": datetime.date.today().isoformat(), "overall": "green", "checks": {}, "needs_agent_review": False}
        with mock.patch.object(L, "_cas_jitter_sleep"):
            L.update_status_json(BUCKET, "status.json", day, 3)
        self.stubs["_s3"].assert_no_pending_responses()

    def test_creation_path_uses_if_none_match(self):
        self.stubs["_s3"].add_client_error("get_object", "NoSuchKey", "x", 404)
        self.stubs["_s3"].add_response("put_object", {}, {"Bucket": BUCKET, "Key": "activity-log.jsonl", "Body": ANY,
                                                          "ContentType": "application/x-ndjson", "IfNoneMatch": "*"})
        day = {"date": datetime.date.today().isoformat(), "overall": "red", "checks": {}, "needs_agent_review": True}
        L.append_activity_log(BUCKET, "activity-log.jsonl", day)
        self.stubs["_s3"].assert_no_pending_responses()

    def test_empty_optional_env_means_not_configured(self):
        with mock.patch.dict(os.environ, {"DMS_TASK_ID": ""}):
            cfg = L._load_config()
        self.assertIsNone(cfg["dms_task_id"])
        self.assertIsNone(cfg["pg_replication_lag_side"])



class FakeTsDb:
    """One side holding `shop`.`orders` rows (id, created_at-or-None); no integer PK, so the
    configured timestamp column drives the watermark. Answers the timestamp-path SQL."""
    CUTOFF = "2026-10-05 12:00:00.000000"

    def __init__(self, rows, col_type="datetime", nullable="YES", has_col=True):
        self.rows, self.col_type, self.nullable, self.has_col, self.executed = list(rows), col_type, nullable, has_col, []

    def _lit(self, sql):
        import re as _re
        m = _re.search(r"X'([0-9a-f]+)'", sql)
        return bytes.fromhex(m.group(1)).decode() if m else None

    def _select(self, sql):
        if "1=0" in sql:
            return []
        where = sql.split(" WHERE ", 1)[1] if " WHERE " in sql else None
        if where is None:
            return self.rows
        x = self._lit(where)
        if "<=" in where and "IS NULL" in where:
            return [r for r in self.rows if r[1] is None or r[1] <= x]
        if "IS NULL" in where:
            return [r for r in self.rows if r[1] is None]
        if "<=" in where:
            return [r for r in self.rows if r[1] is not None and r[1] <= x]
        if ">" in where:
            return [r for r in self.rows if r[1] is not None and r[1] > x]
        raise AssertionError(sql)

    def answer(self, sql):
        self.executed.append(sql)
        up = sql.strip().upper()
        if "KEY_COLUMN_USAGE" in up:
            return [("code", "varchar")]
        if up.startswith("SELECT COLUMN_NAME"):
            cols = [("id", "bigint", "NO", None)]
            if self.has_col:
                cols.append(("created_at", self.col_type, self.nullable, None))
            return cols
        if up.startswith("SELECT DATE_FORMAT(NOW"):
            return [(self.CUTOFF,)]
        if up.startswith("SELECT COUNT(*)"):
            return [(len(self._select(sql)),)]
        if up.startswith("SELECT CONCAT(COUNT(*)"):
            sel = self._select(sql[sql.index("FROM `shop`"):].rsplit(") w", 1)[0]) if "1=0" not in sql else []
            return [(f"{len(sel)}:{sorted((r[0], str(r[1])) for r in sel)}",)]
        if up.startswith("CHECKSUM TABLE"):
            return [("shop.orders", hash(tuple(sorted((r[0], str(r[1])) for r in self.rows))))]
        return []


TS_CFG = {"source_engine": "mysql", "target_engine": "mysql", "tables": ["shop.orders"],
          "checksum_tables": ["shop.orders"], "watermark": {"timestamp_columns": {"shop.orders": "created_at"}, "timestamp_age_minutes": 15}}


def ts_rows(n_old=100, n_new=0, nulls=()):
    rows = [(i, f"2026-10-05 10:{i % 60:02d}:00") for i in range(1, n_old + 1)]
    rows += [(1000 + i, f"2026-10-05 12:{i:02d}:30") for i in range(n_new)]
    rows += [(5000 + i, None) for i in nulls]
    return rows


class TimestampWatermarkTests(unittest.TestCase):
    def lam(self, src, tgt):
        return L.run_day(dict(TS_CFG), DbConn(src), DbConn(tgt))

    def std(self, src, tgt):
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
               "tables": ["shop.orders"], "checksum_tables": ["shop.orders"], "watermark": TS_CFG["watermark"]}
        fake = WatermarkStandaloneTests.fake_batch(None, {"s": src, "t": tgt})
        with mock.patch.object(soak_check, "run_batch", side_effect=fake):
            return soak_check.run_day(cfg)

    def both(self, mk_src, mk_tgt):
        return [f(mk_src(), mk_tgt()) for f in (self.lam, self.std)]

    def test_lagging_recent_rows_and_symmetric_nulls_pass(self):
        for day in self.both(lambda: FakeTsDb(ts_rows(100, 5, nulls=(1, 2))), lambda: FakeTsDb(ts_rows(100, 2, nulls=(1, 2)))):
            d = day["detail"]["row_count"]["shop.orders"]
            self.assertEqual(d["mode"], "timestamp_watermark")
            self.assertEqual(d["null_rows"], {"source": 2, "target": 2})
            self.assertEqual((d["source"], d["target"]), (102, 102))   # NULL rows are in the compared set
            self.assertEqual(d["tail_rows"], {"source": 5, "target": 2})
            self.assertIs(day["checks"]["row_count"], True)
            self.assertIs(day["checks"]["checksum"], True)

    def test_asymmetric_nulls_fail_even_when_totals_match(self):
        # Source row 5000 has NULL; on the target the same row carries an old timestamp:
        # bounded totals match, so only the explicit NULL comparison catches it.
        src = lambda: FakeTsDb(ts_rows(100, nulls=(0,)))
        tgt = lambda: FakeTsDb(ts_rows(100) + [(5000, "2026-10-05 09:00:00")])
        for day in self.both(src, tgt):
            d = day["detail"]["row_count"]["shop.orders"]
            self.assertEqual((d["source"], d["target"]), (101, 101))
            self.assertEqual(d["null_rows"], {"source": 1, "target": 0})
            self.assertIs(day["checks"]["row_count"], False)

    def test_not_null_column_issues_no_null_query(self):
        src, tgt = FakeTsDb(ts_rows(50), nullable="NO"), FakeTsDb(ts_rows(50), nullable="NO")
        day = self.lam(src, tgt)
        self.assertNotIn("null_rows", day["detail"]["row_count"]["shop.orders"])
        self.assertFalse(any("IS NULL" in q for q in src.executed))

    def test_column_missing_on_one_side_falls_back(self):
        for day in self.both(lambda: FakeTsDb(ts_rows(10)), lambda: FakeTsDb(ts_rows(10), has_col=False)):
            d = day["detail"]["row_count"]["shop.orders"]
            self.assertEqual(d["mode"], "whole_table")
            self.assertIn("not found on target", d["note"])

    def test_type_mismatch_falls_back(self):
        day = self.lam(FakeTsDb(ts_rows(10)), FakeTsDb(ts_rows(10), col_type="varchar(30)"))
        self.assertEqual(day["detail"]["row_count"]["shop.orders"]["mode"], "whole_table")
        self.assertIn("same date/time type", day["detail"]["row_count"]["shop.orders"]["note"])

    def test_scripts_agree(self):
        a, b = self.both(lambda: FakeTsDb(ts_rows(100, 3, nulls=(1,))), lambda: FakeTsDb(ts_rows(100, 1, nulls=(1,))))
        self.assertEqual(a["detail"]["row_count"], b["detail"]["row_count"])
        self.assertEqual(a["checks"], b["checks"])

    def test_resolve_mode_is_joint(self):
        cols = {"created_at": {"type": "timestamp", "nullable": "NO"}}
        nullable_t = {"created_at": {"type": "timestamp", "nullable": "YES"}}
        w = L.watermark_config(TS_CFG)
        self.assertEqual(L.resolve_mode("shop.orders", w, [], [], cols, nullable_t), ("timestamp", "created_at", True))
        self.assertEqual(L.resolve_mode("shop.orders", w, [], [], cols, {})[0], None)
        self.assertEqual(L.check_timestamp_column("c", {"c": {"type": "timestamp with time zone", "nullable": "NO"}},
                                                  {"c": {"type": "timestamp without time zone", "nullable": "NO"}})[0], False)


class TimestampPreflightTests(Stubbed):
    def test_missing_timestamp_column_is_not_ok(self):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        with mock.patch.dict(os.environ, {"WATERMARK_TIMESTAMP_COLUMNS": '{"shop.orders": "created_at"}'}), \
             mock.patch.object(L, "_connect", side_effect=[FakeConn(), FakeConn()]), mock.patch("builtins.print"):
            out = L.handler({"mode": "preflight"}, None)
        rows = {r["check"]: r for r in out["checks"]}
        self.assertFalse(out["ok"])
        for side in ("source", "target"):
            self.assertEqual(rows[f"{side}_db_watermark shop.orders"]["result"], "Error")
            self.assertIn("created_at", rows[f"{side}_db_watermark shop.orders"]["message"])
        self.assertEqual(rows["source_db_watermark shop.payments"]["result"], "PASS")


class PgTimeoutTests(unittest.TestCase):
    def test_connect_bounded_then_query_timeout_applied_to_socket(self):
        fake_pg = mock.MagicMock()
        conn = fake_pg.Connection.return_value
        with mock.patch.object(L, "pg8000", fake_pg), mock.patch.object(L, "_tls_context", return_value=None):
            got = L._connect("postgres", "db.example", 5432, {"username": "u", "password": "p"}, "d",
                             connect_timeout=5, read_timeout=300)
        self.assertIs(got, conn)
        self.assertEqual(fake_pg.Connection.call_args.kwargs["timeout"], 5)   # connect + TLS + auth
        conn._usock.settimeout.assert_called_once_with(300)                  # every later read

    def test_real_pg8000_exposes_the_socket_attribute(self):
        import pg8000.core as core, inspect as _inspect
        self.assertIn("self._usock", _inspect.getsource(core.CoreConnection.__init__))

    def test_missing_socket_fails_loudly(self):
        fake_pg = mock.MagicMock()
        fake_pg.Connection.return_value = mock.MagicMock(spec=["close"])
        with mock.patch.object(L, "pg8000", fake_pg), mock.patch.object(L, "_tls_context", return_value=None):
            with self.assertRaises(RuntimeError):
                L._connect("postgres", "h", 5432, {"username": "u", "password": "p"}, "d", 5, 300)


class StreakOrderTests(Stubbed):
    def test_out_of_order_late_retry_keeps_newer_streak(self):
        def day(d):
            return {"date": d, "overall": "green", "needs_agent_review": False, "checks": {"row_count": True}}
        existing = {"soak": {"days": [day("2026-10-05"), day("2026-10-06")], "n_total": 3,
                             "consecutive_green": 2, "state": "active"}}
        self.stubs["_s3"].add_response("get_object", {"Body": body(json.dumps(existing).encode()), "ETag": '"e1"'})
        self.stubs["_s3"].add_response("put_object", {}, {"Bucket": BUCKET, "Key": "status.json", "Body": ANY,
                                                          "ContentType": "application/json", "IfMatch": '"e1"'})
        st = L.update_status_json(BUCKET, "status.json", day("2026-10-05"), 3)   # late retry of Oct 5
        self.assertEqual(st["soak"]["consecutive_green"], 2)
        self.assertEqual([d["date"] for d in st["soak"]["days"]], ["2026-10-05", "2026-10-06"])



class DmsDimensionTests(Stubbed):
    def test_id_normalization(self):
        arn = "arn:aws:dms:ap-northeast-2:111122223333:task:CPSTBQCAAFB67LEICTHDNETPSU"
        self.assertEqual(L.dms_metric_task_id(arn, None), ("CPSTBQCAAFB67LEICTHDNETPSU", None))
        self.assertEqual(L.dms_metric_task_id("CPSTBQCAAFB67LEICTHDNETPSU", arn), ("CPSTBQCAAFB67LEICTHDNETPSU", None))
        tid, prob = L.dms_metric_task_id("dryrun4-kiro-fwd-cdc", None)
        self.assertIn("friendly task name", prob)
        tid, prob = L.dms_metric_task_id("dryrun4-kiro-fwd-cdc", arn)
        self.assertEqual(tid, "CPSTBQCAAFB67LEICTHDNETPSU")
        self.assertIn("not the task's resource id", prob)
        custom = "arn:aws:dms:ap-northeast-2:111122223333:task:my-custom-fwd1"   # ResourceIdentifier set
        self.assertEqual(L.dms_metric_task_id(custom, None), ("my-custom-fwd1", None))
        self.assertEqual(L.dms_metric_task_id("my-custom-fwd1", custom), ("my-custom-fwd1", None))
        for v in ("dryrun4-kiro-fwd-cdc", arn, "X1", custom):
            self.assertEqual(L.dms_metric_task_id(v, None), soak_check.dms_metric_task_id(v, None))

    def run_pf(self, env, dms_points):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok(dms_points=dms_points)
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        with mock.patch.dict(os.environ, env), mock.patch.object(L, "_connect", side_effect=[FakeConn(), FakeConn()]), \
             mock.patch("builtins.print"):
            out = L.handler({"mode": "preflight"}, None)
        return out, {r["check"]: r for r in out["checks"]}

    def test_preflight_empty_dms_datapoints_is_error_not_pass(self):
        out, rows = self.run_pf({}, [])
        self.assertFalse(out["ok"])
        for m in ("CDCLatencyTarget", "CDCLatencySource"):
            self.assertEqual(rows[f"dms_lag_metric {m}"]["result"], "Error")
            self.assertIn("resource-id suffix", rows[f"dms_lag_metric {m}"]["message"])

    def test_preflight_flags_friendly_id(self):
        out, rows = self.run_pf({"DMS_TASK_ID": "dryrun4-kiro-fwd-cdc", "DMS_TASK_ARN": ""}, None)
        self.assertFalse(out["ok"])
        self.assertEqual(rows["dms_task_id"]["result"], "Error")
        self.assertIn("friendly task name", rows["dms_task_id"]["message"])

    def test_normal_run_empty_datapoints_needs_review_with_hint(self):
        self.stubs["_cloudwatch"].add_response("get_metric_statistics", {"Datapoints": []})
        errs = []
        cfg = {"dms_task_id": "ABC", "dms_replication_instance_id": "ri-1", "dms_task_arn": TASK_ARN}
        self.assertEqual(L.measure_replication_lag(cfg, "mysql", None, None, errs), (None, "dms"))
        self.assertIn("resource-id suffix", errs[0]["message"])

    def test_normal_run_uses_arn_suffix_dimension(self):
        self.stubs["_cloudwatch"].add_response(
            "get_metric_statistics", {"Datapoints": [{"Maximum": 2.0}]},
            {"Namespace": "AWS/DMS", "MetricName": "CDCLatencyTarget", "StartTime": ANY, "EndTime": ANY,
             "Period": 300, "Statistics": ["Maximum"],
             "Dimensions": [{"Name": "ReplicationInstanceIdentifier", "Value": "ri-1"},
                            {"Name": "ReplicationTaskIdentifier", "Value": "ABC"}]})
        self.stubs["_cloudwatch"].add_response("get_metric_statistics", {"Datapoints": [{"Maximum": 1.0}]})
        errs = []
        cfg = {"dms_task_id": "my-friendly-task", "dms_replication_instance_id": "ri-1", "dms_task_arn": TASK_ARN}
        self.assertEqual(L.measure_replication_lag(cfg, "mysql", None, None, errs), (2.0, "dms"))
        self.assertIn("not the task's resource id", errs[0]["message"])

    def test_standalone_empty_datapoints_hint(self):
        errs = []
        cfg = {"dms_task_id": "ABC", "dms_replication_instance_id": "ri-1", "region": "ap-northeast-2"}
        with mock.patch.object(soak_check, "_aws_cli", return_value="[]"):
            self.assertEqual(soak_check.measure_replication_lag(cfg, "mysql", None, None, errs), (None, "dms"))
        self.assertIn("resource-id suffix", errs[0]["message"])



class SoakShapeTests(Stubbed):
    DAY = {"date": "2026-10-06", "overall": "green", "needs_agent_review": False, "checks": {"row_count": True}}

    def test_shapes_both_scripts(self):
        for mod in (L, soak_check):
            st = {"phases": []}
            self.assertEqual(mod.ensure_soak_shape(st, 3), ["soak", "soak.days", "soak.n_total", "soak.consecutive_green", "soak.state"])
            self.assertEqual(st["soak"], {"days": [], "n_total": 3, "consecutive_green": 0, "state": "active"})
            st = {"soak": {"n_total": 1, "started_at": "x"}}
            self.assertEqual(mod.ensure_soak_shape(st, 3), ["soak.days", "soak.consecutive_green", "soak.state"])
            self.assertEqual(st["soak"]["n_total"], 1)
            self.assertEqual(st["soak"]["started_at"], "x")
            for bad in ([], {"soak": []}, {"soak": "x"}, {"soak": {"days": {}}}, {"soak": {"days": [{"overall": "green"}]}},
                        {"soak": {"days": [{"date": "2026-10-99"}]}}, {"soak": {"days": [{"date": "2026-02-30"}]}},
                        {"soak": {"days": [{"date": "2026-10-06", "checks": []}]}},
                        {"soak": {"days": [{"date": "2026-10-06", "needs_agent_review": "no"}]}}):
                with self.assertRaises(mod.StatusShapeError):
                    mod.ensure_soak_shape(bad, 3)
            with self.assertRaises(mod.StatusShapeError):
                mod._parse_status(b"{not json" if mod is L else "{not json")

    def test_lambda_writer_creates_missing_days(self):
        seeded = {"phases": [], "soak": {"n_total": 3, "started_at": "2026-10-05T23:30:00Z", "state": "active"}}
        self.stubs["_s3"].add_response("get_object", {"Body": body(json.dumps(seeded).encode()), "ETag": '"e1"'})
        self.stubs["_s3"].add_response("put_object", {}, {"Bucket": BUCKET, "Key": "status.json", "Body": ANY,
                                                          "ContentType": "application/json", "IfMatch": '"e1"'})
        st = L.update_status_json(BUCKET, "status.json", dict(self.DAY), 3)
        self.assertEqual([d["date"] for d in st["soak"]["days"]], ["2026-10-06"])
        self.assertEqual(st["soak"]["started_at"], "2026-10-05T23:30:00Z")
        self.assertEqual(st["phases"], [])

    def test_lambda_writer_refuses_malformed(self):
        self.stubs["_s3"].add_response("get_object", {"Body": body(b'{"soak": []}'), "ETag": '"e1"'})
        with self.assertRaises(L.StatusShapeError):
            L.update_status_json(BUCKET, "status.json", dict(self.DAY), 3)

    def test_standalone_writer_creates_missing_days(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "status.json"
            path.write_text(json.dumps({"soak": {"n_total": 1}}))
            soak_check.update_status_json(path, dict(self.DAY), 1)
            out = json.loads(path.read_text())
            self.assertEqual(len(out["soak"]["days"]), 1)
            path.write_text('{"soak": {"days": "oops"}}')
            with self.assertRaises(soak_check.StatusShapeError):
                soak_check.update_status_json(path, dict(self.DAY), 1)

    def _preflight(self, status_body):
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.s3_get("status.json", status_body)
        self.s3_put_probe("status.json", "PreconditionFailed", 412, status_body)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        with mock.patch.object(L, "_connect", side_effect=[FakeConn(), FakeConn()]), mock.patch("builtins.print"):
            out = L.handler({"mode": "preflight"}, None)
        return out, {r["check"]: r for r in out["checks"]}

    def test_preflight_reports_missing_days_as_pass_with_note(self):
        out, rows = self._preflight(b'{"phases": []}')
        self.assertTrue(out["ok"], [r for r in out["checks"] if r["result"] != "PASS"])
        self.assertIn("soak.days", rows["status_json_shape"]["message"])

    def test_lambda_writer_bad_date_fails_cleanly_on_retry_read(self):
        self.stubs["_s3"].add_response("get_object", {"Body": body(b'{"soak": {"days": []}}'), "ETag": '"e1"'})
        self.stubs["_s3"].add_client_error("put_object", "PreconditionFailed", "lost race", 412)
        # the concurrent writer left an invalid date — the re-read must be re-validated
        self.stubs["_s3"].add_response("get_object", {"Body": body(b'{"soak": {"days": [{"date": "2026-10-99"}]}}'),
                                                      "ETag": '"e2"'})
        with mock.patch.object(L, "_cas_jitter_sleep"), self.assertRaises(L.StatusShapeError):
            L.update_status_json(BUCKET, "status.json", dict(self.DAY), 3)

    def test_preflight_malformed_status_is_not_ok(self):
        for bad in (b'{"soak": "x"}', b'{broken', b'{"soak": {"days": [{"date": "2026-10-99"}]}}'):
            self.setUp_again()
            out, rows = self._preflight(bad)
            self.assertFalse(out["ok"])
            self.assertEqual(rows["status_json_shape"]["result"], "Error")

    def setUp_again(self):
        self.tearDown()
        self.setUp()



class PreflightBudgetTests(Stubbed):
    def test_budget_checked_before_every_db_round_trip(self):
        clock = {"ms": 22500, "calls_below_reserve": 0}

        class TickConn(FakeConn):
            def cursor(self):
                cur = FakeCursor(self)
                real = cur.execute

                def execute(sql, params=None):
                    if clock["ms"] <= L._PREFLIGHT_RESERVE_MS:
                        clock["calls_below_reserve"] += 1
                    clock["ms"] -= 1000          # every DB round trip costs a second
                    return real(sql, params)
                cur.execute = execute
                return cur

        class Ctx:
            def get_remaining_time_in_millis(self):
                return clock["ms"]

        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        conns = [TickConn(), TickConn()]
        with mock.patch.dict(os.environ, {"WATERMARK_TIMESTAMP_COLUMNS": "", "MYSQL_REPLICA_STATUS_SIDE": ""}), \
             mock.patch.object(L, "_connect", side_effect=conns), mock.patch("builtins.print"):
            out = L.run_preflight(L._load_config(), BUCKET, "", Ctx())
        self.assertEqual(clock["calls_below_reserve"], 0, "a DB query started with the budget exhausted")
        self.assertEqual(sum(len(c.executed) for c in conns), 3)   # 22.5, 21.5, 20.5 s; the 4th (19.5 s) is refused
        results = {r["result"] for r in out["checks"]}
        self.assertIn("SKIPPED", results)
        self.assertFalse(out["ok"])
        self.assertFalse(any(r["result"] == "Error" and "budget" in r["message"] for r in out["checks"]))
        self.assertTrue(any(r["result"] == "SKIPPED" and "before the next DB query" in r["message"]
                            for r in out["checks"]), "compound probe stopped mid-way and reported SKIPPED")



class CrossMidnightTests(unittest.TestCase):
    def test_standalone_run_date_pinned_at_start(self):
        import tempfile
        times = iter([datetime.datetime(2026, 10, 6, 23, 30, tzinfo=datetime.timezone.utc),
                      datetime.datetime(2026, 10, 7, 0, 5, tzinfo=datetime.timezone.utc)])

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return next(times, datetime.datetime(2026, 10, 7, 0, 6, tzinfo=datetime.timezone.utc))
        src, tgt = FakeTableDb(range(1, 100)), FakeTableDb(range(1, 100))
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
               "tables": ["shop.orders"], "checksum_tables": ["shop.orders"]}
        fake = WatermarkStandaloneTests.fake_batch(None, {"s": src, "t": tgt})
        with mock.patch.object(soak_check.datetime, "datetime", FakeDT), \
             mock.patch.object(soak_check, "run_batch", side_effect=fake):
            day = soak_check.run_day(cfg)
            pinned = soak_check.run_day(cfg, "2026-10-05")
        self.assertEqual(day["date"], "2026-10-06")
        self.assertEqual(pinned["date"], "2026-10-05")

    def test_standalone_streak_uses_latest_recorded_day(self):
        import tempfile
        def day(d):
            return {"date": d, "overall": "green", "needs_agent_review": False, "checks": {"row_count": True}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "status.json"
            path.write_text(json.dumps({"soak": {"days": [day("2026-10-05")], "n_total": 3}}))
            soak_check.update_status_json(path, day("2026-10-06"), 3)   # written just after midnight
            self.assertEqual(json.loads(path.read_text())["soak"]["consecutive_green"], 2)

    def test_lambda_run_day_pins_date(self):
        self.assertEqual(L.run_day(dict(WM_CFG), DbConn(FakeTableDb(range(1, 50))), DbConn(FakeTableDb(range(1, 50))),
                                   "2026-10-06")["date"], "2026-10-06")



class ChecksumOnlyTableParityTests(unittest.TestCase):
    def test_checksum_only_inconclusive_table_is_none_in_both(self):
        # orders is checksum-only (not in tables) and every row is newer than the watermark:
        # the bounded checksum covers zero rows — must be None (review), never an empty PASS.
        mk = lambda: FakeTsDb([(1000 + i, "2026-10-05 12:30:00") for i in range(5)])
        cfg_l = {**TS_CFG, "tables": ["shop.payments"], "checksum_tables": ["shop.orders"]}

        class Two:
            def __init__(self):
                self.o, self.p = mk(), FakeTableDb(range(1, 50))
            def answer(self, sql):
                return (self.o if "`orders`" in sql or "6f7264657273" in sql or "DATE_FORMAT(NOW" in sql else self.p).answer(sql)
        a = L.run_day(cfg_l, DbConn(Two()), DbConn(Two()))
        cfg_s = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
                 "tables": ["shop.payments"], "checksum_tables": ["shop.orders"], "watermark": TS_CFG["watermark"]}
        fake = WatermarkStandaloneTests.fake_batch(None, {"s": Two(), "t": Two()})
        with mock.patch.object(soak_check, "run_batch", side_effect=fake):
            b = soak_check.run_day(cfg_s)
        self.assertIsNone(a["checks"]["checksum"])
        self.assertIsNone(b["checks"]["checksum"])
        self.assertEqual(a["detail"]["checksum"], b["detail"]["checksum"])
        self.assertNotIn("shop.orders", b["detail"]["row_count"])


class ScheduledDateTests(unittest.TestCase):
    def test_scheduled_time_pins_the_utc_day(self):
        self.assertEqual(L._scheduled_date({"scheduled_time": "2026-10-06T23:30:00Z"}), "2026-10-06")
        self.assertIsNone(L._scheduled_date({"mode": "x"}))
        self.assertIsNone(L._scheduled_date({"scheduled_time": "<aws.scheduler.scheduled-time>"}))
        self.assertIsNone(L._scheduled_date(None))

    def test_streak_counts_back_from_the_run_day_not_now(self):
        days = [{"date": "2026-10-05", "overall": "green", "needs_agent_review": False, "checks": {"a": True}},
                {"date": "2026-10-06", "overall": "green", "needs_agent_review": False, "checks": {"a": True}}]
        self.assertEqual(L._green_streak(list(days), "2026-10-06"), 2)


class PresignCredentialLifetimeTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 5, 18, 0, tzinfo=datetime.timezone.utc)

    def test_long_term_keys_keep_requested_expiry(self):
        eff, secs, warn = presign.effective_expiry(129600, False, None, now=self.NOW)
        self.assertEqual(secs, 129600)
        self.assertIsNone(warn)

    def test_temporary_credentials_cap_the_expiry_and_warn(self):
        cred_exp = self.NOW + datetime.timedelta(minutes=58)
        eff, secs, warn = presign.effective_expiry(172800, True, cred_exp, now=self.NOW)
        self.assertEqual(eff, cred_exp)
        self.assertEqual(secs, 58 * 60)
        self.assertIn("TEMPORARY", warn)
        self.assertIn("REFRESH the credentials first", warn)
        self.assertIn("A3", warn)

    def test_temporary_with_unknown_expiry_is_unknown_never_precise(self):
        eff, secs, warn = presign.effective_expiry(3600, True, None, now=self.NOW)
        self.assertIsNone(eff)
        self.assertIsNone(secs)
        self.assertIn("not known", warn)
        line = presign.expiry_line(None, 3600, now=self.NOW)
        self.assertEqual(line, "EFFECTIVE EXPIRY: unknown — no later than the credential session's expiry "
                               "(temporary credentials), at most 2026-10-05T19:00:00+00:00")

    def test_env_expiration_is_authoritative_when_no_refresh_metadata(self):
        creds = mock.MagicMock(spec=["get_frozen_credentials"])
        creds.get_frozen_credentials.return_value = mock.MagicMock(token="tok")
        session = mock.MagicMock()
        session.get_credentials.return_value = creds
        self.assertEqual(presign.credential_lifetime(session, environ={}), (True, None))
        got = presign.credential_lifetime(session, environ={"AWS_CREDENTIAL_EXPIRATION": "2026-10-05T19:30:00Z"})
        self.assertEqual(got, (True, datetime.datetime(2026, 10, 5, 19, 30, tzinfo=datetime.timezone.utc)))

    def test_main_prints_unknown_for_temporary_without_expiry(self):
        client = mock.MagicMock()
        client.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f"https://b.example/{Params['Key']}?s"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(presign, "resolve_bucket_region", return_value="ap-northeast-2"), \
             mock.patch.object(presign, "make_signing_client", return_value=client), \
             mock.patch.object(presign, "credential_lifetime", return_value=(True, None)), \
             mock.patch.object(presign, "verify_url", return_value=(True, 200, "")), \
             mock.patch.object(sys, "argv", ["x", "--bucket", "b", "--expires-seconds", "172800"]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            presign.main()
        self.assertIn("EFFECTIVE EXPIRY: unknown — no later than the credential session's expiry", out.getvalue())
        self.assertNotRegex(out.getvalue(), r"EFFECTIVE EXPIRY: \d")

    def test_credential_lifetime_detects_session_token(self):
        exp = datetime.datetime(2026, 10, 5, 19, 0)  # naive -> treated as UTC
        creds = mock.MagicMock()
        creds.get_frozen_credentials.return_value = mock.MagicMock(token="FwoG...")
        creds._expiry_time = exp
        session = mock.MagicMock()
        session.get_credentials.return_value = creds
        temporary, expiry = presign.credential_lifetime(session)
        self.assertTrue(temporary)
        self.assertEqual(expiry, exp.replace(tzinfo=datetime.timezone.utc))
        creds.get_frozen_credentials.return_value = mock.MagicMock(token=None)
        del creds._expiry_time
        self.assertEqual(presign.credential_lifetime(session, environ={}), (False, None))

    def test_main_reports_effective_not_requested_expiry(self):
        client = mock.MagicMock()
        client.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f"https://b.example/{Params['Key']}?s"
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=50)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(presign, "resolve_bucket_region", return_value="ap-northeast-2"), \
             mock.patch.object(presign, "make_signing_client", return_value=client), \
             mock.patch.object(presign, "credential_lifetime", return_value=(True, soon)), \
             mock.patch.object(presign, "verify_url", return_value=(True, 200, "")), \
             mock.patch.object(sys, "argv", ["x", "--bucket", "b", "--expires-seconds", "172800"]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            presign.main()
        self.assertIn("TEMPORARY", err.getvalue())
        self.assertIn(f"EFFECTIVE EXPIRY: {soon.isoformat(timespec='seconds')}", out.getvalue())
        self.assertIn("limited by temporary signing credentials", out.getvalue())
        self.assertIn("CUSTOMER LINK:", out.getvalue())
        self.assertIn("embeds the role session token", out.getvalue())   # OBS-40: why the link is ~2 KB
        self.assertIn("before it expires during a long soak", out.getvalue())
        self.assertIn("REFRESH the credentials first", out.getvalue())

    def test_must_cover_until_refuses_a_link_the_credentials_cannot_cover(self):
        # review r8 #3: re-running with the same STS session cannot extend the link
        now = datetime.datetime(2026, 10, 28, 18, 25, tzinfo=datetime.timezone.utc)
        cred = now + datetime.timedelta(hours=6)
        eff, secs, _ = presign.effective_expiry(604800, True, cred, now=now)
        err = presign.coverage_error(eff, now + datetime.timedelta(hours=12), secs < 604800)
        self.assertIn("SIGNING CREDENTIALS", err)
        self.assertIn("same credentials cannot extend", err)
        self.assertIsNone(presign.coverage_error(eff, now + datetime.timedelta(hours=5), True))
        self.assertIn("UNKNOWN", presign.coverage_error(None, now, True))
        self.assertIn("--expires-seconds", presign.coverage_error(now + datetime.timedelta(hours=1),
                                                                 now + datetime.timedelta(hours=2), False))
        self.assertIsNone(presign.coverage_error(eff, None, True))

    def test_cli_must_cover_until_stops_before_issuing(self):
        client = mock.MagicMock()
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=50)
        need = (soon + datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = io.StringIO()
        with mock.patch.object(presign, "resolve_bucket_region", return_value="ap-northeast-2"), \
             mock.patch.object(presign, "make_signing_client", return_value=client), \
             mock.patch.object(presign, "credential_lifetime", return_value=(True, soon)), \
             mock.patch.object(sys, "argv", ["x", "--bucket", "b", "--expires-seconds", "172800",
                                             "--must-cover-until", need]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                presign.main()
        self.assertIn("NOT ISSUED", str(cm.exception))
        self.assertNotIn("CUSTOMER LINK", out.getvalue())
        client.generate_presigned_url.assert_not_called()


class PresignRegionTests(unittest.TestCase):
    def client(self):
        c = boto3.client("s3", region_name="us-east-1")
        st = Stubber(c)
        st.activate()
        return c, st

    def test_region_from_403_header_without_listbucket(self):
        c, st = self.client()
        st.add_client_error("head_bucket", "403", "Forbidden", 403,
                            response_meta={"HTTPHeaders": {"x-amz-bucket-region": "ap-northeast-2"}})
        self.assertEqual(presign.resolve_bucket_region("b", c), "ap-northeast-2")

    def test_fallback_location_none_is_us_east_1(self):
        c, st = self.client()
        st.add_client_error("head_bucket", "403", "Forbidden", 403)
        st.add_response("get_bucket_location", {})  # us-east-1 buckets return no constraint
        self.assertEqual(presign.resolve_bucket_region("b", c), "us-east-1")

    def test_signing_client_is_regional_and_virtual_hosted(self):
        c = presign.make_signing_client("ap-northeast-2", "my-bucket")
        url = presign.presign(c, "my-bucket", "index.html", 60)
        self.assertTrue(url.startswith("https://my-bucket.s3.ap-northeast-2.amazonaws.com/index.html?"), url)
        self.assertIn("%2Fap-northeast-2%2Fs3%2Faws4_request", url)


class FakeTableDb:
    """One side of a MySQL pair holding a single table `shop`.`orders` with integer ids
    (ids=[...]) — answers exactly the SQL shapes the watermark path issues."""

    def __init__(self, ids, pk=("id", "bigint"), values=None):
        self.ids, self.pk, self.values, self.executed = list(ids), pk, values or {}, []

    def answer(self, sql):
        import re as _re
        self.executed.append(sql)
        up = sql.strip().upper()
        if "KEY_COLUMN_USAGE" in up:
            return [self.pk] if self.pk else []
        if up.startswith("SELECT COLUMN_NAME"):
            return [("id", "bigint", "NO", None), ("v", "varchar(10)", "YES", None)]
        if up.startswith("SELECT MAX("):
            return [(max(self.ids) if self.ids else None,)]
        if up.startswith("SELECT MIN("):
            return [(min(self.ids) if self.ids else None,)]
        m = _re.search(r"WHERE `id` (<=|>) (-?\d+)", sql)
        sel = self.ids if not m else [i for i in self.ids if (i <= int(m.group(2)) if m.group(1) == "<=" else i > int(m.group(2)))]
        if up.startswith("SELECT COUNT(*)"):
            return [(len(sel),)]
        if up.startswith("SELECT CONCAT(COUNT(*)"):
            return [(f"{len(sel)}:{sum(self.values.get(i, i) for i in sel)}",)]
        if up.startswith("CHECKSUM TABLE"):
            return [("shop.orders", sum(self.values.get(i, i) for i in self.ids))]
        return []


class DbConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        db = self.db

        class C:
            description = [("x",)]

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                s._rows = db.answer(sql)

            def fetchall(s):
                return s._rows
        return C()

    def close(self):
        pass


WM_CFG = {"source_engine": "mysql", "target_engine": "mysql", "tables": ["shop.orders"],
          "checksum_tables": ["shop.orders"], "watermark": {"pk_margin": 1000}}


class WatermarkLambdaTests(unittest.TestCase):
    def run_day(self, src, tgt, **over):
        cfg = {**WM_CFG, **over}
        return L.run_day(cfg, DbConn(src), DbConn(tgt))

    def test_live_tail_is_informational_not_red(self):
        # Target lags 50 rows behind: whole-table compare would be RED every day.
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 99951)))
        self.assertIs(day["checks"]["row_count"], True)
        self.assertIs(day["checks"]["checksum"], True)
        d = day["detail"]["row_count"]["shop.orders"]
        self.assertEqual(d["mode"], "pk_watermark")
        self.assertEqual(d["watermark"], 99950 - 1000)
        self.assertEqual(d["tail_rows"], {"source": 1050, "target": 1000})
        self.assertEqual((d["source"], d["target"]), (98950, 98950))
        self.assertEqual(day["detail"]["checksum"]["shop.orders"]["mode"], "pk_watermark")

    def test_whole_table_when_disabled_keeps_old_behaviour(self):
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 99951)),
                           watermark={"enabled": False})
        self.assertIs(day["checks"]["row_count"], False)
        self.assertEqual(day["detail"]["row_count"]["shop.orders"]["mode"], "whole_table")
        self.assertIn("disabled", day["detail"]["row_count"]["shop.orders"]["note"])

    def test_no_integer_pk_falls_back_and_says_so(self):
        day = self.run_day(FakeTableDb(range(1, 101), pk=("code", "varchar")),
                           FakeTableDb(range(1, 101), pk=("code", "varchar")))
        d = day["detail"]["row_count"]["shop.orders"]
        self.assertEqual(d["mode"], "whole_table")
        self.assertIn("no single-column integer primary key", d["note"])
        self.assertIs(day["checks"]["row_count"], True)

    def test_mismatch_below_watermark_is_red(self):
        src = FakeTableDb(range(1, 100001))
        tgt = FakeTableDb([i for i in range(1, 100001) if i != 500])   # a lost old row
        day = self.run_day(src, tgt)
        self.assertIs(day["checks"]["row_count"], False)
        self.assertEqual(day["overall"], "red")
        self.assertTrue(day["needs_agent_review"])

    def test_changed_old_row_fails_bounded_checksum(self):
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 100001), values={10: 99}))
        self.assertIs(day["checks"]["row_count"], True)
        self.assertIs(day["checks"]["checksum"], False)

    def test_small_table_below_margin_uses_whole_table(self):
        day = self.run_day(FakeTableDb(range(1, 51)), FakeTableDb(range(1, 51)))
        d = day["detail"]["row_count"]["shop.orders"]
        self.assertEqual(d["mode"], "whole_table")
        self.assertIn("margin", d["note"])
        self.assertIs(day["checks"]["row_count"], True)

    def test_bounded_verdict_inconclusive_is_none(self):
        self.assertIsNone(L.bounded_verdict(0, 0, 5, 3))
        self.assertIs(L.bounded_verdict(10, 10, 0, 0), True)
        self.assertIsNone(L.combine_checks([True, None]))
        self.assertIs(L.combine_checks([None, False]), False)

    def test_env_config(self):
        env = {**ENV, "WATERMARK_PK_MARGIN": "50", "WATERMARK_TIMESTAMP_COLUMNS": '{"shop.orders": "created_at"}',
               "WATERMARK_ENABLED": "", "DB_QUERY_TIMEOUT_SECONDS": ""}
        with mock.patch.dict(os.environ, env):
            cfg = L._load_config()
        self.assertEqual(cfg["watermark"]["pk_margin"], 50)
        self.assertTrue(cfg["watermark"]["enabled"])
        self.assertEqual(cfg["watermark"]["timestamp_columns"], {"shop.orders": "created_at"})
        self.assertEqual(cfg["db_query_timeout_seconds"], 300)


class WatermarkStandaloneTests(unittest.TestCase):
    @staticmethod
    def fake_batch(_self, dbs):
        def run_batch(family, conn, sqls, headers=False, timeout=None):
            db = dbs[conn["host"]]
            return [["\t".join("NULL" if v is None else str(v) for v in row) for row in db.answer(q)] for q in sqls]
        return run_batch

    def run_day(self, src, tgt, **over):
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
               "tables": ["shop.orders"], "checksum_tables": ["shop.orders"], "watermark": {"pk_margin": 1000}, **over}
        with mock.patch.object(soak_check, "run_batch", side_effect=self.fake_batch(None, {"s": src, "t": tgt})):
            return soak_check.run_day(cfg)

    def test_live_tail_is_informational_not_red(self):
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 99951)))
        self.assertIs(day["checks"]["row_count"], True)
        self.assertIs(day["checks"]["checksum"], True)
        d = day["detail"]["row_count"]["shop.orders"]
        self.assertEqual((d["mode"], d["watermark"], d["tail_rows"]), ("pk_watermark", 98950, {"source": 1050, "target": 1000}))

    def test_whole_table_fallback_and_red(self):
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 99951)), watermark={"enabled": False})
        self.assertIs(day["checks"]["row_count"], False)
        self.assertEqual(day["detail"]["row_count"]["shop.orders"]["mode"], "whole_table")

    def test_mismatch_below_watermark_is_red(self):
        day = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 100001), values={10: 99}))
        self.assertIs(day["checks"]["checksum"], False)

    def test_scripts_agree_on_detail(self):
        a = self.run_day(FakeTableDb(range(1, 100001)), FakeTableDb(range(1, 99951)))
        b = L.run_day({**WM_CFG}, DbConn(FakeTableDb(range(1, 100001))), DbConn(FakeTableDb(range(1, 99951))))
        self.assertEqual(a["detail"]["row_count"], b["detail"]["row_count"])
        self.assertEqual(a["checks"], b["checks"])

    def test_sql_builders_identical(self):
        for fam in ("mysql", "postgres"):
            for name in ("_pk_sql",):
                self.assertEqual(getattr(L, name)(fam, "shop.orders"), getattr(soak_check, name)(fam, "shop.orders"))
            self.assertEqual(L._max_sql(fam, "shop.orders", "id"), soak_check._max_sql(fam, "shop.orders", "id"))
            self.assertEqual(L._cutoff_sql(fam, 15), soak_check._cutoff_sql(fam, 15))
            types = {"id": "bigint", "b": "varbinary(16)", "v": "varchar(5)"}
            self.assertEqual(L._bounded_checksum_sql(fam, "shop.orders", types, "`id` <= 5"),
                             soak_check._bounded_checksum_sql(fam, "shop.orders", types, "`id` <= 5"))
        self.assertIn("HEX(`b`)", L._bounded_checksum_sql("mysql", "shop.orders", {"b": "varbinary(16)"}, "1=0"))
        self.assertEqual(L.WATERMARK_DEFAULTS, soak_check.WATERMARK_DEFAULTS)



# ── Cross-family (MySQL <-> PostgreSQL) soak path ───────────────────────────────────────
_had_skip = "CEC_SKIP_DOCKER" in os.environ
os.environ.setdefault("CEC_SKIP_DOCKER", "1")   # importing the compare tests' in-memory sides only
import cross_engine_compare as cec  # noqa: E402
from test_cross_engine_compare import MemSide, mysql_table, pg_table, src_rows, tgt_rows  # noqa: E402
if not _had_skip:
    os.environ.pop("CEC_SKIP_DOCKER", None)


class MemConn:
    """A driver-shaped connection over a MemSide: cursor() for the pymysql path, run() for
    pg8000.native — so the Lambda's own _query()/preflight code paths are exercised."""

    def __init__(self, side):
        self.side = side

    def cursor(self):
        side = self.side

        class C:
            description = [("x",)]

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                s._rows = side.query(sql)

            def fetchall(s):
                return s._rows

            def close(s):
                pass
        return C()

    def run(self, sql, **kw):
        return self.side.query(sql)

    def close(self):
        pass


X_CFG = {"source_engine": "mysql", "target_engine": "aurora-postgresql", "tables": ["shop.items"],
         "checksum_tables": ["shop.items"], "watermark": {"pk_margin": 5}, "cross_engine": {}}


def x_sides(src_n=30, tgt_n=28, tgt=None, catalog=None):
    return (MemSide("mysql", mysql_table(src_rows(src_n)), "source"),
            MemSide("postgres", pg_table(tgt if tgt is not None else tgt_rows(tgt_n), catalog=catalog), "target"))


class CrossFamilySoakTests(unittest.TestCase):
    def std(self, sides, **over):
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "postgres", "host": "t"},
               "tables": ["shop.items"], "checksum_tables": ["shop.items"], "watermark": {"pk_margin": 5}, **over}
        it = iter(sides)
        with mock.patch.object(soak_check, "_cross_connect", side_effect=lambda *a, **k: next(it)):
            return soak_check.run_day(cfg, "2026-10-28")

    def test_lambda_cross_family_day_is_green_under_lag(self):
        s, t = x_sides()
        day = L.run_day(dict(X_CFG), MemConn(s), MemConn(t), "2026-10-28")
        self.assertEqual({k: day["checks"][k] for k in ("row_count", "checksum", "schema_drift")},
                         {"row_count": True, "checksum": True, "schema_drift": True})
        d = day["detail"]["row_count"]["shop.items"]
        self.assertEqual((d["mode"], d["watermark"], d["comparison"]), ("pk_watermark", 23, "cross_engine_normalized"))
        self.assertEqual(day["checks"]["period_evidence"], None)          # unchanged four-state model
        self.assertTrue(day["needs_agent_review"])

    def test_scripts_agree_cross_family(self):
        lam = L.run_day(dict(X_CFG), *map(MemConn, x_sides()), "2026-10-28")
        std = self.std(x_sides())
        self.assertEqual(lam["checks"], std["checks"])
        self.assertEqual(lam["detail"]["row_count"], std["detail"]["row_count"])
        self.assertEqual(lam["detail"]["checksum"], std["detail"]["checksum"])

    def test_cross_family_mismatch_is_red_with_pinpointed_key(self):
        t = tgt_rows(30)
        t[2]["price"] = Decimal("3.1")
        for day in (L.run_day(dict(X_CFG), *map(MemConn, x_sides(tgt=t)), "2026-10-28"), self.std(x_sides(tgt=t))):
            self.assertIs(day["checks"]["checksum"], False)
            self.assertEqual(day["overall"], "red")
            ex = day["detail"]["checksum"]["shop.items"]["mismatches"]["examples"]["different"]
            self.assertEqual(ex[0]["key"], ["3"])

    def test_native_replica_side_cross_family_needs_review(self):
        for day in (L.run_day({**X_CFG, "mysql_replica_status_side": "target"}, *map(MemConn, x_sides()), "2026-10-28"),
                    self.std(x_sides(), mysql_replica_status_side="target")):
            self.assertIsNone(day["checks"]["replication_lag"])
            self.assertEqual(day["detail"]["replication_lag_mechanism"], "native_replication_not_possible_cross_family")
            self.assertIn("DMS", day["detail"]["aws_errors"][0]["message"])

    def test_homogeneous_still_uses_the_single_dialect_path(self):
        with mock.patch.object(L, "_run_day_cross_family") as cross:
            L.run_day({**WM_CFG}, DbConn(FakeTableDb(range(1, 100))), DbConn(FakeTableDb(range(1, 100))))
        cross.assert_not_called()

    def test_load_config_cross_pair_and_unsupported_engines(self):
        env = {**ENV, "SOURCE_ENGINE": "mysql", "TARGET_ENGINE": "aurora-postgresql",
               "CROSS_ENGINE_OPTIONS": '{"naive_timezone": "+09:00"}'}
        with mock.patch.dict(os.environ, env):
            cfg = L._load_config()
        self.assertEqual((cfg["family"], cfg["target_family"], cfg["cross_engine"]["naive_timezone"]),
                         ("mysql", "postgres", "+09:00"))
        with mock.patch.dict(os.environ, {**env, "CROSS_ENGINE_OPTIONS": '{"naive_tz": "x"}'}):
            with self.assertRaises(ValueError):
                L._load_config()
        with mock.patch.dict(os.environ, {**ENV, "TARGET_ENGINE": "oracle"}), self.assertRaises(ValueError) as e:
            L._load_config()
        self.assertIn("Unsupported engine", str(e.exception))
        with mock.patch.dict(os.environ, env), mock.patch.object(L, "cec", None), self.assertRaises(ValueError) as e:
            L._load_config()
        self.assertIn("not bundled", str(e.exception))
        self.assertIn("No secret was read", str(e.exception))

    def test_standalone_unsupported_engine_message_unchanged(self):
        cfg = {"source": {"engine": "mysql"}, "target": {"engine": "sqlserver"}, "tables": ["t"]}
        with self.assertRaises(ValueError) as e:
            soak_check.run_day(cfg)
        self.assertIn("Unsupported engine", str(e.exception))


class CrossFamilyPreflightTests(Stubbed):
    def run_pf(self, catalog=None, **env_over):
        env = {"SOURCE_ENGINE": "mysql", "TARGET_ENGINE": "postgres", "TABLES": '["shop.items"]',
               "MYSQL_REPLICA_STATUS_SIDE": "", **env_over}
        self.secret_ok(SRC_ARN)
        self.secret_ok(TGT_ARN)
        self.pf_aws_ok()
        self.s3_get("status.json")
        self.s3_put_probe("status.json", "PreconditionFailed", 412)
        self.s3_get("activity-log.jsonl", b"", '"e2"')
        self.s3_put_probe("activity-log.jsonl", "PreconditionFailed", 412, b"", "application/x-ndjson")
        self.reports_probe()
        s, t = x_sides(catalog=catalog, tgt=[{**r, "flag": int(r["flag"])} for r in tgt_rows()] if catalog else None)
        with mock.patch.dict(os.environ, env), mock.patch.object(L, "_connect", side_effect=[MemConn(s), MemConn(t)]) as c, \
                mock.patch("builtins.print"):
            out = L.handler({"mode": "preflight"}, None)
        self.assertEqual([call.args[0] for call in c.call_args_list], ["mysql", "postgres"])   # per-side driver
        return out, {r["check"]: r for r in out["checks"]}, s, t

    def test_cross_family_probes_pass_and_stay_zero_row(self):
        out, rows, s, t = self.run_pf()
        self.assertTrue(out["ok"], [r for r in out["checks"] if r["result"] != "PASS"])
        for side in ("source", "target"):
            self.assertIn("cross-engine normalized", rows[f"{side}_db_watermark shop.items"]["message"])
            self.assertIn("pk_watermark", rows[f"{side}_db_watermark shop.items"]["message"])
            self.assertEqual(rows[f"{side}_db_checksum shop.items"]["result"], "PASS")
        # The run's own watermark endpoint reads (MAX/MIN, source clock) — collected from a real
        # normal run on identical sides; preflight must issue exactly these, and every OTHER data
        # statement must be zero-row (only the shared `SELECT 1 … LIMIT 1` privilege probe reads a constant).
        rs, rt = x_sides()
        L.run_day({**X_CFG, "tables": ["shop.items"]}, MemConn(rs), MemConn(rt), "2026-10-28")
        wm = lambda log: {q for q in log if q.startswith(("SELECT MAX(", "SELECT MIN(", "SELECT UTC_TIMESTAMP", "SELECT (now()"))}
        for side, run_side in ((s, rs), (t, rt)):
            run_wm = wm(run_side.log)
            self.assertTrue(run_wm, "the run resolved no watermark")
            self.assertEqual(wm(side.log), run_wm)          # incl. MAX() on the target's NUMERIC key
            data = [q for q in side.log if q.lstrip().upper().startswith("SELECT") and " FROM " in q
                    and "information_schema" not in q and "pg_catalog" not in q]
            others = [q for q in data if q not in run_wm and not re.match(r"SELECT 1 FROM \S+ LIMIT 1$", q)]
            self.assertTrue(others and all("1=0" in q for q in others), others)
        self.assertIn('SELECT MAX("id") FROM "shop"."items"', wm(t.log))

    def test_pg_lag_side_without_dms_fails_preflight_like_the_run(self):
        no_dms = {"DMS_TASK_ID": "", "DMS_REPLICATION_INSTANCE_ID": "", "DMS_TASK_ARN": ""}
        out, rows, _, _ = self.run_pf(PG_REPLICATION_LAG_SIDE="target", **no_dms)
        self.assertFalse(out["ok"])
        self.assertEqual(rows["replication_lag_config"]["result"], "Error")
        day = L.run_day({**X_CFG, "pg_replication_lag_side": "target"}, *map(MemConn, x_sides()), "2026-10-28")
        self.assertEqual(rows["replication_lag_config"]["message"], day["detail"]["aws_errors"][0]["message"])
        out, rows, _, _ = self.run_pf(MYSQL_REPLICA_STATUS_SIDE="target", **no_dms)
        self.assertEqual(rows["replication_lag_config"]["result"], "Error")

    def test_native_lag_side_with_dms_is_accepted_like_the_run(self):
        out, rows, _, _ = self.run_pf(PG_REPLICATION_LAG_SIDE="target")    # ENV configures the DMS task
        self.assertNotIn("replication_lag_config", rows)
        self.assertTrue(out["ok"], [r for r in out["checks"] if r["result"] != "PASS"])

    def test_type_mapping_defect_is_an_error_row(self):
        cat = [("flag", "smallint", "b", "N", "NO", None) if c[0] == "flag" else c for c in pg_table([])[("shop", "items")]["catalog"]]
        out, rows, _, _ = self.run_pf(catalog=cat)
        self.assertFalse(out["ok"])
        self.assertEqual(rows["target_db_watermark shop.items"]["result"], "Error")
        self.assertIn("every day RED", rows["target_db_watermark shop.items"]["message"])


class PassDb(FakeTableDb):
    """FakeTableDb whose ids change per comparison pass: ids_by_pass[n] is served from the
    n-th MAX(pk) read on (0-based; the last entry repeats) — a pass = one re-read."""

    def __init__(self, ids_by_pass, **kw):
        super().__init__(ids_by_pass[0], **kw)
        self.ids_by_pass, self.passes = [list(x) for x in ids_by_pass], 0

    def answer(self, sql):
        if sql.strip().upper().startswith("SELECT MAX("):
            self.ids = self.ids_by_pass[min(self.passes, len(self.ids_by_pass) - 1)]
            self.passes += 1
        return super().answer(sql)


FULL = range(1, 100001)
NO_500 = [i for i in FULL if i != 500]           # source DELETEd old row 500; target still has it


class RecheckTests(unittest.TestCase):
    """OBS-42: a count difference on a LIVE table is re-checked in fresh snapshots before it can
    be RED; static tables are exact; persistent differences stay RED with evidence."""

    def lam(self, src, tgt, **over):
        return L.run_day({**WM_CFG, **over}, DbConn(src), DbConn(tgt), "2026-10-28")

    def std(self, src, tgt, **over):
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "mysql", "host": "t"},
               "tables": ["shop.orders"], "checksum_tables": ["shop.orders"], "watermark": {"pk_margin": 1000}, **over}
        with mock.patch.object(soak_check, "run_batch",
                               side_effect=WatermarkStandaloneTests.fake_batch(None, {"s": src, "t": tgt})):
            return soak_check.run_day(cfg, "2026-10-28")

    def both(self, mk_src, mk_tgt, **over):
        return [run(mk_src(), mk_tgt(), **over) for run in (self.lam, self.std)]

    def test_target_ahead_by_one_converges_and_is_not_red(self):
        sleeps = []
        with mock.patch.object(L, "RECHECK_SLEEP", sleeps.append), mock.patch.object(soak_check, "RECHECK_SLEEP", sleeps.append):
            days = self.both(lambda: FakeTableDb(NO_500), lambda: PassDb([FULL, NO_500]),
                             recheck={"attempts": 3, "delay_seconds": 7})
        self.assertEqual(sleeps, [7, 7], "one re-read per script, after the configured delay")
        for day in days:
            self.assertIs(day["checks"]["row_count"], True)
            self.assertIs(day["checks"]["checksum"], True)
            d = day["detail"]["row_count"]["shop.orders"]
            rc = d["recheck"]
            self.assertEqual(rc["result"], "converged")
            self.assertEqual(rc["first"]["rows"], {"source": 98999, "target": 99000, "match": False})
            self.assertEqual([a["match"] for a in rc["attempts"]], [True])
            self.assertIs(d["match"], True)
            self.assertEqual(day["detail"]["checksum"]["shop.orders"]["recheck"]["result"], "converged")
        self.assertEqual(days[0]["detail"]["row_count"], days[1]["detail"]["row_count"], "both scripts agree")

    def test_persistent_difference_is_red_with_every_reading(self):
        for day in self.both(lambda: FakeTableDb(NO_500), lambda: FakeTableDb(FULL)):
            self.assertIs(day["checks"]["row_count"], False)
            self.assertEqual(day["overall"], "red")
            rc = day["detail"]["row_count"]["shop.orders"]["recheck"]
            self.assertEqual((rc["result"], len(rc["attempts"]), rc["delay_seconds"]), ("persistent", 2, 20))
            self.assertTrue(all(a["rows"] == {"source": 98999, "target": 99000, "match": False} for a in rc["attempts"]))

    def test_static_table_difference_is_red_immediately(self):
        dbs = []

        def tgt():
            dbs.append(PassDb([FULL, NO_500]))
            return dbs[-1]
        for day in self.both(lambda: FakeTableDb(NO_500), tgt, static_tables=["shop.orders"]):
            self.assertIs(day["checks"]["row_count"], False)
            rc = day["detail"]["row_count"]["shop.orders"]["recheck"]
            self.assertEqual((rc["result"], rc["attempts"]), ("static_table_exact", []))
        self.assertEqual([db.passes for db in dbs], [1, 1], "a static table is never re-read")

    def test_tolerance_applies_only_after_rechecks_and_never_to_checksums(self):
        # whole-table mode (watermark off): target keeps one extra row (id 0, value 0 -> same CHECKSUM)
        src, tgt = (lambda: FakeTableDb(range(1, 101))), (lambda: FakeTableDb(range(0, 101)))
        for day in self.both(src, tgt, watermark={"enabled": False}, row_count_tolerance={"shop.orders": 1}):
            self.assertIs(day["checks"]["row_count"], True)
            self.assertTrue(day["detail"]["row_count"]["shop.orders"]["recheck"]["result"].startswith("within_tolerance (1 <= 1)"))
        for day in self.both(src, lambda: FakeTableDb(range(-1, 101)), watermark={"enabled": False},
                             row_count_tolerance={"shop.orders": 1}):
            self.assertIs(day["checks"]["row_count"], False, "2 > tolerance 1")
        for day in self.both(lambda: FakeTableDb(range(1, 101)), lambda: FakeTableDb(range(1, 102)),
                             watermark={"enabled": False}, row_count_tolerance={"shop.orders": 5}):
            self.assertIs(day["checks"]["checksum"], False, "a checksum difference is never tolerated")
            self.assertIs(day["checks"]["row_count"], False)

    def test_attempts_zero_is_needs_review_not_red(self):
        for day in self.both(lambda: FakeTableDb(NO_500), lambda: FakeTableDb(FULL), recheck={"attempts": 0}):
            self.assertIsNone(day["checks"]["row_count"])
            self.assertTrue(day["needs_agent_review"])
            self.assertIn("needs_recheck", day["detail"]["row_count"]["shop.orders"]["recheck"]["result"])

    @staticmethod
    def _seq_rerun(*steps):
        """rerun() returning (row_check, checksum_check) per step; an exception class raises."""
        it = iter(steps)

        def rerun(_tables):
            step = next(it)
            if isinstance(step, type) and issubclass(step, BaseException):
                raise step("re-read timed out")
            s_, t_ = step
            return ({"pass": s_ == t_, "detail": {"t": {"source": s_, "target": t_, "match": s_ == t_}}},
                    {"pass": True, "detail": {}})
        return rerun

    def test_failed_final_reread_is_inconclusive_never_persistent_or_tolerated(self):
        # review r11 #2: reread 1 -> 5/6, reread 2 raises TimeoutError. The earlier reading is
        # kept as evidence, but the table is inconclusive (None) — neither RED-by-persistence
        # (tolerance 0) nor within_tolerance (tolerance 1).
        row = {"pass": False, "detail": {"t": {"source": 5, "target": 6, "match": False}}}
        for mod in (L, soak_check):
            for tol in (0, 1):
                with mock.patch.object(mod, "RECHECK_SLEEP", lambda _s: None):
                    r, _ = mod.apply_rechecks({"row_count_tolerance": {"t": tol}}, json.loads(json.dumps(row)),
                                              {"pass": True, "detail": {}}, self._seq_rerun((5, 6), TimeoutError))
                d = r["detail"]["t"]
                self.assertIsNone(d["match"], (mod.__name__, tol))
                self.assertIsNone(r["pass"], (mod.__name__, tol))
                self.assertTrue(d["recheck"]["result"].startswith("inconclusive (final re-read failed: TimeoutError"),
                                (mod.__name__, tol, d["recheck"]["result"]))
                att = d["recheck"]["attempts"]
                self.assertEqual(att[0]["rows"], {"source": 5, "target": 6, "match": False}, "earlier reading kept")
                self.assertEqual((att[1]["attempt"], att[1]["error"][:12]), (2, "TimeoutError"))
                self.assertEqual(d["recheck"]["first"]["rows"]["target"], 6)

    def test_failure_then_success_is_resolved_by_the_final_reading(self):
        row = {"pass": False, "detail": {"t": {"source": 5, "target": 6, "match": False}}}
        for mod in (L, soak_check):
            with mock.patch.object(mod, "RECHECK_SLEEP", lambda _s: None):
                r, _ = mod.apply_rechecks({}, json.loads(json.dumps(row)), {"pass": True, "detail": {}},
                                          self._seq_rerun(TimeoutError, (6, 6)))
                self.assertEqual((r["detail"]["t"]["recheck"]["result"], r["detail"]["t"]["match"], r["pass"]),
                                 ("converged", True, True), mod.__name__)
                r, _ = mod.apply_rechecks({"row_count_tolerance": {"t": 1}}, json.loads(json.dumps(row)),
                                          {"pass": True, "detail": {}}, self._seq_rerun(TimeoutError, (5, 6)))
                self.assertTrue(r["detail"]["t"]["recheck"]["result"].startswith("within_tolerance (1 <= 1)"), mod.__name__)
                r, _ = mod.apply_rechecks({}, json.loads(json.dumps(row)), {"pass": True, "detail": {}},
                                          self._seq_rerun(TimeoutError, (5, 6)))
                self.assertEqual((r["detail"]["t"]["recheck"]["result"], r["detail"]["t"]["match"]),
                                 ("persistent", False), mod.__name__)

    def test_failed_rereads_are_needs_review(self):
        row = {"pass": False, "detail": {"t": {"source": 1, "target": 2, "match": False}}}
        ck = {"pass": True, "detail": {}}
        for mod in (L, soak_check):
            r, c = mod.apply_rechecks({}, json.loads(json.dumps(row)), dict(ck), lambda sub: 1 / 0)
            self.assertIsNone(r["pass"])
            self.assertEqual(r["detail"]["t"]["recheck"]["result"], "recheck_failed")
            self.assertIn("ZeroDivisionError", r["detail"]["t"]["recheck"]["attempts"][0]["error"])

    def test_no_difference_means_no_reread(self):
        calls = []
        row = {"pass": True, "detail": {"t": {"source": 1, "target": 1, "match": True}}}
        r, _ = L.apply_rechecks({}, row, {"pass": True, "detail": {}}, lambda sub: calls.append(sub))
        self.assertEqual((calls, r["pass"], "recheck" in r["detail"]["t"]), ([], True, False))

    def test_config_is_validated(self):
        for bad in ({"recheck": {"attempts": -1}}, {"recheck": {"attempts": True}}, {"recheck": {"delay_seconds": "5"}},
                    {"recheck": {"attemps": 2}}, {"static_tables": "shop.orders"},
                    {"row_count_tolerance": {"t": -1}}, {"row_count_tolerance": {"t": 1}, "static_tables": ["t"]}):
            for mod in (L, soak_check):
                with self.assertRaises(ValueError, msg=bad):
                    mod.recheck_config(bad)
        env = {**ENV, "RECHECK": '{"attempts": 1, "delay_seconds": 5}', "STATIC_TABLES": '["shop.lookup"]',
               "ROW_COUNT_TOLERANCE": '{"shop.orders": 3}'}
        with mock.patch.dict(os.environ, env):
            cfg = L._load_config()
        self.assertEqual((cfg["recheck"], cfg["static_tables"], cfg["row_count_tolerance"]),
                         ({"attempts": 1, "delay_seconds": 5}, ["shop.lookup"], {"shop.orders": 3}))
        with mock.patch.dict(os.environ, {**env, "RECHECK": '{"attempts": 9}'}), self.assertRaises(ValueError):
            L._load_config()

    def test_recheck_helpers_are_identical_in_both_scripts(self):
        import inspect
        for name in ("recheck_config", "_combine_states", "_recheck_reading", "apply_rechecks",
                     "expected_schema_differences_config", "apply_expected_differences",
                     "_declared_side_family", "matching_catalog_columns"):
            self.assertEqual(inspect.getsource(getattr(L, name)), inspect.getsource(getattr(soak_check, name)), name)
        self.assertEqual(L.RECHECK_DEFAULTS, soak_check.RECHECK_DEFAULTS)
        self.assertEqual((L.EXPECTED_DIFF_KEYS, L.EXPECTED_DIFF_KINDS),
                         (soak_check.EXPECTED_DIFF_KEYS, soak_check.EXPECTED_DIFF_KINDS))

    def test_tolerance_never_overrides_null_count_or_inconclusive_evidence(self):
        # review r9 #1, both scripts: (a) count-only table, totals 101/101 but NULL counts 1/0,
        # tolerance 0 — the failure is the NULL count, not a total difference; (b) the re-read is
        # an inconclusive 0/0 with unexamined tail rows — unknown coverage is never tolerated.
        null_first = {"source": 101, "target": 101, "match": False, "null_rows": {"source": 1, "target": 0}}
        inconclusive = {"source": 0, "target": 0, "match": None, "tail_rows": {"source": 3, "target": 3}}
        for mod in (L, soak_check):
            with mock.patch.object(mod, "RECHECK_SLEEP", lambda s: None):
                r, _ = mod.apply_rechecks({"row_count_tolerance": {"t": 0}},
                                          {"pass": False, "detail": {"t": dict(null_first)}}, {"pass": True, "detail": {}},
                                          lambda sub: ({"detail": {"t": dict(null_first)}}, {"detail": {}}))
                self.assertIs(r["detail"]["t"]["match"], False, mod.__name__)
                self.assertEqual(r["detail"]["t"]["recheck"]["result"], "persistent")
                self.assertIs(r["pass"], False)
                r, _ = mod.apply_rechecks({"row_count_tolerance": {"t": 5}},
                                          {"pass": False, "detail": {"t": {"source": 5, "target": 6, "match": False}}},
                                          {"pass": True, "detail": {}},
                                          lambda sub: ({"detail": {"t": dict(inconclusive)}}, {"detail": {}}))
                self.assertIsNone(r["detail"]["t"]["match"], mod.__name__)
                self.assertEqual(r["detail"]["t"]["recheck"]["result"], "inconclusive")
                self.assertEqual(r["detail"]["t"]["tail_rows"], {"source": 3, "target": 3}, "final reading published")
                self.assertIsNone(r["pass"])
                # a conclusive total-only difference is still tolerated
                r, _ = mod.apply_rechecks({"row_count_tolerance": {"t": 1}},
                                          {"pass": False, "detail": {"t": {"source": 5, "target": 6, "match": False}}},
                                          {"pass": True, "detail": {}},
                                          lambda sub: ({"detail": {"t": {"source": 5, "target": 6, "match": False}}}, {"detail": {}}))
                self.assertIs(r["detail"]["t"]["match"], True)
                self.assertTrue(r["detail"]["t"]["recheck"]["result"].startswith("within_tolerance (1 <= 1)"))

    def test_rereads_publish_the_final_readings_diagnostics(self):
        # review r9 #2: key 2 (DELETEd on the source, still on the target) converges on the
        # re-read; key 3 differs on every reading. The published checksum detail must name key 3
        # — the final reading's digests/mismatches/examples/verdict — with the first under `first`.
        def sides():
            s = MemSide("mysql", mysql_table([r for r in src_rows(30) if int(r["id"]) != 2]), "source")
            rows = tgt_rows(30)
            for r in rows:
                if int(r["id"]) == 3:
                    r["price"] = Decimal("999.5")
            t = MemSide("postgres", pg_table(rows), "target")
            begins, orig = [0], t.query

            def query(sql, *a, **k):
                if sql.strip().upper().startswith("BEGIN"):
                    begins[0] += 1
                    if begins[0] == 2:
                        live = t.tables[("shop", "items")]["rows"]
                        live[:] = [r for r in live if int(r["id"]) != 2]
                return orig(sql, *a, **k)
            t.query = query
            return s, t
        lam = L.run_day(dict(X_CFG), *map(MemConn, sides()), "2026-10-28")
        std = CrossFamilySoakTests.std(self, sides())
        for day in (lam, std):
            self.assertIs(day["checks"]["checksum"], False)
            ck = day["detail"]["checksum"]["shop.items"]
            self.assertEqual((ck["recheck"]["result"], ck["verdict"], ck["match"]), ("persistent", "MISMATCH", False))
            ex = ck["mismatches"]["examples"]
            self.assertEqual([e["key"] for e in ex["different"]], [["3"]])
            self.assertEqual(ex["extra_on_target"], [], "key 2 converged: not reported as the persistent failure")
            self.assertEqual(ck["rows"], {"source": 24, "target": 24})
            first = ck["recheck"]["first"]["checksum_detail"]
            self.assertEqual(first["mismatches"]["examples"]["extra_on_target"], [["2"]])
            self.assertNotEqual(first["target"], ck["target"], "digests come from the final reading")
            reread = ck["recheck"]["attempts"][-1]["checksum_detail"]
            self.assertEqual(reread["target"], ck["target"])
            self.assertIs(day["detail"]["row_count"]["shop.items"]["match"], True)
        self.assertEqual(lam["detail"]["checksum"], std["detail"]["checksum"])

    def test_cross_family_live_drift_converges(self):
        # source DELETEd item 2; the target still has it on the first read, not on the re-read
        def sides():
            s = MemSide("mysql", mysql_table([r for r in src_rows(30) if int(r["id"]) != 2]), "source")
            t = MemSide("postgres", pg_table(tgt_rows(30)), "target")
            begins, orig = [0], t.query

            def query(sql, *a, **k):
                if sql.strip().upper().startswith("BEGIN"):
                    begins[0] += 1
                    if begins[0] == 2:
                        rows = t.tables[("shop", "items")]["rows"]
                        rows[:] = [r for r in rows if int(r["id"]) != 2]
                return orig(sql, *a, **k)
            t.query = query
            return s, t
        lam = L.run_day(dict(X_CFG), *map(MemConn, sides()), "2026-10-28")
        std = CrossFamilySoakTests.std(self, sides())
        for day in (lam, std):
            self.assertEqual((day["checks"]["row_count"], day["checks"]["checksum"]), (True, True))
            rc = day["detail"]["row_count"]["shop.items"]["recheck"]
            self.assertEqual((rc["result"], rc["first"]["rows"]["match"]), ("converged", False))
        self.assertEqual(lam["detail"]["row_count"], std["detail"]["row_count"])



GEN_COL = {"table": "shop.items", "column": "open_invoice_no", "side": "target", "kind": "extra_column",
           "reason": "generated column of the partial-unique-index emulation (mysql-postgresql.md §4.3)"}


class ExpectedSchemaDifferenceTests(unittest.TestCase):
    """OBS-53: an intentional schema difference (the partial-unique-index recipe's generated
    column on the target) is reported as expected, never RED; anything undeclared still fails."""

    def sides(self, extra=("open_invoice_no",)):
        cat = pg_table([])[("shop", "items")]["catalog"] + [(c, "text", "b", "S", "YES", None) for c in extra]
        return x_sides(catalog=cat)

    def days(self, cfg_over, extra=("open_invoice_no",)):
        lam = L.run_day({**X_CFG, **cfg_over}, *map(MemConn, self.sides(extra)), "2026-10-28")
        std = CrossFamilySoakTests.std(self, self.sides(extra), **cfg_over)
        return lam, std

    def test_obs53_generated_column_is_expected_not_red(self):
        undeclared = self.days({})
        for day in undeclared:
            self.assertIs(day["checks"]["schema_drift"], False, "without the declaration it is drift")
        for day in self.days({"expected_schema_differences": [GEN_COL]}):
            self.assertIs(day["checks"]["schema_drift"], True)
            d = day["detail"]["schema_drift"]["shop.items"]
            self.assertEqual((d["target_only"], d["status"]), ([], "expected"))
            self.assertEqual(d["expected"][0]["status"], f"expected ({GEN_COL['reason']})")

    def test_an_undeclared_extra_column_still_fails(self):
        for day in self.days({"expected_schema_differences": [GEN_COL]}, extra=("open_invoice_no", "surprise")):
            self.assertIs(day["checks"]["schema_drift"], False)
            d = day["detail"]["schema_drift"]["shop.items"]
            self.assertEqual(d["target_only"], ["surprise"])
            self.assertEqual([e["column"] for e in d["expected"]], ["open_invoice_no"])

    def test_same_family_shapes_type_kind_and_unexplained_failures(self):
        cfg = {"tables": ["t", "u", "v"], "expected_schema_differences": [
            {"table": "t", "column": "Amount", "side": "target", "kind": "type", "reason": "widened on purpose"},
            {"table": "u", "column": "x", "side": "target", "kind": "extra_column", "reason": "r"},
            {"table": "v", "column": "x", "side": "target", "kind": "extra_column", "reason": "r"}]}
        for mod in (L, soak_check):
            drift = {"pass": False, "detail": {
                "t": {"source_only": [], "target_only": [], "mismatched_attributes": {"amount": {"source": {}, "target": {}}}}}}
            self.assertIs(mod.apply_expected_differences(json.loads(json.dumps(cfg)), drift)["pass"], True)
            # no columns read at all (empty fingerprints) is never explained away
            drift = {"pass": False, "detail": {"u": {"source_only": [], "target_only": [], "mismatched_attributes": {}}}}
            self.assertIs(mod.apply_expected_differences(json.loads(json.dumps(cfg)), drift)["pass"], False)
            # a cross-family planning failure (note) stays a failure
            drift = {"pass": False, "detail": {"v": {"source_only": [], "target_only": ["x"], "mismatched_attributes": {},
                                                    "note": "could not plan"}}}
            self.assertIs(mod.apply_expected_differences(json.loads(json.dumps(cfg)), drift)["pass"], False)
            # the declaration names the target side; the same column on the source side is not covered
            drift = {"pass": False, "detail": {"u": {"source_only": ["x"], "target_only": [], "mismatched_attributes": {}}}}
            self.assertIs(mod.apply_expected_differences(json.loads(json.dumps(cfg)), drift)["pass"], False)
            # green / unknown drift is never touched
            for state in (True, None):
                self.assertIs(mod.apply_expected_differences(cfg, {"pass": state, "detail": {}})["pass"], state)

    @staticmethod
    def _cfgs(table, decl, side="target", engine="postgres"):
        """The same declaration in both scripts' config shapes."""
        item = {"table": table, "column": decl, "side": side, "kind": "extra_column", "reason": "generated"}
        std = {"tables": [table], "expected_schema_differences": [item], side: {"engine": engine}}
        lam = {"tables": [table], "expected_schema_differences": [item], f"{side}_engine": engine}
        return ((soak_check, std), (L, lam))

    def test_review13_unquoted_declaration_never_hides_a_distinct_quoted_pg_column(self):
        # declaring open_invoice_no must not also remove the distinct PG column "Open_Invoice_No"
        for mod, cfg in self._cfgs("t", "open_invoice_no"):
            drift = {"pass": False, "detail": {"t": {"source_only": [], "target_only": ["open_invoice_no", "Open_Invoice_No"],
                                                     "mismatched_attributes": {}}}}
            r = mod.apply_expected_differences(cfg, drift)
            self.assertIs(r["pass"], False, mod.__name__)
            self.assertEqual(r["detail"]["t"]["target_only"], ["Open_Invoice_No"])
            self.assertEqual([e["column"] for e in r["detail"]["t"]["expected"]], ["open_invoice_no"])
        # through the cross-family run as well
        for day in self.days({"expected_schema_differences": [GEN_COL]}, extra=("open_invoice_no", "Open_Invoice_No")):
            self.assertIs(day["checks"]["schema_drift"], False)
            self.assertEqual(day["detail"]["schema_drift"]["shop.items"]["target_only"], ["Open_Invoice_No"])

    def test_quoted_pg_declaration_matches_exactly_and_unquoted_folds(self):
        for mod, cfg in self._cfgs("t", '"Open_Invoice_No"'):
            drift = {"pass": False, "detail": {"t": {"source_only": [], "target_only": ["Open_Invoice_No"],
                                                     "mismatched_attributes": {}}}}
            self.assertIs(mod.apply_expected_differences(cfg, drift)["pass"], True, mod.__name__)
        for mod, cfg in self._cfgs("t", "Open_Invoice_No"):          # unquoted folds to open_invoice_no
            drift = {"pass": False, "detail": {"t": {"source_only": [], "target_only": ["Open_Invoice_No"],
                                                     "mismatched_attributes": {}}}}
            self.assertIs(mod.apply_expected_differences(cfg, drift)["pass"], False, mod.__name__)

    def test_mysql_declaration_is_case_insensitive_single_match(self):
        for mod, cfg in self._cfgs("t", "open_invoice_no", engine="mysql"):
            drift = {"pass": False, "detail": {"t": {"source_only": [], "target_only": ["OPEN_INVOICE_NO"],
                                                     "mismatched_attributes": {}}}}
            r = mod.apply_expected_differences(cfg, drift)
            self.assertIs(r["pass"], True, mod.__name__)
            self.assertEqual(r["detail"]["t"]["status"], "expected")

    def test_ambiguous_declaration_fails_and_suppresses_nothing(self):
        for mod, cfg in self._cfgs("t", "open_invoice_no", engine="mysql"):
            drift = {"pass": False, "detail": {"t": {"source_only": [], "target_only": ["open_invoice_no", "OPEN_INVOICE_NO"],
                                                     "mismatched_attributes": {}}}}
            r = mod.apply_expected_differences(cfg, drift)
            self.assertIs(r["pass"], False, mod.__name__)
            self.assertEqual(r["detail"]["t"]["target_only"], ["open_invoice_no", "OPEN_INVOICE_NO"])
            self.assertIn("ambiguous: matches 2", r["detail"]["t"]["expected_ambiguous"][0]["status"])

    @staticmethod
    def _type_cfgs(table, decl, side):
        """A `type` declaration with MySQL source / PostgreSQL target, in both config shapes."""
        item = {"table": table, "column": decl, "side": side, "kind": "type", "reason": "intentional type change"}
        std = {"tables": [table], "expected_schema_differences": [item],
               "source": {"engine": "mysql"}, "target": {"engine": "postgres"}}
        lam = {"tables": [table], "expected_schema_differences": [item],
               "source_engine": "mysql", "target_engine": "postgres"}
        return ((soak_check, std), (L, lam))

    @staticmethod
    def _type_drift(src_name, tgt_name):
        # cross-family shape: keyed by the SOURCE name, each side carries its catalog name
        return {"pass": False, "detail": {"t": {"source_only": [], "target_only": [], "mismatched_attributes": {
            src_name: {"source": {"name": src_name, "type": "decimal(10,2)"},
                       "target": {"name": tgt_name, "type": "numeric(12,4)"}, "finding": "type differs"}}}}}

    def test_review14_target_type_declaration_matches_the_target_catalog_name(self):
        # MySQL `Amount` -> PG `amount`: a target-side declaration `amount` is the approved one
        for mod, cfg in self._type_cfgs("t", "amount", "target"):
            r = mod.apply_expected_differences(cfg, self._type_drift("Amount", "amount"))
            self.assertIs(r["pass"], True, mod.__name__)
            self.assertEqual(r["detail"]["t"]["mismatched_attributes"], {})
            self.assertEqual(r["detail"]["t"]["status"], "expected")

    def test_review14_quoted_pg_target_name_needs_a_quoted_declaration(self):
        # MySQL `amount` -> PG "Amount": the quoted declaration matches, the unquoted one does not
        for mod, cfg in self._type_cfgs("t", '"Amount"', "target"):
            r = mod.apply_expected_differences(cfg, self._type_drift("amount", "Amount"))
            self.assertIs(r["pass"], True, mod.__name__)
        for mod, cfg in self._type_cfgs("t", "amount", "target"):
            r = mod.apply_expected_differences(cfg, self._type_drift("amount", "Amount"))
            self.assertIs(r["pass"], False, mod.__name__)
            self.assertIn("amount", r["detail"]["t"]["mismatched_attributes"])

    def test_source_side_type_declarations_still_match_the_source_name(self):
        for mod, cfg in self._type_cfgs("t", "AMOUNT", "source"):     # MySQL: case-insensitive
            r = mod.apply_expected_differences(cfg, self._type_drift("Amount", "amount"))
            self.assertIs(r["pass"], True, mod.__name__)
        for mod, cfg in self._type_cfgs("t", "other", "source"):
            r = mod.apply_expected_differences(cfg, self._type_drift("Amount", "amount"))
            self.assertIs(r["pass"], False, mod.__name__)
        # same-family shape (no per-side names): the shared key is the catalog name on both sides
        same = {"pass": False, "detail": {"t": {"source_only": [], "target_only": [], "mismatched_attributes": {
            "amount": {"source": {"type": "int"}, "target": {"type": "bigint"}}}}}}
        for mod, cfg in self._type_cfgs("t", "amount", "target"):
            r = mod.apply_expected_differences(cfg, json.loads(json.dumps(same)))
            self.assertIs(r["pass"], True, mod.__name__)

    def test_config_is_validated_before_any_connection(self):
        base = dict(GEN_COL)
        bad_items = [{**base, "extra": 1}, {k: v for k, v in base.items() if k != "reason"}, {**base, "reason": "  "},
                     {**base, "side": "both"}, {**base, "kind": "rename"}, {**base, "table": "shop.other"}, "x"]
        for mod in (L, soak_check):
            for bad in bad_items + ["not-a-list"]:
                cfg = {"tables": ["shop.items"], "expected_schema_differences": bad if bad == "not-a-list" else [bad]}
                with self.assertRaises(ValueError, msg=(mod.__name__, bad)):
                    mod.recheck_config(cfg)
        cfg = {"source": {"engine": "mysql", "host": "s"}, "target": {"engine": "postgres", "host": "t"},
               "tables": ["shop.items"], "expected_schema_differences": [{**base, "reason": ""}]}
        with mock.patch.object(soak_check, "_cross_connect") as conn, self.assertRaises(ValueError):
            soak_check.run_day(cfg, "2026-10-28")
        conn.assert_not_called()
        env = {**ENV, "EXPECTED_SCHEMA_DIFFERENCES": json.dumps([{**base, "table": "shop.orders"}])}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(L._load_config()["expected_schema_differences"][0]["table"], "shop.orders")
        with mock.patch.dict(os.environ, {**env, "EXPECTED_SCHEMA_DIFFERENCES": json.dumps([{**base, "kind": "x"}])}), \
                self.assertRaises(ValueError):
            L._load_config()
        with mock.patch.dict(os.environ, {**env, "EXPECTED_SCHEMA_DIFFERENCES": ""}):
            self.assertEqual(L._load_config()["expected_schema_differences"], [])

if __name__ == "__main__":
    unittest.main(verbosity=1)
