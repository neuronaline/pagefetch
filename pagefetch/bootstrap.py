"""Explicit optional-dependency checks.

PageFetch auto-installs browser dependencies on first use.  Set
``auto_bootstrap=False`` on :class:`PageFetch` to disable automatic
installation.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("pagefetch.bootstrap")


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


def _get_pip_command() -> list[str]:
    """Return a pip invocation targeting the currently running interpreter."""
    return [sys.executable, "-m", "pip", "install", "--quiet"]


def _has_camoufox_binary() -> bool:
    """Return True when the Camoufox browser binary is already on disk."""
    try:
        from camoufox.pkgman import camoufox_path

        path = camoufox_path(download_if_missing=False)
        return path is not None and Path(path).exists()
    except Exception:
        return False


def auto_bootstrap_browser() -> bool:
    """Install the Camoufox package and browser binary when they are missing.

    Returns ``True`` when the browser is ready to use.  Safe to call
    repeatedly — subsequent calls are no-ops.
    """
    # ── package ──────────────────────────────────────────────────────
    if importlib.util.find_spec("camoufox") is None:
        logger.info("camoufox not found; installing pagefetch[browser] ...")
        try:
            result = subprocess.run(
                [*_get_pip_command(), "pagefetch[browser]"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(
                    "pip install pagefetch[browser] failed (exit %d): %s",
                    result.returncode,
                    result.stderr.strip()[-500:],
                )
                return False
        except Exception as exc:
            logger.warning("pip install pagefetch[browser] failed: %s", exc)
            return False
        importlib.invalidate_caches()
        if importlib.util.find_spec("camoufox") is None:
            logger.warning("camoufox still not importable after pip install")
            return False

    # ── browser binary ───────────────────────────────────────────────
    if not _has_camoufox_binary():
        logger.info("Camoufox browser binary not found; downloading ...")
        try:
            from camoufox.pkgman import camoufox_path

            camoufox_path(download_if_missing=True)
        except Exception as exc:
            logger.warning("Camoufox browser binary download failed: %s", exc)
            return False

    return True


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
