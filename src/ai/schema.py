"""结构化输出的数据模型与逐条校验。

为什么不用「整批 Pydantic 校验」：一次生成 10 条用例只要 1 条缺字段，整批就废了。
这里改成逐条校验 —— 坏条目丢弃并报告原因，好条目照常返回（方案里的「改造 2」）。
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from . import coverage as coverage_mod

PRIORITY_MAP = {
    '高': 3, '中': 2, '低': 1,
    'p0': 3, 'p1': 2, 'p2': 1, 'p3': 1,
    'high': 3, 'medium': 2, 'low': 1,
    '3': 3, '2': 2, '1': 1,
}


def normalize_priority(value: Any) -> int:
    """把 AI 各种写法（高/P0/high/3）统一成 1低 2中 3高，与 test_cases.priority 对齐"""
    if value is None:
        return 2
    if isinstance(value, (int, float)):
        v = int(value)
        if v in (1, 2, 3):
            return v
        return 2
    key = str(value).strip().lower()
    return PRIORITY_MAP.get(key, PRIORITY_MAP.get(str(value).strip(), 2))


def _as_str(value: Any) -> str:
    """AI 常把 steps 写成数组/对象，统一拍平成文本，避免整条被丢"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for i, item in enumerate(value, 1):
            if isinstance(item, dict):
                parts.append('%d. %s' % (i, ' '.join('%s:%s' % (k, v) for k, v in item.items())))
            else:
                parts.append('%d. %s' % (i, item))
        return '\n'.join(parts)
    if isinstance(value, dict):
        return '\n'.join('%s: %s' % (k, v) for k, v in value.items())
    return str(value).strip()


_PCT_RE = None


def _norm_confidence(v: Any) -> float:
    """把 AI 回的 confidence 归一成 0~1。

    实测模型会回 0.85 / 85 / "85" / **"85%"** / "高" 五种形态。原实现只走
    `float(v)`，`'85%'` 会抛异常被兜成 **0.0** —— 静默丢失整条场景的可信度，
    用户在预览里看到的是「信心 0」而不是「85%」，属于会误导决策的错误。
    这里先剥百分号/百分词再转浮点，转不动才归 0。
    """
    global _PCT_RE
    if _PCT_RE is None:
        import re as _re
        _PCT_RE = _re.compile(r'[^\d.]')
    f = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
    elif isinstance(v, str):
        s = _PCT_RE.sub('', v.strip())
        if s:
            try:
                f = float(s)
            except Exception:
                f = None
    if f is None:
        return 0.0
    if f > 1:            # 85 / "85" / "85%" → 0.85
        f = f / 100.0
    return max(0.0, min(1.0, f))


class CaseDraftItem(BaseModel):
    """AI 生成的一条用例草稿"""

    module: str = ''
    function_point: str = ''
    priority: int = 2
    case_type: str = ''
    design_method: str = ''
    # 覆盖维度(C组 C01~C14) 与风险路线(R组 R1~R5) 分开两个字段，**绝不混装**（决策①）
    dims: List[str] = []
    routes: List[str] = []
    # 测试点回标编号（TP1/TP3…）：这条用例覆盖了需求分析阶段的哪些测试点。
    # 为空数组 = 该用例没有对应测试点（不硬凑）。编号由后端按原始下标生成，
    # 白名单校验在 ai_workbench 侧做（不在 schema 层，schema 不知道当前计划的编号集合）。
    tp_refs: List[str] = []
    # 证据类型：basis 需求依据 / assumption 经验假设 / pending 待确认
    evidence_type: str = 'basis'
    # 严重度 S0~S3（决策③：与 priority 双写）
    severity: str = ''
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''
    gate_note: str = ''
    confidence: float = 0.0

    @field_validator('priority', mode='before')
    @classmethod
    def _p(cls, v):
        return normalize_priority(v)

    @field_validator('module', 'function_point', 'case_type', 'design_method', 'pre_condition',
                     'steps', 'expected_result', 'remark', 'gate_note', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _e(cls, v):
        return normalize_evidence(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('dims', mode='before')
    @classmethod
    def _d(cls, v):
        return coverage_mod.normalize_dims(v)

    @field_validator('routes', mode='before')
    @classmethod
    def _r(cls, v):
        return coverage_mod.normalize_routes(v)

    @field_validator('tp_refs', mode='before')
    @classmethod
    def _tp(cls, v):
        return normalize_tp_refs(v)

    @field_validator('confidence', mode='before')
    @classmethod
    def _c(cls, v):
        return _norm_confidence(v)

    @model_validator(mode='after')
    def _sync_priority(self):
        """severity → priority 双写（决策③）。

        severity 为空字符串说明模型没给（老数据/旧提示词），此时**保留原 priority 不动**，
        避免把明确写了「高优先级」的用例误降级成中。
        """
        if self.severity:
            self.priority = coverage_mod.severity_to_priority(self.severity)
        return self


class CasePlanItem(BaseModel):
    """G1 覆盖计划里的一格：不写用例正文，只说要覆盖哪个维度、验证什么、分几条。"""

    module: str = ''
    function_point: str = ''
    dim: str = ''            # C 组维度，单值（一个格子打一个 C 维度）
    route: str = ''          # R 组风险路线，单值
    case_type: str = ''
    design_method: str = ''
    severity: str = ''
    evidence_type: str = 'basis'
    focus: str = ''
    planned: int = 1
    note: str = ''

    @field_validator('module', 'function_point', 'case_type', 'design_method',
                     'focus', 'note', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _e(cls, v):
        return normalize_evidence(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('dim', mode='before')
    @classmethod
    def _d(cls, v):
        return (coverage_mod.normalize_dims(v) or [''])[0]

    @field_validator('route', mode='before')
    @classmethod
    def _r(cls, v):
        return (coverage_mod.normalize_routes(v) or [''])[0]

    @field_validator('planned', mode='before')
    @classmethod
    def _pl(cls, v):
        try:
            n = int(v)
        except Exception:
            return 1
        return max(1, min(8, n))   # 单格最多 8 条，防止模型把一格拆成 20 条凑数

    def as_case_vars(self) -> Dict[str, Any]:
        """转成 case_gen 的提示词变量，让 G2 知道「这一格要写什么」。"""
        d = self.dict()
        d['dim_name'] = coverage_mod.COVERAGE_DIM_NAMES.get(self.dim, '')
        d['route_name'] = coverage_mod.ROUTE_DIM_INFO.get(self.route, ('', ''))[0]
        return d


class CaseGapItem(BaseModel):
    """G3 闸门报出的一条覆盖缺口。"""

    dim: str = ''
    route: str = ''
    missing: int = 1
    severity: str = ''
    suggestion: str = ''

    @field_validator('dim', mode='before')
    @classmethod
    def _d(cls, v):
        return (coverage_mod.normalize_dims(v) or [''])[0]

    @field_validator('route', mode='before')
    @classmethod
    def _r(cls, v):
        return (coverage_mod.normalize_routes(v) or [''])[0]

    @field_validator('suggestion', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('missing', mode='before')
    @classmethod
    def _m(cls, v):
        try:
            n = int(v)
        except Exception:
            return 1
        return max(1, min(5, n))   # 单个缺口最多补 5 条

    def to_plan_vars(self) -> Dict[str, Any]:
        """把缺口回喂给 G2 时用的变量（复用 case_plan 的字段名）。"""
        return {
            'module': '',
            'function_point': '',
            'dim': self.dim,
            'route': self.route,
            'case_type': '',
            'design_method': '',
            'severity': self.severity,
            'evidence_type': 'basis',
            'focus': self.suggestion,
            'planned': self.missing,
            'note': '',
            'dim_name': coverage_mod.COVERAGE_DIM_NAMES.get(self.dim, ''),
            'route_name': coverage_mod.ROUTE_DIM_INFO.get(self.route, ('', ''))[0],
        }


# ---------------------------------------------------------------------------
# 接口用例生成（G1 规划 / G2 可执行场景 / G3 缺口）
#
# 与 CaseDraftItem（文档用例）**刻意分开**：字段名与产物形态都不同 ——
# 文档用例是 module/function_point/pre_condition/steps/expected_result；
# 可执行场景是 params/body/asserts（引擎真正发的东西）+ scene_name（= 执行报告的 case_tag）。
# 混进 CaseDraftItem 会让「场景」丢失可执行性，等于白生成。
# ---------------------------------------------------------------------------
class ApiCasePlanItem(BaseModel):
    """G1 覆盖计划里的一格：不写场景正文，只说要覆盖哪个 I 维度、验证什么、分几条。"""

    dim: str = ''              # I 组维度，单值（一个格子打一个维度）
    case_type: str = ''
    design_method: str = ''
    severity: str = ''
    evidence_type: str = 'assumption'
    focus: str = ''
    planned: int = 1
    note: str = ''

    @field_validator('case_type', 'design_method', 'focus', 'note', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _e(cls, v):
        return normalize_evidence(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('dim', mode='before')
    @classmethod
    def _d(cls, v):
        return (coverage_mod.normalize_api_dims(v) or [''])[0]

    @field_validator('planned', mode='before')
    @classmethod
    def _pl(cls, v):
        try:
            n = int(v)
        except Exception:
            return 1
        return max(1, min(6, n))   # 单格最多 6 条，防止模型把一格拆成 20 条凑数

    def as_scene_vars(self) -> Dict[str, Any]:
        """转成 api_case_gen 的提示词变量，让 G2 知道「这一格要写什么」。"""
        return {
            'dim': self.dim,
            'dim_name': coverage_mod.API_DIM_INFO.get(self.dim, ('', ''))[0],
            'case_type': self.case_type,
            'design_method': self.design_method,
            'severity': self.severity,
            'evidence_type': self.evidence_type,
            'focus': self.focus,
            'planned': self.planned,
            'note': self.note,
        }


class ApiCaseSceneItem(BaseModel):
    """G2 产出的一条**可执行场景**。写回 api_interfaces.dataset 后引擎零改动即可跑。"""

    scene_name: str = ''
    case_tag: str = ''
    dims: List[str] = []
    severity: str = 'S2'
    evidence_type: str = 'assumption'
    case_type: str = ''
    design_method: str = ''
    params: Dict[str, Any] = {}
    body: Any = None
    headers: Dict[str, Any] = {}
    asserts: List[Dict[str, Any]] = []
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

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _e(cls, v):
        return normalize_evidence(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('dims', mode='before')
    @classmethod
    def _d(cls, v):
        # ⚠️ 不在这里按 method 过滤 I06 —— Pydantic 校验器拿不到接口方法。
        # 写接口的 I06 过滤在 router 落地前做（normalize_api_dims(dims, method)）。
        # 这里只做白名单收敛：I05 必被丢弃，绝不进任何生成结果。
        return coverage_mod.normalize_api_dims(v)

    @field_validator('params', 'headers', mode='before')
    @classmethod
    def _dict(cls, v):
        """params/headers 必须是 dict。模型偶尔写成 JSON 字符串或数组。

        ⚠️ 这里**不能**做"字符串自动 parse"：场景参数里值可能是 `null`，
        parse 失败时宁可留空 dict 也不能抛错丢弃整条场景。
        """
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return {}
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        if isinstance(v, list):
            # [{k:v}] 形态 → 合并成 dict（模型爱这么写）
            out: Dict[str, Any] = {}
            for item in v:
                if isinstance(item, dict):
                    out.update(item)
            return out
        return {}

    @field_validator('body', mode='before')
    @classmethod
    def _body(cls, v):
        """body 允许对象/数组/字符串；空字符串与 None 统一成 None（表示不覆盖）。"""
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # 优先尝试解析成结构（引擎 json= 传对象更稳）
            try:
                obj = json.loads(s)
                return obj
            except Exception:
                return s
        return v

    @field_validator('asserts', mode='before')
    @classmethod
    def _asserts(cls, v):
        """按引擎的 8 种 assert_type 白名单过滤。非法类型丢弃（模型自造会让断言静默不生效）。

        顺便补 json_path 缺失的 '=>' 分隔符：引擎按 'path=>值' 解析，
        模型只写 '$.data.id' 时断言必然失败且报错难懂。
        """
        ok, _dropped = coverage_mod.normalize_api_asserts(v)
        return ok

    @field_validator('confidence', mode='before')
    @classmethod
    def _c(cls, v):
        return _norm_confidence(v)

    def to_dataset_entry(self) -> Dict[str, Any]:
        """转成写回 `api_interfaces.dataset` 的场景对象。

        字段名必须与 `_run_interface` 的场景判定严格对齐（automation.py:826）：
        只要含 name/params/headers/body/asserts 任一键就走场景分支。
        这里只输出非空项（空 params / 空 headers 不写），让落地后的 dataset 更干净。
        """
        entry: Dict[str, Any] = {'name': self.scene_name or self.case_tag or '未命名场景'}
        if self.params:
            entry['params'] = self.params
        if self.body is not None and self.body != {}:
            entry['body'] = self.body
        if self.asserts:
            # 场景级断言非空 → 引擎会用它覆盖接口级断言（automation.py:887-889）
            entry['asserts'] = self.asserts
        # headers 刻意不落地：环境头存真实 X-Token，场景级只能覆盖不能删除（D1 决策 C）。
        # 模型若写了鉴权头，在此静默丢弃，避免产出「假通过」的越权场景。
        return entry


class ApiCaseGapItem(BaseModel):
    """G3 闸门报出的一条覆盖缺口。"""

    dim: str = ''
    missing: int = 1
    severity: str = 'S2'
    suggestion: str = ''

    @field_validator('dim', mode='before')
    @classmethod
    def _d(cls, v):
        return (coverage_mod.normalize_api_dims(v) or [''])[0]

    @field_validator('suggestion', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('severity', mode='before')
    @classmethod
    def _sev(cls, v):
        return coverage_mod.normalize_severity(v)

    @field_validator('missing', mode='before')
    @classmethod
    def _m(cls, v):
        try:
            n = int(v)
        except Exception:
            return 1
        return max(1, min(5, n))   # 单个缺口最多补 5 条

    def to_plan_vars(self) -> Dict[str, Any]:
        """把缺口回喂给 G2 时用的变量（复用 api_case_plan 的字段名）。"""
        return {
            'dim': self.dim,
            'dim_name': coverage_mod.API_DIM_INFO.get(self.dim, ('', ''))[0],
            'case_type': '',
            'design_method': '',
            'severity': self.severity,
            'evidence_type': 'assumption',
            'focus': self.suggestion,
            'planned': self.missing,
            'note': '',
        }


_API_DIAG_CATEGORIES = ('assertion', 'param', 'pre_condition', 'other')


class ApiCaseDiagnoseItem(BaseModel):
    """F5 失败自愈：单条修正建议。只建议，不改写任何数据。

    category 归一化到 assertion(改断言) / param(改请求参数) /
    pre_condition(缺前置依赖) / other 四类，非法值回落 other。
    """

    category: str = 'other'
    target: str = ''
    current: str = ''
    suggested: str = ''
    reason: str = ''

    @field_validator('category', mode='before')
    @classmethod
    def _c(cls, v):
        s = _as_str(v).strip().lower()
        if s in _API_DIAG_CATEGORIES:
            return s
        # 兼容中文/近义写法
        if '断言' in s or 'expect' in s or 'assert' in s:
            return 'assertion'
        if '参数' in s or 'param' in s or '请求' in s:
            return 'param'
        if '前置' in s or '依赖' in s or 'pre' in s:
            return 'pre_condition'
        return 'other'

    @field_validator('target', 'current', 'suggested', 'reason', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)


class IssueItem(BaseModel):
    """用例质量体检发现的一个问题"""

    case_id: Optional[int] = None
    severity: str = '中'
    problem: str = ''
    suggestion: str = ''
    fixed_pre: str = ''
    fixed_steps: str = ''
    fixed_expected: str = ''

    @field_validator('problem', 'suggestion', 'fixed_pre', 'fixed_steps',
                     'fixed_expected', 'severity', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('case_id', mode='before')
    @classmethod
    def _id(cls, v):
        try:
            return int(v)
        except Exception:
            return None


class CaseFromTextItem(BaseModel):
    """F4：自然语言描述转成的一条结构化用例（priority 保持文本，由前端 mapPriority 映射成 1/2/3）"""

    module: str = ''
    function_point: str = ''
    priority: str = ''        # 高/中/低 文本
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''
    tags: List[str] = []      # F6 标签建议（仅展示，不落库）

    @field_validator('module', 'function_point', 'priority', 'pre_condition', 'steps',
                     'expected_result', 'remark', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('tags', mode='before')
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(',') if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return []


class CaseSuggestItem(BaseModel):
    """F6：基于已填内容建议的标签与优先级（不修改用例正文）"""

    priority: str = ''        # 高/中/低 文本
    tags: List[str] = []      # 建议标签
    reason: str = ''          # 建议依据

    @field_validator('priority', 'reason', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('tags', mode='before')
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(',') if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return []


class TpMatchItem(BaseModel):
    """F7：用例 → 测试点的自动匹配判定结果。

    `tp_refs` 是**功能点内**的测试点编号（TP1 = 第 1 个测试点），
    与前端下拉框标签一致 —— 不是需求级的原始下标。
    白名单裁剪在 router 侧做（schema 不知道当前功能点有几个测试点）。
    """

    case_id: int = 0
    tp_refs: List[str] = []
    reason: str = ''
    confidence: float = 0.0

    @field_validator('case_id', mode='before')
    @classmethod
    def _cid(cls, v):
        try:
            return int(v)
        except Exception:
            return 0

    @field_validator('tp_refs', mode='before')
    @classmethod
    def _tp(cls, v):
        return normalize_tp_refs(v)

    @field_validator('reason', mode='before')
    @classmethod
    def _r(cls, v):
        return _as_str(v)

    @field_validator('confidence', mode='before')
    @classmethod
    def _c(cls, v):
        return _norm_confidence(v)


class TpImpactItem(BaseModel):
    """测试点变更对**单条关联用例**的影响判定 + 修改点定位。

    为什么不复用 IssueItem：
      IssueItem 是「用例体检」的模型（severity=高/中/低，problem=缺陷描述），
      语义是「这条用例本身有哪些毛病」；
      这里是「这次测试点改动**波及**了这条用例什么」，需要一个三档判定
      （不受影响/需微调/需重写）+ 字段级 diff 列表（前端要按字段高亮）。
      改 IssueItem 会波及已在跑的 case_review 体检功能，故独立建模。
    """

    case_id: Optional[int] = None
    # 四档：none=不受影响 / minor=需微调 / major=需重写 / unknown=未判定
    # ⚠️ unknown 是**必须保留**的一档：模型没给出结论时，绝不能归一成 none，
    #    否则「AI 没判定」会被当成「判定为不受影响」（静默假阴性）。
    impact_level: str = 'none'
    # 需修改的字段：pre_condition / steps / expected_result
    diff_fields: List[str] = []
    diff_reason: str = ''
    fixed_pre: str = ''
    fixed_steps: str = ''
    fixed_expected: str = ''
    suggestion: str = ''

    @field_validator('impact_level', mode='before')
    @classmethod
    def _lvl(cls, v):
        s = _as_str(v).strip().lower()
        if s in ('none', 'minor', 'major', 'unknown'):
            return s
        # 兼容中文写法
        if '重写' in s or 'major' in s or s in ('3', '高'):
            return 'major'
        if '微调' in s or 'minor' in s or '轻微' in s or s in ('2', '中'):
            return 'minor'
        if '未知' in s or '未判定' in s or '无法判定' in s or s in ('0', 'unknown'):
            return 'unknown'
        return 'none'

    @field_validator('diff_fields', mode='before')
    @classmethod
    def _fields(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [x for x in re.split(r'[,，;；、\s]+', v) if x]
        if not isinstance(v, list):
            return []
        alias = {
            'pre_condition': 'pre_condition', 'precondition': 'pre_condition',
            '前置条件': 'pre_condition', '前置': 'pre_condition', 'pre': 'pre_condition',
            'steps': 'steps', 'step': 'steps', '步骤': 'steps', '操作步骤': 'steps',
            'expected_result': 'expected_result', 'expectedresult': 'expected_result',
            'expected': 'expected_result', '预期结果': 'expected_result', '预期': 'expected_result',
        }
        out = []
        for item in v:
            s = _as_str(item).strip().lower()
            key = alias.get(s)
            if key and key not in out:
                out.append(key)
        return out

    @field_validator('diff_reason', 'fixed_pre', 'fixed_steps', 'fixed_expected',
                     'suggestion', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('case_id', mode='before')
    @classmethod
    def _id(cls, v):
        try:
            return int(v)
        except Exception:
            return None


class FunctionPointItem(BaseModel):
    """需求文档分块分析出的功能点"""

    module: str = ''
    function_point: str = ''
    description: str = ''
    test_points: List[str] = []
    risks: List[str] = []
    questions: List[str] = []

    @field_validator('module', 'function_point', 'description', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('test_points', 'risks', 'questions', mode='before')
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split('\n') if x.strip()]
        if isinstance(v, list):
            out = []
            for item in v:
                if isinstance(item, dict):
                    out.append(' '.join('%s:%s' % (k, vv) for k, vv in item.items()))
                elif item is not None:
                    out.append(str(item).strip())
            return [x for x in out if x]
        return []


class DataGenRow(BaseModel):
    """测试数据生成的一行（字段名由输入规则决定，允许任意额外字段）"""

    model_config = ConfigDict(extra='allow')


class SqlGenPlan(BaseModel):
    """自定义 SQL 生成 · 字段计划的一条判定。

    field  : 字段名（必须与模板字段名完全一致）
    change : 该字段是否需要变化（False = 保持模板原值不变）
    rule   : 变化规则的自然语言说明（给模型看的生成指引）
    """

    field: str = ''
    change: bool = False
    rule: str = ''

    @field_validator('field', 'rule', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('change', mode='before')
    @classmethod
    def _b(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        text = str(v or '').strip().lower()
        return text in ('true', '1', 'yes', 'y', '是', '需要变化', '变')


class SqlGenRow(BaseModel):
    """自定义 SQL 生成 · 一行的「变化字段取值」。

    只包含需要变化的字段；不变化的字段由本地模板原样拼回（不经过模型）。
    """

    model_config = ConfigDict(extra='allow')


class CoverageItem(BaseModel):
    """需求点 × 用例 覆盖度矩阵的一行（F8）"""

    requirement_point: str = ''
    covered_cases: List[str] = []
    missing: bool = False
    suggestion: str = ''

    @field_validator('requirement_point', 'suggestion', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('covered_cases', mode='before')
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split('\n') if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return []


# ---------------------------------------------------------------------------
# 需求分析三阶段（Skill1 拆解 / Skill2 提点 / Skill3 补全）
#
# 设计取向：字段刻意比旧 FunctionPointItem 少，单次输出 token 大幅下降，
# 让 JSON 遵循度较弱的模型也能一次吐对（决策⑤ 保留用户自选模型下的代偿措施）。
# ---------------------------------------------------------------------------

#: C01-C14 覆盖维度（与 ai_coverage_dims 表种子一致）
COVERAGE_DIMS = ['C%02d' % i for i in range(1, 15)]

#: 证据三分类：事实与猜测分离
EVIDENCE_TYPES = {
    'basis': '[需求依据]',
    'assumption': '[经验假设]',
    'pending': '[待确认]',
}


def normalize_evidence(value: Any) -> str:
    """把模型五花八门的证据写法收敛到 basis/assumption/pending 三值之一。

    模型常返回「需求依据」「经验假设」「待确认」「文档明确写了」等中文或带标记的变体，
    这里做一次宽松匹配；匹配不上默认 basis（宁可标注为依据，也不要凭空制造「假设」）。
    """
    s = _as_str(value).strip().strip('[]（）()【】')
    if not s:
        return 'basis'
    low = s.lower()
    if 'assumption' in low or '假设' in s or '推测' in s or '经验' in s:
        return 'assumption'
    if 'pending' in low or '待确认' in s or '待定' in s or '未知' in s or '疑问' in s or '缺失' in s:
        return 'pending'
    return 'basis'


def normalize_dims(value: Any) -> List[str]:
    """把 dims 收敛为 C01-C14 内的合法编号列表（去重、保序）。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = re.split(r'[,，;；\s、|]+', value)
    elif isinstance(value, list):
        raw = []
        for x in value:
            if isinstance(x, (int, float)):
                raw.append('C%02d' % int(x))
            else:
                raw.extend(re.split(r'[,，;；\s、|]+', str(x)))
    else:
        return []
    out: List[str] = []
    for x in raw:
        t = str(x).strip().upper().replace(' ', '')
        m = re.search(r'C?(\d{1,2})', t)
        if not m:
            continue
        code = 'C%02d' % int(m.group(1))
        if code in COVERAGE_DIMS and code not in out:
            out.append(code)
    return out


def normalize_batch(value: Any) -> str:
    """批次归一为 A/B/C：A=冒烟、B=重点、C=长尾。非法值归 C（最保守）。"""
    s = _as_str(value).strip().upper()[:1]
    return s if s in ('A', 'B', 'C') else 'C'


#: 测试点编号的合法形态：TP + 数字（TP1、TP12、tp3 都认）
_TP_REF_RE = re.compile(r'^TP\s*0*(\d{1,4})$', re.IGNORECASE)


def normalize_tp_refs(value: Any) -> List[str]:
    """把测试点回标编号收敛成 ['TP1','TP3'] 形态（去重、保序）。

    模型常写成 'tp1' / 'TP 1' / 1 / ['TP01']，这里统一成 `TP{数字}`（去掉前导零）。
    认不出来的直接丢弃 —— 回标编号要能反查数据库，存进去一个不存在的编号
    等于制造脏数据，比不标更糟。
    """
    if value is None:
        return []
    if isinstance(value, (int, float)):
        raw: List[Any] = [int(value)]
    elif isinstance(value, str):
        raw = [x for x in re.split(r'[,，;；\s、|]+', value) if x]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out: List[str] = []
    for x in raw:
        if isinstance(x, (int, float)):
            txt = 'TP%d' % int(x)
        else:
            txt = _as_str(x).strip()
            if not txt:
                continue
            m = _TP_REF_RE.match(txt)
            if not m:
                continue
            txt = 'TP%d' % int(m.group(1))
        if txt not in out:
            out.append(txt)
    return out


class EvidencedItem(BaseModel):
    """带证据标记的条目基类：所有拆解出来的规则/约束都要标出处"""

    content: str = ''
    evidence_type: str = 'basis'
    source: str = ''

    @field_validator('content', 'source', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _ev(cls, v):
        return normalize_evidence(v)


class ReqModule(BaseModel):
    """阶段1 产出：一个功能模块（含主链路、显性规则、隐性约束）"""

    module: str = ''
    summary: str = ''
    main_flow: List[str] = []
    explicit_rules: List[Dict[str, Any]] = []
    implicit_constraints: List[Dict[str, Any]] = []

    @field_validator('module', 'summary', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('main_flow', mode='before')
    @classmethod
    def _flow(cls, v):
        return _as_str_list(v)

    @field_validator('explicit_rules', 'implicit_constraints', mode='before')
    @classmethod
    def _rules(cls, v):
        """规则条目既接受对象也接受纯字符串；统一成 {content, evidence_type, source}。"""
        return _to_evidenced_list(v)


class StructuredTestPoint(BaseModel):
    """阶段2 产出：五要素 + 证据 + 覆盖维度 + 批次"""

    module: str = ''
    title: str = ''
    pre_condition: str = ''
    sample_input: str = ''
    expected: str = ''
    fail_impact: str = ''
    evidence_type: str = 'basis'
    source: str = ''
    dims: List[str] = []
    batch: str = 'C'

    @field_validator('module', 'title', 'pre_condition', 'sample_input',
                     'expected', 'fail_impact', 'source', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('evidence_type', mode='before')
    @classmethod
    def _ev(cls, v):
        return normalize_evidence(v)

    @field_validator('dims', mode='before')
    @classmethod
    def _dm(cls, v):
        return normalize_dims(v)

    @field_validator('batch', mode='before')
    @classmethod
    def _bt(cls, v):
        return normalize_batch(v)


class CoverageGap(BaseModel):
    """阶段3 产出：一个未覆盖维度的补全建议"""

    code: str = ''
    suggestion: str = ''
    priority: str = '中'

    @field_validator('suggestion', mode='before')
    @classmethod
    def _s(cls, v):
        return _as_str(v)

    @field_validator('code', mode='before')
    @classmethod
    def _cd(cls, v):
        return (normalize_dims(v) or [''])[0]

    @field_validator('priority', mode='before')
    @classmethod
    def _pr(cls, v):
        s = _as_str(v).strip()
        return s if s in ('高', '中', '低') else '中'


def _to_evidenced_list(v: Any) -> List[Dict[str, Any]]:
    """把规则列表统一成 [{content, evidence_type, source}]，非法项丢弃。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = [x.strip() for x in v.split('\n') if x.strip()]
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in v:
        if isinstance(item, dict):
            content = _as_str(item.get('content') or item.get('rule')
                              or item.get('text') or item.get('constraint')
                              or item.get('title') or '')
            if not content:
                continue
            out.append({
                'content': content,
                'evidence_type': normalize_evidence(item.get('evidence_type')
                                                    or item.get('evidence')
                                                    or item.get('type')),
                'source': _as_str(item.get('source') or item.get('quote')
                                  or item.get('ref') or ''),
            })
        elif isinstance(item, str):
            t = item.strip()
            # 模型有时把标记写在正文里「[需求依据]验证码5分钟」→ 拆出来
            ev = 'basis'
            for code, label in EVIDENCE_TYPES.items():
                if t.startswith(label):
                    ev = code
                    t = t[len(label):].strip('：: ')
                    break
            if t:
                out.append({'content': t, 'evidence_type': ev, 'source': ''})
    return out


def _as_str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r'[\n;；]+', v) if x.strip()]
    if isinstance(v, list):
        return [_as_str(x).strip() for x in v if _as_str(x).strip()]
    return []


def calc_coverage(test_points: List[Dict[str, Any]],
                  na_dims: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """从结构化测试点算 C01-C14 覆盖度。

    诚实口径（决策②）：分母是「14 - na 数」，且 na 明细与理由必须一并返回，
    前端必须并列显示「已覆盖 X/(14-N) · 不适用 N」，否则全标 NA 就能刷到 100%。
    """
    cnt = {c: 0 for c in COVERAGE_DIMS}
    batch_cnt = {'A': 0, 'B': 0, 'C': 0}
    for tp in test_points or []:
        for d in (tp.get('dims') or []):
            if d in cnt:
                cnt[d] += 1
        b = tp.get('batch') or 'C'
        batch_cnt[b if b in batch_cnt else 'C'] += 1
    na_dims = na_dims or {}
    covered = [d for d, n in cnt.items() if n > 0 and d not in na_dims]
    gaps = [d for d, n in cnt.items() if n == 0 and d not in na_dims]
    return {
        'dims': cnt,
        'batch': batch_cnt,
        'na': {d: na_dims.get(d, '') for d in na_dims},
        'covered': len(covered),
        'gaps': gaps,
        'na_count': len(na_dims),
        'total': len(COVERAGE_DIMS),
        'rate': round(len(covered) / max(1, len(COVERAGE_DIMS) - len(na_dims)) * 100, 1),
        # na 超过半数时前端与导出报告都要警告，防止刷覆盖率
        'na_warning': len(na_dims) > len(COVERAGE_DIMS) // 2,
    }


def collect_open_questions(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """汇总所有 [待确认] + [经验假设] 条目，供一键导出「给产品的疑问清单」。"""
    out: List[Dict[str, Any]] = []
    payload = payload or {}
    for m in payload.get('decompose', {}).get('modules', []) or []:
        for kind, key in (('显性规则', 'explicit_rules'), ('隐性约束', 'implicit_constraints')):
            for r in (m.get(key) or []):
                if r.get('evidence_type') in ('pending', 'assumption'):
                    out.append({
                        'module': m.get('module') or '',
                        'kind': kind,
                        'content': r.get('content') or '',
                        'evidence_type': r['evidence_type'],
                        'source': r.get('source') or '',
                    })
    for tp in payload.get('extract', {}).get('test_points', []) or []:
        if tp.get('evidence_type') in ('pending', 'assumption'):
            out.append({
                'module': tp.get('module') or '',
                'kind': '测试点',
                'content': tp.get('title') or '',
                'evidence_type': tp['evidence_type'],
                'source': tp.get('source') or '',
            })
    return out


def normalize_points(raw: Any) -> Dict[str, Any]:
    """兼容层（决策⑦）：旧结构 analyse_result → 新三阶段结构。

    存量 3 条历史结果里 test_points 是 List[str]，这里读时补齐五要素默认值，
    不改库里的 JSON；旧结果在覆盖地图里显示「历史结果（无维度数据）」，不参与覆盖率。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    old_points = raw.get('function_points') or []
    # 已经是新结构（有 stage_payload）就直接返回
    if raw.get('stage_payload'):
        return raw

    # 旧结构 questions 本就是「待确认」，映射成 pending 约束，
    # 否则存量 74 条待确认在产品清单里会凭空消失
    pending_map: Dict[str, List[Dict[str, Any]]] = {}
    for p in old_points:
        if not isinstance(p, dict):
            continue
        name = _as_str(p.get('module') or '未分类模块')
        items = [_as_str(q).strip() for q in (p.get('questions') or []) if _as_str(q).strip()]
        if items:
            pending_map.setdefault(name, []).extend(items)
    modules: List[Dict[str, Any]] = []
    seen = set()
    for p in old_points:
        if not isinstance(p, dict):
            continue
        name = _as_str(p.get('module') or '未分类模块')
        if name in seen:
            continue
        seen.add(name)
        modules.append({
            'module': name,
            'summary': _as_str(p.get('description') or ''),
            'main_flow': [],
            'explicit_rules': [],
            'implicit_constraints': [
                {'content': q, 'evidence_type': 'pending', 'source': '历史分析结果（questions 字段）'}
                for q in pending_map.get(name, [])
            ],
            'legacy': True,
        })
    test_points: List[Dict[str, Any]] = []
    for p in old_points:
        if not isinstance(p, dict):
            continue
        for tp in (p.get('test_points') or []):
            test_points.append({
                'module': _as_str(p.get('module') or ''),
                'title': _as_str(tp),
                'pre_condition': '', 'sample_input': '', 'expected': '',
                'fail_impact': '', 'source': '',
                'evidence_type': 'basis',
                'dims': [], 'batch': 'C', 'legacy': True,
            })
    return {
        'blocks_total': raw.get('blocks_total'),
        'blocks_analysed': raw.get('blocks_analysed'),
        'model': raw.get('model'),
        'elapsed_ms': raw.get('elapsed_ms'),
        'dropped': raw.get('dropped') or [],
        'legacy': True,
        'stage_payload': {
            'decompose': {'modules': modules},
            'extract': {'test_points': test_points},
            'audit': {'gaps': []},
        },
        'coverage_stat': calc_coverage(test_points, {}),
    }


class CwImpactReviewItem(BaseModel):
    """codewatch A 号位：单条「近似关联」的字段级复核结论。

    为什么 verdict 默认值必须是 unknown：
      模型漏判 / 输出被截断 / 该条没返回，回写时一律按「没判定」处理，
      **绝不能**归一成 irrelevant（不受影响）—— 那会把 AI 没判定伪装成
      「判定为不受影响」，制造静默假阴性（与 TpImpactItem 的 unknown 同理）。
    """

    idx: int = -1
    verdict: str = 'unknown'   # relevant / irrelevant / unknown
    level: str = 'keep'        # keep / must / suggest / drop
    reason: str = ''

    @field_validator('idx', mode='before')
    @classmethod
    def _idx(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    @field_validator('verdict', mode='before')
    @classmethod
    def _verdict(cls, v):
        s = _as_str(v).lower()
        # irrelevant 含 relevant 子串，必须先判
        if 'irrelevant' in s or '无关' in s or s in ('false', 'no', '0'):
            return 'irrelevant'
        if 'relevant' in s or '相关' in s or s in ('true', 'yes', '1'):
            return 'relevant'
        return 'unknown'

    @field_validator('level', mode='before')
    @classmethod
    def _level(cls, v):
        s = _as_str(v).lower()
        if s in ('must', '必须', '高', 'up'):
            return 'must'
        if s in ('suggest', '建议', '低', 'downgrade', 'down'):
            return 'suggest'
        if s in ('drop', '删除', '移除', 'remove'):
            return 'drop'
        return 'keep'

    @field_validator('reason', mode='before')
    @classmethod
    def _reason(cls, v):
        return _as_str(v)[:200]


class CwSceneItem(BaseModel):
    """codewatch B 号位：模板之外补充的一条业务语义场景（一行文本）。

    只收「一条场景」，本地负责去重、截断、编号与拼装；绝不让模型写整段报告。
    """

    idx: int = -1
    scene: str = ''

    @field_validator('idx', mode='before')
    @classmethod
    def _idx(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    @field_validator('scene', mode='before')
    @classmethod
    def _scene(cls, v):
        return _as_str(v)[:160]


REQUIRED_FIELDS = {
    CwImpactReviewItem: [],
    CwSceneItem: [('scene', '缺少场景文本')],
    CaseDraftItem: [('function_point', '缺少功能点'), ('steps', '缺少操作步骤'),
                    ('expected_result', '缺少预期结果')],
    # 接口场景：只强制 scene_name + 有断言。
    # ⚠️ 不强制 steps/expected —— 那是给人看的可读描述，引擎不消费；
    # 强制它们会让"参数与断言都对但描述没写好"的好场景被整条丢掉。
    ApiCaseSceneItem: [('scene_name', '缺少场景名称')],
    ApiCasePlanItem: [('dim', '缺少覆盖维度')],
    IssueItem: [('problem', '缺少问题描述')],
    CaseFromTextItem: [('steps', '缺少操作步骤'), ('expected_result', '缺少预期结果')],
    CaseSuggestItem: [],
    FunctionPointItem: [('function_point', '缺少功能点')],
    CoverageItem: [('requirement_point', '缺少需求点')],
    ReqModule: [('module', '缺少模块名')],
    StructuredTestPoint: [('title', '缺少测试点标题')],
    CoverageGap: [('suggestion', '缺少补全建议')],
    # 自定义 SQL 生成：字段计划必须有字段名，否则这一条判定没有意义
    SqlGenPlan: [('field', '缺少字段名')],
}


def validate_items(items: Any, model_cls, limit: Optional[int] = None
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """逐条校验：返回 (通过的字典列表, 被丢弃的原因列表)"""
    valid: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return valid, [{'reason': '返回结构不是数组（期望 JSON 数组）'}]

    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            dropped.append({'index': str(idx), 'reason': '该条不是对象'})
            continue
        try:
            obj = model_cls.model_validate(raw)
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            dropped.append({'index': str(idx), 'reason': '字段格式错误：%s' % first.get('msg', '未知')})
            continue
        missing = [msg for field, msg in REQUIRED_FIELDS.get(model_cls, [])
                   if not getattr(obj, field, '')]
        if missing:
            dropped.append({'index': str(idx), 'reason': '；'.join(missing)})
            continue
        valid.append(obj.model_dump())

    if limit and limit > 0:
        valid = valid[:limit]
    return valid, dropped


def extract_json_block(text: str) -> Optional[Any]:
    """从模型输出里尽力抠出 JSON：先整体解析，再退化为「第一个 { 到最后一个 }」"""
    import json

    if not text:
        return None
    cleaned = text.strip()
    # 去掉 ```json ... ``` 包裹
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`')
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    for left, right in (('{', '}'), ('[', ']')):
        start, end = cleaned.find(left), cleaned.rfind(right)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                continue
    return None
