from pathlib import Path

from ccproxy.adapters import build_upstream_url, route_request
from ccproxy.checks import next_provider_candidates
from ccproxy.cli import build_health_snapshot, detect_cli_lang
from ccproxy.config import default_config, proxy_runtime_status, resolve_provider_selector, update_proxy_config
from ccproxy.health_store import (
    default_health_state,
    record_failure,
    record_success,
    reorder_providers_by_cooldown,
)
from ccproxy.proxy import provider_attempt_order, should_failover_status
from ccproxy.service import build_unit


def test_resolve_provider_selector_prefers_id() -> None:
    providers = {
        "demo": {"name": "Shared"},
        "other": {"name": "demo"},
    }
    provider_id, provider = resolve_provider_selector(providers, "demo")
    assert provider_id == "demo"
    assert provider["name"] == "Shared"


def test_build_upstream_url_dedupes_v1() -> None:
    url = build_upstream_url("https://example.com/v1", "/v1/responses")
    assert url == "https://example.com/v1/responses"


def test_build_upstream_url_appends_v1_path() -> None:
    url = build_upstream_url("https://example.com", "/v1/messages")
    assert url == "https://example.com/v1/messages"


def test_route_request_maps_codex_aliases() -> None:
    assert route_request("/responses") == route_request("/responses").__class__(
        app="codex",
        upstream_path="/v1/responses",
    )
    assert route_request("/chat/completions") == route_request("/chat/completions").__class__(
        app="codex",
        upstream_path="/v1/chat/completions",
    )


def test_route_request_maps_claude_aliases() -> None:
    assert route_request("/v1/messages") == route_request("/v1/messages").__class__(
        app="claude",
        upstream_path="/v1/messages",
    )
    assert route_request("/claude/v1/messages") == route_request("/claude/v1/messages").__class__(
        app="claude",
        upstream_path="/v1/messages",
    )


def test_next_provider_candidates_rotate_after_current() -> None:
    data = {
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A"},
                    "b": {"name": "B"},
                    "c": {"name": "C"},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    rotated = next_provider_candidates(data, "codex")
    assert [provider_id for provider_id, _provider in rotated] == ["c", "a", "b"]


def test_provider_attempt_order_keeps_current_first() -> None:
    data = {
        "proxy": {"cooldown_sec": 60},
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A"},
                    "b": {"name": "B"},
                    "c": {"name": "C"},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    ordered = provider_attempt_order(data, default_health_state(), "codex")
    assert [provider_id for provider_id, _provider in ordered] == ["b", "c", "a"]


def test_should_failover_status_is_conservative() -> None:
    assert should_failover_status(429) is True
    assert should_failover_status(503) is True
    assert should_failover_status(401) is False
    assert should_failover_status(400) is False


def test_reorder_providers_moves_cooling_provider_to_end() -> None:
    state = default_health_state()
    record_failure(
        state,
        "claude",
        "a",
        "A",
        "boom",
        cooldown_sec=60,
        now_ts=100.0,
    )
    items = [("a", {"name": "A"}), ("b", {"name": "B"})]
    reordered = reorder_providers_by_cooldown(items, state, "claude", now_ts=101.0)
    assert [provider_id for provider_id, _provider in reordered] == ["b", "a"]


def test_record_success_clears_cooldown() -> None:
    state = default_health_state()
    record_failure(
        state,
        "codex",
        "a",
        "A",
        "boom",
        cooldown_sec=60,
        now_ts=100.0,
    )
    record_success(state, "codex", "a", "A", now_ts=120.0)
    entry = state["apps"]["codex"]["a"]
    assert entry["cooldown_until"] is None
    assert entry["consecutive_failures"] == 0


def test_update_proxy_config_changes_cooldown_and_failover() -> None:
    data = default_config()
    changed = update_proxy_config(data, cooldown_sec=120, auto_failover=False)
    assert changed == {"auto_failover": False, "cooldown_sec": 120}
    assert data["proxy"]["cooldown_sec"] == 120
    assert data["proxy"]["auto_failover"] is False


def test_detect_cli_lang_prefers_locale_environment() -> None:
    lang = detect_cli_lang({"LANG": "zh_CN.UTF-8"})
    assert lang == "zh"


def test_detect_cli_lang_prefers_lc_all_over_lang() -> None:
    lang = detect_cli_lang({"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"})
    assert lang == "en"


def test_detect_cli_lang_prefers_lc_messages_over_lang() -> None:
    lang = detect_cli_lang({"LC_MESSAGES": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"})
    assert lang == "en"


def test_proxy_runtime_status_uses_health_probe_without_pid(monkeypatch) -> None:
    data = default_config()
    monkeypatch.setattr("ccproxy.config.remove_stale_pid_file", lambda: None)
    monkeypatch.setattr("ccproxy.config.pid_from_file", lambda: None)
    monkeypatch.setattr("ccproxy.config.is_pid_running", lambda pid: False)
    monkeypatch.setattr("ccproxy.config.proxy_health_ok", lambda host, port, timeout_sec=0.5: True)
    monkeypatch.setattr("ccproxy.config.systemd_service_scope", lambda: "system")

    status = proxy_runtime_status(data)
    assert status["running"] is True
    assert status["healthy"] is True
    assert status["manager"] == "systemd-system"


def test_build_health_snapshot_has_file_and_rows() -> None:
    snapshot = build_health_snapshot("claude")
    assert "health_state_file" in snapshot
    assert "apps" in snapshot
    assert "claude" in snapshot["apps"]


def test_build_user_unit_contains_user_manager_target() -> None:
    unit = build_unit(
        "user",
        Path("/home/demo/.local/bin/ccproxy"),
        "demo",
        home=Path("/home/demo"),
    )
    assert "WantedBy=default.target" in unit
    assert "ExecStart=/home/demo/.local/bin/ccproxy proxy run" in unit
    assert "User=demo" not in unit


def test_build_system_unit_contains_explicit_user() -> None:
    unit = build_unit(
        "system",
        Path("/home/demo/.local/bin/ccproxy"),
        "demo",
        home=Path("/home/demo"),
    )
    assert "WantedBy=multi-user.target" in unit
    assert "User=demo" in unit
