import sys, json
sys.stdout.reconfigure(encoding='utf-8')

with open('config/business_config.json', encoding='utf-8') as f:
    cfg = json.load(f)

fixes = {
    'in_room_dining': {
        'profile': 'food_order',
        'role': 'You are the In-Room Dining assistant for ICONIQA Hotel. You help guests order food and beverages delivered to their room.',
        'professional_behavior': 'Present menu options from service_knowledge. Collect the food items wanted and room number. Confirm the order summary before placing.',
        'slots': [
            {'id': 'food_items', 'label': 'Food Order', 'prompt': 'What would you like to order? I can share our full menu.', 'required': True, 'type': 'text'},
            {'id': 'room_number', 'label': 'Room Number', 'prompt': 'What is your room number?', 'required': True, 'type': 'text'},
            {'id': 'special_requests', 'label': 'Special Requests', 'prompt': 'Any dietary requirements or special requests?', 'required': False, 'type': 'text'},
        ]
    },
    'spa_booking_assist': {
        'profile': 'appointment_booking',
        'role': 'You are the Spa Booking assistant for ICONIQA Hotel. You help guests book spa treatments and wellness sessions.',
        'professional_behavior': 'Present available treatments from service_knowledge. Collect treatment type, date, time, and guest count. Confirm before booking.',
        'slots': [
            {'id': 'treatment_type', 'label': 'Treatment Type', 'prompt': 'Which spa treatment are you interested in?', 'required': True, 'type': 'text'},
            {'id': 'appointment_date', 'label': 'Appointment Date', 'prompt': 'What date would you like?', 'required': True, 'type': 'date'},
            {'id': 'appointment_time', 'label': 'Appointment Time', 'prompt': 'What time works for you?', 'required': True, 'type': 'time'},
            {'id': 'guest_count', 'label': 'Number of Guests', 'prompt': 'How many guests?', 'required': True, 'type': 'number'},
        ]
    },
    'restaurant_reservation': {
        'profile': 'appointment_booking',
        'role': 'You are the Restaurant Reservation assistant for ICONIQA Hotel. You help guests book tables at the hotel restaurant.',
        'professional_behavior': 'Collect reservation date, time, and party size. Confirm the booking summary before placing.',
        'slots': [
            {'id': 'reservation_date', 'label': 'Reservation Date', 'prompt': 'What date would you like to reserve?', 'required': True, 'type': 'date'},
            {'id': 'reservation_time', 'label': 'Reservation Time', 'prompt': 'What time?', 'required': True, 'type': 'time'},
            {'id': 'party_size', 'label': 'Party Size', 'prompt': 'How many guests?', 'required': True, 'type': 'number'},
            {'id': 'seating_preference', 'label': 'Seating Preference', 'prompt': 'Indoor, outdoor, or private dining?', 'required': False, 'type': 'text'},
        ]
    },
    'housekeeping_request': {
        'profile': 'service_request',
        'role': 'You are the Housekeeping assistant for ICONIQA Hotel. You help guests with room cleaning, towels, amenities, and housekeeping needs.',
        'professional_behavior': 'Understand the housekeeping need, collect room number, confirm the request.',
        'slots': [
            {'id': 'request_details', 'label': 'Request Details', 'prompt': 'What do you need? (e.g. room cleaning, extra towels, linen change, fresh amenities)', 'required': True, 'type': 'text'},
            {'id': 'room_number', 'label': 'Room Number', 'prompt': 'What is your room number?', 'required': True, 'type': 'text'},
            {'id': 'preferred_time', 'label': 'Preferred Time', 'prompt': 'Any preferred time for the service?', 'required': False, 'type': 'text'},
        ]
    },
    'early_checkin_support': {
        'profile': 'service_request',
        'role': 'You are the Early Check-in support assistant for ICONIQA Hotel.',
        'professional_behavior': 'Collect arrival date and time to arrange early check-in. Policy: before 7 AM = 50% room rate; subject to availability.',
        'slots': [
            {'id': 'arrival_date', 'label': 'Arrival Date', 'prompt': 'What date will you arrive?', 'required': True, 'type': 'date'},
            {'id': 'arrival_time', 'label': 'Arrival Time', 'prompt': 'What time do you expect to arrive?', 'required': True, 'type': 'time'},
            {'id': 'special_requests', 'label': 'Special Requests', 'prompt': 'Any special requirements?', 'required': False, 'type': 'text'},
        ]
    },
    'maintenance_support': {
        'profile': 'issue_resolution',
        'role': 'You are the Maintenance support assistant for ICONIQA Hotel. You help guests report and resolve room maintenance issues.',
        'professional_behavior': 'Understand the issue clearly, get room number, raise a request for the engineering team.',
        'slots': [
            {'id': 'issue_description', 'label': 'Issue Description', 'prompt': 'Please describe the maintenance issue (e.g. AC not working, plumbing, lights).', 'required': True, 'type': 'text'},
            {'id': 'room_number', 'label': 'Room Number', 'prompt': 'What is your room number?', 'required': True, 'type': 'text'},
        ]
    },
    'lost_found_desk': {
        'profile': 'issue_resolution',
        'role': 'You are the Lost and Found assistant for ICONIQA Hotel.',
        'professional_behavior': 'Collect item description, when and where it was lost, and contact details.',
        'slots': [
            {'id': 'item_description', 'label': 'Item Description', 'prompt': 'Please describe the lost or found item.', 'required': True, 'type': 'text'},
            {'id': 'location_details', 'label': 'Location/When', 'prompt': 'Where and when was it lost or found?', 'required': True, 'type': 'text'},
            {'id': 'contact_info', 'label': 'Contact Info', 'prompt': 'Your room number or contact number?', 'required': False, 'type': 'text'},
        ]
    },
    'invoice_billing_support': {
        'profile': 'service_request',
        'role': 'You are the Invoice and Billing support assistant for ICONIQA Hotel.',
        'professional_behavior': 'Understand the billing query, collect details, raise a request to the finance team.',
        'slots': [
            {'id': 'query_details', 'label': 'Query Details', 'prompt': 'Please describe your billing query or issue.', 'required': True, 'type': 'text'},
            {'id': 'room_number', 'label': 'Room Number', 'prompt': 'What is your room number?', 'required': False, 'type': 'text'},
        ]
    },
}

for svc in cfg.get('services', []):
    svc_id = svc.get('id')
    if svc_id not in fixes:
        continue
    fix = fixes[svc_id]
    pp = svc.setdefault('service_prompt_pack', {})
    pp['profile'] = fix['profile']
    pp['role'] = fix['role']
    pp['professional_behavior'] = fix['professional_behavior']
    pp['required_slots'] = fix['slots']
    print(f'Fixed {svc_id}: profile={fix["profile"]}, slots={[s["id"] for s in fix["slots"]]}')

with open('config/business_config.json', 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('Saved.')
