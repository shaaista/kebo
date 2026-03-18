from services.response_beautifier_service import response_beautifier_service


def test_beautify_strips_markdown_emphasis_and_star_bullets():
    raw = "**Kadak**\n* Soup of the day\n* Garden salad"
    out = response_beautifier_service.beautify_response_text(raw)
    assert "Kadak" in out
    assert "**" not in out
    assert "* Soup" not in out
    assert "- Soup of the day" in out
    assert "- Garden salad" in out


def test_beautify_formats_multi_field_collection_request_as_bullets():
    raw = (
        "I can help with your room booking. "
        "Please share your preferred room type, check-in date, check-out date, and number of guests."
    )
    out = response_beautifier_service.beautify_response_text(raw)
    assert "Please share the following details:" in out
    assert "- preferred room type" in out
    assert "- check-in date" in out
    assert "- check-out date" in out
    assert "- number of guests" in out


def test_beautify_keeps_single_field_request_as_sentence():
    raw = "Could you please share your room number so I can proceed?"
    out = response_beautifier_service.beautify_response_text(raw)
    assert out == raw


def test_beautify_preserves_example_note_when_formatting_identity_request():
    raw = (
        "Before I raise this ticket, please share guest name and contact phone number "
        "(for example: Name: Alex, Phone: +1 555 123 4567)."
    )
    out = response_beautifier_service.beautify_response_text(raw)
    assert "Please share the following details:" in out
    assert "- guest name" in out
    assert "- contact phone number" in out
    assert "Example: Name: Alex, Phone: +1 555 123 4567" in out
