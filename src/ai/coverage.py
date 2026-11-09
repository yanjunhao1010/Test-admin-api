"""覆盖维度与质量闸门（C 组 C01-C14 + R 组 R1-R5 + I 组 I01-I08）。

三套维度是**物理隔离**的，来源与语义完全不同，绝不能混装：
- C 组（COVERAGE_DIMS）：系统原有的 14 个技术维度，口径与 `ai_coverage_dims` 表一致
  （C05=并发冲突、C11=接口幂等、C13=状态机流转……）。定义**一字不改**，历史数据不受影响。
- R 组（ROUTE_DIMS）：Lemon Checkpoints skill 的「风险路线 5 段」，以**用户操作路径**组织，
  补的是 C 组缺的那块视角（同一个 C05 场景，站在用户路径上看它属于哪一段）。
- I 组（API_DIMS）：接口自动化专属的 7 个契约维度。**不复用 C/R**：C 组偏黑盒功能
  （审计日志、会话恢复在接口层无法观测），R 组五段是纯 UI 用户路径（对接口完全不适用），
  硬套会让模型产出一堆「页面跳转后展示是否正确」的假用例。

⚠️ 三个刻意保留的现状（本期不做重构）：
1. `COVERAGE_DIMS` 是硬编码的，`ai_coverage_dims` 表在代码里从未被读取过。本模块维持同样风格，
   不改成「读表驱动」——那会波及需求分析全链路，属另一件事。
2. C 组与 R 组统计**分段返回**，R 组不并入 C 组的分母。否则所有历史需求的覆盖率会从
   100% 掉到 73%，看起来像系统坏了（见方案文档衍生问题 D1）。
3. I 组只被接口用例生成链路使用，需求分析侧**完全不 import 它**——两套口径不互相污染。
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# C 组：系统原有 14 个技术维度（定义与历史口径一致，禁止改动）
# ---------------------------------------------------------------------------
COVERAGE_DIMS: List[str] = ['C%02d' % i for i in range(1, 15)]

COVERAGE_DIM_NAMES: Dict[str, str] = {
    'C01': '正常流', 'C02': '异常流', 'C03': '边界值', 'C04': '权限控制',
    'C05': '并发冲突', 'C06': '重试机制', 'C07': '审计日志', 'C08': '会话恢复',
    'C09': '数据一致性', 'C10': '上下游依赖', 'C11': '接口幂等', 'C12': '输入校验',
    'C13': '状态机流转', 'C14': '安全合规',
}

# ---------------------------------------------------------------------------
# R 组：Lemon 风险路线 5 段（新增正交维度）
# ---------------------------------------------------------------------------
ROUTE_DIMS: List[str] = ['R1', 'R2', 'R3', 'R4', 'R5']

#: R 组名称 + 给模型看的判断口径（与 ai_coverage_dims.prompt_hint 同风格，迁移脚本会写入表）
ROUTE_DIM_INFO: Dict[str, Tuple[str, str]] = {
    'R1': ('进入与展示',
           '入口/页面/列表是否完整可达，有无错位、多余元素、加载失败；空态与首次进入是否正常。'),
    'R2': ('信息输入',
           '输入是否被正确接收；长度/格式/字符集约束是否生效；敏感信息是否脱敏、前后端校验是否一致。'),
    'R3': ('系统校验',
           '服务端校验逻辑是否正确、安全、不可被前端绕过；校验顺序与提示是否合理。'),
    'R4': ('结果反馈',
           '成功与失败的提示是否可理解、可恢复、无敏感信息泄漏；错误码是否可区分；重复提交是否有反馈。'),
    'R5': ('状态保持与退出',
           '会话/登录态/草稿状态是否一致；中断(弱网/退出/刷新)后能否恢复；能否安全退出并清理痕迹。'),
}

#: R 组正文描述，进提示词用（R 组的「本段必问 5 问」来自 Lemon skill 第 2 层）
ROUTE_PROMPT_TEXT = (
    '· R1 进入与展示：入口/页面是否完整、无错位、无多余；空态与首次进入是否正常。\n'
    '· R2 信息输入：输入是否被正确接收、约束是否生效、是否脱敏。\n'
    '· R3 系统校验：校验逻辑是否正确、安全、不可被前端绕过。\n'
    '· R4 结果反馈：成功/失败是否可理解、可恢复、无敏感信息泄漏。\n'
    '· R5 状态保持与退出：会话/状态是否一致、安全、可安全退出。'
)

# ---------------------------------------------------------------------------
# I 组：接口自动化专属 7 维度（新增，与 C/R 物理隔离）
# ---------------------------------------------------------------------------
#: ⚠️ 本期只有 **7 条**，**不含 I05「身份与越权」**（D1 决策 C）。
#:
#: 砍掉 I05 的原因不是"维度不重要"，而是**当前执行引擎根本测不了**：
#: `_execute_interface` 的请求头合并是 `{**env_headers, **iface_headers}`
#: （automation.py:638-640），而 `api_environments.headers` 里存着**真实 X-Token**。
#: 场景级 headers 只能"覆盖"环境头，**无法删除**它。所以生成"不带 token"的越权场景时：
#:   场景写 {"X-Token": null} → 场景层删掉该键 → 环境头的有效 X-Token 又被合并回来
#:   → 请求照常带有效 token → 返回 200 → 断言通过 → **假通过**。
#:
#: 假通过比缺覆盖更有害：缺覆盖是"没测"，假通过是"假装测了还告诉别人测过了"。
#:
#: `I05` 这个编号**刻意保留空位**（sort 105），P2 落地越权能力时原样复用，
#: 这样历史数据的维度编码不会失效。
API_DIMS: List[str] = ['I01', 'I02', 'I03', 'I04', 'I06', 'I07', 'I08']

#: 本期**禁止**分配的维度。模型若返回该标签，`normalize_api_dims()` 会静默丢弃。
API_DIM_FORBIDDEN: List[str] = ['I05']

#: I 组名称 + 给模型看的判断口径（与 ai_coverage_dims.prompt_hint 同风格，迁移脚本会写入表）
API_DIM_INFO: Dict[str, Tuple[str, str]] = {
    'I01': ('正常调用',
            '合法入参下 HTTP状态码 + 业务码 + 关键业务字段全部符合预期。只标真正走通成功路径的点。'),
    'I02': ('必填缺失',
            '必填参数不传/传null/传空串/只传一半，服务端是否拦截并返回明确错误码。'
            '场景 params 里用 null 表示该键不传，不要用空字符串代替。'),
    'I03': ('类型非法',
            '数字字段传字符串、布尔传非0/1、数组传对象、JSON结构错位，是否被正确拒绝。'),
    'I04': ('边界值',
            '数值上下限、字符串最小最大长度、数组条数边界、分页pageNo/pageSize极值与0、负数、超大值。'),
    'I06': ('幂等与重复',
            '同一请求重复调用、并发调用同一资源，结果是否一致（无重复记录、金额不重复扣）。'
            '只对GET/HEAD接口分配该维度，写接口重复提交会产生脏数据。'),
    'I07': ('依赖与降级',
            '依赖变量未获得、上游异常、下游超时或5xx时，本接口是明确失败还是静默降级或产生脏数据。'
            '接口无{{var}}占位且无extracts提取规则时不要硬造该维度。'),
    'I08': ('响应契约',
            '必返字段是否都在、字段类型是否稳定、错误码是否可区分、'
            '敏感字段（手机号/身份证/token）是否脱敏外发。断言优先用 json_path 深断言。'),
}

#: 只允许分配给 GET/HEAD 接口的维度（写接口重复提交会产生脏数据）
API_DIM_SAFE_ONLY: List[str] = ['I06']

#: I 组正文描述，进 api_case_* 提示词用
API_PROMPT_TEXT = (
    '· I01 正常调用：合法入参下状态码、业务码、关键业务字段全部符合预期。\n'
    '· I02 必填缺失：必填不传/传null/传空串/只传一半，是否被拦截并返回明确错误码。\n'
    '· I03 类型非法：数字传字符串、布尔传非0/1、数组传对象、JSON结构错位，是否被正确拒绝。\n'
    '· I04 边界值：数值上下限、字符串长度边界、数组条数边界、分页极值与0/负数/超大值。\n'
    '· I06 幂等与重复：重复调用/并发调用结果是否一致（**只对GET/HEAD接口分配**）。\n'
    '· I07 依赖与降级：依赖变量缺失、上游异常、下游超时/5xx时是明确失败还是静默降级。\n'
    '· I08 响应契约：必返字段、字段类型稳定性、错误码可区分性、敏感字段是否脱敏。'
)

#: 执行引擎 `InterfaceBase.assert_type` 注释里列出的 8 种断言类型。
#: 落地前按此白名单校验，非法值直接丢弃（模型偶尔会自造类型）。
API_ASSERT_TYPES: List[str] = [
    'status_code',    # HTTP 状态码
    'contains',       # 响应体包含子串
    'body_status',    # 响应体 status 字段
    'json_path',      # JSONPath 断言（期望格式 path=>值）
    'regex',          # 正则匹配
    'header',         # 响应头断言（格式 Header=>值）
    'response_time',  # 耗时阈值 ms（<= 通过）
    'custom',         # 自定义表达式（结果为真即通过）
]

# ---------------------------------------------------------------------------
# Lemon 5 个质量闸门（进提示词 + 供硬闸门代码判定）
# ---------------------------------------------------------------------------
GATE_NAMES: List[str] = [
    '能否完成用户目标', '出错提示可理解可恢复', '不造成敏感数据泄露',
    '并发/重复操作下一致', '中断后能恢复',
]

GATE_PROMPT_TEXT = (
    '· 能否完成用户目标；\n'
    '· 出错时提示是否可理解、可恢复；\n'
    '· 是否造成敏感数据泄露；\n'
    '· 并发/重复操作下是否一致；\n'
    '· 中断（弱网/退出/刷新）后能否恢复。'
)

# ---------------------------------------------------------------------------
# 严重度 S0-S3 → priority 1/2/3（决策③：severity 与 priority 双写）
# ---------------------------------------------------------------------------
SEVERITY_LEVELS: List[str] = ['S0', 'S1', 'S2', 'S3']

SEVERITY_NAMES: Dict[str, str] = {
    'S0': '阻断', 'S1': '严重', 'S2': '一般', 'S3': '轻微',
}

#: S0/S1 → 3(高)，S2 → 2(中)，S3 → 1(低)
SEVERITY_TO_PRIORITY: Dict[str, int] = {'S0': 3, 'S1': 3, 'S2': 2, 'S3': 1}

# ---------------------------------------------------------------------------
# 语义去重（决策⑤：Jaccard 0.75，跨批累计）
# ---------------------------------------------------------------------------
#: 相似度阈值。实测「5位 vs 7位验证码」那对恰好 0.75（同属合法边界用例，不应被杀），
#: 因此用严格大于判断；若实际误杀可上调到 0.78。被丢弃内容会写日志，便于事后判断。
CASE_DEDUP_THRESHOLD = 0.75

#: 「同功能点 + 同类型」的更低去重阈值。同一功能点的同类型用例（如两条「正向/场景法」）
#: 几乎必然是「换汤不换药」的重复：模型换种说法把同一条用例又造一遍，措辞略改后
#: 3-gram Jaccard 常落在 0.6~0.75 之间，靠全局 0.75 漏网（实测一对 J=0.622 的近似重复漏网）。
#: 降到 0.5 即可拦下这类重复；而「正向/异常/边界各一条」类型不同，不触发本规则，
#: 仍走 0.75 全局阈值，不受影响 —— 既去重又保留合理的多角度用例。
CASE_DEDUP_SAME_FP_THRESHOLD = 0.5

#: 3-gram 的 n。3 是常用折中：2 太松（"登录""提示"就撞），4 太紧（改一个字的同义场景漏掉）。
_NGRAM_N = 3


def normalize_severity(value: Any) -> str:
    """把模型五花八门的严重度写法收敛到 S0-S3；认不出来默认 S2（最保守的中档）。

    模型常返回「阻断」「严重」「P0」「高」等变体，这里做宽松匹配。
    """
    s = str(value or '').strip().upper().replace(' ', '').replace('　', '')
    m = re.search(r'S?([0-3])', s)
    if m and ('S' + m.group(1)) in SEVERITY_LEVELS:
        return 'S' + m.group(1)
    # 中文词映射（模型只写中文时不至于全军覆没）
    if any(k in s for k in ('阻断', '致命', 'P0', '最高', '不能接受')):
        return 'S0'
    if any(k in s for k in ('严重', '高', '重要', 'P1')):
        return 'S1'
    if any(k in s for k in ('一般', '中', '普通', 'P2')):
        return 'S2'
    if any(k in s for k in ('轻微', '低', '次要', 'P3', '建议')):
        return 'S3'
    return 'S2'


def severity_to_priority(value: Any) -> int:
    """S0-S3 → priority 1/2/3。无法识别时回落到 2（中），与旧逻辑的默认值一致。"""
    return SEVERITY_TO_PRIORITY.get(normalize_severity(value), 2)


def normalize_dims(value: Any) -> List[str]:
    """把 C 组维度收敛到 C01-C14 内的合法编号（去重、保序）。

    复用宽松匹配口径（C1 / C-01 / 1 等写法都能认），但**只认 C 组**：
    混入的 R 编号会被丢弃，绝不让两套编号进同一个字段（决策①）。
    """
    return _normalize_group(value, COVERAGE_DIMS, prefix='C', pad=2)


def normalize_routes(value: Any) -> List[str]:
    """把 R 组路线收敛到 R1-R5 内的合法编号（去重、保序）。只认 R 组。"""
    return _normalize_group(value, ROUTE_DIMS, prefix='R', pad=1)


def normalize_api_dims(value: Any, method: str = '') -> List[str]:
    """把 I 组维度收敛到 7 条白名单内（去重、保序）。只认 I 组。

    两层过滤，都是**静默丢弃**（不抛错、不中断整批生成）：
    1. 不在 `API_DIMS` 白名单里的编号一律丢弃 —— 主要是 `I05`（越权），
       当前执行引擎测不了，硬做会产生"报告通过、实际没测"的假通过用例。
    2. `API_DIM_SAFE_ONLY`（目前只有 I06 幂等）在**写接口**上一律丢弃：
       验证幂等必须真的重复提交一次，POST/PUT/DELETE 这么干会产生脏数据。

    ⚠️ `method` 传大写英文方法名（'GET'/'POST'…）。为空时只做第 1 层过滤。
    """
    out = _normalize_group(value, API_DIMS, prefix='I', pad=2)
    m = str(method or '').strip().upper()
    if m and m not in ('GET', 'HEAD'):
        out = [d for d in out if d not in API_DIM_SAFE_ONLY]
    return out


def normalize_api_asserts(value: Any) -> Tuple[List[Dict[str, Any]], int]:
    """校验场景级断言数组，返回 (合法断言列表, 被丢弃条数)。

    引擎只认 `API_ASSERT_TYPES` 里的 8 种 `assert_type`，模型自造的类型会让
    `_assert_one` 走到未知分支、断言静默不生效 —— 比没有断言更危险，所以直接丢。
    非法条目丢弃但**不阻断整条场景**：断言被丢干净的场景仍可落地（用户能看到提示）。
    """
    if value is None:
        return [], 0
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], 0
    out: List[Dict[str, Any]] = []
    dropped = 0
    for a in value:
        if not isinstance(a, dict):
            dropped += 1
            continue
        at = str(a.get('assert_type') or '').strip().lower()
        if at not in API_ASSERT_TYPES:
            dropped += 1
            continue
        item: Dict[str, Any] = {
            'assert_type': at,
            'expected_value': '' if a.get('expected_value') is None
                             else str(a.get('expected_value')),
            'enabled': 1 if a.get('enabled', 1) in (1, '1', True, 'true') else 0,
        }
        # json_path 期望格式为 'path=>值'（见 automation._assert_one）；缺 '=>' 时补一个，
        # 否则引擎会把整个串当路径去 get，断言必然失败且报错信息难懂。
        if at == 'json_path' and '=>' not in item['expected_value']:
            item['expected_value'] = '%s=>非空' % item['expected_value'].strip()
        out.append(item)
    return out, dropped


_TOKEN_RE = re.compile(r'([A-Z])?\s*-?\s*(\d{1,2})')


def _normalize_group(value: Any, allowed: List[str], prefix: str, pad: int) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        raw = [str(int(value))]
    elif isinstance(value, str):
        raw = re.split(r'[,，;；\s、|]+', value)
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for x in value:
            if isinstance(x, (int, float)):
                raw.append(str(int(x)))
            else:
                raw.extend(re.split(r'[,，;；\s、|]+', str(x)))
    else:
        return []
    out: List[str] = []
    for x in raw:
        t = str(x).strip().upper().replace(' ', '').replace('-', '')
        if not t:
            continue
        m = _TOKEN_RE.fullmatch(t)
        if not m:
            continue
        # ⚠️ 前缀必须严格判定：写了字母前缀的 token 只认自己的组。
        # 早期版本用 'C?(\d)' 这种「前缀可选」的正则，导致 'R3' 被当成 C03、
        # 'C03' 被当成 R3 —— 两套编号互相串门，直接违反决策①的隔离要求。
        # 只有**没写前缀**的纯数字，才按调用方的组来解释。
        token_prefix = m.group(1)
        if token_prefix and token_prefix != prefix:
            continue
        # ⚠️ pad 必须按组区分：C 组是 C01~C14（两位），R 组是 R1~R5（一位）。
        # 统一按两位拼会算出 'R02'，不在 ROUTE_DIMS 里 → R 组会被全部清空。
        code = '%s%0*d' % (prefix, pad, int(m.group(2)))
        if code in allowed and code not in out:
            out.append(code)
    return out


def _grams(text: Any) -> Set[str]:
    """归一化后取 n-gram 集合。空白全去掉：中英文混排的步骤里空格不该影响相似度判定。"""
    s = re.sub(r'\s+', '', str(text or '')).lower()
    if len(s) < _NGRAM_N:
        return {s} if s else set()
    return set(s[i:i + _NGRAM_N] for i in range(len(s) - _NGRAM_N + 1))


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def is_similar(a_grams: Set[str], b_grams: Set[str],
               threshold: float = CASE_DEDUP_THRESHOLD) -> bool:
    """是否判为重复。严格大于阈值 —— 实测 0.75 那个边界对是合法边界用例，不能杀。"""
    return jaccard(a_grams, b_grams) > threshold


class DedupCollector:
    """跨批累计的语义去重器（决策⑤）。

    ⚠️ 关键：`_grams_list` 必须是**整单级**累积，不能在每批开始时重置。
    否则分 3 批生成时，跨批之间的重复一条都拦不住。

    同时保留原有的精确匹配口径（模块+功能点+前置+步骤+预期 全等），
    两者任一命中即判重复 —— 精确匹配能兜住 Jaccard 因分词粒度漏掉的完全雷同。
    """

    def __init__(self, threshold: float = CASE_DEDUP_THRESHOLD,
                 same_fp_threshold: float = CASE_DEDUP_SAME_FP_THRESHOLD):
        self.threshold = threshold
        self.same_fp_threshold = same_fp_threshold
        self._grams_list: List[Set[str]] = []
        self._exact_keys: Set[Tuple[str, ...]] = set()
        # 与 _grams_list 一一同步：每条历史用例的 (function_point, case_type) 归一化，
        # 供「同功能点 + 同类型」用更严阈值判重。
        self._fp_type_list: List[Tuple[str, str]] = []

    @staticmethod
    def exact_key(item: Dict[str, Any]) -> Tuple[str, ...]:
        def _n(v: Any) -> str:
            return re.sub(r'\s+', '', str(v or '')).lower()

        return (
            str(item.get('module') or '').strip().lower(),
            str(item.get('function_point') or '').strip().lower(),
            _n(item.get('pre_condition')),
            _n(item.get('steps')),
            _n(item.get('expected_result')),
        )

    def seen(self, item: Dict[str, Any]) -> bool:
        """判断是否与此前任何一条重复；不重复则登记。

        判定分两档：
        - 同功能点 + 同类型（换汤不换药的同角度重复）→ 用更严的 same_fp_threshold(0.5)；
        - 其余（跨功能点 / 不同类型）→ 用全局 threshold(0.75)，保留「正向/异常/边界各一条」。
        """
        key = self.exact_key(item)
        steps_grams = _grams(item.get('steps'))
        if not steps_grams:
            return False  # 步骤为空的条目不参与语义去重，交由硬闸门处理
        if key in self._exact_keys:
            return True
        fp_norm = str(item.get('function_point') or '').strip().lower()
        type_norm = str(item.get('case_type') or '').strip().lower()
        for g, (hf, ht) in zip(self._grams_list, self._fp_type_list):
            th = self.same_fp_threshold if (hf == fp_norm and ht == type_norm) else self.threshold
            if is_similar(steps_grams, g, th):
                return True
        self._exact_keys.add(key)
        self._grams_list.append(steps_grams)
        self._fp_type_list.append((fp_norm, type_norm))
        return False

    def add(self, item: Dict[str, Any]) -> None:
        """无条件登记（用于补跑产物等需要强制占位的场景）。"""
        g = _grams(item.get('steps'))
        if g:
            self._grams_list.append(g)
            self._exact_keys.add(self.exact_key(item))
            self._fp_type_list.append((
                str(item.get('function_point') or '').strip().lower(),
                str(item.get('case_type') or '').strip().lower(),
            ))

    def add_many(self, items: Iterable[Dict[str, Any]]) -> None:
        for it in items or []:
            self.add(it)


# ---------------------------------------------------------------------------
# 硬质量闸门（代码判定，不依赖模型自觉）
# ---------------------------------------------------------------------------
#: 预期结果里出现这些，才算「可验证的锚点」
_VERIFIABLE_PATTERNS = [
    r'\d',                # 任何数字：个数、状态码、时长、金额
    r'["“「\'\']',         # 引号里的明确文案
    r'(提示|文案|显示|展示|跳转|返回|弹出|toast|message|error)',
    r'(成功|失败|不允许|禁止|拦截|拒绝|置灰|禁用|高亮|选中)',
    r'(一致|不变|相同|等于|为空|非空|存在|不存在)',
]
_VERIFIABLE_RE = [re.compile(p) for p in _VERIFIABLE_PATTERNS]

#: 疑似敏感明文（命中只标记不丢弃，避免误杀）
_SENSITIVE_RE = [
    re.compile(r'(?<!\d)(?:\d[ -]?){15,19}(?!\d)'),                       # 疑似银行卡/身份证长数字
    re.compile(r'\b1[3-9]\d{9}\b'),                                      # 手机号明文
    re.compile(r'\b\d{16,19}\b'),                                        # 长卡号
]


def check_item(item: Dict[str, Any]) -> List[str]:
    """硬闸门：返回该条用例的**问题描述列表**（空列表 = 全部通过）。

    只拦「一定不能用」的：预期结果不可验证、步骤过于笼统、疑似敏感明文。
    刻意不拦「不够好但能改」的——那是 `case_review` 的活，不该在生成阶段直接丢弃。
    """
    issues: List[str] = []
    expected = str(item.get('expected_result') or '').strip()
    steps = str(item.get('steps') or '').strip()
    pre = str(item.get('pre_condition') or '').strip()

    if not expected:
        issues.append('预期结果为空')
    elif not any(r.search(expected) for r in _VERIFIABLE_RE):
        issues.append('预期结果无可验证锚点（缺具体文案/数值/状态变化）')

    if not steps:
        issues.append('步骤为空')
    else:
        # 「多步」按换行或「1.」「①」等序号切
        seg = re.split(r'[\n\r]+|(?<=[；;。])|(?=\s*\d+[.、)]\s)|(?=\s*[①②③④⑤⑥⑦⑧⑨⑩])', steps)
        seg = [x for x in seg if x.strip()]
        if len(seg) < 2:
            issues.append('步骤不足 2 步，无法执行')
        # 「具体」：至少出现一个页面/控件词，否则就是「测试功能是否正常」这类废话
        if not re.search(r'(页面|弹窗|对话框|按钮|输入框|下拉|列表|首页|接口|请求|字段|'
                         r'输入|点击|选择|提交|打开|进入|勾选|上传|搜索)', steps):
            issues.append('步骤未指明具体页面/控件/动作')

    if not pre:
        issues.append('前置条件为空')

    blob = ' '.join([pre, steps, expected])
    for rx in _SENSITIVE_RE:
        if rx.search(blob):
            issues.append('疑似敏感信息明文（需脱敏或改用占位数据）')
            break

    return issues


def filter_by_gate(items: List[Dict[str, Any]],
                   drop_severity: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按硬闸门过滤。返回 (通过, 被拦下)。

    ⚠️ 默认只拦**致命项**：预期结果为空 / 步骤为空 / 步骤不足 2 步。
    「无可验证锚点」「未指明具体控件」「疑似敏感明文」只打标不丢 —— 这三条误杀率明显更高，
    直接丢会让实际产出远低于用户请求的条数，比质量问题更伤体验。
    """
    fatal_prefixes = ('预期结果为空', '步骤为空', '步骤不足 2 步')
    passed: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for it in items or []:
        issues = check_item(it)
        if not issues:
            passed.append(it)
            continue
        it = dict(it)
        it['gate_issues'] = issues
        if drop_severity and any(x.startswith(fatal_prefixes) for x in issues):
            rejected.append(it)
        else:
            passed.append(it)
    return passed, rejected


# ---------------------------------------------------------------------------
# 覆盖统计（响应给前端展示用）
# ---------------------------------------------------------------------------
def build_cover_report(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从一批用例算出 C 组 / R 组两段覆盖统计。

    返回结构刻意把两组分开命名（`dims_*` / `routes_*`），前端必须分两段渲染，
    **不能把 R 组并进 C 组分母**（见模块文档 D1）。
    """
    c_cnt = {c: 0 for c in COVERAGE_DIMS}
    r_cnt = {r: 0 for r in ROUTE_DIMS}
    gate_hit = {g: 0 for g in GATE_NAMES}
    sev_cnt = {s: 0 for s in SEVERITY_LEVELS}
    type_cnt: Dict[str, int] = {}

    for it in items or []:
        for d in normalize_dims(it.get('dims')):
            c_cnt[d] += 1
        for r in normalize_routes(it.get('routes') or it.get('risk_route')):
            r_cnt[r] += 1
        sev_cnt[normalize_severity(it.get('severity'))] += 1
        t = str(it.get('case_type') or '').strip()
        if t:
            type_cnt[t] = type_cnt.get(t, 0) + 1
        # 闸门命中按正文关键词粗判，仅用于「哪几个闸门被覆盖了」的提示，不作为硬判定
        blob = ' '.join(str(it.get(k) or '') for k in
                        ('pre_condition', 'steps', 'expected_result'))
        for g, kws in (
            ('能否完成用户目标', ('成功', '完成', '跳转', '进入')),
            ('出错提示可理解可恢复', ('提示', '错误', '失败', '重试', '重新')),
            ('不造成敏感数据泄露', ('脱敏', '不展示', '不返回', '加密', '掩码', '遮蔽')),
            ('并发/重复操作下一致', ('并发', '同时', '重复', '多次', '多次点击')),
            ('中断后能恢复', ('弱网', '断网', '中断', '刷新', '重进', '退出后', '超时后')),
        ):
            if any(k in blob for k in kws):
                gate_hit[g] += 1

    c_covered = [d for d, n in c_cnt.items() if n > 0]
    r_covered = [r for r, n in r_cnt.items() if n > 0]
    return {
        'total_items': len(items or []),
        # C 组：与需求分析侧口径完全一致（total 恒为 14）
        'dims': c_cnt,
        'dims_names': dict(COVERAGE_DIM_NAMES),
        'dims_covered': len(c_covered),
        'dims_total': len(COVERAGE_DIMS),
        'dims_gaps': [d for d in COVERAGE_DIMS if c_cnt[d] == 0],
        'dims_rate': round(len(c_covered) / len(COVERAGE_DIMS) * 100, 1),
        # R 组：独立成段，不并入 C 组分母
        'routes': r_cnt,
        'routes_names': {r: ROUTE_DIM_INFO[r][0] for r in ROUTE_DIMS},
        'routes_covered': len(r_covered),
        'routes_total': len(ROUTE_DIMS),
        'routes_gaps': [r for r in ROUTE_DIMS if r_cnt[r] == 0],
        'routes_rate': round(len(r_covered) / len(ROUTE_DIMS) * 100, 1),
        # 附加维度：严重度 / 用例类型 / 闸门命中
        'severity': sev_cnt,
        'case_types': type_cnt,
        'gates': gate_hit,
    }


# ---------------------------------------------------------------------------
# 六维检查清单（**不是新维度体系**，是把 C 组维度按"测试设计方法"重新归并的检查表）
# ---------------------------------------------------------------------------
#: ⚠️ 刻意**不新增编号**（没有 D01~D06）：六维是给模型/给人看的**检查清单**，
#: 每一维都锚定到已有的 C 组维度上。这样做有三个好处：
#: 1. 不破坏"C/R/I 三组物理隔离"铁律（决策①），也不用改 C 组 14 维的分母口径；
#: 2. 六维缺口能精确换算成"缺哪个 C 维度"，直接喂给已有的 case_gate 补跑；
#: 3. 覆盖率看板已有 C 组维度统计，六维只是同一批数据的另一种切面，不产生第二套数字。
#:
#: 键名用中文语义名（不用编号），因为它是"清单项"不是"维度编号"。
SIX_DIM_CHECKLIST: List[Dict[str, Any]] = [
    {
        'key': 'normal',
        'name': '功能正常路径',
        'hint': '核心成功流程能否走通：主链路一步不漏地到达成功结果。',
        'c_dims': ['C01'],
    },
    {
        'key': 'boundary',
        'name': '边界值',
        'hint': '最小/最大、临界内/临界外、0、空、超长、极端数值。',
        'c_dims': ['C03'],
    },
    {
        'key': 'exception',
        'name': '异常与错误输入',
        'hint': '非法格式、类型错误、必填缺失、依赖失败、返回异常时的兜底表现。',
        'c_dims': ['C02', 'C12'],
    },
    {
        'key': 'permission',
        'name': '权限与角色',
        'hint': '不同角色可见/可操作范围、无权限时的拦截与提示。',
        'c_dims': ['C04'],
    },
    {
        'key': 'state',
        'name': '状态与流转',
        'hint': '各状态之间的合法流转、非法流转被拒、终态不可再改。',
        'c_dims': ['C13'],
    },
    {
        'key': 'data',
        'name': '数据约束与并发',
        'hint': '唯一性/引用完整性/重复提交/并发冲突下的一致性。',
        'c_dims': ['C09', 'C05', 'C11'],
    },
]

#: 六维 → 该维要求的 C 组维度（取并集，用于"缺这维 = 缺哪些 C 维度"）
SIX_DIM_C_DIMS: Dict[str, List[str]] = {
    d['key']: list(d['c_dims']) for d in SIX_DIM_CHECKLIST
}

#: 六维正文描述，进提示词用
SIX_DIM_PROMPT_TEXT = '\n'.join(
    '· %s（对应 %s）：%s'
    % (d['name'], '、'.join(d['c_dims']), d['hint'])
    for d in SIX_DIM_CHECKLIST
)

#: 六维键 → 中文名（响应/前端展示用）
SIX_DIM_NAMES: Dict[str, str] = {d['key']: d['name'] for d in SIX_DIM_CHECKLIST}


def check_six_dim_gaps(items: List[Dict[str, Any]],
                       skip_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """检查一批用例是否六维齐全，返回**缺失的六维清单**（空 = 六维齐全）。

    判定口径：某维被覆盖 = 该维要求的 C 维度里，**至少有一个**出现在某条用例的
    `dims` 中。取"任一命中"而非"全部命中"，是因为「异常与错误输入」维同时锚定
    C02/C12，模型标了 C02 就算这维有覆盖，不该因为没标 C12 判成缺口。

    `skip_keys` 用于豁免：明确不涉及的维度（如纯展示页无权限流转），
    由调用方依据需求实际内容判定，不在这里臆断。
    """
    skip = {str(k) for k in (skip_keys or [])}
    hit: Set[str] = set()
    for it in items or []:
        for d in normalize_dims(it.get('dims')):
            hit.add(d)
    gaps: List[Dict[str, Any]] = []
    for d in SIX_DIM_CHECKLIST:
        if d['key'] in skip:
            continue
        if not any(cd in hit for cd in d['c_dims']):
            gaps.append({
                'key': d['key'],
                'name': d['name'],
                'c_dims': list(d['c_dims']),
                'hint': d['hint'],
            })
    return gaps


def build_six_dim_report(items: List[Dict[str, Any]],
                         skip_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """六维覆盖报告：每维命中条数 + 缺失清单（供响应给前端展示）。"""
    cnt: Dict[str, int] = {d['key']: 0 for d in SIX_DIM_CHECKLIST}
    for it in items or []:
        ds = normalize_dims(it.get('dims'))
        for d in SIX_DIM_CHECKLIST:
            if any(cd in ds for cd in d['c_dims']):
                cnt[d['key']] += 1
    gaps = check_six_dim_gaps(items, skip_keys)
    total = len(SIX_DIM_CHECKLIST)
    covered = total - len(gaps)
    return {
        'counts': cnt,
        'names': dict(SIX_DIM_NAMES),
        'covered': covered,
        'total': total,
        'rate': round(covered / total * 100, 1) if total else 0,
        'gaps': gaps,
    }


# ---------------------------------------------------------------------------
# I 组覆盖统计（接口用例生成专用，**独立于** build_cover_report）
# ---------------------------------------------------------------------------
def build_api_cover_report(items: List[Dict[str, Any]], method: str = '') -> Dict[str, Any]:
    """从一批接口场景算出 I 组覆盖统计。

    刻意**不复用** `build_cover_report`：那边的字段名是 `pre_condition/steps/
    expected_result`（文档用例），接口场景是 `pre_condition/steps/expected`
    （可执行场景）；而且那边统计 C/R 两组，混进来会污染需求分析侧口径。

    `method` 传接口方法，用于按 `API_DIM_SAFE_ONLY` 过滤（写接口不算 I06 幂等）。
    """
    i_cnt = {d: 0 for d in API_DIMS}
    sev_cnt = {s: 0 for s in SEVERITY_LEVELS}
    ev_cnt = {'basis': 0, 'assumption': 0, 'pending': 0}
    type_cnt: Dict[str, int] = {}
    assert_types: Dict[str, int] = {}
    with_asserts = 0
    with_json_path = 0

    for it in items or []:
        for d in normalize_api_dims(it.get('dims'), method):
            i_cnt[d] += 1
        sev_cnt[normalize_severity(it.get('severity'))] += 1
        ev = str(it.get('evidence_type') or '').strip().lower()
        if ev in ev_cnt:
            ev_cnt[ev] += 1
        t = str(it.get('case_type') or '').strip()
        if t:
            type_cnt[t] = type_cnt.get(t, 0) + 1
        asserts, _ = normalize_api_asserts(it.get('asserts'))
        if asserts:
            with_asserts += 1
        for a in asserts:
            assert_types[a['assert_type']] = assert_types.get(a['assert_type'], 0) + 1
            if a['assert_type'] == 'json_path':
                with_json_path += 1

    covered = [d for d, n in i_cnt.items() if n > 0]
    return {
        'total_items': len(items or []),
        'dims': i_cnt,
        'dims_names': {d: API_DIM_INFO[d][0] for d in API_DIMS},
        'dims_covered': len(covered),
        'dims_total': len(API_DIMS),
        'dims_gaps': [d for d in API_DIMS if i_cnt[d] == 0],
        'dims_rate': round(len(covered) / len(API_DIMS) * 100, 1),
        'severity': sev_cnt,
        'evidence': ev_cnt,
        'case_types': type_cnt,
        'assert_types': assert_types,
        # 断言质量（决策 6.C 的核心验收口径）：每条场景都该自带断言，且优先 json_path 深断言
        'with_asserts': with_asserts,
        'with_json_path': with_json_path,
        # 「有断言但没一条深断言」= 退化成了 body_status 这类浅断言，等于没提升
        'shallow_only': max(0, with_asserts - with_json_path),
    }