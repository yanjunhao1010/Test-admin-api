# -*- coding: utf-8 -*-
"""接口用例 AI 生成 —— 表结构迁移（幂等，可重复执行）。

对应方案 D:\workbuddy_file\api-case-generation-plan.md 的 P0 步骤 1。

改动内容：
1. ai_coverage_dims              补 I 组 **7 行**（I01/I02/I03/I04/I06/I07/I08）
                                  并清理 I05 残留（D1 决策 C：本期不做越权维度）
2. ai_api_case_history           新建：接口用例生成记录（独立表，不污染 ai_case_gen_history）
3. ai_api_case_history_item      新建：生成条目明细（每条一个可执行场景）

⚠️ I05「身份与越权」本期**不写入**（D1 决策 C）：
   执行引擎的请求头合并是 `{**env_headers, **iface_headers}`（automation.py:638-640），
   而 api_environments.headers 里存着真实 X-Token，场景级 headers 只能覆盖不能删除。
   强行生成「不带 token」的越权场景会带着有效 token 拿到 200 → 断言通过 → **假通过**。
   sort 105 空位保留给 P2 的越权能力，届时原样复用该编码，历史数据维度口径不失效。

⚠️ 不改动 api_interfaces / test_cases / ai_case_drafts / automation.py（决策①②④）。

每一步都先判断是否已存在，已存在则跳过，重复执行安全。
所有新增表与列都带中文 COMMENT。
"""
import os
import sys

import pymysql
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'src', 'config', 'config.yml')

# ---------------------------------------------------------------------------
# I 组覆盖维度（本期 7 条，不含 I05）
# code, name, prompt_hint, sort
# sort 105 刻意留空：P2 落地 I05「身份与越权」时复用，避免历史数据编码失效
# ---------------------------------------------------------------------------
API_DIM_ROWS = [
    ('I01', '正常调用',
     '合法入参下 HTTP状态码 + 业务码 + 关键业务字段全部符合预期。只标真正走通成功路径的点。', 101),
    ('I02', '必填缺失',
     '必填参数不传/传null/传空串/只传一半，服务端是否拦截并返回明确错误码。'
     '场景 params 里用 null 表示该键不传（不要用空字符串代替）。', 102),
    ('I03', '类型非法',
     '数字字段传字符串、布尔传非0/1、数组传对象、JSON结构错位，是否被正确拒绝。', 103),
    ('I04', '边界值',
     '数值上下限、字符串最小最大长度、数组条数边界、分页pageNo/pageSize极值与0、负数、超大值。', 104),
    ('I06', '幂等与重复',
     '同一请求重复调用、并发调用同一资源，结果是否一致（无重复记录、金额不重复扣）。'
     '**只对GET/HEAD接口分配该维度**，写接口重复提交会产生脏数据。', 106),
    ('I07', '依赖与降级',
     '依赖变量未获得、上游异常、下游超时或5xx时，本接口是明确失败还是静默降级或产生脏数据。'
     '接口无{{var}}占位且无extracts提取规则时不要硬造该维度。', 107),
    ('I08', '响应契约',
     '必返字段是否都在、字段类型是否稳定、错误码是否可区分、'
     '敏感字段（手机号/身份证/token）是否脱敏外发。断言优先用 json_path 深断言。', 108),
]

#: 本期禁止写入的维度（D1 决策 C）。脚本会主动清理，防止调试残留污染维度表
FORBIDDEN_API_DIM = 'I05'

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS `ai_api_case_history` (
  `id`            int unsigned NOT NULL AUTO_INCREMENT COMMENT '生成记录ID',
  `batch_id`      varchar(40)  NOT NULL COMMENT '生成批次号（一次生成共享，便于前端聚合）',
  `interface_id`  int          NOT NULL COMMENT '来源接口ID(api_interfaces.id)',
  `iface_name`    varchar(100) DEFAULT '' COMMENT '接口名称(快照)',
  `iface_group`   varchar(100) DEFAULT '' COMMENT '接口分组(快照)',
  `method`        varchar(10)  DEFAULT '' COMMENT 'HTTP方法(快照)',
  `url`           varchar(500) DEFAULT '' COMMENT '接口地址(快照)',
  `probe_mode`    varchar(16)  DEFAULT 'none' COMMENT '取证方式：live本次真实试跑/history复用最近执行记录/none无响应仅静态推断',
  `probe_exec_id` int          DEFAULT NULL COMMENT '取证所用的执行记录ID(api_executions.id)',
  `probe_size`    int          DEFAULT 0 COMMENT '取证响应原始字节数',
  `probe_sent`    int          DEFAULT 0 COMMENT '骨架化+截断后实际送模型的字节数',
  `probe_masked`  int          DEFAULT 0 COMMENT '响应脱敏命中处数',
  `probe_truncated` tinyint    DEFAULT 0 COMMENT '响应是否被截断(1是 0否)',
  `model_id`      int          DEFAULT NULL COMMENT '生成所用模型ID(ai_models.id)',
  `model_name`    varchar(100) DEFAULT '' COMMENT '模型名(快照)',
  `scene_count`   int          DEFAULT 0 COMMENT '本次生成场景(用例)条数',
  `dim_count`     int          DEFAULT 0 COMMENT '命中的覆盖维度数',
  `dup_dropped`   int          DEFAULT 0 COMMENT '去重丢弃条数',
  `applied`       tinyint      DEFAULT 0 COMMENT '是否已落地到接口dataset：1是 0否',
  `applied_at`    datetime     DEFAULT NULL COMMENT '落地时间',
  `apply_mode`    varchar(16)  DEFAULT '' COMMENT '落地方式：append追加/replace替换/fill_empty仅补空',
  `elapsed_ms`    int          DEFAULT 0 COMMENT '生成耗时(毫秒)',
  `calls_used`    int          DEFAULT 0 COMMENT '本次消耗的模型调用次数',
  `attempt`       tinyint      DEFAULT 1 COMMENT '第几次尝试(含校验失败重试)',
  `error_msg`     varchar(1000) DEFAULT '' COMMENT '失败原因',
  `create_by`     int          DEFAULT 0 COMMENT '生成人用户ID',
  `create_time`   datetime     DEFAULT NULL COMMENT '生成时间',
  PRIMARY KEY (`id`),
  KEY `idx_iface` (`interface_id`),
  KEY `idx_batch` (`batch_id`),
  KEY `idx_creator_time` (`create_by`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='接口用例AI生成记录'
"""

ITEM_DDL = """
CREATE TABLE IF NOT EXISTS `ai_api_case_history_item` (
  `id`            int unsigned NOT NULL AUTO_INCREMENT COMMENT '生成用例条目ID',
  `history_id`    int          NOT NULL COMMENT '关联 ai_api_case_history.id',
  `seq`           int          DEFAULT 0 COMMENT '生成时顺序号',
  `scene_name`    varchar(100) DEFAULT '' COMMENT '场景名称(写回dataset的name，同时作为case_tag)',
  `case_tag`      varchar(100) DEFAULT '' COMMENT '用例标识，建议格式 维度_类型_序号，如 I04_边界_1',
  `dims`          varchar(64)  DEFAULT '' COMMENT '覆盖维度(I组)，多值逗号分隔，取值 I01/I02/I03/I04/I06/I07/I08（I05越权本期不做）',
  `severity`      varchar(8)   DEFAULT 'S2' COMMENT '严重度：S0阻断/S1严重/S2一般/S3轻微',
  `evidence_type` varchar(16)  DEFAULT 'assumption' COMMENT '证据类型：basis/assumption/pending',
  `case_type`     varchar(32)  DEFAULT '' COMMENT '用例类型：正向/异常/边界/契约',
  `design_method` varchar(64)  DEFAULT '' COMMENT '设计方法：等价类划分法/边界值分析法/错误推测法/判定表法等',
  `params_json`   text COMMENT '场景参数覆盖(JSON对象，值为null表示该键不传)',
  `body_json`     text COMMENT '场景请求体覆盖(JSON，可空)',
  `headers_json`  text COMMENT '场景请求头覆盖(JSON，可空；引擎无法从环境头移除X-Token，故本期不生成越权头)',
  `asserts_json`  text COMMENT '场景级断言数组JSON：[{assert_type,expected_value,enabled}]，覆盖接口级断言',
  `pre_condition` text COMMENT '前置条件(可读描述，含所需环境与依赖变量)',
  `steps`         text COMMENT '执行步骤(可读描述)',
  `expected`      text COMMENT '预期结果(可读描述)',
  `note`          text COMMENT '依据不足说明(evidence_type 为 assumption/pending 时必填)',
  `confidence`    decimal(3,2) DEFAULT 0.00 COMMENT 'AI自评置信度 0~1',
  `applied`       tinyint      DEFAULT 0 COMMENT '是否已写入接口dataset',
  `create_time`   datetime     DEFAULT NULL COMMENT '生成时间',
  PRIMARY KEY (`id`),
  KEY `idx_history` (`history_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='接口用例AI生成条目'
"""


def _table_exists(cur, table):
    cur.execute("SHOW TABLES LIKE %s", (table,))
    return bool(cur.fetchone())


def _columns(cur, table):
    cur.execute('SHOW FULL COLUMNS FROM `%s`' % table)
    return cur.fetchall()


def _ensure_columns(cur, table, cols):
    """幂等补列：cols = [(name, "VARCHAR(16) NULL COMMENT '...'", after)]"""
    have = {r['Field'] for r in _columns(cur, table)}
    after = None
    for name, ddl, after in cols:
        if name in have:
            print('  [跳过] %s.%s 已存在' % (table, name))
            continue
        sql = "ALTER TABLE `%s` ADD COLUMN `%s` %s" % (table, name, ddl)
        if after:
            sql += " AFTER `%s`" % after
        cur.execute(sql)
        print('  [新增] %s.%s' % (table, name))


def main():
    cfg = yaml.safe_load(open(CFG, encoding='utf-8'))['mysql']
    conn = pymysql.connect(host=cfg['host'], user=cfg['user'], password=cfg['password'],
                           database=cfg['database'], charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    # ------------------------------------------------------------------
    print('=== 1) ai_coverage_dims 补 I 组 7 行 ===')
    cur.execute('SELECT id FROM ai_coverage_dims WHERE code = %s', (FORBIDDEN_API_DIM,))
    if cur.fetchone():
        # D1 决策 C：清掉 P0 开发前可能写入的 I05 调试残留
        cur.execute('DELETE FROM ai_coverage_dims WHERE code = %s', (FORBIDDEN_API_DIM,))
        print('  [清理] %s 已删除（本期不做越权维度，D1 决策 C）' % FORBIDDEN_API_DIM)
    else:
        print('  [跳过] %s 本就不存在，无需清理' % FORBIDDEN_API_DIM)

    for code, name, hint, sort_no in API_DIM_ROWS:
        cur.execute('SELECT id FROM ai_coverage_dims WHERE code = %s', (code,))
        if cur.fetchone():
            cur.execute('UPDATE ai_coverage_dims SET name=%s, prompt_hint=%s, sort=%s '
                        'WHERE code=%s', (name, hint, sort_no, code))
            print('  [更新] %s %s' % (code, name))
        else:
            cur.execute('INSERT INTO ai_coverage_dims (code, name, prompt_hint, sort) '
                        'VALUES (%s, %s, %s, %s)', (code, name, hint, sort_no))
            print('  [新增] %s %s' % (code, name))

    # ------------------------------------------------------------------
    print('=== 2) ai_api_case_history 建表 ===')
    if _table_exists(cur, 'ai_api_case_history'):
        print('  [跳过] 表已存在')
    else:
        cur.execute(HISTORY_DDL)
        print('  [新增] 表 ai_api_case_history')

    # 后补列（P0 迭代过程中加的字段，保证老库升级后也能对齐）
    _ensure_columns(cur, 'ai_api_case_history', [
        ('probe_sent', "INT NULL DEFAULT 0 COMMENT '骨架化+截断后实际送模型的字节数'", 'probe_size'),
        ('probe_masked', "INT NULL DEFAULT 0 COMMENT '响应脱敏命中处数'", 'probe_sent'),
        ('calls_used', "INT NULL DEFAULT 0 COMMENT '本次消耗的模型调用次数'", 'elapsed_ms'),
        ('apply_mode', "VARCHAR(16) NULL DEFAULT '' COMMENT '落地方式：append追加/replace替换/fill_empty仅补空'", 'applied_at'),
    ])

    print('=== 3) ai_api_case_history_item 建表 ===')
    if _table_exists(cur, 'ai_api_case_history_item'):
        print('  [跳过] 表已存在')
    else:
        cur.execute(ITEM_DDL)
        print('  [新增] 表 ai_api_case_history_item')

    _ensure_columns(cur, 'ai_api_case_history_item', [
        ('case_tag', "VARCHAR(100) NULL DEFAULT '' COMMENT '用例标识，建议格式 维度_类型_序号'", 'scene_name'),
    ])

    conn.commit()

    # ------------------------------------------------------------------
    print('=== 回读校验 ===')
    cur.execute("SELECT code, name, sort FROM ai_coverage_dims ORDER BY sort")
    rows = cur.fetchall()
    codes = [r['code'] for r in rows]
    print('  ai_coverage_dims 共 %d 行: %s' % (len(rows), ','.join(codes)))
    i_rows = [r for r in rows if r['code'].startswith('I')]
    print('  I 组 %d 条: %s' % (len(i_rows), ','.join(r['code'] for r in i_rows)))
    print('  I05 是否存在: %s（必须为否）' % (FORBIDDEN_API_DIM in codes))
    sorts = sorted(int(r['sort']) for r in i_rows)
    print('  I 组 sort: %s（105 应为空位）' % sorts)

    for t in ('ai_api_case_history', 'ai_api_case_history_item'):
        ok = _table_exists(cur, t)
        print('  %s: %s' % (t, 'OK' if ok else '缺失！'))
        if ok:
            n = len(_columns(cur, t))
            cur.execute('SELECT COUNT(*) AS n FROM `%s`' % t)
            print('    列数 %d，行数 %d' % (n, cur.fetchone()['n']))

    # 未改动的表，确认零变更
    cur.execute('SELECT COUNT(*) AS n FROM ai_coverage_dims WHERE code = %s',
                (FORBIDDEN_API_DIM,))
    assert cur.fetchone()['n'] == 0, 'I05 未清理干净'

    conn.close()
    print('\n迁移完成。api_interfaces / test_cases / ai_case_drafts 未做任何改动。')


if __name__ == '__main__':
    sys.exit(main())