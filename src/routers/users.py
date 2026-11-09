# -*- coding: utf-8 -*-
"""
用户接口

与前端对应关系：
    src/api/userManagement.js  ->  GET  /api/users/list   用户管理页表格（分页 + 按登录名搜索）
                                   POST /api/users/add | update | delete
    src/api/users.js           ->  GET  /api/users/list   任务指派下拉（复用同一份数据）
    src/api/permission.js      ->  GET  /api/users/roles、POST /api/users/assign_roles
                                   （角色分配，定义在 routers/rbac.py）

    GET  /api/users/list       用户列表（分页 + 按登录名搜索）
    POST /api/users/add        新增用户（bcrypt 哈希后落库，返回新 id）
    POST /api/users/update     编辑用户（password 留空表示不修改密码）
    POST /api/users/delete     删除用户（ids[]，级联清理 user_roles）
    GET  /api/users/userLists  旧路径，兼容保留（等价于 /list，可择期下线）

响应信封与项目其余路由保持一致：{ code, msg, data }。
因前端表格自带分页组件，列表额外在顶层返回 totalCount；
旧字段 total 一并保留，避免已有调用方（Postman 集合等）失效。

密码存储：bcrypt（与库中既有账号的 $2b$12$ 哈希格式一致）。
明文密码只在此处被哈希，绝不落库；接口也从不返回 password_hash。
"""
import logging
import os
from typing import Any, Dict, List, Optional

import bcrypt
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator
from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/users',
    tags=['users'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

# 新建用户的默认角色：分配「访客(viewer)」角色，保证新用户不会处于无权限状态。
# 访客权限 = 首页 + 用例仓库 + 任务管理 + 工具管理（仅查看），与 init_rbac.py 的 ROLE_SEED 保持一致。
DEFAULT_ROLE_CODE = 'viewer'

# 列表返回的字段（与前端表格列一一对应）
_LIST_COLUMNS = (
    'id',
    'username',
    'realname',
    'email',
    'phone',
    'avatar',
    'dept',
    'status',
    'last_login_time',
    'create_time',
    'update_time',
)


# ---------------- 请求模型 ----------------
class UserIn(BaseModel):
    """用户新增/编辑入参，带 id 视为编辑。

    注意 avatar 为 Optional 且默认 None：前端编辑弹窗并不管理头像，
    请求体里根本不带这个字段，用 None 表示"本次不改动"，
    否则每次编辑都会把已有头像清空。
    """

    id: Optional[int] = None
    username: str
    realname: str = ''
    password: str = ''
    email: str = ''
    phone: str = ''
    avatar: Optional[str] = None
    dept: str = ''
    department_id: Optional[int] = None
    status: int = 1

    @field_validator('id', mode='before')
    @classmethod
    def _blank_id_to_none(cls, v):
        """前端新增时表单里 id 是空字符串 ''，直接交给 Pydantic 会 int_parsing 报 422。

        这里把 '' / 'null' 统一收敛为 None（表示"新增"），
        否则用户管理页的「添加」按钮会稳定返回 422。
        """
        if v is None or (isinstance(v, str) and v.strip() in ('', 'null', 'undefined')):
            return None
        return v


class IdsIn(BaseModel):
    """批量删除入参"""

    ids: List[int]


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(data['mysql']['host'], data['mysql']['user'], data['mysql']['password'],
                                        data['mysql']['database'])
    return db


def _fmt_time(value: Any) -> Optional[str]:
    """datetime 统一转成 'YYYY-MM-DD HH:MM:SS'，与前端展示格式一致"""
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


def _hash_password(raw: str) -> str:
    """明文 -> bcrypt 哈希（cost 用库默认 12，与既有账号一致）"""
    return bcrypt.hashpw(raw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def _dept_name(db, dept_id: Optional[int]) -> str:
    """按 department_id 反查部门名称，供 denormalize 到 users.dept 冗余列（无则空串）"""
    if not dept_id:
        return ''
    row = db.execute_single_query(
        'SELECT name FROM departments WHERE id=%s AND is_del=0', (dept_id,)
    )
    return row['name'] if row else ''


def _role_map(db, user_ids: List[int]) -> Dict[int, Dict[str, List]]:
    """一次查出这批用户的角色，避免逐行查询造成 N+1"""
    if not user_ids:
        return {}
    placeholders = ','.join(['%s'] * len(user_ids))
    rows = db.execute_query(
        'SELECT ur.user_id, r.id AS role_id, r.name AS role_name '
        'FROM user_roles ur JOIN roles r ON r.id = ur.role_id '
        f'WHERE ur.user_id IN ({placeholders}) ORDER BY r.id',
        tuple(user_ids),
    )
    mapping: Dict[int, Dict[str, List]] = {}
    for row in rows:
        bucket = mapping.setdefault(row['user_id'], {'role_ids': [], 'role_names': []})
        bucket['role_ids'].append(row['role_id'])
        bucket['role_names'].append(row['role_name'])
    return mapping


async def _query_user_list(pageNo: int, pageSize: int, username: str) -> Dict[str, Any]:
    """用户列表查询：分页 + 按登录名模糊搜索 + 附带角色"""
    where, params = '', []
    if username:
        where = 'WHERE username LIKE %s'
        params.append(f'%{username}%')

    try:
        with get_db_handler() as db:
            total_row = db.execute_single_query(
                f'SELECT COUNT(*) AS total FROM users {where}', tuple(params)
            )
            total = total_row['total'] if total_row else 0

            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                "SELECT u.id, u.username, u.realname, u.email, u.phone, u.avatar, "
                "u.dept, u.department_id, u.status, u.last_login_time, u.create_time, u.update_time, "
                "d.name AS department_name "
                "FROM users u LEFT JOIN departments d ON d.id = u.department_id AND d.is_del = 0 "
                f"{where} ORDER BY u.id LIMIT %s OFFSET %s",
                tuple(params) + (pageSize, offset),
            )
            roles = _role_map(db, [r['id'] for r in rows])
    except Exception as e:  # noqa: BLE001 - 统一转成 500，避免泄漏驱动细节
        raise HTTPException(status_code=500, detail=f'数据库查询失败: {str(e)}')

    user_list = []
    for row in rows:
        item = dict(row)
        for field in ('last_login_time', 'create_time', 'update_time'):
            item[field] = _fmt_time(item.get(field))
        # 空值统一成空串，前端表格/编辑弹窗回填不需要额外判空
        for field in ('realname', 'email', 'phone', 'avatar', 'dept', 'department_name'):
            item[field] = item.get(field) or ''
        item['status'] = item.get('status') if item.get('status') is not None else 1
        role = roles.get(item['id'], {'role_ids': [], 'role_names': []})
        item['role_ids'] = role['role_ids']
        item['role_names'] = role['role_names']
        user_list.append(item)

    return {
        'code': 200,
        'msg': 'success',
        'data': user_list,
        'totalCount': total,
        'total': total,
    }


@router.get('/list', summary='用户列表（分页 + 按登录名搜索）')
async def get_user_list(
    pageNo: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=500),
    username: str = Query('', description='按登录名模糊搜索'),
):
    return await _query_user_list(pageNo, pageSize, username)


@router.get('/userLists', summary='用户列表（旧路径，兼容保留）', deprecated=True)
async def get_user_list_legacy(
    pageNo: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=500),
    username: str = Query(''),
):
    return await _query_user_list(pageNo, pageSize, username)


@router.get('/options', summary='用户下拉（返回 id/realname/username）')
def user_options():
    """供「部门管理-负责人」等下拉使用，返回全部用户轻量字段。"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, realname, username FROM users ORDER BY id'
            )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f'数据库查询失败: {str(e)}')
    return {'code': 200, 'msg': 'success', 'data': rows}


@router.post('/add', summary='新增用户')
async def add_user(body: UserIn):
    username = (body.username or '').strip()
    if not username:
        return {'code': 500, 'msg': '用户名不能为空', 'data': None}
    if not body.password:
        # 新增必须设初始密码，否则账号无法登录
        return {'code': 500, 'msg': '新增用户必须设置初始密码', 'data': None}

    try:
        with get_db_handler() as db:
            if db.execute_single_query('SELECT id FROM users WHERE username=%s', (username,)):
                return {'code': 500, 'msg': '用户名已存在', 'data': None}

            db.execute_update(
                'INSERT INTO users (username, realname, password_hash, email, phone, avatar, department_id, dept, status) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (
                    username,
                    (body.realname or '').strip(),
                    _hash_password(body.password),
                    body.email or '',
                    body.phone or '',
                    body.avatar,
                    body.department_id,
                    _dept_name(db, body.department_id),
                    body.status,
                ),
            )
            # DatabaseHandler 不暴露 lastrowid，借唯一键 username 反查新 id
            row = db.execute_single_query('SELECT id FROM users WHERE username=%s', (username,))
            new_id = row['id'] if row else None
            # 默认给新建用户分配 viewer（访客）角色，避免新用户处于无权限状态。
            # 前端用户管理页在 add 之后会再调 assign_roles：若管理员未勾选任何角色，
            # 前端会默认带上 viewer，二者（INSERT IGNORE + 前端默认）合力保证新用户必有角色。
            if new_id is not None:
                viewer = db.execute_single_query(
                    'SELECT id FROM roles WHERE code=%s', (DEFAULT_ROLE_CODE,)
                )
                if viewer:
                    db.execute_update(
                        'INSERT IGNORE INTO user_roles (user_id, role_id) VALUES (%s, %s)',
                        (new_id, viewer['id']),
                    )
    except Exception as e:
        logger.error(f'新增用户失败: {e}')
        raise HTTPException(status_code=500, detail=f'新增用户失败: {str(e)}')

    # 返回新 id，前端拿到后可接着调 /api/users/assign_roles 绑定角色
    return {'code': 200, 'msg': '新增成功', 'data': {'id': row['id'] if row else None}}


@router.post('/update', summary='编辑用户')
async def update_user(body: UserIn):
    if not body.id:
        return {'code': 500, 'msg': '缺少用户 id', 'data': None}
    username = (body.username or '').strip()
    if not username:
        return {'code': 500, 'msg': '用户名不能为空', 'data': None}

    try:
        with get_db_handler() as db:
            if not db.execute_single_query('SELECT id FROM users WHERE id=%s', (body.id,)):
                return {'code': 500, 'msg': '用户不存在', 'data': None}
            dup = db.execute_single_query(
                'SELECT id FROM users WHERE username=%s AND id<>%s', (username, body.id)
            )
            if dup:
                return {'code': 500, 'msg': '用户名已存在', 'data': None}

            fields = ['username=%s', 'realname=%s', 'email=%s', 'phone=%s', 'department_id=%s', 'dept=%s', 'status=%s']
            params: List[Any] = [
                username,
                (body.realname or '').strip(),
                body.email or '',
                body.phone or '',
                body.department_id,
                _dept_name(db, body.department_id),
                body.status,
            ]
            # 密码留空 = 不修改（编辑弹窗的常规行为）
            if body.password:
                fields.append('password_hash=%s')
                params.append(_hash_password(body.password))
            # 头像仅在显式传入时更新，避免被前端的空值覆盖
            if body.avatar is not None:
                fields.append('avatar=%s')
                params.append(body.avatar)

            params.append(body.id)
            db.execute_update(f"UPDATE users SET {', '.join(fields)} WHERE id=%s", tuple(params))
    except Exception as e:
        logger.error(f'编辑用户失败: {e}')
        raise HTTPException(status_code=500, detail=f'编辑用户失败: {str(e)}')

    return {'code': 200, 'msg': '修改成功', 'data': None}


@router.post('/delete', summary='删除用户（支持批量）')
async def delete_users(body: IdsIn):
    if not body.ids:
        return {'code': 500, 'msg': '未选择要删除的用户', 'data': None}

    try:
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(body.ids))
            # 先清理角色关联，避免留下指向已删用户的孤儿行
            db.execute_update(
                f'DELETE FROM user_roles WHERE user_id IN ({placeholders})', tuple(body.ids)
            )
            db.execute_update(f'DELETE FROM users WHERE id IN ({placeholders})', tuple(body.ids))
    except Exception as e:
        logger.error(f'删除用户失败: {e}')
        raise HTTPException(status_code=500, detail=f'删除用户失败: {str(e)}')

    return {'code': 200, 'msg': '删除成功', 'data': None}
