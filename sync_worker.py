"""
sync_worker.py - minimal Nuvio API client for the standalone Reorder Home app.

This file intentionally contains only the Nuvio pieces needed by Reorder Home:
login/session verification, profile/add-on/collection reads, Home layout reads/writes,
and payload normalization. No local user database, no manifest proxy, no Stremio sync.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

NUVIO_PROJECT_URL = os.environ.get("NUVIO_PROJECT_URL", "https://dpyhjjcoabcglfmgecug.supabase.co").rstrip("/")
NUVIO_BASE_REST = f"{NUVIO_PROJECT_URL}/rest/v1"
NUVIO_BASE_RPC = f"{NUVIO_BASE_REST}/rpc"
NUVIO_AUTH_TOKEN_URL = f"{NUVIO_PROJECT_URL}/auth/v1/token"
VALID_NUVIO_PLATFORMS = {"tv", "mobile"}
VALID_NUVIO_PLATFORM_SCOPES = {"tv", "mobile", "both"}
HTTP_TIMEOUT_SECONDS = int(os.environ.get("NUVIO_HTTP_TIMEOUT_SECONDS", "20") or "20")


class SyncError(Exception):
    """User-safe sync/API failure."""


@dataclass
class NuvioSession:
    email: str
    anon_key: str
    jwt: str
    user_id: str
    refresh_token: str = ""

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type": "application/json",
        }


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, child in value.items():
            if str(key).lower() in {"authorization", "apikey", "password", "access_token", "refresh_token", "jwt", "token"}:
                out[key] = "***REDACTED***"
            else:
                out[key] = _redact(child)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _request(method: str, url: str, **kwargs) -> requests.Response:
    start = time.perf_counter()
    try:
        resp = requests.request(method, url, timeout=HTTP_TIMEOUT_SECONDS, **kwargs)
    except requests.exceptions.Timeout as exc:
        raise SyncError("Timed out while contacting Nuvio. Please try again.") from exc
    except requests.exceptions.RequestException as exc:
        raise SyncError("Could not contact Nuvio. Please try again.") from exc

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if os.environ.get("NUVIO_API_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.warning(
            "[NUVIO API] %s %s status=%s elapsed_ms=%s request_json=%s response=%s",
            method,
            url,
            resp.status_code,
            elapsed_ms,
            json.dumps(_redact(kwargs.get("json")), default=str)[:4000],
            json.dumps(_redact(_safe_json(resp)), default=str)[:8000],
        )
    else:
        logger.debug("Nuvio %s %s status=%s elapsed_ms=%s", method, url, resp.status_code, elapsed_ms)
    return resp


def _post(url: str, **kwargs) -> requests.Response:
    return _request("POST", url, **kwargs)


def _get(url: str, **kwargs) -> requests.Response:
    return _request("GET", url, **kwargs)


def normalize_nuvio_platform(platform: Any, default: str = "tv") -> str:
    value = str(platform or default or "tv").strip().lower()
    if value not in VALID_NUVIO_PLATFORMS:
        raise SyncError("platform must be 'tv' or 'mobile'")
    return value


def expand_nuvio_platform_scope(scope: Any, default: str = "tv") -> List[str]:
    value = str(scope or default or "tv").strip().lower()
    if value == "both":
        return ["tv", "mobile"]
    if value in VALID_NUVIO_PLATFORMS:
        return [value]
    raise SyncError("platform scope must be 'tv', 'mobile', or 'both'")


def _request_nuvio_tokens(email: str, password: str, anon_key: str) -> Dict[str, Any]:
    resp = _post(
        f"{NUVIO_AUTH_TOKEN_URL}?grant_type=password",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    if not resp.ok:
        raise SyncError("Nuvio login failed. Check your email and password, then try again.")
    data = _safe_json(resp)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise SyncError("Nuvio login succeeded but no access_token was returned")
    return data


def refresh_nuvio_tokens(refresh_token: str, anon_key: str) -> Dict[str, Any]:
    if not refresh_token:
        raise SyncError("No refresh token is available. Please log in again.")
    resp = _post(
        f"{NUVIO_AUTH_TOKEN_URL}?grant_type=refresh_token",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
    )
    if not resp.ok:
        raise SyncError("Nuvio session refresh failed. Please log in again.")
    data = _safe_json(resp)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise SyncError("Nuvio session refresh succeeded but no access_token was returned")
    return data


def _get_sync_owner(anon_key: str, jwt: str) -> str:
    resp = _post(
        f"{NUVIO_BASE_RPC}/get_sync_owner",
        headers={"apikey": anon_key, "Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json={},
    )
    if not resp.ok:
        raise SyncError("Nuvio session could not be verified. Please log in again.")
    owner = _safe_json(resp)
    if isinstance(owner, str):
        owner = owner.strip().strip('"')
    if not owner:
        raise SyncError("Nuvio session verification returned an empty owner id")
    return str(owner)


def login_nuvio(email: str, password: str, anon_key: str) -> Tuple[NuvioSession, Dict[str, Any]]:
    email = (email or "").strip()
    if not email or not password:
        raise SyncError("Email and password are required")
    logger.info("login_nuvio: authentication attempt")
    token_data = _request_nuvio_tokens(email, password, anon_key)
    jwt = str(token_data.get("access_token") or "")
    owner = _get_sync_owner(anon_key, jwt)
    session = NuvioSession(
        email=email,
        anon_key=anon_key,
        jwt=jwt,
        user_id=owner,
        refresh_token=str(token_data.get("refresh_token") or ""),
    )
    return session, token_data


def session_from_token_payload(payload: Dict[str, Any], anon_key: str) -> NuvioSession:
    jwt = str(payload.get("jwt") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    email = str(payload.get("email") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not jwt or not user_id:
        raise SyncError("Session is missing Nuvio credentials. Please log in again.")
    return NuvioSession(email=email, anon_key=anon_key, jwt=jwt, user_id=user_id, refresh_token=refresh_token)


def jwt_expires_at(jwt: str) -> Optional[int]:
    try:
        parts = str(jwt or "").split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8"))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def fetch_profiles(session: NuvioSession) -> List[Dict[str, Any]]:
    resp = _post(f"{NUVIO_BASE_RPC}/sync_pull_profiles", headers=session.headers, json={})
    if not resp.ok:
        raise SyncError(f"Failed to pull profiles ({resp.status_code}): {_safe_json(resp)}")
    data = _safe_json(resp)
    if not isinstance(data, list):
        raise SyncError("Unexpected profile payload from Nuvio")
    return data


def _parse_profile_number(raw: Any, min_value: int = 1, max_value: int = 4) -> Optional[int]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < min_value or value > max_value:
        return None
    return value


def resolve_profile_rows(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = list(profiles or [])
    if not rows:
        return []

    valid_profile_ids = [
        value
        for value in (_parse_profile_number(row.get("profile_id"), 1, 4) for row in rows)
        if value is not None
    ]
    has_unique_profile_ids = len(valid_profile_ids) == len(rows) and len(set(valid_profile_ids)) == len(rows)
    one_based_indexes = [_parse_profile_number(row.get("profile_index"), 1, 4) for row in rows]
    zero_based_indexes = [_parse_profile_number(row.get("profile_index"), 0, 3) for row in rows]
    has_one_based_indexes = all(idx is not None for idx in one_based_indexes) and len(set(one_based_indexes)) == len(rows)
    has_zero_based_indexes = all(idx is not None for idx in zero_based_indexes) and len(set(zero_based_indexes)) == len(rows)

    resolved_rows: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        resolved = _parse_profile_number(row.get("profile_id"), 1, 4)
        if has_unique_profile_ids:
            resolved_profile_id = resolved or (index + 1)
        elif has_one_based_indexes:
            resolved_profile_id = int(row.get("profile_index"))
        elif has_zero_based_indexes:
            resolved_profile_id = int(row.get("profile_index")) + 1
        else:
            resolved_profile_id = index + 1
        resolved_rows.append({**row, "resolved_profile_id": resolved_profile_id})
    return resolved_rows


def resolve_profile_capabilities(profile: Dict[str, Any]) -> Dict[str, Any]:
    resolved_profile_id = int(profile.get("resolved_profile_id") or profile.get("profile_id") or 0)
    uses_primary_addons = bool(profile.get("uses_primary_addons", False))
    uses_primary_plugins = bool(profile.get("uses_primary_plugins", False))
    return {
        "sync_allowed": True,
        "sync_locked": False,
        "lock_reason": "",
        "addon_write_locked": uses_primary_addons,
        "plugin_write_locked": uses_primary_plugins,
        "inherits_primary_addons": uses_primary_addons,
        "inherits_primary_plugins": uses_primary_plugins,
        "effective_addon_profile_id": 1 if uses_primary_addons else resolved_profile_id,
        "can_sync_addons": not uses_primary_addons,
        "can_sync_collections": True,
        "can_sync_home_layout": True,
        "can_sync_profile_settings": True,
        "addon_lock_reason": "This profile inherits Profile 1’s add-on stack. Add-on changes are managed through Profile 1." if uses_primary_addons else "",
        "plugin_lock_reason": "This profile inherits Profile 1’s plugin stack. Plugin changes are managed through Profile 1." if uses_primary_plugins else "",
        "surface_sync_allowed": True,
    }


def find_profiles_inheriting_addons(profiles: List[Dict[str, Any]], baseline_profile_id: int = 1) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    for profile in resolve_profile_rows(profiles):
        try:
            profile_id = int(profile.get("resolved_profile_id") or profile.get("profile_id") or 0)
        except (TypeError, ValueError):
            continue
        if profile_id == int(baseline_profile_id):
            continue
        if bool(profile.get("uses_primary_addons", False)):
            children.append({**profile, **resolve_profile_capabilities(profile)})
    return children


def fetch_profile_addons(session: NuvioSession, profile_id: int) -> List[Dict[str, Any]]:
    params = {
        "select": "*",
        "user_id": f"eq.{session.user_id}",
        "profile_id": f"eq.{int(profile_id)}",
        "order": "sort_order.asc,created_at.asc",
    }
    resp = _get(f"{NUVIO_BASE_REST}/addons", headers=session.headers, params=params)
    if not resp.ok:
        raise SyncError(f"Failed to fetch add-ons ({resp.status_code}): {_safe_json(resp)}")
    data = _safe_json(resp)
    if not isinstance(data, list):
        raise SyncError("Unexpected add-on list payload from Nuvio")
    return data


def fetch_addon_counts_by_profile(session: NuvioSession) -> Dict[int, int]:
    params = {
        "select": "profile_id",
        "user_id": f"eq.{session.user_id}",
        "order": "profile_id.asc",
    }
    resp = _get(f"{NUVIO_BASE_REST}/addons", headers=session.headers, params=params)
    if not resp.ok:
        raise SyncError(f"Failed to fetch add-on counts ({resp.status_code}): {_safe_json(resp)}")
    data = _safe_json(resp)
    if not isinstance(data, list):
        raise SyncError("Unexpected add-on count payload from Nuvio")
    counts: Dict[int, int] = {}
    for row in data:
        try:
            profile_id = int(row.get("profile_id") or 0)
        except (TypeError, ValueError):
            continue
        if profile_id:
            counts[profile_id] = counts.get(profile_id, 0) + 1
    return counts


def fetch_profile_collections(session: NuvioSession, profile_id: int) -> List[Dict[str, Any]]:
    resp = _post(
        f"{NUVIO_BASE_RPC}/sync_pull_collections",
        headers=session.headers,
        json={"p_profile_id": int(profile_id)},
    )
    if not resp.ok:
        raise SyncError(f"Failed to pull collections ({resp.status_code}): {_safe_json(resp)}")
    raw = _safe_json(resp)
    collections_json: Any = raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        first = raw[0]
        if "collections_json" in first:
            collections_json = first.get("collections_json")
        elif "collectionsJson" in first:
            collections_json = first.get("collectionsJson")
    elif isinstance(raw, dict):
        if "collections_json" in raw:
            collections_json = raw.get("collections_json")
        elif "collectionsJson" in raw:
            collections_json = raw.get("collectionsJson")
    if isinstance(collections_json, str):
        try:
            collections_json = json.loads(collections_json)
        except Exception:
            return []
    return collections_json if isinstance(collections_json, list) else []


def _extract_settings_json_from_rpc_response(raw: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    metadata: Dict[str, Any] = {}
    settings_json: Any = None
    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict):
            first = raw[0]
            metadata = {k: first.get(k) for k in ("platform", "updated_at", "id", "user_id", "profile_id") if first.get(k) is not None}
            settings_json = first.get("settings_json") if "settings_json" in first else first
    elif isinstance(raw, dict):
        metadata = {k: raw.get(k) for k in ("platform", "updated_at", "id", "user_id", "profile_id") if raw.get(k) is not None}
        settings_json = raw.get("settings_json") if "settings_json" in raw else raw
    elif isinstance(raw, str):
        try:
            settings_json = json.loads(raw)
        except Exception:
            settings_json = None
    if isinstance(settings_json, str):
        try:
            settings_json = json.loads(settings_json)
        except Exception:
            settings_json = None
    if not isinstance(settings_json, dict):
        settings_json = {}
    return settings_json, metadata


def fetch_home_catalog_settings(session: NuvioSession, profile_id: int, platform: str = "tv") -> Dict[str, Any]:
    platform = normalize_nuvio_platform(platform)
    resp = _post(
        f"{NUVIO_BASE_RPC}/sync_pull_home_catalog_settings",
        headers=session.headers,
        json={"p_profile_id": int(profile_id), "p_platform": platform},
    )
    if not resp.ok:
        raise SyncError(f"Failed to pull home catalog settings ({resp.status_code}): {_safe_json(resp)}")
    settings_json, metadata = _extract_settings_json_from_rpc_response(_safe_json(resp))
    items = settings_json.get("items")
    out: Dict[str, Any] = {"items": items if isinstance(items, list) else [], "_platform": platform}
    out.update({f"_{k}": v for k, v in metadata.items() if v is not None})
    return out


def push_home_catalog_settings(session: NuvioSession, profile_id: int, payload: Dict[str, Any], platform: str = "tv") -> None:
    platform = normalize_nuvio_platform(platform)
    if not isinstance(payload, dict):
        raise SyncError("home catalog settings payload must be an object")
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    resp = _post(
        f"{NUVIO_BASE_RPC}/sync_push_home_catalog_settings",
        headers=session.headers,
        json={"p_profile_id": int(profile_id), "p_platform": platform, "p_settings_json": {"items": items}},
    )
    if not resp.ok:
        raise SyncError(f"Failed to push home catalog settings ({resp.status_code}): {_safe_json(resp)}")


def _home_layout_item_key(item: Dict[str, Any]) -> str:
    if bool(item.get("is_collection")):
        collection_id = str(item.get("collection_id") or "").strip()
        return f"collection::{collection_id}" if collection_id else ""
    addon_id = str(item.get("addon_id") or "").strip()
    item_type = str(item.get("type") or "").strip()
    catalog_id = str(item.get("catalog_id") or "").strip()
    if not addon_id or not catalog_id:
        return ""
    return f"catalog::{addon_id}::{item_type}::{catalog_id}"


def _coerce_home_order(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_home_layout_item(item: Dict[str, Any]) -> Dict[str, Any]:
    is_collection = bool(item.get("is_collection", False))
    return {
        "type": "" if is_collection else str(item.get("type") or "").strip(),
        "order": _coerce_home_order(item.get("order")),
        "enabled": bool(item.get("enabled", True)),
        "addon_id": "" if is_collection else str(item.get("addon_id") or "").strip(),
        "catalog_id": "" if is_collection else str(item.get("catalog_id") or "").strip(),
        "custom_title": str(item.get("custom_title") or ""),
        "collection_id": str(item.get("collection_id") or "").strip() if is_collection else "",
        "is_collection": is_collection,
    }


def normalize_home_layout_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    order_counts: Dict[int, int] = {}
    keyed_rows: List[Tuple[int, int, str, Dict[str, Any]]] = []
    invalid_rows = 0
    for original_index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            invalid_rows += 1
            continue
        item = _normalize_home_layout_item(raw_item)
        key = _home_layout_item_key(item)
        if not key:
            invalid_rows += 1
            continue
        order = _coerce_home_order(item.get("order"))
        order_counts[order] = order_counts.get(order, 0) + 1
        keyed_rows.append((order, original_index, key, item))
    duplicate_order_positions = sorted(order for order, count in order_counts.items() if count > 1)
    keyed_rows.sort(key=lambda row: (row[0], row[1]))
    seen_keys: set[str] = set()
    normalized_items: List[Dict[str, Any]] = []
    removed_duplicate_keys = 0
    for _order, _original_index, key, item in keyed_rows:
        if key in seen_keys:
            removed_duplicate_keys += 1
            continue
        seen_keys.add(key)
        next_item = dict(item)
        next_item["order"] = len(normalized_items)
        normalized_items.append(next_item)
    return {
        "payload": {"items": normalized_items},
        "stats": {
            "input_count": len(raw_items),
            "output_count": len(normalized_items),
            "invalid_rows_dropped": invalid_rows,
            "duplicate_order_positions": duplicate_order_positions,
            "duplicate_order_position_count": len(duplicate_order_positions),
            "duplicate_key_rows_removed": removed_duplicate_keys,
        },
    }


def summarize_home_layout_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_home_layout_payload(payload)
    items = normalized["payload"]["items"]
    enabled = [item for item in items if bool(item.get("enabled", True))]
    disabled = [item for item in items if not bool(item.get("enabled", True))]
    collections = [item for item in items if bool(item.get("is_collection"))]
    catalogs = [item for item in items if not bool(item.get("is_collection"))]
    stats = dict(normalized["stats"])
    stats.update({
        "row_count": len(items),
        "enabled_count": len(enabled),
        "disabled_count": len(disabled),
        "catalog_count": len(catalogs),
        "collection_count": len(collections),
    })
    return stats
