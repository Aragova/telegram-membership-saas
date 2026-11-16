"""Private Cloud Run broker: create-only GCS resumable upload sessions.

Authentication is enforced by Cloud Run IAM (run.invoker) before this server.
Never expose this service publicly. Only encrypted backup objects are allowed.
"""
import http.server
import json
import os
import re
import urllib.parse
import urllib.request

PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PARTS = frozenset({"membership.dump.age", "pg-globals.sql.age", "manifest.sha256"})
META_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def object_name(prefix: str, part: str, size: int) -> str:
    """Validate every client-controlled path component, fail closed."""
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise ValueError("invalid backup prefix")
    if part not in PARTS:
        raise ValueError("invalid backup part")
    if type(size) is not int or not (1 <= size <= 2 * 1024**4):
        raise ValueError("invalid backup object size")
    if part == "manifest.sha256" and size > 4096:
        raise ValueError("invalid manifest size")
    return f"vimi-pay/postgres/{prefix}/{part}"


def start_gcs_session(bucket: str, name: str, size: int) -> str:
    """Use Cloud Run workload identity; token and session URI never logged."""
    metadata_req = urllib.request.Request(
        META_TOKEN, headers={"Metadata-Flavor": "Google"}
    )
    with urllib.request.urlopen(metadata_req, timeout=8) as response:
        token = json.load(response)["access_token"]

    params = urllib.parse.urlencode(
        {"uploadType": "resumable", "name": name, "ifGenerationMatch": "0"}
    )
    req = urllib.request.Request(
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{params}",
        data=b"",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(size),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        session_url = response.headers["Location"]
    parsed = urllib.parse.urlsplit(session_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "storage.googleapis.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith(f"/upload/storage/v1/b/{bucket}/o")
    ):
        raise ValueError("unsafe GCS upload session origin")
    return session_url


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        # Never log authorization, request body or resumable bearer URL.
        pass

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/session":
            self._send(404, {"error": "not found"})
            return
        bucket = os.environ.get("VIMI_BACKUP_GCS_BUCKET", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,61}[a-z0-9]", bucket):
            self._send(503, {"error": "service unavailable"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 512:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"prefix", "part", "size"}:
                raise ValueError("invalid request")
            name = object_name(payload["prefix"], payload["part"], payload["size"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._send(400, {"error": "invalid request"})
            return
        try:
            session = start_gcs_session(bucket, name, payload["size"])
        except Exception:
            # Fail closed. No traceback: session URI and Google tokens are secrets.
            self._send(502, {"error": "upload session unavailable"})
            return
        self._send(200, {"upload_url": session})

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
