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
    "DMS_TASK_ID": "TASK", "DMS_REPLICATION_INSTANCE_ID": "ri-1", "DMS_TASK_ARN": TASK_ARN,
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

    def pf_aws_ok(self, dms_metrics=("CDCLatencyTarget", "CDCLatencySource")):
        cw = self.stubs["pf_cloudwatch"]
        cw.add_response("describe_alarms", {"MetricAlarms": [{"AlarmName": "tgt-cpu", "StateValue": "OK"}]})
        cw.add_response("get_metric_statistics", {"Datapoints": []})  # RDS FreeStorageSpace
        for m in dms_metrics:
            cw.add_response("get_metric_statistics", {"Datapoints": []},
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
