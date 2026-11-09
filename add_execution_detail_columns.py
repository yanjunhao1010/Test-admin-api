import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

# 新增列：存储每次执行的完整请求/响应快照、提取变量与断言明细，供执行详情页排查
COLUMNS = [
    ("request_dump", "TEXT NULL COMMENT '实际发出的请求快照(JSON:method/url/headers/params/body)'"),
    ("response_dump", "TEXT NULL COMMENT '完整响应快照(JSON:status/headers/body)'"),
    ("extracted_vars", "TEXT NULL COMMENT '本次执行提取的上下文变量(JSON)'"),
    ("assert_detail", "TEXT NULL COMMENT '断言明细(JSON:passed/type/expected/actual/detail)'"),
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
    print("api_executions 详情列处理完成")


if __name__ == '__main__':
    main()
