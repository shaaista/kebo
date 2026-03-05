import csv
import json

import pytest

from config.settings import settings
from services.ticketing_service import ticketing_service


def _write_seed_csv(csv_file, rows):
    headers = list(ticketing_service._LOCAL_TICKET_CSV_HEADERS)
    with csv_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


@pytest.mark.asyncio
async def test_local_mode_create_and_update_persist_to_local_store(tmp_path, monkeypatch):
    store_file = tmp_path / "local_tickets.json"
    csv_file = tmp_path / "local_tickets.csv"
    monkeypatch.setattr(settings, "ticketing_local_mode", True)
    monkeypatch.setattr(settings, "ticketing_local_store_file", str(store_file))
    monkeypatch.setattr(settings, "ticketing_local_csv_file", str(csv_file))
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    create_result = await ticketing_service.create_ticket(
        {
            "guest_id": "42",
            "room_number": "305",
            "issue": "AC not cooling",
            "ticket_status": "open",
        }
    )
    assert create_result.success is True
    assert create_result.ticket_id == "LOCAL-1"
    assert create_result.response.get("csv_written") is True

    data_after_create = json.loads(store_file.read_text(encoding="utf-8"))
    assert data_after_create["next_id"] == 2
    assert len(data_after_create["tickets"]) == 1
    assert data_after_create["tickets"][0]["id"] == "LOCAL-1"
    assert data_after_create["tickets"][0]["issue"] == "AC not cooling"

    with csv_file.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "LOCAL-1"
    assert rows[0]["issue"] == "AC not cooling"
    assert rows[0]["status"] == "open"

    update_result = await ticketing_service.update_ticket(
        ticket_id="LOCAL-1",
        manager_notes="Engineer not yet arrived",
        extra_fields={"status": "in_progress"},
    )
    assert update_result.success is True
    assert update_result.ticket_id == "LOCAL-1"

    data_after_update = json.loads(store_file.read_text(encoding="utf-8"))
    record = data_after_update["tickets"][0]
    assert "Engineer not yet arrived" in str(record.get("manager_notes") or "")
    assert record.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_local_mode_bootstraps_csv_with_existing_json_tickets(tmp_path, monkeypatch):
    store_file = tmp_path / "local_tickets.json"
    csv_file = tmp_path / "local_tickets.csv"
    store_file.write_text(
        json.dumps(
            {
                "next_id": 2,
                "tickets": [
                    {
                        "id": "LOCAL-1",
                        "ticket_id": "LOCAL-1",
                        "created_at": "2026-02-24T10:49:03.460597+00:00",
                        "updated_at": "2026-02-24T10:49:03.460597+00:00",
                        "mode": "local_simulation",
                        "payload": {
                            "session_id": "session-old-1",
                            "issue": "Old issue",
                            "message": "Old issue",
                            "ticket_status": "open",
                            "categorization": "request",
                            "sub_categorization": "order_food",
                            "created_at": "10:49:03 24-02-2026",
                        },
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "ticketing_local_mode", True)
    monkeypatch.setattr(settings, "ticketing_local_store_file", str(store_file))
    monkeypatch.setattr(settings, "ticketing_local_csv_file", str(csv_file))
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    create_result = await ticketing_service.create_ticket(
        {
            "session_id": "session-new-2",
            "issue": "New issue",
            "message": "New issue",
            "ticket_status": "open",
            "categorization": "complaint",
            "sub_categorization": "room_service",
        }
    )
    assert create_result.success is True
    assert create_result.ticket_id == "LOCAL-2"

    with csv_file.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2
    assert rows[0]["ticket_id"] == "LOCAL-1"
    assert rows[0]["issue"] == "Old issue"
    assert rows[1]["ticket_id"] == "LOCAL-2"
    assert rows[1]["issue"] == "New issue"


def test_is_ticketing_enabled_true_in_local_mode_even_without_base_url(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_local_mode", True)
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    assert ticketing_service.is_ticketing_enabled({}) is True


@pytest.mark.asyncio
async def test_local_mode_recovers_empty_json_store_from_csv(tmp_path, monkeypatch):
    store_file = tmp_path / "local_tickets.json"
    csv_file = tmp_path / "local_tickets.csv"
    store_file.write_text("", encoding="utf-8")
    _write_seed_csv(
        csv_file,
        [
            {
                "id": "1",
                "ticket_id": "LOCAL-1",
                "session_id": "session-old",
                "guest_id": "42",
                "issue": "Old issue",
                "message": "Old issue",
                "status": "open",
                "category": "request",
                "sub_category": "order_food",
                "mode": "local_simulation",
                "local_created_at_utc": "2026-02-24T10:49:03.460597+00:00",
                "local_updated_at_utc": "2026-02-24T10:49:03.460597+00:00",
                "payload_json": json.dumps(
                    {
                        "session_id": "session-old",
                        "issue": "Old issue",
                        "message": "Old issue",
                        "ticket_status": "open",
                        "categorization": "request",
                        "sub_categorization": "order_food",
                    }
                ),
                "response_json": json.dumps({"status": "open"}),
            }
        ],
    )

    monkeypatch.setattr(settings, "ticketing_local_mode", True)
    monkeypatch.setattr(settings, "ticketing_local_store_file", str(store_file))
    monkeypatch.setattr(settings, "ticketing_local_csv_file", str(csv_file))
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    create_result = await ticketing_service.create_ticket(
        {
            "session_id": "session-new",
            "issue": "New issue",
            "message": "New issue",
            "ticket_status": "open",
            "categorization": "request",
            "sub_categorization": "spa_booking",
        }
    )
    assert create_result.success is True
    assert create_result.ticket_id == "LOCAL-2"

    data_after_create = json.loads(store_file.read_text(encoding="utf-8"))
    assert data_after_create["next_id"] == 3
    assert len(data_after_create["tickets"]) == 2
    assert data_after_create["tickets"][0]["ticket_id"] == "LOCAL-1"
    assert data_after_create["tickets"][1]["ticket_id"] == "LOCAL-2"


@pytest.mark.asyncio
async def test_local_mode_recovers_corrupt_json_store_from_csv(tmp_path, monkeypatch):
    store_file = tmp_path / "local_tickets.json"
    csv_file = tmp_path / "local_tickets.csv"
    store_file.write_text("{invalid json", encoding="utf-8")
    _write_seed_csv(
        csv_file,
        [
            {
                "id": "5",
                "ticket_id": "LOCAL-5",
                "session_id": "session-old-5",
                "guest_id": "77",
                "issue": "Legacy issue",
                "message": "Legacy issue",
                "status": "open",
                "category": "request",
                "sub_category": "table_booking",
                "mode": "local_simulation",
                "local_created_at_utc": "2026-02-24T10:49:03.460597+00:00",
                "local_updated_at_utc": "2026-02-24T10:49:03.460597+00:00",
                "payload_json": json.dumps(
                    {
                        "session_id": "session-old-5",
                        "issue": "Legacy issue",
                        "message": "Legacy issue",
                        "ticket_status": "open",
                        "categorization": "request",
                        "sub_categorization": "table_booking",
                    }
                ),
                "response_json": json.dumps({"status": "open"}),
            }
        ],
    )

    monkeypatch.setattr(settings, "ticketing_local_mode", True)
    monkeypatch.setattr(settings, "ticketing_local_store_file", str(store_file))
    monkeypatch.setattr(settings, "ticketing_local_csv_file", str(csv_file))
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    create_result = await ticketing_service.create_ticket(
        {
            "session_id": "session-new-6",
            "issue": "Brand new issue",
            "message": "Brand new issue",
            "ticket_status": "open",
            "categorization": "complaint",
            "sub_categorization": "housekeeping",
        }
    )
    assert create_result.success is True
    assert create_result.ticket_id == "LOCAL-6"

    data_after_create = json.loads(store_file.read_text(encoding="utf-8"))
    assert data_after_create["next_id"] == 7
    assert len(data_after_create["tickets"]) == 2
    assert data_after_create["tickets"][0]["ticket_id"] == "LOCAL-5"
    assert data_after_create["tickets"][1]["ticket_id"] == "LOCAL-6"
