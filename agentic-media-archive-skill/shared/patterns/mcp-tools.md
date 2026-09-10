# MCP tool layer

This pattern provides the two Amazon Bedrock AgentCore Gateway Lambda targets for a media archive. Copy the schemas and handlers into the generated project, then adapt only the environment names and model-specific Bedrock request/response adapter chosen during Discovery. The Gateway target name is part of the public tool contract.

## File layout

```text
<project-root>/
├── config/media-archive.yaml
├── infra/lib/media-archive-stack.ts
├── lambdas/
│   ├── common.py                    # tenant resolution, DynamoDB/S3/AOSS helpers
│   ├── catalog/
│   │   ├── index.py                 # media-catalog read target (10 tools)
│   │   └── schema.json
│   └── control/
│       ├── index.py                 # media-commands write target (6 tools)
│       └── schema.json
└── tests/
    ├── test_catalog.py
    └── test_control.py
```

Keep both schemas beside their Lambda handlers. The CDK stack reads those files at synth time and AgentCore Gateway uses their descriptions for semantic tool selection.

## Tool contract

| Tool | Gateway target | Purpose | Input summary | Safety rule |
|---|---|---|---|---|
| `list_assets` | `media-catalog` | List recent assets and safe processing metadata. | Optional `limit`. | Return a projection only; never return object keys, checksums, DynamoDB keys, or signed URLs. |
| `get_asset` | `media-catalog` | Read one asset's safe metadata and advisory enrichment. | `asset_id`. | Do not expose storage locations or treat model tags as approved vocabulary. |
| `search_assets` | `media-catalog` | Find source-timestamped clips by natural-language intent. | `query`, optional rights filter and `limit`. | Filter server-resolved tenant and clip scope; cite evidence; call weak hits “no confident match.” |
| `analyze_asset` | `media-catalog` | Ask a grounded whole-asset video question. | `asset_id`, `question`. | Reject inputs over the selected model's proxy duration/size limit; do not invent observations. |
| `get_job_status` | `media-catalog` | Read ingest, enrichment, index, or render status. | `job_id`. | Read-only; map provider state without accepting caller-supplied provider IDs. |
| `get_preview_url` | `media-catalog` | Issue an inline analysis-proxy URL for a source timestamp. | `asset_id`, `start_sec`, optional TTL. | Re-check completed proxy, version, and rights; TTL is 60–300 seconds. |
| `get_download_url` | `media-catalog` | Issue a source or rendered-derivative download URL. | `asset_id`, `rendition`, optional TTL. | Re-check rights and derivative ownership server-side; TTL is 60–900 seconds; never persist URLs. |
| `get_ontology` | `media-catalog` | Read suggested concepts with provenance. | Optional `concept_id`, `label`, `limit`. | Treat every model-derived concept as advisory until a human approves it. |
| `list_collections` | `media-catalog` | List saved, cited clip collections. | Optional `limit`. | Return collection metadata only; no rendering or source mutation. |
| `get_collection` | `media-catalog` | Read one saved collection and its evidence clips. | `collection_id`. | Strip DynamoDB internals and preserve source evidence. |
| `create_upload_session` | `media-commands` | Register an asset and issue a signed upload contract. | File metadata, rights, checksum, idempotency key. | Media bytes never traverse MCP or Lambda; signed upload TTL is exactly one hour. |
| `complete_upload` | `media-commands` | Verify the upload and idempotently start ingestion. | `asset_id`, idempotency key. | Require exact registered size, metadata, and optional checksum before dispatch. |
| `request_enrichment` | `media-commands` | Re-run proxy, understanding, embedding, and indexing work. | `asset_id`, idempotency key. | Make at-least-once dispatch safe; disclose that it can incur model and indexing cost. |
| `render_highlight` | `media-commands` | Create a non-destructive MP4 derivative from ordered ranges. | `asset_id`, ordered clip ranges, idempotency key. | Reject restricted assets and overlapping/out-of-range clips; write only under `derivatives/`. |
| `request_archive` | `media-commands` | Request lifecycle archival for a source. | `asset_id`, `confirm_archive`. | Require literal `confirm_archive=true`; apply a tag only—S3 lifecycle moves the object. |
| `create_collection` | `media-commands` | Save a reusable, cited set of clips. | Name, optional query, clips, idempotency key. | Metadata only; validate each cited range against its asset and never render or modify originals. |

## Pattern 1 — `lambdas/catalog/schema.json`

Every description deliberately contains a `Use for queries like:` line. AgentCore Gateway semantic search uses this natural-language vocabulary when deciding which target tool to invoke.

```json
[
  {
    "name": "list_assets",
    "description": "List recently registered media assets and their safe processing, rights, storage, and enrichment state.\n\nUse for queries like: \"show recent videos\", \"list media in the archive\", \"what assets are ready\", \"최근 업로드 영상 보여줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "limit": { "type": "integer", "minimum": 1, "maximum": 100, "default": 25 }
      }
    }
  },
  {
    "name": "get_asset",
    "description": "Get one media asset's safe metadata, summary, topics, entities, rights state, and archive state. Raw S3 keys and checksums are never returned.\n\nUse for queries like: \"show asset details\", \"what do we know about this video\", \"이 영상 정보 보여줘\".",
    "inputSchema": {
      "type": "object",
      "properties": { "asset_id": { "type": "string" } },
      "required": ["asset_id"]
    }
  },
  {
    "name": "search_assets",
    "description": "Search source-timestamped media clips with the configured video embedding model. Returns asset_id, start_sec, end_sec, modality, score, confidence (high/medium/low), and source_id evidence. High confidence may be cited, medium should be previewed, and low means no confident match.\n\nUse for queries like: \"find a person entering the building\", \"show clips of a product close-up\", \"find the goal celebration\", \"회의실에서 발표하는 장면 찾아줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Natural-language intent, not keywords. Combine action + subject + context; Korean and English both work."
        },
        "rights_state": { "type": "string", "enum": ["OWNED", "LICENSED", "RESTRICTED"] },
        "limit": { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
      },
      "required": ["query"]
    }
  },
  {
    "name": "analyze_asset",
    "description": "Ask the configured video-understanding model a grounded question about one eligible whole asset. For clip timestamps, use search_assets instead.\n\nUse for queries like: \"what happens in this video\", \"is a safety helmet visible\", \"이 영상에서 누가 발표하나요\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "question": { "type": "string", "minLength": 1, "maxLength": 8000 }
      },
      "required": ["asset_id", "question"]
    }
  },
  {
    "name": "get_job_status",
    "description": "Get ingestion, enrichment, indexing, or MediaConvert render status from a media archive job.\n\nUse for queries like: \"is my upload ready\", \"check render progress\", \"enrichment job status\", \"렌더링 작업 상태 알려줘\".",
    "inputSchema": {
      "type": "object",
      "properties": { "job_id": { "type": "string" } },
      "required": ["job_id"]
    }
  },
  {
    "name": "get_preview_url",
    "description": "Return a 1–5 minute inline URL for the completed analysis-proxy segment containing a source-relative timestamp. The client must seek to playback_start_offset_sec after metadata loads.\n\nUse for queries like: \"preview this search hit\", \"play the moment at 42 seconds\", \"검색 결과 미리보기\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "start_sec": { "type": "number", "minimum": 0, "description": "Source-relative timestamp returned by search_assets" },
        "expires_seconds": { "type": "integer", "minimum": 60, "maximum": 300, "default": 180 }
      },
      "required": ["asset_id", "start_sec"]
    }
  },
  {
    "name": "get_download_url",
    "description": "After server-side rights revalidation, return a short-lived presigned URL for an original source or completed rendered derivative. Never persist or repost the URL.\n\nUse for queries like: \"download this video\", \"get the rendered highlight file\", \"원본 다운로드 링크 줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "rendition": { "type": "string", "default": "source", "description": "Use source or render:<render_id>" },
        "expires_seconds": { "type": "integer", "minimum": 60, "maximum": 900, "default": 300 }
      },
      "required": ["asset_id"]
    }
  },
  {
    "name": "get_ontology",
    "description": "Read model-suggested ontology concepts with provenance. Suggested vocabulary remains advisory until human approval.\n\nUse for queries like: \"show detected concepts\", \"find the concept for a location\", \"추출된 태그와 엔터티 보여줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "concept_id": { "type": "string" },
        "label": { "type": "string" },
        "limit": { "type": "integer", "minimum": 1, "maximum": 100, "default": 50 }
      }
    }
  },
  {
    "name": "list_collections",
    "description": "List saved clip collections, such as curated result sets and highlight-reel plans, newest first.\n\nUse for queries like: \"show saved collections\", \"list my highlight plans\", \"저장한 클립 모음 보여줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "limit": { "type": "integer", "minimum": 1, "maximum": 100, "default": 25 }
      }
    }
  },
  {
    "name": "get_collection",
    "description": "Read one saved collection with its cited clips: asset_id, start_sec, end_sec, source_id, and note.\n\nUse for queries like: \"open this collection\", \"show the clips in my reel plan\", \"클립 모음 세부 내용을 보여줘\".",
    "inputSchema": {
      "type": "object",
      "properties": { "collection_id": { "type": "string" } },
      "required": ["collection_id"]
    }
  }
]
```

## Pattern 2 — `lambdas/catalog/index.py`

```python
"""Read-side MCP tools for the media archive catalog."""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from common import (
    INDEX_NAME,
    aoss_request,
    bedrock,
    ensure_search_index,
    get_job,
    json_safe,
    read_json_body,
    s3,
    table,
    tenant_id,
    validate_id,
)

_mediaconvert_client: Any | None = None


def _mediaconvert() -> Any:
    global _mediaconvert_client
    if _mediaconvert_client is None:
        _mediaconvert_client = boto3.client("mediaconvert")
    return _mediaconvert_client


def _gateway_action(context: Any) -> str:
    """Return the schema name from AgentCore Gateway's target-prefixed name."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw_name = str(custom.get("bedrockAgentCoreToolName", "")).strip()
    prefix = "media-catalog___"
    # WHY: Gateway makes target ownership explicit in tool names; Lambda dispatch must
    # remove only its own prefix so it cannot accidentally execute a command tool.
    if raw_name.startswith(prefix):
        return raw_name[len(prefix) :]
    return raw_name.rsplit("___", 1)[-1]


def _current_asset(asset_id: str) -> dict[str, Any]:
    asset_id = validate_id(asset_id, "asset_id")
    item = table().get_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"ASSET#{asset_id}"},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        raise ValueError("asset not found")
    return item


def _public_asset(asset: dict[str, Any]) -> dict[str, Any]:
    analysis = asset.get("analysis", {})
    # WHY: Object locations, checksum material, and DynamoDB partition keys become
    # capabilities if exposed to a model or client; project a strict allowlist instead.
    return json_safe(
        {
            "asset_id": asset["asset_id"],
            "version": asset.get("version", 1),
            "file_name": asset.get("file_name"),
            "content_type": asset.get("content_type"),
            "file_size": asset.get("file_size"),
            "duration_seconds": asset.get("duration_seconds"),
            "rights_state": asset.get("rights_state"),
            "status": asset.get("status"),
            "storage_state": asset.get("storage_state"),
            "summary": analysis.get("summary"),
            "topics": analysis.get("topics", []),
            "editorial_tags": analysis.get("editorial_tags", []),
            "entities": analysis.get("entities", []),
            "indexed_vectors": asset.get("indexed_vectors", 0),
            "created_at": asset.get("created_at"),
            "updated_at": asset.get("updated_at"),
        }
    )


def list_assets(event: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(event.get("limit", 25)), 100))
    response = table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant_id()}#ASSETS"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return {"assets": [_public_asset(item) for item in response.get("Items", [])]}


def get_asset_detail(event: dict[str, Any]) -> dict[str, Any]:
    return _public_asset(_current_asset(str(event.get("asset_id", ""))))


def _configured_embedding_model() -> str:
    model_id = os.environ.get("EMBEDDING_MODEL_ID", "").strip()
    if not model_id:
        raise RuntimeError("EMBEDDING_MODEL_ID must be recorded in config/media-archive.yaml")
    return model_id


def _embedding_dimension() -> int:
    value = os.environ.get("EMBEDDING_DIMENSION", "").strip()
    if not value.isdecimal() or int(value) <= 0:
        raise RuntimeError("EMBEDDING_DIMENSION must be a positive configured model dimension")
    return int(value)


def _embed_query(query: str) -> list[float]:
    query = query.strip()
    if not query or len(query) > 2000:
        raise ValueError("query must contain 1-2000 characters")
    response = bedrock().invoke_model(
        modelId=_configured_embedding_model(),
        # WHY: This adapter is for the selected video's text-embedding API. Discovery
        # records a compatible model and adapter rather than silently hardcoding one ID.
        body=json.dumps({"inputType": "text", "text": {"inputText": query}}),
        contentType="application/json",
        accept="application/json",
    )
    payload = read_json_body(response)
    data = payload.get("data", payload)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        embedding = data[0].get("embedding")
    elif isinstance(data, dict):
        embedding = data.get("embedding")
    else:
        embedding = None
    dimension = _embedding_dimension()
    if not isinstance(embedding, list) or len(embedding) != dimension:
        raise RuntimeError(f"embedding response did not contain a {dimension}-dimensional vector")
    return [float(value) for value in embedding]


SEARCH_SCORE_HIGH = float(os.environ.get("SEARCH_SCORE_HIGH", "0.80"))
SEARCH_SCORE_MEDIUM = float(os.environ.get("SEARCH_SCORE_MEDIUM", "0.75"))


def _search_confidence(score: float) -> str:
    if score >= SEARCH_SCORE_HIGH:
        return "high"
    if score >= SEARCH_SCORE_MEDIUM:
        return "medium"
    return "low"


def search_assets(event: dict[str, Any]) -> dict[str, Any]:
    query = str(event.get("query", "")).strip()
    limit = max(1, min(int(event.get("limit", 10)), 50))
    candidate_limit = min(max(limit * 6, 30), 200)
    rights = event.get("rights_state")
    if rights is not None and str(rights).upper() not in {"OWNED", "LICENSED", "RESTRICTED"}:
        raise ValueError("rights_state must be OWNED, LICENSED, or RESTRICTED")

    ensure_search_index()
    filters: list[dict[str, Any]] = [
        {"term": {"tenant_id": tenant_id()}},
        {"term": {"embedding_scope": "clip"}},
    ]
    if rights:
        filters.append({"term": {"rights_state": str(rights).upper()}})

    response = aoss_request(
        "POST",
        f"{INDEX_NAME}/_search",
        {
            "size": candidate_limit,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "bool": {
                    "must": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": _embed_query(query),
                                    "k": candidate_limit,
                                }
                            }
                        }
                    ],
                    "filter": filters,
                }
            },
        },
    )

    deduplicated: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for hit in response.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        key = (source.get("asset_id"), source.get("start_sec"), source.get("end_sec"))
        modality = str(source.get("modality") or source.get("embedding_option") or "unknown")
        score = float(hit.get("_score") or 0)
        result = deduplicated.get(key)
        if result is None:
            deduplicated[key] = {
                "asset_id": source.get("asset_id"),
                "start_sec": source.get("start_sec"),
                "end_sec": source.get("end_sec"),
                "modality": modality,
                "modalities": [modality],
                "score": score,
                "summary": source.get("summary"),
                "topics": source.get("topics", []),
                "rights_state": source.get("rights_state"),
                "source_id": hit.get("_id"),
            }
            continue
        # WHY: One clip may be indexed by multiple modalities; collapse it so a
        # multimodal duplicate does not crowd out distinct evidence.
        if modality not in result["modalities"]:
            result["modalities"].append(modality)
        if score > result["score"]:
            result.update({"score": score, "modality": modality, "source_id": hit.get("_id")})

    results = sorted(deduplicated.values(), key=lambda item: item["score"], reverse=True)[:limit]
    for item in results:
        item["confidence"] = _search_confidence(float(item["score"]))

    payload: dict[str, Any] = {"query": query, "results": results, "has_sources": bool(results)}
    # WHY: k-NN always returns neighbors. Explicit abstention guidance keeps a nearby
    # but irrelevant vector from becoming a fabricated visual claim.
    if not results:
        payload["guidance"] = "No indexed clips matched this query; report no confident match."
    elif results[0]["confidence"] == "low":
        payload["guidance"] = (
            "All matches are low-confidence; report no confident match rather than "
            "presenting these clips as evidence."
        )
    return payload


def _configured_understanding_model() -> str:
    model_id = os.environ.get("VIDEO_UNDERSTANDING_MODEL_ID", "").strip()
    if not model_id:
        raise RuntimeError("VIDEO_UNDERSTANDING_MODEL_ID must be recorded in config/media-archive.yaml")
    return model_id


def analyze_asset(event: dict[str, Any]) -> dict[str, Any]:
    asset_id = validate_id(str(event.get("asset_id", "")), "asset_id")
    question = str(event.get("question", "")).strip()
    if not question or len(question) > 8000:
        raise ValueError("question must contain 1-8000 characters")
    asset = _current_asset(asset_id)
    proxy_size = int(asset.get("analysis_proxy_size", asset.get("file_size", 0)))
    duration = float(asset.get("duration_seconds", 0))
    # WHY: Whole-video model calls have model-specific hard input limits; refusing
    # oversized requests is safer than truncating evidence without telling the user.
    if duration > 3600 or proxy_size >= 2_000_000_000:
        raise ValueError("whole-asset analysis requires a <=1 hour, <2 GB analysis proxy")
    media_key = str(asset.get("analysis_proxy_key") or asset["object_key"])
    response = bedrock().invoke_model(
        modelId=_configured_understanding_model(),
        body=json.dumps(
            {
                "inputPrompt": "Answer only from the video. If not observable, say so. " + question,
                "mediaSource": {
                    "s3Location": {
                        "uri": f"s3://{os.environ['MEDIA_BUCKET']}/{media_key}",
                        "bucketOwner": os.environ["MEDIA_BUCKET_OWNER_ACCOUNT_ID"],
                    }
                },
                "temperature": 0.1,
                "maxOutputTokens": 2048,
            }
        ),
        contentType="application/json",
        accept="application/json",
    )
    payload = read_json_body(response)
    return {
        "asset_id": asset_id,
        "answer": payload.get("message", ""),
        "finish_reason": payload.get("finishReason"),
        "sources": [{"asset_id": asset_id, "version": asset.get("version", 1), "duration_seconds": duration}],
        "note": "Whole-asset analysis has no clip timestamps; use search_assets for source-relative evidence.",
    }


def get_job_status(event: dict[str, Any]) -> dict[str, Any]:
    job = get_job(str(event.get("job_id", "")))
    provider_job_id = job.get("provider_job_id")
    if provider_job_id and job.get("stage") == "RENDER":
        provider = _mediaconvert().get_job(Id=provider_job_id)["Job"]
        provider_status = provider.get("Status", "UNKNOWN")
        status = {
            "SUBMITTED": "RUNNING",
            "PROGRESSING": "RUNNING",
            "COMPLETE": "COMPLETED",
            "ERROR": "FAILED",
            "CANCELED": "FAILED",
        }.get(provider_status, provider_status)
    else:
        provider_status = None
        status = job.get("status")
    return json_safe(
        {
            "job_id": job.get("job_id"),
            "asset_id": job.get("asset_id"),
            "stage": job.get("stage"),
            "status": status,
            "provider_status": provider_status,
            "message": job.get("message"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }
    )


def _safe_download_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "media.bin"


def get_preview_url(event: dict[str, Any]) -> dict[str, Any]:
    asset_id = validate_id(str(event.get("asset_id", "")), "asset_id")
    start_sec = float(event.get("start_sec", 0))
    if not math.isfinite(start_sec) or start_sec < 0:
        raise ValueError("start_sec must be a finite non-negative number")
    expires = max(60, min(int(event.get("expires_seconds", 180)), 300))
    asset = _current_asset(asset_id)
    duration = float(asset.get("duration_seconds", 0))
    rights_state = str(asset.get("rights_state", "")).upper()
    latest_job_id = str(asset.get("latest_job_id", ""))
    if (
        asset.get("status") != "READY"
        or rights_state not in {"OWNED", "LICENSED"}
        or not latest_job_id
        or start_sec >= duration
    ):
        raise ValueError("preview unavailable")

    latest_job_id = validate_id(latest_job_id, "latest_job_id")
    job = table().get_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{latest_job_id}"},
        ConsistentRead=True,
    ).get("Item")
    if not job or job.get("asset_id") != asset_id or job.get("status") != "COMPLETED":
        raise ValueError("preview unavailable")

    segment_index = int(start_sec // (55 * 60))
    segment = table().get_item(
        Key={
            "PK": f"TENANT#{tenant_id()}",
            "SK": f"JOB#{latest_job_id}#PROXY#{segment_index:04d}",
        },
        ConsistentRead=True,
    ).get("Item")
    if not segment:
        raise ValueError("preview unavailable")
    source_start = float(segment.get("source_start_sec", -1))
    source_end = float(segment.get("source_end_sec", -1))
    output_key = str(segment.get("output_key", ""))
    expected_prefix = f"proxies/{tenant_id()}/{asset_id}/{latest_job_id}/{segment_index:04d}/"
    if (
        segment.get("asset_id") != asset_id
        or segment.get("job_id") != latest_job_id
        or int(segment.get("segment_index", -1)) != segment_index
        or segment.get("status") != "COMPLETED"
        or not (source_start <= start_sec < source_end <= duration)
        or not output_key.startswith(expected_prefix)
        or not output_key.lower().endswith(".mp4")
    ):
        raise ValueError("preview unavailable")

    metadata = s3().head_object(Bucket=os.environ["MEDIA_BUCKET"], Key=output_key)
    if int(metadata.get("ContentLength", 0)) <= 0:
        raise ValueError("preview unavailable")
    # WHY: Read again immediately before signing. A rights revocation, version change,
    # or replacement workflow between lookup and signing must invalidate the preview.
    current = _current_asset(asset_id)
    if (
        current.get("status") != "READY"
        or str(current.get("rights_state", "")).upper() != rights_state
        or current.get("latest_job_id") != latest_job_id
        or current.get("version") != asset.get("version")
    ):
        raise ValueError("preview unavailable")

    params: dict[str, Any] = {
        "Bucket": os.environ["MEDIA_BUCKET"],
        "Key": output_key,
        "ResponseContentType": "video/mp4",
        "ResponseContentDisposition": f'inline; filename="{asset_id}-preview.mp4"',
        "ResponseCacheControl": "private, no-store, max-age=0",
    }
    if metadata.get("VersionId"):
        params["VersionId"] = metadata["VersionId"]
    url = s3().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
    return json_safe(
        {
            "asset_id": asset_id,
            "asset_version": asset.get("version", 1),
            "rendition": "analysis_proxy",
            "url": url,
            "expires_in_seconds": expires,
            "content_type": "video/mp4",
            "segment_index": segment_index,
            "source_start_sec": source_start,
            "source_end_sec": source_end,
            "requested_source_start_sec": start_sec,
            "playback_start_offset_sec": start_sec - source_start,
            "rights_state": rights_state,
        }
    )


def get_download_url(event: dict[str, Any]) -> dict[str, Any]:
    asset_id = validate_id(str(event.get("asset_id", "")), "asset_id")
    rendition = str(event.get("rendition", "source"))
    asset = _current_asset(asset_id)
    # WHY: A prior search or collection is not authorization. Rights are evaluated at
    # URL issuance, the final point at which data becomes directly retrievable.
    if str(asset.get("rights_state", "")).upper() not in {"OWNED", "LICENSED"}:
        raise ValueError("rights policy forbids downloading this asset")

    if rendition == "source":
        key = asset["object_key"]
        file_name = asset["file_name"]
        content_type = asset["content_type"]
    elif rendition.startswith("render:"):
        render_id = validate_id(rendition.split(":", 1)[1], "render_id")
        job = get_job(f"render-{render_id[:20]}")
        if job.get("asset_id") != asset_id or job.get("stage") != "RENDER":
            raise ValueError("rendered derivative does not belong to this asset")
        prefix = str(job.get("derivative_prefix", ""))
        candidates = [
            item["Key"]
            for item in s3()
            .list_objects_v2(Bucket=os.environ["MEDIA_BUCKET"], Prefix=prefix, MaxKeys=20)
            .get("Contents", [])
            if item["Key"].lower().endswith(".mp4")
        ]
        if not candidates:
            raise ValueError("rendered derivative is not available yet")
        key = candidates[0]
        file_name = f"{asset_id}-{render_id}.mp4"
        content_type = "video/mp4"
    else:
        raise ValueError("rendition must be 'source' or 'render:<render_id>'")

    expires = max(60, min(int(event.get("expires_seconds", 300)), 900))
    url = s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["MEDIA_BUCKET"],
            "Key": key,
            "ResponseContentType": content_type,
            "ResponseContentDisposition": f'attachment; filename="{_safe_download_name(file_name)}"',
        },
        ExpiresIn=expires,
    )
    return {
        "asset_id": asset_id,
        "rendition": rendition,
        "url": url,
        "expires_in_seconds": expires,
        "rights_state": asset.get("rights_state"),
    }


def get_ontology(event: dict[str, Any]) -> dict[str, Any]:
    concept_id = str(event.get("concept_id", "")).strip()
    if concept_id:
        concept_id = validate_id(concept_id, "concept_id")
        item = table().get_item(
            Key={"PK": f"TENANT#{tenant_id()}", "SK": f"CONCEPT#{concept_id}"}
        ).get("Item")
        if not item:
            raise ValueError("ontology concept not found")
        # WHY: Returning provenance and approval state prevents a model suggestion
        # from being presented as an approved controlled-vocabulary term.
        return {"concepts": [json_safe(item)], "advisory": True}

    limit = max(1, min(int(event.get("limit", 50)), 100))
    response = table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant_id()}#CONCEPTS"),
        Limit=limit,
    )
    label_query = str(event.get("label", "")).casefold().strip()
    concepts = [json_safe(item) for item in response.get("Items", [])]
    if label_query:
        concepts = [
            item for item in concepts if label_query in str(item.get("label", "")).casefold()
        ]
    return {"concepts": concepts, "advisory": True}


def _public_collection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK", "GSI1PK", "GSI1SK"}
    }


def list_collections(event: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(event.get("limit", 25)), 100))
    response = table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant_id()}#COLLECTIONS"),
        Limit=limit,
        ScanIndexForward=False,
    )
    return {"collections": [json_safe(_public_collection(item)) for item in response.get("Items", [])]}


def get_collection(event: dict[str, Any]) -> dict[str, Any]:
    collection_id = validate_id(str(event.get("collection_id", "")), "collection_id")
    item = table().get_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"COLLECTION#{collection_id}"}
    ).get("Item")
    if not item:
        raise ValueError("collection not found")
    return json_safe(_public_collection(item))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    actions = {
        "list_assets": list_assets,
        "get_asset": get_asset_detail,
        "search_assets": search_assets,
        "analyze_asset": analyze_asset,
        "get_job_status": get_job_status,
        "get_preview_url": get_preview_url,
        "get_download_url": get_download_url,
        "get_ontology": get_ontology,
        "list_collections": list_collections,
        "get_collection": get_collection,
    }
    action = _gateway_action(context)
    if action not in actions:
        raise ValueError(f"unknown media-catalog tool: {action}")
    return actions[action](event)
```

## Pattern 3 — `lambdas/control/schema.json`

```json
[
  {
    "name": "create_upload_session",
    "description": "Register a video asset and return a one-hour presigned S3 PUT contract. Video bytes never pass through MCP or Lambda.\n\nUse for queries like: \"upload a video\", \"register new footage\", \"영상 업로드 준비해줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "file_name": { "type": "string", "description": "Plain name ending in mp4, mov, mkv, or webm" },
        "file_size": { "type": "integer", "minimum": 1, "maximum": 6000000000 },
        "duration_seconds": { "type": "number", "exclusiveMinimum": 0, "maximum": 14400 },
        "content_type": { "type": "string", "enum": ["video/mp4", "video/quicktime", "video/x-matroska", "video/webm"] },
        "sha256": { "type": "string", "description": "Optional lowercase SHA-256 hex digest, signed as upload metadata" },
        "rights_state": { "type": "string", "enum": ["OWNED", "LICENSED", "RESTRICTED"], "description": "Human-supplied rights state; models never alter it" },
        "idempotency_key": { "type": "string", "minLength": 8, "maxLength": 128 }
      },
      "required": ["file_name", "file_size", "duration_seconds", "rights_state", "idempotency_key"]
    }
  },
  {
    "name": "complete_upload",
    "description": "Verify an uploaded object's size and signed metadata, then idempotently start ingestion. It is normally optional because S3 ObjectCreated dispatch starts the same workflow.\n\nUse for queries like: \"finish my upload\", \"start processing this video\", \"업로드 완료 처리해줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "idempotency_key": { "type": "string", "minLength": 8, "maxLength": 128 }
      },
      "required": ["asset_id", "idempotency_key"]
    }
  },
  {
    "name": "request_enrichment",
    "description": "Idempotently regenerate embeddings and video-understanding metadata for an existing asset. This can incur substantial model and indexing cost.\n\nUse for queries like: \"reprocess this video\", \"refresh the AI tags\", \"임베딩을 다시 생성해줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "idempotency_key": { "type": "string", "minLength": 8, "maxLength": 128 }
      },
      "required": ["asset_id", "idempotency_key"]
    }
  },
  {
    "name": "render_highlight",
    "description": "Create a non-destructive MP4 derivative from sorted, non-overlapping time ranges using AWS Elemental MediaConvert. The original source is never modified.\n\nUse for queries like: \"make a highlight reel\", \"trim these moments into one video\", \"이 구간들로 하이라이트 만들어줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "clips": {
          "type": "array",
          "minItems": 1,
          "maxItems": 150,
          "items": {
            "type": "object",
            "properties": {
              "start_sec": { "type": "number", "minimum": 0 },
              "end_sec": { "type": "number", "exclusiveMinimum": 0 }
            },
            "required": ["start_sec", "end_sec"]
          }
        },
        "idempotency_key": { "type": "string", "minLength": 8, "maxLength": 128 }
      },
      "required": ["asset_id", "clips", "idempotency_key"]
    }
  },
  {
    "name": "request_archive",
    "description": "Tag the original source for lifecycle archival only after explicit human approval. Cold storage can delay later high-resolution edits.\n\nUse for queries like: \"archive this source\", \"move this footage to cold storage\", \"이 영상을 아카이브해줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "confirm_archive": { "type": "boolean", "description": "Must be true only after explicit human approval" }
      },
      "required": ["asset_id", "confirm_archive"]
    }
  },
  {
    "name": "create_collection",
    "description": "Save a named, reusable collection of cited clips. It is metadata only: no rendering and no source changes.\n\nUse for queries like: \"save these search results\", \"make a collection for my reel plan\", \"이 클립들을 모음으로 저장해줘\".",
    "inputSchema": {
      "type": "object",
      "properties": {
        "name": { "type": "string", "minLength": 1, "maxLength": 80 },
        "query": { "type": "string", "maxLength": 300, "description": "Originating search query retained as provenance" },
        "clips": {
          "type": "array",
          "minItems": 1,
          "maxItems": 100,
          "items": {
            "type": "object",
            "properties": {
              "asset_id": { "type": "string" },
              "start_sec": { "type": "number", "minimum": 0 },
              "end_sec": { "type": "number", "minimum": 0 },
              "source_id": { "type": "string", "maxLength": 160 },
              "note": { "type": "string", "maxLength": 300 }
            },
            "required": ["asset_id", "start_sec", "end_sec"]
          }
        },
        "idempotency_key": { "type": "string", "minLength": 8, "maxLength": 128 }
      },
      "required": ["name", "clips", "idempotency_key"]
    }
  }
]
```

## Pattern 4 — `lambdas/control/index.py`

```python
"""Write-side MCP tools for upload, enrichment, rendering, and archive requests."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from common import (
    canonical_hash,
    ddb_safe,
    get_asset,
    ingest_identity,
    now,
    put_job,
    s3,
    table,
    tenant_id,
    update_asset,
    validate_id,
)

MAX_VIDEO_BYTES = 6_000_000_000
MAX_VIDEO_SECONDS = 4 * 60 * 60
SUPPORTED_VIDEO_TYPES = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
}
RIGHTS_STATES = {"OWNED", "LICENSED", "RESTRICTED"}
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

_sfn_client: Any | None = None
_mediaconvert_client: Any | None = None


def _gateway_action(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw_name = str(custom.get("bedrockAgentCoreToolName", "")).strip()
    prefix = "media-commands___"
    # WHY: A target must never dispatch another target's same-named future tool.
    if raw_name.startswith(prefix):
        return raw_name[len(prefix) :]
    return raw_name.rsplit("___", 1)[-1]


def _sfn() -> Any:
    global _sfn_client
    if _sfn_client is None:
        _sfn_client = boto3.client("stepfunctions")
    return _sfn_client


def _mediaconvert() -> Any:
    global _mediaconvert_client
    if _mediaconvert_client is None:
        _mediaconvert_client = boto3.client("mediaconvert")
    return _mediaconvert_client


def _validate_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not IDEMPOTENCY_PATTERN.fullmatch(key):
        raise ValueError("idempotency_key must be 8-128 safe characters")
    return key


def _video_type(file_name: str, content_type: str | None) -> tuple[str, str]:
    safe_name = os.path.basename(str(file_name).strip())
    if not safe_name or safe_name != str(file_name).strip() or "." not in safe_name:
        raise ValueError("file_name must be a plain video file name")
    extension = safe_name.rsplit(".", 1)[-1].lower()
    if extension not in SUPPORTED_VIDEO_TYPES:
        raise ValueError("supported video extensions: mp4, mov, mkv, webm")
    expected = SUPPORTED_VIDEO_TYPES[extension]
    if content_type and content_type != expected:
        raise ValueError(f"content_type must be {expected} for .{extension}")
    return extension, expected


def _idempotency_claim(key: str, payload: dict[str, Any], result_id: str) -> bool:
    payload_hash = canonical_hash(payload)
    item_key = {"PK": f"TENANT#{tenant_id()}", "SK": f"IDEMPOTENCY#{key}"}
    try:
        table().put_item(
            Item={**item_key, "payload_hash": payload_hash, "result_id": result_id, "created_at": now()},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    existing = table().get_item(Key=item_key).get("Item", {})
    # WHY: Reusing a key for altered input must fail rather than accidentally bind a
    # new action to the first action's result.
    if existing.get("payload_hash") != payload_hash:
        raise ValueError("IDEMPOTENCY_CONFLICT: key was already used with different input")
    return False


def _upload_contract(asset: dict[str, Any]) -> dict[str, Any]:
    metadata = {"asset-id": asset["asset_id"]}
    params: dict[str, Any] = {
        "Bucket": os.environ["MEDIA_BUCKET"],
        "Key": asset["object_key"],
        "ContentType": asset["content_type"],
        "Metadata": metadata,
    }
    headers = {"Content-Type": asset["content_type"], "x-amz-meta-asset-id": asset["asset_id"]}
    if asset.get("sha256"):
        metadata["sha256"] = asset["sha256"]
        checksum = base64.b64encode(bytes.fromhex(asset["sha256"])).decode()
        params["ChecksumSHA256"] = checksum
        headers["x-amz-meta-sha256"] = asset["sha256"]
        headers["x-amz-checksum-sha256"] = checksum
    url = s3().generate_presigned_url("put_object", Params=params, ExpiresIn=3600)
    return {"method": "PUT", "url": url, "headers": headers, "expires_in_seconds": 3600}


def create_upload_session(event: dict[str, Any]) -> dict[str, Any]:
    key = _validate_idempotency_key(event.get("idempotency_key"))
    file_name = str(event.get("file_name", "")).strip()
    extension, content_type = _video_type(file_name, event.get("content_type"))
    file_size = int(event.get("file_size", 0))
    duration_seconds = float(event.get("duration_seconds", 0))
    rights_state = str(event.get("rights_state", "")).upper()
    sha256 = str(event.get("sha256", "")).lower().strip()
    if file_size <= 0 or file_size > MAX_VIDEO_BYTES:
        raise ValueError("file_size must be between 1 byte and 6 GB")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0 or duration_seconds > MAX_VIDEO_SECONDS:
        raise ValueError("duration_seconds must be greater than 0 and at most 4 hours")
    if rights_state not in RIGHTS_STATES:
        raise ValueError("rights_state must be OWNED, LICENSED, or RESTRICTED")
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sha256 must be a 64-character lowercase hexadecimal digest")

    # WHY: The idempotency key—not the mutable file name—creates stable identities
    # across retries, while the generated object key prevents path traversal/collision.
    asset_id = uuid.uuid5(uuid.NAMESPACE_URL, f"media-archive:{tenant_id()}:{key}").hex[:26]
    object_key = f"raw/{tenant_id()}/{asset_id}/v1/{asset_id}.{extension}"
    payload = {
        "file_name": file_name,
        "file_size": file_size,
        "duration_seconds": duration_seconds,
        "content_type": content_type,
        "rights_state": rights_state,
        "sha256": sha256,
    }
    created = _idempotency_claim(key, payload, asset_id)
    timestamp = now()
    asset = {
        "PK": f"TENANT#{tenant_id()}",
        "SK": f"ASSET#{asset_id}",
        "GSI1PK": f"TENANT#{tenant_id()}#ASSETS",
        "GSI1SK": timestamp,
        "asset_id": asset_id,
        "version": 1,
        "file_name": file_name,
        "file_size": file_size,
        "duration_seconds": duration_seconds,
        "content_type": content_type,
        "rights_state": rights_state,
        "sha256": sha256,
        "object_key": object_key,
        "status": "AWAITING_UPLOAD",
        "storage_state": "HOT",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if created:
        table().put_item(Item=ddb_safe(asset), ConditionExpression="attribute_not_exists(PK)")
    else:
        try:
            asset = get_asset(asset_id)
        except ValueError:
            table().put_item(Item=ddb_safe(asset), ConditionExpression="attribute_not_exists(PK)")

    return {
        "asset_id": asset_id,
        "version": 1,
        "status": asset.get("status", "AWAITING_UPLOAD"),
        "upload": _upload_contract(asset),
        "next_step": "Upload with the exact headers, then call complete_upload or wait for S3 dispatch.",
    }


def _start_workflow(asset_id: str, idempotency_key: str, reason: str) -> dict[str, Any]:
    asset = get_asset(asset_id)
    head = s3().head_object(
        Bucket=os.environ["MEDIA_BUCKET"], Key=asset["object_key"], ChecksumMode="ENABLED"
    )
    if int(head.get("ContentLength", 0)) != int(asset["file_size"]):
        raise ValueError("uploaded object size does not match registration")
    metadata = head.get("Metadata", {})
    if metadata.get("asset-id") != asset_id:
        raise ValueError("uploaded object metadata does not match asset_id")
    if asset.get("sha256"):
        expected_checksum = base64.b64encode(bytes.fromhex(asset["sha256"])).decode()
        if metadata.get("sha256") != asset["sha256"] or head.get("ChecksumSHA256") != expected_checksum:
            raise ValueError("S3 checksum does not match registered sha256")

    generation = "upload" if reason == "upload" else f"{reason}:{idempotency_key}"
    job_id, execution_name = ingest_identity(
        asset_id,
        etag=str(head.get("ETag", "")),
        version_id=str(head.get("VersionId", "")),
        generation=generation,
    )
    created = _idempotency_claim(idempotency_key, {"asset_id": asset_id, "reason": reason}, job_id)
    put_job(job_id, asset_id, "INGEST")
    if created:
        try:
            response = _sfn().start_execution(
                stateMachineArn=os.environ["STATE_MACHINE_ARN"],
                name=execution_name,
                input=json.dumps(
                    {"tenant_id": tenant_id(), "asset_id": asset_id, "job_id": job_id, "reason": reason}
                ),
            )
            execution_arn = response["executionArn"]
        except ClientError as error:
            # WHY: S3 and MCP completion can race. A deterministic execution identity
            # means an already-running workflow is success, not a duplicate failure.
            if error.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise
            execution_arn = "already-started"
        update_asset(asset_id, status="QUEUED", latest_job_id=job_id)
    else:
        execution_arn = "already-started"
    return {"asset_id": asset_id, "job_id": job_id, "status": "QUEUED", "execution_arn": execution_arn}


def complete_upload(event: dict[str, Any]) -> dict[str, Any]:
    return _start_workflow(
        validate_id(str(event.get("asset_id", "")), "asset_id"),
        _validate_idempotency_key(event.get("idempotency_key")),
        "upload",
    )


def request_enrichment(event: dict[str, Any]) -> dict[str, Any]:
    return _start_workflow(
        validate_id(str(event.get("asset_id", "")), "asset_id"),
        _validate_idempotency_key(event.get("idempotency_key")),
        "reprocess",
    )


def _timecode(seconds: float, fps: int = 30) -> str:
    total_frames = max(0, round(seconds * fps))
    frames = total_frames % fps
    total_seconds = total_frames // fps
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


def _validated_clips(value: Any, duration: float) -> list[dict[str, float]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 150:
        raise ValueError("clips must contain 1-150 time ranges")
    clips: list[dict[str, float]] = []
    previous_end = -1.0
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each clip must be an object")
        start = float(item.get("start_sec", -1))
        end = float(item.get("end_sec", -1))
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration:
            raise ValueError("each clip must satisfy 0 <= start_sec < end_sec <= duration")
        # WHY: MediaConvert InputClippings concatenates in input order; prohibit
        # overlap instead of silently creating duplicate or contradictory moments.
        if start < previous_end:
            raise ValueError("clips must be sorted and non-overlapping")
        clips.append({"start_sec": start, "end_sec": end})
        previous_end = end
    return clips


def render_highlight(event: dict[str, Any]) -> dict[str, Any]:
    asset_id = validate_id(str(event.get("asset_id", "")), "asset_id")
    key = _validate_idempotency_key(event.get("idempotency_key"))
    asset = get_asset(asset_id)
    if str(asset.get("rights_state", "")).upper() == "RESTRICTED":
        raise ValueError("rights policy forbids rendering this asset")
    clips = _validated_clips(event.get("clips"), float(asset["duration_seconds"]))
    payload = {"asset_id": asset_id, "clips": clips, "profile": "mp4-1080p-qvbr"}
    render_id = canonical_hash(payload)[:26]
    created = _idempotency_claim(key, payload, render_id)
    job_id = f"render-{render_id[:20]}"
    put_job(job_id, asset_id, "RENDER")
    destination = f"s3://{os.environ['MEDIA_BUCKET']}/derivatives/{tenant_id()}/{asset_id}/{render_id}/"
    existing_job = table().get_item(
        Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{job_id}"}
    ).get("Item", {})
    provider_job_id = existing_job.get("provider_job_id")

    if created or not provider_job_id:
        response = _mediaconvert().create_job(
            Role=os.environ["MEDIACONVERT_ROLE_ARN"],
            Queue=os.environ["MEDIACONVERT_QUEUE_ARN"],
            UserMetadata={"tenant": tenant_id(), "asset_id": asset_id, "render_id": render_id},
            Settings={
                "TimecodeConfig": {"Source": "ZEROBASED"},
                "Inputs": [
                    {
                        "FileInput": f"s3://{os.environ['MEDIA_BUCKET']}/{asset['object_key']}",
                        "TimecodeSource": "ZEROBASED",
                        "InputClippings": [
                            {"StartTimecode": _timecode(clip["start_sec"]), "EndTimecode": _timecode(clip["end_sec"])}
                            for clip in clips
                        ],
                        "AudioSelectors": {"Default Audio": {"DefaultSelection": "DEFAULT"}},
                        "VideoSelector": {},
                    }
                ],
                "OutputGroups": [
                    {
                        "Name": "MP4 derivatives",
                        "OutputGroupSettings": {
                            "Type": "FILE_GROUP_SETTINGS",
                            "FileGroupSettings": {"Destination": destination},
                        },
                        "Outputs": [
                            {
                                "NameModifier": f"-{render_id}",
                                "ContainerSettings": {"Container": "MP4"},
                                "VideoDescription": {
                                    "CodecSettings": {
                                        "Codec": "H_264",
                                        "H264Settings": {
                                            "RateControlMode": "QVBR",
                                            "MaxBitrate": 5_000_000,
                                            "QvbrSettings": {"QvbrQualityLevel": 7},
                                        },
                                    }
                                },
                                "AudioDescriptions": [
                                    {
                                        "AudioSourceName": "Default Audio",
                                        "CodecSettings": {
                                            "Codec": "AAC",
                                            "AacSettings": {
                                                "Bitrate": 96_000,
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
            },
        )
        provider_job_id = response["Job"]["Id"]
        # WHY: Store the exact derivative prefix with the job so a later download
        # cannot infer a path from untrusted user input or a similarly named render.
        table().update_item(
            Key={"PK": f"TENANT#{tenant_id()}", "SK": f"JOB#{job_id}"},
            UpdateExpression="SET provider_job_id=:provider, derivative_prefix=:prefix, #status=:status, updated_at=:now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":provider": provider_job_id,
                ":prefix": destination.removeprefix(f"s3://{os.environ['MEDIA_BUCKET']}/"),
                ":status": "SUBMITTED",
                ":now": now(),
            },
        )
    else:
        provider_job_id = existing_job.get("provider_job_id", "already-submitted")

    return {
        "asset_id": asset_id,
        "render_id": render_id,
        "job_id": job_id,
        "provider_job_id": provider_job_id,
        "status": "SUBMITTED",
    }


def request_archive(event: dict[str, Any]) -> dict[str, Any]:
    asset_id = validate_id(str(event.get("asset_id", "")), "asset_id")
    if event.get("confirm_archive") is not True:
        raise ValueError("confirm_archive=true is required after explicit human approval")
    asset = get_asset(asset_id)
    existing = s3().get_object_tagging(
        Bucket=os.environ["MEDIA_BUCKET"], Key=asset["object_key"]
    ).get("TagSet", [])
    tags = {item["Key"]: item["Value"] for item in existing}
    # WHY: The command records intent only. Lifecycle policy—not a tool call—moves
    # source data, preserving the safety window and avoiding immediate deletion.
    tags["archive"] = "true"
    s3().put_object_tagging(
        Bucket=os.environ["MEDIA_BUCKET"],
        Key=asset["object_key"],
        Tagging={"TagSet": [{"Key": name, "Value": value} for name, value in tags.items()]},
    )
    update_asset(asset_id, storage_state="ARCHIVE_REQUESTED", archive_requested_at=now())
    return {
        "asset_id": asset_id,
        "status": "ARCHIVE_REQUESTED",
        "note": "The S3 lifecycle policy transitions the tagged source after its configured delay.",
    }


def create_collection(event: dict[str, Any]) -> dict[str, Any]:
    name = str(event.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("name must be 1-80 characters")
    key = _validate_idempotency_key(event.get("idempotency_key"))
    raw_clips = event.get("clips")
    if not isinstance(raw_clips, list) or not 1 <= len(raw_clips) <= 100:
        raise ValueError("clips must contain 1-100 entries")

    assets: dict[str, dict[str, Any]] = {}
    clips: list[dict[str, Any]] = []
    for item in raw_clips:
        if not isinstance(item, dict):
            raise ValueError("each clip must be an object")
        asset_id = validate_id(str(item.get("asset_id", "")), "asset_id")
        if asset_id not in assets:
            assets[asset_id] = get_asset(asset_id)
        duration = float(assets[asset_id].get("duration_seconds", 0))
        start = float(item.get("start_sec", -1))
        end = float(item.get("end_sec", -1))
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
            raise ValueError("each clip must satisfy 0 <= start_sec < end_sec <= duration")
        source_id = str(item.get("source_id", "")).strip()[:160]
        # WHY: Even manually selected clips need stable evidence provenance. A
        # deterministic manual source retains the assertion without pretending it
        # came from a vector retrieval result.
        if not source_id:
            source_id = f"manual:{asset_id}:{start:.3f}:{end:.3f}"
        clips.append(
            {
                "asset_id": asset_id,
                "start_sec": start,
                "end_sec": end,
                "source_id": source_id,
                "note": str(item.get("note", ""))[:300],
            }
        )

    payload = {"name": name, "query": str(event.get("query", ""))[:300], "clips": clips}
    collection_id = canonical_hash(payload)[:26]
    created = _idempotency_claim(key, payload, collection_id)
    timestamp = now()
    if created:
        try:
            table().put_item(
                Item=ddb_safe(
                    {
                        "PK": f"TENANT#{tenant_id()}",
                        "SK": f"COLLECTION#{collection_id}",
                        "GSI1PK": f"TENANT#{tenant_id()}#COLLECTIONS",
                        "GSI1SK": timestamp,
                        "collection_id": collection_id,
                        "name": name,
                        "query": payload["query"],
                        "clips": clips,
                        "clip_count": len(clips),
                        "created_at": timestamp,
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return {"collection_id": collection_id, "name": name, "clip_count": len(clips), "created": created}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    actions = {
        "create_upload_session": create_upload_session,
        "complete_upload": complete_upload,
        "request_enrichment": request_enrichment,
        "render_highlight": render_highlight,
        "request_archive": request_archive,
        "create_collection": create_collection,
    }
    action = _gateway_action(context)
    if action not in actions:
        raise ValueError(f"unknown media-commands tool: {action}")
    return actions[action](event)
```

## Pattern 5 — Gateway wiring excerpt

This is intentionally under 40 lines. AgentCore Gateway exposes each schema tool as `<gatewayTargetName>___<schemaToolName>`, for example `media-catalog___search_assets` and `media-commands___render_highlight`.

```ts
const gateway = new agentcore.Gateway(this, 'MediaArchiveGateway', {
  gatewayName: 'media-archive',
  description: 'Media archive retrieval, analysis, rendering, and download tools',
  authorizerConfiguration: agentcore.GatewayAuthorizer.usingCognito({
    userPool,
    allowedClients: [client],
  }),
  protocolConfiguration: new agentcore.McpProtocolConfiguration({
    instructions: [
      'Preserve source video and create only non-destructive derivatives.',
      'Never infer or change rights from model output.',
      'Cite asset_id, source_id, start_sec, and end_sec for search findings.',
      'Require explicit human approval before archival.',
    ].join(' '),
    searchType: agentcore.McpGatewaySearchType.SEMANTIC,
    supportedVersions: [agentcore.MCPProtocolVersion.MCP_2025_06_18],
  }),
});

const catalogTarget = gateway.addLambdaTarget('CatalogTarget', {
  gatewayTargetName: 'media-catalog',
  description: 'Read-only media search, analysis, ontology, status, and URL tools',
  lambdaFunction: catalogFunction,
  toolSchema: agentcore.ToolSchema.fromLocalAsset(
    path.resolve(process.cwd(), 'lambdas/catalog/schema.json'),
  ),
});
catalogFunction.grantInvoke(gateway.role);
catalogTarget.node.addDependency(gateway.role);

const commandsTarget = gateway.addLambdaTarget('CommandsTarget', {
  gatewayTargetName: 'media-commands',
  description: 'Upload, re-enrichment, rendering, collections, and approved archive commands',
  lambdaFunction: controlFunction,
  toolSchema: agentcore.ToolSchema.fromLocalAsset(
    path.resolve(process.cwd(), 'lambdas/control/schema.json'),
  ),
});
controlFunction.grantInvoke(gateway.role);
commandsTarget.node.addDependency(gateway.role);
```

## Pattern 6 — tool-description quality rules for semantic search

1. Begin with a direct verb and domain noun: “Search source-timestamped media clips,” not “Search.”
2. Include a separate `Use for queries like:` line in every schema description; list at least three natural user phrasings, including Korean examples when the skill supports Korean requests.
3. Describe the returned evidence and the safe next action. For search, name `asset_id`, `source_id`, `start_sec`, `end_sec`, modality, score, and confidence; tell the model to preview medium confidence and abstain on low confidence.
4. State boundaries in user language: whole-asset questions versus timestamp retrieval, rendering versus collection saving, archive request versus immediate deletion, source download versus preview.
5. Include preconditions and irreversible impact in descriptions that can cause cost, data movement, or user-visible change. Examples: model re-enrichment cost, literal archive confirmation, immutable source preservation, and short URL lifetimes.
6. Use input descriptions to improve matching: `query` is an action + subject + context request, `start_sec` is source-relative, and `rendition` has only `source` or `render:<render_id>`.
7. Avoid implementation-only vocabulary as the sole description. “Get vector results” is weak; “find clips of a person entering a building” gives Gateway the semantic signals it needs.
8. Keep names stable after publishing. Add a new versioned schema/tool only with a compatibility path; existing clients and conversations may retain old names.

## Pattern 7 — unit test excerpts

`tests/test_catalog.py`:

```python
import pytest
from catalog import index


def _asset(**overrides):
    value = {
        "asset_id": "asset-1",
        "object_key": "raw/pilot/asset-1/v1/source.mp4",
        "sha256": "a" * 64,
        "file_name": "source.mp4",
        "content_type": "video/mp4",
        "file_size": 1000,
        "duration_seconds": 30,
        "rights_state": "OWNED",
        "status": "READY",
        "storage_state": "HOT",
        "analysis": {"summary": "A mountain hike", "topics": ["hiking"]},
    }
    value.update(overrides)
    return value


def test_public_asset_hides_storage_capabilities():
    result = index._public_asset(_asset())
    assert result["asset_id"] == "asset-1"
    assert "object_key" not in result
    assert "sha256" not in result


def test_download_rejects_restricted_asset(monkeypatch):
    monkeypatch.setattr(index, "_current_asset", lambda _asset_id: _asset(rights_state="RESTRICTED"))
    with pytest.raises(ValueError, match="forbids downloading"):
        index.get_download_url({"asset_id": "asset-1"})


def test_search_confidence_bands():
    assert index._search_confidence(0.85) == "high"
    assert index._search_confidence(0.77) == "medium"
    assert index._search_confidence(0.71) == "low"
```

`tests/test_control.py`:

```python
import pytest
from control import index


def test_clip_validation_rejects_overlap():
    with pytest.raises(ValueError, match="non-overlapping"):
        index._validated_clips(
            [{"start_sec": 0, "end_sec": 5}, {"start_sec": 4, "end_sec": 8}],
            10,
        )


def test_archive_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="explicit human approval"):
        index.request_archive({"asset_id": "asset-1", "confirm_archive": False})
```

## Cross-layer mapping — `render_highlight`

| Layer | Contract and handoff | Guardrail |
|---|---|---|
| `lambdas/control/schema.json` | Requires `asset_id`, 1–150 `clips`, and an idempotency key. | Schema guides semantic selection; handler enforces ordering and asset duration. |
| `lambdas/control/index.py` | `render_highlight` validates a non-restricted asset, finite ordered non-overlapping ranges, and deterministic `render_id`. | It never writes to `raw/`; source edits are not an operation. |
| MediaConvert | A `create_job` request reads the source and uses zero-based 30-fps clippings, H.264 QVBR (quality 7, 5 Mbps maximum), and AAC. | Output is restricted to `derivatives/{tenant}/{asset_id}/{render_id}/`. |
| DynamoDB job item | `put_job` creates `JOB#render-…`; the handler stores the MediaConvert provider ID and exact derivative prefix. | A later request cannot derive an arbitrary path from caller input. |
| `media-catalog___get_job_status` | Reads the job and maps MediaConvert provider state to archive state. | The caller sees status, not provider credentials or storage internals. |
| `media-catalog___get_download_url` | Accepts `render:<render_id>`, verifies that the job belongs to the asset, re-checks rights, and signs only an existing MP4. | URL lifetime is bounded to 60–900 seconds and must not be persisted or reposted. |
