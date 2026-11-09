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
    cols = [c['Field'] for c in db.execute_query('SHOW COLUMNS FROM api_environments')]
    if 'enabled' in cols:
        db.execute_update('ALTER TABLE api_environments DROP COLUMN enabled')
        print('已删除 api_environments.enabled 列')
    else:
        print('api_environments.enabled 不存在，跳过（幂等）')
    cols2 = [c['Field'] for c in db.execute_query('SHOW COLUMNS FROM api_environments')]
    print('当前字段:', ', '.join(cols2))
    db.close()


if __name__ == '__main__':
    main()
