"""Today's weather from Open-Meteo (free, no key). Location from config.weather (default Bengaluru)."""
import requests

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "freezing rain", 80: "rain showers", 81: "rain showers", 82: "violent showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def today(cfg) -> dict | None:
    w = cfg.get("weather") or {}
    if w.get("enabled") is False:
        return None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
            "latitude": w.get("lat", 12.97), "longitude": w.get("lon", 77.59), "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        }).json()
        d = r["daily"]
        return {
            "place": w.get("place", "Bengaluru"),
            "now_c": round(r["current"]["temperature_2m"]),
            "now": WMO.get(r["current"]["weather_code"], "—"),
            "high_c": round(d["temperature_2m_max"][0]),
            "low_c": round(d["temperature_2m_min"][0]),
            "rain_pct": d["precipitation_probability_max"][0],
            "day": WMO.get(d["weather_code"][0], "—"),
        }
    except (requests.RequestException, KeyError, IndexError, TypeError):
        return None
