#!/bin/sh
# 脚本作者@VanillaNahida
# 本文件是用于一键自动下载本项目所需文件，自动创建好目录
# 暂且只支持X86版本的Ubuntu系统，其他系统未测试

# 服务端口配置
PORT_WS=8000
PORT_HTTP=8001
PORT_WEB=8002
PORT_VISION=8003

# 定义中断处理函数
handle_interrupt() {
    echo ""
    echo "安装已被用户中断(Ctrl+C或Esc)"
    echo "如需重新安装，请再次运行脚本"
    exit 1
}

# 设置信号捕获，处理Ctrl+C
trap handle_interrupt SIGINT

# 处理Esc键
# 保存终端设置
old_stty_settings=$(stty -g)
# 设置终端立即响应，不回显
stty -icanon -echo min 1 time 0

# 后台进程检测Esc键
(while true; do
    read -r key
    if [[ $key == $'\e' ]]; then
        # 检测到Esc键，触发中断处理
        kill -SIGINT $$
        break
    fi
done) &

# 脚本结束时恢复终端设置
trap 'stty "$old_stty_settings"' EXIT

# 打印彩色字符画
echo -e "\e[1;32m"
cat << "EOF"
脚本作者：jacob_ng@163.com
 _  __  _____ ______ 
| |/ / / ____|___  / 
| ' / | (___    / /  
|  <   \___ \  / /   
| . \  ____) |/ /__  
|_|\_\|_____//_____| 
                     
EOF
echo -e "\e[0m"
echo -e "\e[1;36m  康硕展AI旅游机项目安装脚本 Ver 0.2 2026年6月16日更新 \e[0m\n"
sleep 1



# 检查并安装whiptail
check_whiptail() {
    if ! command -v whiptail &> /dev/null; then
        echo "正在安装whiptail..."
        apt update
        apt install -y whiptail
    fi
}

check_whiptail

# 创建确认对话框
whiptail --title "安装确认" --yesno "即将安装小智服务端，是否继续？" \
  --yes-button "继续" --no-button "退出" 10 50

# 根据用户选择执行操作
case $? in
  0)
    ;;
  1)
    exit 1
    ;;
esac

# 检查root权限
if [ $EUID -ne 0 ]; then
    whiptail --title "权限错误" --msgbox "请使用root权限运行本脚本" 10 50
    exit 1
fi

# 检查系统版本
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$ID" != "debian" ] && [ "$ID" != "ubuntu" ]; then
        whiptail --title "系统错误" --msgbox "该脚本只支持Debian/Ubuntu系统执行" 10 60
        exit 1
    fi
else
    whiptail --title "系统错误" --msgbox "无法确定系统版本，该脚本只支持Debian/Ubuntu系统执行" 10 60
    exit 1
fi

# 下载配置文件函数
check_and_download() {
    local filepath=$1
    local url=$2
    if [ ! -f "$filepath" ]; then
        if ! curl -fL --progress-bar "$url" -o "$filepath"; then
            whiptail --title "错误" --msgbox "${filepath}文件下载失败" 10 50
            exit 1
        fi
    else
        echo "${filepath}文件已存在，跳过下载"
    fi
}

# 检查是否已安装
check_installed() {
    # 检查目录是否存在且非空
    if [ -d "/opt/xiaozhi-server/" ] && [ "$(ls -A /opt/xiaozhi-server/)" ]; then
        DIR_CHECK=1
    else
        DIR_CHECK=0
    fi
    
    # 检查容器是否存在
    if docker inspect xiaozhi-esp32-server > /dev/null 2>&1; then
        CONTAINER_CHECK=1
    else
        CONTAINER_CHECK=0
    fi
    
    # 两次检查都通过
    if [ $DIR_CHECK -eq 1 ] && [ $CONTAINER_CHECK -eq 1 ]; then
        return 0  # 已安装
    else
        return 1  # 未安装
    fi
}

# 更新相关
if check_installed; then
    if whiptail --title "已安装检测" --yesno "检测到小智服务端已安装，是否进行升级？" 10 60; then
        # 用户选择升级，执行清理操作
        echo "开始升级操作..."
        
        # 停止并移除所有docker-compose服务
        docker compose -f /opt/xiaozhi-server/docker-compose_all.yml down
        
        # 停止并删除特定容器（考虑容器可能不存在的情况）
        containers=(
            "xiaozhi-esp32-server"
            "xiaozhi-esp32-server-web"
            "xiaozhi-esp32-server-db"
            "xiaozhi-esp32-server-redis"
        )
        
        for container in "${containers[@]}"; do
            if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
                docker stop "$container" >/dev/null 2>&1 && \
                docker rm "$container" >/dev/null 2>&1 && \
                echo "成功移除容器: $container"
            else
                echo "容器不存在，跳过: $container"
            fi
        done
        
        # 删除特定镜像（考虑镜像可能不存在的情况）
        images=(
            "ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest"
            "ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:web_latest"
        )
        
        for image in "${images[@]}"; do
            if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${image}$"; then
                docker rmi "$image" >/dev/null 2>&1 && \
                echo "成功删除镜像: $image"
            else
                echo "镜像不存在，跳过: $image"
            fi
        done
        
        echo "所有清理操作完成"
        
        # 备份原有配置文件
        mkdir -p /opt/xiaozhi-server/backup/
        if [ -f /opt/xiaozhi-server/data/.config.yaml ]; then
            cp /opt/xiaozhi-server/data/.config.yaml /opt/xiaozhi-server/backup/.config.yaml
            echo "已备份原有配置文件到 /opt/xiaozhi-server/backup/.config.yaml"
        fi
        
        # 下载最新版配置文件
        check_and_download "/opt/xiaozhi-server/docker-compose_all.yml" "https://ghfast.top/https://raw.githubusercontent.com/xinnan-tech/xiaozhi-esp32-server/refs/heads/main/main/xiaozhi-server/docker-compose_all.yml"
        check_and_download "/opt/xiaozhi-server/data/.config.yaml" "https://ghfast.top/https://raw.githubusercontent.com/xinnan-tech/xiaozhi-esp32-server/refs/heads/main/main/xiaozhi-server/config_from_api.yaml"
        
        # 启动Docker服务
        echo "开始启动最新版本服务..."
        # 升级完成后标记，跳过后续下载步骤
        UPGRADE_COMPLETED=1
        docker compose -f /opt/xiaozhi-server/docker-compose_all.yml up -d
    else
          whiptail --title "跳过升级" --msgbox "已取消升级，将继续使用当前版本。" 10 50
          # 跳过升级，继续执行后续安装流程
    fi
fi


# 检查curl安装
if ! command -v curl &> /dev/null; then
    echo "------------------------------------------------------------"
    echo "未检测到curl，正在安装..."
    apt update
    apt install -y curl
else
    echo "------------------------------------------------------------"
    echo "curl已安装，跳过安装步骤"
fi

# 检查Docker安装
if ! command -v docker &> /dev/null; then
    echo "------------------------------------------------------------"
    echo "未检测到Docker，正在安装..."
    
    # 使用国内镜像源替代官方源
    DISTRO=$(lsb_release -cs)
    MIRROR_URL="https://mirrors.aliyun.com/docker-ce/linux/ubuntu"
    GPG_URL="https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg"
    
    # 安装基础依赖
    apt update
    apt install -y apt-transport-https ca-certificates curl software-properties-common gnupg
    
    # 创建密钥目录并添加国内镜像源密钥
    mkdir -p /etc/apt/keyrings
    curl -fsSL "$GPG_URL" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    
    # 添加国内镜像源
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] $MIRROR_URL $DISTRO stable" \
        > /etc/apt/sources.list.d/docker.list
    
    # 添加备用官方源密钥（避免国内源密钥验证失败）
    apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 7EA0A9C3F273FCD8 2>/dev/null || \
    echo "警告：部分密钥添加失败，继续尝试安装..."
    
    # 安装Docker
    apt update
    apt install -y docker-ce docker-ce-cli containerd.io
    
    # 启动服务
    systemctl start docker
    systemctl enable docker
    
    # 检查是否安装成功
    if docker --version; then
        echo "------------------------------------------------------------"
        echo "Docker安装完成！"
    else
        whiptail --title "错误" --msgbox "Docker安装失败，请检查日志。" 10 50
        exit 1
    fi
else
    echo "Docker已安装，跳过安装步骤"
fi

# Docker镜像源配置
apply_docker_mirror() {
    local mirror_url="$1"
    mkdir -p /etc/docker
    if [ -f /etc/docker/daemon.json ]; then
        cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
        python3 -c "
import json
path = '/etc/docker/daemon.json'
with open(path, 'r') as f:
    config = json.load(f)
config['registry-mirrors'] = ['$mirror_url']
if 'dns' not in config:
    config['dns'] = ['8.8.8.8', '114.114.114.114']
with open(path, 'w') as f:
    json.dump(config, f, indent=4, ensure_ascii=False)
    f.write('\n')
"
    else
        cat > /etc/docker/daemon.json <<EOF
{
    "dns": ["8.8.8.8", "114.114.114.114"],
    "registry-mirrors": ["$mirror_url"]
}
EOF
    fi
    whiptail --title "配置成功" --msgbox "已成功配置镜像源: $mirror_url\n请按Enter键重启Docker服务并继续..." 12 60
    echo "------------------------------------------------------------"
    echo "开始重启Docker服务..."
    systemctl restart docker.service
}

SKIP_MIRROR_CONFIG=0
if [ -f /etc/docker/daemon.json ]; then
    EXISTING_MIRRORS=$(python3 -c "
import json
try:
    with open('/etc/docker/daemon.json') as f:
        mirrors = json.load(f).get('registry-mirrors', [])
    print(', '.join(mirrors) if mirrors else '')
except Exception:
    print('')
" 2>/dev/null)
    if [ -n "$EXISTING_MIRRORS" ]; then
        if whiptail --title "Docker配置" --yesno "检测到已有 Docker 镜像源配置：\n$EXISTING_MIRRORS\n\n是否保留现有 /etc/docker/daemon.json？\n（选择「保留」将不会修改镜像源配置）" 16 70 --yes-button "保留" --no-button "重新配置"; then
            SKIP_MIRROR_CONFIG=1
            echo "保留现有 Docker 配置，跳过镜像源设置"
        fi
    fi
fi

if [ "$SKIP_MIRROR_CONFIG" -eq 0 ]; then
    MIRROR_OPTIONS=(
        "1" "轩辕镜像 (推荐)"
        "2" "腾讯云镜像源"
        "3" "中科大镜像源"
        "4" "网易163镜像源"
        "5" "华为云镜像源"
        "6" "阿里云镜像源"
        "7" "自定义镜像源"
        "8" "跳过配置"
    )

    MIRROR_CHOICE=$(whiptail --title "选择Docker镜像源" --menu "请选择要使用的Docker镜像源" 20 60 10 \
    "${MIRROR_OPTIONS[@]}" 3>&1 1>&2 2>&3) || {
        echo "用户取消选择，退出脚本"
        exit 1
    }

    case $MIRROR_CHOICE in
        1) MIRROR_URL="https://docker.xuanyuan.me" ;;
        2) MIRROR_URL="https://mirror.ccs.tencentyun.com" ;;
        3) MIRROR_URL="https://docker.mirrors.ustc.edu.cn" ;;
        4) MIRROR_URL="https://hub-mirror.c.163.com" ;;
        5) MIRROR_URL="https://05f073ad3c0010ea0f4bc00b7105ec20.mirror.swr.myhuaweicloud.com" ;;
        6) MIRROR_URL="https://registry.aliyuncs.com" ;;
        7) MIRROR_URL=$(whiptail --title "自定义镜像源" --inputbox "请输入完整的镜像源URL:" 10 60 3>&1 1>&2 2>&3) ;;
        8) MIRROR_URL="" ;;
    esac

    if [ -n "$MIRROR_URL" ]; then
        apply_docker_mirror "$MIRROR_URL"
    fi
fi

# 创建安装目录
echo "------------------------------------------------------------"
echo "开始创建安装目录..."
# 检查并创建数据目录
if [ ! -d /opt/xiaozhi-server/data ]; then
    mkdir -p /opt/xiaozhi-server/data
    echo "已创建数据目录: /opt/xiaozhi-server/data"
else
    echo "目录xiaozhi-server/data已存在，跳过创建"
fi

# 检查并创建模型目录
if [ ! -d /opt/xiaozhi-server/models/SenseVoiceSmall ]; then
    mkdir -p /opt/xiaozhi-server/models/SenseVoiceSmall
    echo "已创建模型目录: /opt/xiaozhi-server/models/SenseVoiceSmall"
else
    echo "目录xiaozhi-server/models/SenseVoiceSmall已存在，跳过创建"
fi

echo "------------------------------------------------------------"
echo "开始下载语音识别模型"
# 下载模型文件
MODEL_PATH="/opt/xiaozhi-server/models/SenseVoiceSmall/model.pt"
DEFAULT_MODEL_URL="https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt"

if [ ! -f "$MODEL_PATH" ]; then
    MODEL_URL_CHOICE=$(whiptail --title "模型下载" --menu "请选择语音识别模型下载方式" 15 60 3 \
    "1" "使用默认地址下载 (推荐)" \
    "2" "自定义下载地址" 3>&1 1>&2 2>&3) || {
        echo "用户取消选择，退出脚本"
        exit 1
    }
    
    case $MODEL_URL_CHOICE in
        1) MODEL_URL="$DEFAULT_MODEL_URL" ;;
        2) MODEL_URL=$(whiptail --title "自定义模型地址" --inputbox "请输入模型文件的完整下载URL:" 10 60 "$DEFAULT_MODEL_URL" 3>&1 1>&2 2>&3) ;;
    esac
    
    if [ -z "$MODEL_URL" ]; then
        whiptail --title "错误" --msgbox "模型下载地址不能为空" 10 50
        exit 1
    fi
    
    (
    for i in {1..20}; do
        echo $((i*5))
        sleep 0.5
    done
    ) | whiptail --title "下载中" --gauge "开始下载语音识别模型..." 10 60 0
    curl -fL --progress-bar "$MODEL_URL" -o "$MODEL_PATH" || {
        whiptail --title "错误" --msgbox "model.pt文件下载失败" 10 50
        exit 1
    }
else
    echo "model.pt文件已存在，跳过下载"
fi

# 如果不是升级完成，才执行下载
if [ -z "$UPGRADE_COMPLETED" ]; then
    check_and_download "/opt/xiaozhi-server/docker-compose_all.yml" "https://ghfast.top/https://raw.githubusercontent.com/xinnan-tech/xiaozhi-esp32-server/refs/heads/main/main/xiaozhi-server/docker-compose_all.yml"
    check_and_download "/opt/xiaozhi-server/data/.config.yaml" "https://ghfast.top/https://raw.githubusercontent.com/xinnan-tech/xiaozhi-esp32-server/refs/heads/main/main/xiaozhi-server/config_from_api.yaml"
fi

# LLM配置
if whiptail --title "LLM配置" --yesno "是否需要配置自定义LLM接口？\n（如不需要，将使用默认的ChatGLMLLM免费模型）" 10 60; then
    LLM_BASE_URL=$(whiptail --title "LLM配置 - Base URL" --inputbox "请输入LLM API地址 (base_url):\n例如: https://api.openai.com/v1" 12 60 3>&1 1>&2 2>&3)
    LLM_API_KEY=$(whiptail --title "LLM配置 - API Key" --inputbox "请输入LLM API密钥 (api_key):" 10 60 3>&1 1>&2 2>&3)
    LLM_MODEL_NAME=$(whiptail --title "LLM配置 - 模型名称" --inputbox "请输入模型名称 (model_name):\n例如: gpt-4o, qwen-plus, deepseek-chat" 12 60 3>&1 1>&2 2>&3)
    LLM_LANGUAGE=$(whiptail --title "LLM配置 - 语言" --inputbox "请输入语言参数 (language, 留空则跳过):\n例如: zh, en, auto" 12 60 3>&1 1>&2 2>&3)
    
    if [ -n "$LLM_BASE_URL" ] && [ -n "$LLM_API_KEY" ] && [ -n "$LLM_MODEL_NAME" ]; then
        python3 -c "
import sys, yaml
config_path = '/opt/xiaozhi-server/data/.config.yaml'
with open(config_path, 'r') as f:
    config = yaml.safe_load(f) or {}

# 写入LLM配置
llm_name = 'CustomLLM'
llm_config = {
    'type': 'openai',
    'base_url': '$LLM_BASE_URL',
    'api_key': '$LLM_API_KEY',
    'model_name': '$LLM_MODEL_NAME',
}
if '$LLM_LANGUAGE':
    llm_config['language'] = '$LLM_LANGUAGE'

config['LLM'] = {llm_name: llm_config}
config['selected_module'] = config.get('selected_module', {})
config['selected_module']['LLM'] = llm_name

with open(config_path, 'w') as f:
    yaml.dump(config, f)
"
        whiptail --title "LLM配置成功" --msgbox "已配置自定义LLM:\nAPI地址: $LLM_BASE_URL\n模型: $LLM_MODEL_NAME\n语言: ${LLM_LANGUAGE:-未设置}" 12 60
    else
        whiptail --title "LLM配置" --msgbox "配置信息不完整，将使用默认LLM" 10 50
    fi
else
    echo "跳过LLM配置，使用默认模型"
fi

# 清理同名容器函数
cleanup_existing_containers() {
    echo "正在检查并清理同名容器..."
    local containers=(
        "xiaozhi-esp32-server"
        "xiaozhi-esp32-server-web"
        "xiaozhi-esp32-server-db"
        "xiaozhi-esp32-server-redis"
    )
    
    for container in "${containers[@]}"; do
        if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
            echo "发现同名容器: $container，正在停止并删除..."
            docker stop "$container" >/dev/null 2>&1
            docker rm -f "$container" >/dev/null 2>&1
        fi
    done
    
    # 清理可能残留的网络
    docker network rm xiaozhi-esp32-server_default 2>/dev/null || true
    echo "清理完成"
}

# 启动Docker服务
start_docker_services() {
    echo "------------------------------------------------------------"
    echo "正在拉取Docker镜像..."
    echo "这可能需要几分钟时间，请耐心等待"
    
    # 捕获docker compose输出用于错误诊断
    COMPOSE_OUTPUT=$(docker compose -f /opt/xiaozhi-server/docker-compose_all.yml up -d 2>&1)
    COMPOSE_EXIT_CODE=$?
    
    if [ $COMPOSE_EXIT_CODE -ne 0 ]; then
        echo "$COMPOSE_OUTPUT"
        
        # 分析错误原因并给出针对性提示
        if echo "$COMPOSE_OUTPUT" | grep -qi "Conflict.*container name.*already in use"; then
            # 容器名冲突，自动清理并重试
            echo "检测到容器名称冲突，正在自动清理..."
            cleanup_existing_containers
            sleep 2
            echo "重新启动服务..."
            docker compose -f /opt/xiaozhi-server/docker-compose_all.yml up -d
            if [ $? -eq 0 ]; then
                echo "服务启动成功！"
                return 0
            fi
            ERROR_MSG="容器名称冲突，自动清理后仍然失败\n\n请手动执行：\ndocker rm -f xiaozhi-esp32-server xiaozhi-esp32-server-web xiaozhi-esp32-server-db xiaozhi-esp32-server-redis"
        elif echo "$COMPOSE_OUTPUT" | grep -qi "port.*already in use\|address already in use"; then
            ERROR_MSG="端口被占用！\n\n可能的解决方案：\n1. 运行 'docker compose down' 清理旧容器\n2. 检查端口占用: netstat -tlnp | grep -E '8000|8002|8003'\n3. 修改脚本开头的端口配置"
        elif echo "$COMPOSE_OUTPUT" | grep -qi "pull.*error\|manifest.*not found\|connection refused\|timeout"; then
            ERROR_MSG="镜像拉取失败！\n\n可能的解决方案：\n1. 检查网络连接\n2. 更换Docker镜像源后重新执行\n3. 手动拉取镜像: docker pull ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest"
        elif echo "$COMPOSE_OUTPUT" | grep -qi "network.*error\|failed to create network"; then
            ERROR_MSG="Docker网络创建失败！\n\n可能的解决方案：\n1. 运行 'docker network prune -f' 清理网络\n2. 重启Docker服务: systemctl restart docker\n3. 检查是否有残留容器: docker ps -a"
        elif echo "$COMPOSE_OUTPUT" | grep -qi "no such file\|config.*not found"; then
            ERROR_MSG="配置文件缺失！\n\n请检查以下文件是否存在：\n- /opt/xiaozhi-server/docker-compose_all.yml\n- /opt/xiaozhi-server/data/.config.yaml"
        else
            ERROR_MSG="Docker服务启动失败！\n\n错误信息：\n${COMPOSE_OUTPUT:0:300}\n\n请检查Docker日志获取更多信息"
        fi
        
        whiptail --title "启动错误" --msgbox "$ERROR_MSG" 18 70
        
        # 询问是否重试
        if whiptail --title "重试" --yesno "是否尝试彻底清理后重新启动？" 10 60; then
            echo "正在彻底清理..."
            cleanup_existing_containers
            docker compose -f /opt/xiaozhi-server/docker-compose_all.yml down --remove-orphans 2>/dev/null
            sleep 3
            docker compose -f /opt/xiaozhi-server/docker-compose_all.yml up -d
            if [ $? -ne 0 ]; then
                whiptail --title "错误" --msgbox "重试仍然失败，请手动排查问题后重新运行脚本" 10 60
                exit 1
            fi
        else
            exit 1
        fi
    fi
    
    echo "------------------------------------------------------------"
    echo "正在检查服务启动状态..."
    TIMEOUT=300
    START_TIME=$(date +%s)
    LAST_STATUS=""
    
    while true; do
        CURRENT_TIME=$(date +%s)
        ELAPSED=$((CURRENT_TIME - START_TIME))
        
        if [ $ELAPSED -gt $TIMEOUT ]; then
            # 超时时显示各容器状态
            CONTAINER_STATUS=$(docker compose -f /opt/xiaozhi-server/docker-compose_all.yml ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null)
            whiptail --title "启动超时" --msgbox "服务启动超时(${TIMEOUT}秒)\n\n容器状态：\n$CONTAINER_STATUS\n\n请检查日志: docker logs xiaozhi-esp32-server-web" 16 70
            exit 1
        fi
        
        # 每30秒显示一次进度
        if [ $((ELAPSED % 30)) -eq 0 ] && [ "$ELAPSED" != "$LAST_STATUS" ]; then
            echo "已等待 ${ELAPSED} 秒，继续等待服务启动..."
            LAST_STATUS="$ELAPSED"
        fi
        
        if docker logs xiaozhi-esp32-server-web 2>&1 | grep -q "Started AdminApplication in"; then
            break
        fi
        sleep 2
    done
    
    echo "服务端启动成功！"
}

start_docker_services

# 密钥配置

# 获取服务器公网地址
PUBLIC_IP=$(hostname -I | awk '{print $1}')
whiptail --title "配置服务器密钥" --msgbox "请使用浏览器，访问下方链接，打开智控台并注册账号: \n\n内网地址：http://127.0.0.1:${PORT_WEB}/\n公网地址：http://$PUBLIC_IP:${PORT_WEB}/ (若是云服务器请在服务器安全组放行端口 $PORT_WS $PORT_HTTP $PORT_WEB)。\n\n注册的第一个用户即是超级管理员，以后注册的用户都是普通用户。普通用户只能绑定设备和配置智能体; 超级管理员可以进行模型管理、用户管理、参数配置等功能。\n\n注册好后请按Enter键继续" 18 70
SECRET_KEY=$(whiptail --title "配置服务器密钥" --inputbox "请使用超级管理员账号登录智控台\n内网地址：http://127.0.0.1:${PORT_WEB}/\n公网地址：http://$PUBLIC_IP:${PORT_WEB}/\n在顶部菜单 参数字典 → 参数管理 找到参数编码: server.secret (服务器密钥) \n复制该参数值并输入到下面输入框\n\n请输入密钥(留空则跳过配置):" 15 60 3>&1 1>&2 2>&3)

if [ -n "$SECRET_KEY" ]; then
    python3 -c "
import sys, yaml; 
config_path = '/opt/xiaozhi-server/data/.config.yaml'; 
with open(config_path, 'r') as f: 
    config = yaml.safe_load(f) or {}; 
config['manager-api'] = {'url': 'http://xiaozhi-esp32-server-web:${PORT_WEB}/xiaozhi', 'secret': '$SECRET_KEY'}; 
with open(config_path, 'w') as f: 
    yaml.dump(config, f); 
"
    docker restart xiaozhi-esp32-server
fi

# 获取并显示地址信息
LOCAL_IP=$(hostname -I | awk '{print $1}')

# 修复日志文件获取不到ws的问题，改为硬编码
whiptail --title "安装完成！" --msgbox "\
服务端相关地址如下：\n\
管理后台访问地址: http://$LOCAL_IP:${PORT_WEB}\n\
OTA 地址: http://$LOCAL_IP:${PORT_WEB}/xiaozhi/ota/\n\
视觉分析接口地址: http://$LOCAL_IP:${PORT_VISION}/mcp/vision/explain\n\
WebSocket 地址: ws://$LOCAL_IP:${PORT_WS}/xiaozhi/v1/\n\
\n安装完毕！感谢您的使用！\n按Enter键退出..." 16 70
