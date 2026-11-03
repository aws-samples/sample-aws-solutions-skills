#!/usr/bin/env python3
"""Integrate same-account, same-region S3 Tables/Glue resources with a SMUS project.

inspect: read-only discovery and scoped IAM/LF plan.
apply: apply only the planned project-role inline policy and Glue association tags.
verify: query as the project; optionally run a named job and wait for success.
Never edits LF global settings, role trust, project membership, or SSO settings.
"""
import argparse
import json
import re
import time
from decimal import Decimal
from pathlib import Path

from deployment_gate import load_decision


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid {label}")
    return value


def validate_config(config):
    for key in ["account_id", "region", "prefix", "domain_id", "project_id",
                "table_bucket_arn", "namespace", "tables", "jobs"]:
        if key not in config:
            raise ValueError(f"Missing {key}")
    identifier(config["domain_id"], "domain_id")
    identifier(config["project_id"], "project_id")
    identifier(config["namespace"], "namespace")
    arn = config["table_bucket_arn"].split(":", 5)
    if (len(arn) != 6 or arn[0] != "arn" or arn[2] != "s3tables"
            or arn[3] != config["region"] or arn[4] != config["account_id"]
            or not re.fullmatch(r"bucket/[a-z0-9][a-z0-9-]*", arn[5])):
        raise ValueError("Table bucket must belong to the chosen account and region")
    for key in ["tables", "jobs"]:
        if not isinstance(config[key], list) or not config[key]:
            raise ValueError(f"{key} must be a nonempty list")
        if len(set(config[key])) != len(config[key]):
            raise ValueError(f"Duplicate {key}")
        for name in config[key]:
            identifier(name, key)
    identifier(config.get("policy_name", "PipelineSmusDatasetAccess"), "policy_name")
    if config.get("query_region", config["region"]) != config["region"]:
        raise ValueError("This adapter requires a separately reviewed design for split-region access")
    for check in config.get("checks", []):
        if check.get("table") not in config["tables"]:
            raise ValueError("Check must target a configured table")
        if type(check.get("expected_count")) is not int or check["expected_count"] < 0:
            raise ValueError("Check expected_count must be a nonnegative integer")
        if "sum_column" in check:
            identifier(check["sum_column"], "sum_column")
            if not isinstance(check.get("expected_sum"), str):
                raise ValueError("expected_sum must be a decimal string")
            if not Decimal(check["expected_sum"]).is_finite():
                raise ValueError("expected_sum must be finite")
    return config


def clean(response):
    return {k: v for k, v in response.items() if k != "ResponseMetadata"}


def discover(session, config):
    account = session.client("sts").get_caller_identity()["Account"]
    if account != config["account_id"]:
        raise ValueError("Active AWS identity does not match customer decision")
    dz = session.client("datazone")
    domain = dz.get_domain(identifier=config["domain_id"])
    if domain.get("domainVersion") != "V2":
        raise ValueError("Target must be a SageMaker Unified Studio V2 domain")
    project = dz.get_project(domainIdentifier=config["domain_id"], identifier=config["project_id"])
    if project.get("projectStatus") != "ACTIVE":
        raise ValueError("Project must be ACTIVE")
    environments = []
    request = {"domainIdentifier": config["domain_id"], "projectIdentifier": config["project_id"]}
    while True:
        page = dz.list_environments(**request)
        environments.extend(x for x in page["items"] if x["status"] == "ACTIVE")
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    candidates = []
    for item in environments:
        if config.get("environment_id") and item["id"] != config["environment_id"]:
            continue
        env = dz.get_environment(domainIdentifier=config["domain_id"], identifier=item["id"])
        if (env.get("awsAccountId") != config["account_id"]
                or env.get("awsAccountRegion") != config["region"]
                or env.get("projectId") != config["project_id"]
                or env.get("domainId") != config["domain_id"]):
            if config.get("environment_id"):
                raise ValueError("Selected environment does not match the account, region and project")
            continue
        resources = {x["name"]: x["value"] for x in env.get("provisionedResources", [])}
        if "userRoleArn" in resources and "athenaWorkGroupName" in resources:
            candidates.append((env, resources))
    if len(candidates) != 1:
        raise ValueError("Select environment_id: exactly one ACTIVE SQL Tooling environment is required")
    env, resources = candidates[0]
    role_arn = resources["userRoleArn"]
    partition = config["table_bucket_arn"].split(":")[1]
    if not role_arn.startswith(f"arn:{partition}:iam::{account}:role/"):
        raise ValueError("Discovered project role is outside the selected account")
    bucket_name = config["table_bucket_arn"].split("/")[-1]
    catalog = "s3tablescatalog/" + bucket_name
    catalog_id = account + ":" + catalog
    glue = session.client("glue")
    glue.get_catalog(CatalogId=catalog_id)
    table_arns = []
    for name in config["tables"]:
        table = session.client("s3tables").get_table(
            tableBucketARN=config["table_bucket_arn"], namespace=config["namespace"], name=name)
        if not table["tableARN"].startswith(config["table_bucket_arn"] + "/table/"):
            raise ValueError("Discovered table ARN does not belong to the selected bucket")
        table_arns.append(table["tableARN"])
    desired_tags = {"AmazonDataZoneDomain": config["domain_id"],
                    "AmazonDataZoneProject": config["project_id"]}
    jobs = []
    for name in config["jobs"]:
        glue.get_job(JobName=name)
        arn = f"arn:{partition}:glue:{config['region']}:{account}:job/{name}"
        tags = glue.get_tags(ResourceArn=arn)["Tags"]
        for key, value in desired_tags.items():
            if key in tags and tags[key] != value:
                raise ValueError(f"Job {name} belongs to a different SMUS project/domain; do not reassign it")
        jobs.append({"name": name, "arn": arn, "tags": tags})
    base = f"arn:{partition}:glue:{config['region']}:{account}:"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["glue:GetCatalog", "glue:GetCatalogs", "glue:GetDatabase",
         "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
         "Resource": [base + "catalog", base + "catalog/s3tablescatalog", base + "catalog/" + catalog,
                      base + "database/" + catalog + "/" + config["namespace"]]
                     + [base + "table/" + catalog + "/" + config["namespace"] + "/" + n
                        for n in config["tables"]]},
        # Lake Formation GetDataAccess does not support resource-level IAM scoping.
        # Actual database/table access is restricted by LF grants below.
        {"Effect": "Allow", "Action": ["lakeformation:GetDataAccess"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace",
         "s3tables:ListNamespaces", "s3tables:ListTables"], "Resource": [config["table_bucket_arn"]]},
        {"Effect": "Allow", "Action": ["s3tables:GetTable", "s3tables:GetTableMetadataLocation",
         "s3tables:GetTableData"], "Resource": table_arns},
        {"Effect": "Allow", "Action": ["glue:GetJob", "glue:GetJobRun", "glue:GetJobRuns",
         "glue:StartJobRun", "glue:BatchStopJobRun"], "Resource": [j["arn"] for j in jobs]},
    ]}
    return {"domain_id": config["domain_id"], "project_id": config["project_id"],
            "environment_id": env["id"], "project_role_arn": role_arn,
            "project_role_name": role_arn.rsplit("/", 1)[1],
            "workgroup": resources["athenaWorkGroupName"],
            "catalog": catalog, "catalog_id": catalog_id,
            "policy_name": config.get("policy_name", "PipelineSmusDatasetAccess"),
            "policy": policy, "jobs": jobs, "desired_tags": desired_tags,
            "required_lf_grants": [
                {"principal": role_arn, "catalog_id": catalog_id,
                 "database": config["namespace"], "permissions": ["DESCRIBE"]},
                *[{"principal": role_arn, "catalog_id": catalog_id,
                   "database": config["namespace"], "table": name,
                   "permissions": ["DESCRIBE", "SELECT"]} for name in config["tables"]]],
            "lakeformation_settings": clean(session.client("lakeformation").get_data_lake_settings())}


def apply(session, plan):
    """Idempotent bounded mutations, only after complete read-only discovery succeeds."""
    iam, glue = session.client("iam"), session.client("glue")
    try:
        old = iam.get_role_policy(RoleName=plan["project_role_name"],
                                  PolicyName=plan["policy_name"])["PolicyDocument"]
    except iam.exceptions.NoSuchEntityException:
        old = None
    changed = []
    if old != plan["policy"]:
        iam.put_role_policy(RoleName=plan["project_role_name"], PolicyName=plan["policy_name"],
                            PolicyDocument=json.dumps(plan["policy"]))
        changed.append("project_role_policy")
    for job in plan["jobs"]:
        if any(job["tags"].get(k) != v for k, v in plan["desired_tags"].items()):
            glue.tag_resource(ResourceArn=job["arn"], TagsToAdd=plan["desired_tags"])
            changed.append("job_tags:" + job["name"])
    return {"changed": changed, "lf_grants_applied": False}


def wait_for(fetch, state_key, success, failures, timeout=180, interval=2):
    deadline = time.monotonic() + timeout
    while True:
        result = fetch()
        state = result[state_key]
        if state == success:
            return result
        if state in failures:
            raise RuntimeError(f"Execution failed: {state}")
        if time.monotonic() >= deadline:
            raise TimeoutError("Execution has not completed; do not report success")
        time.sleep(interval)


def verify(session, config, plan, session_factory, run_job=None):
    if not config.get("checks"):
        raise ValueError("Provide source-derived expected counts and sums before verification")
    if run_job and run_job not in config["jobs"]:
        raise ValueError("Only a configured job may be run")
    credentials = session.client("datazone").get_environment_credentials(
        domainIdentifier=config["domain_id"], environmentIdentifier=plan["environment_id"])
    project = session_factory(
        region_name=config["region"], aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials["sessionToken"])
    # No credential values are included in the plan, result, or logs.
    identity = project.client("sts").get_caller_identity()["Arn"]
    expected = (f"arn:{config['table_bucket_arn'].split(':')[1]}:sts::{config['account_id']}:"
                f"assumed-role/{plan['project_role_name']}/")
    if not identity.startswith(expected):
        raise ValueError("Vended identity is not the discovered project execution role")
    report = {"project_identity": identity, "idc_signin_tested": False, "checks": []}
    if run_job:
        glue = project.client("glue")
        run_id = glue.start_job_run(JobName=run_job)["JobRunId"]
        report["job"] = {"name": run_job, "run_id": run_id}
        try:
            run = wait_for(lambda: glue.get_job_run(JobName=run_job, RunId=run_id)["JobRun"],
                           "JobRunState", "SUCCEEDED",
                           {"FAILED", "STOPPED", "TIMEOUT", "ERROR", "EXPIRED"}, timeout=660)
        except TimeoutError:
            # A bounded acceptance run should not be left billing after our wait expires.
            stopped = glue.batch_stop_job_run(JobName=run_job, JobRunIds=[run_id])
            raise TimeoutError(f"Glue run {run_id} timed out; stop requested: "
                               f"{bool(stopped.get('SuccessfulSubmissions'))}") from None
        except RuntimeError as error:
            raise RuntimeError(f"Glue run {run_id}: {error}") from error
        report["job"]["state"] = run["JobRunState"]
    athena = project.client("athena")
    for check in config["checks"]:
        project.client("glue").get_table(CatalogId=plan["catalog_id"],
                                         DatabaseName=config["namespace"], Name=check["table"])
        expression = "COUNT(*)"
        if "sum_column" in check:
            expression += f', SUM("{check["sum_column"]}")'
        sql = f'SELECT {expression} FROM "{config["namespace"]}"."{check["table"]}"'
        q = athena.start_query_execution(
            QueryString=sql, QueryExecutionContext={"Catalog": plan["catalog"],
                                                    "Database": config["namespace"]},
            WorkGroup=plan["workgroup"])["QueryExecutionId"]
        try:
            wait_for(lambda: athena.get_query_execution(QueryExecutionId=q)["QueryExecution"]["Status"],
                     "State", "SUCCEEDED", {"FAILED", "CANCELLED"})
        except TimeoutError:
            athena.stop_query_execution(QueryExecutionId=q)
            raise TimeoutError(f"Query {q} timed out; cancellation requested") from None
        except RuntimeError as error:
            raise RuntimeError(f"Athena query {q}: {error}") from error
        rows = athena.get_query_results(QueryExecutionId=q)["ResultSet"]["Rows"]
        if len(rows) != 2:
            raise AssertionError("Expected exactly one aggregate result row")
        values = [x.get("VarCharValue") for x in rows[1]["Data"]]
        passed = values[0] == str(check["expected_count"])
        if "sum_column" in check:
            passed = passed and values[1] is not None and Decimal(values[1]) == Decimal(check["expected_sum"])
        report["checks"].append({"table": check["table"], "query_id": q,
                                 "actual": values, "passed": passed})
    report["passed"] = all(x["passed"] for x in report["checks"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "apply", "verify"])
    parser.add_argument("--decision", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-job")
    args = parser.parse_args()
    report_path = None
    try:
        output = Path(args.output)
        if output.resolve() in {Path(args.config).resolve(), Path(args.decision).resolve()}:
            raise ValueError("Output must not overwrite the config or customer decision")
        if not output.parent.is_dir():
            raise ValueError("Output directory must exist")
        report_path = output
        config = json.loads(Path(args.config).read_text())
        load_decision(args.decision, config["account_id"], config["region"], config["prefix"], "smus")
        validate_config(config)
        if args.run_job and args.action != "verify":
            raise ValueError("--run-job is only valid with verify")
        import boto3
        session = boto3.Session(region_name=config["region"])
        plan = discover(session, config)
        if args.action == "inspect":
            result = plan
        elif args.action == "apply":
            result = apply(session, plan)
        else:
            result = verify(session, config, plan, boto3.Session, args.run_job)
        output.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(json.dumps({"action": args.action, "output": str(output),
                          "passed": result.get("passed"), "changed": result.get("changed")}))
        return 1 if result.get("passed") is False else 0
    except Exception as error:
        # Do not leave a previous successful report in place after a failed run.
        # This is local evidence, not an AWS mutation or a customer confirmation.
        if report_path is not None:
            try:
                report_path.write_text(json.dumps({
                    "action": args.action, "passed": False,
                    "error_type": type(error).__name__, "error": str(error),
                }, indent=2) + "\n")
            except OSError:
                pass
        parser.exit(2, f"SMUS step stopped: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
