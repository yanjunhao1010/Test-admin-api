import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)


def main():
    """为 api_schedules 增加 prepend_refs 列（前置接口依赖声明，JSON 数组）。幂等。"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    exists = db.execute_query(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='api_schedules' "
        "AND COLUMN_NAME='prepend_refs'",
        (data['mysql']['database'],),
    )
    if exists:
        print('prepend_refs 列已存在，跳过')
    else:
        db.execute_update(
            "ALTER TABLE api_schedules ADD COLUMN prepend_refs TEXT NULL "
            "COMMENT '前置接口依赖声明(JSON数组:[{interface_id,required_vars:[变量名]}])'"
        )
        print('OK: api_schedules 已新增 prepend_refs 列')
    # 校验
    cols = db.execute_query(
        "SELECT COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='api_schedules' "
        "AND COLUMN_NAME='prepend_refs'",
        (data['mysql']['database'],),
    )
    print('校验:', cols)
    db.close()


if __name__ == '__main__':
    main()