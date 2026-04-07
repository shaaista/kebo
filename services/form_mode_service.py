from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any


_ALIAS_SUFFIXES = ("choice", "option", "selection", "selected", "chosen", "value")
_ALIAS_PREFIXES = ("selected", "chosen")
_ROOT_SUFFIXES = (
    "_choice",
    "_option",
    "_selection",
    "_selected",
    "_chosen",
    "_value",
    "_type",
)
_CONFIRMATION_COMMAND_HINTS = (
    "yes",
    "confirm",
    "ok",
    "okay",
    "proceed",
    "go ahead",
    "go_ahead",
    "book",
)
_CONFIRMATION_INSTRUCTION_PATTERNS = (
    re.compile(
        r"(?P<body>(?:please\s+)?(?:reply|respond|type|say)\s+(?:with\s+)?"
        r"(?P<quote>['\"]?)(?P<phrase>[a-z][a-z0-9\s-]{0,40})(?P=quote)\s+to\s+"
        r"(?:confirm|proceed|continue|finali[sz]e|book)(?:[^.!?\n]*)[.!?]?)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?P<body>(?:please\s+)?(?:reply|respond|type|say)\s+(?:with\s+)?"
        r"(?P<quote>['\"]?)(?P<phrase>yes(?:\s+confirm)?|confirm|ok(?:ay)?|proceed|go\s+ahead)"
        r"(?P=quote)[.!?]?)",
        flags=re.IGNORECASE,
    ),
)
_TRIGGER_CANDIDATE_KEY_SUFFIXES = ("candidates", "options", "choices", "values", "list")
_TRIGGER_CANDIDATE_KEY_PREFIXES = ("available", "supported", "allowed")
_TRIGGER_MATCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "or",
    "the",
    "to",
    "with",
    "please",
    "book",
    "booking",
    "select",
    "selected",
    "choose",
    "chosen",
    "option",
    "options",
}


def normalize_form_field_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_")


def _append_unique(items: list[str], value: Any) -> None:
    text = normalize_form_field_key(value)
    if text and text not in items:
        items.append(text)


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(str(value).strip())


def build_trigger_field_aliases(field_id: Any, field_label: Any = "") -> list[str]:
    roots: list[str] = []
    for raw in (field_id, field_label):
        normalized = normalize_form_field_key(raw)
        if not normalized:
            continue
        _append_unique(roots, normalized)
        for suffix in _ROOT_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                _append_unique(roots, normalized[: -len(suffix)])

    aliases: list[str] = []
    for root in roots:
        _append_unique(aliases, root)
        for suffix in _ALIAS_SUFFIXES:
            _append_unique(aliases, f"{root}_{suffix}")
        for prefix in _ALIAS_PREFIXES:
            _append_unique(aliases, f"{prefix}_{root}")
    return aliases


def resolve_trigger_field_value(
    data: Any,
    field_id: Any,
    field_label: Any = "",
) -> tuple[str, Any]:
    if not isinstance(data, dict):
        return "", None

    aliases = build_trigger_field_aliases(field_id, field_label)
    if not aliases:
        return "", None

    by_normalized: dict[str, tuple[str, Any]] = {}
    for raw_key, value in data.items():
        normalized = normalize_form_field_key(raw_key)
        if not normalized or not _value_present(value):
            continue
        by_normalized.setdefault(normalized, (str(raw_key), value))

    for alias in aliases:
        matched = by_normalized.get(alias)
        if matched:
            return matched
    return "", None


def canonicalize_trigger_pending_data(
    data: Any,
    field_id: Any,
    field_label: Any = "",
) -> tuple[dict[str, Any], str, Any]:
    if not isinstance(data, dict):
        return {}, "", None

    canonical = normalize_form_field_key(field_id) or normalize_form_field_key(field_label)
    aliases = set(build_trigger_field_aliases(field_id, field_label))
    matched_key, matched_value = resolve_trigger_field_value(data, field_id, field_label)

    cleaned: dict[str, Any] = {}
    for raw_key, value in data.items():
        normalized = normalize_form_field_key(raw_key)
        if normalized in aliases:
            continue
        cleaned[str(raw_key)] = value

    if canonical and _value_present(matched_value):
        cleaned[canonical] = matched_value

    return cleaned, matched_key, matched_value


def field_matches_trigger(field_name: Any, field_id: Any, field_label: Any = "") -> bool:
    normalized = normalize_form_field_key(field_name)
    if not normalized:
        return False
    return normalized in set(build_trigger_field_aliases(field_id, field_label))


def normalize_trigger_missing_fields(
    missing_fields: Any,
    field_id: Any,
    field_label: Any = "",
    *,
    trigger_value_present: bool = False,
) -> list[str]:
    values = missing_fields if isinstance(missing_fields, (list, tuple)) else []
    canonical = normalize_form_field_key(field_id) or normalize_form_field_key(field_label)
    aliases = set(build_trigger_field_aliases(field_id, field_label))
    normalized_fields: list[str] = []
    seen: set[str] = set()

    for item in values:
        key = normalize_form_field_key(item)
        if not key:
            continue
        if key in aliases:
            if trigger_value_present:
                continue
            key = canonical or key
        if key not in seen:
            seen.add(key)
            normalized_fields.append(key)
    return normalized_fields


def _iter_trigger_candidate_values(value: Any) -> list[str]:
    candidates: list[str] = []

    def _append(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        if text not in candidates:
            candidates.append(text)

    if isinstance(value, str):
        text = value.strip()
        if text:
            if "," in text:
                for token in text.split(","):
                    _append(token)
            else:
                _append(text)
        return candidates

    if isinstance(value, dict):
        for item in value.values():
            for flattened in _iter_trigger_candidate_values(item):
                _append(flattened)
        return candidates

    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                for key in ("label", "name", "title", "value", "id", "text"):
                    if str(item.get(key) or "").strip():
                        _append(item.get(key))
                        break
                continue
            _append(item)
        return candidates

    _append(value)
    return candidates


def _extract_trigger_candidates_from_data(
    data: dict[str, Any],
    aliases: set[str],
) -> list[str]:
    if not aliases:
        return []

    values: list[str] = []
    for raw_key, raw_value in data.items():
        key = normalize_form_field_key(raw_key)
        if not key:
            continue
        if key in aliases:
            continue
        collection_value = isinstance(raw_value, (list, tuple, set, dict))
        alias_match = any(alias and alias in key for alias in aliases)
        suffix_match = any(key.endswith(f"_{suffix}") for suffix in _TRIGGER_CANDIDATE_KEY_SUFFIXES)
        prefix_match = any(key.startswith(f"{prefix}_") for prefix in _TRIGGER_CANDIDATE_KEY_PREFIXES)
        if not suffix_match and not prefix_match and not (alias_match and collection_value):
            continue
        for candidate in _iter_trigger_candidate_values(raw_value):
            normalized = normalize_form_field_key(candidate)
            if not normalized:
                continue
            if candidate not in values:
                values.append(candidate)
    return values


def _candidate_match_score(message_key: str, candidate_key: str) -> float:
    if not message_key or not candidate_key:
        return 0.0
    if candidate_key in message_key:
        return 1.0

    msg_tokens = [token for token in message_key.split("_") if token]
    cand_tokens = [token for token in candidate_key.split("_") if token and token not in _TRIGGER_MATCH_STOPWORDS]
    if cand_tokens and all(token in msg_tokens for token in cand_tokens):
        return 0.94

    # Fuzzy window match handles minor typos inside a longer message string.
    best = 0.0
    window_size = max(1, len(cand_tokens) if cand_tokens else len(candidate_key.split("_")))
    window_size += 2
    for idx in range(len(msg_tokens)):
        chunk = "_".join(msg_tokens[idx : idx + window_size])
        if not chunk:
            continue
        ratio = SequenceMatcher(a=candidate_key, b=chunk).ratio()
        if ratio > best:
            best = ratio
    return best


def infer_trigger_value_from_message(
    data: Any,
    field_id: Any,
    field_label: Any = "",
    message: Any = "",
) -> tuple[str, Any]:
    """
    Best-effort fallback when LLM did not emit trigger value in pending_data_updates.
    Matches the user's message against candidate lists carried in pending data.
    """
    if not isinstance(data, dict):
        return "", None
    message_key = normalize_form_field_key(message)
    if not message_key:
        return "", None

    aliases = set(build_trigger_field_aliases(field_id, field_label))
    if not aliases:
        return "", None

    candidates = _extract_trigger_candidates_from_data(data, aliases)
    if not candidates:
        return "", None

    best_value: str = ""
    best_score = 0.0
    for candidate in candidates:
        candidate_key = normalize_form_field_key(candidate)
        if not candidate_key:
            continue
        score = _candidate_match_score(message_key, candidate_key)
        if score > best_score:
            best_score = score
            best_value = candidate

    if best_score >= 0.82 and best_value:
        return normalize_form_field_key(field_id), best_value
    return "", None


def _should_strip_confirmation_phrase(phrase: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", " ", str(phrase or "").strip().lower()).strip()
    if not compact:
        return False
    return any(token in compact for token in _CONFIRMATION_COMMAND_HINTS)


def strip_form_confirmation_instructions(text: Any) -> str:
    content = str(text or "")
    if not content.strip():
        return content

    cleaned = content
    for pattern in _CONFIRMATION_INSTRUCTION_PATTERNS:
        cleaned = pattern.sub(
            lambda match: "" if _should_strip_confirmation_phrase(match.group("phrase")) else match.group("body"),
            cleaned,
        )

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.;!?])", r"\1", cleaned)
    cleaned = re.sub(r"([.!?])\s*[.!?]+", r"\1", cleaned)
    cleaned = re.sub(r"([.!?])([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
