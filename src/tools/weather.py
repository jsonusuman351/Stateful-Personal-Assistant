"""WeatherTool — calls Open-Meteo API via httpx (FR-2, SC-6)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.config.settings import get_settings

# Open-Meteo geocoding + forecast endpoints (no API key required for Open-Meteo itself;
# WEATHER_API_KEY guards access at the application layer per SC-6).
_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://open-meteo.com/v1/forecast"

_WMO_CONDITIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm",
}


class ToolExecutionError(Exception):
    """Raised when the weather API returns a non-2xx response."""


class WeatherTool:
    """Fetches current weather for a location using Open-Meteo.

    Requires ``WEATHER_API_KEY`` to be set in settings as an application-level
    guard (SC-6).  Missing or empty key returns a config-error dict immediately.
    """

    name = "weather"
    description = "Returns current temperature, condition, and humidity for a location."
    input_schema: dict[str, Any] = {"location": {"type": "string"}}
    is_sensitive = False

    async def execute(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Fetch weather for *tool_input['location']*.

        Returns:
            dict with keys: temperature, condition, humidity, location.

        Raises:
            ToolExecutionError: If the upstream API returns a non-2xx status.
        """
        settings = get_settings()
        if not settings.WEATHER_API_KEY:
            return {"error": "Weather API not configured"}

        location: str = tool_input.get("location", "")
        return await asyncio.to_thread(self._fetch_sync, location)

    def _fetch_sync(self, location: str) -> dict[str, Any]:
        """Blocking HTTP calls — wrapped in asyncio.to_thread by execute()."""
        geo = httpx.get(_GEO_URL, params={"name": location, "count": 1}, timeout=5)
        if geo.status_code != 200 or not geo.json().get("results"):
            raise ToolExecutionError(f"Geocoding failed for '{location}': {geo.text}")

        result = geo.json()["results"][0]
        lat, lon = result["latitude"], result["longitude"]

        forecast = httpx.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weathercode,relative_humidity_2m",
            },
            timeout=5,
        )
        if forecast.status_code != 200:
            raise ToolExecutionError(f"Weather API error {forecast.status_code}: {forecast.text}")

        current = forecast.json()["current"]
        code = current.get("weathercode", 0)
        return {
            "temperature": current["temperature_2m"],
            "condition": _WMO_CONDITIONS.get(code, f"Weather code {code}"),
            "humidity": current["relative_humidity_2m"],
            "location": location,
        }
