# -*- coding: utf-8 -*-
"""
研发流程 P2 迁移：节点关联引用表 + 报表字段（幂等，可重复跑）。
dev/prod 同库，跑一次两边都生效。
运行：.venv/Scripts/python.exe add_dev_process_p2.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def dbh():
    d = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()['mysql']
    return MySqlconection.DatabaseHandler(d['host'], d['user'], d['password'], d['database'])


def add_column(db, table, col, definition):
    try:
        db.execute_update(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        print(f'[OK] {table}.{col} 已添加')
    except Exception as e:
        msg = str(e)
        if 'Duplicate column' in msg or '1054' in msg or 'already exists' in msg.lower():
            print(f'[SKIP] {table}.{col} 已存在')
        else:
            raise


def main():
    with dbh() as db:
        # 1) 节点关联引用表（F10：用例/缺陷/需求变更 一键跳转）
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS dev_process_node_links (
          id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '关联ID',
          process_id  INT UNSIGNED NOT NULL COMMENT '关联 dev_process.id',
          node_index  TINYINT      NOT NULL COMMENT '关联节点序号',
          ref_type    TINYINT      DEFAULT 1 COMMENT '1测试用例 2测试任务/缺陷 3需求变更',
          ref_id      INT UNSIGNED NOT NULL COMMENT '被引用对象ID(test_cases/task_management/requirement_changes)',
          ref_title   VARCHAR(200) DEFAULT '' COMMENT '引用对象标题(冗余存储,便于展示)',
          note        VARCHAR(300) DEFAULT '' COMMENT '关联说明',
          create_by   INT UNSIGNED DEFAULT NULL COMMENT '关联人 user.id',
          create_time DATETIME     DEFAULT CURRENT_TIMESTAMP,
          is_del      TINYINT      DEFAULT 0 COMMENT '删除位',
          KEY idx_proc_node (process_id, node_index),
          KEY idx_ref (ref_type, ref_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='研发流程节点关联引用'
        """)
        print('[OK] dev_process_node_links')

        # 2) 报表字段（F11：按时归档率）
        add_column(db, 'dev_process', 'planned_finish_at', "DATE DEFAULT NULL COMMENT '计划归档日期(用于按时归档率)'")
        add_column(db, 'dev_process', 'completed_at', "DATETIME DEFAULT NULL COMMENT '实际归档(全部完成)时间'")

    print('== P2 迁移完成 ==')


if __name__ == '__main__':
    main()
