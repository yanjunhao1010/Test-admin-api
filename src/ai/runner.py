"""AI 执行编排。

一次调用的完整链路：
    配额校验 → 选模型（我的默认 → 全局默认 → 备用链）→ 渲染模板 → 脱敏
    → 调用 → 结构化校验 → 失败则把「校验错误」回灌给模型重试（最多 2 次）→ 写 ai_logs

设计要点（对应方案里的底座改造）：
1. 结构化校验失败不再「HTTP 200 + 静默」，而是重试后明确报错；
2. 逐条校验：10 条里坏 2 条就丢 2 条、保留 8 条，并把丢弃原因回给前端；
3. 主模型超时/429/5xx 时自动切备用模型（不串到别人的 key）；
4. 每次调用都写 ai_logs（含上游失败），支撑用量统计与成本归因；
5. 输出长度取「模型级 max_tokens → 全局 per_call_max_tokens」的优先级，全局只是兜底。
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from . import client as llm_client
from . import mask as mask_mod
from . import models as model_reg
from . import prompts as prompt_mod
from . import schema as schema_mod
from . import settings as settings_mod
from .errors import AIConfigError, AIQuotaError, AISchemaError, AIUpstreamError

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # 首次 + 最多 2 次回灌重试


def _chain_candidates(chain: List[Dict[str, Any]], structured: Optional[Dict[str, Any]],
                      pinned_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """给结构化任务挑候选链：reasoner 排到最后，且用户显式指定的模型永远留在首位。

    实测踩过：reasoner 模型单次生成可跑满 timeout（300s）才超时，
    而它常常只是链尾的「兜底」—— 前面的 chat 模型偶发网络抖动后，
    请求被这个 300s 的黑洞拖住，整批直接废掉。
    所以：chat 类优先全部试完，实在不行才动用 reasoner；
    但 pinned（用户手动选了它）必须原样置顶，不能被重排。

    pinned_id 有值 -> 只把该模型提到首位，其余按 chat-first 规则排。
    """
    if not structured:
        return chain
    head: List[Dict[str, Any]] = []
    rest = list(chain)
    if pinned_id:
        pinned = next((r for r in rest if int(r['id']) == int(pinned_id)), None)
        if pinned is not None:
            head = [pinned]
            rest = [r for r in rest if int(r['id']) != int(pinned_id)]
    chat = [r for r in rest if (r.get('model_type') or 'chat').lower() != 'reasoner']
    reasoner = [r for r in rest if (r.get('model_type') or 'chat').lower() == 'reasoner']
    if not chat:
        # 全是 reasoner：只能全用（并由提示词强约束兜底），不能返回空
        return head + reasoner
    return head + chat + reasoner


def check_quota(db, uid: int) -> None:
    """当日调用次数上限（按用户计）。上限 <= 0 表示不限制。"""
    limit = settings_mod.get_int(db, 'daily_call_limit', 200)
    if limit <= 0:
        return
    rows = db.execute_query(
        'SELECT COUNT(*) AS n FROM ai_logs WHERE run_by = %s AND create_time >= CURDATE()', (uid,))
    used = rows[0]['n'] if rows else 0
    if used >= limit:
        raise AIQuotaError('今日 AI 调用次数已达上限（%d 次），请明天再试或联系管理员调整配额' % limit)


def log_call(db, **kw) -> None:
    """写调用日志；日志失败绝不能影响业务"""
    try:
        db.execute_update(
            'INSERT INTO ai_logs (model_id, model_name, owner_user_id, run_by, biz_type, prompt_code, '
            'prompt_version, '
            'prompt_tokens, completion_tokens, total_tokens, elapsed_ms, attempt, schema_valid, '
            'http_code, success, error_msg) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (kw.get('model_id'), (kw.get('model_name') or '')[:100], kw.get('owner_user_id') or 0,
             kw.get('run_by'), (kw.get('biz_type') or '')[:32], (kw.get('prompt_code') or '')[:64],
             kw.get('prompt_version'),
             kw.get('prompt_tokens'), kw.get('completion_tokens'), kw.get('total_tokens'),
             kw.get('elapsed_ms'), kw.get('attempt') or 1, kw.get('schema_valid'),
             kw.get('http_code'), 1 if kw.get('success') else 0, (kw.get('error_msg') or '')[:1000]),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning('写 ai_logs 失败（不影响业务）: %s', e)


def _prepare_messages(db, prompt: Dict[str, Any], prompt_vars: Dict[str, Any],
                      structured: bool) -> Tuple[str, str, int]:
    """渲染 system / user 消息并做外发脱敏，返回 (system, user, 脱敏命中数)"""
    values = dict(prompt_vars or {})
    values['schema_text'] = prompt_mod.schema_text(prompt.get('output_schema'))

    user_msg = prompt_mod.render(prompt.get('user'), values)
    system_msg = prompt.get('system') or ''

    # 模板里没引用 {{schema_text}} 时兜底附上，保证模型知道要输出什么结构
    if structured and values['schema_text'] and '{{schema_text}}' not in (prompt.get('user') or ''):
        user_msg += '\n\n请按下面结构输出 JSON：\n' + values['schema_text']

    masked = 0
    if settings_mod.get_bool(db, 'ai_mask_enabled', True):
        rules = settings_mod.get(db, 'ai_mask_rules')
        user_msg, n1 = mask_mod.mask_text(user_msg, rules)
        masked += n1
        if system_msg:
            system_msg, n2 = mask_mod.mask_text(system_msg, rules)
            masked += n2
    if masked:
        # 必须显式告诉模型占位符的含义：否则它会把 {{PHONE_1}} 当成「模板变量没替换」，
        # 进而误判成「数据依赖缺失」这种假根因（实测踩过）。
        user_msg += ('\n\n【系统说明】文中形如 {{PHONE_1}} / {{EMAIL_1}} / {{BANKCARD_1}} 的片段，'
                     '是系统做的敏感信息脱敏占位符，**不是**未替换的模板变量。请按「此处有真实敏感信息」'
                     '理解，不要把它当作缺陷、数据依赖缺失或变量替换失败。')
    return system_msg, user_msg, masked


def _user_content(user_msg: str, images: Optional[List[str]]) -> Any:
    """构造 user 消息内容：有图片时返回多模态 content 数组，否则返回纯文本字符串。"""
    if images:
        parts: List[Dict[str, Any]] = [{'type': 'text', 'text': user_msg}]
        for url in images:
            if url:
                parts.append({'type': 'image_url', 'image_url': {'url': url}})
        return parts
    return user_msg


def _base_messages(system_msg: str, user_msg: str, images: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    if system_msg:
        msgs.append({'role': 'system', 'content': system_msg})
    msgs.append({'role': 'user', 'content': _user_content(user_msg, images)})
    return msgs


def _run_once(db, uid, biz_type, prompt_code, prompt, runtime, system_msg, user_msg,
              structured: Optional[Dict[str, Any]], max_tokens: int, masked: int,
              refill: bool, images: Optional[List[str]] = None) -> Dict[str, Any]:
    """在单个模型上跑完整重试逻辑"""
    base = _base_messages(system_msg, user_msg, images)
    pv = prompt.get('version') if isinstance(prompt, dict) else None
    attempt = 0
    total_elapsed = 0
    total_usage: Dict[str, Any] = {}

    # 模型级 max_tokens 优先于全局上限：推理模型输出长、生成慢，必须在模型中心单独压小，
    # 否则只改全局会把所有模型的输出长度一起砍掉。留空（NULL/0）才回落全局值。
    effective_max_tokens = int(runtime.get('max_tokens') or max_tokens or 4096)

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        messages = base
        call_started = time.time()
        try:
            content, usage, http_status, elapsed, finish_reason = llm_client.chat_completion(
                runtime, messages, json_mode=bool(structured), max_tokens=effective_max_tokens)
        except AIUpstreamError as e:
            # 上游失败（超时 / 5xx / 网络）必须留痕，否则 ai_logs 里只有成功调用，
            # 排查「某模型一直 502 / 超时」时会完全看不到证据（踩过：日志全是 DeepSeek）。
            total_elapsed += int((time.time() - call_started) * 1000)
            log_call(db, model_id=runtime['id'], model_name=runtime.get('name'), run_by=uid,
                     owner_user_id=runtime.get('owner_user_id'), biz_type=biz_type,
                     prompt_code=prompt_code, prompt_version=pv, http_code=None, elapsed_ms=total_elapsed,
                     attempt=attempt, schema_valid=None, success=False,
                     error_msg='上游失败：%s' % e, **total_usage)
            raise
        total_elapsed += elapsed
        for k, v in (usage or {}).items():
            if isinstance(v, int):
                total_usage[k] = (total_usage.get(k) or 0) + v

        if not structured:
            log_call(db, model_id=runtime['id'], model_name=runtime.get('name'), run_by=uid,
                     owner_user_id=runtime.get('owner_user_id'), biz_type=biz_type,
                     prompt_code=prompt_code, prompt_version=pv, http_status=http_status, elapsed_ms=total_elapsed,
                     attempt=attempt, schema_valid=None, success=True, **total_usage)
            return {
                'data': {'text': content},
                'dropped': [],
                'model': {'id': runtime['id'], 'name': runtime.get('name'),
                          'model_name': runtime.get('model_name'), 'model_type': runtime.get('model_type')},
                'usage': total_usage,
                'elapsed_ms': total_elapsed,
                'attempt': attempt,
                'http_status': http_status,
                'masked_count': masked,
                'raw': content,
                'prompt_code': prompt_code,
                'prompt_version': prompt.get('version'),
                'prompt_scope': prompt.get('scope'),
            }

        list_key = structured.get('list_key') or 'items'
        item_model = structured.get('item_model')
        limit = structured.get('limit')

        parsed = schema_mod.extract_json_block(content)
        if parsed is None:
            # finish_reason='length' = 输出被 max_tokens 撞顶，JSON 尾部被砍掉。
            # 这种情况下回灌重试毫无意义 —— 上限没变，下一次必然在同一位置再被砍，
            # 只会白白多烧两次 token 和两次耗时（实测：req_extract 三次撞 8192/16384/24576）。
            # 直接抛出可读错误，让调用方去分批或调上限。
            truncated = (finish_reason == 'length')
            reason = ('输出被 max_tokens(%d) 截断，JSON 不完整。请调大模型中心的 max_tokens，'
                      '或把任务拆成多批（每次少产出几条）' % effective_max_tokens) if truncated \
                else '输出不是合法 JSON'
            log_call(db, model_id=runtime['id'], model_name=runtime.get('name'), run_by=uid,
                     owner_user_id=runtime.get('owner_user_id'), biz_type=biz_type,
                     prompt_code=prompt_code, prompt_version=pv, http_status=http_status, elapsed_ms=elapsed,
                     attempt=attempt, schema_valid=0, success=False, error_msg=reason,
                     **total_usage)
            if truncated:
                raise AISchemaError(reason)
            base = _base_messages(system_msg, user_msg, images) + [
                {'role': 'assistant', 'content': (content or '')[:2000]},
                {'role': 'user', 'content': '你上一次的输出不是合法 JSON。请只输出一个合法 JSON 对象，'
                                            '不要任何解释文字，不要用 markdown 代码块包裹。'},
            ]
            continue

        raw_items = None
        if isinstance(parsed, dict):
            raw_items = parsed.get(list_key)
            if raw_items is None:
                for v in parsed.values():
                    if isinstance(v, list):
                        raw_items = v
                        break
        elif isinstance(parsed, list):
            raw_items = parsed

        valid, dropped = schema_mod.validate_items(raw_items, item_model, limit)
        if not valid:
            reasons = '；'.join(d.get('reason', '') for d in dropped[:3]) or '没有返回任何条目'
            log_call(db, model_id=runtime['id'], model_name=runtime.get('name'), run_by=uid,
                     owner_user_id=runtime.get('owner_user_id'), biz_type=biz_type,
                     prompt_code=prompt_code, prompt_version=pv, http_status=http_status, elapsed_ms=elapsed,
                     attempt=attempt, schema_valid=0, success=False,
                     error_msg='无可用条目：%s' % reasons, **total_usage)
            base = _base_messages(system_msg, user_msg, images) + [
                {'role': 'assistant', 'content': (content or '')[:2000]},
                {'role': 'user', 'content': '上一次返回的 JSON 里没有可用条目（原因：%s）。'
                                            '请严格按给定结构重新输出，确保每条都包含 '
                                            'steps（操作步骤）与 expected_result（预期结果）。' % reasons},
            ]
            continue

        log_call(db, model_id=runtime['id'], model_name=runtime.get('name'), run_by=uid,
                 owner_user_id=runtime.get('owner_user_id'), biz_type=biz_type,
                 prompt_code=prompt_code, prompt_version=pv, http_status=http_status, elapsed_ms=total_elapsed,
                 attempt=attempt, schema_valid=1, success=True, **total_usage)

        data = dict(parsed) if isinstance(parsed, dict) else {}
        data[list_key] = valid
        return {
            'data': data,
            'items': valid,
            'dropped': dropped,
            'model': {'id': runtime['id'], 'name': runtime.get('name'),
                      'model_name': runtime.get('model_name'), 'model_type': runtime.get('model_type')},
            'usage': total_usage,
            'elapsed_ms': total_elapsed,
            'attempt': attempt,
            'http_status': http_status,
            'masked_count': masked,
            'raw': content,
            'prompt_code': prompt_code,
            'prompt_version': prompt.get('version'),
            'prompt_scope': prompt.get('scope'),
        }

    raise AISchemaError(
        '模型连续 %d 次未能返回可用的结构化结果，建议改用 chat 类型模型，或把需求描述写得更具体' % MAX_ATTEMPTS)


def run(db, uid: int, biz_type: str, prompt_code: str,
        prompt_vars: Optional[Dict[str, Any]] = None,
        model_id: Optional[int] = None,
        structured: Optional[Dict[str, Any]] = None,
        prompt_override: Optional[Dict[str, Any]] = None,
        images: Optional[List[str]] = None) -> Dict[str, Any]:
    """执行一次 AI 任务。

    structured=None  -> 文本模式（如 Markdown 报告）
    structured={...} -> 结构化模式，需给 list_key（如 'cases'）与 item_model（Pydantic 类）
    prompt_override  -> 不读 ai_prompts 表，直接用传入的 {system,user,output_schema,...}（调试台用）
    images           -> 多模态图片（data URL 列表）；存在时 user 消息会带 image_url 内容块
    """
    check_quota(db, uid)
    prompt = prompt_override if prompt_override else prompt_mod.get_prompt(db, prompt_code, uid)
    max_tokens = settings_mod.get_int(db, 'per_call_max_tokens', 4096)
    system_msg, user_msg, masked = _prepare_messages(db, prompt, prompt_vars or {}, bool(structured))

    chain = _chain_candidates(model_reg.resolve_chain(db, uid, model_id), structured, pinned_id=model_id)
    last_err: Optional[Exception] = None

    for row in chain:
        try:
            runtime = model_reg.to_runtime(row)
        except Exception as e:  # noqa: BLE001  密钥解密失败等，试下一个
            last_err = e
            logger.warning('模型 %s 不可用：%s', row.get('name'), e)
            continue
        try:
            return _run_once(db, uid, biz_type, prompt_code, prompt, runtime,
                             system_msg, user_msg, structured, max_tokens, masked, False, images)
        except (AIUpstreamError, AISchemaError) as e:
            last_err = e
            logger.warning('模型 %s 执行失败，尝试备用模型：%s', row.get('name'), e)
            continue

    if last_err:
        raise last_err
    raise AIConfigError('未配置可用模型，请先到「AI工作台 → 模型中心」添加模型')


def run_structured(db, uid: int, biz_type: str, prompt_code: str, prompt_vars: Dict[str, Any],
                   list_key: str, item_model, limit: Optional[int] = None,
                   model_id: Optional[int] = None,
                   prompt_override: Optional[Dict[str, Any]] = None,
                   images: Optional[List[str]] = None) -> Dict[str, Any]:
    return run(db, uid, biz_type, prompt_code, prompt_vars, model_id,
               structured={'list_key': list_key, 'item_model': item_model, 'limit': limit},
               prompt_override=prompt_override, images=images)


def run_text(db, uid: int, biz_type: str, prompt_code: str, prompt_vars: Dict[str, Any],
             model_id: Optional[int] = None,
             prompt_override: Optional[Dict[str, Any]] = None,
             images: Optional[List[str]] = None) -> Dict[str, Any]:
    return run(db, uid, biz_type, prompt_code, prompt_vars, model_id, structured=None,
               prompt_override=prompt_override, images=images)
