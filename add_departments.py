# -*- coding: utf-8 -*-
"""部门管理迁移脚本（建表 + 用户表加部门外键列）。

一次运行，幂等：
- CREATE TABLE IF NOT EXISTS departments
- ALTER TABLE users ADD COLUMN department_id（已存在则跳过）

运行：在项目根目录执行
    python add_departments.py
依赖：src/config/config.yml 中的 mysql 配置 + pymysql
"""
import pymysql
import yaml

CONFIG_PATH = 'src/config/config.yml'


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding='utf-8'))['mysql']
    conn = pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset='utf8mb4',
    )
    cur = conn.cursor()
    try:
        # 1) 部门表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS departments (
                id          INT          PRIMARY KEY AUTO_INCREMENT COMMENT '部门ID',
                name        VARCHAR(50)  NOT NULL                  COMMENT '部门名称（唯一）',
                leader      VARCHAR(50)  DEFAULT ''                COMMENT '负责人（realname）',
                status      SMALLINT     DEFAULT 1                 COMMENT '1启用 0禁用',
                remark      VARCHAR(255) DEFAULT ''                COMMENT '备注',
                sort        INT          DEFAULT 0                 COMMENT '排序号，升序',
                is_del      SMALLINT     DEFAULT 0                 COMMENT '逻辑删除 1已删',
                create_time DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                update_time DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                UNIQUE KEY uk_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='部门管理'
            """
        )
        print('departments 表就绪')

        # 2) users 增加部门外键列
        try:
            cur.execute(
                "ALTER TABLE users ADD COLUMN department_id INT NULL "
                "COMMENT '部门ID，关联 departments.id' AFTER dept"
            )
            print('users.department_id 列已添加')
        except Exception as e:
            # 重复执行时 MySQL 报 Duplicate column（1060），视为已存在，安全跳过
            if '1060' in str(e) or 'Duplicate column' in str(e):
                print('users.department_id 列已存在，跳过')
            else:
                raise
        conn.commit()
    finally:
        conn.close()
    print('迁移完成 OK')


if __name__ == '__main__':
    main()
