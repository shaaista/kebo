-- Post-migration verification: 2026-02-11 DB efficiency pack

-- 1) Hotel state should be consolidated.
SELECT id, code, name, city, is_active
FROM new_bot_hotels
ORDER BY id;

SELECT
    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_hotel_count,
    SUM(CASE WHEN code = 'DEFAULT' THEN 1 ELSE 0 END) AS default_code_count
FROM new_bot_hotels;

-- 2) Schema check for new columns.
SELECT table_name, column_name, column_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND (
        (table_name = 'new_bot_conversations' AND column_name IN ('pending_action', 'pending_data', 'channel'))
     OR (table_name = 'new_bot_messages' AND column_name IN ('channel'))
  )
ORDER BY table_name, column_name;

-- 3) Index check.
SELECT table_name, index_name, seq_in_index, column_name
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND (
        (table_name = 'new_bot_messages' AND index_name = 'idx_messages_conv_created')
     OR (table_name = 'new_bot_conversations' AND index_name = 'idx_conversations_hotel_state')
     OR (table_name = 'new_bot_orders' AND index_name = 'idx_orders_rest_status_created')
  )
ORDER BY table_name, index_name, seq_in_index;

-- 4) Per-hotel distributions should now be concentrated in canonical hotel.
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

-- 5) Data integrity checks (orphan scan).
SELECT COUNT(*) AS orphan_business_config
FROM new_bot_business_config bc
LEFT JOIN new_bot_hotels h ON h.id = bc.hotel_id
WHERE h.id IS NULL;

SELECT COUNT(*) AS orphan_capabilities
FROM new_bot_capabilities c
LEFT JOIN new_bot_hotels h ON h.id = c.hotel_id
WHERE h.id IS NULL;

SELECT COUNT(*) AS orphan_intents
FROM new_bot_intents i
LEFT JOIN new_bot_hotels h ON h.id = i.hotel_id
WHERE h.id IS NULL;

SELECT COUNT(*) AS orphan_restaurants
FROM new_bot_restaurants r
LEFT JOIN new_bot_hotels h ON h.id = r.hotel_id
WHERE h.id IS NULL;

SELECT COUNT(*) AS orphan_messages
FROM new_bot_messages m
LEFT JOIN new_bot_conversations c ON c.id = m.conversation_id
WHERE c.id IS NULL;
