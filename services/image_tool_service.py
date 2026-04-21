from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import delete, select

from config.settings import settings
from llm.client import llm_client
from models.database import AsyncSessionLocal, ImageAsset
from services.config_service import config_service
from services.db_config_service import db_config_service


SMART_IMAGE_TOOL_ID = "smart_image_gallery"
SMART_IMAGE_TOOL_HANDLER = "smart_image_selector"


class ImageToolService:
    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text.lower() in {"none", "nan", "null", "n/a", "na"}:
            return ""
        return text

    @staticmethod
    def _normalize_url(value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        if url.lower().startswith(("http://", "https://")):
            return url
        return ""

    @staticmethod
    def _derive_tags_from_title(title: str) -> list[str]:
        lowered = str(title or "").strip().lower()
        if not lowered:
            return []
        tokens = re.findall(r"[a-z0-9]+", lowered)
        stop = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "this",
            "that",
            "your",
            "room",
            "suite",
            "hotel",
            "iconiqa",
        }
        tags = [token for token in tokens if token not in stop and len(token) > 2]
        deduped: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            if tag in seen:
                continue
            seen.add(tag)
            deduped.append(tag)
            if len(deduped) >= 12:
                break
        return deduped

    @staticmethod
    def _title_keywords(title: str) -> list[str]:
        base = str(title or "").strip().lower()
        if not base:
            return []
        base = re.sub(r"iconiqa", " ", base)
        tokens = [tok for tok in re.findall(r"[a-z0-9]+", base) if len(tok) > 2]
        keywords: list[str] = []
        for tok in tokens:
            if tok not in keywords:
                keywords.append(tok)

        joined = "_".join(tokens)
        if joined and joined not in keywords:
            keywords.append(joined)
        if tokens:
            spaced = " ".join(tokens)
            if spaced not in keywords:
                keywords.append(spaced)

        mapping = {
            "lux": ["lux_suite", "lux suite"],
            "prestige": ["prestige_suite", "prestige suite"],
            "reserve": ["reserve_suite", "reserve suite"],
            "premier": ["premier_king", "premier_twin", "premier king", "premier twin"],
            "pool": ["swim club", "poolside", "bombay swim club"],
            "swim": ["swim club", "poolside", "bombay swim club"],
            "spa": ["spa", "wellness"],
            "suite": ["suite", "suites"],
        }
        expanded: list[str] = []
        for key in list(keywords):
            if key in mapping:
                expanded.extend(mapping[key])
        for term in expanded:
            if term not in keywords:
                keywords.append(term)
        return keywords[:14]

    @classmethod
    def _kb_snippet_map_for_images(
        cls,
        *,
        kb_text: str,
        selected_images: list[dict[str, Any]],
        max_chars_per_image: int = 950,
    ) -> dict[int, str]:
        normalized = str(kb_text or "").strip()
        if not normalized:
            return {}
        chunks = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n+", normalized) if str(part).strip()]
        if not chunks:
            return {}
        lower_chunks = [chunk.lower() for chunk in chunks]

        snippet_by_id: dict[int, str] = {}
        for image in selected_images:
            try:
                image_id = int(image.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if image_id <= 0:
                continue
            keywords = cls._title_keywords(str(image.get("title") or ""))
            if not keywords:
                continue
            scored: list[tuple[int, int]] = []
            for idx, chunk_lower in enumerate(lower_chunks):
                score = 0
                for kw in keywords:
                    if kw and kw in chunk_lower:
                        score += 1
                if score > 0:
                    scored.append((score, idx))
            if not scored:
                continue
            scored.sort(key=lambda row: (-row[0], row[1]))
            picked_parts: list[str] = []
            consumed = 0
            for _, idx in scored[:3]:
                text = chunks[idx]
                if not text:
                    continue
                remaining = max_chars_per_image - consumed
                if remaining <= 0:
                    break
                segment = text[:remaining].strip()
                if not segment:
                    continue
                picked_parts.append(segment)
                consumed += len(segment) + 1
            snippet = "\n".join(picked_parts).strip()
            if snippet:
                snippet_by_id[image_id] = snippet
        return snippet_by_id

    @staticmethod
    def _extract_tool_enabled(tools: Any) -> bool:
        if not isinstance(tools, list):
            return False
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = ImageToolService._normalize_identifier(tool.get("id"))
            handler = ImageToolService._normalize_identifier(tool.get("handler"))
            if tool_id == SMART_IMAGE_TOOL_ID or handler == SMART_IMAGE_TOOL_HANDLER:
                return bool(tool.get("enabled", True))
        return False

    async def is_enabled_for_hotel(self, hotel_code: str) -> bool:
        with db_config_service.scoped_hotel(hotel_code):
            tools = await db_config_service.get_tools()
        return self._extract_tool_enabled(tools)

    async def ensure_tool_registered(self, hotel_code: str, *, enabled: bool = True) -> bool:
        payload = {
            "id": SMART_IMAGE_TOOL_ID,
            "name": "Smart Image Gallery",
            "description": "LLM selects relevant hotel images to enrich assistant responses.",
            "type": "workflow",
            "handler": SMART_IMAGE_TOOL_HANDLER,
            "channels": ["web_widget", "whatsapp"],
            "enabled": bool(enabled),
            "requires_confirmation": False,
        }
        with db_config_service.scoped_hotel(hotel_code):
            return await db_config_service.add_tool(payload)

    async def upsert_assets(
        self,
        *,
        hotel_code: str,
        rows: list[dict[str, Any]],
        source_label: str = "",
        replace_existing: bool = True,
    ) -> dict[str, int]:
        normalized_rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row in rows:
            title = self._normalize_text(row.get("title"))
            image_url = self._normalize_url(row.get("image_url"))
            if not title or not image_url:
                continue
            if image_url.lower() in seen_urls:
                continue
            seen_urls.add(image_url.lower())
            description = self._normalize_text(row.get("description"))
            category = self._normalize_text(row.get("category"))
            raw_tags = row.get("tags")
            tags: list[str] = []
            if isinstance(raw_tags, list):
                tags = [self._normalize_identifier(tag) for tag in raw_tags if self._normalize_identifier(tag)]
            if not tags:
                tags = self._derive_tags_from_title(f"{title} {description}".strip())
            priority = row.get("priority")
            try:
                priority_value = int(priority) if priority not in (None, "") else 0
            except (TypeError, ValueError):
                priority_value = 0
            normalized_rows.append(
                {
                    "title": title,
                    "description": description or None,
                    "image_url": image_url,
                    "category": category or None,
                    "tags": tags,
                    "priority": priority_value,
                }
            )

        if not normalized_rows:
            return {"inserted": 0, "updated": 0, "skipped": len(rows)}

        with db_config_service.scoped_hotel(hotel_code):
            hotel_id = await db_config_service.get_current_hotel_id()

        inserted = 0
        updated = 0
        async with AsyncSessionLocal() as session:
            if replace_existing:
                await session.execute(
                    delete(ImageAsset).where(ImageAsset.hotel_id == hotel_id)
                )
                for item in normalized_rows:
                    row = ImageAsset(
                        hotel_id=hotel_id,
                        title=item["title"],
                        description=item["description"],
                        image_url=item["image_url"],
                        category=item["category"],
                        tags=item["tags"],
                        source_label=source_label or None,
                        is_active=True,
                        priority=item["priority"],
                    )
                    session.add(row)
                    inserted += 1
                await session.commit()
                return {"inserted": inserted, "updated": 0, "skipped": max(0, len(rows) - len(normalized_rows))}

            for item in normalized_rows:
                existing = await session.execute(
                    select(ImageAsset).where(
                        ImageAsset.hotel_id == hotel_id,
                        ImageAsset.image_url == item["image_url"],
                    )
                )
                row = existing.scalar_one_or_none()
                if row is None:
                    row = ImageAsset(
                        hotel_id=hotel_id,
                        title=item["title"],
                        description=item["description"],
                        image_url=item["image_url"],
                        category=item["category"],
                        tags=item["tags"],
                        source_label=source_label or None,
                        is_active=True,
                        priority=item["priority"],
                    )
                    session.add(row)
                    inserted += 1
                else:
                    row.title = item["title"]
                    row.description = item["description"]
                    row.category = item["category"]
                    row.tags = item["tags"]
                    row.source_label = source_label or row.source_label
                    row.is_active = True
                    row.priority = item["priority"]
                    updated += 1
            await session.commit()

        return {"inserted": inserted, "updated": updated, "skipped": max(0, len(rows) - len(normalized_rows))}

    async def list_candidate_assets(self, *, hotel_code: str, limit: int = 0) -> list[dict[str, Any]]:
        with db_config_service.scoped_hotel(hotel_code):
            hotel_id = await db_config_service.get_current_hotel_id()
        async with AsyncSessionLocal() as session:
            query = (
                select(ImageAsset)
                .where(
                    ImageAsset.hotel_id == hotel_id,
                    ImageAsset.is_active == True,  # noqa: E712
                )
                .order_by(ImageAsset.priority.desc(), ImageAsset.id.asc())
            )
            parsed_limit = int(limit or 0)
            if parsed_limit > 0:
                query = query.limit(parsed_limit)
            result = await session.execute(query)
            rows = result.scalars().all()
        assets: list[dict[str, Any]] = []
        for row in rows:
            assets.append(
                {
                    "id": int(row.id),
                    "title": str(row.title or "").strip(),
                    "description": str(row.description or "").strip(),
                    "image_url": str(row.image_url or "").strip(),
                    "category": str(row.category or "").strip(),
                    "tags": row.tags if isinstance(row.tags, list) else [],
                    "priority": int(row.priority or 0),
                }
            )
        return assets

    async def llm_select_images(
        self,
        *,
        hotel_code: str,
        user_message: str,
        assistant_message: str,
        intent: str = "",
        max_images: int = 0,
        trace_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return {"images": [], "selection_reason": "openai_key_missing", "tool_used": False}

        candidates = await self.list_candidate_assets(hotel_code=hotel_code, limit=0)
        if not candidates:
            return {"images": [], "selection_reason": "no_candidates", "tool_used": False}

        requested_max = int(max_images or 0)
        limit = len(candidates) if requested_max <= 0 else min(requested_max, len(candidates))
        if limit <= 0:
            return {"images": [], "selection_reason": "no_candidates", "tool_used": False}
        candidate_payload = [
            {
                "id": row["id"],
                "title": row["title"],
                "description": str(row["description"] or "").strip()[:140],
                "category": row["category"],
                "tags": (row["tags"] if isinstance(row.get("tags"), list) else [])[:8],
            }
            for row in candidates
        ]
        system_prompt = (
            "You select and ORDER hotel images for a chat response.\n"
            "Use the assistant answer as the primary source for what to show.\n"
            "The selected_ids list MUST follow the same sequence as topics appear in the assistant answer.\n"
            "If the assistant answer mentions multiple room types, include multiple room images.\n"
            "Prefer comprehensive relevant coverage over a tiny sample.\n"
            "Avoid duplicates and near-duplicates. Include logo/branding only if explicitly relevant.\n"
            "If nothing is relevant, return an empty list.\n"
            "Return valid JSON object only with keys:\n"
            "{"
            "\"selected_ids\": [int], "
            "\"reason\": \"short reason\""
            "}"
        )
        user_prompt = {
            "hotel_code": str(hotel_code or "").strip(),
            "intent": str(intent or "").strip(),
            "max_images": "all" if requested_max <= 0 else limit,
            "ordering_rule": "match topic order in assistant_message",
            "user_message": str(user_message or "").strip(),
            "assistant_message": str(assistant_message or "").strip(),
            "candidates": candidate_payload,
        }
        token_budget = max(520, min(2200, 240 + (limit * 14)))
        raw = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
            model=str(getattr(settings, "chat_display_beautifier_model", "") or "").strip() or None,
            temperature=0.1,
            max_tokens=token_budget,
            trace_context={
                "component": "services.image_tool_service",
                "what_this_call_is_for": "Select image IDs for chat UI carousel",
                "hotel_code": str(hotel_code or "").strip(),
                **(trace_context or {}),
            },
        )
        selected_ids_raw = raw.get("selected_ids") if isinstance(raw, dict) else []
        selected_ids: list[int] = []
        if isinstance(selected_ids_raw, list):
            for item in selected_ids_raw:
                try:
                    image_id = int(item)
                except (TypeError, ValueError):
                    continue
                if image_id not in selected_ids:
                    selected_ids.append(image_id)
                if len(selected_ids) >= limit:
                    break

        by_id = {int(item["id"]): item for item in candidates}
        selected: list[dict[str, Any]] = []
        for image_id in selected_ids:
            match = by_id.get(image_id)
            if not match:
                continue
            description_text = str(match.get("description") or "").strip()
            category_text = str(match["category"] or "").strip()
            tags_list = match["tags"] if isinstance(match["tags"], list) else []
            if not description_text:
                if category_text:
                    description_text = f"{category_text.title()} highlight from hotel gallery."
                elif tags_list:
                    description_text = "Related to: " + ", ".join(str(tag).strip() for tag in tags_list[:3] if str(tag).strip())
                else:
                    description_text = f"{str(match['title'] or '').strip()} image from hotel gallery."
            selected.append(
                {
                    "id": int(match["id"]),
                    "title": str(match["title"] or "").strip(),
                    "description": description_text,
                    "url": str(match["image_url"] or "").strip(),
                    "category": str(match["category"] or "").strip(),
                    "tags": match["tags"] if isinstance(match["tags"], list) else [],
                }
            )
            if len(selected) >= limit:
                break

        selected = await self._enrich_descriptions_from_kb(
            hotel_code=hotel_code,
            selected_images=selected,
            user_message=user_message,
            assistant_message=assistant_message,
            trace_context=trace_context,
        )

        return {
            "images": selected,
            "selection_reason": str(raw.get("reason") or "").strip() if isinstance(raw, dict) else "",
            "tool_used": True,
            "candidate_count": len(candidates),
        }

    async def _enrich_descriptions_from_kb(
        self,
        *,
        hotel_code: str,
        selected_images: list[dict[str, Any]],
        user_message: str,
        assistant_message: str,
        trace_context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if not selected_images:
            return selected_images
        if not bool(getattr(settings, "smart_image_kb_caption_enabled", True)):
            return selected_images

        kb_max_chars = max(3000, int(getattr(settings, "smart_image_kb_context_max_chars", 14000) or 14000))
        with db_config_service.scoped_hotel(hotel_code):
            kb_text = str(
                config_service.get_full_kb_text_with_sources(
                    max_chars=kb_max_chars,
                    max_sources=120,
                )
                or ""
            ).strip()
        if not kb_text:
            return selected_images

        snippet_by_id = self._kb_snippet_map_for_images(
            kb_text=kb_text,
            selected_images=selected_images,
            max_chars_per_image=950,
        )

        system_prompt = (
            "You write short image card descriptions for a hotel chat UI.\n"
            "Use only facts grounded in the provided KB context.\n"
            "For each image id, return one concise description (8-24 words).\n"
            "If KB has no relevant fact for an image title, keep the existing description unchanged.\n"
            "Return valid JSON only with this shape:\n"
            "{"
            "\"images\": ["
            "{\"id\": 1, \"description\": \"...\"}"
            "]"
            "}"
        )
        user_payload = {
            "hotel_code": str(hotel_code or "").strip(),
            "user_message": str(user_message or "").strip(),
            "assistant_message": str(assistant_message or "").strip(),
            "selected_images": [
                {
                    "id": int(item.get("id") or 0),
                    "title": str(item.get("title") or "").strip(),
                    "existing_description": str(item.get("description") or "").strip(),
                    "kb_snippet": str(snippet_by_id.get(int(item.get("id") or 0)) or "").strip(),
                }
                for item in selected_images
            ],
            "kb_context": kb_text,
        }
        caption_model = str(getattr(settings, "smart_image_kb_caption_model", "") or "").strip() or None
        caption_tokens = max(180, int(getattr(settings, "smart_image_kb_caption_max_tokens", 420) or 420))
        raw = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            model=caption_model,
            temperature=0.1,
            max_tokens=caption_tokens,
            trace_context={
                "component": "services.image_tool_service",
                "what_this_call_is_for": "Generate KB-grounded image descriptions",
                "hotel_code": str(hotel_code or "").strip(),
                **(trace_context or {}),
            },
        )
        image_rows = []
        if isinstance(raw, dict):
            rows_any = raw.get("images")
            if isinstance(rows_any, list):
                image_rows = rows_any
            elif isinstance(raw.get("descriptions"), list):
                image_rows = raw.get("descriptions")

        descriptions_by_id: dict[int, str] = {}
        for row in image_rows:
            if not isinstance(row, dict):
                continue
            try:
                image_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            description = re.sub(r"\s+", " ", str(row.get("description") or "").strip())
            if not description:
                continue
            descriptions_by_id[image_id] = description[:220]

        if not descriptions_by_id:
            return selected_images

        enriched: list[dict[str, Any]] = []
        for item in selected_images:
            image_id = int(item.get("id") or 0)
            updated = dict(item)
            replacement = descriptions_by_id.get(image_id)
            if replacement:
                updated["description"] = replacement
            enriched.append(updated)
        return enriched


image_tool_service = ImageToolService()
