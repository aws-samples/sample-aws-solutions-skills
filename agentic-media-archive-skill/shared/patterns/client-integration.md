# Client Integration

> A local stdio bridge turns a service-to-service OAuth-protected Media Archive Gateway into safe, ergonomic MCP tools for ChatGPT/Codex, Kiro, Claude Code, and Amazon Quick. The bridge owns token refresh, secret retrieval, tool-prefix normalization, local-file safety, and MCP Apps delivery; clients never contain an OAuth client secret or a bearer playback URL.

## File layout

```text
<project-root>/
├── local_bridge/
│   ├── __init__.py
│   ├── remote_client.py                 ← OAuth, Gateway transport, direct S3 upload
│   ├── server.py                        ← stdio MCP facade and MCP Apps resource
│   └── ui/
│       └── media-results.html           ← cited carousel, player, and source timeline
├── clients/
│   ├── codex-plugin/
│   │   ├── .codex-plugin/plugin.json
│   │   ├── .mcp.json.template           ← rendered to .mcp.json by scripts/deploy.sh
│   │   └── skills/media-archive/SKILL.md
│   ├── kiro/
│   │   ├── .kiro/settings/mcp.json.template
│   │   └── .kiro/skills/media-archive-operations/SKILL.md
│   ├── claude/
│   │   ├── .mcp.json.template
│   │   └── skills/media-archive-operations/SKILL.md
│   └── quick/
│       ├── README.md
│       └── service-oauth.json.template
├── scripts/
│   └── deploy.sh                         ← deploys and renders client configs from outputs
└── tests/
    └── test_local_bridge.py
```

The Gateway exposes these fixed remote operations: the `media-catalog` target provides `list_assets`, `get_asset`, `search_assets`, `analyze_asset`, `get_job_status`, `get_preview_url`, `get_download_url`, `get_ontology`, `list_collections`, and `get_collection`; the `media-commands` target provides `create_upload_session`, `complete_upload`, `request_enrichment`, `render_highlight`, `request_archive`, and `create_collection`. The standard local bridge deliberately presents a curated 14-tool surface: `upload_local_video`, `list_assets`, `get_asset`, `search_assets`, `render_media_results`, `resolve_media_playback`, `analyze_asset`, `get_job_status`, `render_highlight`, `get_download_url`, `create_collection`, `list_collections`, `get_collection`, and `get_ontology`. Raw upload steps are collapsed into `upload_local_video`, and preview resolution is UI-only so bearer URLs cannot leak into model-visible content.

## Pattern 1 — `local_bridge/remote_client.py`

```python
"""OAuth-authenticated Streamable HTTP client for Media Archive Gateway."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import boto3
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class MediaArchiveMcpError(RuntimeError):
    """Raised when an authenticated Media Archive MCP request fails."""


class RemoteMediaArchiveClient:
    """One process-local OAuth cache and Streamable HTTP connection factory."""

    def __init__(
        self,
        *,
        gateway_url: str,
        token_url: str,
        user_pool_id: str,
        client_id: str,
        aws_profile: str,
        aws_region: str,
        scope: str = "media-archive/read media-archive/write",
        client_secret_arn: str = "",
    ) -> None:
        self.gateway_url = gateway_url
        self.token_url = token_url
        self.user_pool_id = user_pool_id
        self.client_id = client_id
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.scope = scope
        self.client_secret_arn = client_secret_arn
        self._client_secret = ""
        self._access_token = ""
        self._expires_at = 0.0
        self._tool_name_aliases: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> "RemoteMediaArchiveClient":
        required = {
            "MEDIA_ARCHIVE_GATEWAY_URL": os.environ.get("MEDIA_ARCHIVE_GATEWAY_URL", ""),
            "MEDIA_ARCHIVE_TOKEN_URL": os.environ.get("MEDIA_ARCHIVE_TOKEN_URL", ""),
            "MEDIA_ARCHIVE_USER_POOL_ID": os.environ.get("MEDIA_ARCHIVE_USER_POOL_ID", ""),
            "MEDIA_ARCHIVE_CLIENT_ID": os.environ.get("MEDIA_ARCHIVE_CLIENT_ID", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise MediaArchiveMcpError(
                f"missing bridge configuration: {', '.join(missing)}"
            )
        return cls(
            gateway_url=required["MEDIA_ARCHIVE_GATEWAY_URL"],
            token_url=required["MEDIA_ARCHIVE_TOKEN_URL"],
            user_pool_id=required["MEDIA_ARCHIVE_USER_POOL_ID"],
            client_id=required["MEDIA_ARCHIVE_CLIENT_ID"],
            # WHY: a profile selects least-privilege operator credentials without
            # placing an OAuth secret in a client configuration file.
            aws_profile=os.environ.get("AWS_PROFILE", "default"),
            aws_region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")),
            scope=os.environ.get(
                "MEDIA_ARCHIVE_SCOPE", "media-archive/read media-archive/write"
            ),
            # This is an ARN/reference, never a secret value. Omit it to read the
            # generated Cognito app-client secret at runtime instead.
            client_secret_arn=os.environ.get("MEDIA_ARCHIVE_CLIENT_SECRET_ARN", ""),
        )

    def _boto_session(self) -> boto3.Session:
        if not self.aws_region:
            raise MediaArchiveMcpError("AWS_REGION or AWS_DEFAULT_REGION is required")
        return boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region)

    @staticmethod
    def _secret_from_payload(value: str) -> str:
        """Accept the two conventional JSON keys, but never log their value."""
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
        if not isinstance(payload, dict):
            raise MediaArchiveMcpError("Secrets Manager payload must be an object or secret string")
        secret = payload.get("client_secret") or payload.get("clientSecret")
        if not isinstance(secret, str) or not secret:
            raise MediaArchiveMcpError("secret reference does not contain client_secret")
        return secret

    def _secret(self) -> str:
        if self._client_secret:
            return self._client_secret

        session = self._boto_session()
        if self.client_secret_arn:
            response = session.client("secretsmanager").get_secret_value(
                SecretId=self.client_secret_arn
            )
            value = response.get("SecretString")
            if value is None:
                raw = response.get("SecretBinary")
                if isinstance(raw, bytes):
                    value = raw.decode("utf-8")
                elif isinstance(raw, str):
                    value = base64.b64decode(raw).decode("utf-8")
            if not isinstance(value, str) or not value:
                raise MediaArchiveMcpError("secret reference returned no usable value")
            self._client_secret = self._secret_from_payload(value)
            return self._client_secret

        # WHY: Cognito returns the app-client secret only to an AWS principal that
        # already has describe permission. This avoids a second secret distribution
        # channel for desktop MCP clients.
        response = session.client("cognito-idp").describe_user_pool_client(
            UserPoolId=self.user_pool_id,
            ClientId=self.client_id,
        )
        self._client_secret = str(
            response.get("UserPoolClient", {}).get("ClientSecret", "")
        )
        if not self._client_secret:
            raise MediaArchiveMcpError("OAuth app client has no client secret")
        return self._client_secret

    async def _token(self) -> str:
        # WHY: refresh before the final minute so a long Streamable HTTP request
        # cannot begin with a token that expires while the Gateway is authorizing it.
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        async with httpx.AsyncClient(timeout=30) as http_client:
            response = await http_client.post(
                self.token_url,
                data={"grant_type": "client_credentials", "scope": self.scope},
                auth=httpx.BasicAuth(self.client_id, self._secret()),
            )
            response.raise_for_status()
            payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MediaArchiveMcpError("OAuth response contained no access_token")
        self._access_token = token
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return token

    @staticmethod
    def _index_tool_names(remote_names: list[str]) -> tuple[dict[str, str], list[str]]:
        """Expose a bare tool name only when the Gateway suffix is unambiguous."""
        grouped: dict[str, list[str]] = {}
        for remote_name in remote_names:
            canonical = remote_name.rsplit("___", 1)[-1]
            grouped.setdefault(canonical, []).append(remote_name)

        aliases = {name: name for name in remote_names}
        exposed: list[str] = []
        for remote_name in remote_names:
            canonical = remote_name.rsplit("___", 1)[-1]
            if len(grouped[canonical]) == 1:
                aliases[canonical] = remote_name
                exposed.append(canonical)
            else:
                # WHY: silently choosing one target would route a command to the
                # wrong policy boundary when catalog and command tools share a name.
                exposed.append(remote_name)
        return aliases, exposed

    async def list_tools(self) -> list[str]:
        token = await self._token()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=300),
        ) as http_client:
            async with streamable_http_client(
                self.gateway_url, http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
        self._tool_name_aliases, exposed = self._index_tool_names(
            [tool.name for tool in result.tools]
        )
        return exposed

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        token = await self._token()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=300),
        ) as http_client:
            async with streamable_http_client(
                self.gateway_url, http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    remote_name = self._tool_name_aliases.get(name)
                    if remote_name is None:
                        result = await session.list_tools()
                        self._tool_name_aliases, exposed = self._index_tool_names(
                            [tool.name for tool in result.tools]
                        )
                        remote_name = self._tool_name_aliases.get(name)
                        if remote_name is None:
                            raise MediaArchiveMcpError(
                                f"remote tool {name!r} is unavailable; available={exposed}"
                            )
                    result = await session.call_tool(remote_name, arguments)

        if result.isError:
            text = "\n".join(
                getattr(item, "text", str(item)) for item in result.content
            )
            raise MediaArchiveMcpError(f"remote tool {name} failed: {text}")
        if result.structuredContent is not None:
            return result.structuredContent
        text_items = [getattr(item, "text", "") for item in result.content]
        if len(text_items) == 1:
            try:
                return json.loads(text_items[0])
            except json.JSONDecodeError:
                return text_items[0]
        return text_items

    @staticmethod
    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
        # WHY: do not load a multi-gigabyte source into the desktop client's memory.
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                yield chunk

    async def upload_file(
        self,
        *,
        path: Path,
        duration_seconds: float,
        rights_state: str,
    ) -> dict[str, Any]:
        digest = self.file_sha256(path)
        stat = path.stat()
        idempotency_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{digest}",
            )
        )
        registration = await self.call_tool(
            "create_upload_session",
            {
                "file_name": path.name,
                "file_size": stat.st_size,
                "duration_seconds": duration_seconds,
                "content_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
                "sha256": digest,
                "rights_state": rights_state,
                "idempotency_key": idempotency_key,
            },
        )
        if not isinstance(registration, dict) or not isinstance(registration.get("upload"), dict):
            raise MediaArchiveMcpError("upload registration did not return an upload target")
        upload = registration["upload"]
        headers = upload.get("headers")
        url = upload.get("url")
        if not isinstance(headers, dict) or not isinstance(url, str) or not url:
            raise MediaArchiveMcpError("upload registration is incomplete")

        # WHY: use the signed target directly; proxying bytes through the Gateway
        # adds cost, timeouts, and an avoidable data-exposure path.
        upload_headers = {**headers, "Content-Length": str(stat.st_size)}
        async with httpx.AsyncClient(timeout=None) as http_client:
            response = await http_client.put(
                url,
                headers=upload_headers,
                content=self._file_chunks(path),
            )
            response.raise_for_status()

        completion = await self.call_tool(
            "complete_upload",
            {
                "asset_id": registration["asset_id"],
                "idempotency_key": f"complete:{idempotency_key}",
            },
        )
        return {"registration": registration, "processing": completion}
```

Do not implement `_session` as a long-lived client session: the token and connection are intentionally scoped to one operation. This makes token refresh deterministic, keeps credential material in memory only, and recovers cleanly from a suspended desktop process.

## Pattern 2 — `local_bridge/server.py`

```python
"""Safe stdio MCP facade and MCP Apps resource for Media Archive operations."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from local_bridge.remote_client import RemoteMediaArchiveClient

for logger_name in ("httpx", "httpcore"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

MEDIA_RESULTS_UI_URI = "ui://media-archive/media-results.v1.html"
LEGACY_MEDIA_RESULTS_UI_URI = "ui://media-archive/media-results.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
UI_PATH = Path(__file__).with_name("ui") / "media-results.html"
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
)

mcp = FastMCP(
    "media-archive",
    instructions=(
        "Search, inspect, curate, render, and download governed media. Preserve originals, "
        "enforce rights, cite asset/time-range evidence, and use render_media_results when "
        "a visual result carousel helps. Cite high-confidence clips directly, preview medium "
        "confidence clips before citing, and report low-confidence clips as no confident match."
    ),
)
_client: RemoteMediaArchiveClient | None = None


def client() -> RemoteMediaArchiveClient:
    global _client
    if _client is None:
        _client = RemoteMediaArchiveClient.from_env()
    return _client


def allowed_local_video(file_path: str) -> Path:
    configured_root = os.environ.get("MEDIA_ARCHIVE_ALLOWED_ROOT", "").strip()
    if not configured_root:
        raise ValueError("MEDIA_ARCHIVE_ALLOWED_ROOT must be configured")
    root = Path(configured_root).expanduser().resolve()
    path = Path(file_path).expanduser().resolve(strict=True)
    # WHY: rejecting paths outside an explicit root prevents a desktop agent from
    # uploading unrelated local files simply because it can read their path.
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError(f"video must be under allowed root: {root}")
    if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise ValueError("supported local video extensions: mp4, mov, mkv, webm")
    return path


def _normalize_media_results(query: str, results: Any, guidance: str = "") -> dict[str, Any]:
    query = str(query).strip()
    if not query:
        raise ValueError("query is required")
    if not isinstance(results, list):
        raise ValueError("results must be a list")

    normalized: list[dict[str, Any]] = []
    for raw in results[:10]:
        if not isinstance(raw, dict):
            raise ValueError("each media result must be an object")
        asset_id = str(raw.get("asset_id", "")).strip()
        source_id = str(raw.get("source_id", "")).strip()
        start_sec = float(raw.get("start_sec", -1))
        end_sec = float(raw.get("end_sec", -1))
        if (
            not asset_id
            or not source_id
            or not math.isfinite(start_sec)
            or not math.isfinite(end_sec)
            or start_sec < 0
            or end_sec < start_sec
        ):
            raise ValueError("media results require valid asset/source IDs and time ranges")
        topics = raw.get("topics", [])
        if not isinstance(topics, list):
            topics = []
        options = raw.get("embedding_options", [])
        if not isinstance(options, list):
            options = []
        normalized.append(
            {
                "asset_id": asset_id,
                "source_id": source_id,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "modality": str(raw.get("modality") or "visual"),
                "score": float(raw.get("score") or 0),
                "confidence": str(raw.get("confidence") or "low").lower(),
                "summary": str(raw.get("summary") or ""),
                "topics": [str(value) for value in topics if str(value).strip()][:5],
                "rights_state": str(raw.get("rights_state") or "UNKNOWN"),
                "embedding_option": str(raw.get("embedding_option") or ""),
                "embedding_options": [str(value) for value in options if str(value).strip()],
            }
        )
    return {
        "query": query,
        "guidance": str(guidance),
        "results": normalized,
        "has_sources": bool(normalized),
    }


def _resource_meta() -> dict[str, Any]:
    origin = os.environ.get("MEDIA_ARCHIVE_PLAYBACK_ORIGIN", "").strip().rstrip("/")
    domains = [origin] if origin else []
    # WHY: resource metadata is the MCP Apps CSP integration point. An empty list
    # fails closed during local development; deployment supplies one playback origin.
    return {
        "ui": {
            "prefersBorder": True,
            "csp": {"connectDomains": domains, "resourceDomains": domains},
        },
        "openai/widgetDescription": "Interactive cited media search carousel and player.",
        "openai/widgetPrefersBorder": True,
        "openai/widgetCSP": {
            "connect_domains": domains,
            "resource_domains": domains,
        },
    }


@mcp.resource(
    MEDIA_RESULTS_UI_URI,
    name="media-results",
    title="Media search results",
    description="Interactive carousel and source-relative player for cited media clips.",
    mime_type=MCP_APP_MIME_TYPE,
    meta=_resource_meta(),
)
def media_results_app() -> str:
    return UI_PATH.read_text(encoding="utf-8")


# WHY: a URI can be cached by an existing conversation. When publishing v2 or later,
# retain every already-published URI as an alias that serves the current safe widget.
@mcp.resource(
    LEGACY_MEDIA_RESULTS_UI_URI,
    name="media-results-legacy",
    title="Media search results (legacy alias)",
    description="Backward-compatible alias for an earlier media-results resource URI.",
    mime_type=MCP_APP_MIME_TYPE,
    meta=_resource_meta(),
)
def media_results_app_legacy() -> str:
    return UI_PATH.read_text(encoding="utf-8")


@mcp.tool()
async def upload_local_video(
    file_path: str,
    duration_seconds: float,
    rights_state: str = "OWNED",
) -> dict[str, Any]:
    """Hash and stream an approved local video directly to its signed upload target."""
    return await client().upload_file(
        path=allowed_local_video(file_path),
        duration_seconds=duration_seconds,
        rights_state=rights_state,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_assets(limit: int = 25) -> Any:
    """List recently registered media assets."""
    return await client().call_tool("list_assets", {"limit": limit})


@mcp.tool(annotations=READ_ONLY)
async def get_asset(asset_id: str) -> Any:
    """Get safe metadata and generated analysis for one media asset."""
    return await client().call_tool("get_asset", {"asset_id": asset_id})


@mcp.tool(annotations=READ_ONLY)
async def search_assets(query: str, limit: int = 10, rights_state: str = "") -> Any:
    """Find source-time media clips using an intent-rich natural-language query."""
    arguments: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 25))}
    if rights_state:
        arguments["rights_state"] = rights_state
    return await client().call_tool("search_assets", arguments)


@mcp.tool(
    title="Show media results",
    description="Search the archive and render an interactive cited media carousel.",
    annotations=READ_ONLY,
    meta={
        "ui": {"resourceUri": MEDIA_RESULTS_UI_URI, "visibility": ["model", "app"]},
        "openai/outputTemplate": MEDIA_RESULTS_UI_URI,
        "openai/toolInvocation/invoking": "Searching the media archive…",
        "openai/toolInvocation/invoked": "Media results ready.",
    },
)
async def render_media_results(
    query: str,
    limit: int = 6,
    rights_state: str = "",
) -> CallToolResult:
    """Search and return a structured fallback plus the MCP Apps resource binding."""
    arguments: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 10))}
    if rights_state:
        arguments["rights_state"] = rights_state
    remote = await client().call_tool("search_assets", arguments)
    if not isinstance(remote, dict):
        raise ValueError("search_assets returned an invalid response")
    payload = _normalize_media_results(
        query,
        remote.get("results", []),
        str(remote.get("guidance") or ""),
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Showing {len(payload['results'])} cited media clip result(s).",
            )
        ],
        structuredContent=payload,
    )


@mcp.tool(
    title="Resolve media playback",
    description="Prepare a rights-checked, short-lived analysis-proxy playback session.",
    annotations=READ_ONLY,
    meta={"ui": {"visibility": ["app"]}},
)
async def resolve_media_playback(
    asset_id: str,
    start_sec: float,
    expires_seconds: int = 180,
) -> CallToolResult:
    preview = await client().call_tool(
        "get_preview_url",
        {
            "asset_id": asset_id,
            "start_sec": start_sec,
            "expires_seconds": max(60, min(expires_seconds, 300)),
        },
    )
    if not isinstance(preview, dict):
        raise ValueError("get_preview_url returned an invalid response")
    playback_url = str(preview.pop("url", ""))
    if not playback_url:
        raise ValueError("preview response did not contain a playback URL")
    asset = await client().call_tool("get_asset", {"asset_id": asset_id})
    if not isinstance(asset, dict):
        raise ValueError("get_asset returned an invalid response")

    structured = {
        **preview,
        "file_name": asset.get("file_name"),
        "duration_seconds": asset.get("duration_seconds"),
    }
    # WHY: a presigned URL is a bearer credential. Only an app can receive _meta;
    # it must not appear in text, structuredContent, widget state, or model context.
    return CallToolResult(
        content=[TextContent(type="text", text="Playback session prepared.")],
        structuredContent=structured,
        _meta={
            "ui": {"visibility": ["app"]},
            "mediaArchive": {"playbackUrl": playback_url},
        },
    )


@mcp.tool(annotations=READ_ONLY)
async def analyze_asset(asset_id: str, question: str) -> Any:
    """Ask an asset-level question; do not present the response as clip evidence."""
    return await client().call_tool(
        "analyze_asset", {"asset_id": asset_id, "question": question}
    )


@mcp.tool(annotations=READ_ONLY)
async def get_job_status(job_id: str) -> Any:
    """Get ingest, enrichment, index, or render status."""
    return await client().call_tool("get_job_status", {"job_id": job_id})


@mcp.tool()
async def render_highlight(
    asset_id: str,
    clips: list[dict[str, float]],
    idempotency_key: str,
) -> Any:
    """Render sorted, non-overlapping source ranges as a new derivative only."""
    return await client().call_tool(
        "render_highlight",
        {"asset_id": asset_id, "clips": clips, "idempotency_key": idempotency_key},
    )


@mcp.tool()
async def get_download_url(
    asset_id: str,
    rendition: str = "source",
    expires_seconds: int = 300,
) -> Any:
    """Get a short-lived, rights-checked download URL for an allowed rendition."""
    return await client().call_tool(
        "get_download_url",
        {
            "asset_id": asset_id,
            "rendition": rendition,
            "expires_seconds": max(60, min(expires_seconds, 900)),
        },
    )


@mcp.tool()
async def create_collection(
    name: str,
    clips: list[dict[str, Any]],
    idempotency_key: str,
    query: str = "",
) -> Any:
    """Save cited clips as a named metadata collection without rendering media."""
    return await client().call_tool(
        "create_collection",
        {
            "name": name,
            "clips": clips,
            "idempotency_key": idempotency_key,
            "query": query,
        },
    )


@mcp.tool(annotations=READ_ONLY)
async def list_collections(limit: int = 25) -> Any:
    """List saved clip collections, newest first."""
    return await client().call_tool("list_collections", {"limit": limit})


@mcp.tool(annotations=READ_ONLY)
async def get_collection(collection_id: str) -> Any:
    """Read one collection and its cited source-time clips."""
    return await client().call_tool("get_collection", {"collection_id": collection_id})


@mcp.tool(annotations=READ_ONLY)
async def get_ontology(concept_id: str = "", label: str = "", limit: int = 50) -> Any:
    """Read model-suggested ontology candidates and provenance."""
    arguments: dict[str, Any] = {"limit": max(1, min(limit, 100))}
    if concept_id:
        arguments["concept_id"] = concept_id
    if label:
        arguments["label"] = label
    return await client().call_tool("get_ontology", arguments)


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

The 16 remote operation names stay fixed even when a product chooses this 14-tool bridge surface. If a customer needs direct remote administration such as `request_archive`, add it as a separately approved, narrow-purpose bridge tool; it must require `confirm_archive=true` and must not be made available through a read-oriented plugin by default.

## Pattern 3 — `local_bridge/ui/media-results.html`

The server resource metadata is the authoritative CSP declaration. `MEDIA_ARCHIVE_PLAYBACK_ORIGIN` is emitted from deployment outputs and is the only `connectDomains`/`resourceDomains` value; do not add a wildcard CSP tag or a second unbounded client-side network path. The widget receives bearer playback URLs only in `_meta.mediaArchive.playbackUrl`, keeps them out of persisted widget state, and does not interpolate them into markup.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: Canvas; color: CanvasText; }
    button, input { font: inherit; color: inherit; }
    .app { min-height: 220px; padding: 14px; }
    .header, .detail-bar { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .header-side, .toolbar { display: flex; align-items: flex-end; gap: 7px; flex-wrap: wrap; }
    .header-side { flex-direction: column; }
    .eyebrow { color: GrayText; font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
    h1 { font-size: 16px; line-height: 1.35; margin: 3px 0 0; overflow-wrap: anywhere; }
    .count, .chip { border: 1px solid ButtonBorder; border-radius: 999px; font-size: 12px; padding: 4px 8px; white-space: nowrap; }
    .count, .chip { background: ButtonFace; }
    .notice { background: color-mix(in srgb, Orange 12%, Canvas); border: 1px solid color-mix(in srgb, Orange 45%, ButtonBorder); border-radius: 9px; font-size: 13px; line-height: 1.45; margin: 0 0 10px; padding: 9px 12px; }
    .carousel { display: grid; gap: 10px; grid-auto-columns: minmax(242px, 302px); grid-auto-flow: column; overflow-x: auto; padding: 1px 1px 9px; scroll-snap-type: x mandatory; }
    .card { background: Canvas; border: 1px solid ButtonBorder; border-radius: 13px; cursor: pointer; min-width: 0; overflow: hidden; padding: 0; position: relative; scroll-snap-align: start; text-align: left; }
    .card:hover, .card:focus-visible { outline: 2px solid AccentColor; outline-offset: 1px; }
    .card.low { opacity: .64; }
    .poster { background: linear-gradient(135deg, color-mix(in srgb, AccentColor 34%, Black), color-mix(in srgb, AccentColor 10%, Black)); height: 98px; position: relative; }
    .range-big { bottom: 10px; color: White; font-size: 19px; font-variant-numeric: tabular-nums; font-weight: 800; left: 12px; position: absolute; text-shadow: 0 1px 3px rgba(0,0,0,.5); }
    .badge { color: White; font-size: 11px; font-weight: 700; padding: 4px 8px; position: absolute; right: 10px; top: 9px; }
    .badge.high { background: #1a7f37; } .badge.medium { background: #a66c00; } .badge.low { background: #6e7781; }
    .play { background: rgba(255,255,255,.92); border-radius: 50%; bottom: 10px; display: grid; height: 34px; place-items: center; position: absolute; right: 12px; width: 34px; }
    .play::after { border-bottom: 6px solid transparent; border-left: 10px solid #111; border-top: 6px solid transparent; content: ""; margin-left: 2px; }
    .pick { align-items: center; background: rgba(0,0,0,.42); border-radius: 999px; color: White; cursor: pointer; display: flex; font-size: 12px; gap: 5px; left: 8px; padding: 4px 8px 4px 6px; position: absolute; top: 7px; z-index: 2; }
    .pick input { accent-color: AccentColor; height: 15px; margin: 0; width: 15px; }
    .card-body { padding: 10px 11px 11px; }
    .summary { color: GrayText; display: -webkit-box; font-size: 12.5px; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-height: 1.45; margin: 0 0 8px; overflow: hidden; }
    .meta-row { align-items: center; display: flex; flex-wrap: wrap; gap: 5px; }
    .topic { background: color-mix(in srgb, AccentColor 10%, Canvas); }
    .rights { background: color-mix(in srgb, Green 12%, Canvas); }
    .empty, .error { border: 1px dashed ButtonBorder; border-radius: 12px; color: GrayText; font-size: 14px; line-height: 1.5; padding: 24px; text-align: center; }
    .error { background: Mark; color: MarkText; }
    .detail[hidden], .results[hidden] { display: none; }
    .btn { background: ButtonFace; border: 1px solid ButtonBorder; border-radius: 8px; cursor: pointer; min-height: 42px; padding: 8px 12px; }
    .btn.primary { background: AccentColor; border-color: AccentColor; color: Canvas; }
    .btn.small { font-size: 13px; min-height: 34px; padding: 5px 10px; }
    .btn:disabled { cursor: not-allowed; opacity: .55; }
    .player-shell { background: Black; border: 1px solid ButtonBorder; border-radius: 13px; overflow: hidden; }
    video { background: Black; display: block; max-height: 58vh; width: 100%; }
    .player-status, .status { color: GrayText; font-size: 13px; line-height: 1.45; min-height: 28px; padding: 8px 0; }
    .player-status { background: Canvas; padding: 9px 12px; text-align: center; }
    .timeline { border: 1px solid ButtonBorder; border-radius: 12px; margin-top: 10px; padding: 12px; }
    .track { background: ButtonFace; border-radius: 999px; height: 10px; overflow: hidden; position: relative; }
    .range { background: AccentColor; border-radius: 999px; height: 100%; min-width: 3px; position: absolute; }
    .playhead { background: CanvasText; height: 16px; position: absolute; top: -3px; width: 2px; }
    .timeline-meta { color: GrayText; display: flex; font-size: 12px; font-variant-numeric: tabular-nums; justify-content: space-between; margin-top: 8px; }
    .evidence, .note { font-size: 13px; line-height: 1.45; margin-top: 10px; }
    .note { background: color-mix(in srgb, AccentColor 8%, Canvas); border-radius: 8px; padding: 9px 11px; }
    dialog { background: Canvas; border: 1px solid ButtonBorder; border-radius: 14px; color: CanvasText; padding: 18px; width: min(440px, calc(100% - 28px)); }
    dialog::backdrop { background: color-mix(in srgb, Black 45%, transparent); }
    dialog h2 { font-size: 17px; margin: 0 0 8px; }
    dialog p { font-size: 14px; line-height: 1.5; margin: 0 0 14px; }
    dialog input { background: Canvas; border: 1px solid ButtonBorder; border-radius: 8px; min-height: 42px; padding: 8px 11px; width: 100%; }
    .dialog-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
    @media (max-width: 560px) { .app { padding: 10px; } .carousel { grid-auto-columns: 86%; } .detail-bar { align-items: stretch; flex-direction: column; } .toolbar { display: grid; grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
  <main class="app">
    <section id="results" class="results">
      <header class="header">
        <div><div id="archive-label" class="eyebrow"></div><h1 id="query"></h1></div>
        <div class="header-side"><div id="count" class="count"></div><button id="save-collection" class="btn small" type="button"></button></div>
      </header>
      <div id="guidance" class="notice" hidden></div>
      <div id="carousel" class="carousel" aria-live="polite"></div>
      <div id="empty" class="empty" hidden></div>
      <div id="results-status" class="status" role="status" aria-live="polite"></div>
    </section>

    <section id="detail" class="detail" hidden>
      <div class="detail-bar">
        <div><div id="selected-label" class="eyebrow"></div><h1 id="detail-title"></h1></div>
        <div class="toolbar"><button id="back" class="btn" type="button"></button><button id="refresh" class="btn" type="button"></button><button id="fullscreen" class="btn" type="button"></button><button id="highlight" class="btn primary" type="button"></button></div>
      </div>
      <div class="player-shell"><video id="player" controls playsinline preload="metadata"></video><div id="player-status" class="player-status" role="status" aria-live="polite"></div></div>
      <div class="timeline"><div class="track"><div id="clip-range" class="range"></div><div id="playhead" class="playhead"></div></div><div class="timeline-meta"><span id="clip-time"></span><span id="duration"></span></div><div id="evidence" class="evidence"></div><div id="highlight-note" class="note"></div><div id="action-status" class="status" role="status" aria-live="polite"></div></div>
    </section>
    <div id="error" class="error" role="alert" hidden></div>
  </main>

  <dialog id="highlight-dialog"><h2 id="highlight-title"></h2><p id="highlight-body"></p><div class="dialog-actions"><button id="cancel-highlight" class="btn" type="button"></button><button id="confirm-highlight" class="btn primary" type="button"></button></div></dialog>
  <dialog id="collection-dialog"><h2 id="collection-title"></h2><p id="collection-body"></p><input id="collection-name" type="text" maxlength="80" /><div class="dialog-actions"><button id="cancel-collection" class="btn" type="button"></button><button id="confirm-collection" class="btn primary" type="button"></button></div></dialog>

  <script>
    (() => {
      // Swap or extend this table without touching tool names or data contracts.
      const STRINGS = {
        en: {
          archive: "Media Archive", results: "Media search results", clips: count => `${count} clip${count === 1 ? "" : "s"}`,
          saveAll: "Save all as collection", savePicked: count => `Save ${count} selected as collection`, selected: "Selected clip",
          back: "← Results", refresh: "Refresh", fullscreen: "Fullscreen", highlight: range => `Create highlight: ${range}`,
          noResults: query => `No confident match for “${query}”. Try action + subject + context.`, loading: "Preparing rights-checked playback…",
          playerReady: minutes => `Playback is available for about ${minutes} minutes and stops at the cited range.`, playerError: "Playback could not be prepared. Refresh and try again.",
          source: "Source", sourceDuration: duration => `Source ${duration}`, noDuration: "Source duration unavailable", highlightNote: range => `A new derivative will use ${range}; the original is not changed.`,
          confirmHighlight: "Create a non-destructive highlight?", cancel: "Cancel", create: "Create", collection: "Save collection", collectionName: "Collection name",
          collectionSaved: (name, count) => `Saved ${count} cited clip${count === 1 ? "" : "s"} to “${name}”.`, collectionFailed: "The collection could not be saved.",
          requested: "The highlight request was sent. Follow the job status in this conversation.", requestFailed: "The highlight request could not be sent.", high: "High confidence", medium: "Preview before citing", low: "No confident match",
          owned: "Owned", licensed: "Licensed", restricted: "Restricted", unknownRights: "Rights pending", add: "Add"
        },
        ko: {
          archive: "미디어 아카이브", results: "미디어 검색 결과", clips: count => `클립 ${count}개`,
          saveAll: "전체를 컬렉션으로 저장", savePicked: count => `선택 ${count}개 컬렉션으로 저장`, selected: "선택한 영상 구간",
          back: "← 검색 결과", refresh: "다시 불러오기", fullscreen: "전체화면", highlight: range => `${range} 하이라이트 만들기`,
          noResults: query => `‘${query}’와 일치하는 확실한 영상이 없습니다. 행동+대상+맥락으로 다시 설명해 보세요.`, loading: "권리를 확인한 뒤 재생을 준비하고 있어요…",
          playerReady: minutes => `미리보기는 약 ${minutes}분 동안 사용할 수 있고 인용 구간 끝에서 멈춥니다.`, playerError: "재생을 준비하지 못했습니다. 다시 불러온 뒤 시도해 주세요.",
          source: "원본", sourceDuration: duration => `원본 ${duration}`, noDuration: "원본 길이 확인 중", highlightNote: range => `${range} 구간으로 새 파생 영상을 만듭니다. 원본은 바뀌지 않습니다.`,
          confirmHighlight: "비파괴 하이라이트를 만들까요?", cancel: "취소", create: "만들기", collection: "컬렉션으로 저장", collectionName: "컬렉션 이름",
          collectionSaved: (name, count) => `‘${name}’ 컬렉션에 인용 클립 ${count}개를 저장했습니다.`, collectionFailed: "컬렉션을 저장하지 못했습니다.",
          requested: "하이라이트 생성을 요청했습니다. 이 대화에서 작업 상태를 확인하세요.", requestFailed: "하이라이트 요청을 보내지 못했습니다.", high: "일치 높음", medium: "미리보기 후 인용", low: "확실한 일치 없음",
          owned: "권리 보유", licensed: "라이선스", restricted: "재생 제한", unknownRights: "권리 확인 필요", add: "담기"
        }
      };
      const locale = (navigator.language || "en").toLowerCase().startsWith("ko") ? "ko" : "en";
      const t = STRINGS[locale];
      const state = { query: "", guidance: "", results: [], picked: new Set(), selected: null, preview: null, endOffset: null, view: "results" };
      const pending = new Map(); let requestId = 1;
      const el = id => document.getElementById(id);
      const clipKey = item => `${item.asset_id}:${item.start_sec}:${item.end_sec}`;
      const fmt = seconds => { const n = Math.max(0, Number(seconds) || 0), total = Math.floor(n), h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60; return h ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${m}:${String(s).padStart(2,"0")}`; };
      const rangeText = item => `${fmt(item.start_sec)}–${fmt(item.end_sec)}`;
      const confidence = item => { const value = String(item.confidence || "").toLowerCase(); if (["high", "medium", "low"].includes(value)) return value; const score = Number(item.score) || 0; return score >= .8 ? "high" : score >= .75 ? "medium" : "low"; };
      const confidenceLabel = item => t[confidence(item)];
      const rightsLabel = value => ({ OWNED: t.owned, LICENSED: t.licensed, RESTRICTED: t.restricted, UNKNOWN: t.unknownRights })[String(value || "UNKNOWN").toUpperCase()] || t.unknownRights;
      const clearError = () => { el("error").hidden = true; el("error").textContent = ""; };
      const showError = message => { el("error").hidden = false; el("error").textContent = message; };

      function rpc(method, params) { const id = requestId++; window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*"); return new Promise((resolve, reject) => pending.set(id, { resolve, reject })); }
      async function callTool(name, args) { return window.openai?.callTool ? window.openai.callTool(name, args) : rpc("tools/call", { name, arguments: args }); }
      async function requestFullscreen() { try { return window.openai?.requestDisplayMode ? window.openai.requestDisplayMode({ mode: "fullscreen" }) : rpc("ui/request-display-mode", { mode: "fullscreen" }); } catch (_) { return null; } }
      function persist() { try { window.openai?.setWidgetState?.({ query: state.query, guidance: state.guidance, results: state.results, picked: [...state.picked], view: state.view, selectedKey: state.selected ? clipKey(state.selected) : null }); } catch (_) {} }

      function applyStrings() {
        el("archive-label").textContent = t.archive; el("query").textContent = t.results; el("selected-label").textContent = t.selected;
        el("back").textContent = t.back; el("refresh").textContent = t.refresh; el("fullscreen").textContent = t.fullscreen;
        el("player-status").textContent = t.loading; el("highlight-title").textContent = t.confirmHighlight; el("cancel-highlight").textContent = t.cancel; el("confirm-highlight").textContent = t.create;
        el("collection-title").textContent = t.collection; el("collection-name").placeholder = t.collectionName; el("cancel-collection").textContent = t.cancel; el("confirm-collection").textContent = t.collection;
      }

      function updateCollectionButton() { const count = state.picked.size; el("save-collection").textContent = count ? t.savePicked(count) : t.saveAll; el("save-collection").disabled = state.results.length === 0; }
      function renderCards() {
        el("query").textContent = state.query || t.results; el("count").textContent = t.clips(state.results.length); el("guidance").hidden = !state.guidance; el("guidance").textContent = state.guidance;
        const root = el("carousel"); root.replaceChildren(); el("empty").hidden = state.results.length !== 0; el("empty").textContent = t.noResults(state.query);
        state.results.forEach(item => {
          const key = clipKey(item), card = document.createElement("button"); card.type = "button"; card.className = `card ${confidence(item) === "low" ? "low" : ""}`; card.setAttribute("aria-label", rangeText(item));
          const poster = document.createElement("div"); poster.className = "poster";
          const badge = document.createElement("span"); badge.className = `badge ${confidence(item)}`; badge.textContent = confidenceLabel(item);
          const range = document.createElement("span"); range.className = "range-big"; range.textContent = rangeText(item);
          const play = document.createElement("span"); play.className = "play";
          const pick = document.createElement("label"); pick.className = "pick"; const box = document.createElement("input"); box.type = "checkbox"; box.checked = state.picked.has(key); box.addEventListener("click", event => event.stopPropagation()); box.addEventListener("change", () => { box.checked ? state.picked.add(key) : state.picked.delete(key); updateCollectionButton(); persist(); }); pick.addEventListener("click", event => event.stopPropagation()); pick.append(box, document.createTextNode(t.add));
          poster.append(badge, range, play, pick);
          const body = document.createElement("div"); body.className = "card-body"; const summary = document.createElement("p"); summary.className = "summary"; summary.textContent = String(item.summary || "").replace(/^\[[^\]]*\]\s*/, "") || t.results;
          const meta = document.createElement("div"); meta.className = "meta-row"; [ `${(Number(item.end_sec) - Number(item.start_sec)).toFixed(1)}s`, String(item.modality || "visual"), rightsLabel(item.rights_state) ].forEach((value, index) => { const chip = document.createElement("span"); chip.className = `chip ${index === 2 ? "rights" : ""}`; chip.textContent = value; meta.append(chip); }); (item.topics || []).slice(0, 2).forEach(topic => { const chip = document.createElement("span"); chip.className = "chip topic"; chip.textContent = String(topic); meta.append(chip); });
          body.append(summary, meta); card.append(poster, body); card.addEventListener("click", () => openDetail(item)); root.append(card);
        });
        updateCollectionButton();
      }

      async function resolvePlayback(item) {
        clearError(); el("player-status").textContent = t.loading;
        try {
          const result = await callTool("resolve_media_playback", { asset_id: item.asset_id, start_sec: item.start_sec, expires_seconds: 180 });
          const data = result?.structuredContent || result?.structured_content || {};
          // WHY: only read the bearer value at playback time and never save it in state.
          const playbackUrl = result?._meta?.mediaArchive?.playbackUrl || result?.meta?.mediaArchive?.playbackUrl;
          if (!playbackUrl) throw new Error("missing playback URL");
          state.preview = data; const player = el("player"); player.src = playbackUrl; player.load();
          el("detail-title").textContent = `${data.file_name || t.results} · ${rangeText(item)}`; el("duration").textContent = data.duration_seconds ? t.sourceDuration(fmt(data.duration_seconds)) : t.noDuration;
          const duration = Math.max(Number(data.duration_seconds) || Number(item.end_sec) || 1, 1), left = Math.min(100, Math.max(0, Number(item.start_sec) / duration * 100)), width = Math.min(100 - left, Math.max(.5, (Number(item.end_sec) - Number(item.start_sec)) / duration * 100));
          el("clip-range").style.left = `${left}%`; el("clip-range").style.width = `${width}%`; state.endOffset = Number(item.end_sec) - Number(data.source_start_sec || 0);
          player.onloadedmetadata = async () => { player.currentTime = Math.max(0, Number(data.playback_start_offset_sec) || 0); el("player-status").textContent = t.playerReady(Math.max(1, Math.round((Number(data.expires_in_seconds) || 180) / 60))); try { await player.play(); } catch (_) {} };
          player.onerror = () => { el("player-status").textContent = t.playerError; };
        } catch (_) { el("player-status").textContent = t.playerError; }
      }

      async function openDetail(item) { state.selected = item; state.view = "detail"; el("results").hidden = true; el("detail").hidden = false; el("clip-time").textContent = rangeText(item); el("highlight").textContent = t.highlight(rangeText(item)); el("highlight-note").textContent = t.highlightNote(rangeText(item)); el("evidence").textContent = `${t.source}: ${item.source_id} · ${item.modality || "visual"} · ${confidenceLabel(item)}`; el("action-status").textContent = ""; persist(); await resolvePlayback(item); }
      function backToResults() { el("player").pause(); el("player").removeAttribute("src"); state.preview = null; state.view = "results"; el("detail").hidden = true; el("results").hidden = false; persist(); }
      function applyResult(result) { const payload = result?.structuredContent || result?.structured_content || result || {}; if (!Array.isArray(payload.results)) { showError(t.noResults("")); return; } clearError(); state.query = String(payload.query || ""); state.guidance = String(payload.guidance || ""); state.results = payload.results; state.picked = new Set(); state.view = "results"; el("detail").hidden = true; el("results").hidden = false; renderCards(); persist(); }
      function restoreState(saved) { if (!saved || !Array.isArray(saved.results) || !saved.results.length) return false; state.query = String(saved.query || ""); state.guidance = String(saved.guidance || ""); state.results = saved.results; state.picked = new Set(saved.picked || []); renderCards(); return true; }
      function pickedClips() { const chosen = state.picked.size ? state.results.filter(item => state.picked.has(clipKey(item))) : state.results; return chosen.map(item => ({ asset_id: item.asset_id, start_sec: Number(item.start_sec), end_sec: Number(item.end_sec), source_id: String(item.source_id) })); }
      function openCollectionDialog() { const clips = pickedClips(); if (!clips.length) return; el("collection-body").textContent = t.clips(clips.length); el("collection-name").value = (state.query || t.collection).slice(0, 80); el("collection-dialog").showModal(); el("collection-name").focus(); }
      async function saveCollection() { const name = el("collection-name").value.trim(); if (!name) return el("collection-name").focus(); const clips = pickedClips(); el("collection-dialog").close(); el("save-collection").disabled = true; const idempotencyKey = `collection-${crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`}`.slice(0, 64); try { const result = await callTool("create_collection", { name, query: state.query, clips, idempotency_key: idempotencyKey }); const data = result?.structuredContent || result?.structured_content || result || {}; el("results-status").textContent = t.collectionSaved(data.name || name, data.clip_count || clips.length); } catch (_) { el("results-status").textContent = t.collectionFailed; } el("save-collection").disabled = false; updateCollectionButton(); }
      function openHighlightDialog() { if (!state.selected) return; el("highlight-body").textContent = t.highlightNote(rangeText(state.selected)); el("highlight-dialog").showModal(); }
      async function requestHighlight() { if (!state.selected) return; el("highlight-dialog").close(); const item = state.selected, prompt = `Create a non-destructive highlight for asset ${item.asset_id} from ${item.start_sec} to ${item.end_sec} seconds using source ${item.source_id}.`; try { if (window.openai?.sendFollowUpMessage) await window.openai.sendFollowUpMessage({ prompt }); else await rpc("ui/message", { role: "user", content: [{ type: "text", text: prompt }] }); el("action-status").textContent = t.requested; } catch (_) { el("action-status").textContent = t.requestFailed; } }

      window.addEventListener("message", event => { if (event.source !== window.parent) return; const message = event.data; if (!message || message.jsonrpc !== "2.0") return; if (message.id !== undefined && pending.has(message.id)) { const request = pending.get(message.id); pending.delete(message.id); message.error ? request.reject(message.error) : request.resolve(message.result); return; } if (message.method === "ui/notifications/tool-result") applyResult(message.params); });
      el("back").addEventListener("click", backToResults); el("refresh").addEventListener("click", () => state.selected && resolvePlayback(state.selected)); el("fullscreen").addEventListener("click", requestFullscreen); el("highlight").addEventListener("click", openHighlightDialog); el("cancel-highlight").addEventListener("click", () => el("highlight-dialog").close()); el("confirm-highlight").addEventListener("click", requestHighlight); el("save-collection").addEventListener("click", openCollectionDialog); el("cancel-collection").addEventListener("click", () => el("collection-dialog").close()); el("confirm-collection").addEventListener("click", saveCollection); el("player").addEventListener("timeupdate", event => { const player = event.currentTarget, duration = Math.max(Number(state.preview?.duration_seconds) || 1, 1), sourceTime = (Number(state.preview?.source_start_sec) || 0) + player.currentTime; el("playhead").style.left = `${Math.min(100, Math.max(0, sourceTime / duration * 100))}%`; if (Number.isFinite(state.endOffset) && player.currentTime >= state.endOffset) player.pause(); });
      applyStrings(); if (window.openai?.toolOutput) applyResult(window.openai.toolOutput); else restoreState(window.openai?.widgetState);
    })();
  </script>
</body>
</html>
```

## Pattern 4 — Codex / ChatGPT plugin

### `clients/codex-plugin/.codex-plugin/plugin.json`

```json
{
  "name": "media-archive",
  "version": "0.1.0",
  "description": "Search, play, analyze, curate, render, and download governed media from an AWS-native archive.",
  "skills": "./skills/",
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "Media Archive",
    "shortDescription": "Search and play cited media clips.",
    "longDescription": "Semantic media search with source-time evidence, inline playback, non-destructive highlights, collections, rights checks, and short-lived downloads.",
    "developerName": "Media Archive Team",
    "category": "Productivity",
    "capabilities": ["Read", "Write"],
    "brandColor": "#FF9900",
    "composerIcon": "./assets/media-archive.svg",
    "logo": "./assets/media-archive.svg",
    "defaultPrompt": [
      "Search the media archive for a scene and show the cited clips.",
      "Analyze an asset and distinguish whole-asset findings from cited clip evidence.",
      "Build a non-destructive highlight from selected cited clips."
    ]
  }
}
```

### `clients/codex-plugin/.mcp.json.template`

```json
{
  "mcpServers": {
    "media-archive-app": {
      "command": "__PROJECT_ROOT__/.venv/bin/python",
      "args": ["-m", "local_bridge.server"],
      "cwd": "__PROJECT_ROOT__",
      "env": {
        "AWS_PROFILE": "__AWS_PROFILE__",
        "AWS_REGION": "__AWS_REGION__",
        "PYTHONPATH": "__PROJECT_ROOT__",
        "MEDIA_ARCHIVE_GATEWAY_URL": "__GATEWAY_URL__",
        "MEDIA_ARCHIVE_TOKEN_URL": "__TOKEN_URL__",
        "MEDIA_ARCHIVE_USER_POOL_ID": "__USER_POOL_ID__",
        "MEDIA_ARCHIVE_CLIENT_ID": "__CLIENT_ID__",
        "MEDIA_ARCHIVE_SCOPE": "media-archive/read media-archive/write",
        "MEDIA_ARCHIVE_ALLOWED_ROOT": "__ALLOWED_MEDIA_ROOT__",
        "MEDIA_ARCHIVE_PLAYBACK_ORIGIN": "__PLAYBACK_ORIGIN__"
      },
      "enabled": true,
      "startup_timeout_sec": 30,
      "tool_timeout_sec": 900,
      "default_tools_approval_mode": "prompt",
      "enabled_tools": [
        "upload_local_video",
        "list_assets",
        "get_asset",
        "search_assets",
        "render_media_results",
        "resolve_media_playback",
        "analyze_asset",
        "get_job_status",
        "render_highlight",
        "get_download_url",
        "create_collection",
        "list_collections",
        "get_collection",
        "get_ontology"
      ]
    }
  }
}
```

`enabled_tools` must contain every bridge tool, including all three collection tools. The configuration contains no OAuth secret: the bridge retrieves it at runtime with the selected AWS profile.

### `clients/codex-plugin/skills/media-archive/SKILL.md`

```markdown
---
name: media-archive
version: 0.1.0
description: Search, play, analyze, collect, render, and download governed media through the Media Archive. Triggers: media archive, video search, highlight reel, 영상 검색, 영상 편집, 영상 아카이브.
---

# Media Archive

Use the Media Archive MCP tools as the authoritative media backend.

## Search and visual results

- Query `search_assets` or `render_media_results` by **action + subject + context**, optionally entity + intent. Use the user's language.
- Use `render_media_results` when the user asks to show, browse, compare, or play footage. Use `search_assets` when structured evidence is sufficient.
- Every clip claim cites `asset_id`, `source_id`, `start_sec`, `end_sec`, modality, score, and confidence.
- Confidence policy: `high` may be cited; `medium` requires an in-product preview before citing; `low` or response `guidance` means **no confident match**. Do not promote weak hits.
- `analyze_asset` is whole-asset analysis. Never present it as frame-accurate clip evidence.

## Collections

- Use `create_collection` when the user asks to keep, group, reuse, or curate cited clips. Send a name, source query, every clip's `asset_id`, `source_id`, `start_sec`, `end_sec`, and a fresh idempotency key.
- A collection is metadata only: it does not render video and never changes an original.
- Use `list_collections` for saved sets and `get_collection` for a collection's cited clips. Pass selected clips to `render_highlight` only after editorial intent is clear.

## Rights and editing

- Preserve source video. `render_highlight` always creates a derivative under `derivatives/`.
- Do not infer, approve, or change rights from model output. Do not render or download a `RESTRICTED` asset.
- Obtain user confirmation when selected ranges or editorial intent are ambiguous.
- Poll `get_job_status` until processing or rendering reaches a terminal state.
- Treat playback and download URLs as bearer credentials: never quote, save, repost, or add them to notes. The UI-only resolver carries playback URLs in `_meta`.
```

## Pattern 5 — Kiro

### `clients/kiro/.kiro/settings/mcp.json.template`

```json
{
  "mcpServers": {
    "media-archive": {
      "command": "__PROJECT_ROOT__/.venv/bin/python",
      "args": ["-m", "local_bridge.server"],
      "cwd": "__PROJECT_ROOT__",
      "env": {
        "AWS_PROFILE": "__AWS_PROFILE__",
        "AWS_REGION": "__AWS_REGION__",
        "PYTHONPATH": "__PROJECT_ROOT__",
        "MEDIA_ARCHIVE_GATEWAY_URL": "__GATEWAY_URL__",
        "MEDIA_ARCHIVE_TOKEN_URL": "__TOKEN_URL__",
        "MEDIA_ARCHIVE_USER_POOL_ID": "__USER_POOL_ID__",
        "MEDIA_ARCHIVE_CLIENT_ID": "__CLIENT_ID__",
        "MEDIA_ARCHIVE_SCOPE": "media-archive/read media-archive/write",
        "MEDIA_ARCHIVE_ALLOWED_ROOT": "__ALLOWED_MEDIA_ROOT__",
        "MEDIA_ARCHIVE_PLAYBACK_ORIGIN": "__PLAYBACK_ORIGIN__"
      },
      "timeout": 300000
    }
  }
}
```

### `clients/kiro/.kiro/skills/media-archive-operations/SKILL.md`

```markdown
---
name: media-archive-operations
description: Upload, search, analyze, curate, render, and download media through the agentic Media Archive. Triggers: media archive, video search, highlight reel, 영상 검색, 영상 편집, 영상 아카이브.
source: project
---

# Media archive operations

## Safety invariants

- Preserve source video. All edits create derivatives; originals are never overwritten.
- Treat model-generated identity, rights, safety, brand, and ontology data as advisory metadata, not approval.
- Do not render or download `RESTRICTED` assets.
- For every clip-level finding, return `asset_id`, `source_id`, `start_sec`, `end_sec`, modality, score, and confidence.
- Do not claim frame-accurate evidence from `analyze_asset`; it is whole-asset analysis.
- Never persist, repost, or place presigned playback/download URLs in notes. They are short-lived bearer credentials.
- Archive only through a separately approved administrative surface that enforces `confirm_archive=true`; explain cold-retrieval implications first.

## Query and confidence rules

- Phrase queries as intent: action + subject + context, optionally entity + intent. Korean and English both work.
- `high` confidence may be cited. Verify `medium` with `render_media_results`/`resolve_media_playback` before citing. For `low` confidence or `guidance`, say no confident match.
- Use `render_media_results` for visual browsing. Use `search_assets` for structured results only.
- Request timestamps explicitly from `analyze_asset`, then label those findings as model analysis rather than source-time evidence.

## Collections and non-destructive rendering

- Use `create_collection` for selected cited clips with a new idempotency key. Collections store only metadata.
- Use `list_collections` and `get_collection` to resume a curation task.
- Build a source-relative EDL: sort ranges, remove overlaps, and ask for confirmation when editorial intent is ambiguous.
- Call `render_highlight`, poll `get_job_status`, then request `get_download_url` only after server-side rights revalidation.

## Local uploads

- Call `upload_local_video` only for a supported video under `MEDIA_ARCHIVE_ALLOWED_ROOT`.
- Preserve the returned asset and job identifiers. Do not search a not-yet-ready asset.
```

## Pattern 6 — Claude Code

### `clients/claude/.mcp.json.template`

```json
{
  "mcpServers": {
    "media-archive": {
      "command": "__PROJECT_ROOT__/.venv/bin/python",
      "args": ["-m", "local_bridge.server"],
      "cwd": "__PROJECT_ROOT__",
      "env": {
        "AWS_PROFILE": "__AWS_PROFILE__",
        "AWS_REGION": "__AWS_REGION__",
        "PYTHONPATH": "__PROJECT_ROOT__",
        "MEDIA_ARCHIVE_GATEWAY_URL": "__GATEWAY_URL__",
        "MEDIA_ARCHIVE_TOKEN_URL": "__TOKEN_URL__",
        "MEDIA_ARCHIVE_USER_POOL_ID": "__USER_POOL_ID__",
        "MEDIA_ARCHIVE_CLIENT_ID": "__CLIENT_ID__",
        "MEDIA_ARCHIVE_SCOPE": "media-archive/read media-archive/write",
        "MEDIA_ARCHIVE_ALLOWED_ROOT": "__ALLOWED_MEDIA_ROOT__",
        "MEDIA_ARCHIVE_PLAYBACK_ORIGIN": "__PLAYBACK_ORIGIN__"
      }
    }
  }
}
```

### `clients/claude/skills/media-archive-operations/SKILL.md`

```markdown
---
name: media-archive-operations
description: Search, inspect, curate, render, and download governed media. Triggers: media archive, video search, highlight reel, 영상 검색, 영상 편집, 영상 아카이브.
---

# Media archive operations

- Search with action + subject + context; add an entity only when it improves recall.
- For source-time claims, provide `asset_id`, `source_id`, `start_sec`, `end_sec`, modality, score, and confidence.
- Confidence is a policy boundary: cite `high`, preview `medium`, and report `low`/`guidance` as no confident match.
- Use `render_media_results` for visual exploration and `search_assets` for structured evidence.
- `analyze_asset` is whole-asset model analysis, not frame-accurate evidence.
- Use `create_collection`, `list_collections`, and `get_collection` for non-destructive curation. Collections do not produce media.
- `render_highlight` creates only a derivative. Confirm ambiguous editorial selections, poll `get_job_status`, and require rights revalidation before `get_download_url`.
- Never persist bearer playback/download URLs. The MCP App receives playback only in `_meta`.
- `upload_local_video` accepts only approved local files beneath `MEDIA_ARCHIVE_ALLOWED_ROOT`.
```

Install the rendered MCP configuration in the project's or user's Claude Code MCP settings and install the user skill beneath `$HOME/.claude/skills/media-archive-operations/`. Do not copy an OAuth secret into either location.

## Pattern 7 — Amazon Quick

### `clients/quick/README.md`

```markdown
# Amazon Quick connection

## Quick Web: service-to-service OAuth

1. Create or select a **service-to-service OAuth 2.0** connection for the Media Archive Gateway.
2. Set the MCP endpoint to `<gateway-url>`.
3. Set the token endpoint to `<token-url>`.
4. Set the client identifier to `<client-id>`.
5. Store `<secret-reference>` in Quick's managed credential/secret reference field. Do not paste the client secret into a project file, prompt, static header, or connector metadata.
6. Request the scope `media-archive/read media-archive/write`.
7. Enable and publish the connector actions only after the owner reviews the 14 bridge tools. Keep write tools approval-gated.
8. Verify one `search_assets` request, then a `render_media_results` request. A visual-capable Quick surface may render the resource; other surfaces still receive the structured cited fallback.

## Quick Desktop caveat

Some Quick Desktop builds accept only an MCP URL plus static headers. A static `Authorization` header cannot safely implement OAuth client-credentials refresh because its bearer token expires. Do not place a long-lived token or client secret in that header. Use Quick Web's service-to-service OAuth connector when available; otherwise use the local stdio bridge through a supported desktop MCP integration so the bridge refreshes tokens in memory.
```

### `clients/quick/service-oauth.json.template`

```json
{
  "mcp_endpoint": "__GATEWAY_URL__",
  "auth": {
    "type": "oauth2_client_credentials",
    "token_url": "__TOKEN_URL__",
    "client_id": "__CLIENT_ID__",
    "client_secret_reference": "__SECRET_REFERENCE__",
    "scope": "media-archive/read media-archive/write"
  }
}
```

The secret reference is an identifier for the platform credential store, not a secret value. Runtime retrieval must be authorized through the selected service identity.

## Pattern 8 — `scripts/deploy.sh`

This post-deployment pattern keeps source templates safe to commit and writes generated client configuration only after infrastructure outputs are known. It propagates endpoint, client ID, playback origin, and a **secret reference**; it never emits the client secret itself.

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_NAME="${MEDIA_ARCHIVE_STACK_NAME:-MediaArchiveStack}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION before deployment}"
AWS_PROFILE="${AWS_PROFILE:-default}"
ALLOWED_MEDIA_ROOT="${MEDIA_ARCHIVE_ALLOWED_ROOT:-${PROJECT_ROOT}/media}"
OUTPUTS_FILE="${PROJECT_ROOT}/.generated/cdk-outputs.json"

mkdir -p "$(dirname "${OUTPUTS_FILE}")"

npx cdk deploy "${STACK_NAME}" \
  --require-approval never \
  --outputs-file "${OUTPUTS_FILE}" \
  --context region="${AWS_REGION}"

export PROJECT_ROOT STACK_NAME AWS_REGION AWS_PROFILE ALLOWED_MEDIA_ROOT OUTPUTS_FILE
python3 <<'PY'
import json
import os
from pathlib import Path

project_root = Path(os.environ["PROJECT_ROOT"])
outputs_file = Path(os.environ["OUTPUTS_FILE"])
outputs = json.loads(outputs_file.read_text(encoding="utf-8"))
stack = outputs.get(os.environ["STACK_NAME"], {})
if not isinstance(stack, dict):
    raise SystemExit("CDK outputs did not contain the expected stack")

# WHY: fail instead of generating a connector with a fake or stale endpoint.
def required(name: str) -> str:
    value = stack.get(name)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"missing required CDK output: {name}")
    return value

values = {
    "__PROJECT_ROOT__": str(project_root),
    "__AWS_PROFILE__": os.environ["AWS_PROFILE"],
    "__AWS_REGION__": os.environ["AWS_REGION"],
    "__ALLOWED_MEDIA_ROOT__": os.environ["ALLOWED_MEDIA_ROOT"],
    "__GATEWAY_URL__": required("GatewayUrl"),
    "__TOKEN_URL__": required("OAuthTokenUrl"),
    "__USER_POOL_ID__": required("OAuthUserPoolId"),
    "__CLIENT_ID__": required("OAuthClientId"),
    "__PLAYBACK_ORIGIN__": required("PlaybackOrigin"),
    # This is a Secrets Manager reference for Quick Web, not secret material.
    "__SECRET_REFERENCE__": required("OAuthClientSecretReference"),
}

render_targets = {
    project_root / "clients/codex-plugin/.mcp.json.template": project_root / "clients/codex-plugin/.mcp.json",
    project_root / "clients/kiro/.kiro/settings/mcp.json.template": project_root / "clients/kiro/.kiro/settings/mcp.json",
    project_root / "clients/claude/.mcp.json.template": project_root / "clients/claude/.mcp.json",
    project_root / "clients/quick/service-oauth.json.template": project_root / "clients/quick/service-oauth.json",
}

for source, destination in render_targets.items():
    if not source.is_file():
        raise SystemExit(f"missing client configuration template: {source}")
    rendered = source.read_text(encoding="utf-8")
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    unresolved = [token for token in values if token in rendered]
    if unresolved:
        raise SystemExit(f"unresolved placeholders in {source}: {', '.join(unresolved)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    print(f"wrote {destination.relative_to(project_root)}")
PY

python3 "${PROJECT_ROOT}/scripts/verify.py" --check-client-configs
```

Keep `.generated/` and rendered local configuration files out of source control if deployment-specific endpoint values are sensitive to the organization. Commit only the templates and code. The generated configuration may include identifiers and endpoints but never an OAuth client secret.

## Pattern 9 — `tests/test_local_bridge.py` excerpts

```python
import asyncio
from pathlib import Path

from local_bridge import server
from local_bridge.remote_client import RemoteMediaArchiveClient


class PlaybackClient:
    async def call_tool(self, name, arguments):
        if name == "get_preview_url":
            return {
                "asset_id": "asset-1",
                "url": "<bearer-playback-url>",
                "segment_index": 0,
                "source_start_sec": 0,
                "source_end_sec": 30,
                "playback_start_offset_sec": arguments["start_sec"],
                "expires_in_seconds": 180,
            }
        if name == "get_asset":
            return {"file_name": "clip.mp4", "duration_seconds": 30}
        raise AssertionError(name)


def test_playback_bearer_url_is_only_in_result_meta(monkeypatch):
    monkeypatch.setattr(server, "_client", PlaybackClient())
    result = asyncio.run(server.resolve_media_playback("asset-1", 4))

    assert result.meta["mediaArchive"]["playbackUrl"] == "<bearer-playback-url>"
    assert "url" not in result.structuredContent
    assert "<bearer-playback-url>" not in str(result.structuredContent)
    assert "<bearer-playback-url>" not in server.UI_PATH.read_text(encoding="utf-8")


def test_media_results_resource_is_discoverable_mcp_app_html(monkeypatch):
    monkeypatch.setenv("MEDIA_ARCHIVE_PLAYBACK_ORIGIN", "<playback-origin>")
    resources = asyncio.run(server.mcp.list_resources())
    resource = next(item for item in resources if str(item.uri) == server.MEDIA_RESULTS_UI_URI)

    assert resource.mimeType == server.MCP_APP_MIME_TYPE
    assert resource.meta["ui"]["csp"]["resourceDomains"] != ["*"]
    assert server._resource_meta()["ui"]["csp"]["resourceDomains"] == ["<playback-origin>"]
    content = asyncio.run(server.mcp.read_resource(server.MEDIA_RESULTS_UI_URI))
    assert "<video" in content[0].content
    assert "resolve_media_playback" in content[0].content


def test_gateway_prefixes_expose_unique_unprefixed_aliases():
    aliases, exposed = RemoteMediaArchiveClient._index_tool_names(
        ["media-catalog___search_assets", "media-commands___render_highlight"]
    )

    assert exposed == ["search_assets", "render_highlight"]
    assert aliases["search_assets"] == "media-catalog___search_assets"
    assert aliases["render_highlight"] == "media-commands___render_highlight"


def test_allowed_local_video_rejects_a_path_outside_the_configured_root(
    tmp_path: Path, monkeypatch
):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    monkeypatch.setenv("MEDIA_ARCHIVE_ALLOWED_ROOT", str(approved))

    try:
        server.allowed_local_video(str(outside))
    except ValueError as error:
        assert "allowed root" in str(error)
    else:
        raise AssertionError("expected an out-of-root upload to be rejected")
```

## Cross-layer mapping — ChatGPT search to MCP Apps widget

| Layer | `show clips of a runner crossing a finish line` flow | Security / correctness boundary |
|---|---|---|
| ChatGPT/Codex plugin | The plugin skill recognizes “show clips” and selects `render_media_results` instead of a plain `search_assets` call. | Intent query is action + subject + context; no result is asserted without its confidence band. |
| Local stdio bridge | `server.py` calls `RemoteMediaArchiveClient.call_tool("search_assets", …)` and normalizes only cited `asset_id`, `source_id`, time range, modality, score, confidence, rights, and summary. | Gateway prefix stripping maps `media-catalog___search_assets` only when unique; no bearer URL enters `structuredContent`. |
| Gateway | Streamable HTTP accepts the cached client-credentials access token and routes the prefixed catalog operation to the `media-catalog` target. | OAuth scope and Gateway policy separate read catalog access from write commands. |
| Catalog Lambda | The catalog handler applies tenant/rights filters, retrieves vector and metadata matches, and returns source-relative evidence with confidence. | Server-side tenant and rights enforcement remains authoritative; model metadata is advisory. |
| Bridge result | `render_media_results` emits text fallback plus structured cited results and attaches `ui://media-archive/media-results.v1.html`. | Structured output is usable on non-visual clients and is free of playback credentials. |
| MCP Apps widget | The host resolves the versioned resource and renders its carousel. A card click calls `resolve_media_playback`; the bridge rechecks rights and puts the short-lived proxy URL only in `_meta.mediaArchive.playbackUrl`. | CSP is restricted to the deployment playback origin, the URL is never persisted, and the player stops at the cited end time. |

When publishing `media-results.v2.html` or later, leave `ui://media-archive/media-results.v1.html` registered as an alias that serves the updated safe UI until existing conversations and client caches have aged out.
