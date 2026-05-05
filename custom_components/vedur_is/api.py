"""Client for the public vedur.is weather API."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Any
import xml.etree.ElementTree as ET

import aiohttp

BASE_URL = "https://api.vedur.is/weather"
GOTTVEDUR_BASE_URL = "https://gottvedur.is"
XML_FORECAST_URL = "https://xmlweather.vedur.is/"
REQUEST_TIMEOUT_SECONDS = 10
FORECAST_ID_CHUNK_SIZE = 80
GOTTVEDUR_FALLBACK_CONCURRENCY = 8

_LOGGER = logging.getLogger(__name__)

_GOTTVEDUR_OBSERVATION_FIELD_MAP = {
    "temperature": "t",
    "humidity": "rh",
    "dewPoint": "td",
    "windSpeed": "f",
    "maxWindSpeed": "fx",
    "maxWindGust": "fg",
    "windDirection": "d",
    "pressure": "p",
    "precipitation": "r",
    "visibility": "v",
    "cloudCover": "n",
}


class VedurIsApiError(Exception):
    """Base exception for vedur.is API errors."""


class CannotConnect(VedurIsApiError):
    """Raised when the API cannot be reached."""


class InvalidResponse(VedurIsApiError):
    """Raised when the API response is not usable."""


@dataclass(frozen=True, slots=True)
class Station:
    """Weather station metadata."""

    station_id: int
    name: str
    abbr: str | None
    station_type: str | None
    latitude: float | None
    longitude: float | None
    elevation: float | None
    owner: str | None
    start_year: int | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Station":
        """Build station metadata from an API payload."""
        station_id = _required_int(data, "station")
        name = str(data.get("name") or station_id)

        return cls(
            station_id=station_id,
            name=name,
            abbr=_optional_str(data.get("abbr")),
            station_type=_optional_str(data.get("type")),
            latitude=_optional_float(data.get("lat")),
            longitude=_optional_float(data.get("lon")),
            elevation=_optional_float(data.get("ele")),
            owner=_optional_str(data.get("owner")),
            start_year=_optional_int(data.get("start")),
        )

    def as_storage_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "station": self.station_id,
            "name": self.name,
            "abbr": self.abbr,
            "type": self.station_type,
            "lat": self.latitude,
            "lon": self.longitude,
            "ele": self.elevation,
            "owner": self.owner,
            "start": self.start_year,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """Latest observation data for a station."""

    station_id: int
    name: str
    time: datetime | None
    values: Mapping[str, Any]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Observation":
        """Build an observation from an API payload."""
        return cls(
            station_id=_required_int(data, "station"),
            name=str(data.get("name") or ""),
            time=_parse_datetime(data.get("time")),
            values=dict(data),
        )

    def value(self, key: str) -> Any:
        """Return a raw observation value."""
        return self.values.get(key)


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """One XML forecast time point."""

    time: datetime
    temperature: float | None
    wind_speed: float | None
    wind_bearing: str | None
    weather_text: str | None


@dataclass(frozen=True, slots=True)
class StationForecast:
    """XML forecast data for one station."""

    station_id: int
    name: str
    generated_at: datetime | None
    link: str | None
    forecasts: tuple[ForecastPoint, ...]


class VedurIsApiClient:
    """Small async API client for vedur.is."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client."""
        self._session = session
        self._gottvedur_build_id: str | None = None

    async def async_get_stations(
        self,
        keyword: str | None = None,
        *,
        station_type: str | None = "sj",
    ) -> list[Station]:
        """Return active automatic weather stations."""
        params: list[tuple[str, str]] = [
            ("active", "true"),
        ]
        if station_type:
            params.append(("station_type", station_type))
        if keyword:
            params.append(("keyword", keyword))

        payload = await self._async_get_json("/stations", params)
        if not isinstance(payload, list):
            raise InvalidResponse("Expected station list")

        stations: list[Station] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            stations.append(Station.from_api(item))

        return sorted(stations, key=lambda station: station.name.casefold())

    async def async_get_latest_observations(
        self,
        station_ids: Iterable[int],
        *,
        fallback_station_ids: Iterable[int] | None = None,
    ) -> dict[int, Observation]:
        """Return latest hourly observations for the requested stations."""
        requested_station_ids = list(
            dict.fromkeys(int(station_id) for station_id in station_ids)
        )
        params: list[tuple[str, str]] = [("parameters", "basic")]
        params.extend(
            ("station_id", str(station_id)) for station_id in requested_station_ids
        )

        payload = await self._async_get_json("/observations/aws/hour/latest", params)
        if not isinstance(payload, list):
            raise InvalidResponse("Expected observation list")

        observations: dict[int, Observation] = {}
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            observation = Observation.from_api(item)
            observations[observation.station_id] = observation

        if fallback_station_ids is not None:
            await self._async_merge_gottvedur_fallbacks(
                observations,
                fallback_station_ids,
            )

        return observations

    async def async_get_gottvedur_latest_observations(
        self,
        station_ids: Iterable[int],
    ) -> dict[int, Observation]:
        """Return latest observations from the new gottvedur.is page data."""
        requested_station_ids = list(
            dict.fromkeys(int(station_id) for station_id in station_ids)
        )
        if not requested_station_ids:
            return {}

        semaphore = asyncio.Semaphore(GOTTVEDUR_FALLBACK_CONCURRENCY)

        async def fetch_one(station_id: int) -> Observation | None:
            async with semaphore:
                return await self._async_get_gottvedur_latest_observation(station_id)

        results = await asyncio.gather(
            *(fetch_one(station_id) for station_id in requested_station_ids),
            return_exceptions=True,
        )

        observations: dict[int, Observation] = {}
        for station_id, result in zip(requested_station_ids, results, strict=True):
            if isinstance(result, Exception):
                _LOGGER.debug(
                    "Could not fetch gottvedur.is fallback for station %s: %s",
                    station_id,
                    result,
                )
                continue
            if result is not None:
                observations[result.station_id] = result

        return observations

    async def async_get_forecasts(
        self,
        station_ids: Iterable[int],
    ) -> dict[int, StationForecast]:
        """Return XML forecasts for the requested stations."""
        forecasts: dict[int, StationForecast] = {}
        requested_ids = list(
            dict.fromkeys(int(station_id) for station_id in station_ids)
        )

        for start in range(0, len(requested_ids), FORECAST_ID_CHUNK_SIZE):
            chunk = requested_ids[start : start + FORECAST_ID_CHUNK_SIZE]
            params = [
                ("op_w", "xml"),
                ("type", "forec"),
                ("lang", "en"),
                ("view", "xml"),
                ("ids", ";".join(str(station_id) for station_id in chunk)),
            ]
            payload = await self._async_get_text(XML_FORECAST_URL, params)
            forecasts.update(parse_forecasts_xml(payload))

        return forecasts

    async def _async_get_json(
        self, path: str, params: list[tuple[str, str]]
    ) -> Any:
        """Fetch JSON from the API."""
        url = f"{BASE_URL}{path}"
        return await self._async_get_json_url(url, params)

    async def _async_get_json_url(
        self,
        url: str,
        params: list[tuple[str, str]],
    ) -> Any:
        """Fetch JSON from a URL."""

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.get(
                url, params=params, timeout=timeout
            ) as response:
                if response.status >= 400:
                    raise InvalidResponse(
                        f"vedur.is returned HTTP {response.status}"
                    )
                return await response.json(content_type=None)
        except TimeoutError as err:
            raise CannotConnect("Timed out connecting to vedur.is") from err
        except aiohttp.ClientError as err:
            raise CannotConnect("Failed to connect to vedur.is") from err

    async def _async_merge_gottvedur_fallbacks(
        self,
        observations: dict[int, Observation],
        station_ids: Iterable[int],
    ) -> None:
        """Fill missing official observation values from gottvedur.is."""
        fallback_ids = list(
            dict.fromkeys(int(station_id) for station_id in station_ids)
        )
        if not fallback_ids:
            return

        fallback_observations = await self.async_get_gottvedur_latest_observations(
            fallback_ids
        )
        for station_id, fallback_observation in fallback_observations.items():
            primary_observation = observations.get(station_id)
            if primary_observation is None:
                observations[station_id] = fallback_observation
                continue

            observations[station_id] = merge_observation_fallback(
                primary_observation,
                fallback_observation,
            )

    async def _async_get_gottvedur_latest_observation(
        self,
        station_id: int,
    ) -> Observation:
        """Fetch one station's latest observation from gottvedur.is."""
        build_id = await self._async_get_gottvedur_build_id()
        try:
            return await self._async_get_gottvedur_latest_observation_with_build_id(
                station_id,
                build_id,
            )
        except InvalidResponse:
            self._gottvedur_build_id = None
            return await self._async_get_gottvedur_latest_observation_with_build_id(
                station_id,
                await self._async_get_gottvedur_build_id(),
            )

    async def _async_get_gottvedur_latest_observation_with_build_id(
        self,
        station_id: int,
        build_id: str,
    ) -> Observation:
        """Fetch one station's latest observation using a known Next.js build id."""
        payload = await self._async_get_json_url(
            f"{GOTTVEDUR_BASE_URL}/_next/data/{build_id}/en/vedur/athuganir/"
            f"{station_id}.json",
            [("stationId", str(station_id))],
        )
        return parse_gottvedur_observation_payload(payload)

    async def _async_get_gottvedur_build_id(self) -> str:
        """Return the current gottvedur.is Next.js build id."""
        if self._gottvedur_build_id:
            return self._gottvedur_build_id

        payload = await self._async_get_text(
            f"{GOTTVEDUR_BASE_URL}/en/vedur/athuganir/1/",
            [],
        )
        match = re.search(r'"buildId":"([^"]+)"', payload)
        if match is None:
            raise InvalidResponse("Could not find gottvedur.is build id")

        self._gottvedur_build_id = match.group(1)
        return self._gottvedur_build_id

    async def _async_get_text(
        self,
        url: str,
        params: list[tuple[str, str]],
    ) -> str:
        """Fetch text from a URL."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.get(
                url, params=params, timeout=timeout
            ) as response:
                if response.status >= 400:
                    raise InvalidResponse(
                        f"vedur.is returned HTTP {response.status}"
                    )
                return await response.text()
        except TimeoutError as err:
            raise CannotConnect("Timed out connecting to vedur.is") from err
        except aiohttp.ClientError as err:
            raise CannotConnect("Failed to connect to vedur.is") from err


def parse_forecasts_xml(payload: str) -> dict[int, StationForecast]:
    """Parse vedur XML forecast payloads."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as err:
        raise InvalidResponse("Invalid XML forecast response") from err

    forecasts: dict[int, StationForecast] = {}
    for station_element in root.findall("station"):
        station_id_text = station_element.get("id")
        if station_id_text is None:
            continue

        station_id = _optional_int(station_id_text)
        if station_id is None:
            continue

        forecast_points: list[ForecastPoint] = []
        for forecast_element in station_element.findall("forecast"):
            ftime = _text(forecast_element, "ftime")
            forecast_time = _parse_datetime(ftime)
            if forecast_time is None:
                continue

            forecast_points.append(
                ForecastPoint(
                    time=forecast_time,
                    temperature=_optional_float(_text(forecast_element, "T")),
                    wind_speed=_optional_float(_text(forecast_element, "F")),
                    wind_bearing=_optional_str(_text(forecast_element, "D")),
                    weather_text=_optional_str(_text(forecast_element, "W")),
                )
            )

        if not forecast_points:
            continue

        forecasts[station_id] = StationForecast(
            station_id=station_id,
            name=_text(station_element, "name") or str(station_id),
            generated_at=_parse_datetime(_text(station_element, "atime")),
            link=_optional_str(_text(station_element, "link")),
            forecasts=tuple(forecast_points),
        )

    return forecasts


def parse_gottvedur_observation_payload(payload: Mapping[str, Any]) -> Observation:
    """Parse latest observation data from gottvedur.is Next.js page data."""
    page_props = payload.get("pageProps")
    if not isinstance(page_props, Mapping):
        raise InvalidResponse("Missing gottvedur.is page props")

    latest = page_props.get("latestObservation")
    if not isinstance(latest, Mapping):
        raise InvalidResponse("Missing gottvedur.is latest observation")

    station_observation = page_props.get("stationObservation")
    if not isinstance(station_observation, Mapping):
        station_observation = {}

    station_id = _optional_int(latest.get("station")) or _optional_int(
        station_observation.get("stationId")
    )
    if station_id is None:
        raise InvalidResponse("Missing gottvedur.is station id")

    name = str(
        station_observation.get("stationName")
        or station_observation.get("stationDisplayName")
        or latest.get("stationName")
        or station_id
    )
    values: dict[str, Any] = {
        "station": station_id,
        "name": name,
        "time": latest.get("observationTime"),
    }
    for gottvedur_key, vedur_key in _GOTTVEDUR_OBSERVATION_FIELD_MAP.items():
        if gottvedur_key in latest:
            values[vedur_key] = latest.get(gottvedur_key)

    return Observation.from_api(values)


def merge_observation_fallback(
    primary: Observation,
    fallback: Observation,
) -> Observation:
    """Return an observation with unavailable primary values filled from fallback."""
    values = dict(primary.values)
    for key, fallback_value in fallback.values.items():
        if key in {"station", "name", "time"}:
            continue
        if _is_unavailable(values.get(key)) and not _is_unavailable(fallback_value):
            values[key] = fallback_value

    name = primary.name or fallback.name
    observation_time = primary.time or fallback.time
    if _is_unavailable(values.get("name")):
        values["name"] = name
    if _is_unavailable(values.get("time")) and fallback.time is not None:
        values["time"] = fallback.time.isoformat()

    return Observation(
        station_id=primary.station_id,
        name=name,
        time=observation_time,
        values=values,
    )


def _is_unavailable(value: Any) -> bool:
    return value is None or value == "" or value == "?"


def _required_int(data: Mapping[str, Any], key: str) -> int:
    value = _optional_int(data.get(key))
    if value is None:
        raise InvalidResponse(f"Missing integer field: {key}")
    return value


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise InvalidResponse(f"Invalid integer value: {value!r}") from err


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise InvalidResponse(f"Invalid float value: {value!r}") from err


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise InvalidResponse(f"Invalid datetime value: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as err:
        raise InvalidResponse(f"Invalid datetime value: {value!r}") from err


def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None
