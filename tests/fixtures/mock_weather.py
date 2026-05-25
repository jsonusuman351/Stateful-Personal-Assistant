"""Configurable mock for WeatherTool._fetch_sync (T-047)."""

from __future__ import annotations

from typing import Any


class MockWeather:
    """Drop-in replacement for ``WeatherTool._fetch_sync``.

    Usage with pytest's monkeypatch::

        mock = MockWeather()
        monkeypatch.setattr(
            "src.tools.weather.WeatherTool._fetch_sync",
            mock.as_method(),
        )

    Attributes:
        data:        The weather payload returned (minus ``"location"`` which is
                     injected from the ``location`` argument at call time).
        call_count:  Number of fetch calls received.
        last_location: The most recently requested location string.
    """

    def __init__(self) -> None:
        """Initialise with default sunny weather data."""
        self.data: dict[str, Any] = {
            "temperature": 22.5,
            "condition": "Sunny",
            "humidity": 60,
        }
        self.call_count: int = 0
        self.last_location: str = ""

    def set_weather(
        self,
        temperature: float,
        condition: str,
        humidity: int,
    ) -> None:
        """Configure the weather payload returned for all subsequent calls.

        Args:
            temperature: Temperature value (numeric; no unit suffix).
            condition:   Human-readable condition string, e.g. ``"Cloudy"``.
            humidity:    Relative humidity as an integer percentage.
        """
        self.data = {
            "temperature": temperature,
            "condition": condition,
            "humidity": humidity,
        }

    def reset(self) -> None:
        """Reset to default sunny weather, zero call count, empty last_location."""
        self.data = {"temperature": 22.5, "condition": "Sunny", "humidity": 60}
        self.call_count = 0
        self.last_location = ""

    def as_method(self) -> object:
        """Return a callable that replaces the ``_fetch_sync`` instance method.

        The original signature is ``(self, location: str) -> dict``
        where ``self`` is the ``WeatherTool`` instance.  This replacement
        ignores ``self`` and injects ``"location"`` into the returned dict.
        """
        mock = self

        def _mock_fetch_sync(_tool_self: object, location: str) -> dict[str, Any]:
            mock.call_count += 1
            mock.last_location = location
            return {**mock.data, "location": location}

        return _mock_fetch_sync
