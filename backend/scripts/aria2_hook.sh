#!/bin/bash
# Aria2 回调钩子脚本
#
# 使用方法:
# aria2c --on-download-start=/path/to/aria2_hook.sh \
#        --on-download-pause=/path/to/aria2_hook.sh \
#        --on-download-stop=/path/to/aria2_hook.sh \
#        --on-download-complete=/path/to/aria2_hook.sh \
#        --on-download-error=/path/to/aria2_hook.sh \
#        --on-bt-download-complete=/path/to/aria2_hook.sh
#
# 参数说明:
# $1 - GID
# $2 - 文件数量
# $3 - 文件路径
#
# 环境变量:
# ARIA2_HOOK_URL    - 后端 hook 接口地址，默认 http://localhost:8000/api/hooks/aria2
# ARIA2_HOOK_SECRET - Hook 认证密钥（必须与后端 ARIA2C_HOOK_SECRET 一致）

set -e

GID="$1"
HOOK_URL="${ARIA2_HOOK_URL:-http://localhost:8000/api/hooks/aria2}"
HOOK_SECRET="${ARIA2_HOOK_SECRET:-}"

# 从脚本调用方式推断事件类型
# Aria2 会根据不同事件调用脚本，但脚本路径相同
# 我们通过检查 aria2 的状态来判断事件类型
# 或者用户可以为每个事件创建不同的符号链接

# 简单方式：从命令行参数或环境变量获取事件类型
# 如果没有指定，默认尝试通过脚本名称推断
SCRIPT_NAME=$(basename "$0")

case "$SCRIPT_NAME" in
    *start*)
        EVENT="start"
        ;;
    *pause*)
        EVENT="pause"
        ;;
    *stop*)
        EVENT="stop"
        ;;
    *complete*)
        EVENT="complete"
        ;;
    *error*)
        EVENT="error"
        ;;
    *bt*)
        EVENT="bt_complete"
        ;;
    *)
        # 默认使用环境变量或 "complete"
        EVENT="${ARIA2_EVENT:-complete}"
        ;;
esac

# 构建 curl 命令
CURL_ARGS=(-s -X POST "${HOOK_URL}" -H "Content-Type: application/json")

# 添加 Hook Secret header（如果配置了）
if [ -n "$HOOK_SECRET" ]; then
    CURL_ARGS+=(-H "X-Hook-Secret: ${HOOK_SECRET}")
fi

# 发送请求到后端
curl "${CURL_ARGS[@]}" \
    -d "{\"gid\": \"${GID}\", \"event\": \"${EVENT}\"}" \
    > /dev/null 2>&1 || true

# 返回成功，不阻塞 aria2
exit 0
