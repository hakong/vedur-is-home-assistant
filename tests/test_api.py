"""Tests for the vedur.is API client."""

from __future__ import annotations

import asyncio
from typing import Any
import unittest

import aiohttp

from custom_components.vedur_is.api import (
    BASE_URL,
    CannotConnect,
    GOTTVEDUR_BASE_URL,
    InvalidResponse,
    Observation,
    XML_FORECAST_URL,
    merge_observation_fallback,
    parse_forecasts_xml,
    parse_gottvedur_observation_payload,
    VedurIsApiClient,
)


class FakeResponse:
    """Minimal aiohttp response test double."""

    def __init__(self, status: int, payload: Any) -> None:
        """Initialize the fake response."""
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the async context manager."""

    async def json(self, content_type: str | None = None) -> Any:
        """Return the configured JSON payload."""
        return self._payload

    async def text(self) -> str:
        """Return the configured text payload."""
        return str(self._payload)


class FakeSession:
    """Minimal aiohttp session test double."""

    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        error: Exception | None = None,
    ) -> None:
        """Initialize the fake session."""
        self.payload = payload
        self.status = status
        self.error = error
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    def get(
        self,
        url: str,
        *,
        params: list[tuple[str, str]],
        timeout: aiohttp.ClientTimeout,
    ) -> FakeResponse:
        """Return a fake response."""
        if self.error:
            raise self.error
        self.calls.append((url, params))
        return FakeResponse(self.status, self.payload)


class RouteSession:
    """Minimal aiohttp session test double with URL-specific responses."""

    def __init__(self, routes: dict[str, Any]) -> None:
        """Initialize the fake session."""
        self.routes = routes
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    def get(
        self,
        url: str,
        *,
        params: list[tuple[str, str]],
        timeout: aiohttp.ClientTimeout,
    ) -> FakeResponse:
        """Return a fake response for the URL."""
        self.calls.append((url, params))
        payload = self.routes[url]
        return FakeResponse(200, payload)


class TestVedurIsApiClient(unittest.TestCase):
    """Tests for the API client."""

    def test_get_stations_parses_and_sorts_station_metadata(self) -> None:
        """The station endpoint is parsed into stable station objects."""
        session = FakeSession(
            [
                {
                    "station": 3470,
                    "name": "Akureyri",
                    "abbr": "akeyr",
                    "type": "sj",
                    "lat": 65.68,
                    "lon": -18.1,
                    "ele": 24,
                    "owner": "IMO",
                    "start": 2004,
                },
                {
                    "station": 1470,
                    "name": "Reykjavik",
                    "abbr": "reith",
                    "type": "sj",
                    "lat": 64.12,
                    "lon": -21.9,
                    "ele": 60.2,
                    "owner": "IMO",
                    "start": 2021,
                },
            ]
        )
        client = VedurIsApiClient(session)  # type: ignore[arg-type]

        stations = asyncio.run(client.async_get_stations("rey"))

        self.assertEqual([station.station_id for station in stations], [3470, 1470])
        self.assertEqual(stations[1].name, "Reykjavik")
        self.assertEqual(
            session.calls,
            [
                (
                    f"{BASE_URL}/stations",
                    [("active", "true"), ("station_type", "sj"), ("keyword", "rey")],
                )
            ],
        )

    def test_get_stations_can_fetch_all_active_station_types(self) -> None:
        """Station type can be omitted for weather station discovery."""
        session = FakeSession([])
        client = VedurIsApiClient(session)  # type: ignore[arg-type]

        asyncio.run(client.async_get_stations(station_type=None))

        self.assertEqual(
            session.calls,
            [(f"{BASE_URL}/stations", [("active", "true")])],
        )

    def test_get_latest_observations_uses_repeated_station_id_parameters(self) -> None:
        """Multiple selected stations are sent using repeated station_id params."""
        session = FakeSession(
            [
                {
                    "station": 1470,
                    "name": "Reykjavik",
                    "time": "2026-05-04T12:00:00",
                    "t": 3.9,
                    "rh": 42,
                    "f": 4.5,
                    "fg": None,
                    "d": 18,
                    "p": 1020.6,
                    "r": None,
                },
                {
                    "station": 3470,
                    "name": "Akureyri",
                    "time": "2026-05-04T12:00:00",
                    "t": 1.1,
                    "rh": 55,
                },
            ]
        )
        client = VedurIsApiClient(session)  # type: ignore[arg-type]

        observations = asyncio.run(client.async_get_latest_observations([1470, 3470]))

        self.assertEqual(observations[1470].value("t"), 3.9)
        self.assertIsNone(observations[1470].value("fg"))
        self.assertIsNotNone(observations[1470].time)
        self.assertEqual(
            session.calls,
            [
                (
                    f"{BASE_URL}/observations/aws/hour/latest",
                    [
                        ("parameters", "basic"),
                        ("station_id", "1470"),
                        ("station_id", "3470"),
                    ],
                )
            ],
        )

    def test_get_latest_observations_fills_missing_values_from_gottvedur(
        self,
    ) -> None:
        """Missing official values are filled from gottvedur.is page data."""
        session = RouteSession(
            {
                f"{BASE_URL}/observations/aws/hour/latest": [
                    {
                        "station": 1474,
                        "name": "Garðabær Urriðaholt",
                        "time": "2026-05-04T22:00:00",
                        "t": 1.4,
                        "rh": None,
                        "fg": "?",
                        "r": None,
                    }
                ],
                f"{GOTTVEDUR_BASE_URL}/en/vedur/athuganir/1/": (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    '{"buildId":"test-build"}</script>'
                ),
                (
                    f"{GOTTVEDUR_BASE_URL}/_next/data/test-build/en/vedur/"
                    "athuganir/1474.json"
                ): {
                    "pageProps": {
                        "latestObservation": {
                            "station": 1474,
                            "observationTime": "2026-05-04T22:00:00",
                            "temperature": 9.9,
                            "humidity": 59,
                            "maxWindGust": 4.7,
                            "precipitation": 0,
                        },
                        "stationObservation": {
                            "stationName": "Garðabær Urriðaholt",
                        },
                    },
                },
            }
        )
        client = VedurIsApiClient(session)  # type: ignore[arg-type]

        observations = asyncio.run(
            client.async_get_latest_observations(
                [1474],
                fallback_station_ids=[1474],
            )
        )

        self.assertEqual(observations[1474].value("t"), 1.4)
        self.assertEqual(observations[1474].value("rh"), 59)
        self.assertEqual(observations[1474].value("fg"), 4.7)
        self.assertEqual(observations[1474].value("r"), 0)

    def test_parse_gottvedur_observation_payload(self) -> None:
        """Gottvedur page data is mapped to official observation keys."""
        observation = parse_gottvedur_observation_payload(
            {
                "pageProps": {
                    "latestObservation": {
                        "station": 1474,
                        "observationTime": "2026-05-04T22:00:00",
                        "temperature": 1.3,
                        "humidity": 59,
                        "dewPoint": -5.8,
                        "windSpeed": 1.9,
                        "maxWindGust": 4.7,
                        "windDirection": 47,
                        "pressure": 1025.9,
                        "precipitation": 0,
                    },
                    "stationObservation": {
                        "stationName": "Garðabær Urriðaholt",
                    },
                },
            }
        )

        self.assertEqual(observation.station_id, 1474)
        self.assertEqual(observation.name, "Garðabær Urriðaholt")
        self.assertEqual(observation.value("t"), 1.3)
        self.assertEqual(observation.value("rh"), 59)
        self.assertEqual(observation.value("td"), -5.8)
        self.assertEqual(observation.value("f"), 1.9)
        self.assertEqual(observation.value("fg"), 4.7)
        self.assertEqual(observation.value("d"), 47)
        self.assertEqual(observation.value("p"), 1025.9)
        self.assertEqual(observation.value("r"), 0)
        self.assertIsNotNone(observation.time)

    def test_merge_observation_fallback_only_fills_unavailable_values(self) -> None:
        """Fallback data fills null values without replacing official readings."""
        primary = Observation.from_api(
            {
                "station": 1474,
                "name": "Official",
                "time": "2026-05-04T22:00:00",
                "t": 1.0,
                "rh": None,
                "fg": "?",
                "r": None,
            }
        )
        fallback = Observation.from_api(
            {
                "station": 1474,
                "name": "Fallback",
                "time": "2026-05-04T23:00:00",
                "t": 9.0,
                "rh": 59,
                "fg": 4.7,
                "r": 0,
            }
        )

        merged = merge_observation_fallback(primary, fallback)

        self.assertEqual(merged.value("t"), 1.0)
        self.assertEqual(merged.value("rh"), 59)
        self.assertEqual(merged.value("fg"), 4.7)
        self.assertEqual(merged.value("r"), 0)
        self.assertEqual(merged.name, "Official")
        self.assertEqual(merged.time, primary.time)

    def test_invalid_http_status_raises_invalid_response(self) -> None:
        """HTTP errors are converted into integration API errors."""
        client = VedurIsApiClient(FakeSession({}, status=500))  # type: ignore[arg-type]

        with self.assertRaises(InvalidResponse):
            asyncio.run(client.async_get_stations())

    def test_aiohttp_error_raises_cannot_connect(self) -> None:
        """Network errors are converted into connection errors."""
        client = VedurIsApiClient(
            FakeSession({}, error=aiohttp.ClientConnectionError("boom"))
        )  # type: ignore[arg-type]

        with self.assertRaises(CannotConnect):
            asyncio.run(client.async_get_stations())

    def test_parse_forecasts_xml(self) -> None:
        """XML forecasts are parsed into station forecast objects."""
        forecasts = parse_forecasts_xml(
            """<?xml version="1.0" encoding="UTF-8"?>
            <forecasts>
              <station id="31475" valid="1">
                <name>Garðabær - Kauptún</name>
                <atime>2026-05-04 12:00:00</atime>
                <link>https://example.invalid/forecast</link>
                <forecast>
                  <ftime>2026-05-04 13:00:00</ftime>
                  <F>4</F>
                  <D>N</D>
                  <T>5</T>
                  <W>Clear sky</W>
                </forecast>
                <forecast>
                  <ftime>2026-05-04 14:00:00</ftime>
                  <F></F>
                  <D>NNW</D>
                  <T></T>
                  <W>Partly cloudy</W>
                </forecast>
              </station>
            </forecasts>"""
        )

        station_forecast = forecasts[31475]
        self.assertEqual(station_forecast.name, "Garðabær - Kauptún")
        self.assertIsNotNone(station_forecast.generated_at)
        self.assertEqual(station_forecast.generated_at.year, 2026)
        self.assertEqual(station_forecast.forecasts[0].temperature, 5.0)
        self.assertEqual(station_forecast.forecasts[0].wind_speed, 4.0)
        self.assertEqual(station_forecast.forecasts[0].wind_bearing, "N")
        self.assertEqual(station_forecast.forecasts[0].weather_text, "Clear sky")
        self.assertIsNone(station_forecast.forecasts[1].temperature)
        self.assertIsNone(station_forecast.forecasts[1].wind_speed)

    def test_get_forecasts_uses_xml_service(self) -> None:
        """Forecast requests use the XML weather service with semicolon ids."""
        session = FakeSession(
            """<forecasts>
                 <station id="31475">
                   <name>Garðabær - Kauptún</name>
                   <forecast>
                     <ftime>2026-05-04 13:00:00</ftime>
                     <F>4</F><D>N</D><T>5</T><W>Clear sky</W>
                   </forecast>
                 </station>
               </forecasts>"""
        )
        client = VedurIsApiClient(session)  # type: ignore[arg-type]

        forecasts = asyncio.run(client.async_get_forecasts([31475, 3471]))

        self.assertIn(31475, forecasts)
        self.assertEqual(
            session.calls,
            [
                (
                    XML_FORECAST_URL,
                    [
                        ("op_w", "xml"),
                        ("type", "forec"),
                        ("lang", "en"),
                        ("view", "xml"),
                        ("ids", "31475;3471"),
                    ],
                )
            ],
        )

    def test_invalid_forecast_xml_raises_invalid_response(self) -> None:
        """Malformed XML is treated as an invalid API response."""
        with self.assertRaises(InvalidResponse):
            parse_forecasts_xml("<forecasts>")
