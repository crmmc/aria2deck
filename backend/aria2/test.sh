#!/bin/bash

# aria2 RPC 测试脚本

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

RPC_URL="http://localhost:6800/jsonrpc"

echo -e "${GREEN}=== aria2 RPC 测试 ===${NC}"
echo ""

# 测试 1: 获取版本信息
echo -e "${YELLOW}测试 1: 获取 aria2 版本${NC}"
response=$(curl -s "$RPC_URL" -d '{
  "jsonrpc":"2.0",
  "method":"aria2.getVersion",
  "id":"test1"
}')

if echo "$response" | grep -q "version"; then
    echo -e "${GREEN}✓ 成功${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ 失败${NC}"
    echo "$response"
    exit 1
fi

echo ""

# 测试 2: 获取全局状态
echo -e "${YELLOW}测试 2: 获取全局状态${NC}"
response=$(curl -s "$RPC_URL" -d '{
  "jsonrpc":"2.0",
  "method":"aria2.getGlobalStat",
  "id":"test2"
}')

if echo "$response" | grep -q "downloadSpeed"; then
    echo -e "${GREEN}✓ 成功${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ 失败${NC}"
    echo "$response"
    exit 1
fi

echo ""

# 测试 3: 获取全局配置
echo -e "${YELLOW}测试 3: 获取全局配置${NC}"
response=$(curl -s "$RPC_URL" -d '{
  "jsonrpc":"2.0",
  "method":"aria2.getGlobalOption",
  "id":"test3"
}')

if echo "$response" | grep -q "max-concurrent-downloads"; then
    echo -e "${GREEN}✓ 成功${NC}"
    echo "部分配置:"
    echo "$response" | python3 -c "import sys, json; data=json.load(sys.stdin); print(json.dumps({k:v for k,v in list(data['result'].items())[:5]}, indent=2))"
else
    echo -e "${RED}✗ 失败${NC}"
    echo "$response"
    exit 1
fi

echo ""
echo -e "${GREEN}所有测试通过！aria2 RPC 服务正常运行。${NC}"
