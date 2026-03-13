import json

from services.kb_direct_lookup_service import KBDirectLookupService
import services.kb_direct_lookup_service as kb_lookup_module


def _write_structured_kb(tmp_path):
    payload = {
        "data": json.dumps(
            {
                "editable": {
                    "total_floors": "15 (including 4 basements and 10 guest floors)",
                    "Hotel Address": "Near Terminal 2, Hasan Pada Road, Andheri (E), Mumbai, Maharashtra 400099",
                    "in_room_amenities": (
                        "In-room smart laundry closet to iron, steam, refresh your clothes\n"
                        "High-speed WiFi with unlimited device usage\n"
                        "Complimentary well-stocked non-alcoholic minibar"
                    ),
                    "ultimate_suite": "8 rooms, 603 sq. ft.",
                    "prestige_suite": "6 rooms, 485 sq. ft., includes bathtub",
                    "Restaurant Info": (
                        "The Aviation Bar\n"
                        "Food Menu (11am-11pm)\n"
                        "Bar open till 1am\n"
                        "Scarletta - open for members only"
                    ),
                    "airport_transfer": "Available, Toyota Innova Hycross 5 seater. Rs2000 for T1, Rs1500 for T2",
                }
            }
        ),
        "orgId": "test",
    }
    kb_file = tmp_path / "kb.txt"
    kb_file.write_text(json.dumps(payload), encoding="utf-8")
    return kb_file


def test_kb_direct_lookup_handles_wifi_question(tmp_path):
    kb_file = _write_structured_kb(tmp_path)
    service = KBDirectLookupService()
    service.step_logs_enabled = False

    result = service.answer_question(
        query="is wifi free",
        tenant_id="tenant_test",
        source_paths=[kb_file],
    )

    assert result.handled is True
    assert "wifi" in result.answer.lower()


def test_kb_direct_lookup_handles_largest_room(tmp_path):
    kb_file = _write_structured_kb(tmp_path)
    service = KBDirectLookupService()
    service.step_logs_enabled = False

    result = service.answer_question(
        query="largest room",
        tenant_id="tenant_test",
        source_paths=[kb_file],
    )

    assert result.handled is True
    assert "ultimate suite" in result.answer.lower()
    assert "603" in result.answer


def test_kb_direct_lookup_strips_context_noise(tmp_path):
    kb_file = _write_structured_kb(tmp_path)
    service = KBDirectLookupService()
    service.step_logs_enabled = False

    result = service.answer_question(
        query="is alcohol available | Context: previous_request=does prestige suite have bathtub",
        tenant_id="tenant_test",
        source_paths=[kb_file],
    )

    assert result.handled is True
    assert "bar" in result.answer.lower() or "alcohol" in result.answer.lower()


def test_kb_direct_lookup_prefers_structured_library_index(monkeypatch):
    library_payload = {
        "source_signature": "sig_test",
        "pages": [
            {
                "id": "page_00001",
                "title": "pool_timings",
                "location": "editable.pool_timings",
                "text": "Pool timings are 7:00 AM to 7:00 PM. Children are not allowed after 7 PM.",
                "source_name": "kb.txt",
                "source_path": "kb.txt",
            }
        ],
    }
    monkeypatch.setattr(
        kb_lookup_module.config_service,
        "get_structured_kb_library",
        lambda rebuild_if_stale=True, max_sources=50: library_payload,
    )

    service = KBDirectLookupService()
    service.step_logs_enabled = False
    result = service.answer_question(query="what are pool timings", tenant_id="default", source_paths=None)

    assert result.handled is True
    assert "pool" in result.answer.lower()
