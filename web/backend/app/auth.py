"""Sign-in for the dashboard.

Users and password hashes are in a file (LAP_AUTH_FILE, default LAP/deploy/auth/users, one `name:hash` per line; manage it
with deploy/users.sh). A successful sign-in sets a signed, HttpOnly session cookie that lasts SESSION_HOURS (12 by default).
Every request except the sign-in page itself needs that cookie: pages redirect to /login, /api calls answer 401.

Sign-in is ON unless LAP_AUTH=off (local development only). If the users file is missing or empty nobody can sign in,
so a wrong setup locks the dashboard instead of opening it.

Password hashes are scrypt (standard library): scrypt$N$r$p$salt_hex$hash_hex.
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from . import config

ENABLED = os.environ.get("LAP_AUTH", "on").strip().lower() not in ("off", "0", "false", "no")
USERS_FILE = Path(os.environ.get("LAP_AUTH_FILE", str(config.WEB_ROOT.parent / "deploy" / "auth" / "users")))
SESSION_SECONDS = int(float(os.environ.get("SESSION_HOURS", "12")) * 3600)
COOKIE = "lap_session"
SECRET_FILE = config.WEB_ROOT / "data" / ".session_secret"

_N, _R, _P = 2**14, 8, 1  # scrypt cost (about 50 ms and 16 MB per check)
MAX_FAILURES = 8  # failed sign-ins from one address ...
FAILURE_WINDOW = 300  # ... within this many seconds block that address until the window has passed


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def _matches(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected) // 2)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _users() -> dict[str, str]:
    """name -> stored hash, read on every call so a change made with users.sh applies at once."""
    try:
        lines = USERS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    users: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            name, _, stored = line.partition(":")
            users[name] = stored
    return users


_DUMMY = hash_password("not-a-real-password")  # an unknown user takes as long to check as a known one


def verify(username: str, password: str) -> bool:
    stored = _users().get(username)
    ok = _matches(password, stored or _DUMMY)
    return stored is not None and ok


# ---------------------------------------------------------------- session cookie

_secret_cache: bytes | None = None
_secret_lock = threading.Lock()


def _secret() -> bytes:
    """The key that signs session cookies: made once, kept in web/data/.session_secret (so a restart keeps sessions)."""
    global _secret_cache
    with _secret_lock:
        if _secret_cache is None:
            try:
                _secret_cache = SECRET_FILE.read_bytes()
            except OSError:
                _secret_cache = b""
            if len(_secret_cache) < 32:
                _secret_cache = secrets.token_bytes(32)
                try:
                    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(_secret_cache)
                except OSError:
                    pass  # sessions then last until the next restart
        return _secret_cache


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_SECONDS}".encode()
    return f"{_b64(payload)}.{_b64(hmac.new(_secret(), payload, hashlib.sha256).digest())}"


def user_from_token(token: str) -> str | None:
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _unb64(payload_b64)
        if not hmac.compare_digest(_unb64(sig_b64), hmac.new(_secret(), payload, hashlib.sha256).digest()):
            return None
        user, _, expires = payload.decode().rpartition("|")
        if int(expires) < time.time() or user not in _users():  # a user that was removed loses access at once
            return None
        return user
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def user_from_request(request: Request) -> str | None:
    return user_from_token(request.cookies.get(COOKIE, ""))


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


# ---------------------------------------------------------------- slowing down password guessing

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def blocked(ip: str) -> bool:
    now = time.time()
    with _failures_lock:
        recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
        _failures[ip] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    with _failures_lock:
        _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)


def safe_next(target: str | None) -> str:
    """Where to go after signing in: only a path on this site, never another address."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\", "/login")):
        return target
    return "/"


# ---------------------------------------------------------------- the guard in front of the whole app

PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.svg"}
_LOOPBACK = {"127.0.0.1", "::1"}


class SignInGuard:
    """ASGI middleware: no valid session cookie, no access (streams uploads and downloads untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ENABLED:
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope["path"]
        peer = scope["client"][0] if scope.get("client") else ""
        # the container's own health check calls the app from inside (loopback); nobody else gets this shortcut
        if path in PUBLIC_PATHS or (path == "/api/health" and peer in _LOOPBACK):
            return await self.app(scope, receive, send)
        user = user_from_request(request)
        if user is None:
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "Sign in required."}, status_code=401)
            else:
                target = path + (("?" + request.url.query) if request.url.query else "")
                response = RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
            return await response(scope, receive, send)
        scope["state"] = {**scope.get("state", {}), "user": user}
        await self.app(scope, receive, send)
