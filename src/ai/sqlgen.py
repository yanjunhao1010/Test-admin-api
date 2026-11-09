"""自定义 SQL 数据生成：INSERT 语句模板的解析与拼装。

设计要点（为什么不让 AI 直接生成 SQL）：

1. 一条 INSERT 可能有 30+ 个字段，其中还会出现含逗号、含转义引号的 JSON 字符串
   （例：'["https://a.jpg", "https://b.jpg"]'）。让模型"照抄模板再改几个值"，
   必然出现字段错位、漏字段、引号被吃掉、JSON 被切断等事故。
2. 所以这里把职责切开：
   - **本地确定性**：解析模板（表名 / 字段 / 每个字面量的类型与原样文本），
     以及最终 SQL 的拼装与转义。
   - **AI 只做两件小事**：读懂自然语言要求，判定哪些字段要变；再为这些字段产出新值。
   - 拼装时"不变的字段"直接复用模板里的**原样字面量文本**，一个字符都不动，
     保证生成的 SQL 与用户给的模板口径完全一致。

对外接口：
- parse_insert(sql)                  -> 解析结果（表名 / 字段清单 / 头部片段）
- describe_fields(parsed)            -> 给模型看的字段说明文本
- assemble_rows(parsed, change_set, rows_values, merge) -> 完整 INSERT 语句列表
"""
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class SqlGenError(ValueError):
    """SQL 模板无法解析 / 无法拼装。"""


# ---------------------------------------------------------------------------
# 词法：引号感知
# ---------------------------------------------------------------------------
def _skip_quoted(s: str, i: int, quote: str) -> int:
    """s[i] 是引号开头，返回引号闭合后的下标（不含）。

    - 反斜杠转义（MySQL 默认模式）：\' \" \\ 等，下一个字符一律跳过。
    - 双写转义（SQL 标准）：'' 与 "" 视为一个引号。
    - 反引号标识符支持 `` 双写。
    """
    n = len(s)
    j = i + 1
    while j < n:
        c = s[j]
        if c == '\\' and quote != '`':
            j += 2
            continue
        if c == quote:
            if j + 1 < n and s[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    raise SqlGenError('SQL 中存在未闭合的引号（%s）' % quote)


def _split_top_level(text: str) -> List[str]:
    """按顶层逗号切分，忽略引号内 / 括号内的逗号。"""
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ("'", '"', '`'):
            j = _skip_quoted(text, i, c)
            buf.append(text[i:j])
            i = j
            continue
        if c == '(':
            depth = 0
            j = i
            while j < n:
                cc = text[j]
                if cc in ("'", '"', '`'):
                    j = _skip_quoted(text, j, cc)
                    continue
                if cc == '(':
                    depth += 1
                elif cc == ')':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if c == ',':
            parts.append(''.join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_matching_paren(s: str, open_idx: int) -> int:
    """返回与 s[open_idx]=='(' 配对的 ')' 下标；找不到返回 -1。"""
    depth = 0
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"', '`'):
            i = _skip_quoted(s, i, c)
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# 字面量：解析与渲染
# ---------------------------------------------------------------------------
# MySQL 默认转义表。\% 和 \_ 例外：MySQL 读它们时保留反斜杠本身。
_ESCAPE_MAP = {
    '0': '\x00', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t',
    'Z': '\x1a', '\\': '\\', "'": "'", '"': '"',
    '%': '\\%', '_': '\\_',
}

_NUMBER_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')


def _unescape(raw: str) -> str:
    """把单引号字符串内部内容还原成真实值。"""
    out: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == '\\' and i + 1 < n:
            out.append(_ESCAPE_MAP.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        if c == "'" and i + 1 < n and raw[i + 1] == "'":
            out.append("'")
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _escape(raw: str) -> str:
    """真实值 -> 可安全放进单引号字符串的内部内容。"""
    return raw.replace('\\', '\\\\').replace("'", "''")


def parse_literal(token: str) -> Tuple[str, Any]:
    """解析一个 VALUES 里的字面量，返回 (kind, value)。

    kind: 'null' | 'number' | 'string' | 'raw'
    """
    t = (token or '').strip()
    if not t:
        return 'string', ''
    if t.upper() == 'NULL':
        return 'null', None
    if t[0] == "'" and len(t) >= 2 and t[-1] == "'":
        return 'string', _unescape(t[1:-1])
    if t[0] == '"' and len(t) >= 2 and t[-1] == '"':
        return 'string', _unescape(t[1:-1])
    if _NUMBER_RE.match(t):
        if re.search(r'[.eE]', t):
            return 'number', float(t)
        return 'number', int(t)
    # 函数 / 表达式等原样保留
    return 'raw', t


def render_value(kind: str, value: Any, template_value: Any = None) -> str:
    """把新值渲染成 SQL 字面量文本，尽量沿用模板该字段的书写风格。

    - 模板是字符串 -> 新值加引号并转义（数字也加引号，保持 '29' 这种口径）
    - 模板是数字   -> 新值尽量输出为裸数字
    - 模板是 NULL  -> 新值为 None 输出 NULL，否则按内容类型输出
    """
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        if kind == 'string':
            return "'%s'" % _escape(str(value))
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (list, dict)):
        # JSON 列：模型返回结构化数据 -> 序列化成字符串再落库
        text = json.dumps(value, ensure_ascii=False)
        return "'%s'" % _escape(text)
    text = str(value)
    if kind == 'number':
        if _NUMBER_RE.match(text.strip()):
            return text.strip()
        return "'%s'" % _escape(text)
    if kind == 'null':
        # 原本是 NULL 的列：新值是数字就输出裸数字，否则按字符串
        if _NUMBER_RE.match(text.strip()):
            return text.strip()
        return "'%s'" % _escape(text)
    return "'%s'" % _escape(text)


# ---------------------------------------------------------------------------
# 解析 INSERT 模板
# ---------------------------------------------------------------------------
_INSERT_RE = re.compile(
    r'^\s*INSERT\s+(?:IGNORE\s+)?INTO\s+'
    r'(?P<table>(?:`[^`]+`(?:\s*\.\s*`[^`]+`)?)|(?:"[^"]+"\s*\.\s*"[^"]+")|'
    r'(?:[A-Za-z_][\w$]*\s*\.\s*)?[A-Za-z_][\w$]*)\s*\(',
    re.IGNORECASE,
)


def _clean_ident(name: str) -> str:
    name = (name or '').strip()
    if name.startswith('`') and name.endswith('`') and len(name) >= 2:
        return name[1:-1].replace('``', '`')
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        return name[1:-1].replace('""', '"')
    return name


def _table_display(table: str) -> str:
    """把 `` `db`.`tbl` `` 还原成 `` db.tbl `` 这样的可读名字。"""
    parts = [p for p in re.split(r'\s*\.\s*', table.strip()) if p]
    return '.'.join(_clean_ident(p) for p in parts)


def parse_insert(sql: str) -> Dict[str, Any]:
    """解析 INSERT INTO ... (cols) VALUES (...) 模板。"""
    if not sql or not sql.strip():
        raise SqlGenError('请先粘贴 INSERT 语句模板')
    text = sql.strip()
    if text.endswith(';'):
        text = text[:-1].rstrip()

    m = _INSERT_RE.match(text)
    if not m:
        # 区分"没有字段列表"与"根本不是 INSERT"，前者给出更准的提示
        if re.match(r'^\s*INSERT\s+(?:IGNORE\s+)?INTO\s+[^\s(]+\s+VALUES\b', text, re.IGNORECASE):
            raise SqlGenError('这条 INSERT 没有字段列表，无法按字段区分"不变/要变"。'
                              '请补成 INSERT INTO `表名` (`字段1`, `字段2`) VALUES (...) 的形式')
        raise SqlGenError('无法识别 INSERT 语句：需要形如 '
                          'INSERT INTO `表名` (`字段1`, `字段2`) VALUES (...) 的单条插入语句')

    table_raw = m.group('table')
    cols_open = m.end() - 1
    cols_close = _find_matching_paren(text, cols_open)
    if cols_close == -1:
        raise SqlGenError('字段列表的括号没有闭合')
    cols_text = text[cols_open + 1:cols_close]

    rest = text[cols_close + 1:]
    vm = re.search(r'\bVALUES\b', rest, re.IGNORECASE)
    if not vm:
        raise SqlGenError('INSERT 语句里找不到 VALUES 关键字')
    head = text[:cols_close + 1 + vm.end()]
    after_values = rest[vm.end():]

    # 取第一条 VALUES 元组作为模板行
    tup_open = after_values.find('(')
    if tup_open == -1:
        raise SqlGenError('VALUES 后面找不到数据行')
    tup_close = _find_matching_paren(after_values, tup_open)
    if tup_close == -1:
        raise SqlGenError('VALUES 数据行的括号没有闭合')
    tuple_text = after_values[tup_open + 1:tup_close]

    raw_values = _split_top_level(tuple_text)
    col_names = [_clean_ident(c) for c in _split_top_level(cols_text)]
    if not col_names:
        raise SqlGenError('INSERT 语句没有字段列表，无法按字段区分"不变/要变"')
    if len(raw_values) != len(col_names):
        raise SqlGenError('字段数量(%d)与 VALUES 中的值数量(%d)不一致，模板本身有问题'
                          % (len(col_names), len(raw_values)))

    columns: List[Dict[str, Any]] = []
    for name, raw in zip(col_names, raw_values):
        kind, value = parse_literal(raw)
        columns.append({
            'name': name,
            'kind': kind,              # null / number / string / raw
            'value': value,            # 解析后的真实值（None / 数字 / 字符串）
            'literal': raw,            # 原样字面量文本：不变字段直接复用它
        })

    return {
        'table': _table_display(table_raw),
        'table_sql': table_raw,
        'columns': columns,
        'head': head,                 # "INSERT INTO `t` (`a`, `b`) VALUES"
        'sql': text,
    }


def describe_fields(parsed: Dict[str, Any], limit: int = 60) -> str:
    """生成给模型看的字段说明文本。"""
    lines: List[str] = []
    for col in parsed['columns']:
        val = col['value']
        if val is None:
            shown = 'NULL'
        else:
            shown = str(val)
            if len(shown) > limit:
                shown = shown[:limit] + '…'
            shown = shown.replace('\n', '\\n')
        lines.append('- %s（类型：%s；当前值：%s）' % (col['name'], col['kind'], shown))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 拼装
# ---------------------------------------------------------------------------
def assemble_rows(parsed: Dict[str, Any],
                  change_set: Set[str],
                  rows_values: Sequence[Dict[str, Any]],
                  merge: bool = False) -> Tuple[List[str], List[str]]:
    """把 AI 产出的"变化字段值"拼回完整 INSERT 语句。

    返回 (sql 列表, 回退说明列表)。回退说明用于告诉用户"哪一行哪个字段
    模型没给值，已按模板原值补齐"——宁可保守补齐，也不要静默造错数据。

    不变的字段：直接复用模板里的原样字面量文本，一个字符都不改。
    """
    columns = parsed['columns']
    head = parsed['head']
    lowered = {c['name'].strip().lower(): c['name'] for c in columns}
    tuples: List[str] = []
    fallbacks: List[str] = []

    for idx, rv in enumerate(rows_values, 1):
        rv = rv if isinstance(rv, dict) else {}
        rv_lower = {str(k).strip().lower(): v for k, v in rv.items()}
        literals: List[str] = []
        for col in columns:
            name = col['name']
            if name not in change_set:
                literals.append(col['literal'])
                continue
            if name in rv:
                literals.append(render_value(col['kind'], rv[name], col['value']))
                continue
            alt = lowered.get(name.strip().lower())
            if alt and alt.lower() in rv_lower:
                literals.append(render_value(col['kind'], rv_lower[alt.lower()], col['value']))
                continue
            literals.append(col['literal'])
            fallbacks.append('第 %d 行的「%s」模型未给出值，已按模板原值补齐' % (idx, name))
        tuples.append('(%s)' % ', '.join(literals))

    if merge:
        return ([head + '\n' + ',\n'.join(tuples) + ';'], fallbacks)
    return ([head + '\n' + t + ';' for t in tuples], fallbacks)


def build_field_payload(parsed: Dict[str, Any],
                        change_fields: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整理发给模型的"待生成字段"清单（含模板原值，供模型对齐格式）。

    ⚠️ 字段名键是 'field'（计划项的字段名），不是 'name' ——
    写错成 'name' 会让清单恒为空，模型收到空字段说明后只能返回空对象。
    这里两个键都认，兼容调用方传法。
    """
    by_name = {c['name']: c for c in parsed['columns']}
    payload: List[Dict[str, Any]] = []
    for item in change_fields or []:
        name = (item.get('field') or item.get('name') or '').strip()
        if not name:
            continue
        col = by_name.get(name)
        if col is None:
            continue
        sample = col['value']
        payload.append({
            'name': name,
            'kind': col['kind'],
            'sample': None if sample is None else str(sample),
            'rule': (item.get('rule') or '').strip(),
        })
    return payload
