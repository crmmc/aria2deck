import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlparse, urlunparse

from app.core.config import get_credential_pepper

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_URI_LENGTH = 8192
MAX_DOWNLOAD_URI_COUNT = 16
MAX_EMBEDDED_NETWORK_URI_COUNT = 64
HTTP_URI_SCHEMES = frozenset({"http", "https"})
WEBSEED_URI_SCHEMES = frozenset({"http", "https", "ftp"})
TRACKER_URI_SCHEMES = frozenset({"http", "https", "udp"})
DOWNLOAD_URI_SCHEMES = WEBSEED_URI_SCHEMES | {"magnet"}


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


CREDENTIAL_DIGEST_DOMAINS = frozenset({"api-token", "rpc-secret"})


def _credential_digest(kind: str, secret: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"),
        f"aria2deck:{kind}:v1\0{secret}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def credential_digest(kind: str, secret: str) -> str:
    """Return the domain-separated HMAC digest for a credential."""
    if kind not in CREDENTIAL_DIGEST_DOMAINS:
        raise ValueError("不支持的凭证类型")
    return _credential_digest(kind, secret, get_credential_pepper())


def credential_prefix(secret: str) -> str:
    return secret[:16]


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


def redact_url_for_log(url: str) -> str:
    """返回不含凭证、查询参数或片段的 URL 日志视图。"""
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return "<redacted-url>"
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return "<redacted-url>"

    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path
    decoded_path = unquote(path)
    if _SENSITIVE_URL_PATH_RE.search(decoded_path) or any(
        len(segment) >= 64 for segment in decoded_path.split("/")
    ):
        path = "/<redacted>"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


_SENSITIVE_URL_PATH_RE = re.compile(
    r"token|signature|secret|password|passwd|credential|api[-_]?key|auth|session",
    re.IGNORECASE,
)


def is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否不适合作为公网下载目标。"""
    return not ip.is_global


def _has_invalid_uri_chars(value: str) -> bool:
    return any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
    ) or re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None


async def _check_magnet_network_params(query: str) -> str | None:
    try:
        params = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=MAX_EMBEDDED_NETWORK_URI_COUNT,
        )
    except ValueError:
        return "无效的磁力链接参数"

    for raw_key, value in params:
        key = raw_key.lower()
        if key not in {"tr", "ws", "xs", "as"}:
            continue
        if not value or len(value) > MAX_DOWNLOAD_URI_LENGTH:
            return f"磁力链接参数 {key} 无效"

        parsed_value = urlparse(value)
        if key in {"xs", "as"} and parsed_value.scheme.lower() == "urn":
            if parsed_value.netloc or not parsed_value.path or _has_invalid_uri_chars(value):
                return f"磁力链接参数 {key} 无效"
            continue

        allowed_schemes = (
            TRACKER_URI_SCHEMES if key == "tr" else WEBSEED_URI_SCHEMES
        )
        error = await check_url_ssrf(value, allowed_schemes=allowed_schemes)
        if error:
            return f"磁力链接参数 {key} 不安全: {error}"
    return None


async def check_url_ssrf(
    url: str,
    *,
    allowed_schemes: frozenset[str] = DOWNLOAD_URI_SCHEMES,
) -> str | None:
    """检查下载 URI 的协议、凭据和 SSRF 风险。"""
    if not url:
        return "无效的下载链接"
    if len(url) > MAX_DOWNLOAD_URI_LENGTH:
        return "下载链接过长"
    if _has_invalid_uri_chars(url):
        return "下载链接包含非法字符"

    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in allowed_schemes:
            return "不支持的下载链接协议"

        if scheme == "magnet":
            if not parsed.query:
                return "无效的磁力链接"
            return await _check_magnet_network_params(parsed.query)

        hostname = parsed.hostname
        _ = parsed.port
        if not hostname:
            return "无效的下载链接"
        if parsed.username is not None or parsed.password is not None:
            return "下载链接不支持用户名或密码"

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

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip is not None:
            return "不允许下载内网地址" if is_private_ip(ip) else None

        try:
            loop = asyncio.get_running_loop()
            addr_infos = await loop.run_in_executor(
                None, socket.getaddrinfo, hostname, None
            )
        except socket.gaierror:
            return f"域名 {hostname} 无法解析"

        if not addr_infos:
            return f"域名 {hostname} 无法安全解析"
        for addr_info in addr_infos:
            try:
                resolved_ip = ipaddress.ip_address(addr_info[4][0])
            except (IndexError, TypeError, ValueError):
                return f"域名 {hostname} 无法安全解析"
            if is_private_ip(resolved_ip):
                return f"域名 {hostname} 解析到非公网或内网地址，禁止下载"
    except (TypeError, ValueError):
        return "无效的下载链接"
    except Exception as exc:
        logger.warning(
            "SSRF 校验异常 url=%s error_type=%s",
            redact_url_for_log(url),
            type(exc).__name__,
        )
        return "下载链接安全校验失败"

    return None


async def check_torrent_network_endpoints(
    tracker_urls: Sequence[str],
    webseed_urls: Sequence[str],
) -> str | None:
    if len(tracker_urls) + len(webseed_urls) > MAX_EMBEDDED_NETWORK_URI_COUNT:
        return "种子文件包含过多网络地址"

    for label, urls, schemes in (
        ("tracker", tracker_urls, TRACKER_URI_SCHEMES),
        ("webseed", webseed_urls, WEBSEED_URI_SCHEMES),
    ):
        for index, url in enumerate(urls):
            error = await check_url_ssrf(url, allowed_schemes=schemes)
            if error:
                return f"种子文件 {label}[{index}] 不安全: {error}"
    return None


async def check_bt_tracker_option(
    options: Mapping[str, object] | None,
) -> str | None:
    if not options or "bt-tracker" not in options:
        return None
    value = options["bt-tracker"]
    if not isinstance(value, str):
        return "bt-tracker 必须是字符串"

    trackers = value.split(",")
    if not trackers or len(trackers) > MAX_DOWNLOAD_URI_COUNT:
        return "bt-tracker 数量无效"
    for index, tracker in enumerate(trackers):
        error = await check_url_ssrf(
            tracker,
            allowed_schemes=TRACKER_URI_SCHEMES,
        )
        if error:
            return f"bt-tracker[{index}] 不安全: {error}"
    return None
