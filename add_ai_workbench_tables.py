"""AI 工作台 · 建表与初始化脚本（幂等，可重复执行）

包含四件事：
1. 建 7 张 AI 相关表（ai_models / ai_prompts / ai_logs / ai_requirements / ai_reports / ai_case_drafts / ai_settings）
2. 给 test_cases 加 ai_generated / ai_draft_id 两列（标记来源，支撑采纳率统计）
3. 预置 ai_settings 默认值与一条全局 DeepSeek 模型（key 取自 .env 的 DEEPSEEK_API_KEY，Fernet 加密入库）
4. 落库 5 个 AI 权限点并授权

用法：python add_ai_workbench_tables.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')
ENV_PATH = os.path.join(BASE, '.env')

# ---------------------------------------------------------------------------
# 表 DDL（建表规范：所有字段必须带中文 COMMENT）
# ---------------------------------------------------------------------------
TABLES = {}

TABLES['ai_models'] = """
CREATE TABLE IF NOT EXISTS ai_models (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '模型配置ID',
  name          VARCHAR(64)  NOT NULL COMMENT '显示名称，如 DeepSeek 通用',
  provider      VARCHAR(32)  NOT NULL DEFAULT 'custom' COMMENT '供应商标记：deepseek/openai/qwen/zhipu/hunyuan/custom',
  base_url      VARCHAR(255) NOT NULL COMMENT 'OpenAI兼容接口地址，含/v1，如 https://api.deepseek.com/v1',
  api_key_enc   VARCHAR(1024) NOT NULL COMMENT '加密后的API Key（Fernet对称加密，密钥为环境变量 AI_SECRET_KEY）',
  model_name    VARCHAR(100) NOT NULL COMMENT '实际下发给API的模型名，如 deepseek-chat',
  model_type    VARCHAR(16)  NOT NULL DEFAULT 'chat' COMMENT '模型类型：chat通用对话 / reasoner推理',
  owner_user_id INT          NOT NULL DEFAULT 0 COMMENT '归属用户ID：0=全局模型(所有人可用) 其他=个人模型(仅本人可用)',
  scope         TINYINT      NOT NULL DEFAULT 0 COMMENT '可见范围：0全局 1仅本人（与owner_user_id同步维护便于索引）',
  capabilities  VARCHAR(500) NULL COMMENT '能力探测结果(JSON)：json_object等',
  temperature   DECIMAL(3,2) NOT NULL DEFAULT 0.30 COMMENT '默认温度（reasoner类型不下发）',
  max_tokens    INT UNSIGNED NULL COMMENT '单次最大输出token，空则用上游默认',
  timeout       INT UNSIGNED NOT NULL DEFAULT 120 COMMENT '单次请求超时秒数',
  priority      TINYINT UNSIGNED NOT NULL DEFAULT 10 COMMENT '同归属内容错链顺序，数字小的先用',
  is_default    TINYINT      NOT NULL DEFAULT 0 COMMENT '是否该归属范围内的默认模型（全局/每个用户各至多一个）',
  enabled       TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用：1启用 0停用',
  last_test_time DATETIME    NULL COMMENT '最近一次连通性测试时间',
  last_test_ok  TINYINT      NULL COMMENT '最近一次测试是否通过：1通过 0失败 NULL未测',
  last_test_msg VARCHAR(500) NULL COMMENT '最近一次测试结果信息（耗时/错误原因）',
  remark        VARCHAR(255) NULL COMMENT '备注',
  create_by     INT          NULL COMMENT '创建人用户ID',
  create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  update_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  is_del        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除：0未删 1已删',
  PRIMARY KEY (id),
  KEY idx_scope_default (scope, owner_user_id, is_default, enabled)
) COMMENT 'AI模型配置（模型中心），支持全局/个人两层与主备容错'
"""

TABLES['ai_prompts'] = """
CREATE TABLE IF NOT EXISTS ai_prompts (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '提示词模板ID',
  code          VARCHAR(64)  NOT NULL COMMENT '模板编码，如 case_gen / exec_summary',
  name          VARCHAR(100) NOT NULL COMMENT '模板名称',
  model_type    VARCHAR(16)  NOT NULL DEFAULT 'chat' COMMENT '推荐模型类型：chat / reasoner',
  version       INT          NOT NULL DEFAULT 1 COMMENT '版本号，同code可多版本',
  temperature   DECIMAL(3,2) NOT NULL DEFAULT 0.30 COMMENT '建议温度',
  system_prompt TEXT         NULL COMMENT '系统提示词',
  user_prompt_template TEXT  NULL COMMENT '用户提示词模板，变量用 {{变量名}} 占位',
  output_schema TEXT         NULL COMMENT '期望输出结构说明(JSON)，会拼进提示词并用于服务端校验',
  description   VARCHAR(255) NULL COMMENT '用途说明',
  enabled       TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用：1启用 0停用',
  create_by     INT          NULL COMMENT '创建人用户ID',
  create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  update_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  is_del        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除：0未删 1已删',
  PRIMARY KEY (id),
  UNIQUE KEY uk_code_version (code, version)
) COMMENT 'AI提示词模板，支持版本化与运营在线修改'
"""

TABLES['ai_logs'] = """
CREATE TABLE IF NOT EXISTS ai_logs (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '日志ID',
  model_id      INT          NULL COMMENT '使用的模型ID，关联 ai_models.id',
  model_name    VARCHAR(100) NULL COMMENT '冗余模型名，便于模型被删后仍可读',
  owner_user_id INT          NOT NULL DEFAULT 0 COMMENT '模型归属用户ID：0=全局额度 其他=个人额度',
  run_by        INT          NULL COMMENT '调用人用户ID，关联 users.id',
  biz_type      VARCHAR(32)  NULL COMMENT '业务类型：case_gen/exec_summary/req_analyse/case_review/model_test 等',
  prompt_code   VARCHAR(64)  NULL COMMENT '使用的提示词模板编码',
  prompt_tokens INT          NULL COMMENT '输入token数',
  completion_tokens INT      NULL COMMENT '输出token数',
  total_tokens  INT          NULL COMMENT '总token数',
  elapsed_ms    INT          NULL COMMENT '总耗时(毫秒)',
  attempt       TINYINT      NOT NULL DEFAULT 1 COMMENT '第几次尝试(含校验失败重试)',
  schema_valid  TINYINT      NULL COMMENT '结构化输出校验：1通过 0失败 NULL未校验',
  http_code     INT          NULL COMMENT '上游HTTP状态码，异常时为0',
  success       TINYINT      NOT NULL DEFAULT 0 COMMENT '本次调用是否成功：1成功 0失败',
  error_msg     VARCHAR(1000) NULL COMMENT '失败原因（已过滤密钥）',
  create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_user_time (run_by, create_time),
  KEY idx_model (model_id)
) COMMENT 'AI调用日志，支撑用量统计与成本归因'
"""

TABLES['ai_requirements'] = """
CREATE TABLE IF NOT EXISTS ai_requirements (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '需求文档ID',
  project_id    INT          NULL COMMENT '关联项目ID(test_case_project.id)，可空',
  title         VARCHAR(200) NOT NULL COMMENT '需求标题/文档名',
  req_type      TINYINT      NOT NULL DEFAULT 1 COMMENT '来源类型：1文档上传 2纯文本粘贴 3图片(预留)',
  source_files  TEXT         NULL COMMENT '原始文件名列表(JSON数组)',
  content_text  LONGTEXT     NULL COMMENT '解析后的纯文本正文',
  analyse_result LONGTEXT    NULL COMMENT 'AI分析结果(JSON)',
  status        TINYINT      NOT NULL DEFAULT 0 COMMENT '状态：0待分析 1分析中 2已完成 3失败',
  error_msg     VARCHAR(1000) NULL COMMENT '分析失败原因',
  run_by        INT          NULL COMMENT '创建/上传人用户ID',
  create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  update_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  is_del        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除：0未删 1已删',
  PRIMARY KEY (id),
  KEY idx_project (project_id)
) COMMENT 'AI需求文档，存原文与解析结果'
"""

TABLES['ai_reports'] = """
CREATE TABLE IF NOT EXISTS ai_reports (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '报告ID',
  report_type   VARCHAR(32)  NOT NULL COMMENT '报告类型：exec_summary/req_analyse/case_review',
  title         VARCHAR(200) NOT NULL COMMENT '报告标题',
  biz_type      VARCHAR(32)  NULL COMMENT '业务对象类型：batch/interface/requirement/case',
  biz_id        VARCHAR(64)  NULL COMMENT '业务对象ID（批次号/接口id/需求id等）',
  content_md    LONGTEXT     NULL COMMENT '报告正文(Markdown)',
  summary_json  TEXT         NULL COMMENT '结构化摘要(JSON)，便于列表展示',
  model_id      INT          NULL COMMENT '生成所用模型ID',
  run_by        INT          NULL COMMENT '生成人用户ID',
  create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  is_del        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除：0未删 1已删',
  PRIMARY KEY (id),
  KEY idx_type_time (report_type, create_time)
) COMMENT 'AI报告存档，支持导出'
"""

TABLES['ai_case_drafts'] = """
CREATE TABLE IF NOT EXISTS ai_case_drafts (
  id              INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '草稿ID',
  batch_id        VARCHAR(40)  NOT NULL COMMENT '生成批次号（一次生成共享）',
  source_type     TINYINT      NOT NULL DEFAULT 1 COMMENT '来源：1AI生成用例 2需求分析产出 3用例体检补全 4用例互转',
  source_id       INT          NULL COMMENT '来源业务ID（ai_requirements.id / ai_reports.id 等）',
  suggest_project_id INT       NULL COMMENT 'AI建议的项目ID(test_case_project三级节点)，可空',
  module          VARCHAR(255) NULL COMMENT '模块',
  function_point  VARCHAR(512) NULL COMMENT '功能点',
  priority        TINYINT      NOT NULL DEFAULT 2 COMMENT '优先级 1-低 2-中 3-高（与 test_cases 对齐）',
  case_type       VARCHAR(32)  NULL COMMENT '用例类型：正向/异常/边界/权限/兼容',
  pre_condition   TEXT         NULL COMMENT '前置条件',
  case_steps      TEXT         NULL COMMENT '操作步骤',
  expected_result TEXT         NULL COMMENT '预期结果',
  remark          TEXT         NULL COMMENT '备注/来源说明',
  confidence      DECIMAL(3,2) NULL COMMENT 'AI自评置信度 0~1',
  dup_case_id     INT          NULL COMMENT '与库内疑似重复的用例ID，用于去重提示',
  status          TINYINT      NOT NULL DEFAULT 0 COMMENT '状态：0草稿 1已采纳 2已丢弃',
  adopt_case_id   INT          NULL COMMENT '采纳后生成的 test_cases.id（溯源）',
  adopt_project_id INT         NULL COMMENT '采纳时选择的项目ID',
  model_id        INT          NULL COMMENT '生成所用模型ID',
  run_by          INT          NULL COMMENT '生成人用户ID',
  adopt_by        INT          NULL COMMENT '采纳/丢弃操作人用户ID',
  create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  adopt_time      DATETIME     NULL COMMENT '采纳或丢弃时间',
  PRIMARY KEY (id),
  KEY idx_batch (batch_id),
  KEY idx_status_user (status, run_by),
  KEY idx_project (suggest_project_id)
) COMMENT 'AI用例库草稿池，人工确认后才写入 test_cases'
"""

TABLES['ai_settings'] = """
CREATE TABLE IF NOT EXISTS ai_settings (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  cfg_key      VARCHAR(64)  NOT NULL COMMENT '配置键，如 daily_call_limit',
  cfg_value    TEXT         NULL COMMENT '配置值（字符串；结构型配置存JSON）',
  remark       VARCHAR(255) NULL COMMENT '说明',
  update_by    INT          NULL COMMENT '最后修改人用户ID',
  update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_key (cfg_key)
) COMMENT 'AI全局配置（配额上限、脱敏开关等）'
"""

TABLES['ai_inflight'] = """
CREATE TABLE IF NOT EXISTS ai_inflight (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  biz_type   VARCHAR(40)  NOT NULL COMMENT '业务类型：case_gen/exec_summary/case_review/task_summary/data_gen/coverage/weekly_report/assistant/prompt_run/req_analyse/model_test 等',
  owner_uid  INT          NOT NULL COMMENT '发起任务的用户ID',
  ref_id     VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '业务对象ID（需求id/模型id等；无则空）',
  started_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '登记时间',
  create_time DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_biz_uid_ref (biz_type, owner_uid, ref_id),
  KEY idx_uid (owner_uid)
) COMMENT 'AI在途任务锁：登记正在进行的AI调用，防重复提交并支撑前端刷新后禁用按钮'
"""

# test_cases 增列：AI 来源标记
COLUMNS = [
    ("ai_generated", "TINYINT NOT NULL DEFAULT 0 COMMENT '是否AI生成：1是 0否'"),
    ("ai_draft_id", "INT NULL COMMENT '来源AI草稿ID，关联 ai_case_drafts.id'"),
]

# ai_settings 默认值
DEFAULT_SETTINGS = [
    ('per_call_max_tokens', '8192', '单次AI输出token上限（超限直接拒绝调用）'),
    ('daily_call_limit', '200', '每个用户每日AI调用次数上限（0表示不限制）'),
    ('ai_mask_enabled', '1', '外发脱敏开关：1开启 0关闭'),
    ('ai_mask_refill', '0', '脱敏占位符是否在结果中回填：1回填 0不回填（默认不回填，安全优先）'),
    ('ai_mask_rules', '', '自定义脱敏正则(JSON数组，每项{p:正则,r:替换})，空则用内置规则'),
]

# 权限点：(code, name, type, remark)
PERMISSIONS = [
    ('menu:ai', 'AI工作台', 1, 'AI工作台一级菜单'),
    ('menu:ai:model', '全局模型管理', 1, '模型中心-全局模型管理'),
    ('menu:ai:prompt', '提示词管理', 1, 'AI提示词模板管理'),
    ('menu:ai:log', 'AI调用日志', 1, 'AI调用日志与成本统计'),
    ('menu:ai:setting', 'AI配额与脱敏', 1, 'AI配额上限与脱敏开关'),
]

# 角色授权：admin 全给；测试/开发/普通用户给一级菜单（可用 AI 工作台、可配自己的模型）
ROLE_GRANTS = {
    1: ['menu:ai', 'menu:ai:model', 'menu:ai:prompt', 'menu:ai:log', 'menu:ai:setting'],
    12: ['menu:ai'],
    10: ['menu:ai'],
    11: ['menu:ai'],
}


def read_env_file():
    env = {}
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def ensure_secret_key():
    """确保 .env 里有 AI_SECRET_KEY，没有则生成并追加"""
    from cryptography.fernet import Fernet

    env = read_env_file()
    key = env.get('AI_SECRET_KEY')
    if key:
        print('OK   AI_SECRET_KEY 已存在')
        return key
    key = Fernet.generate_key().decode()
    with open(ENV_PATH, 'a', encoding='utf-8') as f:
        f.write(f'\n# AI工作台密钥加密用（Fernet）。换掉此值会导致库中已存的 api_key 全部无法解密\nAI_SECRET_KEY={key}\n')
    print('NEW  已生成 AI_SECRET_KEY 并写入 .env')
    return key


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()

    # 1. 建表
    for name, ddl in TABLES.items():
        try:
            db.execute_update(ddl)
            print(f'OK   建表 {name}')
        except Exception as e:
            print(f'ERR  建表 {name} 失败: {e}')
            raise

    # 2. test_cases 增列
    for col, defn in COLUMNS:
        try:
            db.execute_update(f'ALTER TABLE test_cases ADD COLUMN {col} {defn}')
            print(f'OK   已加列 test_cases.{col}')
        except Exception as e:
            msg = str(e)
            if 'Duplicate column' in msg or '1060' in msg:
                print(f'SKIP test_cases.{col} 已存在')
            else:
                print(f'ERR  test_cases.{col}: {msg}')
                raise

    # 3. ai_settings 默认值
    for k, v, remark in DEFAULT_SETTINGS:
        try:
            db.execute_update(
                'INSERT IGNORE INTO ai_settings (cfg_key, cfg_value, remark) VALUES (%s, %s, %s)',
                (k, v, remark),
            )
        except Exception as e:
            print(f'WARN ai_settings {k}: {e}')
    print(f'OK   ai_settings 预置 {len(DEFAULT_SETTINGS)} 项')

    # 4. 预置全局 DeepSeek 模型（仅在表为空时）
    try:
        rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_models')
        n = rows[0]['n'] if rows else 0
        if n == 0:
            env = read_env_file()
            ds_key = env.get('DEEPSEEK_API_KEY') or os.getenv('DEEPSEEK_API_KEY') or ''
            if ds_key:
                from cryptography.fernet import Fernet
                secret = ensure_secret_key()
                enc = Fernet(secret.encode()).encrypt(ds_key.encode()).decode()
                db.execute_update(
                    """INSERT INTO ai_models
                       (name, provider, base_url, api_key_enc, model_name, model_type,
                        owner_user_id, scope, temperature, max_tokens, timeout, priority,
                        is_default, enabled, remark, create_by)
                       VALUES (%s,%s,%s,%s,%s,%s,0,0,0.30,8192,120,1,1,1,%s,1)""",
                    ('DeepSeek 通用', 'deepseek', 'https://api.deepseek.com/v1', enc,
                     'deepseek-chat', 'chat', '系统预置：取自 .env 的 DEEPSEEK_API_KEY，可在模型中心修改'),
                )
                print('OK   已预置全局默认模型：DeepSeek 通用（deepseek-chat）')
            else:
                print('WARN .env 未配置 DEEPSEEK_API_KEY，跳过预置模型；请到模型中心手动添加')
        else:
            print(f'SKIP ai_models 已有 {n} 条记录，跳过预置')
    except Exception as e:
        print(f'WARN 预置模型失败（不阻塞建表）: {e}')

    # 5. 权限点 + 授权
    perm_ids = {}
    for code, name, ptype, remark in PERMISSIONS:
        try:
            rows = db.execute_query('SELECT id FROM permissions WHERE code = %s', (code,))
            if rows:
                perm_ids[code] = rows[0]['id']
                print(f'SKIP 权限点 {code} 已存在（id={rows[0]["id"]}）')
                continue
            parent_id = perm_ids.get('menu:ai', 0) if code != 'menu:ai' else 0
            # 注意：permissions 表没有 remark/description 列，不要多传字段
            db.execute_update(
                'INSERT INTO permissions (parent_id, name, code, type, path, icon, sort) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (parent_id, name, code, ptype, '', 'brain' if code == 'menu:ai' else '', 90),
            )
            rows = db.execute_query('SELECT id FROM permissions WHERE code = %s', (code,))
            perm_ids[code] = rows[0]['id']
            print(f'OK   新增权限点 {code}（id={perm_ids[code]}）')
        except Exception as e:
            print(f'ERR  权限点 {code}: {e}')

    for role_id, codes in ROLE_GRANTS.items():
        for code in codes:
            pid = perm_ids.get(code)
            if not pid:
                continue
            try:
                rows = db.execute_query(
                    'SELECT id FROM role_permissions WHERE role_id = %s AND permission_id = %s',
                    (role_id, pid),
                )
                if rows:
                    continue
                db.execute_update(
                    'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)',
                    (role_id, pid),
                )
                print(f'OK   授权 role_id={role_id} <- {code}')
            except Exception as e:
                print(f'WARN 授权 role_id={role_id} {code}: {e}')

    db.close()
    print('\nAI 工作台建表与初始化完成 ✔')


if __name__ == '__main__':
    main()
