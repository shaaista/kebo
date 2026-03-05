-- Backup snapshot: 2026-02-11 DB efficiency pack
-- Creates point-in-time backup copies before applying 02_up.sql.

-- Optional sanity check.
SELECT DATABASE() AS current_database;

DROP TABLE IF EXISTS bak_20260211_new_bot_hotels;
CREATE TABLE bak_20260211_new_bot_hotels AS
SELECT * FROM new_bot_hotels;

DROP TABLE IF EXISTS bak_20260211_new_bot_business_config;
CREATE TABLE bak_20260211_new_bot_business_config AS
SELECT * FROM new_bot_business_config;

DROP TABLE IF EXISTS bak_20260211_new_bot_capabilities;
CREATE TABLE bak_20260211_new_bot_capabilities AS
SELECT * FROM new_bot_capabilities;

DROP TABLE IF EXISTS bak_20260211_new_bot_intents;
CREATE TABLE bak_20260211_new_bot_intents AS
SELECT * FROM new_bot_intents;

DROP TABLE IF EXISTS bak_20260211_new_bot_restaurants;
CREATE TABLE bak_20260211_new_bot_restaurants AS
SELECT * FROM new_bot_restaurants;

DROP TABLE IF EXISTS bak_20260211_new_bot_menu_items;
CREATE TABLE bak_20260211_new_bot_menu_items AS
SELECT * FROM new_bot_menu_items;

DROP TABLE IF EXISTS bak_20260211_new_bot_guests;
CREATE TABLE bak_20260211_new_bot_guests AS
SELECT * FROM new_bot_guests;

DROP TABLE IF EXISTS bak_20260211_new_bot_conversations;
CREATE TABLE bak_20260211_new_bot_conversations AS
SELECT * FROM new_bot_conversations;

DROP TABLE IF EXISTS bak_20260211_new_bot_messages;
CREATE TABLE bak_20260211_new_bot_messages AS
SELECT * FROM new_bot_messages;

DROP TABLE IF EXISTS bak_20260211_new_bot_orders;
CREATE TABLE bak_20260211_new_bot_orders AS
SELECT * FROM new_bot_orders;

DROP TABLE IF EXISTS bak_20260211_new_bot_order_items;
CREATE TABLE bak_20260211_new_bot_order_items AS
SELECT * FROM new_bot_order_items;

-- Verify backup counts
SELECT 'bak_20260211_new_bot_hotels' AS table_name, COUNT(*) AS row_count FROM bak_20260211_new_bot_hotels
UNION ALL
SELECT 'bak_20260211_new_bot_business_config', COUNT(*) FROM bak_20260211_new_bot_business_config
UNION ALL
SELECT 'bak_20260211_new_bot_capabilities', COUNT(*) FROM bak_20260211_new_bot_capabilities
UNION ALL
SELECT 'bak_20260211_new_bot_intents', COUNT(*) FROM bak_20260211_new_bot_intents
UNION ALL
SELECT 'bak_20260211_new_bot_restaurants', COUNT(*) FROM bak_20260211_new_bot_restaurants
UNION ALL
SELECT 'bak_20260211_new_bot_menu_items', COUNT(*) FROM bak_20260211_new_bot_menu_items
UNION ALL
SELECT 'bak_20260211_new_bot_guests', COUNT(*) FROM bak_20260211_new_bot_guests
UNION ALL
SELECT 'bak_20260211_new_bot_conversations', COUNT(*) FROM bak_20260211_new_bot_conversations
UNION ALL
SELECT 'bak_20260211_new_bot_messages', COUNT(*) FROM bak_20260211_new_bot_messages
UNION ALL
SELECT 'bak_20260211_new_bot_orders', COUNT(*) FROM bak_20260211_new_bot_orders
UNION ALL
SELECT 'bak_20260211_new_bot_order_items', COUNT(*) FROM bak_20260211_new_bot_order_items;
