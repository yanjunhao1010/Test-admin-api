# -*- coding: utf-8 -*-
"""接口用例 AI 生成 —— 取证与响应骨架化。

为什么必须有这一步（方案第一章结论 3）：
`api_interfaces` 的字段填充率实测 `body` 12.4%、`headers` 0%、`remark` 23.7%。
只喂静态接口定义给模型，它只能靠接口名和 URL 猜 —— 生成质量会明显塌一档。
所以「**先跑一次拿到真实响应，再交给模型反推场景与断言**」是本模块存在的唯一理由。

三件事，按顺序：
1. `probe_source()`    分级取证：GET/HEAD 真跑；写接口读最近一条成功执行记录
2. `skeletonize()`     响应骨架化：保留 `$.data.list[0]` 这类结构 + 首元素样例
3. `pack_probe()`      打包成送模型的文本：脱敏 + 4KB 硬截断

⚠️ 为什么不自己发 HTTP 请求：`automation._execute_interface` 已经把「依赖变量缺失
就不发请求」「环境头合并」「失败重试」「断言」全做对了。**重复实现一遍必然出现行为
不一致**，而且取证用的响应与执行时看到的响应不同源，模型反推的断言会对不上。
本模块只做「决定用哪份响应」+「把它收拾干净」，不发请求。

⚠️ 为什么要分级取证（决策 2.A）：实测 194 个接口里 64 个是 POST，直接对生产环境试跑
会产生脏数据/重复记录。但**全用历史记录也不行** —— 91 个接口（46.9%）从未执行过，
一条历史响应都没有。所以按方法分级：读接口默认真跑，写接口默认读历史，用户可强制。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from . import mask as mask_mod

logger = logging.getLogger(__name__)

#: 送模型时的响应体硬上限。实测 api_executions.response_dump 的 P90 高达 22.7KB、
#: 最大 57KB、37% 超 3KB，而 `ai_settings.per_call_max_tokens` 只有 4096 —— 不截断必炸。
PROBE_MAX_BYTES = 4000

#: 骨架化时单个值的最大字符数。结构信息比具体值重要得多，值只要能看出「类型 + 量级」。
PROBE_VALUE_CHARS = 40

#: 骨架化时数组最多保留几个元素（其余用 "…共N项" 概括），防止大数组撑爆
PROBE_ARRAY_KEEP = 2

#: 骨架化时对象最多保留几个键
PROBE_OBJECT_KEYS = 24

#: 单个响应体的原始字节上限：超过这个长度直接判定「不值得解析」，只送状态码与长度提示
PROBE_RAW_LIMIT = 200000

#: 写方法集合：这些方法的取证默认不发真实请求（决策 2.A）
WRITE_METHODS = ('POST', 'PUT', 'DELETE', 'PATCH')

#: 取证方式三档
PROBE_MODE_LIVE = 'live'          # 本次真实试跑拿到了响应
PROBE_MODE_HISTORY = 'history'    # 复用最近一次执行记录的响应
PROBE_MODE_NONE = 'none'          # 无任何响应，仅静态推断


# ---------------------------------------------------------------------------
# 1. 取证
# ---------------------------------------------------------------------------
def is_write_method(method: Any) -> bool:
    return str(method or 'GET').strip().upper() in WRITE_METHODS


async def probe_source(db, iface: Dict[str, Any], env_id: Optional[int] = None,
                       force_live: bool = False,
                       live_runner=None) -> Dict[str, Any]:
    """决定用哪份响应作为取证来源。**本函数本身不发请求**，只做选择与读取。

    返回 {mode, body_text, exec_id, http_status, elapsed_ms, run_time, raw_size,
          stale, warn}

    - `live_runner` 是**异步**可调用对象 `async fn() -> dict`，由调用方（router）注入，
      内部 await `automation._execute_interface`。这样本模块保持「只管收拾、不管发包」，
      也不必 import automation（避免循环依赖与副作用）。
    - `stale` / `warn`：接口定义比响应快照新时置 True（D2）。
      只警告不阻断 —— 但场景可能对不上，用户必须看见。
    """
    method = str(iface.get('method') or 'GET').strip().upper()
    out: Dict[str, Any] = {
        'mode': PROBE_MODE_NONE, 'body_text': '', 'exec_id': None,
        'http_status': None, 'elapsed_ms': None, 'run_time': None,
        'raw_size': 0, 'stale': False, 'warn': '',
    }

    # —— 分级：写接口默认不真跑 ——
    want_live = force_live or not is_write_method(method)

    if want_live and live_runner is not None:
        try:
            res = await live_runner()
        except HTTPException:
            raise                      # 400 类错误（如未选环境）应该直接暴露给用户
        except Exception as e:  # noqa: BLE001
            # 取证失败不阻断生成：降级到历史记录，全标 assumption
            logger.warning('api_probe 真实试跑失败（降级为历史取证）: %s', e)
            res = {}
        body = _extract_body(res or {})
        if body:
            out.update({
                'mode': PROBE_MODE_LIVE,
                'body_text': body[:PROBE_RAW_LIMIT],
                'http_status': res.get('http_status'),
                'elapsed_ms': res.get('elapsed_ms'),
                'raw_size': len(body),
            })
            return out
        if res:
            # 请求发出去了但没响应体（网络异常/超时）：记录原因，别让用户以为是"没数据"
            out['warn'] = str(res.get('error_msg') or '试跑未拿到响应体')

    # —— 历史取证：取该接口最近一条成功记录 ——
    try:
        rows = db.execute_query(
            'SELECT id, http_status, response_dump, run_time FROM api_executions '
            'WHERE interface_id = %s AND success = 1 AND response_dump IS NOT NULL '
            'AND response_dump <> %s ORDER BY id DESC LIMIT 1',
            (int(iface['id']), ''))
    except Exception as e:  # noqa: BLE001
        logger.warning('api_probe 读历史执行记录失败: %s', e)
        rows = []

    if rows:
        r = rows[0]
        body = _extract_body({'response_dump': _json_loads(r.get('response_dump'))})
        if body:
            out.update({
                'mode': PROBE_MODE_HISTORY,
                'body_text': body[:PROBE_RAW_LIMIT],
                'exec_id': r.get('id'),
                'http_status': r.get('http_status'),
                'run_time': r.get('run_time'),
                'raw_size': len(body),
            })
            if is_write_method(method) and not force_live:
                out['warn'] = ('写接口默认不发真实请求，本用例依据的是历史响应；'
                               '如需现取请勾选「强制试跑取样」')
            # D2：接口定义比响应快照新 → 场景可能对不上
            upd = iface.get('update_time') or iface.get('create_time')
            rt = r.get('run_time')
            if upd and rt and str(upd) > str(rt):
                out['stale'] = True
                out['warn'] = (out['warn'] + '；' if out['warn'] else '') + \
                    '接口定义比响应快照更新，用例可能对不上'
            return out

    if is_write_method(method) and not force_live and not out['warn']:
        out['warn'] = '该接口没有历史执行记录，且写接口默认不试跑，本次仅按接口定义推断'
    elif not out['warn']:
        out['warn'] = '该接口没有可用的响应记录，本次仅按接口定义推断'
    return out


def _extract_body(res: Dict[str, Any]) -> str:
    """从执行结果里挖出响应体文本。三种可能的落点，按优先级试。"""
    if not res:
        return ''
    dump = _json_loads(res.get('response_dump'))
    if isinstance(dump, dict):
        b = dump.get('body')
        if isinstance(b, str) and b.strip():
            return b
        if b is not None and not isinstance(b, str):
            return json.dumps(b, ensure_ascii=False, default=str)
    snip = res.get('response_snippet')
    if isinstance(snip, str) and snip.strip():
        return snip
    return ''


def _json_loads(raw: Any) -> Any:
    if raw is None or raw == '':
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2. 骨架化
# ---------------------------------------------------------------------------
def skeletonize(value: Any, depth: int = 0) -> Any:
    """把响应体压成「结构骨架 + 首元素样例」。

    实测 P90 响应 22.7KB，直接送模型既超token 又信息密度极低（99% 是重复的列表项）。
    保留的只有三样东西：
      - 对象/数组的**层级结构**（模型要靠它反推 JSONPath）
      - 每个键的**第一个**数组元素样例（模型靠它推断字段名与类型）
      - 值截到 40 字符
    """
    if depth > 6:
        return '…(层级过深)'
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= PROBE_OBJECT_KEYS:
                out['…'] = '共%d个键（已省略其余）' % len(value)
                break
            out[str(k)] = skeletonize(v, depth + 1)
        return out
    if isinstance(value, list):
        if not value:
            return []
        keep = [skeletonize(x, depth + 1) for x in value[:PROBE_ARRAY_KEEP]]
        if len(value) > PROBE_ARRAY_KEEP:
            keep.append('…共%d项' % len(value))
        return keep
    if isinstance(value, str):
        s = value.strip()
        if len(s) > PROBE_VALUE_CHARS:
            return s[:PROBE_VALUE_CHARS] + '…(共%d字符)' % len(s)
        return s
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:PROBE_VALUE_CHARS]


# ---------------------------------------------------------------------------
# 3. 打包送模型
# ---------------------------------------------------------------------------
def pack_probe(source: Dict[str, Any], iface: Dict[str, Any],
               rules_json: Optional[str] = None) -> Dict[str, Any]:
    """把取证结果收拾成可以直接拼进提示词的文本块。

    返回 {text, mode, size, sent, masked, truncated, http_status, exec_id,
          run_time, stale, warn, label}

    ⚠️ 顺序不能颠倒：**先脱敏再截断**。反过来的话，截断可能把一个敏感值切成两半
    （比如手机号只剩前 7 位），正则就匹配不上了 —— 脱敏会静默失效。
    """
    raw = str(source.get('body_text') or '')
    mode = source.get('mode') or PROBE_MODE_NONE

    # 脱敏：对整个响应文本跑一遍（mask_text 会处理 dict 序列化后的所有值）
    if raw:
        masked_text, masked_n = mask_mod.mask_text(raw, rules_json)
    else:
        masked_text, masked_n = '', 0
    raw_size = int(source.get('raw_size') or len(raw))

    truncated = False
    payload = masked_text
    if len(payload) > PROBE_MAX_BYTES:
        payload = payload[:PROBE_MAX_BYTES] + '\n…（响应过长已截断，仅供参考前半部分结构）'
        truncated = True

    # 结构化骨架：JSON 能解析时额外给一份带层级的骨架，帮模型写 JSONPath 断言
    skeleton_text = ''
    parsed = _json_loads(masked_text)
    if parsed is not None:
        try:
            sk = json.dumps(skeletonize(parsed), ensure_ascii=False, default=str)
            if len(sk) > PROBE_MAX_BYTES:
                sk = sk[:PROBE_MAX_BYTES] + '…(骨架已截断)'
            skeleton_text = sk
        except Exception as e:  # noqa: BLE001
            logger.warning('骨架化失败（忽略）: %s', e)

    label = {
        PROBE_MODE_LIVE: '本次真实试跑',
        PROBE_MODE_HISTORY: '历史执行记录',
        PROBE_MODE_NONE: '无响应（仅按接口定义推断）',
    }.get(mode, '未知')

    lines: List[str] = []
    lines.append('【取证来源】%s' % label)
    if source.get('http_status') is not None:
        lines.append('【HTTP 状态码】%s' % source.get('http_status'))
    if source.get('run_time'):
        lines.append('【响应时间】%s' % source.get('run_time'))
    lines.append('【响应原始大小】%d 字节' % raw_size)
    if mode == PROBE_MODE_NONE:
        lines.append('【重要】本次没有任何真实响应可参考，你只能依据接口定义与路径语义推断，'
                     '因此所有条目的 evidence_type 必须标 assumption，note 里写明推断依据。')
    elif mode == PROBE_MODE_HISTORY:
        lines.append('【重要】依据的是历史响应快照，接口定义可能已变更；'
                     'note 里需写明「依据为历史响应，接口定义可能已变更」。')
    if skeleton_text:
        lines.append('【响应结构骨架（已脱敏，用于反推 JSONPath 断言）】\n%s' % skeleton_text)
    if payload:
        lines.append('【响应正文（已脱敏%s）】\n%s' % (
            '并截断' if truncated else '', payload))
    if source.get('warn'):
        lines.append('【提示】%s' % source['warn'])

    text = '\n'.join(lines)
    return {
        'text': text,
        'mode': mode,
        'label': label,
        'size': raw_size,
        'sent': len(text.encode('utf-8')),
        'masked': masked_n,
        'truncated': truncated,
        'http_status': source.get('http_status'),
        'exec_id': source.get('exec_id'),
        'run_time': source.get('run_time'),
        'elapsed_ms': source.get('elapsed_ms'),
        'stale': bool(source.get('stale')),
        'warn': source.get('warn') or '',
        # 原始响应体（仅用于后端本地提取断言建议，不随结果下发前端；
        # 前端收到的是 _api_probe_meta 子集，不含此字段）
        'body_text': raw,
    }


def build_iface_brief(iface: Dict[str, Any]) -> str:
    """把接口定义压成一段紧凑文本（进 api_case_* 提示词的 `iface_brief`）。

    ⚠️ `headers` **整段不入模**：`api_interfaces.headers` 虽实测 0% 填充，但环境头里存着
    真实 X-Token；若将来有人往接口级填了 token，进了提示词就是密钥外发。
    这里只保留「有哪些头键」不带值，供模型知道要带哪些头。
    """
    method = str(iface.get('method') or 'GET').upper()
    lines = [
        '【接口名称】%s' % (iface.get('name') or ''),
        '【请求方法】%s' % method,
        '【接口地址】%s' % (iface.get('url') or ''),
        '【接口分组】%s' % (iface.get('group') or ''),
    ]
    if iface.get('remark'):
        lines.append('【备注】%s' % iface['remark'])
    for label, key in (('【查询参数】', 'params'), ('【请求体】', 'body')):
        raw = _json_loads(iface.get(key))
        if raw not in (None, {}, []):
            lines.append('%s%s' % (label, json.dumps(raw, ensure_ascii=False, default=str)[:800]))
    hdr = _json_loads(iface.get('headers'))
    if isinstance(hdr, dict) and hdr:
        lines.append('【请求头键】%s（值由环境统一注入，不要写进场景）' % '、'.join(hdr.keys()))
    asserts = _json_loads(iface.get('asserts'))
    if isinstance(asserts, list) and asserts:
        lines.append('【现有接口级断言】%s'
                     % json.dumps(asserts, ensure_ascii=False, default=str)[:400])
    elif iface.get('assert_type'):
        lines.append('【现有接口级断言】%s = %s'
                     % (iface.get('assert_type'), iface.get('expected_value') or ''))
    ext = _json_loads(iface.get('extracts'))
    if isinstance(ext, list) and ext:
        lines.append('【变量提取规则】%s'
                     % json.dumps(ext, ensure_ascii=False, default=str)[:300])
    ds = _json_loads(iface.get('dataset'))
    if isinstance(ds, list) and ds:
        names = [str(x.get('name')) for x in ds
                 if isinstance(x, dict) and x.get('name')]
        lines.append('【已有数据集场景】%d 条%s'
                     % (len(ds), ('：' + '、'.join(names)) if names else ''))

    # 路径依赖：URL 里的 {{var}} 说明该接口依赖前置接口提供变量
    phs = re.findall(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}', str(iface.get('url') or ''))
    phs += re.findall(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}',
                      json.dumps(_json_loads(iface.get('params')) or {}, ensure_ascii=False))
    phs = sorted(set(phs) - {'base_url'})
    if phs:
        lines.append('【依赖变量】%s（这些变量需由前置接口提供，场景里不要自己造值）'
                     % '、'.join(phs))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 落库前的场景校验（apply 阶段用）
# ---------------------------------------------------------------------------
_VAR_RE = re.compile(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}')


def check_placeholder(value: Any, allowed: set) -> List[str]:
    """场景 params/body 里出现的 {{var}} 是否都在接口自身的变量集合内。

    模型很可能把「自造变量」写进来（例如 `{{token}}` 而接口根本没有该引用）。
    引擎渲染时找不到就发字面量或直接判依赖缺失，用户很难看出是 AI 编的。
    命中即返回违规变量名列表（**只警告不阻断**，前端标红让人工决定）。

    ⚠️ `allowed` 应由调用方用 `_api_iface_vars`（已剔除 `base_url`）构造。
    本函数**刻意不做 base_url 特判** —— 它只是个纯校验器，
    引擎对 `{{base_url}}` 有独立的剥离逻辑（automation.py:632），不该在这里拦。
    """
    try:
        blob = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return []
    return sorted({m for m in _VAR_RE.findall(blob) if m not in allowed})