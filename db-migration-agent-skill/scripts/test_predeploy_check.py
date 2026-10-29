#!/usr/bin/env python3
"""Synthetic-template tests for shared/scripts/predeploy_check.py (stdlib only, no AWS).

Run: python3 -B -m unittest scripts/test_predeploy_check.py
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent / "shared" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import predeploy_check as pc  # noqa: E402

PREFIX = "dryrun5"


def tmpl(resources, name="dryrun5-cache", parameters=None, params=None):
    body = {"Resources": resources}
    if parameters:
        body["Parameters"] = parameters
    t = pc.Template(Path(f"{name or 'loose'}.template.json"), name, body)
    if params:
        t.params.update(params)
    return t


def res(rtype, **props):
    return {"Type": rtype, "Properties": props}


def codes(findings, level=None):
    return sorted(f.code for f in findings if level is None or f.level == level)


class SecurityGroupCharsetTests(unittest.TestCase):
    def test_ascii_descriptions_pass(self):
        t = tmpl({"Sg": res("AWS::EC2::SecurityGroup", GroupDescription="dryrun5 target DB",
                            SecurityGroupIngress=[{"Description": "app client sg-0abc (11211)"}],
                            SecurityGroupEgress=[{"Description": "DMS to target DB"}]),
                  "In": res("AWS::EC2::SecurityGroupIngress", Description="helper: ssm/[a]{b}!$*")})
        self.assertEqual(pc.check_sg_text(t), [])

    def test_bad_characters_fail_inline_and_standalone(self):
        t = tmpl({"Sg": res("AWS::EC2::SecurityGroup", GroupDescription="DMS -> target DB",
                            SecurityGroupIngress=[{"Description": "앱 서버"}],
                            SecurityGroupEgress=[{"Description": "to source — VPN"}]),
                  "Eg": res("AWS::EC2::SecurityGroupEgress", Description="'quoted'"),
                  "In": res("AWS::EC2::SecurityGroupIngress", Description="a → b")})
        found = pc.check_sg_text(t)
        self.assertEqual(len(found), 5)
        self.assertTrue(all(f.level == "FAIL" and f.code == "SG-CHARSET" for f in found))
        self.assertEqual({f.prop for f in found},
                         {"GroupDescription", "SecurityGroupIngress[0].Description",
                          "SecurityGroupEgress[0].Description", "Description"})

    def test_length_and_groupname_rules(self):
        t = tmpl({"Long": res("AWS::EC2::SecurityGroup", GroupDescription="x" * 256),
                  "Max": res("AWS::EC2::SecurityGroup", GroupDescription="x" * 255),
                  "Named": res("AWS::EC2::SecurityGroup", GroupDescription="ok",
                               GroupName="sg-dryrun5")})
        found = pc.check_sg_text(t)
        self.assertEqual(sorted(f.logical_id for f in found), ["Long", "Named"])

    def test_join_and_sub_literal_fragments_checked(self):
        t = tmpl({"J": res("AWS::EC2::SecurityGroup", GroupDescription={
                      "Fn::Join": ["", ["DMS -> ", {"Ref": "AWS::StackName"}]]}),
                  "S": res("AWS::EC2::SecurityGroup", GroupDescription={
                      "Fn::Sub": "app client ${SgId}"}),
                  "S2": res("AWS::EC2::SecurityGroup", GroupDescription={
                      "Fn::Sub": ["${Name} — cache", {"Name": "x"}]})})
        found = pc.check_sg_text(t)
        by = {f.logical_id: (f.level, f.code) for f in found}
        self.assertEqual(by, {"J": ("FAIL", "SG-CHARSET"), "S2": ("FAIL", "SG-CHARSET"),
                              "S": ("WARN", "UNRESOLVED")})   # ${SgId}: unknown, not proven ASCII

    def test_sub_substitution_map_and_parameters_are_resolved(self):
        t = tmpl({"Map": res("AWS::EC2::SecurityGroup", GroupDescription={
                      "Fn::Sub": ["${Description}", {"Description": "앱 서버"}]}),
                  "Param": res("AWS::EC2::SecurityGroup", GroupDescription={"Ref": "Desc"}),
                  "ParamOk": res("AWS::EC2::SecurityGroup", GroupDescription={"Ref": "OkDesc"}),
                  "Escaped": res("AWS::EC2::SecurityGroup", GroupDescription={"Fn::Sub": "cost ${!x}"}),
                  "Long": res("AWS::EC2::SecurityGroup", GroupDescription={
                      "Fn::Join": ["", ["x" * 200, {"Ref": "Pad"}]]})},
                 parameters={"Desc": {"Type": "String", "Default": "DMS → target"},
                             "OkDesc": {"Type": "String", "Default": "dryrun5 ok"},
                             "Pad": {"Type": "String", "Default": "y" * 60}})
        by = {f.logical_id: (f.level, f.code) for f in pc.check_sg_text(t)}
        self.assertEqual(by, {"Map": ("FAIL", "SG-CHARSET"), "Param": ("FAIL", "SG-CHARSET"),
                              "Long": ("FAIL", "SG-CHARSET")})

    def test_groupid_tokens_are_charset_safe(self):
        t = tmpl({"Peer": res("AWS::EC2::SecurityGroup", GroupDescription="peer"),
                  "In": res("AWS::EC2::SecurityGroupIngress", Description={
                      "Fn::Join": ["", ["from ", {"Fn::GetAtt": ["Peer", "GroupId"]}, ":5432"]]}),
                  "In2": res("AWS::EC2::SecurityGroupIngress", Description={
                      "Fn::Sub": "from ${Peer} and ${Peer.GroupId}"})})
        self.assertEqual(pc.check_sg_text(t), [])


class DmsSslTests(unittest.TestCase):
    def test_require_on_mysql_family_fails(self):
        for engine in ("mysql", "mariadb", "aurora", "MySQL"):
            t = tmpl({"Ep": res("AWS::DMS::Endpoint", EngineName=engine, EndpointType="source",
                                SslMode="require", EndpointIdentifier="dryrun5-src")})
            self.assertEqual(codes(pc.check_dms_ssl(t)), ["DMS-SSL"], engine)

    def test_supported_modes_and_other_engines_pass(self):
        resources = {f"Ep{i}": res("AWS::DMS::Endpoint", EngineName=e, EndpointType="target",
                                   SslMode=m)
                     for i, (e, m) in enumerate([("mysql", "verify-ca"), ("aurora", "verify-full"),
                                                 ("mariadb", "none"), ("postgres", "require"),
                                                 ("aurora-postgresql", "require"),
                                                 ("sqlserver", "require")])}
        resources["NoSsl"] = res("AWS::DMS::Endpoint", EngineName="mysql")   # default none
        self.assertEqual(pc.check_dms_ssl(tmpl(resources)), [])

    def test_parameter_defaults_and_overrides_are_resolved(self):
        params = {"Engine": {"Type": "String", "Default": "mysql"},
                  "Ssl": {"Type": "String", "Default": "require"}}
        ep = {"Ep": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode={"Ref": "Ssl"})}
        self.assertEqual(codes(pc.check_dms_ssl(tmpl(ep, parameters=params)), "FAIL"), ["DMS-SSL"])
        fixed = tmpl(ep, parameters=params, params={"Ssl": "verify-full"})
        self.assertEqual(pc.check_dms_ssl(fixed), [])

    def test_unresolved_engine_or_mode_is_reported(self):
        cases = {
            "EngTok": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode="require"),
            "SslTok": res("AWS::DMS::Endpoint", EngineName="aurora", SslMode={"Ref": "Ssl"}),
            "BothTok": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode={"Ref": "Ssl"}),
            "SafeTok": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode="verify-full"),
            "PgTok": res("AWS::DMS::Endpoint", EngineName="postgres", SslMode={"Ref": "Ssl"}),
        }
        by = {f.logical_id: f.code for f in pc.check_dms_ssl(tmpl(cases))}
        self.assertEqual(by, {"EngTok": "UNRESOLVED", "SslTok": "UNRESOLVED", "BothTok": "UNRESOLVED"})


class NamePrefixTests(unittest.TestCase):
    def check(self, resources, stack="dryrun5-cache"):
        return pc.check_names(tmpl(resources, stack), PREFIX, [])

    def test_prefixed_names_pass(self):
        found = self.check({
            "Pg": res("AWS::ElastiCache::ParameterGroup", CacheParameterGroupName="dryrun5-mc-pg",
                      CacheParameterGroupFamily="memcached1.6", Description="d"),
            "Rg": res("AWS::ElastiCache::ReplicationGroup", ReplicationGroupId="DRYRUN5-valkey"),
            "Lt": res("AWS::EC2::LaunchTemplate", LaunchTemplateName="dryrun5-helper-lt"),
            "B": res("AWS::S3::Bucket", BucketName="dryrun5-dashboard"),
            "Sec": res("AWS::SecretsManager::Secret", Name="/dryrun5/db-credentials"),
            "Alias": res("AWS::KMS::Alias", AliasName="alias/dryrun5-db"),
            "Lg": res("AWS::Logs::LogGroup", LogGroupName="/aws/lambda/dryrun5-soak"),
            "Lg2": res("AWS::Logs::LogGroup", LogGroupName="/dryrun5/app"),
            "Vended": res("AWS::Logs::LogGroup",
                          LogGroupName="/aws/vendedlogs/states/dryrun5-sm"),
            "Role": res("AWS::IAM::Role", RoleName="dryrun5-dms-secrets"),
            "DmsVpc": res("AWS::IAM::Role", RoleName="dms-vpc-role"),
            "Alarm": res("AWS::CloudWatch::Alarm", AlarmName="dryrun5-mc-0001-Cpu"),
            "Exact": res("AWS::SNS::Topic", TopicName="dryrun5"),
        })
        self.assertEqual(found, [])

    def test_unprefixed_names_fail_for_every_class(self):
        resources = {}
        for i, (rtype, props) in enumerate(pc.NAME_PROPS.items()):
            for prop, _ci in props:
                resources[f"R{i}{prop}"] = res(rtype, **{prop: "other-team-name"})
        found = self.check(resources)
        fails = [f for f in found if f.code == "NAME-PREFIX"]
        expected = sum(len(v) for v in pc.NAME_PROPS.values())
        self.assertEqual(len(fails), expected)

    def test_boundary_and_case_rules(self):
        found = self.check({
            "Boundary": res("AWS::ElastiCache::CacheCluster", ClusterName="dryrun50-mc"),
            "Glued": res("AWS::EC2::LaunchTemplate",
                         LaunchTemplateName="Dryrun5HelperLaunchTemplateA1B2"),
            "CaseSensitive": res("AWS::Lambda::Function", FunctionName="DRYRUN5-fn"),
            "CaseInsensitive": res("AWS::RDS::DBCluster", DBClusterIdentifier="DryRun5-apg"),
        })
        self.assertEqual(sorted(f.logical_id for f in found if f.code == "NAME-PREFIX"),
                         ["Boundary", "CaseSensitive", "Glued"])

    def test_cdk_requireimdsv2_launch_template_hint(self):
        found = self.check({"HelperLaunchTemplate67EBF5BA": res(
            "AWS::EC2::LaunchTemplate", LaunchTemplateName="HelperLaunchTemplate",
            LaunchTemplateData={"MetadataOptions": {"HttpTokens": "required"}})})
        self.assertEqual(codes(found), ["NAME-PREFIX"])
        self.assertIn("requireImdsv2", found[0].message)
        self.assertIn("HttpTokens.REQUIRED", found[0].message)

    def test_join_sub_resolution(self):
        found = self.check({
            "JoinOk": res("AWS::ElastiCache::SubnetGroup", CacheSubnetGroupName={
                "Fn::Join": ["-", ["dryrun5", {"Ref": "AWS::Region"}, "subnets"]]}),
            "JoinStack": res("AWS::SNS::Topic", TopicName={
                "Fn::Join": ["-", [{"Ref": "AWS::StackName"}, "alarms"]]}),
            "SubOk": res("AWS::S3::Bucket", BucketName={
                "Fn::Sub": "dryrun5-seed-${AWS::AccountId}"}),
            "SubStack": res("AWS::SQS::Queue", QueueName={"Fn::Sub": "${AWS::StackName}-dlq"}),
            "SubBad": res("AWS::Lambda::Function", FunctionName={"Fn::Sub": "soak-${AWS::Region}"}),
            "JoinBad": res("AWS::IAM::Role", RoleName={
                "Fn::Join": ["", ["helper-", {"Ref": "AWS::Region"}]]}),
            "TokenOnly": res("AWS::RDS::DBCluster", DBClusterIdentifier={"Ref": "ClusterName"}),
            "SubToken": res("AWS::DynamoDB::Table", TableName={"Fn::Sub": "${Prefix}-locks"}),
            "PartialPrefix": res("AWS::ECS::Cluster", ClusterName={"Fn::Sub": "dry${Rest}"}),
        }, stack="dryrun5-cache")
        by = {f.logical_id: f.code for f in found}
        self.assertEqual(by, {"SubBad": "NAME-PREFIX", "JoinBad": "NAME-PREFIX",
                              "TokenOnly": "UNRESOLVED", "SubToken": "UNRESOLVED",
                              "PartialPrefix": "UNRESOLVED"})

    def test_prefix_followed_by_token_needs_the_boundary(self):
        glued = {"Fn::Join": ["", ["dryrun5", {"Ref": "Suffix"}]]}
        unknown = self.check({"Q": res("AWS::SQS::Queue", QueueName=glued)})
        self.assertEqual(codes(unknown), ["UNRESOLVED"])
        bad = pc.check_names(tmpl({"Q": res("AWS::SQS::Queue", QueueName=glued)},
                                  parameters={"Suffix": {"Type": "String", "Default": "other"}}),
                             PREFIX, [])
        self.assertEqual(codes(bad), ["NAME-PREFIX"])
        good = pc.check_names(tmpl({"Q": res("AWS::SQS::Queue", QueueName=glued)},
                                   params={"Suffix": "-dlq"}), PREFIX, [])
        self.assertEqual(good, [])
        dashed = self.check({"Q": res("AWS::SQS::Queue", QueueName={
            "Fn::Join": ["-", ["dryrun5", {"Ref": "Suffix"}]]})})
        self.assertEqual(dashed, [])

    def test_ssm_typed_parameter_default_is_not_a_value(self):
        t = tmpl({"F": res("AWS::Lambda::Function", FunctionName={"Ref": "Name"})},
                 parameters={"Name": {"Type": "AWS::SSM::Parameter::Value<String>",
                                      "Default": "dryrun5-looks-prefixed"}})
        self.assertEqual(codes(pc.check_names(t, PREFIX, [])), ["UNRESOLVED"])

    def test_pseudo_parameters_only_from_overrides(self):
        name = {"Fn::Sub": "${AWS::Region}-dryrun5"}
        self.assertEqual(codes(self.check({"T": res("AWS::SNS::Topic", TopicName=name)})),
                         ["UNRESOLVED"])
        t = tmpl({"T": res("AWS::SNS::Topic", TopicName=name)}, params={"AWS::Region": "ap-northeast-2"})
        self.assertEqual(codes(pc.check_names(t, PREFIX, [])), ["NAME-PREFIX"])

    def test_log_group_service_prefixes_are_explicit(self):
        found = self.check({
            "Ok": res("AWS::Logs::LogGroup", LogGroupName="/aws/lambda/dryrun5-app/service"),
            "Bad": res("AWS::Logs::LogGroup", LogGroupName="/aws/lambda/other-app/dryrun5-stream"),
            "Vended": res("AWS::Logs::LogGroup", LogGroupName="/aws/vendedlogs/states/dryrun5-sm"),
            "VendedBad": res("AWS::Logs::LogGroup", LogGroupName="/aws/vendedlogs/states/sm-dryrun5"),
            "Rds": res("AWS::Logs::LogGroup", LogGroupName="/aws/rds/cluster/dryrun5-apg/error"),
            "UnknownSvc": res("AWS::Logs::LogGroup", LogGroupName="/aws/madeup/dryrun5-x"),
            "Partial": res("AWS::Logs::LogGroup", LogGroupName={
                "Fn::Join": ["", ["/aws/", {"Ref": "Svc"}, "/dryrun5-x"]]}),
            "FnTok": res("AWS::Logs::LogGroup", LogGroupName={
                "Fn::Join": ["", ["/aws/lambda/", {"Ref": "Fn"}]]}),
        })
        by = {f.logical_id: f.code for f in found}
        self.assertEqual(by, {"Bad": "NAME-PREFIX", "VendedBad": "NAME-PREFIX",
                              "UnknownSvc": "NAME-PREFIX", "Partial": "UNRESOLVED",
                              "FnTok": "UNRESOLVED"})

    def test_stack_name_token_with_unprefixed_stack_fails(self):
        found = self.check({"T": res("AWS::SNS::Topic", TopicName={
            "Fn::Join": ["-", [{"Ref": "AWS::StackName"}, "alarms"]]})}, stack="OtherStack")
        self.assertEqual(codes(found), ["NAME-PREFIX"])

    def test_allow_name_only_for_iam(self):
        t = tmpl({"Role": res("AWS::IAM::Role", RoleName="customer-mandated-role"),
                  "Fn": res("AWS::Lambda::Function", FunctionName="customer-mandated-role")})
        found = pc.check_names(t, PREFIX, ["customer-mandated-role"])
        self.assertEqual([f.logical_id for f in found], ["Fn"])

    def test_scoped_names_not_checked(self):
        found = self.check({
            "Pol": res("AWS::IAM::Policy", PolicyName="HelperRoleDefaultPolicy1234"),
            "User": res("AWS::ElastiCache::User", UserId="dryrun5-app", UserName="default"),
        })
        self.assertEqual(found, [])


class GeneratedNameWarnTests(unittest.TestCase):
    def test_unnamed_resources_warn(self):
        resources = {f"R{i}": res(rtype) for i, rtype in enumerate(pc.GENERATED_NAME_WARN)}
        found = pc.check_names(tmpl(resources), PREFIX, [])
        self.assertEqual(len(found), len(pc.GENERATED_NAME_WARN))
        self.assertTrue(all(f.level == "WARN" and f.code == "NAME-GENERATED" for f in found))

    def test_unnamed_elasticache_param_group_warns_named_passes(self):
        unnamed = pc.check_names(tmpl({"Params": res(
            "AWS::ElastiCache::ParameterGroup", CacheParameterGroupFamily="valkey8",
            Description="x")}), PREFIX, [])
        self.assertEqual(codes(unnamed), ["NAME-GENERATED"])
        named = pc.check_names(tmpl({"Params": res(
            "AWS::ElastiCache::ParameterGroup", CacheParameterGroupFamily="valkey8",
            Description="x", CacheParameterGroupName="dryrun5-valkey-pg")}), PREFIX, [])
        self.assertEqual(named, [])

    def test_stack_prefix_warning(self):
        self.assertEqual(codes(pc.check_stack_name(tmpl({}, "Dryrun5MemcachedRehearsal"), PREFIX)),
                         ["STACK-PREFIX"])
        self.assertEqual(pc.check_stack_name(tmpl({}, "dryrun5-memcached-target"), PREFIX), [])
        self.assertEqual(pc.check_stack_name(tmpl({}, None), PREFIX), [], "unknown name: no guess")


class LeadingLiteralTests(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(pc.leading_literal("abc"), ("abc", True))
        self.assertEqual(pc.leading_literal({"Ref": "X"}), ("", False))
        self.assertEqual(pc.leading_literal({"Ref": "AWS::StackName"}, "s1"), ("s1", True))
        self.assertEqual(pc.leading_literal({"Fn::Join": ["/", ["a", "b"]]}), ("a/b", True))
        self.assertEqual(pc.leading_literal({"Fn::Join": ["/", ["a", {"Ref": "B"}, "c"]]}),
                         ("a/", False))
        self.assertEqual(pc.leading_literal({"Fn::Sub": ["p-${X}", {"X": "1"}]}), ("p-1", True))
        self.assertEqual(pc.leading_literal({"Fn::Sub": ["p-${X}", {"X": {"Ref": "Y"}}]}), ("p-", False))
        self.assertEqual(pc.leading_literal({"Fn::Sub": "a${!B}c"}), ("a${B}c", True))
        self.assertEqual(pc.leading_literal({"Fn::Select": [1, ["a", "b"]]}), ("b", True))
        self.assertEqual(pc.leading_literal({"Fn::Sub": "${AWS::StackName}-x"}, "st"),
                         ("st-x", True))
        self.assertEqual(pc.leading_literal({"Fn::GetAtt": ["A", "B"]}), ("", False))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "cdk.out"
        self.out.mkdir()

    def write(self, name, resources, stack_name=None):
        (self.out / f"{name}.template.json").write_text(json.dumps({"Resources": resources}))
        manifest = self.out / "manifest.json"
        data = json.loads(manifest.read_text()) if manifest.exists() else {"artifacts": {}}
        data["artifacts"][name] = {"type": "aws:cloudformation:stack", "properties": {
            "templateFile": f"{name}.template.json", "stackName": stack_name or name}}
        manifest.write_text(json.dumps(data))

    def run_main(self, *args):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = pc.main(list(args))
        return rc, buf.getvalue(), err.getvalue()

    def good(self):
        self.write("Dryrun5Cache", {
            "Sg": res("AWS::EC2::SecurityGroup", GroupDescription="dryrun5-mc target cache"),
            "Pg": res("AWS::ElastiCache::ParameterGroup", CacheParameterGroupName="dryrun5-mc-pg",
                      CacheParameterGroupFamily="memcached1.6", Description="d"),
            "Mc": res("AWS::ElastiCache::CacheCluster", ClusterName="dryrun5-mc",
                      CacheParameterGroupName={"Ref": "Pg"},
                      PreferredMaintenanceWindow="sun:18:00-sun:19:00"),
        }, stack_name="dryrun5-cache")

    def test_directory_pass(self):
        self.good()
        rc, out, _ = self.run_main("--prefix", PREFIX, str(self.out))
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS — 1 template(s), 0 failure(s), 0 warning(s)", out)

    def test_directory_fail_and_json(self):
        self.good()
        self.write("Dryrun5Dms", {"Ep": res("AWS::DMS::Endpoint", EngineName="mysql",
                                            SslMode="require", EndpointIdentifier="dryrun5-src")},
                   stack_name="dryrun5-dms")
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertEqual(data["result"], "FAIL")
        self.assertEqual([f["code"] for f in data["failures"]], ["DMS-SSL"])
        self.assertEqual(len(data["templates_scanned"]), 2)

    def test_warnings_pass_unless_strict(self):
        self.write("S", {"Pg": res("AWS::ElastiCache::ParameterGroup",
                                   CacheParameterGroupFamily="valkey8", Description="d")},
                   stack_name="dryrun5-s")
        rc, out, _ = self.run_main("--prefix", PREFIX, str(self.out))
        self.assertEqual(rc, 0)
        self.assertIn("WARN [NAME-GENERATED]", out)
        rc, out, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 1)

    def test_nested_assembly_and_assets_skipped(self):
        stage = self.out / "assembly-Stage"
        stage.mkdir()
        (stage / "Inner.template.json").write_text(json.dumps({"Resources": {
            "B": res("AWS::S3::Bucket", BucketName="not-prefixed")}}))
        asset = self.out / "asset.abc123"
        asset.mkdir()
        (asset / "x.template.json").write_text(json.dumps({"Resources": {
            "B": res("AWS::S3::Bucket", BucketName="also-not-prefixed")}}))
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual([f["value"] for f in data["failures"]], ["not-prefixed"])

    def nested_assembly(self, parent_stack="dryrun5-parent"):
        """Real CDK shape: the parent references the nested template via aws:asset:path; the
        nested template has no manifest entry of its own."""
        self.write("Parent", {"ChildNestedStackResource": {
            "Type": "AWS::CloudFormation::Stack",
            "Properties": {"TemplateURL": {"Fn::Join": ["", ["https://s3/", "abc.json"]]}},
            "Metadata": {"aws:asset:path": "ParentChild8B49BB45.nested.template.json"}}},
            stack_name=parent_stack)
        (self.out / "ParentChild8B49BB45.nested.template.json").write_text(json.dumps({"Resources": {
            "Fn": res("AWS::Lambda::Function", FunctionName={
                "Fn::Join": ["-", [{"Ref": "AWS::StackName"}, "handler"]]})}}))

    def test_nested_stack_name_comes_from_the_parent_not_the_file(self):
        self.nested_assembly()
        rc, out, _ = self.run_main("--prefix", PREFIX, "--strict", "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual((rc, data["failures"], data["warnings"]), (0, [], []), out)

    def test_nested_stack_with_unprefixed_parent_fails(self):
        self.nested_assembly(parent_stack="OtherParent")
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual([f["code"] for f in data["failures"]], ["NAME-PREFIX"])
        self.assertTrue(data["failures"][0]["value"].startswith("OtherParent-ChildNestedStackResource-"))

    def test_orphan_nested_template_stays_unresolved(self):
        (self.out / "ParentChild8B49BB45.nested.template.json").write_text(json.dumps({"Resources": {
            "Fn": res("AWS::Lambda::Function", FunctionName={
                "Fn::Join": ["-", [{"Ref": "AWS::StackName"}, "handler"]]})}}))
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["failures"], [])
        self.assertEqual([w["code"] for w in data["warnings"]], ["UNRESOLVED"],
                         "no STACK-PREFIX from a file name, no prefix FAIL from an inferred name")
        rc, _, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 1, "--strict fails on UNRESOLVED")

    def nested_dms(self, child_params, assigned, grandchild=False):
        """Parent -> child (-> grandchild) with the DMS endpoint in the deepest template."""
        leaf = {"Parameters": child_params, "Resources": {"Ep": res(
            "AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode={"Ref": "Ssl"},
            EndpointIdentifier="dryrun5-src")}}
        nested = lambda asset, params: {"Type": "AWS::CloudFormation::Stack",
                                        "Properties": {"TemplateURL": "https://s3/x.json", "Parameters": params},
                                        "Metadata": {"aws:asset:path": asset}}
        if grandchild:
            (self.out / "ParentMidA1.nested.template.json").write_text(json.dumps({
                "Parameters": {"EngineIn": {"Type": "String", "Default": "postgres"},
                               "SslIn": {"Type": "String", "Default": "verify-full"}},
                "Resources": {"Leaf": nested("ParentMidLeafB2.nested.template.json",
                                             {"Engine": {"Ref": "EngineIn"}, "Ssl": {"Ref": "SslIn"}})}}))
            (self.out / "ParentMidLeafB2.nested.template.json").write_text(json.dumps(leaf))
            self.write("Parent", {"Mid": nested("ParentMidA1.nested.template.json", assigned)},
                       stack_name="dryrun5-parent")
        else:
            (self.out / "ParentChildA1.nested.template.json").write_text(json.dumps(leaf))
            self.write("Parent", {"Child": nested("ParentChildA1.nested.template.json", assigned)},
                       stack_name="dryrun5-parent")

    def test_parent_parameters_override_child_defaults(self):
        # review r6 #2: parent passes mysql/require into a child whose defaults are safe
        defaults = {"Engine": {"Type": "String", "Default": "postgres"},
                    "Ssl": {"Type": "String", "Default": "verify-full"}}
        self.nested_dms(defaults, {"Engine": "mysql", "Ssl": "require"})
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual((rc, [f["code"] for f in data["failures"]]), (1, ["DMS-SSL"]), out)

    def test_unresolvable_parent_assignment_stays_unresolved(self):
        defaults = {"Engine": {"Type": "String", "Default": "postgres"},
                    "Ssl": {"Type": "String", "Default": "verify-full"}}
        self.nested_dms(defaults, {"Engine": "mysql", "Ssl": {"Fn::GetAtt": ["Cfg", "Outputs.Ssl"]}})
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(data["failures"], [], "the child's safe default must not be used instead")
        self.assertEqual([w["code"] for w in data["warnings"]], ["UNRESOLVED"])
        self.assertIn("passed by parent Child", data["warnings"][0]["message"] + data["warnings"][0].get("value", ""))
        rc, _, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 1)

    def test_two_level_nesting_propagates_through_the_middle_stack(self):
        self.nested_dms({"Engine": {"Type": "String", "Default": "postgres"},
                         "Ssl": {"Type": "String", "Default": "verify-full"}},
                        {"EngineIn": "aurora", "SslIn": "require"}, grandchild=True)
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual((rc, [f["code"] for f in data["failures"]]), (1, ["DMS-SSL"]), out)
        self.assertIn("ParentMidLeafB2", data["failures"][0]["template"])
        # and with safe values passed down, the grandchild passes
        for f in self.out.glob("*.json"):
            f.unlink()
        self.nested_dms({"Engine": {"Type": "String"}, "Ssl": {"Type": "String"}},
                        {"EngineIn": "aurora", "SslIn": "verify-full"}, grandchild=True)
        rc, out, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 0, out)

    def test_global_parameters_do_not_leak_into_nested_defaults(self):
        # review r7 #3: --parameters reaches the top-level stack only; the child gets its own
        # Defaults plus what the parent actually passes.
        leaf = {"Parameters": {"Engine": {"Type": "String", "Default": "mysql"},
                               "Ssl": {"Type": "String", "Default": "require"}},
                "Resources": {"Ep": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode={"Ref": "Ssl"},
                                        EndpointIdentifier="dryrun5-src")}}
        (self.out / "ParentChildA1.nested.template.json").write_text(json.dumps(leaf))
        (self.out / "Parent.template.json").write_text(json.dumps({
            "Parameters": {"Engine": {"Type": "String", "Default": "mysql"}},
            "Resources": {"Child": {"Type": "AWS::CloudFormation::Stack",
                                    "Properties": {"TemplateURL": "https://s3/x.json"},
                                    "Metadata": {"aws:asset:path": "ParentChildA1.nested.template.json"}}}}))
        (self.out / "manifest.json").write_text(json.dumps({"artifacts": {"Parent": {
            "type": "aws:cloudformation:stack",
            "properties": {"templateFile": "Parent.template.json", "stackName": "dryrun5-parent"}}}}))
        for args in (("--parameters", "Engine=postgres"), ("--parameters", "dryrun5-parent:Engine=postgres")):
            rc, out, _ = self.run_main("--prefix", PREFIX, "--json", *args, str(self.out))
            data = json.loads(out)
            self.assertEqual((rc, [f["code"] for f in data["failures"]]), (1, ["DMS-SSL"]), (args, out))
            self.assertIn("[dryrun5-parent/Child]", data["failures"][0]["template"])

    def test_env_pseudo_parameters_reach_nested_stacks_but_ordinary_ones_do_not(self):
        # review r8 #2: AWS::Region given via --parameters resolves in a nested SG description
        # (same region for the whole tree); an ordinary parameter still does not leak, and each
        # child keeps its own (unknown) AWS::StackName.
        leaf = {"Parameters": {"Engine": {"Type": "String", "Default": "mysql"}},
                "Resources": {"Sg": res("AWS::EC2::SecurityGroup", GroupDescription={"Fn::Sub": "Allowed in ${AWS::Region}"}),
                              "Ep": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode="require",
                                        EndpointIdentifier="dryrun5-src"),
                              "Fn": res("AWS::Lambda::Function", FunctionName={"Fn::Sub": "${AWS::StackName}-fn"})}}
        (self.out / "ParentChildR.nested.template.json").write_text(json.dumps(leaf))
        (self.out / "Parent.template.json").write_text(json.dumps({
            "Parameters": {"Engine": {"Type": "String", "Default": "mysql"}},
            "Resources": {"Child": {"Type": "AWS::CloudFormation::Stack",
                                    "Properties": {"TemplateURL": "https://s3/x.json"},
                                    "Metadata": {"aws:asset:path": "ParentChildR.nested.template.json"}}}}))
        (self.out / "manifest.json").write_text(json.dumps({"artifacts": {"Parent": {
            "type": "aws:cloudformation:stack",
            "properties": {"templateFile": "Parent.template.json", "stackName": "dryrun5-parent"}}}}))
        for region_arg in ("AWS::Region=ap-northeast-2", "dryrun5-parent:AWS::Region=ap-northeast-2"):
            rc, out, _ = self.run_main("--prefix", PREFIX, "--json", "--parameters", region_arg,
                                       "--parameters", "Engine=postgres", str(self.out))
            data = json.loads(out)
            self.assertEqual([f["code"] for f in data["failures"]], ["DMS-SSL"], (region_arg, out))   # Engine did not leak
            unresolved = [w for w in data["warnings"] if w["code"] == "UNRESOLVED"]
            self.assertFalse([w for w in unresolved if "AWS::Region" in json.dumps(w)], (region_arg, out))
            self.assertFalse([w for w in data["warnings"] + data["failures"] if "SG-CHARSET" in w["code"]])
            # the child's own name derives from the parent ("dryrun5-parent-Child-<suffix>-fn")
            self.assertNotIn("NAME-PREFIX", [f["code"] for f in data["failures"]], out)

    def test_each_nested_instance_is_evaluated_separately(self):
        # review r7 #4: two stack resources share one nested template; the unsafe one must fail
        leaf = {"Parameters": {"Engine": {"Type": "String"}, "Ssl": {"Type": "String"}},
                "Resources": {"Ep": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"}, SslMode={"Ref": "Ssl"},
                                        EndpointIdentifier="dryrun5-src")}}
        (self.out / "ParentShared.nested.template.json").write_text(json.dumps(leaf))
        nested = lambda params: {"Type": "AWS::CloudFormation::Stack",
                                 "Properties": {"TemplateURL": "https://s3/x.json", "Parameters": params},
                                 "Metadata": {"aws:asset:path": "ParentShared.nested.template.json"}}
        self.write("Parent", {"ChildA": nested({"Engine": "mysql", "Ssl": "require"}),
                              "ChildB": nested({"Engine": "postgres", "Ssl": "require"})},
                   stack_name="dryrun5-parent")
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual((rc, [f["code"] for f in data["failures"]]), (1, ["DMS-SSL"]), out)
        self.assertIn("[dryrun5-parent/ChildA]", data["failures"][0]["template"])
        self.assertEqual(sum("ParentShared" in t for t in data["templates_scanned"]), 2)

    def test_intrinsic_parameters_map_makes_every_child_parameter_unresolved(self):
        # review r7 #5: {"Fn::If": [...]} is an expression, not a map with a key named Fn::If
        defaults = {"Engine": {"Type": "String", "Default": "postgres"},
                    "Ssl": {"Type": "String", "Default": "verify-full"}}
        self.nested_dms(defaults, {"Fn::If": ["IsProd", {"Engine": "mysql", "Ssl": "require"},
                                              {"Engine": "postgres", "Ssl": "verify-full"}]})
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(data["failures"], [], "safe child defaults must not be trusted")
        self.assertEqual([w["code"] for w in data["warnings"]], ["UNRESOLVED"], out)
        self.assertIn("non-literal Parameters map", data["warnings"][0]["message"])
        rc, _, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 1)

    def test_intrinsic_entry_value_is_unresolved_and_novalue_keeps_the_default(self):
        defaults = {"Engine": {"Type": "String", "Default": "postgres"},
                    "Ssl": {"Type": "String", "Default": "require"}}
        self.nested_dms(defaults, {"Engine": {"Fn::If": ["IsProd", "mysql", "postgres"]},
                                   "Ssl": {"Ref": "AWS::NoValue"}})
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", str(self.out))
        data = json.loads(out)
        self.assertEqual(data["failures"], [])
        self.assertEqual([w["code"] for w in data["warnings"]], ["UNRESOLVED"], out)
        self.assertIn("Fn::If", data["warnings"][0]["message"] + data["warnings"][0]["value"])

    def test_ssm_typed_parameter_override_is_a_name_not_a_value(self):
        # review r6 #3: the override names the SSM parameter; its value is unknown offline
        (self.out / "Fn.template.json").write_text(json.dumps({
            "Parameters": {"FnName": {"Type": "AWS::SSM::Parameter::Value<String>"},
                           "Subnets": {"Type": "AWS::SSM::Parameter::Value<List<String>>"}},
            "Resources": {"F": res("AWS::Lambda::Function", FunctionName={"Ref": "FnName"})}}))
        (self.out / "manifest.json").write_text(json.dumps({"artifacts": {"Fn": {
            "type": "aws:cloudformation:stack",
            "properties": {"templateFile": "Fn.template.json", "stackName": "dryrun5-fn"}}}}))
        rc, out, _ = self.run_main("--prefix", PREFIX, "--json", "--parameters",
                                   "FnName=/dryrun5/function-name", str(self.out))
        data = json.loads(out)
        self.assertEqual((rc, data["failures"]), (0, []))
        self.assertEqual([w["code"] for w in data["warnings"]], ["UNRESOLVED"])
        self.assertIn("SSM parameter", data["warnings"][0]["message"] + data["warnings"][0].get("value", ""))
        rc, _, _ = self.run_main("--prefix", PREFIX, "--strict", "--parameters",
                                 "FnName=/dryrun5/function-name", str(self.out))
        self.assertEqual(rc, 1, "--strict fails")
        t = pc.Template(Path("x.template.json"), "dryrun5-x", {"Parameters": {
            "S": {"Type": "AWS::SSM::Parameter::Value<List<String>>"}}},
            None, pc._template_params({"Parameters": {"S": {"Type": "AWS::SSM::Parameter::Value<List<String>>"}}},
                                      {"S": "/x"}, {}, "dryrun5-x"))
        self.assertIsInstance(t.params["S"], list, "List<…> inner type is handled the same way")

    def test_parameters_option_and_file(self):
        self.write("Dms", {"Ep": res("AWS::DMS::Endpoint", EngineName={"Ref": "Engine"},
                                     SslMode={"Ref": "Ssl"}, EndpointIdentifier="dryrun5-src")},
                   stack_name="dryrun5-dms")
        rc, out, _ = self.run_main("--prefix", PREFIX, "--strict", str(self.out))
        self.assertEqual(rc, 1)
        self.assertIn("UNRESOLVED", out)
        rc, out, _ = self.run_main("--prefix", PREFIX, "--parameters", "Engine=mysql",
                                   "--parameters", "dryrun5-dms:Ssl=require", str(self.out))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [DMS-SSL]", out)
        pfile = Path(self.temp.name) / "params.json"
        pfile.write_text(json.dumps([{"ParameterKey": "Engine", "ParameterValue": "mysql"},
                                     {"ParameterKey": "Ssl", "ParameterValue": "verify-full"}]))
        rc, out, _ = self.run_main("--prefix", PREFIX, "--strict", "--parameters-file", str(pfile),
                                   str(self.out))
        self.assertEqual(rc, 0, out)
        rc, _, err = self.run_main("--prefix", PREFIX, "--parameters", "novalue", str(self.out))
        self.assertEqual(rc, 2)

    def test_empty_scan_and_missing_path_are_errors(self):
        rc, _, err = self.run_main("--prefix", PREFIX, str(self.out))
        self.assertEqual(rc, 2)
        self.assertIn("not a pass", err)
        rc, _, err = self.run_main("--prefix", PREFIX, str(self.out / "nope"))
        self.assertEqual(rc, 2)

    def test_unreadable_template_is_error(self):
        (self.out / "Bad.template.json").write_text("{not json")
        rc, _, err = self.run_main("--prefix", PREFIX, str(self.out))
        self.assertEqual(rc, 2)
        self.assertIn("unreadable", err)

    def test_file_arguments_and_subprocess(self):
        self.good()
        f = self.out / "Dryrun5Cache.template.json"
        proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "predeploy_check.py"),
                               "--prefix", PREFIX, str(f)], capture_output=True, text=True,
                              check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
