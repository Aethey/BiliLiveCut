#!/usr/bin/env python3
"""Validate tracked JavaScript syntax and all frontend interaction suites."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERACTION_SUITES = (
    "scripts/check_frontend_interactions.mjs",
    "scripts/check_configuration_interactions.mjs",
    "scripts/check_recording_import_interactions.mjs",
)


class InlineScripts(HTMLParser):
    """Collect executable inline scripts while excluding external scripts and JSON."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[str, str]] = []
        self._active = False
        self._parts: list[str] = []
        self._input_type = "commonjs"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start collecting executable script text."""
        if tag != "script":
            return
        attributes = dict(attrs)
        script_type = attributes.get("type") or ""
        self._active = "src" not in attributes and script_type in (
            "",
            "module",
            "text/javascript",
            "application/javascript",
        )
        self._parts = []
        self._input_type = "module" if script_type == "module" else "commonjs"

    def handle_data(self, data: str) -> None:
        """Keep inline script contents intact."""
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Finish a script block."""
        if tag == "script" and self._active:
            self.scripts.append((self._input_type, "".join(self._parts)))
            self._active = False


def tracked_files(*patterns: str) -> list[str]:
    """Read existing tracked paths without splitting spaces or Unicode."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [path for path in result.stdout.split("\0") if path and (REPO_ROOT / path).is_file()]


def main() -> int:
    """Fail on missing Node.js, invalid syntax or any failing interaction suite."""
    node = shutil.which("node")
    if node is None:
        print("Node.js is required for frontend checks. Install Node.js 22 or newer and retry.")
        return 1

    files = tracked_files("*.js", "*.mjs")
    print(f"Checking JavaScript syntax ({len(files)} files)", flush=True)
    syntax_ok = True
    for path in files:
        result = subprocess.run([node, "--check", path], cwd=REPO_ROOT)
        syntax_ok &= result.returncode == 0
    for path in tracked_files("app/web/templates/*.html"):
        collector = InlineScripts()
        collector.feed((REPO_ROOT / path).read_text(encoding="utf-8"))
        for index, (input_type, source) in enumerate(collector.scripts, start=1):
            # 当前模板只在脚本中注入这两个整数；不执行模板或应用代码。
            source = source.replace("{{ candidate_id | int }}", "1").replace("{{ topic_id | int }}", "1")
            print(f"Checking {path} inline script #{index}", flush=True)
            result = subprocess.run(
                [node, f"--input-type={input_type}", "--check"],
                input=source,
                text=True,
                encoding="utf-8",
                cwd=REPO_ROOT,
            )
            syntax_ok &= result.returncode == 0
    if not syntax_ok:
        return 1

    passed = True
    for path in INTERACTION_SUITES:
        print(f"Running {path}", flush=True)
        result = subprocess.run([node, path], cwd=REPO_ROOT)
        passed &= result.returncode == 0
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
