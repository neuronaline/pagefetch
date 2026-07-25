"""Runtime dependency bootstrap for direct source and CLI execution."""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable

_RUNTIME_REQUIREMENTS = (
    ("aiosqlite", "aiosqlite>=0.20"),
    ("bs4", "beautifulsoup4>=4.12"),
    ("camoufox", "camoufox>=0.4"),
    ("httpx", "httpx[http2,socks]>=0.27"),
    ("h2", "httpx[http2,socks]>=0.27"),
    ("socksio", "httpx[http2,socks]>=0.27"),
    ("lxml", "lxml>=5.2"),
    ("platformdirs", "platformdirs>=4.2"),
    ("pypdf", "pypdf>=5.0"),
    ("yaml", "pyyaml>=6.0"),
    ("tldextract", "tldextract>=5.1"),
)
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_bootstrap_complete = False
_camoufox_installed = False


class RuntimeBootstrapError(RuntimeError):
    """Raised when PageFetch cannot install a required runtime component."""


def auto_install_enabled() -> bool:
    """Return whether automatic startup installation is enabled."""
    value = os.getenv("PAGEFETCH_AUTO_INSTALL", "1").strip().lower()
    return value not in _FALSE_VALUES


def _missing_requirements(
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> list[str]:
    missing: list[str] = []
    for module, requirement in _RUNTIME_REQUIREMENTS:
        if find_spec(module) is None and requirement not in missing:
            missing.append(requirement)
    return missing


def install_python_requirements() -> None:
    """Install Python runtime packages that are absent from this interpreter."""
    missing = _missing_requirements()
    if not missing:
        return
    print(
        f"[pagefetch] Installing missing Python requirements: {', '.join(missing)}",
        file=sys.stderr,
    )
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *missing,
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeBootstrapError(
            "PageFetch could not install its Python requirements. "
            "Run 'python -m pip install .' manually or set PAGEFETCH_AUTO_INSTALL=0 "
            "to disable automatic installation."
        ) from exc
    importlib.invalidate_caches()
    unresolved = _missing_requirements()
    if unresolved:
        raise RuntimeBootstrapError(
            f"Requirements remain unavailable after installation: {', '.join(unresolved)}"
        )


def install_camoufox_browser() -> None:
    """Download or update the Camoufox browser binary when it is unavailable."""
    global _camoufox_installed
    if _camoufox_installed:
        return
    try:
        from camoufox.pkgman import camoufox_path

        try:
            camoufox_path(download_if_missing=False)
            _camoufox_installed = True
            return
        except Exception:
            print("[pagefetch] Installing the Camoufox browser runtime...", file=sys.stderr)
        camoufox_path(download_if_missing=True)
        _camoufox_installed = True
    except Exception as exc:
        raise RuntimeBootstrapError(
            "PageFetch could not install the Camoufox browser runtime. "
            "Run 'python -m camoufox fetch' manually or set PAGEFETCH_AUTO_INSTALL=0 "
            "to disable automatic installation."
        ) from exc


def ensure_runtime_requirements(*, needs_browser: bool = True) -> None:
    """Install all runtime requirements once at process startup.

    The caller (PageFetch.start) is responsible for ensuring this is
    called at most once per client lifecycle via its own asyncio.Lock.

    *needs_browser* gates Camoufox installation so that HTTP-only
    clients do not force an unnecessary browser download.
    """
    global _bootstrap_complete
    if not auto_install_enabled():
        return
    if _bootstrap_complete:
        if needs_browser:
            install_camoufox_browser()
        return
    install_python_requirements()
    if needs_browser:
        install_camoufox_browser()
    _bootstrap_complete = True
