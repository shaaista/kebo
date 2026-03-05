"""
Script to recreate all new_bot_* tables with INT AUTO_INCREMENT IDs.
Run this once to set up the database structure.
"""

import asyncio
import aiomysql

DB_CONFIG = {
    'host': '172.16.5.32',
    'port': 3306,
    'user': 'root',
    'password': 'zapcom123',
    'db': 'GHN_PROD_BAK',
    'connect_timeout': 60
}

async def recreate_tables():
    print('Connecting to MySQL...')
    conn = await aiomysql.connect(**DB_CONFIG)

    async with conn.cursor() as cur:
        # Disable foreign key checks to avoid dependency issues during drop
        await cur.execute('SET FOREIGN_KEY_CHECKS = 0')

        # Drop old tables
        print('\n' + '='*60)
        print('DROPPING OLD TABLES')
        print('='*60)

        tables_to_drop = [
            'new_bot_messages', 'new_bot_order_items', 'new_bot_orders',
            'new_bot_conversations', 'new_bot_guests', 'new_bot_menu_items',
            'new_bot_capabilities', 'new_bot_intents', 'new_bot_business_config',
            'new_bot_restaurants', 'new_bot_hotels'
        ]

        for table in tables_to_drop:
            await cur.execute(f'DROP TABLE IF EXISTS {table}')
            print(f'  Dropped: {table}')

        # Re-enable foreign key checks
        await cur.execute('SET FOREIGN_KEY_CHECKS = 1')
        await conn.commit()

        # Create new tables
        print('\n' + '='*60)
        print('CREATING NEW TABLES WITH INT IDs')
        print('='*60)

        # 1. Hotels
        print('\n1. Creating new_bot_hotels...')
        await cur.execute('''
            CREATE TABLE new_bot_hotels (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                city VARCHAR(100) NOT NULL,
                timezone VARCHAR(50) DEFAULT 'Asia/Kolkata',
                is_active BOOLEAN DEFAULT TRUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        ''')
        print('   Done!')

        # 2. Business Config
        print('2. Creating new_bot_business_config...')
        await cur.execute('''
            CREATE TABLE new_bot_business_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                hotel_id INT NOT NULL,
                config_key VARCHAR(100) NOT NULL,
                config_value TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_config (hotel_id, config_key),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 3. Capabilities
        print('3. Creating new_bot_capabilities...')
        await cur.execute('''
            CREATE TABLE new_bot_capabilities (
                id INT AUTO_INCREMENT PRIMARY KEY,
                hotel_id INT NOT NULL,
                capability_id VARCHAR(100) NOT NULL,
                description VARCHAR(255),
                enabled BOOLEAN DEFAULT TRUE,
                hours VARCHAR(100),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_cap (hotel_id, capability_id),
                INDEX idx_hotel (hotel_id),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 4. Intents
        print('4. Creating new_bot_intents...')
        await cur.execute('''
            CREATE TABLE new_bot_intents (
                id INT AUTO_INCREMENT PRIMARY KEY,
                hotel_id INT NOT NULL,
                intent_id VARCHAR(100) NOT NULL,
                label VARCHAR(100) NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_intent (hotel_id, intent_id),
                INDEX idx_hotel (hotel_id),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 5. Restaurants
        print('5. Creating new_bot_restaurants...')
        await cur.execute('''
            CREATE TABLE new_bot_restaurants (
                id INT AUTO_INCREMENT PRIMARY KEY,
                hotel_id INT NOT NULL,
                code VARCHAR(50) NOT NULL,
                name VARCHAR(255) NOT NULL,
                cuisine VARCHAR(100),
                opens_at TIME,
                closes_at TIME,
                delivers_to_room BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_rest (hotel_id, code),
                INDEX idx_hotel (hotel_id),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 6. Menu Items
        print('6. Creating new_bot_menu_items...')
        await cur.execute('''
            CREATE TABLE new_bot_menu_items (
                id INT AUTO_INCREMENT PRIMARY KEY,
                restaurant_id INT NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                price DECIMAL(10,2) DEFAULT 0.00,
                currency VARCHAR(3) DEFAULT 'INR',
                category VARCHAR(100),
                is_vegetarian BOOLEAN DEFAULT FALSE,
                is_available BOOLEAN DEFAULT TRUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_rest (restaurant_id),
                FOREIGN KEY (restaurant_id) REFERENCES new_bot_restaurants(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 7. Guests
        print('7. Creating new_bot_guests...')
        await cur.execute('''
            CREATE TABLE new_bot_guests (
                id INT AUTO_INCREMENT PRIMARY KEY,
                hotel_id INT NOT NULL,
                phone_number VARCHAR(30) NOT NULL,
                name VARCHAR(255),
                room_number VARCHAR(20),
                check_in_date DATE,
                check_out_date DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_guest (hotel_id, phone_number),
                INDEX idx_hotel (hotel_id),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 8. Conversations
        print('8. Creating new_bot_conversations...')
        await cur.execute('''
            CREATE TABLE new_bot_conversations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(100) NOT NULL UNIQUE,
                hotel_id INT NOT NULL,
                guest_id INT,
                state VARCHAR(50) DEFAULT 'idle',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_hotel (hotel_id),
                INDEX idx_guest (guest_id),
                FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE,
                FOREIGN KEY (guest_id) REFERENCES new_bot_guests(id) ON DELETE SET NULL
            )
        ''')
        print('   Done!')

        # 9. Messages
        print('9. Creating new_bot_messages...')
        await cur.execute('''
            CREATE TABLE new_bot_messages (
                id INT AUTO_INCREMENT PRIMARY KEY,
                conversation_id INT NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                intent VARCHAR(100),
                confidence FLOAT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_conv (conversation_id),
                FOREIGN KEY (conversation_id) REFERENCES new_bot_conversations(id) ON DELETE CASCADE
            )
        ''')
        print('   Done!')

        # 10. Orders
        print('10. Creating new_bot_orders...')
        await cur.execute('''
            CREATE TABLE new_bot_orders (
                id INT AUTO_INCREMENT PRIMARY KEY,
                guest_id INT NOT NULL,
                restaurant_id INT NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                total_amount DECIMAL(10,2) DEFAULT 0.00,
                delivery_location VARCHAR(100),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_guest (guest_id),
                INDEX idx_rest (restaurant_id),
                FOREIGN KEY (guest_id) REFERENCES new_bot_guests(id),
                FOREIGN KEY (restaurant_id) REFERENCES new_bot_restaurants(id)
            )
        ''')
        print('   Done!')

        # 11. Order Items
        print('11. Creating new_bot_order_items...')
        await cur.execute('''
            CREATE TABLE new_bot_order_items (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id INT NOT NULL,
                menu_item_id INT NOT NULL,
                quantity INT DEFAULT 1,
                unit_price DECIMAL(10,2) DEFAULT 0.00,
                INDEX idx_order (order_id),
                FOREIGN KEY (order_id) REFERENCES new_bot_orders(id) ON DELETE CASCADE,
                FOREIGN KEY (menu_item_id) REFERENCES new_bot_menu_items(id)
            )
        ''')
        print('   Done!')

        await conn.commit()

        # Show results
        print('\n' + '='*60)
        print('DATABASE STRUCTURE (ALL TABLES EMPTY)')
        print('='*60)

        await cur.execute('SHOW TABLES LIKE "new_bot_%"')
        tables = await cur.fetchall()

        print(f'\nTotal tables: {len(tables)}')
        print('\nTable Details:')
        for table in tables:
            name = table[0]
            await cur.execute(f'SELECT COUNT(*) FROM {name}')
            count = (await cur.fetchone())[0]
            await cur.execute(f'SHOW COLUMNS FROM {name} LIKE "id"')
            id_info = await cur.fetchone()
            id_type = id_info[1] if id_info else 'N/A'
            print(f'  {name}: {count} rows (ID: {id_type})')

    conn.close()
    await conn.ensure_closed()
    print('\n' + '='*60)
    print('SUCCESS! All tables ready with INT AUTO_INCREMENT IDs')
    print('='*60)


if __name__ == '__main__':
    try:
        asyncio.run(recreate_tables())
    except KeyboardInterrupt:
        print('\nAborted.')
    except Exception as e:
        print(f'\nError: {e}')
