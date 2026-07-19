"""Stable, credential-safe cache keys."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..utils.urls import normalize_url


def build_cache_key(
    url: str,
    *,
    mode: str,
    proxy: str,
    settings: dict[str, Any] | None = None,
) -> str:
    payload = {
        "url": normalize_url(url),
        "mode": mode,
        "proxy": proxy,
        "processing_version": 1,
        "settings": settings or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

