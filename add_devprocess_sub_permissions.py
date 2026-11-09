# -*- coding: utf-8 -*-
"""
迁移：把「项目管理」菜单的权限粒度补齐到子菜单级。

背景：
  1) permissions 表里 /devprocess 的父节点名叫「研发流程」，而前端侧栏标题是「项目管理」
     → 「角色管理 → 分配权限」里按侧栏名字找不到该节点，误以为没加权限。
  2) 前端 /devprocess 下 5 个子路由都没写 meta.permissions，
     而 handleRoutes.hasPermission() 在路由没有 permissions 时直接 return true
     → 只要有父级 menu:devprocess，5 个子页面全部可见，无法按子菜单分别授权。
     而表里唯一存在的 menu:devprocess:pfp 前端一行都没引用，是个摆设节点。

本脚本做三件事（全部幂等，可重复执行）：
  ① 父节点改名对齐：研发流程 → 项目管理（code 不变，授权不失效）
  ② 补齐 4 个子菜单节点，并把已有的「产品功能点菜单」对齐为「产品功能点」：
       项目流程看板  menu:devprocess:list
       产品功能点    menu:devprocess:pfp   （已存在，id=72）
       需求变更影响  menu:devprocess:change-impact
       节点配置      menu:devprocess:node-def
       统计报表      menu:devprocess:report
  ③ 授权：只给「已持有父级 menu:devprocess 的角色」补勾这 5 个子节点，
     保证加子路由过滤后菜单可见性不缩水（admin 不豁免，权限链照旧）。
     当前持有父权限的角色：admin(1)、产品人员(14)。

执行：./.venv/Scripts/python.exe add_devprocess_sub_permissions.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')

PARENT_CODE = 'menu:devprocess'
PARENT_NAME = '项目管理'

# (code, name, path, component, sort) —— sort 与前端侧栏顺序一致
SUB_PERMS = [
    ('menu:devprocess:list', '项目流程看板', '/devprocess/list',
     '@/views/devprocess/index', 1),
    ('menu:devprocess:pfp', '产品功能点', '/devprocess/product-points',
     '@/views/devprocess/product-points/index', 2),
    ('menu:devprocess:change-impact', '需求变更影响', '/devprocess/change-impact',
     '@/views/devprocess/change-impact/index', 3),
    ('menu:devprocess:node-def', '节点配置', '/devprocess/node-def',
     '@/views/devprocess/node-def/index', 4),
    ('menu:devprocess:report', '统计报表', '/devprocess/report',
     '@/views/devprocess/report/index', 5),
]


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'],
    )
    with d:
        # ---------- ① 父节点改名对齐 ----------
        parent = d.execute_single_query(
            "SELECT id, name FROM permissions WHERE code=%s", (PARENT_CODE,)
        )
        if not parent:
            print('父权限 %s 不存在，请先确认「项目管理」菜单权限已落库' % PARENT_CODE)
            return
        parent_id = parent['id']
        if parent['name'] != PARENT_NAME:
            d.execute_update(
                "UPDATE permissions SET name=%s WHERE id=%s", (PARENT_NAME, parent_id)
            )
            print('[改名] 父节点 id=%s：%s → %s' % (parent_id, parent['name'], PARENT_NAME))
        else:
            print('[跳过] 父节点 id=%s 名称已是「%s」' % (parent_id, PARENT_NAME))

        # ---------- ② 补齐 / 对齐子菜单节点 ----------
        perm_ids = {}
        for code, name, path, component, sort in SUB_PERMS:
            exist = d.execute_single_query(
                "SELECT id, name FROM permissions WHERE code=%s", (code,)
            )
            if exist:
                perm_id = exist['id']
                # 已存在则把名称/path/component/sort 对齐到与侧栏一致
                d.execute_update(
                    "UPDATE permissions SET name=%s, type=1, parent_id=%s, path=%s, "
                    "component=%s, sort=%s WHERE id=%s",
                    (name, parent_id, path, component, sort, perm_id),
                )
                print('[对齐] %-32s id=%-4s %s → %s' % (code, perm_id, exist['name'], name))
            else:
                d.execute_update(
                    "INSERT INTO permissions (name, type, parent_id, code, path, component, sort) "
                    "VALUES (%s, 1, %s, %s, %s, %s, %s)",
                    (name, parent_id, code, path, component, sort),
                )
                perm_id = d.execute_single_query(
                    "SELECT id FROM permissions WHERE code=%s", (code,)
                )['id']
                print('[新增] %-32s id=%-4s %s' % (code, perm_id, name))
            perm_ids[code] = perm_id

        # ---------- ③ 授权：只给已持有父权限的角色补勾子节点 ----------
        role_rows = d.execute_query(
            "SELECT DISTINCT rp.role_id FROM role_permissions rp "
            "JOIN permissions p ON p.id=rp.permission_id WHERE p.code=%s", (PARENT_CODE,)
        )
        granted = 0
        for r in role_rows:
            rid = r['role_id']
            for code, perm_id in perm_ids.items():
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
        print('已授权角色数: %s（共 %s 个角色持有父权限 menu:devprocess）'
              % (granted, len(role_rows)))

        # ---------- 落库结果回显 ----------
        print('\n=== 最终 permissions（devprocess 子树）===')
        rows = d.execute_query(
            "SELECT id, name, code, sort FROM permissions "
            "WHERE code=%s OR code LIKE 'menu:devprocess:%%' ORDER BY sort, id",
            (PARENT_CODE,),
        )
        for row in rows:
            print('  id=%-4s sort=%-3s %-16s %s'
                  % (row['id'], row['sort'], row['name'], row['code']))


if __name__ == '__main__':
    main()
