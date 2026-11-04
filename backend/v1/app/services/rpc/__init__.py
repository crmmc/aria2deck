"""aria2 RPC services package (M4).

Holds the split of the former ``aria2_rpc_handler`` monolith into
focused modules; ``_shared.py`` carries the private helpers shared
across the write / read / system handler groups.
"""

from app.services.rpc._shared import (
    SAFE_INTERNAL_ERROR_MESSAGE,
    RpcError,
    RpcErrorCode,
)
from app.services.rpc.system import Aria2RpcHandler

__all__ = [
    "SAFE_INTERNAL_ERROR_MESSAGE",
    "Aria2RpcHandler",
    "RpcError",
    "RpcErrorCode",
]
