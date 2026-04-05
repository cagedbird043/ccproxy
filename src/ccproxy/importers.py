from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ccproxy.config import APP_CHOICES, upsert_provider

BASE_URL_RE = re.compile(r'^\s*base_url\s*=\s*"([^"]+)"', re.MULTILINE)
MODEL_RE = re.compile(r'^\s*model\s*=\s*"([^"]+)"', re.MULTILINE)


def _extract_codex_provider(settings: dict[str, Any]) -> dict[str, Any] | None:
    auth = settings.get("auth") or {}
    api_key = auth.get("OPENAI_API_KEY")
    config_text = settings.get("config", "")
    if not api_key or not isinstance(config_text, str):
        return None

    base_match = BASE_URL_RE.search(config_text)
    if not base_match:
        return None

    model_match = MODEL_RE.search(config_text)
    model = model_match.group(1) if model_match else None
    return {
        "base_url": base_match.group(1),
        "api_key": api_key,
        "model": model,
        "auth_mode": "bearer",
    }


def _extract_claude_provider(settings: dict[str, Any]) -> dict[str, Any] | None:
    env = settings.get("env") or {}
    base_url = env.get("ANTHROPIC_BASE_URL")
    if not base_url:
        return None

    if env.get("ANTHROPIC_AUTH_TOKEN"):
        api_key = env["ANTHROPIC_AUTH_TOKEN"]
        auth_mode = "bearer"
    elif env.get("ANTHROPIC_API_KEY"):
        api_key = env["ANTHROPIC_API_KEY"]
        auth_mode = "x-api-key"
    else:
        return None

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": None,
        "auth_mode": auth_mode,
    }


def import_from_cc_switch(
    data: dict[str, Any],
    db_path: Path,
) -> dict[str, int]:
    if not db_path.exists():
        raise FileNotFoundError(f"cc-switch db not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select id, app_type, name, settings_config, is_current
        from providers
        where app_type in ('codex', 'claude')
        order by app_type, coalesce(sort_index, 999999), name
        """
    ).fetchall()
    conn.close()

    imported = {app: 0 for app in APP_CHOICES}
    skipped = {app: 0 for app in APP_CHOICES}

    for row in rows:
        app = row["app_type"]
        settings = json.loads(row["settings_config"])
        if app == "codex":
            extracted = _extract_codex_provider(settings)
        else:
            extracted = _extract_claude_provider(settings)

        if not extracted:
            skipped[app] += 1
            continue

        provider = {
            "name": row["name"],
            **extracted,
        }
        upsert_provider(
            data,
            app,
            row["id"],
            provider,
            set_current=bool(row["is_current"]),
        )
        imported[app] += 1

    return {
        "codex_imported": imported["codex"],
        "claude_imported": imported["claude"],
        "codex_skipped": skipped["codex"],
        "claude_skipped": skipped["claude"],
    }
