"""Summarize static analysis results without exposing source snippets or secrets."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path("quality-reports")
MARKER = "<!-- vimi-quality-report -->"


def load_json(filename: str, fallback: Any) -> Any:
    try:
        return json.loads((ROOT / filename).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return fallback


def read_text(filename: str) -> str:
    try:
        return (ROOT / filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def main() -> None:
    bandit = load_json("bandit.json", {})
    findings = bandit.get("results", []) if isinstance(bandit, dict) else []
    severities = Counter(
        item.get("issue_severity", "UNKNOWN") for item in findings
        if isinstance(item, dict)
    )
    top_bandit = sorted(
        (item for item in findings if isinstance(item, dict)),
        key=lambda item: (
            {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(
                item.get("issue_severity", "UNKNOWN"), 3
            ),
            item.get("filename", ""),
            item.get("line_number", 0),
        ),
    )[:8]

    complexity = load_json("radon.json", {})
    blocks = [
        (filename, block)
        for filename, items in complexity.items()
        if isinstance(items, list)
        for block in items
        if isinstance(block, dict)
    ] if isinstance(complexity, dict) else []
    # Radon emits {"error": "..."} per unparseable file. Never report
    # a partial scan as though that file contained no complex functions.
    radon_errors = sorted(
        filename
        for filename, items in complexity.items()
        if isinstance(items, dict) and "error" in items
    ) if isinstance(complexity, dict) else []
    ranks = Counter(block.get("rank", "?") for _, block in blocks)
    top_complex = sorted(
        blocks,
        key=lambda pair: pair[1].get("complexity", 0),
        reverse=True,
    )[:8]

    statuses = load_json("statuses.json", {})
    mypy_count = sum(" error:" in line for line in read_text("mypy.txt").splitlines())
    ruff_diagnostics = load_json("ruff.json", [])
    ruff_count = len(ruff_diagnostics) if isinstance(ruff_diagnostics, list) else 0

    lines = [
        MARKER,
        "## 🔎 ViMi Pay · автоматический аудит PR",
        "",
        "| Проверка | Результат |",
        "|---|---|",
        f"| Ruff | {statuses.get('ruff', 'нет данных')}; замечаний: **{ruff_count}** |",
        f"| mypy `--strict` | {statuses.get('mypy', 'нет данных')}; "
        f"ошибок типов: **{mypy_count}** |",
        f"| Bandit | {statuses.get('bandit', 'нет данных')}; "
        f"🔴 HIGH: **{severities['HIGH']}**, MEDIUM: **{severities['MEDIUM']}**, "
        f"LOW: **{severities['LOW']}** |",
        f"| Radon | {statuses.get('radon', 'нет данных')}; "
        f"B: **{ranks['B']}**, C: **{ranks['C']}**, "
        f"D: **{ranks['D']}**, E: **{ranks['E']}**, F: **{ranks['F']}**; "
        f"ошибок разбора файлов: **{len(radon_errors)}**"
        f"{' (неполный анализ)' if radon_errors else ''} |",
        f"| code2flow → PNG | {statuses.get('code2flow', 'нет данных')} |",
        "",
        "### Находки Bandit (первые 8 по серьёзности)",
    ]
    if top_bandit:
        for finding in top_bandit:
            filename = finding.get("filename", "?")
            number = finding.get("line_number", "?")
            test_id = finding.get("test_id", "?")
            severity = finding.get("issue_severity", "?")
            lines.append(f"- **{severity}** {test_id}: `{filename}:{number}`")
    else:
        lines.append("- Находок нет либо JSON-отчёт не создан.")

    if radon_errors:
        lines.extend([
            "",
            "### ⚠️ Radon: файлы, не прошедшие анализ",
            *[f"- `{filename}`" for filename in radon_errors[:12]],
        ])
        if len(radon_errors) > 12:
            lines.append(f"- и ещё **{len(radon_errors) - 12}** файлов.")

    lines.extend(["", "### Самые сложные функции (Radon)"])
    if top_complex:
        for filename, block in top_complex:
            name = block.get("name", "?")
            line_number = block.get("lineno", "?")
            lines.append(
                f"- **{block.get('rank', '?')} / {block.get('complexity', '?')}** "
                f"`{filename}:{line_number}` · `{name}`"
            )
    else:
        lines.append("- Функций класса B–F не обнаружено либо JSON-отчёт не создан.")

    lines.extend([
        "",
        "Полные логи доступны в артефакте **vimi-quality-reports** этого Actions run.",
        "Аудит показывает существующие проблемы всего проекта; mypy и Bandit "
        "пока не являются блокирующими проверками. Ruff уже блокирует ошибки "
        "через основной CI. CodeRabbit проверяет изменения отдельно после "
        "подключения GitHub App.",
    ])
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "comment.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
