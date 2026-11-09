# -*- coding: utf-8 -*-
"""
RBAC 权限管理接口（角色 / 权限 / 分配）

与前端 src/api/permission.js 一一对应：
    GET  /api/roles/list              角色列表
    POST /api/roles/add               新增角色
    POST /api/roles/update            编辑角色
    POST /api/roles/delete            删除角色（ids[]，级联清理关联）
    GET  /api/permissions/tree        权限树（菜单 + 按钮，供分配页勾选）
    GET  /api/permissions/codes       某用户的权限码数组
    GET  /api/roles/permissions       角色已分配的权限ID
    POST /api/roles/assign            给角色分配权限
    GET  /api/users/roles             用户已分配的角色ID
    POST /api/users/assign_roles      给用户分配角色

依赖的表由 src/init_rbac.py 创建：roles / permissions / user_roles / role_permissions
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

roles_router = APIRouter(prefix='/roles', tags=['roles'])
permissions_router = APIRouter(prefix='/permissions', tags=['permissions'])
user_roles_router = APIRouter(prefix='/users', tags=['users'])


def get_db_handler():
    """数据库连接（与 routers/users.py 保持一致的用法）"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'],
        data['mysql']['user'],
        data['mysql']['password'],
        data['mysql']['database'],
    )


# ---------------- 请求模型 ----------------
class RoleIn(BaseModel):
    """角色新增/编辑入参，带 id 视为编辑"""

    id: Optional[int] = None
    name: str
    code: str
    description: str = ''
    status: int = 1

    @field_validator('id', mode='before')
    @classmethod
    def _blank_id_to_none(cls, v):
        """前端新增时表单里 id 是空字符串 ''，直接交给 Pydantic 会 int_parsing 报 422。

        把 '' / 'null' 统一收敛为 None（表示"新增"），
        否则角色管理页的「添加」「编辑」都会稳定 422。
        """
        if v is None or (isinstance(v, str) and v.strip() in ('', 'null', 'undefined')):
            return None
        return v


class AssignPermissionsIn(BaseModel):
    """给角色分配权限，permission_ids 为全量勾选结果"""

    role_id: int
    permission_ids: List[int] = Field(default_factory=list)


class AssignRolesIn(BaseModel):
    """给用户分配角色，role_ids 为全量勾选结果"""

    user_id: int
    role_ids: List[int] = Field(default_factory=list)


class IdsIn(BaseModel):
    """批量删除入参"""

    ids: List[int]


# ---------------- 角色 ----------------
@roles_router.get('/list')
async def list_roles():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, name, code, description, status, create_time FROM roles ORDER BY id'
            )
        return {'code': 200, 'msg': 'success', 'data': rows}
    except Exception as e:
        logger.error(f'查询角色列表失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询角色失败: {str(e)}')


@roles_router.post('/add')
async def add_role(body: RoleIn):
    try:
        with get_db_handler() as db:
            if db.execute_single_query('SELECT id FROM roles WHERE code=%s', (body.code,)):
                return {'code': 500, 'msg': '角色标识已存在', 'data': None}
            db.execute_update(
                'INSERT INTO roles (name, code, description, status) VALUES (%s, %s, %s, %s)',
                (body.name, body.code, body.description, body.status),
            )
        return {'code': 200, 'msg': '新增成功', 'data': None}
    except Exception as e:
        logger.error(f'新增角色失败: {e}')
        raise HTTPException(status_code=500, detail=f'新增角色失败: {str(e)}')


@roles_router.post('/update')
async def update_role(body: RoleIn):
    try:
        with get_db_handler() as db:
            # 先确认角色存在：用 SELECT 判定，避免「字段值未变化导致 UPDATE 影响行数为 0」
            # 被误判成「角色不存在」。MySQL 默认 UPDATE 只返回实际发生变化的行数，
            # 当用户保存但未改任何字段时 rowcount=0，但这不代表行不存在。
            if not db.execute_single_query('SELECT id FROM roles WHERE id=%s', (body.id,)):
                return {'code': 500, 'msg': '角色不存在', 'data': None}
            # 编辑时若改了 code，需排除自身后校验唯一性
            if db.execute_single_query(
                'SELECT id FROM roles WHERE code=%s AND id<>%s', (body.code, body.id)
            ):
                return {'code': 500, 'msg': '角色标识已存在', 'data': None}
            db.execute_update(
                'UPDATE '
                'roles SET name=%s, code=%s, description=%s, status=%s WHERE id=%s',
                (body.name, body.code, body.description, body.status, body.id),
            )
        return {'code': 200, 'msg': '修改成功', 'data': None}
    except Exception as e:
        logger.error(f'编辑角色失败: {e}')
        raise HTTPException(status_code=500, detail=f'编辑角色失败: {str(e)}')


@roles_router.post('/delete')
async def delete_role(body: IdsIn):
    if not body.ids:
        return {'code': 500, 'msg': '未选择要删除的角色', 'data': None}
    try:
        with get_db_handler() as db:
            # 先清理关联，避免出现脏数据
            placeholders = ','.join(['%s'] * len(body.ids))
            db.execute_update(
                f'DELETE FROM role_permissions WHERE role_id IN ({placeholders})', tuple(body.ids)
            )
            db.execute_update(
                f'DELETE FROM user_roles WHERE role_id IN ({placeholders})', tuple(body.ids)
            )
            db.execute_update(f'DELETE FROM roles WHERE id IN ({placeholders})', tuple(body.ids))
        return {'code': 200, 'msg': '删除成功', 'data': None}
    except Exception as e:
        logger.error(f'删除角色失败: {e}')
        raise HTTPException(status_code=500, detail=f'删除角色失败: {str(e)}')


# ---------------- 权限 ----------------
@permissions_router.get('/tree')
async def permission_tree():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, parent_id, name, code, `type`, path, icon, sort '
                'FROM permissions ORDER BY sort, id'
            )
        return {'code': 200, 'msg': 'success', 'data': _build_tree(rows)}
    except Exception as e:
        logger.error(f'查询权限树失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询权限失败: {str(e)}')


@permissions_router.get('/codes')
async def permission_codes(user_id: int):
    """获取某用户的权限码数组（角色展开后的并集 + 角色标识）"""
    try:
        with get_db_handler() as db:
            codes = _user_permission_codes(db, user_id)
        return {'code': 200, 'msg': 'success', 'data': codes}
    except Exception as e:
        logger.error(f'查询用户权限码失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询权限码失败: {str(e)}')


# ---------------- 分配 ----------------
@roles_router.get('/permissions')
async def role_permissions(role_id: int):
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT * FROM permissions WHERE id IN (SELECT permission_id FROM role_permissions WHERE role_id=%s)', (role_id,)
            )
        return {'code': 200, 'msg': 'success', 'data': [r for r in rows]}
    except Exception as e:
        logger.error(f'查询角色权限失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询角色权限失败: {str(e)}')


@roles_router.post('/assign')
async def assign_permissions(body: AssignPermissionsIn):
    try:
        with get_db_handler() as db:
            # 全量覆盖：先清后写，前端直接把勾选结果传回来
            db.execute_update('DELETE FROM role_permissions WHERE role_id=%s', (body.role_id,))
            if body.permission_ids:
                db.execute_many(
                    'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)',
                    [(body.role_id, pid) for pid in body.permission_ids],
                )
        return {'code': 200, 'msg': '分配成功', 'data': None}
    except Exception as e:
        logger.error(f'分配权限失败: {e}')
        raise HTTPException(status_code=500, detail=f'分配权限失败: {str(e)}')


@user_roles_router.get('/roles')
async def user_roles(user_id: int):
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT role_id FROM user_roles WHERE user_id=%s', (user_id,))
        return {'code': 200, 'msg': 'success', 'data': [r['role_id'] for r in rows]}
    except Exception as e:
        logger.error(f'查询用户角色失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询用户角色失败: {str(e)}')


@user_roles_router.post('/assign_roles')
async def assign_roles(body: AssignRolesIn):
    try:
        with get_db_handler() as db:
            db.execute_update('DELETE FROM user_roles WHERE user_id=%s', (body.user_id,))
            if body.role_ids:
                db.execute_many(
                    'INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)',
                    [(body.user_id, rid) for rid in body.role_ids],
                )
        return {'code': 200, 'msg': '分配成功', 'data': None}
    except Exception as e:
        logger.error(f'分配角色失败: {e}')
        raise HTTPException(status_code=500, detail=f'分配角色失败: {str(e)}')


# ---------------- 内部方法 ----------------
def _build_tree(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """扁平权限列表转为树形结构，parent_id 为 0 或找不到父节点时为根"""
    nodes = {}
    for row in rows:
        nodes[row['id']] = {
            'id': row['id'],
            'parent_id': row['parent_id'],
            'name': row['name'],
            'code': row['code'],
            'type': row['type'],
            'path': row.get('path') or '',
            'icon': row.get('icon') or '',
            'sort': row.get('sort') or 0,
            'children': [],
        }
    tree = []
    for node in nodes.values():
        parent = nodes.get(node['parent_id'])
        if parent and node['parent_id'] != 0:
            parent['children'].append(node)
        else:
            tree.append(node)
    return tree


def _user_permission_codes(db, user_id: int) -> List[str]:
    """用户权限码 = 所有角色的权限码并集 + 角色标识本身 + 默认首页菜单权限。

    额外返回角色标识（如 admin），用于兼容前端 meta.permissions: ['admin'] 的旧写法，
    便于平滑迁移，无需一次性改完所有路由。

    默认追加 menu:index（首页菜单权限）：保证任何已登录用户都能看到首页，
    且权限数组不为空，避免前端拿到空数组后反复请求 /userInfo 造成死循环。
    """
    codes: List[str] = []
    role_rows = db.execute_query(
        'SELECT r.code FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = %s',
        (user_id,),
    )
    if role_rows:
        codes.extend(row['code'] for row in role_rows)
        perm_rows = db.execute_query(
            'SELECT DISTINCT p.code FROM role_permissions rp '
            'JOIN permissions p ON p.id = rp.permission_id '
            'WHERE rp.role_id IN (SELECT role_id FROM user_roles WHERE user_id = %s)',
            (user_id,),
        )
        codes.extend(row['code'] for row in perm_rows)
    # 默认分配首页菜单权限（任何已登录用户都拥有，确保权限数组非空、首页可见）
    codes.append('menu:index')
    # 去重后排序，保证每次返回顺序一致
    return sorted(set(codes))
