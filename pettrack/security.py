"""Login, CSRF, rate limits, security headers."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict

from flask import abort, current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

PUBLIC_ENDPOINTS = {
    "static", "main.login", "main.public_view", "main.public_found", "main.photo",
    "main.manifest", "main.service_worker", "main.offline",
    "api.health", "api.pet_bundle", "api.sync_push", "api.sync_pull",
}

class RateLimiter:
    """In-memory sliding window. One per app instance."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits: dict[str, list[float]] = defaultdict(list)

    def hit(self, key: str, limit: int, window: int) -> bool:
        now = time.time()
        with self.lock:
            hits = [t for t in self.hits[key] if now - t < window]
            over = len(hits) >= limit
            if not over:
                hits.append(now)
            self.hits[key] = hits
            return over

    def peek(self, key: str, limit: int, window: int) -> bool:
        now = time.time()
        with self.lock:
            return len([t for t in self.hits[key] if now - t < window]) >= limit


def rate_hit(key: str, limit: int, window: int) -> bool:
    """Record a hit; return True if over the limit."""
    return current_app.extensions["pt_rl"].hit(key, limit, window)


def rate_peek(key: str, limit: int, window: int) -> bool:
    return current_app.extensions["pt_rl"].peek(key, limit, window)


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_password(cfg, attempt: str) -> bool:
    if cfg.admin_password_hash:
        return check_password_hash(cfg.admin_password_hash, attempt)
    return hmac.compare_digest(attempt.encode(), cfg.admin_password.encode())


def is_authed(cfg) -> bool:
    return (not cfg.require_login) or bool(session.get("auth"))


def install(app) -> None:
    cfg = app.config["PT"]
    app.extensions["pt_rl"] = RateLimiter()
    app.jinja_env.globals["csrf_token"] = csrf_token  # macros imported without context need it too

    @app.before_request
    def _guard():
        ep = request.endpoint or ""
        # CSRF for browser forms (machine API uses the shared key instead)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.path.startswith("/api/"):
            tok = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            if not tok or not hmac.compare_digest(tok, session.get("csrf", "")):
                abort(400, "Bad or missing CSRF token. Reload the page and retry.")
        if ep in PUBLIC_ENDPOINTS or is_authed(cfg):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="login required"), 401
        return redirect(url_for("main.login", next=request.full_path.rstrip("?")))

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if cfg.cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.context_processor
    def _ctx():
        return {"csrf_token": csrf_token, "authed": is_authed(cfg), "cfg": cfg}
