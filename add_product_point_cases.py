# -*- coding: utf-8 -*-
"""
迁移：新建 product_point_cases 表（产品功能点 ↔ 正式用例库 手动关联桥表）。

由「产品功能点」页面二级功能点行的「关联用例」手动关联而来，
一端是 product_function_points.id，另一端是 test_cases.id（正式用例库）。

执行：在 Fast_api_project 根目录跑
  ./.venv/Scripts/python.exe add_product_point_cases.py
幂等：表已存在则跳过。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'],
    )
    with d:
        exist = d.execute_query(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='product_point_cases'",
            (cfg['mysql']['database'],)
        )
        if exist:
            print('product_point_cases 已存在，跳过创建')
            return
        d.execute_update(
            """
            CREATE TABLE `product_point_cases` (
              `id`         INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '关联记录ID',
              `pfp_id`     INT UNSIGNED NOT NULL                COMMENT '产品功能点(product_function_points.id)',
              `case_id`    INT UNSIGNED NOT NULL                COMMENT '正式用例库用例(test_cases.id)',
              `creator_id` INT UNSIGNED DEFAULT NULL            COMMENT '关联人 users.id',
              `create_time` DATETIME    DEFAULT CURRENT_TIMESTAMP                       COMMENT '关联时间',
              `update_time` DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
              `is_del`     TINYINT      DEFAULT 0               COMMENT '逻辑删除',
              PRIMARY KEY (`id`),
              UNIQUE KEY `uk_pfp_case` (`is_del`, `pfp_id`, `case_id`),
              KEY `idx_pfp` (`pfp_id`),
              KEY `idx_case` (`case_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='产品功能点 ↔ 正式用例库 手动关联桥表'
            """
        )
        print('product_point_cases 创建成功')


if __name__ == '__main__':
    main()
