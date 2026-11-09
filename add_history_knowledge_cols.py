"""迁移：ai_case_gen_history 增加知识库 RAG 注入留痕字段（幂等）。

- knowledge_injected：本次生成命中并注入的知识库条目数（INT，默认 0）
- knowledge_text：本次生成注入的知识库原文（TEXT，便于历史明细回看）

运行：python add_history_knowledge_cols.py
"""
from src.comment import MySqlconection, yaml_data
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')

ADD_COLUMNS = [
    ("knowledge_injected", "INT NOT NULL DEFAULT 0 COMMENT '本次生成命中并注入用例知识库的条目数'"),
    ("knowledge_text", "TEXT COMMENT '本次生成注入用例知识库的原文（RAG 留痕，供历史明细回看）'"),
]


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'], data['mysql']['password'], data['mysql']['database'])
    with db:
        existing = {r['COLUMN_NAME'] for r in db.execute_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ai_case_gen_history'",
            (data['mysql']['database'],))}
        for col, ddl in ADD_COLUMNS:
            if col in existing:
                print("SKIP %s 已存在" % col)
                continue
            db.execute_update(
                "ALTER TABLE ai_case_gen_history ADD COLUMN `%s` %s" % (col, ddl))
            print("ADD %s 完成" % col)
    print("done")


if __name__ == '__main__':
    main()
