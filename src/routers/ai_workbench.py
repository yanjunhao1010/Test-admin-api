"""AI 工作台路由（一期）

覆盖：模型中心（含个人 Key、连通性探测）、提示词管理、AI 生成用例、
AI 用例库（草稿池 → 人工采纳入库）、接口执行总结、需求文档分析与上传、
调用日志与用量统计、配额与脱敏设置。

前缀 /api/ai（main.py 里 include_router 时挂 /api）。
所有 AI 调用同步执行，超时由 ai_models.timeout / ai_settings.per_call_max_tokens 控制。
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import io
import math
import openpyxl
import pandas as pd
import pymysql
import re

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

from src.ai import coverage as coverage_mod
from src.ai import crypto
from src.ai import docs as docs_mod
from src.ai import models as model_reg
from src.ai import prompts as prompt_mod
from src.ai import runner
from src.ai import schema as schema_mod
from src.ai import settings as settings_mod
from src.ai import sqlgen as sqlgen_mod
from src.ai.errors import AIConfigError, AIError, AIQuotaError, AISchemaError, AIUpstreamError
from src.comment import MySqlconection, yaml_data
from src.routers.auth import parse_token
# 通知中心：AI 生成完成触发点（仅追加调用，不阻断主业务）
from src.routers.notifications import notify
from src.routers.uploadfiles import normalize_priority, safe_convert_dataframe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix='/ai', tags=['ai'])

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def get_db_handler():
    # 配置走缓存：原来每个请求都读一次 config.yml + 解析，纯浪费
    data = MySqlconection.load_config_cached(CONFIG_PATH)
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )


def _uid(request: Request) -> int:
    token = request.headers.get('accessToken')
    if not token:
        return 0
    try:
        return parse_token(token) or 0
    except Exception:
        return 0


def _ok(data: Any = None, msg: str = 'success') -> Dict[str, Any]:
    return {'code': 200, 'msg': msg, 'data': data}


# ---------------------------------------------------------------------------
# AI 在途任务锁（ai_inflight）
# 用于「刷新/切页后仍判定在途」：结果仍同步返回（不引入异步任务队列），
# 这里只登记「谁正在跑哪个 AI 任务」，既防重复提交，又供前端挂载时禁用按钮。
# 唯一键 (biz_type, owner_uid, ref_id)；进程重启时由 main.py 启动钩子全表清空。
# ---------------------------------------------------------------------------
def _inflight_upsert(db, biz_type: str, uid: int, ref_id: Any = None) -> bool:
    """登记在途任务。返回 True=登记成功；False=已存在同键（视为重复提交）。"""
    ref = str(ref_id) if ref_id is not None else ''
    try:
        db.execute_update(
            'INSERT INTO ai_inflight (biz_type, owner_uid, ref_id, started_at) '
            'VALUES (%s, %s, %s, NOW())', (biz_type, uid, ref))
        return True
    except pymysql.err.IntegrityError:
        return False


def inflight_mark(db, biz_type: str, uid: int, ref_id: Any = None) -> bool:
    """对外登记入口。"""
    return _inflight_upsert(db, biz_type, uid, ref_id)


def inflight_clear(db, biz_type: str, uid: int, ref_id: Any = None) -> None:
    ref = str(ref_id) if ref_id is not None else ''
    db.execute_update(
        'DELETE FROM ai_inflight WHERE biz_type = %s AND owner_uid = %s AND ref_id = %s',
        (biz_type, uid, ref))


def inflight_list(db, uid: int, biz_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if biz_types:
        ph = ','.join(['%s'] * len(biz_types))
        rows = db.execute_query(
            f'SELECT biz_type, ref_id FROM ai_inflight '
            f'WHERE owner_uid = %s AND biz_type IN ({ph})', (uid, *biz_types))
    else:
        rows = db.execute_query(
            'SELECT biz_type, ref_id FROM ai_inflight WHERE owner_uid = %s', (uid,))
    return [{'biz_type': r['biz_type'], 'ref_id': r.get('ref_id', '')} for r in (rows or [])]


def _raise_ai(e: Exception):
    """把底座异常映射成合适的 HTTP 状态码（不再一律 500）"""
    if isinstance(e, AIQuotaError):
        raise HTTPException(status_code=429, detail=str(e))
    if isinstance(e, AISchemaError):
        raise HTTPException(status_code=422, detail=str(e))
    if isinstance(e, AIConfigError):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, AIUpstreamError):
        raise HTTPException(status_code=502, detail=str(e))
    if isinstance(e, AIError):
        raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=500, detail=str(e))


def _has_perm(db, uid: int, code: str) -> bool:
    """按权限点校验（admin 角色直接放行）"""
    if not uid:
        return False
    try:
        rows = db.execute_query(
            "SELECT 1 AS ok FROM user_roles ur "
            "JOIN roles r ON r.id = ur.role_id "
            "WHERE ur.user_id = %s AND r.code LIKE '%%admin%%' LIMIT 1", (uid,))
        if rows:
            return True
        rows = db.execute_query(
            "SELECT 1 AS ok FROM user_roles ur "
            "JOIN role_permissions rp ON rp.role_id = ur.role_id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "WHERE ur.user_id = %s AND p.code = %s LIMIT 1", (uid, code))
        return bool(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning('权限校验失败，保守放行：%s', e)
        return True


def _is_admin(db, uid: int) -> bool:
    """当前用户是否 admin 角色（roles.code 含 'admin'）"""
    if not uid:
        return False
    try:
        rows = db.execute_query(
            "SELECT 1 AS ok FROM user_roles ur "
            "JOIN roles r ON r.id = ur.role_id "
            "WHERE ur.user_id = %s AND r.code LIKE '%%admin%%' LIMIT 1", (uid,))
        return bool(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning('admin 角色校验失败，保守拒绝：%s', e)
        return False


def _insert_id(db, sql: str, params) -> int:
    """INSERT 并返回自增 id（DatabaseHandler 只返回影响行数）"""
    db._ensure_connection()  # noqa: SLF001  同项目内复用连接
    with db.connection.cursor() as cur:
        cur.execute(sql, params)
        new_id = cur.lastrowid
    db.connection.commit()
    return new_id


def _batch_id() -> str:
    return time.strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:6]


def _clip(text: Any, size: int) -> str:
    if text is None:
        return ''
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return s if len(s) <= size else s[:size] + '…(已截断)'


def _json_loads(raw: Any, default: Any = None):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _int_list(raw: Any) -> List[int]:
    """'1,2,3' / [1,2] → [1,2]，过滤非法值，避免把输入拼进 SQL"""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else str(raw).split(',')
    out = []
    for i in items:
        try:
            out.append(int(i))
        except Exception:
            continue
    return out


# ===========================================================================
# 一、模型中心
# ===========================================================================
class ModelSave(BaseModel):
    id: Optional[int] = None
    name: str
    provider: str = 'custom'
    base_url: str
    api_key: str = ''
    model_name: str
    model_type: str = 'chat'
    scope: int = 1                      # 0 全局（需权限） 1 个人
    temperature: float = 0.30
    max_tokens: Optional[int] = None
    timeout: int = 300
    priority: int = 10
    is_default: int = 0
    enabled: int = 1
    remark: Optional[str] = ''
    cost_per_1k: Optional[float] = 0.0   # 每千 token 估算费用（元），0 表示未配置


@router.get('/models')
def list_models(request: Request):
    """当前用户可见的模型：全局（只读）+ 自己名下的"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            rows = model_reg.list_models(db, uid)
            items = [model_reg.to_public(r) for r in rows]
            return _ok({
                'list': items,
                'uid': uid,
                'can_manage_global': _has_perm(db, uid, 'menu:ai:model'),
            })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询模型列表失败：%s' % e)


@router.post('/models')
def create_model(request: Request, data: ModelSave):
    try:
        uid = _uid(request)
        base_url = (data.base_url or '').strip().rstrip('/')
        if not base_url.startswith('http'):
            raise HTTPException(status_code=400, detail='接口地址必须以 http/https 开头')
        if not data.model_name.strip():
            raise HTTPException(status_code=400, detail='模型名不能为空')
        # 本地模型（Ollama/vLLM）可留空 key；非本地必须填
        if not data.api_key.strip() and not _is_local_url(base_url):
            raise HTTPException(status_code=400, detail='API Key 不能为空（本地模型可留空）')
        if not data.base_url.strip():
            raise HTTPException(status_code=400, detail='接口地址不能为空')

        scope = 0 if int(data.scope) == 0 else 1
        with get_db_handler() as db:
            if scope == 0 and not _has_perm(db, uid, 'menu:ai:model'):
                raise HTTPException(status_code=403, detail='无权添加全局模型（需要「全局模型管理」权限）')
            owner = 0 if scope == 0 else uid
            enc = crypto.encrypt(data.api_key.strip()) if data.api_key.strip() else ''
            new_id = _insert_id(
                db,
                'INSERT INTO ai_models (name, provider, base_url, api_key_enc, model_name, model_type, '
                'owner_user_id, scope, temperature, max_tokens, timeout, priority, is_default, enabled, '
                'remark, cost_per_1k, create_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (data.name.strip(), data.provider or 'custom', base_url, enc, data.model_name.strip(),
                 'reasoner' if (data.model_type or '').lower() == 'reasoner' else 'chat',
                 owner, scope, data.temperature, data.max_tokens, data.timeout, data.priority,
                 int(data.is_default), int(data.enabled), data.remark or '', float(data.cost_per_1k or 0), uid),
            )
            if int(data.is_default):
                model_reg.set_default(db, new_id, uid)
            return _ok({'id': new_id}, '模型已添加')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='添加模型失败：%s' % e)


@router.put('/models')
def update_model(request: Request, data: ModelSave):
    try:
        if not data.id:
            raise HTTPException(status_code=400, detail='缺少模型 id')
        uid = _uid(request)
        with get_db_handler() as db:
            row = model_reg.get_row(db, data.id)
            if not row:
                raise HTTPException(status_code=404, detail='模型不存在或已删除')
            owner = row.get('owner_user_id') or 0
            if owner == 0 and not _has_perm(db, uid, 'menu:ai:model'):
                raise HTTPException(status_code=403, detail='无权修改全局模型（需要「全局模型管理」权限）')
            if owner != 0 and owner != uid:
                raise HTTPException(status_code=403, detail='无权修改他人的个人模型')

            base_url = (data.base_url or '').strip().rstrip('/')
            if not base_url.startswith('http'):
                raise HTTPException(status_code=400, detail='接口地址必须以 http/https 开头')

            # 密钥留空或回传掩码 = 不修改
            if crypto.is_mask_value(data.api_key):
                db.execute_update(
                    'UPDATE ai_models SET name=%s, provider=%s, base_url=%s, model_name=%s, model_type=%s, '
                    'temperature=%s, max_tokens=%s, timeout=%s, priority=%s, enabled=%s, remark=%s, '
                    'cost_per_1k=%s WHERE id=%s',
                    (data.name.strip(), data.provider or 'custom', base_url, data.model_name.strip(),
                     'reasoner' if (data.model_type or '').lower() == 'reasoner' else 'chat',
                     data.temperature, data.max_tokens, data.timeout, data.priority,
                     int(data.enabled), data.remark or '', float(data.cost_per_1k or 0), data.id),
                )
            else:
                db.execute_update(
                    'UPDATE ai_models SET name=%s, provider=%s, base_url=%s, api_key_enc=%s, model_name=%s, '
                    'model_type=%s, temperature=%s, max_tokens=%s, timeout=%s, priority=%s, enabled=%s, '
                    'remark=%s, cost_per_1k=%s WHERE id=%s',
                    (data.name.strip(), data.provider or 'custom', base_url,
                     crypto.encrypt(data.api_key.strip()), data.model_name.strip(),
                     'reasoner' if (data.model_type or '').lower() == 'reasoner' else 'chat',
                     data.temperature, data.max_tokens, data.timeout, data.priority,
                     int(data.enabled), data.remark or '', float(data.cost_per_1k or 0), data.id),
                )
            if int(data.is_default):
                model_reg.set_default(db, data.id, uid)
            return _ok(None, '模型已更新')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='更新模型失败：%s' % e)


@router.post('/models/default')
async def set_default_model(request: Request):
    body = await request.json()
    model_id = body.get('id') or body.get('model_id')
    uid = _uid(request)
    try:
        with get_db_handler() as db:
            row = model_reg.get_row(db, model_id)
            if not row:
                raise HTTPException(status_code=404, detail='模型不存在或已删除')
            owner = row.get('owner_user_id') or 0
            if owner == 0 and not _has_perm(db, uid, 'menu:ai:model'):
                raise HTTPException(status_code=403, detail='无权设置全局默认模型')
            if owner != 0 and owner != uid:
                raise HTTPException(status_code=403, detail='无权修改他人的个人模型')
            model_reg.set_default(db, model_id, uid)
            return _ok(None, '已设为默认模型')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='设置默认模型失败：%s' % e)


@router.post('/models/delete')
async def delete_model(request: Request):
    body = await request.json()
    model_id = body.get('id') or body.get('model_id')
    uid = _uid(request)
    try:
        with get_db_handler() as db:
            row = model_reg.get_row(db, model_id)
            if not row:
                raise HTTPException(status_code=404, detail='模型不存在或已删除')
            owner = row.get('owner_user_id') or 0
            if owner == 0 and not _has_perm(db, uid, 'menu:ai:model'):
                raise HTTPException(status_code=403, detail='无权删除全局模型')
            if owner != 0 and owner != uid:
                raise HTTPException(status_code=403, detail='无权删除他人的个人模型')
            # 被调用过的不能删（否则历史日志与报告的 model_id 直接悬空），只能停用
            rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_logs WHERE model_id = %s', (model_id,))
            used = rows[0]['n'] if rows else 0
            if used:
                raise HTTPException(
                    status_code=400,
                    detail='该模型已被调用过 %d 次，不能删除；请改为「停用」（历史记录需要保留模型信息）' % used)
            db.execute_update('UPDATE ai_models SET is_del = 1, enabled = 0 WHERE id = %s', (model_id,))
            return _ok(None, '模型已删除')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='删除模型失败：%s' % e)


@router.post('/models/{model_id}/test')
def test_model(request: Request, model_id: int):
    """连通性 + 能力探测：真实发一次极小请求"""
    try:
        uid = _uid(request)
        # AI 在途锁：同一模型探测期间拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'test_model', uid, model_id):
                raise HTTPException(status_code=409, detail='该模型正在探测中，请稍候')
        with get_db_handler() as db:
            row = model_reg.get_row(db, model_id)
            if not row:
                raise HTTPException(status_code=404, detail='模型不存在或已删除')
            if not model_reg.can_use(row, uid):
                raise HTTPException(status_code=403, detail='无权测试他人的个人模型')
            result = model_reg.test_and_save(db, model_id)
            return _ok(result, '探测完成' if result.get('ok') else '探测失败')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='探测失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'test_model', uid, model_id)


# ===========================================================================
# 二、提示词管理
# ===========================================================================
class PromptSave(BaseModel):
    id: Optional[int] = None
    code: str
    name: str
    model_type: str = 'chat'
    temperature: float = 0.30
    system_prompt: Optional[str] = ''
    user_prompt_template: Optional[str] = ''
    output_schema: Optional[str] = ''
    description: Optional[str] = ''
    enabled: int = 1
    scope: str = 'global'  # global=全局默认 personal=个人专属(owner=create_by)


class PromptRestore(BaseModel):
    code: str
    scope: str = 'global'  # global=恢复全局默认 personal=仅恢复本人专属


@router.get('/prompts')
def list_prompts(request: Request):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            # 只返回：全局默认(global) 全部 + 本人个人专属(personal, create_by=uid)
            rows = db.execute_query(
                'SELECT id, code, name, model_type, version, temperature, system_prompt, '
                'user_prompt_template, output_schema, description, enabled, update_time, '
                'scope, create_by FROM ai_prompts '
                'WHERE (is_del = 0 AND scope = \'global\') '
                'OR (scope = \'personal\' AND create_by = %s AND is_del = 0 AND enabled = 1) '
                'ORDER BY code ASC, version DESC', (uid,))
            items = []
            for r in rows or []:
                r['temperature'] = float(r['temperature']) if r.get('temperature') is not None else None
                r['is_builtin'] = 0
                r['scope'] = r.get('scope') or 'global'
                items.append(r)
            # 内置模板：global 表里没有对应 code 时补一条「内置默认」便于查看与恢复
            global_codes = {r['code'] for r in items if r['scope'] == 'global'}
            for code, cfg in prompt_mod.BUILTIN_PROMPTS.items():
                if code in global_codes:
                    continue
                items.append({
                    'id': None, 'code': code, 'name': cfg['name'],
                    'model_type': cfg.get('model_type', 'chat'), 'version': 0,
                    'temperature': cfg.get('temperature'), 'system_prompt': cfg.get('system'),
                    'user_prompt_template': cfg.get('user'),
                    'output_schema': prompt_mod.schema_text(cfg.get('output_schema')),
                    'description': cfg.get('description'), 'enabled': 1, 'is_builtin': 1,
                    'update_time': None, 'scope': 'global',
                })
            return _ok({'list': items})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询提示词失败：%s' % e)


@router.post('/prompts')
def save_prompt(request: Request, data: PromptSave):
    """保存提示词。

    - scope='personal'：本人专属，编辑时在同一条记录上覆盖更新（不新增版本、不增 version）
    - scope='global'：全局默认，仅 admin 角色可改，保留版本历史（每次新增一版并停用旧版）
    """
    try:
        uid = _uid(request)
        if not data.code.strip():
            raise HTTPException(status_code=400, detail='模板编码不能为空')
        scope = (data.scope or 'global').strip().lower()
        if scope not in ('global', 'personal'):
            raise HTTPException(status_code=400, detail='scope 必须为 global 或 personal')
        with get_db_handler() as db:
            if scope == 'global':
                # 仅 admin 角色可改全局默认（保留版本历史）
                if not _is_admin(db, uid):
                    raise HTTPException(status_code=403, detail='仅管理员可修改全局默认提示词')
                rows = db.execute_query(
                    'SELECT MAX(version) AS v FROM ai_prompts WHERE code = %s', (data.code.strip(),))
                version = ((rows[0]['v'] or 0) + 1) if rows else 1
                new_id = _insert_id(
                    db,
                    'INSERT INTO ai_prompts (code, name, model_type, version, temperature, system_prompt, '
                    'user_prompt_template, output_schema, description, enabled, create_by, scope) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (data.code.strip(), data.name.strip(), data.model_type, version, data.temperature,
                     data.system_prompt or '', data.user_prompt_template or '', data.output_schema or '',
                     data.description or '', int(data.enabled), uid, scope),
                )
                # 只保留同 (code, scope='global') 下最新版本启用
                db.execute_update(
                    'UPDATE ai_prompts SET enabled = 0 '
                    'WHERE code = %s AND scope = \'global\' AND id <> %s AND is_del = 0',
                    (data.code.strip(), new_id))
                return _ok({'id': new_id, 'version': version, 'scope': 'global'},
                           '已保存为全局默认 v%d' % version)
            else:
                # personal：本人专属，编辑覆盖同一条记录，不新增版本
                if not uid:
                    raise HTTPException(status_code=403, detail='请先登录')
                existing = db.execute_query(
                    'SELECT id, version FROM ai_prompts '
                    'WHERE code = %s AND scope = \'personal\' AND create_by = %s AND is_del = 0 '
                    'ORDER BY version DESC LIMIT 1',
                    (data.code.strip(), uid))
                if existing:
                    pid = existing[0]['id']
                    cur_ver = existing[0]['version'] or 1
                    db.execute_update(
                        'UPDATE ai_prompts SET name=%s, model_type=%s, temperature=%s, '
                        'system_prompt=%s, user_prompt_template=%s, output_schema=%s, '
                        'description=%s, enabled=%s, update_time=NOW() '
                        'WHERE id=%s',
                        (data.name.strip(), data.model_type, data.temperature,
                         data.system_prompt or '', data.user_prompt_template or '', data.output_schema or '',
                         data.description or '', int(data.enabled), pid))
                    return _ok({'id': pid, 'version': cur_ver, 'scope': 'personal'},
                               '已更新我的专属提示词（v%d）' % cur_ver)
                # 首次创建：version 取该 code 当前最大版本+1，避免与 uk_code_version 唯一键冲突
                vrows = db.execute_query(
                    'SELECT MAX(version) AS v FROM ai_prompts WHERE code = %s', (data.code.strip(),))
                version = ((vrows[0]['v'] or 0) + 1) if vrows else 1
                new_id = _insert_id(
                    db,
                    'INSERT INTO ai_prompts (code, name, model_type, version, temperature, system_prompt, '
                    'user_prompt_template, output_schema, description, enabled, create_by, scope) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (data.code.strip(), data.name.strip(), data.model_type, version, data.temperature,
                     data.system_prompt or '', data.user_prompt_template or '', data.output_schema or '',
                     data.description or '', int(data.enabled), uid, 'personal'))
                return _ok({'id': new_id, 'version': version, 'scope': 'personal'},
                           '已创建我的专属提示词（v%d）' % version)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='保存提示词失败：%s' % e)


@router.post('/prompts/delete')
async def delete_prompt(request: Request):
    body = await request.json()
    pid = body.get('id')
    uid = _uid(request)
    if not pid:
        raise HTTPException(status_code=400, detail='缺少 id')
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, scope, create_by FROM ai_prompts WHERE id = %s AND is_del = 0', (pid,))
            if not rows:
                raise HTTPException(status_code=404, detail='提示词不存在或已删除')
            row = rows[0]
            scope = row.get('scope') or 'global'
            if scope == 'personal':
                if row.get('create_by') != uid:
                    raise HTTPException(status_code=403, detail='只能删除自己的个人专属提示词')
            else:
                if not _is_admin(db, uid):
                    raise HTTPException(status_code=403, detail='仅管理员可删除全局默认提示词')
            db.execute_update('UPDATE ai_prompts SET is_del = 1, enabled = 0 WHERE id = %s', (pid,))
            return _ok(None, '已删除（将回退到%s）'
                       % ('全局默认或内置模板' if scope == 'global' else '全局默认/内置默认模板'))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='删除提示词失败：%s' % e)


@router.post('/prompts/restore')
def restore_prompt(request: Request, data: PromptRestore):
    """恢复为默认模板：

    - scope='personal'：仅停用并软删本人专属版本，回退到全局默认/内置（不需 admin）
    - scope='global'：停用并软删该 code 全部全局版本，回退到内置默认（仅 admin，影响所有用户）
    """
    try:
        uid = _uid(request)
        if not data.code.strip():
            raise HTTPException(status_code=400, detail='模板编码不能为空')
        scope = (data.scope or 'global').strip().lower()
        if scope not in ('global', 'personal'):
            raise HTTPException(status_code=400, detail='scope 必须为 global 或 personal')
        with get_db_handler() as db:
            if scope == 'global':
                if not _is_admin(db, uid):
                    raise HTTPException(status_code=403, detail='仅管理员可恢复全局默认提示词')
                db.execute_update(
                    'UPDATE ai_prompts SET is_del = 1, enabled = 0 '
                    'WHERE code = %s AND scope = \'global\' AND is_del = 0',
                    (data.code.strip(),))
                return _ok(None, '已恢复为系统内置默认模板（影响所有用户）')
            else:
                db.execute_update(
                    'UPDATE ai_prompts SET is_del = 1, enabled = 0 '
                    'WHERE code = %s AND scope = \'personal\' AND create_by = %s AND is_del = 0',
                    (data.code.strip(), uid))
                return _ok(None, '已恢复为全局默认/内置默认模板（仅本人）')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='恢复提示词失败：%s' % e)


# ===========================================================================
# 三、AI 生成测试用例
# ===========================================================================
class CaseGenReq(BaseModel):
    project_id: Optional[int] = None
    project_name: str = ''
    module: str = ''
    function_point: str = ''
    requirement: str = ''
    case_types: str = '正向,异常,边界'
    count: int = 5
    model_id: Optional[int] = None
    case_ids: Optional[List[int]] = None  # 基于已有用例补充生成（A2 溯源：精确拉取来源用例拼进上下文）
    # 分批生成：前端把已勾选的测试点切成若干文本块传进来，后端逐块调用模型再合并，
    # 避免「一次要几百条」把输出撞穿 max_tokens。不传则走原来的单次调用（其他页面不受影响）。
    batches: Optional[List[str]] = None
    # P2 三段式：是否启用「规划 → 分维生成 → 闸门补跑」。不传/为 false 时行为与改造前完全一致。
    use_plan: bool = False
    # 来源需求文档 ID：传了且该需求已有阶段2 测试点，就直接复用测试点当覆盖计划，省一次 G1 调用
    requirement_id: Optional[int] = None
    # 跨批去重预热：前端把「此前批次已生成的用例」回传，后端在判重前先灌进 DedupCollector。
    # ⚠️ 背景：去重器是**整单级**累计的（collector 不能在每批开始时重置，否则跨批重复一条都拦不住）。
    # 前端改成「每批一次请求」后，同一整单的多个请求彼此独立，跨批近似重复会漏网
    # （旧问题：14 条里 7 对近似重复全部漏网，有效信息量只剩一半）。
    # 前端回传已生成条目 → 后端预热 → 跨批去重效果与「一次请求跑完全部批次」保持一致。
    seen_items: Optional[List[Dict[str, Any]]] = None
    # 覆盖计划的批次切片（前端「一批一次请求」用）：
    # plan_indexes = 本批对应的测试点在 stage_payload.extract.test_points 里的**原始下标列表**。
    # 只拿这些下标的测试点当计划，各批计划互不重叠 → 不会每批都产出重复用例。
    # ⚠️ 必须用「离散下标列表」而不是「连续区间」：前端会把用户**没勾选**的测试点过滤掉，
    #    剩下的是离散下标（如 0,2,4,...,38）。若后端按连续区间切，会把中间没勾选的
    #    19 个测试点也带进计划 → 本批的计划与本批的需求文本对不上。
    # ⚠️ 不传的话，每一批都会拿到**同一份完整计划**（实测 70~282 格）→ 每批产出重复用例。
    plan_indexes: Optional[List[int]] = None


def _as_multi_str(v: Any) -> List[str]:
    """把多值字段收敛成 list[str]，容忍字符串/逗号串/null/单值。

    ⚠️ 背景：`ai_case_gen_history_item.dims` / `routes` 是 VARCHAR，存的是 'C04,C14'。
    从「生成历史」入库时，前端把 DB 取出的逗号串原样回传，而 pydantic 声明的是 List[str]
    → 直接报 `Input should be a valid list`，整批入库全失败。
    这里在「入参」侧统一收敛，避免再为同类问题各写一处补丁。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [x for x in re.split(r'[,，;；\s、|]+', v) if x]
    if isinstance(v, (list, tuple, set)):
        out: List[str] = []
        for x in v:
            if x is None:
                continue
            out.extend(_as_multi_str(x) if isinstance(x, str) else [str(x)])
        return out
    return [str(v)]


def _as_str(v: Any) -> str:
    """普通字符串字段容错：DB 里NULL / None 一律收敛成空串。

    与 _as_multi_str 同源的问题：`severity` / `evidence_type` 等列在 DB 里可空，
    前端若原样回传 null，pydantic 声明 str 会报 `Input should be a valid string`。
    """
    if v is None:
        return ''
    return v if isinstance(v, str) else str(v)


class CaseGenSaveItem(BaseModel):
    """前端勾选后回传的单条用例（字段与生成结果对齐）"""
    module: str = ''
    function_point: str = ''
    priority: int = 2
    case_type: str = ''
    design_method: str = ''
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''
    confidence: float = 0.0
    # 覆盖与分级字段（前端从 /case-gen 响应原样回传即可；缺省为空串，老前端不受影响）
    dims: List[str] = []
    routes: List[str] = []
    severity: str = ''
    evidence_type: str = ''
    # 测试点回标编号（TP1/TP3…）：该用例覆盖了来源需求的哪些测试点。
    # 落库后，采纳时据此反查产品功能点并自动写 product_point_cases.tp_indexes，
    # 覆盖率看板的测试点级覆盖才能自动更新（不回传就只是装饰字段）。
    tp_refs: List[str] = []
    # 若该用例来自「生成历史」明细，带上历史明细项 id，
    # 入库后回写 ai_case_gen_history_item.draft_id，用于可靠判定「已加入库」并防重复添加
    history_item_id: Optional[int] = None

    @field_validator('dims', 'routes', mode='before')
    @classmethod
    def _multi(cls, v):
        return _as_multi_str(v)

    @field_validator('tp_refs', mode='before')
    @classmethod
    def _tp(cls, v):
        return schema_mod.normalize_tp_refs(v)

    @field_validator('severity', 'evidence_type', mode='before')
    @classmethod
    def _plain(cls, v):
        return _as_str(v)


class CaseGenSaveReq(BaseModel):
    project_id: Optional[int] = None
    model_id: Optional[int] = None
    items: List[CaseGenSaveItem] = []
    source_case_ids: Optional[List[int]] = None  # A2 溯源：派生自哪些已有用例（存草稿）
    # 来源需求文档 id：回标闭环需要它来定位「这个需求的哪些产品功能点」。
    # tp_refs 里的 TP 编号只在**本需求内**有意义（编号 = 需求测试点下标+1），
    # 缺了需求 id 就无法反查，只能靠全局扫描（重号风险）。
    requirement_id: Optional[int] = None


class CaseGenHistoryItem(BaseModel):
    module: Optional[str] = ''
    function_point: Optional[str] = ''
    priority: int = 2
    case_type: Optional[str] = ''
    design_method: Optional[str] = ''
    pre_condition: Optional[str] = ''
    steps: Optional[str] = ''
    expected_result: Optional[str] = ''
    remark: Optional[str] = ''
    confidence: float = 0
    dup_case_id: Optional[int] = None
    # 覆盖与分级字段（与 ai_case_gen_history_item 新增列对齐）
    dims: List[str] = []
    routes: List[str] = []
    severity: Optional[str] = ''
    evidence_type: Optional[str] = ''
    # 测试点回标编号（TP1/TP3…）：与 ai_case_gen_history_item.tp_refs 对齐
    tp_refs: List[str] = []

    @field_validator('dims', 'routes', mode='before')
    @classmethod
    def _multi(cls, v):
        return _as_multi_str(v)

    @field_validator('tp_refs', mode='before')
    @classmethod
    def _tp(cls, v):
        return schema_mod.normalize_tp_refs(v)

    @field_validator('severity', 'evidence_type', mode='before')
    @classmethod
    def _plain(cls, v):
        return _as_str(v)


class CaseGenHistoryReq(BaseModel):
    requirement_id: Optional[int] = None   # 来源需求文档（需求分析页「据此生成用例」时带）
    project_id: Optional[int] = None
    project_name: Optional[str] = ''
    model_id: Optional[int] = None
    model_name: Optional[str] = ''
    module: Optional[str] = ''
    function_point: Optional[str] = ''
    requirement: Optional[str] = ''
    elapsed_ms: Optional[int] = None
    attempt: int = 1
    knowledge_injected: int = 0
    knowledge_text: str = ''
    items: List[CaseGenHistoryItem] = []
    # 追加到已有历史记录（前端「一批一次请求」用）：
    # 首批不传 → 新建一条记录并返回 history_id；后续批次带上 → 明细追加到同一条记录下。
    # ⚠️ 不加这个的话，逐批入库会产生 N 条独立记录，而「用例生成记录」弹窗
    # 默认只选中最新一条 → 用户只看到最后一批的用例，前面几批要自己去翻。
    history_id: Optional[int] = None


def _few_shot_text(db, module: str, limit: int = 4) -> str:
    """取库内同模块已有用例，作为风格参考（few-shot），保证生成风格一致"""
    if module:
        rows = db.execute_query(
            'SELECT module, function_point, pre_condition, steps, expected_result FROM test_cases '
            'WHERE is_del = 0 AND module = %s ORDER BY id DESC LIMIT %s', (module, limit))
    else:
        rows = db.execute_query(
            'SELECT module, function_point, pre_condition, steps, expected_result FROM test_cases '
            'WHERE is_del = 0 ORDER BY id DESC LIMIT %s', (limit,))
    if not rows:
        return '（库内暂无同模块用例）'
    lines = []
    for r in rows:
        lines.append('模块：%s / 功能点：%s\n前置条件：%s\n步骤：%s\n预期结果：%s'
                     % (r.get('module') or '', r.get('function_point') or '',
                        r.get('pre_condition') or '', r.get('steps') or '',
                        r.get('expected_result') or ''))
    return '\n---\n'.join(lines)


_KB_TYPE_NAME = {1: '通用用例', 2: '测试要点', 3: '典型坑点/边界'}


def _kb_knowledge(db, project_id: Optional[int], module: str = '', function_point: str = '',
                  limit: int = 8) -> tuple:
    """RAG 检索：从用例知识库(test_case_kb)取与本项目/模块/功能点相关、且被引用较多的条目，
    拼成文本作为 case_gen 的 {{knowledge}} 上下文。结构化检索（按 project_id + 模糊匹配），
    不需要 embedding / 向量库。返回 (text, count)：text 为注入用文本，count 为命中条目数；
    无命中返回（提示语, 0），避免污染 prompt。"""
    conds = ['is_del=0 AND status=1']
    args: list = []
    if project_id:
        conds.append('project_id=%s')
        args.append(int(project_id))
    soft = []
    if module:
        soft.append('module LIKE %s')
        args.append('%%%s%%' % module)
    if function_point:
        soft.append('function_point LIKE %s')
        args.append('%%%s%%' % function_point)
    where = ' AND '.join(conds)
    if soft:
        where += ' AND (%s)' % ' OR '.join(soft)
    rows = db.execute_query(
        'SELECT title, kb_type, module, function_point, priority, pre_condition, steps, '
        'expected_result, scene_desc FROM test_case_kb WHERE %s '
        'ORDER BY ref_count DESC, id DESC LIMIT %s' % (where, limit), tuple(args))
    if not rows:
        return '（用例知识库暂无相关条目，可忽略本段）', 0
    lines = []
    for r in rows:
        tn = _KB_TYPE_NAME.get(r.get('kb_type') or 1, '通用用例')
        lines.append(
            '【%s】%s\n模块：%s / 功能点：%s / 优先级：%s\n适用场景：%s\n前置条件：%s\n步骤：%s\n预期结果：%s'
            % (tn, r.get('title') or '未命名', r.get('module') or '', r.get('function_point') or '',
               r.get('priority') or 2, r.get('scene_desc') or '', r.get('pre_condition') or '',
               r.get('steps') or '', r.get('expected_result') or ''))
    return '\n---\n'.join(lines), len(rows)


# 生成用例分批大小：单批输出恒定在 max_tokens 安全区。
# 25 条用例的 JSON 约 5k token，远低于 DeepSeek 通用 16384 的上限；再大就有截断风险。
# ⚠️ 实测踩坑（10/27）：ai_logs id=1026 出现 completion_tokens=16384 撞顶截断，
# 报错「输出被 max_tokens(16384) 截断，JSON 不完整」——**单批 25 条仍会撞穿**。
# 因为 limit 只在解析**之后**裁剪结果、不约束模型输出量（见 runner.py），
# 真正的输出量由 prompt 里的 count 决定，25 条完整用例（含 steps/expected_result 长文本）
# 实测能顶到 16384。降到 20 条留出安全余量。
CASE_GEN_BATCH = 20
# 批间隔（秒）：分批会连续建连同一上游，不间隔容易撞上代理/上游的连接数与频率限制，
# 表现为下一批直接 Connection error。留 1s 足以让连接池与限流窗口恢复。
CASE_GEN_BATCH_GAP = 1.0
# 补跑阈值：单批实得 < 目标 × 该比例 就再补一次。
# 实测模型会「自己觉得够了」提前收尾（要 25 条只回 8 条，JSON 合法且 schema 校验通过）。
# 取 0.5：低于一半才补，避免为偶发少 1~2 条多烧一次调用。
CASE_GEN_SHORTFALL_RATIO = 0.5
CASE_GEN_TOPUP_HINT = (
    '\n\n【补充生成】上一次只给出了部分用例，本次请针对上面尚未覆盖的测试点继续补充，'
    '不要重复已给出的用例，务必输出指定条数。')

# ---------------------------------------------------------------------------
# P2 三段式：G1 case_plan 规划 → G2 case_gen 分维生成 → G3 case_gate 闸门
# ---------------------------------------------------------------------------
#: 闸门补跑轮次上限。**硬编码 1**，从结构上杜绝无限循环的可能（方案验收指标之一）。
CASE_GATE_MAX_ROUNDS = 1
#: 单次生成最多跑多少格。计划可能有 19 格，全跑会让调用次数与耗时失控。
#: 取 8 格 × 每次最多 3 条 ≈ 24 条，与前端默认 30 条量级匹配。
CASE_PLAN_MAX_CELLS = 8
#: 每格最多产几条。格子太大又会把「分维防重复」的优势还回去。
CASE_PLAN_MAX_PER_CELL = 3


def _tp_ref_map(db, requirement_id: Optional[int]) -> Dict[str, List[Tuple[int, int]]]:
    """建立「TP 编号 → [(功能点id, 该功能点内测试点下标), ...]」映射。

    数据来源：product_function_points.test_points 里的 tp_ref 字段
    （转产品功能点时写入，指向来源需求里的测试点下标+1）。

    为什么一个 TP 编号可能对应多个功能点：需求的测试点按 module 拆到多个功能点，
    同一个测试点理论上只属于一个 module，但**历史数据可能重复**（同需求多次转出、
    或转出后手工改过）。所以返回列表而不是单值，反查时全部关联 ——
    多关联只是多算一次覆盖，不会漏；少关联才会造成假缺口。

    requirement_id 为空时扫全部功能点（用例生成页不传需求 id 的场景）。
    """
    if requirement_id:
        rows = db.execute_query(
            'SELECT id, test_points FROM product_function_points '
            'WHERE requirement_id = %s AND is_del = 0', (int(requirement_id),))
    else:
        rows = db.execute_query(
            'SELECT id, test_points FROM product_function_points WHERE is_del = 0')
    out: Dict[str, List[Tuple[int, int]]] = {}
    for r in rows or []:
        pfp_id = int(r['id'])
        try:
            items = json.loads(r['test_points']) if r['test_points'] else []
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            ref = str(it.get('tp_ref') or '').strip()
            if not ref:
                continue
            out.setdefault(ref, []).append((pfp_id, idx))
    return out


def _parse_tp_indexes_local(raw: Any) -> Optional[List[int]]:
    """解析 product_point_cases.tp_indexes（与 product_points._parse_tp_indexes 同口径）。

    刻意**不跨模块 import** product_points：routers 之间互相 import 容易成环
    （product_points 已依赖 ai_workbench 的部分能力），这里本地实现一份保持解耦。
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except Exception:
            return None
    if not isinstance(items, list):
        return None
    out: List[int] = []
    for i in items:
        try:
            n = int(i)
        except Exception:
            continue
        if n >= 0 and n not in out:
            out.append(n)
    return out


def _link_cases_by_tp_refs(db, uid: int, case_id: int,
                           tp_refs: List[str],
                           req_map: Dict[str, List[Tuple[int, int]]]) -> int:
    """按回标编号把用例关联到产品功能点（写 product_point_cases）。

    返回实际新增的关联条数。已存在的跳过（幂等）。

    ⚠️ 关联粒度：
    - 回标到具体测试点 → tp_indexes = [该测试点在功能点内的下标]（精细）
    - 回标到整个需求但找不到具体测试点 → 不关联（宁可不算覆盖，也不乱关联）

    只关联「能精确落到测试点」的编号：找不到映射的编号直接跳过，
    因为关联到错误的功能点比不关联更有害（会让覆盖率虚高）。
    """
    if not tp_refs or not req_map:
        return 0
    linked = 0
    for ref in tp_refs:
        targets = req_map.get(ref) or []
        for pfp_id, tp_idx in targets:
            ex = db.execute_single_query(
                'SELECT id, tp_indexes, is_del FROM product_point_cases '
                'WHERE pfp_id = %s AND case_id = %s',
                (pfp_id, case_id))
            if ex:
                if ex['is_del'] == 1:
                    # 已删除的墓碑行：复活而非 INSERT 新行，
                    # 否则「关联→解绑→再关联」会堆积两条 is_del=1 同键行，
                    # 再次解绑 UPDATE 会撞 uk_pfp_case 唯一键（1062）。
                    db.execute_update(
                        'UPDATE product_point_cases SET is_del=0, creator_id=%s, tp_indexes=%s, update_time=NOW() '
                        'WHERE id=%s', (uid, json.dumps([tp_idx]), ex['id']))
                    linked += 1
                    continue
                # 已生效：把新测试点并进已有覆盖范围
                old_idx = _parse_tp_indexes_local(ex.get('tp_indexes'))
                if old_idx is None:
                    continue   # 已是「整体关联」，无需再加
                if tp_idx in old_idx:
                    continue
                merged = sorted(set(old_idx) | {tp_idx})
                db.execute_update(
                    'UPDATE product_point_cases SET tp_indexes = %s, update_time = NOW() '
                    'WHERE id = %s', (json.dumps(merged), ex['id']))
                linked += 1
                continue
            db.execute_update(
                'INSERT INTO product_point_cases (pfp_id, case_id, creator_id, tp_indexes) '
                'VALUES (%s, %s, %s, %s)',
                (pfp_id, case_id, uid, json.dumps([tp_idx])))
            linked += 1
    return linked


def _norm_tp_title(raw: Any) -> str:
    """归一化测试点标题用于比对：去空白（含全角空格）、统一大小写。

    刻意**本地实现**一份而不从 product_points import：
    product_points 已依赖本模块的 _link_cases_by_tp_refs 等能力，
    反向 import 会成环（同 _parse_tp_indexes_local 的处理方式）。
    """
    s = re.sub(r'[\s　]+', '', str(raw or ''))
    return s.strip().lower()


def _link_case_to_tp_index(db, uid: int, case_id: int,
                           pfp_id: int, tp_idx: int,
                           tp_title: str = '') -> int:
    """按**显式坐标**把用例关联到某个功能点的某个测试点（写 product_point_cases）。

    与 _link_cases_by_tp_refs 的区别：那条通道靠「需求级 TP 编号」反查定位，
    这条通道直接用草稿自带的 (pfp_id, tp_index) 坐标。

    为什么需要这条通道：**手动新增的三级测试点没有回标编号**（不是从需求原文
    拆出来的），无法参与 TP 编号反查；而 TP 编号空间是需求内共享的，
    伪造编号会与真编号冲突、破坏「需求→功能点→用例」的来源串联。
    所以让草稿自带坐标，采纳时按坐标精确落位 —— 编号空间零污染。

    ⚠️ 二次校验标题：草稿生成到采纳之间，测试点可能被删除/重排，
    下标会漂移。此时标题对不上就**拒绝关联**（宁可不算覆盖，
    也不能把用例挂到隔壁测试点上 —— 那比不关联更有害）。

    返回实际写入的关联条数（幂等：已关联且范围相同则 0）。
    """
    if not pfp_id or tp_idx is None or tp_idx < 0:
        return 0
    # 校验目标测试点仍然存在且标题未变（防下标漂移串点）
    prow = db.execute_single_query(
        'SELECT test_points FROM product_function_points WHERE id=%s AND is_del=0',
        (pfp_id,))
    if not prow:
        return 0
    try:
        items = json.loads(prow.get('test_points')) if prow.get('test_points') else []
    except Exception:
        items = []
    if not isinstance(items, list) or tp_idx >= len(items):
        return 0
    cur_title = ''
    it = items[tp_idx]
    if isinstance(it, dict):
        cur_title = str(it.get('title') or '').strip()
    else:
        cur_title = str(it or '').strip()
    if tp_title and cur_title and _norm_tp_title(cur_title) != _norm_tp_title(tp_title):
        logger.warning('显式关联中止：测试点标题已变（下标漂移）pfp=%s idx=%s %r != %r',
                       pfp_id, tp_idx, cur_title, tp_title)
        return 0

    ex = db.execute_single_query(
        'SELECT id, tp_indexes, is_del FROM product_point_cases '
        'WHERE pfp_id = %s AND case_id = %s', (pfp_id, case_id))
    if ex:
        if ex['is_del'] == 1:
            # 墓碑行复活（同 _link_cases_by_tp_refs 的口径，避免撞 uk_pfp_case）
            db.execute_update(
                'UPDATE product_point_cases SET is_del=0, creator_id=%s, tp_indexes=%s, update_time=NOW() '
                'WHERE id=%s', (uid, json.dumps([tp_idx]), ex['id']))
            return 1
        old_idx = _parse_tp_indexes_local(ex.get('tp_indexes'))
        if old_idx is None or tp_idx in old_idx:
            return 0
        merged = sorted(set(old_idx) | {tp_idx})
        db.execute_update(
            'UPDATE product_point_cases SET tp_indexes = %s, update_time = NOW() '
            'WHERE id = %s', (json.dumps(merged), ex['id']))
        return 1
    db.execute_update(
        'INSERT INTO product_point_cases (pfp_id, case_id, creator_id, tp_indexes) '
        'VALUES (%s, %s, %s, %s)',
        (pfp_id, case_id, uid, json.dumps([tp_idx])))
    return 1


def _fix_tp_refs(raw: Any, cell: Dict[str, Any],
                 allowed: Optional[Set[str]] = None) -> List[str]:
    """校正一条用例的测试点回标编号。

    三件事，缺一不可：
    1. **补本格编号**：本格（计划格）对应的测试点必须被标上 ——
       模型经常只写 dims 忘了回标，导致该测试点明明有用例却被算成覆盖缺口。
    2. **剔除非白名单编号**：只保留 `allowed`（当前计划里出现过的编号）内的编号。
       模型偶尔会自己编 'TP99'，存进库里就是一个反查不到的死编号（脏数据）。
    3. **去重保序**：同一编号只留一次。

    计划格没有测试点（tp_ref 为空）时，只保留白名单内的编号，否则清空。
    """
    refs = schema_mod.normalize_tp_refs(raw)
    if allowed is not None:
        refs = [x for x in refs if x in allowed]
    own = (cell or {}).get('tp_ref') or ''
    if own and (allowed is None or own in allowed) and own not in refs:
        refs.append(own)
    return refs


def _plan_text(plan: List[Dict[str, Any]]) -> str:
    """把覆盖计划渲染成 case_gen 的 user 模板变量（plan_text）。"""
    if not plan:
        return ''
    lines = ['\n【本次覆盖计划（必须逐格写足，缺格视为未完成）】']
    for i, p in enumerate(plan, 1):
        bits = []
        if p.get('dim'):
            bits.append('维度 %s %s' % (p['dim'], p.get('dim_name') or ''))
        if p.get('route'):
            bits.append('路线 %s %s' % (p['route'], p.get('route_name') or ''))
        head = '第%d格' % i
        if p.get('tp_ref'):
            head += '（测试点 %s）' % p['tp_ref']
        if bits:
            head += '（%s）' % '，'.join(bits)
        extra = []
        if p.get('case_type'):
            extra.append('类型 %s' % p['case_type'])
        if p.get('design_method'):
            extra.append('设计方法 %s' % p['design_method'])
        if p.get('severity'):
            extra.append('严重度 %s' % p['severity'])
        if p.get('evidence_type'):
            extra.append('证据 %s' % p['evidence_type'])
        if extra:
            head += '，' + '，'.join(extra)
        lines.append('%s：%s；本格写 %d 条'
                     % (head, p.get('focus') or '（按该维度常规验证点）', p.get('planned') or 1))
        if p.get('tp_title'):
            lines.append('    测试点标题：%s' % p['tp_title'])
        if p.get('note'):
            lines.append('    依据说明：%s' % p['note'])
    lines.append('每条用例的 dims 必须包含该格的 dim，routes 必须包含该格的 route；'
                 '不同格的用例步骤与预期结果必须实质不同，禁止跨格重复。')
    if any(p.get('tp_ref') for p in plan):
        lines.append('本格标了测试点编号的，用例的 tp_refs 必须回标该编号；'
                     '一格可产多条用例，每条都回标本格编号。')
    return '\n'.join(lines)


def _tp_list_text(plan: List[Dict[str, Any]]) -> str:
    """把覆盖计划里的测试点渲染成 case_gen 的 user 模板变量（tp_list）。

    模型需要看到「编号 → 标题 → 覆盖维度」的对照表才能正确回标。
    没有测试点时返回空串（模板里 {{tp_list}} 会被清空，不影响其他页面）。
    """
    tps = [p for p in (plan or []) if p.get('tp_ref')]
    if not tps:
        return ''
    lines = ['\n【测试点清单（回标依据，编号只能原样使用）】']
    for p in tps:
        bits = [p.get('tp_ref')]
        if p.get('tp_title'):
            bits.append(p['tp_title'])
        if p.get('dim'):
            bits.append('%s %s' % (p['dim'], p.get('dim_name') or ''))
        if p.get('tp_expected'):
            bits.append('预期：%s' % p['tp_expected'])
        lines.append('· %s' % ' | '.join(str(b) for b in bits if b))
    lines.append('每条用例的 tp_refs 必须回标它实际覆盖的测试点编号（数组）；'
                 '找不到对应测试点的留空数组，不要编造编号。')
    return '\n'.join(lines) + '\n'


def _plan_from_test_points(db, requirement_id: Optional[int],
                           plan_indexes: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """复用需求分析阶段 2 的测试点当覆盖计划（省掉 G1 一次调用）。

    需求分析页生成用例时上游已经有 `dims` + `batch` + 五要素，
    再跑一遍 G1 纯属重复烧 token。拿不到（无 requirement_id / 无测试点）时返回空，
    由调用方回退到跑 G1。

    ⚠️ plan_indexes：前端改成「一批一次请求」后，若每次都返回**全部**测试点当计划，
    每一批都会拿到同一份计划 → 产出重复用例。
    所以按前端给的**离散下标列表**精确切片：只拿这些下标的测试点当计划，
    与前端本批传过来的需求文本严格对应同一批测试点。
    """
    if not requirement_id:
        return []
    rows = db.execute_query(
        'SELECT stage_payload FROM ai_requirements WHERE id = %s', (int(requirement_id),))
    if not rows:
        return []
    try:
        payload = rows[0].get('stage_payload')
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload or {}
    except Exception as pe:  # noqa: BLE001
        logger.warning('复用测试点作覆盖计划失败（回退到 G1）: %s', pe)
        return []
    tps = (payload.get('extract') or {}).get('test_points') or []
    # 按前端给的离散下标列表精确切片：只拿本批对应的那几个测试点当计划
    # ⚠️ 编号口径：tp_ref = TP{该测试点在 test_points 数组里的**原始下标+1}。
    # 用原始下标（而不是切片后的相对下标）是因为它与需求页的 plan_indexes 天然对齐，
    # 同一份需求文档内编号稳定可复现 —— 跨批次生成的用例回标到同一个测试点时不会指错。
    picked: List[Tuple[int, Dict[str, Any]]] = []
    if plan_indexes is not None:
        idxs = [int(i) for i in plan_indexes if 0 <= int(i) < len(tps)]
        picked = [(i, tps[i]) for i in idxs]
    else:
        picked = list(enumerate(tps))
    plan: List[Dict[str, Any]] = []
    for orig_idx, tp in picked:
        dims = coverage_mod.normalize_dims(tp.get('dims'))
        if not dims:
            continue
        plan.append({
            'module': tp.get('module') or '',
            'function_point': tp.get('title') or '',
            'dim': dims[0],
            'dim_name': coverage_mod.COVERAGE_DIM_NAMES.get(dims[0], ''),
            'route': '',
            'route_name': '',
            'case_type': '',
            'design_method': '',
            # batch A（冒烟）多给一条，其余给 1 条，与人工排布经验一致
            'severity': 'S0' if (tp.get('batch') or '').upper() == 'A' else 'S2',
            'evidence_type': tp.get('evidence_type') or 'basis',
            'focus': '；'.join(x for x in [
                tp.get('title') or '',
                ('输入：%s' % tp['sample_input']) if tp.get('sample_input') else '',
                ('预期：%s' % tp['expected']) if tp.get('expected') else '',
            ] if x),
            'planned': 2 if (tp.get('batch') or '').upper() == 'A' else 1,
            'note': tp.get('source') or '',
            # 回标三件套：编号 / 标题 / 预期（模型要靠标题判断这条用例覆盖的是哪个测试点）
            'tp_ref': 'TP%d' % (orig_idx + 1),
            'tp_title': tp.get('title') or '',
            'tp_expected': tp.get('expected') or '',
        })
    return plan


@router.post('/case-gen/plan')
def case_gen_plan(request: Request, data: CaseGenReq):
    """G1：只做覆盖规划，不写用例。返回可查看的结构化计划。"""
    try:
        uid = _uid(request)
        count = max(1, int(data.count or 5))
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_gen_plan', uid):
                raise HTTPException(status_code=409, detail='已有「生成用例」任务在运行中，请稍候')
        with get_db_handler() as db:
            project_name = data.project_name
            if not project_name and data.project_id:
                rows = db.execute_query(
                    'SELECT cate_name FROM test_case_project WHERE id = %s', (data.project_id,))
                project_name = rows[0]['cate_name'] if rows else ''
            req_text = data.requirement or '（未填写需求说明，请依据功能点与参考用例推断）'
            r = runner.run_structured(
                db, uid, biz_type='case_plan', prompt_code='case_plan',
                prompt_vars={
                    'project_name': project_name or '未指定',
                    'module': data.module or '未指定',
                    'function_point': data.function_point or '未指定',
                    'case_types': data.case_types or '正向,异常,边界',
                    'count': count,
                    'requirement': req_text,
                    'few_shot': _few_shot_text(db, data.module),
                },
                list_key='plan', item_model=schema_mod.CasePlanItem,
                limit=CASE_PLAN_MAX_CELLS * 2,
            )
            cells = _trim_plan(r['items'], count)
            return _ok({
                'plan': cells,
                'cells_total': len(r['items']),
                'cells_used': len(cells),
                'planned_total': sum(int(x.get('planned') or 1) for x in cells),
                'model': r.get('model') or {},
                'elapsed_ms': r.get('elapsed_ms') or 0,
                'usage': r.get('usage') or {},
            }, '已生成 %d 格覆盖计划' % len(cells))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成覆盖计划失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_gen_plan', uid)


def _trim_plan(plan: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """把 G1 的产出收敛到可控规模：按 S 级排序取前 N 格，每格 planned 封顶。

    不做这一步的话，模型可能规划出 19 格 × 每格 3 条 = 57 条，
    直接超出用户要的数量，且调用次数会失控。
    """
    order = {'S0': 0, 'S1': 1, 'S2': 2, 'S3': 3}
    ranked = sorted(
        [p for p in (plan or []) if p.get('dim') or p.get('route')],
        key=lambda p: (order.get(p.get('severity') or 'S2', 2), -int(p.get('planned') or 1)))
    out: List[Dict[str, Any]] = []
    total = 0
    for p in ranked:
        if len(out) >= CASE_PLAN_MAX_CELLS:
            break
        remain = count - total
        if remain <= 0:
            break
        p = dict(p)
        p['planned'] = max(1, min(int(p.get('planned') or 1), CASE_PLAN_MAX_PER_CELL, remain))
        out.append(p)
        total += p['planned']
    return out


def _run_gate(db, uid, plan: List[Dict[str, Any]], cases: List[Dict[str, Any]],
              model_id: Optional[int]) -> List[Dict[str, Any]]:
    """G3：对照计划核对已有用例，返回真实覆盖缺口（失败时降级为按计划盲补）。"""
    if not plan or not cases:
        return []
    try:
        r = runner.run_structured(
            db, uid, biz_type='case_gate', prompt_code='case_gate',
            prompt_vars={
                'plan_text': json.dumps(plan, ensure_ascii=False, default=str)[:6000],
                'cases_text': json.dumps(
                    [{k: v for k, v in c.items()
                      if k in ('module', 'function_point', 'case_type', 'design_method',
                               'steps', 'expected_result', 'dims', 'routes')}
                     for c in cases], ensure_ascii=False, default=str)[:12000],
            },
            list_key='gaps', item_model=schema_mod.CaseGapItem, limit=8,
            model_id=model_id,
        )
        # run_structured 返回的 items 已是 dict（不是 Pydantic 实例），别再 .dict()。
        # 早期版本这里写成 g.dict() 会抛 AttributeError，被 except 吞掉后闸门静默降级。
        return [g for g in r['items'] if (g.get('dim') or g.get('route'))]
    except Exception as ge:  # noqa: BLE001
        # 闸门失败不该让整单失败：退化成「按计划里 planned 最大且最严重的格子补」
        logger.warning('case_gate 执行失败（降级为按计划盲补）: %s', ge)
        fallbacks = []
        for p in sorted(plan, key=lambda x: -(int(x.get('planned') or 1)))[:3]:
            fallbacks.append({
                'dim': p.get('dim') or '', 'route': p.get('route') or '',
                'missing': int(p.get('planned') or 1), 'severity': p.get('severity') or 'S2',
                'suggestion': p.get('focus') or '',
            })
        return fallbacks


@router.post('/case-gen')
def case_gen(request: Request, data: CaseGenReq):
    """生成用例（仅返回结果，不自动落库；入库由前端勾选后调用 /case-gen/save）"""
    try:
        uid = _uid(request)
        count = max(1, int(data.count or 5))  # 单次条数不再硬性上限（模型输出受 max_tokens 约束，过大可能被截断）
        # AI 在途锁：登记，若已存在同类任务则拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_gen', uid):
                raise HTTPException(status_code=409, detail='已有「生成用例」任务在运行中，请稍候')
        with get_db_handler() as db:
            project_name = data.project_name
            if not project_name and data.project_id:
                rows = db.execute_query('SELECT cate_name FROM test_case_project WHERE id = %s', (data.project_id,))
                project_name = rows[0]['cate_name'] if rows else ''

            # A2：基于已有用例补充生成——精确按 case_ids 拉取来源用例，拼进需求上下文
            source_case_ids = [int(x) for x in (data.case_ids or []) if x]
            source_cases_text = ''
            src_module = data.module
            src_fp = data.function_point
            if source_case_ids:
                placeholders = ','.join(['%s'] * len(source_case_ids))
                rows = db.execute_query(
                    'SELECT id, module, function_point, priority, pre_condition, steps, expected_result '
                    'FROM test_cases WHERE is_del = 0 AND id IN (%s)' % placeholders,
                    tuple(source_case_ids))
                if rows:
                    lines = []
                    for r in rows:
                        lines.append(
                            '【已有用例%d】模块：%s / 功能点：%s / 优先级：%s\n前置条件：%s\n步骤：%s\n预期结果：%s'
                            % (r['id'], r.get('module') or '', r.get('function_point') or '', r.get('priority'),
                               r.get('pre_condition') or '', r.get('steps') or '', r.get('expected_result') or ''))
                    source_cases_text = '\n'.join(lines)
                    # 模块/功能点兜底用首条来源用例，使 few-shot 与生成上下文一致
                    if not data.module and rows[0].get('module'):
                        src_module = rows[0]['module']
                    if not data.function_point and rows[0].get('function_point'):
                        src_fp = rows[0]['function_point']

            # 组装需求文本：用户原始要求 + 来源用例（A2）
            if source_cases_text:
                req_text = (
                    (data.requirement + '\n\n' if data.requirement else '')
                    + '【请基于以下已有用例，补充其缺失的边界/异常/逆向测试点，不要重复已有内容】\n'
                    + source_cases_text
                )
            else:
                req_text = data.requirement or '（未填写需求说明，请依据功能点与参考用例推断）'

            kb_text, kb_count = _kb_knowledge(db, data.project_id, src_module, src_fp)

            base_vars = {
                'project_name': project_name or '未指定',
                'module': src_module or '未指定',
                'function_point': src_fp or '未指定',
                'case_types': data.case_types or '正向,异常,边界',
                'few_shot': _few_shot_text(db, src_module),
                'knowledge': kb_text,
            }

            batches = [b for b in (data.batches or []) if (b or '').strip()]
            # P2 三段式：规划 → 分维生成 → 闸门补跑。
            # 触发条件：传了 use_plan 且条数 >= 8（太小的需求跑三段式纯属浪费调用）。
            # 前端不传 use_plan 时走下面的 batches/单次路径，行为与改造前完全一致。
            use_plan = bool(data.use_plan) and count >= 8
            plan_cells: List[Dict[str, Any]] = []
            calls_used = 0
            if use_plan:
                # G1：有上游需求文档就复用测试点当计划，省一次调用
                # ⚠️ plan_indexes：前端「一批一次请求」时按离散下标精确切片，
                # 否则每批都拿同一份完整计划 → 每批产出重复用例。
                plan_cells = _plan_from_test_points(
                    db, data.requirement_id, data.plan_indexes)
                if plan_cells:
                    plan_cells = _trim_plan(plan_cells, count)
                    calls_used += 1 if data.requirement_id else 0
                if not plan_cells:
                    try:
                        pr = runner.run_structured(
                            db, uid, biz_type='case_plan', prompt_code='case_plan',
                            prompt_vars=dict(base_vars, count=count, requirement=req_text),
                            list_key='plan', item_model=schema_mod.CasePlanItem,
                            limit=CASE_PLAN_MAX_CELLS * 2, model_id=data.model_id,
                        )
                        calls_used += 1
                        plan_cells = _trim_plan(list(pr['items']), count)
                    except Exception as pe:  # noqa: BLE001
                        # 规划失败不该让整单失败：降级为不分维的传统路径
                        logger.warning('G1 case_plan 失败，降级为单次生成: %s', pe)
                        plan_cells = []
                        use_plan = False

            if use_plan and plan_cells:
                # G2：每格单独调一次模型。不同维度的步骤天然不同 —— 这正是分维的意义。
                items_all = []
                dropped_all = 0
                failed = 0
                last: Dict[str, Any] = {}
                # 计划级回标白名单：本次计划里出现过的全部测试点编号。
                # 模型回标超出这个集合的编号一律剔除（防编造脏编号）。
                tp_allowed: Set[str] = {
                    c['tp_ref'] for c in plan_cells if c.get('tp_ref')
                }
                for ci, cell in enumerate(plan_cells):
                    if len(items_all) >= count:
                        break
                    if ci > 0:
                        time.sleep(CASE_GEN_BATCH_GAP)
                    want = min(int(cell.get('planned') or 1), count - len(items_all))
                    try:
                        r = runner.run_structured(
                            db, uid, biz_type='case_gen', prompt_code='case_gen',
                            prompt_vars=dict(
                                base_vars, count=want, requirement=req_text,
                                plan_text=_plan_text([cell]),
                                tp_list=_tp_list_text([cell])),
                            list_key='cases', item_model=schema_mod.CaseDraftItem,
                            limit=want, model_id=data.model_id,
                        )
                        calls_used += 1
                        # 强制回填本格维度：模型偶尔不标 dims，直接丢掉这格覆盖就白跑了。
                        # ⚠️ items 是 dict（runner 已 model_dump），必须用键访问。
                        # 用属性访问会抛 AttributeError，被 except 吞成「本格失败」，
                        # 表现为「分维生成全部失败→回退单次」，很难排查。
                        for it in r['items']:
                            if cell.get('dim'):
                                it['dims'] = coverage_mod.normalize_dims(
                                    list(it.get('dims') or []) + [cell['dim']])
                            if cell.get('route'):
                                it['routes'] = coverage_mod.normalize_routes(
                                    list(it.get('routes') or []) + [cell['route']])
                            if not (it.get('severity') or ''):
                                it['severity'] = coverage_mod.normalize_severity(
                                    cell.get('severity'))
                            # 回标校正：本格对应的测试点必须被标上（模型漏标就补上），
                            # 同时把不在白名单里的编号剔除（防模型编造脏编号）。
                            it['tp_refs'] = _fix_tp_refs(it.get('tp_refs'), cell, tp_allowed)
                        items_all.extend(r['items'])
                        dropped_all += int(r.get('dropped') or 0)
                        last = r
                    except Exception as be:  # noqa: BLE001
                        failed += 1
                        logger.warning('case_gen 第 %d/%d 格失败：%s', ci + 1, len(plan_cells), be)

                # G3：闸门核对 → 缺口补跑（硬上限 1 轮）
                gate_rounds = 0
                gate_gaps: List[Dict[str, Any]] = []
                if items_all and len(items_all) < count and failed == 0:
                    gaps = _run_gate(db, uid, plan_cells, list(items_all),
                                     data.model_id)
                    calls_used += 1
                    # 六维缺口**本地确定性补齐**（不靠提示词自觉）：
                    # 模型漏了某类检查点时，用代码算出缺哪几维，转成补跑格。
                    # 与模型报的 gaps 合并去重（同一 C 维度只补一次）。
                    six_gaps = coverage_mod.check_six_dim_gaps(list(items_all))
                    seen_dims = {g.get('dim') for g in gaps if g.get('dim')}
                    for sg in six_gaps:
                        # 该维锚定的 C 维度里，挑一个还没被 gaps 占用的来补
                        pick = ''
                        for cd in sg['c_dims']:
                            if cd not in seen_dims:
                                pick = cd
                                break
                        if not pick:
                            continue
                        seen_dims.add(pick)
                        gaps.append({
                            'dim': pick,
                            'route': '',
                            'missing': 1,
                            'severity': 'S2',
                            'suggestion': '【六维缺口补齐】%s：%s（对应维度 %s）'
                                          % (sg['name'], sg['hint'], pick),
                        })
                    if gaps and gate_rounds < CASE_GATE_MAX_ROUNDS:
                        gate_gaps = gaps
                        gate_rounds = 1
                        topup_plan = [schema_mod.CaseGapItem(**g).to_plan_vars() for g in gaps]
                        for cell in topup_plan:
                            if len(items_all) >= count:
                                break
                            time.sleep(CASE_GEN_BATCH_GAP)
                            want = min(int(cell.get('planned') or 1), count - len(items_all))
                            try:
                                r2 = runner.run_structured(
                                    db, uid, biz_type='case_gen', prompt_code='case_gen',
                                    prompt_vars=dict(
                                        base_vars, count=want,
                                        requirement=req_text,
                                        plan_text=_plan_text([cell]),
                                        tp_list=_tp_list_text([cell])),
                                    list_key='cases', item_model=schema_mod.CaseDraftItem,
                                    limit=want, model_id=data.model_id,
                                )
                                calls_used += 1
                                for it in r2['items']:
                                    if cell.get('dim'):
                                        it['dims'] = coverage_mod.normalize_dims(
                                            list(it.get('dims') or []) + [cell['dim']])
                                    if cell.get('route'):
                                        it['routes'] = coverage_mod.normalize_routes(
                                            list(it.get('routes') or []) + [cell['route']])
                                    it['tp_refs'] = _fix_tp_refs(
                                        it.get('tp_refs'), cell, tp_allowed)
                                items_all.extend(r2['items'])
                                dropped_all += int(r2.get('dropped') or 0)
                                last = r2
                            except Exception as te:  # noqa: BLE001
                                logger.warning('case_gate 缺口补跑失败：%s', te)

                if not items_all:
                    # 全部分维都失败：退回传统路径，别让用户白等一轮
                    logger.warning('分维生成全部失败，回退单次生成')
                    use_plan = False
                else:
                    result = dict(last)
                    result['items'] = items_all
                    result['dropped'] = dropped_all
                    result['batches_total'] = len(plan_cells)
                    result['batches_failed'] = failed
                    result.setdefault('model', {})
                    result.setdefault('usage', {})
                    result.setdefault('elapsed_ms', 0)
                    msg_extra = '，按 %d 格覆盖计划分维生成' % len(plan_cells)
                    if gate_rounds:
                        msg_extra += '，闸门发现 %d 处缺口已补跑' % len(gate_gaps)
                    if failed:
                        msg_extra += '（%d 格失败已跳过）' % failed
                    if len(items_all) < count:
                        msg_extra += '；目标 %d 条，实际产出 %d 条' % (count, len(items_all))

            if not use_plan and batches:
                # 分批：每批 CASE_GEN_BATCH 条，输出量恒定在 max_tokens 安全区；
                # 最后一批按剩余额度给，不够就少给（宁可少几条也不要被截断）。
                items_all: List[Any] = []
                dropped_all = 0
                failed = 0
                topped = 0
                topup_failed = 0
                last: Dict[str, Any] = {}
                # ⚠️ 非分维路径也要能回标：需求页「整批一次」模式（use_plan=false）
                # 同样带着勾选的测试点，只是不按格生成。这里单独取一次测试点清单
                # 供 tp_list 渲染 + 回标白名单用（不额外调模型，零成本）。
                tp_cells = _plan_from_test_points(
                    db, data.requirement_id, data.plan_indexes)
                tp_allowed_refs: Set[str] = {
                    c['tp_ref'] for c in tp_cells if c.get('tp_ref')
                }
                tp_list_text = _tp_list_text(tp_cells)
                for bi, batch_text in enumerate(batches):
                    rest = count - len(items_all)
                    if rest <= 0:
                        break
                    # 批间隔：只在「上一批已产出」时等，避免第一批也白等1s
                    if bi > 0 and items_all:
                        time.sleep(CASE_GEN_BATCH_GAP)
                    want = min(CASE_GEN_BATCH, rest)
                    try:
                        r = runner.run_structured(
                            db, uid, biz_type='case_gen', prompt_code='case_gen',
                            prompt_vars=dict(base_vars, count=want, requirement=batch_text,
                                             plan_text='', tp_list=tp_list_text),
                            list_key='cases', item_model=schema_mod.CaseDraftItem, limit=want,
                            model_id=data.model_id,
                        )
                        for it in r['items']:
                            it['tp_refs'] = _fix_tp_refs(
                                it.get('tp_refs'), None, tp_allowed_refs)
                        items_all.extend(r['items'])
                        dropped_all += int(r.get('dropped') or 0)
                        last = r
                        # 补跑：模型有时「自己觉得够了」就提前收尾（实测要25 条只回 8 条，
                        # 且 JSON 合法、schema 校验通过 —— 不是被截断也不是校验放水）。
                        # 不补的话用户会发现「要 100 条只回来 60 条」却毫无提示。
                        # 阈值取一半：低于一半才补，避免为偶发少1~2 条多烧一次调用。
                        if len(r['items']) < want * CASE_GEN_SHORTFALL_RATIO:
                            gap = want - len(items_all)
                            if gap > 0:
                                time.sleep(CASE_GEN_BATCH_GAP)
                                try:
                                    r2 = runner.run_structured(
                                        db, uid, biz_type='case_gen', prompt_code='case_gen',
                                        prompt_vars=dict(base_vars, count=gap,
                                                         requirement=batch_text + '\n'
                                             +CASE_GEN_TOPUP_HINT,
                                                         tp_list=tp_list_text),
                                        list_key='cases', item_model=schema_mod.CaseDraftItem,
                                        limit=gap, model_id=data.model_id,
                                    )
                                    for it in r2['items']:
                                        it['tp_refs'] = _fix_tp_refs(
                                            it.get('tp_refs'), None, tp_allowed_refs)
                                    items_all.extend(r2['items'])
                                    dropped_all += int(r2.get('dropped') or 0)
                                    topped += 1
                                    last = r2
                                except Exception as te:  # noqa: BLE001
                                    topup_failed += 1
                                    logger.warning('case_gen 第 %d 批补跑失败：%s', bi + 1, te)
                    except Exception as be:  # noqa: BLE001
                        # 单批失败不拖垮整单：记一条日志继续下一批（与阶段2 分批同策略）
                        failed += 1
                        logger.warning('case_gen 第 %d/%d 批失败：%s', bi + 1, len(batches), be)
                        # 空结果列表会让后续 rest 计算失真（len(items_all) 不变），
                        # 但更严重的是：若这次是唯一有内容的批次，last 会是上一批的，
                        # 导致返回结果与实际批次对不上。这里显式标记，交给调用方判断。
                        if not items_all:
                            last = {}
                if not items_all:
                    raise HTTPException(
                        status_code=500,
                        detail='全部分批都失败了（%d 批），请减少生成条数或换个模型后重试' % len(batches))
                # model / usage / elapsed 等取最后一批真值；分批次数一并回传，
                # 每批的 ai_logs 独立成行，所以成本看板按批统计是准的。
                result = dict(last)
                result['items'] = items_all
                result['dropped'] = dropped_all
                result['batches_total'] = len(batches)
                result['batches_failed'] = failed
                result['batches_topped_up'] = topped
                result['topup_failed'] = topup_failed
                # 元信息缺失时给个中性占位，避免下游result['model'] 取空炸掉
                result.setdefault('model', {})
                result.setdefault('usage', {})
                result.setdefault('elapsed_ms', 0)
                msg_extra = '，分 %d 批生成' % len(batches)
                if topped:
                    msg_extra += '（%d 批产出不足已自动补跑）' % topped
                    calls_used += topped
                if failed:
                    msg_extra += '（%d 批失败已跳过）' % failed
                if topup_failed:
                    msg_extra += '（%d 批补跑也失败）' % topup_failed
                    calls_used += topup_failed
                if len(items_all) < count:
                    # 让用户知道「要N 条只回来 M 条」的真实原因，而不是默默少给
                    msg_extra += '；目标 %d 条，实际产出 %d 条' % (count, len(items_all))
                calls_used += len(batches)
            else:
                # 单次生成（无 batches）。同样带测试点清单，支持回标。
                tp_cells = _plan_from_test_points(
                    db, data.requirement_id, data.plan_indexes)
                tp_allowed_refs = {
                    c['tp_ref'] for c in tp_cells if c.get('tp_ref')
                }
                result = runner.run_structured(
                    db, uid, biz_type='case_gen', prompt_code='case_gen',
                    prompt_vars=dict(base_vars, count=count, requirement=req_text,
                                     plan_text='', tp_list=_tp_list_text(tp_cells)),
                    list_key='cases', item_model=schema_mod.CaseDraftItem, limit=count,
                    model_id=data.model_id,
                )
                for it in (result.get('items') or []):
                    it['tp_refs'] = _fix_tp_refs(
                        it.get('tp_refs'), None, tp_allowed_refs)
                msg_extra = ''
                calls_used += 1

            saved = []
            dropped_dup = 0
            dropped_gate = 0
            dropped_dup_samples: List[Dict[str, Any]] = []
            # 决策⑤：语义去重（3-gram Jaccard > 0.75），**整单级跨批累计**。
            # ⚠️ 关键：collector 不能在每批开始时重置，否则分 3 批生成时跨批重复一条都拦不住。
            # 旧实现只用 _seen_keys 做「五字段全等」精确匹配，实测 14 条里 7 对近似重复全部漏网
            # （其中 3 条步骤逐字相同，JC=1.00），有效信息量只剩一半。
            collector = coverage_mod.DedupCollector(coverage_mod.CASE_DEDUP_THRESHOLD)
            # 跨批预热：前端「每批一次请求」后，同一整单的多个请求彼此独立，
            # collector 看不到前几批的产物 → 跨批近似重复会漏网。
            # 前端把已生成条目回传（seen_items），这里先无条件登记，
            # 让本次请求的判重与「一次请求跑完全部批次」的效果保持一致。
            if data.seen_items:
                collector.add_many(data.seen_items)
            for item in result['items']:
                if collector.seen(item):
                    dropped_dup += 1
                    # 被丢内容留样（最多 5 条）写进响应，便于事后判断阈值是否误杀（D4）
                    if len(dropped_dup_samples) < 5:
                        dropped_dup_samples.append({
                            'steps': str(item.get('steps') or '')[:120],
                            'expected_result': str(item.get('expected_result') or '')[:80],
                            'dims': item.get('dims') or [],
                        })
                    continue
                saved.append(item)

            # 硬质量闸门：拦掉「预期结果为空 / 步骤为空 / 步骤不足 2 步」这类致命项。
            # 可验证性、控件具体度、敏感明文只打标不丢（见 coverage.filter_by_gate 说明）。
            saved, gate_rejected = coverage_mod.filter_by_gate(saved)
            dropped_gate = len(gate_rejected)

            # 与库内正式用例的疑似重复标记（仅展示，不自动入库；入库由前端勾选后调用 /case-gen/save）
            for item in saved:
                rows = db.execute_query(
                    'SELECT id FROM test_cases WHERE is_del = 0 AND module = %s AND function_point = %s '
                    'AND pre_condition = %s AND steps = %s AND expected_result = %s LIMIT 1',
                    (item.get('module') or data.module or '', item.get('function_point') or '',
                     item.get('pre_condition') or '', item.get('steps') or '',
                     item.get('expected_result') or ''))
                item['dup_case_id'] = rows[0]['id'] if rows else None

            # 覆盖统计（C 组 + R 组两段，分开统计，R 组不并入 C 组分母）
            cover = coverage_mod.build_cover_report(saved)
            # 六维检查清单报告（同一批 C 维度数据的另一种切面，不产生第二套数字）：
            # 提示词要求六维逐维覆盖，这里用代码**确定性核对**实际产出，
            # 缺口直接暴露给前端 —— 不能指望提示词写「必须」就一定会做到（铁律：本地确定性补齐）。
            six = coverage_mod.build_six_dim_report(saved)
            # 待确认项单列（决策：evidence_type=pending 不混进 items，Lemon 的待确认表精神）
            pending_items = [it for it in saved if it.get('evidence_type') == 'pending']
            # 闸门软告警：只提示不丢弃，让用户看到质量问题但条数不缩水
            warned = [it for it in saved if it.get('gate_issues')]

            # 质量与覆盖情况追加到提示语里，让用户一眼看到这次漏了什么
            if dropped_dup:
                msg_extra += '；已去掉 %d 条重复用例' % dropped_dup
            if dropped_gate:
                msg_extra += '；已去掉 %d 条步骤/预期不完整的用例' % dropped_gate
            if warned:
                msg_extra += '；%d 条质量提示见结果区' % len(warned)
            if cover['dims_gaps']:
                msg_extra += '；未覆盖 %d 个维度（%s）' % (
                    len(cover['dims_gaps']), ','.join(cover['dims_gaps']))
            if six['gaps']:
                msg_extra += '；六维缺口 %d 项（%s）' % (
                    len(six['gaps']), '、'.join(g['name'] for g in six['gaps']))

            # AI 生成完成通知：生成成功 → 给生成人推一条（type=4），失败不影响生成
            try:
                notify(
                    uid, 4, 'AI生成用例完成',
                    '已生成 %d 条用例，请在「AI生成用例」页勾选后入库' % len(saved),
                    'ai', '', '/aiwork/casegen', uid,
                )
            except Exception as ne:  # noqa: BLE001
                logger.warning('case_gen 生成通知失败（不影响生成）: %s', ne)

            return _ok({
                'items': saved,
                'dropped': result['dropped'],
                'dropped_dup': dropped_dup,
                # 语义去重阈值与被丢样例：便于事后判断 0.75 是否误杀（决策⑤ / D4）
                'dedup_threshold': coverage_mod.CASE_DEDUP_THRESHOLD,
                'dropped_dup_samples': dropped_dup_samples,
                'dropped_gate': dropped_gate,
                'gate_warn_count': len(warned),
                # 覆盖统计：C 组(14) + R 组(5) 两段，分母独立，R 组不并入 C 组
                'coverage': cover,
                # 六维检查清单报告（同一批 C 维度数据的另一种切面）
                'six_dim': six,
                'pending_items': pending_items,
                'model': result['model'],
                'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
                'attempt': result['attempt'],
                'masked_count': result['masked_count'],
                'source_case_ids': source_case_ids,
                # 来源需求文档 id：前端入库时要原样回传，采纳时据此反查产品功能点
                'requirement_id': data.requirement_id,
                'prompt_version': result.get('prompt_version'),
                'prompt_scope': result.get('prompt_scope'),
                'knowledge_injected': kb_count,
                'knowledge_text': kb_text,
                'batches_total': result.get('batches_total') or 1,
                'batches_failed': result.get('batches_failed') or 0,
                # 实际消耗的模型调用次数（三段式下会放大，前端据此提示用户，D3）
                'calls_used': calls_used,
                # 是否走了三段式规划路径，以及规划内容（供前端「查看覆盖计划」回看）
                'use_plan': bool(use_plan),
                'plan': plan_cells if use_plan else [],
            }, '已生成 %d 条用例%s，请勾选后点「加入 AI 用例库」入库' % (len(saved), msg_extra))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成用例失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_gen', uid)


@router.post('/case-gen/save')
def case_gen_save(request: Request, data: CaseGenSaveReq):
    """把前端勾选的用例写入 ai_case_drafts（同一批次一个 batch_id）；未勾选的不入库"""
    try:
        uid = _uid(request)
        items = data.items or []
        if not items:
            raise HTTPException(status_code=400, detail='请至少勾选 1 条用例再入库')
        with get_db_handler() as db:
            batch = _batch_id()
            saved = []
            source_json = json.dumps(data.source_case_ids, ensure_ascii=False) if data.source_case_ids else None
            for it in items:
                dims = coverage_mod.normalize_dims(it.dims)
                routes = coverage_mod.normalize_routes(it.routes)
                severity = coverage_mod.normalize_severity(it.severity) if it.severity else ''
                # priority 必须由 severity 重算，不能直接信前端传的 it.priority：
                # 前端回传的是模型自报的 priority，与 severity 可能打架
                # （实测 S1 却存成 priority=2）。有 severity 时以 severity 为准，
                # 没有才回退到前端值（老数据无severity 时行为不变）。
                priority = (coverage_mod.severity_to_priority(severity)
                            if severity else (it.priority or 2))
                # ⚠️ 列顺序固定：..., confidence, status, model_id, ...
                # VALUES 里 1 = source_type，0 = status（刚加入 AI 用例库必须是「待处理」）。
                # 早期把 0 写在 confidence 位上，使置信度参数落到 status 列：
                # 0.x 被塞进 tinyint status 四舍五入成 1，草稿显示「已采纳」且无法再采纳。
                item_id = _insert_id(
                    db,
                    'INSERT INTO ai_case_drafts (batch_id, source_type, source_id, suggest_project_id, '
                    'module, function_point, priority, case_type, design_method, dims, routes, '
                    'evidence_type, severity, pre_condition, case_steps, '
                    'expected_result, remark, confidence, status, model_id, run_by, source_case_ids, tp_refs) '
                    'VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s)',
                    (batch, data.requirement_id, data.project_id, (it.module or '').strip(),
                     (it.function_point or '').strip(), priority,
                     it.case_type or '', it.design_method or '',
                     ','.join(dims), ','.join(routes),
                     (it.evidence_type or 'basis'), severity,
                     it.pre_condition or '',
                     it.steps or '', it.expected_result or '',
                     it.remark or '', it.confidence or 0,
                     data.model_id, uid, source_json,
                     ','.join(schema_mod.normalize_tp_refs(it.tp_refs))),
                )
                saved.append(item_id)
                # 若该用例来自「生成历史」明细，回写 draft_id，可靠标记「已加入 AI 用例库」
                # 并防止从历史再次点击时重复入库
                hid = getattr(it, 'history_item_id', None)
                if hid:
                    try:
                        db.execute_update(
                            'UPDATE ai_case_gen_history_item SET draft_id = %s WHERE id = %s',
                            (item_id, int(hid)))
                    except Exception as ue:  # noqa: BLE001
                        logger.warning('回写 history_item.draft_id 失败（不影响入库）: %s', ue)
        # AI 生成完成通知：入库完成 → 给生成人推一条（type=4）
        try:
            notify(
                uid, 4, 'AI生成用例已入库',
                '已加入 %d 条用例到 AI 用例库（批次 %s）' % (len(saved), batch),
                'ai', batch, '/aiwork/drafts', uid,
            )
        except Exception as ne:  # noqa: BLE001
            logger.warning('case_gen_save 入库通知失败（不影响入库）: %s', ne)
        return _ok({'batch_id': batch, 'count': len(saved)}, '已加入 %d 条到 AI 用例库' % len(saved))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='加入 AI 用例库失败：%s' % e)


# ===========================================================================
# 三·B、AI 生成用例历史记录
# 每次「生成」成功后由前端调用 /case-gen/history 落库（失败不影响生成）；
# 列表/明细供 AI 生成用例页「生成历史」面板回看，即使当时未入库也不丢失。
# ===========================================================================
@router.post('/case-gen/history')
def case_gen_history_save(request: Request, data: CaseGenHistoryReq):
    """把一次生成的产物落库为历史记录（前端生成成功后调用；无用例则跳过）

    追加模式：传了 history_id → 明细追加到那条已有记录下（不再新建）。
    前端「一批一次请求」时，首批新建、后续批次追加，
    这样同一轮生成在「用例生成记录」里仍是**一条**记录（弹窗默认选中最新一条）。
    """
    try:
        uid = _uid(request)
        items = data.items or []
        if not items:
            return _ok(None, '无用例，跳过历史记录')
        with get_db_handler() as db:
            hid = data.history_id
            if hid:
                # 追加：必须校验这条记录存在且属于本人，否则传个别人的 id 就能往里塞数据
                own = db.execute_single_query(
                    'SELECT id, case_count FROM ai_case_gen_history WHERE id = %s AND run_by = %s',
                    (int(hid), uid))
                if not own:
                    raise HTTPException(status_code=404, detail='生成历史记录不存在或无权追加')
                base_seq = int(own.get('case_count') or 0)
                batch = db.execute_single_query(
                    'SELECT batch_id FROM ai_case_gen_history WHERE id = %s', (int(hid),))
                batch = (batch or {}).get('batch_id') or _batch_id()
                # 明细 seq 从已有条数往后接，避免 seq 冲突
                rows = [
                    (hid, base_seq + i, (it.module or '')[:255], (it.function_point or '')[:512],
                     (coverage_mod.severity_to_priority(sev)
                      if sev else (it.priority or 2)),
                     (it.case_type or '')[:32], (it.design_method or '')[:64],
                     ','.join(coverage_mod.normalize_dims(it.dims)),
                     ','.join(coverage_mod.normalize_routes(it.routes)),
                     sev,
                     (it.evidence_type or 'basis')[:16],
                     it.pre_condition or '', it.steps or '', it.expected_result or '',
                     it.remark or '', it.confidence or 0, it.dup_case_id,
                     ','.join(schema_mod.normalize_tp_refs(it.tp_refs)))
                    for i, it in enumerate(items)
                    for sev in [coverage_mod.normalize_severity(it.severity) if it.severity else '']
                ]
                db.execute_many(
                    'INSERT INTO ai_case_gen_history_item '
                    '(history_id, seq, module, function_point, priority, case_type, design_method, '
                    'dims, routes, severity, evidence_type, '
                    'pre_condition, case_steps, expected_result, remark, confidence, dup_case_id, tp_refs) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
                total = base_seq + len(items)
                db.execute_update(
                    'UPDATE ai_case_gen_history SET case_count = %s WHERE id = %s', (total, int(hid)))
                return _ok({'history_id': hid, 'batch_id': batch, 'count': len(items), 'total': total},
                           '已追加 %d 条到生成历史' % len(items))
            batch = _batch_id()
            hid = _insert_id(
                db,
                'INSERT INTO ai_case_gen_history '
                '(batch_id, run_by, requirement_id, project_id, project_name, model_id, model_name, '
                'module, function_point, requirement, case_count, elapsed_ms, attempt, '
                'knowledge_injected, knowledge_text) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (batch, uid, data.requirement_id, data.project_id, (data.project_name or '')[:255],
                 data.model_id, (data.model_name or '')[:100],
                 (data.module or '')[:255], (data.function_point or '')[:512],
                 data.requirement, len(items), data.elapsed_ms, data.attempt,
                 int(data.knowledge_injected or 0), (data.knowledge_text or '')[:65535]))
            rows = [
                (hid, i, (it.module or '')[:255], (it.function_point or '')[:512],
                 # 与 /case-gen/save 同口径：severity 存在时由它重算 priority，避免双写打架
                 (coverage_mod.severity_to_priority(sev)
                  if sev else (it.priority or 2)),
                 (it.case_type or '')[:32], (it.design_method or '')[:64],
                 ','.join(coverage_mod.normalize_dims(it.dims)),
                 ','.join(coverage_mod.normalize_routes(it.routes)),
                 sev,
                 (it.evidence_type or 'basis')[:16],
                 it.pre_condition or '', it.steps or '', it.expected_result or '',
                 it.remark or '', it.confidence or 0, it.dup_case_id,
                 ','.join(schema_mod.normalize_tp_refs(it.tp_refs)))
                for i, it in enumerate(items)
                for sev in [coverage_mod.normalize_severity(it.severity) if it.severity else '']
            ]
            db.execute_many(
                'INSERT INTO ai_case_gen_history_item '
                '(history_id, seq, module, function_point, priority, case_type, design_method, '
                'dims, routes, severity, evidence_type, '
                'pre_condition, case_steps, expected_result, remark, confidence, dup_case_id, tp_refs) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
        return _ok({'history_id': hid, 'batch_id': batch, 'count': len(items)}, '已记录生成历史')
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='记录生成历史失败：%s' % e)


@router.get('/case-gen/history')
def list_case_gen_history(request: Request, pageNo: int = 1, pageSize: int = 20,
                          requirementId: Optional[int] = None):
    """生成历史列表（仅本人，按时间倒序分页）。

    requirementId 不为空时只看该需求文档下的生成记录 —— 需求分析页
    「用例生成记录」按钮用它把某个需求的历次生成批次拉出来。
    """
    try:
        uid = _uid(request)
        where = 'run_by = %s'
        args: List[Any] = [uid]
        if requirementId:
            where += ' AND requirement_id = %s'
            args.append(int(requirementId))
        with get_db_handler() as db:
            total_rows = db.execute_query(
                'SELECT COUNT(*) AS n FROM ai_case_gen_history WHERE %s' % where, tuple(args))
            total = total_rows[0]['n'] if total_rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            offset = (pageNo - 1) * pageSize
            items = db.execute_query(
                'SELECT h.id, h.batch_id, h.project_id, h.requirement_id, h.model_id, h.project_name, '
                'h.model_name, h.module, h.function_point, h.case_count, h.elapsed_ms, h.attempt, '
                'h.create_time, h.knowledge_injected, '
                'u.username AS run_by_name '
                'FROM ai_case_gen_history h LEFT JOIN users u ON u.id = h.run_by '
                'WHERE %s ORDER BY h.id DESC LIMIT %%s OFFSET %%s' % where,
                (*args, pageSize, offset))
            return _ok({'list': items or [], 'total': total, 'pageNo': pageNo, 'pageSize': pageSize})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询生成历史失败：%s' % e)


@router.get('/case-gen/history/{history_id}')
def get_case_gen_history_items(request: Request, history_id: int):
    """生成历史明细：返回该次生成的所有用例"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            head = db.execute_query(
                'SELECT * FROM ai_case_gen_history WHERE id = %s AND run_by = %s',
                (history_id, uid))
            if not head:
                raise HTTPException(status_code=404, detail='历史记录不存在或无权限')
            rows = db.execute_query(
                'SELECT hi.*, '
                '(d.id IS NOT NULL AND d.status != 2) AS in_lib '
                'FROM ai_case_gen_history_item hi '
                'LEFT JOIN ai_case_drafts d ON d.id = hi.draft_id '
                'WHERE hi.history_id = %s ORDER BY hi.seq ASC, hi.id ASC', (history_id,))
            items = rows or []
            # 「已加入 AI 用例库」判定：直接看 draft_id 外键（入库时回写）。
            # 不再靠内容匹配，避免入库时 trim / 微调导致误判为「未加入库」。
            # 草稿被丢弃(status=2)视为已移出用例库，可再次添加。
            for it in items:
                it['in_library'] = bool(it.get('in_lib'))
                it.pop('in_lib', None)
                # DB 里 dims/routes 是 VARCHAR（'C04,C14'），但 /case-gen 响应给的是数组。
                # 这里统一拆成数组回传：前端不必猜类型，入库时也不会再撞
                # pydantic 的 List[str] 校验（原报错：Input should be a valid list）。
                it['dims'] = _as_multi_str(it.get('dims'))
                it['routes'] = _as_multi_str(it.get('routes'))
                # tp_refs 同理：DB 里是 'TP1,TP3' 逗号串，拆成数组回传，
                # 前端不必猜类型，入库时也不会撞 pydantic 的 List[str] 校验。
                it['tp_refs'] = schema_mod.normalize_tp_refs(it.get('tp_refs'))
            return _ok({'head': head[0], 'items': items})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询生成历史明细失败：%s' % e)


# ===========================================================================
# 四、AI 用例库（草稿池）
# ===========================================================================
@router.get('/drafts')
def list_drafts(request: Request,
                      pageNo: int = 1, pageSize: int = 20,
                      status: Optional[int] = None,
                      batch_id: str = '',
                      mine: int = 0):
    try:
        uid = _uid(request)
        where = ['1=1']
        args: List[Any] = []
        if status is not None and str(status) != '':
            where.append('d.status = %s')
            args.append(int(status))
        if batch_id:
            where.append('d.batch_id = %s')
            args.append(batch_id)
        if mine:
            where.append('d.run_by = %s')
            args.append(uid)
        cond = ' AND '.join(where)

        with get_db_handler() as db:
            rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_case_drafts d WHERE %s' % cond, tuple(args))
            total = rows[0]['n'] if rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            offset = (pageNo - 1) * pageSize
            list_sql = (
                'SELECT d.*, p.cate_name AS suggest_project_name, m.name AS model_name, u.username AS run_by_name '
                'FROM ai_case_drafts d '
                'LEFT JOIN test_case_project p ON p.id = d.suggest_project_id '
                'LEFT JOIN ai_models m ON m.id = d.model_id '
                'LEFT JOIN users u ON u.id = d.run_by '
                'WHERE %s ORDER BY d.id DESC LIMIT %%s OFFSET %%s' % cond)
            items = db.execute_query(list_sql, tuple(args) + (pageSize, offset))
            # 与 /case-gen/history/{id} 保持同一形状：DB 的逗号串一律拆成数组，
            # 否则前端在草稿页再点「加入库」会把 'C04,C14' 当数组用而报错。
            for it in items or []:
                it['dims'] = _as_multi_str(it.get('dims'))
                it['routes'] = _as_multi_str(it.get('routes'))
                # tp_refs 同理：DB 里是 'TP1,TP3' 逗号串，拆成数组回传，
                # 前端不必猜类型，入库时也不会撞 pydantic 的 List[str] 校验。
                it['tp_refs'] = schema_mod.normalize_tp_refs(it.get('tp_refs'))
            return _ok({'list': items or [], 'total': total, 'pageNo': pageNo, 'pageSize': pageSize})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询用例草稿失败：%s' % e)


class DraftAction(BaseModel):
    ids: List[int]
    project_id: Optional[int] = None


@router.post('/drafts/adopt')
def adopt_drafts(request: Request, data: DraftAction):
    """采纳：把草稿写入 test_cases（带 ai_generated / ai_draft_id 溯源标记）"""
    try:
        uid = _uid(request)
        ids = _int_list(data.ids)
        if not ids:
            raise HTTPException(status_code=400, detail='请选择要采纳的草稿')
        if not data.project_id:
            raise HTTPException(status_code=400, detail='请选择要加入的项目（三级节点）')

        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(ids))
            drafts = db.execute_query(
                'SELECT * FROM ai_case_drafts WHERE status = 0 AND id IN (%s)' % placeholders, tuple(ids))
            if not drafts:
                raise HTTPException(status_code=400, detail='没有可采纳的草稿（可能已被采纳或丢弃）')

            adopted = []
            linked_total = 0
            # 回标映射：按需求分组构建。
            # TP 编号只在**本需求内**有意义（= 需求测试点下标+1），跨需求会重号，
            # 所以必须按草稿的 source_id（= requirement_id）分组查，不能全局扫。
            req_ids = {int(d['source_id']) for d in drafts
                       if d.get('source_id') and int(d.get('source_id') or 0) > 0}
            req_map_cache: Dict[int, Dict[str, List[Tuple[int, int]]]] = {}
            for rid in req_ids:
                req_map_cache[rid] = _tp_ref_map(db, rid)
            for d in drafts:
                new_id = _insert_id(
                    db,
                    'INSERT INTO test_cases (project_id, module, function_point, priority, pre_condition, '
                    'steps, expected_result, remark, status, is_del, creator_id, ai_generated, ai_draft_id) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,0,%s,1,%s)',
                    (data.project_id, d.get('module') or '', d.get('function_point') or '',
                     d.get('priority') or 2, d.get('pre_condition') or '', d.get('case_steps') or '',
                     d.get('expected_result') or '', d.get('remark') or '', uid, d['id']),
                )
                db.execute_update(
                    'UPDATE ai_case_drafts SET status = 1, adopt_case_id = %s, adopt_project_id = %s, '
                    'adopt_by = %s, adopt_time = NOW() WHERE id = %s',
                    (new_id, data.project_id, uid, d['id']))
                adopted.append({'draft_id': d['id'], 'case_id': new_id})
                # 回标闭环：按 tp_refs 反查产品功能点，自动写 product_point_cases。
                # 不做这一步的话，tp_refs 只是装饰字段 —— 测试点覆盖率不会因
                # 「AI 生成并采纳了用例」而更新，覆盖率看板永远是缺口。
                #
                # 【显式定位通道】草稿自带 target_pfp_id/target_tp_index 时
                # （= 手动新增的测试点，无回标编号），按坐标精确关联，
                # 不走 TP 编号反查 —— 需求编号空间零污染。
                # 两条通道互斥：target 优先，未带 target 才走 tp_refs。
                t_pfp = d.get('target_pfp_id')
                t_idx = d.get('target_tp_index')
                if t_pfp and t_idx is not None:
                    try:
                        linked_total += _link_case_to_tp_index(
                            db, uid, new_id, int(t_pfp), int(t_idx),
                            str(d.get('target_tp_title') or ''))
                    except Exception as le:  # noqa: BLE001
                        logger.warning('显式关联产品功能点失败（不影响采纳）: %s', le)
                    continue
                tp_refs = schema_mod.normalize_tp_refs(d.get('tp_refs'))
                rid = int(d.get('source_id') or 0)
                if tp_refs and rid in req_map_cache:
                    try:
                        linked_total += _link_cases_by_tp_refs(
                            db, uid, new_id, tp_refs, req_map_cache[rid])
                    except Exception as le:  # noqa: BLE001
                        # 关联失败不该让采纳失败：用例已经入库了，不能因为关联回滚
                        logger.warning('回标关联产品功能点失败（不影响采纳）: %s', le)
            msg = '已采纳 %d 条用例到项目' % len(adopted)
            if linked_total:
                msg += '，并回标关联 %d 处功能点测试点' % linked_total
            return _ok({'adopted': adopted, 'count': len(adopted),
                        'linked': linked_total}, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='采纳失败：%s' % e)


class DraftSyncAdoptedReq(BaseModel):
    ids: List[int]
    project_id: Optional[int] = None


@router.post('/drafts/sync-adopted')
def sync_adopted_drafts(request: Request, data: DraftSyncAdoptedReq):
    """「补充用例 → 采纳并关联」收尾：把本批已入库的草稿状态同步为「已采纳」。

    以正式用例表的 ai_draft_id 做对账，而不是按 ids 无条件改状态：
      - 只有确实已入库的草稿才置为已采纳，未勾选/未入库的仍留在待处理；
      - 已丢弃（status=2）的不复活，source_type 限定为产品功能点补充用例；
      - 幂等：重复调用写回同一组值，adopt_by / adopt_time 只补空不覆盖原值。
    这样即使采纳接口漏标、或草稿状态被其它流程改回待处理，也能自愈。
    """
    try:
        uid = _uid(request)
        ids = _int_list(data.ids)
        if not ids:
            return _ok({'synced': 0}, '没有需要同步的草稿')
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(ids))
            cases = db.execute_query(
                'SELECT id, project_id, ai_draft_id FROM test_cases '
                'WHERE is_del = 0 AND ai_draft_id IN (' + placeholders + ')',
                tuple(ids))
            synced = 0
            for c in cases or []:
                n = db.execute_update(
                    'UPDATE ai_case_drafts SET status = 1, adopt_case_id = %s, '
                    'adopt_project_id = %s, adopt_by = COALESCE(adopt_by, %s), '
                    'adopt_time = COALESCE(adopt_time, NOW()) '
                    'WHERE id = %s AND status <> 2 AND source_type = 3',
                    (c['id'], c.get('project_id') or data.project_id, uid,
                     c.get('ai_draft_id')))
                synced += int(n or 0)
            return _ok({'synced': synced}, '已同步 %d 条草稿为已采纳' % synced)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='同步草稿状态失败：%s' % e)


@router.post('/drafts/discard')
def discard_drafts(request: Request, data: DraftAction):
    try:
        uid = _uid(request)
        ids = _int_list(data.ids)
        if not ids:
            raise HTTPException(status_code=400, detail='请选择要丢弃的草稿')
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(ids))
            db.execute_update(
                'UPDATE ai_case_drafts SET status = 2, adopt_by = %s, adopt_time = NOW() '
                'WHERE status = 0 AND id IN (' + placeholders + ')',
                tuple([uid] + ids))
            return _ok(None, '已丢弃 %d 条草稿' % len(ids))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='丢弃失败：%s' % e)


@router.get('/drafts/stat')
def drafts_stat(request: Request):
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT status, COUNT(*) AS n FROM ai_case_drafts GROUP BY status')
            stat = {0: 0, 1: 0, 2: 0}
            for r in rows or []:
                stat[int(r['status'])] = r['n']
            total = sum(stat.values())
            rate = round(stat[1] * 100.0 / total, 1) if total else 0.0
            return _ok({'draft': stat[0], 'adopted': stat[1], 'discarded': stat[2],
                        'total': total, 'adopt_rate': rate})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='统计失败：%s' % e)


# ===========================================================================
# 五、AI 分析测试总结（接口自动化线）
# ===========================================================================
class ExecSummaryReq(BaseModel):
    run_batch: Optional[int] = None
    interface_id: Optional[int] = None
    days: int = 7
    limit: int = 30
    only_failed: bool = True
    model_id: Optional[int] = None


@router.post('/exec-summary')
def exec_summary(request: Request, data: ExecSummaryReq):
    """基于 api_executions 的请求/响应快照与断言明细，输出失败根因分类与修复建议"""
    try:
        uid = _uid(request)
        days = int(data.days or 7)
        limit = max(1, min(int(data.limit or 30), 60))
        since = datetime.now() - timedelta(days=days)

        # base_*：整体口径（不受 only_failed 影响），用于概览统计展示
        base_where = ['e.run_time >= %s']
        base_args: List[Any] = [since]
        if data.run_batch:
            base_where.append('e.run_batch = %s')
            base_args.append(int(data.run_batch))
        if data.interface_id:
            base_where.append('e.interface_id = %s')
            base_args.append(int(data.interface_id))
        base_cond = ' AND '.join(base_where)
        # cond：明细口径（只看失败时再加一个条件）
        cond = base_cond + (' AND e.success = 0' if data.only_failed else '')
        args = base_args

        # AI 在途锁：登记，若已存在同类任务则拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'exec_summary', uid):
                raise HTTPException(status_code=409, detail='已有「接口执行总结」任务在运行中，请稍候')
        with get_db_handler() as db:
            stat_rows = db.execute_query(
                'SELECT COUNT(*) AS total, SUM(CASE WHEN e.success = 1 THEN 1 ELSE 0 END) AS ok, '
                'COUNT(DISTINCT e.interface_id) AS iface FROM api_executions e WHERE %s' % base_cond,
                tuple(base_args))
            stat = stat_rows[0] if stat_rows else {}
            total = int(stat.get('total') or 0)
            ok = int(stat.get('ok') or 0)
            fail = total - ok
            rate = round(ok * 100.0 / total, 2) if total else 0.0

            if total == 0:
                raise HTTPException(status_code=400, detail='所选范围内没有执行记录，请换个时间范围或批次')

            detail_rows = db.execute_query(
                'SELECT e.id, e.run_time, e.http_status, e.elapsed_ms, e.success, e.assertion_result, '
                'e.error_msg, e.request_dump, e.response_dump, e.assert_detail, e.case_tag, '
                'i.name AS interface_name, i.method, i.url '
                'FROM api_executions e LEFT JOIN api_interfaces i ON i.id = e.interface_id '
                'WHERE %s ORDER BY e.run_time DESC LIMIT %%s' % cond, tuple(args) + (limit,))

            lines = []
            for r in detail_rows or []:
                req = _json_loads(r.get('request_dump'), {}) or {}
                resp = _json_loads(r.get('response_dump'), {}) or {}
                asserts = _json_loads(r.get('assert_detail'), {}) or {}
                detail_txt = []
                for d in (asserts.get('details') or [])[:6]:
                    if d.get('passed') is False:
                        detail_txt.append('失败断言：%s 期望=%s 实际=%s' %
                                          (d.get('type'), _clip(d.get('expected'), 60), _clip(d.get('actual'), 60)))
                lines.append(
                    '#%s [%s] %s %s\n执行时间：%s  HTTP=%s  耗时=%sms  结果=%s\n请求：%s %s\n请求体：%s\n'
                    '响应体：%s\n错误信息：%s\n断言差异：%s'
                    % (r['id'], '失败' if not r.get('success') else '成功',
                       r.get('method') or '', r.get('interface_name') or r.get('url') or '',
                       r.get('run_time'), r.get('http_status'), r.get('elapsed_ms'),
                       r.get('assertion_result') or '', req.get('method') or '', req.get('url') or '',
                       _clip(req.get('body'), 400), _clip(resp.get('body'), 700),
                       _clip(r.get('error_msg'), 200),
                       '；'.join(detail_txt) or '无'))
            exec_text = '\n\n'.join(lines)

            stats_text = ('时间范围：近 %d 天\n执行总数：%d，成功：%d，失败：%d，通过率：%s%%\n'
                          '涉及接口数：%s\n本次分析明细条数：%d'
                          % (days, total, ok, fail, rate, stat.get('iface') or 0, len(lines)))

            result = runner.run_text(
                db, uid, biz_type='exec_summary', prompt_code='exec_summary',
                prompt_vars={'stats_text': stats_text, 'executions_text': exec_text},
                model_id=data.model_id,
            )

            title = '接口执行总结（近%d天，失败%d/%d）' % (days, fail, total)
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('exec_summary', title, 'batch', str(data.run_batch or '%d天' % days),
                 result['data'].get('text') or '',
                 json.dumps({'total': total, 'ok': ok, 'fail': fail, 'pass_rate': rate,
                             'days': days, 'analyzed': len(lines),
                             # 耗时/脱敏数一并存进 summary_json：ai_reports 没有这两列，
                             # 不存的话历史报告详情只能显示「耗时 0.0s」
                             'elapsed_ms': result['elapsed_ms'],
                             'masked_count': result['masked_count']}, ensure_ascii=False),
                 result['model']['id'], uid),
            )
            return _ok({
                'report_id': report_id,
                'title': title,
                'content_md': result['data'].get('text') or '',
                'stat': {'total': total, 'ok': ok, 'fail': fail, 'pass_rate': rate,
                         'analyzed': len(lines)},
                'model': result['model'],
                'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
                'masked_count': result['masked_count'],
            }, '总结已生成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成总结失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'exec_summary', uid)


# ===========================================================================
# 六、用例质量体检
# ===========================================================================
class CaseReviewReq(BaseModel):
    ids: Optional[List[int]] = None
    project_id: Optional[int] = None
    model_id: Optional[int] = None


@router.post('/case-review')
def case_review(request: Request, data: CaseReviewReq):
    try:
        uid = _uid(request)
        # AI 在途锁：登记，若已存在同类任务则拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_review', uid):
                raise HTTPException(status_code=409, detail='已有「用例质量体检」任务在运行中，请稍候')
        with get_db_handler() as db:
            if data.ids:
                ids = _int_list(data.ids)[:50]
                placeholders = ','.join(['%s'] * len(ids))
                rows = db.execute_query(
                    'SELECT id, module, function_point, priority, pre_condition, steps, expected_result '
                    'FROM test_cases WHERE is_del = 0 AND id IN (%s)' % placeholders, tuple(ids))
            elif data.project_id:
                rows = db.execute_query(
                    'SELECT id, module, function_point, priority, pre_condition, steps, expected_result '
                    'FROM test_cases WHERE is_del = 0 AND project_id = %s ORDER BY id DESC LIMIT 50',
                    (data.project_id,))
            else:
                rows = db.execute_query(
                    'SELECT id, module, function_point, priority, pre_condition, steps, expected_result '
                    'FROM test_cases WHERE is_del = 0 ORDER BY id DESC LIMIT 50')
            if not rows:
                raise HTTPException(status_code=400, detail='没有可体检的用例')

            result = runner.run_structured(
                db, uid, biz_type='case_review', prompt_code='case_review',
                prompt_vars={'cases_json': json.dumps(rows, ensure_ascii=False, default=str)},
                list_key='issues', item_model=schema_mod.IssueItem,
                model_id=data.model_id,
            )
            issues = result['items']
            title = '用例质量体检（%d 条用例，发现 %d 个问题）' % (len(rows), len(issues))
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('case_review', title, 'case', ','.join(str(r['id']) for r in rows[:50]),
                 _issues_markdown(issues),
                 json.dumps({'case_count': len(rows), 'issue_count': len(issues),
                             'issues': issues,
                             'elapsed_ms': result['elapsed_ms'],
                             'masked_count': result['masked_count']}, ensure_ascii=False, default=str),
                 result['model']['id'], uid),
            )
            # AI 生成完成通知：体检报告生成完成 → 给生成人推一条（type=4）
            try:
                notify(
                    uid, 4, '用例质量体检已完成', title,
                    'ai', report_id, '/aiwork/review', uid,
                )
            except Exception as ne:  # noqa: BLE001
                logger.warning('case_review 完成通知失败（不影响体检）: %s', ne)
            return _ok({
                'report_id': report_id,
                'title': title,
                'issues': issues,
                'content_md': _issues_markdown(issues),
                'case_count': len(rows),
                'dropped': result['dropped'],
                'model': result['model'],
                'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
            }, '体检完成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='用例体检失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_review', uid)


class CaseReviewDraftReq(BaseModel):
    """F3 AI 体检（草稿态）：直接收未保存表单，而非按 ids 查库"""
    module: str = ''
    function_point: str = ''
    priority: Any = None
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''
    model_id: Optional[int] = None


class CaseFromTextReq(BaseModel):
    """F4 自然语言转结构化用例：收一段自然语言描述 + 当前上下文"""
    text: str
    project_id: Optional[int] = None
    module: str = ''
    function_point: str = ''
    model_id: Optional[int] = None


class CaseSuggestReq(BaseModel):
    """F6 标签/优先级建议：收当前未保存表单内容"""
    module: str = ''
    function_point: str = ''
    priority: Any = None
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''
    model_id: Optional[int] = None


@router.post('/case-review/draft')
def case_review_draft(request: Request, data: CaseReviewDraftReq):
    """F3：对正在填写、尚未保存的用例做质量体检。

    复用 case_review 提示词与 IssueItem；输入形态从「按 ids 查库」改为「收未保存表单对象」。
    不落库、不发通知（草稿无 case_id），仅返回 issues 供前端「采纳」回填。
    """
    try:
        uid = _uid(request)
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_review_draft', uid):
                raise HTTPException(status_code=409, detail='已有「草稿体检」任务在运行中，请稍候')
        with get_db_handler() as db:
            draft = {
                'id': None,
                'module': data.module or '',
                'function_point': data.function_point or '',
                'priority': data.priority,
                'pre_condition': data.pre_condition or '',
                'steps': data.steps or '',
                'expected_result': data.expected_result or '',
            }
            result = runner.run_structured(
                db, uid, biz_type='case_review_draft', prompt_code='case_review',
                prompt_vars={'cases_json': json.dumps([draft], ensure_ascii=False, default=str)},
                list_key='issues', item_model=schema_mod.IssueItem,
                model_id=data.model_id,
            )
            issues = result['items']
            return _ok({
                'issues': issues,
                'case_count': 1,
                'dropped': result['dropped'],
                'model': result['model'],
                'elapsed_ms': result['elapsed_ms'],
            }, '体检完成' if issues else '未发现明显问题')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='草稿体检失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_review_draft', uid)


@router.post('/case-from-text')
def case_from_text(request: Request, data: CaseFromTextReq):
    """F4：自然语言描述 → 一条结构化用例（回填表单，不落库）。"""
    try:
        uid = _uid(request)
        if not (data.text or '').strip():
            raise HTTPException(status_code=422, detail='请先输入自然语言描述')
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_from_text', uid):
                raise HTTPException(status_code=409, detail='已有「AI 帮我写」任务在运行中，请稍候')
        with get_db_handler() as db:
            project_name = ''
            if data.project_id:
                rows = db.execute_query(
                    'SELECT cate_name FROM test_case_project WHERE id = %s', (data.project_id,))
                project_name = rows[0]['cate_name'] if rows else ''
            result = runner.run_structured(
                db, uid, biz_type='case_from_text', prompt_code='case_from_text',
                prompt_vars={
                    'project_name': project_name or '未指定',
                    'module': data.module or '未指定',
                    'function_point': data.function_point or '未指定',
                    'text': data.text or '',
                },
                list_key='cases', item_model=schema_mod.CaseFromTextItem, limit=1,
                model_id=data.model_id,
            )
            items = result['items']
            if not items:
                raise HTTPException(status_code=422, detail='未能从描述中解析出结构化用例，请补充更具体的描述')
            return _ok({
                'case': items[0],
                'dropped': result['dropped'],
                'model': result['model'],
            }, '已生成结构化用例')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_from_text', uid)


@router.post('/case-suggest')
def case_suggest(request: Request, data: CaseSuggestReq):
    """F6：基于已填内容建议标签与优先级（不修改用例正文，标签仅展示）。"""
    try:
        uid = _uid(request)
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_suggest', uid):
                raise HTTPException(status_code=409, detail='已有「AI 建议」任务在运行中，请稍候')
        with get_db_handler() as db:
            result = runner.run_structured(
                db, uid, biz_type='case_suggest', prompt_code='case_suggest',
                prompt_vars={
                    'module': data.module or '',
                    'function_point': data.function_point or '',
                    'priority': data.priority if data.priority is not None else '',
                    'pre_condition': data.pre_condition or '',
                    'steps': data.steps or '',
                    'expected_result': data.expected_result or '',
                    'remark': data.remark or '',
                },
                list_key='suggestions', item_model=schema_mod.CaseSuggestItem, limit=1,
                model_id=data.model_id,
            )
            items = result['items']
            if not items:
                raise HTTPException(status_code=422, detail='未能生成标签/优先级建议')
            return _ok({
                'suggestion': items[0],
                'dropped': result['dropped'],
                'model': result['model'],
            }, '已生成建议')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='建议失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_suggest', uid)


def _issues_markdown(issues: List[Dict[str, Any]]) -> str:
    if not issues:
        return '## 体检结果\n\n未发现明显问题。'
    lines = ['## 体检发现 %d 个问题\n' % len(issues)]
    for i, it in enumerate(issues, 1):
        lines.append('### %d. [%s] 用例 #%s\n**问题**：%s\n\n**建议**：%s\n' %
                     (i, it.get('severity') or '中', it.get('case_id') or '-',
                      it.get('problem') or '', it.get('suggestion') or ''))
        if it.get('fixed_steps'):
            lines.append('**建议步骤**：\n%s\n' % it['fixed_steps'])
        if it.get('fixed_expected'):
            lines.append(                    '**建议预期结果**：\n%s\n' % it['fixed_expected'])
    return '\n'.join(lines)


# 用例质量体检单次上限，与前端 REVIEW_LIMIT、case_review 的 LIMIT 50 保持一致
REVIEW_LIMIT = 50


def _review_column_map(columns) -> Dict[str, Any]:
    """体检上传专用列映射：兼容模板里两个「测试功能点」列
    （第 1 个当 module、最后 1 个当 function_point）。"""
    norm = [str(c).replace('\n', '').replace(' ', '').replace('（', '(').replace('）', ')').lower()
            for c in columns]
    col_of: Dict[str, Any] = {}
    single = {
        'project': ['项目', '项目名称', 'project', 'projectid', 'project_id'],
        'module': ['所属模块', '模块', 'module'],
        'pre_condition': ['前置条件', 'precondition', 'pre_condition'],
        'steps': ['步骤', 'steps'],
        'expected_result': ['预期结果', 'expectedresult', 'expected_result'],
        'remark': ['备注', 'remark'],
    }
    for field, al in single.items():
        for c, n in zip(columns, norm):
            if n in al or any(a.replace(' ', '').lower() in n for a in al):
                col_of.setdefault(field, c)
                break
    for c, n in zip(columns, norm):
        if '优先级' in n:
            col_of.setdefault('priority', c)
            break
    # 收集所有含「测试功能点」的列，原序：第 1 个当 module，最后 1 个当 function_point
    fp_cols = [c for c, n in zip(columns, norm)
               if ('测试功能点' in n or '功能点' in n or 'functionpoint' in n)]
    if 'module' not in col_of and fp_cols:
        col_of['module'] = fp_cols[0]
    if fp_cols:
        col_of['function_point'] = fp_cols[-1]
    return col_of


@router.post('/case-review/upload')
def case_review_upload(
        request: Request,
        file: UploadFile = File(...),
        model_id: Optional[int] = Form(None),
):
    """导入 Excel 做质量体检（仅分析，不写入用例库）。行号作为 case_id。"""
    try:
        uid = _uid(request)
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'case_review', uid):
                raise HTTPException(status_code=409, detail='已有「用例质量体检」任务在运行中，请稍候')
        with get_db_handler() as db:
            if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls', '.xlsm')):
                raise HTTPException(status_code=400, detail='仅支持 Excel 文件（.xlsx/.xls/.xlsm）')
            # 同步读底层文件（本路由是同步 def，不能用 await file.read()）
            contents = file.file.read()
            try:
                xl = pd.ExcelFile(io.BytesIO(contents))
            except Exception as e:
                raise HTTPException(status_code=400, detail='Excel 解析失败：%s' % e)
            # 逐 sheet 解析：有标准表头（含 测试功能点/步骤/预期结果）的用列名映射；
            # 无表头行（首行即数据，如模板曾出现的 Sheet2）按列位置映射，
            # 避免多 sheet 表头不一致时盲拼导致数据错位。
            REQ = ('function_point', 'steps', 'expected_result')
            raw_cases = []
            for s in xl.sheet_names:
                try:
                    df = pd.read_excel(
                        io.BytesIO(contents), sheet_name=s,
                        na_values=['', 'NA', 'N/A', 'NaN', 'null', 'NULL'], keep_default_na=True,
                    )
                except Exception:
                    continue
                if df.empty or len(df.columns) == 0:
                    continue
                col_map = _review_column_map(df.columns)
                if all(k in col_map for k in REQ):
                    use = df
                else:
                    # 无表头行（首行即数据）：上面 header=0 已把首行误当表头吃掉，
                    # 这里用 header=None 重读，把所有行当数据，再按列位置映射
                    # （列序 = 项目/模块/功能点/优先级/前置条件/步骤/预期结果/备注）
                    try:
                        df = pd.read_excel(
                            io.BytesIO(contents), sheet_name=s, header=None,
                            na_values=['', 'NA', 'N/A', 'NaN', 'null', 'NULL'], keep_default_na=True,
                        )
                    except Exception:
                        continue
                    if df.empty or len(df.columns) == 0:
                        continue
                    n = df.shape[1]
                    pos = {'project': 0, 'module': 1, 'function_point': 2, 'priority': 3,
                           'pre_condition': 4, 'steps': 5, 'expected_result': 6}
                    if n > 7:
                        pos['remark'] = 7
                    use = df
                    use.columns = ['__pos%d' % i for i in range(n)]
                    col_map = {k: '__pos%d' % v for k, v in pos.items()}
                records = safe_convert_dataframe(use)
                for rec in records:
                    vals = [rec.get(col_map.get(k)) for k in
                            ('module', 'function_point', 'steps', 'expected_result', 'pre_condition')]
                    if all((v is None or (isinstance(v, str) and not v.strip())) for v in vals):
                        continue
                    raw_cases.append({
                        'module': rec.get(col_map.get('module')) or '',
                        'function_point': rec.get(col_map['function_point']) or '',
                        'priority': normalize_priority(rec.get(col_map.get('priority'))),
                        'pre_condition': rec.get(col_map.get('pre_condition')) or '',
                        'steps': rec.get(col_map['steps']) or '',
                        'expected_result': rec.get(col_map['expected_result']) or '',
                    })
            if not raw_cases:
                raise HTTPException(status_code=400, detail='Excel 没有有效用例数据（所有行都为空或缺少必要列）')
            raw_count = len(raw_cases)
            exceeded = raw_count > REVIEW_LIMIT
            if exceeded:
                raw_cases = raw_cases[:REVIEW_LIMIT]
            cases = [dict(c, id=i) for i, c in enumerate(raw_cases, 1)]
            result = runner.run_structured(
                db, uid, biz_type='case_review', prompt_code='case_review',
                prompt_vars={'cases_json': json.dumps(cases, ensure_ascii=False, default=str)},
                list_key='issues', item_model=schema_mod.IssueItem,
                model_id=model_id,
            )
            issues = result['items']
            title = '用例质量体检-Excel（%d 条用例，发现 %d 个问题）' % (len(cases), len(issues))
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('case_review', title, 'case', ','.join(str(c['id']) for c in cases),
                 _issues_markdown(issues),
                 json.dumps({'case_count': len(cases), 'issue_count': len(issues), 'issues': issues,
                             'elapsed_ms': result['elapsed_ms'], 'masked_count': result['masked_count']},
                            ensure_ascii=False, default=str),
                 result['model']['id'], uid),
            )
            dropped = []
            if exceeded:
                dropped.append('Excel 共 %d 行有效数据，超过单次上限 %d 行，仅体检前 %d 行'
                                % (raw_count, REVIEW_LIMIT, REVIEW_LIMIT))
            return _ok({
                'report_id': report_id,
                'title': title,
                'issues': issues,
                'content_md': _issues_markdown(issues),
                'case_count': len(cases),
                'dropped': dropped,
                'model': result['model'],
                'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
            }, '体检完成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='Excel 体检失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'case_review', uid)


# ===========================================================================
# 七、需求文档：上传 / 列表 / 分析
# ===========================================================================
@router.post('/requirement/upload')
async def upload_requirement(request: Request, files: List[UploadFile] = File(...),
                             project_id: Optional[int] = Form(None)):
    try:
        uid = _uid(request)
        if not files:
            raise HTTPException(status_code=400, detail='未收到任何文件')
        texts: List[str] = []
        text_names: List[str] = []
        images: List[Dict[str, Any]] = []
        for file in files:
            filename = file.filename or 'unnamed'
            raw = await file.read()
            if not raw:
                raise HTTPException(status_code=400, detail='文件 %s 内容为空' % filename)
            if len(raw) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail='文件 %s 过大（上限 10MB）' % filename)
            ext = os.path.splitext(filename)[1].lower()
            try:
                if docs_mod.is_image_ext(filename):
                    if len(images) >= 8:
                        raise HTTPException(status_code=400, detail='图片最多上传 8 张')
                    images.append(docs_mod.prep_image(filename, raw))
                else:
                    texts.append(docs_mod.extract_text(filename, raw))
                    text_names.append(filename)
            except ValueError as ve:
                raise HTTPException(status_code=400, detail=str(ve))

        text = '\n\n'.join(t for t in texts if t.strip())
        if not text.strip() and not images:
            raise HTTPException(status_code=400, detail='没有解析出任何文本内容，也没有有效图片')

        title = (os.path.splitext(text_names[0])[0] if text_names
                 else (images[0]['filename'] if images else '需求文档'))
        title = title[:200]
        with get_db_handler() as db:
            # 归属项目：可选归类字段，不传就存 NULL（上传不阻断）。
            # 需求三阶段分析不读它；列表页可事后用 /requirement/project 补绑。
            pid = None
            if project_id:
                prow = db.execute_query(
                    'SELECT id FROM test_case_project WHERE id = %s', (int(project_id),))
                if not prow:
                    raise HTTPException(status_code=400, detail='所选项目不存在')
                pid = int(project_id)
            new_id = _insert_id(
                db,
                'INSERT INTO ai_requirements (project_id, title, req_type, source_files, content_text, '
                'source_images, img_count, status, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s)',
                (pid, title, 1, json.dumps(text_names, ensure_ascii=False),
                 text, json.dumps(images, ensure_ascii=False) if images else None,
                 len(images), uid),
            )
            blocks = len(docs_mod.split_blocks(text)) + len(images)
            return _ok({'id': new_id, 'title': title, 'project_id': pid,
                        'chars': len(text), 'images': len(images), 'blocks': blocks},
                       '已解析：%d 字符 / %d 张图片 / %d 个分析块' % (len(text), len(images), blocks))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='上传解析失败：%s' % e)


@router.get('/requirements')
def list_requirements(request: Request, pageNo: int = 1, pageSize: int = 20):
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_requirements WHERE is_del = 0')
            total = rows[0]['n'] if rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            items = db.execute_query(
                'SELECT r.id, r.title, r.req_type, r.source_files, r.status, r.error_msg, r.run_by, '
                'r.project_id, p.cate_name AS project_name, '
                'r.model_id, r.img_count, m.name AS model_name, '
                'r.create_time, CHAR_LENGTH(IFNULL(r.content_text, \'\')) AS chars, '
                'u.username AS run_by_name FROM ai_requirements r '
                'LEFT JOIN users u ON u.id = r.run_by '
                'LEFT JOIN test_case_project p ON p.id = r.project_id '
                'LEFT JOIN ai_models m ON m.id = r.model_id WHERE r.is_del = 0 '
                'ORDER BY r.id DESC LIMIT %s OFFSET %s', (pageSize, (pageNo - 1) * pageSize))
            return _ok({'list': items or [], 'total': total, 'pageNo': pageNo, 'pageSize': pageSize})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询需求文档失败：%s' % e)


class ReqDeleteReq(BaseModel):
    id: int


@router.post('/requirements/delete')
def delete_requirement(request: Request, data: ReqDeleteReq):
    """软删除需求文档：只允许删除「待分析」(status=0) 的文档。"""
    try:
        uid = _uid(request)
        rid = int(data.id or 0)
        if rid <= 0:
            raise HTTPException(status_code=400, detail='缺少需求文档 id')
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, status FROM ai_requirements WHERE id = %s AND is_del = 0', (rid,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            if int(rows[0]['status']) != 0:
                raise HTTPException(status_code=400, detail='只有「待分析」状态的文档才能删除')
            db.execute_update('UPDATE ai_requirements SET is_del = 1 WHERE id = %s', (rid,))
            return _ok(None, '已删除')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='删除需求文档失败：%s' % e)


class ReqProjectReq(BaseModel):
    id: int
    project_id: Optional[int] = None


@router.post('/requirement/project')
def set_requirement_project(request: Request, data: ReqProjectReq):
    """补绑 / 解绑需求文档的归属项目（可选操作）。

    归属项目只是归类字段：需求三阶段的 prompt_vars 里没有 project_id，分析结果不受影响。
    所以这里允许传 None 解绑，也不因为没绑而阻止上传。
    """
    try:
        uid = _uid(request)
        rid = int(data.id or 0)
        if rid <= 0:
            raise HTTPException(status_code=400, detail='缺少需求文档 id')
        pid = int(data.project_id) if data.project_id else None
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id FROM ai_requirements WHERE id = %s AND is_del = 0', (rid,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            if pid is not None:
                prow = db.execute_query(
                    'SELECT id FROM test_case_project WHERE id = %s', (pid,))
                if not prow:
                    raise HTTPException(status_code=400, detail='所选项目不存在')
            db.execute_update(
                'UPDATE ai_requirements SET project_id = %s WHERE id = %s', (pid, rid))
            return _ok({'id': rid, 'project_id': pid},
                       '已绑定归属项目' if pid else '已解除归属项目绑定')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='修改归属项目失败：%s' % e)


@router.get('/inflight')
def list_inflight(request: Request, biz_types: str = ''):
    """返回当前用户正在进行的 AI 任务（biz_type 列表），供前端挂载时禁用对应按钮。

    biz_types 为逗号分隔的白名单（留空返回该用户全部在途）。"""
    try:
        uid = _uid(request)
        wanted = [b.strip() for b in biz_types.split(',') if b.strip()] or None
        with get_db_handler() as db:
            rows = inflight_list(db, uid, wanted)
        return _ok({'list': rows})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询在途任务失败：%s' % e)


class ReqAnalyseReq(BaseModel):
    requirement_id: int
    model_id: Optional[int] = None
    max_blocks: int = 8        # 单次最多分析的章节块数，避免同步请求过长
    text_only: int = 0         # 1=直接粘贴的文本（不通过文件）
    stage: Optional[str] = None  # 决策①C：不传=一键跑全三阶段；传 decompose/extract/audit 只跑该阶段


class ReqStageUpdateReq(BaseModel):
    """决策⑥：人工修改阶段1 产物后回写"""
    requirement_id: int
    modules: List[Dict[str, Any]] = []


class ReqNaMarkReq(BaseModel):
    """决策②：人工标 NA，reason 必填"""
    requirement_id: int
    dims: List[str] = []
    reason: str = ''


#: 三阶段顺序与中文名（前端 Tab 与留痕表共用）
REQ_STAGES = ['decompose', 'extract', 'audit']
REQ_STAGE_NAMES = {'decompose': '需求拆解', 'extract': '测试点提取', 'audit': '覆盖审计'}
REQ_STAGE_NO = {'decompose': 1, 'extract': 2, 'audit': 3}


def _load_req_blocks(db, req: Dict[str, Any], max_blocks: int):
    """切块 + 图片块，返回 (used_blocks, blocks_total, skipped)"""
    text_blocks = docs_mod.split_blocks(req.get('content_text') or '')
    raw_imgs = req.get('source_images') or '[]'
    try:
        img_list = json.loads(raw_imgs) if isinstance(raw_imgs, str) else (raw_imgs or [])
    except Exception:
        img_list = []
    img_list = [x for x in (img_list or []) if isinstance(x, dict) and x.get('data_url')]
    image_blocks = [{'section_path': '图片',
                     'text': '（本块为上传的图片，请直接分析下方图片，不要依赖文字说明）',
                     'images': [x['data_url']]} for x in img_list]
    blocks = text_blocks + image_blocks
    used = blocks[:max(1, min(int(max_blocks or 8), 20))]
    return used, len(blocks), max(0, len(blocks) - len(used))


def _load_payload(db, req_id: int, allow_legacy: bool = False) -> Dict[str, Any]:
    """读 stage_payload（三阶段新增列）。

    allow_legacy=False（默认，用于**阶段编排**）：只认 stage_payload。
        必须严格 —— 否则旧 analyse_result 里转换出的测试点会让「阶段1 没跑过」的
        文档看起来已经有测试点，阶段顺序校验会被绕过。

    allow_legacy=True（用于**页面展示**）：stage_payload 为空时回落到旧
        analyse_result 走 normalizePoints() 兼容层（决策⑦：存量只读兼容不迁移）。
    """
    rows = db.execute_query(
        'SELECT stage_payload, analyse_result FROM ai_requirements WHERE id = %s', (req_id,))
    if not rows:
        return {}
    row = rows[0]
    raw = row.get('stage_payload')
    payload = None
    if raw:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            payload = None
    if isinstance(payload, dict) and payload.get('decompose'):
        return payload
    if not allow_legacy:
        return payload if isinstance(payload, dict) else {}
    legacy = (schema_mod.normalize_points(row.get('analyse_result')) or {}).get('stage_payload')
    return legacy if isinstance(legacy, dict) else {}


def _save_stage(db, req_id: int, stage: str, payload: Dict[str, Any], *,
                model_used=None, elapsed_ms: int = 0, blocks_total: int = 0,
                blocks_done: int = 0, items: int = 0, dropped: int = 0,
                uid: int = 0, status: int = 1) -> None:
    """写阶段产物 + 留痕。覆盖率只存在于 audit.coverage_stat，不另存冗余字段。"""
    payload[stage] = payload.get(stage) or {}
    payload[stage]['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if model_used:
        payload[stage]['model'] = model_used
    payload[stage]['elapsed_ms'] = elapsed_ms

    db.execute_update(
        'UPDATE ai_requirements SET stage_payload = %s, stage = %s, total_elapsed = %s '
        'WHERE id = %s',
        (json.dumps(payload, ensure_ascii=False, default=str),
         REQ_STAGE_NO.get(stage, 0), elapsed_ms, req_id))
    if model_used:
        db.execute_update('UPDATE ai_requirements SET model_id = %s WHERE id = %s',
                          (model_used.get('id'), req_id))
    db.execute_update(
        'INSERT INTO ai_req_stage_runs (requirement_id, stage, status, blocks_total, blocks_done, '
        'item_count, dropped_count, model_id, total_tokens, elapsed_ms, run_by, create_time) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())',
        (req_id, stage, status, blocks_total, blocks_done, items, dropped,
         (model_used or {}).get('id'), 0, elapsed_ms, uid))


def _save_coverage(db, req_id: int, coverage: Dict[str, Any],
                   questions: List[Dict[str, Any]]) -> None:
    db.execute_update(
        'UPDATE ai_requirements SET coverage_stat = %s, open_questions = %s WHERE id = %s',
        (json.dumps(coverage, ensure_ascii=False, default=str),
         json.dumps(questions, ensure_ascii=False, default=str), req_id))


@router.post('/requirement/analyse')
def analyse_requirement(request: Request, data: ReqAnalyseReq):
    """三阶段串行分析。不传 stage 一键全跑；传 stage 只跑该阶段（决策①C）。"""
    try:
        uid = _uid(request)
        stage = (data.stage or '').strip() or None
        if stage and stage not in REQ_STAGES:
            raise HTTPException(status_code=400, detail='未知阶段：%s' % stage)
        want = [stage] if stage else REQ_STAGES

        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, title, content_text, source_images FROM ai_requirements '
                'WHERE id = %s AND is_del = 0', (data.requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            req = rows[0]
            used, blocks_total, skipped = _load_req_blocks(db, req, data.max_blocks)
            if not used:
                raise HTTPException(status_code=400, detail='该需求文档没有正文也没有图片，无法分析')

            payload = _load_payload(db, req['id'])
            db.execute_update('UPDATE ai_requirements SET status = 1 WHERE id = %s', (req['id'],))

            summary: Dict[str, Any] = {'blocks_total': blocks_total,
                                        'blocks_analysed': len(used),
                                        'blocks_skipped': skipped,
                                        'stages': want}
            total_ms = 0
            try:
                for st in want:
                    res = _run_req_stage(db, uid, req, used, st, payload, data.model_id)
                    summary[st] = res['summary']
                    total_ms += res['elapsed_ms']
            except Exception:
                db.execute_update('UPDATE ai_requirements SET status = 3 WHERE id = %s', (req['id'],))
                raise

            # 三阶段各自把产物写进 stage_payload；这里只统一收口状态与耗时
            db.execute_update(
                'UPDATE ai_requirements SET status = 2, total_elapsed = %s WHERE id = %s',
                (total_ms, req['id']))

            cov = (payload.get('audit') or {}).get('coverage_stat')
            if not cov:
                cov = schema_mod.calc_coverage(
                    (payload.get('extract') or {}).get('test_points') or [], {})
            _save_coverage(db, req['id'], cov,
                           schema_mod.collect_open_questions(payload))

            summary['elapsed_ms'] = total_ms
            summary['stage_payload'] = payload
            summary['coverage_stat'] = cov
            summary['model'] = ((payload.get(want[-1]) or {}).get('model')) or None
            return _ok(summary, '已完成：%s' % '、'.join(REQ_STAGE_NAMES[s] for s in want))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='需求分析失败：%s' % e)


# ---------------------------------------------------------------------------
# 异步执行器（阶段三 R8）
#
# 为什么需要：需求分析三阶段串行，实测 9 模块 14 批耗时 249 秒、最坏单批 300 秒。
# 同步 HTTP 请求会让前端「一直卡在分析中」，浏览器/网关任何一层的超时都会
# 把用户正在等的结果直接丢掉（后端还在跑，但用户看不到也拿不到）。
#
# 方案第三节明确「不做 Celery/Redis」——用「后台线程 + 任务表 + 前端轮询」：
#   1. POST /ai/requirement/analyse-async 立刻返回 task_id（毫秒级）
#   2. 后台线程自建 DB 连接跑三阶段，每批把进度写回 ai_async_tasks
#   3. 前端每 2 秒 GET /ai/requirement/async/{task_id} 拉进度
# 注意：线程内绝不复用请求作用域的 db 连接，必须 get_db_handler() 自建。
# ---------------------------------------------------------------------------

# 阶段权重：decompose 通常 1~2 批，extract 最多 14 批，audit 1 次。
# 用「已完成批次数 / 总批次数」算总进度，所以先要知道各阶段批数 → 由阶段自己上报。
_STAGE_LABELS = {'decompose': '需求拆解', 'extract': '测试点提取', 'audit': '覆盖审计'}


def _task_update(task_id: str, **fields) -> None:
    """更新任务进度。独立连接、独立 try，写库失败只记日志不影响主流程。"""
    if not fields:
        return
    cols = ', '.join('`%s` = %%s' % k for k in fields)
    args = list(fields.values()) + [task_id]
    try:
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE ai_async_tasks SET %s, update_time = NOW() WHERE task_id = %%s' % cols,
                tuple(args))
    except Exception as e:  # noqa: BLE001
        logger.warning('[需求异步] 进度写库失败 task=%s: %s', task_id, e)


def _task_log(task_id: str, message: str, stage: str = '', level: str = 'info') -> None:
    """写进度流水，供前端时间线与排查「卡在哪一步」。"""
    try:
        with get_db_handler() as db:
            db.execute_update(
                'INSERT INTO ai_async_task_logs (task_id, level, stage, message, create_time) '
                'VALUES (%s, %s, %s, %s, NOW())',
                (task_id, level, stage, (message or '')[:500]))
    except Exception as e:  # noqa: BLE001
        logger.warning('[需求异步] 流水写库失败 task=%s: %s', task_id, e)


def _req_stage_batch_count(db, req: Dict[str, Any], stage: str,
                           payload: Dict[str, Any]) -> int:
    """预估某阶段的总批次数（只用于算总进度，不影响执行）。"""
    if stage == 'decompose':
        try:
            return max(1, len(_load_req_blocks(db, req, 8)[0]))
        except Exception:  # noqa: BLE001
            return 1
    if stage == 'extract':
        mods = (payload.get('decompose') or {}).get('modules') or []
        n = 0
        for mod in mods:
            rules = len(mod.get('explicit_rules') or []) + len(mod.get('implicit_constraints') or [])
            n += max(1, (rules + 9) // 10)
        return max(1, n)
    return 1


def _run_async_requirement(task_id: str, uid: int, req_id: int,
                           want: List[str], model_id: Optional[int]) -> None:
    """后台线程主体：跑完三阶段并把结果落 ai_async_tasks.result。"""
    t0 = time.time()
    # 每阶段的 (done, total)，用于跨阶段汇总总进度
    stage_plan: Dict[str, Tuple[int, int]] = {}
    total_batches = 0

    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, title, content_text, source_images FROM ai_requirements '
                'WHERE id = %s AND is_del = 0', (req_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            req = rows[0]
            used, blocks_total, skipped = _load_req_blocks(db, req, 8)
            if not used:
                raise HTTPException(status_code=400, detail='该需求文档没有正文也没有图片，无法分析')
            payload = _load_payload(db, req_id)

            for st in want:
                stage_plan[st] = (0, _req_stage_batch_count(db, req, st, payload))
                total_batches += stage_plan[st][1]

            db.execute_update('UPDATE ai_requirements SET status = 1 WHERE id = %s', (req_id,))

        _task_update(task_id, status=1, total_stage=len(want), total_batch=total_batches,
                     progress=2, cur_stage=want[0] if want else '')
        _task_log(task_id, '开始分析，共 %d 个阶段 / 预计 %d 批'
                  % (len(want), total_batches), want[0] if want else '')

        # 已完成的总批次数（跨阶段累加）
        done_batches_before = 0
        summary: Dict[str, Any] = {'blocks_total': blocks_total,
                                   'blocks_analysed': len(used),
                                   'blocks_skipped': skipped,
                                   'stages': want}
        total_ms = 0

        def make_progress_cb(stage: str):
            def _cb(kw: Dict[str, Any]) -> None:
                done = int(kw.get('done') or 0)
                total = int(kw.get('total') or 1)
                done_batches_before_local = sum(v[0] for k, v in stage_plan.items() if k < stage)
                overall = int((done_batches_before_local + done) * 100 / max(1, total_batches))
                done_stage = sum(1 for k, v in stage_plan.items() if v[0] >= v[1] and k < stage)
                _task_update(task_id, cur_stage=stage,
                             cur_batch='%s · %s' % (_STAGE_LABELS.get(stage, stage),
                                                    kw.get('desc') or ''),
                             done_batch=done_batches_before_local + done,
                             done_stage=done_stage,
                             progress=max(2, min(98, overall)))
            return _cb

        for st in want:
            _task_update(task_id, cur_stage=st, cur_batch='%s 开始' % _STAGE_LABELS.get(st, st))
            _task_log(task_id, '%s 开始' % _STAGE_LABELS.get(st, st), st)
            with get_db_handler() as db:
                res = _run_req_stage(db, uid, req, used, st, payload, model_id,
                                     progress=make_progress_cb(st))
            summary[st] = res['summary']
            total_ms += res['elapsed_ms']
            stage_plan[st] = (stage_plan[st][1], stage_plan[st][1])  # 该阶段标记完成
            _task_log(task_id, '%s 完成（%.1fs）' % (_STAGE_LABELS.get(st, st),
                                                res['elapsed_ms'] / 1000.0), st)

        # 收口：统一写状态/耗时/覆盖率/待确认清单（与同步路径保持一致）
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE ai_requirements SET status = 2, total_elapsed = %s WHERE id = %s',
                (total_ms, req_id))
            cov = (payload.get('audit') or {}).get('coverage_stat')
            if not cov:
                cov = schema_mod.calc_coverage(
                    (payload.get('extract') or {}).get('test_points') or [], {})
            _save_coverage(db, req_id, cov, schema_mod.collect_open_questions(payload))

        summary['elapsed_ms'] = total_ms
        summary['stage_payload'] = payload
        summary['coverage_stat'] = cov
        summary['model'] = ((payload.get(want[-1]) or {}).get('model')) or None
        summary['requirement_id'] = req_id
        summary['title'] = req.get('title')

        elapsed = int((time.time() - t0) * 1000)
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE ai_async_tasks SET status = 2, progress = 100, cur_batch = %s, '
                'result = %s, elapsed_ms = %s, finish_time = NOW(), update_time = NOW() '
                'WHERE task_id = %s',
                ('全部完成 · 共 %d 条测试点' % len((payload.get('extract') or {}).get('test_points') or []),
                 json.dumps(summary, ensure_ascii=False, default=str), elapsed, task_id))
        _task_log(task_id, '分析完成，总耗时 %.1fs' % (elapsed / 1000.0))
        logger.info('[需求异步] 任务完成 task=%s req=%s elapsed=%sms', task_id, req_id, elapsed)

    except HTTPException as e:
        _fail_task(task_id, e.detail)
    except AIError as e:
        _fail_task(task_id, str(e))
    except Exception as e:  # noqa: BLE001
        logger.error('[需求异步] 任务异常 task=%s', task_id, exc_info=True)
        _fail_task(task_id, '分析失败：%s' % e)


def _fail_task(task_id: str, msg: str) -> None:
    _task_update(task_id, status=3, cur_batch='失败', error_msg=(msg or '')[:2000])
    _task_log(task_id, msg or '失败', level='error')
    try:
        with get_db_handler() as db:
            r = db.execute_query(
                'SELECT requirement_id FROM ai_async_tasks WHERE task_id = %s', (task_id,))
            if r:
                db.execute_update('UPDATE ai_requirements SET status = 3 WHERE id = %s',
                                  (r[0]['requirement_id'],))
    except Exception:  # noqa: BLE001
        pass
    logger.warning('[需求异步] 任务失败 task=%s: %s', task_id, msg)


class ReqAnalyseAsync(BaseModel):
    requirement_id: int
    model_id: Optional[int] = None
    stage: Optional[str] = None       # 不传=三阶段全跑；传=decompose/extract/audit
    max_blocks: int = 8


@router.post('/requirement/analyse-async')
def analyse_requirement_async(request: Request, data: ReqAnalyseAsync):
    """提交异步分析任务，立刻返回 task_id；进度用 GET /requirement/async/{task_id} 轮询。"""
    try:
        uid = _uid(request)
        stage = (data.stage or '').strip() or None
        if stage and stage not in REQ_STAGES:
            raise HTTPException(status_code=400, detail='未知阶段：%s' % stage)
        want = [stage] if stage else list(REQ_STAGES)

        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, title FROM ai_requirements WHERE id = %s AND is_del = 0',
                (data.requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')

            # 提交前先回收僵尸，否则僵尸任务会被「复用」回来，用户永远提交不了新任务
            db.execute_update(
                "UPDATE ai_async_tasks SET status = 3, error_msg = '任务无响应已超时，请重新提交', "
                "cur_batch = '已超时', finish_time = NOW(), update_time = NOW() "
                "WHERE status IN (0, 1) AND update_time < DATE_SUB(NOW(), INTERVAL 20 MINUTE)")

            # 同一人同一需求已有在途任务 → 直接返回该任务，避免重复烧 token
            busy = db.execute_query(
                'SELECT task_id, cur_stage, cur_batch, progress FROM ai_async_tasks '
                'WHERE requirement_id = %s AND run_by = %s AND status IN (0, 1) '
                'ORDER BY create_time DESC LIMIT 1',
                (data.requirement_id, uid))
            if busy:
                b = busy[0]
                return _ok({'task_id': b['task_id'], 'reused': True,
                            'status': 1, 'progress': b['progress'],
                            'cur_batch': b['cur_batch']},
                           '该需求正在分析中，已为你接上现有任务')

            task_id = uuid.uuid4().hex
            db.execute_update(
                'INSERT INTO ai_async_tasks (task_id, requirement_id, stages, status, '
                'progress, run_by, model_id, create_time) '
                'VALUES (%s, %s, %s, 0, 0, %s, %s, NOW())',
                (task_id, data.requirement_id, ','.join(want), uid, data.model_id))

        th = threading.Thread(target=_run_async_requirement,
                              args=(task_id, uid, data.requirement_id, want, data.model_id),
                              daemon=True)
        th.start()
        return _ok({'task_id': task_id, 'status': 0, 'reused': False,
                    'stages': want, 'total_stage': len(want)},
                   '已提交，正在后台分析，可离开本页稍后回来查看结果')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='提交异步任务失败：%s' % e)


@router.get('/requirement/async/{task_id}')
def get_async_task(request: Request, task_id: str, with_logs: int = 0):
    """查异步任务进度。with_logs>0 时一并返回进度流水（默认不带，避免轮询变重）。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            # 自愈：update_time 超过 20 分钟仍停在 0/1，说明后台线程已死（进程重启由 main.py 复位，
            # 但线程崩溃/异常逃逸这类只能靠超时判定）。前端会持续轮询，所以在这里回收最合适。
            db.execute_update(
                "UPDATE ai_async_tasks SET status = 3, error_msg = '任务无响应已超时，请重新提交', "
                "cur_batch = '已超时', finish_time = NOW(), update_time = NOW() "
                "WHERE status IN (0, 1) AND update_time < DATE_SUB(NOW(), INTERVAL 20 MINUTE)")
            rows = db.execute_query(
                'SELECT task_id, requirement_id, stages, status, cur_stage, progress, cur_batch, '
                'done_batch, total_batch, done_stage, total_stage, error_msg, result, '
                'model_id, run_by, elapsed_ms, create_time, finish_time '
                'FROM ai_async_tasks WHERE task_id = %s', (task_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='任务不存在或已过期')
            t = rows[0]
            # 别人的任务不返回（避免探测他人任务进度）
            if t.get('run_by') and uid and int(t['run_by']) != int(uid):
                raise HTTPException(status_code=403, detail='无权查看该任务')

            data: Dict[str, Any] = {
                'task_id': t['task_id'],
                'requirement_id': t['requirement_id'],
                'stages': (t.get('stages') or '').split(','),
                'status': t['status'],          # 0排队 1运行 2完成 3失败
                'stage_label': _STAGE_LABELS.get(t.get('cur_stage') or '', t.get('cur_stage') or ''),
                'cur_stage': t.get('cur_stage') or '',
                'progress': int(t.get('progress') or 0),
                'cur_batch': t.get('cur_batch') or '',
                'done_batch': t.get('done_batch'), 'total_batch': t.get('total_batch'),
                'done_stage': t.get('done_stage'), 'total_stage': t.get('total_stage'),
                'error_msg': t.get('error_msg') or '',
                'elapsed_ms': t.get('elapsed_ms') or 0,
                'model_id': t.get('model_id'),
                'create_time': str(t.get('create_time') or ''),
                'finish_time': str(t.get('finish_time') or ''),
            }
            if t['status'] == 2 and t.get('result'):
                try:
                    data['result'] = json.loads(t['result'])
                except Exception:  # noqa: BLE001
                    data['result'] = None
            if with_logs:
                lg = db.execute_query(
                    'SELECT level, stage, message, create_time FROM ai_async_task_logs '
                    'WHERE task_id = %s ORDER BY id ASC LIMIT %s',
                    (task_id, int(min(max(with_logs, 1), 200))))
                data['logs'] = [{'level': r['level'], 'stage': r['stage'],
                                 'message': r['message'], 'time': str(r['create_time'])} for r in lg]

            msg = ('分析完成' if t['status'] == 2 else
                   ('分析失败：%s' % data['error_msg'] if t['status'] == 3 else
                    ('%s（%d%%）' % (data['cur_batch'], data['progress']))))
            return _ok(data, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询任务失败：%s' % e)


@router.get('/requirement/async-list')
def list_async_tasks(request: Request, limit: int = 10):
    """当前用户的最近异步任务，用于页面刷新后回填「正在分析」状态。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT task_id, requirement_id, status, cur_stage, progress, cur_batch, '
                'error_msg, create_time, finish_time FROM ai_async_tasks '
                'WHERE run_by = %s ORDER BY create_time DESC LIMIT %s',
                (uid, int(min(max(limit, 1), 50))))
            return _ok({'list': [{
                'task_id': r['task_id'], 'requirement_id': r['requirement_id'],
                'status': r['status'],
                'stage_label': _STAGE_LABELS.get(r.get('cur_stage') or '', ''),
                'progress': int(r.get('progress') or 0),
                'cur_batch': r.get('cur_batch') or '',
                'error_msg': r.get('error_msg') or '',
                'create_time': str(r.get('create_time') or ''),
                'finish_time': str(r.get('finish_time') or ''),
            } for r in rows]})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询任务列表失败：%s' % e)


def _run_req_stage(db, uid: int, req: Dict[str, Any], used: List[Dict[str, Any]],
                   stage: str, payload: Dict[str, Any], model_id: Optional[int],
                   progress: Optional[Callable[[Dict[str, Any]], None]] = None
                   ) -> Dict[str, Any]:
    """执行单个阶段并落库。阶段之间只通过 payload 传参，互不依赖调用次数。

    progress: 可选进度回调（阶段三 R8 异步用）。仅用于写 ai_async_tasks 进度，
    不传时行为与同步调用完全一致（不改变任何既有语义）。
    """

    def _tick(**kw):
        if progress:
            try:
                progress(kw)
            except Exception:  # noqa: BLE001 进度写库失败绝不能影响主流程
                pass

    req_id = req['id']
    title = req.get('title') or ''
    items: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    model_used = None
    elapsed = 0

    if stage == 'decompose':
        for bi, b in enumerate(used):
            _tick(stage='decompose', done=bi, total=len(used),
                  desc='分析章节块「%s」' % (b.get('section_path') or '正文'))
            r = runner.run_structured(
                db, uid, biz_type='req_decompose', prompt_code='req_decompose',
                prompt_vars={'title': title,
                             'section_path': b.get('section_path') or '正文',
                             'block_text': b.get('text') or ''},
                list_key='modules', item_model=schema_mod.ReqModule,
                model_id=model_id, images=b.get('images'))
            items.extend(r['items'])
            dropped.extend(r['dropped'])
            model_used = r['model']
            elapsed += r['elapsed_ms']
        _tick(stage='decompose', done=len(used), total=len(used), desc='需求拆解完成')
        # 决策④：某模块没有隐性约束 → 该模块校验失败重跑过仍为空则整条丢弃，
        # 保证留下来的每个模块都至少产出了 1 条（含「文档未提及 X」的否定式条目）
        modules, weak = [], []
        for m in items:
            if not (m.get('implicit_constraints') or []):
                weak.append(m.get('module') or '')
                continue
            modules.append(m)
        if weak:
            dropped.append({'index': '-', 'reason': '以下模块未产出隐性约束（需含「文档未提及X」形式的条目）：%s'
                                                 % '、'.join(weak[:5])})
        # 同名模块合并（跨块）
        merged: Dict[str, Dict[str, Any]] = {}
        for m in modules:
            k = m.get('module') or ''
            if k in merged:
                t = merged[k]
                t['main_flow'] = list({*(t.get('main_flow') or []), *(m.get('main_flow') or [])})
                for f in ('explicit_rules', 'implicit_constraints'):
                    t[f] = list({*(t.get(f) or []), *(m.get(f) or [])})
            else:
                merged[k] = dict(m)
        modules = list(merged.values())
        payload['decompose'] = {'modules': modules}
        _save_stage(db, req_id, stage, payload, model_used=model_used, elapsed_ms=elapsed,
                    blocks_total=len(used), blocks_done=len(used), items=len(modules),
                    dropped=len(dropped), uid=uid,
                    status=1 if modules else 2)
        return {'summary': {'modules': len(modules), 'dropped': dropped}, 'elapsed_ms': elapsed}

    if stage == 'extract':
        modules = (payload.get('decompose') or {}).get('modules') or []
        if not modules:
            raise HTTPException(status_code=400, detail='请先完成阶段1 需求拆解，再提取测试点')

        # 分批策略（两级）：
        # L1 按模块 —— 9 个模块一次性提取时输出必然撞 max_tokens(8192) 截断，
        #    实测 #427~#429 三次重试 completion=8192/16384/24576 全被砍在同一位置。
        # L2 大模块再按规则切片 —— 单模块规则数过多（如 19 条）时仍会撞顶（实测 #436），
        #    所以按规则数超阈值时切成多份，每份都带上模块摘要保持上下文。
        MAX_RULES_PER_SLICE = 10
        slices: List[Tuple[str, Dict[str, Any], int, int]] = []  # (模块名, 切片内容, 第几片, 共几片)
        for mod in modules:
            mod_name = mod.get('module') or '未命名模块'
            rules = [r for r in ((mod.get('explicit_rules') or []) + (mod.get('implicit_constraints') or []))
                     if r is not None]
            if len(rules) <= MAX_RULES_PER_SLICE:
                slices.append((mod_name, mod, 1, 1))
                continue
            total = (len(rules) + MAX_RULES_PER_SLICE - 1) // MAX_RULES_PER_SLICE
            n_explicit = len(mod.get('explicit_rules') or [])
            for i in range(total):
                part = rules[i * MAX_RULES_PER_SLICE:(i + 1) * MAX_RULES_PER_SLICE]
                cut_explicit = part[:max(0, n_explicit - i * MAX_RULES_PER_SLICE)]
                cut_implicit = part[max(0, n_explicit - i * MAX_RULES_PER_SLICE):]
                slices.append((mod_name, {
                    'module': mod_name,
                    'summary': mod.get('summary') or '',
                    'main_flow': mod.get('main_flow') or '',
                    'explicit_rules': cut_explicit,
                    'implicit_constraints': cut_implicit,
                    '_slice': '%d/%d' % (i + 1, total),
                }, i + 1, total))

        items: List[Dict[str, Any]] = []
        dropped: List[Dict[str, str]] = []
        model_used = None
        elapsed = 0
        per_module_errors: List[Dict[str, str]] = []
        for si, (mod_name, mod, idx, total) in enumerate(slices):
            one = json.dumps([mod], ensure_ascii=False)
            _tick(stage='extract', done=si, total=len(slices),
                  desc='提取测试点 %d/%d · %s' % (idx, total, mod_name))
            hint = ('本批次共 1 个模块，请只输出这个模块的测试点。'
                    if total == 1 else
                    '模块「%s」规则较多，已拆成 %d 份，这是第 %d 份，请只针对本份列出的规则产出测试点。'
                    % (mod_name, total, idx))
            try:
                r = runner.run_structured(
                    db, uid, biz_type='req_extract', prompt_code='req_extract',
                    prompt_vars={'title': title, 'modules_text': one[:6000], 'batch_hint': hint},
                    list_key='test_points', item_model=schema_mod.StructuredTestPoint,
                    model_id=model_id)
            except AIError as e:
                # 单个切片失败不拖垮整批：记录下来继续跑，最后一并回给前端
                per_module_errors.append({'module': mod_name,
                                          'slice': '%d/%d' % (idx, total), 'error': str(e)})
                continue
            items.extend(r['items'])
            dropped.extend(r['dropped'])
            model_used = r['model']
            elapsed += r['elapsed_ms']

        if per_module_errors and not items:
            # 一个都没成功 = 不是单模块问题，直接抛，让前端显示真实原因
            raise HTTPException(
                status_code=500,
                detail='全部 %d 个批次提取失败。首个原因：%s'
                       % (len(slices), per_module_errors[0]['error']))

        cov = schema_mod.calc_coverage(items, _load_na(db, req_id))
        _tick(stage='extract', done=len(slices), total=len(slices),
              desc='测试点提取完成，共 %d 条' % len(items))
        payload['extract'] = {'test_points': items, 'coverage_stat': cov,
                              'module_errors': per_module_errors,
                              'slices_total': len(slices)}
        _save_stage(db, req_id, stage, payload, model_used=model_used, elapsed_ms=elapsed,
                    blocks_total=len(slices), blocks_done=len(slices) - len(per_module_errors),
                    items=len(items), dropped=len(dropped), uid=uid,
                    status=1 if items else 2)
        summary = {'test_points': len(items), 'coverage': cov, 'dropped': dropped}
        if per_module_errors:
            summary['module_errors'] = per_module_errors
        return {'summary': summary, 'elapsed_ms': elapsed}

    # audit
    test_points = (payload.get('extract') or {}).get('test_points') or []
    if not test_points:
        raise HTTPException(status_code=400, detail='请先完成阶段2 测试点提取，再做覆盖审计')
    na = _load_na(db, req_id)
    cov = schema_mod.calc_coverage(test_points, na)
    gaps = cov['gaps']
    model_used = None
    if gaps:
        r = runner.run_structured(
            db, uid, biz_type='req_audit', prompt_code='req_audit',
            prompt_vars={'title': title,
                         'points_text': json.dumps(
                             [{k: t.get(k) for k in ('module', 'title', 'dims')}
                              for t in test_points], ensure_ascii=False)[:10000],
                         'gap_text': '、'.join(gaps)},
            list_key='gaps', item_model=schema_mod.CoverageGap, model_id=model_id)
        items = r['items']
        dropped = r['dropped']
        model_used = r['model']
        elapsed = r['elapsed_ms']
    payload['audit'] = {'gaps': items, 'coverage_stat': cov}
    _save_stage(db, req_id, stage, payload, model_used=model_used, elapsed_ms=elapsed,
                blocks_total=1, blocks_done=1, items=len(items), dropped=len(dropped),
                uid=uid, status=1 if not gaps else (1 if items else 2))
    return {'summary': {'gaps': items, 'coverage': cov, 'dropped': dropped},
            'elapsed_ms': elapsed}


def _load_na(db, req_id: int) -> Dict[str, str]:
    """读人工标注的 NA 明细（决策②：理由必填已在写入时保证）"""
    rows = db.execute_query('SELECT coverage_stat FROM ai_requirements WHERE id = %s', (req_id,))
    if not rows:
        return {}
    try:
        raw = rows[0].get('coverage_stat')
        cov = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except Exception:
        return {}
    na = cov.get('na') or {}
    return {k: v for k, v in na.items() if v}


@router.get('/requirement/stage/{name}')
def requirement_stage(request: Request, requirement_id: int, name: str):
    """查单阶段产物（前端切 Tab / 单阶段重跑后回读）"""
    if name not in REQ_STAGES:
        raise HTTPException(status_code=400, detail='未知阶段：%s' % name)
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, title, stage, stage_edited, coverage_stat, open_questions, '
                'total_elapsed, analyse_result, model_id FROM ai_requirements '
                'WHERE id = %s AND is_del = 0', (requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            r = rows[0]
            payload = _load_payload(db, requirement_id, allow_legacy=True)
            cov = r.get('coverage_stat')
            try:
                cov = json.loads(cov) if isinstance(cov, str) and cov else (cov or {})
            except Exception:
                cov = {}
            if not cov:
                cov = schema_mod.calc_coverage(
                    (payload.get('extract') or {}).get('test_points') or [], {})
            return _ok({
                'requirement_id': requirement_id,
                'title': r.get('title'),
                'stage': r.get('stage'),
                'stage_edited': bool(r.get('stage_edited')),
                'stage_payload': payload,
                'current': payload.get(name) or {},
                'coverage_stat': cov,
                'open_questions': schema_mod.collect_open_questions(payload),
                'total_elapsed': r.get('total_elapsed') or 0,
                'model_id': r.get('model_id'),
                'legacy': bool((schema_mod.normalize_points(
                    r.get('analyse_result')) or {}).get('legacy')),
            })
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='读取阶段产物失败：%s' % e)


@router.post('/requirement/stage/decompose/update')
def requirement_stage_update(request: Request, data: ReqStageUpdateReq):
    """决策⑥：人工修改阶段1 产物后回写，改完可重跑阶段2"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT id, stage_payload FROM ai_requirements '
                                    'WHERE id = %s AND is_del = 0', (data.requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            payload = _load_payload(db, data.requirement_id)
            modules = []
            for m in (data.modules or []):
                if not isinstance(m, dict):
                    continue
                try:
                    obj = schema_mod.ReqModule.model_validate(m)
                except Exception:
                    continue
                modules.append(obj.model_dump())
            if not modules:
                raise HTTPException(status_code=400, detail='至少需要保留 1 个有效模块')
            payload['decompose'] = {'modules': modules, 'edited': True,
                                    'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            db.execute_update(
                'UPDATE ai_requirements SET stage_payload = %s, stage_edited = 1 WHERE id = %s',
                (json.dumps(payload, ensure_ascii=False, default=str), data.requirement_id))
            return _ok({'modules': len(modules)}, '已保存阶段1 产物，可重跑阶段2 使用新内容')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='保存失败：%s' % e)


class ReqExtractUpdateReq(BaseModel):
    requirement_id: int
    test_points: List[Dict[str, Any]] = Body(default_factory=list)


@router.post('/requirement/stage/extract/update')
def requirement_extract_update(request: Request, data: ReqExtractUpdateReq):
    """阶段三 R11：人工修改阶段2 测试点后回写。

    与阶段1 编辑同构：逐条过 StructuredTestPoint 校验（非法条目丢弃并回传条数），
    保存后**重算覆盖率**（因为测试点的 dims 可能被改，覆盖率必须跟着变，
    不能留着旧的 14/14 骗人）。
    """
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT id FROM ai_requirements '
                                    'WHERE id = %s AND is_del = 0', (data.requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            payload = _load_payload(db, data.requirement_id)
            points = []
            skipped = 0
            # 质量闸门：StructuredTestPoint 的字段全有默认值（为兼容模型漏字段），
            # 所以 {bad:1} 会被「容错」成一条全空测试点。这里显式要求至少有标题+触发前置，
            # 否则用户不小心保存就会把测试点清成空白列表。
            empty_rows = 0
            for t in (data.test_points or []):
                if not isinstance(t, dict):
                    skipped += 1
                    continue
                try:
                    obj = schema_mod.StructuredTestPoint.model_validate(t)
                except Exception:
                    skipped += 1
                    continue
                d = obj.model_dump()
                if not (d.get('title') or '').strip():
                    empty_rows += 1
                    continue
                points.append(d)
            skipped += empty_rows
            if not points:
                raise HTTPException(status_code=400, detail='至少需要保留 1 条有效测试点（每条必须有标题）')
            cov = schema_mod.calc_coverage(points, _load_na(db, data.requirement_id))
            # 覆盖率变了 → 阶段3 的缺口结论必然失效，必须清掉让它重跑，
            # 否则页面会拿旧的「14/14 无缺口」骗人（这正是决策②要防的那类假数据）
            audit_invalidated = bool(payload.get('audit'))
            payload['audit'] = {}
            payload['extract'] = {'test_points': points, 'coverage_stat': cov,
                                  'edited': True,
                                  'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            db.execute_update(
                'UPDATE ai_requirements SET stage_payload = %s, stage_edited = 1 WHERE id = %s',
                (json.dumps(payload, ensure_ascii=False, default=str), data.requirement_id))
            _save_coverage(db, data.requirement_id, cov,
                           schema_mod.collect_open_questions(payload))
            msg = '已保存 %d 条测试点' % len(points)
            if skipped:
                msg += '（%d 条无效已丢弃）' % skipped
            if audit_invalidated:
                msg += '；覆盖审计结论已失效，请重跑阶段3'
            return _ok({'test_points': len(points), 'skipped': skipped,
                        'coverage_stat': cov,
                        'audit_invalidated': audit_invalidated}, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='保存失败：%s' % e)


@router.post('/requirement/coverage/na')
def requirement_mark_na(request: Request, data: ReqNaMarkReq):
    """决策②：人工标「不适用」。reason 为空直接 400，防止刷覆盖率。"""
    dims = [schema_mod.normalize_dims([d]) and (schema_mod.normalize_dims([d]) or [''])[0]
            for d in (data.dims or [])]
    dims = [d for d in dims if d in schema_mod.COVERAGE_DIMS]
    if not dims:
        raise HTTPException(status_code=400, detail='请选择要标记的覆盖维度')
    if not (data.reason or '').strip():
        raise HTTPException(status_code=400, detail='必须填写不适用理由，否则覆盖率可被刷分')
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT id FROM ai_requirements WHERE id = %s AND is_del = 0',
                                    (data.requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            payload = _load_payload(db, data.requirement_id)
            tps = (payload.get('extract') or {}).get('test_points') or []
            cov_old = _load_cov(db, data.requirement_id)
            na = dict(cov_old.get('na') or {})
            for d in dims:
                na[d] = data.reason.strip()
            cov = schema_mod.calc_coverage(tps, na)
            payload.setdefault('audit', {})['coverage_stat'] = cov
            db.execute_update(
                'UPDATE ai_requirements SET stage_payload = %s, coverage_stat = %s WHERE id = %s',
                (json.dumps(payload, ensure_ascii=False, default=str),
                 json.dumps(cov, ensure_ascii=False, default=str), data.requirement_id))
            msg = '已标记 %d 个维度为不适用' % len(dims)
            if cov.get('na_warning'):
                msg += '；⚠️ 不适用已超过半数，覆盖率参考性下降，请确认是否误标'
            return _ok({'coverage_stat': cov, 'na': na}, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='标记失败：%s' % e)


@router.post('/requirement/coverage/na/reset')
def requirement_reset_na(request: Request, requirement_id: int = Body(..., embed=True)):
    """取消人工标注的 NA（恢复为默认口径）"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query('SELECT id FROM ai_requirements WHERE id = %s AND is_del = 0',
                                    (requirement_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            payload = _load_payload(db, requirement_id)
            tps = (payload.get('extract') or {}).get('test_points') or []
            cov = schema_mod.calc_coverage(tps, {})
            payload.setdefault('audit', {})['coverage_stat'] = cov
            db.execute_update(
                'UPDATE ai_requirements SET stage_payload = %s, coverage_stat = %s WHERE id = %s',
                (json.dumps(payload, ensure_ascii=False, default=str),
                 json.dumps(cov, ensure_ascii=False, default=str), requirement_id))
            return _ok({'coverage_stat': cov}, '已清除不适用标注')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='重置失败：%s' % e)


def _load_cov(db, req_id: int) -> Dict[str, Any]:
    rows = db.execute_query('SELECT coverage_stat FROM ai_requirements WHERE id = %s', (req_id,))
    if not rows:
        return {}
    raw = rows[0].get('coverage_stat')
    try:
        return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except Exception:
        return {}


@router.get('/requirement/open-questions')
def requirement_open_questions(request: Request, requirement_id: int):
    """所有 [待确认]+[经验假设]，供一键导出给产品经理反向对齐"""
    try:
        with get_db_handler() as db:
            payload = _load_payload(db, requirement_id, allow_legacy=True)
            items = schema_mod.collect_open_questions(payload)
            rows = db.execute_query('SELECT title FROM ai_requirements WHERE id = %s',
                                    (requirement_id,))
            return _ok({
                'requirement_id': requirement_id,
                'title': rows[0].get('title') if rows else '',
                'count': len(items),
                'items': items,
                'assumption_count': len([x for x in items
                                         if x.get('evidence_type') == 'assumption']),
                'pending_count': len([x for x in items
                                      if x.get('evidence_type') == 'pending']),
            }, '共 %d 条待产品确认' % len(items))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='读取待确认清单失败：%s' % e)


@router.get('/requirement/stage-runs')
def requirement_stage_runs(request: Request, requirement_id: int):
    """三阶段执行留痕（含 tokens/elapsed），供报告与排查用"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT stage, status, blocks_total, blocks_done, item_count, dropped_count, '
                'total_tokens, elapsed_ms, error_msg, create_time FROM ai_req_stage_runs '
                'WHERE requirement_id = %s ORDER BY id DESC LIMIT 30', (requirement_id,))
            return _ok({'runs': rows})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='读取执行记录失败：%s' % e)


@router.get('/requirement/detail')
def requirement_detail(request: Request, id: int):
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, title, req_type, content_text, analyse_result, status, error_msg, create_time '
                'FROM ai_requirements WHERE id = %s AND is_del = 0', (id,))
            if not rows:
                raise HTTPException(status_code=404, detail='需求文档不存在')
            row = rows[0]
            row['analyse_result'] = _json_loads(row.get('analyse_result'), None)
            return _ok(row)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询详情失败：%s' % e)


# ===========================================================================
# 七-3、任务测试总结（F3b，支持无实际结果的降级模式）
# ===========================================================================
_STATUS_MAP = {0: '未开始', 1: '待执行', 2: '通过', 3: '失败', 4: '跳过'}


class TaskSummaryReq(BaseModel):
    task_id: int
    model_id: Optional[int] = None


@router.post('/task-summary')
def task_summary(request: Request, data: TaskSummaryReq):
    """基于 task_management + task_detail 输出测试结论；无 final_result 走降级推断模式"""
    try:
        uid = _uid(request)
        # AI 在途锁：登记，若已存在同类任务则拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'task_summary', uid):
                raise HTTPException(status_code=409, detail='已有「任务测试总结」任务在运行中，请稍候')
        with get_db_handler() as db:
            t = db.execute_query(
                'SELECT id, task_name, priority, task_status, begin_time, end_time, '
                'passed_nums, failed_nums, completed_nums, tc_nums '
                'FROM task_management WHERE id = %s', (data.task_id,))
            if not t:
                raise HTTPException(status_code=404, detail='任务不存在')
            task = t[0]
            details = db.execute_query(
                'SELECT id, project, module, function_point, priority, pre_condition, steps, '
                'expected_result, final_result, remark, case_status '
                'FROM task_detail WHERE task_id = %s ORDER BY id DESC LIMIT 100', (data.task_id,))
            details = details or []
            has_actual = any((d.get('final_result') or '').strip() for d in details)
            mode = ('A（有实测实际结果）' if has_actual
                    else 'B（降级·无实际结果，仅依据标题/步骤/状态推断，结论低置信度）')

            detail_lines = []
            for d in details:
                detail_lines.append(
                    '#用例 %s [%s] %s / %s / %s\n前置：%s\n步骤：%s\n预期：%s\n实际结果：%s\n备注：%s'
                    % (d['id'], _STATUS_MAP.get(d.get('case_status'), '未知'),
                       d.get('project') or '', d.get('module') or '', d.get('function_point') or '',
                       _clip(d.get('pre_condition'), 120), _clip(d.get('steps'), 300),
                       _clip(d.get('expected_result'), 200),
                       _clip(d.get('final_result'), 200) or '（未填写）',
                       _clip(d.get('remark'), 100)))
            task_text = ('任务：%s\n优先级：%s\n状态：%s\n计划开始：%s 结束：%s\n'
                         '用例总数：%s 完成：%s 通过：%s 失败：%s'
                         % (task.get('task_name'), task.get('priority'), task.get('task_status'),
                            task.get('begin_time'), task.get('end_time'),
                            task.get('tc_nums'), task.get('completed_nums'),
                            task.get('passed_nums'), task.get('failed_nums')))

            result = runner.run_text(
                db, uid, biz_type='task_summary', prompt_code='task_summary',
                prompt_vars={'mode': mode, 'task_text': task_text,
                             'detail_text': '\n\n'.join(detail_lines)},
                model_id=data.model_id)
            title = '任务测试总结 #%s（%s）' % (task['id'], task.get('task_name'))
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('task_summary', title, 'task', str(task['id']),
                 result['data'].get('text') or '',
                 json.dumps({'task_id': task['id'], 'mode': mode, 'has_actual': has_actual,
                             'detail_count': len(details), 'elapsed_ms': result['elapsed_ms'],
                             'masked_count': result['masked_count']}, ensure_ascii=False, default=str),
                 result['model']['id'], uid))
            return _ok({
                'report_id': report_id, 'title': title,
                'content_md': result['data'].get('text') or '', 'mode': mode,
                'has_actual': has_actual, 'detail_count': len(details),
                'model': result['model'], 'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'], 'masked_count': result['masked_count'],
            }, '总结已生成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成任务总结失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'task_summary', uid)


# ===========================================================================
# 八、调用日志 / 报告 / 设置 / 概览
# ===========================================================================
@router.get('/logs')
def list_logs(request: Request, pageNo: int = 1, pageSize: int = 20,
                    biz_type: str = '', success: Optional[int] = None, mine: int = 0):
    try:
        uid = _uid(request)
        where = ['1=1']
        args: List[Any] = []
        if biz_type:
            where.append('l.biz_type = %s')
            args.append(biz_type)
        if success is not None and str(success) != '':
            where.append('l.success = %s')
            args.append(int(success))
        if mine:
            where.append('l.run_by = %s')
            args.append(uid)
        cond = ' AND '.join(where)
        with get_db_handler() as db:
            rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_logs l WHERE %s' % cond, tuple(args))
            total = rows[0]['n'] if rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            items = db.execute_query(
                'SELECT l.*, u.username AS run_by_name FROM ai_logs l '
                'LEFT JOIN users u ON u.id = l.run_by WHERE %s '
                'ORDER BY l.id DESC LIMIT %%s OFFSET %%s' % cond,
                tuple(args) + (pageSize, (pageNo - 1) * pageSize))
            return _ok({'list': items or [], 'total': total, 'pageNo': pageNo, 'pageSize': pageSize})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询日志失败：%s' % e)


@router.get('/logs/stat')
def logs_stat(request: Request, days: int = 7):
    try:
        days = max(1, min(int(days or 7), 90))
        since = datetime.now() - timedelta(days=days)
        with get_db_handler() as db:
            summary = db.execute_query(
                'SELECT COUNT(*) AS calls, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok, '
                'SUM(IFNULL(total_tokens, 0)) AS tokens, '
                'ROUND(AVG(elapsed_ms), 0) AS avg_ms, MAX(elapsed_ms) AS max_ms '
                'FROM ai_logs WHERE create_time >= %s', (since,))
            by_model = db.execute_query(
                'SELECT model_name, COUNT(*) AS calls, SUM(IFNULL(total_tokens,0)) AS tokens, '
                'SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok '
                'FROM ai_logs WHERE create_time >= %s GROUP BY model_name ORDER BY calls DESC', (since,))
            by_biz = db.execute_query(
                'SELECT biz_type, COUNT(*) AS calls, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok '
                'FROM ai_logs WHERE create_time >= %s GROUP BY biz_type ORDER BY calls DESC', (since,))
            by_user = db.execute_query(
                'SELECT l.run_by, u.username, COUNT(*) AS calls, '
                'SUM(IFNULL(l.total_tokens,0)) AS tokens '
                'FROM ai_logs l LEFT JOIN users u ON u.id = l.run_by WHERE l.create_time >= %s '
                'GROUP BY l.run_by, u.username ORDER BY calls DESC LIMIT 20', (since,))
            s = summary[0] if summary else {}
            calls = int(s.get('calls') or 0)
            ok = int(s.get('ok') or 0)
            return _ok({
                'days': days,
                'summary': {
                    'calls': calls, 'success': ok, 'failed': calls - ok,
                    'success_rate': round(ok * 100.0 / calls, 1) if calls else 0.0,
                    'tokens': int(s.get('tokens') or 0),
                    'avg_ms': int(s.get('avg_ms') or 0), 'max_ms': int(s.get('max_ms') or 0),
                },
                'by_model': by_model or [],
                'by_biz': by_biz or [],
                'by_user': by_user or [],
            })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='统计失败：%s' % e)


@router.get('/reports')
def list_reports(request: Request, pageNo: int = 1, pageSize: int = 20,
                       report_type: str = '', days: int = 30):
    try:
        since = datetime.now() - timedelta(days=max(1, min(int(days or 30), 365)))
        where = ['r.is_del = 0', 'r.create_time >= %s']
        args: List[Any] = [since]
        if report_type:
            where.append('r.report_type = %s')
            args.append(report_type)
        cond = ' AND '.join(where)
        with get_db_handler() as db:
            rows = db.execute_query('SELECT COUNT(*) AS n FROM ai_reports r WHERE %s' % cond, tuple(args))
            total = rows[0]['n'] if rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            items = db.execute_query(
                'SELECT r.id, r.report_type, r.title, r.biz_type, r.biz_id, r.summary_json, r.model_id, '
                'r.run_by, r.create_time, u.username AS run_by_name, m.name AS model_name '
                'FROM ai_reports r LEFT JOIN users u ON u.id = r.run_by '
                'LEFT JOIN ai_models m ON m.id = r.model_id '
                'WHERE %s ORDER BY r.id DESC LIMIT %%s OFFSET %%s' % cond,
                tuple(args) + (pageSize, (pageNo - 1) * pageSize))
            for it in items or []:
                it['summary_json'] = _json_loads(it.get('summary_json'), None)
            return _ok({'list': items or [], 'total': total, 'pageNo': pageNo, 'pageSize': pageSize})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询报告失败：%s' % e)


@router.get('/reports/detail')
def report_detail(request: Request, id: int):
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT r.*, u.username AS run_by_name, m.name AS model_name FROM ai_reports r '
                'LEFT JOIN users u ON u.id = r.run_by LEFT JOIN ai_models m ON m.id = r.model_id '
                'WHERE r.id = %s AND r.is_del = 0', (id,))
            if not rows:
                raise HTTPException(status_code=404, detail='报告不存在')
            row = rows[0]
            row['summary_json'] = _json_loads(row.get('summary_json'), None)
            return _ok(row)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询报告详情失败：%s' % e)


@router.get('/settings')
def get_ai_settings(request: Request):
    try:
        with get_db_handler() as db:
            cfg = settings_mod.load(db, force=True)
            return _ok({
                'per_call_max_tokens': int(cfg.get('per_call_max_tokens') or 4096),
                'daily_call_limit': int(cfg.get('daily_call_limit') or 200),
                'ai_mask_enabled': cfg.get('ai_mask_enabled', '1'),
                'ai_mask_refill': cfg.get('ai_mask_refill', '0'),
                'ai_mask_rules': cfg.get('ai_mask_rules') or '',
                'can_edit': _has_perm(db, _uid(request), 'menu:ai:setting'),
            })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='读取设置失败：%s' % e)


class SettingsSave(BaseModel):
    per_call_max_tokens: Optional[int] = None
    daily_call_limit: Optional[int] = None
    ai_mask_enabled: Optional[int] = None
    ai_mask_refill: Optional[int] = None
    ai_mask_rules: Optional[str] = None


@router.put('/settings')
def save_ai_settings(request: Request, data: SettingsSave):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            if not _has_perm(db, uid, 'menu:ai:setting'):
                raise HTTPException(status_code=403, detail='无权修改 AI 配额与脱敏设置')
            payload = data.model_dump(exclude_none=True)
            if 'ai_mask_rules' in payload and payload['ai_mask_rules']:
                # 提前校验正则，避免脏规则让脱敏静默失效
                try:
                    json.loads(payload['ai_mask_rules'])
                except Exception:
                    raise HTTPException(status_code=400, detail='自定义脱敏规则必须是合法 JSON 数组')
            for k, v in payload.items():
                settings_mod.set_value(db, k, v, uid)
            return _ok(None, '设置已保存')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='保存设置失败：%s' % e)


@router.get('/overview')
def overview(request: Request):
    """AI 工作台首页概览：模型、今日用量、草稿池、最近报告"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            models = model_reg.list_models(db, uid)
            enabled = [m for m in models if m.get('enabled')]
            default_model = next((m for m in enabled
                                  if m.get('is_default') and (m.get('owner_user_id') or 0) == uid), None)
            if default_model is None:
                default_model = next((m for m in enabled
                                      if m.get('is_default') and (m.get('owner_user_id') or 0) == 0), None)
            today = db.execute_query(
                'SELECT COUNT(*) AS calls, SUM(IFNULL(total_tokens,0)) AS tokens, '
                'SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok '
                'FROM ai_logs WHERE run_by = %s AND create_time >= CURDATE()', (uid,))
            drafts = db.execute_query(
                'SELECT status, COUNT(*) AS n FROM ai_case_drafts GROUP BY status')
            stat = {0: 0, 1: 0, 2: 0}
            for r in drafts or []:
                stat[int(r['status'])] = r['n']
            reports = db.execute_query(
                'SELECT id, report_type, title, create_time FROM ai_reports WHERE is_del = 0 '
                'ORDER BY id DESC LIMIT 5')
            t = today[0] if today else {}
            limit = settings_mod.get_int(db, 'daily_call_limit', 200)
            used = int(t.get('calls') or 0)
            return _ok({
                'models': {'total': len(models), 'enabled': len(enabled),
                           'default': (default_model or {}).get('name') if default_model else None,
                           'default_type': (default_model or {}).get('model_type') if default_model else None,
                           'is_personal_default': bool(default_model
                                                       and (default_model.get('owner_user_id') or 0) == uid)},
                'today': {'calls': used, 'tokens': int(t.get('tokens') or 0),
                          'success': int(t.get('ok') or 0),
                          'quota': limit, 'remaining': max(0, limit - used) if limit > 0 else -1},
                'drafts': {'draft': stat[0], 'adopted': stat[1], 'discarded': stat[2],
                           'total': sum(stat.values())},
                'recent_reports': reports or [],
            })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询概览失败：%s' % e)


# ===========================================================================
# 九、三期功能：用例↔接口互转 / 测试数据 / 覆盖度 / 多模型评测 / 调试台
# ===========================================================================
def _dump_json(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# ---- F7 测试数据生成 ----
class DataGenReq(BaseModel):
    rules: List[Dict[str, Any]] = []
    count: int = 8
    model_id: Optional[int] = None


@router.post('/data-gen')
def data_gen(request: Request, data: DataGenReq):
    try:
        uid = _uid(request)
        if not data.rules:
            raise HTTPException(status_code=400, detail='请先定义字段规则')
        count = max(1, min(int(data.count or 8), 30))
        rules_text = '\n'.join(
            '- %s（类型：%s；约束：%s；示例：%s）' % (r.get('name'), r.get('type'), r.get('constraint'), r.get('example'))
            for r in data.rules)
        # AI 在途锁：登记，若已存在同类任务则拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'data_gen', uid):
                raise HTTPException(status_code=409, detail='已有「测试数据生成」任务在运行中，请稍候')
        with get_db_handler() as db:
            result = runner.run_structured(
                db, uid, biz_type='data_gen', prompt_code='data_gen',
                prompt_vars={'rules_text': rules_text, 'count': count},
                list_key='rows', item_model=schema_mod.DataGenRow, model_id=data.model_id)
            rows = [r for r in result['items']]
            title = '测试数据生成（%d 行 / %d 字段）' % (len(rows), len(data.rules))
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('data_gen', title, 'data', str(count), _dump_json(rows),
                 json.dumps({'rows': len(rows), 'fields': len(data.rules),
                             'elapsed_ms': result['elapsed_ms'],
                             'masked_count': result['masked_count']}, ensure_ascii=False),
                 result['model']['id'], uid))
            return _ok({'report_id': report_id, 'title': title, 'rows': rows,
                        'dropped': result['dropped'], 'model': result['model'],
                        'usage': result['usage'], 'elapsed_ms': result['elapsed_ms']}, '已生成测试数据')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成测试数据失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'data_gen', uid)


class DataSetCommitReq(BaseModel):
    interface_id: int
    rows: List[Dict[str, Any]] = []


@router.post('/dataset/commit')
def dataset_commit(request: Request, data: DataSetCommitReq):
    """把生成的数据集写入指定接口的数据驱动字段"""
    try:
        with get_db_handler() as db:
            db.execute_update('UPDATE api_interfaces SET dataset = %s, update_time = NOW() WHERE id = %s',
                              (_dump_json(data.rows), data.interface_id))
        return _ok(None, '已写入 %d 行数据集' % len(data.rows))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='写入数据集失败：%s' % e)


# ---- F7b 自定义 SQL 数据生成 ----
# 三步走：
#   1) /sql-parse  纯本地解析 INSERT 模板（不耗 AI），列出字段与当前值
#   2) /sql-plan   AI 读懂用户的文字描述，判定哪些字段要变（结果可在前端人工调整）
#   3) /sql-gen    AI 只为"要变的字段"生成取值，本地拼回完整 INSERT 语句
#
# 为什么不一步到位让模型直接写 SQL：一条 INSERT 可能有 30+ 字段，还夹着含逗号、
# 含转义引号的 JSON 字符串（'["https://a.jpg", "https://b.jpg"]'），
# 让模型照抄模板改几个值，必然出现字段错位、引号被吃、JSON 被切断。
# 所以：**SQL 的拼装与转义全在本地确定性完成，模型只产出"字段级判定"和"字段值"**。

SQL_GEN_MAX_ROWS = 50
SQL_GEN_BATCH = 10          # 每批生成的行数：输出量恒定在 max_tokens 安全区
SQL_GEN_BATCH_GAP = 1.0


class SqlParseReq(BaseModel):
    sql: str = ''


class SqlPlanReq(BaseModel):
    sql: str = ''
    requirement: str = ''
    model_id: Optional[int] = None


class SqlGenReq(BaseModel):
    sql: str = ''
    requirement: str = ''
    count: int = 10
    model_id: Optional[int] = None
    merge: bool = False
    # 前端人工调整后的字段计划（可选）：[{'field': 'x', 'change': true, 'rule': '...'}]
    # 传了就以它为准（不再调 AI 判计划），没传就现场调 AI 判一次。
    plan: Optional[List[Dict[str, Any]]] = None


def _sql_parse_or_400(sql: str) -> Dict[str, Any]:
    try:
        return sqlgen_mod.parse_insert(sql)
    except sqlgen_mod.SqlGenError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post('/sql-parse')
def sql_parse(request: Request, data: SqlParseReq):
    """解析 INSERT 模板：纯本地，不调用模型，不产生任何 AI 消耗。"""
    parsed = _sql_parse_or_400(data.sql)
    columns = []
    for col in parsed['columns']:
        val = col['value']
        columns.append({
            'name': col['name'],
            'kind': col['kind'],
            'value': None if val is None else str(val),
            'literal': col['literal'],
        })
    return _ok({
        'table': parsed['table'],
        'columns': columns,
        'fields_text': sqlgen_mod.describe_fields(parsed),
    }, '解析成功，共 %d 个字段' % len(columns))


@router.post('/sql-plan')
def sql_plan(request: Request, data: SqlPlanReq):
    """阶段一：让模型读懂文字描述，判定每个字段"保持不变 / 需要变化"。"""
    try:
        uid = _uid(request)
        parsed = _sql_parse_or_400(data.sql)
        requirement = (data.requirement or '').strip()
        if not requirement:
            raise HTTPException(status_code=400, detail='请先描述哪些字段不变、哪些字段要改')
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'sql_gen_plan', uid):
                raise HTTPException(status_code=409, detail='已有「SQL 字段计划」任务在运行中，请稍候')
        try:
            with get_db_handler() as db:
                result = runner.run_structured(
                    db, uid, biz_type='sql_gen_plan', prompt_code='sql_gen_plan',
                    prompt_vars={
                        'table': parsed['table'],
                        'fields_text': sqlgen_mod.describe_fields(parsed),
                        'requirement': requirement,
                    },
                    list_key='plan', item_model=schema_mod.SqlGenPlan)
                items = result['items']
                # 只保留模板里真实存在的字段，模型臆造的名字直接丢
                valid_names = {c['name'] for c in parsed['columns']}
                plan = []
                seen = set()
                for it in items:
                    name = (it.get('field') or '').strip()
                    if name not in valid_names or name in seen:
                        continue
                    seen.add(name)
                    plan.append({'field': name, 'change': bool(it.get('change')),
                                 'rule': (it.get('rule') or '').strip()})
                # 补齐模型没提到的字段：默认保持不变（保守 —— 宁可不变也别乱改）
                for col in parsed['columns']:
                    if col['name'] not in seen:
                        plan.append({'field': col['name'], 'change': False, 'rule': ''})
                changed = [p for p in plan if p['change']]
                return _ok({
                    'table': parsed['table'],
                    'plan': plan,
                    'changed_fields': [p['field'] for p in changed],
                    'model': result['model'],
                    'usage': result['usage'],
                    'elapsed_ms': result['elapsed_ms'],
                }, '已判定 %d 个字段需要变化' % len(changed))
        finally:
            with get_db_handler() as _lk:
                inflight_clear(_lk, 'sql_gen_plan', uid)
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成字段计划失败：%s' % e)


@router.post('/sql-gen')
def sql_gen(request: Request, data: SqlGenReq):
    """阶段二：模型只为"要变的字段"生成取值，本地拼回完整 INSERT 语句。"""
    try:
        uid = _uid(request)
        parsed = _sql_parse_or_400(data.sql)
        count = max(1, min(int(data.count or 10), SQL_GEN_MAX_ROWS))

        # 在途锁放在最前面：内嵌的 plan 阶段也会调模型，必须一并防重复提交
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'sql_gen', uid):
                raise HTTPException(status_code=409, detail='已有「SQL 数据生成」任务在运行中，请稍候')
        try:
            return _sql_gen_run(request, data, parsed, count, uid)
        finally:
            with get_db_handler() as _lk:
                inflight_clear(_lk, 'sql_gen', uid)
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成 SQL 数据失败：%s' % e)


def _sql_gen_run(request: Request, data: SqlGenReq, parsed: Dict[str, Any],
                 count: int, uid: int) -> Dict[str, Any]:
    """sql_gen 的实际执行体（在途锁已由调用方持有）。"""
    # 1) 拿到字段计划（前端传了就用前端的，否则调 AI 判一次）
    plan_in = data.plan or []
    if plan_in:
        valid_names = {c['name'] for c in parsed['columns']}
        plan = []
        for p in plan_in:
            name = (p.get('field') or '').strip()
            if name not in valid_names:
                continue
            plan.append({'field': name, 'change': bool(p.get('change')),
                         'rule': (p.get('rule') or '').strip()})
    else:
        requirement = (data.requirement or '').strip()
        if not requirement:
            raise HTTPException(status_code=400, detail='请先描述哪些字段不变、哪些字段要改')
        with get_db_handler() as db:
            pr = runner.run_structured(
                db, uid, biz_type='sql_gen_plan', prompt_code='sql_gen_plan',
                prompt_vars={
                    'table': parsed['table'],
                    'fields_text': sqlgen_mod.describe_fields(parsed),
                    'requirement': requirement,
                },
                list_key='plan', item_model=schema_mod.SqlGenPlan)
            valid_names = {c['name'] for c in parsed['columns']}
            plan = []
            seen = set()
            for it in pr['items']:
                name = (it.get('field') or '').strip()
                if name not in valid_names or name in seen:
                    continue
                seen.add(name)
                plan.append({'field': name, 'change': bool(it.get('change')),
                             'rule': (it.get('rule') or '').strip()})
            for col in parsed['columns']:
                if col['name'] not in seen:
                    plan.append({'field': col['name'], 'change': False, 'rule': ''})

    change_set = {p['field'] for p in plan if p['change']}

    # 2) 没有字段要变 -> 直接复制模板 N 次（这本身也是一种合法诉求）
    #    注意：这条路径**没有调用模型**，所以不写 ai_reports（那是 AI 产出的报告，
    #    纯复制写进去会污染「调用日志/报告」列表）。
    if not change_set:
        one = parsed['head'] + '\n' + '(%s);' % ', '.join(c['literal'] for c in parsed['columns'])
        if data.merge:
            sqls = [parsed['head'] + '\n' + ',\n'.join(
                ['(%s)' % ', '.join(c['literal'] for c in parsed['columns'])] * count) + ';']
        else:
            sqls = [one for _ in range(count)]
        return _ok({'report_id': None, 'sqls': sqls, 'plan': plan,
                    'changed_fields': [], 'fallbacks': [],
                    'elapsed_ms': 0}, '未识别到需要变化的字段，已按模板原样输出 %d 条' % count)

    # 3) 分批让模型生成"变化字段"的取值
    payload = sqlgen_mod.build_field_payload(parsed, [p for p in plan if p['change']])
    rows_values: List[Dict[str, Any]] = []
    model_info = None
    usage_total: Dict[str, Any] = {}
    elapsed_total = 0
    batches_failed = 0
    with get_db_handler() as db:
        done = 0
        while done < count:
            want = min(SQL_GEN_BATCH, count - done)
            fields_text = '\n'.join(
                '- %s（类型：%s；样例：%s；规则：%s）'
                % (f['name'], f['kind'],
                   'NULL' if f['sample'] is None else f['sample'],
                   f['rule'] or '按要求合理变化')
                for f in payload)
            try:
                r = runner.run_structured(
                    db, uid, biz_type='sql_gen', prompt_code='sql_gen_values',
                    prompt_vars={'table': parsed['table'],
                                 'fields_text': fields_text,
                                 'count': want},
                    list_key='rows', item_model=schema_mod.SqlGenRow)
                rows_values.extend(r['items'])
                model_info = r['model']
                elapsed_total += r['elapsed_ms'] or 0
                for k, v in (r['usage'] or {}).items():
                    if isinstance(v, (int, float)):
                        usage_total[k] = usage_total.get(k, 0) + v
            except Exception as e:  # noqa: BLE001
                batches_failed += 1
                logger.warning('sql_gen 第 %d 批失败：%s', done // SQL_GEN_BATCH + 1, e)
                if not rows_values:
                    raise HTTPException(
                        status_code=500,
                        detail='生成取值失败：%s' % e)
            done += want
            if done < count:
                time.sleep(SQL_GEN_BATCH_GAP)

        # 4) 本地拼装（不变字段直接复用模板原样字面量）
        sqls, fallbacks = sqlgen_mod.assemble_rows(parsed, change_set, rows_values,
                                                   merge=bool(data.merge))
        title = '自定义SQL生成（%d 行 / %d 字段变化）' % (count, len(change_set))
        report_id = _insert_id(
            db,
            'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
            'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            ('sql_gen', title, 'sql', str(count), _dump_json(sqls),
             json.dumps({'rows': count, 'changed': sorted(change_set),
                         'batches_failed': batches_failed,
                         'fallbacks': fallbacks[:20],
                         'elapsed_ms': elapsed_total}, ensure_ascii=False),
             (model_info or {}).get('id'), uid))
    return _ok({
        'report_id': report_id,
        'sqls': sqls,
        'plan': plan,
        'changed_fields': sorted(change_set),
        'fallbacks': fallbacks,
        'batches_failed': batches_failed,
        'model': model_info,
        'usage': usage_total,
        'elapsed_ms': elapsed_total,
    }, '已生成 %d 条 INSERT 语句' % len(sqls))


# ---- F8 需求覆盖度矩阵 ----
class CoverageReq(BaseModel):
    requirement_text: str = ''
    project_id: Optional[int] = None
    cases_text: str = ''
    model_id: Optional[int] = None


@router.post('/coverage')
def coverage(request: Request, data: CoverageReq):
    try:
        uid = _uid(request)
        # AI 在途锁：同一用户覆盖度分析期间拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'coverage', uid):
                raise HTTPException(status_code=409, detail='已有「覆盖度分析」任务在运行中，请稍候')
        with get_db_handler() as db:
            if data.project_id:
                cases = db.execute_query(
                    'SELECT module, function_point, steps FROM test_cases '
                    'WHERE is_del = 0 AND project_id = %s ORDER BY id DESC LIMIT 100', (data.project_id,))
                cases_text = data.cases_text or '\n'.join(
                    '【%s/%s】%s' % (c.get('module'), c.get('function_point'), _clip(c.get('steps'), 200))
                    for c in cases)
            else:
                cases_text = data.cases_text
            if not (data.requirement_text or '').strip():
                raise HTTPException(status_code=400, detail='请填写需求功能点清单')
            if not (cases_text or '').strip():
                raise HTTPException(status_code=400, detail='请选择项目或粘贴已有用例')
            result = runner.run_structured(
                db, uid, biz_type='coverage', prompt_code='coverage',
                prompt_vars={'requirements_text': data.requirement_text, 'cases_text': cases_text},
                list_key='coverage', item_model=schema_mod.CoverageItem, model_id=data.model_id)
            items = result['items']
            missing = sum(1 for it in items if it.get('missing'))
            title = '覆盖度分析（%d 个需求点，%d 个缺口）' % (len(items), missing)
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('coverage', title, 'project', str(data.project_id or 0), _dump_json(items),
                 json.dumps({'points': len(items), 'missing': missing,
                             'elapsed_ms': result['elapsed_ms'],
                             'masked_count': result['masked_count']}, ensure_ascii=False),
                 result['model']['id'], uid))
            return _ok({'report_id': report_id, 'title': title, 'coverage': items,
                        'missing': missing, 'model': result['model'],
                        'usage': result['usage'], 'elapsed_ms': result['elapsed_ms']}, '覆盖度分析完成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='覆盖度分析失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'coverage', uid)


# ---- F16 Prompt 调试台 ----
class PromptRunReq(BaseModel):
    model_id: Optional[int] = None
    system: str = ''
    user: str = ''
    structured: bool = False
    schema_text: str = ''
    vars: Dict[str, Any] = {}


@router.post('/prompt/run')
def prompt_run(request: Request, data: PromptRunReq):
    try:
        uid = _uid(request)
        if not (data.user or '').strip():
            raise HTTPException(status_code=400, detail='请填写用户提示词')
        user_msg = data.user
        if data.structured and data.schema_text:
            user_msg += '\n\n请只输出一个合法 JSON 对象，不要解释文字、不要 markdown 包裹：\n' + data.schema_text
        override = {'system': data.system or '', 'user': user_msg, 'output_schema': None}
        # AI 在途锁：调试运行拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'prompt_run', uid):
                raise HTTPException(status_code=409, detail='调试运行正在处理中，请稍候')
        with get_db_handler() as db:
            res = runner.run_text(
                db, uid, biz_type='prompt_run', prompt_code='__run__',
                prompt_vars=data.vars or {}, model_id=data.model_id, prompt_override=override)
        return _ok({
            'output': res['data'].get('text') or '', 'model': res['model'],
            'usage': res['usage'], 'elapsed_ms': res['elapsed_ms'],
            'attempt': res['attempt'], 'dropped': res.get('dropped'),
            'masked_count': res.get('masked_count'),
        }, '运行完成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='调试运行失败：%s' % e)


# ===========================================================================
# 九、四期：成本看板（F15）/ 缺陷聚类（F9）/ 周报（F10）/ 上下文助手（F11）
# ===========================================================================
def _is_local_url(url: str) -> bool:
    """本地模型（Ollama / vLLM）常无鉴权，识别为本地地址即允许空 key"""
    if not url:
        return False
    return any(seg in url for seg in ('localhost', '127.0.0.1', '0.0.0.0', '::1'))


@router.get('/cost')
def cost_stat(request: Request, days: int = 7):
    """F15 AI 用量与成本看板：按模型 / 功能 / 用户聚合 token、费用、成功率、P95 耗时 + 每日趋势"""
    try:
        days = max(1, min(int(days or 7), 90))
        since = datetime.now() - timedelta(days=days)
        with get_db_handler() as db:
            summary = db.execute_query(
                'SELECT COUNT(*) AS calls, '
                'SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok, '
                'SUM(IFNULL(total_tokens,0)) AS tokens, '
                'ROUND(AVG(elapsed_ms),0) AS avg_ms, MAX(elapsed_ms) AS max_ms '
                'FROM ai_logs WHERE create_time >= %s', (since,))
            p95 = db.execute_query(
                'SELECT ROUND(AVG(elapsed_ms),0) AS p95 FROM ('
                'SELECT elapsed_ms, ROW_NUMBER() OVER (ORDER BY elapsed_ms) rn, '
                'COUNT(*) OVER () c FROM ai_logs WHERE create_time >= %s AND elapsed_ms > 0) t '
                'WHERE rn >= c*0.95', (since,))
            by_model = db.execute_query(
                'SELECT l.model_name, COUNT(*) AS calls, SUM(IFNULL(l.total_tokens,0)) AS tokens, '
                'SUM(CASE WHEN l.success=1 THEN 1 ELSE 0 END) AS ok, '
                'COALESCE(m.cost_per_1k,0) AS cost_per_1k '
                'FROM ai_logs l LEFT JOIN ai_models m ON m.id = l.model_id '
                'WHERE l.create_time >= %s GROUP BY l.model_name, m.cost_per_1k '
                'ORDER BY tokens DESC', (since,))
            by_biz = db.execute_query(
                'SELECT biz_type, COUNT(*) AS calls, SUM(IFNULL(total_tokens,0)) AS tokens, '
                'SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok '
                'FROM ai_logs WHERE create_time >= %s GROUP BY biz_type ORDER BY calls DESC', (since,))
            by_user = db.execute_query(
                'SELECT l.run_by, u.username, COUNT(*) AS calls, SUM(IFNULL(l.total_tokens,0)) AS tokens '
                'FROM ai_logs l LEFT JOIN users u ON u.id = l.run_by '
                'WHERE l.create_time >= %s GROUP BY l.run_by, u.username ORDER BY calls DESC LIMIT 20', (since,))
            daily = db.execute_query(
                'SELECT DATE(create_time) AS day, COUNT(*) AS calls, SUM(IFNULL(total_tokens,0)) AS tokens '
                'FROM ai_logs WHERE create_time >= %s GROUP BY DATE(create_time) ORDER BY day', (since,))

            s = summary[0] if summary else {}
            calls = int(s.get('calls') or 0)
            ok = int(s.get('ok') or 0)
            total_tokens = int(s.get('tokens') or 0)
            cost_total = 0.0
            models_out = []
            for m in (by_model or []):
                tok = int(m.get('tokens') or 0)
                price = float(m.get('cost_per_1k') or 0)
                cost = round(tok / 1000.0 * price, 4) if price else 0.0
                cost_total += cost
                c = int(m.get('calls') or 0)
                models_out.append({
                    'model_name': m.get('model_name') or '未知模型', 'calls': c, 'tokens': tok,
                    'success': int(m.get('ok') or 0),
                    'success_rate': round(int(m.get('ok') or 0) * 100.0 / max(c, 1), 1),
                    'cost_per_1k': price, 'cost': cost,
                })
            for b in (by_biz or []):
                c = int(b.get('calls') or 0)
                b['success_rate'] = round(int(b.get('ok') or 0) * 100.0 / max(c, 1), 1)
            for u in (by_user or []):
                u['username'] = u.get('username') or ('用户#%s' % u.get('run_by'))
            return _ok({
                'days': days,
                'summary': {
                    'calls': calls, 'success': ok, 'failed': calls - ok,
                    'success_rate': round(ok * 100.0 / calls, 1) if calls else 0.0,
                    'tokens': total_tokens, 'cost': round(cost_total, 4),
                    'avg_ms': int(s.get('avg_ms') or 0), 'max_ms': int(s.get('max_ms') or 0),
                    'p95_ms': int((p95[0].get('p95') if p95 else 0) or 0),
                },
                'by_model': models_out, 'by_biz': by_biz or [],
                'by_user': by_user or [], 'daily': daily or [],
            })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='成本统计失败：%s' % e)


class WeeklyReportReq(BaseModel):
    start: str
    end: str
    model_id: Optional[int] = None


@router.post('/weekly-report')
def weekly_report(request: Request, data: WeeklyReportReq):
    """F10 周报/日报自动生成：接口 + 任务 + 用例三线数据 → AI 生成 Markdown 周报"""
    try:
        uid = _uid(request)
        start = (data.start or '')[:10]
        end = (data.end or '')[:10]
        if not start or not end:
            raise HTTPException(status_code=400, detail='请选择开始与结束日期')
        with get_db_handler() as db:
            end_ts = end + ' 23:59:59'
            exec_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok, '
                'SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail '
                'FROM api_executions WHERE run_time >= %s AND run_time <= %s', (start, end_ts))
            task_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN task_status=4 THEN 1 ELSE 0 END) AS done '
                'FROM task_management WHERE create_time >= %s AND create_time <= %s', (start, end_ts))
            case_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN ai_generated=1 THEN 1 ELSE 0 END) AS ai '
                'FROM test_cases WHERE create_time >= %s AND create_time <= %s', (start, end_ts))
            es = exec_stat[0] if exec_stat else {}
            ts = task_stat[0] if task_stat else {}
            cs = case_stat[0] if case_stat else {}
            brief = (
                '【接口自动化】区间执行 %d 次，通过 %d，失败 %d。\n'
                '【测试任务】区间新建任务 %d 个，已完成 %d 个。\n'
                '【测试用例】区间新增用例 %d 条，其中 AI 生成 %d 条。\n'
                % (int(es.get('n') or 0), int(es.get('ok') or 0), int(es.get('fail') or 0),
                   int(ts.get('n') or 0), int(ts.get('done') or 0),
                   int(cs.get('n') or 0), int(cs.get('ai') or 0))
            )
            result = runner.run_text(
                db, uid, biz_type='weekly_report', prompt_code='weekly_report',
                prompt_vars={'range_text': '%s ~ %s' % (start, end), 'brief': brief},
                model_id=data.model_id)
            md = result['data'].get('text') or ''
            title = '测试周报（%s ~ %s）' % (start, end)
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('weekly', title, 'week', '%s~%s' % (start, end), md,
                 json.dumps({'brief': brief, 'elapsed_ms': result['elapsed_ms']}, ensure_ascii=False, default=str),
                 result['model']['id'], uid))
            return _ok({
                'report_id': report_id, 'title': title, 'content_md': md,
                'model': result['model'], 'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
            }, '周报已生成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成周报失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'weekly_report', uid)


# ===== F10 周报：禅道缺陷导出导入（F10a 解析 / F10b 汇总 / F10e 合并 / F10c 生成）=====
_ZENTAO_SEV_MAP = {'1': '致命', '2': '严重', '3': '一般', '4': '提示'}
_ZENTAO_SEV_NAME = {'致命': '致命', '严重': '严重', '一般': '一般', '提示': '提示'}
_ZENTAO_SEV_CODE = {'fatal': '致命', 'serious': '严重', 'normal': '一般', 'prompt': '提示'}
_ZENTAO_RESOLVED_STATUS = {'已解决', '已关闭'}  # 视为已解决


def _zentao_split_enum(val):
    """禅道导出枚举格式 '显示名(#code)' → (name, code)。如 '3(#3)'→('3','3')；'激活(#active)'→('激活','active')"""
    if val is None:
        return '', ''
    s = str(val).strip()
    m = re.match(r'^(.*?)\(#([^)]*)\)\s*$', s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ''


def _zentao_norm_severity(val):
    name, code = _zentao_split_enum(val)
    if name in _ZENTAO_SEV_MAP:
        return _ZENTAO_SEV_MAP[name]
    if name in _ZENTAO_SEV_NAME:
        return name
    if code in _ZENTAO_SEV_CODE:
        return _ZENTAO_SEV_CODE[code]
    return name or '未知'


def _zentao_norm_status(val):
    name, code = _zentao_split_enum(val)
    return name or code or '未知'


def _zentao_module_from_title(title):
    if not title:
        return '未分类'
    m = re.search(r'【([^】]+)】', str(title))
    return m.group(1).strip() if m else '未分类'


def _parse_zentao_bugs_from_rows(header, rows):
    """禅道缺陷固定模板解析 + 结构化汇总。header: list; rows: list[list/tuple]。返回 (records, summary, preview)"""
    from collections import Counter
    col = {str(h).strip(): i for i, h in enumerate(header)}

    def pick(r, *names):
        for n in names:
            if n in col:
                idx = col[n]
                if idx < len(r):
                    return r[idx]
        return None

    records = []
    for r in rows:
        if r is None:
            continue
        if not any(c is not None and str(c).strip() != '' for c in r):
            continue
        bid = str(pick(r, 'Bug编号') or '').strip().replace('\t', '')
        title = str(pick(r, 'Bug标题') or '').strip()
        if not bid and not title:
            continue
        created = pick(r, '创建日期')
        created = created.strftime('%Y-%m-%d') if hasattr(created, 'strftime') else (str(created)[:10] if created else '')
        _a_name, _a_code = _zentao_split_enum(pick(r, '指派给'))
        assigned = str(_a_name or '').strip()
        if assigned.lower() == 'closed' or str(_a_code).lower() == 'closed':
            assigned = '（已关闭）'
        records.append({
            'id': bid,
            'title': title,
            'severity': _zentao_norm_severity(pick(r, '严重程度')),
            'priority': _zentao_norm_severity(pick(r, '优先级')),
            'status': _zentao_norm_status(pick(r, 'Bug状态')),
            'module': _zentao_module_from_title(title),
            'assigned_to': assigned,
            'created': created,
        })
    total = len(records)
    sev_c = Counter(x['severity'] for x in records)
    status_c = Counter(x['status'] for x in records)
    prio_c = Counter(x['priority'] for x in records)
    mod_c = Counter(x['module'] for x in records)
    assigned_c = Counter(x['assigned_to'] for x in records if x['assigned_to'])
    resolved = sum(1 for x in records if x['status'] in _ZENTAO_RESOLVED_STATUS)
    summary = {
        'total': total,
        'resolved': resolved,
        'resolve_rate': round(resolved / total * 100, 1) if total else 0,
        'by_severity': dict(sev_c),
        'by_status': dict(status_c),
        'by_priority': dict(prio_c),
        'by_module': dict(mod_c.most_common(10)),
        'by_assigned': dict(assigned_c.most_common(10)),
    }
    return records, summary, records[:8]


@router.post('/weekly/import-parse')
async def weekly_import_parse(request: Request, file: UploadFile = File(None)):
    """F10a/F10b：解析禅道缺陷导出文件(CSV/XLSX) → 预览 + 结构化汇总（不调 AI）"""
    try:
        _uid(request)
        if not file:
            raise HTTPException(status_code=400, detail='请上传禅道导出文件（CSV/XLSX）')
        raw = await file.read()
        filename = file.filename or ''
        ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        if ext == 'csv':
            import csv as _csv
            text = raw.decode('utf-8-sig', errors='ignore')
            reader = list(_csv.reader(io.StringIO(text)))
            if not reader:
                raise HTTPException(status_code=400, detail='CSV 为空')
            header = reader[0]
            rows = reader[1:]
        elif ext in ('xlsx', 'xls'):
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            try:
                header = list(next(it))
            except StopIteration:
                raise HTTPException(status_code=400, detail='Excel 为空')
            rows = [list(r) for r in it]
        else:
            raise HTTPException(status_code=400, detail='仅支持 CSV / XLSX 文件')
        records, summary, preview = _parse_zentao_bugs_from_rows(header, rows)
        if not records:
            raise HTTPException(status_code=400, detail='未解析到有效 Bug 记录，请检查文件列名（需含 Bug编号 / Bug标题 等）')
        return _ok({
            'filename': filename,
            'file_type': ext,
            'total': summary['total'],
            'columns': [str(h) for h in header],
            'preview': preview,
            'summary': summary,
        }, '解析完成')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='解析失败：%s' % e)


class WeeklyImportSummarizeReq(BaseModel):
    start: str
    end: str
    file_name: str = ''
    summary: Dict[str, Any]
    top_details: str = ''
    model_id: Optional[int] = None


@router.post('/weekly/import-summarize')
def weekly_import_summarize(request: Request, data: WeeklyImportSummarizeReq):
    """F10e/F10c：合并内部三线 + 禅道导入 → AI 生成周报 → 存 ai_reports(report_type=weekly_zentao)"""
    try:
        uid = _uid(request)
        start = (data.start or '')[:10]
        end = (data.end or '')[:10]
        if not start or not end:
            raise HTTPException(status_code=400, detail='请选择开始与结束日期')
        with get_db_handler() as db:
            end_ts = end + ' 23:59:59'
            exec_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok, '
                'SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail '
                'FROM api_executions WHERE run_time >= %s AND run_time <= %s', (start, end_ts))
            task_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN task_status=4 THEN 1 ELSE 0 END) AS done '
                'FROM task_management WHERE create_time >= %s AND create_time <= %s', (start, end_ts))
            case_stat = db.execute_query(
                'SELECT COUNT(*) AS n, SUM(CASE WHEN ai_generated=1 THEN 1 ELSE 0 END) AS ai '
                'FROM test_cases WHERE create_time >= %s AND create_time <= %s', (start, end_ts))
            es = exec_stat[0] if exec_stat else {}
            ts = task_stat[0] if task_stat else {}
            cs = case_stat[0] if case_stat else {}
            internal_brief = (
                '【接口自动化】区间执行 %d 次，通过 %d，失败 %d。\n'
                '【测试任务】区间新建任务 %d 个，已完成 %d 个。\n'
                '【测试用例】区间新增用例 %d 条，其中 AI 生成 %d 条。\n'
                % (int(es.get('n') or 0), int(es.get('ok') or 0), int(es.get('fail') or 0),
                   int(ts.get('n') or 0), int(ts.get('done') or 0),
                   int(cs.get('n') or 0), int(cs.get('ai') or 0))
            )
            s = data.summary or {}
            zentao_lines = ['- 总数 %d，已解决 %d（解决率 %s%%）'
                            % (s.get('total', 0), s.get('resolved', 0), s.get('resolve_rate', 0))]
            if s.get('by_severity'):
                zentao_lines.append('- 按严重程度：' + '、'.join('%s %d' % (k, v) for k, v in s['by_severity'].items()))
            if s.get('by_status'):
                zentao_lines.append('- 按状态：' + '、'.join('%s %d' % (k, v) for k, v in s['by_status'].items()))
            if s.get('by_module'):
                zentao_lines.append('- TOP 模块：' + '、'.join('%s %d' % (k, v) for k, v in list(s['by_module'].items())[:6]))
            zentao_brief = '\n'.join(zentao_lines)
            result = runner.run_text(
                db, uid, biz_type='weekly_zentao', prompt_code='weekly_zentao',
                prompt_vars={
                    'range_text': '%s ~ %s' % (start, end),
                    'internal_brief': internal_brief,
                    'file_name': data.file_name or '禅道导出文件',
                    'total': s.get('total', 0),
                    'zentao_brief': zentao_brief,
                    'top_details': data.top_details or '（无）',
                },
                model_id=data.model_id)
            md = result['data'].get('text') or ''
            title = '测试周报-禅道合并（%s ~ %s）' % (start, end)
            report_id = _insert_id(
                db,
                'INSERT INTO ai_reports (report_type, title, biz_type, biz_id, content_md, summary_json, '
                'model_id, run_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                ('weekly_zentao', title, 'week', '%s~%s' % (start, end), md,
                 json.dumps({'internal_brief': internal_brief, 'zentao_summary': s,
                             'elapsed_ms': result['elapsed_ms']}, ensure_ascii=False, default=str),
                 result['model']['id'], uid))
            return _ok({
                'report_id': report_id, 'title': title, 'content_md': md,
                'model': result['model'], 'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
            }, '周报已生成')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成周报失败：%s' % e)


class AssistantReq(BaseModel):
    context: str = ''
    message: str
    model_id: Optional[int] = None


@router.post('/assistant')
def assistant_chat(request: Request, data: AssistantReq):
    """F11 上下文感知助手：结合当前页/任务/批次上下文回答用户问题"""
    try:
        uid = _uid(request)
        if not (data.message or '').strip():
            raise HTTPException(status_code=400, detail='请输入消息')
        # AI 在途锁：助手回复拒绝重复提交（刷新/切页后仍可判定在途）
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'assistant', uid):
                raise HTTPException(status_code=409, detail='助手正在回复中，请稍候')
        with get_db_handler() as db:
            result = runner.run_text(
                db, uid, biz_type='assistant', prompt_code='assistant',
                prompt_vars={
                    'assistant_context': (data.context or '（无附加上下文）')[:4000],
                    'user_message': data.message[:4000],
                },
                model_id=data.model_id)
            return _ok({
                'text': result['data'].get('text') or '',
                'model': result['model'], 'usage': result['usage'],
                'elapsed_ms': result['elapsed_ms'],
            }, 'ok')
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='助手回复失败：%s' % e)


# ===========================================================================
# 十、接口用例 AI 生成（接口 → 可执行测试场景）
#
# 方案：D:/workbuddy_file/api-case-generation-plan.md
# P0 交付 6 个接口：probe / plan / gen / gate / history / apply
#
# 三条不可动摇的边界（改代码前先读）：
# 1. **automation.py 零改动**。产物写进 api_interfaces.dataset，_run_interface 已完整支持
#    场景对象 {name, params, headers, body, asserts}，params 值 null 会移除该键。
# 2. **不产生假通过**。I05 越权维度本期全链路禁止（coverage.API_DIMS 无此项 +
#    ApiCaseSceneItem 白名单过滤 + 落地时丢弃 headers）。引擎无法从环境头移除 X-Token
#    （{**env_headers, **iface_headers} 只能覆盖不能删除），强行生成越权场景会带着
#    有效 token 拿 200 → 断言通过 → 报告说测过了，实际根本没测。
# 3. **写接口默认不发真实请求**（决策 2.A）。POST/PUT/DELETE 默认读最近一次成功执行记录；
#    用户显式 force_live 才真跑。且 I06 幂等维度只对 GET/HEAD 生成。
#
# 与 ai_case_gen（文档用例）完全独立：独立历史表、独立维度组（I 组）、独立 biz_type，
# 只共用 runner / mask / coverage 去重 / inflight / logs 底座。
# ===========================================================================
from src.ai import api_probe as probe_mod   # noqa: E402  放在常量之后便于阅读

API_CASE_MIN_COUNT = 3           # 单接口目标条数下限
API_CASE_MAX_COUNT = 12          # 上限（决策 7：默认 6，范围 3~12）
API_CASE_PLAN_MAX_CELLS = 7      # I 组只有 7 个维度，格子数天然有上限
API_CASE_PLAN_MAX_PER_CELL = 3   # 每格最多 3 条，避免某一维刷屏
API_CASE_GATE_MAX_ROUNDS = 1     # 闸门补跑硬上限 1 轮（结构上杜绝死循环）
API_CASE_BATCH_GAP = 1.0         # 批次间隔秒（同 case_gen，实测降瞬断错误）
#: 场景去重阈值比文档用例（0.75）略严：场景是结构化的，描述撞了往往就是真的重复
API_CASE_DUP_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# 入参模型
# ---------------------------------------------------------------------------
class ApiCaseProbeReq(BaseModel):
    interface_id: int
    env_id: Optional[int] = None
    force_live: bool = False      # 写接口要真跑必须显式打开（决策 2.A）


class ApiCaseGenReq(BaseModel):
    interface_id: int
    env_id: Optional[int] = None
    count: int = 6
    model_id: Optional[int] = None
    force_live: bool = False
    use_probe: bool = True         # false = 纯静态推断（更快，但质量降一档且全标 assumption）
    use_plan: bool = True          # 三段式：G1 规划 → G2 生成 → G3 闸门补跑
    plan: Optional[List[dict]] = None   # 断点续跑：复用上一批的 plan，省掉重复 G1


class ApiCaseHistorySaveReq(BaseModel):
    interface_id: int
    model_id: Optional[int] = None
    model_name: str = ''
    probe_mode: str = 'none'
    probe_exec_id: Optional[int] = None
    probe_size: int = 0
    probe_sent: int = 0
    probe_masked: int = 0
    probe_truncated: int = 0
    elapsed_ms: int = 0
    calls_used: int = 0
    attempt: int = 1
    items: List[dict] = []


class ApiCaseGateReq(BaseModel):
    """G3 独立调用：自带接口 ID + 待检查场景 + 可选覆盖计划。

    不复用 `ApiCaseGenReq`：gate 的 `scenes` 是主体入参，塞进 gen 的模型里会
    让"生成时不该传 scenes"这种误调用也合法。
    """
    interface_id: int
    scenes: List[dict] = []
    plan: Optional[List[dict]] = None
    model_id: Optional[int] = None


class ApiCaseApplyReq(BaseModel):
    interface_id: int
    history_id: Optional[int] = None
    scenes: List[dict] = []        # 要落地的场景（前端回传，可能已被手工编辑过）
    mode: str = 'append'           # append追加 / replace替换 / fill_empty仅补空（决策 4.A）
    dry_run: bool = False          # 只算 diff 不落库，供前端二次确认


class ApiCaseDiagnoseReq(BaseModel):
    """F5 失败自愈：针对一条失败的执行记录做诊断，只出建议不改写。"""
    execution_id: int
    model_id: Optional[int] = None


class ApiCaseRefineReq(BaseModel):
    """F6 对话式精调：在已有场景基础上按自然语言指令增量补充场景。"""
    interface_id: int
    instruction: str                         # 用户自然语言指令，如"再生成3条边界值"
    existing_scenes: List[Dict[str, Any]] = []  # 当前已存在的场景（去重参考）
    env_id: Optional[int] = None
    model_id: Optional[int] = None
    force_live: bool = False
    use_probe: bool = True


class ApiSceneInput(BaseModel):
    """前端回传的一条场景。刻意比生成模型宽松 —— 用户可能在预览弹窗里
    手工改过 params/asserts，不能因为类型再刁难他。"""

    id: Optional[int] = None
    scene_name: str = ''
    case_tag: str = ''
    dims: List[Any] = []
    severity: str = 'S2'
    evidence_type: str = 'assumption'
    case_type: str = ''
    design_method: str = ''
    params: Any = {}
    body: Any = None
    headers: Any = {}
    asserts: Any = []
    pre_condition: str = ''
    steps: str = ''
    expected: str = ''
    note: str = ''
    confidence: float = 0.0

    @field_validator('scene_name', 'case_tag', 'case_type', 'design_method',
                     'pre_condition', 'steps', 'expected', 'note', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('dims', mode='before')
    @classmethod
    def _d(cls, v):
        # ⚠️ 两步都要做：_as_multi_str 负责把 'I02,I05' / ['I02'] / None 统一成 list，
        # normalize_api_dims 才负责白名单过滤（丢掉 I05 与 C/R 组编号）。
        # 只做前者会把 I05 原样带进落地结果 —— 那正是"假通过"的来源。
        return coverage_mod.normalize_api_dims(_as_multi_str(v))


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _api_iface(db, interface_id: int) -> Dict[str, Any]:
    rows = db.execute_query('SELECT * FROM api_interfaces WHERE id = %s', (int(interface_id),))
    if not rows:
        raise HTTPException(status_code=404, detail='接口不存在或已被删除')
    return rows[0]


def _api_iface_vars(iface: Dict[str, Any]) -> set:
    """接口自身引用的变量集合。场景里出现集合外的 {{var}} 就是模型自造的。"""
    blob = ' '.join([
        str(iface.get('url') or ''),
        json.dumps(_json_loads(iface.get('params')) or {}, ensure_ascii=False, default=str),
        json.dumps(_json_loads(iface.get('body')) or {}, ensure_ascii=False, default=str),
    ])
    return {m for m in probe_mod._VAR_RE.findall(blob) if m != 'base_url'}


def _api_live_runner(iface: Dict[str, Any], env_id: Optional[int]):
    """构造"真实试跑一次"的异步回调，交给 api_probe 决定要不要用。

    复用 `automation._execute_interface` 而不是自己发 HTTP —— 依赖变量缺失处理、
    环境头合并、SSL 开关、失败重试都在那里踩过坑了，重复实现必然行为不一致，
    且取证响应与执行时响应不同源，模型反推的断言会对不上。

    ⚠️ 取证**不落库 api_executions**：这是"为了看一眼"的调用，写进执行记录会污染
    执行统计与报告（用户没点试跑却多了一条记录）。
    """
    async def _run() -> Dict[str, Any]:
        from src.routers import automation as auto
        if env_id is None:
            raise HTTPException(status_code=400, detail='真实试跑需要先选择执行环境')
        with get_db_handler() as _db:
            env = auto._resolve_env(_db, env_id)
            ctx = auto._build_context(env, {})
            return await auto._execute_interface(iface, ctx, env)

    return _run


async def _api_probe(db, uid: int, iface: Dict[str, Any],
                     env_id: Optional[int] = None,
                     force_live: bool = False,
                     enabled: bool = True) -> Dict[str, Any]:
    """分级取证 + 骨架化 + 脱敏 + 截断，返回 pack_probe 的结果。

    `enabled=False`（用户关掉了取证）时返回空取证块，所有场景按 assumption 处理。
    """
    if not enabled:
        return _empty_probe()
    is_write = probe_mod.is_write_method(iface.get('method'))
    # 写接口且未强制 → 完全不构造 live_runner，从根上杜绝误发写请求
    live = None if (is_write and not force_live) else _api_live_runner(iface, env_id)
    # 脱敏规则与需求分析侧同源（runner 也读这一项），运营改一处两边生效
    rules = settings_mod.get(db, 'ai_mask_rules') or ''
    try:
        src = await probe_mod.probe_source(
            db, iface, env_id, force_live=force_live, live_runner=live)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning('api_probe 取证异常（降级为无响应）: %s', e)
        src = {}
    out = probe_mod.pack_probe(src or {}, iface, rules or None)
    if force_live and is_write:
        out['warn'] = (out.get('warn') or '') + \
            '；本次已按你的勾选真实发送了写请求，请自行确认是否产生了脏数据'
    return out


def _empty_probe() -> Dict[str, Any]:
    return {'mode': probe_mod.PROBE_MODE_NONE, 'label': '未取证', 'text': '', 'size': 0,
            'sent': 0, 'masked': 0, 'truncated': False, 'stale': False,
            'warn': '未取证：全部条目按接口定义推断，evidence_type 均为 assumption'}


def _api_plan_text(plan: List[Dict[str, Any]]) -> str:
    """把覆盖计划渲染成 api_case_gen 的 plan_text 变量。"""
    if not plan:
        return ''
    lines = ['【本次覆盖计划（必须逐格写足，缺格视为未完成）】']
    for i, p in enumerate(plan, 1):
        bits = []
        if p.get('dim'):
            bits.append('维度 %s %s' % (p['dim'], p.get('dim_name') or ''))
        head = '第%d格' % i
        if bits:
            head += '（%s）' % '，'.join(bits)
        extra = []
        if p.get('case_type'):
            extra.append('类型 %s' % p['case_type'])
        if p.get('design_method'):
            extra.append('设计方法 %s' % p['design_method'])
        if p.get('severity'):
            extra.append('严重度 %s' % p['severity'])
        if p.get('evidence_type'):
            extra.append('证据 %s' % p['evidence_type'])
        if extra:
            head += '，' + '，'.join(extra)
        lines.append('%s：%s；本格写 %d 条'
                     % (head, p.get('focus') or '（按该维度常规验证点）',
                        int(p.get('planned') or 1)))
        if p.get('note'):
            lines.append('    依据说明：%s' % p['note'])
    lines.append('每条场景的 dims 必须包含该格的 dim；不同格的场景 params/asserts 必须实质不同，'
                 '禁止跨格重复。')
    return '\n'.join(lines)


def _api_trim_plan(plan: List[Dict[str, Any]], count: int,
                   method: str) -> List[Dict[str, Any]]:
    """收敛 G1 产出：剔非法维度 → 写接口剔 I06 → 同维去重 → 按严重度排序 → planned 封顶。"""
    order = {'S0': 0, 'S1': 1, 'S2': 2, 'S3': 3}
    dedup: Dict[str, Dict[str, Any]] = {}
    for p in plan or []:
        # ⚠️ 传 method：normalize_api_dims 内部会对写接口剔掉 I06
        dim = (coverage_mod.normalize_api_dims(p.get('dim'), method) or [''])[0]
        if not dim:
            continue                      # I05 / C 组 / R 组编号 / 空 → 直接丢
        p = dict(p)
        p['dim'] = dim
        p['dim_name'] = coverage_mod.API_DIM_INFO.get(dim, ('', ''))[0]
        old = dedup.get(dim)
        # 同一维度只留一格（模型偶尔把同一维度排两遍），保留 planned 大的那条
        if old is None or int(p.get('planned') or 1) > int(old.get('planned') or 1):
            dedup[dim] = p
    ranked = sorted(dedup.values(),
                    key=lambda p: (order.get(p.get('severity') or 'S2', 2),
                                   -int(p.get('planned') or 1)))
    out: List[Dict[str, Any]] = []
    total = 0
    for p in ranked:
        if len(out) >= API_CASE_PLAN_MAX_CELLS:
            break
        remain = count - total
        if remain <= 0:
            break
        p['planned'] = max(1, min(int(p.get('planned') or 1),
                                  API_CASE_PLAN_MAX_PER_CELL, remain))
        out.append(p)
        total += p['planned']
    return out


def _api_scenes_text(scenes: List[Dict[str, Any]]) -> str:
    """G3 用：只带判定覆盖所需的字段，避免把 params/body 全量塞进去撑爆 token。"""
    keys = ('scene_name', 'dims', 'case_type', 'design_method',
            'params', 'asserts', 'expected')
    return json.dumps([{k: s.get(k) for k in keys} for s in scenes or []],
                      ensure_ascii=False, default=str)[:8000]


async def _api_run_gate(db, uid: int, plan: List[Dict[str, Any]],
                        scenes: List[Dict[str, Any]], method: str,
                        model_id: Optional[int]) -> List[Dict[str, Any]]:
    """G3：对照计划核对已有场景，返回真实覆盖缺口（失败时降级为按计划盲补）。"""
    if not plan or not scenes:
        return []
    try:
        r = runner.run_structured(
            db, uid, biz_type='api_case_gate', prompt_code='api_case_gate',
            prompt_vars={
                'plan_text': json.dumps(plan, ensure_ascii=False, default=str)[:4000],
                'scenes_text': _api_scenes_text(scenes),
                'method': method,
            },
            list_key='gaps', item_model=schema_mod.ApiCaseGapItem,
            limit=6, model_id=model_id,
        )
        # ⚠️ run_structured 返回的 items 已是 dict（runner 内部 model_dump 过），
        # 这里绝不能再 .dict() —— 会抛 AttributeError 被 except 吞成静默降级。
        gaps: List[Dict[str, Any]] = []
        for g in r['items']:
            # 二次过滤：模型可能在闸门里又吐出 I05 / C 组编号
            dim = (coverage_mod.normalize_api_dims(g.get('dim'), method) or [''])[0]
            if not dim:
                continue
            g['dim'] = dim
            gaps.append(g)
        return gaps
    except Exception as ge:  # noqa: BLE001
        logger.warning('api_case_gate 执行失败（降级为按计划盲补）: %s', ge)
        fallbacks = []
        for p in sorted(plan, key=lambda x: -(int(x.get('planned') or 1)))[:2]:
            fallbacks.append({
                'dim': p.get('dim') or '', 'missing': int(p.get('planned') or 1),
                'severity': p.get('severity') or 'S2',
                'suggestion': p.get('focus') or '',
            })
        return fallbacks


def _api_backfill_asserts(scenes: List[Dict[str, Any]],
                         probe: Dict[str, Any]) -> List[Dict[str, Any]]:
    """给漏写断言的场景补一条兜底 JSONPath 断言，返回被补的场景名列表。

    实测（10/15，qwen3.8-flash 6 条里漏 1 条）：提示词已经写了"每条至少 1 条断言"，
    模型仍会漏；而 G3 闸门是**维度级**的（只比计划里的 dim 够不够），维度齐了它就
    报 `gaps=[]`，压根发现不了断言缺失。

    不补的后果不是"少一条断言"这么简单：`automation.py:887` 是
    `if sc.get('asserts')` 才覆盖接口级断言，场景没写断言就会**静默回退到接口级**，
    而接口级通常只有 `status_code=200` —— 正是本期要消灭的浅断言，用户还以为自己
    拿到的是 json_path 深断言。所以这里必须本地确定性补齐，不依赖模型自觉。

    补的值一律用 `=>非空`：宁可只验"字段存在且非空"，也不要替模型猜具体期望值
    （猜错会产出假失败，用户会以为接口有问题）。有真实响应时不猜值，
    只验结构；这属于"补齐下限"而不是"提升断言质量"。
    """
    if not scenes:
        return []
    filled: List[str] = []
    for sc in scenes:
        if sc.get('asserts'):
            continue
        sc['asserts'] = [{'assert_type': 'json_path',
                          'expected_value': '$.status=>非空', 'enabled': 1}]
        sc['asserts_backfilled'] = 1        # 前端可据此标注「系统补齐」
        filled.append(str(sc.get('scene_name') or ''))
    return filled


# ---------------------------------------------------------------------------
# 智能断言建议（F1）：从真实响应体确定性提取 JSONPath 断言「建议」
# ---------------------------------------------------------------------------
#: 字段名黑名单：这些通常是动态 / 随机 / 时间戳 / 鉴权类，断言它们会产生「假失败」，
#: 默认不预选（前端标记「不稳定」，需用户主动勾选才落地）。
_SUGGEST_UNSAFE_FIELDS = {
    # 鉴权 / 签名 / 密钥
    'token', 'tokens', 'access_token', 'refresh_token', 'sign', 'signature',
    'sign_key', 'nonce', 'salt', 'secret', 'password', 'pwd', 'passwd',
    'session', 'session_id', 'sessionid', 'cookie', 'ticket', 'verify_code',
    # 时间 / 版本
    'timestamp', 'time', 'date', 'datetime', 'create_time', 'update_time',
    'created_at', 'updated_at', 'create_at', 'expire', 'expires', 'expired_at',
    'expire_at', 'sign_time',
    # 随机 / 追踪 / 主键（跨请求会变，断言易假失败）
    'rand', 'random', 'uuid', 'id', 'ids', 'trace_id', 'request_id', 'req_id',
    'msg_id', 'log_id',
}

#: 建议上限，避免大响应刷屏
_SUGGEST_MAX = 30


def _suggest_field_name(path: str) -> str:
    """从 JSONPath 取末段字段名（去掉 [0] 等下标）。如 $.data.list[0].id → id。"""
    seg = re.sub(r'\[\d+\]', '', path).split('.')[-1]
    return seg.lower()


def _suggest_is_safe(path: str) -> bool:
    return _suggest_field_name(path) not in _SUGGEST_UNSAFE_FIELDS


def _suggest_sample(v: Any) -> str:
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.strip()[:30]
    if isinstance(v, list):
        return '数组(%d)' % len(v)
    if isinstance(v, dict):
        return '对象(%d键)' % len(v)
    return str(v)[:30]


def _suggest_add(out: List[Dict[str, Any]], path: str, op: str,
                 ftype: str, sample: str) -> None:
    expr = '%s=>%s' % (path, op)
    if any(o['expr'] == expr for o in out):
        return
    out.append({
        'expr': expr, 'path': path, 'op': op, 'field_type': ftype,
        'sample': sample, 'safe': _suggest_is_safe(path),
        'reason': '真实响应中该字段为%s' % ftype,
    })


def _suggest_from_value(out: List[Dict[str, Any]], path: str, v: Any) -> None:
    """对一个具体值产出一条建议（布尔字面量跳过，避免与引擎比对不一致）。"""
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        _suggest_add(out, path, '数字', 'number', str(v))
    elif isinstance(v, str):
        if v.strip():
            _suggest_add(out, path, '非空', 'string', v.strip()[:30])
    # None / 空串不产出（没东西可断言）


def build_assert_suggestions(probe: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从取证得到的真实响应体，确定性提取 JSONPath 断言「建议」列表。

    与模型生成的区别：这里**不调模型**，纯解析响应结构，产出引擎已有算子能直接
    命中的断言（非空 / 数组 / 数字）。目的是把「刚修好的 json_path 引擎」用起来——
    让用户勾选后落地深断言，而不是只有 body_status 浅断言。

    设计要点：
      - 只提取、不注入：结果作为独立 `assert_suggestions` 返回，前端勾选后才合并进场景。
      - 不稳定字段（id / 时间戳 / token 等）标记 `safe=false`，前端默认不预选。
      - 响应体可能已脱敏，只影响样例展示，不影响字段名 / 类型推断。
      - 解析失败 / 非 JSON / 根非对象 → 返回空列表，不阻断生成。
    """
    out: List[Dict[str, Any]] = []
    body = probe.get('body_text') or ''
    if not body:
        return out
    try:
        data = json.loads(body)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    def _walk(prefix: str, obj: Dict[str, Any], depth: int) -> None:
        if not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if len(out) >= _SUGGEST_MAX:
                return
            path = '%s.%s' % (prefix, k)
            if isinstance(v, dict):
                if depth < 2 and v:
                    _walk(path, v, depth + 1)        # 继续下钻，不给容器本身建议（降噪）
                else:
                    _suggest_add(out, path, '非空', 'object', _suggest_sample(v))
            elif isinstance(v, list):
                _suggest_add(out, path, '数组', 'array', _suggest_sample(v[0] if v else None))
                if v and isinstance(v[0], dict) and depth < 2:
                    for j, (sk, sv) in enumerate(v[0].items()):
                        if j >= 3 or len(out) >= _SUGGEST_MAX:
                            break
                        _suggest_from_value(out, '%s[0].%s' % (path, sk), sv)
            else:
                _suggest_from_value(out, path, v)

    _walk('$', data, 0)
    return out


def _api_scene_dedup_key(sc: Dict[str, Any]) -> Tuple[str, ...]:
    """场景精确去重键：维度 + 参数 + 断言。

    比文档用例多带了 params/asserts —— 场景的"验证点"就在这两处，
    只比描述文字会漏掉「描述不同但断言一样」的实质重复。
    """
    def _n(v: Any) -> str:
        return re.sub(r'\s+', '', json.dumps(v, ensure_ascii=False, default=str, sort_keys=True))
    return (str(sc.get('scene_name') or '').strip().lower(),
            ','.join(coverage_mod.normalize_api_dims(sc.get('dims'))),
            _n(sc.get('params')), _n(sc.get('body')), _n(sc.get('asserts')))


def _api_scene_grams(sc: Dict[str, Any]):
    """场景语义去重的 3-gram 集合。取 scene_name + steps + expected 拼起来算。"""
    from src.ai.coverage import _grams
    return _grams('%s %s %s' % (sc.get('scene_name') or '',
                                sc.get('steps') or '',
                                sc.get('expected') or ''))


# ---------------------------------------------------------------------------
# 1) 取证预览：POST /ai/api-case/probe
# ---------------------------------------------------------------------------
@router.post('/api-case/probe')
async def api_case_probe(request: Request, data: ApiCaseProbeReq):
    """按方法分级取证，返回骨架化响应（生成前给用户确认用）。

    GET/HEAD 默认真实试跑；POST/PUT/DELETE 默认读最近一次成功执行记录，
    用户勾「强制试跑取样」才真发请求。
    """
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            is_write = probe_mod.is_write_method(iface.get('method'))
            # 真跑必须有环境；历史取证不需要。这里提前拦，避免白等一次网络往返
            if (data.force_live or not is_write) and data.env_id is None:
                raise HTTPException(
                    status_code=400,
                    detail='%s接口默认需要真实试跑取证，请先选择执行环境'
                           % ('写操作' if is_write else '读'))
            src = await _api_probe(db, uid, iface, data.env_id, data.force_live, True)
            return _ok({
                'interface': {
                    'id': iface['id'], 'name': iface.get('name') or '',
                    'method': (iface.get('method') or 'GET').upper(),
                    'url': iface.get('url') or '', 'group': iface.get('group') or '',
                    'enabled': int(iface.get('enabled') or 0),
                },
                'probe_mode': src.get('mode'),
                'probe_label': src.get('label'),
                'probe_size': src.get('size'),
                'probe_sent': src.get('sent'),
                'masked': src.get('masked'),
                'truncated': src.get('truncated'),
                'stale': src.get('stale'),
                'warn': src.get('warn'),
                'exec_id': src.get('exec_id'),
                'run_time': src.get('run_time'),
                'http_status': src.get('http_status'),
                'is_write_method': is_write,
                'text': src.get('text') or '',
            }, '取证完成（%s）' % (src.get('label') or '未知'))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='取证失败：%s' % e)


# ---------------------------------------------------------------------------
# 2) G1 覆盖规划：POST /ai/api-case/plan
# ---------------------------------------------------------------------------
@router.post('/api-case/plan')
async def api_case_plan(request: Request, data: ApiCaseGenReq):
    """G1：只规划 I 组 7 维度的覆盖分配，不写场景正文。"""
    uid = 0
    try:
        uid = _uid(request)
        count = max(API_CASE_MIN_COUNT, min(API_CASE_MAX_COUNT, int(data.count or 6)))
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'api_case_plan', uid):
                raise HTTPException(status_code=409, detail='已有生成任务在运行中，请稍候')
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            probe = await _api_probe(db, uid, iface, data.env_id,
                                     data.force_live, data.use_probe)
            method = (iface.get('method') or 'GET').upper()
            r = runner.run_structured(
                db, uid, biz_type='api_case_plan', prompt_code='api_case_plan',
                prompt_vars={
                    'iface_brief': probe_mod.build_iface_brief(iface),
                    'probe_text': probe.get('text') or '（未取证，仅按接口定义推断）',
                    'method': method,
                    'count': count,
                },
                list_key='plan', item_model=schema_mod.ApiCasePlanItem,
                limit=API_CASE_PLAN_MAX_CELLS * 2, model_id=data.model_id,
            )
            cells = _api_trim_plan(r['items'], count, method)
            return _ok({
                'plan': cells,
                'cells_total': len(r['items']),
                'cells_used': len(cells),
                'planned_total': sum(int(x.get('planned') or 1) for x in cells),
                'method': method,
                'probe': _api_probe_meta(probe),
                'model': r.get('model') or {},
                'elapsed_ms': r.get('elapsed_ms') or 0,
                'usage': r.get('usage') or {},
            }, '已生成 %d 格覆盖计划' % len(cells))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成覆盖计划失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'api_case_plan', uid)


def _api_probe_meta(p: Dict[str, Any]) -> Dict[str, Any]:
    """取证元信息（不含正文）：前端展示「取证来源/大小/脱敏/截断」用。"""
    return {
        'mode': p.get('mode') or 'none',
        'label': p.get('label') or '未取证',
        'size': p.get('size') or 0,
        'sent': p.get('sent') or 0,
        'masked': p.get('masked') or 0,
        'truncated': bool(p.get('truncated')),
        'stale': bool(p.get('stale')),
        'warn': p.get('warn') or '',
        'exec_id': p.get('exec_id'),
        'run_time': p.get('run_time'),
        'http_status': p.get('http_status'),
    }


# ---------------------------------------------------------------------------
# 3) G2 场景生成（内含 G1 规划与 G3 闸门补跑）：POST /ai/api-case/gen
# ---------------------------------------------------------------------------
@router.post('/api-case/gen')
async def api_case_gen(request: Request, data: ApiCaseGenReq):
    """生成可执行测试场景（只返回结果，落库由前端勾选后调 /history + /apply）。

    三段式：G1 规划 → G2 分格生成 → G3 闸门补跑（硬上限 1 轮）。
    跨批去重用**整单级**累积（`_grams_list` 不在批次间重置），否则批间重复一条都拦不住。
    """
    uid = 0
    try:
        uid = _uid(request)
        count = max(API_CASE_MIN_COUNT, min(API_CASE_MAX_COUNT, int(data.count or 6)))
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'api_case_gen', uid, str(data.interface_id)):
                raise HTTPException(status_code=409,
                                    detail='该接口已有生成任务在运行中，请稍候')
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            method = (iface.get('method') or 'GET').upper()
            is_write = method not in ('GET', 'HEAD')
            t0 = time.time()
            calls_used = 0

            probe = await _api_probe(db, uid, iface, data.env_id,
                                     data.force_live, data.use_probe)
            brief = probe_mod.build_iface_brief(iface)
            base_vars = {
                'iface_brief': brief,
                'probe_text': probe.get('text') or '（未取证，仅按接口定义推断）',
                'method': method,
            }

            # —— G1 ——
            plan_cells: List[Dict[str, Any]] = []
            use_plan = bool(data.use_plan)
            if data.plan:
                # 断点续跑：前端把上次的 plan 传回来，省一次调用
                plan_cells = _api_trim_plan(list(data.plan), count, method)
            elif use_plan:
                try:
                    pr = runner.run_structured(
                        db, uid, biz_type='api_case_plan', prompt_code='api_case_plan',
                        prompt_vars=dict(base_vars, count=count),
                        list_key='plan', item_model=schema_mod.ApiCasePlanItem,
                        limit=API_CASE_PLAN_MAX_CELLS * 2, model_id=data.model_id,
                    )
                    calls_used += 1
                    plan_cells = _api_trim_plan(list(pr['items']), count, method)
                except Exception as pe:  # noqa: BLE001
                    # 规划失败不该让整单失败：降级为单次不分格生成
                    logger.warning('api_case G1 规划失败，降级为单次生成: %s', pe)
                    plan_cells = []
                    use_plan = False

            # 跨批累计去重器（⚠️ 不能在每批开始时 new 一个）
            exact_keys: set = set()
            gram_list: List[set] = []
            scenes_all: List[Dict[str, Any]] = []
            dup_dropped = 0
            dup_samples: List[str] = []
            dropped = 0
            failed_batches = 0
            last: Dict[str, Any] = {}

            def _collect(items: List[Dict[str, Any]]) -> int:
                """把一批 items 并入结果，同时做精确 + 语义去重。返回本批丢弃条数。"""
                nonlocal dup_dropped
                lost = 0
                for it in items or []:
                    it = dict(it)
                    # ⚠️ 按 method 再过一次维度白名单：写接口在此丢掉 I06（此时才知道方法）
                    it['dims'] = coverage_mod.normalize_api_dims(it.get('dims'), method)
                    key = _api_scene_dedup_key(it)
                    if key in exact_keys:
                        lost += 1
                        if len(dup_samples) < 3:
                            dup_samples.append(it.get('scene_name') or '')
                        continue
                    g = _api_scene_grams(it)
                    if g and any(coverage_mod.is_similar(g, prev, API_CASE_DUP_THRESHOLD)
                                 for prev in gram_list):
                        lost += 1
                        if len(dup_samples) < 3:
                            dup_samples.append(it.get('scene_name') or '')
                        continue
                    exact_keys.add(key)
                    if g:
                        gram_list.append(g)
                    scenes_all.append(it)
                dup_dropped += lost
                return lost

            if use_plan and plan_cells:
                # —— G2：每格单独调一次模型。不同维度的入参天然不同，这正是分格的意义 ——
                for ci, cell in enumerate(plan_cells):
                    if len(scenes_all) >= count:
                        break
                    if ci > 0:
                        time.sleep(API_CASE_BATCH_GAP)
                    want = min(int(cell.get('planned') or 1), count - len(scenes_all))
                    try:
                        r = runner.run_structured(
                            db, uid, biz_type='api_case_gen', prompt_code='api_case_gen',
                            prompt_vars=dict(
                                base_vars, plan_text=_api_plan_text([cell]),
                                batch_cursor=ci + 1, batch_size=want),
                            list_key='scenes', item_model=schema_mod.ApiCaseSceneItem,
                            limit=want, model_id=data.model_id,
                        )
                        calls_used += 1
                        # 强制回填本格维度：模型偶尔不标 dims，丢掉这格覆盖就白跑了。
                        # ⚠️ items 是 dict（runner 已 model_dump），必须用键访问。
                        for it in r['items']:
                            if cell.get('dim'):
                                it['dims'] = coverage_mod.normalize_api_dims(
                                    list(it.get('dims') or []) + [cell['dim']], method)
                            if not (it.get('severity') or ''):
                                it['severity'] = coverage_mod.normalize_severity(
                                    cell.get('severity'))
                        dropped += int(r.get('dropped') or 0)
                        _collect(r['items'])
                        last = r
                    except Exception as be:  # noqa: BLE001
                        failed_batches += 1
                        logger.warning('api_case_gen 第 %d/%d 格失败：%s',
                                       ci + 1, len(plan_cells), be)

                # —— G3：闸门核对 → 缺口补跑（硬上限 1 轮）——
                gate_gaps: List[Dict[str, Any]] = []
                if scenes_all and len(scenes_all) < count and failed_batches == 0:
                    gate_gaps = await _api_run_gate(db, uid, plan_cells, scenes_all,
                                                   method, data.model_id)
                    calls_used += 1
                    if gate_gaps and len(gate_gaps) <= API_CASE_GATE_MAX_ROUNDS * 6:
                        topup = [schema_mod.ApiCaseGapItem(**g).to_plan_vars()
                                 for g in gate_gaps]
                        for cell in topup:
                            if len(scenes_all) >= count:
                                break
                            time.sleep(API_CASE_BATCH_GAP)
                            want = min(int(cell.get('planned') or 1),
                                       count - len(scenes_all))
                            try:
                                r2 = runner.run_structured(
                                    db, uid, biz_type='api_case_gen',
                                    prompt_code='api_case_gen',
                                    prompt_vars=dict(
                                        base_vars,
                                        plan_text=_api_plan_text([cell]),
                                        batch_cursor=0, batch_size=want),
                                    list_key='scenes',
                                    item_model=schema_mod.ApiCaseSceneItem,
                                    limit=want, model_id=data.model_id,
                                )
                                calls_used += 1
                                for it in r2['items']:
                                    if cell.get('dim'):
                                        it['dims'] = coverage_mod.normalize_api_dims(
                                            list(it.get('dims') or []) + [cell['dim']],
                                            method)
                                dropped += int(r2.get('dropped') or 0)
                                _collect(r2['items'])
                                last = r2
                            except Exception as te:  # noqa: BLE001
                                logger.warning('api_case_gate 缺口补跑失败：%s', te)

                if not scenes_all:
                    # 全部分格都失败：退回单次生成，别让用户白等一轮
                    logger.warning('接口用例分格生成全部失败，回退单次生成')
                    use_plan = False
            else:
                # 单次不分格：模型一次性给 count 条
                use_plan = False

            if not use_plan:
                time.sleep(0)
                try:
                    r = runner.run_structured(
                        db, uid, biz_type='api_case_gen', prompt_code='api_case_gen',
                        prompt_vars=dict(
                            base_vars,
                            plan_text=_api_plan_text(plan_cells)
                            if plan_cells else '（无覆盖计划，请自行判断要覆盖哪些维度）',
                            batch_cursor=1, batch_size=count),
                        list_key='scenes', item_model=schema_mod.ApiCaseSceneItem,
                        limit=count, model_id=data.model_id,
                    )
                    calls_used += 1
                    dropped += int(r.get('dropped') or 0)
                    _collect(r['items'])
                    last = r
                except Exception as se:  # noqa: BLE001
                    logger.warning('接口用例单次生成失败：%s', se)
                    if not scenes_all:
                        raise

            scenes_all = scenes_all[:count]
            elapsed = int((time.time() - t0) * 1000)
            if not scenes_all:
                raise HTTPException(status_code=422,
                                    detail='模型未产出可用场景，请稍后重试或调低目标条数')

            # —— 落地前自检：断言补齐 + 占位符 ——
            # 顺序要紧：先补断言再统计 no_assert，否则统计永远为 0、指标形同虚设
            backfilled = _api_backfill_asserts(scenes_all, probe)
            if backfilled:
                logger.info('api_case 有 %d 条场景缺断言，已补兜底 json_path：%s',
                            len(backfilled), backfilled[:5])
            # F1：从真实响应确定性提取 JSONPath 断言建议（不调模型，前端勾选后落地）
            assert_suggestions = build_assert_suggestions(probe)
            allowed_vars = _api_iface_vars(iface)
            for sc in scenes_all:
                bad = probe_mod.check_placeholder(
                    {'params': sc.get('params'), 'body': sc.get('body')}, allowed_vars)
                if bad:
                    sc['bad_vars'] = bad     # 只标红不丢弃，由用户判断
            no_assert = [sc for sc in scenes_all if not (sc.get('asserts') or [])]
            cover = coverage_mod.build_api_cover_report(scenes_all, method=method)

            msg = '已生成 %d 条可执行场景' % len(scenes_all)
            if probe.get('mode') == 'none':
                msg += '（未取证，均为推断）'
            if dup_dropped:
                msg += '，去重丢弃 %d 条' % dup_dropped
            if len(scenes_all) < count:
                msg += '；目标 %d 条，实际 %d 条' % (count, len(scenes_all))
            if failed_batches:
                msg += '（%d 格失败已跳过）' % failed_batches
            if backfilled:
                msg += '；%d 条漏写断言已补兜底断言' % len(backfilled)

            return _ok({
                'interface_id': data.interface_id,
                'interface_name': iface.get('name') or '',
                'method': method,
                'is_write_method': is_write,
                'scenes': scenes_all,
                'count': len(scenes_all),
                'target_count': count,
                'dropped': dropped,
                'dropped_dup': dup_dropped,
                'dropped_dup_samples': dup_samples,
                'dedup_threshold': API_CASE_DUP_THRESHOLD,
                'no_assert_count': len(no_assert),
                'asserts_backfilled': backfilled,
                'assert_suggestions': assert_suggestions,
                'coverage': cover,
                'plan': plan_cells if use_plan else [],
                'use_plan': bool(use_plan),
                'gate_gaps': gate_gaps,
                'batches_total': len(plan_cells) if use_plan else 1,
                'batches_failed': failed_batches,
                'calls_used': calls_used,
                'probe': _api_probe_meta(probe),
                'model': last.get('model') or {},
                'usage': last.get('usage') or {},
                'elapsed_ms': elapsed,
                'prompt_version': last.get('prompt_version'),
            }, msg)
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='生成接口用例失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'api_case_gen', uid, str(data.interface_id))


# ---------------------------------------------------------------------------
# 4) G3 闸门：POST /ai/api-case/gate
#
# 独立暴露的原因：`gen` 里已经内置了闸门补跑。本接口给「已生成的结果」单独再核一次用 ——
# 例如用户手工删掉几条后想知道还缺什么，或生成失败想看闸门怎么判。
# ---------------------------------------------------------------------------
@router.post('/api-case/gate')
async def api_case_gate(request: Request, data: ApiCaseGateReq):
    """对照覆盖计划核对已有场景，返回真实覆盖缺口（不落库、不改数据）。"""
    try:
        uid = _uid(request)
        items = data.scenes or []
        if not items:
            raise HTTPException(status_code=400, detail='请提供待检查的场景')
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            method = (iface.get('method') or 'GET').upper()
            # 没传 plan 就按已覆盖的维度现编一份，否则闸门无从对照
            plan = (_api_trim_plan([dict(x) for x in data.plan], API_CASE_MAX_COUNT, method)
                    if data.plan else _api_plan_from_scenes(items, method))
            gaps = await _api_run_gate(db, uid, plan, items, method, data.model_id)
            return _ok({
                'gaps': gaps,
                'plan': plan,
                'method': method,
                'coverage': coverage_mod.build_api_cover_report(items, method=method),
            }, '发现 %d 处覆盖缺口' % len(gaps))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='覆盖闸门执行失败：%s' % e)


def _api_plan_from_scenes(scenes: List[Dict[str, Any]], method: str) -> List[Dict[str, Any]]:
    """没有 plan 时，从已有场景反推一份「计划」——每格 planned=1。

    这样闸门只报**计划外**的空白维度，而不是把已有覆盖当成已满足（那会导致永远无缺口）。
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for sc in scenes or []:
        for d in coverage_mod.normalize_api_dims(sc.get('dims'), method):
            if d not in seen:
                seen[d] = {
                    'dim': d,
                    'dim_name': coverage_mod.API_DIM_INFO.get(d, ('', ''))[0],
                    'case_type': '', 'design_method': '', 'severity': 'S2',
                    'evidence_type': 'basis',
                    'focus': '该维度至少 1 条场景（由已有场景反推）',
                    'planned': 1, 'note': '',
                }
    return list(seen.values())


# ---------------------------------------------------------------------------
# 5) 生成记录：POST /ai/api-case/history（落库） + GET（同列表）+ GET /{id}（明细）
# ---------------------------------------------------------------------------
@router.post('/api-case/history')
def api_case_history_save(request: Request, data: ApiCaseHistorySaveReq):
    """把一次生成的产物落库为历史记录（生成成功后由前端调用；无场景则跳过）。

    失败**不影响**生成结果 —— 生成已经成功返回了，记录只是回溯用。
    """
    try:
        uid = _uid(request)
        items = data.items or []
        if not items:
            return _ok(None, '无场景，跳过历史记录')
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            method = (iface.get('method') or 'GET').upper()
            batch = _batch_id()
            dim_all = set()
            hid = _insert_id(
                db,
                'INSERT INTO ai_api_case_history '
                '(batch_id, interface_id, iface_name, iface_group, method, url, '
                ' probe_mode, probe_exec_id, probe_size, probe_sent, probe_masked, '
                ' probe_truncated, model_id, model_name, scene_count, dim_count, '
                ' dup_dropped, elapsed_ms, calls_used, attempt, create_by) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (batch, data.interface_id, (iface.get('name') or '')[:100],
                 (iface.get('group') or '')[:100], method, (iface.get('url') or '')[:500],
                 (data.probe_mode or 'none')[:16], data.probe_exec_id,
                 data.probe_size, data.probe_sent, data.probe_masked,
                 1 if data.probe_truncated else 0, data.model_id,
                 (data.model_name or '')[:100], len(items), 0, 0,
                 data.elapsed_ms, data.calls_used, data.attempt, uid),
            )
            rows = []
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                # 落库前再过一次白名单：前端可能改过 dims，也可能有历史脏数据
                dims = coverage_mod.normalize_api_dims(it.get('dims'), method)
                dim_all.update(dims)
                asserts, _ = coverage_mod.normalize_api_asserts(it.get('asserts'))
                rows.append((
                    hid, i,
                    (str(it.get('scene_name') or '')[:100]),
                    (str(it.get('case_tag') or '')[:100]),
                    ','.join(dims),
                    coverage_mod.normalize_severity(it.get('severity')),
                    (str(it.get('evidence_type') or 'assumption')[:16]),
                    (str(it.get('case_type') or '')[:32]),
                    (str(it.get('design_method') or '')[:64]),
                    json.dumps(it.get('params') or {}, ensure_ascii=False, default=str),
                    json.dumps(it.get('body'), ensure_ascii=False, default=str)
                    if it.get('body') is not None else None,
                    json.dumps(asserts, ensure_ascii=False, default=str),
                    str(it.get('pre_condition') or ''), str(it.get('steps') or ''),
                    str(it.get('expected') or ''), str(it.get('note') or ''),
                    float(it.get('confidence') or 0),
                ))
            if rows:
                db.execute_many(
                    'INSERT INTO ai_api_case_history_item '
                    '(history_id, seq, scene_name, case_tag, dims, severity, '
                    ' evidence_type, case_type, design_method, params_json, body_json, '
                    ' asserts_json, pre_condition, steps, expected, note, confidence) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
                db.execute_update(
                    'UPDATE ai_api_case_history SET dim_count = %s WHERE id = %s',
                    (len(dim_all), hid))
        return _ok({'history_id': hid, 'batch_id': batch, 'count': len(rows)},
                   '已记录生成历史')
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='记录生成历史失败：%s' % e)


def _api_item_out(r: Dict[str, Any]) -> Dict[str, Any]:
    """历史条目出参：把 *_json 列解回结构，dims 拆成数组（与生成接口口径一致）。"""
    return {
        'id': r.get('id'),
        'history_id': r.get('history_id'),
        'seq': r.get('seq'),
        'scene_name': r.get('scene_name') or '',
        'case_tag': r.get('case_tag') or '',
        'dims': _as_multi_str(r.get('dims')),
        'severity': r.get('severity') or 'S2',
        'evidence_type': r.get('evidence_type') or 'assumption',
        'case_type': r.get('case_type') or '',
        'design_method': r.get('design_method') or '',
        'params': _json_loads(r.get('params_json'), {}) or {},
        'body': _json_loads(r.get('body_json')),
        'asserts': _json_loads(r.get('asserts_json'), []) or [],
        'pre_condition': r.get('pre_condition') or '',
        'steps': r.get('steps') or '',
        'expected': r.get('expected') or '',
        'note': r.get('note') or '',
        'confidence': float(r.get('confidence') or 0),
        'applied': int(r.get('applied') or 0),
    }


@router.get('/api-case/history')
def api_case_history_list(request: Request, pageNo: int = 1, pageSize: int = 20,
                          interfaceId: Optional[int] = None):
    """生成记录列表（仅本人，按时间倒序分页）。"""
    try:
        uid = _uid(request)
        where = 'h.create_by = %s'
        args: List[Any] = [uid]
        if interfaceId:
            where += ' AND h.interface_id = %s'
            args.append(int(interfaceId))
        with get_db_handler() as db:
            total_rows = db.execute_query(
                'SELECT COUNT(*) AS n FROM ai_api_case_history h WHERE %s' % where,
                tuple(args))
            total = total_rows[0]['n'] if total_rows else 0
            pageNo = max(1, int(pageNo))
            pageSize = max(1, int(pageSize)) if pageSize and int(pageSize) > 0 else 20
            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                'SELECT h.*, '
                ' (SELECT COUNT(*) FROM ai_api_case_history_item i '
                '  WHERE i.history_id = h.id) AS item_count '
                'FROM ai_api_case_history h WHERE %s '
                'ORDER BY h.id DESC LIMIT %%s OFFSET %%s' % where,
                tuple(args + [pageSize, offset]))
            items = []
            for r in rows or []:
                d = dict(r)
                for k in ('probe_truncated', 'applied'):
                    if k in d:
                        d[k] = int(d[k] or 0)
                items.append(d)
            return _ok({'total': total, 'items': items,
                        'pageNo': pageNo, 'pageSize': pageSize}, 'ok')
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询生成历史失败：%s' % e)


@router.get('/api-case/history/{history_id}')
def api_case_history_detail(request: Request, history_id: int):
    """记录详情（含全部条目），供回看与再次落地。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT * FROM ai_api_case_history WHERE id = %s AND create_by = %s',
                (int(history_id), uid))
            if not rows:
                raise HTTPException(status_code=404, detail='生成记录不存在或无权查看')
            rows = db.execute_query(
                'SELECT * FROM ai_api_case_history_item WHERE history_id = %s ORDER BY seq, id',
                (int(history_id),))
            return _ok({'history': dict(db.execute_query(
                'SELECT * FROM ai_api_case_history WHERE id = %s', (int(history_id),))[0]),
                'items': [_api_item_out(r) for r in (rows or [])]}, 'ok')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='查询生成记录详情失败：%s' % e)


# ---------------------------------------------------------------------------
# 6) 落地：POST /ai/api-case/apply
#
# 决策 4.A：三选一 + 落地前 diff 预览 + **永不静默覆盖**。
# `dry_run=True` 只算 diff 不落库，前端拿它做二次确认。
# ---------------------------------------------------------------------------
@router.post('/api-case/apply')
def api_case_apply(request: Request, data: ApiCaseApplyReq):
    """把选中的场景写回 api_interfaces.dataset。

    mode:
      append      —— 追加到现有场景之后
      replace     —— 整体替换（会毁掉手工写的场景，务必先 dry_run 看 diff）
      fill_empty  —— 仅当现有 dataset 为空时才写入
    """
    try:
        uid = _uid(request)
        mode = (data.mode or 'append').strip().lower()
        if mode not in ('append', 'replace', 'fill_empty'):
            raise HTTPException(status_code=400, detail='落地方式非法')
        if not data.scenes:
            raise HTTPException(status_code=400, detail='请至少勾选 1 条场景再落地')

        # 入参逐条校验（宽松模型，容忍前端编辑过的内容）
        parsed: List[ApiSceneInput] = []
        bad_rows: List[Dict[str, Any]] = []
        for i, sc in enumerate(data.scenes or []):
            if not isinstance(sc, dict):
                bad_rows.append({'index': i, 'reason': '不是对象'})
                continue
            try:
                parsed.append(ApiSceneInput.model_validate(sc))
            except Exception as pe:  # noqa: BLE001
                bad_rows.append({'index': i, 'reason': str(pe)[:150]})
        if not parsed:
            raise HTTPException(status_code=400, detail='没有可落地的场景：%s' % bad_rows[:3])

        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            method = (iface.get('method') or 'GET').upper()
            # 合法变量集合：场景里的 {{var}} 必须来自接口自身
            allowed_vars = _api_iface_vars(iface)

            # 组装要写入的场景对象，并做落地前的最后一道清洗
            entries: List[Dict[str, Any]] = []
            invalid_asserts = 0
            bad_var_hits: List[str] = []
            for p in parsed:
                name = (p.scene_name or '').strip()
                if not name:
                    name = (p.case_tag or '').strip() or '未命名场景'
                entry: Dict[str, Any] = {'name': name[:100]}
                params = p.params if isinstance(p.params, dict) else {}
                if params:
                    entry['params'] = params
                if p.body is not None and p.body != {} and p.body != '':
                    entry['body'] = p.body
                asserts, dropped = coverage_mod.normalize_api_asserts(p.asserts)
                invalid_asserts += dropped
                if asserts:
                    # 场景级断言非空 → 引擎用它覆盖接口级断言（automation.py:887-889）
                    entry['asserts'] = asserts
                # ⚠️ headers **刻意不落地**：环境头存真实 X-Token，场景级只能覆盖不能删除。
                # 写了鉴权头的场景会带有效 token 通过 → 假通过（D1 决策 C）。
                badv = probe_mod.check_placeholder(
                    {'params': entry.get('params'), 'body': entry.get('body')},
                    allowed_vars)
                if badv:
                    bad_var_hits.append('%s: %s' % (name, '、'.join(badv)))
                entries.append(entry)

            # —— 读现有 dataset，判断落地形态 ——
            raw_ds = _json_loads(iface.get('dataset'))
            cur_list = raw_ds if isinstance(raw_ds, list) else []
            is_legacy_flat = bool(cur_list) and not any(
                isinstance(x, dict) and any(
                    k in x for k in ('name', 'params', 'headers', 'body', 'asserts'))
                for x in cur_list)
            cur_count = len(cur_list)

            if mode == 'fill_empty' and cur_count:
                # D5：把判定依据如实返回，让用户看见为什么没写入
                return _ok({
                    'applied': 0, 'mode': mode, 'reason': 'fill_empty_skipped',
                    'current_count': cur_count, 'is_legacy_flat': is_legacy_flat,
                    'entries': entries,
                    'tip': ('检测到该接口已有 %d 条%s数据集，fill_empty 未写入任何内容。'
                            % (cur_count,
                               '（旧版扁平数据集，仅变量替换）' if is_legacy_flat else '个场景'))
                            + ('建议改用「替换」以避免两种形态共存。'
                               if is_legacy_flat else '如需追加请改用「追加」。'),
                }, '未写入：接口已有数据集')
            if mode == 'fill_empty':
                new_list = list(entries)
            elif mode == 'replace':
                new_list = list(entries)
            else:  # append
                new_list = list(cur_list) + list(entries)

            diff = {
                'mode': mode,
                'current_count': cur_count,
                'new_count': len(new_list),
                'added': len(entries),
                'is_legacy_flat': is_legacy_flat,
                'removed_names': [str(x.get('name') or '') for x in entries]
                if mode == 'replace' else [],
                'kept_names': [str(x.get('name') or '') for x in cur_list]
                if mode == 'append' else [],
            }
            payload = json.dumps(new_list, ensure_ascii=False, default=str)

            if data.dry_run:
                return _ok({
                    'applied': 0, 'dry_run': True, 'mode': mode, 'diff': diff,
                    'entries': entries, 'invalid_asserts': invalid_asserts,
                    'bad_vars': bad_var_hits[:10],
                    'dataset_preview': payload[:4000],
                }, '试算完成，未写入')

            # —— 真写入 ——
            # F7：AI 重新生成并落地 → 场景已是最新，清除「可能过期」标记
            db.execute_update(
                'UPDATE api_interfaces SET dataset = %s, dataset_stale = 0 WHERE id = %s',
                (payload, int(data.interface_id)))
            # 标记已落地（能追溯到具体是哪批场景进了 dataset）
            if data.history_id:
                try:
                    db.execute_update(
                        'UPDATE ai_api_case_history SET applied = 1, applied_at = NOW(), '
                        'apply_mode = %s WHERE id = %s',
                        (mode, int(data.history_id)))
                    db.execute_update(
                        'UPDATE ai_api_case_history_item SET applied = 1 '
                        'WHERE history_id = %s', (int(data.history_id),))
                except Exception as me:  # noqa: BLE001
                    logger.warning('回写落地标记失败（场景已写入）: %s', me)

        msg = '已写入 %d 条场景（当前共 %d 条）' % (len(entries), len(new_list))
        if bad_var_hits:
            msg += '；%d 条含接口未定义的变量占位符，请复核' % len(bad_var_hits)
        return _ok({
            'applied': len(entries), 'mode': mode, 'diff': diff,
            'invalid_asserts': invalid_asserts, 'bad_vars': bad_var_hits[:10],
            'bad_rows': bad_rows[:10],
        }, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='落地失败：%s' % e)


@router.post('/api-case/diagnose')
def api_case_diagnose(request: Request, data: ApiCaseDiagnoseReq):
    """F5 失败自愈：针对一次失败的执行记录，结合真实响应与当前用例配置，给出修正建议。

    ⚠️ 只建议、不改写任何数据（已拍板：失败自愈只出建议，绝不自动改 dataset）。
    返回 suggestions 列表，由前端展示供人工确认。
    """
    try:
        uid = _uid(request)
        exec_id = int(data.execution_id)
        if exec_id <= 0:
            raise HTTPException(status_code=400, detail='execution_id 非法')
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'api_case_diagnose', uid, str(exec_id)):
                raise HTTPException(status_code=409, detail='已有「失败诊断」任务在运行中，请稍候')
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT * FROM api_executions WHERE id = %s', (exec_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='执行记录不存在')
            ex = rows[0]
            interface_id = ex.get('interface_id')

            # 当前用例配置（场景 + 断言），作为诊断上下文
            iface = _api_iface(db, interface_id) if interface_id else {}
            dataset = _json_loads(iface.get('dataset')) if iface else []
            if not isinstance(dataset, list):
                dataset = []
            current_config = json.dumps(dataset, ensure_ascii=False, default=str)[:3000]

            # 失败断言明细
            ad = _json_loads(ex.get('assert_detail'))
            failed_lines = []
            if isinstance(ad, dict):
                for d in (ad.get('details') or []):
                    if isinstance(d, dict) and not d.get('passed'):
                        failed_lines.append(
                            '- [%s] 期望=%s 实际=%s：%s'
                            % (str(d.get('type')), schema_mod._as_str(d.get('expected')),
                               schema_mod._as_str(d.get('actual')),
                               schema_mod._as_str(d.get('detail'))))
            failed_asserts = '\n'.join(failed_lines) or '（无逐条断言明细，请结合响应与错误信息判断）'

            # 响应 / 请求快照
            resp = _json_loads(ex.get('response_dump'))
            resp_body = (schema_mod._as_str(resp.get('body'))
                         if isinstance(resp, dict) else schema_mod._as_str(ex.get('response_dump')))[:4000]
            req = _json_loads(ex.get('request_dump'))
            req_text = (json.dumps(req, ensure_ascii=False, default=str)[:2000]
                        if isinstance(req, dict) and req else '（无）')

            conclusion = (ex.get('assertion_result') or
                          ('error' if ex.get('success') != 1 else 'pass'))
            http_status = ex.get('http_status')
            error_msg = schema_mod._as_str(ex.get('error_msg')) or '（无）'

            method = (iface.get('method') or 'GET').upper() if iface else 'GET'
            iface_name = iface.get('name') or ('接口#%s' % interface_id)
            iface_brief = '接口：%s\n方法：%s\n接口ID：%s' % (iface_name, method, interface_id)

            prompt = prompt_mod.get_prompt(db, 'api_case_diagnose', uid)
            schema_text = prompt_mod.schema_text(prompt.get('output_schema') or {})
            r = runner.run_structured(
                db, uid, biz_type='api_case_diagnose', prompt_code='api_case_diagnose',
                prompt_vars={
                    'interface_brief': iface_brief,
                    'request_text': req_text,
                    'response_text': resp_body,
                    'conclusion': conclusion,
                    'http_status': http_status if http_status is not None else '（无）',
                    'error_msg': error_msg,
                    'failed_asserts': failed_asserts,
                    'current_config': current_config or '（该接口暂无场景用例）',
                    'schema_text': schema_text,
                },
                list_key='suggestions', item_model=schema_mod.ApiCaseDiagnoseItem,
                limit=10, model_id=data.model_id,
            )
            items = r.get('items') or []
            return _ok({
                'execution_id': exec_id,
                'interface_id': interface_id,
                'suggestions': items,
                'model': r.get('model') or {},
                'elapsed_ms': r.get('elapsed_ms') or 0,
                'usage': r.get('usage') or {},
                'summary': '共 %d 条修正建议' % len(items),
            }, '诊断完成，共 %d 条建议' % len(items))
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='失败诊断失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'api_case_diagnose', uid, str(data.execution_id))


def _api_existing_text(scenes: List[Dict[str, Any]], method: str) -> str:
    """把已有场景压成紧凑文本，塞进精调提示词作去重参考（不泄露完整断言体，避免 prompt 过长）。"""
    if not scenes:
        return '（暂无已有场景）'
    lines = []
    for i, sc in enumerate(scenes[:40], 1):
        name = (sc.get('scene_name') or sc.get('case_tag') or '未命名')[:40]
        dims = '/'.join(sc.get('dims') or []) or '—'
        lines.append('%d. %s [%s]' % (i, name, dims))
    if len(scenes) > 40:
        lines.append('……（共 %d 条，仅列前 40）' % len(scenes))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 6) F6 对话式精调：POST /ai/api-case/refine
#
# 在已有场景基础上按自然语言指令增量补充，避免整轮重跑。
# ---------------------------------------------------------------------------
@router.post('/api-case/refine')
async def api_case_refine(request: Request, data: ApiCaseRefineReq):
    """F6 对话式精调：在已有场景基础上按自然语言指令增量补充场景（不整轮重跑）。"""
    uid = 0
    try:
        uid = _uid(request)
        instruction = (data.instruction or '').strip()
        if not instruction:
            raise HTTPException(status_code=400, detail='请填写精调指令，如「再生成 3 条边界值」')
        with get_db_handler() as _lk:
            if not inflight_mark(_lk, 'api_case_refine', uid, str(data.interface_id)):
                raise HTTPException(status_code=409, detail='该接口已有生成任务在运行中，请稍候')
        with get_db_handler() as db:
            iface = _api_iface(db, data.interface_id)
            method = (iface.get('method') or 'GET').upper()
            is_write = method not in ('GET', 'HEAD')
            t0 = time.time()
            probe = await _api_probe(db, uid, iface, data.env_id, data.force_live, data.use_probe)
            brief = probe_mod.build_iface_brief(iface)

            # 把已有场景灌进去重器，保证新生成的不会和已有重复
            exact_keys: set = set()
            gram_list: List[set] = []
            for sc in (data.existing_scenes or []):
                exact_keys.add(_api_scene_dedup_key(sc))
                g = _api_scene_grams(sc)
                if g:
                    gram_list.append(g)

            try:
                r = runner.run_structured(
                    db, uid, biz_type='api_case_refine', prompt_code='api_case_refine',
                    prompt_vars={
                        'iface_brief': brief,
                        'probe_text': probe.get('text') or '（未取证，仅按接口定义推断）',
                        'instruction': instruction,
                        'existing_text': _api_existing_text(data.existing_scenes, method),
                        'method': method,
                    },
                    list_key='scenes', item_model=schema_mod.ApiCaseSceneItem,
                    limit=API_CASE_MAX_COUNT, model_id=data.model_id,
                )
            except Exception as re_:  # noqa: BLE001
                logger.warning('api_case_refine 生成失败：%s', re_)
                raise HTTPException(status_code=422, detail='模型未产出可用场景：%s' % re_)

            scenes_all: List[Dict[str, Any]] = []
            dup_dropped = 0
            for it in (r.get('items') or []):
                it = dict(it)
                it['dims'] = coverage_mod.normalize_api_dims(it.get('dims'), method)
                key = _api_scene_dedup_key(it)
                if key in exact_keys:
                    dup_dropped += 1
                    continue
                g = _api_scene_grams(it)
                if g and any(coverage_mod.is_similar(g, prev, API_CASE_DUP_THRESHOLD)
                             for prev in gram_list):
                    dup_dropped += 1
                    continue
                exact_keys.add(key)
                if g:
                    gram_list.append(g)
                scenes_all.append(it)

            if not scenes_all:
                raise HTTPException(status_code=422,
                                    detail='未产出与已有场景不重复的新场景，请调整指令后重试')

            # 落地前自检：断言补齐 + 占位符校验
            backfilled = _api_backfill_asserts(scenes_all, probe)
            allowed_vars = _api_iface_vars(iface)
            for sc in scenes_all:
                bad = probe_mod.check_placeholder(
                    {'params': sc.get('params'), 'body': sc.get('body')}, allowed_vars)
                if bad:
                    sc['bad_vars'] = bad
            no_assert = [sc for sc in scenes_all if not (sc.get('asserts') or [])]
            cover = coverage_mod.build_api_cover_report(scenes_all, method=method)

            elapsed = int((time.time() - t0) * 1000)
            msg = '已增量生成 %d 条新场景' % len(scenes_all)
            if dup_dropped:
                msg += '，去重丢弃 %d 条' % dup_dropped
            if backfilled:
                msg += '；%d 条漏写断言已补兜底断言' % len(backfilled)

            return _ok({
                'interface_id': data.interface_id,
                'interface_name': iface.get('name') or '',
                'method': method,
                'is_write_method': is_write,
                'scenes': scenes_all,
                'count': len(scenes_all),
                'dropped_dup': dup_dropped,
                'no_assert_count': len(no_assert),
                'asserts_backfilled': backfilled,
                'coverage': cover,
                'probe': _api_probe_meta(probe),
                'model': r.get('model') or {},
                'usage': r.get('usage') or {},
                'elapsed_ms': elapsed,
                'prompt_version': r.get('prompt_version'),
            }, msg)
    except HTTPException:
        raise
    except AIError as e:
        _raise_ai(e)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='对话式精调失败：%s' % e)
    finally:
        with get_db_handler() as _lk:
            inflight_clear(_lk, 'api_case_refine', uid, str(data.interface_id))


# ---------------------------------------------------------------------------
# 7) F8 导出 / 导入（Excel/JSON）：POST /ai/api-case/import、GET /ai/api-case/export
# ---------------------------------------------------------------------------
def _api_scene_to_entry(p: 'ApiSceneInput', method: str, allowed_vars: set) -> Dict[str, Any]:
    """把校验过的场景转成 dataset 落库条目（与 apply 同口径：headers 刻意不落地）。"""
    name = (p.scene_name or '').strip() or (p.case_tag or '').strip() or '未命名场景'
    entry: Dict[str, Any] = {'name': name[:100]}
    params = p.params if isinstance(p.params, dict) else {}
    if params:
        entry['params'] = params
    if p.body is not None and p.body != {} and p.body != '':
        entry['body'] = p.body
    asserts, _ = coverage_mod.normalize_api_asserts(p.asserts)
    if asserts:
        entry['asserts'] = asserts
    return entry


def _cell(row, col):
    if col not in row:
        return ''
    v = row[col]
    if v is None:
        return ''
    try:
        if isinstance(v, float) and math.isnan(v):
            return ''
    except Exception:
        pass
    return str(v)


def _coerce_json_cell(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    s = str(v).strip()
    if not s or s.lower() in ('nan', 'none', 'null'):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _parse_import_json(data: Any, fallback_id: Optional[int]) -> List[Tuple[int, List[Any]]]:
    """把导入的 JSON 解析为 [(interface_id, scenes), ...]。

    支持四种形态：
      - [{interface_id, scenes}, ...]   导出批量格式（每项自带 interface_id）
      - [scene, ...]                    纯场景数组，需外部指定 interface_id
      - {interface_id, scenes}           单接口
      - {interfaces:[{interface_id, scenes}, ...]}  批量（跨接口回填）
    """
    groups: List[Tuple[int, List[Any]]] = []
    if isinstance(data, list):
        # 导出批量格式：列表里每个元素是带 interface_id 的接口对象
        if data and isinstance(data[0], dict) and 'interface_id' in data[0]:
            for it in data:
                iid = it.get('interface_id', fallback_id)
                if iid is not None:
                    groups.append((int(iid), it.get('scenes') or []))
        elif fallback_id is not None:
            groups.append((fallback_id, data))
    elif isinstance(data, dict):
        if 'interfaces' in data:
            for it in (data.get('interfaces') or []):
                iid = it.get('interface_id', fallback_id)
                if iid is not None:
                    groups.append((int(iid), it.get('scenes') or []))
        elif 'scenes' in data:
            iid = data.get('interface_id', fallback_id)
            if iid is not None:
                groups.append((int(iid), data.get('scenes') or []))
    return groups


def _parse_import_excel(contents: bytes, fallback_id: Optional[int] = None) -> List[Tuple[int, List[Dict[str, Any]]]]:
    """Excel 每行一个场景，JSON 形态列（参数/请求体/断言）用字符串存，这里回解。

    按「接口ID」列分组（导出 Excel 已带该列）；若该列为空则回落到 fallback_id。
    返回 [(interface_id, scenes), ...]，便于跨接口回填。
    """
    na_values = ['', 'NA', 'N/A', 'NaN', 'null', 'NULL']
    raw = pd.read_excel(io.BytesIO(contents), sheet_name=None, na_values=na_values,
                        keep_default_na=True)
    frames = (list(raw.values()) if isinstance(raw, dict) else [raw])
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for df in frames:
        if getattr(df, 'empty', False) or len(df.columns) == 0:
            continue
        for _, row in df.iterrows():
            name = _cell(row, '场景名')
            if not name:
                continue
            iid_raw = _cell(row, '接口ID')
            try:
                iid = int(float(iid_raw)) if iid_raw not in ('', None) else None
            except Exception:
                iid = None
            if iid is None:
                iid = fallback_id
            if iid is None:
                continue
            params = _coerce_json_cell(_cell(row, '参数(JSON)'))
            body = _coerce_json_cell(_cell(row, '请求体(JSON)'))
            asserts = _coerce_json_cell(_cell(row, '断言(JSON)'))
            dims_raw = _cell(row, '维度')
            dims = ([x.strip() for x in re.split(r'[/,，\s]+', str(dims_raw)) if x.strip()]
                    if dims_raw else [])
            grouped.setdefault(iid, []).append({
                'scene_name': name,
                'dims': dims,
                'severity': (_cell(row, '严重级') or 'S2'),
                'design_method': _cell(row, '设计方法') or '',
                'params': params if isinstance(params, dict) else {},
                'body': body if body is not None else {},
                'asserts': asserts if isinstance(asserts, list) else [],
                'evidence_type': _cell(row, '证据') or 'assumption',
                'steps': _cell(row, '步骤') or '',
                'expected': _cell(row, '预期') or '',
            })
    return [(k, v) for k, v in grouped.items()]


def _api_case_export_excel(payload_list: List[Dict[str, Any]]) -> Response:
    wb = openpyxl.Workbook()
    for item in payload_list:
        title = ('%s(#%s)' % ((item.get('interface_name') or '接口')[:18], item['interface_id']))[:31]
        ws = wb.create_sheet(title=title)
        ws.append(['接口ID', '接口名', '方法', 'URL', '场景名', '维度', '严重级', '设计方法',
                   '参数(JSON)', '请求体(JSON)', '断言(JSON)', '证据', '步骤', '预期'])
        for sc in (item.get('scenes') or []):
            ws.append([
                item['interface_id'], item.get('interface_name') or '', item.get('method') or '',
                item.get('url') or '', sc.get('scene_name') or sc.get('case_tag') or '',
                '/'.join(sc.get('dims') or []), sc.get('severity') or '',
                sc.get('design_method') or '', _dump_json(sc.get('params') or {}),
                _dump_json(sc.get('body') if sc.get('body') is not None else {}),
                _dump_json(sc.get('asserts') or []), sc.get('evidence_type') or '',
                sc.get('steps') or '', sc.get('expected') or '',
            ])
    default_sheet = 'Sheet'
    if default_sheet in wb.sheetnames and len(wb.sheetnames) > 1:
        del wb[default_sheet]
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=api_case_export.xlsx'},
    )


@router.get('/api-case/export')
async def api_case_export(request: Request, interface_id: str = '', format: str = 'json', all: int = 0):
    """F8 导出接口测试场景：JSON 或 Excel，供评审 / 备份。

    interface_id 支持逗号分隔批量；all=1 导出全部；format=json|excel。
    """
    try:
        _uid(request)
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, name, method, url, dataset FROM api_interfaces')
            if not all:
                idset = set(int(x) for x in (interface_id or '').split(',') if x.strip())
                rows = [r for r in rows if r['id'] in idset]
            if not rows:
                raise HTTPException(status_code=400, detail='没有可导出的接口')
            payload_list = []
            for r in rows:
                ds = _json_loads(r.get('dataset'))
                payload_list.append({
                    'interface_id': r['id'],
                    'interface_name': r.get('name') or '',
                    'method': (r.get('method') or 'GET').upper(),
                    'url': r.get('url') or '',
                    'scenes': ds if isinstance(ds, list) else [],
                })
            if format == 'excel':
                return _api_case_export_excel(payload_list)
            single = (not all) and len(payload_list) == 1
            body = json.dumps(payload_list[0] if single else payload_list,
                              ensure_ascii=False, default=str, indent=2)
            return Response(
                content=body.encode('utf-8'), media_type='application/json',
                headers={'Content-Disposition': 'attachment; filename=api_case_export.json'})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='导出失败：%s' % e)


@router.post('/ai/api-case/import')
async def api_case_import(
    request: Request,
    file: UploadFile = File(None),
    content: str = Form(None),
    interface_ids: List[int] = Form([]),
    mode: str = Form('append'),
):
    """F8 导入接口测试场景：解析 JSON / Excel，校验后合并进 dataset（append 追加 / replace 替换）。

    - 文件优先；无文件则取粘贴的 content（JSON）。
    - interface_ids 用于限定落地范围（只写选中接口），未指定则按文件内 interface_id 落地。
    - JSON 支持导出批量格式 / 单接口 / 纯场景数组；Excel 按「接口ID」列分组。
    """
    try:
        if mode not in ('append', 'replace'):
            raise HTTPException(status_code=400, detail='落地方式非法')
        contents: Optional[bytes] = None
        fn = ''
        if file is not None and getattr(file, 'filename', ''):
            fn = (file.filename or '').lower()
            contents = await file.read()
        is_json = fn.endswith('.json') or (contents is None and bool(content and content.strip()))
        if contents is None:
            if not content or not content.strip():
                raise HTTPException(status_code=400, detail='请提供文件或粘贴 JSON 内容')
            contents = content.encode('utf-8')

        fallback_id = interface_ids[0] if len(interface_ids) == 1 else None
        groups: List[Tuple[int, List[Any]]] = []
        if is_json:
            try:
                data = json.loads(contents.decode('utf-8'))
            except Exception:
                raise HTTPException(status_code=400, detail='JSON 解析失败')
            groups = _parse_import_json(data, fallback_id)
        else:
            groups = _parse_import_excel(contents, fallback_id)
        if not groups:
            raise HTTPException(status_code=400, detail='没有可导入的场景（接口可能不匹配或文件为空）')

        # 若前端指定了接口范围，则只落地范围内的分组（避免误写未选接口）
        idset = set(int(x) for x in interface_ids) if interface_ids else None

        total = 0
        imported_ifaces: List[int] = []
        invalid = 0
        with get_db_handler() as db:
            for (iid, scenes) in groups:
                iid = int(iid)
                if idset is not None and iid not in idset:
                    continue
                iface = db.execute_query(
                    'SELECT id, method, dataset FROM api_interfaces WHERE id=%s', (iid,))
                if not iface:
                    continue
                method = (iface[0].get('method') or 'GET').upper()
                allowed_vars = _api_iface_vars(iface[0])
                entries = []
                for sc in (scenes or []):
                    try:
                        p = ApiSceneInput.model_validate(sc)
                    except Exception:
                        invalid += 1
                        continue
                    entries.append(_api_scene_to_entry(p, method, allowed_vars))
                if not entries:
                    continue
                raw_ds = _json_loads(iface[0].get('dataset'))
                cur = raw_ds if isinstance(raw_ds, list) else []
                new_list = entries if mode == 'replace' else (list(cur) + list(entries))
                payload = json.dumps(new_list, ensure_ascii=False, default=str)
                # F7：导入即刷新场景 → 清除「可能过期」标记
                db.execute_update(
                    'UPDATE api_interfaces SET dataset=%s, dataset_stale=0, update_time=NOW() WHERE id=%s',
                    (payload, iid))
                total += len(entries)
                imported_ifaces.append(iid)
        if not imported_ifaces:
            raise HTTPException(status_code=400,
                                detail='没有成功导入任何场景（接口可能不存在 / 不在选中范围 / 场景全非法）')
        skipped = max((len(interface_ids) - len(imported_ifaces)), 0) if interface_ids else 0
        return _ok({
            'imported': len(imported_ifaces),
            'skipped': skipped,
            'scenes_added': total,
            'invalid': invalid,
            'mode': mode,
        }, '已导入 %d 个接口共 %d 条场景（跳过 %d 条非法）' % (len(imported_ifaces), total, invalid))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail='导入失败：%s' % e)
