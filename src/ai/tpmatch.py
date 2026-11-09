# -*- coding: utf-8 -*-
"""用例 → 测试点 的自动判定（零成本优先，AI 兜底）。

## 背景

「产品功能点 ↔ 正式用例库」关联时，关联本身只是一条桥表记录；
真正决定**测试点覆盖率**的是这条关联覆盖了功能点下哪几个测试点
（`product_point_cases.tp_indexes`）。此前只能人工逐条下拉勾选，
功能点动辄二三十个测试点、一次关联十几条用例，工作量不可接受。

本模块提供**三层判定**，按成本从低到高依次尝试，能零成本解决的绝不调模型：

### 第 1 层：回标反查（零成本、精确）
AI 生成的用例在草稿阶段就带 `tp_refs`（形如 `['TP3']`），
指向它在**来源需求**里的测试点编号。功能点的测试点也存了 `tp_ref`，
两者求交集即可精确反查「这条用例覆盖本功能点的第几个测试点」。
命中即 100% 准确，零模型调用。

⚠️ 反查必须**按需求分组**：`tp_ref` 是「需求内原始下标+1」，
不同需求都从 TP1 开始，全局扫会张冠李戴。因此只拿
「与该功能点同需求」的草稿来做交集。

### 第 2 层：标题相似度（零成本、瞬时）
把「用例标题」和「测试点标题」做**字符 bigram Dice 系数**比对。

实测（11/02，真实库 10 个功能点 / 28 条用例）：
- AI 生成的用例，其标题与对应测试点标题**逐字相同** → Dice = 1.000，
  且与次优项差距 0.55~0.84；
- 不属于该功能点的用例，最高只有 0.417，且与次优项差距 < 0.07。

所以判据是 **「绝对分高 + 与次优拉开差距」双条件**，只看绝对分会误判
（不同模块的用例也能蹭到 0.4）。相似度只能作为**建议**，需人工确认。

### 第 3 层：AI 判定（消耗额度，最后兜底）
前两层都定不下来的用例，交给模型判断：把该功能点的测试点清单
（含标题 + 覆盖维度）+ 用例正文喂给模型，让它回标测试点编号。
回标结果**必须做白名单校正**（只保留清单内真实存在的编号），
模型爱自己编 `TP99`，不校正就会造出反查不到的死编号。

## 三层的置信度语义

| 层级 | source | confidence | 含义 |
|---|---|---|---|
| 1 | `tp_ref` | 1.0 | 回标反查精确命中，**零成本可信** |
| 2 | `similar` | 0~1 | 标题相似度建议分，**需人工确认** |
| 3 | `ai` | 模型给分 | AI 判定，**需人工确认** |

本模块**只产出建议，不写库**。写入由调用方（router）在用户确认后执行。
"""
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 标题相似度阈值（基于 11/02 真实库实测分布）
#   精确命中：Dice ≥ 0.95（逐字相同或极近）
#   建议命中：Dice ≥ 0.45 且与次优拉开 ≥ 0.15（否则不区分得开，不敢建议）
EXACT_SIM = 0.95
SUGGEST_SIM = 0.45
MIN_GAP = 0.15


def _norm_text(s: Any) -> str:
    """归一化：只保留字母数字与中文（去掉标点/空格/换行），用于 bigram 比对。

    标点和空格在中文用例里差异很大（"「申请汇付」" vs "申请汇付"），
    去掉后能让同义写法更容易对齐；全角数字也统一不了，先只做这一层。
    """
    if s is None:
        return ''
    out = []
    for ch in str(s):
        if ch.isalnum():
            out.append(ch)
    return ''.join(out)


def _bigrams(s: str) -> set:
    """字符 bigram 集合。中文没有空格分词，bigram 是最省事又够用的近似。"""
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dice(a: set, b: set) -> float:
    """Dice 系数：2|A∩B| / (|A|+|B|)，取值 0~1。"""
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def similarity(title_a: Any, title_b: Any) -> float:
    """两个标题的相似度（0~1）。"""
    a = _bigrams(_norm_text(title_a))
    b = _bigrams(_norm_text(title_b))
    return dice(a, b)


def parse_ref_list(raw: Any) -> List[str]:
    """解析回标编号列表：兼容 JSON 数组字符串 / list / 逗号分隔串。

    只认 `TP+数字` 形态，认不出来的一律丢弃 —— 存一个不存在的编号
    比不标更糟（会造出反查不到的死编号）。
    """
    if raw is None:
        return []
    items: List[Any] = []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith('['):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = []
            except Exception:
                items = []
        if not items:
            items = [x for x in re.split(r'[,，;；\s、|]+', s) if x]
    out: List[str] = []
    for x in items:
        if isinstance(x, (int, float)):
            txt = 'TP%d' % int(x)
        else:
            m = re.match(r'^TP\s*0*(\d{1,4})$', str(x).strip(), re.IGNORECASE)
            if not m:
                continue
            txt = 'TP%d' % int(m.group(1))
        if txt not in out:
            out.append(txt)
    return out


def match_by_tp_ref(
    case_tp_refs: Any,
    tp_items: Sequence[Dict[str, Any]],
) -> List[int]:
    """第 1 层：回标反查。

    `case_tp_refs`  : 该用例草稿上的回标编号（如 ['TP3']）
    `tp_items`      : 功能点下的测试点（每项含 'tp_ref'，形如 'TP3'）

    返回命中的测试点在**功能点内**的下标列表（0 起，与 tp_indexes 对齐）。

    ⚠️ 只能匹配**本功能点自己有的编号** —— 调用方必须保证
    `case_tp_refs` 已经按 requirement_id 过滤过，否则跨需求重号会指错。
    """
    refs = set(parse_ref_list(case_tp_refs))
    if not refs:
        return []
    out: List[int] = []
    for i, tp in enumerate(tp_items or []):
        ref = _norm_ref(tp.get('tp_ref') if isinstance(tp, dict) else '')
        if ref and ref in refs:
            out.append(i)
    return out


def _norm_ref(raw: Any) -> str:
    m = re.match(r'^TP\s*0*(\d{1,4})$', str(raw or '').strip(), re.IGNORECASE)
    return 'TP%d' % int(m.group(1)) if m else ''


def match_by_similarity(
    case_title: Any,
    tp_items: Sequence[Dict[str, Any]],
) -> Tuple[List[int], float, float]:
    """第 2 层：标题相似度。

    返回 (建议下标列表, 最高相似度, 与次优的差距)。

    判据是**双条件**：最高分够高 **且** 与次优拉开差距。
    只看绝对分会把「别的模块的用例」也蹭进来（实测能到 0.417）。

    - 最高分 ≥ EXACT_SIM → 视为精确命中（逐字相同），直接返回该下标
    - 最高分 ≥ SUGGEST_SIM 且差距 ≥ MIN_GAP → 作为建议返回（需人工确认）
    - 否则返回空（交给下一层）
    """
    items = tp_items or []
    if not items:
        return [], 0.0, 0.0
    tb = _bigrams(_norm_text(case_title))
    if not tb:
        return [], 0.0, 0.0
    scores: List[Tuple[float, int]] = []
    for i, tp in enumerate(items):
        title = tp.get('title', '') if isinstance(tp, dict) else str(tp)
        scores.append((similarity(case_title, title), i))
    scores.sort(key=lambda x: (-x[0], x[1]))
    best_score, best_idx = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    gap = best_score - second
    if best_score >= EXACT_SIM:
        return [best_idx], best_score, gap
    if best_score >= SUGGEST_SIM and gap >= MIN_GAP:
        return [best_idx], best_score, gap
    return [], best_score, gap


def clamp_indexes(indexes: Any, tp_count: int) -> List[int]:
    """把测试点**编号**收敛为功能点内的 0 基下标（去重保序）。

    ⚠️ 入参是 **1 基编号**（形如 'TP9' 或 9），不是 0 基下标。
    编号 TP9 = 功能点内第 9 个测试点 = 下标 8。

    这里的编号→下标转换**必须做对**：早先版本误把 'TP9' 当下标直接 int()，
    解析失败后全部丢弃 —— 模型明明回对了编号，结果却一条都不计入覆盖，
    且全程无任何报错（静默丢数据，最难查的一类 bug）。
    """
    out: List[int] = []
    if not isinstance(indexes, (list, tuple)):
        return out
    for x in indexes:
        if isinstance(x, (int, float)):
            n = int(x)
        else:
            m = re.match(r'^TP\s*0*(\d{1,4})$', str(x).strip(), re.IGNORECASE)
            if not m:
                continue
            n = int(m.group(1))
        idx = n - 1                      # 1 基编号 → 0 基下标
        if 0 <= idx < tp_count and idx not in out:
            out.append(idx)
    return out


def build_ai_context(
    tp_items: Sequence[Dict[str, Any]],
    cases: Sequence[Dict[str, Any]],
) -> Tuple[str, str]:
    """构造第 3 层 AI 判定的输入：测试点清单 + 用例正文。

    测试点编号用**功能点内下标+1**（TP1/TP2…）呈现给模型，
    与前端下拉框里的标签一致，避免模型在两套编号之间搞混。
    """
    tp_lines = []
    for i, tp in enumerate(tp_items or []):
        title = tp.get('title', '') if isinstance(tp, dict) else str(tp)
        dims = tp.get('dims') or [] if isinstance(tp, dict) else []
        if isinstance(dims, str):
            dims = [d.strip() for d in dims.split(',') if d.strip()]
        dim_txt = ('（覆盖维度：%s）' % '、'.join(str(d) for d in dims)) if dims else ''
        tp_lines.append('TP%d：%s%s' % (i + 1, title, dim_txt))
    tp_text = '\n'.join(tp_lines) if tp_lines else '（该功能点没有测试点）'

    case_lines = []
    for c in cases or []:
        cid = c.get('case_id')
        title = c.get('title') or ''
        steps = c.get('steps') or ''
        expected = c.get('expected_result') or ''
        case_lines.append(
            '用例 #%s：%s\n  步骤：%s\n  预期结果：%s' % (cid, title, steps, expected))
    case_text = '\n'.join(case_lines) if case_lines else '（无待判定用例）'
    return tp_text, case_text
