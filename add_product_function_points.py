# -*- coding: utf-8 -*-
"""
迁移：新建 product_function_points 表（产品功能点 / 产品需求点）。
由需求分析「需求拆分点」批量转出而来，归属某个研发流程(dev_process)。

执行：在 Fast_api_project 根目录跑
  ./.venv/Scripts/python.exe add_product_function_points.py
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
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='product_function_points'",
            (cfg['mysql']['database'],)
        )
        if exist:
            print('product_function_points 已存在，跳过创建')
            return
        d.execute_update(
            """
            CREATE TABLE `product_function_points` (
              `id`             INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '产品功能点ID',
              `process_id`     INT UNSIGNED NOT NULL                COMMENT '归属研发流程(dev_process.id)，即"选择的项目"',
              `requirement_id` INT UNSIGNED DEFAULT NULL            COMMENT '来源需求分析(ai_requirements.id)，溯源用',
              `source_key`     VARCHAR(60)  DEFAULT ''              COMMENT '来源拆分项标识(同需求内唯一)，去重用',
              `title`          VARCHAR(200) NOT NULL               COMMENT '产品功能点/产品需求点名称',
              `module`         VARCHAR(120) DEFAULT ''              COMMENT '所属模块',
              `test_points`    TEXT                                 COMMENT '关联测试点/验收点(JSON数组字符串，来自需求拆分)',
              `status`         TINYINT      DEFAULT 0               COMMENT '状态：0待处理 1进行中 2已完成 3已关闭',
              `creator_id`     INT UNSIGNED DEFAULT NULL            COMMENT '创建人 users.id',
              `create_time`    DATETIME     DEFAULT CURRENT_TIMESTAMP                      COMMENT '创建时间',
              `update_time`    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
              `is_del`         TINYINT      DEFAULT 0               COMMENT '逻辑删除',
              PRIMARY KEY (`id`),
              KEY `idx_process` (`process_id`),
              KEY `idx_req` (`requirement_id`),
              KEY `idx_src` (`requirement_id`, `source_key`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='产品功能点（由需求分析拆分点转化而来）'
            """
        )
        print('product_function_points 创建成功')


if __name__ == '__main__':
    main()
