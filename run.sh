#!/bin/bash
# 小智服务端启动脚本
# 基于官方 docker-compose_all.yml 部署

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XIAOZHI_SERVER_DIR="$SCRIPT_DIR/main/xiaozhi-server"

echo "=========================================="
echo "  小智服务端启动脚本 (全模块部署)"
echo "=========================================="

# 获取宿主机 IP
get_host_ip() {
    if grep -qi microsoft /proc/version 2>/dev/null; then
        # WSL2: 使用 eth0 的 IP
        HOST_IP=$(ip addr show eth0 2>/dev/null | grep "inet " | awk '{print $2}' | cut -d/ -f1 | head -1)
        if [ -z "$HOST_IP" ]; then
            HOST_IP=$(hostname -I | awk '{print $1}')
        fi
    else
        HOST_IP=$(ip route get 1 | awk '{print $7; exit}' 2>/dev/null || hostname -I | awk '{print $1}')
    fi
    echo "$HOST_IP"
}

HOST_IP=$(get_host_ip)
echo "宿主机 IP: $HOST_IP"

# 进入 xiaozhi-server 目录
cd "$XIAOZHI_SERVER_DIR"
echo "工作目录: $(pwd)"

# 检查必要目录
echo "检查目录结构..."
mkdir -p data uploadfile mysql/data models/SenseVoiceSmall

# 创建配置文件（如果不存在）
CONFIG_FILE="$XIAOZHI_SERVER_DIR/data/.config.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "创建配置文件..."
    cat > "$CONFIG_FILE" << EOF
# 全模块 Docker 部署配置 - 从智控台获取配置
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8003

manager-api:
  # bridge 网络模式，使用容器名称
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
  secret: 请从智控台参数管理获取server.secret的值

prompt_template: agent-base-prompt.txt
EOF
    echo "✓ 已创建配置文件: $CONFIG_FILE"
    echo "⚠ 请编辑配置文件，填入正确的 server.secret"
fi

# 创建空的模型占位文件（使用外部 ASR 时）
if [ ! -f "models/SenseVoiceSmall/model.pt" ]; then
    echo "# 使用外部 ASR，不需要本地模型" > models/SenseVoiceSmall/model.pt
    echo "✓ 已创建模型占位文件"
fi

# 停止旧容器
echo "停止旧容器..."
docker compose -f docker-compose_all.yml down 2>/dev/null || true

# 同时停止根目录的旧容器（如果存在）
docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true

# 启动服务
echo "启动 Docker Compose 服务..."
docker compose -f docker-compose_all.yml up -d

# 等待服务启动
echo "等待服务启动..."
sleep 10

# 检查容器状态
echo ""
echo "=========================================="
echo "  容器状态"
echo "=========================================="
docker compose -f docker-compose_all.yml ps

# 显示服务地址
echo ""
echo "=========================================="
echo "  服务地址"
echo "=========================================="
echo "WebSocket 地址: ws://$HOST_IP:8000/xiaozhi/v1/"
echo "管理后台地址:   http://$HOST_IP:8002/"
echo "OTA 接口地址:   http://$HOST_IP:8002/xiaozhi/ota/"
echo "视觉接口地址:   http://$HOST_IP:8003/mcp/vision/explain"
echo ""
echo "=========================================="
echo "  首次部署步骤"
echo "=========================================="
echo "1. 打开管理后台: http://$HOST_IP:8002/"
echo "2. 注册第一个用户（即超级管理员）"
echo "3. 进入【参数管理】，复制 server.secret 的值"
echo "4. 编辑配置文件: $CONFIG_FILE"
echo "   将 secret 改为复制的值"
echo "5. 在【参数管理】中配置:"
echo "   - server.websocket = ws://$HOST_IP:8000/xiaozhi/v1/"
echo "   - server.ota = http://$HOST_IP:8002/xiaozhi/ota/"
echo "6. 重启服务: docker compose -f docker-compose_all.yml restart"
echo ""
echo "查看日志: docker compose -f docker-compose_all.yml logs -f"
echo "=========================================="
