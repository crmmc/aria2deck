from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aria2deck.api.errors import install_error_handlers
from aria2deck.domain.errors import DomainError, ErrorCode


def schema_ref(operation: dict[str, object], status: str) -> str:
    responses = operation["responses"]
    response = responses[status]
    content = response["content"]
    json_content = content["application/json"]
    schema = json_content["schema"]
    return schema["$ref"]


def test_openapi_declares_real_cookie_bearer_and_public_boundaries(app: FastAPI) -> None:
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "aria2deck_v2_session",
    }
    assert schemes["ApiToken"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "API Token",
    }

    paths = schema["paths"]
    assert paths["/api/v2/auth/me"]["get"]["security"] == [
        {"ApiToken": []},
        {"SessionCookie": []},
    ]
    assert paths["/api/v2/users"]["get"]["security"] == [{"SessionCookie": []}]
    assert paths["/api/v2/users"]["post"]["security"] == [{}, {"SessionCookie": []}]
    assert paths["/api/v2/settings"]["get"]["security"] == [{"SessionCookie": []}]
    assert paths["/api/v2/credentials/rpc"]["get"]["security"] == [{"SessionCookie": []}]
    for path, method in (
        ("/api/v2/auth/login", "post"),
        ("/api/v2/public/site-info", "get"),
        ("/api/health", "get"),
        ("/api/health/live", "get"),
        ("/api/health/ready", "get"),
    ):
        assert "security" not in paths[path][method]


def test_openapi_success_and_error_models_match_actual_payloads(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]

    login = paths["/api/v2/auth/login"]["post"]
    assert set(login["responses"]) == {"200", "401", "422", "429", "500"}
    assert schema_ref(login, "200") == "#/components/schemas/UserResponse"
    assert schema_ref(login, "401") == "#/components/schemas/ErrorResponse"
    assert schema_ref(login, "422") == "#/components/schemas/ErrorResponse"

    public = paths["/api/v2/public/site-info"]["get"]
    assert set(public["responses"]) == {"200", "429", "500"}
    assert schema_ref(public, "200") == "#/components/schemas/PublicSiteInfoResponse"
    assert schema_ref(
        paths["/api/v2/users"]["get"], "200"
    ) == "#/components/schemas/UserListResponse"
    assert schema_ref(
        paths["/api/health/ready"]["get"], "503"
    ) == "#/components/schemas/ReadyFailureResponse"

    for path, operations in paths.items():
        for operation in operations.values():
            for status, response in operation["responses"].items():
                if status.isdigit() and int(status) >= 400 and "content" in response:
                    expected = (
                        "ReadyFailureResponse"
                        if path == "/api/health/ready" and status == "503"
                        else "ErrorResponse"
                    )
                    assert schema_ref(operation, status).endswith(expected)


def test_validation_and_unknown_api_use_the_same_chinese_structure(app: FastAPI) -> None:
    with TestClient(app) as client:
        invalid = client.post("/api/v2/auth/login", json={})
        wrong_method = client.get("/api/v2/auth/login")

    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {"code": "invalid_input", "message": "请求参数无效"}
    }
    assert wrong_method.status_code == 404
    assert wrong_method.json() == {
        "error": {"code": "not_found", "message": "接口不存在"}
    }


def test_framework_method_not_allowed_is_chinese_and_structured() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/test")
    def accept_post() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/test")
    assert response.status_code == 405
    assert response.json() == {
        "error": {"code": "http_error", "message": "请求方法不允许"}
    }


@pytest.mark.parametrize(
    "code,expected_status",
    [
        (ErrorCode.INVALID_INPUT, 422),
        (ErrorCode.INVALID_STATE, 409),
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.UNAUTHENTICATED, 401),
        (ErrorCode.PERMISSION_DENIED, 403),
        (ErrorCode.DUPLICATE_RESOURCE, 409),
        (ErrorCode.QUOTA_EXCEEDED, 409),
        (ErrorCode.PHYSICAL_SPACE_EXCEEDED, 409),
        (ErrorCode.CONTENT_BUSY, 409),
        (ErrorCode.CONTENT_PENDING_DELETE, 409),
        (ErrorCode.SHARE_EXPIRED, 410),
        (ErrorCode.SHARE_REVOKED, 410),
        (ErrorCode.SHARE_EXHAUSTED, 410),
        (ErrorCode.SHARE_UNAVAILABLE, 410),
        (ErrorCode.SHARE_LIMIT_EXCEEDED, 409),
        (ErrorCode.MISSING_CREDENTIALS, 401),
        (ErrorCode.INVALID_CREDENTIALS, 403),
        (ErrorCode.RANGE_NOT_SATISFIABLE, 416),
        (ErrorCode.STALE_EVENT, 409),
        (ErrorCode.SUBMISSION_UNCERTAIN, 502),
        (ErrorCode.TASK_TOO_LARGE, 413),
        (ErrorCode.DOWNLOAD_UNAVAILABLE, 410),
        (ErrorCode.UPSTREAM_FAILURE, 502),
        (ErrorCode.RECOVERY_UNSAFE, 503),
        (ErrorCode.LAST_ADMIN, 409),
        (ErrorCode.SELF_DELETE, 409),
        (ErrorCode.CREDENTIAL_INVALID, 401),
        (ErrorCode.CONFIGURATION_INVALID, 500),
        (ErrorCode.SCHEMA_MISMATCH, 500),
        (ErrorCode.RATE_LIMITED, 429),
    ],
)
def test_every_domain_error_has_an_explicit_http_mapping(
    code: ErrorCode, expected_status: int
) -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/test")
    def fail() -> None:
        raise DomainError(code, "中文错误")

    with TestClient(app) as client:
        response = client.get("/test")
    assert response.status_code == expected_status
    assert response.json() == {"error": {"code": code.value, "message": "中文错误"}}


def test_non_string_http_details_and_internal_exceptions_do_not_leak() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/http")
    def http_failure() -> None:
        raise HTTPException(status_code=400, detail={"secret": "value"}, headers={"X-Test": "yes"})

    @app.get("/internal")
    def internal_failure() -> None:
        raise RuntimeError("sensitive internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        http_response = client.get("/http")
        internal_response = client.get("/internal")

    assert http_response.status_code == 400
    assert http_response.headers["X-Test"] == "yes"
    assert http_response.json() == {"error": {"code": "http_error", "message": "请求无效"}}
    assert internal_response.status_code == 500
    assert internal_response.json() == {
        "error": {"code": "internal_error", "message": "服务器内部错误"}
    }
    assert "sensitive" not in internal_response.text
