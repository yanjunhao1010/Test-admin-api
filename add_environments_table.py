import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

SQL = """
CREATE TABLE IF NOT EXISTS api_environments (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '环境名称',
  base_url VARCHAR(500) NOT NULL DEFAULT '' COMMENT '环境基础地址,接口只存路径,执行时按所选环境拼接',
  headers TEXT COMMENT '全局请求头(JSON字符串,该环境下所有接口均自动携带)',
  variables TEXT COMMENT '全局变量(JSON字符串,键值对,供接口{{key}}引用)',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  update_time DATETIME DEFAULT NULL COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    for stmt in SQL.split(';'):
        stmt = stmt.strip()
        if not stmt:
            continue
        db.execute_update(stmt)
        print('OK:', stmt.splitlines()[0][:60])
    print('api_environments created OK')
    db.close()


if __name__ == '__main__':
    main()
