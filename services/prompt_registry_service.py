"""
Prompt Registry Service

Single source of truth for every customizable prompt used by the bot.
Reads from the DB (table: new_bot_prompt_registry) with a simple in-process cache.

Resolution order:
    1. hotel override  (hotel_id = current hotel)
    2. industry default (industry = hotel.business.type, hotel_id NULL)
    3. raise PromptMissingError

Local files in prompts/defaults/<industry>/<key>.md are the *seed source* for industry
defaults. Claude (or anyone) can edit those files; on startup the seed service syncs
them into the DB (non-destructive to hotel overrides). See prompt_seed_service.py.
"""

from __future__ import annotations

import logging
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AsyncSessionLocal, BusinessConfig, Hotel, PromptRegistry

logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 300.0
SUPPORTED_INDUSTRIES = ("hotel", "restaurant", "spa", "clinic", "retail")
DEFAULT_INDUSTRY = "hotel"


class PromptMissingError(RuntimeError):
    """Raised when no hotel override and no industry default exists for a key."""

    def __init__(self, key: str, hotel_id: Optional[int], industry: Optional[str]) -> None:
        super().__init__(
            f"Prompt missing: key={key!r} hotel_id={hotel_id} industry={industry!r}. "
            "Seed it via prompts/defaults/<industry>/<key>.md or create a hotel override."
        )
        self.key = key
        self.hotel_id = hotel_id
        self.industry = industry


class _SafeFormatMap(dict):
    """
    Missing variables render as '{var}' instead of raising KeyError.
    Keeps prompts resilient when a call site forgets to pass a variable.
    """

    def __missing__(self, key: str) -> str:  # noqa: D401
        logger.warning("prompt_registry: missing variable %r at format time", key)
        return "{" + key + "}"


@dataclass
class PromptRecord:
    key: str
    content: str
    industry: Optional[str]
    hotel_id: Optional[int]
    version: int
    variables: List[str] = field(default_factory=list)
    description: Optional[str] = None


@dataclass
class EffectivePrompt:
    """
    What the UI shows for one prompt key, resolved for a given hotel.
    """

    key: str
    content: str
    source: str  # "hotel_override" | "industry_default"
    industry: Optional[str]
    has_override: bool
    industry_default_content: Optional[str]
    variables: List[str]
    description: Optional[str]
    version: int


class PromptRegistryService:
    def __init__(self) -> None:
        # Cache key: (hotel_id_or_0, key) -> (content, cached_at_seconds)
        self._cache: Dict[Tuple[int, str], Tuple[str, float]] = {}
        self._epoch: int = 0  # bumped on every write -> invalidates stale cache entries

    # ---------- internal helpers ----------

    def _cache_get(self, hotel_id: Optional[int], key: str) -> Optional[str]:
        slot = (int(hotel_id or 0), key)
        hit = self._cache.get(slot)
        if not hit:
            return None
        content, cached_at = hit
        if (time.monotonic() - cached_at) > CACHE_TTL_SECONDS:
            self._cache.pop(slot, None)
            return None
        return content

    def _cache_put(self, hotel_id: Optional[int], key: str, content: str) -> None:
        slot = (int(hotel_id or 0), key)
        self._cache[slot] = (content, time.monotonic())

    def _invalidate_all(self) -> None:
        self._cache.clear()
        self._epoch += 1

    @staticmethod
    def _extract_variables(template: str) -> List[str]:
        """Return the list of {name} placeholders in a template (no duplicates)."""
        found: List[str] = []
        formatter = string.Formatter()
        try:
            for _, field_name, _, _ in formatter.parse(template):
                if field_name and field_name not in found:
                    # Ignore positional / index fields
                    if not field_name[0].isdigit():
                        found.append(field_name)
        except Exception:
            return []
        return found

    async def _resolve_hotel_and_industry(self) -> Tuple[Optional[int], Optional[str]]:
        """
        Look up the current hotel's id and industry (business.type).

        Returns (None, None) if no hotel scope is set — in that case resolution
        falls back to DEFAULT_INDUSTRY.
        """
        # Lazy import avoids a cycle at module load time.
        from services.db_config_service import db_config_service

        try:
            hotel_id = await db_config_service.get_current_hotel_id()
        except Exception:
            hotel_id = None

        industry: Optional[str] = None
        if hotel_id:
            try:
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(BusinessConfig.config_value).where(
                            and_(
                                BusinessConfig.hotel_id == hotel_id,
                                BusinessConfig.config_key == "business.type",
                            )
                        )
                    )
                    raw = result.scalar_one_or_none()
                    if raw:
                        industry = str(raw).strip().lower() or None
            except Exception:
                industry = None

        if not industry:
            industry = DEFAULT_INDUSTRY

        return hotel_id, industry

    async def _fetch_row(
        self,
        session: AsyncSession,
        *,
        key: str,
        hotel_id: Optional[int],
        industry: Optional[str],
    ) -> Optional[PromptRegistry]:
        """Fetch a single registry row scoped by (key, hotel_id OR industry)."""
        stmt = select(PromptRegistry).where(
            and_(
                PromptRegistry.prompt_key == key,
                PromptRegistry.is_active == True,  # noqa: E712
            )
        )
        if hotel_id is not None:
            stmt = stmt.where(PromptRegistry.hotel_id == hotel_id)
        else:
            stmt = stmt.where(PromptRegistry.hotel_id.is_(None))
            if industry is not None:
                stmt = stmt.where(PromptRegistry.industry == industry)
            else:
                stmt = stmt.where(PromptRegistry.industry.is_(None))
        stmt = stmt.limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    # ---------- public API ----------

    async def get(
        self,
        key: str,
        variables: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """
        Resolve a prompt: hotel override -> industry default -> raise.
        When `variables` is provided, the result is .format_map-substituted.
        """
        hotel_id, industry = await self._resolve_hotel_and_industry()

        cached = self._cache_get(hotel_id, key)
        if cached is not None:
            return cached.format_map(_SafeFormatMap(variables or {})) if variables else cached

        async with AsyncSessionLocal() as session:
            row: Optional[PromptRegistry] = None
            if hotel_id:
                row = await self._fetch_row(session, key=key, hotel_id=hotel_id, industry=None)
            if row is None:
                row = await self._fetch_row(session, key=key, hotel_id=None, industry=industry)

        if row is None:
            raise PromptMissingError(key=key, hotel_id=hotel_id, industry=industry)

        content = str(row.content or "")
        self._cache_put(hotel_id, key, content)
        return content.format_map(_SafeFormatMap(variables or {})) if variables else content

    async def get_raw(
        self,
        key: str,
        *,
        hotel_id: Optional[int] = None,
        industry: Optional[str] = None,
    ) -> Optional[PromptRecord]:
        """Fetch one specific row (no fallback chain). Returns None if not found."""
        async with AsyncSessionLocal() as session:
            row = await self._fetch_row(session, key=key, hotel_id=hotel_id, industry=industry)
            if row is None:
                return None
            return self._row_to_record(row)

    async def upsert(
        self,
        key: str,
        content: str,
        *,
        hotel_id: Optional[int] = None,
        industry: Optional[str] = None,
        variables: Optional[List[str]] = None,
        description: Optional[str] = None,
        updated_by: Optional[str] = None,
        seeded_from_file_hash: Optional[str] = None,
    ) -> PromptRecord:
        """
        Insert or update a registry row. Exactly one of (hotel_id, industry) should be set
        for typical usage: hotel_id for hotel overrides, industry for industry defaults.
        """
        if hotel_id is None and industry is None:
            raise ValueError("upsert requires either hotel_id or industry")
        async with AsyncSessionLocal() as session:
            row = await self._fetch_row(
                session, key=key, hotel_id=hotel_id, industry=industry if hotel_id is None else None
            )
            if row is None:
                row = PromptRegistry(
                    prompt_key=key,
                    industry=industry,
                    hotel_id=hotel_id,
                    content=content,
                    variables=variables or self._extract_variables(content),
                    description=description,
                    updated_by=updated_by,
                    seeded_from_file_hash=seeded_from_file_hash,
                    version=1,
                    is_active=True,
                )
                session.add(row)
            else:
                row.content = content
                row.variables = variables if variables is not None else self._extract_variables(content)
                if description is not None:
                    row.description = description
                if updated_by is not None:
                    row.updated_by = updated_by
                if seeded_from_file_hash is not None:
                    row.seeded_from_file_hash = seeded_from_file_hash
                row.version = int(row.version or 1) + 1
                row.is_active = True
            await session.commit()
            await session.refresh(row)
            record = self._row_to_record(row)

        self._invalidate_all()
        return record

    async def delete_override(self, key: str, hotel_id: int) -> bool:
        """Drop a hotel override row. Returns True when a row was deleted."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(PromptRegistry).where(
                    and_(
                        PromptRegistry.prompt_key == key,
                        PromptRegistry.hotel_id == hotel_id,
                    )
                )
            )
            await session.commit()
            deleted = int(result.rowcount or 0) > 0

        if deleted:
            self._invalidate_all()
        return deleted

    async def list_effective_for_hotel(self, hotel_code: Optional[str] = None) -> List[EffectivePrompt]:
        """
        List every registered prompt resolved for the current (or given) hotel,
        annotated with whether the hotel has an override and the industry default.
        """
        from services.db_config_service import db_config_service

        if hotel_code:
            token = db_config_service.set_hotel_context(hotel_code)
        else:
            token = None
        try:
            hotel_id, industry = await self._resolve_hotel_and_industry()
        finally:
            if token is not None:
                db_config_service.reset_hotel_context(token)

        async with AsyncSessionLocal() as session:
            # Every distinct prompt_key we know about (defaults + overrides)
            result = await session.execute(select(PromptRegistry).where(PromptRegistry.is_active == True))  # noqa: E712
            rows: List[PromptRegistry] = list(result.scalars().all())

        # Group rows by key
        by_key: Dict[str, Dict[str, PromptRegistry]] = {}
        for row in rows:
            slot = by_key.setdefault(row.prompt_key, {})
            if row.hotel_id == hotel_id and row.hotel_id is not None:
                slot["override"] = row
            elif row.hotel_id is None and (row.industry == industry):
                slot["industry"] = row
            elif row.hotel_id is None and row.industry is None:
                slot.setdefault("global", row)

        effective: List[EffectivePrompt] = []
        for key, slot in sorted(by_key.items()):
            override = slot.get("override")
            industry_row = slot.get("industry") or slot.get("global")
            base = override or industry_row
            if base is None:
                continue
            industry_default_content = industry_row.content if industry_row else None
            effective.append(
                EffectivePrompt(
                    key=key,
                    content=str(base.content or ""),
                    source="hotel_override" if override else "industry_default",
                    industry=base.industry,
                    has_override=override is not None,
                    industry_default_content=(
                        str(industry_default_content) if industry_default_content is not None else None
                    ),
                    variables=list(base.variables or []),
                    description=base.description,
                    version=int(base.version or 1),
                )
            )
        return effective

    async def regenerate_from_instruction(
        self,
        key: str,
        instruction: str,
        *,
        hotel_code: Optional[str] = None,
    ) -> str:
        """
        Take the currently-effective prompt for `key` and ask an LLM to rewrite it
        per the user's plain-language instruction. Returns the rewrite WITHOUT saving.
        """
        from llm.client import llm_client

        instruction_text = str(instruction or "").strip()
        if not instruction_text:
            raise ValueError("instruction is required")

        # Get currently effective content (fresh — don't use cache to avoid staleness)
        self._invalidate_all()
        current = await self.get(key)

        system = (
            "You are rewriting a production system prompt for a customer-service chatbot. "
            "The operator gives you a short plain-English instruction describing what they want changed. "
            "Apply only the requested change and keep everything else intact.\n\n"
            "Hard rules:\n"
            "1. Preserve every {variable} placeholder EXACTLY as-is (same spelling, same braces, same positions).\n"
            "2. Preserve the structural shape: if the original has numbered steps, rules, or sections, "
            "keep the same sections and numbering unless the instruction asks you to change them.\n"
            "3. Preserve any JSON schema / output-contract requirements verbatim — do not rephrase them.\n"
            "4. Do not add markdown fences, commentary, or explanations. Output ONLY the rewritten prompt text.\n"
            "5. If the instruction is ambiguous, make the minimal reasonable change and keep everything else.\n"
            "6. Never remove safety / refusal / PII rules unless the instruction explicitly says so.\n"
        )
        user = (
            f"PROMPT KEY: {key}\n\n"
            f"CURRENT PROMPT:\n<<<BEGIN>>>\n{current}\n<<<END>>>\n\n"
            f"OPERATOR INSTRUCTION:\n{instruction_text}\n\n"
            "Return ONLY the rewritten prompt text between the same triple-angle markers if present, "
            "otherwise return only the rewritten text with no preamble."
        )

        raw = await llm_client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=4000,
            trace_context={
                "responder_type": "admin",
                "agent": "prompt_regenerator",
                "prompt_key": key,
            },
        )
        rewrite = str(raw or "").strip()

        # Strip any accidental code fences or angle markers the LLM might add.
        if rewrite.startswith("```"):
            parts = rewrite.split("```")
            if len(parts) >= 3:
                rewrite = parts[1]
                # Drop an optional language hint on the first line.
                if "\n" in rewrite:
                    first_line, rest = rewrite.split("\n", 1)
                    if len(first_line) < 20 and " " not in first_line:
                        rewrite = rest
                rewrite = rewrite.strip()
        if rewrite.startswith("<<<BEGIN>>>"):
            rewrite = rewrite[len("<<<BEGIN>>>") :].lstrip("\n")
        if rewrite.endswith("<<<END>>>"):
            rewrite = rewrite[: -len("<<<END>>>")].rstrip("\n")

        return rewrite or current

    # ---------- mapping ----------

    @staticmethod
    def _row_to_record(row: PromptRegistry) -> PromptRecord:
        variables = list(row.variables or []) if isinstance(row.variables, list) else []
        return PromptRecord(
            key=row.prompt_key,
            content=str(row.content or ""),
            industry=row.industry,
            hotel_id=row.hotel_id,
            version=int(row.version or 1),
            variables=variables,
            description=row.description,
        )


# Module-level singleton (consistent with db_config_service / config_service pattern)
prompt_registry = PromptRegistryService()
