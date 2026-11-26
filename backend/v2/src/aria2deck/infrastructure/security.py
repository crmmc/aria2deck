from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from ..domain.downloads import derive_planned_gid
from ..domain.errors import DomainError, ErrorCode


class CredentialManager:
    def __init__(self, *, pepper: str | None, debug: bool) -> None:
        key = pepper if pepper is not None else "aria2deck-v2-debug-only"
        self._key = key.encode("utf-8")
        if not debug and (pepper is None or len(self._key) < 32):
            raise DomainError(ErrorCode.CONFIGURATION_INVALID, "生产环境凭证密钥无效")
        encryption_key = base64.urlsafe_b64encode(
            hashlib.sha256(self._key + b":encryption").digest()
        )
        self._fernet = Fernet(encryption_key)

    def new_session_id(self) -> str:
        return secrets.token_urlsafe(32)

    def new_api_token(self) -> str:
        return "aria2_" + secrets.token_urlsafe(32)

    def new_rpc_secret(self) -> str:
        return "rpc_" + secrets.token_urlsafe(32)

    def new_temporary_password(self) -> str:
        """管理员重置用的一次性密码：16 位可输入随机串，只在成功响应里出现一次。"""
        return secrets.token_urlsafe(12)

    def digest_session(self, session_id: str) -> str:
        return self._digest(b"session", session_id)

    def digest_api_token(self, token: str) -> str:
        return self._digest(b"api-token", token)

    def _digest(self, purpose: bytes, value: str) -> str:
        return hmac.new(self._key, purpose + b":" + value.encode("utf-8"), hashlib.sha256).hexdigest()

    def derive_password_hash(self, *, username: str, password: str) -> str:
        salt = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest().encode("ascii")
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 10000).hex()

    def hash_password(self, password_hash: str) -> str:
        iterations = 120000
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password_hash.encode("ascii"), salt, iterations
        )
        return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

    def verify_password(self, password_hash: str, stored: str) -> bool:
        try:
            algorithm, raw_iterations, salt_hex, expected_hex = stored.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password_hash.encode("ascii"),
                bytes.fromhex(salt_hex),
                int(raw_iterations),
            )
        except (ValueError, UnicodeError):
            return False
        return hmac.compare_digest(actual.hex(), expected_hex)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise DomainError(ErrorCode.INVALID_STATE, "凭证数据无法读取") from None

    def derive_download_gid(self, attempt_id: int) -> str:
        return derive_planned_gid(attempt_id, pepper=self._key.decode("utf-8"))

    def sign_download_fetch(self, *, attempt_id: int, source_index: int) -> str:
        value = f"{attempt_id}:{source_index}".encode("ascii")
        digest = hmac.new(self._key, b"download-fetch:" + value, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify_download_fetch(
        self, *, attempt_id: int, source_index: int, capability: str
    ) -> bool:
        expected = self.sign_download_fetch(attempt_id=attempt_id, source_index=source_index)
        return hmac.compare_digest(expected, capability)
