"""New task architecture modules (Task Core / User Ref / Backend Adapter).

This package is the seam for the v1 three-layer refactor. It is purely
additive: no router, service, or repository is switched over yet.
"""

from app.modules import backend, task_core, user_ref

__all__ = ["backend", "task_core", "user_ref"]
