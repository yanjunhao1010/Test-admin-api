#!/usr/bin/env bash
# 在 Alibaba Cloud Linux 3 上安装并加固 MySQL 8.0，并创建项目所需的库与账号
#
# 用法：
#   sudo bash deploy/install-mysql.sh
#   MYSQL_ROOT_PASSWORD='已有root密码' sudo bash deploy/install-mysql.sh   # 机器上已装过 MySQL 时
#
# 对应文档：deploy/DATABASE.md 第 1.1 ~ 1.6 节
set -euo pipefail

APP_DB="zentao"
APP_USER="fastapi_app"
CNF="/etc/my.cnf.d/99-fastapi.cnf"

log()  { printf '\033[32m[mysql]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[mysql][错误]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "请用 root 执行：sudo bash $0"

# 生成满足默认密码策略的随机密码（含大小写/数字/特殊字符，长度 16）
APP_PASS="${APP_PASS:-Aa1!$(openssl rand -hex 6)}"

# ---------- 1. 安装 ----------
if command -v mysqld >/dev/null 2>&1; then
    log "检测到 MySQL 已安装（$(mysqld --version 2>/dev/null || echo 版本未知)），跳过安装步骤"
else
    log "开始安装 MySQL 8.0"
    if dnf -q module list mysql 2>/dev/null | grep -qE '^mysql[[:space:]]'; then
        log "使用 Alibaba Cloud Linux 3 自带模块 mysql"
        dnf module reset mysql -y >/dev/null 2>&1 || true
        dnf module enable mysql:8.0 -y >/dev/null 2>&1 \
            || dnf module enable mysql -y >/dev/null 2>&1 \
            || true
        dnf install -y mysql-server
    else
        log "系统源没有 mysql 模块，改用 MySQL 官方源"
        # MySQL 8 依赖旧版 OpenSSL，AL3 上缺这个包会导致 mysqld 启动报 libssl 错误
        dnf install -y compat-openssl10
        # 系统自带源的 mysql 模块会与官方源冲突，先禁用（不存在时忽略报错）
        dnf module disable mysql -y >/dev/null 2>&1 || true
        dnf install -y https://dev.mysql.com/get/mysql80-community-release-el8-7.noarch.rpm
        dnf install -y mysql-community-server --nogpgcheck
    fi
fi

# ---------- 2. 启动服务 ----------
log "启动 mysqld 并设置开机自启"
systemctl enable --now mysqld
if ! systemctl is-active --quiet mysqld; then
    journalctl -u mysqld -n 50 --no-pager
    fail "mysqld 启动失败，见上方日志"
fi

# ---------- 3. 加固配置 ----------
log "写入加固配置 ${CNF}"
cat > "${CNF}" <<'EOF'
[mysqld]
# 只监听本机：应用与数据库同机部署，无需暴露到网络
bind-address = 127.0.0.1
character-set-server = utf8mb4
collation-server = utf8mb4_general_ci
default-time-zone = '+08:00'
EOF
systemctl restart mysqld

# ---------- 4. 取得可用的 root 密码 ----------
ROOT_PASS="${MYSQL_ROOT_PASSWORD:-}"
ROOT_RESET=0

if [ -z "${ROOT_PASS}" ]; then
    TMP_PASS="$(grep 'temporary password' /var/log/mysqld.log 2>/dev/null | tail -1 | sed 's/.*: //' || true)"
    [ -n "${TMP_PASS}" ] || fail "未提供 MYSQL_ROOT_PASSWORD，且日志中找不到初始临时密码。请用 MYSQL_ROOT_PASSWORD='你的root密码' 重新执行"
    ROOT_PASS="Aa1!$(openssl rand -hex 8)"
    log "用初始临时密码重置 root 密码"
    # 临时密码处于「已过期」状态，必须加 --connect-expired-password
    mysql --connect-expired-password -uroot -p"${TMP_PASS}" \
        -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${ROOT_PASS}';" \
        || fail "重置 root 密码失败（可能已被改过）。请用 MYSQL_ROOT_PASSWORD='你的root密码' 重新执行"
    ROOT_RESET=1
fi

if ! mysql -uroot -p"${ROOT_PASS}" -e "SELECT 1;" >/dev/null 2>&1; then
    fail "无法用该 root 密码登录，请确认 MYSQL_ROOT_PASSWORD 是否正确"
fi

# ---------- 5. 建库建账号 ----------
log "创建业务库 ${APP_DB} 与应用账号 ${APP_USER}"
mysql -uroot -p"${ROOT_PASS}" <<SQL
DELETE FROM mysql.user WHERE User = '';
DROP DATABASE IF EXISTS test;
CREATE DATABASE IF NOT EXISTS \`${APP_DB}\` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;
-- localhost（socket）与 127.0.0.1（TCP）是两套账号，必须都建
CREATE USER IF NOT EXISTS '${APP_USER}'@'localhost' IDENTIFIED BY '${APP_PASS}';
CREATE USER IF NOT EXISTS '${APP_USER}'@'127.0.0.1' IDENTIFIED BY '${APP_PASS}';
ALTER USER '${APP_USER}'@'localhost' IDENTIFIED BY '${APP_PASS}';
ALTER USER '${APP_USER}'@'127.0.0.1' IDENTIFIED BY '${APP_PASS}';
GRANT ALL PRIVILEGES ON \`${APP_DB}\`.* TO '${APP_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${APP_DB}\`.* TO '${APP_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

# ---------- 6. 保存 root 密码供备份脚本使用 ----------
if [ "${ROOT_RESET}" = "1" ] || [ -n "${MYSQL_ROOT_PASSWORD:-}" ]; then
    umask 077
    printf '%s' "${ROOT_PASS}" > /root/.mysql_root_pass
    chmod 600 /root/.mysql_root_pass
    log "root 密码已写入 /root/.mysql_root_pass（权限 600），供备份脚本读取"
fi

# ---------- 7. 验证应用账号 ----------
log "验证应用账号连接"
mysql -h127.0.0.1 -u"${APP_USER}" -p"${APP_PASS}" "${APP_DB}" \
    -e "SELECT VERSION() AS version, @@character_set_server AS charset, @@bind_address AS bind_address;" \
    || fail "应用账号连接失败"

cat <<EOF

============================================================
MySQL 安装完成
  root 密码   : ${ROOT_PASS}
  应用账号    : ${APP_USER}
  应用密码    : ${APP_PASS}
  业务库      : ${APP_DB}
  监听        : 127.0.0.1:3306（仅本机，安全组不要放行 3306）

把下面这段填入 /opt/fastapi/src/config/config.yml：

mysql:
  host: "127.0.0.1"
  port: 3306
  user: "${APP_USER}"
  password: "${APP_PASS}"
  database: "${APP_DB}"
  charset: "utf8mb4"

下一步：导入老库数据 → 跑 python -m src.init_rbac 建 RBAC 表
        → 配置 /etc/cron.daily/mysql-backup 定时备份（见 DATABASE.md 1.8）

密码只显示这一次，请立即保存。
============================================================
EOF
