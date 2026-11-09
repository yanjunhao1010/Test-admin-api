"""给用例草稿表与生成历史明细表增加 tp_refs 列（测试点回标编号）。

背景：选项三「case_gen 回标测试点编号」落地。
生成侧每条用例会带 tp_refs（如 TP1/TP3），指向来源需求文档
`stage_payload.extract.test_points` 里的原始下标+1。
这个编号要能一路存到草稿表，采纳时才能反查「是哪个产品功能点的第几个测试点」，
从而自动写 product_point_cases.tp_indexes —— 否则回标只是装饰字段。

存储形态：VARCHAR(128)，逗号分隔（如 'TP1,TP3'）。
选 VARCHAR 而不是 JSON：与同表 dims/routes 的存储口径保持一致，读写都简单。

幂等：列已存在则直接跳过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection  # noqa: E402

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')

#: 表 → 列定义
ALTER_SPECS = [
    ('ai_case_drafts', 'tp_refs', 'VARCHAR(128)',
     '测试点回标编号(TP1/TP3…)，指向来源需求的测试点下标+1；采纳时据此反查功能点测试点'),
    ('ai_case_gen_history_item', 'tp_refs', 'VARCHAR(128)',
     '测试点回标编号(TP1/TP3…)，指向来源需求的测试点下标+1'),
]

def main():
    cfg = MySqlconection.load_config_cached(CFG)
    db = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'])
    with db as c:
        for table, column, coltype, comment in ALTER_SPECS:
            # 用 information_schema 查列是否存在：
            # `SHOW COLUMNS FROM t LIKE %s` 不支持参数化占位符（PyMySQL 会把 %s 当字面量）。
            cols = c.execute_query(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (table, column))
            if cols:
                print('[SKIP] %s.%s 已存在' % (table, column))
                continue
            sql = ("ALTER TABLE `%s` ADD COLUMN `%s` %s COMMENT %s"
                   % (table, column, coltype, "'%s'" % comment.replace("'", "''")))
            c.execute_update(sql)
            print('[OK] %s.%s 已添加' % (table, column))
        # 复核
        for table, column, _, _ in ALTER_SPECS:
            cols = c.execute_query(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (table, column))
            print('[CHECK] %s.%s ->' % (table, column), 'YES' if cols else 'MISSING')


if __name__ == '__main__':
    main()
