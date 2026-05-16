import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.b64encode(salt + digest).decode("utf-8")


def derive_client_password_hash(password: str, username: str) -> str:
    """Derive the frontend-compatible password hash sent to auth endpoints."""
    salt = hashlib.sha256(username.lower().encode("utf-8")).digest()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        10000,
    )
    return digest.hex()


def verify_password(password: str, encoded: str) -> bool:
    data = base64.b64decode(encoded.encode("utf-8"))
    salt = data[:16]
    stored = data[16:]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return hmac.compare_digest(stored, digest)


# 预生成的 dummy hash，用于防止时序攻击
# 当用户不存在时，仍然执行 PBKDF2 计算以保持响应时间一致
_DUMMY_HASH = hash_password("dummy-password-for-timing-attack-prevention")


def verify_password_constant_time(password: str, encoded: str | None) -> bool:
    """常量时间密码验证，防止时序攻击。

    当 encoded 为 None（用户不存在）时，仍执行 PBKDF2 计算。
    """
    if encoded is None:
        # 用户不存在，执行 dummy 验证以保持时间一致
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, encoded)


# ANSI 转义序列正则（匹配 ESC[ 开头的控制序列）
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b[^[]")

# 控制字符（除了 \t 和 \n，但包括 \r 以防止覆盖攻击）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f]")


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
    s = _ANSI_ESCAPE_RE.sub("", s)
    # 再移除其他控制字符
    s = _CONTROL_CHARS_RE.sub("", s)
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
        return urlunparse(
            (
                parsed.scheme,
                masked_netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )
    except (ValueError, AttributeError) as e:
        # 解析失败时返回原 URL（可能是 magnet 等特殊协议）
        logger.debug(f"Failed to parse URL for sanitization: {e}")
        return url


def is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否为私有/内网地址"""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


async def check_url_ssrf(url: str) -> str | None:
    """检查 URL 是否存在 SSRF 风险。

    Args:
        url: 要检查的 URL

    Returns:
        如果安全返回 None，否则返回错误信息
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname

        if scheme not in ("http", "https", "ftp"):
            return None

        if not hostname:
            return "无效的下载链接"

        blocked_hosts = {
            "localhost",
            "localhost.localdomain",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "::",
        }
        if hostname.lower() in blocked_hosts:
            return "不允许下载本机地址"

        # 检查是否为 IP 地址
        try:
            ip = ipaddress.ip_address(hostname)
            if is_private_ip(ip):
                return "不允许下载内网地址"
            return None
        except ValueError:
            pass

        # 域名解析检查
        try:
            loop = asyncio.get_running_loop()
            addr_infos = await loop.run_in_executor(
                None, socket.getaddrinfo, hostname, None
            )
            for addr_info in addr_infos:
                ip_str = addr_info[4][0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if is_private_ip(ip):
                        return f"域名 {hostname} 解析到内网地址，禁止下载"
                except ValueError:
                    continue
        except socket.gaierror:
            return f"域名 {hostname} 无法解析"

    except Exception as exc:
        logger.warning("SSRF 校验异常 url=%s error=%s", mask_url_credentials(url), exc)

    return None
