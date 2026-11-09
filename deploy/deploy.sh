#!/usr/bin/env bash
# 一键发布脚本（在 ECS 上以 root 执行）
#
# 用法：
#   sudo bash deploy/deploy.sh                          # 发布当前仓库目录
#   sudo bash /tmp/fastapi-release/deploy/deploy.sh     # 发布指定的上传目录
#
# 作用：同步代码到 /opt/fastapi → 创建/复用 venv → 安装依赖 → 修正权限 → 重启服务 → 本机自检
set -euo pipefail

APP_DIR="/opt/fastapi"
SERVICE="fastapi"
RUN_USER="fastapi"
PY_BIN="python3.11"
VENV="${APP_DIR}/.venv"
PORT="8090"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$#" -ge 1 ]; then
    SRC_DIR="$(cd "$1" && pwd)"
fi

log()  { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[deploy][错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 0. 前置检查 ----------
[ "$(id -u)" -eq 0 ] || fail "请用 root 执行：sudo bash $0"
[ -f "${SRC_DIR}/src/main.py" ] || fail "源码目录不正确（未找到 src/main.py）：${SRC_DIR}"
[ "${SRC_DIR}" != "/" ] || fail "拒绝以根目录作为源码目录"
command -v "${PY_BIN}" >/dev/null 2>&1 || fail "未找到 ${PY_BIN}，请先执行：dnf install -y ${PY_BIN}"
command -v rsync >/dev/null 2>&1 || { log "安装 rsync"; dnf install -y rsync; }

# ---------- 1. 运行用户 ----------
if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    log "创建系统用户 ${RUN_USER}"
    useradd --system --shell /sbin/nologin --home-dir "${APP_DIR}" "${RUN_USER}"
fi

# ---------- 2. 同步代码 ----------
mkdir -p "${APP_DIR}"
if [ "${SRC_DIR}" != "${APP_DIR}" ]; then
    log "同步代码：${SRC_DIR} → ${APP_DIR}"
    # .env 与 config.yml 含密钥，始终保留服务器上的版本，不覆盖也不删除
    rsync -a --delete \
        --exclude='.git/' \
        --exclude='.venv/' \
        --exclude='.idea/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='src/config/config.yml' \
        "${SRC_DIR}/" "${APP_DIR}/"
else
    log "源码目录即目标目录，跳过同步"
fi

# ---------- 3. 配置文件检查 ----------
[ -f "${APP_DIR}/src/config/config.yml" ] || fail "缺少 ${APP_DIR}/src/config/config.yml（参照 deploy/config.yml.example 创建，否则所有数据库接口都会 500）"
if [ ! -f "${APP_DIR}/.env" ]; then
    log "警告：未找到 ${APP_DIR}/.env，AI 接口将报「服务端未配置 DEEPSEEK_API_KEY」，全局鉴权也会处于放行状态"
fi

# ---------- 4. 虚拟环境与依赖 ----------
if [ ! -x "${VENV}/bin/python" ]; then
    log "创建虚拟环境"
    "${PY_BIN}" -m venv "${VENV}"
fi
log "安装/校验依赖"
"${VENV}/bin/pip" install --upgrade pip -q
"${VENV}/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

# ---------- 5. 权限 ----------
log "修正属主与权限"
chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"
chmod 600 "${APP_DIR}/src/config/config.yml"
[ -f "${APP_DIR}/.env" ] && chmod 600 "${APP_DIR}/.env" || true

# ---------- 6. 注册并重启服务 ----------
install -m 644 "${APP_DIR}/deploy/fastapi.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
log "重启服务"
systemctl restart "${SERVICE}"
sleep 2
if ! systemctl is-active --quiet "${SERVICE}"; then
    journalctl -u "${SERVICE}" -n 40 --no-pager
    fail "服务启动失败，见上方日志"
fi

# ---------- 7. 本机自检 ----------
log "本机自检 http://127.0.0.1:${PORT}/docs"
OK=0
for _ in 1 2 3 4 5; do
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/docs"; then
        OK=1
        break
    fi
    sleep 2
done
if [ "${OK}" -ne 1 ]; then
    journalctl -u "${SERVICE}" -n 40 --no-pager
    fail "端口 ${PORT} 无响应"
fi

log "发布完成。实时日志：journalctl -u ${SERVICE} -f"
