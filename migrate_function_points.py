# -*- coding: utf-8 -*-
"""
存量功能点文本归并回填（幂等）：
扫描 test_cases.function_point 非空文本，按 (project_id, 文本) 归并去重建 function_points 行，
回填 test_cases.function_point_id。未匹配/为空留 NULL。可重复跑。
运行：.venv/Scripts/python.exe migrate_function_points.py
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
        # 已存在的 (project_id, name) -> id
        existing = {}
        for r in db.execute_query("SELECT id, project_id, name FROM function_points WHERE is_del=0"):
            existing.setdefault((r['project_id'], r['name']), r['id'])

        # 存量文本分组
        rows = db.execute_query(
            "SELECT DISTINCT project_id, function_point, `module` FROM test_cases "
            "WHERE is_del=0 AND function_point IS NOT NULL AND function_point <> ''"
        )
        created = 0
        for r in rows:
            pid = r['project_id'] if r['project_id'] is not None else 0
            name = (r['function_point'] or '').strip()
            if not name:
                continue
            key = (pid, name)
            if key in existing:
                continue
            db.execute_update(
                "INSERT INTO function_points (code, name, project_id, module) VALUES ('', %s, %s, %s)",
                (name, r['project_id'], r.get('module') or ''),
            )
            lid = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
            existing[key] = lid
            created += 1
        print(f'[OK] 新建功能点实体 {created} 条')

        # 回填 test_cases.function_point_id（仅回填仍为 NULL 的）
        to_update = db.execute_query(
            "SELECT id, project_id, function_point FROM test_cases "
            "WHERE is_del=0 AND function_point_id IS NULL AND function_point IS NOT NULL AND function_point <> ''"
        )
        updated = 0
        for r in to_update:
            pid = r['project_id'] if r['project_id'] is not None else 0
            name = (r['function_point'] or '').strip()
            key = (pid, name)
            fp_id = existing.get(key)
            if not fp_id:
                continue
            db.execute_update(
                "UPDATE test_cases SET function_point_id=%s WHERE id=%s", (fp_id, r['id'])
            )
            updated += 1
        print(f'[OK] 回填 test_cases.function_point_id {updated} 行')

    print('== 存量功能点归并完成 ==')


if __name__ == '__main__':
    main()
