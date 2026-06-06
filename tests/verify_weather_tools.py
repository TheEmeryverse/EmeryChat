import os
import sys
import tempfile
import asyncio
import importlib

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MockResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MockHttpClient:
    def __init__(self):
        self.geocoder_calls = 0

    async def get(self, url, params=None, headers=None, timeout=None):
        if "nominatim.openstreetmap.org/search" in url:
            self.geocoder_calls += 1
            query = (params or {}).get("q", "")
            if query.lower() == "houston, tx":
                return MockResponse([
                    {
                        "lat": "29.7604",
                        "lon": "-95.3698",
                        "display_name": "Houston, Harris County, Texas, United States"
                    }
                ])
            if query.lower() == "houston":
                return MockResponse([
                    {
                        "lat": "29.7604",
                        "lon": "-95.3698",
                        "display_name": "Houston, Harris County, Texas, United States"
                    }
                ])
            return MockResponse([])

        if "geocoding-api.open-meteo.com/v1/search" in url:
            self.geocoder_calls += 1
            query = ((params or {}).get("name", "") or "").lower()
            if query == "waukesha":
                return MockResponse({
                    "results": [
                        {
                            "name": "Waukesha",
                            "admin1": "Wisconsin",
                            "country": "United States",
                            "latitude": 43.01168,
                            "longitude": -88.23148,
                        }
                    ]
                })
            if query == "53186":
                return MockResponse({
                    "results": [
                        {
                            "name": "Waukesha",
                            "admin1": "Wisconsin",
                            "country": "United States",
                            "latitude": 43.01168,
                            "longitude": -88.23148,
                        }
                    ]
                })
            return MockResponse({"generationtime_ms": 0.5})

        if "api.weather.gov/points/" in url:
            return MockResponse({
                "properties": {
                    "forecast": "https://api.weather.gov/gridpoints/HGX/52,88/forecast",
                    "forecastHourly": "https://api.weather.gov/gridpoints/HGX/52,88/forecast/hourly",
                    "forecastZone": "https://api.weather.gov/zones/forecast/TXZ213"
                }
            })

        if url.endswith("/forecast"):
            return MockResponse({
                "properties": {
                    "periods": [
                        {"name": "Today", "detailedForecast": "Sunny with a high near 91."},
                        {"name": "Tonight", "detailedForecast": "Partly cloudy with a low around 76."},
                        {"name": "Wednesday", "detailedForecast": "Hot and humid with a slight chance of storms."},
                    ]
                }
            })

        if url.endswith("/forecast/hourly"):
            return MockResponse({
                "properties": {
                    "periods": [
                        {
                            "startTime": "2026-06-02T09:00:00-05:00",
                            "temperature": 87,
                            "temperatureUnit": "F",
                            "shortForecast": "Sunny",
                            "windSpeed": "6 mph",
                            "windDirection": "SE",
                        }
                    ]
                }
            })

        if "api.weather.gov/alerts/active" in url:
            return MockResponse({
                "features": [
                    {
                        "properties": {
                            "event": "Heat Advisory",
                            "headline": "Heat Advisory in effect until 8 PM CDT."
                        }
                    }
                ]
            })

        raise AssertionError(f"Unexpected URL: {url}")


def _reload_weather_modules(weather_file_path):
    os.environ["ENABLE_WEATHER"] = "true"
    os.environ["WEATHER_LOCATIONS_FILE_PATH"] = weather_file_path
    os.environ["NOAA_LAT"] = ""
    os.environ["NOAA_LONG"] = ""

    import emery.config as config
    import emery.globals as globals_module
    import emery.tools as tools
    import emery.engine as engine

    importlib.reload(config)
    importlib.reload(globals_module)
    importlib.reload(tools)
    importlib.reload(engine)
    return globals_module, tools, engine


async def test_alias_roundtrip():
    print("--- 1. Testing weather alias save and forecast lookup ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, tools, _ = _reload_weather_modules(weather_file)
        mock_client = MockHttpClient()
        globals_module.http_client = mock_client

        save_result = await tools.set_weather_location_alias("home", "Houston, TX")
        assert "Saved weather alias 'home'" in save_result, save_result

        weather_result = await tools.get_noaa_weather("home")
        assert "Weather Forecast for Houston" in weather_result, weather_result
        assert "Today: Sunny with a high near 91." in weather_result, weather_result
        assert "Active alerts:" in weather_result, weather_result
        assert mock_client.geocoder_calls == 1, f"Expected 1 geocoder call, got {mock_client.geocoder_calls}"
    print("✅ Weather alias roundtrip verified.")


async def test_zero_config_direct_lookup():
    print("--- 2. Testing zero-config direct place lookup ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, tools, _ = _reload_weather_modules(weather_file)
        mock_client = MockHttpClient()
        globals_module.http_client = mock_client

        weather_result = await tools.get_noaa_weather("Houston")
        assert "Weather Forecast for Houston" in weather_result, weather_result
        assert mock_client.geocoder_calls == 1, f"Expected direct geocoder lookup, got {mock_client.geocoder_calls}"
    print("✅ Zero-config direct lookup verified.")


async def test_missing_default_guidance():
    print("--- 3. Testing missing default-location guidance ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, tools, engine = _reload_weather_modules(weather_file)
        globals_module.http_client = MockHttpClient()

        result = await tools.get_noaa_weather()
        assert "No default weather location is set yet." in result, result

        schema_by_name = {tool["function"]["name"]: tool["function"] for tool in engine.tools_schema}
        assert "set_weather_location_alias" in schema_by_name, "Weather alias setter missing from schema"
        assert "remove_weather_location_alias" in schema_by_name, "Weather alias remover missing from schema"
        assert "list_weather_location_aliases" in schema_by_name, "Weather alias listing missing from schema"
    print("✅ Missing default guidance and weather schema verified.")


async def test_weather_prompt_includes_alias_write_guidance():
    print("--- 4. Testing weather prompt guidance for saved aliases ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, _, _ = _reload_weather_modules(weather_file)
        globals_module.http_client = MockHttpClient()

        import emery.helpers as helpers
        importlib.reload(helpers)

        prompt = await helpers.get_current_system_prompt("Set my home to Houston, TX")
        assert "You ARE able to save, update, list, and remove weather locations" in prompt, prompt
        assert "Do NOT say that you cannot set or save a weather location" in prompt, prompt
        assert "alias=`home`, location=`Austin, TX`" in prompt, prompt
    print("✅ Weather prompt alias-write guidance verified.")


async def test_alias_save_survives_geocoder_failure():
    print("--- 5. Testing alias save when geocoder is unavailable ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, tools, _ = _reload_weather_modules(weather_file)

        class FailingGeocoderClient:
            async def get(self, url, params=None, headers=None, timeout=None):
                if "nominatim.openstreetmap.org/search" in url:
                    raise RuntimeError("403 Forbidden")
                if "geocoding-api.open-meteo.com/v1/search" in url:
                    return MockResponse({"results": []})
                raise AssertionError(f"Unexpected URL: {url}")

        globals_module.http_client = FailingGeocoderClient()

        save_result = await tools.set_weather_location_alias("home", "Waukesha, WI")
        assert "Saved weather alias 'home'" in save_result, save_result
        assert "could not verify coordinates right now" in save_result, save_result

        aliases = await tools.list_weather_location_aliases()
        assert "- home: Waukesha, WI" in aliases, aliases
    print("✅ Alias save fallback verified.")


async def test_open_meteo_normalizes_city_state_and_addresses():
    print("--- 6. Testing Open-Meteo query normalization ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        weather_file = os.path.join(tmpdir, "weather_locations.json")
        globals_module, tools, _ = _reload_weather_modules(weather_file)

        class OpenMeteoOnlyClient(MockHttpClient):
            async def get(self, url, params=None, headers=None, timeout=None):
                if "nominatim.openstreetmap.org/search" in url:
                    self.geocoder_calls += 1
                    raise RuntimeError("403 Forbidden")
                return await super().get(url, params=params, headers=headers, timeout=timeout)

        globals_module.http_client = OpenMeteoOnlyClient()

        city_result, city_error = await tools._geocode_weather_location("Waukesha, WI")
        assert city_error is None, city_error
        assert city_result["label"].startswith("Waukesha"), city_result

        address_result, address_error = await tools._geocode_weather_location("401 S Grand Ave, Waukesha, WI 53186")
        assert address_error is None, address_error
        assert address_result["label"].startswith("Waukesha"), address_result
    print("✅ Open-Meteo normalization verified.")


async def main():
    await test_alias_roundtrip()
    await test_zero_config_direct_lookup()
    await test_missing_default_guidance()
    await test_weather_prompt_includes_alias_write_guidance()
    await test_alias_save_survives_geocoder_failure()
    await test_open_meteo_normalizes_city_state_and_addresses()
    print("\n🎉 Weather tools verified successfully!")


if __name__ == "__main__":
    asyncio.run(main())
