"""
Form Fields Service

Extracts structured form field definitions from a service's ticketing_conditions.

Primary method: regex parsing (fast, no API call, always works).
Enhancement: LLM pass to improve labels/types when regex produces rough results.

Results are cached in memory by service_id for the lifetime of the process.
New services automatically get forms the first time they trigger a collect_info turn.
"""

import json
import logging
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client

logger = logging.getLogger(__name__)

# In-memory cache: service_id -> list of field dicts
_form_fields_cache: dict[str, list[dict[str, Any]]] = {}

# ── Type heuristics ────────────────────────────────────────────────────────────

_DATE_WORDS   = {"date", "check-in", "check-out", "checkin", "checkout",
                 "arrival", "departure", "start", "end", "check in", "check out"}
_TIME_WORDS   = {"time", "appointment time", "hour"}
_TEL_WORDS    = {"phone", "mobile", "contact number", "telephone", "phone number",
                 "contact information", "contact info", "contact"}
_NUMBER_WORDS = {"number of guests", "guests", "no. of guests", "count",
                 "quantity", "pax", "persons", "people"}
_EMAIL_WORDS  = {"email", "email address", "e-mail"}
_AREA_WORDS   = {"description", "issue", "notes", "details", "message",
                 "brief description", "reason", "comment"}


def _infer_type(raw: str) -> str:
    low = raw.lower().strip()
    if any(w in low for w in _DATE_WORDS):
        return "date"
    if any(w in low for w in _TIME_WORDS):
        return "time"
    if any(w in low for w in _NUMBER_WORDS):
        return "number"
    if any(w in low for w in _EMAIL_WORDS):
        return "email"
    if any(w in low for w in _TEL_WORDS):
        return "tel"
    if any(w in low for w in _AREA_WORDS):
        return "textarea"
    return "text"


def _to_label(raw: str) -> str:
    """Convert raw field phrase to a title-case label."""
    cleaned = re.sub(r"\b(desired|preferred|specific|their|the guest.?s?|a brief)\b", "", raw, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
    return cleaned.title() if cleaned else raw.title()


def _to_id(label: str) -> str:
    """Convert label to a snake_case id."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ── Regex extraction ───────────────────────────────────────────────────────────

# Patterns to find the enumerated fields inside a ticketing_conditions sentence.
_FIELD_LIST_PATTERNS = [
    r"by providing (?:their |the (?:guest(?:'s)? ))?(.+?)(?:\.|$)",
    r"ensuring (?:the )?(?:guest(?:'s)? )?(.+?)(?:are|is) collected",
    r"(?:collecting|collect|collected) (?:the )?(?:following )?(?:details?|information|fields?)?[:\s]+(.+?)(?:\.|$)",
    r"after (?:the )?(?:guest )?(?:provides?|confirming|submitting) (.+?)(?:\.|$)",
    r"(?:requires?|requiring|must include)[:\s]+(.+?)(?:\.|$)",
]


def _regex_extract(ticketing_conditions: str) -> list[dict[str, Any]]:
    """
    Parse ticketing_conditions text with regex and return a list of field dicts.
    Returns an empty list if the text doesn't match any known pattern.
    """
    for pattern in _FIELD_LIST_PATTERNS:
        m = re.search(pattern, ticketing_conditions, re.IGNORECASE | re.DOTALL)
        if not m:
            continue

        raw_list = m.group(1).strip()

        # Split on commas / semicolons / " and "
        parts = re.split(r",\s*(?:and\s+)?|;\s*(?:and\s+)?|\s+and\s+", raw_list)

        fields: list[dict[str, Any]] = []
        for part in parts:
            part = part.strip(" .,;")
            # Remove parenthetical clarifications like "(phone number and email)"
            part = re.sub(r"\s*\(.*?\)", "", part).strip()
            if not part or len(part) < 2:
                continue

            label = _to_label(part)
            if not label:
                continue

            field_id = _to_id(label)
            field_type = _infer_type(part)

            fields.append({
                "id": field_id,
                "label": label,
                "type": field_type,
                "required": True,
            })

        if fields:
            return fields

    return []


# ── LLM extraction (enhancement, not primary) ─────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = (
    "You are a form schema extractor. Given a ticketing condition description, "
    "extract the required fields as a JSON array.\n\n"
    "Each field object must have exactly these keys:\n"
    '  "id"       : snake_case identifier (e.g. "full_name", "checkin_date")\n'
    '  "label"    : human-readable label (e.g. "Full Name", "Check-in Date")\n'
    '  "type"     : one of "text", "date", "time", "number", "tel", "email", "textarea"\n'
    '  "required" : true or false\n\n'
    "Type assignment rules:\n"
    '- check-in, check-out, date, arrival, departure -> "date"\n'
    '- time, appointment time -> "time"\n'
    '- phone, contact, mobile -> "tel"\n'
    '- number of guests, count -> "number"\n'
    '- email -> "email"\n'
    '- description, issue, notes, details -> "textarea"\n'
    "- everything else -> \"text\"\n\n"
    "Return ONLY a valid JSON array, no markdown fences, no explanation."
)


async def _llm_extract(ticketing_conditions: str) -> list[dict[str, Any]]:
    """Attempt LLM-based extraction. Returns [] on any failure."""
    try:
        model = str(getattr(settings, "openai_model", None) or "gpt-4o-mini")

        completion = await llm_client.raw_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Extract form fields from this ticketing condition:\n{ticketing_conditions}"
                    ),
                },
            ],
            temperature=0,
            max_tokens=600,
            trace_context={
                "component": "form_fields_extraction",
                "service_id": "form_fields_service",
            },
            purpose="Extract structured form fields from ticketing conditions",
        )

        raw = str(completion.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        validated: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            fid   = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            ftype = str(item.get("type") or "text").strip()
            req   = bool(item.get("required", True))
            if not fid or not label:
                continue
            if ftype not in {"text", "date", "time", "number", "tel", "email", "textarea"}:
                ftype = "text"
            validated.append({"id": fid, "label": label, "type": ftype, "required": req})

        return validated

    except Exception as exc:
        logger.debug("form_fields_service: LLM extraction skipped: %s", exc)
        return []


# ── Public API ─────────────────────────────────────────────────────────────────

async def extract_form_fields(service: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract form fields from a service.

    If the service has ticketing_mode=form and form_config.fields defined,
    those are used directly (admin-configured). Otherwise falls back to
    regex/LLM extraction from ticketing_conditions text.

    Results are cached by service_id; LLM is called at most once per service
    per server restart.
    """
    service_id = str(service.get("id") or "").strip().lower()
    if not service_id:
        return []

    if service_id in _form_fields_cache:
        return _form_fields_cache[service_id]

    # ── Fast path: admin-configured form_config fields ────────────────────
    ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
    form_config = service.get("form_config")
    if ticketing_mode == "form" and isinstance(form_config, dict):
        configured_fields = form_config.get("fields")
        if isinstance(configured_fields, list) and configured_fields:
            fields = []
            for f in configured_fields:
                if not isinstance(f, dict):
                    continue
                fid = str(f.get("id") or "").strip()
                if not fid:
                    continue
                fields.append({
                    "id": fid,
                    "label": str(f.get("label") or fid).strip(),
                    "type": str(f.get("type") or "text").strip(),
                    "required": bool(f.get("required", True)),
                })
            if fields:
                logger.info(
                    "form_fields_service: using %d admin-configured field(s) for '%s'",
                    len(fields), service_id,
                )
                _form_fields_cache[service_id] = fields
                return fields

    # ── Fallback: regex + LLM extraction from ticketing_conditions ────────
    prompt_pack = service.get("service_prompt_pack") or {}
    ticketing_conditions = str(
        service.get("ticketing_conditions")
        or (prompt_pack.get("ticketing_conditions") if isinstance(prompt_pack, dict) else "")
        or service.get("ticketing_policy")
        or ""
    ).strip()

    if not ticketing_conditions:
        logger.debug("form_fields_service: no ticketing_conditions for '%s'", service_id)
        _form_fields_cache[service_id] = []
        return []

    # Step 1 — regex
    regex_fields = _regex_extract(ticketing_conditions)
    logger.info(
        "form_fields_service: regex extracted %d field(s) for '%s'",
        len(regex_fields), service_id,
    )

    # Step 2 — if regex gave enough fields, we're done
    if len(regex_fields) >= 2:
        _form_fields_cache[service_id] = regex_fields
        return regex_fields

    # Step 3 — try LLM for a richer result
    llm_fields = await _llm_extract(ticketing_conditions)
    if llm_fields:
        logger.info(
            "form_fields_service: LLM extracted %d field(s) for '%s'",
            len(llm_fields), service_id,
        )
        _form_fields_cache[service_id] = llm_fields
        return llm_fields

    # Step 4 — fall back to regex even if it returned only 1 field
    _form_fields_cache[service_id] = regex_fields
    return regex_fields


def invalidate_cache(service_id: str | None = None) -> None:
    """
    Invalidate the cached form fields for one service or for all services.
    Call when a service's ticketing_conditions are updated.
    """
    global _form_fields_cache
    if service_id:
        _form_fields_cache.pop(str(service_id).strip().lower(), None)
    else:
        _form_fields_cache.clear()
