from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


@dataclass(frozen=True)
class RoutedRequest:
    app: str
    upstream_path: str


class BaseAdapter:
    app: str

    def route(self, path: str) -> str | None:
        raise NotImplementedError

    def build_headers(self, request_headers: Any, provider: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError


class ClaudeAdapter(BaseAdapter):
    app = "claude"

    def route(self, path: str) -> str | None:
        if path.startswith("/claude/"):
            return path[len("/claude") :]
        if path == "/v1/messages":
            return path
        return None

    def build_headers(self, request_headers: Any, provider: dict[str, Any]) -> dict[str, str]:
        headers = {
            key: value
            for key, value in request_headers.items()
            if key.lower() not in {"host", "authorization", "x-api-key", "content-length", "accept-encoding"}
        }
        api_key = provider["api_key"]
        auth_mode = provider.get("auth_mode", "bearer")
        if auth_mode == "x-api-key":
            headers["x-api-key"] = api_key
        elif auth_mode == "both":
            headers["Authorization"] = f"Bearer {api_key}"
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers


class CodexAdapter(BaseAdapter):
    app = "codex"

    def route(self, path: str) -> str | None:
        if path in {"/responses", "/chat/completions"}:
            return f"/v1{path}"
        if path.startswith("/v1/"):
            return path
        return None

    def build_headers(self, request_headers: Any, provider: dict[str, Any]) -> dict[str, str]:
        headers = {
            key: value
            for key, value in request_headers.items()
            if key.lower() not in {"host", "authorization", "x-api-key", "content-length", "accept-encoding"}
        }
        headers["Authorization"] = f"Bearer {provider['api_key']}"
        return headers


ADAPTERS = [ClaudeAdapter(), CodexAdapter()]
ADAPTERS_BY_APP = {adapter.app: adapter for adapter in ADAPTERS}


def build_upstream_url(base_url: str, request_path: str, query_string: str = "") -> str:
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/v1") and request_path.startswith("/v1/"):
        final_path = base_path + request_path[3:]
    elif base_path and request_path.startswith(base_path + "/"):
        final_path = request_path
    else:
        final_path = f"{base_path}{request_path}" if base_path else request_path

    result = SplitResult(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=final_path,
        query=query_string,
        fragment="",
    )
    return urlunsplit(result)


def build_upstream_websocket_url(base_url: str, request_path: str, query_string: str = "") -> str:
    http_url = build_upstream_url(base_url, request_path, query_string)
    parsed = urlsplit(http_url)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in {"ws", "wss"}:
        scheme = parsed.scheme
    else:
        raise ValueError(f"unsupported websocket upstream scheme: {parsed.scheme}")
    return urlunsplit(
        SplitResult(
            scheme=scheme,
            netloc=parsed.netloc,
            path=parsed.path,
            query=parsed.query,
            fragment="",
        )
    )


def route_request(path: str) -> RoutedRequest | None:
    for adapter in ADAPTERS:
        upstream_path = adapter.route(path)
        if upstream_path is not None:
            return RoutedRequest(app=adapter.app, upstream_path=upstream_path)
    return None
