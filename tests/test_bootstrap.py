from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from pagefetch import bootstrap


def test_optional_requirement_check_is_read_only_and_actionable(monkeypatch):
    monkeypatch.setattr(
        bootstrap.importlib.util,
        "find_spec",
        lambda module: None if module in {"camoufox", "pypdf"} else object(),
    )

    with pytest.raises(bootstrap.RuntimeBootstrapError, match=r"pagefetch\[browser\]"):
        bootstrap.ensure_runtime_requirements(needs_browser=True)
    with pytest.raises(bootstrap.RuntimeBootstrapError, match=r"pagefetch\[pdf\]"):
        bootstrap.ensure_runtime_requirements(needs_browser=False, needs_pdf=True)


def test_core_http_mode_has_no_optional_requirements(monkeypatch):
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda _module: None)
    bootstrap.ensure_runtime_requirements(needs_browser=False, needs_pdf=False)


def test_camoufox_browser_install_is_only_explicit(monkeypatch):
    import camoufox.pkgman

    calls: list[bool] = []
    monkeypatch.setattr(
        camoufox.pkgman,
        "camoufox_path",
        lambda *, download_if_missing: calls.append(download_if_missing) or "browser",
    )
    bootstrap.install_camoufox_browser()
    assert calls == [True]


def test_browser_and_pdf_dependencies_are_optional_extras():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    dependencies = "\n".join(project["dependencies"])
    assert "camoufox" not in dependencies
    assert "pypdf" not in dependencies
    assert any("camoufox" in item for item in project["optional-dependencies"]["browser"])
    assert any("pypdf" in item for item in project["optional-dependencies"]["pdf"])
