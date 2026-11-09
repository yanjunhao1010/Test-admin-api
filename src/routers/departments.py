# -*- coding: utf-8 -*-
"""部门管理接口

与前端对应关系：
    src/api/department.js  ->  GET  /api/departments/list    分页列表（pageNo/pageSize/keyword 搜 名称/负责人）
                               GET  /api/departments/options 用户「部门」下拉专用（仅启用部门，不带分页）
                               POST /api/departments/add | update | delete

    GET  /api/departments/list    分页列表，返回 {code,msg,data,totalCount,total}
    GET  /api/departments/options 启用的部门 [{id, name}]，供用户新增/编辑的部门下拉
    POST /api/departments/add     新增（name 唯一校验）
    POST /api/departments/update  编辑
    POST /api/departments/delete  软删 is_del=1；若有用户关联（users.department_id）则拒绝删除

响应信封与项目其余路由一致：{ code, msg, data }。
路由注册前缀 /api（见 src/main.py）。
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator
from src.comment import MySqlconection, yaml_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/departments',
    tags=['departments'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    return db


def _fmt_time(value: Any) -> Optional[str]:
    """datetime 统一转成 'YYYY-MM-DD HH:MM:SS'"""
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


class DepartmentIn(BaseModel):
    """部门新增/编辑入参，带 id 视为编辑。"""

    id: Optional[int] = None
    name: str
    leader: str = ''
    status: int = 1
    remark: str = ''
    sort: int = 0

    @field_validator('id', mode='before')
    @classmethod
    def _blank_id_to_none(cls, v):
        """前端新增时表单里 id 是空字符串 ''，直接交给 Pydantic 会 int_parsing 报 422。"""
        if v is None or (isinstance(v, str) and v.strip() in ('', 'null', 'undefined')):
            return None
        return v


class IdsIn(BaseModel):
    """批量删除入参"""

    ids: List[int]


@router.get('/list', summary='部门列表（分页 + 按名称/负责人搜索）')
def dept_list(
    pageNo: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=500),
    keyword: str = Query('', description='按部门名称/负责人模糊搜索'),
):
    conds = ['d.is_del = 0']
    params: List[Any] = []
    if keyword:
        conds.append('(d.name LIKE %s OR d.leader LIKE %s)')
        params += [f'%{keyword}%', f'%{keyword}%']
    where = ' AND '.join(conds)

    try:
        with get_db_handler() as db:
            total_row = db.execute_single_query(
                f'SELECT COUNT(*) AS total FROM departments d WHERE {where}', tuple(params)
            )
            total = total_row['total'] if total_row else 0

            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                'SELECT d.id, d.name, d.leader, d.status, d.remark, d.sort, '
                'd.create_time, d.update_time '
                f'FROM departments d WHERE {where} '
                'ORDER BY d.sort ASC, d.id ASC LIMIT %s OFFSET %s',
                tuple(params) + (pageSize, offset),
            )
            for r in rows:
                for f in ('create_time', 'update_time'):
                    r[f] = _fmt_time(r.get(f))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f'数据库查询失败: {str(e)}')

    return {
        'code': 200,
        'msg': 'success',
        'data': rows,
        'totalCount': total,
        'total': total,
    }


@router.get('/options', summary='部门下拉选项（仅启用）')
def dept_options():
    """用户新增/编辑的「部门」下拉专用：返回启用部门 [{id, name}]，不带分页。"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, name FROM departments '
                'WHERE is_del = 0 AND status = 1 ORDER BY sort ASC, id ASC'
            )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f'数据库查询失败: {str(e)}')
    return {'code': 200, 'msg': 'success', 'data': rows}


@router.post('/add', summary='新增部门')
def add_dept(body: DepartmentIn):
    name = (body.name or '').strip()
    if not name:
        return {'code': 500, 'msg': '部门名称不能为空', 'data': None}
    try:
        with get_db_handler() as db:
            if db.execute_single_query(
                'SELECT id FROM departments WHERE name=%s AND is_del=0', (name,)
            ):
                return {'code': 500, 'msg': '部门名称已存在', 'data': None}
            db.execute_update(
                'INSERT INTO departments (name, leader, status, remark, sort) '
                'VALUES (%s, %s, %s, %s, %s)',
                (name, body.leader or '', body.status, body.remark or '', body.sort),
            )
    except Exception as e:
        logger.error(f'新增部门失败: {e}')
        raise HTTPException(status_code=500, detail=f'新增部门失败: {str(e)}')
    return {'code': 200, 'msg': '新增成功', 'data': None}


@router.post('/update', summary='编辑部门')
def update_dept(body: DepartmentIn):
    if not body.id:
        return {'code': 500, 'msg': '缺少部门 id', 'data': None}
    name = (body.name or '').strip()
    if not name:
        return {'code': 500, 'msg': '部门名称不能为空', 'data': None}
    try:
        with get_db_handler() as db:
            if not db.execute_single_query(
                'SELECT id FROM departments WHERE id=%s AND is_del=0', (body.id,)
            ):
                return {'code': 500, 'msg': '部门不存在', 'data': None}
            dup = db.execute_single_query(
                'SELECT id FROM departments WHERE name=%s AND id<>%s AND is_del=0',
                (name, body.id),
            )
            if dup:
                return {'code': 500, 'msg': '部门名称已存在', 'data': None}
            db.execute_update(
                'UPDATE departments SET name=%s, leader=%s, status=%s, remark=%s, sort=%s '
                'WHERE id=%s',
                (name, body.leader or '', body.status, body.remark or '', body.sort, body.id),
            )
    except Exception as e:
        logger.error(f'编辑部门失败: {e}')
        raise HTTPException(status_code=500, detail=f'编辑部门失败: {str(e)}')
    return {'code': 200, 'msg': '修改成功', 'data': None}


@router.post('/delete', summary='删除部门（软删，校验用户关联）')
def delete_dept(body: IdsIn):
    if not body.ids:
        return {'code': 500, 'msg': '未选择要删除的部门', 'data': None}
    try:
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(body.ids))
            # 决策④：删除前校验是否有用户关联该部门，有关联则拒绝
            ref = db.execute_single_query(
                f'SELECT COUNT(*) AS c FROM users WHERE department_id IN ({placeholders})',
                tuple(body.ids),
            )
            if ref and ref['c']:
                return {
                    'code': 500,
                    'msg': f'有 {ref["c"]} 个用户仍关联该部门，无法删除',
                    'data': None,
                }
            db.execute_update(
                f'UPDATE departments SET is_del = 1 WHERE id IN ({placeholders})',
                tuple(body.ids),
            )
    except Exception as e:
        logger.error(f'删除部门失败: {e}')
        raise HTTPException(status_code=500, detail=f'删除部门失败: {str(e)}')
    return {'code': 200, 'msg': '删除成功', 'data': None}
