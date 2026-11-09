# -*- coding: utf-8 -*-
"""ai_case_drafts 增加「目标测试点定位」三列。幂等。

背景：
    测试点级「补充用例」的草稿，原先只靠 `tp_refs`（需求级 TP 编号）回标关联。
    但**手动新增的三级测试点没有回标编号**（tp_ref 为空串）——它不是从需求
    原文拆出来的，没有可回标的来源编号。原先的实现直接 400 拒绝。

    关键设计：**不伪造 TP 编号**。TP 编号是「需求内全局编号」，手动测试点若硬造
    一个（比如按 pfp 内下标+1），会与需求内真实编号空间冲突，回标反查时可能命中
    别的功能点的测试点（串点），且污染「需求→功能点→用例」的来源链条。

    所以给草稿加一组**显式定位列**，让「补充用例」这条链路自带目标坐标：
      target_pfp_id    目标产品功能点 id
      target_tp_index  目标测试点在该功能点 test_points 数组里的 0 基下标
      target_tp_title  目标测试点标题（冗余留痕 + 采纳时二次校验，防下标漂移）

    采纳时（ai_workbench.adopt_drafts）若草稿带 target_pfp_id，就走
    「显式定位关联」而不是「TP 回标反查」——两条通道互斥、互不干扰。

运行：在项目根目录执行
    python add_ai_case_drafts_target_tp.py
依赖：src/config/config.yml 中的 mysql 配置 + pymysql
"""
import pymysql
import yaml

CONFIG_PATH = 'src/config/config.yml'

COLUMNS = [
    ('target_pfp_id', 'INT NULL',
     '补充用例草稿的目标产品功能点ID（手动新增测试点无回标编号时用于精确定位）'),
    ('target_tp_index', 'INT NULL',
     '补充用例草稿的目标测试点在功能点内的0基下标（配合target_pfp_id使用）'),
    ('target_tp_title', 'VARCHAR(512) NULL',
     '补充用例草稿的目标测试点标题（留痕+采纳时二次校验，防止下标漂移串点）'),
]


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding='utf-8'))['mysql']
    conn = pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )
    cur = conn.cursor()
    try:
        for name, coltype, comment in COLUMNS:
            cur.execute("SHOW COLUMNS FROM ai_case_drafts LIKE %s", (name,))
            if cur.fetchone():
                print('%s 已存在，跳过' % name)
                continue
            cur.execute(
                "ALTER TABLE ai_case_drafts ADD COLUMN `%s` %s COMMENT %s"
                % (name, coltype, '%s'),
                (comment,),
            )
            print('%s 列已添加' % name)
        conn.commit()
    finally:
        conn.close()
    print('OK')


if __name__ == '__main__':
    main()
