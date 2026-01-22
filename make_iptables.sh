#!/bin/bash

# =================配置区域=================
SERVICE_NAME="iptables-chaos"
TARGET_PORT="9988"  # 选择一个测试端口
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
# ==========================================

echo "=== 1. 正在部署轻量级监听服务 (Alpine + NC) ==="

# 1. 创建 Systemd 服务
# 使用 nc -lk -p 9999 持续监听端口
# 这里的 Description 故意写得很正常，模拟业务服务
cat > ${UNIT_FILE} <<EOF
[Unit]
Description=Internal Data Sync Service (TCP)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5s
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}

# 启动命令
# 1. 映射宿主机的 TARGET_PORT 到容器内的 9999
# 2. 容器内使用 nc 监听 9999
ExecStart=/usr/bin/docker run --rm --name ${SERVICE_NAME} \\
    -p ${TARGET_PORT}:9999 \\
    alpine:latest nc -lk -p 9999

ExecStop=/usr/bin/docker stop ${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

# 2. 启动服务
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo "等待服务启动..."
sleep 10

# 3. 检查端口是否正常开启
if netstat -tlnp | grep -q "${TARGET_PORT}"; then
    echo "✅ 服务启动成功，端口 ${TARGET_PORT} 正在监听。"
else
    echo "❌ 服务启动失败，请检查 Docker。"
    exit 1
fi

echo ""
echo "=== 2. 🔥 正在注入 iptables 故障 (DROP 规则) ==="

# 核心故障：在 INPUT 链第一行插入丢弃规则
# 这会拦截所有发往该端口的 TCP 包
iptables -I INPUT -p tcp --dport ${TARGET_PORT} -j DROP

echo "规则已应用：禁止访问 TCP 端口 ${TARGET_PORT}"

echo ""
echo "=== 3. 验证故障效果 ==="
echo "正在尝试连接 localhost:${TARGET_PORT} (预期应超时)..."

# 使用 curl 测试，设置 3 秒超时
curl -v --connect-timeout 3 http://localhost:${TARGET_PORT} 2>&1 | grep "timed out"

if [ $? -eq 0 ]; then
    echo "✅ 故障复现成功！连接已超时 (Connection timed out)。"
else
    echo "⚠️  注意：虽然没有显示'timed out'字样，但如果 curl 卡住了也是成功的。"
fi

echo ""
echo "========================================================"
echo "🎯 演练场景说明："
echo "   1. 现象：服务 Systemd 状态是 Active (Running)。"
echo "   2. 现象：'netstat -tlnp' 显示端口 ${TARGET_PORT} 正在监听。"
echo "   3. 故障：但业务端反馈无法连接（Telnet 不通 / Curl 超时）。"
echo ""
echo "🕵️‍♂️  排查提示："
echo "   学员需要运行 'iptables -L -n --line-numbers' 才能发现 DROP 规则。"
echo ""
echo "🧹 演练结束后恢复命令："
echo "   iptables -D INPUT -p tcp --dport ${TARGET_PORT} -j DROP"
echo "========================================================"
