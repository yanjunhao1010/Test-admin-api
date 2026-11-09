# -*- coding: utf-8 -*-
"""ai_case_drafts 增加 source_case_ids 列（A2 溯源：派生自哪些已有用例）。幂等。

运行：在项目根目录执行
    python add_ai_case_drafts_source_ids.py
依赖：src/config/config.yml 中的 mysql 配置 + pymysql
"""
import pymysql
import yaml

CONFIG_PATH = 'src/config/config.yml'


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding='utf-8'))['mysql']
    conn = pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )
    cur = conn.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM ai_case_drafts LIKE 'source_case_ids'")
        if cur.fetchone():
            print('source_case_ids 已存在，跳过')
        else:
            cur.execute(
                "ALTER TABLE ai_case_drafts "
                "ADD COLUMN source_case_ids JSON NULL COMMENT '派生来源用例ID列表（基于已有用例补充生成时的溯源）'"
            )
            print('source_case_ids 列已添加')
        conn.commit()
    finally:
        conn.close()
    print('OK')


if __name__ == '__main__':
    main()
