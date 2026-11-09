"""模型注册表：可见性过滤、默认模型优先级链、密钥解密、连通性与能力探测。"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import client as llm_client
from . import crypto
from .errors import AIConfigError, AIUpstreamError

logger = logging.getLogger(__name__)

SELECT_FIELDS = (
    'id, name, provider, base_url, api_key_enc, model_name, model_type, owner_user_id, scope, '
    'capabilities, temperature, max_tokens, timeout, priority, is_default, enabled, '
    'last_test_time, last_test_ok, last_test_msg, remark, cost_per_1k, create_by, create_time, update_time'
)


def parse_capabilities(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def to_public(row: Dict[str, Any]) -> Dict[str, Any]:
    """给前端的形态：密钥只回后 4 位，绝不返回密文或明文"""
    out = {k: v for k, v in row.items() if k != 'api_key_enc'}
    try:
        plain = crypto.decrypt(row.get('api_key_enc'))
        out['api_key_mask'] = crypto.mask_key(plain)
        out['has_key'] = bool(plain)
    except Exception:
        out['api_key_mask'] = ''
        out['has_key'] = False
    out['capabilities'] = parse_capabilities(row.get('capabilities'))
    out['temperature'] = float(row['temperature']) if row.get('temperature') is not None else None
    out['cost_per_1k'] = float(row['cost_per_1k']) if row.get('cost_per_1k') is not None else 0.0
    return out


def to_runtime(row: Dict[str, Any]) -> Dict[str, Any]:
    """给调用层用的形态：含解密后的 api_key"""
    return {
        'id': row.get('id'),
        'name': row.get('name'),
        'base_url': row.get('base_url'),
        'api_key': crypto.decrypt(row.get('api_key_enc')),
        'model_name': row.get('model_name'),
        'model_type': (row.get('model_type') or 'chat').lower(),
        'temperature': float(row['temperature']) if row.get('temperature') is not None else 0.3,
        'max_tokens': row.get('max_tokens'),
        # 默认 300s：推理模型（reasoner）单次生成可达 60~170s，120s 必然被腰斩成「502/超时」
        'timeout': int(row.get('timeout') or 300),
        'capabilities': parse_capabilities(row.get('capabilities')),
        'owner_user_id': row.get('owner_user_id') or 0,
    }


def list_models(db, uid: int, owner: Optional[int] = None) -> List[Dict[str, Any]]:
    """可见模型：全局（scope=0）+ 自己名下的。owner 参数用于只看某个归属。"""
    sql = 'SELECT %s FROM ai_models WHERE is_del = 0 AND (scope = 0 OR owner_user_id = %%s)' % SELECT_FIELDS
    args: List[Any] = [uid or 0]
    if owner is not None:
        sql += ' AND owner_user_id = %s'
        args.append(owner)
    sql += ' ORDER BY owner_user_id DESC, is_default DESC, priority ASC, id ASC'
    return db.execute_query(sql, tuple(args)) or []


def get_row(db, model_id: int) -> Optional[Dict[str, Any]]:
    rows = db.execute_query(
        'SELECT %s FROM ai_models WHERE id = %%s AND is_del = 0' % SELECT_FIELDS, (model_id,)
    )
    return rows[0] if rows else None


def can_use(row: Dict[str, Any], uid: int) -> bool:
    """全局模型人人可用；个人模型仅本人可用（权限在 SQL 之后再兜一层）"""
    owner = row.get('owner_user_id') or 0
    return owner == 0 or owner == (uid or 0)


def _sort_key(row: Dict[str, Any]):
    # 先默认模型，再按 priority 升序
    return (0 if row.get('is_default') else 1, row.get('priority') or 10, row.get('id'))


def resolve_chain(db, uid: int, model_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """返回有序候选模型链：[首选, 备用...]

    优先级：我的默认模型 → 全局默认模型 → 我的其余模型(priority) → 全局其余模型(priority)
    个人模型链失败后回落全局默认，不会串到别人的 key。
    """
    rows = [r for r in list_models(db, uid) if r.get('enabled')]
    uid = uid or 0
    mine = [r for r in rows if (r.get('owner_user_id') or 0) == uid and uid]
    glob = [r for r in rows if (r.get('owner_user_id') or 0) == 0]
    chain = sorted(mine, key=_sort_key) + sorted(glob, key=_sort_key)

    if model_id:
        picked = next((r for r in rows if int(r['id']) == int(model_id)), None)
        if picked is None:
            raise AIConfigError('指定的模型不存在、已停用或无权使用')
        chain = [picked] + [r for r in chain if int(r['id']) != int(picked['id'])]

    if not chain:
        raise AIConfigError('未配置可用模型，请先到「AI工作台 → 模型中心」添加模型')
    return chain


def probe(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """连通性 + 能力探测：发一次极小请求

    返回 {ok, elapsed_ms, http_code, msg, json_object}
    json_object 为 None 表示未探测（reasoner 类型或连通失败）
    """
    out: Dict[str, Any] = {'ok': False, 'elapsed_ms': 0, 'http_code': None,
                           'msg': '', 'json_object': None}
    want_json = runtime.get('model_type') != 'reasoner'
    # 注意：DeepSeek 等厂商要求「prompt 里必须出现 json 字样」才允许 response_format=json_object，
    # 若用 "ping" 去探，会把本来支持 JSON 模式的模型误判成不支持（踩过）。
    ping = '请以 json 格式回复：{"pong": true}'
    try:
        content, usage, status, elapsed, _fr = llm_client.chat_completion(
            runtime, [{'role': 'user', 'content': ping}], json_mode=want_json, max_tokens=32)
        out.update(ok=True, elapsed_ms=elapsed, http_code=status,
                   msg='连通正常，耗时 %dms' % elapsed, json_object=True if want_json else None)
        return out
    except AIUpstreamError as e:
        first_err = str(e)

    if want_json:
        # 可能是该模型不支持 json_object：去掉该参数再试一次，用来区分「不通」和「不支持 JSON 模式」
        try:
            content, usage, status, elapsed, _fr = llm_client.chat_completion(
                runtime, [{'role': 'user', 'content': ping}], json_mode=False, max_tokens=32)
            out.update(ok=True, elapsed_ms=elapsed, http_code=status, json_object=False,
                       msg='连通正常，但不支持 JSON 模式（耗时 %dms）' % elapsed)
            return out
        except AIUpstreamError as e2:
            out['msg'] = str(e2)
            return out

    out['msg'] = first_err
    return out


def test_and_save(db, model_id: int) -> Dict[str, Any]:
    """探测并把结果写回 last_test_* / capabilities"""
    row = get_row(db, model_id)
    if not row:
        raise AIConfigError('模型不存在或已删除')
    runtime = to_runtime(row)
    result = probe(runtime)

    caps = parse_capabilities(row.get('capabilities'))
    if result.get('json_object') is not None:
        caps['json_object'] = bool(result['json_object'])

    db.execute_update(
        'UPDATE ai_models SET last_test_time = NOW(), last_test_ok = %s, last_test_msg = %s, '
        'capabilities = %s WHERE id = %s',
        (1 if result['ok'] else 0, (result.get('msg') or '')[:500],
         json.dumps(caps, ensure_ascii=False), model_id),
    )
    result['capabilities'] = caps
    return result


def set_default(db, model_id: int, uid: int) -> None:
    """设为默认：同一归属范围内只保留一个默认"""
    row = get_row(db, model_id)
    if not row:
        raise AIConfigError('模型不存在或已删除')
    owner = row.get('owner_user_id') or 0
    if owner != 0 and owner != (uid or 0):
        raise AIConfigError('无权修改他人的个人模型')
    db.execute_update('UPDATE ai_models SET is_default = 0 WHERE is_del = 0 AND owner_user_id = %s', (owner,))
    db.execute_update('UPDATE ai_models SET is_default = 1 WHERE id = %s', (model_id,))
