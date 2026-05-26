#!/bin/bash

# aria2 启动脚本 - 前台运行模式
# 用于本地开发测试

set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 切换到项目根目录
cd "$PROJECT_ROOT"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 本地开发用的 Hook Secret（与 make run 保持一致）
DEV_HOOK_SECRET="dev_hook_secret_local_12345"
ARIA2_CONF="backend/aria2/aria2.conf"

read_conf_value() {
    local key="$1"
    local default_value="$2"
    local value

    value="$(awk -F= -v key="$key" '
        /^[[:space:]]*(#|$)/ { next }
        {
            left = $1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", left)
            if (left == key) {
                sub(/^[^=]*=/, "")
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
                print $0
                exit
            }
        }
    ' "$ARIA2_CONF")"

    if [ -n "$value" ]; then
        printf '%s' "$value"
    else
        printf '%s' "$default_value"
    fi
}

DOWNLOAD_DIR="$(read_conf_value "dir" "backend/downloads")"
LOG_FILE="$(read_conf_value "log" "backend/aria2/aria2.log")"
SESSION_FILE="$(read_conf_value "input-file" "backend/aria2/aria2.session")"
RPC_PORT="$(read_conf_value "rpc-listen-port" "6800")"

echo -e "${GREEN}=== aria2 本地测试服务 ===${NC}"
echo ""

# 检查 aria2c 是否安装
if ! command -v aria2c &> /dev/null; then
    echo -e "${RED}错误: aria2c 未安装${NC}"
    echo ""
    echo "请先安装 aria2:"
    echo "  macOS:   brew install aria2"
    echo "  Ubuntu:  sudo apt-get install aria2"
    echo "  CentOS:  sudo yum install aria2"
    exit 1
fi

# 显示 aria2 版本
echo -e "${YELLOW}aria2 版本:${NC}"
aria2c --version | head -n 1
echo ""

# 创建必要的目录
mkdir -p "$DOWNLOAD_DIR"
mkdir -p backend/aria2

# 创建空的 session 文件（如果不存在）
touch "$SESSION_FILE"

# 显示配置信息
echo -e "${YELLOW}配置信息:${NC}"
echo "  配置文件: $ARIA2_CONF"
echo "  下载目录: $DOWNLOAD_DIR"
echo "  日志文件: $LOG_FILE"
echo "  会话文件: $SESSION_FILE"
echo "  RPC 端口: $RPC_PORT"
echo "  RPC 地址: http://localhost:$RPC_PORT/jsonrpc"
echo ""
echo -e "${CYAN}Hook Secret: ${DEV_HOOK_SECRET}${NC}"
echo -e "${CYAN}提示: 启动后端时请使用 'make run' 以自动配置 Hook Secret${NC}"
echo ""

echo -e "${GREEN}启动 aria2 服务...${NC}"
echo -e "${YELLOW}提示: 按 Ctrl+C 停止服务${NC}"
echo ""

# 启动 aria2（前台模式）
exec aria2c --conf-path="$ARIA2_CONF"
