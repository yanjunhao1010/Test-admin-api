# -*- coding: utf-8 -*-
"""
菜单管理接口

与前端对应关系（src/api/menuManagement.js）：
    GET  /api/menus/tree      菜单树（菜单管理页的左树 + 右侧表格）
    POST /api/menus/add       新增菜单 / 权限点
    POST /api/menus/update    编辑菜单 / 权限点
    POST /api/menus/delete    删除菜单 / 权限点（连带子节点与角色关联）

重要：菜单与"权限点"是同一张表（permissions）上的两类节点（type: 1菜单 2按钮）。
配置页的「菜单管理」与「角色管理 → 分配权限」用的是同一棵树，
因此这里没有另建 menus 表，避免出现两套对不上的菜单数据。

permissions 表结构（由 src/init_rbac.py 创建）：
    id, parent_id, name, code, type, path, icon, sort,
    component, redirect, hidden, always_show, affix, no_keep_alive, badge,
    create_time, update_time

响应信封与项目其余路由保持一致：{ code, msg, data }。
"""
import logging
import os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/menus',
    tags=['menus'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

# 菜单/权限点的可变字段（新增与编辑共用，集中一处避免遗漏）
# 前端 MenuManagementEdit 的表单恰好提交这一整套，故编辑时做全量覆盖是安全的。
# name / code 需要 strip 归一化，单独处理，不放进这个元组。
MUTABLE_FIELDS = (
    'parent_id',
    'type',
    'path',
    'icon',
    'sort',
    'component',
    'redirect',
    'hidden',
    'always_show',
    'affix',
    'no_keep_alive',
    'badge',
)

# 树查询返回的列（type 是 MySQL 保留字，必须加反引号）
_TREE_COLUMNS = 'id, parent_id, name, code, `type`, path, icon, sort, component, redirect, ' \
                'hidden, always_show, affix, no_keep_alive, badge'


# ---------------- 请求模型 ----------------
class MenuIn(BaseModel):
    """菜单新增/编辑入参，带 id 视为编辑"""

    id: Optional[int] = None
    parent_id: int = 0
    name: str
    code: str
    type: int = 1
    path: str = ''
    icon: str = ''
    sort: int = 0
    component: str = ''
    redirect: str = ''
    hidden: int = 0
    always_show: int = 0
    affix: int = 0
    no_keep_alive: int = 0
    badge: str = ''

    @field_validator('id', mode='before')
    @classmethod
    def _blank_id_to_none(cls, v):
        """前端新增时表单里 id 是空字符串 ''，直接交给 Pydantic 会 int_parsing 报 422。

        把 '' / 'null' 统一收敛为 None（表示"新增"），否则菜单管理页的「添加」会稳定 422。
        """
        if v is None or (isinstance(v, str) and v.strip() in ('', 'null', 'undefined')):
            return None
        return v

    @field_validator('parent_id', 'sort', 'hidden', 'always_show', 'affix', 'no_keep_alive',
                     mode='before')
    @classmethod
    def _blank_int_to_zero(cls, v):
        """勾选框 / 数字输入框被清空时会提交空字符串，按 0 处理，避免 422。"""
        if v is None or (isinstance(v, str) and v.strip() == ''):
            return 0
        return v

    @field_validator('type', mode='before')
    @classmethod
    def _blank_type_to_menu(cls, v):
        """类型缺失时按「菜单」处理（1），0 不是合法类型值。"""
        if v is None or (isinstance(v, str) and v.strip() in ('', 'null', 'undefined')):
            return 1
        return v


class IdsIn(BaseModel):
    """批量删除入参"""

    ids: List[int]


def get_db_handler():
    """数据库连接依赖注入（与 routers/users.py 保持一致的用法）"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'],
        data['mysql']['user'],
        data['mysql']['password'],
        data['mysql']['database'],
    )


def _build_tree(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """扁平列表转树形结构，parent_id 为 0 或找不到父节点时视为根节点。

    返回字段为扁平结构（非前端路由表的 meta 嵌套结构），
    与 src/api/menuManagement.js 的注释契约一致。
    """
    nodes: Dict[int, Dict[str, Any]] = {}
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
            'component': row.get('component') or '',
            'redirect': row.get('redirect') or '',
            'hidden': row.get('hidden') or 0,
            'always_show': row.get('always_show') or 0,
            'affix': row.get('affix') or 0,
            'no_keep_alive': row.get('no_keep_alive') or 0,
            'badge': row.get('badge') or '',
            'children': [],
        }

    tree: List[Dict[str, Any]] = []
    for node in nodes.values():
        parent = nodes.get(node['parent_id'])
        if parent and node['parent_id'] != 0:
            parent['children'].append(node)
        else:
            tree.append(node)
    return tree


def _collect_descendant_ids(db, root_ids: List[int]) -> List[int]:
    """收集自身 + 所有后代的 id。

    删除菜单时若只删父节点，子节点会变成永远挂不上树的孤儿行，
    所以这里一次性深度收集，连同 role_permissions 关联一起清掉。
    """
    rows = db.execute_query('SELECT id, parent_id FROM permissions')
    children_map: Dict[int, List[int]] = {}
    for row in rows:
        children_map.setdefault(row['parent_id'], []).append(row['id'])

    seen: set = set()
    stack = list(root_ids)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children_map.get(cur, []))
    return list(seen)


def _parent_exists(db, parent_id: int) -> bool:
    """parent_id 为 0 表示顶级；其余必须真实存在于表中"""
    if not parent_id:
        return True
    return bool(db.execute_single_query('SELECT id FROM permissions WHERE id=%s', (parent_id,)))


# ---------------- 菜单 / 权限点 ----------------
@router.get('/tree', summary='菜单树（含按钮节点）')
async def menu_tree():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                f'SELECT {_TREE_COLUMNS} FROM permissions ORDER BY sort, id'
            )
        return {'code': 200, 'msg': 'success', 'data': _build_tree(rows)}
    except Exception as e:
        logger.error(f'查询菜单树失败: {e}')
        raise HTTPException(status_code=500, detail=f'查询菜单失败: {str(e)}')


@router.post('/add', summary='新增菜单 / 权限点')
async def add_menu(body: MenuIn):
    name = (body.name or '').strip()
    code = (body.code or '').strip()
    if not name or not code:
        return {'code': 500, 'msg': '名称与权限码不能为空', 'data': None}

    try:
        with get_db_handler() as db:
            if db.execute_single_query('SELECT id FROM permissions WHERE code=%s', (code,)):
                return {'code': 500, 'msg': '权限码已存在', 'data': None}
            if not _parent_exists(db, body.parent_id):
                return {'code': 500, 'msg': '上级菜单不存在', 'data': None}

            columns = ('name', 'code') + MUTABLE_FIELDS
            params = (name, code) + tuple(getattr(body, f) for f in MUTABLE_FIELDS)
            placeholders = ', '.join(['%s'] * len(columns))
            db.execute_update(
                f"INSERT INTO permissions ({', '.join(columns)}) VALUES ({placeholders})",
                params,
            )
    except Exception as e:
        logger.error(f'新增菜单失败: {e}')
        raise HTTPException(status_code=500, detail=f'新增菜单失败: {str(e)}')

    return {'code': 200, 'msg': '新增成功', 'data': None}


@router.post('/update', summary='编辑菜单 / 权限点')
async def update_menu(body: MenuIn):
    if not body.id:
        return {'code': 500, 'msg': '缺少菜单 id', 'data': None}
    name = (body.name or '').strip()
    code = (body.code or '').strip()
    if not name or not code:
        return {'code': 500, 'msg': '名称与权限码不能为空', 'data': None}

    try:
        with get_db_handler() as db:
            if not db.execute_single_query('SELECT id FROM permissions WHERE id=%s', (body.id,)):
                return {'code': 500, 'msg': '菜单不存在', 'data': None}
            dup = db.execute_single_query(
                'SELECT id FROM permissions WHERE code=%s AND id<>%s', (code, body.id)
            )
            if dup:
                return {'code': 500, 'msg': '权限码已存在', 'data': None}
            # 禁止挂到自己或自己的子孙下面，否则这棵子树会从树里彻底消失
            if body.parent_id in _collect_descendant_ids(db, [body.id]):
                return {'code': 500, 'msg': '上级菜单不能是自己或自己的子节点', 'data': None}
            if not _parent_exists(db, body.parent_id):
                return {'code': 500, 'msg': '上级菜单不存在', 'data': None}

            assignments = ', '.join(
                ['name=%s', 'code=%s'] + [f'{f}=%s' for f in MUTABLE_FIELDS]
            )
            params = (name, code) + tuple(getattr(body, f) for f in MUTABLE_FIELDS) + (body.id,)
            db.execute_update(f'UPDATE permissions SET {assignments} WHERE id=%s', params)
    except Exception as e:
        logger.error(f'编辑菜单失败: {e}')
        raise HTTPException(status_code=500, detail=f'编辑菜单失败: {str(e)}')

    return {'code': 200, 'msg': '修改成功', 'data': None}


@router.post('/delete', summary='删除菜单 / 权限点（连带子节点）')
async def delete_menu(body: IdsIn):
    if not body.ids:
        return {'code': 500, 'msg': '未选择要删除的菜单', 'data': None}

    try:
        with get_db_handler() as db:
            ids = _collect_descendant_ids(db, body.ids)
            placeholders = ','.join(['%s'] * len(ids))
            # 先清理角色关联，避免 role_permissions 指向已删节点
            db.execute_update(
                f'DELETE FROM role_permissions WHERE permission_id IN ({placeholders})', tuple(ids)
            )
            db.execute_update(
                f'DELETE FROM permissions WHERE id IN ({placeholders})', tuple(ids)
            )
    except Exception as e:
        logger.error(f'删除菜单失败: {e}')
        raise HTTPException(status_code=500, detail=f'删除菜单失败: {str(e)}')

    extra = len(ids) - len(set(body.ids))
    msg = f'删除成功（连带 {extra} 个子节点）' if extra else '删除成功'
    return {'code': 200, 'msg': msg, 'data': None}
