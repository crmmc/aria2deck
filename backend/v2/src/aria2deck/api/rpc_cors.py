from __future__ import annotations


def cors_headers(
    origin: str | None, *, allow_all_origins: bool, allowed_origins: frozenset[str]
) -> dict[str, str]:
    """/aria2/jsonrpc 的 CORS 响应头。

    - 白名单模式：来源精确匹配，带 credentials（Authorization/Bearer 仍可发送）。
    - 任意来源模式：`*` 且不带 credentials，仅支持 POST/OPTIONS。
    - 未配置且不匹配：返回空（OPTIONS 预检即被拒），origin 未配置时不放行。
    """
    if allow_all_origins:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }
    if origin is not None and origin in allowed_origins:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }
    return {}
