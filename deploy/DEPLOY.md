# 部署手册 · FastAPI 测试管理后台到阿里云 ECS

> 目标形态：**阿里云 ECS（Alibaba Cloud Linux 3）+ RDS MySQL + 公网 IP 直连 HTTP + systemd 托管 Gunicorn**
> 全流程约 8 个步骤，其中第 1、2 步在阿里云控制台完成，其余在服务器上完成。

---

## 0. 前置清单

开始前请准备好：

| 项目 | 说明 |
|---|---|
| ECS 实例 | 建议 2 核 4G 起（pandas 处理 Excel 比较吃内存），Alibaba Cloud Linux 3.2104 |
| RDS MySQL | 8.0 版本，与 ECS **同一地域、同一 VPC** |
| ECS 公网 IP | 后续前端 `CORS_ORIGINS` 要用 |
| DeepSeek API Key | 注意：仓库 `.env.example` 里注明的旧 Key 已泄露，必须换新 Key |
| 本机 OpenSSH | Windows 10+ 自带 `ssh` / `scp` 命令 |

---

## 1. ECS 安全组放行

控制台 → ECS → 安全组 → 配置规则 → 入方向，添加：

| 端口 | 授权对象 | 用途 |
|---|---|---|
| 80/tcp | 0.0.0.0/0 | Nginx 对外入口 |
| 22/tcp | **你的本机公网 IP**（不要图省事写 0.0.0.0/0） | SSH 登录 |

> ⚠️ **不要放行 8090**。8090 是 Gunicorn 的本机监听端口，只允许 127.0.0.1 访问，对外一律走 Nginx 的 80。放行 8090 等于绕过 Nginx 把应用直接暴露到公网。
>
> 3306 也不要放行——应用访问 RDS 走内网，不经过安全组。

---

## 2. RDS 准备

> 如果你决定**不用 RDS，改为在同一台 ECS 上自建 MySQL**，请跳过本章，改看 **`deploy/DATABASE.md`**（第 1 章有完整安装与加固步骤，或用一键脚本 `sudo bash deploy/install-mysql.sh`），并把 `config.yml` 的 `host` 写成 `127.0.0.1`。两条路的差别与取舍见该文档第 0 节的对照表。

### 2.1 创建账号与库

在 RDS 控制台创建：

```sql
-- 用高权限账号在 RDS 上执行
CREATE DATABASE IF NOT EXISTS zentao DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER 'fastapi_app'@'%' IDENTIFIED BY '换成你的强密码';
GRANT ALL PRIVILEGES ON zentao.* TO 'fastapi_app'@'%';
FLUSH PRIVILEGES;
```

### 2.2 配置白名单（最容易漏的一步）

RDS 控制台 → 数据安全性 → **白名单设置**，把 **ECS 的内网 IP**（不是公网 IP）加进去。

查询 ECS 内网 IP：

```bash
hostname -I | awk '{print $1}'
```

> 报错 `Can't connect to MySQL server` 且网络 8 成是白名单没加。

### 2.3 记录内网地址

RDS 控制台 → 数据库连接 → **内网地址**，形如 `rm-xxxxxxxx.mysql.rds.aliyuncs.com`。第 4 步要填进 `config.yml`。

### 2.4 迁移现有数据

这个项目依赖的表有两组：

- **业务表**（来自现有库，需从老库迁过来）：`test_cases`、`test_case_project`、`case_package`、`task_management`、`task_detail`、`zt_user`
- **RBAC 表**（由 `src/init_rbac.py` 自动创建，无需手工迁移）：`roles`、`permissions`、`user_roles`、`role_permissions`

从旧的 `192.168.1.7` 导出（在能连通老库的机器上执行）：

```bash
# 只导结构
mysqldump -h192.168.1.7 -uremote_root -p --no-data --single-transaction zentao > schema.sql
# 导数据
mysqldump -h192.168.1.7 -uremote_root -p --single-transaction --default-character-set=utf8mb4 zentao > data.sql

# 导入 RDS（在 ECS 上执行，走内网）
mysql -h rm-xxxxxxxx.mysql.rds.aliyuncs.com -u fastapi_app -p zentao < schema.sql
mysql -h rm-xxxxxxxx.mysql.rds.aliyuncs.com -u fastapi_app -p zentao < data.sql
```

> 若 `mysqldump` 报 `Unknown table 'COLUMN_STATISTICS'`，加参数 `--column-statistics=0`。

---

## 3. 服务器一次性初始化

SSH 登录 ECS 后执行：

```bash
# 3.1 安装 Python 3.11（AL3 默认只有 3.6，跑不了 pydantic 2.x）
dnf install -y python3.11
python3.11 --version          # 应输出 Python 3.11.x

# 3.2 安装 Nginx 与 rsync
dnf install -y nginx rsync
systemctl enable --now nginx

# 3.3 放行防火墙 80 端口
firewall-cmd --permanent --add-service=http
firewall-cmd --reload

# 3.4 SELinux 允许 Nginx 反向代理到本机其他端口（不做会 502）
setsebool -P httpd_can_network_connect 1
getenforce                    # 确认 SELinux 状态
```

> **为什么必须显式处理 SELinux**：AL3 默认开启 SELinux，Nginx 默认策略禁止它连接本机非 80/443 端口。漏掉这一步的表现是：`curl 127.0.0.1:8090` 正常，但浏览器访问 80 一直是 **502 Bad Gateway**，且应用日志毫无异常——新手很容易在这里卡很久。

> **`python3.11 -m venv` 报 ensurepip 缺失**时，补装 `dnf install -y python3.11-pip`；仍不行就用 `python3.11 -m venv --without-pip .venv && curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python`。

---

## 4. 上传代码

本地 Windows **Git Bash**（或用 WinSCP 拖拽）：

```bash
cd /d/pyproject

# 打包时排除本地虚拟环境、IDE 缓存等，避免把几百 MB 垃圾传上去
tar --exclude=.venv --exclude=__pycache__ --exclude=.idea -czf fastapi-release.tar.gz Fast_api_project

scp fastapi-release.tar.gz root@<ECS公网IP>:/tmp/

ssh root@<ECS公网IP> "cd /tmp && rm -rf /tmp/Fast_api_project && tar xzf fastapi-release.tar.gz"
```

> 本项目目录**不是 git 仓库**（没有 `.git`），所以不能用 `git clone` 的方式部署，要么用上面的 tar 方式，要么用 WinSCP 上传目录。

---

## 5. 创建生产配置

项目里没有、也不该有生产配置——以下两个文件必须在服务器上手工创建。

### 5.1 数据库配置

```bash
mkdir -p /opt/fastapi
cd /tmp/Fast_api_project
cp deploy/config.yml.example src/config/config.yml
vi src/config/config.yml        # 把 host 改成 RDS 内网地址，密码改成真实密码
```

### 5.2 环境变量

```bash
cp deploy/env.production.example .env

# 生成一个 32 字节随机 API Key
openssl rand -hex 32

vi .env
```

`.env` 需要填三项：

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 平台申请的新 Key |
| `API_KEY` | 上面 `openssl rand -hex 32` 的输出；**前端 `VUE_APP_API_KEY` 必须与此完全一致** |
| `CORS_ORIGINS` | 前端页面地址，英文逗号分隔，例如 `http://1.2.3.4` |

> ⚠️ **`.env` 里不要写行内注释**。这个文件会被 systemd 的 `EnvironmentFile` 和 `src/main.py` 的 `_load_env()` 双重解析，两者都不会剥离行内注释——`API_KEY=abc  # 说明` 会让密钥值变成 `abc  # 说明`。

> 另外注意 `src/config/` 下有个空的 `config.yaml`，代码读的是 **`config.yml`**，别改错文件。

---

## 6. 一键发布

```bash
cd /tmp/Fast_api_project
sudo bash deploy/deploy.sh
```

脚本会依次完成：创建 `fastapi` 系统用户 → rsync 同步代码到 `/opt/fastapi` → 建 venv 装依赖 → 修正属主与 `600` 权限 → 安装 systemd 单元 → 重启服务 → 请求 `/docs` 自检。

看到 `发布完成` 即成功。后续代码有更新，重复执行这条命令即可（`.env` 和 `config.yml` 不会被覆盖或误删）。

### 手动方式（脚本失败时排查用）

```bash
cd /opt/fastapi
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
chown -R fastapi:fastapi /opt/fastapi
systemctl daemon-reload && systemctl restart fastapi
systemctl status fastapi
```

---

## 7. 初始化 RBAC 表

RBAC 四张表不在业务库里，需要跑一次初始化脚本（幂等，可重复执行）：

```bash
cd /opt/fastapi
sudo -u fastapi env PYTHONPATH=/opt/fastapi:/opt/fastapi/src .venv/bin/python -m src.init_rbac
```

输出中若出现 `roles` / `permissions` 相关的创建与写入日志即为正常。

---

## 8. 配置 Nginx

```bash
cp /opt/fastapi/deploy/nginx-fastapi.conf /etc/nginx/conf.d/fastapi.conf
nginx -t                       # 必须 syntax is ok
systemctl reload nginx
```

验证：

```bash
curl -i http://127.0.0.1/api/testcases/testcaseList -H "X-API-Key: 你的API_KEY"
```

若访问 ECS 公网 IP 看到的是 Nginx 欢迎页而不是接口响应，说明 `/etc/nginx/nginx.conf` 里还有个默认 `server` 块占着 80 端口，把它注释掉再 `systemctl reload nginx`。

---

## 9. 验收清单

| # | 检查项 | 命令 / 方式 | 期望 |
|---|---|---|---|
| 1 | 服务在跑 | `systemctl is-active fastapi` | `active` |
| 2 | 开机自启 | `systemctl is-enabled fastapi` | `enabled` |
| 3 | 本机接口 | `curl -I http://127.0.0.1:8090/docs` | `200` |
| 4 | Nginx 转发 | `curl -I http://127.0.0.1/docs` | `200` |
| 5 | 公网可达 | 浏览器打开 `http://<公网IP>/docs` | 看到 Swagger 文档页 |
| 6 | 鉴权生效 | 不带 `X-API-Key` 请求 `/api/testcases/testcaseList` | `403` |
| 7 | 鉴权放行 | 带正确 `X-API-Key` 请求同上 | `200` + 数据 |
| 8 | 数据库连通 | 同上接口返回真实数据 | 排除 `Can't connect to MySQL` |
| 9 | AI 接口 | `POST /api/ais/chat` | 返回模型回复，非「服务端未配置 DEEPSEEK_API_KEY」 |
| 10 | Excel 上传 | 上传一个 xlsx 到 `/api/upload/upload_file` | 非 `413 Request Entity Too Large` |
| 11 | 前端跨域 | 前端页面发起请求 | 浏览器控制台无 CORS 报错 |

---

## 10. 常见报错对照

| 现象 | 根因 | 处理 |
|---|---|---|
| `502 Bad Gateway`，应用日志正常 | SELinux 拦了 Nginx 到 8090 的连接 | `setsebool -P httpd_can_network_connect 1` |
| `ModuleNotFoundError: No module named 'routers'` | `PYTHONPATH` 少了 `/opt/fastapi/src` | 检查 systemd 单元的 `Environment=PYTHONPATH=...` |
| `ModuleNotFoundError: No module named 'src'` | `PYTHONPATH` 少了 `/opt/fastapi` | 同上 |
| 所有接口 `500`，日志见 `[Errno 2] No such file or directory: 'D:\\pyproject\\...'` | 代码仍是旧的硬编码路径 | 用最新代码重新发布（本次已修复） |
| `Can't connect to MySQL server` | RDS 白名单没加 ECS 内网 IP / 用了公网地址 | 白名单 + 内网地址 |
| `Authentication plugin 'caching_sha2_password' cannot be loaded` | 缺 `cryptography` | `pip install cryptography`（已加入 requirements） |
| 接口报「服务端未配置 DEEPSEEK_API_KEY」 | `.env` 没建或 Key 为空 | 检查 `/opt/fastapi/.env` 并重启服务 |
| 上传 Excel 返回 `413` | Nginx 请求体上限 | 确认 `client_max_body_size 50m` 已生效 |
| AI 对话超时 `504` | Nginx 读超时太短 | 确认 `proxy_read_timeout 300s` 已生效 |
| 浏览器 CORS 报错 | `CORS_ORIGINS` 没含前端来源 | 补上并 `systemctl restart fastapi` |
| `Permission denied` 读写配置 | 属主不对 | `chown -R fastapi:fastapi /opt/fastapi` |

---

## 11. 日常运维

```bash
# 实时日志
journalctl -u fastapi -f
# 最近 100 行
journalctl -u fastapi -n 100 --no-pager
# 重启 / 停止
systemctl restart fastapi
systemctl stop fastapi
# Nginx 日志
tail -f /var/log/nginx/fastapi.access.log
tail -f /var/log/nginx/fastapi.error.log
```

**更新代码**：本地重新打包上传 → 在服务器上再次执行 `sudo bash deploy/deploy.sh`。

**Gunicorn worker 数**：当前 2 个，经验值是 `CPU 核数 × 2 + 1`。2 核机器保持 2～4 即可，改 `/etc/systemd/system/fastapi.service` 的 `--workers` 后需 `systemctl daemon-reload && systemctl restart fastapi`。

---

## 12. 后续建议（本次未做）

1. **域名 + HTTPS**：现在只跑 HTTP，`X-API-Key` 是明文传输的。拿到域名并完成备案后，启用 `deploy/nginx-fastapi.conf` 里注释掉的 443 段，用 `certbot --nginx -d your.domain.com` 自动申请与续期证书。
2. **数据库连接复用**：`get_db_handler()` 目前是每个请求新建一条 MySQL 连接，并发上来会打爆 RDS 连接数上限。建议引入连接池（如 `DBUtils.PooledDB`）。
3. **备份**：RDS 开自动备份；ECS 上的 `/opt/fastapi` 建议打快照或纳入配置管理。
4. **日志轮转**：`journalctl` 默认按 journal 大小轮转，可在 `/etc/systemd/journald.conf` 设置 `SystemMaxUse=500M` 防止日志撑满磁盘。
