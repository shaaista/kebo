"""
Seed database with initial hotel data.
Inserts: hotel, restaurants, menu items, capabilities, intents, business config.
Data sourced from business_config.json and capabilities.py defaults.

Run: python seed_data.py
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


async def seed():
    print('Connecting to MySQL...')
    conn = await aiomysql.connect(**DB_CONFIG)

    async with conn.cursor() as cur:
        # ============================================================
        # 1. INSERT HOTEL
        # ============================================================
        print('\n1. Inserting hotel...')
        await cur.execute('''
            INSERT INTO new_bot_hotels (code, name, city, timezone, is_active)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE name=VALUES(name), city=VALUES(city)
        ''', ('mangala_giri', 'Mangala Giri', 'Bangalore', 'Asia/Kolkata', True))
        await conn.commit()

        # Get hotel ID
        await cur.execute("SELECT id FROM new_bot_hotels WHERE code = 'mangala_giri'")
        hotel_row = await cur.fetchone()
        hotel_id = hotel_row[0]
        print(f'   Hotel ID: {hotel_id}')

        # ============================================================
        # 2. INSERT RESTAURANTS
        # ============================================================
        print('\n2. Inserting restaurants...')
        restaurants = [
            ('ird', 'In-Room Dining', 'Multi-cuisine', '00:00:00', '23:59:00', True, True),
            ('kadak', 'Kadak', 'Indian Snacks & Chai', '06:00:00', '22:00:00', False, True),
            ('aviator', 'Aviator Lounge', 'Bar & Lounge', '17:00:00', '01:00:00', False, True),
            ('cafe247', '24/7 Cafe', 'All-day Dining', '00:00:00', '23:59:00', True, True),
        ]

        restaurant_ids = {}
        for code, name, cuisine, opens, closes, delivers, active in restaurants:
            await cur.execute('''
                INSERT INTO new_bot_restaurants
                (hotel_id, code, name, cuisine, opens_at, closes_at, delivers_to_room, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE name=VALUES(name), cuisine=VALUES(cuisine)
            ''', (hotel_id, code, name, cuisine, opens, closes, delivers, active))

        await conn.commit()

        # Get restaurant IDs
        await cur.execute(
            "SELECT id, code FROM new_bot_restaurants WHERE hotel_id = %s", (hotel_id,)
        )
        for row in await cur.fetchall():
            restaurant_ids[row[1]] = row[0]
            print(f'   {row[1]}: ID {row[0]}')

        # ============================================================
        # 3. INSERT MENU ITEMS
        # ============================================================
        print('\n3. Inserting menu items...')

        # In-Room Dining menu
        ird_items = [
            ('Margherita Pizza', 'Classic tomato and mozzarella', 450, 'Pizza', True),
            ('Pepperoni Pizza', 'Spicy pepperoni with cheese', 550, 'Pizza', False),
            ('Chicken Alfredo Pasta', 'Creamy white sauce pasta', 480, 'Pasta', False),
            ('Veg Penne Arrabiata', 'Spicy tomato sauce pasta', 380, 'Pasta', True),
            ('Butter Chicken', 'Creamy tomato-based curry', 520, 'Indian Main', False),
            ('Paneer Tikka Masala', 'Cottage cheese in spicy gravy', 420, 'Indian Main', True),
            ('Grilled Chicken', 'Herb marinated grilled chicken', 580, 'Continental', False),
            ('Caesar Salad', 'Romaine lettuce with caesar dressing', 320, 'Salads', True),
            ('Chocolate Brownie', 'Warm brownie with ice cream', 280, 'Desserts', True),
            ('Fresh Lime Soda', 'Refreshing lime drink', 120, 'Beverages', True),
        ]

        # Kadak menu
        kadak_items = [
            ('Dal Makhani', 'Creamy black lentils', 380, 'Main Course', True),
            ('Rogan Josh', 'Kashmiri lamb curry', 620, 'Main Course', False),
            ('Veg Biryani', 'Fragrant rice with spices', 450, 'Rice', True),
            ('Chicken Biryani', 'Hyderabadi style chicken biryani', 520, 'Rice', False),
            ('Naan', 'Tandoor baked bread', 80, 'Breads', True),
            ('Gulab Jamun', 'Sweet milk dumplings', 180, 'Desserts', True),
        ]

        # Aviator Lounge menu
        aviator_items = [
            ('Grilled Salmon', 'Atlantic salmon with herbs', 980, 'Seafood', False),
            ('Ribeye Steak', 'Prime cut with mushroom sauce', 1200, 'Steaks', False),
            ('Mushroom Risotto', 'Creamy arborio rice', 580, 'Main Course', True),
            ('Soup of the Day', "Chef's special soup", 280, 'Starters', True),
            ('Tiramisu', 'Classic Italian dessert', 380, 'Desserts', True),
        ]

        # 24/7 Cafe menu
        cafe_items = [
            ('Classic Burger', 'Beef patty with cheese and veggies', 350, 'Burgers', False),
            ('Veggie Burger', 'Grilled vegetable patty', 280, 'Burgers', True),
            ('Chicken Sandwich', 'Grilled chicken club sandwich', 320, 'Sandwiches', False),
            ('Veg Club Sandwich', 'Triple decker veggie sandwich', 260, 'Sandwiches', True),
            ('French Fries', 'Crispy golden fries', 180, 'Sides', True),
            ('Chicken Wings', 'Spicy buffalo wings', 380, 'Sides', False),
            ('Greek Salad', 'Fresh veggies with feta cheese', 290, 'Salads', True),
            ('Cappuccino', 'Classic Italian coffee', 180, 'Beverages', True),
            ('Iced Tea', 'Refreshing lemon iced tea', 150, 'Beverages', True),
            ('Chocolate Shake', 'Rich chocolate milkshake', 220, 'Beverages', True),
        ]

        menu_map = {
            'ird': ird_items,
            'kadak': kadak_items,
            'aviator': aviator_items,
            'cafe247': cafe_items,
        }

        total_items = 0
        for rest_code, items in menu_map.items():
            rest_id = restaurant_ids[rest_code]
            for name, desc, price, category, is_veg in items:
                await cur.execute('''
                    INSERT INTO new_bot_menu_items
                    (restaurant_id, name, description, price, currency, category, is_vegetarian, is_available)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (rest_id, name, desc, price, 'INR', category, is_veg, True))
                total_items += 1

        await conn.commit()
        print(f'   Inserted {total_items} menu items')

        # ============================================================
        # 4. INSERT BUSINESS CONFIG
        # ============================================================
        print('\n4. Inserting business config...')
        config_items = [
            ('business.type', 'hotel'),
            ('business.bot_name', 'maccha'),
            ('business.welcome_message', 'hello maccha'),
            ('business.currency', 'INR'),
            ('business.language', 'en'),
            ('escalation.confidence_threshold', '0.4'),
            ('escalation.max_clarification_attempts', '3'),
            ('escalation.escalation_message', 'Let me connect you with our team for better assistance.'),
            ('escalation.modes', '["live_chat", "ticket", "callback"]'),
        ]

        for key, value in config_items:
            await cur.execute('''
                INSERT INTO new_bot_business_config (hotel_id, config_key, config_value)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
            ''', (hotel_id, key, value))

        await conn.commit()
        print(f'   Inserted {len(config_items)} config entries')

        # ============================================================
        # 5. INSERT CAPABILITIES
        # ============================================================
        print('\n5. Inserting capabilities...')
        capabilities = [
            ('food_ordering', 'Order food from restaurants', True, '24/7'),
            ('room_service', 'In-room dining service', True, '24/7'),
            ('table_booking', 'Reserve tables at restaurants', True, None),
            ('spa_booking', 'Book spa treatments', True, '10:00 AM - 8:00 PM'),
            ('transport', 'Local cab and airport transfers', True, None),
            ('housekeeping', 'Room cleaning and amenity requests', True, '7:00 AM - 10:00 PM'),
            ('delivery_tracking', 'Track delivery status', True, None),
            ('feedback', 'Submit feedback', True, None),
            ('human_escalation', 'Connect to human agent', True, None),
            ('concierge', 'General hotel inquiries', True, None),
        ]

        for cap_id, desc, enabled, hours in capabilities:
            await cur.execute('''
                INSERT INTO new_bot_capabilities (hotel_id, capability_id, description, enabled, hours)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE description=VALUES(description), enabled=VALUES(enabled)
            ''', (hotel_id, cap_id, desc, enabled, hours))

        await conn.commit()
        print(f'   Inserted {len(capabilities)} capabilities')

        # ============================================================
        # 6. INSERT INTENTS
        # ============================================================
        print('\n6. Inserting intents...')
        intents = [
            ('greeting', 'Greeting'),
            ('menu_request', 'View Menu'),
            ('order_food', 'Order Food'),
            ('table_booking', 'Book Table'),
            ('room_service', 'Room Service'),
            ('spa_booking', 'Spa Booking'),
            ('transport', 'Transport Request'),
            ('complaint', 'Complaint'),
            ('faq', 'General Question'),
            ('human_request', 'Talk to Human'),
            ('feedback', 'Feedback'),
        ]

        for intent_id, label in intents:
            await cur.execute('''
                INSERT INTO new_bot_intents (hotel_id, intent_id, label, enabled)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE label=VALUES(label)
            ''', (hotel_id, intent_id, label, True))

        await conn.commit()
        print(f'   Inserted {len(intents)} intents')

        # ============================================================
        # SUMMARY
        # ============================================================
        print('\n' + '='*60)
        print('SEED DATA SUMMARY')
        print('='*60)

        tables = [
            'new_bot_hotels', 'new_bot_restaurants', 'new_bot_menu_items',
            'new_bot_business_config', 'new_bot_capabilities', 'new_bot_intents'
        ]
        for table in tables:
            await cur.execute(f'SELECT COUNT(*) FROM {table}')
            count = (await cur.fetchone())[0]
            print(f'  {table}: {count} rows')

        print('\nRestaurant -> Menu Items:')
        await cur.execute('''
            SELECT r.name, COUNT(m.id)
            FROM new_bot_restaurants r
            LEFT JOIN new_bot_menu_items m ON m.restaurant_id = r.id
            WHERE r.hotel_id = %s
            GROUP BY r.id, r.name
        ''', (hotel_id,))
        for row in await cur.fetchall():
            print(f'  {row[0]}: {row[1]} items')

    conn.close()
    await conn.ensure_closed()
    print('\n' + '='*60)
    print('DATABASE SEEDED SUCCESSFULLY!')
    print('='*60)


if __name__ == '__main__':
    try:
        asyncio.run(seed())
    except KeyboardInterrupt:
        print('\nAborted.')
    except Exception as e:
        print(f'\nError: {e}')
        import traceback
        traceback.print_exc()
