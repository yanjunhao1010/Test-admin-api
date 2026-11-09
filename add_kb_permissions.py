# -*- coding: utf-8 -*-
"""知识库菜单权限落库迁移脚本（幂等）。

在 permissions 表插入「用例知识库」菜单节点（挂在 用例仓库 menu:testcase 下），
并给 admin(role 1) 与 tester(role 12) 授权，确保侧边栏可见（admin 不豁免）。

运行：在 Fast_api_project 目录下执行
    python add_kb_permissions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

# 用例仓库 菜单节点 id（实测 permissions 表：code='menu:testcase' -> id=3）
PARENT_ID = 3
CODE = 'menu:testcase:kb'


def get_db_handler():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'], data['mysql']['password'], data['mysql']['database']
    )


def main():
    with get_db_handler() as db:
        exist = db.execute_query("SELECT id FROM permissions WHERE code=%s", (CODE,))
        if exist:
            pid = exist[0]['id']
            print('权限节点已存在 id=%s' % pid)
        else:
            db.execute_update(
                "INSERT INTO permissions "
                "(parent_id, name, code, type, path, icon, sort, component, redirect, "
                " hidden, always_show, affix, no_keep_alive, badge) "
                "VALUES (%s, '用例知识库', %s, 1, '/tcmanagement/kb', 'book', 3, "
                "'@/views/testcase/kb/index', '', 0, 0, 0, 0, '')",
                (PARENT_ID, CODE),
            )
            pid = db.execute_query("SELECT id FROM permissions WHERE code=%s", (CODE,))[0]['id']
            print('已插入权限节点 id=%s' % pid)

        # 授权：admin(1) + tester(12)，均可见「用例知识库」
        for role in (1, 12):
            hit = db.execute_query(
                "SELECT id FROM role_permissions WHERE role_id=%s AND permission_id=%s",
                (role, pid),
            )
            if not hit:
                db.execute_update(
                    "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)",
                    (role, pid),
                )
                print('已授权 role %s' % role)
            else:
                print('role %s 已授权（跳过）' % role)


if __name__ == '__main__':
    main()
