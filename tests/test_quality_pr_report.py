"""Regression tests for the GitHub PR quality summary."""

from __future__ import annotations

import json
from pathlib import Path

from ops.quality import pr_report


def _write_json(root: Path, filename: str, payload: object) -> None:
    (root / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_radon_parse_failures_are_disclosed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pr_report, "ROOT", tmp_path)
    _write_json(
        tmp_path,
        "statuses.json",
        {"radon": "success", "ruff": "success", "mypy": "success"},
    )
    _write_json(
        tmp_path,
        "radon.json",
        {
            "good.py": [{"name": "complex_function", "lineno": 10, "rank": "D", "complexity": 24}],
            "broken.py": {"error": "SyntaxError: invalid syntax"},
            "empty.py": [],
        },
    )
    _write_json(
        tmp_path,
        "ruff.json",
        [{"code": "F401", "filename": "example.py"}],
    )
    pr_report.main()

    result = (tmp_path / "comment.md").read_text(encoding="utf-8")
    assert "замечаний: **1**" in result
    assert "D: **1**" in result
    assert "ошибок разбора файлов: **1** (неполный анализ)" in result
    assert "### ⚠️ Radon: файлы, не прошедшие анализ" in result
    assert "`broken.py`" in result


def test_radon_valid_files_are_not_marked_incomplete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pr_report, "ROOT", tmp_path)
    _write_json(tmp_path, "radon.json", {"ok.py": []})
    _write_json(tmp_path, "statuses.json", {"radon": "success"})
    pr_report.main()

    result = (tmp_path / "comment.md").read_text(encoding="utf-8")
    assert "ошибок разбора файлов: **0**" in result
    assert "неполный анализ" not in result
