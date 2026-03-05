-- Down migration: 2026-02-11 DB efficiency pack
-- Restores data from backup snapshot tables and reverts schema additions.
-- Requires tables created by 01_backup.sql (bak_20260211_*).

DELIMITER $$
DROP PROCEDURE IF EXISTS sp_assert_backup_exists $$
CREATE PROCEDURE sp_assert_backup_exists()
BEGIN
    DECLARE backup_count INT DEFAULT 0;

    SELECT COUNT(*) INTO backup_count
    FROM information_schema.tables
    WHERE table_schema = DATABASE()
      AND table_name IN (
        'bak_20260211_new_bot_hotels',
        'bak_20260211_new_bot_business_config',
        'bak_20260211_new_bot_capabilities',
        'bak_20260211_new_bot_intents',
        'bak_20260211_new_bot_restaurants',
        'bak_20260211_new_bot_menu_items',
        'bak_20260211_new_bot_guests',
        'bak_20260211_new_bot_conversations',
        'bak_20260211_new_bot_messages',
        'bak_20260211_new_bot_orders',
        'bak_20260211_new_bot_order_items'
      );

    IF backup_count <> 11 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Rollback aborted: required backup tables are missing.';
    END IF;
END $$
DELIMITER ;

CALL sp_assert_backup_exists();
DROP PROCEDURE IF EXISTS sp_assert_backup_exists;

SET FOREIGN_KEY_CHECKS = 0;

TRUNCATE TABLE new_bot_order_items;
TRUNCATE TABLE new_bot_messages;
TRUNCATE TABLE new_bot_orders;
TRUNCATE TABLE new_bot_conversations;
TRUNCATE TABLE new_bot_menu_items;
TRUNCATE TABLE new_bot_restaurants;
TRUNCATE TABLE new_bot_guests;
TRUNCATE TABLE new_bot_capabilities;
TRUNCATE TABLE new_bot_intents;
TRUNCATE TABLE new_bot_business_config;
TRUNCATE TABLE new_bot_hotels;

-- Restore base data (original column layout from backup snapshot).
INSERT INTO new_bot_hotels (
    id, code, name, city, timezone, is_active, created_at, updated_at
)
SELECT
    id, code, name, city, timezone, is_active, created_at, updated_at
FROM bak_20260211_new_bot_hotels;

INSERT INTO new_bot_business_config (
    id, hotel_id, config_key, config_value, created_at, updated_at
)
SELECT
    id, hotel_id, config_key, config_value, created_at, updated_at
FROM bak_20260211_new_bot_business_config;

INSERT INTO new_bot_capabilities (
    id, hotel_id, capability_id, description, enabled, hours, created_at, updated_at
)
SELECT
    id, hotel_id, capability_id, description, enabled, hours, created_at, updated_at
FROM bak_20260211_new_bot_capabilities;

INSERT INTO new_bot_intents (
    id, hotel_id, intent_id, label, enabled, created_at, updated_at
)
SELECT
    id, hotel_id, intent_id, label, enabled, created_at, updated_at
FROM bak_20260211_new_bot_intents;

INSERT INTO new_bot_restaurants (
    id, hotel_id, code, name, cuisine, opens_at, closes_at, delivers_to_room, is_active, created_at, updated_at
)
SELECT
    id, hotel_id, code, name, cuisine, opens_at, closes_at, delivers_to_room, is_active, created_at, updated_at
FROM bak_20260211_new_bot_restaurants;

INSERT INTO new_bot_menu_items (
    id, restaurant_id, name, description, price, currency, category, is_vegetarian, is_available, created_at, updated_at
)
SELECT
    id, restaurant_id, name, description, price, currency, category, is_vegetarian, is_available, created_at, updated_at
FROM bak_20260211_new_bot_menu_items;

INSERT INTO new_bot_guests (
    id, hotel_id, phone_number, name, room_number, check_in_date, check_out_date, created_at, updated_at
)
SELECT
    id, hotel_id, phone_number, name, room_number, check_in_date, check_out_date, created_at, updated_at
FROM bak_20260211_new_bot_guests;

INSERT INTO new_bot_conversations (
    id, session_id, hotel_id, guest_id, state, created_at, updated_at
)
SELECT
    id, session_id, hotel_id, guest_id, state, created_at, updated_at
FROM bak_20260211_new_bot_conversations;

INSERT INTO new_bot_orders (
    id, guest_id, restaurant_id, status, total_amount, delivery_location, created_at, updated_at
)
SELECT
    id, guest_id, restaurant_id, status, total_amount, delivery_location, created_at, updated_at
FROM bak_20260211_new_bot_orders;

INSERT INTO new_bot_order_items (
    id, order_id, menu_item_id, quantity, unit_price
)
SELECT
    id, order_id, menu_item_id, quantity, unit_price
FROM bak_20260211_new_bot_order_items;

INSERT INTO new_bot_messages (
    id, conversation_id, role, content, intent, confidence, created_at
)
SELECT
    id, conversation_id, role, content, intent, confidence, created_at
FROM bak_20260211_new_bot_messages;

SET FOREIGN_KEY_CHECKS = 1;

-- Revert indexes added in 02_up.sql (idempotent).
SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_messages'
      AND index_name = 'idx_messages_conv_created'
);
SET @sql := IF(
    @idx_exists > 0,
    'DROP INDEX idx_messages_conv_created ON new_bot_messages',
    'SELECT ''skip drop idx_messages_conv_created'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND index_name = 'idx_conversations_hotel_state'
);
SET @sql := IF(
    @idx_exists > 0,
    'DROP INDEX idx_conversations_hotel_state ON new_bot_conversations',
    'SELECT ''skip drop idx_conversations_hotel_state'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_orders'
      AND index_name = 'idx_orders_rest_status_created'
);
SET @sql := IF(
    @idx_exists > 0,
    'DROP INDEX idx_orders_rest_status_created ON new_bot_orders',
    'SELECT ''skip drop idx_orders_rest_status_created'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Revert columns added in 02_up.sql (idempotent).
SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'channel'
);
SET @sql := IF(
    @col_exists > 0,
    'ALTER TABLE new_bot_conversations DROP COLUMN channel',
    'SELECT ''skip drop conversations.channel'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'pending_data'
);
SET @sql := IF(
    @col_exists > 0,
    'ALTER TABLE new_bot_conversations DROP COLUMN pending_data',
    'SELECT ''skip drop pending_data'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_conversations'
      AND column_name = 'pending_action'
);
SET @sql := IF(
    @col_exists > 0,
    'ALTER TABLE new_bot_conversations DROP COLUMN pending_action',
    'SELECT ''skip drop pending_action'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'new_bot_messages'
      AND column_name = 'channel'
);
SET @sql := IF(
    @col_exists > 0,
    'ALTER TABLE new_bot_messages DROP COLUMN channel',
    'SELECT ''skip drop messages.channel'' AS msg'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Final sanity output
SELECT id, code, name, city, is_active
FROM new_bot_hotels
ORDER BY id;
