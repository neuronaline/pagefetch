from __future__ import annotations

import subprocess

import pytest

from pagefetch import bootstrap


def test_missing_requirements_are_deduplicated():
    missing = {"aiosqlite", "httpx", "h2", "socksio"}

    def find_spec(module: str):
        return None if module in missing else object()

    assert bootstrap._missing_requirements(find_spec) == [
        "aiosqlite>=0.20",
        "httpx[http2,socks]>=0.27",
    ]


def test_python_requirements_install_uses_current_interpreter(monkeypatch):
    checks = iter([["aiosqlite>=0.20"], []])
    commands: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "_missing_requirements", lambda: next(checks))
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda command, check: commands.append(command),
    )
    bootstrap.install_python_requirements()
    assert commands[0][:4] == [
        bootstrap.sys.executable,
        "-m",
        "pip",
        "install",
    ]
    assert commands[0][-1] == "aiosqlite>=0.20"


def test_python_requirement_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(bootstrap, "_missing_requirements", lambda: ["missing-package"])

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    with pytest.raises(bootstrap.RuntimeBootstrapError, match="pip install"):
        bootstrap.install_python_requirements()


def test_auto_install_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PAGEFETCH_AUTO_INSTALL", "off")
    assert bootstrap.auto_install_enabled() is False


def test_camoufox_browser_is_downloaded_only_when_missing(monkeypatch):
    import camoufox.pkgman

    calls: list[bool] = []

    def path(*, download_if_missing: bool):
        calls.append(download_if_missing)
        if not download_if_missing:
            raise FileNotFoundError
        return "browser"

    monkeypatch.setattr(camoufox.pkgman, "camoufox_path", path)
    bootstrap.install_camoufox_browser()
    assert calls == [False, True]
