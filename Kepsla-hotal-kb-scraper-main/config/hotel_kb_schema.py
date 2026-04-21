"""
Hotel KB output schema — defines the sections and structure
that the final KB file should have for optimal kebo-main ingestion.

Each section maps to one or more services in kebo-main's service KB system.
The RAG chunker will split on section headers (===) for indexing.
"""

# Ordered list of KB sections — each becomes a header in the output .txt
KB_SECTIONS = [
    {
        "id": "property_overview",
        "title": "Property Overview",
        "description": "Canonical property name, brand, property type, positioning, address/location summary, star rating, inventory totals, check-in/check-out, overview notes",
        "maps_to_services": ["general_info", "concierge"],
    },
    {
        "id": "rooms_and_suites",
        "title": "Rooms & Suites",
        "description": "All room and suite types with names, counts, sizes, bed configuration, occupancy, views, in-room amenities, inclusions, and notable differentiators",
        "maps_to_services": ["room_info", "booking"],
    },
    {
        "id": "dining_restaurants",
        "title": "Dining & Restaurants",
        "description": "Restaurants, cafes, bars, lounges, room service, breakfast details, cuisine, timings, signature dishes, dining rules, and reservation guidance",
        "maps_to_services": ["restaurant_booking", "room_service", "menu_info"],
    },
    {
        "id": "amenities_facilities",
        "title": "Amenities & Facilities",
        "description": "Pool, gym, spa-adjacent facilities, recreation, Wi-Fi, business services, parking, laundry, accessibility, family amenities, and other guest services",
        "maps_to_services": ["amenities", "housekeeping", "laundry"],
    },
    {
        "id": "spa_wellness",
        "title": "Spa & Wellness",
        "description": "Spa, salon, wellness, yoga, fitness programming, treatment highlights, hours, booking requirements, and wellness access rules",
        "maps_to_services": ["spa_booking", "wellness"],
    },
    {
        "id": "meetings_events",
        "title": "Meetings & Events",
        "description": "Banquet halls, meeting rooms, event lawns, venue capacities, sizes, layouts, AV support, catering, weddings, and private event information",
        "maps_to_services": ["events", "meetings"],
    },
    {
        "id": "policies_rules",
        "title": "Policies & Rules",
        "description": "Cancellation, payment, children, pets, smoking, extra bed, early check-in, late checkout, deposits, IDs, and stay rules",
        "maps_to_services": ["policies", "booking"],
    },
    {
        "id": "location_transport",
        "title": "Location & Transport",
        "description": "Address, city/area, airport/station access, shuttle/transfer/taxi details, nearby landmarks, attractions, district context, and travel convenience notes",
        "maps_to_services": ["transport", "concierge", "local_guide"],
    },
    {
        "id": "contact_info",
        "title": "Contact Information",
        "description": "Phone, email, reservation contacts, website, social channels, front office/help desk details, and emergency or escalation contacts",
        "maps_to_services": ["general_info", "escalation"],
    },
    {
        "id": "special_offers",
        "title": "Special Offers & Packages",
        "description": "Packages, promotions, loyalty notes, direct booking offers, seasonal deals, corporate/long-stay offers, and package terms",
        "maps_to_services": ["booking", "loyalty"],
    },
]

# LLM extraction prompt template — used per chunk
EXTRACTION_SYSTEM_PROMPT = """You are a hotel knowledge base analyst creating a production-ready
property KB from hotel website content.

OUTPUT CONTRACT:
- Output ALL sections in the exact order provided.
- Use the exact header format: === SECTION TITLE ===
- Prefer concise `Field: Value` lines for facts.
- Use `## Subsection Name` blocks for room types, restaurants, event spaces, policies, or packages.
- Use bullet points only for lists of amenities, features, attractions, menu highlights, or inclusions.
- Extract only verifiable facts from the provided content. Remove marketing language and promotional filler.
- Preserve canonical proper nouns exactly: hotel name, brand, room names, restaurant names, venue names.
- Normalize the property identity to the real hotel/property name when the source metadata reveals it.
- Keep concrete numbers whenever available: counts, sizes, capacities, timings, prices, distances, fees, taxes, policies.
- For dining outlets, ALWAYS preserve meal timings plus any explicit menu highlights, signature dishes, beverages, wine labels, service style, dress codes, or reservation notes mentioned on the page.
- For amenities, spa, recreation, and packages, keep operational specifics like hours, access rules, age guidance, inclusions, and booking contacts whenever present.
- ABSOLUTE RULE — ROOMS: List EVERY room type as its own ## subsection. If every room has the same amenities, you MUST copy and repeat the full amenity list under each room subsection. "Same as [room name]", "Same amenities", "Amenities as above", or any cross-reference is a critical error. Each room section must be 100% self-contained even if that means repeating identical bullet lists. Omitting this repetition will corrupt downstream RAG retrieval.
- ABSOLUTE RULE — MEETINGS: Banquet halls, meeting rooms, conference venues, boardrooms, event lawns, and event spaces belong in the Meetings & Events section ONLY — not in Rooms & Suites. Extract them there with capacity, dimensions, AV equipment, and catering details.
- If a whole section has no reliable data, still output the section and add one line:
  Status: Not found on reviewed pages.
- Never invent missing facts and never pad with generic travel advice.
- If multiple properties appear, keep only facts relevant to the target property.
"""

EXTRACTION_USER_PROMPT = """Extract hotel information from the following website content.
Organize it into these sections (skip sections with no relevant data):

{sections_list}

Website URL: {url}
Property Name (if detected): {property_name}

--- RAW CONTENT ---
{content}
--- END CONTENT ---

Write a structured hotel KB using the exact section headers above.
Inside each section:
- Start with the most important facts as `Field: Value`
- Use `##` subsections for specific room types, outlets, venues, or policies
- Use bullets for amenities/features lists, menu highlights, package inclusions, or rule lists
- Keep wording neutral, compact, and factual
- Do not drop useful outlet-level specifics such as dish names, wine names, beverage labels, entertainment, or outlet-specific meal timings when they are stated in the source

BEFORE SUBMITTING: Search your output for the phrases "Same as", "same amenities", "as above", "see above". If ANY are found, go back and replace them with the full repeated list. This is mandatory.
"""

# Final merge/consolidation prompt
MERGE_PROMPT = """You are consolidating multiple extractions from different pages of the same
hotel website into a single, comprehensive knowledge base.

RULES:
- Merge duplicate information and keep the most detailed factual version.
- Resolve contradictions by keeping the most specific value; if uncertain, keep the safer factual wording.
- Output ALL sections in the provided order, each with an exact === SECTION HEADER === marker.
- Keep the property identity canonical and avoid generic SEO page titles when a better hotel name is evident.
- Convert vague marketing language into neutral factual statements.
- Preserve structured formatting: `Field: Value`, `## Subsection`, and bullets for lists.
- Preserve outlet-specific specifics during merging: dish names, wine labels, menu highlights, entertainment, meal timings, spa rules, and package inclusions should survive if they appear anywhere in the extracted chunks.
- ABSOLUTE RULE — ROOMS: Copy and repeat the full amenity/feature list under every room subsection even if identical across rooms. "Same as [room name]", "Same amenities", or any cross-reference is a critical error. Every room ## subsection must be 100% self-contained.
- ABSOLUTE RULE — MEETINGS: Banquet halls, meeting rooms, genius rooms, and event venues belong in Meetings & Events only. Merge all such pages into Meetings & Events with capacity, dimensions, and AV/catering details.
- When a section truly has no reliable data, include:
  Status: Not found on reviewed pages.
- The result must be a complete standalone property KB, not notes about the extraction process.

Property: {property_name}
Website: {url}

--- EXTRACTED CHUNKS ---
{chunks}
--- END CHUNKS ---

Output the final consolidated KB document with all sections.
"""
