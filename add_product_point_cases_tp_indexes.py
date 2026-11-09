# -*- coding: utf-8 -*-
"""
迁移：product_point_cases 桥表新增 tp_indexes 列（测试点粒度覆盖）。

背景：
  覆盖粒度原先只到「功能点」级（一条关联 = 这个功能点有用例）。
  为支持「不漏测试点」，把粒度下放到单个测试点：
  一条关联记录可以声明它覆盖该功能点下的**哪些测试点**。

语义（tp_indexes，TEXT，存 JSON 数组）：
  NULL / '[]'  → 整体关联：这条用例覆盖该功能点的**全部**测试点
                 （历史数据与默认行为，向后兼容）
  '[0,2]'      → 精细关联：这条用例只覆盖第 1、3 个测试点（下标从 0 开始）

执行：在 Fast_api_project 根目录跑
  ./.venv/Scripts/python.exe add_product_point_cases_tp_indexes.py
幂等：列已存在则跳过。
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
        cols = d.execute_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='product_point_cases'",
            (cfg['mysql']['database'],)
        )
        names = {c['COLUMN_NAME'] for c in cols}
        if not names:
            print('product_point_cases 表不存在，请先执行 add_product_point_cases.py')
            return
        if 'tp_indexes' in names:
            print('tp_indexes 列已存在，跳过')
            return
        d.execute_update(
            "ALTER TABLE `product_point_cases` "
            "ADD COLUMN `tp_indexes` TEXT DEFAULT NULL "
            "COMMENT '该关联覆盖的测试点下标(JSON数组，下标从0开始)；NULL或[]=覆盖功能点全部测试点'"
        )
        print('tp_indexes 列添加成功')


if __name__ == '__main__':
    main()
