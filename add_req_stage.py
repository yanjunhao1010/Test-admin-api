# -*- coding: utf-8 -*-
"""需求分析三阶段改造：ai_requirements 补列 + 三张新表 + 字典种子

执行：python add_req_stage.py
幂等：可重复执行（ADD COLUMN / CREATE TABLE 均先查存在性）
"""
import sys
import pymysql
import yaml

cfg = yaml.safe_load(open('src/config/config.yml', encoding='utf-8'))['mysql']
conn = pymysql.connect(host=cfg['host'], user=cfg['user'],
                       password=cfg['password'], database=cfg['database'],
                       charset='utf8mb4')
cur = conn.cursor()


def has_col(table, col):
    cur.execute('SHOW COLUMNS FROM `%s`' % table)
    return col in [c[0] for c in cur.fetchall()]


def has_table(t):
    cur.execute("SHOW TABLES LIKE %s", (t,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------- 1. 补列
NEW_COLS = [
    ('stage', 'tinyint DEFAULT 0', '当前分析到的阶段：0未开始 1需求拆解 2测试点提取 3覆盖审计'),
    ('stage_payload', 'longtext', '三阶段产物 JSON：{decompose:{...},extract:{...},audit:{...}}'),
    ('coverage_stat', 'longtext', 'C01-C14 覆盖统计 JSON（含 na 明细与理由）'),
    ('open_questions', 'longtext', '所有[待确认]+[经验假设]条目 JSON，供一键发产品'),
    ('total_elapsed', 'int DEFAULT 0', '三阶段累计耗时（毫秒）'),
    ('stage_edited', 'tinyint DEFAULT 0', '阶段1 产物是否被人工编辑过：0否 1是'),
]
for col, ddl, comment in NEW_COLS:
    if has_col('ai_requirements', col):
        print('  skip  ai_requirements.%s 已存在' % col)
        continue
    cur.execute('ALTER TABLE ai_requirements ADD COLUMN `%s` %s COMMENT %s'
                % (col, ddl, "'%s'" % comment))
    print('  ADD   ai_requirements.%s' % col)

# ---------------------------------------------------------------- 2. 建表
TABLES = [
    ('ai_coverage_dims', """CREATE TABLE IF NOT EXISTS ai_coverage_dims (
  id          int unsigned NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  code        varchar(10)  NOT NULL COMMENT '维度编号，如C01',
  name        varchar(50)  NOT NULL COMMENT '维度名称，如正常流',
  prompt_hint varchar(500) NOT NULL COMMENT '给模型看的判断口径，避免空泛标注',
  sort        int          NOT NULL DEFAULT 0 COMMENT '排序号',
  PRIMARY KEY (id),
  UNIQUE KEY uk_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI需求分析C01-C14覆盖维度字典'"""),
    # 证据类型字典
    ('ai_evidence_types', """CREATE TABLE IF NOT EXISTS ai_evidence_types (
  id          int unsigned NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  code        varchar(20)  NOT NULL COMMENT '证据类型编码：basis/assumption/pending',
  label       varchar(20)  NOT NULL COMMENT '正文标记，如[需求依据]',
  color       varchar(20)  NOT NULL DEFAULT 'info' COMMENT '前端标签色：success/warning/danger/info',
  sort        int          NOT NULL DEFAULT 0 COMMENT '排序号',
  PRIMARY KEY (id),
  UNIQUE KEY uk_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI需求分析证据三分类字典（事实与猜测分离）'"""),
    # 阶段执行留痕
    ('ai_req_stage_runs', """CREATE TABLE IF NOT EXISTS ai_req_stage_runs (
  id             int unsigned NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  requirement_id int unsigned NOT NULL COMMENT '需求文档ID',
  stage          varchar(20)  NOT NULL COMMENT '阶段：decompose/extract/audit',
  status         tinyint      NOT NULL DEFAULT 0 COMMENT '0失败 1成功 2部分成功',
  blocks_total   int          NOT NULL DEFAULT 0 COMMENT '该阶段总块数',
  blocks_done    int          NOT NULL DEFAULT 0 COMMENT '该阶段完成块数',
  item_count     int          NOT NULL DEFAULT 0 COMMENT '产出的条目数（模块/测试点/缺口）',
  dropped_count  int          NOT NULL DEFAULT 0 COMMENT '被丢弃的条目数',
  model_id       int          DEFAULT NULL COMMENT '使用的模型ID',
  total_tokens   int          NOT NULL DEFAULT 0 COMMENT '消耗token总数',
  elapsed_ms     int          NOT NULL DEFAULT 0 COMMENT '本阶段耗时（毫秒）',
  error_msg      varchar(1000) DEFAULT NULL COMMENT '失败原因',
  run_by         int          DEFAULT NULL COMMENT '执行人用户ID',
  create_time    datetime     NOT NULL COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_req (requirement_id),
  KEY idx_stage (stage)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI需求分析三阶段执行留痕'"""),
]
for tname, ddl in TABLES:
    cur.execute(ddl)
    print('  TABLE %s' % tname)

# ---------------------------------------------------------------- 3. 字典种子
C01_C14 = [
    ('C01', '正常流', '合法输入下主流程能走通、结果符合预期。注意只标真正走通成功路径的点。'),
    ('C02', '异常流', '非法输入、依赖失败、超时、异常返回时系统的容错表现。'),
    ('C03', '边界值', '取临界值、刚好越界值、上下限、空值、极大极小值。'),
    ('C04', '权限控制', '未登录/角色不符/跨用户越权访问时是否被正确拦截。'),
    ('C05', '并发冲突', '多人同时操作、重复提交、并发写同一数据的一致性问题。'),
    ('C06', '重试机制', '失败后重试是否幂等、重试次数与间隔、退避策略。'),
    ('C07', '审计日志', '关键操作是否留痕、日志内容能否追溯操作人与时间。'),
    ('C08', '会话恢复', '登录态过期、进程中断、断网重连后能否恢复到正确状态。'),
    ('C09', '数据一致性', '跨表/跨服务写入是否最终一致，是否存在脏数据与补偿。'),
    ('C10', '上下游依赖', '上游调用失败、下游返回异常时本功能的降级表现。'),
    ('C11', '接口幂等', '同一请求重复发送是否只产生一次副作用（幂等键/去重）。'),
    ('C12', '输入校验', '前后端是否都做了校验、校验规则是否一致、能否被绕过。'),
    ('C13', '状态机流转', '对象在各状态间迁移的合法路径与非法迁移的拦截。'),
    ('C14', '安全合规', '敏感信息加密、脱敏、防重放、防篡改、越权与注入。'),
]
cur.execute('SELECT code FROM ai_coverage_dims')
exist = {c[0] for c in cur.fetchall()}
n = 0
for code, name, hint in C01_C14:
    if code in exist:
        continue
    cur.execute('INSERT INTO ai_coverage_dims (code, name, prompt_hint, sort) '
                'VALUES (%s, %s, %s, %s)', (code, name, hint, int(code[1:])))
    n += 1
print('  seed  ai_coverage_dims 新增 %d/%d 行' % (n, len(C01_C14)))

EVIDENCE = [
    ('basis', '[需求依据]', 'success', 1),
    ('assumption', '[经验假设]', 'warning', 2),
    ('pending', '[待确认]', 'danger', 3),
]
cur.execute('SELECT code FROM ai_evidence_types')
exist = {c[0] for c in cur.fetchall()}
n = 0
for code, label, color, s in EVIDENCE:
    if code in exist:
        continue
    cur.execute('INSERT INTO ai_evidence_types (code, label, color, sort) '
                'VALUES (%s, %s, %s, %s)', (code, label, color, s))
    n += 1
print('  seed  ai_evidence_types 新增 %d/3 行' % n)

conn.commit()
print('DONE')
conn.close()
