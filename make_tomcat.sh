#!/bin/bash

# 配置变量
SERVICE_NAME="tomcat-chaos"
HOST_PORT="8089"  # 改用 8089 防止端口冲突
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== 1. 正在覆写 Systemd 配置文件: ${UNIT_FILE} ==="

# 写入配置文件
# 核心修正：
# 1. 去掉 -m 参数（解决 LXC 环境报错）
# 2. JAVA_OPTS=-Xmx32m 直接写死，不要引号，不要空格（解决 Systemd 解析报错）
cat > ${UNIT_FILE} <<EOF
[Unit]
Description=Tomcat Chaos Service (OOM Generator)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5s
# 启动前强制清理旧容器
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}
# 启动命令
ExecStart=/usr/bin/docker run --rm --name ${SERVICE_NAME} \\
    -p ${HOST_PORT}:8080 \\
    -e JAVA_OPTS=-Xmx32m \\
    tomcat:9-jre11-slim
# 停止命令
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

echo "=== 2. 重载配置并重启服务 ==="
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo "=== 3. 检查服务状态 ==="
# 等待两秒让 docker 跑起来
sleep 2

if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "✅ 服务启动成功！(端口: ${HOST_PORT})"
else
    echo "❌ 服务启动失败！请检查下方日志。"
    systemctl status ${SERVICE_NAME} --no-pager
    exit 1
fi

echo ""
echo "========================================================"
echo "⚠️  注意：端口已改为 ${HOST_PORT} (避开 8080 冲突)"
echo "👇 请复制以下命令，在【另一个终端窗口】运行来制造 OOM："
echo ""
echo "   ab -n 50000 -c 100 http://localhost:${HOST_PORT}/"
echo ""
echo "========================================================"
echo "正在自动进入日志监控模式 (按 Ctrl+C 退出)..."
sleep 1

# 自动打开日志
journalctl -u ${SERVICE_NAME} -f