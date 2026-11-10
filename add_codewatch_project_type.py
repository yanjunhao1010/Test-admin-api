# -*- coding: utf-8 -*-
"""代码变更监听：给 cw_repos 增加「项目类型」字段。

幂等：可重复执行。
  python add_codewatch_project_type.py

背景：分析器原先只认 Python/FastAPI（`src/**/*.py`），PHP / Java 仓库会产出
0 个影响接口。新增 project_type 后，添加仓库时可选 AUTO / PYTHON / PHP / JAVA，
分析器按所选语言分派解析器。

存量记录默认 AUTO（自动按仓库文件后缀识别），无需手工回填。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

ADD_COLUMN = (
    "ALTER TABLE cw_repos ADD COLUMN project_type VARCHAR(16) NOT NULL "
    "DEFAULT 'AUTO' COMMENT '项目类型(AUTO自动识别/PYTHON/PHP/JAVA)'"
)


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    try:
        row = db.execute_single_query(
            'SELECT COUNT(*) n FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s',
            ('cw_repos', 'project_type'))
        if row and row['n']:
            print('OK: cw_repos.project_type 已存在，跳过')
        else:
            db.execute_update(ADD_COLUMN)
            print('OK: 已新增 cw_repos.project_type')

        rows = db.execute_query(
            "SELECT id, name, project_type FROM cw_repos WHERE is_del=0 ORDER BY id")
        for r in rows:
            print('  repo id=%s name=%s project_type=%s' % (
                r['id'], r['name'], r['project_type']))
        print('项目类型字段迁移完成')
    finally:
        db.close()


if __name__ == '__main__':
    main()
