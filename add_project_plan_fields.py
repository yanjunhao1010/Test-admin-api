"""
迁移脚本：为 test_case_project 增加「计划目标」字段（F6）。
- planned_start / planned_end：计划起止日期（DATE）
- planned_case_cnt：计划用例数（目标基线，INT）
- milestone：里程碑节点（JSON，结构 [{name, date, done}]）
带中文 COMMENT，幂等（列已存在则跳过），不重建已有表。

运行：python add_project_plan_fields.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    m = data['mysql']
    db = MySqlconection.DatabaseHandler(m['host'], m['user'], m['password'], m['database'])

    cols = [
        ("planned_start", "DATE", "计划开始日期"),
        ("planned_end", "DATE", "计划结束日期"),
        ("planned_case_cnt", "INT", "计划用例数(目标基线)"),
        ("milestone", "JSON", "里程碑节点(JSON:[{name,date,done}])"),
    ]
    for col, ctype, comment in cols:
        try:
            db.execute_update(
                f"ALTER TABLE test_case_project ADD COLUMN `{col}` {ctype} NULL COMMENT '{comment}'"
            )
            print(f"[OK] 已新增列 {col}")
        except Exception as e:
            msg = str(e)
            # 1060 = 列已存在（幂等跳过）；其它错误上报
            if '1060' in msg or 'Duplicate column' in msg:
                print(f"[SKIP] 列 {col} 已存在")
            else:
                print(f"[ERR] 列 {col} 失败: {msg}")
                raise
    print("迁移完成。")


if __name__ == '__main__':
    main()
