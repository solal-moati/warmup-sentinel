"""Tiny .env loader — no python-dotenv dependency.

Reads KEY=VALUE lines from the repo-root .env (git-ignored). Values are never
logged; callers get them via get() and must not print them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

_cache: dict = {}


def load() -> dict:
    global _cache
    if _cache:
        return _cache
    env: dict = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip inline comments ("VALUE  # note") but not #-in-value
            value = re.split(r"\s+#", value, 1)[0]
            env[key.strip()] = value.strip()
    _cache = env
    return env


def get(key: str, required: bool = True) -> str:
    # Process env wins (GitHub Actions secrets), .env is the local fallback.
    value = os.environ.get(key, "") or load().get(key, "")
    if required and not value:
        raise SystemExit(
            f"Missing {key} (env var or {ENV_PATH}) — add it before running "
            f"this script.")
    return value
