import base64
import hashlib
import hmac
import os
import re
from urllib.parse import urlparse, urlunparse


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.b64encode(salt + digest).decode("utf-8")


def verify_password(password: str, encoded: str) -> bool:
    data = base64.b64decode(encoded.encode("utf-8"))
    salt = data[:16]
    stored = data[16:]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return hmac.compare_digest(stored, digest)


# ANSI 转义序列正则（匹配 ESC[ 开头的控制序列）
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b[^[]')

# 控制字符（除了 \t 和 \n，但包括 \r 以防止覆盖攻击）
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f]')


def sanitize_string(s: str | None) -> str | None:
    """清理字符串中的控制字符和 ANSI 转义序列

    用于防止日志注入攻击。

    Args:
        s: 待清理的字符串

    Returns:
        清理后的字符串，控制字符被替换为空
    """
    if s is None:
        return None
    # 先移除 ANSI 转义序列
    s = _ANSI_ESCAPE_RE.sub('', s)
    # 再移除其他控制字符
    s = _CONTROL_CHARS_RE.sub('', s)
    return s


def mask_url_credentials(url: str) -> str:
    """脱敏 URL 中的用户名和密码

    将 http://user:password@host/path 转换为 http://***:***@host/path

    Args:
        url: 原始 URL

    Returns:
        脱敏后的 URL
    """
    if not url:
        return url

    try:
        parsed = urlparse(url)

        # 如果没有用户名密码，直接返回
        if not parsed.username and not parsed.password:
            return url

        # 构建脱敏后的 netloc
        masked_netloc = ""
        if parsed.username:
            masked_netloc += "***"
        if parsed.password:
            masked_netloc += ":***"
        if parsed.username or parsed.password:
            masked_netloc += "@"

        # 添加 host 和 port
        masked_netloc += parsed.hostname or ""
        if parsed.port:
            masked_netloc += f":{parsed.port}"

        # 重新组装 URL
        return urlunparse((
            parsed.scheme,
            masked_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))
    except Exception:
        # 解析失败时返回原 URL（可能是 magnet 等特殊协议）
        return url
