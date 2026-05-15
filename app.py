
"""
Standalone Nuvio Reorder Home app.

Phase 3 public-ready hardening:
- No local account database.
- No saved Nuvio passwords.
- No saved target records.
- No manifest removal / manifest proxy / catalog exclusion state.
- A temporary encrypted, HttpOnly browser cookie stores the Nuvio access token
  and refresh token so users can log in once, reorder Home rows, save, and log out.
- Production security headers, no-store cache headers, noindex, generic login
  errors, and rate limits are enabled by default.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, g, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from reorder_home_helpers import (
    apply_home_row_reorder,
    build_effective_home_rows,
    build_profile_catalog_inventory,
    build_reorder_ui_rows,
    enrich_addon_rows_with_manifest_preview,
    home_item_key,
    normalize_collections_payload,
)
from sync_worker import (
    SyncError,
    expand_nuvio_platform_scope,
    fetch_addon_counts_by_profile,
    fetch_home_catalog_settings,
    fetch_profile_addons,
    fetch_profile_collections,
    fetch_profiles,
    find_profiles_inheriting_addons,
    jwt_expires_at,
    login_nuvio,
    normalize_home_layout_payload,
    normalize_nuvio_platform,
    push_home_catalog_settings,
    refresh_nuvio_tokens,
    resolve_profile_capabilities,
    resolve_profile_rows,
    session_from_token_payload,
    summarize_home_layout_payload,
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw if raw is not None else default).strip())
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


_IS_PROD_EARLY = (
    os.environ.get("FLASK_PRODUCTION", "").strip().lower() in {"1", "true", "yes", "on"}
    or os.environ.get("FLASK_ENV", "").strip().lower() == "production"
)
_LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "").strip().upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, None) if _LOG_LEVEL_NAME else None
if _LOG_LEVEL is None:
    _LOG_LEVEL = logging.INFO if _IS_PROD_EARLY else logging.DEBUG
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

app = Flask(__name__)
app.logger.setLevel(_LOG_LEVEL)

FLASK_SECRET = os.environ.get("FLASK_SECRET", "").strip()
if not FLASK_SECRET:
    if _IS_PROD_EARLY:
        raise RuntimeError("FLASK_SECRET is required in production")
    FLASK_SECRET = secrets.token_urlsafe(48)
    app.logger.warning("FLASK_SECRET is not set. A temporary dev secret was generated; sessions reset on restart.")

app.config["SECRET_KEY"] = FLASK_SECRET
app.config["MAX_CONTENT_LENGTH"] = env_int("MAX_CONTENT_LENGTH_BYTES", 2 * 1024 * 1024, minimum=128 * 1024, maximum=8 * 1024 * 1024)

IS_PRODUCTION = env_flag("FLASK_PRODUCTION") or os.environ.get("FLASK_ENV", "").strip().lower() == "production"
TRUST_PROXY = env_flag("TRUST_PROXY", default=IS_PRODUCTION)
SESSION_COOKIE_NAME = os.environ.get("NUVIO_REORDER_COOKIE_NAME", "nuvio_reorder_session").strip() or "nuvio_reorder_session"
SESSION_HOURS = env_int("PERMANENT_SESSION_LIFETIME_HOURS", 8, minimum=1, maximum=24)
SESSION_MAX_AGE_SECONDS = max(15 * 60, SESSION_HOURS * 60 * 60)
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax").strip().title() or "Lax"
if SESSION_COOKIE_SAMESITE not in {"Lax", "Strict"}:
    raise RuntimeError("SESSION_COOKIE_SAMESITE must be either Lax or Strict")
SESSION_COOKIE_SECURE = env_flag("SESSION_COOKIE_SECURE", default=IS_PRODUCTION)
REFRESH_JWT_BEFORE_SECONDS = env_int("REFRESH_JWT_BEFORE_SECONDS", 300, minimum=60, maximum=3600)
NUVIO_ANON_KEY = os.environ.get("NUVIO_ANON_KEY", "").strip()

SECURITY_HEADERS = env_flag("SECURITY_HEADERS", default=True)
CACHE_CONTROL_NO_STORE = env_flag("CACHE_CONTROL_NO_STORE", default=True)
PUBLIC_ERROR_DETAILS = env_flag("PUBLIC_ERROR_DETAILS", default=False)
HEALTH_REVEALS_CONFIG = env_flag("HEALTH_REVEALS_CONFIG", default=False)

LOGIN_RATE_LIMIT_CALLS = env_int("LOGIN_RATE_LIMIT_CALLS", 5, minimum=2, maximum=60)
LOGIN_RATE_LIMIT_WINDOW = env_int("LOGIN_RATE_LIMIT_WINDOW", 300, minimum=30, maximum=3600)
LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 900, minimum=60, maximum=7200)
API_RATE_LIMIT_CALLS = env_int("API_RATE_LIMIT_CALLS", 180, minimum=30, maximum=1000)
API_RATE_LIMIT_WINDOW = env_int("API_RATE_LIMIT_WINDOW", 60, minimum=10, maximum=3600)
RATE_LIMIT_STATE_MAX_KEYS = env_int("RATE_LIMIT_STATE_MAX_KEYS", 5000, minimum=100, maximum=50000)

if TRUST_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

if not NUVIO_ANON_KEY:
    app.logger.warning("NUVIO_ANON_KEY is not set; login and Nuvio API routes will fail until configured.")


def _public_error(exc: Exception | str, context: str = "request") -> str:
    raw = str(exc or "").strip()
    if PUBLIC_ERROR_DETAILS and raw:
        return raw
    lower = raw.lower()
    safe_starts = (
        "please log in", "session expired", "session could not", "session payload", "security token",
        "profile_id", "platform must", "platform scope", "ordered_keys", "updates must", "removed_keys",
        "apply_to_inherited", "each update", "only orphaned",
    )
    if any(lower.startswith(prefix) for prefix in safe_starts):
        return raw
    if context == "login":
        return "Login failed. Check your Nuvio email and password, then try again."
    if context == "rate_limit":
        return "Too many requests. Please wait a few minutes and try again."
    if context == "config":
        return "This app is not configured yet. The owner needs to set the standalone .env file."
    if context == "save":
        return "Could not save the Home layout. Refresh the page and try again."
    if context == "load":
        return "Could not load the Home layout from Nuvio. Refresh the page and try again."
    if context == "profiles":
        return "Could not load Nuvio profiles. Refresh the page and try again."
    return "Something went wrong. Refresh the page and try again."


def _fernet() -> Fernet:
    digest = hashlib.sha256(FLASK_SECRET.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _now_ts() -> int:
    return int(time.time())


def _encrypt_cookie_payload(payload: Dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("utf-8")


def _decrypt_cookie_payload(raw: str) -> Dict[str, Any]:
    try:
        decoded = _fernet().decrypt(raw.encode("utf-8"), ttl=SESSION_MAX_AGE_SECONDS)
        payload = json.loads(decoded.decode("utf-8"))
    except InvalidToken as exc:
        raise SyncError("Session expired or invalid. Please log in again.") from exc
    except Exception as exc:
        raise SyncError("Session could not be read. Please log in again.") from exc
    if not isinstance(payload, dict):
        raise SyncError("Session payload is invalid. Please log in again.")
    return payload


def _set_session_cookie(resp: Response, payload: Dict[str, Any]) -> Response:
    token = _encrypt_cookie_payload(payload)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )
    return resp


def _clear_session_cookie(resp: Response) -> Response:
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite=SESSION_COOKIE_SAMESITE)
    return resp


def _read_cookie_payload(required: bool = False) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not raw:
        if required:
            raise SyncError("Please log in to Nuvio first.")
        return None
    payload = _decrypt_cookie_payload(raw)
    issued_at = int(payload.get("issued_at") or 0)
    if issued_at and _now_ts() - issued_at > SESSION_MAX_AGE_SECONDS:
        raise SyncError("Session expired. Please log in again.")
    return payload


def _jwt_needs_refresh(jwt: str) -> bool:
    exp = jwt_expires_at(jwt)
    if not exp:
        return False
    return (exp - _now_ts()) <= REFRESH_JWT_BEFORE_SECONDS


def _current_nuvio_session(require_csrf: bool = False):
    if not NUVIO_ANON_KEY:
        raise SyncError("NUVIO_ANON_KEY is not configured")
    payload = _read_cookie_payload(required=True)
    assert payload is not None
    if require_csrf:
        expected = str(payload.get("csrf") or "")
        actual = request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, actual):
            raise SyncError("Security token mismatch. Refresh the page and try again.")
    if _jwt_needs_refresh(str(payload.get("jwt") or "")):
        try:
            refreshed = refresh_nuvio_tokens(str(payload.get("refresh_token") or ""), NUVIO_ANON_KEY)
            payload["jwt"] = str(refreshed.get("access_token") or "")
            payload["refresh_token"] = str(refreshed.get("refresh_token") or payload.get("refresh_token") or "")
            payload["refreshed_at"] = _now_ts()
            g.refreshed_session_payload = payload
        except SyncError:
            app.logger.info("Nuvio token refresh failed; continuing with existing access token", exc_info=True)
    return session_from_token_payload(payload, NUVIO_ANON_KEY), payload


def require_nuvio_session(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            g.nuvio_session, g.nuvio_payload = _current_nuvio_session(require_csrf=request.method in {"POST", "PATCH", "PUT", "DELETE"})
        except SyncError as exc:
            context = "config" if "NUVIO_ANON_KEY" in str(exc) else "request"
            return jsonify({"error": _public_error(exc, context)}), 401
        return fn(*args, **kwargs)
    return wrapper


_RATE_BUCKETS: Dict[str, List[float]] = {}
_LOCKOUT_UNTIL: Dict[str, float] = {}


def _client_key() -> str:
    ip = request.remote_addr or "unknown"
    return str(ip)[:128]


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _prune_rate_state(now: Optional[float] = None) -> None:
    now = now or time.time()
    if len(_RATE_BUCKETS) <= RATE_LIMIT_STATE_MAX_KEYS:
        return
    stale_before = now - max(LOGIN_RATE_LIMIT_WINDOW, API_RATE_LIMIT_WINDOW, LOGIN_LOCKOUT_SECONDS, 3600)
    for key in list(_RATE_BUCKETS.keys()):
        values = [ts for ts in _RATE_BUCKETS.get(key, []) if ts >= stale_before]
        if values:
            _RATE_BUCKETS[key] = values
        else:
            _RATE_BUCKETS.pop(key, None)
    for key, until in list(_LOCKOUT_UNTIL.items()):
        if until <= now:
            _LOCKOUT_UNTIL.pop(key, None)


def _check_rate_limit(key: str, calls: int, window: int, lockout_seconds: int = 0) -> Optional[int]:
    now = time.time()
    _prune_rate_state(now)
    locked_until = _LOCKOUT_UNTIL.get(key, 0)
    if locked_until > now:
        return max(1, int(locked_until - now))
    attempts = [ts for ts in _RATE_BUCKETS.get(key, []) if now - ts <= window]
    if len(attempts) >= calls:
        retry_after = lockout_seconds or max(1, int(window - (now - attempts[0])))
        if lockout_seconds:
            _LOCKOUT_UNTIL[key] = now + lockout_seconds
        _RATE_BUCKETS[key] = attempts
        return retry_after
    attempts.append(now)
    _RATE_BUCKETS[key] = attempts
    return None


def _clear_rate_keys(*keys: str) -> None:
    for key in keys:
        _RATE_BUCKETS.pop(key, None)
        _LOCKOUT_UNTIL.pop(key, None)


@app.before_request
def _api_rate_guard():
    g.csp_nonce = secrets.token_urlsafe(16)
    if request.path.startswith("/api/") and request.endpoint != "session_login":
        retry_after = _check_rate_limit(f"api:{_client_key()}", API_RATE_LIMIT_CALLS, API_RATE_LIMIT_WINDOW)
        if retry_after:
            return jsonify({"error": _public_error("rate", "rate_limit")}), 429, {"Retry-After": str(retry_after)}
    return None


def _request_is_secure() -> bool:
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"


@app.after_request
def _finalize_response(resp: Response) -> Response:
    if SECURITY_HEADERS:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), interest-cohort=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        nonce = getattr(g, "csp_nonce", "")
        script_src = f"script-src 'self' 'nonce-{nonce}'" if nonce else "script-src 'self'"
        resp.headers.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; {script_src}; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net data:; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
        )
        if IS_PRODUCTION and _request_is_secure():
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
    if CACHE_CONTROL_NO_STORE and not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    payload = getattr(g, "refreshed_session_payload", None)
    if isinstance(payload, dict):
        _set_session_cookie(resp, payload)
    return resp


@app.errorhandler(413)
def _payload_too_large(_exc):
    return jsonify({"error": "Request is too large. Reload the page and try a smaller change."}), 413


@app.errorhandler(404)
def _not_found(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return render_template("index.html", bootstrap={"loggedIn": False, "email": "", "userId": "", "csrfToken": "", "sessionHours": SESSION_HOURS, "nuvioAnonConfigured": bool(NUVIO_ANON_KEY)}), 404


@app.errorhandler(500)
def _server_error(exc):
    app.logger.exception("Unhandled server error")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Server error. Refresh and try again."}), 500
    return "Server error. Refresh and try again.", 500


@app.context_processor
def _inject_template_globals():
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.route("/")
def index():
    payload = None
    try:
        payload = _read_cookie_payload(required=False)
    except SyncError:
        payload = None
    bootstrap = {
        "loggedIn": bool(payload),
        "email": str((payload or {}).get("email") or ""),
        "userId": str((payload or {}).get("user_id") or ""),
        "csrfToken": str((payload or {}).get("csrf") or ""),
        "sessionHours": SESSION_HOURS,
        "nuvioAnonConfigured": bool(NUVIO_ANON_KEY),
    }
    return render_template("index.html", bootstrap=bootstrap)


@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/health")
def health():
    payload = {"ok": True, "service": "nuvio-reorder-home-only", "time": datetime.now(timezone.utc).isoformat()}
    if HEALTH_REVEALS_CONFIG:
        payload["nuvio_anon_configured"] = bool(NUVIO_ANON_KEY)
    return jsonify(payload)


@app.route("/api/session/login", methods=["POST"])
def session_login():
    if not NUVIO_ANON_KEY:
        return jsonify({"error": _public_error("missing config", "config")}), 503
    req = request.get_json(silent=True) or {}
    email = str(req.get("email") or "").strip()
    password = str(req.get("password") or "")
    if not email or not password:
        return jsonify({"error": "Enter your Nuvio email and password first."}), 400
    ip_key = f"login:ip:{_client_key()}"
    email_key = f"login:email:{_hash_value(email.lower())}"
    retry_after = _check_rate_limit(ip_key, LOGIN_RATE_LIMIT_CALLS, LOGIN_RATE_LIMIT_WINDOW, LOGIN_LOCKOUT_SECONDS)
    retry_after = retry_after or _check_rate_limit(email_key, LOGIN_RATE_LIMIT_CALLS, LOGIN_RATE_LIMIT_WINDOW, LOGIN_LOCKOUT_SECONDS)
    if retry_after:
        return jsonify({"error": _public_error("rate", "rate_limit")}), 429, {"Retry-After": str(retry_after)}
    try:
        nuvio_session, token_data = login_nuvio(email, password, NUVIO_ANON_KEY)
    except SyncError as exc:
        app.logger.info("Nuvio login failed for ip=%s reason=%s", _client_key(), str(exc)[:160])
        return jsonify({"error": _public_error(exc, "login")}), 400
    except Exception:
        app.logger.exception("Unexpected Nuvio login error")
        return jsonify({"error": _public_error("unexpected", "login")}), 400
    _clear_rate_keys(ip_key, email_key)
    csrf = secrets.token_urlsafe(32)
    payload = {
        "email": nuvio_session.email,
        "user_id": nuvio_session.user_id,
        "jwt": nuvio_session.jwt,
        "refresh_token": str(token_data.get("refresh_token") or ""),
        "issued_at": _now_ts(),
        "csrf": csrf,
    }
    resp = jsonify({"success": True, "email": nuvio_session.email, "user_id": nuvio_session.user_id, "csrf_token": csrf, "expires_in_seconds": SESSION_MAX_AGE_SECONDS})
    return _set_session_cookie(resp, payload)


@app.route("/api/session/logout", methods=["POST"])
def session_logout():
    resp = jsonify({"success": True})
    return _clear_session_cookie(resp)


@app.route("/api/session/me", methods=["GET"])
def session_me():
    try:
        payload = _read_cookie_payload(required=False)
    except SyncError:
        payload = None
    return jsonify({"logged_in": bool(payload), "email": str((payload or {}).get("email") or ""), "user_id": str((payload or {}).get("user_id") or ""), "csrf_token": str((payload or {}).get("csrf") or ""), "session_hours": SESSION_HOURS})


@app.route("/api/nuvio/profiles", methods=["POST"])
@require_nuvio_session
def nuvio_profiles():
    try:
        profiles = resolve_profile_rows(fetch_profiles(g.nuvio_session))
        addon_counts_by_profile = fetch_addon_counts_by_profile(g.nuvio_session)
    except SyncError as exc:
        app.logger.info("Failed to load profiles: %s", str(exc)[:240])
        return jsonify({"error": _public_error(exc, "profiles")}), 400
    inherited_addon_profiles = find_profiles_inheriting_addons(profiles, baseline_profile_id=1)
    inherited_ids = set()
    for inherited_profile in inherited_addon_profiles:
        try:
            inherited_profile_id = int(inherited_profile.get("resolved_profile_id") or inherited_profile.get("profile_id") or 0)
        except (TypeError, ValueError):
            inherited_profile_id = 0
        if inherited_profile_id:
            inherited_ids.add(inherited_profile_id)
    profile_rows: List[Dict[str, Any]] = []
    for profile in profiles:
        try:
            profile_id = int(profile.get("resolved_profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        profile_rows.append({
            **profile,
            "addon_count": addon_counts_by_profile.get(profile_id, 0),
            "is_primary_addon_baseline": profile_id == 1,
            "inherited_addon_profile_ids": sorted(inherited_ids) if profile_id == 1 else [],
            "inherited_addon_profile_count": len(inherited_ids) if profile_id == 1 else 0,
            **resolve_profile_capabilities({**profile, "resolved_profile_id": profile_id}),
        })
    return jsonify({"success": True, "profiles": profile_rows, "user_id": g.nuvio_session.user_id, "email": g.nuvio_session.email, "inherited_addon_profile_ids": sorted(inherited_ids), "inherited_addon_profile_count": len(inherited_ids)})


def _state_payload_for_profile(profile_id: int, platform: str) -> Tuple[Dict[str, Any], int]:
    profiles = resolve_profile_rows(fetch_profiles(g.nuvio_session))
    profile_summary = next((p for p in profiles if int(p.get("resolved_profile_id") or 0) == profile_id), None)
    profile_capabilities = resolve_profile_capabilities(profile_summary or {"profile_id": profile_id, "resolved_profile_id": profile_id})
    effective_addon_profile_id = int(profile_capabilities.get("effective_addon_profile_id") or profile_id)
    addon_rows_raw = fetch_profile_addons(g.nuvio_session, effective_addon_profile_id)
    addon_rows = enrich_addon_rows_with_manifest_preview(addon_rows_raw)
    inventory = build_profile_catalog_inventory(addon_rows)
    collections = normalize_collections_payload(fetch_profile_collections(g.nuvio_session, profile_id))
    remote_home = fetch_home_catalog_settings(g.nuvio_session, profile_id, platform=platform)
    effective_items = build_effective_home_rows(remote_home, inventory, collections)
    home_health = summarize_home_layout_payload(remote_home)
    rows = build_reorder_ui_rows(effective_items, inventory, collections)
    duplicate_raw_orders = sorted({int(r.get("raw_order") or 0) for r in rows if r.get("raw_order_duplicate")})
    orphan_collection_count = sum(1 for r in rows if r.get("is_orphan_collection"))
    home_health = {**(home_health or {}), "duplicate_raw_order_count": len(duplicate_raw_orders), "duplicate_raw_orders": duplicate_raw_orders, "orphan_collection_count": orphan_collection_count}
    payload = {
        "success": True,
        "profile": {**(profile_summary or {"resolved_profile_id": profile_id, "name": f"Profile {profile_id}"}), **profile_capabilities},
        "effective_addon_profile_id": effective_addon_profile_id,
        "addon_inventory_source": "profile_1_inherited_addons" if effective_addon_profile_id != profile_id else "selected_profile",
        "inherited_addon_profiles": [
            {"profile_id": int(p.get("resolved_profile_id") or p.get("profile_id")), "name": p.get("name") or f"Profile {p.get('resolved_profile_id') or p.get('profile_id')}"}
            for p in find_profiles_inheriting_addons(profiles, baseline_profile_id=1)
        ] if profile_id == 1 else [],
        "rows": rows,
        "platform": platform,
        "available_platforms": ["tv", "mobile"],
        "home_health": home_health,
        "home_settings": {"items": effective_items, "platform": platform},
    }
    return payload, effective_addon_profile_id


@app.route("/api/nuvio/reorder-home/state", methods=["POST"])
@require_nuvio_session
def reorder_home_state():
    req = request.get_json(silent=True) or {}
    profile_id_raw = req.get("profile_id")
    platform_raw = req.get("platform") or "tv"
    try:
        profile_id = int(profile_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id must be an integer"}), 400
    try:
        platform = normalize_nuvio_platform(platform_raw)
        payload, _effective_addon_profile_id = _state_payload_for_profile(profile_id, platform)
    except SyncError as exc:
        app.logger.info("Failed to load Reorder Home state: %s", str(exc)[:240])
        return jsonify({"error": _public_error(exc, "load")}), 400
    except Exception:
        app.logger.exception("Failed to load Reorder Home state")
        return jsonify({"error": _public_error("unexpected", "load")}), 400
    return jsonify(payload)


@app.route("/api/nuvio/reorder-home/save", methods=["PATCH"])
@require_nuvio_session
def reorder_home_save():
    req = request.get_json(silent=True) or {}
    platform_raw = req.get("platform") or "tv"
    save_scope_raw = req.get("save_platform_scope") or platform_raw
    profile_id_raw = req.get("profile_id")
    ordered_keys = req.get("ordered_keys")
    visibility_updates = req.get("updates") or []
    removed_keys = req.get("removed_keys") or []
    manifest_updates = req.get("manifest_updates") or []
    inherited_raw = req.get("apply_to_inherited_profile_ids") or []
    if manifest_updates:
        return jsonify({"error": "This standalone tool only changes Home layout visibility."}), 400
    try:
        profile_id = int(profile_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id must be an integer"}), 400
    try:
        platform = normalize_nuvio_platform(platform_raw)
        save_platforms = expand_nuvio_platform_scope(save_scope_raw, default=platform)
    except SyncError as exc:
        return jsonify({"error": _public_error(exc)}), 400
    if not isinstance(ordered_keys, list):
        return jsonify({"error": "ordered_keys must be an array"}), 400
    if not isinstance(visibility_updates, list):
        return jsonify({"error": "updates must be an array"}), 400
    if not isinstance(removed_keys, list):
        return jsonify({"error": "removed_keys must be an array"}), 400
    if not isinstance(inherited_raw, list):
        return jsonify({"error": "apply_to_inherited_profile_ids must be an array"}), 400
    try:
        apply_to_inherited_profile_ids = sorted({int(v) for v in inherited_raw if int(v) in {2, 3, 4}})
    except (TypeError, ValueError):
        return jsonify({"error": "apply_to_inherited_profile_ids must contain profile ids"}), 400
    # RENAME_CATALOGS_AND_BULK_SELECT_V1: save Home visibility and optional per-row custom titles.
    visibility_by_key: Dict[str, bool] = {}
    custom_title_by_key: Dict[str, str] = {}
    for item in visibility_updates:
        if not isinstance(item, dict):
            return jsonify({"error": "each update must be an object"}), 400
        home_key = str(item.get("home_key") or "").strip()
        enabled = item.get("enabled")
        if not home_key:
            return jsonify({"error": "each update.home_key must be a non-empty string"}), 400
        if not isinstance(enabled, bool):
            return jsonify({"error": "each update.enabled must be boolean"}), 400
        visibility_by_key[home_key] = bool(enabled)
        if 'custom_title' in item:
            custom_title = str(item.get('custom_title') or '').strip()
            if len(custom_title) > 120:
                return jsonify({'error': 'custom_title is limited to 120 characters'}), 400
            custom_title_by_key[home_key] = custom_title
    removed_key_set = {str(k).strip() for k in removed_keys if str(k or '').strip()}
    try:
        profiles = resolve_profile_rows(fetch_profiles(g.nuvio_session))
        profile_summary = next((p for p in profiles if int(p.get("resolved_profile_id") or 0) == profile_id), None)
        profile_capabilities = resolve_profile_capabilities(profile_summary or {"profile_id": profile_id, "resolved_profile_id": profile_id})
        effective_addon_profile_id = int(profile_capabilities.get("effective_addon_profile_id") or profile_id)
        addon_rows = enrich_addon_rows_with_manifest_preview(fetch_profile_addons(g.nuvio_session, effective_addon_profile_id))
        inventory = build_profile_catalog_inventory(addon_rows)
        collections = normalize_collections_payload(fetch_profile_collections(g.nuvio_session, profile_id))
        remote_home = fetch_home_catalog_settings(g.nuvio_session, profile_id, platform=platform)
        effective_items = build_effective_home_rows(remote_home, inventory, collections)
        safety_rows = build_reorder_ui_rows(effective_items, inventory, collections)
        removable_keys = {str(row.get("key") or "") for row in safety_rows if row.get("remove_allowed")}
        if any(key not in removable_keys for key in removed_key_set):
            return jsonify({"error": "Only orphaned Home rows can be removed directly. Use Hide from Home for valid rows."}), 400
        effective_items = [item for item in effective_items if home_item_key(item) not in removed_key_set]
        final_payload = apply_home_row_reorder(effective_items, ordered_keys)
        for item in final_payload.get("items", []):
            key = home_item_key(item)
            if key in visibility_by_key:
                item["enabled"] = visibility_by_key[key]
            if key in custom_title_by_key:
                item["custom_title"] = custom_title_by_key[key]
        normalization = normalize_home_layout_payload(final_payload)
        final_payload = normalization["payload"]
        home_normalization_stats = normalization["stats"]
        duplicate_raw_orders_normalized = int(home_normalization_stats.get("duplicate_order_position_count") or 0)
        for save_platform in save_platforms:
            push_home_catalog_settings(g.nuvio_session, profile_id, final_payload, platform=save_platform)
        inherited_results: List[Dict[str, Any]] = []
        if profile_id == 1 and apply_to_inherited_profile_ids:
            inherited_by_id = {int(p.get("resolved_profile_id") or p.get("profile_id") or 0): p for p in find_profiles_inheriting_addons(profiles, baseline_profile_id=1)}
            for child_profile_id in apply_to_inherited_profile_ids:
                if child_profile_id not in inherited_by_id:
                    continue
                for save_platform in save_platforms:
                    push_home_catalog_settings(g.nuvio_session, child_profile_id, final_payload, platform=save_platform)
                inherited_results.append({"profile_id": child_profile_id, "profile_name": inherited_by_id[child_profile_id].get("name") or f"Profile {child_profile_id}", "saved_platforms": save_platforms, "saved_row_count": len(final_payload.get("items") or [])})
    except SyncError as exc:
        app.logger.info("Failed to save Reorder Home state: %s", str(exc)[:240])
        return jsonify({"error": _public_error(exc, "save")}), 400
    except Exception:
        app.logger.exception("Failed to save Reorder Home state")
        return jsonify({"error": _public_error("unexpected", "save")}), 400
    return jsonify({"success": True, "platform": platform, "saved_platforms": save_platforms, "saved_row_count": len(final_payload.get("items") or []), "removed_row_count": len(removed_key_set), "addon_inventory_source": "profile_1_inherited_addons" if effective_addon_profile_id != profile_id else "selected_profile", "effective_addon_profile_id": effective_addon_profile_id, "addon_write_locked": bool(profile_capabilities.get("addon_write_locked")), "inherited_home_apply_results": inherited_results, "duplicate_raw_orders_normalized": duplicate_raw_orders_normalized, "home_normalization_stats": home_normalization_stats, "visibility_update_count": len(visibility_by_key), "title_update_count": len(custom_title_by_key), "manifest_update_count": 0, "manifest_updates": []})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7862") or "7862")
    app.run(host="0.0.0.0", port=port, debug=not IS_PRODUCTION)
