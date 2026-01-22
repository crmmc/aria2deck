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
NC='\033[0m' # No Color

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
mkdir -p backend/downloads
mkdir -p backend/aria2

# 创建空的 session 文件（如果不存在）
touch backend/aria2/aria2.session

# 显示配置信息
echo -e "${YELLOW}配置信息:${NC}"
echo "  配置文件: backend/aria2/aria2.conf"
echo "  下载目录: backend/downloads"
echo "  日志文件: backend/aria2/aria2.log"
echo "  会话文件: backend/aria2/aria2.session"
echo "  RPC 端口: 6800"
echo "  RPC 地址: http://localhost:6800/jsonrpc"
echo ""

echo -e "${GREEN}启动 aria2 服务...${NC}"
echo -e "${YELLOW}提示: 按 Ctrl+C 停止服务${NC}"
echo ""

# 启动 aria2（前台模式）
exec aria2c --conf-path=backend/aria2/aria2.conf
