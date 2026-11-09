import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    # 幂等：先确认 remark 列不存在再加，避免重复执行报错
    exists = db.execute_query(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='api_interfaces' AND COLUMN_NAME='remark'",
        (data['mysql']['database'],),
    )
    if exists:
        print('remark 列已存在，跳过')
    else:
        db.execute_update(
            "ALTER TABLE api_interfaces "
            "ADD COLUMN remark TEXT NULL COMMENT '备注（接口说明、注意事项、负责人等，选填）'"
        )
        print('OK: api_interfaces 已新增 remark 列')
    db.close()


if __name__ == '__main__':
    main()
