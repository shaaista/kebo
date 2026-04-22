"""
Widget Deployment Service

Manages embeddable chat widget deployments. Each deployment has:
  - widget_key: public, rotatable identifier exposed in customer snippets
  - hotel_code: tenant scope
  - allowed_origins: list of host domains permitted to embed this widget
  - theme/size/position: branding overrides

Storage: JSON file at config/widget_deployments.json (keyed by widget_key).
Designed to be migrated to a DB table later without changing the API surface.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


_BASE_DIR = Path(__file__).resolve().parent.parent
_DEPLOYMENTS_FILE = _BASE_DIR / "config" / "widget_deployments.json"
_PROPERTIES_DIR = _BASE_DIR / "config" / "properties"

DEFAULT_THEME = {
    "brand_color": "#C72C41",
    "accent_color": "#C72C41",
    "bg_color": "#FFFFFF",
    "text_color": "#1A1A2E",
}

DEFAULT_SIZE = {
    "width": 380,
    "height": 620,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_origin(value: str) -> str:
    """Normalize an origin to scheme://host[:port], lowercased. '*' passes through."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw == "*":
        return "*"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except Exception:
        return ""


def _normalize_color(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if raw.startswith("#"):
        return raw
    if len(raw) in (3, 4, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in raw):
        return "#" + raw
    return raw


def _clamp(num: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        v = int(num)
    except Exception:
        return fallback
    return max(lo, min(hi, v))


class WidgetDeploymentService:
    """JSON-backed deployment store. Thread-safe via a single lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None
        _DEPLOYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ---- Storage ---------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not _DEPLOYMENTS_FILE.exists():
                self._cache = {"deployments": {}}
                return self._cache
            try:
                payload = json.loads(_DEPLOYMENTS_FILE.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {"deployments": {}}
                payload.setdefault("deployments", {})
                self._cache = payload
                return self._cache
            except Exception:
                self._cache = {"deployments": {}}
                return self._cache

    def _persist(self) -> None:
        with self._lock:
            data = self._load()
            tmp = _DEPLOYMENTS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_DEPLOYMENTS_FILE)

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # ---- Helpers ---------------------------------------------------------

    @staticmethod
    def _gen_key() -> str:
        # 24 url-safe chars ~ 144 bits of entropy. Prefixed for log-grep.
        return "wk_" + secrets.token_urlsafe(18)

    @staticmethod
    def _hotel_code(value: Any) -> str:
        return str(value or "").strip().lower() or "default"

    def _hotel_defaults(self, hotel_code: str) -> Dict[str, Any]:
        """Pull theme/branding defaults from the property JSON if available."""
        defaults = {
            "bot_name": "Assistant",
            "welcome_message": "",
            "theme": dict(DEFAULT_THEME),
            "size": dict(DEFAULT_SIZE),
            "position": "right",
            "auto_open": False,
            "phase": "pre_booking",
        }
        try:
            path = _PROPERTIES_DIR / f"{hotel_code}.json"
            if not path.exists():
                return defaults
            payload = json.loads(path.read_text(encoding="utf-8"))
            business = (payload or {}).get("business", {}) or {}
            if business.get("bot_name"):
                defaults["bot_name"] = str(business["bot_name"]).strip() or defaults["bot_name"]
            if business.get("welcome_message"):
                defaults["welcome_message"] = str(business["welcome_message"]).strip()
            theme_src = business.get("widget_theme") or business.get("theme") or {}
            if isinstance(theme_src, dict):
                for k in ("brand_color", "accent_color", "bg_color", "text_color"):
                    if theme_src.get(k):
                        defaults["theme"][k] = _normalize_color(theme_src[k], defaults["theme"][k])
        except Exception:
            pass
        return defaults

    def _normalize_record(self, hotel_code: str, body: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._hotel_defaults(hotel_code)
        theme_in = (body.get("theme") or {}) if isinstance(body.get("theme"), dict) else {}
        size_in = (body.get("size") or {}) if isinstance(body.get("size"), dict) else {}

        theme = {
            "brand_color": _normalize_color(theme_in.get("brand_color"), defaults["theme"]["brand_color"]),
            "accent_color": _normalize_color(theme_in.get("accent_color"), defaults["theme"]["accent_color"]),
            "bg_color": _normalize_color(theme_in.get("bg_color"), defaults["theme"]["bg_color"]),
            "text_color": _normalize_color(theme_in.get("text_color"), defaults["theme"]["text_color"]),
        }
        size = {
            "width": _clamp(size_in.get("width"), defaults["size"]["width"], 280, 600),
            "height": _clamp(size_in.get("height"), defaults["size"]["height"], 360, 900),
        }
        position = str(body.get("position") or defaults["position"]).strip().lower()
        if position not in ("left", "right"):
            position = "right"

        allowed_origins_in = body.get("allowed_origins") or []
        if not isinstance(allowed_origins_in, list):
            allowed_origins_in = []
        normalized_origins: List[str] = []
        for origin in allowed_origins_in:
            n = _normalize_origin(str(origin))
            if n and n not in normalized_origins:
                normalized_origins.append(n)

        bot_name = str(body.get("bot_name") or defaults["bot_name"]).strip() or defaults["bot_name"]
        phase = str(body.get("phase") or defaults["phase"]).strip().lower() or defaults["phase"]
        status = str(body.get("status") or "active").strip().lower()
        if status not in ("active", "inactive"):
            status = "active"
        name = str(body.get("name") or "").strip() or f"{hotel_code} widget"

        return {
            "hotel_code": hotel_code,
            "name": name,
            "status": status,
            "allowed_origins": normalized_origins,
            "theme": theme,
            "size": size,
            "position": position,
            "bot_name": bot_name,
            "phase": phase,
            "auto_open": bool(body.get("auto_open", defaults["auto_open"])),
        }

    # ---- Public API ------------------------------------------------------

    def list(self, hotel_code: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            rows = []
            for key, dep in data["deployments"].items():
                if hotel_code and self._hotel_code(dep.get("hotel_code")) != self._hotel_code(hotel_code):
                    continue
                rows.append({"widget_key": key, **dep})
            rows.sort(key=lambda r: str(r.get("created_at") or ""))
            return rows

    def get(self, widget_key: str) -> Optional[Dict[str, Any]]:
        if not widget_key:
            return None
        with self._lock:
            data = self._load()
            dep = data["deployments"].get(widget_key)
            if not dep:
                return None
            return {"widget_key": widget_key, **dep}

    def create(self, hotel_code: str, body: Dict[str, Any]) -> Dict[str, Any]:
        hotel_code = self._hotel_code(hotel_code)
        record = self._normalize_record(hotel_code, body or {})
        record["created_at"] = _now_iso()
        record["updated_at"] = record["created_at"]
        with self._lock:
            data = self._load()
            key = self._gen_key()
            while key in data["deployments"]:
                key = self._gen_key()
            data["deployments"][key] = record
            self._persist()
            return {"widget_key": key, **record}

    def update(self, widget_key: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            existing = data["deployments"].get(widget_key)
            if not existing:
                return None
            hotel_code = self._hotel_code(body.get("hotel_code") or existing.get("hotel_code"))
            merged = {**existing, **(body or {})}
            record = self._normalize_record(hotel_code, merged)
            record["created_at"] = existing.get("created_at") or _now_iso()
            record["updated_at"] = _now_iso()
            data["deployments"][widget_key] = record
            self._persist()
            return {"widget_key": widget_key, **record}

    def delete(self, widget_key: str) -> bool:
        with self._lock:
            data = self._load()
            if widget_key not in data["deployments"]:
                return False
            data["deployments"].pop(widget_key, None)
            self._persist()
            return True

    def rotate_key(self, widget_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            existing = data["deployments"].get(widget_key)
            if not existing:
                return None
            new_key = self._gen_key()
            while new_key in data["deployments"]:
                new_key = self._gen_key()
            data["deployments"].pop(widget_key, None)
            existing["updated_at"] = _now_iso()
            data["deployments"][new_key] = existing
            self._persist()
            return {"widget_key": new_key, **existing}

    # ---- Bootstrap (public, served to the loader) ------------------------

    def bootstrap_payload(self, widget_key: str, host_origin: str = "") -> Optional[Dict[str, Any]]:
        """Return the public, embed-safe config the loader needs to mount the iframe."""
        dep = self.get(widget_key)
        if not dep or dep.get("status") != "active":
            return None

        host = (host_origin or "").rstrip("/")
        chat_url = f"{host}/chat" if host else "/chat"

        return {
            "widget_key": widget_key,
            "hotel_code": dep["hotel_code"],
            "bot_name": dep["bot_name"],
            "phase": dep["phase"],
            "theme": dep["theme"],
            "size": dep["size"],
            "position": dep["position"],
            "auto_open": dep["auto_open"],
            "iframe_url": chat_url,
        }

    # ---- Origin enforcement ---------------------------------------------

    def origin_allowed(self, widget_key: str, origin: str) -> bool:
        """Check whether `origin` is in the deployment's allowlist."""
        dep = self.get(widget_key)
        if not dep:
            return False
        allowed = dep.get("allowed_origins") or []
        if not allowed:
            # Empty allowlist = wildcard (only useful for dev; admins should set domains).
            return True
        if "*" in allowed:
            return True
        normalized = _normalize_origin(origin)
        return bool(normalized) and normalized in allowed


widget_deployment_service = WidgetDeploymentService()
