import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

# 新增列：记录每次执行所用的环境与触发人，供仪表盘「最近执行」展示环境/执行人
COLUMNS = [
    ("env_id", "INT NULL COMMENT '执行时使用的环境ID，关联 api_environments.id；无头调度或历史数据为NULL'"),
    ("run_by", "INT NULL COMMENT '触发执行的用户ID，关联 users.id；定时调度无头执行时为NULL'"),
]


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    for col, defn in COLUMNS:
        try:
            db.execute_update(f"ALTER TABLE api_executions ADD COLUMN {col} {defn}")
            print(f"OK   已加列 {col}")
        except Exception as e:
            msg = str(e)
            if "Duplicate column" in msg or "1072" in msg or "already exists" in msg.lower():
                print(f"SKIP {col} 已存在")
            else:
                print(f"ERR  {col}: {msg}")
                raise
    db.close()
    print("api_executions 环境/执行人列处理完成")


if __name__ == '__main__':
    main()
