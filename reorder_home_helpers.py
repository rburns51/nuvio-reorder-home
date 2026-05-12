"""
reorder_home_helpers.py - standalone Reorder Home payload helpers.

No database state and no manifest-removal/proxy logic lives here. Catalog manifests are
read only so the app can map Nuvio Home rows to human-friendly titles.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)
MANIFEST_TIMEOUT_SECONDS = 10


def _is_private_host(host: str) -> bool:
    if host.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_link_local or ip.is_multicast
    except ValueError:
        h = host.lower()
        return h.endswith(".local") or h.endswith(".internal") or h.endswith(".lan")


def _build_manifest_url(addon_url: str) -> str:
    parsed = urlparse((addon_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Addon URL must use http or https")
    if not parsed.netloc:
        raise ValueError("Addon URL is missing a hostname")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("Addon URL is missing a hostname")
    if _is_private_host(host):
        raise ValueError("Private, loopback, and reserved addon URLs are not supported")
    path = parsed.path or ""
    manifest_path = path if path.lower().endswith("/manifest.json") else f"{path.rstrip('/')}/manifest.json"
    return urlunparse((parsed.scheme, parsed.netloc, manifest_path, "", "", ""))


def fetch_addon_manifest_preview(addon_url: str) -> Dict[str, Any]:
    manifest_url = _build_manifest_url(addon_url)
    response = requests.get(
        manifest_url,
        headers={"Accept": "application/json"},
        timeout=MANIFEST_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("Manifest response must be a JSON object")
    catalogs = manifest.get("catalogs") if isinstance(manifest.get("catalogs"), list) else []
    normalized_catalogs: List[Dict[str, Any]] = []
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        extra_required = catalog.get("extraRequired") if isinstance(catalog.get("extraRequired"), list) else []
        normalized_catalogs.append({
            "type": str(catalog.get("type") or "").strip(),
            "id": str(catalog.get("id") or "").strip(),
            "name": str(catalog.get("name") or "").strip(),
            "extra_required": [str(item).strip() for item in extra_required if str(item).strip()],
        })
    return {
        "manifest_id": str(manifest.get("id") or "").strip() or None,
        "manifest_name": str(manifest.get("name") or "").strip() or None,
        "catalogs": normalized_catalogs,
    }


def enrich_addon_rows_with_manifest_preview(addons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched_rows: List[Dict[str, Any]] = []
    for index, addon in enumerate(addons or []):
        if not isinstance(addon, dict):
            continue
        preview_url = str(addon.get("url") or "").strip()
        row: Dict[str, Any] = {
            "id": addon.get("id"),
            "name": addon.get("name"),
            "url": addon.get("url"),
            "enabled": bool(addon.get("enabled", True)),
            "sort_order": addon.get("sort_order", index),
            "profile_id": addon.get("profile_id"),
            "manifest_id": None,
            "manifest_name": None,
            "catalogs": [],
            "home_managed": False,
            "manifest_error": None,
            "manifest_preview_url": preview_url,
        }
        for key, value in addon.items():
            if key not in row:
                row[key] = value
        try:
            row.update(fetch_addon_manifest_preview(preview_url))
            row["home_managed"] = bool(row.get("manifest_id"))
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            row["manifest_id"] = None
            row["manifest_name"] = None
            row["catalogs"] = []
            row["home_managed"] = False
            row["manifest_error"] = str(exc)
            logger.info("Manifest preview failed for add-on %s: %s", addon.get("name") or addon.get("id"), exc)
        enriched_rows.append(row)
    return enriched_rows


def build_home_catalog_key(manifest_id: str, catalog_type: str, catalog_id: str) -> str:
    return f"{(manifest_id or '').strip()}_{(catalog_type or '').strip()}_{(catalog_id or '').strip()}"


def build_catalog_key(catalog_type: Any, catalog_id: Any) -> str:
    ctype = str(catalog_type or "").strip()
    cid = str(catalog_id or "").strip()
    return f"{ctype}:{cid}" if ctype and cid else ""


def collection_home_key(collection_id: str) -> str:
    return f"collection_{(collection_id or '').strip()}"


def normalize_home_catalog_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "addon_id": str(item.get("addon_id") or "").strip(),
        "type": str(item.get("type") or "").strip(),
        "catalog_id": str(item.get("catalog_id") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
        "order": int(item.get("order") or 0),
        "custom_title": str(item.get("custom_title") or ""),
        "is_collection": bool(item.get("is_collection", False)),
        "collection_id": str(item.get("collection_id") or "").strip() or None,
    }


def home_item_key(item: Dict[str, Any]) -> str:
    if bool(item.get("is_collection")):
        return collection_home_key(str(item.get("collection_id") or ""))
    return build_home_catalog_key(str(item.get("addon_id") or ""), str(item.get("type") or ""), str(item.get("catalog_id") or ""))


def build_profile_catalog_inventory(addons_with_manifest: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    inventory: List[Dict[str, Any]] = []
    for addon in addons_with_manifest or []:
        catalogs = addon.get("catalogs") if isinstance(addon.get("catalogs"), list) else []
        manifest_id = str(addon.get("manifest_id") or "").strip()
        if not manifest_id:
            continue
        for catalog in catalogs:
            if not isinstance(catalog, dict):
                continue
            catalog_id = str(catalog.get("id") or "").strip()
            catalog_type = str(catalog.get("type") or "").strip()
            if not catalog_id or not catalog_type:
                continue
            catalog_key = build_catalog_key(catalog_type, catalog_id)
            inventory.append({
                "addon_supabase_id": str(addon.get("id") or "").strip(),
                "addon_name": str(addon.get("name") or ""),
                "addon_url": str(addon.get("url") or ""),
                "manifest_id": manifest_id,
                "manifest_name": addon.get("manifest_name"),
                "addon_enabled": bool(addon.get("enabled", True)),
                "addon_sort_order": int(addon.get("sort_order") or 0),
                "catalog_id": catalog_id,
                "catalog_name": str(catalog.get("name") or ""),
                "catalog_type": catalog_type,
                "catalog_key": catalog_key,
                "home_key": build_home_catalog_key(manifest_id, catalog_type, catalog_id),
                "home_enabled": True,
                "home_custom_title": "",
            })
    return inventory


def normalize_collections_payload(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        clone = dict(item)
        clone["id"] = cid
        clone["title"] = str(item.get("title") or cid)
        if not isinstance(clone.get("folders"), list):
            clone["folders"] = []
        clone["pinToTop"] = bool(item.get("pinToTop", False))
        out.append(clone)
    return out


def build_effective_home_rows(remote_payload: Dict[str, Any], inventory: List[Dict[str, Any]], collections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remote_items_raw = remote_payload.get("items") if isinstance(remote_payload, dict) else []
    remote_items = [normalize_home_catalog_item(it) for it in (remote_items_raw or []) if isinstance(it, dict)]
    inventory_keys = {row["home_key"] for row in inventory if row.get("home_key")}
    collection_ids_in_remote: set[str] = set()
    merged: List[Dict[str, Any]] = []

    for item in remote_items:
        key = home_item_key(item)
        if not key:
            continue
        if bool(item.get("is_collection")):
            collection_ids_in_remote.add(str(item.get("collection_id") or "").strip())
            merged.append(item)
        elif key in inventory_keys:
            merged.append(item)

    existing_keys = {home_item_key(it) for it in merged}
    for inv in inventory:
        key = inv.get("home_key")
        if not key or key in existing_keys:
            continue
        merged.append({
            "addon_id": inv.get("manifest_id"),
            "type": inv.get("catalog_type"),
            "catalog_id": inv.get("catalog_id"),
            "enabled": True,
            "order": 0,
            "custom_title": "",
            "is_collection": False,
            "collection_id": None,
        })
        existing_keys.add(key)

    for coll in collections:
        coll_id = str(coll.get("id") or "").strip() if isinstance(coll, dict) else ""
        if not coll_id or coll_id in collection_ids_in_remote:
            continue
        key = collection_home_key(coll_id)
        if key in existing_keys:
            continue
        merged.append({
            "addon_id": "",
            "type": "",
            "catalog_id": "",
            "enabled": True,
            "order": 0,
            "custom_title": "",
            "is_collection": True,
            "collection_id": coll_id,
        })
        existing_keys.add(key)

    for idx, item in enumerate(merged):
        if "raw_order" not in item:
            item["raw_order"] = int(item.get("order") or 0)
        item["order"] = idx
    return merged


def apply_home_row_reorder(effective_rows: List[Dict[str, Any]], ordered_keys: List[str]) -> Dict[str, Any]:
    items_by_key: Dict[str, Dict[str, Any]] = {}
    for item in effective_rows:
        key = home_item_key(item)
        if key and key not in items_by_key:
            items_by_key[key] = item
    ordered: List[Dict[str, Any]] = []
    used_keys: set[str] = set()
    for raw_key in ordered_keys:
        key = str(raw_key).strip()
        if not key or key in used_keys:
            continue
        item = items_by_key.get(key)
        if item:
            ordered.append(item)
            used_keys.add(key)
    for item in effective_rows:
        key = home_item_key(item)
        if key and key not in used_keys:
            ordered.append(item)
            used_keys.add(key)
    for idx, item in enumerate(ordered):
        item["order"] = idx
    return {"items": ordered}


def build_reorder_ui_rows(effective_items: List[Dict[str, Any]], inventory: List[Dict[str, Any]], collections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    inventory_by_key = {row["home_key"]: row for row in inventory if row.get("home_key")}
    collections_by_id = {
        str(c.get("id") or "").strip(): c
        for c in collections
        if isinstance(c, dict) and str(c.get("id") or "").strip()
    }
    rows: List[Dict[str, Any]] = []
    raw_order_counts: Dict[int, int] = {}
    for item in effective_items or []:
        key = home_item_key(item)
        if not key:
            continue
        raw_order = int(item.get("raw_order", item.get("order")) or 0)
        raw_order_counts[raw_order] = raw_order_counts.get(raw_order, 0) + 1
        enabled = bool(item.get("enabled", True))
        if bool(item.get("is_collection")):
            coll_id = str(item.get("collection_id") or "").strip()
            collection_exists = bool(coll_id and coll_id in collections_by_id)
            coll = collections_by_id.get(coll_id) or {}
            is_orphan = not collection_exists
            title = (coll_id or key) if is_orphan else (str(coll.get("title") or "") or coll_id or key)
            rows.append({
                "key": key,
                "row_type": "collection",
                "title": title,
                "raw_title": title,
                "custom_title": "",
                "enabled": enabled,
                "order": raw_order,
                "raw_order": raw_order,
                "raw_order_duplicate": False,
                "display_index": len(rows),
                "subtitle": "Not found in current collections. This is likely a stale Home layout row." if is_orphan else "Collection row",
                "collection_id": coll_id,
                "collection_exists": collection_exists,
                "is_orphan_collection": is_orphan,
                "folder_count": len(coll.get("folders") or []),
                "pin_to_top": bool(coll.get("pinToTop", False)),
                "pinned": bool(coll.get("pinToTop", False)),
                "addon_name": "",
                "manifest_id": "",
                "catalog_type": "collection",
                "catalog_id": coll_id,
                "catalog_key": "",
                "home_layout_visible": enabled,
                "home_layout_hidden": not enabled,
                "home_visibility_label": "Home layout visible" if enabled else "Home layout hidden",
                "home_visibility_help": "",
                "addon_supabase_id": "",
                "addon_url": "",
                "row_warning": "Missing collection reference" if is_orphan else "",
                "remove_allowed": bool(is_orphan),
                "remove_action": "remove_orphan_collection" if is_orphan else "",
                "remove_help": "This collection row exists in the Home layout but is not present in the current collections blob. Removing it only removes the stale Home row." if is_orphan else "",
                "action_set": "orphan_collection" if is_orphan else "collection",
            })
            continue

        inv = inventory_by_key.get(key) or {}
        raw_title = str(inv.get("catalog_name") or inv.get("manifest_name") or key)
        custom_title = str(item.get("custom_title") or "")
        title = custom_title or raw_title
        rows.append({
            "key": key,
            "row_type": "catalog",
            "title": title,
            "raw_title": raw_title,
            "custom_title": custom_title,
            "enabled": enabled,
            "order": raw_order,
            "raw_order": raw_order,
            "raw_order_duplicate": False,
            "display_index": len(rows),
            "subtitle": "",
            "addon_name": str(inv.get("addon_name") or inv.get("manifest_name") or ""),
            "addon_id": str(item.get("addon_id") or inv.get("manifest_id") or ""),
            "type": str(item.get("type") or inv.get("catalog_type") or ""),
            "manifest_id": str(inv.get("manifest_id") or ""),
            "catalog_type": str(inv.get("catalog_type") or ""),
            "catalog_id": str(inv.get("catalog_id") or ""),
            "addon_supabase_id": str(inv.get("addon_supabase_id") or ""),
            "addon_url": str(inv.get("addon_url") or ""),
            "catalog_key": str(inv.get("catalog_key") or build_catalog_key(inv.get("catalog_type"), inv.get("catalog_id")) or ""),
            "home_layout_visible": enabled,
            "home_layout_hidden": not enabled,
            "home_visibility_label": "Home layout visible" if enabled else "Home layout hidden",
            "home_visibility_help": "Visible through Nuvio Home settings." if enabled else "Hidden through Nuvio Home layout settings.",
            "row_warning": "",
            "remove_allowed": False,
            "remove_action": "",
            "remove_help": "",
            "action_set": "catalog",
        })
    duplicate_raw_orders = {order for order, count in raw_order_counts.items() if count > 1}
    for row in rows:
        row["raw_order_duplicate"] = int(row.get("raw_order") or 0) in duplicate_raw_orders
    return rows
