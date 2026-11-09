"""幂等脚本：给 api_schedules 加 env_id 列（绑定执行环境）。
符合建表规范：每个字段都带中文 COMMENT。
重复执行安全（列已存在则跳过）。
"""
import sys
sys.path.insert(0, 'D:/pyproject/Fast_api_project')
from src.comment import MySqlconection, yaml_data


def main():
    d = yaml_data.BaseYamlData('D:/pyproject/Fast_api_project/src/config/config.yml').read_yaml()
    mysql = d['mysql']
    db = MySqlconection.DatabaseHandler(
        mysql['host'], mysql['user'], mysql['password'], mysql['database']
    )
    with db:
        cols = [r['Field'] for r in db.execute_query('SHOW FULL COLUMNS FROM api_schedules')]
        if 'env_id' in cols:
            print('env_id 已存在，跳过')
            return
        db.execute_update(
            "ALTER TABLE api_schedules ADD COLUMN env_id INT NULL "
            "COMMENT '绑定的环境ID，执行时该计划全部接口使用此环境的base_url/headers' "
            "AFTER enabled"
        )
        print('已添加 env_id 列')
        cols2 = [r['Field'] for r in db.execute_query('SHOW FULL COLUMNS FROM api_schedules')]
        print('当前字段:', cols2)


if __name__ == '__main__':
    main()
