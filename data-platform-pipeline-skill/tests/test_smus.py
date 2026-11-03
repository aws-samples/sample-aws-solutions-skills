"""Offline behavior tests. No AWS credentials or cloud mutations are used."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SKILL = Path(__file__).resolve().parents[1]
BUNDLE = SKILL / "claude-code/skills/data-platform-pipeline"
sys.path.insert(0, str(BUNDLE / "scripts"))
import deployment_gate as gate
import smus_project as smus

ACCOUNT = "111122223333"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/project-role"
BUCKET = f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/acme-table-bucket"
CONFIG = {"account_id": ACCOUNT, "region": REGION, "prefix": "acme",
          "domain_id": "dzd-test", "project_id": "project-test",
          "table_bucket_arn": BUCKET, "namespace": "acme_db",
          "tables": ["orders_clean"], "jobs": ["acme-ingest"],
          "checks": [{"table": "orders_clean", "expected_count": 6,
                      "sum_column": "line_total", "expected_sum": "100.00"}]}


def decision(mode=True):
    return {"account_id": ACCOUNT, "region": REGION, "prefix": "acme", "use_smus": mode,
            "customer_decision": {"source": "customer", "confirmed": True,
                                  "answer": "Use SMUS." if mode else "Build without SMUS."}}


class FakeSession:
    def __init__(self):
        self.clients = {name: MagicMock() for name in
                        ["sts", "datazone", "s3tables", "glue", "iam", "lakeformation", "athena"]}
        self.clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
        dz = self.clients["datazone"]
        dz.get_domain.return_value = {"domainVersion": "V2"}
        dz.get_project.return_value = {"projectStatus": "ACTIVE"}
        dz.list_environments.return_value = {"items": [{"id": "env-test", "status": "ACTIVE"}]}
        dz.get_environment.return_value = {"id": "env-test",
            "awsAccountId": ACCOUNT, "awsAccountRegion": REGION,
            "projectId": CONFIG["project_id"], "domainId": CONFIG["domain_id"], "provisionedResources": [
            {"name": "userRoleArn", "value": ROLE},
            {"name": "athenaWorkGroupName", "value": "project-workgroup"}]}
        self.clients["s3tables"].get_table.return_value = {"tableARN": BUCKET + "/table/123"}
        self.clients["glue"].get_tags.return_value = {"Tags": {
            "Owner": "customer", "AmazonDataZoneDomain": "dzd-test",
            "AmazonDataZoneProject": "project-test"}}
        self.clients["lakeformation"].get_data_lake_settings.return_value = {
            "DataLakeSettings": {"DataLakeAdmins": [{"DataLakePrincipalIdentifier": "existing-admin"}]}}
        self.clients["iam"].exceptions.NoSuchEntityException = type("NoSuchEntity", (Exception,), {})

    def client(self, name):
        return self.clients[name]

    def project(self):
        self.clients["sts"].get_caller_identity.return_value = {
            "Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/project-role/session"}
        athena = self.clients["athena"]
        athena.start_query_execution.return_value = {"QueryExecutionId": "query-test"}
        athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        self.result("6", "100.00")
        glue = self.clients["glue"]
        glue.start_job_run.return_value = {"JobRunId": "run-test"}
        glue.get_job_run.return_value = {"JobRun": {"JobRunState": "SUCCEEDED"}}
        return self

    def result(self, count, total):
        self.clients["athena"].get_query_results.return_value = {"ResultSet": {"Rows": [
            {"Data": [{"VarCharValue": "count"}, {"VarCharValue": "sum"}]},
            {"Data": [{"VarCharValue": count}, {"VarCharValue": total}]},
        ]}}


class DecisionGateTests(unittest.TestCase):
    def run_gate(self, value, mode, **target):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record, marker = root / "decision.json", root / "child-ran"
            if value is not None:
                record.write_text(json.dumps(value))
            args = [sys.executable, str(BUNDLE / "scripts/deployment_gate.py"),
                    "--decision", str(record), "--account", target.get("account", ACCOUNT),
                    "--region", target.get("region", REGION), "--prefix", target.get("prefix", "acme"),
                    "--mode", mode, "--", sys.executable, "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)]
            result = subprocess.run(args, capture_output=True, text=True)
            return result.returncode, marker.exists(), result.stderr

    def test_both_explicit_choices_execute_correct_branch(self):
        for choice, mode in [(True, "smus"), (False, "standalone")]:
            with self.subTest(mode=mode):
                code, ran, _ = self.run_gate(decision(choice), mode)
                self.assertEqual(code, 0)
                self.assertTrue(ran)

    def test_unresolved_or_fabricated_shape_never_starts_command(self):
        variants = [None, {}, [], {**decision(), "use_smus": "false"},
                    {**decision(), "use_smus": 1}, {**decision(), "use_smus": None}]
        for change in [{"confirmed": False}, {"confirmed": "true"}, {"source": "inferred"},
                       {"answer": ""}, {"answer": "   "}, {"answer": None}]:
            item = decision()
            item["customer_decision"].update(change)
            variants.append(item)
        for value in variants:
            with self.subTest(value=value):
                code, ran, _ = self.run_gate(value, "smus")
                self.assertNotEqual(code, 0)
                self.assertFalse(ran)

    def test_mode_conflict_never_starts_command(self):
        for choice, mode in [(True, "standalone"), (False, "smus")]:
            code, ran, _ = self.run_gate(decision(choice), mode)
            self.assertNotEqual(code, 0)
            self.assertFalse(ran)

    def test_changed_account_region_or_project_requires_new_decision(self):
        for target in [{"account": "999988887777"}, {"region": "eu-west-1"}, {"prefix": "other"}]:
            code, ran, _ = self.run_gate(decision(), "smus", **target)
            self.assertNotEqual(code, 0)
            self.assertFalse(ran)

    def test_existing_long_prefix_is_not_rejected_by_platform_choice_gate(self):
        value = {**decision(False), "prefix": "existing-customer-data-platform"}
        code, ran, _ = self.run_gate(value, "standalone", prefix=value["prefix"])
        self.assertEqual(code, 0)
        self.assertTrue(ran)

    def test_adapter_blocks_standalone_before_importing_aws_sdk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "decision.json").write_text(json.dumps(decision(False)))
            (root / "config.json").write_text(json.dumps(CONFIG))
            (root / "out.json").write_text(json.dumps({"passed": True}))
            # If the SDK is imported before validating the choice, this sentinel fires.
            (root / "boto3.py").write_text("raise RuntimeError('SDK_USED_BEFORE_GATE')")
            import os
            env = dict(os.environ, PYTHONPATH=str(root))
            result = subprocess.run([sys.executable, str(BUNDLE / "scripts/smus_project.py"),
                                     "apply", "--decision", str(root / "decision.json"),
                                     "--config", str(root / "config.json"),
                                     "--output", str(root / "out.json")],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("conflicts", result.stderr)
            self.assertNotIn("SDK_USED_BEFORE_GATE", result.stderr)
            self.assertFalse(json.loads((root / "out.json").read_text())["passed"])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.config = copy.deepcopy(CONFIG)

    def test_config_rejects_unreviewed_scope_and_bad_expectations(self):
        for change in [
            {"table_bucket_arn": BUCKET.replace(ACCOUNT, "999988887777")},
            {"table_bucket_arn": BUCKET.replace(REGION, "eu-west-1")},
            {"query_region": "eu-west-1"}, {"tables": []},
            {"jobs": ["acme-ingest", "acme-ingest"]}, {"namespace": 'db"; DROP TABLE x;'},
            {"checks": [{"table": "other", "expected_count": 6}]},
            {"checks": [{"table": "orders_clean", "expected_count": True}]},
            {"checks": [{"table": "orders_clean", "expected_count": 6,
                         "sum_column": "line_total", "expected_sum": 100.0}]},
            {"checks": [{"table": "orders_clean", "expected_count": 6,
                         "sum_column": "line_total", "expected_sum": "NaN"}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                smus.validate_config({**self.config, **change})

    def test_discovery_is_read_only_and_resources_are_scoped(self):
        plan = smus.discover(self.session, self.config)
        self.assertEqual(plan["project_role_arn"], ROLE)
        self.assertEqual(plan["workgroup"], "project-workgroup")
        self.assertEqual(plan["required_lf_grants"][1]["permissions"], ["DESCRIBE", "SELECT"])
        self.assertNotIn("ALL", str(plan["required_lf_grants"]))
        for statement in plan["policy"]["Statement"]:
            if statement["Resource"] == "*":
                self.assertEqual(statement["Action"], ["lakeformation:GetDataAccess"])
        for name in ["iam", "athena"]:
            self.assertEqual(self.session.clients[name].mock_calls, [])
        self.session.clients["glue"].tag_resource.assert_not_called()
        self.session.clients["lakeformation"].put_data_lake_settings.assert_not_called()

    def test_wrong_active_account_stops_before_domain_discovery(self):
        self.session.clients["sts"].get_caller_identity.return_value = {"Account": "999988887777"}
        with self.assertRaises(ValueError):
            smus.discover(self.session, self.config)
        self.session.clients["datazone"].get_domain.assert_not_called()

    def test_inactive_or_non_unified_targets_fail(self):
        for domain, project in [({"domainVersion": "V1"}, {"projectStatus": "ACTIVE"}),
                                ({"domainVersion": "V2"}, {"projectStatus": "FAILED"})]:
            self.session.clients["datazone"].get_domain.return_value = domain
            self.session.clients["datazone"].get_project.return_value = project
            with self.assertRaises(ValueError):
                smus.discover(self.session, self.config)

    def test_environment_pagination_and_explicit_selection(self):
        dz = self.session.clients["datazone"]
        dz.list_environments.side_effect = [
            {"items": [{"id": "old", "status": "DELETE_FAILED"}], "nextToken": "page2"},
            {"items": [{"id": "env-test", "status": "ACTIVE"}]}]
        plan = smus.discover(self.session, self.config)
        self.assertEqual(plan["environment_id"], "env-test")
        self.assertEqual(dz.list_environments.call_args.kwargs["nextToken"], "page2")

    def test_ambiguous_environments_fail_without_mutation(self):
        self.session.clients["datazone"].list_environments.return_value = {
            "items": [{"id": "a", "status": "ACTIVE"}, {"id": "b", "status": "ACTIVE"}]}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            smus.discover(self.session, self.config)
        self.session.clients["iam"].put_role_policy.assert_not_called()

    def test_environment_region_mismatch_fails_before_mutation(self):
        self.session.clients["datazone"].get_environment.return_value["awsAccountRegion"] = "eu-west-1"
        with self.assertRaisesRegex(ValueError, "does not match"):
            smus.discover(self.session, {**self.config, "environment_id": "env-test"})
        self.session.clients["iam"].put_role_policy.assert_not_called()

    def test_foreign_project_association_cannot_be_overwritten(self):
        self.session.clients["glue"].get_tags.return_value = {
            "Tags": {"AmazonDataZoneProject": "someone-elses-project"}}
        with self.assertRaisesRegex(ValueError, "different SMUS project"):
            smus.discover(self.session, self.config)
        self.session.clients["iam"].put_role_policy.assert_not_called()
        self.session.clients["glue"].tag_resource.assert_not_called()

    def test_discovered_foreign_table_or_role_fails(self):
        self.session.clients["s3tables"].get_table.return_value = {"tableARN": "arn:other"}
        with self.assertRaises(ValueError):
            smus.discover(self.session, self.config)
        self.session = FakeSession()
        self.session.clients["datazone"].get_environment.return_value["provisionedResources"][0]["value"] = (
            ROLE.replace(ACCOUNT, "999988887777"))
        with self.assertRaises(ValueError):
            smus.discover(self.session, self.config)

    def test_idempotent_apply_makes_zero_changes(self):
        plan = smus.discover(self.session, self.config)
        iam = self.session.clients["iam"]
        iam.get_role_policy.return_value = {"PolicyDocument": plan["policy"]}
        self.assertEqual(smus.apply(self.session, plan)["changed"], [])
        iam.put_role_policy.assert_not_called()
        self.session.clients["glue"].tag_resource.assert_not_called()
        self.session.clients["lakeformation"].grant_permissions.assert_not_called()
        self.session.clients["lakeformation"].put_data_lake_settings.assert_not_called()

    def test_apply_preserves_unrelated_tags_and_has_no_lf_mutations(self):
        self.session.clients["glue"].get_tags.return_value = {"Tags": {"Owner": "customer"}}
        plan = smus.discover(self.session, self.config)
        iam = self.session.clients["iam"]
        iam.get_role_policy.side_effect = iam.exceptions.NoSuchEntityException()
        result = smus.apply(self.session, plan)
        self.assertEqual(result["changed"], ["project_role_policy", "job_tags:acme-ingest"])
        tags = self.session.clients["glue"].tag_resource.call_args.kwargs["TagsToAdd"]
        self.assertEqual(set(tags), {"AmazonDataZoneDomain", "AmazonDataZoneProject"})
        self.session.clients["glue"].untag_resource.assert_not_called()
        self.session.clients["lakeformation"].put_data_lake_settings.assert_not_called()

    def project_verification(self, project=None, config=None, run_job=None):
        plan = smus.discover(self.session, self.config)
        self.session.clients["datazone"].get_environment_credentials.return_value = {
            "accessKeyId": "TEST_ACCESS", "secretAccessKey": "TEST_SECRET", "sessionToken": "TEST_TOKEN"}
        project = project or FakeSession().project()
        return smus.verify(self.session, config or self.config, plan, lambda **kwargs: project, run_job)

    def test_project_role_job_then_exact_decimal_query_passes_without_leaking_credentials(self):
        project = FakeSession().project()
        report = self.project_verification(project, run_job="acme-ingest")
        self.assertTrue(report["passed"])
        self.assertEqual(report["job"]["state"], "SUCCEEDED")
        self.assertFalse(report["idc_signin_tested"])
        self.assertNotIn("TEST_SECRET", json.dumps(report))
        self.assertNotIn("TEST_TOKEN", json.dumps(report))
        call = project.clients["athena"].start_query_execution.call_args.kwargs
        self.assertEqual(call["WorkGroup"], "project-workgroup")
        self.assertIn('SUM("line_total")', call["QueryString"])
        self.session.clients["athena"].start_query_execution.assert_not_called()

    def test_wrong_rows_or_totals_fail_reconciliation(self):
        for count, total in [("7", "100.00"), ("6", "100.01"), ("6", "0.00")]:
            project = FakeSession().project()
            project.result(count, total)
            self.assertFalse(self.project_verification(project)["passed"])

    def test_wrong_vended_identity_fails_before_query(self):
        project = FakeSession().project()
        project.clients["sts"].get_caller_identity.return_value["Arn"] = (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/session")
        with self.assertRaisesRegex(ValueError, "not the discovered"):
            self.project_verification(project)
        project.clients["athena"].start_query_execution.assert_not_called()

    def test_missing_expected_data_or_unlisted_job_stops_before_vending(self):
        for config, job in [({**self.config, "checks": []}, None), (self.config, "other-job")]:
            with self.assertRaises(ValueError):
                self.project_verification(config=config, run_job=job)
            self.session.clients["datazone"].get_environment_credentials.assert_not_called()

    def test_athena_and_glue_failure_are_not_success(self):
        for service in ["athena", "glue"]:
            project = FakeSession().project()
            if service == "athena":
                project.clients["athena"].get_query_execution.return_value = {
                    "QueryExecution": {"Status": {"State": "FAILED"}}}
            else:
                project.clients["glue"].get_job_run.return_value = {"JobRun": {"JobRunState": "FAILED"}}
            with self.assertRaises(RuntimeError):
                self.project_verification(project, run_job="acme-ingest" if service == "glue" else None)

    def test_timeout_cancels_only_the_test_query(self):
        project = FakeSession().project()
        with patch.object(smus, "wait_for", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.project_verification(project)
        project.clients["athena"].stop_query_execution.assert_called_once_with(QueryExecutionId="query-test")

    def test_timeout_requests_stop_for_only_started_job(self):
        project = FakeSession().project()
        with patch.object(smus, "wait_for", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.project_verification(project, run_job="acme-ingest")
        project.clients["glue"].batch_stop_job_run.assert_called_once_with(
            JobName="acme-ingest", JobRunIds=["run-test"])
        project.clients["athena"].start_query_execution.assert_not_called()

    def test_wait_polls_until_success_and_fails_closed(self):
        with patch.object(smus.time, "sleep"):
            fetch = MagicMock(side_effect=[{"State": "QUEUED"}, {"State": "SUCCEEDED"}])
            self.assertEqual(smus.wait_for(fetch, "State", "SUCCEEDED", {"FAILED"})["State"], "SUCCEEDED")
            with self.assertRaises(TimeoutError):
                smus.wait_for(lambda: {"State": "RUNNING"}, "State", "SUCCEEDED", {"FAILED"}, timeout=0)


class BundleTests(unittest.TestCase):
    def test_all_three_installable_bundles_are_identical(self):
        def files(root):
            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
        canonical = files(BUNDLE)
        for tool in ["kiro", "codex"]:
            self.assertEqual(canonical, files(SKILL / tool / "skills/data-platform-pipeline"))


if __name__ == "__main__":
    unittest.main()
