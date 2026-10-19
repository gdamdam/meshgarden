"""FORECAST: cached short weather forecasts (WP5).

Disabled (the default) constructs nothing. Enabled, the factory builds a
`ForecastService` (cache + unstarted fetcher thread) attached as the module's
`service`, which the daemon starts and stops. The handler reads the cache
only and never touches the network.
"""

from __future__ import annotations

from meshgarden.commands import Command
from meshgarden.forecast import ForecastService, render_forecast
from meshgarden.modules import CommandModule, ModuleRequest


def factory(cfg) -> list[CommandModule]:
    fc = cfg.forecast
    if not fc.enabled:
        return []
    service = ForecastService(fc)

    def handler(req: ModuleRequest) -> str:
        return render_forecast(
            service.cache.get(), now=req.now, stale_after=fc.stale_after_seconds,
            max_bytes=req.max_bytes,
        )

    return [
        CommandModule(
            name="FORECAST", command=Command.FORECAST, handler=handler,
            usage="FORECAST\nShort NWS forecast for the garden, from a cache with its age",
            menu="FORECAST short forecast", channel_ok=True, service=service,
        )
    ]
