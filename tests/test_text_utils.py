import re

from utils.text_utils import (
    clean_menu_item_name,
    extract_price,
    generate_order_id,
    is_vegetarian_indicator,
    normalize_text,
)


def test_normalize_text():
    assert normalize_text("  Hello, WORLD!! ") == "hello world"


def test_extract_price_with_rupee_symbol():
    assert extract_price("Rs. 450") == 450.0


def test_extract_price_with_dash():
    assert extract_price("450/-") == 450.0


def test_extract_price_with_commas():
    assert extract_price("INR 1,250") == 1250.0


def test_extract_price_no_price():
    assert extract_price("Butter Chicken") is None


def test_is_vegetarian_indicator_true():
    assert is_vegetarian_indicator("Paneer (Veg)") is True


def test_is_vegetarian_indicator_false():
    assert is_vegetarian_indicator("Chicken (Non-Veg)") is False


def test_is_vegetarian_indicator_none():
    assert is_vegetarian_indicator("Chicken Biryani") is None


def test_clean_menu_item_name():
    assert clean_menu_item_name("Butter Chicken 450/-") == "Butter Chicken"
    assert clean_menu_item_name("Paneer Tikka (V) Rs. 320") == "Paneer Tikka"
    assert clean_menu_item_name("Chicken Biryani [NV]") == "Chicken Biryani"


def test_generate_order_id_format():
    order_id = generate_order_id()
    assert re.match(r"^ORD-\d{8}-[A-Z0-9]{6}$", order_id)
