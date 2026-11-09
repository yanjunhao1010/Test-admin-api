# -*- coding: utf-8 -*-
"""
迁移：新增「产品功能点」菜单权限 menu:devprocess:pfp，并授予所有已持有
menu:devprocess 的角色（与「项目管理」菜单可见性保持一致，admin 不豁免）。

执行：./.venv/Scripts/python.exe add_product_points_permission.py
幂等：权限已存在则跳过插入；已授权的角色不重复授权。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')

NEW_CODE = 'menu:devprocess:pfp'
NEW_NAME = '产品功能点菜单'
PARENT_CODE = 'menu:devprocess'


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'],
    )
    with d:
        parent = d.execute_single_query(
            "SELECT id FROM permissions WHERE code=%s", (PARENT_CODE,)
        )
        if not parent:
            print('父权限 %s 不存在，请先确认「项目管理」菜单权限已落库' % PARENT_CODE)
            return
        parent_id = parent['id']

        exist = d.execute_single_query(
            "SELECT id FROM permissions WHERE code=%s", (NEW_CODE,)
        )
        if exist:
            perm_id = exist['id']
            print('权限 %s 已存在(id=%s)，跳过插入' % (NEW_CODE, perm_id))
        else:
            d.execute_update(
                "INSERT INTO permissions (name, type, parent_id, code) VALUES (%s, 1, %s, %s)",
                (NEW_NAME, parent_id, NEW_CODE),
            )
            perm_id = d.execute_single_query(
                "SELECT id FROM permissions WHERE code=%s", (NEW_CODE,)
            )['id']
            print('已插入权限 %s (id=%s, parent_id=%s)' % (NEW_CODE, perm_id, parent_id))

        # 授予所有已持有父权限的角色（含 admin），保持菜单可见性一致
        role_rows = d.execute_query(
            "SELECT DISTINCT rp.role_id FROM role_permissions rp "
            "JOIN permissions p ON p.id=rp.permission_id WHERE p.code=%s", (PARENT_CODE,)
        )
        granted = 0
        for r in role_rows:
            rid = r['role_id']
            has = d.execute_single_query(
                "SELECT 1 FROM role_permissions WHERE role_id=%s AND permission_id=%s",
                (rid, perm_id),
            )
            if not has:
                d.execute_update(
                    "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)",
                    (rid, perm_id),
                )
                granted += 1
        print('已授权角色数: %s（共 %s 个角色持有父权限）' % (granted, len(role_rows)))


if __name__ == '__main__':
    main()
