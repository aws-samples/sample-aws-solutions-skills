#!/usr/bin/env python3
"""Pre-deploy check over synthesized CloudFormation templates (run after `cdk synth`).

`cdk synth` succeeds on several defects that only fail at `cdk deploy` (after a rollback)
or that silently break the customer's naming rule. Every one below was hit in live runs:

FAIL (exit 1 — do not deploy):
  SG-CHARSET   EC2 security-group GroupDescription / GroupName / rule Description outside the
               EC2 charset (a-z A-Z 0-9 space ._-:/()#,@[]+=&;{}!$*, <= 255 chars; GroupName
               may not start with "sg-"). Literal fragments inside Fn::Join / Fn::Sub are
               checked too. Source: EC2 API CreateSecurityGroup.
  DMS-SSL      AWS::DMS::Endpoint with a MySQL-family EngineName (mysql, mariadb, aurora) and
               SslMode "require" — DMS: "Not supported" for MySQL/MariaDB/Aurora MySQL.
  NAME-PREFIX  A physical-name property that is visible account-/region-wide and does not
               start with the engagement prefix (e.g. a CDK `requireImdsv2` LaunchTemplate
               named "<Id>LaunchTemplate", an un-prefixed bucket/role/alarm name).

WARN (exit 0; `--strict` makes warnings fail):
  NAME-GENERATED  The physical-name property is absent, so CloudFormation generates the
                  name; for these resource classes generated names were seen to drop or
                  truncate the prefix (ElastiCache parameter group "dryrun-cache-rjsouic6f79d").
  UNRESOLVED      A safety check could not be decided because the value contains a token
                  the checker cannot know (resource Ref/GetAtt, a parameter without a
                  default or --parameters value, an unknown stack name, an unsupported
                  intrinsic) — the property and the tokens are named. Covers names (incl.
                  the prefix followed directly by a token: the boundary is unknown), SG
                  text and DMS EngineName/SslMode.
  STACK-PREFIX    The KNOWN stack name lacks the prefix (CloudFormation-generated names
                  derive from it). Only manifest.json stack names count — a template's file
                  name never does (CDK nested stacks: "<parent>-<LogicalId>-<suffix>").

Values are resolved before checking: Ref to parameters (template Defaults, overridden by
--parameters / --parameters-file — TOP-LEVEL stacks only), AWS::StackName when known, pseudo
parameters passed with --parameters, Fn::Join, Fn::Sub (both forms, substitution map,
${!Literal}), Fn::Select. A nested stack's parameters are the child's own Defaults plus its
parent resource's actual Properties.Parameters, resolved in that parent's context
(recursively); global overrides never leak into a child. Each nested-stack RESOURCE instance
is evaluated separately with its subtree (findings name it: "<file> [<root>/<LogicalId>]").
What the parent can't resolve stays UNRESOLVED in the child — an intrinsic-built Parameters
map ({"Fn::If": …}) makes every child parameter UNRESOLVED, an intrinsic entry value that
parameter; {"Ref": "AWS::NoValue"} keeps the child's Default. AWS::SSM::Parameter::Value<…> parameters never
resolve offline: their Default, any --parameters value and any parent assignment is the SSM
parameter NAME, so they stay UNRESOLVED (check the parameter's value or the deployed name).

Prefix rule: the name must equal the prefix or continue with a non-alphanumeric character
("dryrun5-cache" passes for prefix "dryrun5"; "dryrun50-x" and "Dryrun5Helper..." do not).
Identifiers that AWS stores lowercase (RDS, ElastiCache, DMS ids, S3 buckets, ...) are
compared case-insensitively; other names case-sensitively. KMS aliases are checked after
"alias/", log groups after a known AWS service path (LOG_GROUP_SERVICE_PREFIXES, e.g.
"/aws/lambda/", "/aws/vendedlogs/states/") or a leading "/", secrets/SSM after a leading "/".
IAM role names that AWS services require verbatim (dms-vpc-role, dms-cloudwatch-logs-role,
dms-access-for-endpoint) are allowed; add others with --allow-name.

Usage:
  python3 -B predeploy_check.py --prefix dryrun5 cdk.out
  python3 -B predeploy_check.py --prefix dryrun5 --json --strict a.template.json b.template.json
  python3 -B predeploy_check.py --prefix dryrun5 --parameters Engine=mysql \
      --parameters dryrun5-dms:SslMode=verify-ca --parameters AWS::Region=ap-northeast-2 cdk.out
Exit codes: 0 pass (warnings allowed unless --strict), 1 findings that block the deploy,
2 usage error / no templates found / unreadable template.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

SG_CHARSET = re.compile(r"[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]*")
SG_MAX_LEN = 255
MYSQL_FAMILY_DMS_ENGINES = {"mysql", "mariadb", "aurora"}
REQUIRED_AWS_ROLE_NAMES = {"dms-vpc-role", "dms-cloudwatch-logs-role", "dms-access-for-endpoint"}

# Resource type -> [(property, case_insensitive)] for properties that NAME the resource itself
# (not references to other resources). Inline names scoped to a parent (AWS::IAM::Policy
# PolicyName — CDK always sets "<Role>DefaultPolicy...") and ElastiCache::User UserName (the
# login the app authenticates with; Redis-engine groups require a user literally named
# "default") are deliberately not checked; the account-visible UserId is.
NAME_PROPS: Dict[str, List[Tuple[str, bool]]] = {
    "AWS::RDS::DBCluster": [("DBClusterIdentifier", True)],
    "AWS::RDS::DBInstance": [("DBInstanceIdentifier", True)],
    "AWS::RDS::DBClusterParameterGroup": [("DBClusterParameterGroupName", True)],
    "AWS::RDS::DBParameterGroup": [("DBParameterGroupName", True)],
    "AWS::RDS::DBSubnetGroup": [("DBSubnetGroupName", True)],
    "AWS::RDS::OptionGroup": [("OptionGroupName", True)],
    "AWS::RDS::DBProxy": [("DBProxyName", True)],
    "AWS::ElastiCache::ParameterGroup": [("CacheParameterGroupName", True)],
    "AWS::ElastiCache::ReplicationGroup": [("ReplicationGroupId", True)],
    "AWS::ElastiCache::CacheCluster": [("ClusterName", True)],
    "AWS::ElastiCache::SubnetGroup": [("CacheSubnetGroupName", True)],
    "AWS::ElastiCache::ServerlessCache": [("ServerlessCacheName", True)],
    "AWS::ElastiCache::User": [("UserId", True)],
    "AWS::ElastiCache::UserGroup": [("UserGroupId", True)],
    "AWS::EC2::LaunchTemplate": [("LaunchTemplateName", False)],
    "AWS::EC2::SecurityGroup": [("GroupName", True)],
    "AWS::EC2::KeyPair": [("KeyName", False)],
    "AWS::S3::Bucket": [("BucketName", True)],
    "AWS::SecretsManager::Secret": [("Name", False)],
    "AWS::SSM::Parameter": [("Name", False)],
    "AWS::IAM::Role": [("RoleName", True)],
    "AWS::IAM::ManagedPolicy": [("ManagedPolicyName", True)],
    "AWS::IAM::InstanceProfile": [("InstanceProfileName", True)],
    "AWS::IAM::User": [("UserName", True)],
    "AWS::IAM::Group": [("GroupName", True)],
    "AWS::DMS::ReplicationInstance": [("ReplicationInstanceIdentifier", True)],
    "AWS::DMS::ReplicationTask": [("ReplicationTaskIdentifier", True)],
    "AWS::DMS::Endpoint": [("EndpointIdentifier", True)],
    "AWS::DMS::ReplicationSubnetGroup": [("ReplicationSubnetGroupIdentifier", True)],
    "AWS::DMS::Certificate": [("CertificateIdentifier", True)],
    "AWS::DMS::ReplicationConfig": [("ReplicationConfigIdentifier", True)],
    "AWS::Lambda::Function": [("FunctionName", False)],
    "AWS::Logs::LogGroup": [("LogGroupName", False)],
    "AWS::SNS::Topic": [("TopicName", False)],
    "AWS::SQS::Queue": [("QueueName", False)],
    "AWS::CloudWatch::Alarm": [("AlarmName", False)],
    "AWS::CloudWatch::CompositeAlarm": [("AlarmName", False)],
    "AWS::CloudWatch::Dashboard": [("DashboardName", False)],
    "AWS::KMS::Alias": [("AliasName", False)],
    "AWS::Events::Rule": [("Name", False)],
    "AWS::Scheduler::Schedule": [("Name", False)],
    "AWS::StepFunctions::StateMachine": [("StateMachineName", False)],
    "AWS::DynamoDB::Table": [("TableName", False)],
    "AWS::ECS::Cluster": [("ClusterName", False)],
}

# Resource classes whose CloudFormation-GENERATED names were seen to drop/truncate the prefix
# or are otherwise worth naming explicitly (visible in shared consoles, prefix-scoped IAM).
GENERATED_NAME_WARN: Dict[str, str] = {
    "AWS::ElastiCache::ParameterGroup": "CacheParameterGroupName",
    "AWS::ElastiCache::SubnetGroup": "CacheSubnetGroupName",
    "AWS::EC2::LaunchTemplate": "LaunchTemplateName",
    "AWS::RDS::DBClusterParameterGroup": "DBClusterParameterGroupName",
    "AWS::RDS::DBParameterGroup": "DBParameterGroupName",
    "AWS::RDS::DBSubnetGroup": "DBSubnetGroupName",
    "AWS::DMS::ReplicationSubnetGroup": "ReplicationSubnetGroupIdentifier",
    "AWS::DMS::ReplicationInstance": "ReplicationInstanceIdentifier",
    "AWS::DMS::Endpoint": "EndpointIdentifier",
    "AWS::DMS::ReplicationTask": "ReplicationTaskIdentifier",
}

# Log groups AWS services create or expect under a fixed service path: the customer-chosen part
# (which must carry the prefix) starts right after it. Longest first. Anything else under
# "/aws/" is checked as written (minus the leading "/") and therefore fails.
LOG_GROUP_SERVICE_PREFIXES: Tuple[str, ...] = tuple(sorted((
    "/aws/lambda/", "/aws/vendedlogs/states/", "/aws/vendedlogs/", "/aws/states/",
    "/aws/codebuild/", "/aws/ecs/containerinsights/", "/aws/ecs/", "/aws/apigateway/",
    "/aws/events/", "/aws/rds/cluster/", "/aws/rds/instance/", "/aws/rds/proxy/",
    "/aws/elasticache/", "/aws/eks/", "/aws/batch/", "/aws/glue/", "/aws/kinesisfirehose/",
    "/aws/appsync/apis/", "/aws/sagemaker/", "/aws/ssm/", "/aws/cloudtrail/",
), key=len, reverse=True))


@dataclass
class Finding:
    level: str          # FAIL | WARN
    code: str
    template: str
    logical_id: str
    resource_type: str
    prop: str
    value: str
    message: str

    def line(self) -> str:
        where = f"{self.template}:{self.logical_id} ({self.resource_type})"
        val = f" {self.prop}={self.value!r}" if self.prop else ""
        return f"{self.level} [{self.code}] {where}{val} — {self.message}"


# ---------------------------------------------------------------- intrinsic resolution

@dataclass(frozen=True)
class Tok:
    """A value the checker cannot know before deploy (a resource Ref/GetAtt, an unknown
    parameter or pseudo parameter, an unsupported intrinsic)."""
    desc: str
    charset_safe: bool = False     # known to stay inside the EC2 SG charset (e.g. an sg-… GroupId)


Parts = List[Union[str, Tok]]


@dataclass
class Template:
    path: Path
    stack_name: Optional[str]          # KNOWN physical stack name (manifest/argument); None = unknown
    body: Dict[str, Any]
    stack_parts: Optional[Parts] = None   # partial name (nested stack: "<parent>-<LogicalId>-" + suffix)
    # resolved parameter values (default: template Defaults); a value is a literal string or,
    # when only partly known (nested-stack assignment, SSM-typed parameter), a Parts list
    params: Optional[Dict[str, Union[str, Parts]]] = None
    # nested-stack INSTANCE path ("<root>/<LogicalId>/…"); None for a top-level template. A nested
    # template referenced by N stack resources is evaluated N times, once per instance.
    instance: Optional[str] = None

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = _template_params(self.body, {}, {}, self.stack_name)

    @property
    def label(self) -> str:
        return self.path.name if self.instance is None else f"{self.path.name} [{self.instance}]"

    def stack_name_parts(self) -> Parts:
        if self.stack_name is not None:
            return [self.stack_name]
        if self.stack_parts is not None:
            return list(self.stack_parts)
        return [Tok("AWS::StackName (unknown for this template)")]


def _merge(parts: Parts) -> Parts:
    out: Parts = []
    for part in parts:
        if isinstance(part, str):
            if not part:
                continue
            if out and isinstance(out[-1], str):
                out[-1] = out[-1] + part
                continue
        out.append(part)
    return out


_SUB_VAR = re.compile(r"\$\{([^}]*)\}")


def resolve(value: Any, t: Template) -> Parts:
    """Resolve a property value into literal strings and Tok placeholders.

    Supported: strings/numbers, Ref (parameters with a known value, AWS::StackName when
    known, pseudo parameters given with --parameters), Fn::Join, Fn::Sub (string and
    [string, map] forms, ${!Literal} escapes), Fn::Select over a literal list. Everything
    else becomes a Tok, so callers can report it as UNRESOLVED instead of ignoring it.
    """
    if isinstance(value, bool):
        return [str(value).lower()]
    if isinstance(value, (str, int, float)):
        return [str(value)]
    if not isinstance(value, dict) or len(value) != 1:
        return [Tok("non-scalar value")]
    (fn, arg), = value.items()
    resources = t.body.get("Resources") or {}
    if fn == "Ref":
        return _ref(str(arg), t, resources)
    if fn == "Fn::GetAtt":
        target, attr = (arg if isinstance(arg, list) and len(arg) == 2
                        else (str(arg).split(".", 1) + [""])[:2])
        return [Tok(f"GetAtt {target}.{attr}", charset_safe=(attr == "GroupId"))]
    if fn == "Fn::Join" and isinstance(arg, list) and len(arg) == 2 and isinstance(arg[0], str):
        delim, items = arg
        if not isinstance(items, list):
            return [Tok("Fn::Join over a non-literal list")]
        out: Parts = []
        for i, item in enumerate(items):
            if i:
                out.append(delim)
            out += resolve(item, t)
        return _merge(out)
    if fn == "Fn::Sub":
        if isinstance(arg, str):
            tmpl, mapping = arg, {}
        elif isinstance(arg, list) and arg and isinstance(arg[0], str):
            tmpl, mapping = arg[0], (arg[1] if len(arg) > 1 and isinstance(arg[1], dict) else {})
        else:
            return [Tok("Fn::Sub with a non-literal template")]
        out, pos = [], 0
        for m in _SUB_VAR.finditer(tmpl):
            out.append(tmpl[pos:m.start()])
            var = m.group(1)
            if var.startswith("!"):
                out.append("${" + var[1:] + "}")          # ${!Literal} -> literal ${Literal}
            elif var in mapping:
                out += resolve(mapping[var], t)
            elif "." in var:
                out.append(Tok(f"GetAtt {var}", charset_safe=var.endswith(".GroupId")))
            else:
                out += _ref(var, t, resources)
            pos = m.end()
        out.append(tmpl[pos:])
        return _merge(out)
    if fn == "Fn::Select" and isinstance(arg, list) and len(arg) == 2 and isinstance(arg[1], list):
        try:
            return resolve(arg[1][int(arg[0])], t)
        except (ValueError, TypeError, IndexError):
            return [Tok("Fn::Select")]
    return [Tok(fn)]


def _ref(name: str, t: Template, resources: Dict[str, Any]) -> Parts:
    if name in t.params:
        value = t.params[name]
        return [value] if isinstance(value, str) else list(value)
    if name == "AWS::StackName":
        return t.stack_name_parts()
    if name.startswith("AWS::"):
        return [Tok(f"Ref {name} (pass --parameters {name}=…)")]
    res = resources.get(name)
    if isinstance(res, dict):
        return [Tok(f"Ref {name}", charset_safe=res.get("Type") == "AWS::EC2::SecurityGroup")]
    return [Tok(f"Ref {name} (parameter without a known value)")]


def text_of(parts: Parts) -> Tuple[str, bool]:
    """(leading literal, complete?)"""
    acc = ""
    for part in parts:
        if isinstance(part, Tok):
            return acc, False
        acc += part
    return acc, True


def shown(parts: Parts) -> str:
    return "".join(p if isinstance(p, str) else f"<{p.desc}>" for p in parts)


def leading_literal(value: Any, stack_name: Optional[str] = None) -> Tuple[str, bool]:
    """Return (leading literal text, complete?) for a value (kept for callers/tests)."""
    return text_of(resolve(value, Template(Path("x.template.json"), stack_name, {})))


# ---------------------------------------------------------------- template loading

def _manifest_stack_names(assembly_dir: Path) -> Dict[str, str]:
    """templateFile -> stackName from a cloud assembly manifest.json (if present)."""
    names: Dict[str, str] = {}
    manifest = assembly_dir / "manifest.json"
    if not manifest.is_file():
        return names
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return names
    for art_id, art in (data.get("artifacts") or {}).items():
        if art.get("type") != "aws:cloudformation:stack":
            continue
        props = art.get("properties") or {}
        tfile = props.get("templateFile")
        if tfile:
            names[tfile] = props.get("stackName") or art_id
    return names


def _ssm_typed(body: Dict[str, Any]) -> set:
    """Parameters of type AWS::SSM::Parameter::Value<…> (any inner type, List<…> included)."""
    return {name for name, decl in (body.get("Parameters") or {}).items()
            if isinstance(decl, dict) and str(decl.get("Type", "")).startswith("AWS::SSM::Parameter::Value")}


def _ssm_token(name: str, given: Any) -> Parts:
    return [Tok(f"Ref {name}: value of SSM parameter {shown(given) if isinstance(given, list) else given!r} "
                "(an override/assignment of an SSM-typed parameter is the parameter NAME, not its value — "
                "read the parameter, or check the deployed value)")]


def _template_params(body: Dict[str, Any], overrides: Dict[str, str], scoped: Dict[str, Dict[str, str]],
                     stack_name: Optional[str]) -> Dict[str, Union[str, Parts]]:
    values: Dict[str, Union[str, Parts]] = {}
    ssm = _ssm_typed(body)
    for name, decl in (body.get("Parameters") or {}).items():
        if not isinstance(decl, dict) or "Default" not in decl:
            continue
        # an SSM-typed parameter's Default is the PARAMETER NAME, not the value
        if name in ssm:
            continue
        default = decl["Default"]
        values[name] = ",".join(map(str, default)) if isinstance(default, list) else str(default)
    values.update(overrides)
    if stack_name and stack_name in scoped:
        values.update(scoped[stack_name])
    # ...and so is any --parameters value given for it: CloudFormation resolves the SSM
    # parameter at deploy time, which this offline check cannot do.
    for name in ssm & set(values):
        values[name] = _ssm_token(name, values[name])
    return values


# Environment pseudo parameters are the SAME for a stack and every nested stack under it (same
# account, region, partition), so --parameters values for them flow down the tree; ordinary
# parameters and AWS::StackName (each nested stack has its own) never do.
ENV_PSEUDO = ("AWS::Region", "AWS::AccountId", "AWS::Partition", "AWS::URLSuffix")


def _env_pseudo(overrides: Dict[str, str], scoped: Dict[str, Dict[str, str]], stack_name: Optional[str]) -> Dict[str, str]:
    env = {k: v for k, v in overrides.items() if k in ENV_PSEUDO}
    if stack_name and stack_name in scoped:
        env.update({k: v for k, v in scoped[stack_name].items() if k in ENV_PSEUDO})
    return env


def _propagate_nested(parent: "Template", lid: str, res: Dict[str, Any], child: "Template") -> None:
    """Apply a nested-stack resource's Properties.Parameters to its child template, resolved
    in the PARENT's context; they override the child's Defaults (as CloudFormation does).
    Anything the parent can't resolve stays a token (UNRESOLVED) in the child."""
    if child.stack_name is None:          # CloudFormation: "<parent stack name>-<LogicalId>-<suffix>"
        if parent.stack_name is not None:
            child.stack_parts = [f"{parent.stack_name}-{lid}-", Tok("nested stack suffix")]
        elif parent.stack_parts is not None:
            child.stack_parts = _merge(list(parent.stack_parts) + [f"-{lid}-", Tok("nested stack suffix")])
    assigned = (res.get("Properties") or {}).get("Parameters")
    if assigned is None:
        return
    ssm = _ssm_typed(child.body)
    declared = set((child.body.get("Parameters") or {}).keys())
    if not isinstance(assigned, dict) or _is_intrinsic(assigned):
        # an intrinsic-built Parameters map ({"Fn::If": …}, {"Ref": …}, …) could set ANY child
        # parameter: nothing is knowable, the child's Defaults included
        for name in declared:
            child.params[name] = [Tok(f"Ref {name} (parent {lid} passes a non-literal Parameters map)")]
        return
    for name, value in assigned.items():
        if value == {"Ref": "AWS::NoValue"}:
            continue                     # not passed: the child's Default applies
        parts = resolve(value, parent)
        if name in ssm:
            child.params[name] = _ssm_token(name, parts)
            continue
        text, complete = text_of(parts)
        child.params[name] = text if complete else [
            p if isinstance(p, str) else Tok(f"{p.desc} (passed by parent {lid})", p.charset_safe)
            for p in parts]


def _is_intrinsic(value: Any) -> bool:
    """A one-key dict whose key is an intrinsic function / Ref / Condition — not a literal map."""
    if not isinstance(value, dict) or len(value) != 1:
        return False
    (key,), = [tuple(value)]
    return key.startswith("Fn::") or key in ("Ref", "Condition")


def load_templates(paths: Iterable[str], overrides: Optional[Dict[str, str]] = None,
                   scoped: Optional[Dict[str, Dict[str, str]]] = None) -> List[Template]:
    """Load templates from files and/or cloud-assembly directories (recursive, skips assets).

    The physical stack name is KNOWN only from a manifest.json stack entry. A template without
    one (a CDK nested stack's *.nested.template.json, a loose file) keeps it unknown — its file
    name is never used as a stack name. A CDK nested stack whose parent is known (parent
    resource Metadata "aws:asset:path" = the nested file) gets the partial name
    "<parent>-<LogicalId>-<suffix>", which is how CloudFormation names nested stacks.
    """
    overrides, scoped = overrides or {}, scoped or {}
    files_loaded: List[Tuple[Path, Dict[str, Any], Optional[str]]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files = sorted(f for f in p.rglob("*.template.json")
                           if not any(part.startswith("asset.") for part in f.relative_to(p).parts))
        elif p.is_file():
            files = [p]
        else:
            raise FileNotFoundError(f"no such file or directory: {raw}")
        for f in files:
            names = _manifest_stack_names(f.parent)
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"unreadable template {f}: {exc}") from exc
            if not isinstance(body, dict):
                raise ValueError(f"template {f} is not a JSON object")
            files_loaded.append((f, body, names.get(f.name)))
    # Nested stacks (CDK: parent resource Metadata "aws:asset:path" = the nested file). Edges are
    # per FILE, but evaluation is per stack-resource INSTANCE: a top-level template gets its
    # Defaults + the deploy-time --parameters; each nested instance gets its own context = the
    # child's Defaults + the parent resource's actual Parameters assignments (resolved in that
    # parent instance's context) — never the global overrides — and its whole subtree is
    # evaluated again for every instance that references it.
    by_file = {(f.parent, f.name): (f, body, known) for f, body, known in files_loaded}
    edges: Dict[Tuple[Path, str], List[Tuple[str, Dict[str, Any], Tuple[Path, str]]]] = {}
    is_child: set = set()
    for f, body, _known in files_loaded:
        for lid, res in (body.get("Resources") or {}).items():
            if not isinstance(res, dict) or res.get("Type") != "AWS::CloudFormation::Stack":
                continue
            asset = (res.get("Metadata") or {}).get("aws:asset:path")
            ckey = (f.parent, asset) if isinstance(asset, str) else None
            if ckey in by_file and ckey != (f.parent, f.name):
                edges.setdefault((f.parent, f.name), []).append((lid, res, ckey))
                is_child.add(ckey)
    out: List[Template] = []

    def visit(t: Template, key: Tuple[Path, str], ancestry: Tuple[Tuple[Path, str], ...],
              env: Dict[str, str]) -> None:
        out.append(t)
        for lid, res, ckey in edges.get(key, []):
            if ckey in ancestry:          # a reference cycle: CloudFormation would reject it
                continue
            cf, cbody, cknown = by_file[ckey]
            path = f"{t.instance or t.stack_name or t.path.name}/{lid}"
            # child context: its Defaults + the environment pseudo parameters of the tree,
            # then the parent's actual assignments — never ordinary global overrides
            child = Template(cf, cknown, cbody, None, _template_params(cbody, dict(env), {}, None), instance=path)
            _propagate_nested(t, lid, res, child)
            visit(child, ckey, ancestry + (ckey,), env)

    for f, body, known in files_loaded:
        key = (f.parent, f.name)
        if key not in is_child:           # top level: Defaults + --parameters (+ Stack:KEY=VALUE)
            visit(Template(f, known, body, None, _template_params(body, overrides, scoped, known)), key, (key,),
                  _env_pseudo(overrides, scoped, known))
    for f, body, known in files_loaded:   # nested files reachable only through a cycle
        key = (f.parent, f.name)
        if key in is_child and not any(t.path == f for t in out):
            out.append(Template(f, known, body, None, _template_params(body, {}, {}, known)))
    return out


# ---------------------------------------------------------------- value helpers

def _strip_name(resource_type: str, name: str) -> str:
    if resource_type == "AWS::KMS::Alias" and name.startswith("alias/"):
        return name[len("alias/"):]
    if resource_type == "AWS::Logs::LogGroup":
        for svc in LOG_GROUP_SERVICE_PREFIXES:
            if name.startswith(svc):
                return name[len(svc):]
        return name.lstrip("/")
    if resource_type in ("AWS::SecretsManager::Secret", "AWS::SSM::Parameter"):
        return name.lstrip("/")
    return name


def _log_group_undecided(text: str) -> bool:
    """A partial log-group literal that may still become a known service path."""
    return any(svc.startswith(text) and svc != text for svc in LOG_GROUP_SERVICE_PREFIXES)


def prefix_status(name: str, prefix: str, case_insensitive: bool, complete: bool) -> str:
    """'ok' | 'fail' | 'unresolved' for a (possibly partial) name against the prefix.

    The name must equal the prefix or continue with a non-alphanumeric character, so a
    prefix followed directly by an unknown token is undecided, never silently ok.
    """
    n, p = (name.lower(), prefix.lower()) if case_insensitive else (name, prefix)
    if n.startswith(p):
        if len(n) == len(p):
            if complete or not p[-1].isalnum():
                return "ok"
            return "unresolved"               # "dryrun5" + <token>: the boundary is unknown
        if not p[-1].isalnum() or not n[len(p)].isalnum():
            return "ok"
        return "fail"
    if not complete and p.startswith(n):
        return "unresolved"                   # literal ends before the prefix is decided
    return "fail"


def _unresolved(t: Template, lid: str, rtype: str, prop: str, parts: Parts, what: str) -> Finding:
    toks = ", ".join(sorted({p.desc for p in parts if isinstance(p, Tok)}))
    return Finding("WARN", "UNRESOLVED", t.label, lid, rtype, prop, shown(parts),
                   f"{what} cannot be decided before deploy: unresolved {toks} — pass "
                   "--parameters KEY=VALUE for parameters, or check the deployed value")


# ---------------------------------------------------------------- checks

def _resources(t: Template) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    for lid, res in (t.body.get("Resources") or {}).items():
        if isinstance(res, dict):
            props = res.get("Properties")
            yield lid, str(res.get("Type", "")), props if isinstance(props, dict) else {}


def _sg_text_problem(text: str, is_group_name: bool, check_len: bool = True) -> Optional[str]:
    if check_len and len(text) > SG_MAX_LEN:
        return f"longer than {SG_MAX_LEN} characters"
    if not SG_CHARSET.fullmatch(text):
        bad = sorted({c for c in text if not SG_CHARSET.fullmatch(c)})
        return ("characters outside the EC2 set a-zA-Z0-9 space ._-:/()#,@[]+=&;{}!$*: "
                + " ".join(repr(c) for c in bad))
    if is_group_name and text.lower().startswith("sg-"):
        return "GroupName may not start with 'sg-'"
    return None


def check_sg_text(t: Template) -> List[Finding]:
    found: List[Finding] = []

    def check(lid: str, rtype: str, prop: str, value: Any) -> None:
        if value is None:
            return
        parts = resolve(value, t)
        is_name = prop == "GroupName"
        text, complete = text_of(parts)
        if complete:
            problem = _sg_text_problem(text, is_name)
        else:
            literals = [x for x in parts if isinstance(x, str)]
            problem = next((pr for pr in (_sg_text_problem(x, False, check_len=False) for x in literals)
                            if pr), None)
            if problem is None and is_name and text.lower().startswith("sg-"):
                problem = "GroupName may not start with 'sg-'"
            if problem is None and sum(len(x) for x in literals) > SG_MAX_LEN:
                problem = f"longer than {SG_MAX_LEN} characters"
        if problem:
            found.append(Finding("FAIL", "SG-CHARSET", t.label, lid, rtype, prop, shown(parts),
                                 problem + " (cdk deploy fails and rolls back; synth does not check)"))
        elif not complete and not all(x.charset_safe for x in parts if isinstance(x, Tok)):
            found.append(_unresolved(t, lid, rtype, prop, parts, "the EC2 charset/length rule"))

    for lid, rtype, props in _resources(t):
        if rtype == "AWS::EC2::SecurityGroup":
            check(lid, rtype, "GroupDescription", props.get("GroupDescription"))
            check(lid, rtype, "GroupName", props.get("GroupName"))
            for key in ("SecurityGroupIngress", "SecurityGroupEgress"):
                rules = props.get(key)
                if isinstance(rules, list):
                    for i, rule in enumerate(rules):
                        if isinstance(rule, dict):
                            check(lid, rtype, f"{key}[{i}].Description", rule.get("Description"))
        elif rtype in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"):
            check(lid, rtype, "Description", props.get("Description"))
    return found


def check_dms_ssl(t: Template) -> List[Finding]:
    found: List[Finding] = []
    for lid, rtype, props in _resources(t):
        if rtype != "AWS::DMS::Endpoint" or "EngineName" not in props:
            continue
        e_parts = resolve(props["EngineName"], t)
        engine, e_known = text_of(e_parts)
        if "SslMode" in props:
            s_parts = resolve(props["SslMode"], t)
            ssl, s_known = text_of(s_parts)
        else:
            s_parts, ssl, s_known = ["none"], "none", True          # DMS default
        mysql = e_known and engine.lower() in MYSQL_FAMILY_DMS_ENGINES
        require = s_known and ssl.lower() == "require"
        if mysql and require:
            found.append(Finding(
                "FAIL", "DMS-SSL", t.label, lid, rtype, "SslMode", ssl,
                f"DMS does not support SslMode 'require' for EngineName '{engine}' (MySQL/MariaDB/"
                "Aurora MySQL: none | verify-ca | verify-full) — use verify-ca (self-managed source "
                "with an auto-generated cert) or verify-full (RDS/Aurora) plus the imported CA"))
        elif (mysql and not s_known) or (not e_known and (require or not s_known)):
            found.append(_unresolved(t, lid, rtype, "EngineName/SslMode",
                                     e_parts + [" / "] + s_parts, "the MySQL-family SslMode rule"))
    return found


def check_names(t: Template, prefix: str, allow: Iterable[str]) -> List[Finding]:
    found: List[Finding] = []
    allowed = {a.lower() for a in allow} | REQUIRED_AWS_ROLE_NAMES
    for lid, rtype, props in _resources(t):
        for prop, ci in NAME_PROPS.get(rtype, []):
            if prop not in props:
                continue
            parts = resolve(props[prop], t)
            text, complete = text_of(parts)
            if complete and text.lower() in allowed and rtype.startswith("AWS::IAM::"):
                continue
            if rtype == "AWS::Logs::LogGroup" and not complete and _log_group_undecided(text):
                status = "unresolved"
            else:
                status = prefix_status(_strip_name(rtype, text), prefix, ci, complete)
            if status == "fail":
                hint = ""
                if rtype == "AWS::EC2::LaunchTemplate" and text.endswith("LaunchTemplate"):
                    hint = (" — looks like the fixed name CDK's `requireImdsv2: true` gives its "
                            "launch template; use `httpTokens: ec2.HttpTokens.REQUIRED` on the "
                            "Instance (no template) or an explicitly prefixed LaunchTemplate")
                found.append(Finding(
                    "FAIL", "NAME-PREFIX", t.label, lid, rtype, prop, shown(parts),
                    f"physical name does not start with the engagement prefix '{prefix}'" + hint))
            elif status == "unresolved":
                found.append(_unresolved(t, lid, rtype, prop, parts,
                                         f"whether the name starts with '{prefix}'"))
        gen_prop = GENERATED_NAME_WARN.get(rtype)
        if gen_prop and gen_prop not in props:
            found.append(Finding(
                "WARN", "NAME-GENERATED", t.label, lid, rtype, gen_prop, "",
                f"no {gen_prop}: CloudFormation will generate a name that may drop or truncate "
                f"the prefix — set '{prefix}-<purpose>' at FIRST creation (renaming later "
                "replaces the resource)"))
    return found


def check_stack_name(t: Template, prefix: str) -> List[Finding]:
    """Only a KNOWN (or partially known nested) stack name is checked — never a file name."""
    if t.stack_name is None and t.stack_parts is None:
        return []
    parts = t.stack_name_parts()
    text, complete = text_of(parts)
    if prefix_status(text, prefix, True, complete) == "ok":
        return []
    return [Finding("WARN", "STACK-PREFIX", t.label, "-", "AWS::CloudFormation::Stack",
                    "StackName", shown(parts),
                    f"stack name lacks the prefix '{prefix}'; CloudFormation-generated physical "
                    "names derive from it")]


def run_checks(templates: List[Template], prefix: str, allow: Iterable[str] = ()) -> List[Finding]:
    allow = list(allow)
    findings: List[Finding] = []
    for t in templates:
        findings += check_stack_name(t, prefix)
        findings += check_sg_text(t)
        findings += check_dms_ssl(t)
        findings += check_names(t, prefix, allow)
    return findings


def parse_parameters(items: Iterable[str], files: Iterable[str]) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """--parameters [Stack:]KEY=VALUE (cdk deploy syntax) and --parameters-file JSON.

    JSON forms: {"KEY": "VALUE"}, {"StackName": {"KEY": "VALUE"}} or the CloudFormation CLI
    list [{"ParameterKey": "KEY", "ParameterValue": "VALUE"}].
    """
    flat: Dict[str, str] = {}
    scoped: Dict[str, Dict[str, str]] = {}
    for f in files:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        if isinstance(data, list):
            for item in data:
                flat[str(item["ParameterKey"])] = str(item["ParameterValue"])
        elif isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    scoped.setdefault(k, {}).update({str(a): str(b) for a, b in v.items()})
                else:
                    flat[str(k)] = str(v)
        else:
            raise ValueError(f"{f}: expected a JSON object or list")
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--parameters expects [Stack:]KEY=VALUE, got {item!r}")
        stack, colon, name = key.partition(":")
        if colon and not key.startswith("AWS::"):
            scoped.setdefault(stack, {})[name] = value
        else:
            flat[key] = value
    return flat, scoped


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fail a deploy on synthesized-template defects that cdk synth does not catch.")
    ap.add_argument("paths", nargs="+", help="cdk.out directory and/or *.template.json files")
    ap.add_argument("--prefix", required=True, help="engagement prefix every visible name must carry")
    ap.add_argument("--allow-name", action="append", default=[], metavar="NAME",
                    help="exact IAM name AWS requires verbatim (repeatable)")
    ap.add_argument("--parameters", action="append", default=[], metavar="[STACK:]KEY=VALUE",
                    help="deploy-time parameter value (cdk deploy syntax; repeatable); also pseudo "
                         "parameters such as AWS::Region=ap-northeast-2")
    ap.add_argument("--parameters-file", action="append", default=[], metavar="JSON",
                    help='{"KEY": "VALUE"}, {"Stack": {"KEY": "VALUE"}} or the CloudFormation '
                         "CLI list form")
    ap.add_argument("--strict", action="store_true",
                    help="warnings (incl. UNRESOLVED) also fail")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    prefix = args.prefix.strip()
    if not prefix:
        print("predeploy_check: --prefix must not be empty", file=sys.stderr)
        return 2
    try:
        flat, scoped = parse_parameters(args.parameters, args.parameters_file)
        templates = load_templates(args.paths, flat, scoped)
    except (FileNotFoundError, ValueError, KeyError, OSError) as exc:
        print(f"predeploy_check: {exc}", file=sys.stderr)
        return 2
    if not templates:
        print("predeploy_check: no *.template.json found — run `cdk synth` first "
              "(an empty scan is not a pass)", file=sys.stderr)
        return 2
    findings = run_checks(templates, prefix, args.allow_name)
    fails = [f for f in findings if f.level == "FAIL"]
    warns = [f for f in findings if f.level == "WARN"]
    blocked = bool(fails) or (args.strict and bool(warns))
    if args.json:
        print(json.dumps({
            "result": "FAIL" if blocked else "PASS",
            "prefix": prefix,
            "templates_scanned": [str(t.path) if t.instance is None else f"{t.path} [{t.instance}]" for t in templates],
            "failures": [asdict(f) for f in fails],
            "warnings": [asdict(f) for f in warns],
        }, indent=2, ensure_ascii=False))
    else:
        for f in fails + warns:
            print(f.line())
        print(f"predeploy_check: {'FAIL' if blocked else 'PASS'} — {len(templates)} template(s), "
              f"{len(fails)} failure(s), {len(warns)} warning(s)"
              + (" (--strict)" if args.strict else ""))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
