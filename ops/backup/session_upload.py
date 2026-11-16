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
import sys
import urllib.parse
import urllib.request

PARTS = {"membership.dump.age", "pg-globals.sql.age", "manifest.sha256"}
PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"


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
        or not url.path.startswith("/upload/storage/v1/b/")
        or not url.query
    ):
        raise ValueError("unexpected upload session origin")
    return url


def request_session(service_url: str, prefix: str, part: str, size: int) -> str:
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
    with urllib.request.urlopen(req, timeout=40) as response:
        session_url = json.load(response)["upload_url"]
    validate_session_url(session_url)
    return session_url


def stream_put(session_url: str, path: str) -> None:
    url = validate_session_url(session_url)
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("empty upload file")
    conn = http.client.HTTPSConnection("storage.googleapis.com", timeout=120)
    try:
        target = url.path + ("?" + url.query if url.query else "")
        conn.putrequest("PUT", target)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(path, "rb") as infile:
            while data := infile.read(1024 * 1024):
                conn.send(data)
        response = conn.getresponse()
        if response.status not in (200, 201):
            raise RuntimeError("GCS upload was not completed")
        response.read()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--part", choices=sorted(PARTS), required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    try:
        service_url = validate_service_url(args.service_url)
        size = os.path.getsize(args.file)
        session_url = request_session(service_url, args.prefix, args.part, size)
        stream_put(session_url, args.file)
    except Exception:
        # Never print exception strings: HTTP errors can contain bearer URIs.
        print("BACKUP_FAILED: encrypted upload did not complete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
