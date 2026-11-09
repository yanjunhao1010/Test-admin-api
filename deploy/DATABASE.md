# 数据库安装与对接指南

> 上一版部署手册按 **RDS** 写的（RDS 是"开通"，不涉及安装）。如果你说的"安装"是指在 **ECS 上自己装 MySQL**，那看本文的第 1 章就对了。

---

## 0. 先分清两条路

| | 路径 A：ECS 自建 MySQL | 路径 B：阿里云 RDS |
|---|---|---|
| 操作 | `dnf install` 装在本机 | 控制台点几下买实例 |
| 成本 | 0 元（共用 ECS） | 每月几十元起 |
| 网络 | 走 `127.0.0.1`，无白名单概念 | 必须配白名单 + 内网地址 |
| 备份 | **要自己写定时任务** | 控制台自动备份、可回滚 |
| 高可用 | 无（机器挂了数据就没了） | 支持主备、故障切换 |
| 运维 | 升级、调优、扩容全自己来 | 托管 |
| 适合 | 单机小项目、测试环境、预算敏感 | 生产环境、数据重要、想省心 |

这个项目是单机测试管理后台、数据量不大，**两条路都可行**。选 A 省钱省事（不用配白名单），选 B 数据更安全。下面两条路都给。

---

## 1. 路径 A：在 ECS 上自建 MySQL 8.0

> 全部命令基于 **Alibaba Cloud Linux 3**，包管理器是 `dnf`；全程需要 root（`sudo -i` 或每条命令前加 sudo）。

### 1.1 安装

先看系统自带源里有没有 MySQL 模块：

```bash
dnf module list mysql
```

**情况一：列表里有 `mysql` 模块**（推荐，不用外网、无兼容包依赖）

```bash
dnf module reset mysql -y
dnf module enable mysql:8.0 -y
dnf install -y mysql-server
```

**情况二：提示 "No matching Modules to list"**（走上游官方源）

```bash
# MySQL 8 依赖旧版 OpenSSL，AL3 上必须先装这个兼容包，否则启动报 libssl 错误
dnf install -y compat-openssl10

# 系统自带源里可能残存 mysql 模块，会和官方源冲突，先禁掉
dnf module disable mysql -y

# 添加 MySQL 官方 8.0 源（el8 后缀的包与 AL3 兼容）
dnf install -y https://dev.mysql.com/get/mysql80-community-release-el8-7.noarch.rpm

dnf install -y mysql-community-server --nogpgcheck
```

验证：

```bash
mysqld --version        # 期望：mysqld  Ver 8.0.x for Linux on x86_64
```

> ⚠️ MySQL 官方源现在的默认版本是 **9.x**，如果你执行 `dnf install mysql-community-server` 时没指定 8.0 子仓库，可能装成 9.x。上面用的是 `mysql80-community-release` 包，默认就启用了 8.0 子仓库，不会跑偏。装完务必用 `mysqld --version` 确认是 8.0。

### 1.2 启动服务

```bash
systemctl enable --now mysqld
systemctl status mysqld        # 期望 active (running)
```

注意 **服务名是 `mysqld`**，不是 `mysql`。写错会报 "Unit mysql.service not found"。

### 1.3 设置 root 密码

MySQL 8 首次启动会生成一个临时密码：

```bash
grep 'temporary password' /var/log/mysqld.log
# 输出示例：2026-09-15T01:12:33.4Z 6 [Note] [MY-010454] [Server] A temporary password is generated for root@localhost: r_V&f2wyu_vI
```

用临时密码登录并改密码（注意 `--connect-expired-password`，临时密码是"已过期"状态，不加这个参数连不上）：

```bash
mysql --connect-expired-password -uroot -p'上一步拿到的临时密码' \
  -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '换成你的强密码';"
```

**密码策略**：默认 `validate_password` 组件要求 ≥8 位、且同时包含大写字母、小写字母、数字、特殊字符。不满足会报：

```
ERROR 1819 (HY000): Your password does not satisfy the current policy requirements
```

只想内网自用、不想被策略卡住的话（不推荐，但可行）：

```sql
SET GLOBAL validate_password.policy = LOW;
```

也可以直接用官方交互式脚本走一遍，效果一样：`mysql_secure_installation`（会顺带删匿名用户、禁 root 远程登录、删 test 库）。

### 1.4 加固：只监听本机 ★最关键的一步

```bash
cat > /etc/my.cnf.d/99-fastapi.cnf <<'EOF'
[mysqld]
# 只监听本机：应用与数据库同机部署，完全不需要暴露到网络
bind-address = 127.0.0.1
character-set-server = utf8mb4
collation-server = utf8mb4_general_ci
default-time-zone = '+08:00'
EOF

systemctl restart mysqld
```

三个要点：

1. **`bind-address = 127.0.0.1`**：MySQL 8 默认监听**所有网卡**。绑到本机后，即使安全组误开了 3306，外网也连不进来，等于多一道保险。
2. **安全组不要放行 3306**。阿里云官方那篇 MySQL 部署教程里让放行 3306，那是给"远程客户端直连"场景写的，你这个项目不需要——应用就在同一台机器上。
3. **需要本地用 Navicat / DataGrip 连怎么办**？走 SSH 隧道，比开端口安全得多：

   ```bash
   # 在你自己的电脑上执行
   ssh -L 13306:127.0.0.1:3306 root@<ECS公网IP>
   # 然后客户端连 127.0.0.1:13306 即可
   ```

### 1.5 建业务库与应用账号

```bash
mysql -uroot -p
```

```sql
-- 清掉默认的匿名用户和测试库
DELETE FROM mysql.user WHERE User = '';
DROP DATABASE IF EXISTS test;

-- 业务库（与项目 config.yml 里的 database 保持一致）
CREATE DATABASE IF NOT EXISTS zentao DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;

-- 应用账号：localhost 和 127.0.0.1 两个都要建
CREATE USER IF NOT EXISTS 'fastapi_app'@'localhost' IDENTIFIED BY '换成强密码';
CREATE USER IF NOT EXISTS 'fastapi_app'@'127.0.0.1' IDENTIFIED BY '换成强密码';

GRANT ALL PRIVILEGES ON zentao.* TO 'fastapi_app'@'localhost';
GRANT ALL PRIVILEGES ON zentao.* TO 'fastapi_app'@'127.0.0.1';
FLUSH PRIVILEGES;
```

> **为什么两个 host 都要建？** MySQL 把 `localhost` 解释为 **Unix socket 连接**，把 `127.0.0.1` 解释为 **TCP 连接**，两者是两套独立账号。项目里 pymysql 配 `host: 127.0.0.1` 是走 TCP 的，只建 `@'localhost'` 会报 `Access denied for user 'fastapi_app'@'localhost'`——这个报错信息很有迷惑性（它显示的 host 是 localhost，但实际是 TCP 匹配失败）。两个都建最省心。

### 1.6 验证应用账号能连

```bash
mysql -h127.0.0.1 -ufastapi_app -p'你的密码' zentao \
  -e "SELECT VERSION() AS ver, @@character_set_server AS charset, @@bind_address AS bind;"
```

期望看到 `8.0.x` / `utf8mb4` / `127.0.0.1`。

### 1.7 迁移现有数据

业务表（`test_cases`、`test_case_project`、`case_package`、`task_management`、`task_detail`、`zt_user`）需要从老的 `192.168.1.7` 迁过来。在能连通老库的机器上执行：

```bash
mysqldump -h192.168.1.7 -uremote_root -p --no-data --single-transaction zentao > schema.sql
mysqldump -h192.168.1.7 -uremote_root -p --single-transaction --default-character-set=utf8mb4 zentao > data.sql
```

把两个 `.sql` 传到 ECS 后导入：

```bash
mysql -h127.0.0.1 -uroot -p zentao < schema.sql
mysql -h127.0.0.1 -uroot -p zentao < data.sql
```

> 若 `mysqldump` 报 `Unknown table 'COLUMN_STATISTICS in information_schema'`，加参数 `--column-statistics=0`。

RBAC 的四张表（`roles` / `permissions` / `user_roles` / `role_permissions`）**不用迁**，跑一次项目里的脚本即可（幂等）：

```bash
cd /opt/fastapi
sudo -u fastapi env PYTHONPATH=/opt/fastapi:/opt/fastapi/src .venv/bin/python -m src.init_rbac
```

### 1.8 定时备份（自建的必做项）

RDS 有自动备份，自建就只能靠自己。用 `cron.daily` 一天一备、保留 14 天：

```bash
mkdir -p /var/backups/mysql

cat > /etc/cron.daily/mysql-backup <<'EOF'
#!/bin/bash
set -e
D=/var/backups/mysql
mkdir -p "$D"
mysqldump -h127.0.0.1 -uroot -p"$(cat /root/.mysql_root_pass)" \
  --single-transaction --default-character-set=utf8mb4 zentao \
  | gzip > "$D/zentao-$(date +%F).sql.gz"
find "$D" -name '*.sql.gz' -mtime +14 -delete
EOF

chmod +x /etc/cron.daily/mysql-backup
```

`/root/.mysql_root_pass` 里放 root 密码，并收紧权限：

```bash
umask 077 && printf '%s' '你的root密码' > /root/.mysql_root_pass
chmod 600 /root/.mysql_root_pass
```

先手工跑一次确认能出文件：`/etc/cron.daily/mysql-backup && ls -lh /var/backups/mysql/`。

> 更进一步：把备份文件定期 `scp` 到本地或对象存储（OSS）。备份和数据库放在同一块磁盘上，磁盘挂了就一起没了。

### 1.9 同步修改项目配置

把 `/opt/fastapi/src/config/config.yml` 的 host 改成 `127.0.0.1`：

```yaml
mysql:
  host: "127.0.0.1"
  port: 3306
  user: "fastapi_app"
  password: "你的强密码"
  database: "zentao"
  charset: "utf8mb4"
```

然后重启应用：

```bash
systemctl restart fastapi
journalctl -u fastapi -n 20 --no-pager
```

---

## 2. 路径 B：用 RDS（不用装）

如果你选 RDS，**第 1 章整章都可以跳过**——RDS 是托管服务，没有"安装"这个动作。需要做的只有四件事：

1. 控制台买一个 MySQL 8.0 实例，地域和 VPC 与 ECS 保持一致
2. 建库 `zentao` + 建应用账号 `fastapi_app`
3. **白名单里加上 ECS 的内网 IP**（不是公网 IP）：`hostname -I | awk '{print $1}'`
4. 把 RDS 的**内网地址**填进 `config.yml` 的 `host`

详细的 SQL、白名单位置、内网地址在哪看，见 `deploy/DEPLOY.md` 第 2 章。RDS 连接方式与自建库完全一致（都是 MySQL 协议），所以 `DATABASE.md` 第 1.7 节之后的数据迁移、第 1.9 节的配置改法照样适用。

---

## 3. 一键安装脚本

第 1.1–1.6 节的内容已经固化成一个脚本：

```bash
cd /opt/fastapi          # 或上传后的临时目录
sudo bash deploy/install-mysql.sh
```

脚本会：安装 MySQL 8.0（自动判断用系统源还是官方源）→ 启动并设开机自启 → 写入 `bind-address=127.0.0.1` 等加固配置 → 重置 root 密码 → 建库建账号 → 验证连接 → 打印**可直接粘贴的 `config.yml` 片段**。

- root 密码与应用密码由脚本随机生成并**只打印一次**，请立刻保存
- 机器上**已经装过 MySQL** 时，脚本跳过安装，只做加固与建库建账号；此时需要把已有的 root 密码传进去：
  ```bash
  MYSQL_ROOT_PASSWORD='你的root密码' sudo bash deploy/install-mysql.sh
  ```

---

## 4. 排错对照

| 报错 / 现象 | 根因 | 处理 |
|---|---|---|
| `Access denied for user 'fastapi_app'@'localhost'` | 只建了 `@'localhost'`，TCP 连接匹配不上 | 补建 `@'127.0.0.1'` |
| `Your password does not satisfy the current policy requirements` | 默认密码强度策略 | 密码含大小写+数字+特殊字符且 ≥8 位，或 `SET GLOBAL validate_password.policy=LOW` |
| `Authentication plugin 'caching_sha2_password' cannot be loaded` | pymysql 缺少 RSA 加密依赖 | `pip install cryptography`（已在 requirements.txt） |
| `Can't connect to local MySQL server through socket` | 服务没起 | `systemctl start mysqld` / `journalctl -u mysqld -n 50` |
| `Unit mysql.service not found` | 服务名写错 | 用 `mysqld` |
| `dnf install mysql-community-server` 报模块冲突 | 系统自带 mysql 模块与官方源冲突 | `dnf module disable mysql -y` 后重试 |
| `mysqld` 启动报 `libssl.so.10` 找不到 | 缺旧版 OpenSSL 兼容包 | `dnf install -y compat-openssl10` |
| 外网用客户端连 3306 连不上 | **设计如此**（已绑 127.0.0.1） | 走 SSH 隧道，不要改成 0.0.0.0 |
| 中文存进去变问号 | 库/表/连接字符集不是 utf8mb4 | 建库时指定 `DEFAULT CHARSET utf8mb4`，配置里 `charset: utf8mb4` |
| 时间差 8 小时 | MySQL 时区是 UTC | 配置里 `default-time-zone = '+08:00'` |
