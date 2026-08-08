"""Backend adapter: translates Task Core intents to a download backend."""

from app.modules.backend import aria2_adapter, port

__all__ = ["aria2_adapter", "port"]
