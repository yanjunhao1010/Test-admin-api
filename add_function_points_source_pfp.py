# -*- coding: utf-8 -*-
"""
迁移：给 function_points 增加 source_pfp_id 列。

用途：「需求分析-转产品功能点」成功后，需要把该产品功能点同步到
「需求变更影响-功能点管理」(function_points 表)。该列用于溯源与幂等：
- 记录来源产品功能点 product_function_points.id；
- 同步前先查 source_pfp_id，避免同一来源被重复同步。

执行：在 Fast_api_project 根目录
  ./.venv/Scripts/python.exe add_function_points_source_pfp.py
幂等：列已存在则跳过（捕获 1060）。
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
        try:
            d.execute_update(
                "ALTER TABLE function_points ADD COLUMN source_pfp_id INT UNSIGNED DEFAULT NULL "
                "COMMENT '来源产品功能点ID(product_function_points.id)；NULL=手动录入/迁移；用于转产品功能点时同步溯源与幂等'"
            )
            print('[OK] function_points.source_pfp_id 已新增')
        except Exception as e:
            if '1060' in str(e) or 'Duplicate' in str(e):
                print('[SKIP] function_points.source_pfp_id 已存在')
            else:
                raise


if __name__ == '__main__':
    main()
