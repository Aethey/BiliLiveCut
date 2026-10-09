"""Quality gates must reject real failures and retain reproducible tool versions."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import check_frontend, ci_gate


def test_hook_tool_versions_match_development_and_ci() -> None:
    root = ci_gate.REPO_ROOT
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["optional-dependencies"]["dev"]
    ruff_repo = next(repo for repo in config["repos"] if repo["repo"].endswith("/ruff-pre-commit"))
    assert f"ruff=={ruff_repo['rev'].removeprefix('v')}" in dependencies
    version = config["minimum_pre_commit_version"]
    assert f"pre-commit=={version}" in dependencies
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert any(f"pre-commit=={version}" in step.get("run", "") for step in workflow["jobs"]["lint"]["steps"])


def test_local_gate_rejects_real_pytest_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_skipped.py").write_text(
        "import pytest\ndef test_missing_dependency():\n    pytest.skip('missing dependency')\n", encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text((ci_gate.REPO_ROOT / "conftest.py").read_text(), encoding="utf-8")
    monkeypatch.setattr(ci_gate, "REPO_ROOT", tmp_path)
    assert ci_gate._pytest("tests/") is False


def test_payload_failure_cannot_validate_old_portable_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suites: list[str] = []
    monkeypatch.setattr(ci_gate, "_run", lambda command, **kwargs: "packaging/portable/build_payload.py" not in command)
    monkeypatch.setattr(ci_gate, "_pytest", lambda path, **kwargs: suites.append(path) or True)
    monkeypatch.setattr(sys, "argv", ["ci_gate.py", "--skip-audit"])
    assert ci_gate.main() == 1
    assert suites == ["tests/"]
    assert "refusing to test stale artifacts" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--skip-portable", "--skip-coverage", "--skip-audit"])
def test_partial_gate_does_not_report_full_ci_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.setattr(ci_gate, "_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(ci_gate, "_pytest", lambda *args, **kwargs: True)
    monkeypatch.setattr(sys, "argv", ["ci_gate.py", flag])
    assert ci_gate.main() == 0
    output = capsys.readouterr().out
    assert "full CI gate not evaluated" in output
    assert "ALL CHECKS PASSED" not in output


@pytest.fixture
def frontend_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(check_frontend, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_frontend, "INTERACTION_SUITES", ())
    assert shutil.which("node") is not None, "Node.js is required for frontend gate tests"
    return tmp_path


@pytest.mark.parametrize("inline", [False, True], ids=["javascript-file", "inline-template"])
def test_frontend_gate_rejects_actual_javascript_syntax_error(frontend_repository: Path, inline: bool) -> None:
    if inline:
        path = frontend_repository / "app/web/templates/broken.html"
        source = "<script>const value = ;</script>"
    else:
        path = frontend_repository / "broken script 文件.js"
        source = "const value = ;"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    subprocess.run(["git", "add", "--", str(path)], cwd=frontend_repository, check=True)
    assert check_frontend.main() == 1


def test_frontend_gate_propagates_interaction_failure(
    frontend_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (frontend_repository / "failure.mjs").write_text("process.exit(7);", encoding="utf-8")
    (frontend_repository / "next.mjs").write_text(
        "import fs from 'node:fs'; fs.writeFileSync('next-ran', 'yes');", encoding="utf-8"
    )
    subprocess.run(["git", "add", "--", "failure.mjs", "next.mjs"], cwd=frontend_repository, check=True)
    monkeypatch.setattr(check_frontend, "INTERACTION_SUITES", ("failure.mjs", "next.mjs"))
    assert check_frontend.main() == 1
    assert (frontend_repository / "next-ran").read_text() == "yes"


def test_frontend_gate_requires_node(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_frontend.shutil, "which", lambda name: None)
    assert check_frontend.main() == 1
    assert "Node.js is required" in capsys.readouterr().out
