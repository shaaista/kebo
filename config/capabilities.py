"""
Capability Registry

Defines what the bot CAN and CANNOT do for each hotel.
This prevents hallucinations and false promises.

Based on Lumira failures:
- Problem C: Bot promised Hyderabad cab (hotel is in Mumbai)
- Problem G: Bot couldn't send multiple menus
- Kadak delivery: Bot implied delivery possible, then said dine-in only
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
from datetime import time


class ServiceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"  # Available with constraints
    COMING_SOON = "coming_soon"


class TimeWindow(BaseModel):
    """Operating hours for a service."""
    start: str = "00:00"  # 24-hour format
    end: str = "23:59"
    days: list[str] = Field(default=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])

    def is_open(self, current_time: str, current_day: str) -> bool:
        """Check if service is available at given time."""
        if current_day.lower()[:3] not in [d.lower()[:3] for d in self.days]:
            return False
        return self.start <= current_time <= self.end


class DeliveryZone(str, Enum):
    """Where food can be delivered."""
    ROOM = "room"
    POOL = "pool"
    LOBBY = "lobby"
    RESTAURANT_ONLY = "restaurant_only"  # Dine-in only


class MenuItem(BaseModel):
    """Individual menu item."""
    id: str
    name: str
    description: str = ""
    price: float
    currency: str = "INR"
    category: str = ""  # Starters, Main Course, Desserts, Beverages
    is_vegetarian: bool = False
    is_available: bool = True
    preparation_time_minutes: int = 20


class Restaurant(BaseModel):
    """Restaurant/outlet configuration."""
    id: str
    name: str
    cuisine: str
    status: ServiceStatus = ServiceStatus.AVAILABLE
    hours: TimeWindow = Field(default_factory=TimeWindow)
    delivery_zones: list[DeliveryZone] = Field(default=[DeliveryZone.RESTAURANT_ONLY])
    menu_url: Optional[str] = None
    menu_available: bool = True
    reservations_enabled: bool = True
    max_party_size: int = 10
    menu_items: list[MenuItem] = Field(default=[])  # Actual menu items

    def can_deliver_to_room(self) -> bool:
        return DeliveryZone.ROOM in self.delivery_zones

    def is_dine_in_only(self) -> bool:
        return self.delivery_zones == [DeliveryZone.RESTAURANT_ONLY]

    def get_menu_by_category(self) -> dict[str, list[MenuItem]]:
        """Group menu items by category."""
        categories = {}
        for item in self.menu_items:
            cat = item.category or "Other"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(item)
        return categories


class TransportService(BaseModel):
    """Transport/cab service configuration."""
    airport_transfer: bool = True
    local_travel: bool = True
    intercity: bool = False  # Most hotels don't offer this!
    max_distance_km: Optional[int] = 50
    advance_booking_hours: int = 2
    available_cities: list[str] = Field(default=[])  # Empty = local only


class SpaService(BaseModel):
    """Spa and wellness configuration."""
    status: ServiceStatus = ServiceStatus.AVAILABLE
    hours: TimeWindow = Field(default_factory=lambda: TimeWindow(start="10:00", end="20:00"))
    services: list[str] = Field(default=["massage", "facial", "body_treatment"])
    advance_booking_required: bool = True
    min_advance_hours: int = 2


class RoomServiceConfig(BaseModel):
    """In-room dining configuration."""
    status: ServiceStatus = ServiceStatus.AVAILABLE
    hours: TimeWindow = Field(default_factory=TimeWindow)  # 24/7 by default
    menu_id: Optional[str] = "ird_menu"
    max_items_per_order: int = 10
    delivery_time_minutes: int = 30


class HousekeepingConfig(BaseModel):
    """Housekeeping service configuration."""
    status: ServiceStatus = ServiceStatus.AVAILABLE
    hours: TimeWindow = Field(default_factory=lambda: TimeWindow(start="07:00", end="22:00"))
    services: list[str] = Field(default=[
        "room_cleaning",
        "towel_replacement",
        "amenity_request",
        "laundry",
        "turndown_service"
    ])


class MenuCapability(BaseModel):
    """Menu sending capabilities."""
    can_send_multiple: bool = True  # Fixes Lumira Problem G
    max_menus_at_once: int = 5
    formats_available: list[str] = Field(default=["pdf", "image", "text"])


class HotelCapabilities(BaseModel):
    """Complete capability configuration for a hotel."""

    # Hotel identification
    hotel_id: str
    hotel_code: str
    hotel_name: str
    city: str
    timezone: str = "Asia/Kolkata"

    # Core services
    room_service: RoomServiceConfig = Field(default_factory=RoomServiceConfig)
    housekeeping: HousekeepingConfig = Field(default_factory=HousekeepingConfig)
    spa: SpaService = Field(default_factory=SpaService)
    transport: TransportService = Field(default_factory=TransportService)
    menu_capability: MenuCapability = Field(default_factory=MenuCapability)

    # Restaurants/outlets
    restaurants: list[Restaurant] = Field(default=[])

    # Feature flags
    human_escalation_enabled: bool = True
    ticket_creation_enabled: bool = True
    table_booking_enabled: bool = True

    # Explicit limitations (things bot should NEVER promise)
    not_available: list[str] = Field(default=[
        "medical_services",
        "legal_advice",
        "financial_services",
    ])

    # Custom messages for unavailable services
    unavailable_messages: dict[str, str] = Field(default={
        "intercity_cab": "We only provide local cab services within {city}. For intercity travel, I can help you find external options.",
        "medical": "For medical emergencies, please call the front desk immediately or dial emergency services.",
    })


class CapabilityCheck(BaseModel):
    """Result of a capability check."""
    allowed: bool
    reason: str
    alternatives: list[str] = Field(default=[])
    constraints: Optional[dict] = None


class CapabilityRegistry:
    """
    Central registry for checking hotel capabilities.

    Usage:
        registry = CapabilityRegistry()
        registry.load_hotel("MUMBAI_GRAND", hotel_config)

        check = registry.can_do("MUMBAI_GRAND", "room_delivery", {"restaurant": "Kadak"})
        if not check.allowed:
            print(check.reason)  # "Kadak is dine-in only"
    """

    def __init__(self):
        self._hotels: dict[str, HotelCapabilities] = {}
        self._load_default_hotels()

    def _load_default_hotels(self):
        """Load default test hotel configurations."""

        # Sample menu items for In-Room Dining
        ird_menu = [
            MenuItem(id="ird_1", name="Margherita Pizza", description="Classic tomato and mozzarella", price=450, category="Pizza", is_vegetarian=True),
            MenuItem(id="ird_2", name="Pepperoni Pizza", description="Spicy pepperoni with cheese", price=550, category="Pizza"),
            MenuItem(id="ird_3", name="Chicken Alfredo Pasta", description="Creamy white sauce pasta", price=480, category="Pasta"),
            MenuItem(id="ird_4", name="Veg Penne Arrabiata", description="Spicy tomato sauce pasta", price=380, category="Pasta", is_vegetarian=True),
            MenuItem(id="ird_5", name="Butter Chicken", description="Creamy tomato-based curry", price=520, category="Indian Main"),
            MenuItem(id="ird_6", name="Paneer Tikka Masala", description="Cottage cheese in spicy gravy", price=420, category="Indian Main", is_vegetarian=True),
            MenuItem(id="ird_7", name="Grilled Chicken", description="Herb marinated grilled chicken", price=580, category="Continental"),
            MenuItem(id="ird_8", name="Caesar Salad", description="Romaine lettuce with caesar dressing", price=320, category="Salads", is_vegetarian=True),
            MenuItem(id="ird_9", name="Chocolate Brownie", description="Warm brownie with ice cream", price=280, category="Desserts", is_vegetarian=True),
            MenuItem(id="ird_10", name="Fresh Lime Soda", description="Refreshing lime drink", price=120, category="Beverages", is_vegetarian=True),
        ]

        # Sample menu items for 24/7 Cafe
        cafe_menu = [
            MenuItem(id="cafe_1", name="Classic Burger", description="Beef patty with cheese and veggies", price=350, category="Burgers"),
            MenuItem(id="cafe_2", name="Veggie Burger", description="Grilled vegetable patty", price=280, category="Burgers", is_vegetarian=True),
            MenuItem(id="cafe_3", name="Chicken Sandwich", description="Grilled chicken club sandwich", price=320, category="Sandwiches"),
            MenuItem(id="cafe_4", name="Veg Club Sandwich", description="Triple decker veggie sandwich", price=260, category="Sandwiches", is_vegetarian=True),
            MenuItem(id="cafe_5", name="French Fries", description="Crispy golden fries", price=180, category="Sides", is_vegetarian=True),
            MenuItem(id="cafe_6", name="Chicken Wings", description="Spicy buffalo wings", price=380, category="Sides"),
            MenuItem(id="cafe_7", name="Greek Salad", description="Fresh veggies with feta cheese", price=290, category="Salads", is_vegetarian=True),
            MenuItem(id="cafe_8", name="Cappuccino", description="Classic Italian coffee", price=180, category="Beverages", is_vegetarian=True),
            MenuItem(id="cafe_9", name="Iced Tea", description="Refreshing lemon iced tea", price=150, category="Beverages", is_vegetarian=True),
            MenuItem(id="cafe_10", name="Chocolate Shake", description="Rich chocolate milkshake", price=220, category="Beverages", is_vegetarian=True),
        ]

        # Sample menu items for Kadak (Indian)
        kadak_menu = [
            MenuItem(id="kad_1", name="Dal Makhani", description="Creamy black lentils", price=380, category="Main Course", is_vegetarian=True),
            MenuItem(id="kad_2", name="Rogan Josh", description="Kashmiri lamb curry", price=620, category="Main Course"),
            MenuItem(id="kad_3", name="Biryani", description="Fragrant rice with spices", price=450, category="Rice", is_vegetarian=True),
            MenuItem(id="kad_4", name="Chicken Biryani", description="Hyderabadi style chicken biryani", price=520, category="Rice"),
            MenuItem(id="kad_5", name="Naan", description="Tandoor baked bread", price=80, category="Breads", is_vegetarian=True),
            MenuItem(id="kad_6", name="Gulab Jamun", description="Sweet milk dumplings", price=180, category="Desserts", is_vegetarian=True),
        ]

        # Sample menu items for Aviator (Continental)
        aviator_menu = [
            MenuItem(id="avi_1", name="Grilled Salmon", description="Atlantic salmon with herbs", price=980, category="Seafood"),
            MenuItem(id="avi_2", name="Ribeye Steak", description="Prime cut with mushroom sauce", price=1200, category="Steaks"),
            MenuItem(id="avi_3", name="Mushroom Risotto", description="Creamy arborio rice", price=580, category="Main Course", is_vegetarian=True),
            MenuItem(id="avi_4", name="Soup of the Day", description="Chef's special soup", price=280, category="Starters", is_vegetarian=True),
            MenuItem(id="avi_5", name="Tiramisu", description="Classic Italian dessert", price=380, category="Desserts", is_vegetarian=True),
        ]

        # Test hotel with full capabilities
        test_hotel = HotelCapabilities(
            hotel_id="1",
            hotel_code="TEST_HOTEL",
            hotel_name="Test Hotel",
            city="Mumbai",
            restaurants=[
                Restaurant(
                    id="ird",
                    name="In-Room Dining",
                    cuisine="Multi-cuisine",
                    delivery_zones=[DeliveryZone.ROOM],
                    hours=TimeWindow(start="00:00", end="23:59"),
                    menu_items=ird_menu,
                ),
                Restaurant(
                    id="kadak",
                    name="Kadak",
                    cuisine="Indian",
                    delivery_zones=[DeliveryZone.RESTAURANT_ONLY],  # Dine-in only!
                    hours=TimeWindow(start="12:00", end="23:00"),
                    menu_items=kadak_menu,
                ),
                Restaurant(
                    id="aviator",
                    name="Aviator",
                    cuisine="Continental",
                    delivery_zones=[DeliveryZone.RESTAURANT_ONLY],  # Dine-in only!
                    hours=TimeWindow(start="18:00", end="23:00"),
                    menu_items=aviator_menu,
                ),
                Restaurant(
                    id="cafe247",
                    name="24/7 Cafe",
                    cuisine="All-day dining",
                    delivery_zones=[DeliveryZone.ROOM, DeliveryZone.POOL],
                    hours=TimeWindow(start="00:00", end="23:59"),
                    menu_items=cafe_menu,
                ),
            ],
            transport=TransportService(
                airport_transfer=True,
                local_travel=True,
                intercity=False,  # NO intercity!
                max_distance_km=50,
                available_cities=["Mumbai"],
            ),
        )
        self._hotels["TEST_HOTEL"] = test_hotel

    def load_hotel(self, hotel_code: str, config: HotelCapabilities):
        """Load a hotel configuration."""
        self._hotels[hotel_code] = config

    def get_hotel(self, hotel_code: str) -> Optional[HotelCapabilities]:
        """Get hotel configuration."""
        return self._hotels.get(hotel_code)

    def can_do(
        self,
        hotel_code: str,
        action: str,
        params: Optional[dict] = None
    ) -> CapabilityCheck:
        """
        Check if an action is allowed for this hotel.

        Args:
            hotel_code: Hotel identifier
            action: Action to check (e.g., "room_delivery", "intercity_cab", "send_menu")
            params: Additional parameters (e.g., {"restaurant": "Kadak"})

        Returns:
            CapabilityCheck with allowed status and reason
        """
        hotel = self.get_hotel(hotel_code)
        if not hotel:
            return CapabilityCheck(
                allowed=False,
                reason=f"Hotel {hotel_code} not found in registry"
            )

        params = params or {}

        # Route to specific capability checks
        check_methods = {
            "room_delivery": self._check_room_delivery,
            "restaurant_reservation": self._check_restaurant_reservation,
            "intercity_cab": self._check_intercity_cab,
            "local_cab": self._check_local_cab,
            "airport_transfer": self._check_airport_transfer,
            "spa_booking": self._check_spa_booking,
            "room_service": self._check_room_service,
            "send_menu": self._check_send_menu,
            "send_multiple_menus": self._check_multiple_menus,
            "human_escalation": self._check_human_escalation,
        }

        check_method = check_methods.get(action)
        if check_method:
            return check_method(hotel, params)

        # Check if explicitly not available
        if action in hotel.not_available:
            return CapabilityCheck(
                allowed=False,
                reason=f"{action} is not available at this hotel"
            )

        # Default: allow unknown actions (be permissive)
        return CapabilityCheck(allowed=True, reason="Action allowed")

    def _check_room_delivery(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check if a restaurant can deliver to room."""
        restaurant_name = params.get("restaurant", "").lower()

        for restaurant in hotel.restaurants:
            if restaurant.name.lower() == restaurant_name or restaurant.id.lower() == restaurant_name:
                if restaurant.can_deliver_to_room():
                    return CapabilityCheck(
                        allowed=True,
                        reason=f"{restaurant.name} can deliver to your room"
                    )
                else:
                    # Find alternatives that can deliver
                    alternatives = [
                        r.name for r in hotel.restaurants
                        if r.can_deliver_to_room()
                    ]
                    return CapabilityCheck(
                        allowed=False,
                        reason=f"{restaurant.name} is dine-in only and does not offer room delivery",
                        alternatives=alternatives
                    )

        return CapabilityCheck(
            allowed=False,
            reason=f"Restaurant '{restaurant_name}' not found"
        )

    def _check_restaurant_reservation(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check if table booking is available."""
        if not hotel.table_booking_enabled:
            return CapabilityCheck(
                allowed=False,
                reason="Table reservations are currently not available"
            )

        restaurant_name = params.get("restaurant", "").lower()
        if restaurant_name:
            for restaurant in hotel.restaurants:
                if restaurant.name.lower() == restaurant_name:
                    if restaurant.reservations_enabled:
                        return CapabilityCheck(
                            allowed=True,
                            reason=f"Table booking available at {restaurant.name}",
                            constraints={
                                "max_party_size": restaurant.max_party_size,
                                "hours": f"{restaurant.hours.start} - {restaurant.hours.end}"
                            }
                        )
                    else:
                        return CapabilityCheck(
                            allowed=False,
                            reason=f"{restaurant.name} does not accept reservations"
                        )

        return CapabilityCheck(allowed=True, reason="Table booking available")

    def _check_intercity_cab(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check intercity cab availability - usually NOT available!"""
        if hotel.transport.intercity:
            return CapabilityCheck(allowed=True, reason="Intercity cab service available")

        return CapabilityCheck(
            allowed=False,
            reason=hotel.unavailable_messages.get(
                "intercity_cab",
                f"We only provide local cab services within {hotel.city}. For intercity travel, please contact external providers."
            ).format(city=hotel.city),
            alternatives=["local_cab", "airport_transfer"]
        )

    def _check_local_cab(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check local cab availability."""
        if hotel.transport.local_travel:
            return CapabilityCheck(
                allowed=True,
                reason="Local cab service available",
                constraints={"max_distance_km": hotel.transport.max_distance_km}
            )
        return CapabilityCheck(allowed=False, reason="Local cab service not available")

    def _check_airport_transfer(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check airport transfer availability."""
        if hotel.transport.airport_transfer:
            return CapabilityCheck(
                allowed=True,
                reason="Airport transfer service available",
                constraints={"advance_booking_hours": hotel.transport.advance_booking_hours}
            )
        return CapabilityCheck(allowed=False, reason="Airport transfer not available")

    def _check_spa_booking(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check spa booking availability."""
        if hotel.spa.status == ServiceStatus.AVAILABLE:
            return CapabilityCheck(
                allowed=True,
                reason="Spa booking available",
                constraints={
                    "hours": f"{hotel.spa.hours.start} - {hotel.spa.hours.end}",
                    "services": hotel.spa.services,
                    "advance_booking_required": hotel.spa.advance_booking_required
                }
            )
        return CapabilityCheck(allowed=False, reason="Spa service currently unavailable")

    def _check_room_service(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check room service availability."""
        if hotel.room_service.status == ServiceStatus.AVAILABLE:
            return CapabilityCheck(
                allowed=True,
                reason="Room service available",
                constraints={
                    "hours": f"{hotel.room_service.hours.start} - {hotel.room_service.hours.end}",
                    "delivery_time": f"{hotel.room_service.delivery_time_minutes} minutes"
                }
            )
        return CapabilityCheck(allowed=False, reason="Room service currently unavailable")

    def _check_send_menu(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check if menu can be sent."""
        restaurant_name = params.get("restaurant", "").lower()

        for restaurant in hotel.restaurants:
            if restaurant.name.lower() == restaurant_name or restaurant.id.lower() == restaurant_name:
                if restaurant.menu_available:
                    return CapabilityCheck(
                        allowed=True,
                        reason=f"{restaurant.name} menu available",
                        constraints={"menu_url": restaurant.menu_url}
                    )
                else:
                    return CapabilityCheck(
                        allowed=False,
                        reason=f"{restaurant.name} menu is currently not available"
                    )

        # Return list of available menus
        available = [r.name for r in hotel.restaurants if r.menu_available]
        return CapabilityCheck(
            allowed=True,
            reason="Menus available",
            constraints={"available_menus": available}
        )

    def _check_multiple_menus(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check if multiple menus can be sent at once."""
        if hotel.menu_capability.can_send_multiple:
            return CapabilityCheck(
                allowed=True,
                reason=f"Can send up to {hotel.menu_capability.max_menus_at_once} menus at once"
            )
        return CapabilityCheck(
            allowed=False,
            reason="Only one menu can be sent at a time"
        )

    def _check_human_escalation(self, hotel: HotelCapabilities, params: dict) -> CapabilityCheck:
        """Check if human escalation is available."""
        if hotel.human_escalation_enabled:
            return CapabilityCheck(allowed=True, reason="Human escalation available")
        return CapabilityCheck(
            allowed=False,
            reason="Human escalation currently unavailable. Please try again later."
        )

    def get_restaurant_by_name(self, hotel_code: str, name: str) -> Optional[Restaurant]:
        """Get restaurant details by name."""
        hotel = self.get_hotel(hotel_code)
        if not hotel:
            return None

        name_lower = name.lower()
        for restaurant in hotel.restaurants:
            if restaurant.name.lower() == name_lower or restaurant.id.lower() == name_lower:
                return restaurant
        return None

    def get_menu_items(self, hotel_code: str, restaurant_name: str) -> list[MenuItem]:
        """Get menu items for a restaurant."""
        restaurant = self.get_restaurant_by_name(hotel_code, restaurant_name)
        if restaurant:
            return restaurant.menu_items
        return []

    def get_all_menus(self, hotel_code: str, room_delivery_only: bool = False) -> dict:
        """Get all menus for a hotel."""
        hotel = self.get_hotel(hotel_code)
        if not hotel:
            return {}

        menus = {}
        for restaurant in hotel.restaurants:
            if room_delivery_only and not restaurant.can_deliver_to_room():
                continue
            menus[restaurant.name] = {
                "cuisine": restaurant.cuisine,
                "hours": f"{restaurant.hours.start} - {restaurant.hours.end}",
                "delivers_to_room": restaurant.can_deliver_to_room(),
                "items": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "price": item.price,
                        "category": item.category,
                        "is_vegetarian": item.is_vegetarian,
                    }
                    for item in restaurant.menu_items if item.is_available
                ]
            }
        return menus

    def list_restaurants(self, hotel_code: str, delivery_to_room_only: bool = False) -> list[Restaurant]:
        """List all restaurants for a hotel."""
        hotel = self.get_hotel(hotel_code)
        if not hotel:
            return []

        if delivery_to_room_only:
            return [r for r in hotel.restaurants if r.can_deliver_to_room()]
        return hotel.restaurants

    def get_capability_summary(self, hotel_code: str) -> dict:
        """Get a summary of hotel capabilities for LLM context."""
        hotel = self.get_hotel(hotel_code)
        if not hotel:
            return {"error": "Hotel not found"}

        restaurants_info = []
        for r in hotel.restaurants:
            restaurants_info.append({
                "name": r.name,
                "cuisine": r.cuisine,
                "delivers_to_room": r.can_deliver_to_room(),
                "dine_in_only": r.is_dine_in_only(),
                "hours": f"{r.hours.start} - {r.hours.end}",
            })

        return {
            "hotel_name": hotel.hotel_name,
            "city": hotel.city,
            "services": {
                "room_service": hotel.room_service.status == ServiceStatus.AVAILABLE,
                "room_service_hours": f"{hotel.room_service.hours.start} - {hotel.room_service.hours.end}",
                "spa": hotel.spa.status == ServiceStatus.AVAILABLE,
                "spa_hours": f"{hotel.spa.hours.start} - {hotel.spa.hours.end}",
                "local_cab": hotel.transport.local_travel,
                "airport_transfer": hotel.transport.airport_transfer,
                "intercity_cab": hotel.transport.intercity,
                "table_booking": hotel.table_booking_enabled,
                "human_escalation": hotel.human_escalation_enabled,
            },
            "restaurants": restaurants_info,
            "limitations": hotel.not_available,
            "can_send_multiple_menus": hotel.menu_capability.can_send_multiple,
        }


# Global instance
capability_registry = CapabilityRegistry()
