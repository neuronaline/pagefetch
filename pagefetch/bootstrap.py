"""Explicit optional-dependency checks.

PageFetch never installs packages or downloads browser binaries at import or
startup time. Applications keep control of their environment and can use this
module to validate optional features before starting work.
"""

from __future__ import annotations

import importlib.util


class RuntimeBootstrapError(RuntimeError):
    """Raised when an explicitly requested optional feature is unavailable."""


def ensure_runtime_requirements(
    *,
    needs_browser: bool = True,
    needs_pdf: bool = False,
) -> None:
    """Validate optional feature dependencies without changing the environment."""
    missing: list[str] = []
    if needs_browser and importlib.util.find_spec("camoufox") is None:
        missing.append("browser support: pip install 'pagefetch[browser]'")
    if needs_pdf and importlib.util.find_spec("pypdf") is None:
        missing.append("PDF support: pip install 'pagefetch[pdf]'")
    if missing:
        raise RuntimeBootstrapError("Missing optional dependencies: " + "; ".join(missing))


def install_camoufox_browser() -> None:
    """Explicitly download the Camoufox binary for callers that request it."""
    ensure_runtime_requirements(needs_browser=True)
    try:
        from camoufox.pkgman import camoufox_path

        camoufox_path(download_if_missing=True)
    except Exception as exc:
        raise RuntimeBootstrapError(
            "Camoufox could not install its browser runtime. "
            "Run 'python -m camoufox fetch' for diagnostic output."
        ) from exc
