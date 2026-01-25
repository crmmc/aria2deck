"""日志注入防护和 URL 凭证脱敏测试

测试场景：
1. 控制字符被正确过滤
2. ANSI 转义序列被正确移除
3. URL 凭证被正确脱敏
"""
import pytest

from app.core.security import sanitize_string, mask_url_credentials


class TestSanitizeString:
    """控制字符清理测试"""

    def test_removes_ansi_escape_sequences(self):
        """测试移除 ANSI 转义序列"""
        # 红色文字
        assert sanitize_string("\x1b[31mRED\x1b[0m") == "RED"
        # 清屏
        assert sanitize_string("\x1b[2J\x1b[HHello") == "Hello"
        # 光标移动
        assert sanitize_string("\x1b[10;20HText") == "Text"

    def test_removes_control_characters(self):
        """测试移除控制字符"""
        # NULL 字符
        assert sanitize_string("Hello\x00World") == "HelloWorld"
        # 退格
        assert sanitize_string("Hello\x08World") == "HelloWorld"
        # 响铃
        assert sanitize_string("Hello\x07World") == "HelloWorld"

    def test_preserves_normal_whitespace(self):
        """测试保留正常空白字符"""
        # 空格、换行、制表符应该保留
        assert sanitize_string("Hello World") == "Hello World"
        assert sanitize_string("Hello\nWorld") == "Hello\nWorld"
        assert sanitize_string("Hello\tWorld") == "Hello\tWorld"

    def test_handles_none(self):
        """测试处理 None"""
        assert sanitize_string(None) is None

    def test_handles_empty_string(self):
        """测试处理空字符串"""
        assert sanitize_string("") == ""

    def test_complex_injection_attempt(self):
        """测试复杂注入尝试"""
        # 模拟攻击：伪造日志条目
        malicious = "Normal log\x1b[2K\r[CRITICAL] Fake critical error\n"
        result = sanitize_string(malicious)
        assert "[CRITICAL]" in result  # 文本保留
        assert "\x1b" not in result  # 转义序列移除
        assert "\r" not in result  # 回车移除


class TestMaskUrlCredentials:
    """URL 凭证脱敏测试"""

    def test_masks_username_password(self):
        """测试脱敏用户名和密码"""
        url = "http://user:password@example.com/file.zip"
        masked = mask_url_credentials(url)
        assert masked == "http://***:***@example.com/file.zip"

    def test_masks_username_only(self):
        """测试只有用户名"""
        url = "http://user@example.com/file.zip"
        masked = mask_url_credentials(url)
        assert masked == "http://***@example.com/file.zip"

    def test_preserves_url_without_credentials(self):
        """测试无凭证的 URL 保持不变"""
        url = "http://example.com/file.zip"
        masked = mask_url_credentials(url)
        assert masked == url

    def test_preserves_port(self):
        """测试保留端口号"""
        url = "http://user:pass@example.com:8080/file.zip"
        masked = mask_url_credentials(url)
        assert masked == "http://***:***@example.com:8080/file.zip"

    def test_preserves_query_and_fragment(self):
        """测试保留查询参数和锚点"""
        url = "http://user:pass@example.com/file.zip?token=abc#section"
        masked = mask_url_credentials(url)
        assert "?token=abc#section" in masked
        assert "user" not in masked
        assert "pass" not in masked

    def test_handles_https(self):
        """测试 HTTPS 协议"""
        url = "https://user:pass@example.com/file.zip"
        masked = mask_url_credentials(url)
        assert masked.startswith("https://")
        assert "***:***@" in masked

    def test_handles_ftp(self):
        """测试 FTP 协议"""
        url = "ftp://user:pass@ftp.example.com/file.zip"
        masked = mask_url_credentials(url)
        assert masked.startswith("ftp://")
        assert "***:***@" in masked

    def test_handles_magnet_link(self):
        """测试磁力链接不受影响"""
        url = "magnet:?xt=urn:btih:abc123"
        masked = mask_url_credentials(url)
        assert masked == url

    def test_handles_empty_string(self):
        """测试空字符串"""
        assert mask_url_credentials("") == ""

    def test_handles_special_characters_in_password(self):
        """测试密码中的特殊字符"""
        url = "http://user:p%40ss%3Aword@example.com/file.zip"
        masked = mask_url_credentials(url)
        assert "***:***@" in masked
        assert "p%40ss" not in masked
