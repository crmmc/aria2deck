"""Aria2 错误码映射

将 aria2 的错误码映射为用户友好的中文提示。
参考: https://aria2.github.io/manual/en/html/aria2c.html#exit-status
"""

# aria2 错误码到中文描述的映射
ERROR_CODE_MAP: dict[int, str] = {
    0: "下载成功",
    1: "未知错误",
    2: "网络超时",
    3: "资源未找到 (404)",
    4: "资源未找到，aria2 已重试最大次数",
    5: "下载速度过慢，已中止",
    6: "网络问题",
    7: "未完成的下载",
    8: "远程服务器不支持断点续传",
    9: "磁盘空间不足",
    10: "分片长度与控制文件不匹配",
    11: "下载任务重复",
    12: "BitTorrent 下载任务重复",
    13: "文件已存在，使用 --allow-overwrite 重试",
    14: "文件重命名失败",
    15: "无法打开已存在的文件",
    16: "无法创建新文件或截断已存在的文件",
    17: "文件 I/O 错误",
    18: "无法创建目录",
    19: "名称解析失败 (DNS 错误)",
    20: "无法解析 Metalink 文件",
    21: "FTP 命令失败",
    22: "HTTP 响应头错误",
    23: "重定向次数过多",
    24: "HTTP 认证失败",
    25: "无法解析 BEncode 格式 (种子文件损坏)",
    26: "种子文件损坏或丢失",
    27: "Magnet 链接错误",
    28: "选项错误或无法识别的选项",
    29: "服务器过载（临时错误）",
    30: "JSON-RPC 请求解析失败",
    31: "保留",
    32: "校验和验证失败",
}


def get_error_message(error_code: int | str | None, fallback: str | None = None) -> str:
    """获取错误码对应的中文描述

    Args:
        error_code: aria2 错误码（可以是 int 或 str）
        fallback: 如果找不到映射，使用的默认消息

    Returns:
        中文错误描述
    """
    if error_code is None:
        return fallback or "未知错误"

    try:
        code = int(error_code)
    except (ValueError, TypeError):
        return fallback or str(error_code)

    return ERROR_CODE_MAP.get(code, fallback or f"错误码 {code}")


def parse_error_message(aria2_error: str | None) -> str:
    """解析 aria2 错误消息，尝试提取错误码并翻译

    Args:
        aria2_error: aria2 返回的原始错误消息

    Returns:
        用户友好的中文错误描述
    """
    if not aria2_error:
        return "未知错误"

    # aria2 错误消息格式通常是 "errorCode=X errorMessage=..."
    # 或直接是错误描述
    import re

    # 尝试提取错误码
    match = re.search(r'errorCode[=:\s]*(\d+)', aria2_error, re.IGNORECASE)
    if match:
        code = int(match.group(1))
        translated = ERROR_CODE_MAP.get(code)
        if translated:
            return translated

    # 常见错误消息模式匹配
    error_patterns = [
        (r'timeout', "网络超时"),
        (r'404|not found', "资源未找到 (404)"),
        (r'403|forbidden', "访问被拒绝 (403)"),
        (r'401|unauthorized', "需要认证 (401)"),
        (r'500|internal server error', "服务器内部错误 (500)"),
        (r'502|bad gateway', "网关错误 (502)"),
        (r'503|service unavailable', "服务不可用 (503)"),
        (r'dns|name.*resolution', "DNS 解析失败"),
        (r'connection refused', "连接被拒绝"),
        (r'connection reset', "连接被重置"),
        (r'no space', "磁盘空间不足"),
        (r'permission denied', "权限不足"),
        (r'ssl|certificate', "SSL/TLS 证书错误"),
        (r'too many redirect', "重定向次数过多"),
    ]

    aria2_error_lower = aria2_error.lower()
    for pattern, message in error_patterns:
        if re.search(pattern, aria2_error_lower):
            return message

    # 无法识别，返回原始消息（截断过长的消息）
    if len(aria2_error) > 100:
        return aria2_error[:97] + "..."
    return aria2_error
