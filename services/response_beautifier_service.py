"""
Response Beautifier Service

Normalizes guest-facing response text so chat output is clean and consistent.
"""

from __future__ import annotations

import re


class ResponseBeautifierService:
    """Format outbound assistant text for cleaner chat presentation."""

    _COLLECTION_REQUEST_PATTERN = re.compile(
        r"(?P<lemma>(?:could\s+you\s+)?(?:please|kindly)\s+share(?:\s+your)?\s+)"
        r"(?P<fields>[^.?!\n]+)"
        r"(?P<end>[.?!]?)",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _strip_markdown_emphasis(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", cleaned)
        cleaned = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _normalize_bullet_markers(text: str) -> str:
        lines: list[str] = []
        for raw_line in str(text or "").split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                lines.append("")
                continue
            if re.match(r"^[-*]\s+", stripped):
                item = re.sub(r"^[-*]\s+", "", stripped)
                lines.append(f"- {item}")
                continue
            if stripped.startswith("• "):
                lines.append(f"- {stripped[2:].strip()}")
                continue
            lines.append(raw_line.rstrip())
        return "\n".join(lines)

    @staticmethod
    def _split_detail_fields(raw_fields: str) -> list[str]:
        fields = str(raw_fields or "").strip()
        if not fields:
            return []

        # Remove trailing purpose clause for cleaner bullets.
        fields = re.split(
            r"\bso\s+(?:that\s+)?(?:i|we)\b|\bto\s+(?:help|proceed|continue|forward)\b",
            fields,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,:;")
        if not fields:
            return []

        normalized = re.sub(r"\s+\band\b\s+", ", ", fields, flags=re.IGNORECASE)
        parts = [segment.strip(" .,:;") for segment in normalized.split(",")]
        cleaned_parts: list[str] = []
        for part in parts:
            if not part:
                continue
            item = re.sub(r"^(?:the|your)\s+", "", part, flags=re.IGNORECASE).strip(" .,:;")
            if not item:
                continue
            cleaned_parts.append(item)
        return cleaned_parts

    def _format_collection_request(self, text: str) -> str:
        source = str(text or "")
        match = self._COLLECTION_REQUEST_PATTERN.search(source)
        if not match:
            return source

        fields_segment = str(match.group("fields") or "")
        example_note = ""
        example_parts = re.findall(r"\(([^)]*)\)", fields_segment)
        if example_parts:
            for raw_note in example_parts:
                if re.search(r"(?:for\s+example|e\.?g\.?)", raw_note, flags=re.IGNORECASE):
                    example_note = re.sub(
                        r"^(?:for\s+example|e\.?g\.?)[\s:.-]*",
                        "",
                        raw_note.strip(),
                        flags=re.IGNORECASE,
                    ).strip()
                    break
        fields_without_notes = re.sub(r"\([^)]*\)", "", fields_segment).strip()

        parts = self._split_detail_fields(fields_without_notes)
        if len(parts) < 2:
            return source

        prefix = source[: match.start()].rstrip().rstrip(",;:")
        suffix = source[match.end() :].lstrip()

        bullets = "\n".join(f"- {item}" for item in parts)
        replacement = f"Please share the following details:\n{bullets}"
        if example_note:
            replacement = f"{replacement}\nExample: {example_note}"

        rebuilt = replacement
        if prefix:
            rebuilt = f"{prefix}\n{replacement}"
        if suffix:
            rebuilt = f"{rebuilt}\n{suffix}"
        return rebuilt

    @staticmethod
    def _cleanup_spacing(text: str) -> str:
        compact = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        compact = re.sub(r"[ \t]+\n", "\n", compact)
        return compact.strip()

    def beautify_response_text(self, text: str) -> str:
        if not str(text or "").strip():
            return ""
        cleaned = self._strip_markdown_emphasis(text)
        cleaned = self._normalize_bullet_markers(cleaned)
        cleaned = self._format_collection_request(cleaned)
        cleaned = self._cleanup_spacing(cleaned)
        return cleaned


response_beautifier_service = ResponseBeautifierService()
