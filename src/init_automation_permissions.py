# -*- coding: utf-8 -*-
"""
为「自动化管理」菜单补齐 RBAC 权限点，并同步给超级管理员角色。

背景：
- 前端 src/router/index.js 的 /automation 路由 meta.permissions = ['menu:automation']，
  intelligence 模式下路由守卫只会展示当前用户拥有的权限码对应的菜单。
- 权限码来自后端 permissions 表，经 role_permissions 关联到用户角色。
- admin 角色在 init_rbac.py 首次创建时按 '__ALL__' 拿到了当时全部权限点，
  但后续新增的权限点不会自动补进 admin（seed_roles 对已存在角色直接跳过）。

本脚本幂等，可重复执行：
1. 确保 menu:automation 及其子权限点存在于 permissions 表；
2. 让 admin 角色补齐所有「尚未拥有」的权限点（含本次新增的），不删已有。
"""
import logging
import os

from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'config.yml')

# 自动化管理菜单权限点（与前端路由 meta.permissions 对应）
AUTOMATION_PERMISSIONS = [
    {'code': 'menu:automation', 'name': '自动化管理', 'type': 1,
     'path': '/automation', 'icon': 'robot', 'sort': 7, 'children': [
        {'code': 'menu:automation:interfaces', 'name': '接口管理', 'type': 1,
         'path': '/automation/interfaces', 'sort': 1},
        {'code': 'menu:automation:schedules', 'name': '执行计划', 'type': 1,
         'path': '/automation/schedules', 'sort': 2},
        {'code': 'menu:automation:summary', 'name': '执行总结', 'type': 1,
         'path': '/automation/summary', 'sort': 3},
    ]},
]


def ensure_permissions(db, items, parent_id=0):
    for item in items:
        row = db.execute_single_query(
            'SELECT id FROM permissions WHERE code=%s', (item['code'],)
        )
        if row:
            perm_id = row['id']
            logger.info(f'  权限点已存在，跳过: {item["code"]}')
        else:
            db.execute_update(
                'INSERT INTO permissions (parent_id, name, code, `type`, path, icon, sort) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (
                    parent_id, item['name'], item['code'], item['type'],
                    item.get('path', ''), item.get('icon', ''), item.get('sort', 0),
                ),
            )
            perm_id = db.execute_single_query(
                'SELECT id FROM permissions WHERE code=%s', (item['code'],)
            )['id']
            logger.info(f'  新增权限点: {item["code"]}')
        children = item.get('children') or []
        if children:
            ensure_permissions(db, children, perm_id)


def sync_admin_all_permissions(db):
    """让 admin 角色补齐所有尚未拥有的权限点（保持 __ALL__ 语义，不删已有）。"""
    admin_row = db.execute_single_query("SELECT id FROM roles WHERE code='admin'")
    if not admin_row:
        logger.warning('未找到 admin 角色，跳过同步')
        return
    role_id = admin_row['id']
    # 把所有 permissions 表中 admin 还没有的权限点补上
    db.execute_update(
        'INSERT INTO role_permissions (role_id, permission_id) '
        'SELECT %s, p.id FROM permissions p '
        'LEFT JOIN role_permissions rp ON rp.role_id=%s AND rp.permission_id=p.id '
        'WHERE rp.permission_id IS NULL',
        (role_id, role_id),
    )
    logger.info('已为 admin 角色补齐全部权限点（含自动化管理）')


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'],
        data['mysql']['user'],
        data['mysql']['password'],
        data['mysql']['database'],
    )
    with db:
        logger.info('写入自动化管理权限点...')
        ensure_permissions(db, AUTOMATION_PERMISSIONS)
        logger.info('同步 admin 角色权限...')
        sync_admin_all_permissions(db)
    logger.info('自动化管理 RBAC 权限补齐完成')


if __name__ == '__main__':
    main()
