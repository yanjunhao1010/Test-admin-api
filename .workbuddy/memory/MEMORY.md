# 项目长期记忆 · Fast_api_project

## 项目定位
FastAPI 测试管理后台，模块包括测试用例、任务管理、RBAC 权限、AI 对话（DeepSeek）、Excel 上传。
前端是独立的 Vue 项目，通过 `X-API-Key` 请求头鉴权（密钥来自环境变量 `API_KEY`）。

## 部署约定
- 目标环境：阿里云 ECS（Alibaba Cloud Linux 3.2104）+ MySQL 8.0 + 公网 IP 直连 HTTP + systemd + Gunicorn
- 数据库两条路都留了口子，由 `config.yml` 的 `host` 决定：
  - **ECS 自建**：`host: 127.0.0.1`。安装与加固见 `deploy/DATABASE.md` / `deploy/install-mysql.sh`
    自建时务必 `bind-address = 127.0.0.1` 且**安全组不放行 3306**；备份要自己做 cron
  - **阿里云 RDS**：内网地址 + 白名单加 ECS 内网 IP。见 `deploy/DEPLOY.md` 第 2 章
- 部署根目录 `/opt/fastapi`；运行用户 `fastapi`（system 用户、nologin）
- 监听 `127.0.0.1:8090`，对外只经 Nginx 80 端口。**安全组不放行 8090**，否则绕过 Nginx 把应用裸暴露到公网
- Gunicorn 的 `PYTHONPATH` 必须同时包含 `/opt/fastapi` 和 `/opt/fastapi/src`
  —— `main.py` 用 `from routers import ...`（需要 src），各 router 用 `from src.comment import ...`（需要项目根），两套导入方式并存
- `src/config/config.yml` 与 `.env` 都被 `.gitignore` 忽略，必须在服务器上手工创建，不随代码下发
- 更新代码：重新打包上传后执行 `sudo bash deploy/deploy.sh`；完整手册见 `deploy/DEPLOY.md`
- 项目目录**不是 git 仓库**（无 `.git`），部署只能走 tar + scp / WinSCP 上传

## deploy/ 目录产物
- `DEPLOY.md` —— 部署主手册（安全组 / RDS / 系统初始化 / 发布 / Nginx / 11 项验收 / 报错对照）
- `DATABASE.md` —— 数据库指南：ECS 自建 MySQL 完整步骤 + RDS 对照 + 排错
- `deploy.sh` —— 幂等发布脚本（rsync → venv → 依赖 → 权限 → systemd → 自检）
- `install-mysql.sh` —— ECS 自建 MySQL 一键安装加固脚本（自动判断系统源/官方源）
- `fastapi.service` —— systemd 单元；`nginx-fastapi.conf` —— Nginx 反代配置
- `config.yml.example` / `env.production.example` —— 生产配置模板

## 技术约定
- 配置文件路径统一用 `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml')` 推导，**禁止硬编码绝对路径**（曾因此在 Linux 上全线失败）
- 环境变量统一由 `src/main.py` 的 `_load_env()` 零依赖加载，**必须放在所有 `from routers import ...` 之前**（`routers/AIS.py` 在导入期就会读 `DEEPSEEK_API_KEY`）
- 数据库层：pymysql + `DictCursor`，`DatabaseHandler` 支持 `with` 上下文；目前是每请求新建连接，未做连接池
- `src/config/` 下同时存在空的 `config.yaml` 和非空的 `config.yml`，代码读的是 **`config.yml`**
- 业务表：`test_cases`、`test_case_project`、`case_package`、`task_management`、`task_detail`、`zt_user`
  RBAC 表由 `python -m src.init_rbac` 幂等创建：`roles`、`permissions`、`user_roles`、`role_permissions`
