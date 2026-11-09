import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import List, Optional

import httpx
import openpyxl
import pandas as pd
try:
    import certifi

    _CA_BUNDLE = certifi.where()  # 使用 certifi 自带的 Mozilla CA 包，兼容 Windows 缺系统根证书
except Exception:
    _CA_BUNDLE = None
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from src.comment import MySqlconection, yaml_data
from src.routers.auth import parse_token
# 通知中心：定时任务完成触发点（仅追加调用，不阻断主业务）
from src.routers.notifications import notify
from src.routers.wechat_notify import notify_wechat, get_wechat_cfg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/automation',
    tags=['automation'],
)

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


def _current_uid(request: Request) -> int:
    """从 accessToken 解析当前用户 id，失败返回 0"""
    token = request.headers.get('accessToken')
    if not token:
        return 0
    try:
        return parse_token(token) or 0
    except Exception as e:
        logger.warning(f'解析当前用户失败: {e}')
        return 0


def _safe_json(value):
    """DB 中可能是 JSON 字符串，尝试解析回对象；失败原样返回"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


_TRUNC_FLAG = '\n...[内容过长已按字节截断]'


def _truncate_bytes(text: str, limit: int) -> str:
    """按 UTF-8 **字节**截断，绝不切坏多字节字符，且保证结果**含标记也不超过** limit。

    为什么不能用 s[:limit]：
    MySQL 的 TEXT 上限是 65535 **字节**，不是字符。中文响应体一个汉字 3 字节，
    s[:50000] 对纯中文就是 15 万字节 → 1406 Data too long。
    现有最大记录已 57257 字节（接口 89），离上限只剩 8KB。

    注意：标记本身也要占预算，所以先给标记留出字节再截正文——
    否则会出现「截到正好 limit，加上标记又超了」的二次溢出。

    errors='ignore' 会丢掉末尾不完整的半个字符，这是有意为之：
    总比抛异常导致整次执行 500 好。
    """
    if not text:
        return text
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    flag = _TRUNC_FLAG.encode('utf-8')
    budget = limit - len(flag)
    if budget <= 0:
        # limit 比标记还小（不该发生），至少保证结果不超 limit
        return _TRUNC_FLAG.encode('utf-8')[:limit].decode('utf-8', errors='ignore')
    kept = raw[:budget].decode('utf-8', errors='ignore')
    return (kept + _TRUNC_FLAG)


# TEXT 列上限 65535 字节，扣掉 JSON 结构、字段名、截断标记本身，留足余量。
# 60000 是实测安全线（现网最大 57257 字节）。
_DUMP_MAX_BYTES = 60000


def _dump_json(value) -> str:
    """序列化后按字节兜底截断，**并保证结果仍是合法 JSON**。

    这是所有 dump 类字段（request_dump / response_dump / assert_detail /
    extracted_vars）落库的唯一收口点，在这里兜底能一次保护全部字段，
    避免某个字段超长就把整条执行记录写失败。

    为什么不能直接对序列化结果截字符串：
    那会把 JSON 从中间切断，落库后 `json.loads` 抛 JSONDecodeError，
    执行详情页读这条记录时直接崩。所以这里做两层：
      1) 先判断超限，超了就把结构降级成 `{"_truncated": true, ...}` 这种
         仍然合法的短JSON；
      2) 顺带把大字符串字段（响应体等）的长度信息一并带出来，方便排查。
    """
    if value is None:
        return ''
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    if len(text.encode('utf-8')) <= _DUMP_MAX_BYTES:
        return text
    # 超限：不能裸截字符串（会产出非法 JSON），改为输出合法但已标记的摘要
    summary = {
        '_truncated': True,
        '_reason': '内容超过 TEXT 列字节上限，已省略正文',
        '_raw_bytes': len(text.encode('utf-8')),
        '_limit_bytes': _DUMP_MAX_BYTES,
        '_preview': _truncate_bytes(text, 8000),
    }
    return json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------
class InterfaceBase(BaseModel):
    name: str
    method: str = 'GET'
    url: str
    headers: Optional[object] = None   # 接受 dict 或 JSON 字符串（前端 textarea 文本提交）
    params: Optional[object] = None    # 接受 dict 或 JSON 字符串
    body: Optional[object] = None
    assert_type: str = 'status_code'      # status_code=HTTP状态码 | contains=包含子串 | body_status=响应体status字段 | json_path=JSONPath断言(期望格式 path=>值) | regex=正则匹配 | header=响应头断言(格式 Header=>值) | response_time=耗时阈值ms(<=通过) | custom=自定义表达式(结果为真即通过)
    expected_value: str = ''              # 期望值；custom 时填自定义表达式，如 body.status == 200 and body.data.total > 0
    enabled: int = 1
    verify_ssl: int = 1                       # 是否校验SSL证书(1校验0跳过,内网/自签证书端点可关)
    extracts: Optional[list] = []          # 响应变量提取规则，用于接口间上下文传递
    # —— 多断言：断言规则数组，每条 {assert_type, expected_value, enabled}；为空则回退单条 assert_type/expected_value —— #
    asserts: Optional[list] = None
    timeout: int = 0                         # 请求超时秒数(0 或空则使用默认 10s)
    retry: int = 0                           # 失败重试次数(仅网络/超时异常重试，断言失败不重试)
    dataset: Optional[list] = None           # 数据驱动参数(JSON数组)：每行一个数据对象，按行替换 {{key}} 多遍运行
    group: str = ''                           # 接口分组（按模块/业务线归类，便于列表筛选与聚合）
    remark: Optional[str] = ''                # 备注（接口说明、注意事项、负责人等，选填）


class InterfaceCreate(InterfaceBase):
    pass


class InterfaceUpdate(InterfaceBase):
    id: int


class ScheduleBase(BaseModel):
    name: str
    interface_ids: List[int] = Field(default_factory=list)
    cron_expr: str = ''
    enabled: int = 1
    env_id: Optional[int] = None          # 绑定的环境ID，执行时该计划全部接口使用此环境的 base_url/headers
    on_failure: int = 1                   # 前序接口失败策略(1=继续后续 / 2=中止后续)
    # 前置接口依赖声明(JSON数组): [{"interface_id": 12, "required_vars": ["token"]}]
    # 执行本计划前先跑这些接口，把变量写进上下文供正式接口用 {{key}} 引用；
    # 前置接口取不到 required_vars 时判失败，依赖它的下游接口会被跳过并在报告标注。
    prepend_refs: Optional[list] = None


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleUpdate(ScheduleBase):
    id: int


def _clean_prepend_refs(refs) -> list:
    """清洗前置接口声明：只保留有合法interface_id 的项，变量名去空白去重。

    存库统一为 [{"interface_id": int, "required_vars": [str]}]，
    避免前端传脏数据（如字符串 id、空对象）导致执行期解析失败。
    """
    out = []
    if not isinstance(refs, list):
        return out
    seen = set()
    for r in refs:
        if not isinstance(r, dict):
            continue
        try:
            iid = int(r.get('interface_id'))
        except Exception:
            continue
        if iid in seen:
            continue
        seen.add(iid)
        names = []
        for v in (r.get('required_vars') or []):
            s = str(v).strip() if v is not None else ''
            if s and s not in names:
                names.append(s)
        out.append({'interface_id': iid, 'required_vars': names})
    return out


# ---------------------------------------------------------------------------
# 执行引擎（httpx 异步调用 + 断言）
# ---------------------------------------------------------------------------
def _split_assert(exp: str):
    """把 'path=>value' 拆成 (path, value)，用于 json_path / header 断言。"""
    if '=>' in exp:
        a, b = exp.split('=>', 1)
        return a.strip(), b.strip()
    return exp.strip(), ''


def _vals_equal(a, b) -> bool:
    """数值优先比较，否则按字符串比较。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return float(a) == float(b)
    except Exception:
        return str(a) == str(b)


def _eval_jsonpath_assert(actual, exp_val):
    """json_path 断言的期望值判定：支持语义算子，其余按字面相等。

    expected_value 形如 'path=>算子/期望值'。算子分四类：
      1) 空值类：非空 / 为空 / 存在 / 不存在
      2) 类型类（模型常生成自然语言描述）：数组/对象/字符串/数字/整数/布尔
      3) 取值集合：'0或1' / '成功|失败'（命中任一即通过）
      4) 数值比较：'>=0' '<=100' '>0' '<10' '!=0'
      5) 否定：'非200'（实际值 != 200，兼容数字/字符串）
    未命中任何算子时回落 _vals_equal 字面相等（兼容现有数字/字符串断言）。
    历史 bug：引擎从未实现这些算子，把 '数组'/'0或1'/'非空' 等当字面量去比，
    导致大量生成侧断言（含自动补齐的）一律失败。
    """
    op = (exp_val or '').strip()
    # 1) 空值类
    if op == '非空':
        return actual is not None and not (
            isinstance(actual, (str, list, dict)) and len(actual) == 0)
    if op == '为空':
        return actual is None or (
            isinstance(actual, (str, list, dict)) and len(actual) == 0)
    if op == '存在':
        return actual is not None
    if op == '不存在':
        return actual is None
    # 2) 类型类
    if op in ('数组', 'array', 'list'):
        return isinstance(actual, list)
    if op in ('对象', 'object', '字典', 'dict', 'json'):
        return isinstance(actual, dict)
    if op in ('字符串', '文本', 'string', 'str'):
        return isinstance(actual, str)
    if op in ('数字', '数值', 'number'):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if op in ('整数', 'int'):
        return isinstance(actual, int) and not isinstance(actual, bool)
    if op in ('布尔', 'bool', 'boolean'):
        return isinstance(actual, bool)
    # 3) 取值集合：A或B / A|B / A或者B（命中任一即通过）
    if '或' in op or '|' in op:
        cands = [c.strip() for c in re.split(r'或者|或|\|', op) if c.strip()]
        if cands:
            return any(_vals_equal(actual, c) for c in cands)
    # 3.5) 否定：非XXX → 实际值 != XXX（复用 _vals_equal，兼容数字/字符串）
    #      例：'非200' 当实际为 400 时通过；实际为 200/\"200\" 时失败。
    if op.startswith('非'):
        return not _vals_equal(actual, op[1:])
    # 4) 数值比较：>= <= != > < 开头且后面是数字
    m = re.match(r'^(>=|<=|!=|>|<)\s*(-?\d+(?:\.\d+)?)$', op)
    if m:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        sym, num = m.group(1), float(m.group(2))
        return {'>=': actual >= num, '<=': actual <= num,
                '>': actual > num, '<': actual < num,
                '!=': actual != num}[sym]
    # 回落：字面相等
    return _vals_equal(actual, exp_val)


def _jsonpath(obj, path: str):
    """轻量 JSONPath：支持 $ 前缀、点路径、数组下标 [n]，如 $.data.list[0].id。"""
    p = path[1:] if path.startswith('$') else path
    cur = obj
    for tok in re.findall(r'\[(\d+)\]|([^.\[\]]+)', p):
        idx, name = tok
        if idx != '':
            if not isinstance(cur, list):
                return None
            try:
                cur = cur[int(idx)]
            except Exception:
                return None
        else:
            name = name.strip()
            if name == '':
                continue
            if not isinstance(cur, dict) or name not in cur:
                return None
            cur = cur[name]
    return cur


class _AttrDict(dict):
    """响应体包装：既支持 body['data'] 下标取值，又支持 body.data.total 点号取值。
    字段不存在时抛 AttributeError（自定义表达式会捕获并给出明确提示）。
    """
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f'响应体中不存在字段 {name!r}')


def _wrap_attr(obj):
    """递归包装：dict → _AttrDict，list 内元素同样处理，便于自定义表达式点号取值。"""
    if isinstance(obj, dict):
        return _AttrDict({k: _wrap_attr(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap_attr(v) for v in obj]
    return obj


def _assert_one(at: str, exp: str, http_status: Optional[int], text: str, elapsed_ms=None, resp=None) -> dict:
    """单条断言明细：{ passed, type, expected, actual, detail }。由 _assert_all 调用。
    支持：status_code / contains / body_status / json_path / regex / header / response_time / custom
    """
    if at == 'contains':
        passed = exp != '' and exp in text
        return {
            'passed': passed, 'type': 'contains', 'expected': exp,
            'actual': text[:500],
            'detail': f'响应中包含"{exp}"：{"是" if passed else "否"}',
        }
    if at == 'body_status':
        # 校验响应体 JSON 中的 status 字段（业务状态码），
        # 避免 HTTP 200 但业务返回 status!=200 被误判为通过
        try:
            body = json.loads(text)
            actual = body.get('status')
            try:
                passed = int(str(actual)) == int(exp)
            except Exception:
                passed = False
            return {
                'passed': passed, 'type': 'body_status', 'expected': exp,
                'actual': actual,
                'detail': f'响应体 status={actual}，期望 {exp}',
            }
        except Exception:
            return {
                'passed': False, 'type': 'body_status', 'expected': exp,
                'actual': None, 'detail': '响应体非 JSON，无法取 status 字段',
            }
    if at == 'json_path':
        # expected_value 形如 path=>期望值，如 $.data.id=>13 / data.status=>200
        path, exp_val = _split_assert(exp)
        try:
            body = json.loads(text)
        except Exception:
            body = None
        actual = _jsonpath(body, path) if body is not None else None
        passed = _eval_jsonpath_assert(actual, exp_val)
        # json_path 可能命中一整棵子树（$.data.list → 上万字），按字节截断后再落库。
        # 不截断会让 assert_detail 撑爆 TEXT 列，整次执行写库失败。
        if isinstance(actual, (dict, list)):
            # 走 _dump_json 超长时会自动降级成合法 JSON 的摘要结构，
            # json.loads 再解回来，保证这里拿到的一定是可序列化的对象。
            actual = json.loads(_dump_json(actual))
        elif isinstance(actual, str):
            actual = _truncate_bytes(actual, 2000)
        return {
            'passed': passed, 'type': 'json_path', 'expected': exp_val,
            'actual': actual,
            'detail': f'{path} = {actual}，期望 {exp_val}',
        }
    if at == 'regex':
        # expected_value 默认是「匹配整个响应文本」的正则；也支持 "path=>正则"
        # 形式，只对提取出的字段值做匹配（用于断言某个具体字段值符合正则）。
        # 含 "=>" 视为指定了字段路径，与 json_path / header 的约定一致。
        if '=>' in exp:
            path, pattern = _split_assert(exp)
            try:
                body = json.loads(text)
            except Exception:
                body = None
            val = _jsonpath(body, path) if body is not None else None
            if val is None:
                return {
                    'passed': False, 'type': 'regex', 'expected': exp,
                    'actual': None,
                    'detail': f'正则断言路径 {path} 未取到值，无法匹配',
                }
            target_text = str(val)
        else:
            # 无 "=>"：整个 expected_value 即正则，匹配整段响应文本。
            # 注意不能用 _split_assert 的第二个返回值（无 => 时它返回空串），
            # 否则 re.search('', ...) 永远匹配、断言形同虚设。
            pattern = exp
            target_text = text
        try:
            passed = bool(re.search(pattern, target_text))
            return {
                'passed': passed, 'type': 'regex', 'expected': exp,
                'actual': target_text[:500],
                'detail': f'正则"{pattern}"匹配：{"是" if passed else "否"}',
            }
        except Exception:
            return {
                'passed': False, 'type': 'regex', 'expected': exp,
                'actual': None, 'detail': '正则语法错误',
            }
    if at == 'header':
        # expected_value 形如 Header-Name=>期望值，校验响应头
        hname, exp_val = _split_assert(exp)
        actual = resp.headers.get(hname) if resp is not None else None
        passed = _vals_equal(actual, exp_val)
        return {
            'passed': passed, 'type': 'header', 'expected': exp_val,
            'actual': actual,
            'detail': f'响应头 {hname} = {actual}，期望 {exp_val}',
        }
    if at == 'response_time':
        # expected_value 为毫秒阈值，耗时 <= 阈值则通过
        try:
            thr = int(exp)
            actual = elapsed_ms
            passed = actual is not None and actual <= thr
            return {
                'passed': passed, 'type': 'response_time', 'expected': thr,
                'actual': actual,
                'detail': f'耗时 {actual}ms，阈值 {thr}ms（<=通过）',
            }
        except Exception:
            return {
                'passed': False, 'type': 'response_time', 'expected': exp,
                'actual': elapsed_ms,
                'detail': '阈值需为整数毫秒',
            }
    if at == 'custom':
        # 自定义表达式断言：expected_value 为用户手写表达式，求值为真即通过。
        # 提供白名单变量与函数，且禁用 __builtins__（不暴露 __import__ 等），
        # 禁止出现双下划线以阻断 __class__/__subclasses__ 这类反射逃逸。
        expr = (exp or '').strip()
        if expr == '':
            return {
                'passed': False, 'type': 'custom', 'expected': exp,
                'actual': None, 'detail': '自定义表达式为空',
            }
        if '__' in expr:
            return {
                'passed': False, 'type': 'custom', 'expected': exp,
                'actual': None, 'detail': '自定义表达式不允许包含双下划线(__)',
            }
        if len(expr) > 500:
            return {
                'passed': False, 'type': 'custom', 'expected': exp,
                'actual': None, 'detail': f'自定义表达式过长({len(expr)} 字符，上限 500)',
            }
        try:
            body = _wrap_attr(json.loads(text))
        except Exception:
            body = None
        env = {
            'status': http_status,
            'body': body,
            'text': text,
            'headers': dict(resp.headers) if resp is not None else {},
            'elapsed': elapsed_ms,
            # 白名单内置函数（eval 时 __builtins__ 置空，只能用这里显式暴露的）
            'json': json, 're': re,
            'len': len, 'int': int, 'float': float, 'str': str, 'bool': bool,
            'abs': abs, 'min': min, 'max': max, 'sum': sum, 'round': round,
            'sorted': sorted, 'any': any, 'all': all,
        }
        try:
            result = eval(expr, {'__builtins__': {}}, env)  # noqa: S307 内部工具，已限制内置与双下划线
            passed = bool(result)
            return {
                'passed': passed, 'type': 'custom', 'expected': expr,
                'actual': result,
                'detail': f'自定义表达式「{expr}」求值 = {result}，判定{"通过" if passed else "未通过"}',
            }
        except SyntaxError as e:
            return {
                'passed': False, 'type': 'custom', 'expected': expr,
                'actual': None, 'detail': f'自定义表达式语法错误: {e}',
            }
        except Exception as e:
            return {
                'passed': False, 'type': 'custom', 'expected': expr,
                'actual': None, 'detail': f'自定义表达式执行异常: {type(e).__name__}: {e}',
            }
    # 默认按 HTTP 状态码断言
    try:
        passed = int(exp) == http_status
    except Exception:
        passed = False
    return {
        'passed': passed, 'type': 'status_code', 'expected': exp,
        'actual': http_status,
        'detail': f'HTTP 状态码 {http_status}，期望 {exp}',
    }


def _assert_all(iface: dict, http_status: Optional[int], text: str, elapsed_ms=None, resp=None) -> dict:
    """聚合断言：优先用接口上的 asserts 数组（多条，全部通过才 pass）；
    为空则回退到旧的 assert_type/expected_value 单条字段（兼容历史数据）。
    """
    raw = iface.get('asserts')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    # 显式空数组 = 无任何断言（视为通过）；只有缺失/非数组才回退旧单条字段
    asserts = raw if isinstance(raw, list) else None
    if asserts is None:
        asserts = [{
            'assert_type': iface.get('assert_type') or 'status_code',
            'expected_value': iface.get('expected_value') or '',
        }]
    details = []
    for a in asserts:
        if not isinstance(a, dict):
            continue
        if a.get('enabled') == 0:   # 禁用的断言跳过
            continue
        at = a.get('assert_type') or 'status_code'
        ev = a.get('expected_value') or ''
        details.append(_assert_one(at, ev, http_status, text, elapsed_ms, resp))
    passed = all(d['passed'] for d in details) if details else True
    failed = sum(1 for d in details if not d['passed'])
    return {
        'passed': passed,
        'details': details,
        'detail': ('全部断言通过' if passed else f'{failed}/{len(details)} 条断言未通过'),
    }


# 占位符正则：{{ key }}（key 允许字母数字下划线及点）
_VAR_RE = re.compile(r'\{\{\s*([^}]+?)\s*\}\}')


def _var_to_str(val) -> str:
    """把 context 里的变量值转成可放进请求体的字符串。

    非标量（dict/list）必须走 json.dumps：直接 str() 得到的是 Python 字面量
    （单引号、True/False），那不是合法 JSON，下游接口几乎必然解析失败。
    标量仍用 str()，避免把 18 变成 "18.0" 或给字符串多包一层引号。
    """
    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, ensure_ascii=False)
        except Exception:
            return str(val)
    return str(val)


def _render(value, context: dict):
    """递归把 value 中的 {{key}} 替换为 context[key]；value 可为 str/dict/list"""
    if isinstance(value, str):
        if not context:
            return value
        return _VAR_RE.sub(
            lambda m: _var_to_str(context.get(m.group(1).strip(), m.group(0))), value
        )
    if isinstance(value, dict):
        return {k: _render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    return value


def _get_path(obj, path: str):
    """按 a.b.c 取嵌套字段；obj 非 dict 或路径缺失返回 None"""
    if obj is None or not path:
        return None
    cur = obj
    for seg in path.split('.'):
        seg = seg.strip()
        if seg == '':
            continue
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _apply_extracts(resp, resp_json, extracts, context: dict):
    """根据 extracts 规则从响应提取变量写入 context；返回本次提取的 dict。

    关键口径（前置接口依赖，方案B）：
      - **取不到值（None）时不写入 context**，而是记入 `missing`。
        原因见下方说明：若把 None 写进 context，后续接口的 `{{key}}` 会被
        `str(None)` 渲染成字面量 "None" 发出去（如 `Bearer None`），静默出错极难排查。
      - `extracted[name] = None` 仍然保留在返回字典里，由调用方决定如何判失败。
    extracts 元素：{ name, source('body'|'header'), path }
    """
    extracted = {}
    if not extracts:
        return extracted
    for rule in extracts:
        if not isinstance(rule, dict):
            continue
        name = (rule.get('name') or '').strip()
        if not name:
            continue
        source = rule.get('source', 'body')
        path = rule.get('path', '')
        if source == 'header':
            val = resp.headers.get(path) if resp is not None else None
        else:
            # 用 _jsonpath 而非 _get_path：前者是断言侧已在用的解析器，支持
            # `$.data.list[1].album_name` 这类数组下标；后者只认 `a.b.c` 点号
            # 路径，遇到 list 直接返回 None，导致「响应里是列表就提不出任何值」。
            # 两者对纯点号路径行为一致，存量配置不受影响（另见兼容说明）。
            val = _jsonpath(resp_json, path)
        extracted[name] = val
        # 只有取到真值才写入 context；None 视为提取失败，不污染下游渲染
        if val is not None:
            context[name] = val
    return extracted


def _extract_missing(iface: dict, extracted: dict) -> list:
    """返回该接口本次提取失败的变量名列表（extracts 声明了但取不到值的）。"""
    rules = _safe_json(iface.get('extracts')) or []
    missing = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = (rule.get('name') or '').strip()
        if name and (extracted or {}).get(name) is None:
            missing.append(name)
    return missing


def _collect_placeholders(value, out=None):
    """递归收集 value（str/dict/list）里所有 {{key}} 的 key 名。"""
    if out is None:
        out = []
    if isinstance(value, str):
        for m in _VAR_RE.finditer(value):
            k = m.group(1).strip()
            if k and k != 'base_url' and k not in out:
                out.append(k)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_placeholders(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_placeholders(v, out)
    return out


def _iface_placeholders(iface: dict) -> list:
    """接口执行时会用到的所有 {{key}}（URL / Headers / Params / Body），去重去base_url。"""
    keys = []
    _collect_placeholders(iface.get('url') or '', keys)
    _collect_placeholders(_safe_json(iface.get('headers')) or {}, keys)
    _collect_placeholders(_safe_json(iface.get('params')) or {}, keys)
    _collect_placeholders(_safe_json(iface.get('body')), keys)
    return keys


def _missing_bindings(iface: dict, context: dict) -> list:
    """接口引用了但context 里拿不到值的变量（缺失或值为 None）——直接判失败，不再发请求。

    这是方案 A/B 的核心防错：宁可明确失败，也不要把 `{{token}}` 渲染成
    字面量 "None" /原样占位符发出去。
    """
    missing = []
    for k in _iface_placeholders(iface):
        if k not in context or context.get(k) is None or context.get(k) == '':
            missing.append(k)
    return missing


def _resolve_env(db, env_id) -> dict:
    """按 env_id 解析执行环境，返回 {base_url, headers, variables}。
    用于「执行时显式选环境」：环境必须被指定，无默认兜底（用户要求）。
    缺失/不存在/未配 base_url 均抛 400，由调用方转为友好报错。
    """
    if env_id is None:
        raise HTTPException(status_code=400, detail='请先选择执行环境')
    try:
        env_id = int(env_id)
    except Exception:
        raise HTTPException(status_code=400, detail='环境参数非法')
    rows = db.execute_query('SELECT * FROM api_environments WHERE id = %s', (env_id,))
    if not rows:
        raise HTTPException(status_code=400, detail='所选环境不存在，请先选择环境')
    env = rows[0]
    base_url = (env.get('base_url') or '').strip()
    if not base_url:
        raise HTTPException(status_code=400, detail='环境未配置 base_url，无法执行')
    return {
        'id': env.get('id'),
        'base_url': base_url,
        'headers': _safe_json(env.get('headers')) or {},
        'variables': _safe_json(env.get('variables')) or {},
    }


def _build_context(env: dict, user_ctx: dict) -> dict:
    """合并环境级变量与用户手动上下文：环境变量作基础，用户 context 可覆盖。"""
    ctx = {}
    ctx.update(env.get('variables') or {})
    if isinstance(user_ctx, dict):
        ctx.update(user_ctx)
    return ctx


async def _execute_interface(iface: dict, context: dict = None, env: dict = None) -> dict:
    """调用单个接口并断言，返回执行结果字典。
    context 为接口间共享的上下文变量：发送前把 URL/Headers/Params/Body 中的
    {{key}} 替换为 context[key]；响应返回后再按 extracts 规则提取变量写回 context。
    env 为执行时选定的环境 {base_url, headers, variables}：接口 url 若为纯路径，
    则用 env.base_url 拼接成完整地址；env.headers 与接口 headers 合并（接口优先）。
    """
    ctx = context if context is not None else {}
    method = (iface.get('method') or 'GET').upper()
    # 依赖变量缺失 → 直接判失败，**不发出请求**。
    # 否则 {{token}} 会被渲染成字面量 "None"（str(None)）发出去，静默拿到莫名 401，极难排查。
    miss = _missing_bindings(iface, ctx)
    if miss:
        names = ', '.join(miss)
        return {
            'success': 0,
            'http_status': None,
            'elapsed_ms': 0,
            'response_snippet': '',
            'error_msg': '依赖变量未获得：%s（可能前置接口失败、字段路径写错或该变量未在接口里配置提取规则）' % names,
            'assertion_result': 'error',
            'extracted': {},
            'request_dump': None,
            'response_dump': None,
            'assert_detail': {
                'passed': False,
                'type': 'dependency',
                'detail': '依赖变量未获得：%s' % names,
                'details': [{
                    'type': 'dependency',
                    'passed': False,
                    'expected': '上下文变量 %s 存在且非空' % names,
                    'actual': '缺失',
                    'detail': '引用了 {{%s}} 但前置接口未提供该变量，请求未发出' % '}}、{{'.join(miss),
                }],
            },
        }
    url = _render(iface.get('url') or '', ctx)
    # 防御：若仍残留 {{base_url}} 占位（手动输入等情况），剥离后再按环境拼接
    if url.startswith('{{base_url}}'):
        url = '/' + url[len('{{base_url}}'):].lstrip('/')
    # 纯路径模式：接口 url 只存路径，执行时按所选环境的 base_url 拼接成完整地址
    if env and env.get('base_url') and not url.startswith(('http://', 'https://')):
        url = env['base_url'].rstrip('/') + '/' + url.lstrip('/')
    # 请求头：环境级公共头作基础，接口私有头可覆盖同名项
    iface_headers = _render(_safe_json(iface.get('headers')) or {}, ctx)
    env_headers = _render(env.get('headers') or {}, ctx) if env else {}
    headers = {**env_headers, **iface_headers}
    params = _render(_safe_json(iface.get('params')) or {}, ctx)
    raw_body = _safe_json(iface.get('body'))
    json_body = None
    if raw_body is not None:
        json_body = _render(raw_body, ctx)

    t0 = time.time()
    request_dump = None
    response_dump = None
    try:
        # SSL 校验开关：默认校验(使用 certifi CA 包)；verify_ssl=0 时跳过(内网/自签证书端点)
        flag = iface.get('verify_ssl', 1)
        try:
            flag = int(flag)
        except Exception:
            flag = 1
        verify = _CA_BUNDLE if flag else False
        # 请求参数：params / json 仅在非空时才传递。
        # 否则 httpx 会用空对象覆盖 URL 中自带的 query 串，导致参数被吞掉。
        req_kwargs = {'headers': headers}
        if params:
            req_kwargs['params'] = params
        if json_body is not None and json_body != '':
            req_kwargs['json'] = json_body
        # 实际发出的请求快照（含 {{var}} 渲染后的真实值），供执行详情排查
        request_dump = {
            'method': method,
            'url': url,
            'headers': headers,
            'params': params,
            'body': json_body,
        }
        to = float(iface.get('timeout') or 10.0) or 10.0
        attempts = max(1, int(iface.get('retry') or 0) + 1)
        resp = None
        for _att in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=to, verify=verify) as client:
                    resp = await client.request(method, url, **req_kwargs)
                break
            except Exception:
                # 仅网络/超时异常重试，断言失败不重试（断言失败是确定性结果）
                if _att < attempts - 1:
                    await asyncio.sleep(0.5 * (_att + 1))
                    continue
                raise
        elapsed = int((time.time() - t0) * 1000)
        status = resp.status_code
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = None
        snippet = _truncate_bytes(resp.text, 2000)
        # 断言并产出明细（passed / 期望值 / 实际值 / 结论说明）。
        # 传入完整响应文本（不是截断的 snippet）：body_status/json_path/custom 需要完整 JSON，
        # 否则响应体超过 2000 字符时 json.loads 会因截断失败而误判；明细里的展示字段各自截断。
        ar = _assert_all(iface, status, resp.text, elapsed, resp)
        ok = ar['passed']
        # body 单独给预算：_DUMP_MAX_BYTES 是整个 response_dump 的上限，
        # 这里留出 headers/结构/JSON 转义开销，body 占 45000 字节足够排查用。
        # 超限时保留 _body_truncated 标记，让前端能提示「响应体已省略」。
        raw_body = resp.text
        raw_bytes = len(raw_body.encode('utf-8'))
        response_dump = {
            'status': status,
            'elapsed_ms': elapsed,
            'headers': dict(resp.headers),
            'body': _truncate_bytes(raw_body, 45000),
        }
        if raw_bytes > 45000:
            response_dump['_body_truncated'] = True
            response_dump['_body_bytes'] = raw_bytes
        # 响应后提取变量到上下文（供后续接口引用）
        extracts = _safe_json(iface.get('extracts')) or []
        extracted = _apply_extracts(resp, resp_json, extracts, ctx)
        # 提取失败（声明了变量但取不到值）直接判失败：下游会拿到缺失变量，
        # 与其让下游带着 None 去请求，不如在这里就把失败原因说清楚。
        missing_vars = _extract_missing(iface, extracted)
        return {
            'success': 0 if missing_vars else (1 if ok else 0),
            'http_status': status,
            'elapsed_ms': elapsed,
            'response_snippet': snippet,
            'error_msg': ('变量提取失败：%s（检查响应结构与提取路径是否匹配）'
                          % ', '.join(missing_vars)) if missing_vars else '',
            'assertion_result': 'extract_fail' if missing_vars else ('pass' if ok else 'fail'),
            'extracted': extracted,
            'missing_vars': missing_vars,
            'request_dump': request_dump,
            'response_dump': response_dump,
            'assert_detail': ar,
        }
    except Exception as e:
        elapsed = int((time.time() - t0) * 1000)
        return {
            'success': 0,
            'http_status': None,
            'elapsed_ms': elapsed,
            'response_snippet': '',
            'error_msg': _truncate_bytes(str(e), 500),
            'assertion_result': 'error',
            'extracted': {},
            'missing_vars': [],
            'request_dump': request_dump,
            'response_dump': response_dump,
        'assert_detail': None,
    }


def _format_failure_reason(res: dict) -> str:
    """从执行结果抽取可读的失败原因，供微信告警明细。
    优先：请求异常 error_msg（详细异常文本）；
    其次：断言失败 assert_detail.details 中未通过条目的 detail（含类型/期望/实际）；
    兜底：assert_detail.detail / assertion_result / '未知失败'。
    """
    if not isinstance(res, dict):
        return '未知失败'
    em = (res.get('error_msg') or '').strip()
    if em:
        return em
    ad = res.get('assert_detail')
    if isinstance(ad, dict):
        details = ad.get('details') or []
        failed_details = [d.get('detail') for d in details
                          if isinstance(d, dict) and not d.get('passed') and d.get('detail')]
        if failed_details:
            return '；'.join(failed_details)
        summary = (ad.get('detail') or '').strip()
        if summary and summary != '全部断言通过':
            return summary
    ar = (res.get('assertion_result') or '').strip()
    if ar and ar not in ('pass', 'fail', 'error'):
        return ar
    return '未知失败'


def _insert_execution(db, interface_id: int, schedule_id, result: dict, case_tag: str = '',
                      run_batch=None, trigger='manual', env_id=None, run_by=None):
    """落一条执行记录。env_id/run_by 供仪表盘「最近执行」展示环境与触发人：
    手动试跑与手动运行计划写入真实值与当前用户；定时调度无头执行 run_by 为 NULL；
    历史数据这两列为 NULL，前端显示「—」。"""
    sql = """
        INSERT INTO api_executions
            (schedule_id, interface_id, run_time, http_status, elapsed_ms,
             success, response_snippet, error_msg, assertion_result,
             request_dump, response_dump, extracted_vars, assert_detail, case_tag,
             run_batch, `trigger`, env_id, run_by)
        VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    db.execute_update(sql, (
        schedule_id, interface_id, result['http_status'], result['elapsed_ms'],
        result['success'], result['response_snippet'], result['error_msg'],
        result['assertion_result'],
        _dump_json(result.get('request_dump')),
        _dump_json(result.get('response_dump')),
        _dump_json(result.get('extracted')),
        _dump_json(result.get('assert_detail')),
        case_tag or '',
        run_batch,
        trigger,
        env_id or None,
        run_by or None,
    ))


def _fetch_interface(db, interface_id: int) -> Optional[dict]:
    rows = db.execute_query('SELECT * FROM api_interfaces WHERE id = %s', (interface_id,))
    return rows[0] if rows else None


async def _run_interface(iface: dict, context: dict, env: dict):
    """按接口执行，支持两种 dataset 形态：
    - 场景对象数组：每行 {name, params:{键:值|null}, headers, body, asserts}。
      params/headers 中值为 null 的键会从请求中移除（实现「不传」）；asserts 为该场景
      独立期望（覆盖接口级断言）；name 作为执行记录 case_tag。
    - 旧版扁平值数组：每行 {key:value} 仅作 {{}} 替换映射（兼容历史数据）。
    无 dataset 则单次执行。返回 (results, failed)。共享 context 会被提取变量更新。
    """
    dataset = iface.get('dataset')
    if isinstance(dataset, str):
        try:
            dataset = json.loads(dataset)
        except Exception:
            dataset = None
    if not isinstance(dataset, list) or not dataset:
        res = await _execute_interface(iface, context, env)
        return [(res, '')], (res.get('success') != 1)
    results = []
    any_failed = False
    for idx, sc in enumerate(dataset, start=1):
        if not isinstance(sc, dict):
            continue
        # 场景对象判定：含任一结构键即为新场景形态，否则按旧版扁平值映射处理
        is_scenario = any(k in sc for k in ('name', 'params', 'headers', 'body', 'asserts'))
        if not is_scenario:
            row_ctx = {**context, **sc}  # 数据行变量覆盖环境/上游变量
            res = await _execute_interface(iface, row_ctx, env)
            results.append((res, f'用例{idx}'))
            if res.get('success') != 1:
                any_failed = True
            extracted = res.get('extracted') or {}
            if isinstance(extracted, dict):
                context.update(extracted)
            continue

        # 场景对象：构造该场景独立的接口副本（不污染原 iface，供后续场景复用）
        sc_iface = dict(iface)
        sc_ctx = dict(context)
        sc_params = sc.get('params') if isinstance(sc.get('params'), dict) else {}
        sc_headers = sc.get('headers') if isinstance(sc.get('headers'), dict) else {}
        # 场景参数值并入替换上下文（供 URL/模板中的 {{}} 解析）
        # ⚠️ 纯 {{var}} 形态的值要跳过：它写的是「引用上游变量」的占位符，不是数据行变量。
        # 若照样并进sc_ctx，渲染 {{share_id}} 时查到的仍是 "{{share_id}}" 本身（自己替换自己），
        # 最终把占位符原样发出去，对端回 400「分享不存在」，且 _missing_bindings 因
        # 「该键有值」而不拦——排查成本极高。跳过后该键留在 sc_iface.params 里，
        # 由 _execute_interface 用真正的上游值统一渲染一次。
        for d in (sc_params, sc_headers):
            for k, v in d.items():
                if v is None:
                    continue
                if isinstance(v, str) and _VAR_RE.fullmatch(v.strip()):
                    continue
                sc_ctx[k] = v

        # params：以接口模板为基础，按场景覆盖；值为 null 的键移除（实现「不传」）
        # ⚠️ 必须拷贝：_safe_json 对 dict 是**原样返回同一引用**（第 70 行），
        # 直接改会把场景 N 的参数原地写进iface['params']，导致①场景之间互相污染
        # （场景 5 的值泄漏到场景 1）②接口定义本身被改脏，后续单跑/重跑结果不稳定。
        base_params = _safe_json(iface.get('params')) or {}
        if isinstance(base_params, dict):
            merged = dict(base_params)
            for k, v in sc_params.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            sc_iface['params'] = merged

        # headers：逻辑同 params（环境公共头在 _execute_interface 内再合并）
        base_headers = _safe_json(iface.get('headers')) or {}
        if isinstance(base_headers, dict):
            merged_h = dict(base_headers)
            for k, v in sc_headers.items():
                if v is None:
                    merged_h.pop(k, None)
                else:
                    merged_h[k] = v
            sc_iface['headers'] = merged_h

        # body：场景显式提供则整体覆盖（可为对象/数组/字符串）
        if 'body' in sc and sc['body'] is not None:
            sc_iface['body'] = sc['body']

        # 逐场景断言：覆盖接口级断言（置空 assert_type 防止回退到旧单条）
        if sc.get('asserts'):
            sc_iface['asserts'] = sc['asserts']
            sc_iface['assert_type'] = None

        tag = sc.get('name') or f'场景{idx}'
        res = await _execute_interface(sc_iface, sc_ctx, env)
        results.append((res, tag))
        if res.get('success') != 1:
            any_failed = True
        extracted = res.get('extracted') or {}
        if isinstance(extracted, dict):
            context.update(extracted)
    return results, any_failed


async def run_schedule_by_id(schedule_id: int, trigger: str = 'manual', run_by=None) -> dict:
    """
    执行某个计划下的全部接口并落库（供手动按钮与定时调度器共用）。
    不依赖 Request 上下文，失败以返回字典的 error 字段体现，便于调度器静默调用。
    run_by 为触发用户 id（手动运行传当前用户，调度器不传 → NULL）。
    返回 { schedule_id, executed, success, error? }
    """
    with get_db_handler() as db:
        rows = db.execute_query('SELECT * FROM api_schedules WHERE id = %s', (schedule_id,))
        if not rows:
            return {'schedule_id': schedule_id, 'executed': 0, 'success': 0, 'error': '计划不存在'}
        schedule = rows[0]
        ids = _safe_json(schedule.get('interface_ids')) or []
        executed = 0
        success = 0
        failed_items = []  # 收集失败接口名与失败原因，供微信告警明细
        skipped_items = []  # 因接口已被禁用而跳过的接口名（不计入执行/失败）
        # 执行环境：必须显式绑定（用户要求不保留默认兜底）
        try:
            env = _resolve_env(db, schedule.get('env_id'))
        except HTTPException as he:
            return {'schedule_id': schedule_id, 'executed': 0, 'success': 0, 'error': str(he.detail)}
        context = _build_context(env, {})  # 环境级变量作基础，计划内提取变量累加其上
        on_failure = int(schedule.get('on_failure') or 1)  # 1=继续 2=中止
        aborted = False
        # 同一次计划运行的批次号：该次运行产出的所有执行记录共享，便于按运行聚合报告
        run_batch = int(time.time() * 1000)

        # ===== 变量血缘：记录「哪些变量由哪个接口提供」，用于下游依赖缺失时精准跳过 =====
        var_owner = {}   # 变量名 -> 提供它的接口名

        def _record_provided(iface, results):
            """根据 extracts 声明登记变量归属（不依赖本次是否取到值，
            取不到时由 dependency_missing 触发下游跳过）。"""
            for rule in _safe_json(iface.get('extracts')) or []:
                if isinstance(rule, dict):
                    nm = (rule.get('name') or '').strip()
                    if nm:
                        var_owner.setdefault(nm, iface.get('name') or ('接口#%s' % iface.get('id')))

        # 因依赖变量缺失而被跳过的接口：{变量名: 提供该变量的前置接口名}
        # 记入执行记录（success=0/ 标记 dep_skip），让报告里能看到「为什么没跑」。
        dep_skips = []

        def _skip_by_dependency(db, iface, missing, run_batch):
            """依赖变量缺失时不发请求，直接落一条标记为dep_skip 的执行记录。"""
            names = ', '.join(missing)
            owners = '、'.join(sorted({var_owner.get(k, '未知来源') for k in missing}))
            reason = '依赖变量未获得，已跳过：%s（应由前置接口「%s」提供）' % (names, owners)
            res = {
                'success': 0,
                'http_status': None,
                'elapsed_ms': 0,
                'response_snippet': '',
                'error_msg': reason,
                'assertion_result': 'dep_skip',
                'extracted': {},
                'missing_vars': list(missing),
                'request_dump': None,
                'response_dump': None,
                'assert_detail': {
                    'passed': False,
                    'type': 'dependency',
                    'detail': reason,
                    'details': [{
                        'type': 'dependency',
                        'passed': False,
                        'expected': '前置接口「%s」提供 %s' % (owners, names),
                        'actual': '未获得',
                        'detail': reason,
                    }],
                },
            }
            _insert_execution(db, iface.get('id'), schedule_id, res, case_tag='',
                              run_batch=run_batch, trigger=trigger,
                              env_id=env.get('id'), run_by=run_by)
            dep_skips.append((iface.get('name') or ('接口#%s' % iface.get('id')), names))

        # —— 第一步：执行前置接口（方案B）——
        # 计划配置的 prepend_refs 形如 [{"interface_id":12,"required_vars":["token"]}]，
        # 这些接口先跑，把变量写进 context 供正式接口的 {{key}} 引用。
        prepend_refs = _safe_json(schedule.get('prepend_refs')) or []
        prepend_ok = True
        for ref in prepend_refs:
            if not isinstance(ref, dict):
                continue
            try:
                pid = int(ref.get('interface_id'))
            except Exception:
                continue
            p_iface = _fetch_interface(db, pid)
            if not p_iface:
                prepend_ok = False
                logger.warning('[执行] 计划 %s 前置接口不存在: #%s', schedule_id, pid)
                continue
            if not int(p_iface.get('enabled') or 0):
                skipped_items.append('前置:' + (p_iface.get('name') or ('接口#%s' % pid)))
                continue
            _record_provided(p_iface, None)
            p_results, p_failed = await _run_interface(p_iface, context, env)
            for res, tag in p_results:
                _insert_execution(db, pid, schedule_id, res, case_tag=tag,
                                  run_batch=run_batch, trigger=trigger,
                                  env_id=env.get('id'), run_by=run_by)
                executed += 1
                success += int(res.get('success') or 0)
                if res.get('success') != 1:
                    prepend_ok = False
                    pname = p_iface.get('name') or ('接口#%s' % pid)
                    failed_items.append(('(前置)' + pname, tag, _format_failure_reason(res)))
            # 前置接口声明了required_vars 但实际没拿到 → 直接判失败（用户拍板③）
            declared = [str(v).strip() for v in (ref.get('required_vars') or []) if str(v).strip()]
            got = set(context.keys())
            lost = [v for v in declared if v not in got or context.get(v) in (None, '')]
            if lost:
                prepend_ok = False
                logger.info('[执行] 计划 %s 前置接口 #%s 未提供变量: %s',
                            schedule_id, pid, lost)

        # —— 第二步：正式接口列表 ——
        for iid in ids:
            iface = _fetch_interface(db, iid)
            if not iface:
                continue
            # 已禁用的接口自动跳过：接口管理里把enabled 置 0 后，存量计划里
            # 仍残留该id，若照常执行会出现「接口都禁用了计划还在跑」的困惑。
            # 跳过不计入 executed / failed_items，不触发失败策略与微信告警。
            if not int(iface.get('enabled') or 0):
                skipped_items.append(iface.get('name') or ('接口#%s' % iid))
                logger.info('[执行] 计划 %s 跳过已禁用接口: %s(#%s)', schedule_id, iface.get('name'), iid)
                continue
            _record_provided(iface, None)
            # 依赖变量缺失 → 跳过该接口及其后所有同样依赖它的接口（用户拍板②）
            # 只在本轮「变量血缘」里缺失时跳过；纯占位符笔误由 _execute_interface 判失败。
            need = _iface_placeholders(iface)
            lack = [k for k in need if k in var_owner and
                    (k not in context or context.get(k) in (None, ''))]
            if lack:
                _skip_by_dependency(db, iface, lack, run_batch)
                logger.info('[执行] 计划 %s 跳过依赖缺失接口: %s(#%s) 缺 %s',
                            schedule_id, iface.get('name'), iid, lack)
                continue
            # 单接口可能因数据驱动产多行执行记录；返回的 (results, failed) 用于失败策略判断
            results, failed = await _run_interface(iface, context, env)
            for res, tag in results:
                _insert_execution(db, iid, schedule_id, res, case_tag=tag, run_batch=run_batch,
                                  trigger=trigger, env_id=env.get('id'), run_by=run_by)
                executed += 1
                success += int(res.get('success') or 0)
                if res.get('success') != 1:  # 失败：收集接口名 + 失败原因，供微信告警明细
                    iface_name = iface.get('name') or ('接口#%s' % iid)
                    reason = _format_failure_reason(res)
                    failed_items.append((iface_name, tag, reason))
            if failed and on_failure == 2:
                aborted = True
                break
        db.execute_update(
            'UPDATE api_schedules SET last_run_time = NOW() WHERE id = %s', (schedule_id,)
        )
        # 定时任务完成通知：执行计划跑完 → 通知创建人（type=3）
        # 不论手动触发还是以后 APScheduler 自动触发都发（口径见方案拍板②A）。
        try:
            if executed > 0 and schedule.get('create_by'):
                sch_name = schedule.get('name') or ('计划#%s' % schedule_id)
                skip_text = '，跳过已禁用接口 %d 个' % len(skipped_items) if skipped_items else ''
                dep_text = '，因依赖变量缺失跳过 %d 个' % len(dep_skips) if dep_skips else ''
                notify(
                    int(schedule['create_by']), 3, '执行计划已完成',
                    '执行计划「%s」已运行，共执行 %d 个接口，成功 %d 个%s%s'
                    % (sch_name, executed, success, skip_text, dep_text),
                    'schedule', schedule_id, '/automation/summary', run_by,
                )
        except Exception as ne:  # noqa: BLE001
            logger.warning('run_schedule_by_id 完成通知失败（不影响执行）: %s', ne)
        # 微信告警：接口自动化任务执行完毕且存在未通过接口 → 推企业微信群机器人
        # 口径：仅「定时自动触发(trigger='schedule')」才推送；手动触发(trigger='manual')
        # 由用户在页面实时查看结果，不推送企业微信（避免无谓打扰）。executed > success
        # （含断言失败与请求异常）才视为有错误。
        try:
            if trigger == 'schedule' and executed > success:
                sch_name = schedule.get('name') or ('计划#%s' % schedule_id)
                failed = executed - success
                base = os.getenv('WECHAT_REPORT_BASE_URL') or get_wechat_cfg().get('report_base_url') or ''
                link_line = ''
                if base:
                    link = '%s/automation/summary?id=%s' % (base.rstrip('/'), schedule_id)
                    link_line = '\n> [查看报告](%s)' % link
                content = (
                    '接口自动化执行计划「%s」运行完成，存在未通过接口\n'
                    '> 执行接口：**%s** 个，通过：**%s** 个，未通过：**%s** 个%s'
                    % (sch_name, executed, success, failed, link_line)
                )
                if failed_items:  # 逐条列出失败接口名与失败原因
                    detail = ['\n> 失败明细：']
                    shown = 0
                    for iname, stag, reason in failed_items:
                        shown += 1
                        if shown > 20:
                            break
                        r = reason.strip().replace('\n', ' ')
                        if len(r) > 150:
                            r = r[:150] + '…'
                        tag_part = ('（%s）' % stag) if stag else ''
                        detail.append('> %d. **%s**%s：%s' % (shown, iname, tag_part, r))
                    if len(failed_items) > 20:
                        detail.append('> … 其余 %d 条省略' % (len(failed_items) - 20))
                    content += '\n'.join(detail)
                if dep_skips:
                    # 依赖变量缺失被跳过的接口：属「未执行」，单独成段避免与失败混淆
                    dl = ['\n> 因依赖变量缺失跳过（未执行）：']
                    for iname, vars_ in dep_skips[:20]:
                        dl.append('> - %s（缺 %s）' % (iname, vars_))
                    if len(dep_skips) > 20:
                        dl.append('> - … 其余 %d 条省略' % (len(dep_skips) - 20))
                    content += '\n'.join(dl)
                await notify_wechat('接口自动化执行告警', content)
            elif executed > success:
                logger.info('手动触发执行计划#%s 有 %s 个未通过，按口径不推送企业微信',
                            schedule_id, executed - success)
        except Exception as we:  # noqa: BLE001
            logger.warning('微信告警推送失败(不影响主流程): %s', we)
        return {'schedule_id': schedule_id, 'executed': executed, 'success': success,
                'aborted': aborted, 'skipped': len(skipped_items),
                'skipped_names': skipped_items,
                'dep_skipped': len(dep_skips),
                'dep_skip_details': [{'name': n, 'missing_vars': v} for n, v in dep_skips]}


# ---------------------------------------------------------------------------
# 接口管理 CRUD
# ---------------------------------------------------------------------------
@router.get('/interfaces')
async def list_interfaces(
    request: Request,
    name: str = '',
    url: str = '',
    method: str = '',           # 多选以逗号分隔，如 "GET,POST"
    enabled: str = '',          # ''=全部 / '1' / '0'
    group: str = '',            # 分组筛选
    pageNo: int = 1,
    pageSize: int = 20,
):
    try:
        with get_db_handler() as db:
            where = '1=1'
            params = []
            # 数据权限（与 taskcases/defects 一致）：超管(角色含 admin)查看全部，
            # 普通用户仅看自己创建的接口（create_by = 当前用户 id）。
            token = request.headers.get('accessToken')
            if token:
                try:
                    uid = parse_token(token)
                    if uid:
                        role_rows = db.execute_query(
                            "SELECT r.code FROM roles r "
                            "JOIN user_roles ur ON r.id = ur.role_id "
                            "WHERE ur.user_id = %s",
                            (uid,),
                        )
                        codes = [r['code'] for r in role_rows]
                        if 'admin' not in codes:
                            where += ' AND create_by = %s'
                            params.append(uid)
                except Exception as e:
                    logger.warning(f"解析当前用户失败，按查看全部处理: {e}")
            if name:
                where += ' AND name LIKE %s'
                params.append(f'%{name}%')
            if url:
                where += ' AND url LIKE %s'
                params.append(f'%{url}%')
            if method:
                methods = [m.strip().upper() for m in method.split(',') if m.strip()]
                if methods:
                    ph = ','.join(['%s'] * len(methods))
                    where += f' AND method IN ({ph})'
                    params.extend(methods)
            if enabled not in ('', None):
                where += ' AND enabled = %s'
                params.append(int(enabled))
            if group:
                where += ' AND `group` = %s'
                params.append(group)
            total = db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE {where}',
                tuple(params),
            )[0]['c'] or 0
            if pageSize and pageSize > 0:
                offset = (max(1, pageNo) - 1) * pageSize
                rows = db.execute_query(
                    f'SELECT * FROM api_interfaces WHERE {where} ORDER BY id DESC LIMIT %s OFFSET %s',
                    tuple(params + [pageSize, offset]),
                )
            else:
                # pageSize<=0：不分页（供导出全量使用）
                rows = db.execute_query(
                    f'SELECT * FROM api_interfaces WHERE {where} ORDER BY id DESC',
                    tuple(params),
                )
            for r in rows:
                r['headers'] = _safe_json(r.get('headers'))
                r['params'] = _safe_json(r.get('params'))
                r['body'] = _safe_json(r.get('body'))
                r['extracts'] = _safe_json(r.get('extracts')) or []
                r['asserts'] = _safe_json(r.get('asserts')) or []
                r['dataset'] = _safe_json(r.get('dataset')) or []
                r['timeout'] = r.get('timeout') or 0
                r['retry'] = r.get('retry') or 0
                r.setdefault('group', '')
            return {'code': 200, 'msg': 'success', 'data': rows, 'totalCount': total}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询接口失败: {str(e)}')


@router.get('/interfaces/groups')
async def list_interface_groups():
    """返回所有非空的接口分组名称及每组接口数，供左侧分组侧栏使用"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                "SELECT `group` g, COUNT(*) c FROM api_interfaces "
                "WHERE `group` IS NOT NULL AND `group` <> '' GROUP BY `group` ORDER BY g"
            )
            groups = [{'group': r['g'], 'count': r['c']} for r in rows if r.get('g')]
            total = db.execute_query('SELECT COUNT(*) c FROM api_interfaces')[0]['c'] or 0
            return {'code': 200, 'msg': 'success', 'data': {'groups': groups, 'total': total}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询分组失败: {str(e)}')


@router.post('/interfaces/batch-set-enabled')
async def batch_set_enabled(data: dict):
    """批量启用/禁用：data: { ids: List[int], enabled: int }"""
    try:
        ids = [int(i) for i in (data.get('ids') or []) if i is not None]
        if not ids:
            raise HTTPException(status_code=400, detail='请先选择接口')
        enabled = int(data.get('enabled', 1))
        with get_db_handler() as db:
            ph = ','.join(['%s'] * len(ids))
            db.execute_update(
                f'UPDATE api_interfaces SET enabled=%s, update_time=NOW() WHERE id IN ({ph})',
                tuple([enabled] + ids),
            )
        return {'code': 200, 'msg': 'success', 'data': {'count': len(ids)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'批量设置状态失败: {str(e)}')


@router.post('/interfaces/batch-delete')
async def batch_delete_interfaces(data: dict):
    """批量删除：data: { ids: List[int] }"""
    try:
        ids = [int(i) for i in (data.get('ids') or []) if i is not None]
        if not ids:
            raise HTTPException(status_code=400, detail='请先选择接口')
        with get_db_handler() as db:
            ph = ','.join(['%s'] * len(ids))
            db.execute_update(f'DELETE FROM api_interfaces WHERE id IN ({ph})', tuple(ids))
        return {'code': 200, 'msg': 'success', 'data': {'count': len(ids)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'批量删除失败: {str(e)}')


@router.post('/interfaces/batch-add-to-plan')
async def batch_add_to_plan(data: dict):
    """批量加入执行计划：data: { schedule_id: int, interface_ids: List[int] }（去重追加）"""
    try:
        schedule_id = data.get('schedule_id')
        interface_ids = [int(i) for i in (data.get('interface_ids') or []) if i is not None]
        if not schedule_id:
            raise HTTPException(status_code=400, detail='请选择执行计划')
        if not interface_ids:
            raise HTTPException(status_code=400, detail='请选择接口')
        with get_db_handler() as db:
            rows = db.execute_query('SELECT interface_ids FROM api_schedules WHERE id=%s', (schedule_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='执行计划不存在')
            existing = _safe_json(rows[0].get('interface_ids')) or []
            merged = list(existing)
            for i in interface_ids:
                if i not in merged:
                    merged.append(i)
            db.execute_update(
                'UPDATE api_schedules SET interface_ids=%s, update_time=NOW() WHERE id=%s',
                (_dump_json(merged), schedule_id),
            )
        return {'code': 200, 'msg': 'success', 'data': {'count': len(interface_ids), 'total': len(merged)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'加入计划失败: {str(e)}')


@router.post('/interfaces')
async def create_interface(data: InterfaceCreate, request: Request):
    try:
        with get_db_handler() as db:
            sql = """
                INSERT INTO api_interfaces
                    (name, method, url, headers, params, body, assert_type,
                     expected_value, enabled, verify_ssl, extracts, asserts,
                     timeout, retry, dataset, remark, `group`, create_by, create_time, update_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """
            db.execute_update(sql, (
                data.name, data.method, data.url, _dump_json(data.headers),
                _dump_json(data.params), _dump_json(data.body), data.assert_type,
                data.expected_value, data.enabled, data.verify_ssl, _dump_json(data.extracts),
                _dump_json(data.asserts), int(data.timeout or 0), int(data.retry or 0),
                _dump_json(data.dataset), data.remark or '', data.group or '', _current_uid(request),
            ))
            new_id = db.execute_query('SELECT LAST_INSERT_ID() as id')[0]['id']
            return {'code': 200, 'msg': 'success', 'data': {'id': new_id}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'创建接口失败: {str(e)}')


def _iface_signature(method, url, params, body) -> str:
    """接口定义签名：method+url+params+body 的 MD5。

    用于 F7 接口变更感知：签名变化说明接口入参/请求体改了，其 dataset 场景可能过期。
    params/body 用 sort_keys 保证序列化稳定；同时兼容「dict」与「已序列化的 JSON 串」
    两种形态（接口入参是 dict、存库/回填时读到的是 JSON 串），确保签名一致。
    """
    def _canon(v):
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s:
                try:
                    return json.loads(s)
                except Exception:
                    return s
        return v if v is not None else {}
    try:
        p = json.dumps(_canon(params), ensure_ascii=False, sort_keys=True)
    except Exception:
        p = ''
    try:
        b = json.dumps(_canon(body), ensure_ascii=False, sort_keys=True)
    except Exception:
        b = ''
    raw = '%s\n%s\n%s\n%s' % ((method or '').upper(), url or '', p, b)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


@router.put('/interfaces')
async def update_interface(data: InterfaceUpdate, request: Request):
    try:
        with get_db_handler() as db:
            # F7：先取旧签名，判定接口定义是否变化
            cur = db.execute_query(
                'SELECT dataset_sig, dataset_stale FROM api_interfaces WHERE id=%s',
                (int(data.id),))
            old_sig = (cur[0]['dataset_sig'] or '') if cur else ''
            old_stale = int((cur[0].get('dataset_stale') or 0)) if cur else 0
            ds_in = data.dataset
            ds_nonempty = isinstance(ds_in, list) and len(ds_in) > 0
            new_sig = _iface_signature(data.method, data.url, data.params, data.body)
            sig_changed = new_sig != old_sig
            if not old_sig:
                # 没有历史基线（新接口 / 迁移前创建的接口）→ 只建立基线，不误标过期
                stale = 0
            elif sig_changed and ds_nonempty:
                # 接口定义变了且有场景 → 标记可能过期
                stale = 1
            elif not ds_nonempty:
                stale = 0
            else:
                # 签名没变：维持原状（别强行清零，等重新生成/编辑场景时清）
                stale = old_stale
            sql = """
                UPDATE api_interfaces
                SET name=%s, method=%s, url=%s, headers=%s, params=%s, body=%s,
                    assert_type=%s, expected_value=%s, enabled=%s, verify_ssl=%s, extracts=%s,
                    asserts=%s, timeout=%s, retry=%s, dataset=%s, remark=%s, `group`=%s,
                    dataset_sig=%s, dataset_stale=%s,
                    update_time=NOW()
                WHERE id=%s
            """
            db.execute_update(sql, (
                data.name, data.method, data.url, _dump_json(data.headers),
                _dump_json(data.params), _dump_json(data.body), data.assert_type,
                data.expected_value, data.enabled, data.verify_ssl, _dump_json(data.extracts),
                _dump_json(data.asserts), int(data.timeout or 0), int(data.retry or 0),
                _dump_json(data.dataset), data.remark or '', data.group or '',
                new_sig, stale,
                data.id,
            ))
            return {'code': 200, 'msg': 'success', 'data': {'id': data.id, 'dataset_stale': stale}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'更新接口失败: {str(e)}')


@router.put('/interfaces/dataset')
async def update_interface_dataset(data: dict, request: Request):
    """仅更新指定接口的测试场景(dataset)，不影响该接口其它字段。

    data: { id: int, dataset: [...] }
    dataset 为场景数组，元素形如 {name, params:{键:值|null}, headers:{}, body:任意, asserts:[...]}；
    传 [] 或 null 表示清空该接口的全部测试场景。
    为什么单独开一个接口：update_interface 是全字段覆盖式更新，若只传 dataset 会把其它列写成空。
    """
    try:
        if not data or not data.get('id'):
            raise HTTPException(status_code=400, detail='缺少参数 id')
        with get_db_handler() as db:
            # F7：场景被显式编辑/重设 → 视为最新，清除「可能过期」标记
            db.execute_update(
                'UPDATE api_interfaces SET dataset=%s, dataset_stale=0, update_time=NOW() WHERE id=%s',
                (_dump_json(data.get('dataset') or []), data['id']),
            )
            return {'code': 200, 'msg': 'success', 'data': {'id': data['id']}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'更新测试场景失败: {str(e)}')


@router.delete('/interfaces')
async def delete_interface(data: dict):
    """data: { id: int }"""
    try:
        iface_id = data.get('id')
        if not iface_id:
            raise HTTPException(status_code=400, detail='缺少 id')
        with get_db_handler() as db:
            db.execute_update('DELETE FROM api_interfaces WHERE id=%s', (iface_id,))
            return {'code': 200, 'msg': 'success', 'data': None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'删除接口失败: {str(e)}')


@router.post('/interfaces/test')
async def test_interface(data: dict, request: Request):
    """手动试跑单个接口：data: { id: int, env_id: int, context?: dict }
    返回执行结果、断言结论与本接口提取到的变量（便于调试串联链路）。
    env_id 为必填（执行时显式选环境）；context 为可选前置上下文（如已手工提供的
    token），本接口内 {{x}} 会被替换，且可覆盖环境级变量。
    """
    try:
        iface_id = data.get('id')
        if not iface_id:
            raise HTTPException(status_code=400, detail='缺少 id')
        env_id = data.get('env_id')
        if env_id is None:
            raise HTTPException(status_code=400, detail='请先选择执行环境')
        ctx = data.get('context')
        if not isinstance(ctx, dict):
            ctx = {}
        with get_db_handler() as db:
            env = _resolve_env(db, env_id)
            ctx = _build_context(env, ctx)  # 环境变量作基础，用户手动 context 可覆盖
            iface = _fetch_interface(db, iface_id)
            if not iface:
                raise HTTPException(status_code=404, detail='接口不存在')
            results, failed = await _run_interface(iface, ctx, env)
            for res, tag in results:
                _insert_execution(db, iface_id, None, res, case_tag=tag,
                                  env_id=env.get('id'), run_by=_current_uid(request))
            return {
                'code': 200, 'msg': 'success',
                'data': {
                    'results': [{'case_tag': tag, **res} for res, tag in results],
                    'failed': failed,
                    'scenario_count': len(results),
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'试跑失败: {str(e)}')


# ---------------------------------------------------------------------------
# 执行计划 CRUD
# ---------------------------------------------------------------------------
@router.get('/schedules')
async def list_schedules(name: str = ''):
    try:
        with get_db_handler() as db:
            where = '1=1'
            params = []
            if name:
                where += ' AND name LIKE %s'
                params.append(f'%{name}%')
            rows = db.execute_query(
                f'SELECT * FROM api_schedules WHERE {where} ORDER BY id DESC',
                tuple(params),
            )
            # 解析 interface_ids 并补全接口名称，便于前端展示
            all_ifaces = {
                r['id']: r['name']
                for r in db.execute_query('SELECT id, name FROM api_interfaces')
            }
            env_map = {
                r['id']: r['name']
                for r in db.execute_query('SELECT id, name FROM api_environments')
            }
            for s in rows:
                ids = _safe_json(s.get('interface_ids')) or []
                s['interface_ids'] = ids
                s['interface_names'] = [all_ifaces.get(i, f'未知({i})') for i in ids]
                s['env_name'] = env_map.get(s.get('env_id')) or '未绑定'
                # 前置接口依赖声明：解析 JSON 并补全接口名称，便于列表直接展示
                prefs = _safe_json(s.get('prepend_refs')) or []
                s['prepend_refs'] = prefs
                s['prepend_names'] = [
                    all_ifaces.get(int(p.get('interface_id')), f'未知({p.get("interface_id")})')
                    for p in prefs if isinstance(p, dict) and p.get('interface_id') is not None
                ]
            return {'code': 200, 'msg': 'success', 'data': rows, 'totalCount': len(rows)}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询计划失败: {str(e)}')


@router.post('/schedules')
async def create_schedule(data: ScheduleCreate, request: Request):
    try:
        with get_db_handler() as db:
            sql = """
                INSERT INTO api_schedules
                    (name, interface_ids, cron_expr, enabled, env_id, on_failure,
                     prepend_refs, create_by, create_time, update_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """
            db.execute_update(sql, (
                data.name, _dump_json(data.interface_ids), data.cron_expr,
                data.enabled, data.env_id, int(data.on_failure or 1),
                _dump_json(_clean_prepend_refs(data.prepend_refs)), _current_uid(request),
            ))
            new_id = db.execute_query('SELECT LAST_INSERT_ID() as id')[0]['id']
            return {'code': 200, 'msg': 'success', 'data': {'id': new_id}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'创建计划失败: {str(e)}')


@router.put('/schedules')
async def update_schedule(data: ScheduleUpdate, request: Request):
    try:
        with get_db_handler() as db:
            sql = """
                UPDATE api_schedules
                SET name=%s, interface_ids=%s, cron_expr=%s, enabled=%s, env_id=%s,
                    on_failure=%s, prepend_refs=%s, create_by=%s, update_time=NOW()
                WHERE id=%s
            """
            db.execute_update(sql, (
                data.name, _dump_json(data.interface_ids), data.cron_expr,
                data.enabled, data.env_id, int(data.on_failure or 1),
                _dump_json(_clean_prepend_refs(data.prepend_refs)),
                _current_uid(request), data.id,
            ))
            return {'code': 200, 'msg': 'success', 'data': {'id': data.id}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'更新计划失败: {str(e)}')


@router.delete('/schedules')
async def delete_schedule(data: dict):
    try:
        sid = data.get('id')
        if not sid:
            raise HTTPException(status_code=400, detail='缺少 id')
        with get_db_handler() as db:
            db.execute_update('DELETE FROM api_schedules WHERE id=%s', (sid,))
            return {'code': 200, 'msg': 'success', 'data': None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'删除计划失败: {str(e)}')


@router.post('/schedules/run')
async def run_schedule(data: dict, request: Request):
    """手动立即执行某计划：data: { id: int }，逐个调用其下接口并落库"""
    try:
        sid = data.get('id')
        if not sid:
            raise HTTPException(status_code=400, detail='缺少 id')
        result = await run_schedule_by_id(sid, trigger='manual', run_by=_current_uid(request))
        if result.get('error'):
            raise HTTPException(status_code=404, detail=result['error'])
        return {'code': 200, 'msg': 'success', 'data': result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'执行计划失败: {str(e)}')


# ---------------------------------------------------------------------------
# 执行总结与记录
# ---------------------------------------------------------------------------
@router.get('/summary')
async def execution_summary():
    try:
        with get_db_handler() as db:
            stats = db.execute_query(
                """
                SELECT COUNT(*) as total,
                       SUM(success) as success,
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as fail
                FROM api_executions
                """
            )[0]
            total = stats['total'] or 0
            success = stats['success'] or 0
            fail = stats['fail'] or 0
            rate = round(success / total * 100, 2) if total else 0

            trend = db.execute_query(
                """
                SELECT DATE(run_time) as day,
                       COUNT(*) as total,
                       SUM(success) as success
                FROM api_executions
                WHERE run_time >= DATE_SUB(NOW(), INTERVAL 14 DAY)
                GROUP BY DATE(run_time)
                ORDER BY day
                """
            )
            trend = [
                {
                    'day': str(r['day']),
                    'total': r['total'] or 0,
                    'success': r['success'] or 0,
                }
                for r in trend
            ]

            # 覆盖率：被至少 1 个启用计划引用的接口 / 接口总数
            covered = set()
            for srow in db.execute_query(
                "SELECT interface_ids FROM api_schedules WHERE enabled=1 "
                "AND interface_ids IS NOT NULL AND interface_ids <> '[]'"
            ):
                for i in (_safe_json(srow['interface_ids']) or []):
                    try:
                        covered.add(int(i))
                    except Exception:
                        pass
            total_ifaces = db.execute_query('SELECT COUNT(*) c FROM api_interfaces')[0]['c'] or 0

            return {
                'code': 200, 'msg': 'success',
                'data': {
                    'statistics': {
                        'total': total, 'success': success,
                        'fail': fail, 'success_rate': rate,
                    },
                    'trend': trend,
                    'coverage': {
                        'total': total_ifaces,
                        'covered': len(covered),
                        'rate': round(len(covered) / total_ifaces * 100, 2) if total_ifaces else 0,
                    },
                },
            }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'统计失败: {str(e)}')


# ---------------------------------------------------------------------------
# 仪表盘：一次请求返回看板全部面板数据
# ---------------------------------------------------------------------------
# 口径（用户已确认）：
#   接口用例 = 未配 dataset 的接口；场景用例 = dataset(JSON 数组) 展开后的条目数
_NO_DATASET = "(dataset IS NULL OR TRIM(dataset) IN ('', '[]', 'null'))"


def _scene_count(rows) -> int:
    """累加若干接口行的 dataset 场景条目数（非数组/解析失败记 0）"""
    total = 0
    for r in rows:
        ds = _safe_json(r.get('dataset'))
        if isinstance(ds, list):
            total += len(ds)
    return total


@router.get('/dashboard')
async def automation_dashboard(days: int = 7):
    """接口自动化看板聚合数据。

    days 控制趋势类面板的回溯天数（仅接受 7/14/30，其它值回落到 7）。
    一次返回指标卡、执行趋势、用例类型分布、接口分组分布、通过率趋势、响应时间趋势、
    环境状态、最近执行、接口健康度 Top5。日期序列由后端补零，避免无执行记录的日期
    在折线图上跳变；schedule_id 指向已删除计划的历史脏数据会给出「计划已删除」文案。
    """
    try:
        if days not in (7, 14, 30):
            days = 7
        start_day = date.today() - timedelta(days=days - 1)
        window_start = f'{start_day} 00:00:00'

        with get_db_handler() as db:
            # ---------------- 指标卡 ----------------
            interface_total = db.execute_query(
                'SELECT COUNT(*) c FROM api_interfaces'
            )[0]['c'] or 0
            interface_case = db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE {_NO_DATASET}'
            )[0]['c'] or 0
            scene_rows = db.execute_query(
                f'SELECT dataset FROM api_interfaces WHERE NOT {_NO_DATASET}'
            )
            scene_case = _scene_count(scene_rows)
            env_count = db.execute_query(
                'SELECT COUNT(*) c FROM api_environments'
            )[0]['c'] or 0

            today = db.execute_query(
                'SELECT COUNT(*) n, SUM(success) ok FROM api_executions '
                'WHERE DATE(run_time) = CURDATE()'
            )[0]
            today_run = int(today['n'] or 0)
            today_ok = int(today['ok'] or 0)
            pass_rate = round(today_ok / today_run * 100, 2) if today_run else 0

            # ---------------- 较昨日（统一以「昨天全天」为对比基准）----------------
            yest = db.execute_query(
                'SELECT COUNT(*) n, SUM(success) ok FROM api_executions '
                'WHERE DATE(run_time) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)'
            )[0]
            yest_run = int(yest['n'] or 0)
            yest_ok = int(yest['ok'] or 0)
            yest_rate = round(yest_ok / yest_run * 100, 2) if yest_run else None

            yest_new = 'DATE(create_time) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)'
            new_iface = db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE {yest_new}'
            )[0]['c'] or 0
            new_iface_case = db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE {yest_new} AND {_NO_DATASET}'
            )[0]['c'] or 0
            new_scene_case = _scene_count(db.execute_query(
                f'SELECT dataset FROM api_interfaces WHERE {yest_new} AND NOT {_NO_DATASET}'
            ))
            new_env = db.execute_query(
                f'SELECT COUNT(*) c FROM api_environments WHERE {yest_new}'
            )[0]['c'] or 0

            # ---------------- 执行趋势（按天聚合后补零日）----------------
            trend_rows = db.execute_query(
                """
                SELECT DATE(run_time) d, COUNT(*) total, SUM(success) ok,
                       ROUND(AVG(elapsed_ms)) avg_ms
                FROM api_executions
                WHERE run_time >= %s
                GROUP BY DATE(run_time)
                """,
                (window_start,),
            )
            by_day = {str(r['d']): r for r in trend_rows}
            exec_trend, pass_rate_trend, resp_time_trend = [], [], []
            for i in range(days):
                key = str(start_day + timedelta(days=i))
                row = by_day.get(key)
                total = int(row['total'] or 0) if row else 0
                ok = int(row['ok'] or 0) if row else 0
                exec_trend.append({
                    'day': key, 'total': total, 'success': ok, 'fail': total - ok,
                })
                pass_rate_trend.append({
                    'day': key,
                    'rate': round(ok / total * 100, 2) if total else 0,
                })
                resp_time_trend.append({
                    'day': key,
                    'avg_ms': int(row['avg_ms'] or 0) if row else 0,
                })

            # ---------------- 用例类型分布 / 接口分组分布 ----------------
            case_type = [
                {'name': '接口用例', 'value': int(interface_case)},
                {'name': '场景用例', 'value': int(scene_case)},
            ]
            group_dist = [
                {'name': r['g'] or '未分组', 'value': int(r['n'] or 0)}
                for r in db.execute_query(
                    "SELECT IFNULL(NULLIF(TRIM(`group`), ''), '未分组') g, COUNT(*) n "
                    'FROM api_interfaces GROUP BY g ORDER BY n DESC'
                )
            ]

            # ---------------- 环境状态（无 enabled 字段，改给出被计划引用数）----------------
            env_status = db.execute_query(
                """
                SELECT e.id, e.name, e.base_url, e.update_time,
                       u.realname AS owner,
                       (SELECT COUNT(*) FROM api_schedules s WHERE s.env_id = e.id)
                           AS schedule_count
                FROM api_environments e
                LEFT JOIN users u ON u.id = e.create_by
                ORDER BY e.update_time DESC
                """
            )
            for e in env_status:
                e['update_time'] = str(e['update_time']) if e.get('update_time') else None
                e['schedule_count'] = int(e.get('schedule_count') or 0)
                e['owner'] = e.get('owner') or '—'

            # ---------------- 最近执行（含环境、执行人）----------------
            recent_runs = db.execute_query(
                """
                SELECT e.id, e.interface_id, i.name AS case_name, e.case_tag,
                       e.schedule_id, s.name AS schedule_name,
                       e.env_id, env.name AS env_name,
                       e.run_by, u.realname AS operator,
                       e.http_status, e.elapsed_ms, e.success, e.`trigger`, e.run_time
                FROM api_executions e
                LEFT JOIN api_interfaces i ON i.id = e.interface_id
                LEFT JOIN api_environments env ON env.id = e.env_id
                LEFT JOIN users u ON u.id = e.run_by
                LEFT JOIN api_schedules s ON s.id = e.schedule_id
                ORDER BY e.run_time DESC
                LIMIT 8
                """
            )
            for r in recent_runs:
                r['success'] = int(r.get('success') or 0)
                r['elapsed_ms'] = int(r.get('elapsed_ms') or 0)
                rt = r.get('run_time')
                r['finish_time'] = str(rt + timedelta(milliseconds=r['elapsed_ms'])) if rt else None
                r['run_time'] = str(rt) if rt else None
                r['case_name'] = r.get('case_name') or f"接口#{r.get('interface_id')}"
                r['scene'] = r.get('case_tag') or '—'
                if r.get('schedule_id') is None:
                    r['run_scope'] = '手动执行'
                elif not r.get('schedule_name'):
                    r['run_scope'] = '计划已删除'   # 历史脏数据兜底
                else:
                    r['run_scope'] = r['schedule_name']
                r['env_name'] = r.get('env_name') or '—'
                r['operator'] = r.get('operator') or '—'

            # ---------------- 接口健康度 Top5（失败数降序，样本 <3 次不参与）----------------
            health_top = []
            for r in db.execute_query(
                """
                SELECT e.interface_id, i.name, i.`group`,
                       COUNT(*) total, SUM(e.success) ok, ROUND(AVG(e.elapsed_ms)) avg_ms
                FROM api_executions e
                LEFT JOIN api_interfaces i ON i.id = e.interface_id
                WHERE e.run_time >= %s
                GROUP BY e.interface_id, i.name, i.`group`
                HAVING COUNT(*) >= 3
                ORDER BY (COUNT(*) - SUM(e.success)) DESC, total DESC
                LIMIT 5
                """,
                (window_start,),
            ):
                total = int(r['total'] or 0)
                fail = total - int(r['ok'] or 0)
                health_top.append({
                    'interface_id': r['interface_id'],
                    'name': r.get('name') or f"接口#{r['interface_id']}",
                    'group': r.get('group') or '未分组',
                    'total': total,
                    'fail': fail,
                    'fail_rate': round(fail / total * 100, 2) if total else 0,
                    'avg_ms': int(r['avg_ms'] or 0),
                })
            insufficient = db.execute_query(
                """
                SELECT COUNT(*) c FROM (
                    SELECT interface_id FROM api_executions
                    WHERE run_time >= %s
                    GROUP BY interface_id HAVING COUNT(*) < 3
                ) t
                """,
                (window_start,),
            )[0]['c'] or 0

            return {
                'code': 200, 'msg': 'success',
                'data': {
                    'days': days,
                    'stats': {
                        'interface_total': int(interface_total),
                        'interface_case': int(interface_case),
                        'scene_case': int(scene_case),
                        'scene_interface': len(scene_rows),
                        'today_run': today_run,
                        'today_success': today_ok,
                        'pass_rate': pass_rate,
                        'fail_count': today_run - today_ok,
                        'env_count': int(env_count),
                    },
                    'deltas': {
                        'interface_total': int(new_iface),
                        'interface_case': int(new_iface_case),
                        'scene_case': int(new_scene_case),
                        'today_run': yest_run,
                        'pass_rate': yest_rate,
                        'fail_count': yest_run - yest_ok,
                        'env_count': int(new_env),
                    },
                    'exec_trend': exec_trend,
                    'pass_rate_trend': pass_rate_trend,
                    'resp_time_trend': resp_time_trend,
                    'case_type': case_type,
                    'group_dist': group_dist,
                    'env_status': env_status,
                    'recent_runs': recent_runs,
                    'health_top': health_top,
                    'health_insufficient': int(insufficient),
                },
            }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'看板统计失败: {str(e)}')


@router.get('/executions')
async def list_executions(
    pageNo: int = 1, pageSize: int = 20,
    schedule_id: int = None, interface_id: int = None,
):
    try:
        with get_db_handler() as db:
            where = '1=1'
            params = []
            if schedule_id:
                where += ' AND e.schedule_id = %s'
                params.append(schedule_id)
            if interface_id:
                where += ' AND e.interface_id = %s'
                params.append(interface_id)

            total = db.execute_query(
                f'SELECT COUNT(*) as c FROM api_executions e WHERE {where}',
                tuple(params),
            )[0]['c']

            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                f"""
                SELECT e.id, e.schedule_id, e.interface_id, i.name as interface_name,
                       e.run_time, e.http_status, e.elapsed_ms, e.success,
                       e.assertion_result, e.error_msg
                FROM api_executions e
                LEFT JOIN api_interfaces i ON i.id = e.interface_id
                WHERE {where}
                ORDER BY e.run_time DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [pageSize, offset]),
            )
            return {'code': 200, 'msg': 'success', 'data': rows, 'totalCount': total}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询执行记录失败: {str(e)}')


@router.get('/executions/{execution_id}')
async def get_execution_detail(execution_id: int):
    """执行记录详情：返回完整请求快照/响应快照/断言明细/提取变量，用于排查失败。"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT * FROM api_executions WHERE id = %s', (execution_id,)
            )
            if not rows:
                raise HTTPException(status_code=404, detail='执行记录不存在')
            return {'code': 200, 'msg': 'success', 'data': rows[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询执行详情失败: {str(e)}')


def _scenario_names(iface):
    """返回该接口一次运行期望的用例(case)标识列表，与 _run_interface 生成的 case_tag 对齐：
    - 无 dataset：单次执行，case_tag 为空串；
    - 场景对象数组：取场景 name（缺省 场景N）；
    - 旧版扁平值数组：用例N。"""
    dataset = iface.get('dataset')
    if isinstance(dataset, str):
        try:
            dataset = json.loads(dataset)
        except Exception:
            dataset = None
    if isinstance(dataset, list) and dataset:
        names = []
        for idx, sc in enumerate(dataset, start=1):
            if isinstance(sc, dict) and any(k in sc for k in ('name', 'params', 'headers', 'body', 'asserts')):
                names.append(sc.get('name') or f'场景{idx}')
            else:
                names.append(f'用例{idx}')
        return names
    return ['']


def _build_iface_cases(iface, iid, srows):
    """按接口 dataset 期望的场景列表去匹配本次运行的执行行，产出
    (cases, passed, failed, not_run)。

    匹配规则与 _scenario_names 对齐：无 dataset 期望 1 条空 tag；场景对象数组取场景 name。
    dataset 已变更导致执行行的 tag 不在当前场景列表中时，那些执行行仍按真实结果追加到
    末尾（不丢历史执行结果）。依赖变量缺失被跳过的记录（assertion_result=dep_skip）
    计入 not_run，不进成功率分母。
    正式接口与前置接口共用这一个装配逻辑，保证两边结论口径完全一致。
    """
    srows = list(srows or [])
    names = _scenario_names(iface)
    used = set()
    cases = []
    passed = 0
    failed = 0
    not_run = 0
    for nm in names:
        matched = None
        for ri, row in enumerate(srows):
            if ri in used:
                continue
            if (row.get('case_tag') or '') == (nm or ''):
                matched = ri
                break
        if matched is not None:
            used.add(matched)
            row = srows[matched]
            cases.append(_case_row(iface, iid, row, None, not_run=False))
            if (row.get('assertion_result') or '') == 'dep_skip':
                not_run += 1
            elif row['success']:
                passed += 1
            else:
                failed += 1
        else:
            cases.append(_case_row(iface, iid, None, nm, not_run=True))
            not_run += 1
    for ri, row in enumerate(srows):
        if ri in used:
            continue
        used.add(ri)
        cases.append(_case_row(iface, iid, row, None, not_run=False))
        if (row.get('assertion_result') or '') == 'dep_skip':
            not_run += 1
        elif row['success']:
            passed += 1
        else:
            failed += 1
    return cases, passed, failed, not_run


def _prepend_sections(db, plan_row, rows_by_iface, iface_name_map):
    """报告详情中的「前置接口执行情况」：与正式接口同结构（接口头 + 用例明细），
    数据来自该计划本次运行(run_batch)落下的前置接口执行记录。
    前置接口是取变量的手段，其结果**不计入**报告的通过/失败统计（统计口径仍只看正式接口），
    避免一个「登录接口」把业务接口的成功率拉低。"""
    out = []
    for ref in (_safe_json(plan_row.get('prepend_refs')) or []):
        if not isinstance(ref, dict):
            continue
        try:
            pid = int(ref.get('interface_id'))
        except Exception:
            continue
        p_iface = _fetch_interface(db, pid) or {}
        cases, _p, _f, _n = _build_iface_cases(p_iface, pid, (rows_by_iface or {}).get(pid, []))
        out.append({
            'interface_id': pid,
            'name': p_iface.get('name') or ('接口#' + str(pid)),
            'method': p_iface.get('method'),
            'url': p_iface.get('url'),
            'required_vars': [str(v) for v in (ref.get('required_vars') or []) if str(v).strip()],
            'cases': cases,
        })
    return out


def prepend_names_of(plan_row, iface_name_map) -> list:
    """取计划的前置接口名称列表（报告用）：[{'interface_id':12,'name':'登录','required_vars':['token']}]"""
    out = []
    for ref in (_safe_json(plan_row.get('prepend_refs')) or []):
        if not isinstance(ref, dict):
            continue
        try:
            iid = int(ref.get('interface_id'))
        except Exception:
            continue
        out.append({
            'interface_id': iid,
            'name': (iface_name_map or {}).get(iid) or ('接口#%s' % iid),
            'required_vars': [str(v) for v in (ref.get('required_vars') or []) if str(v).strip()],
        })
    return out


@router.get('/schedules/report')
async def plan_execution_report():
    """计划执行报告：每次计划运行(按 run_batch 区分)一条记录，相当于一份接口自动化测试报告。
    详情按「接口 → 用例(case)」展开，含通过/失败/未执行；失败策略=中止后续时，
    未跑到的用例计入「未执行」（由接口 dataset 场景数重建得出）。"""
    try:
        with get_db_handler() as db:
            plans = db.execute_query(
                'SELECT id, name, interface_ids, env_id, on_failure, cron_expr, enabled, '
                'last_run_time, prepend_refs FROM api_schedules ORDER BY id DESC'
            )
            env_map = {
                r['id']: r['name']
                for r in db.execute_query('SELECT id, name FROM api_environments')
            }
            # 各计划的每一次运行(批次)，按 (计划, 批次号) 聚合，批次号降序=先最新
            batches = db.execute_query(
                'SELECT schedule_id, run_batch, MIN(run_time) AS run_time, COUNT(*) AS executed '
                'FROM api_executions WHERE run_batch IS NOT NULL '
                'GROUP BY schedule_id, run_batch ORDER BY schedule_id DESC, run_batch DESC'
            )
            # 一次性取出这些批次的全部执行行，按 (计划,批次) 归组
            batch_rows = {}
            if batches:
                clauses = []
                vals = []
                for b in batches:
                    clauses.append('(%s,%s)')
                    vals += [b['schedule_id'], b['run_batch']]
                rows = db.execute_query(
                    'SELECT * FROM api_executions '
                    'WHERE (schedule_id, run_batch) IN (' + ','.join(clauses) + ')',
                    vals,
                )
                for r in rows:
                    batch_rows.setdefault((r['schedule_id'], r['run_batch']), []).append(r)

            result = []
            # 前置接口名称映射（供报告标注「本计划依赖的前置接口」）
            iface_name_map = {
                r['id']: r['name'] for r in db.execute_query('SELECT id, name FROM api_interfaces')
            }
            for p in plans:
                ids = [i for i in (_safe_json(p.get('interface_ids')) or []) if i not in (None, '')]
                plan_batches = [b for b in batches if b['schedule_id'] == p['id']]
                # 从未执行过的计划：仅当有接口时，产出一条「未运行」记录
                if not plan_batches:
                    if not ids:
                        continue
                    iface_list = []
                    not_run = 0
                    for raw_iid in ids:
                        try:
                            iid = int(raw_iid)
                        except Exception:
                            continue
                        iface = _fetch_interface(db, iid) or {}
                        for nm in _scenario_names(iface):
                            iface_list.append(_case_row(iface, iid, None, nm, not_run=True))
                            not_run += 1
                    result.append({
                        'id': p['id'],
                        'name': p['name'],
                        'run_no': 0,
                        'run_batch': None,
                        'run_time': None,
                        'interface_count': len(ids),
                        'case_count': not_run,
                        'executed': 0,
                        'passed': 0,
                        'failed': 0,
                        'not_run': not_run,
                        'success_rate': 0,
                        'status': 'none',
                        'enabled': p.get('enabled'),
                        'env_name': env_map.get(p.get('env_id')) or '未绑定',
                        'on_failure': p.get('on_failure'),
                        'trigger': None,
                        'prepend_names': prepend_names_of(p, iface_name_map),
                        'prepend_details': [],
                        'interfaces': iface_list,
                    })
                    continue
                # 已执行：每个批次一条运行记录（最新在前）
                for run_no, b in enumerate(plan_batches, start=1):
                    rb = b['run_batch']
                    rows = batch_rows.get((p['id'], rb), [])
                    rows_by_iface = {}
                    for r in rows:
                        rows_by_iface.setdefault(r['interface_id'], []).append(r)
                    passed = 0
                    failed = 0
                    not_run = 0
                    iface_list = []
                    for raw_iid in ids:
                        try:
                            iid = int(raw_iid)
                        except Exception:
                            continue
                        iface = _fetch_interface(db, iid) or {}
                        cases, cp, cf, cn = _build_iface_cases(
                            iface, iid, rows_by_iface.get(iid, []))
                        passed += cp
                        failed += cf
                        not_run += cn
                        iface_list.append({
                            'interface_id': iid,
                            'name': iface.get('name') or ('接口#' + str(iid)),
                            'method': iface.get('method'),
                            'url': iface.get('url'),
                            'cases': cases,
                        })
                    executed = passed + failed
                    case_count = passed + failed + not_run
                    rate = round(passed / executed * 100, 2) if executed else 0
                    if executed == 0:
                        status = 'none'
                    elif failed == 0:
                        status = 'pass'
                    elif passed == 0:
                        status = 'fail'
                    else:
                        status = 'partial'
                    result.append({
                        'id': p['id'],
                        'name': p['name'],
                        'run_no': run_no,
                        'run_batch': rb,
                        'run_time': b['run_time'],
                        'interface_count': len(ids),
                        'case_count': case_count,
                        'executed': executed,
                        'passed': passed,
                        'failed': failed,
                        'not_run': not_run,
                        'success_rate': rate,
                        'status': status,
                        'enabled': p.get('enabled'),
                        'env_name': env_map.get(p.get('env_id')) or '未绑定',
                        'on_failure': p.get('on_failure'),
                        'trigger': rows[0].get('trigger') if rows else None,
                        'prepend_names': prepend_names_of(p, iface_name_map),
                        'prepend_details': _prepend_sections(db, p, rows_by_iface, iface_name_map),
                        'interfaces': iface_list,
                    })
            return {'code': 200, 'msg': 'success', 'data': result}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'计划报告统计失败: {str(e)}')


def _failure_of(row) -> dict:
    """从执行行解析失败原因，产出 (fail_type, fail_reason, failed_asserts)。

    fail_type 取值：
      pass    —— 通过
      assert  —— 断言失败（响应拿到了，但不满足期望；含 status_code 期望 2xx 实际非 2xx）
      error   —— 请求异常（网络/超时/URL 错误，请求根本没成功发出）
      not_run —— 未执行（前序失败已中止）
    注意：断言失败时 api_executions.error_msg 为空串，原因存在 assert_detail 里，
    所以必须解析 assert_detail 才能让报告看出「没通过到底为什么」。
    """
    success = 1 if row['success'] else 0
    if success == 1:
        return {'fail_type': 'pass', 'fail_reason': '', 'failed_asserts': []}
    assertion_result = row.get('assertion_result') or ''
    error_msg = row.get('error_msg') or ''
    # 前置接口依赖新增的两种结论：需在断言分支之前处理，否则会被当成普通断言失败，
    # 报告里会显示成「断言失败：依赖变量未获得…」而丢掉「未执行」的语义。
    if assertion_result == 'dep_skip':
        # 因依赖变量缺失被跳过 —— 请求根本没发出，不计入成功率分母（与 not_run 同口径）
        return {
            'fail_type': 'dep_skip',
            'fail_reason': error_msg or '依赖变量未获得，已跳过（未执行）',
            'failed_asserts': [],
        }
    if assertion_result == 'extract_fail':
        ad0 = _safe_json(row.get('assert_detail'))
        details0 = ad0.get('details') if isinstance(ad0, dict) else None
        asserts0 = [{
            'type': d.get('type') or '',
            'expected': d.get('expected'),
            'actual': d.get('actual'),
            'detail': d.get('detail') or '',
        } for d in (details0 or []) if isinstance(d, dict) and not d.get('passed')]
        return {
            'fail_type': 'extract_fail',
            'fail_reason': error_msg or '变量提取失败',
            'failed_asserts': asserts0,
        }
    if assertion_result == 'error':
        return {
            'fail_type': 'error',
            'fail_reason': '请求异常：' + (error_msg or '请求未成功发出（网络 / 超时 / URL 错误）'),
            'failed_asserts': [],
        }
    # 断言失败：展开 assert_detail.details，挑出未通过的断言
    ad = _safe_json(row.get('assert_detail'))
    details = ad.get('details') if isinstance(ad, dict) else None
    failed_asserts = []
    if isinstance(details, list):
        for d in details:
            if not isinstance(d, dict) or d.get('passed'):
                continue
            failed_asserts.append({
                'type': d.get('type') or '',
                'expected': d.get('expected'),
                'actual': d.get('actual'),
                'detail': d.get('detail') or '',
            })
    if failed_asserts:
        first = failed_asserts[0]
        tail = f'；另有 {len(failed_asserts) - 1} 条断言未通过' if len(failed_asserts) > 1 else ''
        reason = first['detail'] or (f"{first['type']} 期望 {first['expected']}，实际 {first['actual']}")
        fail_reason = '断言失败：' + reason + tail
    else:
        summary = ad.get('detail') if isinstance(ad, dict) else ''
        fail_reason = '断言失败：' + (summary or '响应未满足期望（断言明细缺失）')
    return {'fail_type': 'assert', 'fail_reason': fail_reason, 'failed_asserts': failed_asserts}


def _case_row(iface, iid, row, not_run_name, not_run=False):
    """构造报告详情中的单条用例(case)记录。row 为 None 表示未执行。"""
    if not_run:
        return {
            'interface_id': iid,
            'case_tag': not_run_name or '',
            'name': not_run_name or '默认',
            'success': None,
            'http_status': None,
            'elapsed_ms': None,
            'error_msg': '未执行（前序失败已中止 或 该用例未运行）',
            'fail_type': 'not_run',
            'fail_reason': '未执行（前序失败已中止 或 该用例未运行）',
            'failed_asserts': [],
            'execution_id': None,
            'not_run': True,
        }
    fail = _failure_of(row)
    return {
        'interface_id': iid,
        'case_tag': row.get('case_tag') or '',
        'name': row.get('case_tag') or '默认',
        'success': 1 if row['success'] else 0,
        'http_status': row['http_status'],
        'elapsed_ms': row['elapsed_ms'],
        'error_msg': row.get('error_msg') or '',
        'assertion_result': row.get('assertion_result') or '',
        'fail_type': fail['fail_type'],
        'fail_reason': fail['fail_reason'],
        'failed_asserts': fail['failed_asserts'],
        'execution_id': row['id'],
        'not_run': False,
    }


# ---------------------------------------------------------------------------
# 执行记录清理（A4）+ 接口批量导入（B2）
# ---------------------------------------------------------------------------
@router.post('/executions/cleanup')
async def cleanup_executions(data: dict, request: Request):
    """按保留天数清理执行记录：删除 run_time < NOW()-keep_days 的记录。
    keep_days 缺省 90，最小 1。返回删除条数（避免误删：仅删早于保留期的记录）。"""
    try:
        keep = int(data.get('keep_days') or 90)
    except Exception:
        keep = 90
    if keep < 1:
        keep = 1
    try:
        with get_db_handler() as db:
            cnt = db.execute_query(
                'SELECT COUNT(*) c FROM api_executions WHERE run_time < DATE_SUB(NOW(), INTERVAL %s DAY)',
                (keep,),
            )[0]['c'] or 0
            if cnt:
                db.execute_update(
                    'DELETE FROM api_executions WHERE run_time < DATE_SUB(NOW(), INTERVAL %s DAY)',
                    (keep,),
                )
            return {'code': 200, 'msg': 'success', 'data': {'deleted': cnt, 'keep_days': keep}}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'清理失败: {str(e)}')


async def cleanup_old_executions(days: int = 90) -> int:
    """被调度器每日调用：删除早于 keep_days 的执行记录，防止表无限膨胀（A4）。"""
    try:
        with get_db_handler() as db:
            db.execute_update(
                'DELETE FROM api_executions WHERE run_time < DATE_SUB(NOW(), INTERVAL %s DAY)',
                (days,),
            )
        return days
    except Exception as e:
        logger.warning(f'[清理] 自动清理执行记录失败: {e}')
        return 0


def _collect_postman(items, out):
    """递归遍历 Postman Collection v2.1 的 item 树，收集 request 为接口定义。"""
    for it in items or []:
        if it.get('item'):
            _collect_postman(it['item'], out)
            continue
        req = it.get('request')
        if not req:
            continue
        method = (req.get('method') or 'GET').upper()
        url = req.get('url')
        if isinstance(url, dict):
            url = url.get('raw') or ''
        if not url:
            continue
        # 去掉协议+域名，只保留 path（执行时按环境拼接 base_url）
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path or url
        if parsed.query:
            path = path + '?' + parsed.query
        headers = {}
        for h in req.get('header') or []:
            if h.get('key'):
                headers[h['key']] = h.get('value')
        body = None
        rb = req.get('body') or {}
        if isinstance(rb, dict) and rb.get('raw'):
            try:
                body = json.loads(rb['raw'])
            except Exception:
                body = rb['raw']
        out.append({
            'name': it.get('name') or (method + ' ' + path),
            'method': method, 'url': path,
            'headers': headers, 'params': {}, 'body': body,
        })


def _collect_swagger(spec, out):
    """遍历 OpenAPI 3 的 paths，每个 method 生成一条接口定义（url 仅保留 path）。"""
    paths = (spec or {}).get('paths') or {}
    for p, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in ('get', 'post', 'put', 'delete', 'patch'):
                continue
            headers, params = {}, {}
            for prm in op.get('parameters') or []:
                if prm.get('in') == 'header':
                    headers[prm.get('name')] = prm.get('schema', {}).get('example', '')
                elif prm.get('in') == 'query':
                    params[prm.get('name')] = prm.get('schema', {}).get('example', '')
            body = None
            rb = (op.get('requestBody') or {}).get('content') or {}
            for ct, cv in rb.items():
                ex = cv.get('example')
                sc = cv.get('schema')
                if ex is not None:
                    body = ex
                elif sc:
                    body = (sc.get('example') or sc.get('properties') or {})
                if body is not None:
                    break
            out.append({
                'name': (op.get('summary') or op.get('operationId') or f'{method.upper()} {p}'),
                'method': method.upper(), 'url': p,
                'headers': headers, 'params': params, 'body': body,
            })


# ---------------- JMeter .jmx 导入 ----------------
# .jmx 是 XML：<jmeterTestPlan><hashTree>…<HTTPSamplerProxy testname="…">…
# 解析成与 Postman/Swagger 相同的 6 字段字典，直接复用 import_interfaces 的去重 + INSERT。
_JMX_SAMPLER_TAGS = ('HTTPSamplerProxy', 'HTTPSampler')
_JMX_VAR_RE = re.compile(r'\$\{\s*([^}]+?)\s*\}')


def _jmx_vars(value):
    """JMeter 的 ${var} → 本系统的 {{var}}，执行时由 _render 渲染。"""
    if value is None:
        return ''
    return _JMX_VAR_RE.sub(r'{{\1}}', str(value))


def _jmx_prop(node, name, default=''):
    """取节点直接子级 <stringProp name="…"> 的文本。"""
    if node is None:
        return default
    el = node.find("./stringProp[@name='%s']" % name)
    return (el.text or '') if el is not None else default


def _jmx_bool_prop(node, name):
    el = node.find("./boolProp[@name='%s']" % name) if node is not None else None
    return el is not None and str(el.text or '').strip().lower() == 'true'


def _jmx_header_manager(node):
    """HeaderManager → {name: value}，忽略空请求头名。"""
    headers = {}
    for ep in node.findall("./collectionProp[@name='HeaderManager.headers']/elementProp"):
        k = _jmx_prop(ep, 'Header.name')
        if k:
            headers[k] = _jmx_vars(_jmx_prop(ep, 'Header.value'))
    return headers


def _jmx_headers_for(sampler, parent_map):
    """按 JMeter 作用域收集 HeaderManager：祖先 hashTree（全局/线程组级）→ sampler 直属（覆盖）。"""
    headers = {}
    ancestors = []
    node = sampler
    while node in parent_map:
        node = parent_map[node]
        ancestors.append(node)
    for h in reversed(ancestors):  # 由外到内，内层覆盖外层
        if h.tag == 'hashTree':
            for child in h:
                if child.tag == 'HeaderManager':
                    headers.update(_jmx_header_manager(child))
    # JMeter 里元件后紧跟一个同级 <hashTree> 作为它的子树，直属 HeaderManager 在这里
    parent = parent_map.get(sampler)
    if parent is not None:
        children = list(parent)
        try:
            idx = children.index(sampler)
        except ValueError:
            idx = -1
        if idx >= 0 and idx + 1 < len(children) and children[idx + 1].tag == 'hashTree':
            for child in children[idx + 1]:
                if child.tag == 'HeaderManager':
                    headers.update(_jmx_header_manager(child))
    return headers


def _jmx_normalize_path(path):
    """只保留 path(+query)：域名/协议由执行环境 base_url 拼接，不落到 url 列。"""
    p = _jmx_vars(path).strip()
    if not p:
        return ''
    m = re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://[^/]+(/.*)?$', p)
    if m:
        p = m.group(1) or '/'
    else:
        # {{host}}/api/x 这类域名变量前缀 → 去掉首段
        m2 = re.match(r'^\{\{[^}]+\}\}(/.*)$', p)
        if m2:
            p = m2.group(1)
    if not p.startswith('/'):
        p = '/' + p
    return p


def _jmx_params_body(sampler, method='GET'):
    """HTTPsampler.Arguments → (params, body)。
    postBodyRaw=true 时 Arguments 里只有一个空名参数，其 value 即原始请求体。
    ⚠️ postBodyRaw=false 时的 Arguments 语义取决于方法：
       · GET/HEAD  → URL query 参数（放 params）
       · POST/PUT/PATCH/DELETE → 表单/multipart 请求体（放 body）
         JMeter 的 DO_MULTIPART_POST=true 就是这种，若误放 params 会变成 URL query，
         请求体为空 → 服务端收不到参数。系统执行时 body 走 httpx json=，params 走 params=。
    """
    params, body = {}, None
    args_el = None
    for ep in sampler.findall('elementProp'):
        if ep.get('name') == 'HTTPsampler.Arguments':
            args_el = ep
            break
    args = []
    if args_el is not None:
        coll = args_el.find("./collectionProp[@name='Arguments.arguments']")
        if coll is not None:
            for ep in coll.findall('elementProp'):
                args.append((_jmx_prop(ep, 'Argument.name'), _jmx_prop(ep, 'Argument.value')))
    if _jmx_bool_prop(sampler, 'HTTPSampler.postBodyRaw'):
        raw_text = next((v for k, v in args if v and not k), '')
        if not raw_text and args:
            raw_text = args[0][1]
        raw_text = _jmx_vars(raw_text)
        if raw_text:
            try:
                body = json.loads(raw_text)
            except Exception:
                body = raw_text
    else:
        form = {}
        for k, v in args:
            if k:
                form[k] = _jmx_vars(v)
        if method in ('POST', 'PUT', 'PATCH', 'DELETE') and form:
            # JMeter 的 POST 表单/multipart 参数在请求体里，不是 URL query
            body = form
        else:
            params = form
    return params, body


def _collect_jmx(text, out):
    """解析 JMeter .jmx，把 HTTP Sampler 转成与 Postman/Swagger 一致的接口字典列表。"""
    if not text or not str(text).strip():
        return
    # Windows 下导出的 .jmx 可能带 UTF-8 BOM，会让 ElementTree 判定声明不在开头
    text = text.lstrip('\ufeff')
    if len(text) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail='JMX 文件过大（上限 10MB）')
    if '<!DOCTYPE' in text[:4096].upper():
        raise HTTPException(status_code=400, detail='JMX 文件不支持 DOCTYPE')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise HTTPException(status_code=400, detail='JMX 不是合法 XML: %s' % e)
    if root.tag.split('}')[-1] != 'jmeterTestPlan':
        raise HTTPException(status_code=400, detail='不是 JMeter 测试计划（根元素应为 jmeterTestPlan）')
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for sampler in root.iter():
        if sampler.tag.split('}')[-1] not in _JMX_SAMPLER_TAGS:
            continue
        if str(sampler.get('enabled') or 'true').strip().lower() == 'false':
            continue
        method = (_jmx_prop(sampler, 'HTTPSampler.method') or 'GET').upper()
        path = _jmx_normalize_path(_jmx_prop(sampler, 'HTTPSampler.path'))
        if not path:
            continue
        params, body = _jmx_params_body(sampler, method)
        out.append({
            'name': sampler.get('testname') or (method + ' ' + path),
            'method': method, 'url': path,
            'headers': _jmx_headers_for(sampler, parent_map),
            'params': params, 'body': body,
        })


@router.post('/interfaces/import')
async def import_interfaces(data: dict, request: Request):
    """批量导入接口（B2）。data: { source: 'postman'|'swagger'|'json'|'jmx', content: str|dict|list }
    jmx 的 content 是 .jmx 的 **XML 原文**（不走 JSON 解析），其余来源为 JSON 字符串/对象。
    按 url+method 去重，已存在则跳过。返回导入/跳过数量。"""
    source = (data.get('source') or '').lower()
    content = data.get('content')
    if content is None:
        raise HTTPException(status_code=400, detail='缺少 content')
    if source == 'jmx':
        # JMX 是 XML，绝不能走下面的 json.loads
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail='JMX content 必须是 .jmx 文件文本')
    elif isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            raise HTTPException(status_code=400, detail='content 不是合法 JSON')
    raw = []
    if source == 'postman':
        _collect_postman((content or {}).get('item') or [], raw)
    elif source == 'swagger':
        _collect_swagger(content, raw)
    elif source == 'json':
        raw = content if isinstance(content, list) else []
    elif source == 'jmx':
        _collect_jmx(content, raw)
    else:
        raise HTTPException(status_code=400, detail='未知 source（支持 postman/swagger/json/jmx）')
    if not raw:
        return {'code': 200, 'msg': 'success', 'data': {'imported': 0, 'skipped': 0}}

    try:
        with get_db_handler() as db:
            existing = {
                (r['method'], r['url'])
                for r in db.execute_query('SELECT method, url FROM api_interfaces')
            }
            uid = _current_uid(request)
            imported = skipped = 0
            for it in raw:
                method = (it.get('method') or 'GET').upper()
                url = (it.get('url') or '').strip()
                if not url:
                    skipped += 1
                    continue
                if (method, url) in existing:
                    skipped += 1
                    continue
                db.execute_update(
                    """
                    INSERT INTO api_interfaces
                        (name, method, url, headers, params, body, assert_type,
                         expected_value, enabled, verify_ssl, create_by, create_time, update_time)
                    VALUES (%s, %s, %s, %s, %s, %s, 'status_code', '200', 1, 1, %s, NOW(), NOW())
                    """,
                    (
                        it.get('name') or f'{method} {url}',
                        method, url,
                        _dump_json(it.get('headers') or {}),
                        _dump_json(it.get('params') or {}),
                        _dump_json(it.get('body') if it.get('body') is not None else {}),
                        uid,
                    ),
                )
                existing.add((method, url))
                imported += 1
            return {'code': 200, 'msg': 'success', 'data': {'imported': imported, 'skipped': skipped}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'导入失败: {str(e)}')


# ---------------------------------------------------------------------------
# Excel 导入 / 模板下载（接口自动化）
# ---------------------------------------------------------------------------
def _build_interface_column_map(columns) -> dict:
    """将 Excel 列头映射到标准接口字段，兼容多种模板写法（中文/英文/带空格）"""
    aliases = {
        'name': ['接口名称', '名称', 'name', 'apiname', '接口'],
        'method': ['方法', '请求方法', 'method', 'httpmethod'],
        'url': ['url', '路径', '接口路径', 'apiurl'],
        'group': ['分组', '分组名', 'group', 'category'],
        'enabled': ['启用', '是否启用', 'enabled', '状态'],
        'headers': ['headers', '请求头', '请求头部'],
        'params': ['params', '参数', '查询参数', 'queryparams'],
        'body': ['body', '请求体', '请求正文'],
        'assert_type': ['断言类型', 'asserttype', '断言'],
        'expected_value': ['期望值', '预期值', 'expected', 'expectedvalue'],
        'timeout': ['超时', '超时秒', 'timeout', '超时时间'],
        'retry': ['失败重试', '重试', 'retry'],
    }
    col_map = {}
    for col in columns:
        n = str(col).replace('\n', '').replace(' ', '').replace('（', '(').replace('）', ')').lower()
        for field, al in aliases.items():
            if n in al or any(a.replace(' ', '').lower() in n for a in al):
                col_map.setdefault(field, col)
                break
    return col_map


def _is_number(v) -> bool:
    if v is None:
        return False
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def _normalize_enabled(value) -> int:
    if value is None:
        return 1
    s = str(value).strip().upper()
    if s in ('1', 'TRUE', '启用', '是', 'Y', 'YES'):
        return 1
    if s in ('0', 'FALSE', '禁用', '否', 'N', 'NO'):
        return 0
    try:
        return 1 if int(float(value)) else 0
    except (ValueError, TypeError):
        return 1


def _coerce_json(value):
    """Excel 单元格可能是字符串 JSON 或空；空返回 {}，非法 JSON 也返回 {}（宽松解析，避免整行失败）"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    s = str(value).strip()
    if not s:
        return {}
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, (dict, list)) else {}
    except Exception:
        return {}


@router.post('/interfaces/import-excel')
async def import_interfaces_excel(request: Request, file: UploadFile = File(...)):
    """从 Excel(.xlsx/.xls)批量导入接口，按 url+method 去重，已存在跳过。返回 {imported, skipped}。"""
    try:
        if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls', '.xlsm')):
            raise HTTPException(status_code=400, detail='仅支持 Excel 文件(.xlsx/.xls)')
        contents = await file.read()
        na_values = ['', 'NA', 'N/A', 'NaN', 'null', 'NULL']
        raw = pd.read_excel(io.BytesIO(contents), sheet_name=None, na_values=na_values, keep_default_na=True)
        if isinstance(raw, dict):
            df = pd.concat([v for v in raw.values() if not v.empty], ignore_index=True)
        else:
            df = raw
        if df.empty or len(df.columns) == 0:
            return {'code': 200, 'msg': 'success', 'data': {'imported': 0, 'skipped': 0}}
        col_map = _build_interface_column_map(df.columns)
        if 'url' not in col_map:
            raise HTTPException(status_code=400, detail='Excel 缺少必要列「URL」（表头可写 URL / 路径 / 接口路径）')
        if 'name' not in col_map:
            raise HTTPException(status_code=400, detail='Excel 缺少必要列「接口名称」')
        records = []
        for _, row in df.iterrows():
            records.append({f: (row[col_map[f]] if f in col_map else None) for f in col_map})
        with get_db_handler() as db:
            existing = {
                (r['method'], r['url'])
                for r in db.execute_query('SELECT method, url FROM api_interfaces')
            }
            uid = _current_uid(request)
            imported = skipped = 0
            for rec in records:
                method = (str(rec.get('method') or 'GET')).strip().upper()
                url = (str(rec.get('url') or '')).strip()
                name = (str(rec.get('name') or '')).strip()
                if not url or not name:
                    skipped += 1
                    continue
                if (method, url) in existing:
                    skipped += 1
                    continue
                db.execute_update(
                    """
                    INSERT INTO api_interfaces
                        (name, method, url, headers, params, body, assert_type,
                         expected_value, enabled, verify_ssl, extracts, asserts,
                         timeout, retry, dataset, `group`, create_by, create_time, update_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (
                        name,
                        method, url,
                        _dump_json(_coerce_json(rec.get('headers'))),
                        _dump_json(_coerce_json(rec.get('params'))),
                        _dump_json(_coerce_json(rec.get('body'))),
                        (str(rec.get('assert_type') or 'status_code')).strip() or 'status_code',
                        (str(rec.get('expected_value') or '200')).strip() or '200',
                        _normalize_enabled(rec.get('enabled')),
                        1,
                        _dump_json([]),
                        _dump_json([]),
                        int(float(rec.get('timeout'))) if _is_number(rec.get('timeout')) else 10,
                        int(float(rec.get('retry'))) if _is_number(rec.get('retry')) else 0,
                        _dump_json([]),
                        (str(rec.get('group') or '')).strip(),
                        uid,
                    ),
                )
                existing.add((method, url))
                imported += 1
            return {'code': 200, 'msg': 'success', 'data': {'imported': imported, 'skipped': skipped}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'Excel 导入失败: {str(e)}')


@router.get('/interfaces/import-template')
async def interface_import_template():
    """生成标准列头的空白 Excel 导入模板并返回文件流"""
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '接口导入'
        headers = ['接口名称', '方法', 'URL', '分组', '启用', 'Headers', 'Params', 'Body',
                   '断言类型', '期望值', '超时秒', '失败重试']
        ws.append(headers)
        ws.append(['登录接口', 'POST', '/merapp/login', '用户中心', '启用',
                   '{"Authorization":"Bearer xxx"}', '', '{"username":"admin","password":"123456"}',
                   'status_code', '200', '10', '0'])
        ws.freeze_panes = 'A2'
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename=interface_import_template.xlsx'},
        )
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'生成模板失败: {str(e)}')


# ---------------------------------------------------------------------------
# 环境管理（base_url 抽离，多环境切换）
# ---------------------------------------------------------------------------
@router.get('/environments')
async def list_environments():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, name, base_url, headers, variables, '
                'create_by, create_time, update_time FROM api_environments '
                'ORDER BY id'
            )
            return {'code': 200, 'msg': 'success', 'data': rows}
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'查询环境失败: {str(e)}')


@router.post('/environments')
async def create_environment(data: dict, request: Request):
    try:
        name = (data.get('name') or '').strip()
        if not name:
            raise HTTPException(status_code=400, detail='环境名称必填')
        with get_db_handler() as db:
            db.execute_update(
                """
                INSERT INTO api_environments
                    (name, base_url, headers, variables, create_by,
                     create_time, update_time)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    name, data.get('base_url', '') or '',
                    _dump_json(_safe_json(data.get('headers'))),
                    _dump_json(_safe_json(data.get('variables'))),
                    _current_uid(request),
                ),
            )
            return {'code': 200, 'msg': 'success', 'data': None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'创建环境失败: {str(e)}')


@router.put('/environments')
async def update_environment(data: dict, request: Request):
    try:
        env_id = data.get('id')
        if not env_id:
            raise HTTPException(status_code=400, detail='缺少 id')
        with get_db_handler() as db:
            db.execute_update(
                """
                UPDATE api_environments
                SET name=%s, base_url=%s, headers=%s, variables=%s,
                    update_time=NOW()
                WHERE id=%s
                """,
                (
                    data.get('name', '') or '',
                    data.get('base_url', '') or '',
                    _dump_json(_safe_json(data.get('headers'))),
                    _dump_json(_safe_json(data.get('variables'))),
                    env_id,
                ),
            )
            return {'code': 200, 'msg': 'success', 'data': None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'更新环境失败: {str(e)}')


@router.delete('/environments')
async def delete_environment(data: dict):
    try:
        env_id = data.get('id')
        if not env_id:
            raise HTTPException(status_code=400, detail='缺少 id')
        with get_db_handler() as db:
            db.execute_update('DELETE FROM api_environments WHERE id=%s', (env_id,))
            return {'code': 200, 'msg': 'success', 'data': None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'删除环境失败: {str(e)}')
