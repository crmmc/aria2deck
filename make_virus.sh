#!/bin/bash

# =================配置区域=================
SERVICE_NAME="virus-chaos"
# 选择一个不起眼的高位端口
GHOST_PORT="60000"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
# ==========================================

echo "=== 1. 正在创建 '病毒' 模拟服务: ${UNIT_FILE} ==="

# 写入 Systemd 配置文件
# 技巧：我们将 Description 写得像一个正经的系统服务，用来迷惑排查人员
cat > ${UNIT_FILE} <<EOF
[Unit]
Description=Linux Kernel Optional Communication Service
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10s
# 启动前清理
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}

# 启动命令
# 1. 使用 alpine 镜像
# 2. -p ${GHOST_PORT}:${GHOST_PORT} 映射高位端口
# 3. nc -lk -p ${GHOST_PORT} : 持续监听该端口，不输出任何日志 (静默模式)
ExecStart=/usr/bin/docker run --rm --name ${SERVICE_NAME} \\
    -p ${GHOST_PORT}:${GHOST_PORT} \\
    alpine:latest nc -lk -p ${GHOST_PORT}

# 停止命令
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

echo "=== 2. 重载配置并启动服务 ==="
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo "=== 3. 验证端口监听状态 ==="
sleep 2

# 使用 ss 或 netstat 检查端口
if ss -tlnp | grep -q "${GHOST_PORT}" || netstat -tlnp | grep -q "${GHOST_PORT}"; then
    echo "✅ 病毒服务启动成功！"
    echo "💀 正在监听高位端口: ${GHOST_PORT}"
else
    echo "❌ 启动失败，端口未监听。"
    systemctl status ${SERVICE_NAME} --no-pager
    exit 1
fi

echo ""
echo "========================================================"
echo "🕵️‍♂️  排查演练指南："
echo ""
echo "1. 现象描述：'安全扫描报告显示服务器有一个未知的高位端口在对外开放。'"
echo ""
echo "2. 学员应执行的排查命令："
echo "   netstat -tlnp | grep ${GHOST_PORT}"
echo "   或者"
echo "   ss -tlnp | grep ${GHOST_PORT}"
echo ""
echo "3. 预期发现："
echo "   会看到一个 docker-proxy 进程在监听 ${GHOST_PORT}。"
echo "   (进阶：学员需要通过 docker ps | grep ${GHOST_PORT} 找到对应的容器名为 virus-chaos)"
echo "========================================================"
