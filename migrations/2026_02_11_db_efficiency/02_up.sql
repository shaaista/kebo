-- Up migration: 2026-02-11 DB efficiency pack
-- Applies schema upgrades and consolidates data to canonical hotel.

-- Canonical hotel selection:
-- 1) Hotel whose code is DEFAULT, else
-- 2) Lowest hotel id.
SET @canonical_hotel_id := COALESCE(
    (SELECT id FROM new_bot_hotels WHERE code = 'DEFAULT' ORDER BY id LIMIT 1),
    (SELECT MIN(id) FROM new_bot_hotels)
);

SELECT @canonical_hotel_id AS canonical_hotel_id;

DROP TEMPORARY TABLE IF EXISTS tmp_source_hotels;
CREATE TEMPORARY TABLE tmp_source_hotels AS
SELECT id
FROM new_bot_hotels
WHERE id <> @canonical_hotel_id;

-- Hard-stop preconditions for safe merge.
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_assert_migration_preconditions $$
CREATE PROCEDURE sp_assert_migration_preconditions()
BEGIN
    DECLARE canonical_exists INT DEFAULT 0;
    DECLARE restaurant_conflicts INT DEFAULT 0;
    DECLARE guest_conflicts INT DEFAULT 0;

    SELECT COUNT(*) INTO canonical_exists
    FROM new_bot_hotels
    WHERE id = @canonical_hotel_id;

    IF canonical_exists = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Migration aborted: canonical hotel does not exist.';
    END IF;

    SELECT COUNT(*) INTO restaurant_conflicts
    FROM new_bot_restaurants s
    JOIN tmp_source_hotels src ON src.id = s.hotel_id
    JOIN new_bot_restaurants c
      ON c.hotel_id = @canonical_hotel_id
     AND c.code = s.code;

    IF restaurant_conflicts > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Migration aborted: duplicate restaurant codes detected.';
    END IF;

    SELECT COUNT(*) INTO guest_conflicts
    FROM new_bot_guests s
    JOIN tmp_source_hotels src ON src.id = s.hotel_id
    JOIN new_bot_guests c
      ON c.hotel_id = @canonical_hotel_id
     AND c.phone_number = s.phone_number;

    IF guest_conflicts > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Migration aborted: duplicate guest phone numbers detected.';
    END IF;
END $$
DELIMITER ;

CALL sp_assert_migration_preconditions();
DROP PROCEDURE IF EXISTS sp_assert_migration_preconditions;

-- -------------------------------
-- Schema upgrades (idempotent)
-- -------------------------------

-- new_bot_conversations.pending_action
SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'pending_action'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE new_bot_conversations ADD COLUMN pending_action VARCHAR(100) NULL AFTER state',
    'SELECT ''skip pending_action'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- new_bot_conversations.pending_data
SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'pending_data'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE new_bot_conversations ADD COLUMN pending_data JSON NULL AFTER pending_action',
    'SELECT ''skip pending_data'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- new_bot_conversations.channel
SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'channel'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE new_bot_conversations ADD COLUMN channel VARCHAR(20) NOT NULL DEFAULT ''web'' AFTER pending_data',
    'SELECT ''skip conversations.channel'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- new_bot_messages.channel
SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_messages'
      AND column_name = 'channel'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE new_bot_messages ADD COLUMN channel VARCHAR(20) NOT NULL DEFAULT ''web'' AFTER confidence',
    'SELECT ''skip messages.channel'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Index: new_bot_messages(conversation_id, created_at, id)
SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_messages'
      AND index_name = 'idx_messages_conv_created'
);
SET @sql := IF(
    @idx_exists = 0,
    'CREATE INDEX idx_messages_conv_created ON new_bot_messages (conversation_id, created_at, id)',
    'SELECT ''skip idx_messages_conv_created'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Index: new_bot_conversations(hotel_id, state)
SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND index_name = 'idx_conversations_hotel_state'
);
SET @sql := IF(
    @idx_exists = 0,
    'CREATE INDEX idx_conversations_hotel_state ON new_bot_conversations (hotel_id, state)',
    'SELECT ''skip idx_conversations_hotel_state'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Index: new_bot_orders(restaurant_id, status, created_at)
SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_orders'
      AND index_name = 'idx_orders_rest_status_created'
);
SET @sql := IF(
    @idx_exists = 0,
    'CREATE INDEX idx_orders_rest_status_created ON new_bot_orders (restaurant_id, status, created_at)',
    'SELECT ''skip idx_orders_rest_status_created'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- -------------------------------
-- Data consolidation to canonical hotel
-- -------------------------------

-- Merge key-value business config.
INSERT INTO new_bot_business_config (hotel_id, config_key, config_value, created_at, updated_at)
SELECT
    @canonical_hotel_id AS hotel_id,
    bc.config_key,
    bc.config_value,
    bc.created_at,
    bc.updated_at
FROM new_bot_business_config bc
JOIN tmp_source_hotels src ON src.id = bc.hotel_id
ON DUPLICATE KEY UPDATE
    config_value = VALUES(config_value),
    updated_at = CURRENT_TIMESTAMP;

DELETE bc
FROM new_bot_business_config bc
JOIN tmp_source_hotels src ON src.id = bc.hotel_id;

-- Merge capabilities.
INSERT INTO new_bot_capabilities (hotel_id, capability_id, description, enabled, hours, created_at, updated_at)
SELECT
    @canonical_hotel_id AS hotel_id,
    c.capability_id,
    c.description,
    c.enabled,
    c.hours,
    c.created_at,
    c.updated_at
FROM new_bot_capabilities c
JOIN tmp_source_hotels src ON src.id = c.hotel_id
ON DUPLICATE KEY UPDATE
    description = VALUES(description),
    enabled = VALUES(enabled),
    hours = VALUES(hours),
    updated_at = CURRENT_TIMESTAMP;

DELETE c
FROM new_bot_capabilities c
JOIN tmp_source_hotels src ON src.id = c.hotel_id;

-- Merge intents.
INSERT INTO new_bot_intents (hotel_id, intent_id, label, enabled, created_at, updated_at)
SELECT
    @canonical_hotel_id AS hotel_id,
    i.intent_id,
    i.label,
    i.enabled,
    i.created_at,
    i.updated_at
FROM new_bot_intents i
JOIN tmp_source_hotels src ON src.id = i.hotel_id
ON DUPLICATE KEY UPDATE
    label = VALUES(label),
    enabled = VALUES(enabled),
    updated_at = CURRENT_TIMESTAMP;

DELETE i
FROM new_bot_intents i
JOIN tmp_source_hotels src ON src.id = i.hotel_id;

-- Move service, guest, and conversation ownership.
UPDATE new_bot_restaurants r
JOIN tmp_source_hotels src ON src.id = r.hotel_id
SET r.hotel_id = @canonical_hotel_id;

UPDATE new_bot_guests g
JOIN tmp_source_hotels src ON src.id = g.hotel_id
SET g.hotel_id = @canonical_hotel_id;

UPDATE new_bot_conversations c
JOIN tmp_source_hotels src ON src.id = c.hotel_id
SET c.hotel_id = @canonical_hotel_id;

-- Keep only canonical hotel active and ensure default code for runtime lookup.
UPDATE new_bot_hotels h
JOIN tmp_source_hotels src ON src.id = h.id
SET h.is_active = 0;

UPDATE new_bot_hotels
SET is_active = 1
WHERE id = @canonical_hotel_id;

UPDATE new_bot_hotels
SET code = 'DEFAULT'
WHERE id = @canonical_hotel_id;

-- End-state summary
SELECT id, code, name, city, is_active
FROM new_bot_hotels
ORDER BY id;

SELECT hotel_id, COUNT(*) AS config_rows
FROM new_bot_business_config
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS capability_rows
FROM new_bot_capabilities
GROUP BY hotel_id
ORDER BY hotel_id;

SELECT hotel_id, COUNT(*) AS intent_rows
FROM new_bot_intents
GROUP BY hotel_id
ORDER BY hotel_id;
