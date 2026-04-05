from ccproxy.config import resolve_provider_selector
from ccproxy.proxy import build_upstream_url, classify_request


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


def test_classify_request_maps_codex_aliases() -> None:
    assert classify_request("/responses") == ("codex", "/v1/responses")
    assert classify_request("/chat/completions") == ("codex", "/v1/chat/completions")


def test_classify_request_maps_claude_aliases() -> None:
    assert classify_request("/v1/messages") == ("claude", "/v1/messages")
    assert classify_request("/claude/v1/messages") == ("claude", "/v1/messages")
