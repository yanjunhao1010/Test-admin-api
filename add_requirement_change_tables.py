# -*- coding: utf-8 -*-
"""
需求变更影响分析：建 4 张表 + test_cases.function_point_id 外键（幂等）。
运行：.venv/Scripts/python.exe add_requirement_change_tables.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def dbh():
    d = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()['mysql']
    return MySqlconection.DatabaseHandler(d['host'], d['user'], d['password'], d['database'])


def main():
    with dbh() as db:
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS function_points (
          id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '功能点ID',
          code            VARCHAR(40)  DEFAULT '' COMMENT '功能点编号 如 FP-001',
          name            VARCHAR(120) NOT NULL COMMENT '功能点名称(规范化)',
          project_id      INT UNSIGNED DEFAULT NULL COMMENT '所属项目 test_case_project.id',
          module          VARCHAR(255) DEFAULT '' COMMENT '模块',
          description     TEXT         COMMENT '功能点描述',
          baseline        VARCHAR(60)  DEFAULT '' COMMENT '基线版本/迭代(追溯用)',
          is_del          TINYINT      DEFAULT 0 COMMENT '删除位',
          create_time     DATETIME     DEFAULT CURRENT_TIMESTAMP,
          update_time     DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          KEY idx_proj (project_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='功能点实体'
        """)
        print('[OK] function_points')

        db.execute_update("""
        CREATE TABLE IF NOT EXISTS requirement_changes (
          id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '变更ID',
          title           VARCHAR(200) NOT NULL COMMENT '变更标题/摘要',
          project_id      INT UNSIGNED DEFAULT NULL COMMENT '关联项目',
          change_type     TINYINT      DEFAULT 1 COMMENT '1需求变更 2新增需求',
          description     TEXT         COMMENT '改动说明',
          creator_id      INT UNSIGNED DEFAULT NULL COMMENT '发起人',
          status          TINYINT      DEFAULT 0 COMMENT '0草稿 1已分析',
          create_time     DATETIME     DEFAULT CURRENT_TIMESTAMP,
          KEY idx_proj (project_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='需求变更记录'
        """)
        print('[OK] requirement_changes')

        db.execute_update("""
        CREATE TABLE IF NOT EXISTS change_function_points (
          id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT 'ID',
          change_id           INT UNSIGNED NOT NULL COMMENT '关联 requirement_changes.id',
          function_point_id   INT UNSIGNED NOT NULL COMMENT '关联 function_points.id',
          is_new              TINYINT      DEFAULT 0 COMMENT '0改动已有 1本次新增的功能点',
          KEY idx_change (change_id),
          KEY idx_fp (function_point_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='变更涉及的功能点'
        """)
        print('[OK] change_function_points')

        db.execute_update("""
        CREATE TABLE IF NOT EXISTS change_case_links (
          id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT 'ID',
          change_id           INT UNSIGNED NOT NULL COMMENT '关联 requirement_changes.id',
          function_point_id   INT UNSIGNED NOT NULL COMMENT '本次变更涉及的功能点',
          case_id             INT UNSIGNED DEFAULT NULL COMMENT '命中的用例 test_cases.id',
          disposition         TINYINT      DEFAULT 0 COMMENT '0待定 1需修改 2需新增 3无影响',
          note                VARCHAR(500) DEFAULT '' COMMENT '处置说明',
          handled_by          INT UNSIGNED DEFAULT NULL COMMENT '处置人',
          handled_at          DATETIME     DEFAULT NULL COMMENT '处置时间',
          KEY idx_change (change_id),
          KEY idx_fp (function_point_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='变更与用例的处置结论'
        """)
        print('[OK] change_case_links')

        # test_cases 加外键列（幂等）
        try:
            db.execute_update(
                "ALTER TABLE test_cases ADD COLUMN function_point_id INT UNSIGNED DEFAULT NULL "
                "COMMENT '功能点实体ID(function_points.id),存量可空' AFTER function_point"
            )
            print('[OK] test_cases.function_point_id 已新增')
        except Exception as e:
            if '1060' in str(e) or 'Duplicate' in str(e):
                print('[SKIP] test_cases.function_point_id 已存在')
            else:
                raise

    print('== 需求变更四表 + 外键 完成 ==')


if __name__ == '__main__':
    main()
