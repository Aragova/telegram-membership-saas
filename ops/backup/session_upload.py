#!/usr/bin/env python3
"""VM-side encrypted GCS uploader using IAM-protected Cloud Run sessions.

Upload URLs are bearer secrets. Keep them exclusively in process memory and
never print them, pass them as CLI arguments, or include them in logs.
"""
import argparse
import http.client
import json
import os
import re
import stat
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PARTS = {"membership.dump.age", "pg-globals.sql.age", "manifest.sha256"}
# The root-owned opt-in backup script creates only directories with this prefix.
BACKUP_ROOT = Path("/var/backups/vimi-pay")
WORKDIR_RE = re.compile(r"\.gcs-upload\.[A-Za-z0-9]{8}")
SESSION_PATH_RE = re.compile(
    r"/upload/storage/v1/b/[a-z0-9][a-z0-9._-]{2,61}[a-z0-9]/o"
)
PREFIX_RE = re.compile(
    r"^\d{8}T\d{6}Z-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"


class _NoBrokerRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the Cloud Run IAM bearer token to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_private_broker(req: urllib.request.Request, *, timeout: int):
    return urllib.request.build_opener(_NoBrokerRedirect()).open(req, timeout=timeout)


def validate_service_url(raw: str) -> str:
    url = urllib.parse.urlsplit(raw)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".run.app")
        or url.username is not None
        or url.password is not None
        or url.port not in (None, 443)
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("expected the private Cloud Run service HTTPS URL")
    return raw.rstrip("/")


def validate_session_url(raw: str) -> urllib.parse.SplitResult:
    url = urllib.parse.urlsplit(raw)
    if (
        url.scheme != "https"
        or url.hostname != "storage.googleapis.com"
        or url.port not in (None, 443)
        or url.username is not None
        or url.password is not None
        or SESSION_PATH_RE.fullmatch(url.path) is None
        or not urllib.parse.parse_qs(url.query).get("upload_id")
        or url.fragment
    ):
        raise ValueError("unexpected upload session origin")
    return url


def request_session(service_url: str, prefix: str, part: str, size: int) -> str:
    # Trust boundary: caller-controlled CLI/config URLs must never reach
    # Google metadata audience or the IAM-token-bearing request unvalidated.
    service_url = validate_service_url(service_url)
    if not PREFIX_RE.fullmatch(prefix) or part not in PARTS or size <= 0:
        raise ValueError("invalid upload request")
    audience = urllib.parse.quote(service_url, safe="")
    req = urllib.request.Request(
        f"{METADATA}?audience={audience}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        id_token = response.read().decode("ascii")
    body = json.dumps({"prefix": prefix, "part": part, "size": size}).encode()
    req = urllib.request.Request(
        service_url + "/session",
        data=body,
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # Cloud Run is the only service allowed to receive this identity token.
    # urllib's default redirect handler can otherwise copy Authorization.
    with _open_private_broker(req, timeout=40) as response:
        session_url = json.load(response)["upload_url"]
    validate_session_url(session_url)
    return session_url


def checked_backup_file(path: str, part: str) -> Path:
    """Only upload files created by our root-owned encrypted backup run.

    The caller is a local privileged script, not an HTTP request. This
    additional boundary prevents a mistyped or injected CLI path from
    uploading arbitrary VM files under our trusted backup identity.
    """
    if part not in PARTS:
        raise ValueError("unknown backup part")
    raw = Path(path)
    # Reject symlinks in the complete source path, not only the leaf file.
    # resolve() would otherwise conceal symlinked parent work directories.
    if not raw.is_absolute() or any(node.is_symlink() for node in (raw, *raw.parents)):
        raise ValueError("backup path must be absolute and have no symlinks")
    resolved = raw.resolve(strict=True)
    if raw != resolved:
        raise ValueError("non-canonical backup path")
    root = BACKUP_ROOT.resolve(strict=True)
    if (
        resolved.name != part
        or resolved.parent.parent != root
        or not WORKDIR_RE.fullmatch(resolved.parent.name)
        or not resolved.is_file()
    ):
        raise ValueError("backup file is outside the active encrypted workdir")
    return resolved


def stream_put(session_url: str, path: str, *, part: str) -> None:
    # Validate BOTH the GCS receiver and the local file before network I/O.
    url = validate_session_url(session_url)
    validated = checked_backup_file(path, part)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(validated, flags)
    try:
        with os.fdopen(fd, "rb") as infile:
            fd = -1
            info = os.fstat(infile.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError("empty or non-regular encrypted backup file")
            conn = http.client.HTTPSConnection("storage.googleapis.com", timeout=120)
            try:
                target = url.path + ("?" + url.query if url.query else "")
                conn.putrequest("PUT", target)
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(info.st_size))
                conn.endheaders()
                while data := infile.read(1024 * 1024):
                    conn.send(data)
                response = conn.getresponse()
                if response.status not in (200, 201):
                    raise RuntimeError("GCS upload was not completed")
                response.read()
            finally:
                conn.close()
    finally:
        if fd != -1:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--part", choices=sorted(PARTS), required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    try:
        validated_file = checked_backup_file(args.file, args.part)
        size = validated_file.stat().st_size
        session_url = request_session(args.service_url, args.prefix, args.part, size)
        stream_put(session_url, str(validated_file), part=args.part)
    except Exception:
        # Never print exception strings: HTTP errors can contain bearer URIs.
        print("BACKUP_FAILED: encrypted upload did not complete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
