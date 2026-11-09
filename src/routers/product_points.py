# -*- coding: utf-8 -*-
"""
产品功能点（需求分析拆分点 → 产品需求点）后端路由。

背景：需求分析阶段产出的「需求拆分点」(ai_requirements.stage_payload.decompose.modules)
需要批量转出为可跟踪的「产品功能点」，并归属到某个研发流程(dev_process)。
本路由只负责落库 / 列表 / 状态变更 / 删除；源数据由前端直接 POST 勾选项（决策：后端更轻）。

接口：
  GET  /process-options          归属项目下拉（dev_process id/code/title，带行级权限）
  POST /convert                  批量转出（按 process_id + requirement_id + source_key 去重：
                                 同一项目不重复，跨项目可重复转出）
  GET  /list                     分页 + 项目筛选 + 关键字
  POST /update                   改状态 / 标题
  POST /delete                   逻辑删除
  POST /create                   手动新增功能点（一级项目行「新增」）
  POST /add-test-point           给功能点追加测试点（二级功能点行「新增」）
  POST /project/update           一级项目行「改状态」：项目状态 / 批量功能点状态
  POST /project/delete           一级项目行「删除」：只删该项目下的功能点，流程保留
"""
import os
import json
import re
import time
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from src.comment import MySqlconection
from src.routers.auth import parse_token
from src.ai import tpmatch
from src.ai import coverage as coverage_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/product-function-points', tags=['product-function-points'])


def _insert_id(db, sql: str, params) -> int:
    """INSERT 并返回自增 id（DatabaseHandler 只返回影响行数）。

    与 ai_workbench._insert_id 同义，这里本地实现一份 —— 避免本模块为拿 id
    而 import 另一个路由模块（模块解耦铁律：跨模块纯函数各自本地实现）。
    """
    db._ensure_connection()  # noqa: SLF001
    with db.connection.cursor() as cur:
        cur.execute(sql, params)
        new_id = cur.lastrowid
    db.connection.commit()
    return new_id

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    data = MySqlconection.load_config_cached(CONFIG_PATH)
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )


def _uid(request: Request) -> Optional[int]:
    token = request.headers.get('accessToken')
    if not token:
        return None
    try:
        return parse_token(token)
    except Exception:
        return None


def _is_admin(db, uid) -> bool:
    if not uid:
        return False
    r = db.execute_query(
        "SELECT COUNT(*) c FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
        "WHERE ur.user_id=%s AND r.code='admin'", (uid,)
    )
    return bool(r and r[0]['c'] > 0)


def _envelope(data=None, total=None, msg='success', code=200, extra=None):
    out = {'code': code, 'msg': msg, 'data': data}
    if total is not None:
        out['total'] = total
    if extra:
        out['extra'] = extra
    return out


# ---------- 测试点归一化 ----------
# 落库格式统一为 JSON 数组字符串：[{"title":"...","dims":["C01","C13"],"batch":"A"}]
# 历史数据是纯字符串数组 '["点1","点2"]'（维度信息当时未落库），读取时按 dims=[] 兼容。
def _norm_tp_list(raw) -> list:
    """把 product_function_points.test_points 归一化为 [{'title','dims','batch','tp_ref','chg'}]。

    兼容三种输入：JSON 字符串 / 已解析的 list / None。

    `tp_ref` 是**回标编号**（形如 TP3），指向该测试点在来源需求文档
    `stage_payload.extract.test_points` 里的原始下标+1。
    它让「需求测试点 → 功能点测试点 → 用例」三者能串起来：
    用例回标 TP3 → 反查是哪个功能点的第几个测试点 → 写 product_point_cases.tp_indexes。
    老数据没有 tp_ref（存的是纯字符串数组），归一化后 tp_ref 为空，不影响既有逻辑。

    `chg` 是**测试点自持的「本次修改」标记**（11/04 晚新增）：
    写修改留痕时一并落库到该测试点项内。`chg.pending` 表示「这次真改带来的
    诊断资格还没被消费」——**只有它为真时「诊断」按钮才点亮**；
    **诊断成功（结论可信）后**就把 pending 置为 false → 按钮熄灭，
    必须再次真正修改内容才会重新点亮。
    ⚠️ 消费时机（11/05）：原为「应用到用例 / 替换为新用例成功后」，
       但「全部不受影响 → 跳过生成新用例」的诊断没有任何处理动作可做，
       会导致资格永远不被消费、按钮一直亮着 → 前移到诊断成功。
    详见 `_norm_chg` / `_tp_change_flags` / `_consume_tp_pending`。
    """
    items = None
    if raw is None or raw == '':
        return []
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except Exception:
            return []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            t = it.strip()
            if t:
                out.append({'title': t, 'dims': [], 'batch': '', 'tp_ref': ''})
            continue
        if not isinstance(it, dict):
            continue
        t = str(it.get('title') or it.get('text') or '').strip()
        if not t:
            continue
        dims = it.get('dims') or []
        if isinstance(dims, str):
            dims = [d.strip() for d in dims.split(',') if d.strip()]
        dims = [str(d).strip() for d in dims if str(d).strip()]
        batch = str(it.get('batch') or '').strip()
        item = {
            'title': t, 'dims': dims, 'batch': batch,
            'tp_ref': _norm_tp_ref(it.get('tp_ref')),
        }
        # ⚠️ chg 必须保留：写回 test_points 时它会随项一起落库，
        #    任何一次写回若丢掉它，「粘性」就失效了。
        chg = _norm_chg(it.get('chg'))
        if chg:
            item['chg'] = chg
        out.append(item)
    return out


def _norm_chg(raw) -> Optional[dict]:
    """归一化测试点自持的「本次修改」标记。

    结构：{'id': 留痕行id|None, 'time': 修改时间, 'old_title': 改前标题,
           'new_title': 改后标题, 'pending': 本次修改的诊断资格是否还没消费}
    合法条件：id 或 old_title 至少有一个非空（否则视为没有标记）。

    ⚠️ **pending 语义**（11/05 最终口径，取代「永久粘性」/「应用替换后消费」）：
       · True  = 这次真改刚发生、还没诊断成功 → 「诊断」按钮可点
       · False / 字段缺失 = 已诊断完成（拿到可信结论）或改造前的历史存量
                            → 「诊断」按钮置灰
       ⚠️ 消费时机 = **诊断成功**，不是「应用/替换成功」：上一轮新增
          「全部不受影响 → 跳过生成新用例」后，这类诊断没有任何处理动作可做，
          若仍等 apply/replace，按钮会一直亮着（可反复点、反复烧 AI）。
       只有 `update_test_point` 的**真改**分支才写入 pending=True；
       历史标记 / 回填脚本写出的标记不带该字段 → 天然视为「已诊断完成」→ 全部置灰。
       这样「没有再次修改就不能反复诊断」由数据本身保证，不依赖留痕反推。

    ⚠️ `new_title`：标记对应那一次修改的**改后标题**。
       它让「这次修改是否**真的改了内容**」可**自证**（old_title != new_title），
       不必依赖留痕行还在不在 → 空改（old==new）留下的历史标记也能被识别出来。
       老标记没有该字段 → 读取时回退到「查留痕核实」。
    """
    if not isinstance(raw, dict):
        return None
    cid = None
    try:
        if raw.get('id') is not None:
            cid = int(raw.get('id'))
    except (TypeError, ValueError):
        cid = None
    old_title = str(raw.get('old_title') or '').strip()
    new_title = str(raw.get('new_title') or '').strip()
    if not cid and not old_title:
        return None
    return {
        'id': cid,
        'time': str(raw.get('time') or ''),
        'old_title': old_title,
        'new_title': new_title,
        # 只认真的布尔 True：JSON 里缺字段 / false / 字符串一律按「已处理」处理
        'pending': raw.get('pending') is True,
    }


def _strip_chg(items: list) -> list:
    """去掉测试点项上的 chg 标记（用于「新建」路径，不接受外部传入的标记）。"""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            it = {k: v for k, v in it.items() if k != 'chg'}
        out.append(it)
    return out


def _norm_tp_ref(raw) -> str:
    """归一化回标编号：只认 TP+数字；认不出来返回空串。"""
    if raw is None:
        return ''
    m = re.match(r'^TP\s*0*(\d{1,4})$', str(raw).strip(), re.IGNORECASE)
    return 'TP%d' % int(m.group(1)) if m else ''


def _load_req_test_points(db, requirement_id) -> list:
    """取需求分析阶段产出的**原始测试点**（含 expected / sample_input / 前置条件等完整字段）。

    product_function_points.test_points 是「切分到功能点后的精简版」，
    实测只保留 title / dims / batch / tp_ref 四个字段 —— **没有预期结果、没有输入样例**，
    直接拿它喂 AI 等于只给了标题，生成的用例必然泛化。

    所以生成用例时回需求原文取五要素。

    ⚠️ 定位口径：原始测试点数组下标 = tp_ref 数字 - 1
    （tp_ref 是**需求内全局编号**，实测 199/199 与原文标题一致，可安全反查）。
    """
    if not requirement_id:
        return []
    row = db.execute_single_query(
        'SELECT stage_payload FROM ai_requirements WHERE id=%s', (int(requirement_id),))
    if not row:
        return []
    payload = row.get('stage_payload')
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return []
    if not isinstance(payload, dict):
        return []
    tps = (payload.get('extract') or {}).get('test_points') or []
    return tps if isinstance(tps, list) else []


def _norm_tp_title(raw) -> str:
    """归一化测试点标题用于比对：去空白（含全角空格）、统一大小写。

    比对时只认「去掉所有空白后完全相同」——
    宁可匹配不上（退回需求原文摘要），也不要匹配到一条**内容不同**的测试点，
    因为借来的五要素会直接喂给模型当写作依据，认错一条 = 整批用例写偏。
    """
    s = str(raw or '')
    s = re.sub(r'[\s　]+', '', s)
    return s.strip().lower()


def _find_req_tp_by_title(req_tps: list, title: str) -> Optional[dict]:
    """在需求原文测试点里找**标题完全一致**的一条（归一化空白后比对）。

    用途：手动新增的三级测试点没有来源编号（tp_ref 为空），
    但如果它的文字是用户从需求原文里抄的，就仍能借到那条原文测试点的
    五要素（前置/输入/预期），让 AI 有据可依，而不是对着标题瞎编。

    找不到返回 None —— 调用方负责退回「需求原文摘要」，
    绝不做模糊匹配（宁可信息少，也不能给错依据）。
    """
    if not req_tps or not title:
        return None
    target = _norm_tp_title(title)
    if not target:
        return None
    for it in req_tps:
        if not isinstance(it, dict):
            continue
        if _norm_tp_title(it.get('title')) == target:
            return it
    return None


def _resolve_raw_tp(db, requirement_id, tp: dict):
    """回需求原文取该测试点的**完整五要素**（前置/输入/预期/来源）。

    pfp 里只存了 title/dims/batch/tp_ref，缺预期/输入/前置条件 ——
    直接拿它喂 AI 等于只给了标题，生成的用例必然泛化。

    定位口径：
      · 有 tp_ref → 按「需求内原始下标 = 编号-1」反查
      · 无 tp_ref（手动新增）→ 按标题精确匹配原文，命中就借五要素
      · 都没有 → 返回 None，调用方退回需求原文摘要
    """
    tp_title = (tp.get('title') or '').strip()
    tp_ref = tp.get('tp_ref') or ''
    req_tps = _load_req_test_points(db, requirement_id)
    if not req_tps:
        return None
    if tp_ref:
        try:
            return req_tps[int(tp_ref[2:]) - 1]
        except Exception:
            return None
    return _find_req_tp_by_title(req_tps, tp_title)


def _load_req_doc(db, requirement_id, limit=1200) -> str:
    """取需求原文（content_text）摘要 —— 手动新增测试点的上下文兜底。

    手动测试点既没有回标编号、也未必抄自需求原文，
    这时唯一能给模型看的背景就是需求正文（截断，避免 prompt 失控）。
    """
    if not requirement_id:
        return ''
    row = db.execute_single_query(
        'SELECT content_text FROM ai_requirements WHERE id=%s', (int(requirement_id),))
    if not row:
        return ''
    txt = str(row.get('content_text') or '').strip()
    if len(txt) > limit:
        txt = txt[:limit] + '…（原文已截断）'
    return txt


def _dump_tp_list(items: list) -> str:
    """把 [{'title','dims','batch'}] 序列化为 JSON 数组字符串。"""
    try:
        return json.dumps(items, ensure_ascii=False)
    except Exception:
        return '[]'


def _tp_titles(items: list) -> str:
    """测试点标题拼串（用于同步到 function_points.description）。"""
    return '；'.join(str(x.get('title') or '') for x in items if x.get('title'))


def _tp_content_changed(old_tp: Optional[dict], new_tp: Optional[dict]) -> bool:
    """判断测试点内容**是否真的变了**（用于区分「空改」与「真正修改」）。

    ⚠️ 为什么需要这个（11/04 晚）：
       「打开修改弹窗 → 一个字没改 → 点保存」也会产生一次 `update-test-point` 请求。
       若把它当成一次「修改」，就会写一条 old==new 的留痕 + 打上「改过」标记 →
       「诊断」按钮被永久点亮，但根本不存在可比对的「改前内容」→ 诊断无从下手。
       用户要的是「**真正修改过**才能可点」，不是「点过保存就能点」。

    比较口径（与「内容是否等价」一致）：
       · `title`：去首尾空白后比较（前后空格不算改动）
       · `dims` ：**忽略顺序**（集合比较）—— 维度是集合语义
       · `batch`：去首尾空白后比较
       · `tp_ref`：不比较（回标编号不是用户可编辑的内容）

    ⚠️ `chg` 是内部标记，不是内容 → 一律排除。
    """
    def _norm(tp: Optional[dict]) -> tuple:
        tp = tp or {}
        dims = tp.get('dims') or []
        if isinstance(dims, str):
            dims = [d.strip() for d in dims.split(',') if d.strip()]
        dims = sorted({str(d).strip() for d in dims if str(d).strip()})
        return (
            str(tp.get('title') or '').strip(),
            tuple(dims),
            str(tp.get('batch') or '').strip(),
        )
    return _norm(old_tp) != _norm(new_tp)


def _chg_is_real_by_titles(old_title, new_title) -> bool:
    """按「改前标题 / 改后标题」判断是否一次**真正的修改**。

    判定口径（三条同时满足才算真改）：
       ① 改前内容**可辨识**（old_title 非空）—— 否则没有「改前内容」可比，
          无法证明发生过变更（历史脏数据 / 无改前快照的记录）。
       ② 改后内容非空。
       ③ 改前 != 改后 —— 内容确实变了。
    """
    ot = str(old_title or '').strip()
    nt = str(new_title or '').strip()
    return bool(ot) and bool(nt) and ot != nt


def _tp_change_is_real(change: Optional[dict]) -> bool:
    """一条留痕是否代表一次「真正的修改」（内容确实变了）。

    留痕表里的 `old_title` / `new_title` 相同 → 说明当初是「空改」，
    不能算作「修改过」→ 「诊断」按钮不该被点亮。

    ⚠️ 优先用 `old_tp_json` / `new_tp_json` 全量快照比较（能覆盖 dims/batch 的变化）；
       快照缺失或改前内容不可辨识时，才退回比较标题（`_chg_is_real_by_titles`）。
    """
    if not change:
        return False
    oj = change.get('old_tp_json')
    nj = change.get('new_tp_json')
    if oj and nj:
        try:
            a = json.loads(oj) if isinstance(oj, str) else oj
            b = json.loads(nj) if isinstance(nj, str) else nj
            if isinstance(a, dict) and isinstance(b, dict):
                # ⚠️ 改前内容必须可辨识（标题非空）才算「有改前内容」
                if str(a.get('title') or '').strip():
                    return _tp_content_changed(a, b)
        except Exception:  # noqa: BLE001
            pass
    return _chg_is_real_by_titles(change.get('old_title'), change.get('new_title'))


def _parse_tp_indexes(raw) -> Optional[list]:
    """解析 product_point_cases.tp_indexes。

    返回 None 表示「未指定测试点」（历史数据 / 整体关联）；
    返回 list 表示只覆盖这些测试点下标。
    ⚠️ 真实计数口径以列表接口为准：**只有非空 list 才计入测试点覆盖**，
    None / 空列表一律「不计入任何测试点覆盖」（见 list 接口 tp_case_counts 计算）。
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
    out = []
    for i in items:
        try:
            n = int(i)
        except Exception:
            continue
        if n >= 0 and n not in out:
            out.append(n)
    return out


def _dump_tp_indexes(indexes) -> Optional[str]:
    """把测试点下标列表序列化为 JSON 字符串；空列表/None → NULL（= 不计入任何测试点覆盖）。"""
    if indexes is None:
        return None
    if not isinstance(indexes, (list, tuple)):
        return None
    vals = []
    for i in indexes:
        try:
            n = int(i)
        except Exception:
            continue
        if n >= 0 and n not in vals:
            vals.append(n)
    if not vals:
        return None
    try:
        return json.dumps(vals)
    except Exception:
        return None


def _linked_case_ids(db, pfp_id: int, tp_index: int) -> List[int]:
    """取「显式关联了指定测试点」的用例 ID 列表。

    口径与前端 `caseCount` 完全一致：**只有 tp_indexes 里显式含本下标的才算关联**；
    tp_indexes 为 NULL/空（未指定测试点）的那批不计入 —— 那是「未指定」状态，
    不是「关联了本测试点」。
    """
    rows = db.execute_query(
        "SELECT pc.case_id, pc.tp_indexes FROM product_point_cases pc "
        "WHERE pc.pfp_id=%s AND pc.is_del=0", (pfp_id,)
    )
    out = []
    for r in rows or []:
        idxs = _parse_tp_indexes(r.get('tp_indexes'))
        if not idxs or tp_index not in idxs:
            continue
        try:
            cid = int(r.get('case_id'))
        except Exception:
            continue
        if cid > 0:
            out.append(cid)
    return out


# ---------------------------------------------------------------------------
# 测试点修改留痕（product_point_changes）的读取与匹配
#
# ⚠️ 为什么不能直接按 (pfp_id, tp_index) 查留痕：
#   product_point_changes.tp_index 是「**修改时刻**」的下标，而测试点被删除后
#   后续测试点的下标会整体回缩（-1）→ 同一个下标在不同时间指向不同的测试点。
#   直接按下标查会出现两种错误：
#     ① 漏判：改过的测试点因下标回缩而查不到留痕 → 被误判成「没改过」
#     ② 误判：回缩后新落到该下标的测试点被当成「改过」→ 诊断给出错的改前内容
#   匹配规则按可靠度排序：
#     ① tp_ref（需求回标编号，跨下标稳定）—— 需求来源的测试点走这条
#     ② 下标相同 + 改后标题与当前标题一致 —— 手动新增的测试点走这条
#     ③ 全表唯一标题兜底（下标回缩后靠标题认领；多条同名 → 视为不匹配）
# ---------------------------------------------------------------------------

_CHANGE_COLS = "id, pfp_id, tp_index, tp_ref, old_title, new_title, create_time"


def _load_pfp_changes(db, pfp_ids: List[int]) -> Dict[int, List[dict]]:
    """批量取若干功能点的修改留痕，按 id 倒序（新的在前）。"""
    if not pfp_ids:
        return {}
    ph = ', '.join(['%s'] * len(pfp_ids))
    rows = db.execute_query(
        "SELECT %s FROM product_point_changes WHERE pfp_id IN (%s) AND is_del=0 "
        "ORDER BY id DESC" % (_CHANGE_COLS, ph),
        tuple(pfp_ids),
    )
    out: Dict[int, List[dict]] = {}
    for r in rows or []:
        out.setdefault(int(r['pfp_id']), []).append(r)
    return out


def _match_change_to_tp(changes: List[dict], tp_items: List[dict],
                        tp_index: int) -> Optional[dict]:
    """在修改留痕里找出属于「当前这条测试点」的最新一条记录；找不到返回 None。

    tp_items 必须是 `_norm_tp_list` 的结果（含 tp_ref / title）。
    changes 需按 id 倒序（新的在前）—— 返回的是**最近一次**修改。
    """
    if not changes or not (0 <= tp_index < len(tp_items)):
        return None
    tp = tp_items[tp_index] or {}
    cur_ref = (tp.get('tp_ref') or '').strip()
    cur_title = (tp.get('title') or '').strip()

    # ① tp_ref 命中：需求来源的测试点，编号不随下标回缩变化 → 最可靠
    if cur_ref:
        for c in changes:
            if (c.get('tp_ref') or '').strip() == cur_ref:
                return c
    # ② 下标相同 + 改后标题与当前标题一致：手动新增的测试点（无 tp_ref）
    for c in changes:
        try:
            idx = int(c.get('tp_index'))
        except (TypeError, ValueError):
            continue
        if idx != int(tp_index):
            continue
        if (c.get('new_title') or '').strip() == cur_title:
            return c
    # ③ 兜底：下标回缩后靠标题认领。
    #    必须「该标题在本功能点内唯一」且「该留痕唯一」→ 否则无法确定是哪条测试点，不认。
    dup_title = sum(1 for t in tp_items if (t.get('title') or '').strip() == cur_title)
    if dup_title <= 1:
        cand = [c for c in changes if (c.get('new_title') or '').strip() == cur_title]
        if len(cand) == 1:
            return cand[0]
    return None


def _tp_change_flags(db, pfp_id: int, tp_items: List[dict],
                     changes: Optional[List[dict]] = None) -> List[dict]:
    """算出每个测试点的「是否还有**待诊断的修改**」标记（与下标对齐）。

    每项：{changed, change_id, change_time, old_title, new_title}

    ⚠️ **一次性语义**（11/05 最终口径，取代「改过一次永久可点」）：
       一次「真正的内容修改」只赋予**一次诊断资格**：
         真改 → chg.pending=True → 按钮点亮
         **诊断成功且结论可信** → pending=False → 按钮立即熄灭
         之后**没有再次真改** → 一直置灰，必须再改一次内容才重新点亮。
       ⚠️ 消费在 `diagnose_tp_impact` 里完成（不依赖 apply/replace 是否执行），
          否则「全部不受影响 → 跳过生成新用例」的诊断没有处理动作 → 按钮永远亮。
       例外：AI 判定失败 / 存在 unknown → 结论不可信，不消费，保留重试入口。

    ⚠️ 判定**只看测试点自持的 `chg` 标记**，绝不从留痕表反推：
       ① 没有 `chg`（改造前老数据）/ `chg.pending` 为假 → 一律 false；
       ② 有 pending 但标记本身是空改（old_title == new_title）→ false；
       ③ 只有 pending=True **且**确实是真改 → true。
       历史留痕属于「上一次已经处理过的修改」，不该再点亮按钮；
       反推还会因下标回缩 / 改名 / 同名 / 留痕软删而翻转。

    ⚠️ 存量数据：改造前写入的 `chg` 都没有 `pending` 字段 → 按「已处理」全部置灰。
       `changes` 参数仅用于校验 pending 标记本身是不是空改，不参与点亮判定。
    """
    if changes is None:
        changes = _load_pfp_changes(db, [pfp_id]).get(int(pfp_id), []) or []
    changes_by_id = {}
    for c in changes:
        try:
            changes_by_id[int(c['id'])] = c
        except (TypeError, ValueError, KeyError):
            continue

    def _marker_is_real(chg: dict) -> bool:
        """粘性标记对应的修改是不是「真正改过」。"""
        # ① 标记自带改后标题 → 自证（同一口径：改前可辨识 + 改前 != 改后）
        nt = str(chg.get('new_title') or '').strip()
        if nt:
            return _chg_is_real_by_titles(chg.get('old_title'), nt)
        # ② 老标记没有 new_title → 查留痕核实
        cid = chg.get('id')
        if cid:
            try:
                row = changes_by_id.get(int(cid))
            except (TypeError, ValueError):
                row = None
            if row is not None:
                return _tp_change_is_real(row)
        # ③ 留痕查不到（软删/物理删）→ 保守当真改
        return True

    flags = []
    for tp in tp_items:
        tp = tp or {}
        chg = tp.get('chg') or {}
        # ⚠️ 只有「本次真改产生的、还没被消费的诊断资格」才点亮按钮。
        #    没有标记 / 已消费 / 空改标记 → 一律置灰，**绝不**回退到留痕反推
        #    （历史留痕是上一次已处理完的修改，反推会让按钮「又亮起来」）。
        real = chg.get('pending') is True and _marker_is_real(chg)
        flags.append({
            'changed': bool(real),
            'change_id': chg.get('id') if real else None,
            'change_time': (chg.get('time') or '') if real else '',
            'old_title': (chg.get('old_title') or '') if real else '',
            'new_title': (chg.get('new_title') or tp.get('title') or '') if real else '',
        })
    return flags


def _consume_tp_pending(db, pfp_id: int, tp_index: int) -> bool:
    """消费「本次修改」的诊断资格：把该测试点 `chg.pending` 置为 False 并落库。

    **主调用方 = `diagnose_tp_impact` 诊断成功（结论可信）时** —— 只要点过诊断
    并拿到可信结论，本次修改就算处理完，「诊断」按钮立即熄灭，
    直到用户下一次真正修改该测试点内容。
    「应用到用例」/「替换为新用例」成功后仍会调用一次，作为**幂等兜底**
    （诊断那次已消费时这里直接返回 False，不会重复写库）。

    ⚠️ AI 判定失败 / 存在 unknown（结论不可信）时调用方**不要**调本函数，
       保留重试入口（与弹窗「开始诊断」的重试例外同口径）。
    ⚠️ 幂等：没有标记 / 已消费 / 下标越界，都安全返回 False 且不改库。
    ⚠️ 只改 `pending`，保留 id/time/old_title/new_title —— 改前锚点仍可追溯。
    ⚠️ 单条 UPDATE（`execute_update` 自带 commit），失败只记日志、不阻断主流程；
       调用方按返回值决定是否把 `changed=false` 带给前端。
    """
    try:
        row = db.execute_single_query(
            "SELECT test_points FROM product_function_points WHERE id=%s AND is_del=0",
            (pfp_id,),
        )
        if not row:
            return False
        items = _norm_tp_list(row.get('test_points'))
        idx = int(tp_index)
        if not (0 <= idx < len(items)):
            return False
        chg = items[idx].get('chg') or {}
        if chg.get('pending') is not True:
            return False
        items[idx]['chg'] = {**chg, 'pending': False}
        db.execute_update(
            "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
            (_dump_tp_list(items), pfp_id),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning('消费测试点诊断资格失败 pfp=%s idx=%s: %s', pfp_id, tp_index, e)
        return False


# ---------- 请求模型 ----------
class ProcessOptionsReq(BaseModel):
    pass


class ConvertItem(BaseModel):
    source_key: str
    title: str
    module: str = ''
    # 测试点：JSON 数组字符串。元素可以是纯字符串（旧格式）或对象
    # {"title":"...","dims":["C01","C13"],"batch":"A"}（新格式，带六维覆盖维度）。
    test_points: Optional[str] = None


class ConvertReq(BaseModel):
    requirement_id: Optional[int] = None
    process_id: int
    items: List[ConvertItem]


class UpdateReq(BaseModel):
    id: int
    status: Optional[int] = None
    title: Optional[str] = None
    module: Optional[str] = None


class DeleteReq(BaseModel):
    id: int


class ProjectUpdateReq(BaseModel):
    """一级项目行「改状态」：项目状态与功能点状态可分别传，不传表示该项不改"""
    process_id: int
    process_status: Optional[int] = None   # dev_process.status：0进行中 1已完成 2已终止
    point_status: Optional[int] = None     # 批量改该项目下功能点：0待处理 1进行中 2已完成 3已关闭


class ProjectDeleteReq(BaseModel):
    process_id: int


class CreatePointReq(BaseModel):
    """手动新增产品功能点（挂在指定研发流程下，一级项目行的「新增」）"""
    process_id: int
    title: str
    module: str = ''
    test_points: List[str] = []      # 初始测试点，可为空


class AddTestPointReq(BaseModel):
    """给指定功能点追加测试点（二级功能点行的「新增」）"""
    id: int
    test_points: List[str] = []


class UpdateTestPointReq(BaseModel):
    """修改指定功能点的某一条测试点（三级测试点行的「修改」）"""
    id: int
    index: int
    text: str


class DeleteTestPointReq(BaseModel):
    """删除指定功能点的某一条测试点（三级测试点行的「删除」）"""
    pfp_id: int
    index: int


class ReorderTestPointsReq(BaseModel):
    """三级测试点行「拖拽调整顺序」。

    order 是**新顺序**，元素为**重排前**的测试点下标（0 基），必须是
    0..n-1 的一个完整排列（n = 当前 test_points 长度）。
    例：原来 [A, B, C]（下标 0,1,2），把 C 拖到最前 → order=[2, 0, 1]。
    """
    pfp_id: int
    order: List[int]


class AutoMatchReq(BaseModel):
    """「自动匹配」：为一批用例自动判定它们覆盖该功能点的哪几个测试点。

    只产出**建议**，不写库 —— 写入由用户确认后走 link-cases / batch-tp。
    """
    pfp_id: int
    case_ids: List[int] = []
    use_ai: bool = True        # 前两层定不下来时是否调模型兜底（消耗额度）
    ai_limit: int = 20         # 单次最多送多少条给模型，避免一次请求过大


class BatchTpReq(BaseModel):
    """批量写某功能点下多条关联的覆盖测试点范围。

    items 里可带 auto=True 的条目 → 其 tp_indexes 由后端自动判定决定。
    """
    pfp_id: int
    items: List[Dict[str, Any]] = []    # [{'case_id': 1, 'tp_indexes': [0,2]}]
    auto_match: bool = False            # 是否对 auto=True 的条目自动判定覆盖范围
    use_ai: bool = True                  # 自动判定时前两层定不下来是否调模型兜底


# ---------- 可见流程子查询（行级权限） ----------
def _visible_process_sql(uid, admin) -> str:
    if admin:
        return '1=1'
    return (
        'p.process_id IN (SELECT dp.id FROM dev_process dp WHERE dp.is_del=0 AND '
        '(dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n '
        'WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0)))' % (uid, uid)
    )


def _process_visible(db, uid, admin, process_id) -> bool:
    """该用户对研发流程是否有操作权限。

    流程必须存在且未删除（admin 也一样，否则改不存在的 id 会静默返回成功）；
    admin 全量可操作，否则需为流程发起人或节点负责人。
    """
    own = '1=1' if admin else (
        '(dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n '
        'WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0))'
    )
    args = (process_id,) if admin else (process_id, uid, uid)
    r = db.execute_query(
        "SELECT COUNT(*) c FROM dev_process dp WHERE dp.id=%s AND dp.is_del=0 AND " + own,
        args,
    )
    return bool(r and r[0]['c'] > 0)


@router.get('/process-options')
async def process_options(request: Request):
    uid = _uid(request)
    with get_db_handler() as db:
        admin = _is_admin(db, uid)
        if admin:
            rows = db.execute_query(
                "SELECT id, code, title FROM dev_process WHERE is_del=0 ORDER BY create_time DESC"
            )
        else:
            rows = db.execute_query(
                "SELECT id, code, title FROM dev_process dp WHERE dp.is_del=0 AND "
                "(dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n "
                "WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0)) "
                "ORDER BY dp.create_time DESC", (uid, uid)
            )
        return _envelope([
            {'id': r['id'], 'code': r['code'], 'title': r['title']} for r in rows
        ])


@router.post('/convert')
async def convert(req: ConvertReq, request: Request):
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.process_id:
        return _envelope(None, None, '请选择归属项目', 400)
    if not req.items:
        return _envelope(None, None, '请勾选至少一项', 400)
    with get_db_handler() as db:
        proc = db.execute_single_query(
            "SELECT id, code, title, project_id FROM dev_process WHERE id=%s AND is_del=0",
            (req.process_id,),
        )
        if not proc:
            return _envelope(None, None, '归属项目不存在', 404)
        # 研发流程关联的测试项目(test_case_project.id)：有则同步功能点到「需求变更影响-功能点管理」
        proc_project_id = proc.get('project_id')
        proc_code = proc.get('code') or ''
        inserted = 0
        skipped = 0
        synced = 0
        for it in req.items:
            title = (it.title or '').strip()
            if not title:
                continue
            if req.requirement_id:
                # 去重按「项目 + 需求 + 拆分点」三键：
                # 同一项目下不重复（防手滑重复转出），
                # 但允许把同一需求拆分点转到不同项目（跨项目复用）。
                ex = db.execute_single_query(
                    "SELECT id FROM product_function_points "
                    "WHERE process_id=%s AND requirement_id=%s AND source_key=%s AND is_del=0",
                    (req.process_id, req.requirement_id, it.source_key),
                )
                if ex:
                    skipped += 1
                    continue
            tp = it.test_points
            if tp is None or tp == '':
                tp = '[]'
            # 归一化为 [{'title','dims','batch'}] 结构化格式（六维维度随测试点一起落库）
            # ⚠️ 剥除外部可能传入的 chg —— 新建功能点的测试点一律「从未修改过」
            tp_items = _strip_chg(_norm_tp_list(tp))
            tp = _dump_tp_list(tp_items)
            db.execute_update(
                """INSERT INTO product_function_points
                   (process_id, requirement_id, source_key, title, module, test_points, status, creator_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 0, %s)""",
                (req.process_id, req.requirement_id, it.source_key, title,
                 it.module or '', tp, uid),
            )
            new_pfp_id = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
            inserted += 1
            # 同步到「项目管理-需求变更影响-功能点管理」(function_points 表)：
            # 研发流程若关联了测试项目，则在该项目下新增一条功能点基线，便于后续影响分析复用。
            if proc_project_id:
                ex_fp = db.execute_single_query(
                    "SELECT id FROM function_points WHERE source_pfp_id=%s AND is_del=0",
                    (new_pfp_id,),
                )
                if not ex_fp:
                    desc = ''
                    if tp_items:
                        desc = '测试点：' + _tp_titles(tp_items)
                    db.execute_update(
                        "INSERT INTO function_points "
                        "(name, project_id, module, description, baseline, source_pfp_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (title, proc_project_id, it.module or '', desc, proc_code, new_pfp_id),
                    )
                    synced += 1
    msg = f'已新增 {inserted} 条，跳过重复 {skipped} 条'
    if synced:
        msg += f'；已同步至需求变更影响-功能点管理 {synced} 条'
    return _envelope(
        {'inserted': inserted, 'skipped': skipped, 'synced': synced},
        None, msg,
    )


@router.get('/list')
async def list_points(pageNo: int = 1, pageSize: int = 20, process_id: int = 0,
                      keyword: str = '', request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            admin = _is_admin(db, uid)
            conds = ['p.is_del=0']
            args = []
            if process_id:
                conds.append('p.process_id=%s')
                args.append(process_id)
            if keyword:
                conds.append('(p.title LIKE %s OR p.module LIKE %s)')
                args.extend([f'%{keyword}%', f'%{keyword}%'])
            if not admin:
                conds.append(_visible_process_sql(uid, False))
            where = ' AND '.join(conds)
            # 一级分页口径 = 命中的「项目(process_id)」数量。
            # 不能按平铺功能点行分页：否则同一项目的多个功能点会被拆到不同页、三级树被割裂。
            total = db.execute_query(
                f"SELECT COUNT(DISTINCT p.process_id) c FROM product_function_points p WHERE {where}",
                tuple(args),
            )[0]['c']
            # 全局汇总（顶部「共 N 个项目 · M 功能点 · K 测试点」），不受分页影响
            all_rows = db.execute_query(
                f"SELECT test_points FROM product_function_points p WHERE {where}", tuple(args))
            total_points = len(all_rows)
            total_tps = sum(len(_norm_tp_list(r['test_points'])) for r in all_rows)
            # 项目级分页：取当前页命中的 process_id 集合（pageSize<=0 取全部，向后兼容）
            if pageSize and pageSize > 0:
                proj_rows = db.execute_query(
                    f"SELECT DISTINCT p.process_id AS pid FROM product_function_points p WHERE {where} "
                    f"ORDER BY p.process_id DESC LIMIT %s OFFSET %s",
                    tuple(args + [pageSize, (pageNo - 1) * pageSize]),
                )
            else:
                proj_rows = db.execute_query(
                    f"SELECT DISTINCT p.process_id AS pid FROM product_function_points p WHERE {where} "
                    f"ORDER BY p.process_id DESC", tuple(args))
            pids = [r['pid'] for r in proj_rows]
            if not pids:
                return _envelope([], total, extra={'total_points': total_points, 'total_tps': total_tps})
            ph = ', '.join(['%s'] * len(pids))
            # 仅取这些项目下的功能点（复用 where 保留关键字/可见性过滤，再限制到当前页项目）。
            # 关键字命中的功能点若跨项目，整组项目都会出现（树展示需要父子完整）。
            rows = db.execute_query(
                f"""SELECT p.id, p.process_id, p.requirement_id, p.source_key, p.title, p.module,
                           p.test_points, p.status, p.creator_id, p.create_time,
                           dp.code AS process_code, dp.title AS process_title,
                           dp.status AS process_status,
                           u.realname AS creator_name
                    FROM product_function_points p
                    LEFT JOIN dev_process dp ON dp.id = p.process_id
                    LEFT JOIN users u ON u.id = p.creator_id
                    WHERE {where} AND p.process_id IN ({ph})
                    ORDER BY p.create_time DESC""",
                tuple(args + pids),
            )
            for r in rows:
                tp_items = _norm_tp_list(r['test_points'])
                r['test_points_count'] = len(tp_items)
                # tp_items：结构化测试点 [{title,dims,batch}]，前端直接用；
                # test_points 保留原始 JSON 字符串（向后兼容旧消费方）
                r['tp_items'] = tp_items
            # 批量统计每个功能点已关联的正式用例数（避免 N+1）
            if rows:
                ids = [r['id'] for r in rows]
                ph2 = ', '.join(['%s'] * len(ids))
                cnts = db.execute_query(
                    f"SELECT pfp_id, COUNT(*) c FROM product_point_cases "
                    f"WHERE pfp_id IN ({ph2}) AND is_del=0 GROUP BY pfp_id",
                    tuple(ids),
                )
                cnt_map = {x['pfp_id']: x['c'] for x in cnts}
                # 测试点级覆盖：一条关联 tp_indexes 为空 = 覆盖该功能点全部测试点；
                # 非空则只覆盖列出的测试点下标。
                links = db.execute_query(
                    f"SELECT pfp_id, tp_indexes FROM product_point_cases "
                    f"WHERE pfp_id IN ({ph2}) AND is_del=0",
                    tuple(ids),
                )
                cov_map = {}
                # 每个测试点被多少条用例覆盖（第三级「关联用例」列显示条数用）
                tp_cnt_map = {}
                # 未指定测试点的关联条数（自动识别失败 / 历史未指定）
                # → 三级测试点列表下方自动追加一行「未指定」，供人工指定测试点
                tp_unspec_map = {}
                for x in links:
                    s = set(cov_map.get(x['pfp_id'], set()))
                    idxs = _parse_tp_indexes(x['tp_indexes'])
                    # 仅当「显式指定了测试点下标」才计入覆盖；
                    # None（历史未指定 / 整体关联）或空列表 = 不计入任何测试点覆盖。
                    # 这样「只要关联了用例就显示已覆盖」的假覆盖被根治：
                    # 历史未指定关联立即全部变为缺口，需逐条重新映射。
                    if idxs:
                        s.update(idxs)
                        cm = tp_cnt_map.setdefault(x['pfp_id'], {})
                        for i in idxs:
                            cm[i] = cm.get(i, 0) + 1
                    else:
                        tp_unspec_map[x['pfp_id']] = tp_unspec_map.get(x['pfp_id'], 0) + 1
                    cov_map[x['pfp_id']] = s
                    tp_cnt_map.setdefault(x['pfp_id'], {})
                for r in rows:
                    r['link_count'] = cnt_map.get(r['id'], 0)
                    total_tp = len(r['tp_items'])
                    cov = cov_map.get(r['id'], set())
                    covered_idx = sorted(i for i in cov if 0 <= i < total_tp)
                    r['tp_total'] = total_tp
                    r['tp_covered_count'] = len(covered_idx)
                    r['tp_gap_count'] = total_tp - len(covered_idx)
                    r['tp_covered_indexes'] = covered_idx
                    # 每个测试点的关联用例条数（下标对齐 tp_items）
                    cm = tp_cnt_map.get(r['id'], {})
                    r['tp_case_counts'] = [cm.get(i, 0) for i in range(total_tp)]
                    # 未指定测试点的关联条数（前端据此在三级列表下追加「未指定」行）
                    r['tp_unspecified_count'] = tp_unspec_map.get(r['id'], 0)
                # 测试点修改留痕 → 每个测试点「是否还有待诊断的修改」标记（与下标对齐）
                # ⚠️ 前端据此决定「诊断」按钮是否可点：只有「本次真改且还没处理完」
                #    才点亮；没改过 / 已处理完 → 置灰，别让用户白跑一次 AI。
                # ⚠️ 表不存在（生产库还没跑迁移脚本）时**不能让整个列表挂掉** →
                #    降级为「全部无待诊断修改」，诊断按钮全置灰（保守方向，安全）。
                try:
                    chg_map = _load_pfp_changes(db, ids)
                except Exception as e:  # noqa: BLE001
                    logger.warning('读取测试点修改留痕失败（诊断按钮将全部置灰）: %s', e)
                    chg_map = {}
                for r in rows:
                    r['tp_change_flags'] = _tp_change_flags(
                        db, int(r['id']), r.get('tp_items') or [],
                        chg_map.get(int(r['id']), []))
            return _envelope(rows, total, extra={'total_points': total_points, 'total_tps': total_tps})
    except Exception as e:  # noqa: BLE001
        logger.warning('产品功能点列表查询失败: %s', e)
        return _envelope(None, 0, f'查询失败: {e}', 500)


@router.post('/update')
async def update_point(req: UpdateReq, request: Request):
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id FROM product_function_points WHERE id=%s AND is_del=0", (req.id,)
        )
        if not row:
            return _envelope(None, None, '记录不存在', 404)
        sets = []
        args = []
        if req.status is not None:
            sets.append('status=%s')
            args.append(int(req.status))
        if req.title is not None and req.title.strip():
            sets.append('title=%s')
            args.append(req.title.strip())
        if req.module is not None:
            sets.append('module=%s')
            args.append(req.module.strip())
        if not sets:
            return _envelope(None, None, '无可更新字段', 400)
        args.append(req.id)
        db.execute_update(
            f"UPDATE product_function_points SET {', '.join(sets)} WHERE id=%s", tuple(args)
        )
        return _envelope(None, None, '已更新')


@router.post('/delete')
async def delete_point(req: DeleteReq, request: Request):
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id FROM product_function_points WHERE id=%s AND is_del=0", (req.id,)
        )
        if not row:
            return _envelope(None, None, '记录不存在', 404)
        db.execute_update(
            "UPDATE product_function_points SET is_del=1 WHERE id=%s", (req.id,)
        )
        return _envelope(None, None, '已删除')


@router.post('/project/update')
async def project_update(req: ProjectUpdateReq, request: Request):
    """一级项目行「改状态」：改项目状态(dev_process.status) / 批量改该项目下功能点状态。

    两项可分别传、传了才改；都不传则不做任何修改。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, req.process_id):
            return _envelope(None, None, '项目不存在或无权操作', 404)
        changed = []
        point_n = 0
        if req.process_status is not None:
            if req.process_status not in (0, 1, 2):
                return _envelope(None, None, '项目状态取值非法', 400)
            db.execute_update(
                "UPDATE dev_process SET status=%s, update_time=NOW() WHERE id=%s AND is_del=0",
                (req.process_status, req.process_id),
            )
            changed.append('项目状态')
        if req.point_status is not None:
            if req.point_status not in (0, 1, 2, 3):
                return _envelope(None, None, '功能点状态取值非法', 400)
            point_n = db.execute_update(
                "UPDATE product_function_points SET status=%s, update_time=NOW() "
                "WHERE process_id=%s AND is_del=0",
                (req.point_status, req.process_id),
            )
            changed.append('%d 个功能点状态' % point_n)
        if not changed:
            return _envelope(None, None, '未选择要修改的内容', 400)
        return _envelope({'points': point_n}, None, '已更新' + '、'.join(changed))


@router.post('/project/delete')
async def project_delete(req: ProjectDeleteReq, request: Request):
    """一级项目行「删除」：只逻辑删除该项目下的产品功能点，研发流程本体保留。

    研发流程(dev_process)不在本模块删，避免影响「研发流程」看板与节点数据。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, req.process_id):
            return _envelope(None, None, '项目不存在或无权操作', 404)
        n = db.execute_update(
            "UPDATE product_function_points SET is_del=1, update_time=NOW() "
            "WHERE process_id=%s AND is_del=0",
            (req.process_id,),
        )
        return _envelope({'deleted': n}, None, '已删除该项目下 %d 个产品功能点（研发流程保留）' % n)


@router.post('/create')
async def create_point(req: CreatePointReq, request: Request):
    """一级项目行「新增」：手动新增一个产品功能点挂到该项目下。

    与 convert（从需求拆分点转出）不同：这里没有需求来源，requirement_id 留空，
    source_key 用 manual_ 前缀保证唯一（convert 的幂等去重不会误判）。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    title = (req.title or '').strip()
    if not title:
        return _envelope(None, None, '请填写功能点名称', 400)
    with get_db_handler() as db:
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, req.process_id):
            return _envelope(None, None, '项目不存在或无权操作', 404)
        ex = db.execute_single_query(
            "SELECT id FROM product_function_points "
            "WHERE process_id=%s AND title=%s AND is_del=0", (req.process_id, title)
        )
        if ex:
            return _envelope(None, None, '该项目下已存在同名功能点', 400)
        source_key = 'manual_%s_%s' % (uid, int(time.time() * 1000))
        db.execute_update(
            """INSERT INTO product_function_points
               (process_id, requirement_id, source_key, title, module, test_points, status, creator_id)
               VALUES (%s, NULL, %s, %s, %s, %s, 0, %s)""",
            (req.process_id, source_key, title, req.module or '',
             _dump_tp_list(_strip_chg(_norm_tp_list(req.test_points or []))), uid),
        )
        return _envelope({'id': db.execute_single_query(
            "SELECT id FROM product_function_points WHERE source_key=%s", (source_key,))['id']},
            None, '已新增功能点「%s」' % title)


@router.post('/add-test-point')
async def add_test_point(req: AddTestPointReq, request: Request):
    """二级功能点行「新增」：给该功能点追加测试点（需求依据）。

    test_points 存的是结构化 JSON（title/dims/batch），这里读出→去重→追加→回写。
    手动新增的测试点没有维度信息，dims 为空。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    new_tps = [str(t).strip() for t in (req.test_points or []) if t and str(t).strip()]
    if not new_tps:
        return _envelope(None, None, '请填写测试点内容', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, test_points FROM product_function_points "
            "WHERE id=%s AND is_del=0", (req.id,)
        )
        if not row:
            return _envelope(None, None, '记录不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        old = _norm_tp_list(row['test_points'])
        existed = {str(x.get('title') or '').strip() for x in old}
        added = [t for t in new_tps if t not in existed]
        skipped = len(new_tps) - len(added)
        if not added:
            return _envelope({'added': 0, 'skipped': skipped, 'total': len(old)}, None,
                             '测试点已存在，未新增', 400)
        merged = old + [{'title': t, 'dims': [], 'batch': '', 'tp_ref': ''}
                        for t in added]
        db.execute_update(
            "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
            (_dump_tp_list(merged), req.id),
        )
        tip = '（跳过 %d 个重复）' % skipped if skipped else ''
        return _envelope({'added': len(added), 'skipped': skipped, 'total': len(merged)}, None,
                         '已新增 %d 个测试点%s' % (len(added), tip))


@router.post('/update-test-point')
async def update_test_point(req: UpdateTestPointReq, request: Request):
    """三级测试点行「修改」：改某一条测试点的文本（保留其维度信息）。

    ⚠️ 同时写一行 `product_point_changes` 留痕（old/new 标题 + 各自的完整 tp_json）。
    没有改前锚点，「新旧对比」就是假的 —— 只能拿新测试点重新体检一遍旧用例。
    （表结构见 add_product_point_changes.py）
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    text = (req.text or '').strip()
    if not text:
        return _envelope(None, None, '请填写测试点内容', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, test_points FROM product_function_points "
            "WHERE id=%s AND is_del=0", (req.id,)
        )
        if not row:
            return _envelope(None, None, '记录不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        old = _norm_tp_list(row['test_points'])
        if req.index < 0 or req.index >= len(old):
            return _envelope(None, None, '测试点不存在', 404)
        old_tp = dict(old[req.index])            # 改前快照（浅拷贝够用：只有 title/dims/batch/tp_ref/chg 几个标量字段）
        old_title = str(old_tp.get('title') or '')
        old[req.index]['title'] = text
        new_tp = dict(old[req.index])             # 改后快照
        # ⚠️ chg 是内部标记（不是测试点内容），不进留痕快照，避免快照里带上一轮的脏标记
        old_tp.pop('chg', None)
        new_tp.pop('chg', None)
        # ---- ⚠️ 判定「是否真的改了」----
        # 「打开弹窗 → 一个字没改 → 点保存」也会走到这里。若把它当成一次「修改」，
        # 就会写一条 old==new 的空改留痕 + 打上「改过」标记 → 「诊断」按钮被永久点亮，
        # 但根本不存在可比对的「改前内容」→ 诊断无从下手。
        # 用户要的是「**真正修改过**才能可点」，不是「点过保存就能点」。
        if not _tp_content_changed(old_tp, new_tp):
            # 内容没变 → 不写留痕、不动 chg 标记（pending 原样保留）。
            # ⚠️ 空改既不能**点亮**按钮，也不能**消费**掉待诊断资格：
            #    用户还没处理这次变更，凭一次「一字未改的保存」就把按钮熄灭是错的。
            db.execute_update(
                "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
                (_dump_tp_list(old), req.id),
            )
            return _envelope({'total': len(old), 'change_logged': False, 'unchanged': True},
                             None, '内容未变化，未记录修改历史')
        # ---- 改前留痕（诊断锚点）----
        # affected_case_ids 记下「改前这一刻关联着本测试点的用例」，
        # 这样即使后来关联关系变了，也能还原当时的受影响面。
        case_ids = _linked_case_ids(db, req.id, req.index)
        change_id = None
        try:
            change_id = _insert_id(
                db,
                'INSERT INTO product_point_changes (pfp_id, tp_index, tp_ref, old_title, new_title, '
                'old_tp_json, new_tp_json, affected_case_ids, operator_id, create_time, is_del) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),0)',
                (req.id, req.index, (old_tp.get('tp_ref') or '')[:32],
                 old_title[:512], text[:512],
                 json.dumps(old_tp, ensure_ascii=False),
                 json.dumps(new_tp, ensure_ascii=False),
                 json.dumps(case_ids),
                 uid),
            )
        except Exception as e:  # noqa: BLE001
            # 留痕失败**不阻断修改**（正文仍要改成功），但必须让调用方知道诊断会走降级模式。
            # ⚠️ 此时 change_id 为 None —— 但「改过一次」这个事实仍要粘住，
            #    所以下面的 chg 标记照样写（只有 old_title，没有 id）。
            logger.warning('测试点修改留痕写入失败 pfp=%s idx=%s: %s', req.id, req.index, e)
        # ---- 回填「粘性修改标记」----
        # 标记写在**测试点自己身上**（test_points JSON 项内），
        # 这样「是否被修改过」不再依赖每次从留痕表反推 ——
        # 下标回缩 / 改名 / 同名 / 留痕软删 都不会让判定翻转。
        chg_time = ''
        if change_id:
            crow = db.execute_single_query(
                "SELECT create_time FROM product_point_changes WHERE id=%s", (change_id,))
            chg_time = str(crow.get('create_time') or '') if crow else ''
        old[req.index]['chg'] = {
            'id': change_id,
            'time': chg_time,
            'old_title': old_title[:512],
            # ⚠️ new_title：标记对应那次修改的**改后标题** → 读取时可自证
            #    「这次是真的改了内容」（old_title != new_title），不必依赖留痕行还在。
            'new_title': text[:512],
            # ⚠️ pending：这次真改带来的**一次**诊断资格。覆盖写入很关键 ——
            #    即使上一次已处理完（pending=False），只要再真改一次就重新点亮。
            'pending': True,
        }
        db.execute_update(
            "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
            (_dump_tp_list(old), req.id),
        )
        if not change_id:
            return _envelope({'total': len(old), 'change_logged': False},
                             None, '已修改（但修改历史写入失败，诊断将无法做新旧对比）')
        return _envelope({'total': len(old), 'change_logged': True, 'change_id': change_id,
                          'old_title': old_title, 'new_title': text}, None, '已修改')


@router.post('/delete-test-point')
async def delete_test_point(req: DeleteTestPointReq, request: Request):
    """三级测试点行「删除」：从功能点的 test_points 数组中移除某一条测试点。

    安全闸：若该测试点已通过 product_point_cases 显式关联了用例（tp_indexes 含本下标），
    禁止删除（必须先到「关联用例」里取消关联），避免产生悬空引用。
    删除后，同功能点后续测试点的 tp_indexes 统一 -1 回缩，保证引用不漂移。
    （tp_ref 锚定来源需求、不随 pfp 内下标变化，故无需重排 tp_ref。）
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, test_points FROM product_function_points "
            "WHERE id=%s AND is_del=0", (req.pfp_id,)
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        old = _norm_tp_list(row['test_points'])
        if req.index < 0 or req.index >= len(old):
            return _envelope(None, None, '测试点不存在', 404)
        # 精确判定：该测试点是否被显式关联（tp_indexes 含本下标）
        links = db.execute_query(
            "SELECT id, tp_indexes FROM product_point_cases WHERE pfp_id=%s AND is_del=0",
            (req.pfp_id,),
        )
        for l in links:
            idxs = _parse_tp_indexes(l['tp_indexes'])
            if not idxs:
                continue  # NULL=整体关联，不绑定到具体测试点，不阻止删除
            if req.index in idxs:
                return _envelope(None, None, '该测试点已关联用例，请先取消关联后再删除', 400)
        # 移除该测试点
        del old[req.index]
        db.execute_update(
            "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
            (_dump_tp_list(old), req.pfp_id),
        )
        # 回缩后续测试点的 tp_indexes（凡 > index 的下标 -1），保持引用正确。
        #
        # ⚠️ 必须「保留下标 ≤ index 的部分」：
        #    旧写法 `[i - 1 for i in idxs if i > req.index]` 用 filter 把 index 之前的
        #    下标直接丢掉了 —— 删除末尾测试点时，其余测试点的 tp_indexes 会被清成 NULL，
        #    导致列表「关联用例」列(caseCount)全部归零（看起来像被重置成未关联），
        #    但 product_point_cases 里的关联行还活着（用例仍是关联状态），两边对不上。
        for l in links:
            idxs = _parse_tp_indexes(l['tp_indexes'])
            if not idxs:
                continue  # NULL/空 = 不绑定具体测试点，不受下标回缩影响
            # 下标 == index 指向被删测试点（安全闸已拦，这里兜底丢弃）；
            # 下标 > index 整体 -1；下标 < index 原样保留。
            new_idxs = [(i - 1) if i > req.index else i for i in idxs if i != req.index]
            if new_idxs == idxs:
                continue
            if not new_idxs:
                # 原本只覆盖被删测试点 → 已无有效覆盖，整体解绑（不留空/整体关联的脏引用）
                db.execute_update(
                    "UPDATE product_point_cases SET is_del=1, update_time=NOW() WHERE id=%s",
                    (l['id'],),
                )
                continue
            db.execute_update(
                "UPDATE product_point_cases SET tp_indexes=%s, update_time=NOW() WHERE id=%s",
                (_dump_tp_indexes(new_idxs), l['id']),
            )
        return _envelope({'pfp_id': req.pfp_id, 'deleted_index': req.index, 'remaining': len(old)},
                         None, '已删除该测试点')


@router.post('/reorder-test-points')
async def reorder_test_points(req: ReorderTestPointsReq, request: Request):
    """三级测试点行「拖拽调整顺序」：按前端给出的新顺序重写 test_points，
    并**同步迁移所有引用测试点下标的持久化数据**（显式事务，要么全成、要么全回滚）。

    ⚠️ 下标被 4 处引用，重排必须一起处理，否则会静默串点：
      ① product_point_cases.tp_indexes（显式关联覆盖的测试点）—— 必迁移；
      ② ai_case_drafts.target_tp_index（**未采纳**的「手动新增测试点」草稿坐标）—— 必迁移，
         否则采纳时 _link_case_to_tp_index 的标题校验会失败 → 静默不建立关联、覆盖率丢失；
      ③ 测试点项内自持的 chg / tp_ref —— 随 JSON 项整体移动，天然跟随
         （_dump_tp_list 原样序列化整个 item，切勿重建 item 丢字段）；
      ④ product_point_changes.tp_index —— 历史留痕（「修改时刻的下标」），按设计不改写；
         重排后该表按 tp_index 的兜底匹配会错位，但主路径已改为「chg 标记 + tp_ref 优先」。

    ⚠️ 与 delete-test-point 的「下标 -1 回缩」不同：这里做的是任意置换，
       必须用 old→new 映射**保留全部下标**，不能用 filter 过滤（会丢下标）。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, test_points FROM product_function_points "
            "WHERE id=%s AND is_del=0", (req.pfp_id,)
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        old = _norm_tp_list(row['test_points'])
        n = len(old)
        if n < 2:
            return _envelope(None, None, '测试点不足两条，无需排序', 400)
        order = list(req.order or [])
        # 必须是 0..n-1 的完整排列：长度一致 + 排序后严格递增（同时挡住重复/越界/负数）
        if len(order) != n or sorted(order) != list(range(n)):
            return _envelope(None, None, '排序参数不合法：需为当前测试点下标的完整排列', 400)
        if order == list(range(n)):
            return _envelope({'pfp_id': req.pfp_id, 'order': order, 'total': n},
                             None, '顺序未变化')
        # old_index -> new_index
        mapping = {old_i: new_i for new_i, old_i in enumerate(order)}
        new_items = [old[i] for i in order]
        links = db.execute_query(
            "SELECT id, tp_indexes FROM product_point_cases WHERE pfp_id=%s AND is_del=0",
            (req.pfp_id,),
        ) or []
        try:
            drafts = db.execute_query(
                "SELECT id, target_tp_index FROM ai_case_drafts "
                "WHERE status=0 AND target_pfp_id=%s AND target_tp_index IS NOT NULL",
                (req.pfp_id,),
            ) or []
        except Exception:
            # 老库若还没跑 target_tp 迁移脚本，不能因此阻断排序主流程
            logger.warning('reorder-test-points: ai_case_drafts.target_tp_index 不可用，跳过草稿坐标迁移')
            drafts = []

        # ⚠️ 显式事务：execute_update 每条自带 commit，中途失败会留下
        #    「顺序已改但关联下标没迁移」的破数据 → 必须整批一次提交。
        db._ensure_connection()  # noqa: SLF001
        _conn = db.connection
        try:
            with _conn.cursor() as _cur:
                _cur.execute(
                    "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
                    (_dump_tp_list(new_items), req.pfp_id),
                )
                for l in links:
                    idxs = _parse_tp_indexes(l.get('tp_indexes'))
                    if not idxs:
                        continue  # NULL / 空 = 未绑定到具体测试点，不受重排影响
                    # 保留全部下标；越界的脏值原样留着（不要静默丢弃）
                    new_idxs = [mapping.get(i, i) for i in idxs]
                    if new_idxs == idxs:
                        continue
                    _cur.execute(
                        "UPDATE product_point_cases SET tp_indexes=%s, update_time=NOW() WHERE id=%s",
                        (_dump_tp_indexes(new_idxs), l['id']),
                    )
                for d in drafts:
                    try:
                        oi = int(d['target_tp_index'])
                    except (TypeError, ValueError, KeyError):
                        continue
                    ni = mapping.get(oi)
                    if ni is None or ni == oi:
                        continue
                    _cur.execute(
                        "UPDATE ai_case_drafts SET target_tp_index=%s WHERE id=%s",
                        (ni, d['id']),
                    )
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        return _envelope(
            {'pfp_id': req.pfp_id, 'order': order, 'total': n,
             'links_checked': len(links), 'drafts_checked': len(drafts)},
            None, '已调整测试点顺序')


# ---------- 功能点 ↔ 正式用例库 手动关联 ----------
class CaseLinkItem(BaseModel):
    """逐条关联：指定某条用例覆盖哪些测试点下标（0 起）。

    tp_indexes 为 None / 空列表 → 该关联不计入任何测试点覆盖（需后续在「已关联用例」中补充指定）。
    传 [0,1,2] 等完整列表 → 显式覆盖这些测试点（即便是全选也下发完整列表，不再用 NULL 表示整体）。
    auto=True → **忽略 tp_indexes**，交给后端自动判定覆盖范围（保存时自动匹配用）。
    """
    case_id: int
    tp_indexes: Optional[List[int]] = None
    auto: bool = False


class LinkCasesReq(BaseModel):
    """二级功能点行「关联用例」：把选中的正式用例库用例关联到该功能点。

    优先使用 items（逐条映射测试点）；若只传 case_ids（旧调用），则整批共用 tp_indexes。
    auto_match=True 时，未指定测试点的关联（items[].auto=True）由后端自动判定覆盖范围。
    """
    pfp_id: int
    case_ids: Optional[List[int]] = None
    tp_indexes: Optional[List[int]] = None
    items: Optional[List[CaseLinkItem]] = None
    auto_match: bool = False        # 是否对「未指定测试点」的关联自动判定
    use_ai: bool = True              # 自动判定时前两层定不下来是否调模型兜底（消耗额度）


class UpdateLinkTpReq(BaseModel):
    """修改某条关联的测试点覆盖范围（把整体关联细化为指定测试点）。"""
    pfp_id: int
    case_id: int
    tp_indexes: Optional[List[int]] = None


class UnlinkCaseReq(BaseModel):
    pfp_id: int
    case_id: int


@router.get('/case-library')
async def case_library(keyword: str = '', project_id: int = 0, pfp_id: int = 0,
                       pageNo: int = 1, pageSize: int = 20):
    """关联用例弹窗的「正式用例库」选择器：从 test_cases(status=1,is_del=0) 检索。

    支持关键字（module / function_point 模糊）+ 可选按项目(project_id)过滤。
    若传入 pfp_id，则排除已关联到该功能点的用例，避免在选择列表里重复勾选。
    """
    try:
        with get_db_handler() as db:
            conds = ['tc.is_del=0', 'tc.`status`=1']
            args = []
            if keyword:
                conds.append('(tc.module LIKE %s OR tc.function_point LIKE %s)')
                args.extend([f'%{keyword}%', f'%{keyword}%'])
            if project_id:
                conds.append('tc.project_id=%s')
                args.append(project_id)
            if pfp_id:
                conds.append(
                    'NOT EXISTS (SELECT 1 FROM product_point_cases pc '
                    'WHERE pc.pfp_id=%s AND pc.case_id=tc.id AND pc.is_del=0)'
                )
                args.append(pfp_id)
            where = ' AND '.join(conds)
            total = db.execute_query(
                f"SELECT COUNT(*) c FROM test_cases tc WHERE {where}", tuple(args)
            )[0]['c']
            limit_sql = ''
            limit_args = []
            if pageSize and pageSize > 0:
                offset = (pageNo - 1) * pageSize
                limit_sql = ' LIMIT %s OFFSET %s'
                limit_args = [pageSize, offset]
            rows = db.execute_query(
                f"""SELECT tc.id, tcp.cate_name AS project, tc.project_id,
                           tc.module, tc.function_point, tc.priority,
                           tc.pre_condition, tc.steps, tc.expected_result, tc.create_time
                    FROM test_cases tc
                    LEFT JOIN test_case_project tcp ON tcp.id = tc.project_id
                    WHERE {where} ORDER BY tc.id DESC{limit_sql}""",
                tuple(args + limit_args),
            )
            return _envelope(rows, total)
    except Exception as e:  # noqa: BLE001
        logger.warning('正式用例库检索失败: %s', e)
        return _envelope(None, 0, f'查询失败: {e}', 500)


@router.get('/link-cases')
async def link_cases_list(pfp_id: int = 0):
    """列出某功能点已关联的正式用例（含每条关联覆盖的测试点下标）。"""
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                """SELECT pc.id AS rel_id, pc.case_id, pc.tp_indexes,
                          tc.id, tcp.cate_name AS project, tc.module,
                          tc.function_point, tc.priority, tc.steps, tc.expected_result,
                          tc.pre_condition, tc.remark
                   FROM product_point_cases pc
                   LEFT JOIN test_cases tc ON tc.id = pc.case_id
                   LEFT JOIN test_case_project tcp ON tcp.id = tc.project_id
                   WHERE pc.pfp_id=%s AND pc.is_del=0
                   ORDER BY pc.id DESC""",
                (pfp_id,),
            )
            for r in rows:
                idxs = _parse_tp_indexes(r.get('tp_indexes'))
                # None / 空 = 未指定测试点（自动识别失败或历史未指定）→ 不计入任何测试点覆盖
                r['tp_indexes'] = idxs
                # is_unspecified=true 的即「自动识别失败、待人工指定测试点」的关联
                r['is_unspecified'] = not bool(idxs)
            return _envelope(rows, len(rows))
    except Exception as e:  # noqa: BLE001
        logger.warning('关联用例列表查询失败: %s', e)
        return _envelope(None, 0, f'查询失败: {e}', 500)


@router.post('/link-cases')
async def link_cases(req: LinkCasesReq, request: Request):
    """手动关联：把选中的正式用例挂到功能点下（幂等，已关联的跳过）。

    优先按 items 逐条映射测试点；旧调用可只传 case_ids + 共享 tp_indexes。
    某条关联不指定 tp_indexes（None/空）→ 不计入任何测试点覆盖（避免「假覆盖」）。

    **auto_match=True 时自动判定覆盖范围**：
    items 里标了 auto=True 的条目（或旧式 case_ids 调用且未给 tp_indexes），
    其 tp_indexes 由后端跑三层判定（回标反查 → 标题相似 → AI 兜底）自动决定，
    判定结果直接随关联一起落库 —— 前端不必先调 auto-match 再调本接口。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id:
        return _envelope(None, None, '缺少功能点 id', 400)
    # 组装 (case_id, tp_idx_json) 配对：优先 items，其次 case_ids + 共享 tp_indexes
    # auto_ids 里的条目 tp_idx_json 留空，稍后由自动判定填入
    pairs = []
    auto_ids: List[int] = []
    if req.items:
        for it in req.items:
            if it and it.case_id:
                if it.auto:
                    auto_ids.append(it.case_id)
                    pairs.append((it.case_id, None))
                else:
                    pairs.append((it.case_id, _dump_tp_indexes(it.tp_indexes)))
    elif req.case_ids:
        tp_idx_json = _dump_tp_indexes(req.tp_indexes)
        # 旧式调用：给了 tp_indexes 就是显式指定；没给且开了 auto_match 才自动判定
        legacy_auto = bool(req.auto_match) and req.tp_indexes is None
        for cid in req.case_ids:
            if legacy_auto:
                auto_ids.append(cid)
            pairs.append((cid, tp_idx_json))
    if not pairs:
        return _envelope(None, None, '请至少选择一条用例', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, requirement_id, test_points "
            "FROM product_function_points WHERE id=%s AND is_del=0",
            (req.pfp_id,),
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)

        # ---- 自动判定：把「未指定测试点」的关联补上覆盖范围 ----
        auto_results: Dict[int, Dict[str, Any]] = {}
        auto_stats = None
        if auto_ids:
            tp_items = _norm_tp_list(row.get('test_points'))
            if tp_items:
                matches, auto_stats, _note = _match_cases_for_pfp(
                    db, uid, row, tp_items, auto_ids, use_ai=req.use_ai)
                by_case = {m['case_id']: m for m in matches}
                new_pairs = []
                for cid, tp_idx_json in pairs:
                    if cid in auto_ids:
                        m = by_case.get(cid)
                        if m:
                            idxs = m.get('tp_indexes') or []
                            auto_results[cid] = {
                                'tp_indexes': idxs,
                                'source': m.get('source'),
                                'confidence': m.get('confidence'),
                                'reason': m.get('reason') or '',
                            }
                            # source=none（判不出来）→ 落 NULL = 不计入覆盖，不造假
                            if m.get('source') != 'none' and idxs:
                                tp_idx_json = _dump_tp_indexes(idxs)
                            else:
                                auto_results[cid]['tp_indexes'] = []
                    new_pairs.append((cid, tp_idx_json))
                pairs = new_pairs

        # 现有行（含已删除墓碑），按 case_id 归并。
        # ⚠️ 关键：重关联时**复活已删除的墓碑行**而非 INSERT 新行，
        # 否则「关联→解绑→再关联」会堆积两条 is_del=1 同键行，
        # 再次解绑 UPDATE 必然撞 uk_pfp_case 唯一键（1062 Duplicate entry）。
        ex_rows = db.execute_query(
            "SELECT id, case_id, is_del FROM product_point_cases WHERE pfp_id=%s",
            (req.pfp_id,),
        )
        active = {r['case_id'] for r in ex_rows if r['is_del'] == 0}
        tombs: dict = {}  # case_id -> [id] 已删除行 id 列表
        for r in ex_rows:
            if r['is_del'] == 1:
                tombs.setdefault(r['case_id'], []).append(r['id'])
        inserted = 0
        reactivated = 0
        for cid, tp_idx_json in pairs:
            if cid in active:
                # 已生效：若新传入的 tp_indexes 与现存不同，则同步更新覆盖范围
                ex = db.execute_single_query(
                    "SELECT id, tp_indexes FROM product_point_cases "
                    "WHERE pfp_id=%s AND case_id=%s AND is_del=0",
                    (req.pfp_id, cid),
                )
                if ex and _dump_tp_indexes(_parse_tp_indexes(ex['tp_indexes'])) != tp_idx_json:
                    db.execute_update(
                        "UPDATE product_point_cases SET tp_indexes=%s, update_time=NOW() WHERE id=%s",
                        (tp_idx_json, ex['id']))
                continue
            if cid in tombs and tombs[cid]:
                # 复用墓碑行：复活最新一条，删除其余多余墓碑，避免重复物理行堆积
                ids = sorted(tombs[cid])
                keep = ids[-1]
                for del_id in ids[:-1]:
                    db.execute_update(
                        "DELETE FROM product_point_cases WHERE id=%s", (del_id,))
                db.execute_update(
                    "UPDATE product_point_cases SET is_del=0, creator_id=%s, tp_indexes=%s, update_time=NOW() "
                    "WHERE id=%s", (uid, tp_idx_json, keep))
                reactivated += 1
            else:
                db.execute_update(
                    """INSERT INTO product_point_cases (pfp_id, case_id, creator_id, tp_indexes)
                       VALUES (%s, %s, %s, %s)""",
                    (req.pfp_id, cid, uid, tp_idx_json),
                )
                inserted += 1
    return _envelope(
        {'inserted': inserted, 'reactivated': reactivated,
         'auto_results': {str(k): v for k, v in auto_results.items()},
         'auto_stats': auto_stats},
        None, '已关联 %d 条（复用 %d 条）' % (inserted, reactivated))


@router.post('/link-cases/update-tp')
async def update_link_tp(req: UpdateLinkTpReq, request: Request):
    """修改某条关联覆盖的测试点范围（整体关联 ↔ 指定测试点）。"""
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id or not req.case_id:
        return _envelope(None, None, '参数不完整', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT pc.id AS rel_id, pfp.process_id FROM product_point_cases pc "
            "JOIN product_function_points pfp ON pfp.id=pc.pfp_id "
            "WHERE pc.pfp_id=%s AND pc.case_id=%s AND pc.is_del=0",
            (req.pfp_id, req.case_id),
        )
        if not row:
            return _envelope(None, None, '关联记录不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        db.execute_update(
            "UPDATE product_point_cases SET tp_indexes=%s, update_time=NOW() "
            "WHERE pfp_id=%s AND case_id=%s AND is_del=0",
            (_dump_tp_indexes(req.tp_indexes), req.pfp_id, req.case_id),
        )
    return _envelope(None, None, '已更新覆盖范围')


@router.post('/link-cases/batch-tp')
async def batch_update_link_tp(req: BatchTpReq, request: Request):
    """批量写某功能点下多条关联的覆盖测试点范围。

    「自动匹配」确认后一次性落库用；也可用于人工改完多条一起保存。
    只更新**已存在且生效**的关联，不会新建关联（新建走 link-cases）。

    **auto_match=True 时自动判定覆盖范围**：
    items 里带 auto=True 的条目，其 tp_indexes 由后端跑三层判定决定，
    判定结果直接随保存一起落库 —— 前端不必先调 auto-match 再调本接口。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id or not req.items:
        return _envelope(None, None, '参数不完整', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id, requirement_id, test_points "
            "FROM product_function_points WHERE id=%s AND is_del=0",
            (req.pfp_id,),
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)

        # ---- 自动判定：把 auto=True 的条目的 tp_indexes 补上 ----
        auto_results: Dict[int, Dict[str, Any]] = {}
        auto_stats = None
        auto_ids: List[int] = []
        for it in req.items:
            if it.get('auto'):
                try:
                    cid = int(it.get('case_id'))
                except Exception:
                    continue
                if cid:
                    auto_ids.append(cid)
        if auto_ids:
            tp_items = _norm_tp_list(row.get('test_points'))
            if tp_items:
                matches, auto_stats, _note = _match_cases_for_pfp(
                    db, uid, row, tp_items, auto_ids, use_ai=req.use_ai)
                for m in matches:
                    cid = m['case_id']
                    idxs = m.get('tp_indexes') or []
                    auto_results[cid] = {
                        'tp_indexes': idxs if m.get('source') != 'none' else [],
                        'source': m.get('source'),
                        'confidence': m.get('confidence'),
                        'reason': m.get('reason') or '',
                    }

        updated = 0
        for it in req.items:
            try:
                cid = int(it.get('case_id'))
            except Exception:
                continue
            if not cid:
                continue
            if it.get('auto'):
                m = auto_results.get(cid)
                # source=none（判不出来）→ 落 NULL = 不计入覆盖，不造假
                idxs = _parse_tp_indexes((m or {}).get('tp_indexes'))
            else:
                idxs = _parse_tp_indexes(it.get('tp_indexes'))
            db.execute_update(
                "UPDATE product_point_cases SET tp_indexes=%s, update_time=NOW() "
                "WHERE pfp_id=%s AND case_id=%s AND is_del=0",
                (_dump_tp_indexes(idxs), req.pfp_id, cid),
            )
            updated += 1
    return _envelope(
        {'updated': updated,
         'auto_results': {str(k): v for k, v in auto_results.items()},
         'auto_stats': auto_stats},
        None, '已更新 %d 条覆盖范围' % updated)


# ===========================================================================
# 测试点级「补充用例」：针对某个尚未关联用例的测试点，生成 1~2 条用例，
# 落 ai_case_drafts（source_type=3，source_id=requirement_id，tp_refs=该测试点真实编号）。
#
# 设计取舍：
#  - 复用 ai_workbench 的 case_gen 提示词 + run_structured（同一套生成底座，零重复造）。
#  - 落库即带 tp_refs + source_id(=requirement_id) → 前端采纳时直接调已有的
#    adopt_drafts，其内置的「_tp_ref_map + _link_cases_by_tp_refs」回标闭环会
#    把新用例**精确关联回本测试点**（复用 100% 已测代码，最可控、幂等）。
#  - ⚠️ tp_refs 必须写**库里存的真实编号**（需求级全局编号，如 TP25），
#    绝不能按「pfp 内下标+1」重算 —— 那样反查会命中**别的功能点**的测试点。
#
#  【手动新增测试点】（无 tp_ref）—— 本次新增能力：
#  手动新增的三级测试点不是从需求原文拆出来的，没有回标编号。
#  这里**绝不伪造 TP 编号**：TP 是需求内全局编号空间，手动点硬造一个会与真编号
#  冲突、破坏「需求→功能点→用例」的来源串联（用户明确要求保住这一点）。
#  改走**显式定位**通道：草稿额外带 target_pfp_id / target_tp_index，
#  采纳时按坐标精确关联到本测试点。两条通道互斥、互不影响：
#    需求来源测试点 → tp_refs 回标（行为零变化）
#    手动新增测试点 → target 坐标显式关联（不碰编号空间）
#
#  - 喂给模型的测试点上下文回需求原文取（pfp 里只存了 title/dims/batch，
#    缺预期/输入/前置条件），否则模型只能对着标题瞎编。
#    手动测试点无编号可反查 → 先按标题精确匹配原文（命中就借五要素），
#    命不中则退回需求原文摘要作为背景。
#  - use_ai=False 时走本地确定性骨架生成（不调模型、不耗额度）。
#  - 额外返回 suggested_project_id：取「该 pfp 已关联用例最多的三级项目」，前端
#    采纳时预选，避免每次手选项目（project_id 是 test_cases 必填，但 pfp 上无此字段，
#    dev_process.project_id 又常为空，故只能「建议」+ 前端可改）。
# ===========================================================================
class GenerateTpCasesReq(BaseModel):
    pfp_id: int
    tp_index: int            # 测试点在 test_points 数组里的 0 基下标
    count: int = 2           # 本次生成条数（默认 2）
    use_ai: bool = True
    model_id: Optional[int] = None


def _gen_tp_drafts(db, uid, row, tp_index: int, count: int = 2,
                   use_ai: bool = True, model_id=None,
                   focus_override: str = '') -> Dict[str, Any]:
    """**生成通道核心**：为一个测试点生成用例草稿（不写 test_cases，只落 ai_case_drafts）。

    「补充用例」（generate-tp-cases）与「变更影响诊断」（diagnose-tp-impact）
    复用同一个函数 —— 保证两条链路产出的上下文与草稿格式完全一致。

    返回：
      {drafts, suggested_project_id, suggested_project_name, pfp_id, tp_index,
       tp_title, tp_ref, ai_used, is_manual_tp, error}
      失败时 error 非空（调用方决定怎么报），不抛异常。
    """
    tp_items = _norm_tp_list(row.get('test_points'))
    if not tp_items or not (0 <= tp_index < len(tp_items)):
        return {'error': '测试点下标越界'}
    tp = tp_items[tp_index]
    tp_title = tp.get('title') or ''
    tp_dims = coverage_mod.normalize_dims(tp.get('dims'))
    tp_batch = (tp.get('batch') or '').upper()

    # ⚠️ tp_ref 必须用**库里存的真实编号**，不能算「pfp 内下标+1」。
    # tp_ref 的口径是「需求内原始测试点下标+1」（需求级全局编号）：
    # 同一个需求拆成多个功能点时，编号在功能点之间是连续且不重号的
    # （实测 req=15：pfp110=TP1~24、pfp111=TP25~37、pfp112=TP38~50…）。
    # 若按 pfp 内下标+1 造号，草稿落库后 adopt 的回标反查会命中
    # **另一个功能点的测试点**，新用例关联到错误的地方（覆盖率假涨 + 真缺口仍在）。
    #
    # ⚠️ 手动新增的三级测试点**没有** tp_ref（不是从需求原文拆出来的）。
    # 这时**绝不伪造 TP 编号** —— 需求内编号空间是共享的，伪造会与真编号冲突，
    # 且破坏「需求→功能点→用例」的来源串联（正是用户要保住的东西）。
    # 改为走**显式定位**通道：草稿额外带 target_pfp_id / target_tp_index，
    # 采纳时按坐标精确关联到本测试点，与 tp_ref 回标通道互不干扰。
    tp_ref = tp.get('tp_ref') or ''
    is_manual_tp = not tp_ref

    # 回需求原文取完整测试点（pfp 里只存了 title/dims/batch，缺预期/输入/前置条件）
    tp_input = tp.get('sample_input') or ''
    tp_expected = tp.get('expected') or ''
    tp_pre = tp.get('pre_condition') or ''
    tp_source = tp.get('source') or ''
    raw_tp = _resolve_raw_tp(db, row.get('requirement_id'), tp)
    if isinstance(raw_tp, dict):
        tp_input = raw_tp.get('sample_input') or tp_input
        tp_expected = raw_tp.get('expected') or tp_expected
        tp_pre = raw_tp.get('pre_condition') or tp_pre
        tp_source = raw_tp.get('source') or tp_source

    # 建议 project_id：取「该 pfp 已关联用例最多的三级项目」。
    # （test_cases.project_id 必填，但 pfp 上无此字段、dev_process.project_id 又常为空，
    #   故只能「建议」，前端采纳时可改选。）
    project_id = 0
    sug = db.execute_single_query(
        "SELECT tc.project_id, COUNT(*) n FROM product_point_cases pc "
        "JOIN test_cases tc ON tc.id=pc.case_id "
        "WHERE pc.pfp_id=%s AND pc.is_del=0 AND tc.is_del=0 "
        "GROUP BY tc.project_id ORDER BY n DESC LIMIT 1", (row['id'],))
    if sug and sug.get('project_id'):
        project_id = int(sug['project_id'])
    proj_name = ''
    if project_id:
        pn = db.execute_single_query(
            "SELECT cate_name FROM test_case_project WHERE id=%s", (project_id,))
        if pn:
            proj_name = pn.get('cate_name') or ''

    module = (tp.get('module') or row.get('title') or '').strip()
    function_point = tp_title
    # 测试点上下文作为「需求说明」喂给模型，让它围绕该测试点写用例。
    # 前置条件/预期/输入样例都来自需求原文（pfp 里裁掉了），缺了模型只能瞎编。
    focus_bits = [tp_title]
    if tp_pre:
        focus_bits.append('前置：%s' % tp_pre)
    if tp_input:
        focus_bits.append('输入：%s' % tp_input)
    if tp_expected:
        focus_bits.append('预期：%s' % tp_expected)
    if tp_source:
        focus_bits.append('（%s）' % tp_source)
    # 手动新增的测试点既无编号、也未必抄自原文 → 五要素可能全空。
    # 这时把需求原文摘要作为背景喂进去，否则模型只能对着标题瞎编。
    # （有五要素时不再重复喂原文，避免 prompt 冗余、也避免原文与测试点冲突。）
    if is_manual_tp and not (tp_input or tp_expected or tp_pre):
        doc = _load_req_doc(db, row.get('requirement_id'))
        if doc:
            focus_bits.append('需求背景：%s' % doc)
    # 诊断链路可传入额外上下文（如「改前测试点原文」），让新用例更贴近变更语义
    if focus_override:
        focus_bits.append(focus_override)
    req_text = '；'.join(x for x in focus_bits if x)

    # 复用 ai_workbench 的渲染与落库辅助函数
    from src.routers.ai_workbench import (
        _few_shot_text, _plan_text, _tp_list_text, _batch_id,
    )
    few_shot = _few_shot_text(db, module)
    kb_text, _kb_count = '', 0
    try:
        from src.routers.ai_workbench import _kb_knowledge
        kb_text, _kb_count = _kb_knowledge(db, project_id, module, function_point)
    except Exception as ke:  # noqa: BLE001
        logger.warning('补充用例 KB 参考拉取失败（不影响生成）: %s', ke)

    # 构造单格覆盖计划（与 _plan_from_test_points 同口径）
    cell = {
        'module': module,
        'function_point': function_point,
        'dim': tp_dims[0] if tp_dims else '',
        'dim_name': coverage_mod.COVERAGE_DIM_NAMES.get(tp_dims[0], '') if tp_dims else '',
        'route': '',
        'route_name': '',
        'case_type': '',
        'design_method': '',
        'severity': 'S0' if tp_batch == 'A' else 'S2',
        'evidence_type': tp.get('evidence_type') or 'basis',
        'focus': req_text,
        'planned': 2 if tp_batch == 'A' else 1,
        'note': tp.get('source') or '',
        'tp_ref': tp_ref,
        'tp_title': tp_title,
        'tp_expected': tp_expected,
    }
    base_vars = {
        'project_name': proj_name or '未指定',
        'module': module or '未指定',
        'function_point': function_point or '未指定',
        'case_types': '正向,异常,边界,权限',
        'few_shot': few_shot,
        'knowledge': kb_text,
    }

    from src.ai import runner as ai_runner
    from src.ai import schema as ai_schema
    ai_used = False
    if use_ai:
        try:
            r = ai_runner.run_structured(
                db, uid, biz_type='case_gen', prompt_code='case_gen',
                prompt_vars=dict(
                    base_vars, count=count, requirement=req_text,
                    plan_text=_plan_text([cell]),
                    tp_list=_tp_list_text([cell])),
                list_key='cases', item_model=ai_schema.CaseDraftItem,
                limit=count, model_id=model_id,
            )
        except HTTPException:
            raise
        except Exception as ge:  # noqa: BLE001
            logger.exception('测试点用例生成失败 pfp=%s tp=%s', row.get('id'), tp_index)
            return {'error': '生成失败：%s' % ge}
        items = r.get('items') or []
        ai_used = bool(r.get('model'))
    else:
        # use_ai=False：本地确定性生成（不调模型、不耗额度）。
        # 依据测试点原文的前置/输入/预期拼骨架用例 —— 内容有限但可编辑，
        # 至少让「补充用例」在无 AI 权限时也有出路。
        # 按 count 生成对应条数（与 AI 路径条数语义一致），
        # 用不同 case_type 区分角度，避免生成 N 条一模一样的骨架。
        _local_types = ['正向', '异常', '边界', '权限']
        _local_angles = {
            '正向': '按测试点「%s」执行操作',
            '异常': '构造异常输入/中断操作，执行测试点「%s」',
            '边界': '取边界值（空值/极值/临界）执行测试点「%s」',
            '权限': '以无权限账号执行测试点「%s」',
        }
        items = []
        for _i in range(max(1, min(int(count or 1), 10))):
            _ct = _local_types[_i % len(_local_types)]
            items.append({
                'module': module,
                'function_point': function_point,
                'priority': 2,
                'case_type': _ct,
                'design_method': '',
                'dims': tp_dims,
                'routes': [],
                'evidence_type': 'basis',
                'severity': cell['severity'],
                'pre_condition': tp_pre,
                'steps': '1. %s\n2. 检查系统响应' % (_local_angles[_ct] % tp_title),
                'expected_result': tp_expected or '（待人工补充预期结果）',
                'remark': '本地生成骨架（%s场景），请人工复核步骤与预期' % _ct,
                'confidence': 0.3,
            })

    base = {
        'suggested_project_id': project_id,
        'suggested_project_name': proj_name,
        'pfp_id': row.get('id'),
        'tp_index': tp_index,
        'tp_title': tp_title,
        'tp_ref': tp_ref,
        'ai_used': ai_used,
        # is_manual_tp=true 表示该测试点是手动新增的（无回标编号），
        # 草稿走「显式定位」关联通道。前端据此提示用户。
        'is_manual_tp': is_manual_tp,
        'tp_dims': tp_dims,
        'raw_tp': raw_tp if isinstance(raw_tp, dict) else None,
        'module': module,
        'function_point': function_point,
    }
    if not items:
        base['drafts'] = []
        base['error'] = '未生成出可用用例，请调整测试点描述后重试'
        return base

    # 落 ai_case_drafts（source_type=3 表示「产品功能点·测试点补充」，
    # source_id=requirement_id + tp_refs=真实回标编号 → 采纳时由 adopt 的
    # 「_tp_ref_map + _link_cases_by_tp_refs」闭环自动关联到**本测试点**）
    #
    # ⚠️ 手动新增的测试点没有 tp_ref（is_manual_tp）：
    #   tp_refs 留空（不伪造编号 → 需求编号空间零污染），
    #   改用 target_pfp_id / target_tp_index 显式定位，
    #   采纳时按坐标精确关联到本测试点。
    #
    # ⚠️ target 坐标**只给手动测试点写**：
    #   需求来源测试点必须保持 target=NULL —— 否则 adopt 会优先走显式定位分支，
    #   抢在 tp_ref 回标通道前面，行为与改动前不一致（回归）。
    batch = _batch_id()
    draft_ids = []
    meta = []
    for it in items:
        severity = coverage_mod.normalize_severity(it.get('severity'))
        priority = (coverage_mod.severity_to_priority(severity)
                    if severity else (int(it.get('priority') or 2)))
        did = _insert_id(
            db,
            'INSERT INTO ai_case_drafts (batch_id, source_type, source_id, suggest_project_id, '
            'module, function_point, priority, case_type, design_method, dims, routes, '
            'evidence_type, severity, pre_condition, case_steps, '
            'expected_result, remark, confidence, status, model_id, run_by, source_case_ids, tp_refs, '
            'target_pfp_id, target_tp_index, target_tp_title) '
            'VALUES (%s,3,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s)',
            (batch, row.get('requirement_id'), project_id, (it.get('module') or module)[:255],
             (it.get('function_point') or function_point)[:512], priority,
             (it.get('case_type') or '')[:32], (it.get('design_method') or '')[:64],
             ','.join(coverage_mod.normalize_dims(it.get('dims'))),
             ','.join(coverage_mod.normalize_routes(it.get('routes'))),
             (it.get('evidence_type') or 'basis')[:16], severity,
             (it.get('pre_condition') or '')[:65535],
             (it.get('steps') or '')[:65535],
             (it.get('expected_result') or '')[:65535],
             (it.get('remark') or '')[:65535], float(it.get('confidence') or 0),
             model_id, uid, None, tp_ref,
             row.get('id') if is_manual_tp else None,
             tp_index if is_manual_tp else None,
             tp_title[:512] if is_manual_tp else None),
        )
        draft_ids.append(did)
        meta.append({'severity': severity, 'priority': priority})

    # 组装返回（tp_refs 不回传，前端预览不需要回标信息）
    drafts = []
    for did, it, m in zip(draft_ids, items, meta):
        drafts.append({
            'id': did,
            'module': it.get('module') or module,
            'function_point': it.get('function_point') or function_point,
            'priority': m['priority'],
            'case_type': it.get('case_type') or '',
            'design_method': it.get('design_method') or '',
            'dims': coverage_mod.normalize_dims(it.get('dims')),
            'routes': coverage_mod.normalize_routes(it.get('routes')),
            'severity': m['severity'],
            'evidence_type': it.get('evidence_type') or 'basis',
            'pre_condition': it.get('pre_condition') or '',
            'steps': it.get('steps') or '',
            'expected_result': it.get('expected_result') or '',
            'remark': it.get('remark') or '',
            'confidence': float(it.get('confidence') or 0),
            'tp_refs': [],
        })
    base['drafts'] = drafts
    return base


@router.post('/generate-tp-cases')
def generate_tp_cases(req: GenerateTpCasesReq, request: Request):
    """针对单个测试点生成补充用例（不自动入库，返回草稿供前端预览勾选）"""
    try:
        uid = _uid(request)
        if not uid:
            return _envelope(None, None, '未登录', 401)
        if not req.pfp_id or req.tp_index is None:
            return _envelope(None, None, '参数不完整', 400)
        count = max(1, min(int(req.count or 2), 4))  # 补充用例不需要太多，封顶 4
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT id, process_id, requirement_id, test_points, title "
                "FROM product_function_points WHERE id=%s AND is_del=0",
                (req.pfp_id,),
            )
            if not row:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, row['process_id']):
                return _envelope(None, None, '无权操作', 404)

            out = _gen_tp_drafts(db, uid, row, req.tp_index, count=count,
                                 use_ai=req.use_ai, model_id=req.model_id)
            if out.get('error') and not out.get('drafts'):
                return _envelope(
                    {'drafts': [], 'suggested_project_id': out.get('suggested_project_id', 0),
                     'suggested_project_name': out.get('suggested_project_name', ''),
                     'tp_index': req.tp_index, 'tp_title': out.get('tp_title', ''),
                     'ai_used': False},
                    None, out['error'], 200 if '未生成出' in out['error'] else 500)
            return _envelope(out, None, '已生成 %d 条候选用例' % len(out['drafts']))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception('generate_tp_cases 异常')
        return _envelope(None, None, '生成失败：%s' % e, 500)


class UpdateTpDraftReq(BaseModel):
    """「补充用例」草稿编辑保存（采纳前）。

    ⚠️ `steps` 是**前端字段名**（与生成接口返回的草稿形状一致），
       草稿表里的真实列名是 `case_steps`，由端点负责映射，别写成同名。
    """
    draft_id: int
    pfp_id: int
    tp_index: int
    module: str = ''
    function_point: str = ''
    priority: int = 2
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''


@router.post('/update-tp-draft')
def update_tp_draft(req: UpdateTpDraftReq, request: Request):
    """编辑「补充用例」生成的草稿并**立即保存**，之后采纳入库的就是编辑后的内容。

    只写 `ai_case_drafts`（草稿表），**不动 `test_cases`** —— 采纳动作仍由
    `/ai/drafts/adopt` 统一完成，避免两套入库逻辑分叉。

    ⚠️ 只接受 `status=0`（未采纳/未丢弃）且 `source_type=3`（补充用例生成）的草稿：
       已采纳的草稿再改会出现「卡片显示改了、入库还是旧内容」的自相矛盾。
    ⚠️ 步骤写入 `case_steps` 列（历史列名，不是 `steps`）。
    ⚠️ 只更新用户可编辑的 7 个字段，**绝不碰** `tp_refs` / `target_pfp_id` /
       `target_tp_index` / `source_id` —— 否则会破坏「采纳时自动关联到本测试点」
       的坐标，导致覆盖率算错。
    """
    try:
        uid = _uid(request)
        if not uid:
            return _envelope(None, None, '未登录', 401)
        if not req.draft_id or not req.pfp_id or req.tp_index is None:
            return _envelope(None, None, '参数不完整', 400)
        # 用例「标题」= module + function_point，两者都空入库后无法辨认
        if not str(req.module or '').strip() and not str(req.function_point or '').strip():
            return _envelope(None, None, '请至少填写模块或功能点', 400)
        with get_db_handler() as db:
            # ① 功能点存在 + 当前用户对该研发流程有操作权限
            pfp = db.execute_single_query(
                "SELECT id, process_id FROM product_function_points WHERE id=%s AND is_del=0",
                (req.pfp_id,),
            )
            if not pfp:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, pfp['process_id']):
                return _envelope(None, None, '无权操作', 404)
            # ② 草稿可编辑校验
            d = db.execute_single_query(
                "SELECT id, status, source_type, run_by FROM ai_case_drafts WHERE id=%s",
                (req.draft_id,),
            )
            if not d:
                return _envelope(None, None, '草稿不存在', 404)
            if int(d.get('status') or 0) != 0:
                return _envelope(None, None, '该草稿已采纳或丢弃，不能编辑', 400)
            if int(d.get('source_type') or 0) != 3:
                return _envelope(None, None, '只能编辑「补充用例」生成的草稿', 400)
            if not admin and int(d.get('run_by') or 0) != int(uid):
                return _envelope(None, None, '无权编辑他人生成的草稿', 404)
            # ③ 字段清洗（长度口径与 `_gen_tp_drafts` 写入时一致）
            try:
                priority = int(req.priority or 2)
            except (TypeError, ValueError):
                priority = 2
            if priority < 1 or priority > 4:
                priority = 2
            db.execute_update(
                "UPDATE ai_case_drafts SET module=%s, function_point=%s, priority=%s, "
                "pre_condition=%s, case_steps=%s, expected_result=%s, remark=%s "
                "WHERE id=%s AND status=0 AND source_type=3",
                (str(req.module or '').strip()[:255],
                 str(req.function_point or '').strip()[:512],
                 priority,
                 str(req.pre_condition or '')[:65535],
                 str(req.steps or '')[:65535],
                 str(req.expected_result or '')[:65535],
                 str(req.remark or '')[:65535],
                 int(req.draft_id)),
            )
            # ④ 回读最新草稿，按「生成时」的形状返回，前端可就地替换卡片
            row = db.execute_single_query(
                "SELECT id, module, function_point, priority, pre_condition, case_steps, "
                "expected_result, remark, confidence, dims "
                "FROM ai_case_drafts WHERE id=%s", (req.draft_id,))
            dims = [x.strip() for x in str(row.get('dims') or '').split(',') if x.strip()]
            draft = {
                'id': int(row['id']),
                'module': row.get('module') or '',
                'function_point': row.get('function_point') or '',
                'priority': row.get('priority') or 2,
                'dims': dims,
                'pre_condition': row.get('pre_condition') or '',
                'steps': row.get('case_steps') or '',
                'expected_result': row.get('expected_result') or '',
                'remark': row.get('remark') or '',
                'confidence': float(row.get('confidence') or 0),
            }
            return _envelope({'draft': draft}, None, '已保存编辑后的用例')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception('update_tp_draft 异常')
        return _envelope(None, None, '保存失败：%s' % e, 500)


class AddTpDraftReq(BaseModel):
    """「编辑候选用例」弹窗的「添加」：按表单内容新增一条候选用例草稿。

    ⚠️ `steps` 是前端字段名，落库列名是 `case_steps`。
    ⚠️ `tp_refs` / `target_*` / `source_id` 一律由后端按 pfp_id + tp_index 重算，
       绝不接受前端传参（防止伪造需求回标编号 / 关联串到别的测试点）。
    """
    pfp_id: int
    tp_index: int
    module: str = ''
    function_point: str = ''
    priority: int = 2
    pre_condition: str = ''
    steps: str = ''
    expected_result: str = ''
    remark: str = ''


@router.post('/add-tp-draft')
def add_tp_draft(req: AddTpDraftReq, request: Request):
    """新增一条候选用例草稿，返回 {draft: {...}}（形状与生成/编辑接口一致）。

    与 `_gen_tp_drafts` 的 INSERT 完全同构：同表、source_type=3、status=0、run_by=uid，
    因此能被 `/ai/drafts/adopt` 原样查到并采纳入库；关联通道也按同一口径：
      · 需求来源测试点 → source_id=requirement_id + tp_refs=真实 tp_ref（target_* 留 NULL）
      · 手动新增测试点 → target_pfp_id/target_tp_index/target_tp_title 显式坐标
    前端把返回的 draft push 进列表并勾选即可，无需重跑 AI、不消耗额度。
    """
    try:
        uid = _uid(request)
        if not uid:
            return _envelope(None, None, '未登录', 401)
        if not req.pfp_id or req.tp_index is None:
            return _envelope(None, None, '参数不完整：缺少功能点/测试点', 400)
        if not (str(req.module or '').strip() or str(req.function_point or '').strip()):
            return _envelope(None, None, '请至少填写模块或功能点', 400)
        try:
            priority = int(req.priority)
        except (TypeError, ValueError):
            priority = 2
        if priority < 1 or priority > 4:
            priority = 2
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT id, process_id, requirement_id, test_points, title "
                "FROM product_function_points WHERE id=%s AND is_del=0", (req.pfp_id,))
            if not row:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, row['process_id']):
                return _envelope(None, None, '无权操作', 404)
            items = _norm_tp_list(row.get('test_points'))
            if req.tp_index < 0 or req.tp_index >= len(items):
                return _envelope(None, None, '测试点不存在', 404)
            tp = items[req.tp_index]
            tp_title = str(tp.get('title') or '')
            tp_ref = str(tp.get('tp_ref') or '')
            is_manual_tp = not tp_ref
            # 建议入库项目：与 _gen_tp_drafts 同口径（该 pfp 已关联用例最多的项目）
            project_id = 0
            try:
                sug = db.execute_single_query(
                    "SELECT tc.project_id, COUNT(*) n FROM product_point_cases pc "
                    "JOIN test_cases tc ON tc.id=pc.case_id "
                    "WHERE pc.pfp_id=%s AND pc.is_del=0 AND tc.is_del=0 "
                    "GROUP BY tc.project_id ORDER BY n DESC LIMIT 1", (req.pfp_id,))
                if sug and sug.get('project_id'):
                    project_id = int(sug['project_id'])
            except Exception:  # noqa: BLE001
                project_id = 0
            from src.routers.ai_workbench import _batch_id
            did = _insert_id(
                db,
                'INSERT INTO ai_case_drafts (batch_id, source_type, source_id, suggest_project_id, '
                'module, function_point, priority, case_type, design_method, dims, routes, '
                'evidence_type, severity, pre_condition, case_steps, '
                'expected_result, remark, confidence, status, model_id, run_by, source_case_ids, tp_refs, '
                'target_pfp_id, target_tp_index, target_tp_title) '
                'VALUES (%s,3,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s)',
                (_batch_id(), row.get('requirement_id'), project_id,
                 str(req.module or '')[:255], str(req.function_point or '')[:512], priority,
                 '', '', '', '', 'basis', '',
                 str(req.pre_condition or '')[:65535],
                 str(req.steps or '')[:65535],
                 str(req.expected_result or '')[:65535],
                 str(req.remark or '')[:65535], 0.0,
                 None, uid, None, tp_ref,
                 row.get('id') if is_manual_tp else None,
                 req.tp_index if is_manual_tp else None,
                 tp_title[:512] if is_manual_tp else None),
            )
            draft = {
                'id': int(did),
                'module': str(req.module or ''),
                'function_point': str(req.function_point or ''),
                'priority': priority,
                'dims': [],
                'pre_condition': str(req.pre_condition or ''),
                'steps': str(req.steps or ''),
                'expected_result': str(req.expected_result or ''),
                'remark': str(req.remark or ''),
                'confidence': 0.0,
            }
            return _envelope({'draft': draft}, None, '已新增候选用例')
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception('add_tp_draft 异常')
        return _envelope(None, None, '新增候选用例失败：%s' % e, 500)


def _match_cases_for_pfp(db, uid, pfp, tp_items, case_ids, use_ai=True, ai_limit=20):
    """三层自动判定：给定功能点的一批用例，各自覆盖了哪几个测试点。

    返回 (matches, stats, ai_note)：
      matches 每项 {case_id, tp_indexes, source, confidence, reason}
        source = tp_ref(回标反查，零成本精确) / similar(标题相似，零成本)
                 / ai(模型判定) / none(无法判定，需人工指定)
      stats   含各来源计数与 auto_hit 合计、ai_used
      ai_note 非空表示 AI 层出过问题（已降级为前两层结果）

    ⚠️ 只产出建议，**不写库** —— 写入由调用方执行。
    """
    matches: List[Dict[str, Any]] = []
    ai_note = ''
    ids: List[int] = []
    for c in (case_ids or []):
        try:
            n = int(c)
        except Exception:
            continue
        if n > 0 and n not in ids:
            ids.append(n)
    empty_stats = {
        'total': 0, 'tp_ref': 0, 'similar': 0, 'ai': 0, 'none': 0,
        'auto_hit': 0, 'ai_used': False,
    }
    if not ids or not tp_items:
        return matches, empty_stats, ai_note

    ph = ', '.join(['%s'] * len(ids))
    cases = db.execute_query(
        f"""SELECT tc.id, tc.module, tc.function_point, tc.steps,
                   tc.expected_result, tc.pre_condition,
                   d.tp_refs, d.source_id
            FROM test_cases tc
            LEFT JOIN ai_case_drafts d ON d.id = tc.ai_draft_id
            WHERE tc.id IN ({ph}) AND tc.is_del=0""",
        tuple(ids),
    )
    if not cases:
        return matches, empty_stats, ai_note

    req_id = pfp.get('requirement_id')
    pending_ai: List[Dict[str, Any]] = []
    for c in cases:
        cid = int(c['id'])
        title = c.get('function_point') or ''
        # ---- 第 1 层：回标反查（零成本、精确） ----
        # ⚠️ 必须按需求分组：tp_ref 是「需求内原始下标+1」，跨需求会重号。
        # 只有草稿的 source_id 与本功能点的 requirement_id 一致才敢用。
        ref_idx: List[int] = []
        draft_refs = c.get('tp_refs')
        draft_src = c.get('source_id')
        if draft_refs and req_id and draft_src and int(draft_src) == int(req_id):
            ref_idx = tpmatch.match_by_tp_ref(draft_refs, tp_items)
        if ref_idx:
            matches.append({
                'case_id': cid,
                'tp_indexes': ref_idx,
                'source': 'tp_ref',
                'confidence': 1.0,
                'reason': '按生成时的测试点回标精确命中',
            })
            continue

        # ---- 第 2 层：标题相似度（零成本、瞬时） ----
        sim_idx, sim_score, sim_gap = tpmatch.match_by_similarity(title, tp_items)
        if sim_idx:
            tp_titles = '、'.join(
                tp_items[i].get('title', '') for i in sim_idx if i < len(tp_items))
            matches.append({
                'case_id': cid,
                'tp_indexes': sim_idx,
                'source': 'similar',
                'confidence': round(sim_score, 3),
                'reason': '标题与测试点相似度 %.0f%%（与次优拉开 %.0f%%）→ %s' % (
                    sim_score * 100, sim_gap * 100, tp_titles),
            })
            continue

        # ---- 前两层定不下来 → 留给第 3 层 AI ----
        pending_ai.append({
            'case_id': cid,
            'title': title,
            'steps': c.get('steps') or '',
            'expected_result': c.get('expected_result') or '',
        })

    # ---- 第 3 层：AI 判定（消耗额度，最后兜底） ----
    ai_used = False
    if pending_ai and use_ai:
        limit = max(1, min(int(ai_limit or 20), len(pending_ai)))
        batch = pending_ai[:limit]
        tp_text, case_text = tpmatch.build_ai_context(tp_items, batch)
        try:
            from src.ai import runner as ai_runner
            from src.ai import schema as ai_schema
            r = ai_runner.run_structured(
                db, uid, biz_type='tp_match', prompt_code='tp_match',
                prompt_vars={'tp_list': tp_text, 'case_list': case_text},
                list_key='matches', item_model=ai_schema.TpMatchItem,
                limit=limit * 2,
            )
            ai_used = True
            by_case: Dict[int, Dict[str, Any]] = {}
            for it in (r.get('items') or []):
                try:
                    cid = int(it.get('case_id') or 0)
                except Exception:
                    continue
                if cid:
                    by_case[cid] = it
            for item in batch:
                cid = item['case_id']
                it = by_case.get(cid)
                if not it:
                    matches.append({
                        'case_id': cid, 'tp_indexes': [], 'source': 'none',
                        'confidence': 0.0,
                        'reason': 'AI 未能判定，请手动指定覆盖的测试点',
                    })
                    continue
                # 白名单校正：只保留清单内真实存在的编号（模型爱自己编）
                # ⚠️ tp_refs 存的是 JSON 字符串（如 '["TP1"]'），
                # 必须先解析成 ['TP1'] 再转下标，否则会静默丢成空列表。
                idxs = tpmatch.clamp_indexes(
                    tpmatch.parse_ref_list(it.get('tp_refs')), len(tp_items))
                conf = float(it.get('confidence') or 0)
                matches.append({
                    'case_id': cid,
                    'tp_indexes': idxs,
                    'source': 'ai' if idxs else 'none',
                    'confidence': round(conf, 3),
                    'reason': (it.get('reason') or '') or (
                        'AI 判定覆盖 %d 个测试点' % len(idxs) if idxs
                        else 'AI 判定不覆盖任何测试点'),
                })
            if len(pending_ai) > limit:
                ai_note = 'AI 判定只处理了前 %d 条，其余 %d 条请手动指定' % (
                    limit, len(pending_ai) - limit)
                for item in pending_ai[limit:]:
                    matches.append({
                        'case_id': item['case_id'], 'tp_indexes': [],
                        'source': 'none', 'confidence': 0.0,
                        'reason': '超出单次 AI 判定上限，请手动指定',
                    })
        except Exception as e:  # noqa: BLE001
            logger.warning('AI 判定用例覆盖测试点失败: %s', e)
            ai_note = 'AI 判定失败（%s），已保留前两层结果' % e
            for item in batch:
                matches.append({
                    'case_id': item['case_id'], 'tp_indexes': [],
                    'source': 'none', 'confidence': 0.0,
                    'reason': 'AI 判定失败，请手动指定覆盖的测试点',
                })
    else:
        for item in pending_ai:
            matches.append({
                'case_id': item['case_id'], 'tp_indexes': [],
                'source': 'none', 'confidence': 0.0,
                'reason': '相似度不足且未启用 AI 判定，请手动指定覆盖的测试点',
            })

    stats = {
        'total': len(matches),
        'tp_ref': sum(1 for m in matches if m['source'] == 'tp_ref'),
        'similar': sum(1 for m in matches if m['source'] == 'similar'),
        'ai': sum(1 for m in matches if m['source'] == 'ai'),
        'none': sum(1 for m in matches if m['source'] == 'none'),
        'ai_used': ai_used,
    }
    stats['auto_hit'] = stats['tp_ref'] + stats['similar'] + stats['ai']
    return matches, stats, ai_note


@router.post('/auto-match')
async def auto_match(req: AutoMatchReq, request: Request):
    """自动判定一批用例覆盖该功能点的哪几个测试点（**只出建议，不写库**）。

    三层信号，按成本从低到高：
      1. 回标反查（零成本、精确）：AI 生成用例的草稿带 tp_refs，
         与功能点测试点的 tp_ref 求交集。
      2. 标题相似度（零成本）：用例标题 vs 测试点标题的字符 bigram Dice，
         判据是「分数够高 + 与次优拉开差距」双条件。
      3. AI 判定（消耗额度）：前两层定不下来的才送模型，回标结果做白名单校正。

    返回每条用例的 {case_id, tp_indexes, source, confidence, reason}。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id:
        return _envelope(None, None, '缺少功能点 id', 400)
    if not req.case_ids:
        return _envelope({'matches': [], 'stats': {}}, None, '请先选择用例')

    try:
        with get_db_handler() as db:
            pfp = db.execute_single_query(
                "SELECT id, process_id, requirement_id, title, test_points "
                "FROM product_function_points WHERE id=%s AND is_del=0",
                (req.pfp_id,),
            )
            if not pfp:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, pfp['process_id']):
                return _envelope(None, None, '无权操作', 404)

            tp_items = _norm_tp_list(pfp.get('test_points'))
            if not tp_items:
                return _envelope(
                    {'matches': [], 'stats': {}},
                    None, '该功能点还没有测试点，无法自动匹配')

            case_ids = [int(c) for c in req.case_ids if int(c) > 0]
            if not case_ids:
                return _envelope({'matches': [], 'stats': {}}, None, '请先选择用例')

            matches, stats, ai_note = _match_cases_for_pfp(
                db, uid, pfp, tp_items, case_ids,
                use_ai=req.use_ai, ai_limit=req.ai_limit)
            if not matches:
                return _envelope(None, None, '未找到用例', 404)

            msg = '已自动匹配 %d 条（回标 %d / 相似 %d / AI %d / 待确认 %d）' % (
                stats['total'], stats['tp_ref'], stats['similar'], stats['ai'], stats['none'])
            if ai_note:
                msg += '；' + ai_note
            return _envelope(
                {'matches': matches, 'stats': stats,
                 'ai_used': stats.get('ai_used', False), 'ai_note': ai_note},
                None, msg)
    except Exception as e:  # noqa: BLE001
        logger.warning('自动匹配失败: %s', e)
        return _envelope(None, None, f'自动匹配失败: {e}', 500)


@router.post('/unlink-case')
async def unlink_case(req: UnlinkCaseReq, request: Request):
    """取消某功能点下的一条关联用例。"""
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT pfp.id, pfp.process_id FROM product_point_cases pc "
            "JOIN product_function_points pfp ON pfp.id=pc.pfp_id "
            "WHERE pc.pfp_id=%s AND pc.case_id=%s AND pc.is_del=0",
            (req.pfp_id, req.case_id),
        )
        if not row:
            return _envelope(None, None, '关联记录不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        db.execute_update(
            "UPDATE product_point_cases SET is_del=1 WHERE pfp_id=%s AND case_id=%s AND is_del=0",
            (req.pfp_id, req.case_id),
        )
    return _envelope(None, None, '已取消关联')


class UnlinkCasesReq(BaseModel):
    """批量取消关联：把选中的多条正式用例从功能点下解绑。"""
    pfp_id: int
    case_ids: List[int] = []


@router.post('/unlink-cases')
async def unlink_cases(req: UnlinkCasesReq, request: Request):
    """批量取消某功能点下的多条关联用例（幂等：已解绑的跳过）。"""
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id:
        return _envelope(None, None, '缺少功能点 id', 400)
    if not req.case_ids:
        return _envelope(None, None, '请至少选择一条', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id FROM product_function_points WHERE id=%s AND is_del=0",
            (req.pfp_id,),
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)
        # 仅对当前仍生效(is_del=0)的关联执行解绑，保证幂等
        ex = {r['case_id'] for r in db.execute_query(
            "SELECT case_id FROM product_point_cases WHERE pfp_id=%s AND is_del=0",
            (req.pfp_id,),
        )}
        target = [c for c in req.case_ids if c in ex]
        for cid in target:
            db.execute_update(
                "UPDATE product_point_cases SET is_del=1 WHERE pfp_id=%s AND case_id=%s AND is_del=0",
                (req.pfp_id, cid),
            )
        return _envelope({'unlinked': len(target)}, None, '已取消关联 %d 条' % len(target))


# ===========================================================================
# 六、测试点变更影响诊断（三级测试点行「诊断」按钮）
#
#  需求：用户改了已经关联了用例的测试点 → 需要知道
#        ① 这个改动是否影响了对应的用例
#        ② 用例的哪些地方要改
#        ③ 把新的测试用例罗列出来，与旧用例做新旧对比，判断是否更换用例
#
#  核心约束：**必须有「改前锚点」**。
#  update-test-point 以前是直接覆盖 test_points[].title，改前的文字没地方存 ——
#  没有 old_title，「新旧对比」就是假的（只能拿新测试点重新体检一遍旧用例）。
#  现在补了 product_point_changes 表做留痕（见 add_product_point_changes.py）。
#
#  降级策略（决策 1.A）：无改前历史时**不拒绝**，降级为「当前一致性审查」，
#  并在返回里 has_history=false 明示，前端弹窗顶部要提示用户。
# ===========================================================================
class DiagnoseTpImpactReq(BaseModel):
    """诊断某个测试点变更对关联用例的影响。"""
    pfp_id: int
    tp_index: int                 # 测试点在 test_points 数组里的 0 基下标
    change_id: Optional[int] = None   # 指定某次修改记录；不传取最近一次
    use_ai: bool = True
    model_id: Optional[int] = None
    gen_count: int = 2            # 生成几条新用例做对比（1~4）


def _load_change_anchor(db, pfp_id: int, tp_index: int, change_id: Optional[int],
                        tp_items: Optional[List[dict]] = None) -> Optional[dict]:
    """取「改前锚点」：改前测试点的原文。

    change_id 指定 → 取那一条（用户显式指定，不做匹配）。
    不指定 → 按**测试点自持的 `chg` 标记**精确取（标记记着留痕 id）。
    标记缺失（老数据）→ 回退到 `_match_change_to_tp` 反推。
    查不到 → 返回 None（走降级：当前一致性审查）。

    ⚠️ 与「按钮是否可点」的关系（11/04 晚）：按钮由 `_tp_change_flags` 按
       `chg.pending` 判定，诊断处理完成后即熄灭；但 `chg` 本身**不删除**
       （只把 pending 置 false），所以这里仍能取到改前锚点、历史可追溯。
       按钮点亮时 chg 必来自一次真改 → 不会「按钮亮着却没有改前历史」。
    """
    if change_id:
        return db.execute_single_query(
            "SELECT id, pfp_id, tp_index, tp_ref, old_title, new_title, old_tp_json, "
            "new_tp_json, affected_case_ids, operator_id, create_time "
            "FROM product_point_changes WHERE id=%s AND pfp_id=%s AND is_del=0",
            (change_id, pfp_id),
        )
    # ---- ① 粘性标记：测试点自己记着最近一次修改的留痕 id → 精确取 ----
    tp_chg = None
    if tp_items is not None and 0 <= tp_index < len(tp_items):
        tp_chg = (tp_items[tp_index] or {}).get('chg') or None
    if tp_chg and tp_chg.get('id'):
        row = db.execute_single_query(
            "SELECT id, pfp_id, tp_index, tp_ref, old_title, new_title, old_tp_json, "
            "new_tp_json, affected_case_ids, operator_id, create_time "
            "FROM product_point_changes WHERE id=%s AND pfp_id=%s AND is_del=0",
            (int(tp_chg['id']), pfp_id),
        )
        if row:
            # ⚠️ 空改留痕（改前 == 改后）不是一次「真正的修改」→ 没有可比对的锚点
            if not _tp_change_is_real(row):
                return None
            return row
        # 留痕行被软删/物理删了 → 标记里的 old_title 仍可用，构造一个等价锚点
        if tp_chg.get('old_title'):
            # ⚠️ 标记自带改后标题 → 自证是不是真改（老标记没这个字段 → 保守当真改）
            nt = str(tp_chg.get('new_title') or '').strip()
            if nt and not _chg_is_real_by_titles(tp_chg.get('old_title'), nt):
                return None
            return {
                'id': int(tp_chg['id']),
                'pfp_id': int(pfp_id),
                'tp_index': int(tp_index),
                'tp_ref': (tp_items[tp_index] or {}).get('tp_ref') or '',
                'old_title': tp_chg.get('old_title') or '',
                'new_title': nt or (tp_items[tp_index] or {}).get('title') or '',
                'old_tp_json': None,
                'new_tp_json': None,
                'affected_case_ids': None,
                'operator_id': None,
                'create_time': tp_chg.get('time') or '',
                'is_del': 0,
            }
    # ---- ② 老数据兜底：无标记 → 按留痕反推 ----
    if tp_items is None:
        return db.execute_single_query(
            "SELECT id, pfp_id, tp_index, tp_ref, old_title, new_title, old_tp_json, "
            "new_tp_json, affected_case_ids, operator_id, create_time "
            "FROM product_point_changes WHERE pfp_id=%s AND tp_index=%s AND is_del=0 "
            "ORDER BY id DESC LIMIT 1",
            (pfp_id, tp_index),
        )
    changes = _load_pfp_changes(db, [pfp_id]).get(int(pfp_id), []) or []
    hit = _match_change_to_tp(changes, tp_items, tp_index)
    if not hit:
        return None
    row = db.execute_single_query(
        "SELECT id, pfp_id, tp_index, tp_ref, old_title, new_title, old_tp_json, "
        "new_tp_json, affected_case_ids, operator_id, create_time "
        "FROM product_point_changes WHERE id=%s AND pfp_id=%s AND is_del=0",
        (hit['id'], pfp_id),
    )
    # ⚠️ 同口径：空改留痕不构成锚点
    if row and not _tp_change_is_real(row):
        return None
    return row


def _load_linked_cases(db, pfp_id: int, tp_index: int) -> List[dict]:
    """取「显式关联了本测试点」的全部用例（含正文），用于诊断判定。

    口径与前端 `caseCount` 一致：只有 tp_indexes 含本下标的才算关联；
    tp_indexes 为 NULL/空（未指定测试点）的那批不参与诊断。

    ⚠️ 这里额外取了 `tc.project_id` —— link-cases 接口漏了这个字段，
    但「应用到用例」要写回 test_cases 全字段，project_id 是必填项，必须能拿到。
    """
    rows = db.execute_query(
        """SELECT pc.id AS rel_id, pc.case_id, pc.tp_indexes,
                  tc.id, tc.project_id, tcp.cate_name AS project, tc.module,
                  tc.function_point, tc.priority, tc.steps, tc.expected_result,
                  tc.pre_condition, tc.remark, tc.status
           FROM product_point_cases pc
           LEFT JOIN test_cases tc ON tc.id = pc.case_id
           LEFT JOIN test_case_project tcp ON tcp.id = tc.project_id
           WHERE pc.pfp_id=%s AND pc.is_del=0
           ORDER BY pc.id DESC""",
        (pfp_id,),
    )
    out = []
    for r in rows or []:
        idxs = _parse_tp_indexes(r.get('tp_indexes'))
        if not idxs or tp_index not in idxs:
            continue
        # 用例本体已被删 → 跳过（孤儿关联行，另行清理）
        if not r.get('id'):
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 本地启发式兜底（AI 不可用时用）
#
# ⚠️ 为什么需要它：AI 调用失败时，若把用例判成「不受影响」，那是**静默假阴性**
#    —— UI 上是绿色标签，用户会以为「AI 判定过了，确实不用改」，从而漏掉一次
#    真实的语义反转（例：测试点从「点击需要有弹窗提示」改成「点击不需要有弹窗提示」，
#    而用例 expected_result 仍写着「弹出提示弹窗…不跳转第三方」，本该判「需重写」）。
#    所以：**没有 AI 结论 = 未判定(unknown)，绝不等于「不受影响」**。
#
# 启发式的定位：**只在「明确反向」时下结论，拿不准一律 unknown**。
# 它给的是方向（哪条用例的正文跟改后的测试点对不上），不给改写内容。
# ---------------------------------------------------------------------------

# 否定/反向词（用于检测「同一件事的正反表述」）
_NEG_RE = re.compile(
    r'不需要|没有|无需|不会|不得|不再|不弹|不显示|不出现|不跳转|不进入|不弹窗|不|无|非'
)

# 通用动词/名词：命中它们不足以说明「用例正文在描述改前测试点要求的事」→ 排除
_TP_COMMON = {
    '点击', '功能', '操作', '支持', '可以', '能够', '进行', '显示', '提示', '页面', '用户',
    '系统', '内容', '相关', '对应', '正确', '正常', '成功', '失败', '存在', '一个', '这个',
    '界面', '按钮', '信息', '数据', '是否', '需要', '并且', '以及', '根据', '对应',
}


def _tp_bigrams(text: str) -> set:
    """把一段中文标题切成可比较的语素集合（中文 2-gram + 英文/数字整词）。

    2-gram 的作用：中文没有空格分词，「有弹窗提示」→ 有弹/弹窗/窗提/提示，
    这样「弹窗」「提示」既能单独命中，也能反映相邻字的组合。
    """
    t = re.sub(r'[^0-9a-zA-Z\u4e00-\u9fa5]+', ' ', (text or '').lower())
    out = set()
    for seg in t.split():
        if re.match(r'^[0-9a-zA-Z]+$', seg):
            out.add(seg)
            continue
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _local_heuristic_impacts(old_title: str, new_title: str, cases: List[dict]) -> List[dict]:
    """AI 不可用时的本地兜底判定。

    规则（**保守**，只在有把握时下结论）：
      ① 该用例正文命中了「改前标题」的语素 → 说明用例确实在描述改前那件事；
      ② 该用例正文**没有**命中「改后标题」的关键语素 → 说明改后的要求它没覆盖；
      ③ 满足 ①② 且改前改后**否定词极性相反** → 判 major（需重写）；
         满足 ①② 但极性不变 → 判 minor（需微调）；
      ④ 任一条不满足 → 不下结论（调用方记为 unknown）。

    返回的条目**不含 fixed_*** —— 本地启发式不生成改写内容（自动改写正文风险高），
    前端会明示「未生成改写内容，请重试 AI 或人工修改」。
    """
    old_t = (old_title or '').strip()
    new_t = (new_title or '').strip()
    if not old_t or not new_t or old_t == new_t:
        return []
    old_grams = _tp_bigrams(old_t)
    new_grams = _tp_bigrams(new_t)
    if not old_grams or not new_grams:
        return []
    old_neg = bool(_NEG_RE.search(old_t))
    new_neg = bool(_NEG_RE.search(new_t))
    polarity_flipped = (old_neg != new_neg)

    out = []
    for c in cases:
        body = ' '.join([
            c.get('pre_condition') or '', c.get('steps') or '', c.get('expected_result') or '',
        ])
        body_grams = _tp_bigrams(body)
        # ① 用例正文命中了改前的哪些「有意义的」语素
        old_hit = {g for g in old_grams if g in body_grams} - _TP_COMMON
        if not old_hit:
            continue    # 这条用例根本没在讲改前测试点的事 → 无法本地判断
        # ② 改后的哪些语素在这条用例正文里根本没出现
        new_miss = {g for g in new_grams if g not in body_grams} - _TP_COMMON
        if not new_miss:
            continue    # 改后的要求它已覆盖 → 很可能确实不受影响，但不敢断言 → unknown

        level = 'major' if polarity_flipped else 'minor'
        # ③ 推断要改哪些字段：正文哪个部分命中了改前语素就改哪个
        diff_fields = []
        for key, label in (('pre_condition', '前置条件'), ('steps', '步骤'),
                           ('expected_result', '预期结果')):
            fg = _tp_bigrams(c.get(key) or '')
            if fg & old_hit:
                diff_fields.append(key)
        if not diff_fields:
            diff_fields = ['steps', 'expected_result']

        reason = ('本地启发式判定（AI 未完成判定，非模型结论）：改前「%s」与改后「%s」%s，'
                  '而该用例正文仍在描述改前的要求（命中：%s；改后要求未覆盖：%s）'
                  % (old_t, new_t,
                     '语意相反' if polarity_flipped else '要求发生变化',
                     '、'.join(sorted(old_hit)[:4]), '、'.join(sorted(new_miss)[:4])))
        out.append({
            'case_id': int(c['id']),
            'impact_level': level,
            'diff_fields': diff_fields,
            'diff_reason': reason,
            'fixed_pre': '', 'fixed_steps': '', 'fixed_expected': '',
            'suggestion': '重写（本地启发式判定，需人工复核）' if level == 'major'
                         else '微调（本地启发式判定，需人工复核）',
            'heuristic': True,
        })
    return out


@router.post('/diagnose-tp-impact')
async def diagnose_tp_impact(req: DiagnoseTpImpactReq, request: Request):
    """诊断测试点变更对关联用例的影响（**不改用例正文 / 关联关系**）。

    返回三部分：
      ① linked_cases：每条关联用例的影响判定（none/minor/major）+ 要改的字段 + 改写建议
      ② new_cases：基于**改后**测试点新生成的用例草稿（旧 vs 新对比的「新」）
      ③ has_history：是否有改前锚点。无 → 降级为「当前一致性审查」，前端明示

    ⚠️ 唯一写库的两个副作用：留痕 `diagnosis_json` 回填，以及
       **诊断成功（结论可信）后消费 `chg.pending`**（`data.changed=false`）。
       后者是 11/05 新增：只要点过一次诊断并拿到可信结论，三级行「诊断」
       按钮就置灰，不再依赖用户是否执行了「应用 / 替换 / 生成」。
       消费只改状态位，不删 chg → `_load_change_anchor` 的改前锚点仍可追溯。
    """
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.pfp_id or req.tp_index is None:
        return _envelope(None, None, '参数不完整', 400)
    gen_count = max(1, min(int(req.gen_count or 2), 4))
    fallbacks: List[str] = []

    try:
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT id, process_id, requirement_id, test_points, title "
                "FROM product_function_points WHERE id=%s AND is_del=0",
                (req.pfp_id,),
            )
            if not row:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, row['process_id']):
                return _envelope(None, None, '无权操作', 404)

            tp_items = _norm_tp_list(row.get('test_points'))
            if not tp_items or not (0 <= req.tp_index < len(tp_items)):
                return _envelope(None, None, '测试点下标越界', 400)
            tp = tp_items[req.tp_index]
            new_title = (tp.get('title') or '').strip()

            # ---- ① 取改前锚点 ----
            # ⚠️ 用与「是否被修改过」标记相同的匹配逻辑（tp_ref → 下标+标题 → 唯一标题），
            #    否则测试点删除导致下标回缩时，会出现「按钮亮着但诊断说没有改前历史」。
            anchor = _load_change_anchor(db, req.pfp_id, req.tp_index, req.change_id, tp_items)
            has_history = bool(anchor)
            old_title = (anchor.get('old_title') or '').strip() if anchor else ''
            if not has_history:
                fallbacks.append('该测试点没有修改历史，无法做新旧对比，本次仅判断用例是否仍与测试点匹配')

            # ---- ② 取关联用例 ----
            linked = _load_linked_cases(db, req.pfp_id, req.tp_index)
            if not linked:
                # 「无需诊断」也是一次明确的诊断结论 → 同样消费诊断资格，
                # 否则用户刚改完的测试点会因为「没有关联用例」而一直亮着按钮。
                consumed = _consume_tp_pending(db, req.pfp_id, req.tp_index)
                return _envelope(
                    {'has_history': has_history, 'old_title': old_title, 'new_title': new_title,
                     'linked_cases': [], 'new_cases': [], 'ai_used': False,
                     'changed': False if consumed else None,
                     'fallbacks': fallbacks + ['该测试点没有显式关联任何用例，无需诊断']},
                    None, '该测试点没有关联用例，无需诊断')

            # ---- ③ 组装上下文 ----
            tp_dims = coverage_mod.normalize_dims(tp.get('dims'))
            dims_text = '、'.join(
                '%s %s' % (d, coverage_mod.COVERAGE_DIM_NAMES.get(d, '')) for d in tp_dims
            ) or '（无）'
            raw_tp = _resolve_raw_tp(db, row.get('requirement_id'), tp)
            raw_tp_json = json.dumps(raw_tp, ensure_ascii=False) if isinstance(raw_tp, dict) else ''

            cases_for_ai = []
            for c in linked:
                cases_for_ai.append({
                    'case_id': int(c['id']),
                    'module': c.get('module') or '',
                    'function_point': c.get('function_point') or '',
                    'pre_condition': c.get('pre_condition') or '',
                    'steps': c.get('steps') or '',
                    'expected_result': c.get('expected_result') or '',
                })
            cases_json = json.dumps(cases_for_ai, ensure_ascii=False, indent=2)

            # ---- ④ AI 判定 ----
            impacts: List[dict] = []
            ai_used = False
            ai_failed = False
            valid_case_ids = {int(c['id']) for c in linked}
            if req.use_ai:
                from src.ai import runner as ai_runner
                from src.ai import schema as ai_schema
                try:
                    r = ai_runner.run_structured(
                        db, uid, biz_type='tp_impact', prompt_code='tp_impact',
                        prompt_vars={
                            'old_title': old_title,
                            'new_title': new_title,
                            'dims': dims_text,
                            'raw_tp_json': raw_tp_json,
                            'cases_json': cases_json,
                        },
                        list_key='impacts', item_model=ai_schema.TpImpactItem,
                        limit=len(cases_for_ai) * 2, model_id=req.model_id,
                    )
                    impacts = r.get('items') or []
                    ai_used = bool(r.get('model'))
                    if not impacts:
                        # 模型通了但没给出任何判定 → 同样按「未判定」处理，不当成「不受影响」
                        ai_failed = True
                        fallbacks.append('AI 未返回任何判定结果，已按「未判定」处理')
                except HTTPException:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.exception('测试点变更影响诊断失败 pfp=%s tp=%s', req.pfp_id, req.tp_index)
                    fallbacks.append('AI 判定失败：%s（结论不可信，请重试）' % e)
                    impacts = []
                    ai_failed = True
            else:
                fallbacks.append('未启用 AI，已按「未判定」处理')

            # ---- ④.5 AI 不可用时的本地启发式兜底 ----
            # ⚠️ 绝不能在这里把用例判成「不受影响」—— 那是静默假阴性。
            #    本地启发式只对「明确反向」下结论，其余保持 unknown。
            if ai_failed and has_history and old_title and new_title:
                try:
                    heur = _local_heuristic_impacts(old_title, new_title, linked)
                    if heur:
                        # 并入 impacts 走同一条校验/兜底链路（case_id 仍然受白名单约束）
                        impacts.extend(heur)
                        fallbacks.append(
                            'AI 未完成判定，已用本地启发式标出 %d 条疑似受影响的用例'
                            '（仅提示方向，未生成改写内容，请重试 AI 或人工修改）' % len(heur))
                except Exception as e:  # noqa: BLE001
                    logger.warning('本地启发式兜底失败: %s', e)

            # ---- 本地兜底 + 校验（防 AI 编造 case_id / 漏字段）----
            by_case: Dict[int, dict] = {}
            for it in impacts:
                cid = it.get('case_id')
                try:
                    cid = int(cid)
                except Exception:
                    continue
                if cid not in valid_case_ids:
                    # ⚠️ 模型编造的 case_id —— 直接丢弃，不让它进入写回链路
                    fallbacks.append('AI 返回了不存在的用例编号 #%s，已忽略' % cid)
                    continue
                if cid in by_case:
                    continue
                by_case[cid] = it

            result_cases = []
            for c in linked:
                cid = int(c['id'])
                it = by_case.get(cid)
                if not it:
                    # ⚠️ 没有 AI 结论 → 记「未判定(unknown)」，**绝不记「不受影响」**
                    #    否则 AI 一挂，UI 上全是绿色「不受影响」，用户会误以为不用改。
                    level = 'unknown'
                    if req.use_ai:
                        fallbacks.append('AI 未对用例 #%d 给出判定，已记为「未判定」（结论不可信，请重试）' % cid)
                else:
                    level = it.get('impact_level') or 'none'
                    if level == 'unknown':
                        level = 'none'
                diff_fields = list(it.get('diff_fields') or []) if it else []
                # 兜底：判为需改但没给 diff_fields → 按 fixed_* 是否有内容反推
                if it and level in ('minor', 'major') and not diff_fields:
                    for f, key in (('pre_condition', 'fixed_pre'), ('steps', 'fixed_steps'),
                                   ('expected_result', 'fixed_expected')):
                        if (it.get(key) or '').strip():
                            diff_fields.append(f)
                    if diff_fields:
                        fallbacks.append('用例 #%d 判定为「%s」但未列出待改字段，已按改写内容推断' % (cid, level))
                # 兜底：判为需改但没给改写内容 → 该字段不可采纳
                fixed_pre = (it.get('fixed_pre') or '') if it else ''
                fixed_steps = (it.get('fixed_steps') or '') if it else ''
                fixed_expected = (it.get('fixed_expected') or '') if it else ''
                is_heuristic = bool(it.get('heuristic')) if it else False
                if it and level in ('minor', 'major') and not is_heuristic:
                    if not any([fixed_pre, fixed_steps, fixed_expected]):
                        fallbacks.append('用例 #%d 判定为「%s」但未给出改写内容，无法直接采纳' % (cid, level))
                        level = 'none'
                elif it and level in ('minor', 'major') and is_heuristic:
                    fallbacks.append('用例 #%d 的判定来自本地启发式（非 AI 结论），未生成改写内容，无法直接采纳'
                                     % cid)
                result_cases.append({
                    'case_id': cid,
                    'rel_id': c.get('rel_id'),
                    'project_id': c.get('project_id'),
                    'project': c.get('project') or '',
                    'module': c.get('module') or '',
                    'function_point': c.get('function_point') or '',
                    'priority': c.get('priority'),
                    'pre_condition': c.get('pre_condition') or '',
                    'steps': c.get('steps') or '',
                    'expected_result': c.get('expected_result') or '',
                    'remark': c.get('remark') or '',
                    'tp_indexes': _parse_tp_indexes(c.get('tp_indexes')) or [],
                    'impact_level': level,
                    'diff_fields': diff_fields,
                    'diff_reason': (it.get('diff_reason') or '') if it else '',
                    'fixed_pre': fixed_pre,
                    'fixed_steps': fixed_steps,
                    'fixed_expected': fixed_expected,
                    'suggestion': (it.get('suggestion') or '') if it else '',
                    'heuristic': is_heuristic,
                })

            # ---- ⑤ 生成新用例做对比（复用补充用例通道，决策 5.A）----
            # ⚠️ 省 token（11/05）：**全部关联用例都明确判定「不受影响」时，跳过整个
            #    新用例生成阶段**。原来无条件调 _gen_tp_drafts(use_ai=True) → 再烧一次
            #    模型额度，还往 ai_case_drafts 写一批没人会采纳的孤儿草稿。
            #    只有存在「受影响(需微调/需重写)」或「未判定」的用例时，才有必要准备
            #    「替换为全新用例」的素材。
            # ⚠️ unknown（未判定）**绝不算「不受影响」**（铁律：防静默假阴性）——
            #    AI 挂了/漏判定 → 保留生成通道，用户重试后仍能看到新用例草稿。
            n_impact = sum(1 for x in result_cases if x['impact_level'] in ('minor', 'major'))
            n_unknown = sum(1 for x in result_cases if x['impact_level'] == 'unknown')
            need_gen = (n_impact + n_unknown) > 0
            gen_skipped = not need_gen
            gen: Dict[str, Any] = {}
            new_cases: List[dict] = []
            if need_gen:
                focus_override = ''
                if has_history and old_title:
                    focus_override = '注意：本测试点原为「%s」，现已修改为「%s」，请按修改后的内容编写用例' % (
                        old_title, new_title)
                gen = _gen_tp_drafts(db, uid, row, req.tp_index, count=gen_count,
                                     use_ai=req.use_ai, model_id=req.model_id,
                                     focus_override=focus_override)
                new_cases = gen.get('drafts') or []
                if gen.get('error') and not new_cases:
                    fallbacks.append('新用例生成失败：%s' % gen['error'])
            else:
                fallbacks.append(
                    '本次判定 %d 条关联用例均不受影响，已跳过新用例 AI 生成（未消耗生成额度）'
                    % len(result_cases))

            # 诊断结果回填到留痕行（供回溯；失败不影响主流程）
            if anchor:
                try:
                    snap = json.dumps({
                        'linked_cases': [
                            {'case_id': x['case_id'], 'impact_level': x['impact_level'],
                             'diff_fields': x['diff_fields'], 'diff_reason': x['diff_reason']}
                            for x in result_cases
                        ],
                        'fallbacks': fallbacks,
                    }, ensure_ascii=False)
                    db.execute_update(
                        "UPDATE product_point_changes SET diagnosis_json=%s WHERE id=%s",
                        (snap, anchor['id']),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning('诊断结果回填留痕失败 change=%s: %s', anchor.get('id'), e)

            # ---- ⑥ 消费「本次修改」的诊断资格（11/05）----
            # ⚠️ 消费时机从「应用/替换成功」前移到「诊断成功」：
            #    上一轮新增「全部不受影响 → 跳过生成新用例」后，这类诊断
            #    既没有受影响用例可应用、也没有新用例可替换，用户没有任何
            #    处理动作可做 → 若还在 apply/replace 里消费，pending 永远不翻，
            #    三级行「诊断」按钮会一直亮着（可反复点、反复烧 AI）。
            #    新口径：只要点过诊断、且拿到**可信结论**，本次修改就算处理完
            #    → 按钮立即置灰；必须再次真正修改测试点内容才会重新点亮。
            # ⚠️ 例外：AI 判定失败 / 存在 unknown（结论不可信）时不消费，
            #    保留重试入口 —— 与弹窗「开始诊断」按钮的重试例外同口径。
            # ⚠️ 只把 pending 置 false，不删 chg → 改前锚点仍可追溯，
            #    同一次弹窗内的「重试诊断」仍能取到改前内容。
            trustworthy = (not ai_failed) and n_unknown == 0
            consumed = _consume_tp_pending(db, req.pfp_id, req.tp_index) if trustworthy else False

            msg = '诊断完成：%d 条关联用例，%d 条受影响' % (len(result_cases), n_impact)
            if n_unknown:
                msg += '，%d 条未判定（AI 未完成判定，结论不可信，请重试）' % n_unknown
            if gen_skipped:
                msg += '；全部不受影响，已跳过新用例生成（节省 AI 额度）'
            return _envelope(
                {'pfp_id': req.pfp_id, 'tp_index': req.tp_index,
                 'tp_ref': tp.get('tp_ref') or '',
                 'is_manual_tp': not (tp.get('tp_ref') or ''),
                 'tp_title': new_title,
                 'old_title': old_title, 'new_title': new_title,
                 'change_id': anchor.get('id') if anchor else None,
                 'has_history': has_history,
                 'change_time': str(anchor.get('create_time') or '') if anchor else '',
                 'linked_cases': result_cases,
                 'new_cases': new_cases,
                 'suggested_project_id': gen.get('suggested_project_id', 0),
                 'suggested_project_name': gen.get('suggested_project_name', ''),
                 'ai_used': ai_used, 'ai_failed': ai_failed,
                 # 前端据此把「未生成出新用例」的空态换成「已跳过生成」的说明
                 'gen_skipped': gen_skipped,
                 # 诊断成功即消费资格 → 前端把当前行 changed 就地置 false
                 # （不依赖「应用/替换」是否执行；结论不可信时为 None，保留重试）
                 'changed': False if consumed else None,
                 'impact_count': n_impact, 'unknown_count': n_unknown,
                 'fallbacks': fallbacks},
                None, msg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception('diagnose_tp_impact 异常')
        return _envelope(None, None, '诊断失败：%s' % e, 500)


# ===========================================================================
# 七、诊断结果的采纳（两个独立动作，决策 6.A）
#
#  A) 「应用到用例」：把 AI 的 fixed_* 写回**原用例正文**。
#     用例 ID 不变、关联关系不变、不产生孤儿行 —— 这是推荐动作。
#
#  B) 「替换为新用例」：旧用例归档（is_del=1），新草稿入库并**继承原关联**。
#     风险点：归档旧用例会产生孤儿关联行 → 必须在同一 try/finally 里清理。
#     另注意：uk_pfp_case(is_del, pfp_id, case_id) 含 is_del，
#     同一 (pfp,case) 不能同时存在两行 is_del=1 的墓碑 →
#     归档时要先看有没有同 (pfp,case) 的旧墓碑行，有则复用（复活）而不是再插一条。
# ===========================================================================
class ApplyTpImpactReq(BaseModel):
    """「应用到用例」：把诊断给出的改写内容写回原用例。"""
    pfp_id: int
    tp_index: int
    items: List[Dict[str, Any]] = []
    # items 每项：{'case_id': 123, 'pre_condition': '...', 'steps': '...', 'expected_result': '...'}


class ReplaceTpCaseReq(BaseModel):
    """「替换为新用例」：旧用例归档，新用例入库并继承关联。

    ⚠️ 支持**一次替换多条**（`draft_ids` 列表）：
       旧用例只归档一次，**每条**新用例都继承它的全部关联行。
       兼容旧的单条调用（`draft_id`）。
    """
    pfp_id: int
    tp_index: int
    old_case_id: int           # 被替换的旧用例
    draft_id: Optional[int] = None            # 新用例草稿（diagnose 返回的 new_cases[].id）
    draft_ids: List[int] = []                 # 新用例草稿列表（多条替换，优先于 draft_id）
    project_id: Optional[int] = None   # 新用例归属项目；不传则沿用旧用例的


@router.post('/apply-tp-impact')
async def apply_tp_impact(req: ApplyTpImpactReq, request: Request):
    """「应用到用例」：把诊断的改写建议写回原用例正文（用例 ID 与关联都不变）。"""
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    if not req.items:
        return _envelope(None, None, '请选择要应用的用例', 400)
    with get_db_handler() as db:
        row = db.execute_single_query(
            "SELECT id, process_id FROM product_function_points WHERE id=%s AND is_del=0",
            (req.pfp_id,),
        )
        if not row:
            return _envelope(None, None, '功能点不存在', 404)
        admin = _is_admin(db, uid)
        if not _process_visible(db, uid, admin, row['process_id']):
            return _envelope(None, None, '无权操作', 404)

        # 只允许改「本测试点显式关联的用例」——防越权改别的用例
        allowed = {int(c['id']) for c in _load_linked_cases(db, req.pfp_id, req.tp_index)}
        applied = []
        skipped = []
        for it in req.items:
            try:
                cid = int(it.get('case_id'))
            except Exception:
                continue
            if cid not in allowed:
                skipped.append(cid)
                continue
            fields, params = [], []
            for col in ('pre_condition', 'steps', 'expected_result'):
                v = it.get(col)
                # ⚠️ 空值/纯空白一律不写 —— 否则会把用例正文清空
                if v is None or not str(v).strip():
                    continue
                fields.append('%s=%%s' % col)
                params.append(str(v))
            if not fields:
                skipped.append(cid)
                continue
            params.append(cid)
            db.execute_update(
                "UPDATE test_cases SET %s, update_time=NOW() WHERE id=%%s AND is_del=0"
                % ', '.join(fields),
                tuple(params),
            )
            applied.append(cid)
        # ⚠️ 诊断资格消费：主消费点已前移到 `diagnose_tp_impact` 诊断成功时；
        #    这里保留一次**幂等兜底**（AI 判定不可信时诊断没消费，应用成功即视为
        #    处理完成）。已消费时函数直接返回 False，不会重复写库。
        consumed = _consume_tp_pending(db, req.pfp_id, req.tp_index) if applied else False
        msg = '已应用到 %d 条用例' % len(applied)
        if skipped:
            msg += '；跳过 %d 条（不在该测试点关联范围内或无有效内容）' % len(skipped)
        return _envelope({'applied': applied, 'skipped': skipped,
                          'changed': False if consumed else None}, None, msg)


@router.post('/replace-tp-case')
async def replace_tp_case(req: ReplaceTpCaseReq, request: Request):
    """「替换为新用例」：旧用例归档，新草稿入库并继承原关联（tp_indexes 原样搬过去）。"""
    uid = _uid(request)
    if not uid:
        return _envelope(None, None, '未登录', 401)
    try:
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT id, process_id FROM product_function_points WHERE id=%s AND is_del=0",
                (req.pfp_id,),
            )
            if not row:
                return _envelope(None, None, '功能点不存在', 404)
            admin = _is_admin(db, uid)
            if not _process_visible(db, uid, admin, row['process_id']):
                return _envelope(None, None, '无权操作', 404)

            linked = _load_linked_cases(db, req.pfp_id, req.tp_index)
            old = next((c for c in linked if int(c['id']) == int(req.old_case_id)), None)
            if not old:
                return _envelope(None, None, '旧用例不在该测试点的关联范围内', 400)

            # ---- 收集要替换成的新用例草稿（支持一次替换多条）----
            draft_ids = []
            for _x in (req.draft_ids or []):
                try:
                    _xi = int(_x)
                except Exception:
                    continue
                if _xi > 0 and _xi not in draft_ids:
                    draft_ids.append(_xi)
            if req.draft_id:
                try:
                    _di = int(req.draft_id)
                    if _di > 0 and _di not in draft_ids:
                        draft_ids.append(_di)
                except Exception:
                    pass
            if not draft_ids:
                return _envelope(None, None, '未指定新用例草稿', 400)

            # ⚠️ 先把**所有**草稿一次性校验完（存在 + 未被采纳），再动任何数据。
            #    否则「第 1 条已入库、第 2 条才发现不可用」会留下半成品状态。
            drafts = []
            for _did in draft_ids:
                _d = db.execute_single_query(
                    "SELECT * FROM ai_case_drafts WHERE id=%s AND status=0", (_did,)
                )
                if not _d:
                    return _envelope(
                        None, None, '新用例草稿 #%d 不存在或已被采纳' % _did, 400)
                drafts.append(_d)

            project_id = req.project_id or old.get('project_id')
            if not project_id:
                return _envelope(None, None, '无法确定新用例的项目，请指定 project_id', 400)

            # ⚠️⚠️ 替换语义的关键（实测纠正）：
            #   实测库里有 **200 条用例同时被多个功能点关联**（同一 case_id 出现在多个 pfp 下），
            #   而一条关联行的 tp_indexes 也可能覆盖**同一功能点的多个测试点**。
            #   所以「替换」= 旧用例整体退场、新用例**整体继承**旧用例的**全部**关联行，
            #   而不是只搬当前这一个 (pfp, 测试点)。
            #   只搬当前这条的话：
            #     · 旧用例在**其它功能点**下的关联会变成僵尸（is_del=0 却指向已删用例）
            #     · 旧用例在本功能点**其它测试点**上的覆盖会凭空消失（覆盖率掉）
            #   所以这里搬全部关联行，并对每个 (pfp, case) 分别带上原 tp_indexes。
            old_links = db.execute_query(
                "SELECT id, pfp_id, case_id, tp_indexes FROM product_point_cases "
                "WHERE case_id=%s AND is_del=0", (req.old_case_id,)
            ) or []
            if not old_links:
                return _envelope(None, None, '旧用例没有生效中的关联记录', 400)
            # 当前这条关联的 tp_indexes 单独取（诊断命中的是它）
            old_tp_indexes = _parse_tp_indexes(old.get('tp_indexes')) or []

            new_case_ids = []
            try:
                # ⚠️ 显式事务：整个替换（插 N 条新用例 + 归档旧用例 + 迁移关联行 + 标记草稿）
                #    要么全成、要么全回滚。
                #    不能用 db.execute_update —— 它每条语句都自己 commit，
                #    中途失败会留下「旧用例已归档但新用例只入库一半」的破数据。
                db._ensure_connection()  # noqa: SLF001
                _conn = db.connection
                try:
                    with _conn.cursor() as _cur:
                        # ① 逐条插入新用例
                        for _d in drafts:
                            _cur.execute(
                                'INSERT INTO test_cases (project_id, module, function_point, priority, '
                                'pre_condition, steps, expected_result, remark, status, is_del, '
                                'creator_id, ai_generated, ai_draft_id) '
                                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,0,%s,1,%s)',
                                (project_id, _d.get('module') or old.get('module') or '',
                                 _d.get('function_point') or old.get('function_point') or '',
                                 _d.get('priority') or old.get('priority') or 2,
                                 _d.get('pre_condition') or '',
                                 _d.get('case_steps') or '',
                                 _d.get('expected_result') or '',
                                 _d.get('remark') or '',
                                 uid, _d['id']),
                            )
                            new_case_ids.append(int(_cur.lastrowid))
                        # ② 归档旧用例（逻辑删除）—— 多条新用例共用同一个旧用例，只归档一次
                        _cur.execute("UPDATE test_cases SET is_del=1 WHERE id=%s",
                                     (req.old_case_id,))
                        # ③ 迁移关联：旧行全部归档，新用例在**同样的每个 pfp** 下建立新关联行，
                        #    并原样继承各自的 tp_indexes。
                        for lk in old_links:
                            pfp_id_2 = int(lk['pfp_id'])
                            idx_json = _dump_tp_indexes(_parse_tp_indexes(lk.get('tp_indexes')))
                            # 归档旧关联行：uk_pfp_case(is_del, pfp_id, case_id) 含 is_del，
                            # 同一 (pfp,case) 不能同时存在两行 is_del=1 的墓碑 →
                            # 若已有墓碑行则先物理删除那条墓碑，再把活跃行改成墓碑。
                            _cur.execute(
                                "SELECT id FROM product_point_cases WHERE pfp_id=%s AND case_id=%s "
                                "AND is_del=1", (pfp_id_2, req.old_case_id))
                            for _t in _cur.fetchall():
                                _cur.execute("DELETE FROM product_point_cases WHERE id=%s", (_t['id'],))
                            _cur.execute(
                                "UPDATE product_point_cases SET is_del=1, update_time=NOW() WHERE id=%s",
                                (lk['id'],),
                            )
                            # 每条新用例都在这个 pfp 下建自己的关联行
                            # ⚠️ 新用例是新 id，不会有墓碑行，直接 INSERT 安全
                            for _nc in new_case_ids:
                                _cur.execute(
                                    "INSERT INTO product_point_cases (pfp_id, case_id, tp_indexes, creator_id) "
                                    "VALUES (%s,%s,%s,%s)",
                                    (pfp_id_2, _nc, idx_json, uid),
                                )
                        # ④ 草稿标记为已采纳（每条草稿各自指向自己生成的新用例）
                        for _d, _nc in zip(drafts, new_case_ids):
                            _cur.execute(
                                "UPDATE ai_case_drafts SET status=1, adopt_case_id=%s, adopt_project_id=%s, "
                                "adopt_by=%s, adopt_time=NOW() WHERE id=%s",
                                (_nc, project_id, uid, _d['id']),
                            )
                    _conn.commit()
                except Exception:
                    _conn.rollback()
                    raise
            finally:
                # 孤儿关联行清理：归档旧用例后，指它的活跃关联行若没同步归档就成了孤儿。
                # 必须内联在 finally 里 —— 任何异常路径都要清。
                # ⚠️ 口径严格按铁律 #21：只清「用例物理不存在（tc.id IS NULL）」的行。
                #    **不包含 tc.is_del=1（用例软删除）** —— 那种是历史遗留的僵尸行，
                #    清它会越出「本次替换」的影响面、连带改动别的功能点的关联数据。
                try:
                    db.execute_update(
                        "DELETE ppc FROM product_point_cases ppc "
                        "LEFT JOIN test_cases tc ON tc.id=ppc.case_id "
                        "WHERE ppc.is_del=0 AND tc.id IS NULL"
                    )
                except Exception as ce:  # noqa: BLE001
                    logger.warning('孤儿关联行清理失败: %s', ce)

            _n = len(new_case_ids)
            # ⚠️ 诊断资格消费：主消费点已前移到 `diagnose_tp_impact`（诊断成功即消费）。
            #    这里保留幂等兜底；放在事务 commit 之后、返回之前，
            #    失败只记日志，不把成功的替换判成失败。
            consumed = _consume_tp_pending(db, req.pfp_id, req.tp_index)
            # ⚠️ 把新用例的**完整详情**一并返回（正文 + 关联行 + tp_indexes）。
            #    这样前端在替换成功后可以**就地更新弹窗状态**，
            #    不需要重新调用 diagnose-tp-impact（那会再跑一次 AI、又生成一批草稿）。
            new_case_details = []
            if new_case_ids:
                # ⚠️ 这里用**字符串拼接**构造 IN 列表，不能用 `%` 格式化 ——
                #    `%s` 会被 Python 的 % 运算符一起吃掉（"not enough arguments for format string"）。
                _ph = ','.join(['%s'] * len(new_case_ids))
                _q = (
                    "SELECT tc.id, tc.project_id, tcp.cate_name AS project, tc.module, "
                    "tc.function_point, tc.priority, tc.steps, tc.expected_result, "
                    "tc.pre_condition, tc.remark, pc.id AS rel_id, pc.tp_indexes "
                    "FROM product_point_cases pc "
                    "JOIN test_cases tc ON tc.id = pc.case_id "
                    "LEFT JOIN test_case_project tcp ON tcp.id = tc.project_id "
                    "WHERE pc.pfp_id=%s AND pc.case_id IN (" + _ph + ") AND pc.is_del=0"
                )
                _rows = db.execute_query(
                    _q,
                    tuple([int(req.pfp_id)] + [int(x) for x in new_case_ids]),
                ) or []
                # 一个新用例可能在多个 pfp 下有关联行，这里只取本 pfp 的那条
                _by_case = {}
                for _r in _rows:
                    _cid = int(_r['id'])
                    _by_case.setdefault(_cid, _r)
                for _cid in new_case_ids:
                    _r = _by_case.get(int(_cid))
                    if not _r:
                        continue
                    new_case_details.append({
                        'case_id': int(_cid),
                        'rel_id': _r.get('rel_id'),
                        'project_id': _r.get('project_id'),
                        'project': _r.get('project') or '',
                        'module': _r.get('module') or '',
                        'function_point': _r.get('function_point') or '',
                        'priority': _r.get('priority'),
                        'pre_condition': _r.get('pre_condition') or '',
                        'steps': _r.get('steps') or '',
                        'expected_result': _r.get('expected_result') or '',
                        'remark': _r.get('remark') or '',
                        'tp_indexes': _parse_tp_indexes(_r.get('tp_indexes')) or [],
                    })
            return _envelope(
                {'old_case_id': req.old_case_id, 'new_case_id': new_case_ids[0] if new_case_ids else 0,
                 'new_case_ids': new_case_ids,
                 'new_cases': new_case_details,
                 'tp_indexes': old_tp_indexes, 'project_id': project_id,
                 'inherited_links': len(old_links), 'replaced_count': _n,
                 # 前端据此把当前行 changed 即时置 false（按钮立刻熄灭，不等列表刷新）
                 'changed': False if consumed else None},
                None, '已替换：旧用例 #%d 已归档，新用例 %s 已继承旧用例的 %d 处关联'
                      % (req.old_case_id,
                         '、'.join('#%d' % i for i in new_case_ids),
                         len(old_links)))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception('replace_tp_case 异常')
        return _envelope(None, None, '替换失败：%s' % e, 500)
