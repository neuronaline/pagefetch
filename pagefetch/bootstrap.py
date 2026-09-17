"""Explicit optional-dependency checks.

PageFetch auto-installs browser dependencies on first use. Set
``PAGEFETCH_AUTO_INSTALL=0`` to disable automatic installation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("pagefetch.bootstrap")

_BOOTSTRAP_LOCK = threading.Lock()
_INSTALL_TIMEOUT = 120
_FETCH_TIMEOUT = 300

# Single source of truth for the Camoufox pip spec used by the auto-install
# path. This MUST stay in lockstep with the ``browser`` extra defined in
# ``pyproject.toml``. ``pip install`` runs arbitrary PyPI-provided code in the
# host Python environment with full user privileges; the spec here is a trust
# boundary. Set ``PAGEFETCH_AUTO_INSTALL=0`` to disable this path entirely
# and require an explicit ``pip install pagefetch[browser]``.
_CAMOUFOX_SPEC = "camoufox>=0.5.6,<1"


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


def _auto_install_enabled() -> bool:
    """Return whether automatic browser installation is enabled."""
    return os.getenv("PAGEFETCH_AUTO_INSTALL", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _run_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run an installer command with deterministic text decoding."""
    return subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _install_browser_sync() -> None:
    """Install the Camoufox package and binary, raising actionable failures."""
    with _BOOTSTRAP_LOCK:
        if importlib.util.find_spec("camoufox") is None:
            logger.info("camoufox not found; installing Camoufox ...")
            try:
                result = _run_command(
                    [*_get_pip_command(), _CAMOUFOX_SPEC], timeout=_INSTALL_TIMEOUT
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeBootstrapError("Could not install Camoufox.") from exc
            if result.returncode:
                detail = result.stderr.strip()[-500:]
                raise RuntimeBootstrapError(
                    "Could not install Camoufox. " + (detail or "pip exited unsuccessfully.")
                )
            importlib.invalidate_caches()
            if importlib.util.find_spec("camoufox") is None:
                raise RuntimeBootstrapError("Camoufox is not importable after installation.")

        if _has_camoufox_binary():
            return
        logger.info("Camoufox browser binary not found; downloading ...")
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result = _run_command(
                    [sys.executable, "-m", "camoufox", "fetch"], timeout=_FETCH_TIMEOUT
                )
                if result.returncode == 0 and _has_camoufox_binary():
                    return
                detail = result.stderr.strip()[-500:]
                last_error = RuntimeBootstrapError(detail or "camoufox fetch exited unsuccessfully.")
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
            if attempt == 0:
                logger.warning("Camoufox browser download failed; retrying once.")
        raise RuntimeBootstrapError(
            "Camoufox browser runtime could not be installed. "
            "Run 'python -m camoufox fetch' for diagnostic output."
        ) from last_error


def auto_bootstrap_browser() -> bool:
    """Install the Camoufox package and browser binary when they are missing.

    Returns ``True`` when the browser is ready to use.  Safe to call
    repeatedly — subsequent calls are no-ops.
    """
    if not _auto_install_enabled():
        return _has_camoufox_binary()
    try:
        _install_browser_sync()
    except RuntimeBootstrapError as exc:
        logger.warning("Camoufox bootstrap failed: %s", exc)
        return False
    return True


async def bootstrap_browser() -> None:
    """Ensure browser dependencies without blocking the event loop."""
    if not _auto_install_enabled():
        if not _has_camoufox_binary():
            raise RuntimeBootstrapError(
                "Automatic browser installation is disabled by PAGEFETCH_AUTO_INSTALL. "
                "Install pagefetch[browser] and run 'python -m camoufox fetch'."
            )
        return
    await asyncio.to_thread(_install_browser_sync)
