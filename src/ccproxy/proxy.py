from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from ccproxy.config import current_provider, current_provider_id, load_config, save_config

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

FAILOVER_STATUS_CODES = {408, 429, 500, 502, 503, 504}


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


def classify_request(path: str) -> tuple[str, str] | None:
    if path.startswith("/claude/"):
        return "claude", path[len("/claude") :]
    if path == "/v1/messages":
        return "claude", path
    if path in {"/responses", "/chat/completions"}:
        return "codex", f"/v1{path}"
    if path.startswith("/v1/"):
        return "codex", path
    return None


def build_forward_headers(
    request_headers: Any,
    app: str,
    provider: dict[str, Any],
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request_headers.items()
        if key.lower() not in {"host", "authorization", "x-api-key", "content-length"}
    }

    api_key = provider["api_key"]
    auth_mode = provider.get("auth_mode", "bearer")
    if app == "codex":
        headers["Authorization"] = f"Bearer {api_key}"
        return headers

    if auth_mode == "x-api-key":
        headers["x-api-key"] = api_key
    elif auth_mode == "both":
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def should_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


def provider_attempt_order(
    config: dict[str, Any],
    app: str,
) -> list[tuple[str, dict[str, Any]]]:
    providers = config["apps"][app]["providers"]
    if not providers:
        raise ValueError(f"no providers configured for {app}")

    items = list(providers.items())
    current_id = current_provider_id(config, app)
    if not current_id:
        return items

    index = next(
        (idx for idx, (provider_id, _provider) in enumerate(items) if provider_id == current_id),
        None,
    )
    if index is None:
        return items
    return items[index:] + items[:index]


def persist_failover_selection(
    config: dict[str, Any],
    app: str,
    provider_id: str,
) -> None:
    if config["apps"][app]["current"] == provider_id:
        return
    config["apps"][app]["current"] = provider_id
    save_config(config)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def forward(request: web.Request) -> web.StreamResponse:
    classified = classify_request(request.path)
    if classified is None:
        return web.json_response(
            {
                "error": "unsupported path",
                "path": request.path,
            },
            status=404,
        )

    app, upstream_path = classified
    config = load_config()
    body = await request.read()
    session: ClientSession = request.app["session"]
    auto_failover = bool(config["proxy"].get("auto_failover", True))
    attempts = provider_attempt_order(config, app)
    last_error: str | None = None

    for attempt_index, (provider_id, provider) in enumerate(attempts):
        upstream_url = build_upstream_url(
            provider["base_url"], upstream_path, request.query_string
        )
        headers = build_forward_headers(request.headers, app, provider)
        logging.info("proxy %s -> %s (%s)", request.path_qs, upstream_url, provider_id)

        try:
            async with session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=body,
            ) as upstream:
                can_failover = (
                    auto_failover
                    and attempt_index + 1 < len(attempts)
                    and should_failover_status(upstream.status)
                )
                if can_failover:
                    last_error = f"upstream status {upstream.status}"
                    logging.warning(
                        "provider %s returned %s for %s, trying next provider",
                        provider_id,
                        upstream.status,
                        request.path_qs,
                    )
                    await upstream.read()
                    continue

                if attempt_index > 0:
                    persist_failover_selection(config, app, provider_id)
                    logging.warning(
                        "auto failover switched %s current provider to %s",
                        app,
                        provider_id,
                    )

                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() not in HOP_BY_HOP_HEADERS
                }
                response = web.StreamResponse(
                    status=upstream.status,
                    headers=response_headers,
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            last_error = str(exc)
            can_failover = auto_failover and attempt_index + 1 < len(attempts)
            if can_failover:
                logging.warning(
                    "provider %s request failed for %s: %s; trying next provider",
                    provider_id,
                    request.path_qs,
                    exc,
                )
                continue
            return web.json_response(
                {
                    "error": "upstream request failed",
                    "provider_id": provider_id,
                    "detail": str(exc),
                },
                status=502,
            )

    return web.json_response(
        {
            "error": "all providers failed",
            "detail": last_error or "unknown upstream failure",
        },
        status=502,
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/__ccproxy/health", health)
    app.router.add_route("*", "/{tail:.*}", forward)
    return app


async def run_proxy(host: str, port: int) -> None:
    timeout = ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=180)
    app = make_app()
    app["session"] = ClientSession(timeout=timeout, auto_decompress=False)

    stop_event = asyncio.Event()

    async def _cleanup(_app: web.Application) -> None:
        await app["session"].close()

    app.on_cleanup.append(_cleanup)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info("ccproxy listening on http://%s:%s", host, port)

    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, _request_shutdown)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: _request_shutdown())

    await stop_event.wait()
    logging.info("ccproxy shutting down")
    await runner.cleanup()
