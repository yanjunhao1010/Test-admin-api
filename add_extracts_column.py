"""幂等脚本：为 api_interfaces 增加 extracts 列（存储接口响应变量提取规则）。

提取规则为 JSON 数组，例如：
[
  {"name": "token", "source": "body", "path": "data.access_token"},
  {"name": "uid",   "source": "header", "path": "X-User-Id"}
]
运行：python add_extracts_column.py
"""
import os
import sys
import pymysql

# 复用项目现有配置读取（与 DatabaseHandler 同一套 yaml）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.comment import yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

COLUMN_DDL = (
    "ALTER TABLE api_interfaces "
    "ADD COLUMN extracts TEXT NULL "
    "COMMENT '响应变量提取规则(JSON数组)，用于接口间上下文传递'"
)


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    mysql = data['mysql']
    conn = pymysql.connect(
        host=mysql['host'],
        port=int(mysql.get('port', 3306)),
        user=mysql['user'],
        password=mysql['password'],
        database=mysql['database'],
        charset=mysql.get('charset', 'utf8mb4'),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM api_interfaces LIKE 'extracts'")
            if cur.fetchone():
                print('OK: extracts 列已存在，无需改动')
                return
            cur.execute(COLUMN_DDL)
            conn.commit()
            print('OK: 已为 api_interfaces 增加 extracts 列')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
