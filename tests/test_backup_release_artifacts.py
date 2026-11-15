"""Contracts for the opt-in backup deployment artifacts (no cloud credentials)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gcloud_lifecycle_file_uses_root_rule_shape() -> None:
    policy = json.loads(
        (ROOT / "ops/backup/gcs-lifecycle-90days.json").read_text(encoding="utf-8")
    )
    # 'gcloud storage buckets update --lifecycle-file' does NOT accept the
    # outer {"lifecycle": ...} JSON API wrapper.
    assert set(policy) == {"rule"}
    assert policy["rule"] == [
        {"action": {"type": "Delete"}, "condition": {"age": 90}}
    ]


def test_timer_requires_explicit_install_and_service_is_opt_in() -> None:
    base = ROOT / "ops/backup"
    timer = (base / "vimi-pay-gcs-backup.timer").read_text(encoding="utf-8")
    service = (base / "vimi-pay-gcs-backup.service").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer
    assert "ConditionPathExists=/etc/vimi-pay/backup.env" in service
    assert "ExecStart=/usr/local/sbin/vimi-pay-gcs-backup" in service
