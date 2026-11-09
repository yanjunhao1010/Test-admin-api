# -*- coding: utf-8 -*-
"""case_gen 覆盖率提升 —— 表结构迁移（幂等，可重复执行）。

改动内容（方案 v2 决策①②③④⑤ 的落地）：
1. ai_case_drafts            +dims +routes +evidence_type +severity +索引
2. ai_case_gen_history_item  +dims +routes +severity +evidence_type
3. ai_coverage_dims          补 R1~R5 五行（风险路线 5 段）
4. test_cases                **不动**（决策④）

每一步都先判断是否已存在，已存在则跳过，因此重复执行安全。
所有新增列都带中文 COMMENT。
"""
import os
import sys

import yaml
import pymysql

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'src', 'config', 'config.yml')

ROUTE_ROWS = [
    ('R1', '进入与展示', '入口/页面/列表是否完整可达，有无错位、多余元素、加载失败；空态与首次进入是否正常。', 15),
    ('R2', '信息输入', '输入是否被正确接收；长度/格式/字符集约束是否生效；敏感信息是否脱敏、前后端校验是否一致。', 16),
    ('R3', '系统校验', '服务端校验逻辑是否正确、安全、不可被前端绕过；校验顺序与提示是否合理。', 17),
    ('R4', '结果反馈', '成功与失败的提示是否可理解、可恢复、无敏感信息泄漏；错误码是否可区分；重复提交是否有反馈。', 18),
    ('R5', '状态保持与退出', '会话/登录态/草稿状态是否一致；中断(弱网/退出/刷新)后能否恢复；能否安全退出并清理痕迹。', 19),
]

DRAFT_COLS = [
    ('dims', "VARCHAR(64) NULL COMMENT '覆盖维度(C组)，多值逗号分隔，取值 C01~C14；R组见 routes 列'"),
    ('routes', "VARCHAR(32) NULL COMMENT '风险路线(R组)，多值逗号分隔，取值 R1~R5（用户路径五段）'"),
    ('evidence_type', "VARCHAR(16) NULL COMMENT '证据类型：basis需求依据/assumption经验假设/pending待确认'"),
    ('severity', "VARCHAR(8) NULL COMMENT '严重度：S0阻断/S1严重/S2一般/S3轻微；priority 列存映射后 1/2/3'"),
]

HISTORY_COLS = [
    ('dims', "VARCHAR(64) NULL COMMENT '覆盖维度(C组)，多值逗号分隔，取值 C01~C14'"),
    ('routes', "VARCHAR(32) NULL COMMENT '风险路线(R组)，多值逗号分隔，取值 R1~R5'"),
    ('severity', "VARCHAR(8) NULL COMMENT '严重度：S0阻断/S1严重/S2一般/S3轻微'"),
    ('evidence_type', "VARCHAR(16) NULL COMMENT '证据类型：basis/assumption/pending'"),
]


def _existing_columns(cur, table):
    cur.execute('SHOW FULL COLUMNS FROM `%s`' % table)
    return {r['Field'] for r in cur.fetchall()}


def _existing_indexes(cur, table):
    cur.execute('SHOW INDEX FROM `%s`' % table)
    return {r['Key_name'] for r in cur.fetchall()}


def add_columns(cur, table, cols, after):
    have = _existing_columns(cur, table)
    added = []
    for name, ddl in cols:
        if name in have:
            print('  [跳过] %s.%s 已存在' % (table, name))
            continue
        cur.execute("ALTER TABLE `%s` ADD COLUMN `%s` %s AFTER `%s`"
                    % (table, name, ddl, after))
        print('  [新增] %s.%s' % (table, name))
        added.append(name)
        after = name
    return added


def main():
    cfg = yaml.safe_load(open(CFG, encoding='utf-8'))['mysql']
    conn = pymysql.connect(host=cfg['host'], user=cfg['user'], password=cfg['password'],
                           database=cfg['database'], charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    print('=== 1) ai_case_drafts 增列 ===')
    add_columns(cur, 'ai_case_drafts', DRAFT_COLS, 'design_method')
    if 'idx_drafts_dims' not in _existing_indexes(cur, 'ai_case_drafts'):
        cur.execute('ALTER TABLE `ai_case_drafts` ADD INDEX `idx_drafts_dims` (`dims`)')
        print('  [新增] 索引 idx_drafts_dims')
    else:
        print('  [跳过] 索引 idx_drafts_dims 已存在')

    print('=== 2) ai_case_gen_history_item 增列 ===')
    add_columns(cur, 'ai_case_gen_history_item', HISTORY_COLS, 'design_method')

    print('=== 3) ai_coverage_dims 补 R1~R5 ===')
    for code, name, hint, sort_no in ROUTE_ROWS:
        cur.execute('SELECT id FROM ai_coverage_dims WHERE code = %s', (code,))
        if cur.fetchone():
            print('  [跳过] %s 已存在' % code)
            continue
        cur.execute('INSERT INTO ai_coverage_dims (code, name, prompt_hint, sort) '
                    'VALUES (%s, %s, %s, %s)', (code, name, hint, sort_no))
        print('  [新增] %s %s' % (code, name))

    conn.commit()

    print('=== 回读校验 ===')
    for t in ('ai_case_drafts', 'ai_case_gen_history_item'):
        cols = _existing_columns(cur, t)
        want = [c[0] for c in (DRAFT_COLS if t == 'ai_case_drafts' else HISTORY_COLS)]
        missing = [c for c in want if c not in cols]
        print('  %s: %s' % (t, 'OK' if not missing else '缺列 %s' % missing))
    cur.execute('SELECT code, name FROM ai_coverage_dims ORDER BY sort')
    rows = cur.fetchall()
    print('  ai_coverage_dims 共 %d 行: %s' % (len(rows), ','.join(r['code'] for r in rows)))

    cur.execute('SHOW FULL COLUMNS FROM ai_case_drafts')
    for r in cur.fetchall():
        if r['Field'] in ('dims', 'routes', 'evidence_type', 'severity'):
            print('   - %s %s | %s' % (r['Field'], r['Type'], r['Comment']))

    conn.close()
    print('\n迁移完成。test_cases 未做任何改动（决策④）。')


if __name__ == '__main__':
    sys.exit(main())