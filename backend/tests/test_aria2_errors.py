"""Aria2 错误码映射测试

测试场景：
1. 已知错误码正确翻译
2. 未知错误码返回默认格式
3. 错误消息模式匹配
"""
import pytest

from app.aria2.errors import get_error_message, parse_error_message, ERROR_CODE_MAP


class TestGetErrorMessage:
    """错误码翻译测试"""

    def test_known_error_codes(self):
        """测试已知错误码"""
        assert get_error_message(0) == "下载成功"
        assert get_error_message(3) == "资源未找到 (404)"
        assert get_error_message(9) == "磁盘空间不足"
        assert get_error_message(19) == "名称解析失败 (DNS 错误)"

    def test_string_error_code(self):
        """测试字符串格式的错误码"""
        assert get_error_message("3") == "资源未找到 (404)"
        assert get_error_message("9") == "磁盘空间不足"

    def test_unknown_error_code(self):
        """测试未知错误码"""
        assert get_error_message(999) == "错误码 999"
        assert get_error_message(100) == "错误码 100"

    def test_unknown_with_fallback(self):
        """测试带 fallback 的未知错误码"""
        assert get_error_message(999, "自定义错误") == "自定义错误"

    def test_none_error_code(self):
        """测试 None 错误码"""
        assert get_error_message(None) == "未知错误"
        assert get_error_message(None, "默认消息") == "默认消息"

    def test_invalid_error_code(self):
        """测试无效错误码"""
        assert get_error_message("not_a_number") == "not_a_number"


class TestParseErrorMessage:
    """错误消息解析测试"""

    def test_extract_error_code(self):
        """测试从消息中提取错误码"""
        assert parse_error_message("errorCode=3 File not found") == "资源未找到 (404)"
        assert parse_error_message("errorCode:9") == "磁盘空间不足"

    def test_pattern_matching(self):
        """测试模式匹配"""
        assert parse_error_message("Connection timeout") == "网络超时"
        assert parse_error_message("HTTP 404 Not Found") == "资源未找到 (404)"
        assert parse_error_message("403 Forbidden") == "访问被拒绝 (403)"
        assert parse_error_message("DNS resolution failed") == "DNS 解析失败"
        assert parse_error_message("No space left on device") == "磁盘空间不足"
        assert parse_error_message("SSL certificate error") == "SSL/TLS 证书错误"

    def test_empty_message(self):
        """测试空消息"""
        assert parse_error_message("") == "未知错误"
        assert parse_error_message(None) == "未知错误"

    def test_unrecognized_message(self):
        """测试无法识别的消息"""
        msg = "Some random error message"
        assert parse_error_message(msg) == msg

    def test_long_message_truncation(self):
        """测试长消息截断"""
        long_msg = "A" * 150
        result = parse_error_message(long_msg)
        assert len(result) == 100
        assert result.endswith("...")


class TestErrorCodeMapCoverage:
    """错误码映射覆盖测试"""

    def test_common_error_codes_exist(self):
        """测试常见错误码都有映射"""
        common_codes = [0, 1, 2, 3, 6, 9, 19, 24]
        for code in common_codes:
            assert code in ERROR_CODE_MAP, f"错误码 {code} 缺少映射"

    def test_all_codes_have_chinese_description(self):
        """测试所有映射都是中文描述"""
        for code, desc in ERROR_CODE_MAP.items():
            # 检查是否包含中文字符
            has_chinese = any('\u4e00' <= c <= '\u9fff' for c in desc)
            assert has_chinese, f"错误码 {code} 的描述 '{desc}' 不包含中文"
