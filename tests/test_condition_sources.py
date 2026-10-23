"""Provider icons, exact XML fallbacks, solar locations, and source provenance."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from custom_components.vedur_is.api import (
    ForecastPoint,
    InvalidResponse,
    Observation,
    Station,
    StationForecast,
    VedurIsApiClient,
    parse_forecasts_xml,
    parse_gottvedur_forecast_points,
)
from custom_components.vedur_is.forecast_utils import (
    daily_forecast_dicts,
    hourly_forecast_dicts,
    twice_daily_forecast_dicts,
)
from custom_components.vedur_is.weather_utils import (
    GOTTVEDUR_ICON_CONDITIONS,
    _solar_events,
    condition_from_forecast_point,
    condition_from_forecast_text,
    condition_from_icon,
    condition_from_observation,
    current_forecast_point,
    current_station_forecast_point,
    forecast_condition_attributes,
)
from tests.test_api import FakeSession

STATION = Station(1470, "Reykjavik", None, "sj", 64.12884, -21.90822, 60, "IMO", None)
DAY = datetime(2026, 10, 23)


class TestProviderConditions(unittest.TestCase):
    def test_all_35_renderer_icons_are_intentionally_mapped(self):
        expected = {
            "sunny": "ClearSky",
            "clear-night": "ClearSkyNight",
            "partlycloudy": "PartlyCloudy PartlyCloudyNight",
            "cloudy": "Cloudy CloudyNight Overcast",
            "rainy": "LightRain Rain LightRainShowers LightRainShowersNight RainShowers RainShowersNight LightDrizzle Drizzle FreezingRain",
            "snowy-rainy": "LightSleet Sleet SleetShowers SleetShowersNight",
            "snowy": "LightSnow Snow SnowShowers SnowShowersNight BlowingSnow",
            "windy": "Storm",
            "exceptional": "DustDevil DustStorm",
            "fog": "Fog FogDrizzle FogMist",
            "hail": "Hail HailNight",
            "lightning": "LightThunder Thunder",
        }
        self.assertEqual(len(GOTTVEDUR_ICON_CONDITIONS), 35)
        for condition, icons in expected.items():
            for icon in icons.split():
                with self.subTest(icon=icon):
                    self.assertEqual(condition_from_icon(icon), condition)

    def test_unknowns_are_not_guessed_or_reported_exceptional(self):
        for text in (None, "", "Rain and unidentified phenomenon", "fairly unclear"):
            self.assertIsNone(condition_from_forecast_text(text, DAY, station=STATION))
        self.assertIsNone(condition_from_icon("NewStormVariant"))

    def test_documented_xml_conditions_in_both_languages(self):
        descriptions = {
            "sunny": ("Clear sky", "Heiðskírt"),
            "partlycloudy": ("Partly cloudy", "Léttskýjað"),
            "cloudy": ("Cloudy", "Skýjað", "Overcast", "Alskýjað"),
            "rainy": (
                "Light rain",
                "Lítils háttar rigning",
                "Rain",
                "Rigning",
                "Rain showers",
                "Skúrir",
                "Light drizzle",
                "Lítils háttar súld",
                "Drizzle",
                "Súld",
                "Freezing rain",
                "Frostrigning",
            ),
            "snowy-rainy": (
                "Light sleet",
                "Lítils háttar slydda",
                "Sleet",
                "Slydda",
                "Sleet showers",
                "Slydduél",
            ),
            "snowy": (
                "Light snow",
                "Lítils háttar snjókoma",
                "Snow",
                "Snjókoma",
                "Snow showers",
                "Snjóél",
                "Blowing snow",
                "Skafrenningur",
            ),
            "exceptional": ("Dust devil", "Skýstrókar", "Dust storm", "Moldrok"),
            "fog": ("Fog", "Þoka"),
            "hail": ("Hail", "Hagl"),
            "lightning": (
                "Light thunder",
                "Lítils háttar þrumuveður",
                "Thunder",
                "Þrumuveður",
            ),
        }
        self.assertEqual(sum(map(len, descriptions.values())), 46)
        for condition, texts in descriptions.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(
                        condition_from_forecast_text(
                            text,
                            DAY.replace(hour=12),
                            station=STATION,
                        ),
                        condition,
                    )

    def test_provider_night_takes_precedence_over_xml_and_solar(self):
        point = ForecastPoint(
            DAY.replace(hour=18), 1, 2, 88, "Clear sky", "ClearSkyNight", "gottvedur_is"
        )
        self.assertEqual(condition_from_forecast_point(point, STATION), "clear-night")
        self.assertEqual(
            condition_from_forecast_point(point, STATION, at_time=DAY.replace(hour=12)),
            "clear-night",
        )
        attrs = forecast_condition_attributes(point, STATION)
        self.assertEqual(attrs["condition_source"], "gottvedur_forecast")
        self.assertEqual(attrs["condition_raw"], "ClearSkyNight")
        self.assertEqual(attrs["condition_time"], "2026-10-23T18:00:00+00:00")

    def test_xml_clear_sky_has_no_fixed_clock_hours(self):
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", DAY.replace(hour=18), station=STATION
            ),
            "clear-night",
        )
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", datetime(2026, 6, 21, 18), station=STATION
            ),
            "sunny",
        )
        self.assertIsNone(condition_from_forecast_text("Clear sky", DAY))
        self.assertIsNone(
            condition_from_forecast_text(
                "Clear sky", DAY, station=replace(STATION, latitude=None)
            )
        )

    def test_station_location_not_home_and_timezone_offsets(self):
        west = replace(STATION, latitude=0, longitude=-100)
        east = replace(STATION, latitude=0, longitude=40)
        when = DAY.replace(hour=12)
        self.assertEqual(
            condition_from_forecast_text("Clear sky", when, station=west), "clear-night"
        )
        self.assertEqual(
            condition_from_forecast_text("Clear sky", when, station=east), "sunny"
        )
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky",
                when.replace(hour=14, tzinfo=timezone(timedelta(hours=2))),
                station=east,
            ),
            "sunny",
        )

    def test_sunset_and_polar_day_night(self):
        _, setting = _solar_events(STATION.latitude, STATION.longitude, DAY.date())
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", setting - timedelta(seconds=1), station=STATION
            ),
            "sunny",
        )
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", setting + timedelta(seconds=1), station=STATION
            ),
            "clear-night",
        )
        polar = replace(STATION, latitude=80)
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", datetime(2026, 6, 21), station=polar
            ),
            "sunny",
        )
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky", datetime(2026, 12, 21, 12), station=polar
            ),
            "clear-night",
        )

    def test_unknown_icon_can_use_same_time_xml(self):
        point = ForecastPoint(DAY.replace(hour=18), 1, 2, 88, "Clear sky", "NewIcon")
        self.assertEqual(condition_from_forecast_point(point, STATION), "clear-night")
        self.assertEqual(
            forecast_condition_attributes(point, STATION)["condition_source"],
            "xml_forecast",
        )
        self.assertIsNone(
            condition_from_forecast_point(replace(point, weather_text=None), STATION)
        )

    def test_derived_and_hourly_forecasts_use_provider_conditions(self):
        points = tuple(
            ForecastPoint(
                DAY + timedelta(hours=h), 1, 2, 88, None, "Hail", "gottvedur_is"
            )
            for h in (12, 18)
        )
        for helper in (
            hourly_forecast_dicts,
            daily_forecast_dicts,
            twice_daily_forecast_dicts,
        ):
            for row in helper(points, now=DAY, station=STATION):
                self.assertEqual(row["condition"], "hail")

    def test_expired_forecasts_do_not_supply_current_conditions(self):
        point = ForecastPoint(DAY, 1, 2, 88, "Rain")
        self.assertIsNone(current_forecast_point([point], DAY + timedelta(days=2)))
        future = replace(point, time=DAY + timedelta(hours=5), icon="ClearSky")
        forecast = StationForecast(
            1470, "Station", None, None, (future,), xml_forecasts=(point,)
        )
        self.assertEqual(current_station_forecast_point(forecast, DAY), point)

    def test_xml_error_is_not_an_empty_success(self):
        for payload in (
            "<forecasts><error>Unavailable</error></forecasts>",
            "<html />",
        ):
            with self.assertRaises(InvalidResponse):
                parse_forecasts_xml(payload)

    def test_xml_calm_has_zero_speed_but_no_invented_compass_direction(self):
        for bearing in ("Calm", "Logn"):
            payload = f'<forecasts><station id="1470"><forecast><ftime>2026-10-23 18:00:00</ftime><F>0</F><D>{bearing}</D><W>Clear sky</W></forecast></station></forecasts>'
            point = parse_forecasts_xml(payload)[1470].forecasts[0]
            self.assertEqual(point.wind_speed, 0)
            self.assertIsNone(point.wind_bearing)

    def test_stale_observation_icon_is_not_mistaken_for_current_weather(self):
        observation = Observation.from_api(
            {
                "station": 1470,
                "time": datetime.now(timezone.utc).isoformat(),
                "icon": "Rain",
            }
        )
        stale = replace(
            observation,
            value_times={"icon": datetime.now(timezone.utc) - timedelta(hours=1)},
        )
        self.assertEqual(condition_from_observation(observation), "rainy")
        self.assertIsNone(condition_from_observation(stale))


class TestGottvedurForecastSource(unittest.TestCase):
    def test_one_cached_response_supplies_conditions_numbers_and_amounts(self):
        row = {
            "time": "2026-10-23T18:00:00",
            "model": "ig",
            "icon": "ClearSkyNight",
            "temperature": 1,
            "windSpeed": 2,
            "windDirection": 88,
            "precipitation": 0.3,
        }
        session = FakeSession({"nextDays": [row]})
        client = VedurIsApiClient(session)
        xml = StationForecast(
            1470,
            "Station",
            DAY,
            None,
            (ForecastPoint(DAY.replace(hour=18), 99, 99, "S", "Clear sky"),),
        )
        first = asyncio.run(
            client.async_enrich_forecast_precipitation({1470: xml}, [1470])
        )
        second = asyncio.run(client.async_enrich_forecast_precipitation(first, [1470]))
        self.assertEqual(len(session.calls), 1)
        point = second[1470].forecasts[0]
        self.assertEqual(
            (point.temperature, point.wind_speed, point.wind_bearing), (1, 2, 88)
        )
        self.assertEqual(point.icon, "ClearSkyNight")
        self.assertEqual(point.weather_text, "Clear sky")
        self.assertEqual(point.model, "ig")
        self.assertIsNone(second[1470].generated_at)
        self.assertEqual(second[1470].precipitation[0].amount, 0.3)
        client._gottvedur_forecast_cache[1470] = (
            datetime.now(timezone.utc) - timedelta(hours=2),
            (),
        )
        session.status = 503
        fallback = asyncio.run(
            client.async_enrich_forecast_precipitation(second, [1470])
        )
        self.assertEqual(fallback[1470].forecasts, xml.forecasts)
        self.assertEqual(
            condition_from_forecast_point(fallback[1470].forecasts[0], STATION),
            "clear-night",
        )

    def test_no_cross_time_xml_guess_and_invalid_numbers_stay_unknown(self):
        row = {
            "time": "2026-10-23T20:00:00+02:00",
            "icon": None,
            "temperature": True,
            "windSpeed": float("inf"),
            "windDirection": None,
        }
        point = parse_gottvedur_forecast_points({"nextDays": [row]})[0]
        self.assertEqual(point.time, DAY.replace(hour=18))
        self.assertIsNone(point.temperature)
        self.assertIsNone(point.wind_speed)
        self.assertIsNone(condition_from_forecast_point(point, STATION))
        self.assertEqual(
            parse_gottvedur_forecast_points(
                {"nextDays": [{"time": DAY.isoformat(), "precipitation": 2}]}
            ),
            (),
        )

    def test_observation_condition_timestamp_is_not_forecast_time(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        observation = Observation.from_api(
            {"station": 1470, "time": now.isoformat(), "icon": "Rain"}
        )
        attrs = forecast_condition_attributes(
            None, None, observation=observation, observation_station=STATION
        )
        self.assertEqual(attrs["condition_raw"], "Rain")
        self.assertEqual(attrs["condition_time"], now.isoformat())
        self.assertEqual(attrs["condition_station_id"], 1470)
