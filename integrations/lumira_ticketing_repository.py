"""
Lumira ticketing DB repository.

Read-only helpers for legacy Lumira tables used by ticketing flows.
All methods are best-effort and return empty values on DB errors.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value).strip())
    except Exception:
        return None


def _normalize_phone_digits(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return re.sub(r"\D+", "", raw)


class LumiraTicketingRepository:
    """Legacy Lumira DB lookups used by ticketing and handoff flows."""

    async def fetch_departments_of_entity(
        self,
        db_session: AsyncSession | None,
        entity_id: int | str | None,
    ) -> list[dict[str, Any]]:
        eid = _to_int(entity_id)
        if db_session is None or eid is None:
            return []

        query = text(
            """
            SELECT DISTINCT
                dm.ID AS department_id,
                dm.DEPT_NAME AS department_name,
                dsc.STAFF_ID AS agent_id
            FROM
                GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING m
                JOIN GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG dsc
                    ON m.RI_Entity_Id = dsc.ORG_ID
                JOIN GHN_PROD_BAK.DEPARTMENT_MASTER dm
                    ON dsc.DEPARTMENT_ID = dm.ID
            WHERE
                m.FMS_Entity_Id = :entity_id
                AND dsc.MANAGER = 1
            ORDER BY
                dm.ID
            """
        )

        try:
            result = await db_session.execute(query, {"entity_id": eid})
            rows = result.mappings().all()
        except Exception:
            logger.exception("fetch_departments_of_entity failed for entity_id=%s", eid)
            return []

        normalized: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            normalized.append(
                {
                    "department_id": str(rec.get("department_id") or "").strip(),
                    "department_name": str(rec.get("department_name") or "").strip(),
                    # Lumira queries do not always return manager name/phone.
                    "department_head": str(rec.get("department_head") or "").strip(),
                    "department_head_phone": str(rec.get("department_head_phone") or "").strip(),
                    "agent_id": str(rec.get("agent_id") or "").strip(),
                }
            )
        return normalized

    async def fetch_outlets_of_entity(
        self,
        db_session: AsyncSession | None,
        entity_id: int | str | None,
    ) -> list[dict[str, Any]]:
        eid = _to_int(entity_id)
        if db_session is None or eid is None:
            return []

        query = text(
            """
            SELECT
                id AS outlet_id,
                outlet_name AS outlet_name
            FROM
                GHN_PROD_BAK.ORGANIZATION_OUTLETS
            WHERE
                fms_entity_id = :entity_id
            """
        )
        try:
            result = await db_session.execute(query, {"entity_id": eid})
            rows = result.mappings().all()
        except Exception:
            logger.exception("fetch_outlets_of_entity failed for entity_id=%s", eid)
            return []

        return [
            {
                "outlet_id": str(dict(row).get("outlet_id") or "").strip(),
                "outlet_name": str(dict(row).get("outlet_name") or "").strip(),
            }
            for row in rows
        ]

    async def fetch_candidate_tickets(
        self,
        db_session: AsyncSession | None,
        guest_id: int | str | None,
        room_number: str | None,
    ) -> list[dict[str, Any]]:
        if db_session is None:
            return []

        gid = _to_int(guest_id)
        room = str(room_number or "").strip() or None
        if gid is None and not room:
            return []

        query = text(
            """
            SELECT
                t.id,
                t.status,
                t.issue,
                t.room_number,
                t.guest_id,
                t.manager_notes
            FROM GHN_PROD_BAK.GHN_OPERATION_TICKETS t
            WHERE (
                    (:guest_id IS NOT NULL AND t.guest_id = :guest_id)
                 OR (:room_number IS NOT NULL AND t.room_number = :room_number)
              )
              AND UPPER(t.status) IN ('OPEN','IN_PROGRESS')
            ORDER BY t.id DESC
            """
        )
        try:
            result = await db_session.execute(
                query,
                {"guest_id": gid, "room_number": room},
            )
            rows = result.mappings().all()
        except Exception:
            logger.exception(
                "fetch_candidate_tickets failed for guest_id=%s room_number=%s",
                gid,
                room,
            )
            return []

        normalized: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            normalized.append(
                {
                    "id": str(rec.get("id") or "").strip(),
                    "status": str(rec.get("status") or "").strip(),
                    "issue": str(rec.get("issue") or "").strip(),
                    "room_number": str(rec.get("room_number") or "").strip(),
                    "guest_id": str(rec.get("guest_id") or "").strip(),
                    "manager_notes": str(rec.get("manager_notes") or "").strip(),
                }
            )
        return normalized

    async def fetch_agent_for_handover(
        self,
        db_session: AsyncSession | None,
        *,
        department_id: int | str | None,
        entity_id: int | str | None = None,
        group_id: int | str | None = None,
    ) -> str:
        if db_session is None:
            return ""

        dep = _to_int(department_id)
        ent = _to_int(entity_id)
        grp = _to_int(group_id)
        if dep is None:
            return ""

        query_sql = """
            SELECT DISTINCT
                dsc.STAFF_ID AS agent_id
            FROM
                GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG dsc
            WHERE
                dsc.DEPARTMENT_ID = :department_id
                AND dsc.MANAGER = 1
        """
        params: dict[str, Any] = {"department_id": dep}

        if ent is not None:
            query_sql += " AND dsc.ENTITY_ID = :entity_id"
            params["entity_id"] = ent
        elif grp is not None:
            query_sql += " AND dsc.GROUP_ID = :group_id"
            params["group_id"] = grp
        else:
            return ""

        query_sql += " LIMIT 1"
        try:
            result = await db_session.execute(text(query_sql), params)
            row = result.mappings().first()
        except Exception:
            logger.exception(
                "fetch_agent_for_handover failed for department_id=%s entity_id=%s group_id=%s",
                dep,
                ent,
                grp,
            )
            return ""

        if not row:
            return ""
        return str(dict(row).get("agent_id") or "").strip()

    async def fetch_ri_entity_id_from_mapping(
        self,
        db_session: AsyncSession | None,
        fms_entity_id: int | str | None,
    ) -> str:
        eid = _to_int(fms_entity_id)
        if db_session is None or eid is None:
            return ""

        query = text(
            """
            SELECT
                RI_Entity_Id AS ri_entity_id
            FROM
                GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING
            WHERE
                FMS_Entity_Id = :entity_id
            LIMIT 1
            """
        )
        try:
            result = await db_session.execute(query, {"entity_id": eid})
            row = result.mappings().first()
        except Exception:
            logger.exception(
                "fetch_ri_entity_id_from_mapping failed for fms_entity_id=%s",
                eid,
            )
            return ""

        if not row:
            return ""
        return str(dict(row).get("ri_entity_id") or "").strip()

    async def fetch_guest_profile(
        self,
        db_session: AsyncSession | None,
        *,
        entity_id: int | str | None,
        guest_id: int | str | None = None,
        guest_phone: str | None = None,
    ) -> dict[str, Any]:
        """
        Best-effort guest profile lookup from GHN_FF_GUEST_INFO.

        Used to auto-fill room_number/guest_name for ticket intake so the bot
        can behave naturally without repeatedly asking for known details.
        """
        if db_session is None:
            return {}

        eid = _to_int(entity_id)
        gid = _to_int(guest_id)
        phone_digits = _normalize_phone_digits(guest_phone)
        phone_last10 = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

        if gid is None and not phone_digits:
            return {}

        # First preference: explicit guest row lookup.
        if gid is not None:
            query_by_id = text(
                """
                SELECT
                    gi.ID AS guest_id,
                    gi.ENTITY_ID AS entity_id,
                    gi.GUEST_NAME AS guest_name,
                    gi.ROOM_NUMBER AS room_number,
                    gi.MOBILE_NUMBER_1 AS mobile_number_1,
                    gi.MOBILE_NUMBER_2 AS mobile_number_2
                FROM GHN_FF_PROD.GHN_FF_GUEST_INFO gi
                WHERE gi.ID = :guest_id
                  AND (:entity_id IS NULL OR gi.ENTITY_ID = :entity_id)
                LIMIT 1
                """
            )
            try:
                result = await db_session.execute(
                    query_by_id,
                    {"guest_id": gid, "entity_id": eid},
                )
                row = result.mappings().first()
            except Exception:
                logger.exception(
                    "fetch_guest_profile by guest_id failed for guest_id=%s entity_id=%s",
                    gid,
                    eid,
                )
                row = None

            if row:
                rec = dict(row)
                return {
                    "guest_id": str(rec.get("guest_id") or "").strip(),
                    "entity_id": str(rec.get("entity_id") or "").strip(),
                    "guest_name": str(rec.get("guest_name") or "").strip(),
                    "room_number": str(rec.get("room_number") or "").strip(),
                    "mobile_number_1": str(rec.get("mobile_number_1") or "").strip(),
                    "mobile_number_2": str(rec.get("mobile_number_2") or "").strip(),
                }

        if not phone_digits:
            return {}

        # Fallback: phone-based lookup with active-stay preference.
        query_by_phone = text(
            """
            SELECT
                gi.ID AS guest_id,
                gi.ENTITY_ID AS entity_id,
                gi.GUEST_NAME AS guest_name,
                gi.ROOM_NUMBER AS room_number,
                gi.MOBILE_NUMBER_1 AS mobile_number_1,
                gi.MOBILE_NUMBER_2 AS mobile_number_2
            FROM GHN_FF_PROD.GHN_FF_GUEST_INFO gi
            WHERE
                (:entity_id IS NULL OR gi.ENTITY_ID = :entity_id)
                AND (
                    gi.MOBILE_NUMBER_1 = :phone_raw
                    OR gi.MOBILE_NUMBER_2 = :phone_raw
                    OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(gi.MOBILE_NUMBER_1, '+', ''), '-', ''), ' ', ''), '(', ''), ')', '') LIKE CONCAT('%', :phone_last10)
                    OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(gi.MOBILE_NUMBER_2, '+', ''), '-', ''), ' ', ''), '(', ''), ')', '') LIKE CONCAT('%', :phone_last10)
                )
            ORDER BY
                CASE
                    WHEN NOW() BETWEEN gi.CHECK_IN_DATE AND gi.CHECK_OUT_DATE THEN 0
                    ELSE 1
                END,
                gi.UPDATED_DATE DESC
            LIMIT 1
            """
        )
        try:
            result = await db_session.execute(
                query_by_phone,
                {
                    "entity_id": eid,
                    "phone_raw": str(guest_phone or "").strip(),
                    "phone_last10": phone_last10,
                },
            )
            row = result.mappings().first()
        except Exception:
            logger.exception(
                "fetch_guest_profile by phone failed for entity_id=%s phone=%s",
                eid,
                guest_phone,
            )
            return {}

        if not row:
            return {}
        rec = dict(row)
        return {
            "guest_id": str(rec.get("guest_id") or "").strip(),
            "entity_id": str(rec.get("entity_id") or "").strip(),
            "guest_name": str(rec.get("guest_name") or "").strip(),
            "room_number": str(rec.get("room_number") or "").strip(),
            "mobile_number_1": str(rec.get("mobile_number_1") or "").strip(),
            "mobile_number_2": str(rec.get("mobile_number_2") or "").strip(),
        }


lumira_ticketing_repository = LumiraTicketingRepository()
