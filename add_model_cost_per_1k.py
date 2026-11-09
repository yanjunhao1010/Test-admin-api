"""迁移：ai_models 增加 cost_per_1k（每千 token 估算费用，元）。

四期 F15 成本看板需要按模型聚合费用，单价挂在模型上最自然。
遵循建表规范：新列必须写中文 COMMENT。
"""
import pymysql

HOST = "8.134.96.9"
PORT = 3306
USER = "testadmin"
PASSWORD = "ta158ph43tsCwMrd"
DATABASE = "test_admin"

SQL_ALTER = (
    "ALTER TABLE ai_models ADD COLUMN cost_per_1k DECIMAL(10,6) NOT NULL DEFAULT 0.0 "
    "COMMENT '每千token估算费用(元)，0=未配置'"
)


def main():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           database=DATABASE, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM ai_models LIKE 'cost_per_1k'")
            if cur.fetchone():
                print("COLUMN_EXISTS_SKIP")
                return
            cur.execute(SQL_ALTER)
            conn.commit()
            print("ALTER_OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
