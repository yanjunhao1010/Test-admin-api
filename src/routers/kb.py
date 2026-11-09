# -*- coding: utf-8 -*-
"""测试用例知识库接口。

功能（一期）：列表(项目/类型/关键词/标签筛选+分页)、详情、新增/更新、逻辑删除、
从正式用例批量收录、从 AI 用例库(已采纳)批量收录、引用(ref_count+1)。

权限模型（D1，团队共享）：所有人可读可引用可检索；新增时记录 creator_id/owner_id；
编辑/删除不强制作者校验（团队资产，登录即可）。二期再接 RAG。
"""
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import List, Optional

from .auth import parse_token
from src.comment import MySqlconection, yaml_data

router = APIRouter(prefix='/kb', tags=['kb'])

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'], data['mysql']['password'], data['mysql']['database']
    )


def _uid(request: Request) -> int:
    token = request.headers.get('accessToken')
    return parse_token(token) if token else 0


# ----------------------------- 请求体 -----------------------------
class KbSave(BaseModel):
    id: Optional[int] = None
    title: str
    project_id: int
    module: str = ''
    function_point: str = ''
    priority: int = 2
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    kb_type: int = 1
    tags: str = ''
    scene_desc: str = ''


class KbDelete(BaseModel):
    id: int


class KbCollectCases(BaseModel):
    ids: List[int]


class KbCollectDrafts(BaseModel):
    ids: List[int]
    project_id: int


class KbReference(BaseModel):
    id: int


# ----------------------------- 列表 -----------------------------
@router.get('/list')
async def kb_list(
    request: Request,
    project_id: str = '',
    kb_type: int = 0,
    keyword: str = '',
    tags: str = '',
    pageNo: int = 1,
    pageSize: int = 20,
):
    _uid(request)
    where = 'is_del=0 AND status=1'
    args = []
    if project_id:
        ids = [x for x in str(project_id).split(',') if x.strip().isdigit()]
        if ids:
            where += ' AND project_id IN (%s)' % ','.join(['%s'] * len(ids))
            args += [int(x) for x in ids]
    if kb_type:
        where += ' AND kb_type=%s'
        args.append(int(kb_type))
    if keyword:
        where += (
            ' AND (title LIKE %s OR function_point LIKE %s OR module LIKE %s '
            'OR steps LIKE %s OR expected_result LIKE %s OR pre_condition LIKE %s)'
        )
        kw = '%%%s%%' % keyword
        args += [kw] * 6
    if tags:
        where += ' AND tags LIKE %s'
        args.append('%%%s%%' % tags)

    with get_db_handler() as db:
        cnt = db.execute_query('SELECT COUNT(*) AS n FROM test_case_kb WHERE %s' % where, tuple(args))
        total = cnt[0]['n'] if cnt else 0
        page = max(1, int(pageNo))
        size = max(1, min(int(pageSize), 200))
        rows = db.execute_query(
            'SELECT * FROM test_case_kb WHERE %s ORDER BY id DESC LIMIT %s OFFSET %s'
            % (where, size, (page - 1) * size),
            tuple(args),
        )
    return {'code': 200, 'msg': 'success', 'data': {'list': rows, 'total': total}}


# ----------------------------- 详情 -----------------------------
@router.get('/detail')
async def kb_detail(id: int):
    with get_db_handler() as db:
        rows = db.execute_query('SELECT * FROM test_case_kb WHERE id=%s AND is_del=0', (id,))
    if not rows:
        return {'code': 404, 'msg': '知识条目不存在', 'data': None}
    return {'code': 200, 'msg': 'success', 'data': rows[0]}


# ----------------------------- 统计卡（F8） -----------------------------
@router.get('/stats')
async def kb_stats(request: Request):
    _uid(request)
    with get_db_handler() as db:
        total = db.execute_query(
            'SELECT COUNT(*) AS n FROM test_case_kb WHERE is_del=0 AND status=1'
        )[0]['n']
        ref_total = db.execute_query(
            'SELECT COALESCE(SUM(ref_count), 0) AS n FROM test_case_kb WHERE is_del=0 AND status=1'
        )[0]['n']
        by_type = db.execute_query(
            'SELECT kb_type, COUNT(*) AS n FROM test_case_kb WHERE is_del=0 AND status=1 GROUP BY kb_type'
        )
        by_source = db.execute_query(
            'SELECT source_type, COUNT(*) AS n FROM test_case_kb WHERE is_del=0 AND status=1 GROUP BY source_type'
        )
        by_project = db.execute_query(
            'SELECT project_id, COUNT(*) AS n FROM test_case_kb WHERE is_del=0 AND status=1 '
            'GROUP BY project_id ORDER BY n DESC LIMIT 8'
        )
        top_referenced = db.execute_query(
            'SELECT id, title, kb_type, ref_count FROM test_case_kb '
            'WHERE is_del=0 AND status=1 AND ref_count > 0 ORDER BY ref_count DESC LIMIT 5'
        )
    return {
        'code': 200,
        'msg': 'success',
        'data': {
            'total': total,
            'ref_total': ref_total,
            'by_type': by_type,
            'by_source': by_source,
            'by_project': by_project,
            'top_referenced': top_referenced,
        },
    }


# ----------------------------- 新增 / 更新 -----------------------------
@router.post('/save')
async def kb_save(request: Request, body: KbSave):
    uid = _uid(request)
    if body.id:
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE test_case_kb SET title=%s, project_id=%s, module=%s, function_point=%s, '
                'priority=%s, pre_condition=%s, steps=%s, expected_result=%s, kb_type=%s, '
                'tags=%s, scene_desc=%s, update_time=NOW() WHERE id=%s AND is_del=0',
                (
                    body.title, body.project_id, body.module, body.function_point, body.priority,
                    body.pre_condition, body.steps, body.expected_result, body.kb_type,
                    body.tags, body.scene_desc, body.id,
                ),
            )
        return {'code': 200, 'msg': '已更新', 'data': {'id': body.id}}

    with get_db_handler() as db:
        db.execute_update(
            'INSERT INTO test_case_kb '
            '(title, project_id, module, function_point, priority, pre_condition, steps, '
            'expected_result, kb_type, tags, scene_desc, source_type, source_ref_id, scope, '
            'owner_id, creator_id) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NULL,\'global\',%s,%s)',
            (
                body.title, body.project_id, body.module, body.function_point, body.priority,
                body.pre_condition, body.steps, body.expected_result, body.kb_type,
                body.tags, body.scene_desc, uid, uid,
            ),
        )
        new_id = db.execute_query('SELECT LAST_INSERT_ID() AS n')[0]['n']
    return {'code': 200, 'msg': '已新增', 'data': {'id': new_id}}


# ----------------------------- 逻辑删除 -----------------------------
@router.post('/delete')
async def kb_delete(body: KbDelete):
    with get_db_handler() as db:
        db.execute_update('UPDATE test_case_kb SET is_del=1, status=0 WHERE id=%s', (body.id,))
    return {'code': 200, 'msg': '已删除', 'data': None}


# ----------------------------- 从正式用例收录 -----------------------------
@router.post('/collectFromCases')
async def kb_collect_cases(request: Request, body: KbCollectCases):
    uid = _uid(request)
    ids = [int(x) for x in body.ids if str(x).isdigit()]
    if not ids:
        return {'code': 400, 'msg': '请选择要收录的用例', 'data': None}
    placeholders = ','.join(['%s'] * len(ids))
    with get_db_handler() as db:
        rows = db.execute_query(
            'SELECT id, project_id, module, function_point, priority, pre_condition, steps, '
            'expected_result, remark FROM test_cases WHERE id IN (%s) AND is_del=0' % placeholders,
            tuple(ids),
        )
        added, skipped = 0, 0
        for r in rows:
            hit = db.execute_query(
                'SELECT id FROM test_case_kb WHERE source_type=1 AND source_ref_id=%s AND is_del=0',
                (r['id'],),
            )
            if hit:
                skipped += 1
                continue
            title = (r.get('function_point') or r.get('module') or '未命名用例')
            db.execute_update(
                'INSERT INTO test_case_kb '
                '(title, project_id, module, function_point, priority, pre_condition, steps, '
                'expected_result, scene_desc, kb_type, source_type, source_ref_id, scope, '
                'owner_id, creator_id) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,\'global\',%s,%s)',
                (
                    title, r['project_id'], r.get('module') or '', r.get('function_point') or '',
                    r.get('priority') or 2, r.get('pre_condition') or '', r.get('steps') or '',
                    r.get('expected_result') or '', r.get('remark') or '', r['id'], uid, uid,
                ),
            )
            added += 1
    return {
        'code': 200,
        'msg': '已收录 %d 条，跳过重复 %d 条' % (added, skipped),
        'data': {'added': added, 'skipped': skipped},
    }


# ----------------------------- 从 AI 用例库(已采纳)收录 -----------------------------
@router.post('/collectFromDrafts')
async def kb_collect_drafts(request: Request, body: KbCollectDrafts):
    uid = _uid(request)
    if not body.project_id:
        return {'code': 400, 'msg': '请选择目标项目', 'data': None}
    ids = [int(x) for x in body.ids if str(x).isdigit()]
    if not ids:
        return {'code': 400, 'msg': '请选择要收录的草稿', 'data': None}
    placeholders = ','.join(['%s'] * len(ids))
    with get_db_handler() as db:
        rows = db.execute_query(
            'SELECT id, module, function_point, priority, pre_condition, case_steps, '
            'expected_result, remark FROM ai_case_drafts '
            'WHERE id IN (%s) AND status=1' % placeholders,
            tuple(ids),
        )
        added, skipped = 0, 0
        for r in rows:
            hit = db.execute_query(
                'SELECT id FROM test_case_kb WHERE source_type=2 AND source_ref_id=%s AND is_del=0',
                (r['id'],),
            )
            if hit:
                skipped += 1
                continue
            title = (r.get('function_point') or r.get('module') or '未命名用例')
            db.execute_update(
                'INSERT INTO test_case_kb '
                '(title, project_id, module, function_point, priority, pre_condition, steps, '
                'expected_result, scene_desc, kb_type, source_type, source_ref_id, scope, '
                'owner_id, creator_id) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,2,%s,\'global\',%s,%s)',
                (
                    title, body.project_id, r.get('module') or '', r.get('function_point') or '',
                    r.get('priority') or 2, r.get('pre_condition') or '', r.get('case_steps') or '',
                    r.get('expected_result') or '', r.get('remark') or '', r['id'], uid, uid,
                ),
            )
            added += 1
    return {
        'code': 200,
        'msg': '已收录 %d 条，跳过重复 %d 条' % (added, skipped),
        'data': {'added': added, 'skipped': skipped},
    }


# ----------------------------- 引用（复用统计） -----------------------------
@router.post('/reference')
async def kb_reference(body: KbReference):
    with get_db_handler() as db:
        db.execute_update(
            'UPDATE test_case_kb SET ref_count=ref_count+1 WHERE id=%s AND is_del=0', (body.id,)
        )
        rows = db.execute_query('SELECT * FROM test_case_kb WHERE id=%s AND is_del=0', (body.id,))
    if not rows:
        return {'code': 404, 'msg': '知识条目不存在', 'data': None}
    return {'code': 200, 'msg': '已引用', 'data': rows[0]}
