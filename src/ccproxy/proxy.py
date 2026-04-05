from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from ccproxy.adapters import ADAPTERS_BY_APP, build_upstream_url, route_request
from ccproxy.config import current_provider_id, load_config, proxy_max_body_bytes, save_config
from ccproxy.health_store import (
    load_health_state,
    record_failure,
    record_success,
    reorder_providers_by_cooldown,
    save_health_state,
)

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


def should_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


def provider_attempt_order(
    config: dict[str, Any],
    health_state: dict[str, Any],
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
        return reorder_providers_by_cooldown(items, health_state, app)
    ordered = items[index:] + items[:index]
    return reorder_providers_by_cooldown(ordered, health_state, app)


def persist_failover_selection(
    config: dict[str, Any],
    app: str,
    provider_id: str,
) -> None:
    if config["apps"][app]["current"] == provider_id:
        return
    config["apps"][app]["current"] = provider_id
    save_config(config)


async def _update_health_state(
    request: web.Request,
    updater,
) -> None:
    lock: asyncio.Lock = request.app["health_lock"]
    async with lock:
        health_state = request.app["health_state"]
        updater(health_state)
        save_health_state(health_state)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def forward(request: web.Request) -> web.StreamResponse:
    routed = route_request(request.path)
    if routed is None:
        return web.json_response(
            {
                "error": "unsupported path",
                "path": request.path,
            },
            status=404,
        )

    app = routed.app
    upstream_path = routed.upstream_path
    adapter = ADAPTERS_BY_APP[app]
    config = load_config()
    health_state = request.app["health_state"]
    body = await request.read()
    session: ClientSession = request.app["session"]
    auto_failover = bool(config["proxy"].get("auto_failover", True))
    cooldown_sec = int(config["proxy"].get("cooldown_sec", 60))
    attempts = provider_attempt_order(config, health_state, app)
    last_error: str | None = None

    for attempt_index, (provider_id, provider) in enumerate(attempts):
        upstream_url = build_upstream_url(
            provider["base_url"], upstream_path, request.query_string
        )
        headers = adapter.build_headers(request.headers, provider)
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
                    await _update_health_state(
                        request,
                        lambda state: record_failure(
                            state,
                            app,
                            provider_id,
                            provider.get("name", provider_id),
                            last_error or "",
                            cooldown_sec,
                            now_ts=time.time(),
                        ),
                    )
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
                if 200 <= upstream.status < 300:
                    await _update_health_state(
                        request,
                        lambda state: record_success(
                            state,
                            app,
                            provider_id,
                            provider.get("name", provider_id),
                            now_ts=time.time(),
                        ),
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
            await _update_health_state(
                request,
                lambda state: record_failure(
                    state,
                    app,
                    provider_id,
                    provider.get("name", provider_id),
                    last_error or "",
                    cooldown_sec,
                    now_ts=time.time(),
                ),
            )
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


def make_app(max_body_bytes: int) -> web.Application:
    app = web.Application(client_max_size=max_body_bytes)
    app.router.add_get("/__ccproxy/health", health)
    app.router.add_route("*", "/{tail:.*}", forward)
    return app


async def run_proxy(host: str, port: int) -> None:
    timeout = ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=180)
    config = load_config()
    max_body_bytes = proxy_max_body_bytes(config)
    app = make_app(max_body_bytes)
    app["session"] = ClientSession(timeout=timeout, auto_decompress=False)
    app["health_state"] = load_health_state()
    app["health_lock"] = asyncio.Lock()

    stop_event = asyncio.Event()

    async def _cleanup(_app: web.Application) -> None:
        await app["session"].close()

    app.on_cleanup.append(_cleanup)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info(
        "ccproxy listening on http://%s:%s (max_body_mb=%s)",
        host,
        port,
        config["proxy"].get("max_body_mb", 64),
    )

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
