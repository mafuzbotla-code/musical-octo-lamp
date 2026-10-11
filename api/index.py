
import json
import os
import urllib.request
from http.client import responses as _REASONS
from urllib.error import HTTPError
from urllib.parse import parse_qsl

# ---------------- config ----------------
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://100067.connect.garena.com").rstrip("/")
try:
    UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "8") or 8)
except ValueError:
    UPSTREAM_TIMEOUT = 8.0


_DROP_EXACT = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
    "accept-encoding",  
}


def _drop(name: str) -> bool:
    n = name.lower()
    return n in _DROP_EXACT or n.startswith("x-vercel-") or n.startswith("x-forwarded-")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect follow না করে upstream-এর response হুবহু ফেরত দাও।
    redirect_request → None হলে urllib HTTPError(code) raise করে,
    যা নিচে status + headers + body সহ verbatim forward হয়।"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)

_DROP_RESP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
def _out_headers(resp_headers, body_len):
    out = []
    seen_ct = False
    for k, v in (resp_headers.items() if resp_headers else []):
        kl = k.lower()
        if kl in _DROP_RESP_HEADERS or kl == "content-length":
            continue
        if kl == "content-type":
            seen_ct = True
        out.append((k, v))
    if not seen_ct:
        out.append(("Content-Type", "application/octet-stream"))
    out.append(("Content-Length", str(body_len)))
    return out


def _mask(v, keep=4):
    v = str(v or "")
    return v if len(v) <= keep else v[:keep] + "***"


def _json_response(start_response, status, obj):
    body = json.dumps(obj).encode("utf-8")
    start_response(
        "%d %s" % (status, _REASONS.get(status, "OK")),
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


def app(environ, start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"

    if path == "/api/index" or path.startswith("/api/index/"):
        path = path[len("/api/index"):] or "/"
    query = environ.get("QUERY_STRING") or ""

    if path == "/health" and method == "GET":
        return _json_response(
            start_response, 200,
            {"ok": True, "proxy": "fuhx-garena-proxy", "upstream": UPSTREAM_BASE},
        )

    # request body পড়ি
    try:
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    body = environ["wsgi.input"].read(content_length) if content_length > 0 else None

    # log-এর জন্য uid — password কখনো log হবে না
    uid = "-"
    if (environ.get("CONTENT_TYPE") or "").startswith("application/x-www-form-urlencoded") and body:
        try:
            uid = _mask(dict(parse_qsl(body.decode("utf-8", "replace"))).get("uid", "-"))
        except Exception:
            pass
    print("[proxy] %s %s uid=%s" % (method, path, uid), flush=True)

    url = UPSTREAM_BASE + path + ("?" + query if query else "")

    fwd_headers = {}
    for key, val in environ.items():
        if key.startswith("HTTP_") and val:
            name = key[5:].replace("_", "-")
            if not _drop(name):
                fwd_headers["-".join(p[:1].upper() + p[1:].lower() for p in name.split("-"))] = val
    content_type = environ.get("CONTENT_TYPE")
    if content_type:
        fwd_headers["Content-Type"] = content_type

    req = urllib.request.Request(url, data=body, headers=fwd_headers, method=method)
    try:
        with _opener.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
            status = resp.status
            resp_body = resp.read()
            out_hdrs = _out_headers(resp.headers, len(resp_body))
    except HTTPError as e:
        # Garena-র error/redirect response-ও হুবহু ফেরত যাবে (400/429/302/...)
        status = e.code
        try:
            resp_body = e.read()
        except Exception:
            resp_body = b""
        out_hdrs = _out_headers(e.headers, len(resp_body)) if e.headers else [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(resp_body))),
        ]
    except Exception as e:
        print("[proxy] upstream fail: %r" % (e,), flush=True)
        return _json_response(
            start_response, 502,
            {"ok": False, "error": "upstream unreachable", "detail": repr(e)[:200]},
        )

    print("[proxy] %s %s uid=%s -> upstream %s" % (method, path, uid, status), flush=True)
    start_response("%d %s" % (status, _REASONS.get(status, "OK")), out_hdrs)
    return [resp_body]
