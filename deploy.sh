#!/usr/bin/env bash
# 服务器端一键部署看板为 systemd 服务（Ubuntu，非 root 用户 sudo）
set -e
SVC=checkin-dashboard
DIR="$HOME/checkin/dashboard"
UNIT="/etc/systemd/system/${SVC}.service"

echo "==> 安装 systemd 单元：$UNIT"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Multi-platform check-in dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=on-failure
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF

echo "==> 重载并启动服务"
sudo systemctl daemon-reload
sudo systemctl enable "$SVC"
sudo systemctl restart "$SVC"
sleep 2
systemctl is-active "$SVC" && echo "看板已启动：http://127.0.0.1:8899/"

echo "==> 常用命令"
echo "  查看状态: sudo systemctl status $SVC"
echo "  查看日志: journalctl -u $SVC -f"
echo "  重启:     sudo systemctl restart $SVC"
