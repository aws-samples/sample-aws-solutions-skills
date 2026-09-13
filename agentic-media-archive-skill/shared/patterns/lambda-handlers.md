# Enrichment pipeline Lambda handlers

Copy these files into the generated project's `lambdas/` directory, then set the environment variables from `config/media-archive.yaml`. The dispatcher and workflow handlers below own ingest and enrichment only; read-side and write-side MCP tool Lambdas belong in their own pattern file.

## File layout

```text
lambdas/
├── common.py
├── dispatch/
│   └── index.py
└── workflow/
    └── index.py
tests/
├── test_common.py
├── test_dispatch.py
└── test_workflow.py
```

## Pattern 1 — `lambdas/common.py`

```python
"""Shared runtime helpers for media-archive workflow and MCP Lambdas."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
INDEX_NAME = os.environ.get("SEARCH_INDEX", "media-clips-v1")

_table_resource: Any | None = None
_s3_client: Any | None = None
_bedrock_client: Any | None = None
_http = urllib3.PoolManager()


def now() -> str:
    return datetime.now(UTC).isoformat()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _positive_int_env(
    name: str,
    *,
    default: int | None = None,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        if default is None:
            raise RuntimeError(f"{name} must be configured")
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be at least {minimum}{upper}")
    return value


def embedding_dimension() -> int:
    # WHY: a model migration can change vector size; a literal dimension silently corrupts search.
    return _positive_int_env("EMBEDDING_DIMENSION")


def embedding_model_id() -> str:
    # WHY: model availability and identifiers change by Region; deployment configuration is authoritative.
    return _required_env("EMBEDDING_MODEL_ID")


def understanding_model_id() -> str:
    # WHY: understanding-model request and response formats are provider-specific and must be selectable.
    return _required_env("UNDERSTANDING_MODEL_ID")


def tenant_id() -> str:
    """Resolve the server-side pilot tenant; never trust a caller-provided tenant."""
    value = os.environ.get("TENANT_ID", "pilot").strip()
    return validate_id(value, "TENANT_ID")


def validate_id(value: str, field: str) -> str:
    value = str(value).strip()
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be 1-128 letters, numbers, '_' or '-'")
    return value


def table() -> Any:
    global _table_resource
    if _table_resource is None:
        # WHY: Lambda process reuse makes this safe and avoids a new DynamoDB resource per invocation.
        _table_resource = boto3.resource("dynamodb").Table(_required_env("CATALOG_TABLE"))
    return _table_resource


def s3() -> Any:
    global _s3_client
    if _s3_client is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        # WHY: explicit SigV4 and virtual addressing avoid endpoint-dependent S3 signing behavior.
        _s3_client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3_client


def bedrock() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        # WHY: long-running video calls need a bounded read timeout and adaptive retries, not SDK defaults.
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=10,
                read_timeout=840,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
    return _bedrock_client


def tool_name(context: Any) -> str:
    """Strip the AgentCore Gateway target prefix when a tool Lambda needs its local operation name."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    return str(custom.get("bedrockAgentCoreToolName", "")).rsplit("___", 1)[-1]


def canonical_hash(value: Any) -> str:
    # WHY: canonical JSON makes retries and reordered dictionaries produce the same idempotency key.
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def ingest_identity(
    asset_id: str,
    *,
    etag: str = "",
    version_id: str = "",
    generation: str = "upload",
) -> tuple[str, str]:
    """Return stable job and Step Functions execution identities for one object generation."""
    asset_id = validate_id(asset_id, "asset_id")
    fingerprint = canonical_hash(
        {
            "tenant_id": tenant_id(),
            "asset_id": asset_id,
            "etag": str(etag).strip('"'),
            "version_id": str(version_id),
            "generation": generation,
        }
    )[:20]
    # WHY: S3 and EventBridge are at-least-once; an object version must map to exactly one execution name.
    return f"ingest-{fingerprint}", f"media-{asset_id[:20]}-{fingerprint}"


def json_safe(value: Any) -> Any:
    """Convert DynamoDB resource values into JSON-serializable response values."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # WHY: DynamoDB returns sets, but Lambda JSON marshalling rejects them; sort for stable API output.
        return sorted((json_safe(item) for item in value), key=str)
    return value


def ddb_safe(value: Any) -> Any:
    """Convert Python values into DynamoDB resource serializer values."""
    if isinstance(value, float):
        # WHY: DynamoDB rejects binary floats; string conversion preserves the caller's decimal intent.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: ddb_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [ddb_safe(item) for item in value]
    if isinstance(value, set):
        return {ddb_safe(item) for item in value}
    return value


def asset_key(asset_id: str) -> dict[str, str]:
    return {
        "PK": f"TENANT#{tenant_id()}",
        "SK": f"ASSET#{validate_id(asset_id, 'asset_id')}",
    }


def get_asset(asset_id: str) -> dict[str, Any]:
    item = table().get_item(Key=asset_key(asset_id)).get("Item")
    if not item:
        raise ValueError("asset not found")
    return item


def get_job(job_id: str) -> dict[str, Any]:
    job_id = validate_id(job_id, "job_id")
    item = table().get_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{job_id}"}
    ).get("Item")
    if not item:
        raise ValueError("job not found")
    return item


def put_job(job_id: str, asset_id: str, stage: str, *, status: str = "QUEUED") -> None:
    timestamp = now()
    try:
        table().put_item(
            Item={
                "PK": f"TENANT#{tenant_id()}",
                "SK": f"JOB#{validate_id(job_id, 'job_id')}",
                "GSI1PK": f"TENANT#{tenant_id()}#JOBS",
                "GSI1SK": timestamp,
                "job_id": job_id,
                "asset_id": validate_id(asset_id, "asset_id"),
                "stage": stage,
                "status": status,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            # WHY: duplicate delivery must preserve the original job rather than reset its progress.
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def update_job(job_id: str, *, status: str, stage: str, message: str = "") -> None:
    table().update_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{validate_id(job_id, 'job_id')}"},
        UpdateExpression="SET #status=:status, #stage=:stage, updated_at=:now, #message=:message",
        ExpressionAttributeNames={"#status": "status", "#stage": "stage", "#message": "message"},
        ExpressionAttributeValues={
            ":status": status,
            ":stage": stage,
            ":now": now(),
            ":message": message[:1000],
        },
    )


def update_asset(asset_id: str, **values: Any) -> None:
    if not values:
        return
    names: dict[str, str] = {}
    data: dict[str, Any] = {":updated_at": now()}
    parts = ["updated_at=:updated_at"]
    for index, (name, value) in enumerate(values.items()):
        name_token = f"#n{index}"
        value_token = f":v{index}"
        names[name_token] = name
        data[value_token] = ddb_safe(value)
        parts.append(f"{name_token}={value_token}")
    table().update_item(
        Key=asset_key(asset_id),
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=data,
    )


def read_json_body(response: dict[str, Any]) -> dict[str, Any]:
    body = response["body"].read()
    return json.loads(body.decode("utf-8"))


def _aoss_url(path: str) -> str:
    endpoint = _required_env("AOSS_ENDPOINT").rstrip("/")
    if not endpoint.startswith("https://"):
        endpoint = f"https://{endpoint}"
    return f"{endpoint}/{path.lstrip('/')}"


def aoss_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    allow_status: set[int] | None = None,
) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    payload_bytes = payload.encode()
    request = AWSRequest(
        method=method,
        url=_aoss_url(path),
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Amz-Content-Sha256": hashlib.sha256(payload_bytes).hexdigest(),
        },
    )
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are unavailable for OpenSearch signing")
    # WHY: AOSS rejects unsigned or OpenSearch-service-signed requests; it requires service name `aoss`.
    SigV4Auth(
        credentials.get_frozen_credentials(),
        "aoss",
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
    ).add_auth(request)
    prepared = request.prepare()
    response = _http.request(
        method,
        prepared.url,
        body=payload.encode() if payload else None,
        headers=dict(prepared.headers),
    )
    allowed = allow_status or set()
    if response.status >= 300 and response.status not in allowed:
        detail = response.data.decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"OpenSearch {method} {path} failed ({response.status}): {detail}")
    if not response.data:
        return {"status": response.status}
    try:
        return json.loads(response.data.decode("utf-8"))
    except json.JSONDecodeError:
        return {"status": response.status, "body": response.data.decode(errors="replace")}


def search_index_mapping(dimension: int) -> dict[str, Any]:
    """Build the AOSS mapping for the selected embedding model's vector width."""
    if not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("embedding dimension must be a positive integer")
    # WHY: the index schema must be generated from the configured model dimension before any document is indexed.
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "tenant_id": {"type": "keyword"},
                "document_key": {"type": "keyword"},
                "asset_id": {"type": "keyword"},
                "source_id": {"type": "keyword"},
                "model_id": {"type": "keyword"},
                "embedding_option": {"type": "keyword"},
                "embedding_scope": {"type": "keyword"},
                "modality": {"type": "keyword"},
                "start_sec": {"type": "double"},
                "end_sec": {"type": "double"},
                "summary": {"type": "text"},
                "topics": {"type": "keyword"},
                "rights_state": {"type": "keyword"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "cosinesimil",
                    },
                },
            }
        },
    }


def ensure_search_index(dimension: int | None = None) -> None:
    dimension = embedding_dimension() if dimension is None else dimension
    head = aoss_request("HEAD", INDEX_NAME, allow_status={404})
    if head.get("status") != 404:
        return
    aoss_request("PUT", INDEX_NAME, search_index_mapping(dimension), allow_status={400, 409})


def extract_embeddings(value: Any, *, expected_dimension: int | None = None) -> list[dict[str, Any]]:
    """Recursively unwrap asynchronous model output and return validated embedding records."""
    expected_dimension = embedding_dimension() if expected_dimension is None else expected_dimension
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "embedding" in node:
                vector = node["embedding"]
                if not isinstance(vector, list):
                    raise ValueError("embedding payload must be a list")
                if len(vector) != expected_dimension:
                    # WHY: silently dropping or padding vectors creates an index/model mismatch that is hard to recover.
                    raise ValueError(
                        f"unexpected embedding dimension: expected {expected_dimension}, got {len(vector)}"
                    )
                if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in vector):
                    raise ValueError("embedding payload must contain only numeric values")
                found.append(node)
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def quote_document_id(value: str) -> str:
    # WHY: document keys include hashes and provider labels; URL encoding prevents path injection into AOSS routes.
    return quote(value, safe="")
```

## Pattern 2 — `lambdas/dispatch/index.py`

```python
"""Idempotent S3 EventBridge dispatcher for newly uploaded media."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from common import get_asset, ingest_identity, put_job, tenant_id, update_asset

_sfn_client: Any | None = None


def _sfn() -> Any:
    global _sfn_client
    if _sfn_client is None:
        # WHY: reuse the client across warm invocations rather than creating sockets for every S3 event.
        _sfn_client = boto3.client("stepfunctions")
    return _sfn_client


def _ignored(reason: str) -> dict[str, str]:
    return {"status": "IGNORED", "reason": reason}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # WHY: EventBridge rules can be broadened accidentally; reject everything except native S3 object-created events.
    if event.get("source") != "aws.s3" or event.get("detail-type") != "Object Created":
        return _ignored("not an S3 object-created event")

    detail = event.get("detail")
    if not isinstance(detail, dict):
        return _ignored("missing event detail")
    bucket = str(detail.get("bucket", {}).get("name", ""))
    object_detail = detail.get("object")
    if not isinstance(object_detail, dict):
        return _ignored("missing object detail")
    key = unquote_plus(str(object_detail.get("key", "")))
    if not bucket or not key:
        return _ignored("missing bucket or object key")

    parts = key.split("/")
    # WHY: only the registered raw/v1 namespace can start enrichment; proxies and derivatives must never loop back.
    if len(parts) < 5 or parts[0] != "raw" or parts[3] != "v1":
        return _ignored("not a managed raw object")
    event_tenant, asset_id = parts[1], parts[2]
    if event_tenant != tenant_id():
        # WHY: prefix partitioning is a second tenant boundary even in a single-tenant pilot.
        return _ignored("tenant prefix mismatch")

    asset = get_asset(asset_id)
    if asset.get("object_key") != key or bucket != os.environ["MEDIA_BUCKET"]:
        # WHY: an EventBridge event alone is not authorization to process an arbitrary object in the bucket.
        raise ValueError("uploaded object does not match the registered asset")

    job_id, execution_name = ingest_identity(
        asset_id,
        etag=str(object_detail.get("etag", "")),
        version_id=str(object_detail.get("version-id", "")),
        generation="upload",
    )
    put_job(job_id, asset_id, "INGEST")
    try:
        response = _sfn().start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=execution_name,
            input=json.dumps(
                {
                    "tenant_id": tenant_id(),
                    "asset_id": asset_id,
                    "job_id": job_id,
                    "reason": "s3-upload",
                }
            ),
        )
        execution_arn = response["executionArn"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
        # WHY: EventBridge uses at-least-once delivery; this is a successful duplicate, not a failed ingest.
        execution_arn = "already-started"

    update_asset(asset_id, status="QUEUED", latest_job_id=job_id)
    return {
        "status": "QUEUED",
        "asset_id": asset_id,
        "job_id": job_id,
        "execution_arn": execution_arn,
    }
```

## Pattern 3 — `lambdas/workflow/index.py`

```python
"""Step Functions task dispatcher for media analysis, enrichment, and vector indexing."""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from common import (
    INDEX_NAME,
    aoss_request,
    bedrock,
    canonical_hash,
    ddb_safe,
    embedding_dimension,
    embedding_model_id,
    ensure_search_index,
    extract_embeddings,
    get_asset,
    get_job,
    now,
    quote_document_id,
    read_json_body,
    s3,
    table,
    tenant_id,
    understanding_model_id,
    update_asset,
    update_job,
    validate_id,
)

MAX_PROXY_CHUNK_MINUTES = 55
PROXY_MAX_BYTES = 2_000_000_000
PROXY_VIDEO_BITRATE = 4_000_000
PROXY_AUDIO_BITRATE = 128_000
PROXY_PROFILE_VERSION = "h264-aac-720p-cbr-v1"

UNDERSTANDING_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "language": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["PERSON", "ORG", "PLACE", "OBJECT", "EVENT", "CONCEPT"],
                    },
                    "label": {"type": "string"},
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": "number"},
                },
                "required": ["type", "label"],
            },
        },
        "editorial_tags": {"type": "array", "items": {"type": "string"}},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "topics", "entities", "editorial_tags"],
}

_mediaconvert_client: Any | None = None


def _positive_int_env(
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be at least {minimum}{upper}")
    return value


def _chunk_seconds() -> int:
    # WHY: video understanding input limits apply per proxy; allow a lower limit but never exceed the approved maximum.
    return _positive_int_env(
        "CHUNK_MINUTES",
        default=MAX_PROXY_CHUNK_MINUTES,
        maximum=MAX_PROXY_CHUNK_MINUTES,
    ) * 60


def _max_chunks() -> int:
    # WHY: a bounded fan-out protects MediaConvert cost and Step Functions history from arbitrary-duration uploads.
    return _positive_int_env("MAX_CHUNKS", default=5)


def _embedding_poll_max_attempts() -> int:
    # WHY: a completed async API operation can still be permanently unavailable; never poll forever.
    return _positive_int_env("EMBEDDING_POLL_MAX_ATTEMPTS", default=720)


def _embedding_poll_interval_seconds() -> int:
    return _positive_int_env("EMBEDDING_POLL_INTERVAL_SECONDS", default=30)


def _mediaconvert() -> Any:
    global _mediaconvert_client
    if _mediaconvert_client is None:
        # WHY: warm Lambda reuse avoids creating a MediaConvert client for every polling tick.
        _mediaconvert_client = boto3.client("mediaconvert")
    return _mediaconvert_client


def _ids(event: dict[str, Any]) -> tuple[str, str]:
    if event.get("tenant_id") != tenant_id():
        # WHY: Step Functions input is data, not a trusted tenancy assertion; validate it again at every task.
        raise ValueError("workflow tenant does not match configured tenant")
    return (
        validate_id(str(event.get("asset_id", "")), "asset_id"),
        validate_id(str(event.get("job_id", "")), "job_id"),
    )


def _payload(asset_id: str, job_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id(),
        "asset_id": asset_id,
        "job_id": job_id,
        "status": status,
        **extra,
    }


def _proxy_key(job_id: str, index: int) -> dict[str, str]:
    return {"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{job_id}#PROXY#{index:04d}"}


def _timecode_at_second(seconds: float) -> str:
    total = max(0, int(math.floor(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:00"


def _proxy_plan(asset_id: str, job_id: str, duration_seconds: float) -> list[dict[str, Any]]:
    if duration_seconds <= 0:
        raise ValueError("source duration must be greater than zero")
    chunk_seconds = _chunk_seconds()
    count = math.ceil(duration_seconds / chunk_seconds)
    if count > _max_chunks():
        raise ValueError(
            "source duration exceeds MAX_CHUNKS × CHUNK_MINUTES; request an approved larger workflow limit"
        )

    plan: list[dict[str, Any]] = []
    for index in range(count):
        start = float(index * chunk_seconds)
        end = min(float((index + 1) * chunk_seconds), duration_seconds)
        expected_prefix = f"proxies/{tenant_id()}/{asset_id}/{job_id}/segments/{index:04d}/"
        token = canonical_hash(
            {
                "job_id": job_id,
                "index": index,
                "start": start,
                "end": end,
                "profile": PROXY_PROFILE_VERSION,
            }
        )
        plan.append(
            {
                "segment_index": index,
                "source_start_sec": start,
                "source_end_sec": end,
                "expected_prefix": expected_prefix,
                "client_request_token": token,
                "profile_version": PROXY_PROFILE_VERSION,
            }
        )
    return plan


def _get_proxy_segment(job_id: str, index: int) -> dict[str, Any] | None:
    return table().get_item(Key=_proxy_key(job_id, index)).get("Item")


def _put_proxy_plan(asset_id: str, job_id: str, segment: dict[str, Any]) -> dict[str, Any]:
    item = {
        **_proxy_key(job_id, int(segment["segment_index"])),
        "job_id": job_id,
        "asset_id": asset_id,
        "status": "PLANNED",
        "created_at": now(),
        "updated_at": now(),
        **segment,
    }
    try:
        table().put_item(
            Item=ddb_safe(item),
            # WHY: retries must recover the existing plan and its provider job, not create a second encode.
            ConditionExpression="attribute_not_exists(PK)",
        )
        return item
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    existing = _get_proxy_segment(job_id, int(segment["segment_index"]))
    if not existing:
        raise RuntimeError("proxy segment disappeared after conditional create")
    return existing


def _proxy_job_settings(
    asset: dict[str, Any],
    segment: dict[str, Any],
    segment_count: int,
) -> dict[str, Any]:
    input_settings: dict[str, Any] = {
        "FileInput": f"s3://{os.environ['MEDIA_BUCKET']}/{asset['object_key']}",
        "TimecodeSource": "ZEROBASED",
        # WHY: implicit stream selection can choose commentary or an unintended video track.
        "AudioSelectors": {"Default Audio": {"DefaultSelection": "DEFAULT"}},
        "VideoSelector": {},
    }
    if segment_count > 1:
        # WHY: one clipping range per job keeps each analysis proxy source-relative and avoids concatenation ambiguity.
        input_settings["InputClippings"] = [
            {
                "StartTimecode": _timecode_at_second(segment["source_start_sec"]),
                "EndTimecode": _timecode_at_second(segment["source_end_sec"]),
            }
        ]

    destination = f"s3://{os.environ['MEDIA_BUCKET']}/{segment['expected_prefix']}"
    return {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "Inputs": [input_settings],
        "OutputGroups": [
            {
                "Name": "analysis-proxy",
                "OutputGroupSettings": {
                    "Type": "FILE_GROUP_SETTINGS",
                    "FileGroupSettings": {"Destination": destination},
                },
                "Outputs": [
                    {
                        "NameModifier": f"-analysis-{segment['segment_index']:04d}",
                        "ContainerSettings": {"Container": "MP4"},
                        "VideoDescription": {
                            "Width": 1280,
                            "Height": 720,
                            "ScalingBehavior": "DEFAULT",
                            "CodecSettings": {
                                "Codec": "H_264",
                                "H264Settings": {
                                    # WHY: CBR keeps proxy size predictable for provider input limits.
                                    "RateControlMode": "CBR",
                                    "Bitrate": PROXY_VIDEO_BITRATE,
                                    "FramerateControl": "INITIALIZE_FROM_SOURCE",
                                },
                            },
                        },
                        "AudioDescriptions": [
                            {
                                "AudioSourceName": "Default Audio",
                                "CodecSettings": {
                                    "Codec": "AAC",
                                    "AacSettings": {
                                        "RateControlMode": "CBR",
                                        "Bitrate": PROXY_AUDIO_BITRATE,
                                        "CodingMode": "CODING_MODE_2_0",
                                        "SampleRate": 48_000,
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def prepare(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    update_asset(asset_id, status="PROCESSING", processing_started_at=now())
    update_job(job_id, status="RUNNING", stage="PREPARE")
    return _payload(
        asset_id,
        job_id,
        "PREPARED",
        object_key=asset["object_key"],
        file_size=int(asset["file_size"]),
        duration_seconds=float(asset["duration_seconds"]),
    )


def start_proxy(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    plan = _proxy_plan(asset_id, job_id, float(asset["duration_seconds"]))
    for planned in plan:
        segment = _put_proxy_plan(asset_id, job_id, planned)
        if segment.get("provider_job_id"):
            continue
        response = _mediaconvert().create_job(
            Role=os.environ["MEDIACONVERT_ROLE_ARN"],
            Queue=os.environ["MEDIACONVERT_QUEUE_ARN"],
            ClientRequestToken=segment["client_request_token"],
            StatusUpdateInterval="SECONDS_60",
            UserMetadata={
                "archive_kind": "analysis_proxy",
                "tenant_id": tenant_id(),
                "asset_id": asset_id,
                "workflow_job_id": job_id,
                "segment_index": str(segment["segment_index"]),
                "profile_version": PROXY_PROFILE_VERSION,
            },
            Settings=_proxy_job_settings(asset, segment, len(plan)),
        )
        table().update_item(
            Key=_proxy_key(job_id, int(segment["segment_index"])),
            UpdateExpression="SET provider_job_id=:provider, #status=:status, updated_at=:now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":provider": response["Job"]["Id"],
                ":status": "SUBMITTED",
                ":now": now(),
            },
        )
    update_asset(asset_id, status="PROXYING", proxy_segment_count=len(plan))
    update_job(job_id, status="RUNNING", stage="PROXY")
    return _payload(asset_id, job_id, "IN_PROGRESS")


def _output_paths(job: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for group in job.get("OutputGroupDetails", []):
        for output in group.get("OutputDetails", []):
            paths.extend(output.get("OutputFilePaths", []))
    return paths


def _validate_proxy_output(segment: dict[str, Any], media_job: dict[str, Any]) -> str:
    candidates = [path for path in _output_paths(media_job) if path.lower().endswith(".mp4")]
    if len(candidates) != 1:
        response = s3().list_objects_v2(
            Bucket=os.environ["MEDIA_BUCKET"],
            Prefix=segment["expected_prefix"],
        )
        candidates = [
            f"s3://{os.environ['MEDIA_BUCKET']}/{item['Key']}"
            for item in response.get("Contents", [])
            if str(item["Key"]).lower().endswith(".mp4")
        ]
    if len(candidates) != 1:
        raise RuntimeError("MediaConvert proxy must produce exactly one MP4 output")
    parsed = urlparse(candidates[0])
    if parsed.scheme != "s3" or parsed.netloc != os.environ["MEDIA_BUCKET"]:
        raise RuntimeError("MediaConvert proxy output bucket is invalid")
    key = parsed.path.lstrip("/")
    if not key.startswith(segment["expected_prefix"]):
        # WHY: output validation prevents a malformed job result from escaping its tenant-and-job prefix.
        raise RuntimeError("MediaConvert proxy output escaped the planned prefix")
    metadata = s3().head_object(Bucket=os.environ["MEDIA_BUCKET"], Key=key)
    if int(metadata.get("ContentLength", 0)) >= PROXY_MAX_BYTES:
        raise RuntimeError("MediaConvert proxy exceeds the configured model input size limit")
    return key


def poll_proxy(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    plan = _proxy_plan(asset_id, job_id, float(asset["duration_seconds"]))
    complete = 0
    for planned in plan:
        segment = _get_proxy_segment(job_id, int(planned["segment_index"]))
        if not segment or not segment.get("provider_job_id"):
            return _payload(asset_id, job_id, "IN_PROGRESS")
        if segment.get("status") == "COMPLETED" and segment.get("output_key"):
            complete += 1
            continue
        media_job = _mediaconvert().get_job(Id=segment["provider_job_id"])["Job"]
        status = str(media_job.get("Status", "UNKNOWN"))
        if status in {"SUBMITTED", "PROGRESSING"}:
            continue
        if status in {"ERROR", "CANCELED"}:
            message = str(media_job.get("ErrorMessage") or f"MediaConvert {status}")
            table().update_item(
                Key=_proxy_key(job_id, int(segment["segment_index"])),
                UpdateExpression="SET #status=:status, #message=:message, updated_at=:now",
                ExpressionAttributeNames={"#status": "status", "#message": "message"},
                ExpressionAttributeValues={
                    ":status": "FAILED",
                    ":message": message[:1000],
                    ":now": now(),
                },
            )
            return _payload(asset_id, job_id, "FAILED", message=message)
        if status != "COMPLETE":
            return _payload(asset_id, job_id, "IN_PROGRESS")
        output_key = _validate_proxy_output(segment, media_job)
        table().update_item(
            Key=_proxy_key(job_id, int(segment["segment_index"])),
            UpdateExpression="SET output_key=:key, #status=:status, updated_at=:now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":key": output_key, ":status": "COMPLETED", ":now": now()},
        )
        complete += 1
    if complete == len(plan):
        update_job(job_id, status="RUNNING", stage="EMBEDDING")
        return _payload(asset_id, job_id, "COMPLETED")
    return _payload(asset_id, job_id, "IN_PROGRESS")


# ---- Embedding adapter seam -------------------------------------------------
# Change only embed_request() and parse_embedding() when the selected embedding model changes.


def embed_request(asset: dict[str, Any], output_prefix: str) -> dict[str, Any]:
    """Build the selected embedding model's async request using the canonical archive inputs."""
    # WHY: provider request JSON is model-specific; keeping it here prevents model syntax from leaking into workflow logic.
    return {
        "modelInput": {
            "inputType": "video",
            "video": {
                "mediaSource": {
                    "s3Location": {
                        "uri": f"s3://{os.environ['MEDIA_BUCKET']}/{asset['object_key']}",
                        "bucketOwner": os.environ["AWS_ACCOUNT_ID"],
                    }
                },
                "segmentation": {"method": "dynamic", "dynamic": {"minDurationSec": 4}},
                "embeddingOption": ["visual", "audio", "transcription"],
                "embeddingType": ["separate_embedding"],
                "embeddingScope": ["clip", "asset"],
            },
        },
        "outputDataConfig": {
            "s3OutputDataConfig": {
                "s3Uri": f"s3://{os.environ['MEDIA_BUCKET']}/{output_prefix}",
                "kmsKeyId": os.environ["KMS_KEY_ARN"],
            }
        },
    }


def _label(value: Any, default: str) -> str:
    if isinstance(value, list):
        return "+".join(str(item) for item in value) or default
    return str(value) if value not in (None, "") else default


def parse_embedding(record: dict[str, Any], *, default_end_sec: float) -> dict[str, Any]:
    """Normalize one provider output record into the archive's canonical clip-vector contract."""
    # WHY: format conversion happens before indexing so downstream code never depends on a provider's field names.
    vector = record.get("embedding")
    expected_dimension = embedding_dimension()
    if not isinstance(vector, list) or len(vector) != expected_dimension:
        raise RuntimeError(
            f"unexpected embedding dimension; expected {expected_dimension} from EMBEDDING_DIMENSION"
        )
    return {
        "embedding": vector,
        "embedding_option": _label(record.get("embeddingOption"), "unknown"),
        "embedding_scope": _label(record.get("embeddingScope"), "clip"),
        "start_sec": float(record.get("startSec", 0)),
        "end_sec": float(record.get("endSec", default_end_sec)),
    }


def start_embedding(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    job = get_job(job_id)
    existing_arn = job.get("embedding_invocation_arn")
    if existing_arn:
        # WHY: a retry after the service accepted the async request must continue polling, not submit another model job.
        return _payload(asset_id, job_id, "IN_PROGRESS", embedding_poll_attempt=0)

    output_prefix = f"analysis/{tenant_id()}/{asset_id}/{job_id}/embeddings/"
    model_id = embedding_model_id()
    request = embed_request(asset, output_prefix)
    response = bedrock().start_async_invoke(
        # WHY: deterministic request tokens make the provider call idempotent across Lambda/Step Functions retries.
        clientRequestToken=canonical_hash({"asset_id": asset_id, "job_id": job_id, "model_id": model_id}),
        modelId=model_id,
        **request,
    )
    invocation_arn = response["invocationArn"]
    table().update_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{job_id}"},
        UpdateExpression=(
            "SET embedding_invocation_arn=:arn, embedding_output_prefix=:prefix, "
            "embedding_model_id=:model, #stage=:stage, updated_at=:now"
        ),
        ExpressionAttributeNames={"#stage": "stage"},
        ExpressionAttributeValues={
            ":arn": invocation_arn,
            ":prefix": output_prefix,
            ":model": model_id,
            ":stage": "EMBEDDING",
            ":now": now(),
        },
    )
    update_asset(asset_id, status="EMBEDDING")
    return _payload(asset_id, job_id, "IN_PROGRESS", embedding_poll_attempt=0)


def poll_embedding(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    job = get_job(job_id)
    invocation_arn = job.get("embedding_invocation_arn")
    if not invocation_arn:
        raise ValueError("embedding invocation ARN is missing")

    response = bedrock().get_async_invoke(invocationArn=invocation_arn)
    raw_status = str(response.get("status", "Failed"))
    status = {"InProgress": "IN_PROGRESS", "Completed": "COMPLETED", "Failed": "FAILED"}.get(
        raw_status, raw_status.upper()
    )
    if status == "FAILED":
        message = str(response.get("failureMessage", "embedding invocation failed"))
        update_job(job_id, status="FAILED", stage="EMBEDDING", message=message)
        return _payload(asset_id, job_id, "FAILED", message=message)
    if status == "COMPLETED":
        update_job(job_id, status="RUNNING", stage="UNDERSTANDING")
        return _payload(asset_id, job_id, "COMPLETED")

    attempts = int(event.get("embedding_poll_attempt", 0)) + 1
    if attempts > _embedding_poll_max_attempts():
        message = "embedding polling exceeded EMBEDDING_POLL_MAX_ATTEMPTS"
        update_job(job_id, status="FAILED", stage="EMBEDDING", message=message)
        return _payload(asset_id, job_id, "FAILED", message=message)
    # WHY: the state machine, not a sleeping Lambda, performs the wait and preserves a bounded retry history.
    return _payload(
        asset_id,
        job_id,
        "IN_PROGRESS",
        embedding_poll_attempt=attempts,
        poll_wait_seconds=_embedding_poll_interval_seconds(),
    )


def prepare_enrichment(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    plan = _proxy_plan(asset_id, job_id, float(asset["duration_seconds"]))
    segments: list[dict[str, Any]] = []
    for planned in plan:
        item = _get_proxy_segment(job_id, int(planned["segment_index"]))
        if not item or item.get("status") != "COMPLETED" or not item.get("output_key"):
            raise RuntimeError("analysis proxy is not complete")
        segments.append(
            {
                "segment_index": int(item["segment_index"]),
                "source_start_sec": float(item["source_start_sec"]),
                "source_end_sec": float(item["source_end_sec"]),
                "output_key": item["output_key"],
            }
        )
    return _payload(asset_id, job_id, "READY", analysis_segments=segments)


def _shift_entity_times(entity: dict[str, Any], source_start: float, source_end: float) -> dict[str, Any]:
    shifted = dict(entity)
    if "start_sec" in shifted:
        local_start = max(0.0, float(shifted["start_sec"]))
        shifted["start_sec"] = min(source_end, source_start + local_start)
    if "end_sec" in shifted:
        local_end = max(0.0, float(shifted["end_sec"]))
        start = float(shifted.get("start_sec", source_start))
        shifted["end_sec"] = max(start, min(source_end, source_start + local_end))
    return shifted


# ---- Understanding adapter seam ---------------------------------------------
# Change only understand_request() and parse_understanding() when the selected understanding model changes.


def understand_request(proxy_key: str) -> dict[str, Any]:
    """Build the selected understanding model's synchronous request for one analysis proxy."""
    # WHY: model JSON and completion semantics vary, while the rest of the workflow consumes one canonical analysis shape.
    return {
        "inputPrompt": (
            "Analyze this contiguous proxy segment for a professional media archive. Return only observed "
            "content: summary, speech topics, people or roles, organizations, places, objects, events, "
            "editorial tags, and safety notes. All timestamps must be relative to this proxy starting at "
            "0 seconds. Omit uncertain timestamps and never infer rights."
        ),
        "mediaSource": {
            "s3Location": {
                "uri": f"s3://{os.environ['MEDIA_BUCKET']}/{proxy_key}",
                "bucketOwner": os.environ["AWS_ACCOUNT_ID"],
            }
        },
        "temperature": 0.1,
        "maxOutputTokens": 4096,
        "responseFormat": {"jsonSchema": UNDERSTANDING_SCHEMA},
    }


def parse_understanding(model_response: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the selected understanding model's response."""
    finish_reason = str(model_response.get("finishReason", ""))
    if finish_reason.lower() in {"length", "max_tokens", "content_filtered", "error"}:
        # WHY: truncated JSON can look syntactically valid but loses findings and source evidence.
        raise RuntimeError(f"understanding output is incomplete: finishReason={finish_reason}")
    if finish_reason and finish_reason.lower() not in {"stop", "complete", "end_turn"}:
        raise RuntimeError(f"unexpected understanding finishReason={finish_reason}")
    message = model_response.get("message")
    if not isinstance(message, str):
        raise RuntimeError("understanding response does not contain a JSON message string")
    try:
        analysis = json.loads(message)
    except json.JSONDecodeError as error:
        raise RuntimeError("understanding response is not valid JSON") from error
    if not isinstance(analysis, dict):
        raise RuntimeError("understanding response must be a JSON object")
    for required in ("summary", "topics", "entities", "editorial_tags"):
        if required not in analysis:
            raise RuntimeError(f"understanding response omitted required field: {required}")
    if not isinstance(analysis["entities"], list):
        raise RuntimeError("understanding response entities must be a list")
    return analysis


def enrich_segment(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    segment = event.get("segment")
    if not isinstance(segment, dict):
        raise ValueError("segment is required")
    index = int(segment["segment_index"])
    existing = _get_proxy_segment(job_id, index)
    if existing and existing.get("enrichment_status") == "COMPLETED" and existing.get("analysis_key"):
        # WHY: the S3 read confirms that a partially written DynamoDB update is not treated as completed work.
        s3().head_object(Bucket=os.environ["MEDIA_BUCKET"], Key=existing["analysis_key"])
        return {"segment_index": index, "analysis_key": existing["analysis_key"], "status": "COMPLETED"}

    source_start = float(segment["source_start_sec"])
    source_end = float(segment["source_end_sec"])
    output_key = str(segment["output_key"])
    expected_prefix = f"proxies/{tenant_id()}/{asset_id}/{job_id}/segments/{index:04d}/"
    if not output_key.startswith(expected_prefix):
        raise ValueError("proxy key escaped its planned prefix")

    response = bedrock().invoke_model(
        modelId=understanding_model_id(),
        body=json.dumps(understand_request(output_key)),
        contentType="application/json",
        accept="application/json",
    )
    analysis = parse_understanding(read_json_body(response))
    # WHY: model timestamps are proxy-relative; searching and rendering require source-relative evidence.
    analysis["entities"] = [
        _shift_entity_times(entity, source_start, source_end) for entity in analysis.get("entities", [])
    ]
    analysis.update(
        {
            "model_id": understanding_model_id(),
            "segment_index": index,
            "source_start_sec": source_start,
            "source_end_sec": source_end,
            "proxy_key": output_key,
        }
    )
    analysis_key = f"analysis/{tenant_id()}/{asset_id}/{job_id}/understanding/segments/{index:04d}.json"
    s3().put_object(
        Bucket=os.environ["MEDIA_BUCKET"],
        Key=analysis_key,
        Body=json.dumps(analysis, ensure_ascii=False).encode(),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ["KMS_KEY_ARN"],
    )
    table().update_item(
        Key=_proxy_key(job_id, index),
        UpdateExpression="SET analysis_key=:key, enrichment_status=:status, updated_at=:now",
        ExpressionAttributeValues={":key": analysis_key, ":status": "COMPLETED", ":now": now()},
    )
    return {"segment_index": index, "analysis_key": analysis_key, "status": "COMPLETED"}


def _stable_unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _store_ontology(asset_id: str, entities: list[dict[str, Any]]) -> None:
    for entity in entities:
        label = str(entity.get("label", "")).strip()
        entity_type = str(entity.get("type", "CONCEPT")).upper()
        if not label:
            continue
        concept_id = hashlib.sha256(f"{entity_type}:{label.casefold()}".encode()).hexdigest()[:24]
        timestamp = now()
        table().update_item(
            Key={"PK": f"TENANT#{tenant_id()}", "SK": f"CONCEPT#{concept_id}"},
            UpdateExpression=(
                "SET GSI1PK=:gsi_pk, GSI1SK=:gsi_sk, concept_id=:concept_id, "
                "#type=:type, label=:label, #status=if_not_exists(#status,:status), "
                "created_at=if_not_exists(created_at,:now), updated_at=:now "
                "ADD source_asset_ids :source_ids"
            ),
            ExpressionAttributeNames={"#type": "type", "#status": "status"},
            ExpressionAttributeValues={
                ":gsi_pk": f"TENANT#{tenant_id()}#CONCEPTS",
                ":gsi_sk": f"{entity_type}#{label.casefold()}#{concept_id}",
                ":concept_id": concept_id,
                ":type": entity_type,
                ":label": label,
                # WHY: model concepts are suggestions; review is required before controlled-vocabulary approval.
                ":status": "MODEL_SUGGESTED",
                ":source_ids": {asset_id},
                ":now": timestamp,
            },
        )


def merge_enrichment(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    plan = _proxy_plan(asset_id, job_id, float(asset["duration_seconds"]))
    analyses: list[dict[str, Any]] = []
    for planned in plan:
        item = _get_proxy_segment(job_id, int(planned["segment_index"]))
        if not item or item.get("enrichment_status") != "COMPLETED":
            raise RuntimeError("understanding segment enrichment is incomplete")
        body = s3().get_object(Bucket=os.environ["MEDIA_BUCKET"], Key=item["analysis_key"])["Body"].read()
        analyses.append(json.loads(body.decode("utf-8")))

    analyses.sort(key=lambda item: int(item["segment_index"]))
    entities = _stable_unique([entity for analysis in analyses for entity in analysis.get("entities", [])])
    topics = _stable_unique([topic for analysis in analyses for topic in analysis.get("topics", [])])
    editorial_tags = _stable_unique(
        [tag for analysis in analyses for tag in analysis.get("editorial_tags", [])]
    )
    safety_notes = _stable_unique(
        [note for analysis in analyses for note in analysis.get("safety_notes", [])]
    )
    summary = "\n".join(
        f"[{analysis['source_start_sec']:.1f}-{analysis['source_end_sec']:.1f}s] {analysis.get('summary', '')}"
        for analysis in analyses
    )
    manifest_key = f"analysis/{tenant_id()}/{asset_id}/{job_id}/understanding/manifest.json"
    manifest = {
        "asset_id": asset_id,
        "job_id": job_id,
        "model_id": understanding_model_id(),
        "segments": analyses,
        "summary": summary,
        "topics": topics,
        "entities": entities,
        "editorial_tags": editorial_tags,
        "safety_notes": safety_notes,
    }
    s3().put_object(
        Bucket=os.environ["MEDIA_BUCKET"],
        Key=manifest_key,
        Body=json.dumps(manifest, ensure_ascii=False).encode(),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ["KMS_KEY_ARN"],
    )
    analysis = {
        "summary": summary,
        "topics": topics,
        "entities": entities[:200],
        "entities_truncated": len(entities) > 200,
        "editorial_tags": editorial_tags,
        "safety_notes": safety_notes,
        "enrichment_status": "COMPLETED",
        "model_id": manifest["model_id"],
        "segment_count": len(analyses),
        "manifest_key": manifest_key,
    }
    _store_ontology(asset_id, entities)
    update_asset(asset_id, status="ENRICHED", analysis=analysis)
    update_job(job_id, status="RUNNING", stage="INDEX")
    return _payload(asset_id, job_id, "ENRICHED")


def _load_embedding_results(prefix: str, *, default_end_sec: float) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=os.environ["MEDIA_BUCKET"], Prefix=prefix):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            if not (key.endswith(".json") or key.endswith("output")):
                continue
            body = s3().get_object(Bucket=os.environ["MEDIA_BUCKET"], Key=key)["Body"].read()
            payload = json.loads(body.decode("utf-8"))
            # WHY: asynchronous providers add nesting over time; recursive validation accepts wrappers but rejects bad vectors.
            raw_records = extract_embeddings(payload, expected_dimension=embedding_dimension())
            records.extend(parse_embedding(record, default_end_sec=default_end_sec) for record in raw_records)
    return records


def index_embeddings(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    asset = get_asset(asset_id)
    job = get_job(job_id)
    records = _load_embedding_results(
        str(job.get("embedding_output_prefix", "")),
        default_end_sec=float(asset["duration_seconds"]),
    )
    if not records:
        raise RuntimeError("embedding model completed but no validated embedding records were found")

    ensure_search_index(embedding_dimension())
    analysis = asset.get("analysis", {})
    indexed = 0
    for record in records:
        document_key = canonical_hash(
            {
                "asset_id": asset_id,
                "model_id": embedding_model_id(),
                "option": record["embedding_option"],
                "scope": record["embedding_scope"],
                "start": record["start_sec"],
                "end": record["end_sec"],
            }
        )[:40]
        document = {
            "tenant_id": tenant_id(),
            "document_key": document_key,
            "asset_id": asset_id,
            "source_id": str(asset.get("source_id", asset_id)),
            "model_id": embedding_model_id(),
            "embedding_option": record["embedding_option"],
            "embedding_scope": record["embedding_scope"],
            "modality": record["embedding_option"],
            "start_sec": record["start_sec"],
            "end_sec": record["end_sec"],
            "summary": analysis.get("summary", ""),
            "topics": analysis.get("topics", []),
            "rights_state": asset.get("rights_state", "RESTRICTED"),
            "embedding": record["embedding"],
        }
        # WHY: a deterministic document id makes index retries replace the same vector instead of duplicating search hits.
        aoss_request("PUT", f"{INDEX_NAME}/_doc/{quote_document_id(document_key)}", document)
        indexed += 1

    update_asset(asset_id, status="INDEXED", indexed_vectors=indexed)
    update_job(job_id, status="RUNNING", stage="FINALIZE")
    return _payload(asset_id, job_id, "INDEXED", indexed_vectors=indexed)


def finish(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    update_asset(asset_id, status="READY", search_ready_at=now())
    update_job(job_id, status="COMPLETED", stage="COMPLETE")
    return _payload(asset_id, job_id, "COMPLETED")


def mark_failed(event: dict[str, Any]) -> dict[str, Any]:
    asset_id, job_id = _ids(event)
    error = event.get("error", {})
    if not isinstance(error, dict):
        error = {"Cause": str(error)}
    message = str(error.get("Cause") or error.get("Error") or event.get("message") or "failed")
    update_asset(asset_id, status="FAILED", failure_message=message[:1000])
    update_job(job_id, status="FAILED", stage="FAILED", message=message)
    return _payload(asset_id, job_id, "FAILED", message=message[:1000])


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    action = str(event.get("action", ""))
    if action == "mark_failed" and isinstance(event.get("payload"), dict):
        event = {"action": action, **event["payload"]}
    actions = {
        "prepare": prepare,
        "start_proxy": start_proxy,
        "poll_proxy": poll_proxy,
        "start_embedding": start_embedding,
        "poll_embedding": poll_embedding,
        "prepare_enrichment": prepare_enrichment,
        "enrich_segment": enrich_segment,
        "merge_enrichment": merge_enrichment,
        "index_embeddings": index_embeddings,
        "finish": finish,
        "mark_failed": mark_failed,
    }
    if action not in actions:
        raise ValueError(f"unknown workflow action: {action}")
    return actions[action](event)
```

## Pattern 4 — Step Functions wiring notes

Pair the handlers above with these CDK state-machine rules:

- Send `{ action, tenant_id, asset_id, job_id }` to every Lambda task and set each task `resultPath` to `$` so the next task retains the canonical IDs.
- `start_proxy → Wait → poll_proxy` loops until `COMPLETED`; on `FAILED`, send the full result into `mark_failed`. Retry transient Lambda, SDK-throttling, and service-unavailable failures with exponential backoff, but do not retry validation failures.
- `start_embedding → Wait → poll_embedding` uses the returned `poll_wait_seconds`. Configure the state machine timeout to 12 hours and set `EMBEDDING_POLL_MAX_ATTEMPTS × EMBEDDING_POLL_INTERVAL_SECONDS` to the selected model's approved per-invocation bound (six hours by default).
- `prepare_enrichment` supplies `analysis_segments` to a `Map` whose `MaxConcurrency` comes from configuration (default `2`). Each iteration calls `enrich_segment` with one `segment`; the Map must retry a transient model call but must not continue after a malformed or truncated model response.
- Route all `Catch` branches to `mark_failed`, then end successfully only after that status is persisted. The terminal `finish` task is the only transition to `READY`.

## Pattern 5 — representative pytest coverage

Run tests with the Lambda directory on `PYTHONPATH` so imports mirror the deployed module layout.

### `tests/test_common.py`

```python
from decimal import Decimal

import pytest

import common


def test_extract_embeddings_unwraps_nested_async_payload_and_validates_dimension():
    payload = {
        "output": [
            {
                "data": {
                    "embedding": [0.1, 0.2, 0.3],
                    "embeddingOption": "visual",
                    "startSec": 0,
                    "endSec": 4.2,
                }
            }
        ]
    }
    assert common.extract_embeddings(payload, expected_dimension=3) == [payload["output"][0]["data"]]


def test_extract_embeddings_rejects_dimension_mismatch_instead_of_skipping_it():
    with pytest.raises(ValueError, match="expected 3"):
        common.extract_embeddings({"data": {"embedding": [0.1, 0.2]}}, expected_dimension=3)


def test_json_safe_converts_decimal_and_dynamodb_sets_recursively():
    assert common.json_safe(
        {"whole": Decimal("1"), "fraction": [Decimal("2.5")], "ids": {"b", "a"}}
    ) == {"whole": 1, "fraction": [2.5], "ids": ["a", "b"]}
```

### `tests/test_dispatch.py`

```python
from dispatch import index


def test_dispatch_ignores_non_raw_object():
    result = index.handler(
        {
            "source": "aws.s3",
            "detail-type": "Object Created",
            "detail": {
                "bucket": {"name": "media-bucket"},
                "object": {"key": "derivatives/pilot/asset-1/output.mp4"},
            },
        },
        None,
    )
    assert result == {"status": "IGNORED", "reason": "not a managed raw object"}


def test_dispatch_ignores_another_tenant(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "pilot")
    result = index.handler(
        {
            "source": "aws.s3",
            "detail-type": "Object Created",
            "detail": {
                "bucket": {"name": "media-bucket"},
                "object": {"key": "raw/another-tenant/asset-1/v1/source.mp4"},
            },
        },
        None,
    )
    assert result == {"status": "IGNORED", "reason": "tenant prefix mismatch"}
```

### `tests/test_workflow.py`

```python
from workflow import index


def asset(duration: float) -> dict:
    return {
        "asset_id": "asset-1",
        "object_key": "raw/pilot/asset-1/v1/asset-1.mp4",
        "file_size": 1000,
        "duration_seconds": duration,
        "rights_state": "OWNED",
    }


def configure(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_ID", "pilot")
    monkeypatch.setenv("MEDIA_BUCKET", "media-bucket")
    monkeypatch.setenv("CHUNK_MINUTES", "55")
    monkeypatch.setenv("MAX_CHUNKS", "5")


def test_proxy_plan_respects_configured_chunk_and_maximum(monkeypatch):
    configure(monkeypatch)
    plan = index._proxy_plan("asset-1", "job-1", 55 * 60 * 3 + 1)
    assert len(plan) == 4
    assert all(
        segment["source_end_sec"] - segment["source_start_sec"] <= 55 * 60 for segment in plan
    )
    assert plan[0]["source_start_sec"] == 0


def test_proxy_settings_use_one_clip_per_chunk_and_cbr(monkeypatch):
    configure(monkeypatch)
    plan = index._proxy_plan("asset-1", "job-1", 7_000)
    settings = index._proxy_job_settings(asset(7_000), plan[0], len(plan))
    media_input = settings["Inputs"][0]
    output = settings["OutputGroups"][0]["Outputs"][0]

    assert len(media_input["InputClippings"]) == 1
    assert media_input["TimecodeSource"] == "ZEROBASED"
    assert media_input["AudioSelectors"] == {"Default Audio": {"DefaultSelection": "DEFAULT"}}
    assert output["VideoDescription"]["CodecSettings"]["H264Settings"] == {
        "RateControlMode": "CBR",
        "Bitrate": 4_000_000,
        "FramerateControl": "INITIALIZE_FROM_SOURCE",
    }
    assert output["AudioDescriptions"][0]["CodecSettings"]["AacSettings"]["Bitrate"] == 128_000


def test_proxy_local_entity_times_shift_to_source_timeline():
    shifted = index._shift_entity_times(
        {"type": "EVENT", "label": "goal", "start_sec": 2, "end_sec": 9},
        3_300,
        3_305,
    )
    assert shifted["start_sec"] == 3_302
    assert shifted["end_sec"] == 3_305


def test_parse_embedding_rejects_wrong_configured_dimension(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSION", "3")
    try:
        index.parse_embedding({"embedding": [0.1, 0.2]}, default_end_sec=10)
    except RuntimeError as error:
        assert "expected 3" in str(error)
    else:
        raise AssertionError("dimension mismatch must fail closed")
```

## Model-swap guidance

The selected embedding and understanding models are a runtime discovery decision, not a hardcoded catalog. During Discovery, run `aws bedrock list-foundation-models --region <region>` (or use AWS Knowledge MCP), filter for video-capable embedding and understanding candidates, present the filtered choices, and record the selected IDs and verified vector dimension in `config/media-archive.yaml`.

When the embedding model changes:

1. Set `EMBEDDING_MODEL_ID` and the verified `EMBEDDING_DIMENSION`; regenerate the AOSS mapping with that dimension.
2. Change only `embed_request()` and `parse_embedding()` for provider-specific input, output wrapper, timestamps, and modality fields. Keep their canonical return contract: numeric `embedding`, `embedding_option`, `embedding_scope`, `start_sec`, and `end_sec`.
3. Never mix vectors from two embedding models in one physical index. Create a new index, reindex historical content, dual-write only during the verified migration window, then cut the search alias over.
4. Re-run the nested-payload and dimension-rejection tests against a captured, sanitized provider response.

When the understanding model changes:

1. Set `UNDERSTANDING_MODEL_ID`, then change only `understand_request()` and `parse_understanding()` for that model's video input syntax, structured-output contract, and completion field.
2. Keep `UNDERSTANDING_SCHEMA` and the canonical output keys (`summary`, `topics`, `entities`, `editorial_tags`, `safety_notes`) unless the product contract itself changes.
3. Preserve the completion guard and source-timeline timestamp shift; model text is advisory metadata and must not auto-approve ontology vocabulary or rights.
4. Re-evaluate the model's per-input duration, size, concurrency, and async/sync behavior. Update `CHUNK_MINUTES`, `MAX_CHUNKS`, Map concurrency, polling bounds, and tests together.

## Cross-layer mapping — one ingest

1. A registered upload writes an immutable original under `raw/{tenant}/{asset_id}/v1/`. The CDK EventBridge rule matches only `aws.s3` `Object Created` events from the media bucket and invokes `dispatch/index.handler`.
2. The dispatcher validates event shape, raw-prefix version, server-side tenant, and the registered DynamoDB `object_key`. It derives an ETag/version-based job identity, conditionally persists `JOB#{job_id}`, and starts one named Step Functions execution; `ExecutionAlreadyExists` is treated as a successful duplicate.
3. The state machine invokes `prepare`, proxy planning and MediaConvert polling, then starts and polls the configured embedding model. Proxy plans and provider job IDs are persisted in DynamoDB so retrying a state machine task does not re-encode media.
4. `prepare_enrichment` passes proxy descriptors to the bounded-concurrency Map. Each `enrich_segment` task writes a source-time-normalized JSON result beneath `analysis/`; `merge_enrichment` persists advisory ontology candidates as `MODEL_SUGGESTED` and updates the asset analysis record.
5. `index_embeddings` recursively parses and dimension-validates embedding output, ensures the configured-dimension AOSS mapping, and performs deterministic document-id upserts. `finish` is the only path that moves the asset to `READY`, after DynamoDB status and AOSS vectors are both durable.
