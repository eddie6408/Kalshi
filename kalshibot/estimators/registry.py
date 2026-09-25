"""Which estimators exist, and how to build one from settings.

This lived in app.py, which also wired up the original standalone Engine.
Anything that wanted an estimator - the arbitrage engine, the weather audit -
had to import the whole of that wiring to get at this dictionary, which is
what kept a retired engine alive by accident.
"""
from __future__ import annotations

from .weather import WeatherTemperatureEstimator
from ..sources import nws_user_agent
from ..weatherdata import WeatherData

ESTIMATORS = {
    "weather_temperature": lambda s: WeatherTemperatureEstimator(
        s.section("weather"),
        WeatherData(
            ensemble_models=s.get("weather.ensemble_models"),
            cache_seconds=int(s.get("loop.forecast_cache_seconds", 1800)),
            cross_check=bool(s.get("sources.cross_check_forecasts", True)),
            user_agent=nws_user_agent(s.section("sources")),
        ),
    ),
}
