#!/usr/bin/env python3
"""Offline unit tests for shared/scripts/soak_check_lambda.py and soak_check.py — no AWS,
no database. Uses botocore Stubber for every AWS call and fake DB connections/cursors.

Run: python3 scripts/test_soak_scripts.py   (needs boto3 + pymysql importable)
"""
import contextlib
import datetime
import io
import json
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
        self.assertIn("re-issue on demand", warn)
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



if __name__ == "__main__":
    unittest.main(verbosity=1)
