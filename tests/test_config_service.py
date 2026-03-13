import json
import importlib
from pathlib import Path

from services.config_service import ConfigService

config_service_module = importlib.import_module("services.config_service")


def _build_temp_config_service(tmp_path: Path, monkeypatch) -> ConfigService:
    config_dir = tmp_path / "config"
    templates_dir = config_dir / "templates"
    prompt_templates_dir = config_dir / "prompt_templates"
    config_file = config_dir / "business_config.json"

    monkeypatch.setattr(config_service_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_service_module, "TEMPLATES_DIR", templates_dir)
    monkeypatch.setattr(config_service_module, "PROMPT_TEMPLATES_DIR", prompt_templates_dir)
    monkeypatch.setattr(config_service_module, "BUSINESS_CONFIG_FILE", config_file)

    service = ConfigService()
    service.save_config(service._default_config())  # Seed isolated config for tests.
    return service


def test_custom_intent_mapping_updates_enabled_core_ids(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_intent(
        {
            "id": "book_appointment_custom",
            "label": "Book Appointment (Custom)",
            "enabled": True,
            "maps_to": "table_booking",
        }
    )

    assert service.resolve_intent_to_core("book_appointment_custom") == "table_booking"
    assert "book_appointment_custom" in service.get_enabled_intent_ids()
    assert "table_booking" in service.get_enabled_intent_ids()

    assert service.update_intent("book_appointment_custom", {"enabled": False})
    assert "book_appointment_custom" not in service.get_enabled_intent_ids()
    assert "table_booking" not in service.get_enabled_intent_ids()

    assert service.delete_intent("book_appointment_custom")
    assert service.resolve_intent_to_core("book_appointment_custom") == "book_appointment_custom"


def test_resolve_hotel_code_maps_default_and_placeholders_to_business_id(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    config = service.load_config()
    config["business"]["id"] = "hotel_1771239708469"
    config["business"]["name"] = "Sarovar Demo"
    service.save_config(config)

    assert service.resolve_hotel_code("DEFAULT") == "hotel_1771239708469"
    assert service.resolve_hotel_code("test_hotel") == "hotel_1771239708469"
    assert service.resolve_hotel_code("MUMBAI_GRAND") == "hotel_1771239708469"
    assert service.resolve_hotel_code("") == "hotel_1771239708469"
    assert service.resolve_hotel_code("custom_branch_1") == "custom_branch_1"


def test_service_description_flows_into_capability_summary(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "aviator",
            "name": "Aviator Lounge",
            "type": "restaurant",
            "description": "Bar and craft cocktails",
            "hours": {"open": "17:00", "close": "01:00"},
            "delivery_zones": ["dine_in_only"],
            "is_active": True,
        }
    )

    summary = service.get_capability_summary()
    service_catalog = summary.get("service_catalog", [])
    restaurants = summary.get("restaurants", [])

    assert len(service_catalog) == 1
    assert service_catalog[0]["description"] == "Bar and craft cocktails"
    assert service_catalog[0]["type"] == "restaurant"
    assert len(restaurants) == 1
    # Restaurant summary falls back to description when cuisine is not explicitly set.
    assert restaurants[0]["cuisine"] == "Bar and craft cocktails"


def test_post_checkout_prebuilt_services_available(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    templates = service.get_prebuilt_phase_services("post_checkout")
    template_ids = {str(item.get("id")) for item in templates}

    assert "postcheckout_invoice_support" in template_ids
    assert "postcheckout_refund_followup" in template_ids
    assert "postcheckout_lost_and_found" in template_ids
    assert all(str(item.get("phase_id")) == "post_checkout" for item in templates)
    assert all(str(item.get("ticketing_policy") or "").strip() for item in templates)
    assert all(item.get("ticketing_enabled") is True for item in templates)


def test_service_ticketing_toggle_is_persisted(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "booking_modification",
            "name": "Booking Modification",
            "type": "service",
            "description": "Modify booking requests",
            "phase_id": "pre_checkin",
            "ticketing_enabled": False,
            "is_active": True,
        }
    )

    services = service.get_services()
    row = next(item for item in services if item.get("id") == "booking_modification")
    assert row.get("ticketing_enabled") is False


def test_service_prompt_pack_auto_generated_on_add(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "airport_transfer",
            "name": "Airport Transfer",
            "type": "service",
            "description": "Arrange pickup and drop support before arrival.",
            "phase_id": "pre_checkin",
            "ticketing_enabled": False,
            "ticketing_policy": "Create ticket only when manual dispatch is required.",
            "is_active": True,
        }
    )

    services = service.get_services()
    row = next(item for item in services if item.get("id") == "airport_transfer")
    pack = row.get("service_prompt_pack")

    assert isinstance(pack, dict)
    assert pack.get("source") == "auto_generated"
    assert isinstance(pack.get("required_slots"), list) and len(pack.get("required_slots")) > 0
    assert any(str(slot.get("id")) == "request_details" for slot in pack.get("required_slots", []))
    ticketing_policy = pack.get("ticketing_policy", {})
    assert isinstance(ticketing_policy, dict)
    assert ticketing_policy.get("enabled") is False
    assert row.get("service_prompt_pack_custom") is False


def test_service_prompt_pack_manual_override_persists_on_update(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    manual_pack = {
        "role": "You are custom transport concierge.",
        "professional_behavior": "Collect exact route details and avoid assumptions.",
        "required_slots": [
            {
                "id": "pickup_location",
                "label": "Pickup Location",
                "prompt": "Share pickup location.",
                "required": True,
                "type": "text",
            }
        ],
        "confirmation_format": {
            "style": "manual",
            "template": "Please confirm transport details: {summary}.",
            "required_phrase": "yes confirm",
        },
        "ticketing_policy": {
            "enabled": True,
            "policy": "Always ticket.",
            "decision_template": "Always ticket.",
        },
    }

    assert service.add_service(
        {
            "id": "transport_manual",
            "name": "Transport Manual",
            "type": "service",
            "description": "Manual transport support.",
            "phase_id": "pre_checkin",
            "service_prompt_pack": manual_pack,
            "is_active": True,
        }
    )

    assert service.update_service("transport_manual", {"description": "Updated description text"})

    row = next(item for item in service.get_services() if item.get("id") == "transport_manual")
    pack = row.get("service_prompt_pack", {})

    assert row.get("service_prompt_pack_custom") is True
    assert pack.get("source") == "manual_override"
    assert pack.get("role") == "You are custom transport concierge."
    assert str(pack.get("confirmation_format", {}).get("template") or "").startswith("Please confirm transport details")


def test_service_prompt_pack_regenerates_for_non_custom_service_on_update(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "spa_booking",
            "name": "Spa Booking",
            "type": "service",
            "description": "Book spa sessions.",
            "phase_id": "during_stay",
            "is_active": True,
        }
    )
    before = next(item for item in service.get_services() if item.get("id") == "spa_booking")
    assert before.get("service_prompt_pack_custom") is False
    assert "Spa Booking" in str(before.get("service_prompt_pack", {}).get("role") or "")

    assert service.update_service("spa_booking", {"name": "Wellness Booking"})
    after = next(item for item in service.get_services() if item.get("id") == "spa_booking")
    pack = after.get("service_prompt_pack", {})

    assert after.get("service_prompt_pack_custom") is False
    assert pack.get("source") == "auto_generated"
    assert "Wellness Booking" in str(pack.get("role") or "")


def test_faq_bank_crud_and_matching(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_faq_entry(
        {
            "id": "medicine_availability",
            "question": "Is medicine available?",
            "answer": "Yes, medicine is available at the pharmacy desk 24/7.",
            "description": "Emergency medicine policy",
            "tags": ["Medicine", "Pharmacy"],
            "enabled": True,
        }
    )

    faq_bank = service.get_faq_bank()
    assert len(faq_bank) == 1
    assert faq_bank[0]["id"] == "medicine_availability"
    assert faq_bank[0]["tags"] == ["medicine", "pharmacy"]

    assert service.update_faq_entry(
        "medicine_availability",
        {"answer": "Yes, medicine is available from the front desk pharmacy 24/7."},
    )
    updated = service.get_faq_bank()[0]
    assert "front desk pharmacy" in updated["answer"].lower()

    matched = service.find_faq_entry("is medicin available?")
    assert matched is not None
    assert matched["id"] == "medicine_availability"
    assert matched["match_score"] >= 0.72

    assert service.delete_faq_entry("medicine_availability")
    assert service.get_faq_bank() == []


def test_tools_crud_and_capability_fallback_to_tools_and_intents(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    # Remove explicit human escalation capability to verify fallback logic.
    config = service.load_config()
    config["capabilities"].pop("human_escalation", None)
    service.save_config(config)

    assert service.add_intent({"id": "human_request", "label": "Talk to Human", "enabled": True})
    assert service.add_tool(
        {
            "id": "ticketing",
            "name": "Ticketing",
            "description": "Create a support ticket",
            "type": "workflow",
            "enabled": True,
            "channels": ["web_widget"],
        }
    )

    tools = service.get_tools()
    assert len(tools) >= 1
    assert any(tool["id"] == "ticketing" for tool in tools)
    assert service.is_capability_enabled("human_escalation") is True


def test_legacy_ticketing_plugin_service_migrates_to_tools(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    config = service.load_config()
    config["services"] = [
        {
            "id": "ticketing_agent",
            "name": "Ticketing Agent",
            "type": "plugin",
            "description": "Legacy ticketing plugin entry under services.",
            "is_active": True,
            "ticketing_plugin_enabled": True,
            "ticketing_cases": ["room booking", "spa booking"],
        }
    ]
    config["tools"] = []
    service.save_config(config)

    reloaded = service.reload_config()
    tools = reloaded.get("tools", [])
    services = reloaded.get("services", [])

    assert all(str(item.get("id", "")).lower() != "ticketing_agent" for item in services)
    ticketing_tool = next((item for item in tools if str(item.get("id", "")).lower() == "ticketing"), None)
    assert ticketing_tool is not None
    assert ticketing_tool.get("type") == "workflow"
    assert ticketing_tool.get("handler") == "ticket_create"
    assert ticketing_tool.get("ticketing_plugin_enabled") is True
    assert ticketing_tool.get("ticketing_cases") == ["room booking", "spa booking"]


def test_capability_summary_handles_legacy_none_service_fields(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    # Simulate legacy/bad data that may already exist in JSON.
    config = service.load_config()
    config["services"] = [
        {
            "id": "ird",
            "name": "In-Room Dining",
            "type": "restaurant",
            "description": "Multi cuisine",
            "hours": None,
            "delivery_zones": None,
            "is_active": True,
        }
    ]
    service.save_config(config)

    summary = service.get_capability_summary()
    assert len(summary["service_catalog"]) == 1
    assert summary["service_catalog"][0]["hours"] == {}
    assert summary["service_catalog"][0]["delivery_zones"] == []
    assert len(summary["restaurants"]) == 1
    assert summary["restaurants"][0]["delivers_to_room"] is False


def test_clear_services_and_menu_runtime_flag_defaults_to_rag_mode(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.is_menu_runtime_enabled() is False

    assert service.add_service(
        {
            "id": "ird",
            "name": "In-Room Dining",
            "type": "restaurant",
            "description": "Multi cuisine",
            "is_active": True,
        }
    )
    assert len(service.get_services()) == 1

    assert service.clear_services() is True
    assert service.get_services() == []


def test_service_summary_prefers_json_and_ignores_runtime_orphans(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    config = service.load_config()
    config["services"] = []
    service.save_config(config)

    # With no JSON services, summary should stay empty in config-service path.
    summary = service.get_capability_summary()
    assert summary.get("service_catalog") == []
    assert summary.get("restaurants") == []


def test_quick_actions_exclude_greeting_and_knowledge_query(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    quick_actions = service.get_quick_actions(limit=6)
    lowered = {str(action).strip().lower() for action in quick_actions}

    assert "greeting" not in lowered
    assert "knowledge query" not in lowered


def test_knowledge_conflict_report_flags_service_status_and_delivery_mismatch(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "kadak",
            "name": "Kadak",
            "type": "restaurant",
            "description": "Tea lounge",
            "delivery_zones": ["dine_in_only"],
            "is_active": False,
        }
    )

    kb_file = tmp_path / "kb_conflict.txt"
    kb_file.write_text(
        "Kadak is available 24/7 for guests and provides room delivery on request.",
        encoding="utf-8",
    )
    service.update_knowledge_config({"sources": [str(kb_file)]})

    report = service.get_knowledge_conflict_report()
    warnings = report.get("warnings", [])
    warning_codes = {warning.get("code") for warning in warnings}

    assert "inactive_service_marked_available" in warning_codes
    assert "dine_in_only_conflicts_with_kb_delivery" in warning_codes
    assert any(str(kb_file) == source for source in report.get("sources_checked", []))


def test_agent_plugin_settings_roundtrip(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    defaults = service.get_agent_plugin_settings()
    assert defaults["enabled"] is True
    assert defaults["shared_context"] is True
    assert defaults["strict_mode"] is True

    updated = service.update_agent_plugin_settings(
        {
            "enabled": False,
            "strict_mode": False,
            "strict_unavailable_response": "Outside plugin scope",
        }
    )
    assert updated["enabled"] is False
    assert updated["strict_mode"] is False
    assert updated["strict_unavailable_response"] == "Outside plugin scope"


def test_agent_plugin_fact_workflow_and_service_kb_sync(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_agent_plugin(
        {
            "id": "spa_booking_agent",
            "name": "Spa Booking",
            "service_id": "spa_booking",
            "service_category": "transactional",
            "trigger_phrases": ["spa booking"],
            "slot_schema": [{"id": "time", "prompt": "Preferred time?", "required": True}],
            "is_active": True,
        }
    )

    created = service.add_agent_plugin_fact(
        "spa_booking_agent",
        {"text": "Spa operates from 8 AM to 10 PM.", "source": "spa_manual.pdf"},
    )
    assert created is not None
    fact_id = str(created.get("id"))

    approved = service.approve_agent_plugin_fact("spa_booking_agent", fact_id, approved_by="qa")
    assert approved is not None
    assert approved.get("status") == "approved"
    assert approved.get("approved_by") == "qa"

    kb_record = service.get_service_kb_record(service_id="spa_booking", plugin_id="spa_booking_agent")
    assert kb_record is not None
    assert any(
        str(item.get("id")) == fact_id and str(item.get("status")) == "approved"
        for item in (kb_record.get("facts") or [])
    )


def test_service_kb_upsert_and_fetch_latest(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    first = service.upsert_service_kb_record(
        {
            "id": "airport_transfer_kb",
            "service_id": "airport_transfer",
            "plugin_id": "airport_transfer_agent",
            "strict_mode": True,
            "facts": [{"text": "Airport transfer requires 4 hours prior notice.", "status": "approved"}],
            "version": 1,
            "is_active": True,
        }
    )
    assert first is not None

    second = service.upsert_service_kb_record(
        {
            "id": "airport_transfer_kb",
            "service_id": "airport_transfer",
            "plugin_id": "airport_transfer_agent",
            "strict_mode": True,
            "facts": [{"text": "Airport transfer is available 24/7.", "status": "approved"}],
            "version": 2,
            "is_active": True,
        }
    )
    assert second is not None

    latest = service.get_service_kb_record(service_id="airport_transfer", plugin_id="airport_transfer_agent")
    assert latest is not None
    assert int(latest.get("version") or 0) == 2


def test_compile_service_kb_records_extracts_and_preserves_manual_overrides(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "spa_booking",
            "name": "Spa Booking",
            "type": "service",
            "description": "Book spa therapies and wellness sessions.",
            "phase_id": "during_stay",
            "ticketing_enabled": False,
            "is_active": True,
        }
    )
    kb_file = tmp_path / "spa_kb.txt"
    kb_file.write_text(
        (
            "Spa Booking is available for in-house guests. "
            "Spa timings are 9 AM to 11 PM daily. "
            "Advance booking is recommended for premium therapies."
        ),
        encoding="utf-8",
    )
    service.update_knowledge_config({"sources": [str(kb_file)]})

    compile_result = service.compile_service_kb_records(
        service_id="spa_booking",
        force=True,
        preserve_manual=True,
        published_by="test-suite",
    )
    assert compile_result.get("compiled_count") == 1
    record = service.get_service_kb_record(service_id="spa_booking")
    assert record is not None
    assert any("spa timings" in str(fact.get("text") or "").lower() for fact in (record.get("facts") or []))

    updated = service.set_service_kb_manual_facts(
        service_id="spa_booking",
        facts=["Manual override: Spa bookings for groups need staff confirmation."],
        published_by="qa",
    )
    assert updated is not None
    assert any(
        str(fact.get("origin") or "") == "manual"
        for fact in (updated.get("facts") or [])
    )

    service.compile_service_kb_records(
        service_id="spa_booking",
        force=True,
        preserve_manual=True,
        published_by="test-suite",
    )
    compiled_again = service.get_service_kb_record(service_id="spa_booking")
    assert compiled_again is not None
    assert any(
        "groups need staff confirmation" in str(fact.get("text") or "").lower()
        and str(fact.get("origin") or "") == "manual"
        for fact in (compiled_again.get("facts") or [])
    )


def test_capability_summary_includes_service_kb_records(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    assert service.add_service(
        {
            "id": "room_discovery",
            "name": "Room Discovery",
            "type": "service",
            "description": "Share room options and amenities.",
            "phase_id": "pre_booking",
            "ticketing_enabled": True,
            "is_active": True,
        }
    )
    compile_result = service.compile_service_kb_records(
        service_id="room_discovery",
        force=True,
        preserve_manual=True,
        published_by="test-suite",
    )
    assert compile_result.get("compiled_count") == 1

    summary = service.get_capability_summary()
    kb_records = summary.get("service_kb_records") or []
    assert any(str(item.get("service_id") or "") == "room_discovery" for item in kb_records)


def test_knowledge_sources_deduped_by_content_hash(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    payload = {
        "data": json.dumps(
            {
                "editable": {
                    "pool_timings": "7:00 AM to 7:00 PM. Children not allowed after 7 PM.",
                    "Restaurant Info": "Bombay Swim Club is open till late for reservations.",
                }
            }
        ),
        "orgId": "test",
    }
    source_a = tmp_path / "a.txt"
    source_b = tmp_path / "b.txt"
    body = json.dumps(payload)
    source_a.write_text(body, encoding="utf-8")
    source_b.write_text(body, encoding="utf-8")

    service.update_knowledge_config({"sources": [str(source_a), str(source_b)]})
    knowledge = service.get_knowledge_config()
    sources = knowledge.get("sources", [])

    assert isinstance(sources, list)
    assert len(sources) == 1


def test_structured_library_builds_books_and_context(tmp_path, monkeypatch):
    service = _build_temp_config_service(tmp_path, monkeypatch)

    payload = {
        "data": json.dumps(
            {
                "editable": {
                    "pool_timings": "Pool timings are 7:00 AM to 7:00 PM. No children after 7 PM.",
                    "Bombay Swim Club Menu": "Guests with reservations may use the pool until 11 PM.",
                    "airport_transfer": "Available. T1 INR 2000, T2 INR 1500.",
                }
            }
        ),
        "orgId": "test",
    }
    kb_file = tmp_path / "kb_structured.txt"
    kb_file.write_text(json.dumps(payload), encoding="utf-8")
    service.update_knowledge_config({"sources": [str(kb_file)]})

    library = service.get_structured_kb_library(rebuild_if_stale=True)
    assert int(library.get("source_count") or 0) == 1
    assert int((library.get("coverage") or {}).get("total_pages") or 0) > 0
    assert int((library.get("coverage") or {}).get("covered_pages") or 0) == int(
        (library.get("coverage") or {}).get("total_pages") or 0
    )
    books = library.get("books", [])
    assert any(str(book.get("id") or "") == "swimming_pool" for book in books)

    context_payload = service.build_service_library_context(
        service_name="Swimming Pool",
        service_description="Need pool timings and restrictions",
    )
    context_text = str(context_payload.get("context_text") or "").lower()
    assert context_payload.get("page_count", 0) > 0
    assert "pool" in context_text
