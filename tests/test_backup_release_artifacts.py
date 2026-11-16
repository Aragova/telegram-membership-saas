"""Contracts for the opt-in backup deployment artifacts (no cloud credentials)."""

import io
import json
import urllib.parse
import urllib.request

import pytest

from ops.backup.session_broker.server import object_name, start_gcs_session
from ops.backup.session_upload import (
    request_session,
    validate_service_url,
    validate_session_url,
)
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


def test_backup_broker_enforces_fixed_prefix_parts_and_object_sizes() -> None:
    prefix = "20261116T123000Z-12345678-1234-1234-1234-123456789abc"
    assert object_name(prefix, "membership.dump.age", 5000000) == (
        f"vimi-pay/postgres/{prefix}/membership.dump.age"
    )
    for bad in ("../etc", "membership.dump", "other/manifest.sha256"):
        with pytest.raises(ValueError):
            object_name(prefix, bad, 15)
    with pytest.raises(ValueError):
        object_name("../" + prefix, "membership.dump.age", 15)
    with pytest.raises(ValueError):
        object_name(prefix, "manifest.sha256", 9000)
    with pytest.raises(ValueError):
        object_name(prefix, "manifest.sha256", True)


def test_uploader_does_not_accept_arbitrary_endpoints() -> None:
    assert validate_service_url("https://broker-a12-ew.a.run.app") == (
        "https://broker-a12-ew.a.run.app"
    )
    with pytest.raises(ValueError):
        validate_service_url("http://broker-a12-ew.a.run.app")
    with pytest.raises(ValueError):
        validate_service_url("https://example.com")
    with pytest.raises(ValueError):
        validate_service_url("https://broker-a12-ew.a.run.app/evil")
    with pytest.raises(ValueError):
        validate_session_url("https://metadata.google.internal/computeMetadata")
    with pytest.raises(ValueError):
        validate_session_url("https://storage.googleapis.com.evil.tld/upload/x")
    assert validate_session_url(
        "https://storage.googleapis.com/upload/storage/v1/b/example/o?upload_id=secret"
    ).hostname == "storage.googleapis.com"


def test_backup_script_is_independent_of_vm_gcs_oauth_scopes() -> None:
    script = (ROOT / "ops/backup/vimi-pay-gcs-backup.sh").read_text()
    assert "VIMI_BACKUP_SESSION_SERVICE_URL" in script
    assert "session_upload.py" in script
    assert "gcloud auth print-access-token" not in script
    assert "storage.googleapis.com/upload" not in script


class FakeHTTPResponse(io.BytesIO):
    def __init__(self, content: bytes = b"", headers: dict | None = None):
        super().__init__(content)
        self.headers = headers or {}


def test_broker_uses_exact_create_only_resumable_upload(monkeypatch) -> None:
    called = []

    def fake_urlopen(req: urllib.request.Request, timeout: int):
        called.append(req)
        if len(called) == 1:
            assert req.full_url.startswith("http://metadata.google.internal/")
            return FakeHTTPResponse(b'{"access_token":"test-token"}')
        parsed = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        assert parsed.netloc == "storage.googleapis.com"
        assert query["ifGenerationMatch"] == ["0"]
        assert query["uploadType"] == ["resumable"]
        assert query["name"] == ["vimi-pay/postgres/example/membership.dump.age"]
        assert req.get_header("Authorization") == "Bearer test-token"
        assert req.get_header("X-upload-content-length") == "5000"
        return FakeHTTPResponse(
            headers={
                "Location": (
                    "https://storage.googleapis.com/upload/storage/v1/b/"
                    "backup-bucket/o?upload_id=opaque-session"
                )
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert start_gcs_session(
        "backup-bucket", "vimi-pay/postgres/example/membership.dump.age", 5000
    ).startswith("https://storage.googleapis.com/upload/")
    assert len(called) == 2


def test_vm_requests_id_token_for_real_broker_audience(monkeypatch) -> None:
    called = []
    broker = "https://vimi-backup-123-ew.a.run.app"
    session = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        "backup-bucket/o?upload_id=opaque-session"
    )

    def fake_urlopen(req: urllib.request.Request, timeout: int):
        called.append(req)
        if len(called) == 1:
            assert urllib.parse.parse_qs(
                urllib.parse.urlsplit(req.full_url).query
            )["audience"] == [broker]
            return FakeHTTPResponse(b"test-id-token")
        assert req.full_url == broker + "/session"
        assert req.get_header("Authorization") == "Bearer test-id-token"
        assert json.loads(req.data)["part"] == "membership.dump.age"
        return FakeHTTPResponse(json.dumps({"upload_url": session}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = request_session(
        broker,
        "20261116T123000Z-12345678-1234-1234-1234-123456789abc",
        "membership.dump.age",
        4096,
    )
    assert got == session
    assert len(called) == 2
