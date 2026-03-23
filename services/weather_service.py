import logging
import asyncio
import httpx
from typing import Optional, Tuple
from datetime import datetime, timedelta
from services.config_service import config_service

logger = logging.getLogger(__name__)

# Cache variables to avoid spamming the Open-Meteo API
_WEATHER_CACHE = {}
_CACHE_TTL_MINUTES = 30

WEATHER_CODE_MAPPING = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

async def get_coordinates_for_city(city: str) -> Optional[Tuple[float, float]]:
    logger.info(f"[WeatherService] Geocoding city {city}")
    url = f"https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 1, "language": "en", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if "results" in data and len(data["results"]) > 0:
                loc = data["results"][0]
                logger.info(f"[WeatherService] Geocoded location: {loc.get('latitude')}, {loc.get('longitude')}")
                return loc.get("latitude"), loc.get("longitude")
            logger.warning(f"[WeatherService] No results found for city {city}")
    except Exception as e:
        logger.warning(f"[WeatherService] Failed to geocode city '{city}': {e}")
    return None

async def fetch_weather_for_coords(lat: float, lon: float) -> Optional[dict]:
    logger.info(f"[WeatherService] Fetching weather for {lat}, {lon}")
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "timezone": "auto"
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("current_weather")
    except Exception as e:
        logger.warning(f"[WeatherService] Failed to fetch weather for {lat},{lon}: {e}")
    return None

async def get_current_weather(hotel_code: str = "DEFAULT") -> str:
    """Gets the current weather as a formatted string for the hotel's city."""
    global _WEATHER_CACHE
    logger.info(f"[WeatherService] Handling weather request for {hotel_code}")
    
    # 1. Get city from config
    try:
        cap_summary = config_service.get_capability_summary(hotel_code)
        city = str(cap_summary.get("business", {}).get("city") or "Mumbai").strip()
    except Exception as e:
        logger.warning(f"[WeatherService] Could not resolve city for {hotel_code}: {e}")
        city = "Mumbai"
        
    cache_key = city.lower()
    
    # 2. Check cache
    if cache_key in _WEATHER_CACHE:
        cache_entry = _WEATHER_CACHE[cache_key]
        if datetime.now() - cache_entry["timestamp"] < timedelta(minutes=_CACHE_TTL_MINUTES):
            logger.info(f"[WeatherService] Returning cached weather for {city}")
            return cache_entry["data"]

    # 3. Fetch from API
    coords = await get_coordinates_for_city(city)
    if not coords:
        logger.warning(f"[WeatherService] Failed getting coords for {city}")
        return ""
        
    lat, lon = coords
    weather_data = await fetch_weather_for_coords(lat, lon)
    
    if not weather_data:
        logger.warning(f"[WeatherService] Failed to get weather data for {city}")
        return ""
        
    temp = weather_data.get("temperature", "unknown")
    code = weather_data.get("weathercode", -1)
    condition = WEATHER_CODE_MAPPING.get(code, "Unknown condition")
    
    result = f"The current weather in {city} is {condition} with a temperature of {temp}°C."
    logger.info(f"[WeatherService] Successfully fetched: {result}")
    
    # 4. Update cache
    _WEATHER_CACHE[cache_key] = {
        "timestamp": datetime.now(),
        "data": result
    }
    
    return result
