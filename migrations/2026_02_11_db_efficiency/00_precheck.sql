-- Migration precheck: 2026-02-11 DB efficiency pack
-- Non-destructive diagnostics only.

SET @canonical_hotel_id := COALESCE(
    (SELECT id FROM new_bot_hotels WHERE code = 'DEFAULT' ORDER BY id LIMIT 1),
    (SELECT MIN(id) FROM new_bot_hotels)
);

SELECT @canonical_hotel_id AS canonical_hotel_id;

SELECT id, code, name, city, is_active, created_at, updated_at
FROM new_bot_hotels
ORDER BY id;

DROP TEMPORARY TABLE IF EXISTS tmp_source_hotels;
CREATE TEMPORARY TABLE tmp_source_hotels AS
SELECT id
FROM new_bot_hotels
WHERE id <> @canonical_hotel_id;

SELECT COUNT(*) AS source_hotel_count
FROM tmp_source_hotels;

-- Per-hotel table distribution (current)
SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_business_config
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_capabilities
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_intents
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_restaurants
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_guests
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS row_count
FROM new_bot_conversations
GROUP BY hotel_id
ORDER BY hotel_id;

-- Conflict checks (must be zero rows before running 02_up.sql)

-- 1) Restaurant code collisions if merged to canonical hotel.
SELECT
    s.hotel_id AS source_hotel_id,
    s.code AS conflicting_restaurant_code
FROM new_bot_restaurants s
JOIN tmp_source_hotels src ON src.id = s.hotel_id
JOIN new_bot_restaurants c
  ON c.hotel_id = @canonical_hotel_id
 AND c.code = s.code
ORDER BY s.hotel_id, s.code;

-- 2) Guest phone collisions if merged to canonical hotel.
SELECT
    s.hotel_id AS source_hotel_id,
    s.phone_number AS conflicting_phone_number
FROM new_bot_guests s
JOIN tmp_source_hotels src ON src.id = s.hotel_id
JOIN new_bot_guests c
  ON c.hotel_id = @canonical_hotel_id
 AND c.phone_number = s.phone_number
ORDER BY s.hotel_id, s.phone_number;

-- 3) Informational: overlapping config keys (safe due upsert, but shown).
SELECT
    s.hotel_id AS source_hotel_id,
    s.config_key
FROM new_bot_business_config s
JOIN tmp_source_hotels src ON src.id = s.hotel_id
JOIN new_bot_business_config c
  ON c.hotel_id = @canonical_hotel_id
 AND c.config_key = s.config_key
ORDER BY s.hotel_id, s.config_key;

-- 4) Informational: overlapping capability IDs (safe due upsert).
SELECT
    s.hotel_id AS source_hotel_id,
    s.capability_id
FROM new_bot_capabilities s
JOIN tmp_source_hotels src ON src.id = s.hotel_id
JOIN new_bot_capabilities c
  ON c.hotel_id = @canonical_hotel_id
 AND c.capability_id = s.capability_id
ORDER BY s.hotel_id, s.capability_id;

-- 5) Informational: overlapping intent IDs (safe due upsert).
SELECT
    s.hotel_id AS source_hotel_id,
    s.intent_id
FROM new_bot_intents s
JOIN tmp_source_hotels src ON src.id = s.hotel_id
JOIN new_bot_intents c
  ON c.hotel_id = @canonical_hotel_id
 AND c.intent_id = s.intent_id
ORDER BY s.hotel_id, s.intent_id;
