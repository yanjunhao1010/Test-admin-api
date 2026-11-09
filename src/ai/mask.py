"""外发文本脱敏。

在把需求正文 / 用例内容 / 响应快照发给第三方模型之前，把敏感信息替换成占位符。
设计取舍：
- 默认只做「打码」，不做回填（占位符留在结果里），安全优先；开关见 ai_settings.ai_mask_refill。
- 同一敏感值复用同一个占位符编号（{{PHONE_1}}），保住「多处引用同一对象」的语义。
- 正则不可能 100% 覆盖（业务自造 ID 识别不了），所以页面要显式提示「已脱敏 N 处」。
"""
import json
import re
from typing import List, Optional, Tuple

def _luhn_ok(num: str) -> bool:
    """Luhn 校验：只有真正像银行卡号的才脱敏。

    没有这道校验时，形如 f73ae1234567890123 的业务图片 ID 会被 16~19 位数字规则
    误伤成 {{BANKCARD_1}} —— AI 看到占位符会误判成「变量未替换 / 数据依赖缺失」（踩过）。
    """
    digits = [int(c) for c in num if c.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# (正则, 占位符标签, 校验函数或 None)
BUILTIN_RULES = [
    (r'(?<!\d)1[3-9]\d{9}(?!\d)', 'PHONE', None),
    # 身份证：6 位地区码 + 合法出生日期 + 3 位顺序码 + 校验位，避免把 18 位业务 ID 误判
    (r'(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dXx])',
     'IDCARD', None),
    (r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', 'EMAIL', None),
    (r'(?<!\d)\d{16,19}(?!\d)', 'BANKCARD', _luhn_ok),
    (r'(sk|ak|pk)-[A-Za-z0-9_\-]{12,}', 'APIKEY', None),
    (r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b', 'PRIVATE_IP', None),
]


def _parse_rules(rules_json: Optional[str]) -> Optional[List[Tuple[str, str]]]:
    """自定义规则 JSON：[{"p": "正则", "r": "标签"}]；非法则返回 None 用内置规则"""
    if not rules_json:
        return None
    try:
        data = json.loads(rules_json)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    rules = []
    for item in data:
        if isinstance(item, dict) and item.get('p'):
            rules.append((item['p'], item.get('r') or 'MASK', None))
    return rules or None


def mask_text(text: str, rules_json: Optional[str] = None) -> Tuple[str, int]:
    """返回 (脱敏后文本, 命中的敏感值个数)。text 为空原样返回。"""
    if not text:
        return text, 0

    rules = _parse_rules(rules_json) or BUILTIN_RULES
    mapping = {}          # 原值 → 占位符
    counter = {}          # 标签 → 已编号数量

    def make_replacer(label: str, validator=None):
        def _repl(m):
            val = m.group(0)
            if validator is not None and not validator(val):
                return val          # 校验不过（如不像银行卡号）原样保留，避免误伤业务 ID
            if val in mapping:
                return mapping[val]
            counter[label] = counter.get(label, 0) + 1
            placeholder = '{{%s_%d}}' % (label, counter[label])
            mapping[val] = placeholder
            return placeholder

        return _repl

    out = text
    for rule in rules:
        pattern, label = rule[0], rule[1]
        validator = rule[2] if len(rule) > 2 else None
        try:
            out = re.sub(pattern, make_replacer(label, validator), out)
        except re.error:
            # 自定义正则非法时跳过该条，不影响其它规则
            continue
    return out, len(mapping)


def mask_obj(obj, rules_json: Optional[str] = None):
    """递归脱敏 dict/list/str（响应快照等结构化数据用）。返回 (新对象, 命中数)"""
    total = 0
    if isinstance(obj, str):
        out, n = mask_text(obj, rules_json)
        return out, n
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            nv, n = mask_obj(v, rules_json)
            new[k] = nv
            total += n
        return new, total
    if isinstance(obj, list):
        new_list = []
        for v in obj:
            nv, n = mask_obj(v, rules_json)
            new_list.append(nv)
            total += n
        return new_list, total
    return obj, 0
