"""Stage-3 DNS/HTTPS examples must not accidentally activate a public API.

This is a static guard, not a claim that Nginx/DNS/TLS is deployed or healthy.
"""

from pathlib import Path

INGRESS = Path(__file__).resolve().parents[1] / "ops" / "ingress"


def test_only_new_pay_api_host_is_in_templates():
    for name in ("pay-api.http.nginx.conf.example", "pay-api.https.nginx.conf.example"):
        content = (INGRESS / name).read_text(encoding="utf-8")
        assert "server_name pay-api.usevimi.ru;" in content
        assert "server_name usevimi.ru;" not in content
        assert "server_name api.usevimi.ru;" not in content
        assert "server_name staging.usevimi.ru;" not in content
        assert "listen 8000" not in content
        assert "proxy_pass" not in content
        assert "127.0.0.1:8000" not in content
        assert "listen [::]" not in content


def test_closed_ingress_does_not_pretend_api_is_live():
    http = (INGRESS / "pay-api.http.nginx.conf.example").read_text(encoding="utf-8")
    https = (INGRESS / "pay-api.https.nginx.conf.example").read_text(encoding="utf-8")
    assert "return 404;" in http
    assert "ssl_certificate" not in http
    assert "listen 443 ssl;" in https
    assert "return 503" in https
    assert "api_not_deployed" in https
    assert "return 404;" in https
    assert "/.well-known/acme-challenge/" in http
    assert "/.well-known/acme-challenge/" in https

def test_certbot_renew_hook_is_scoped_and_validates_before_reload():
    hook = (INGRESS / "vimi-pay-certbot-deploy-hook.sh.example").read_text(
        encoding="utf-8"
    )
    assert "RENEWED_LINEAGE" in hook
    assert "/etc/letsencrypt/live/pay-api.usevimi.ru" in hook
    assert "/usr/sbin/nginx -t" in hook
    assert "/bin/systemctl reload nginx" in hook
    assert hook.index("/usr/sbin/nginx -t") < hook.index("/bin/systemctl reload nginx")
