# -*- coding: utf-8 -*-
"""
RBAC 初始化脚本：建表 + 灌入初始角色与权限点

幂等设计，可重复执行：已存在的权限点与角色会跳过，不会重复写入。

执行方式（在项目根目录 Fast_api_project 下）：
    python -m src.init_rbac

权限码规范（与前端 mock/controller/permission.js 保持一致）：
    菜单：menu:<模块>[:<子模块>]
    按钮：<模块>:<操作>
"""
import logging
import os
from typing import List, Optional

from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'config.yml')

# ---------------- 建表语句 ----------------
CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS roles (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(50) NOT NULL COMMENT '角色名称',
        code VARCHAR(50) NOT NULL COMMENT '角色标识',
        description VARCHAR(200) DEFAULT '' COMMENT '角色描述',
        status TINYINT DEFAULT 1 COMMENT '1启用 0禁用',
        create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
        update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_roles_code (code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色表'
    """,
    """
    CREATE TABLE IF NOT EXISTS permissions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        parent_id INT DEFAULT 0 COMMENT '父级权限ID，0为顶级',
        name VARCHAR(50) NOT NULL COMMENT '权限名称',
        code VARCHAR(100) NOT NULL COMMENT '权限码',
        `type` TINYINT NOT NULL COMMENT '1菜单 2按钮',
        path VARCHAR(200) DEFAULT '' COMMENT '前端路由路径',
        icon VARCHAR(50) DEFAULT '' COMMENT '菜单图标',
        sort INT DEFAULT 0 COMMENT '排序号',
        create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
        update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_permissions_code (code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限点表'
    """,
    """
    CREATE TABLE IF NOT EXISTS user_roles (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL COMMENT '用户ID，对应 users.id',
        role_id INT NOT NULL COMMENT '角色ID',
        UNIQUE KEY uk_user_role (user_id, role_id),
        KEY idx_role_id (role_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户角色关联表'
    """,
    """
    CREATE TABLE IF NOT EXISTS role_permissions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        role_id INT NOT NULL COMMENT '角色ID',
        permission_id INT NOT NULL COMMENT '权限ID',
        UNIQUE KEY uk_role_perm (role_id, permission_id),
        KEY idx_permission_id (permission_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色权限关联表'
    """,
]

# ---------------- 权限点种子数据（type: 1菜单 2按钮）----------------
PERMISSION_SEED = [
    {'code': 'menu:index', 'name': '首页', 'type': 1, 'path': '/index', 'icon': 'home', 'sort': 1},
    {'code': 'menu:map', 'name': '地图管理', 'type': 1, 'path': '/map', 'icon': 'map', 'sort': 2},
    {
        'code': 'menu:testcase',
        'name': '用例仓库',
        'type': 1,
        'path': '/tcmanagement',
        'icon': 'file',
        'sort': 3,
        'children': [
            {'code': 'testcase:add', 'name': '新增用例', 'type': 2, 'sort': 1},
            {'code': 'testcase:edit', 'name': '编辑用例', 'type': 2, 'sort': 2},
            {'code': 'testcase:delete', 'name': '删除用例', 'type': 2, 'sort': 3},
            {'code': 'testcase:import', 'name': '导入用例', 'type': 2, 'sort': 4},
        ],
    },
    {
        'code': 'menu:task',
        'name': '任务管理',
        'type': 1,
        'path': '/taskmanagement',
        'icon': 'tasks',
        'sort': 4,
        'children': [
            {'code': 'task:add', 'name': '新建任务', 'type': 2, 'sort': 1},
            {'code': 'task:edit', 'name': '编辑任务', 'type': 2, 'sort': 2},
            {'code': 'task:delete', 'name': '删除任务', 'type': 2, 'sort': 3},
        ],
    },
    {'code': 'menu:tools', 'name': '工具管理', 'type': 1, 'path': '/tools', 'icon': 'brain', 'sort': 5},
    {
        'code': 'menu:personnel',
        'name': '配置',
        'type': 1,
        'path': '/personnelManagement',
        'icon': 'users-cog',
        'sort': 6,
        'children': [
            {
                'code': 'menu:personnel:user',
                'name': '用户管理',
                'type': 1,
                'path': '/personnelManagement/userManagement',
                'sort': 1,
                'children': [{'code': 'personnel:user:edit', 'name': '编辑用户', 'type': 2, 'sort': 1}],
            },
            {
                'code': 'menu:personnel:role',
                'name': '角色管理',
                'type': 1,
                'path': '/personnelManagement/roleManagement',
                'sort': 2,
                'children': [
                    {'code': 'personnel:role:edit', 'name': '编辑角色', 'type': 2, 'sort': 1},
                    {'code': 'personnel:role:assign', 'name': '分配权限', 'type': 2, 'sort': 2},
                ],
            },
            {
                'code': 'menu:personnel:menu',
                'name': '菜单管理',
                'type': 1,
                'path': '/personnelManagement/menuManagement',
                'sort': 3,
                'children': [{'code': 'personnel:menu:edit', 'name': '编辑菜单', 'type': 2, 'sort': 1}],
            },
        ],
    },
]

# ---------------- 角色种子数据 ----------------
# permissions 为 '__ALL__' 表示拥有全部权限点，避免 admin 因后续新增权限点而漏配
ROLE_SEED = [
    {
        'code': 'admin',
        'name': '超级管理员',
        'description': '拥有全部权限',
        'permissions': '__ALL__',
    },
    {
        'code': 'tester',
        'name': '测试员',
        'description': '用例与任务的日常操作',
        'permissions': [
            'menu:index',
            'menu:testcase',
            'testcase:add',
            'testcase:edit',
            'testcase:import',
            'menu:task',
            'task:add',
            'task:edit',
        ],
    },
    {
        'code': 'viewer',
        'name': '访客',
        'description': '仅可查看',
        'permissions': ['menu:index', 'menu:testcase', 'menu:task', 'menu:tools'],
    },
]


def create_tables(db) -> None:
    """创建 RBAC 相关表"""
    for sql in CREATE_TABLES:
        db.execute_update(sql)
    logger.info('RBAC 表结构就绪')


def seed_permissions(db, items, parent_id: int = 0) -> None:
    """递归写入权限点，已存在（按 code 判断）则跳过"""
    for item in items:
        row = db.execute_single_query(
            'SELECT id FROM permissions WHERE code=%s', (item['code'],)
        )
        if row:
            perm_id = row['id']
        else:
            db.execute_update(
                'INSERT INTO permissions (parent_id, name, code, `type`, path, icon, sort) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (
                    parent_id,
                    item['name'],
                    item['code'],
                    item['type'],
                    item.get('path', ''),
                    item.get('icon', ''),
                    item.get('sort', 0),
                ),
            )
            new_row = db.execute_single_query(
                'SELECT id FROM permissions WHERE code=%s', (item['code'],)
            )
            perm_id = new_row['id']
            logger.info(f"  新增权限点: {item['code']}")

        children = item.get('children') or []
        if children:
            seed_permissions(db, children, perm_id)


def seed_roles(db) -> None:
    """写入初始角色并建立角色与权限的关联"""
    all_permission_ids: Optional[List[int]] = None

    for role in ROLE_SEED:
        row = db.execute_single_query('SELECT id FROM roles WHERE code=%s', (role['code'],))
        if row:
            role_id = row['id']
            # 已存在则不再改动其权限配置，避免覆盖你在页面上做的调整
            logger.info(f"  角色已存在，跳过: {role['code']}")
            continue

        db.execute_update(
            'INSERT INTO roles (name, code, description, status) VALUES (%s, %s, %s, %s)',
            (role['name'], role['code'], role['description'], 1),
        )
        new_row = db.execute_single_query('SELECT id FROM roles WHERE code=%s', (role['code'],))
        role_id = new_row['id']
        logger.info(f"  新增角色: {role['code']}")

        if role['permissions'] == '__ALL__':
            if all_permission_ids is None:
                rows = db.execute_query('SELECT id FROM permissions')
                all_permission_ids = [r['id'] for r in rows]
            perm_ids = all_permission_ids
        else:
            codes = role['permissions']
            if not codes:
                continue
            placeholders = ','.join(['%s'] * len(codes))
            rows = db.execute_query(
                f'SELECT id FROM permissions WHERE code IN ({placeholders})', tuple(codes)
            )
            perm_ids = [r['id'] for r in rows]

        if perm_ids:
            db.execute_many(
                'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)',
                [(role_id, pid) for pid in perm_ids],
            )


def main() -> None:
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'],
        data['mysql']['user'],
        data['mysql']['password'],
        data['mysql']['database'],
    )
    with db:
        create_tables(db)
        logger.info('写入权限点...')
        seed_permissions(db, PERMISSION_SEED)
        logger.info('写入角色...')
        seed_roles(db)
    logger.info('RBAC 初始化完成')


if __name__ == '__main__':
    main()
