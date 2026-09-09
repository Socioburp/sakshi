"""Adapter registry.

Cached on the RESOLVED provider name, not on the argument: `get_adapter()` and
`get_adapter("meta")` must hand back the same instance, or a stateful adapter
(the mock's outbox, a live httpx client with its connection pool) quietly
becomes two.
"""

from functools import cache

from app.channels.base import ChannelAdapter
from app.config import settings


@cache
def _build(name: str) -> ChannelAdapter:
    if name == "meta":
        from app.channels.whatsapp.adapters.meta import MetaAdapter

        return MetaAdapter()
    if name == "gupshup":
        from app.channels.whatsapp.adapters.gupshup import GupshupAdapter

        return GupshupAdapter()
    if name == "twilio":
        from app.channels.whatsapp.adapters.twilio import TwilioAdapter

        return TwilioAdapter()
    from app.channels.whatsapp.adapters.mock import MockAdapter

    return MockAdapter()


def get_adapter(provider: str | None = None) -> ChannelAdapter:
    return _build(provider or settings.wa_provider)


def reset_adapter_cache() -> None:
    _build.cache_clear()
