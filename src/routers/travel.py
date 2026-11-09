# -*- coding: utf-8 -*-
"""智能出行接口：POI 聚合 + LLM 行程编排（grounding）+ 行程保存/回看。

与前端对应关系：
    src/api/travel.js  ->  GET  /api/travel/pois               POI 推荐（景点/美食/酒店，真实腾讯地图数据）
                           POST /api/travel/plan               AI 行程规划（POI 白名单 grounding + 花费汇总）
                           POST /api/travel/plans/save         保存行程
                           GET  /api/travel/plans/list         我的行程列表（仅本人）
                           GET  /api/travel/plans/detail       行程明细（仅本人）
                           POST /api/travel/plans/delete       删除行程（软删，仅本人）

架构（对应方案文档 smart-travel-plan.md 第六节）：
    1. POI 聚合层：并行意图上串行调腾讯地图 WebService（带 10 分钟内存缓存省配额），
       全部真实坐标/地址 → 这就是防幻觉白名单；
    2. LLM 编排层：复用模型中心模型链（ai_runner.run_structured），
       硬约束 = schedule 每项 poi_name 必须逐字来自候选列表；
    3. 校验层：生成后按名称回查白名单，回查不到即剔除（dropped_grounding 透传前端）；
    4. 花费汇总：LLM 给每项参考价（页面统一标注「参考价」），服务端按类汇总。

响应信封与项目其余路由一致：{ code, msg, data }。路由注册前缀 /api（见 src/main.py）。
"""
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from src.ai import models as model_reg
from src.ai import runner as ai_runner
from src.ai.errors import AIConfigError, AIError
from src.comment import MySqlconection, yaml_data
from .auth import parse_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/travel',
    tags=['travel'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

# 腾讯地图 WebService key：与前端 GL JS 同 key（控制台已开通 WebServiceAPI 权限）。
# 优先读环境变量 TENCENT_MAP_KEY，便于更换/升配时不改代码。
TMAP_KEY = os.getenv('TENCENT_MAP_KEY', 'EYXBZ-ZA3LL-ERLPN-EWB7T-2YNQ2-B3FYT')

# POI 内存缓存：同城市同类目 10 分钟内直接命中，省每日配额（免费 key 配额有限，实测会打满）
_POI_CACHE: Dict[tuple, tuple] = {}
_CACHE_TTL = 600

# POI 类目 -> 检索关键词（腾讯地图 v1/search 按关键词检索）
POI_TYPE_KEYWORD = {
    'scenic': '景点',
    'food': '美食',
    'hotel': '酒店',
}


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    return db


def _current_uid(request: Request) -> int:
    """从 accessToken 头解析当前登录用户 id（与 defects.py 同思路）"""
    token = request.headers.get('accessToken')
    if token:
        try:
            uid = parse_token(token)
            if uid:
                return int(uid)
        except Exception:  # noqa: BLE001
            pass
    raise HTTPException(status_code=401, detail='未登录或登录已过期')


def _fmt_time(value: Any) -> Optional[str]:
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


# ---------------------------------------------------------------------------
# 一、腾讯地图 WebService 客户端（stdlib 实现，零新增依赖）
# ---------------------------------------------------------------------------
def _tmap_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(params)
    params['key'] = TMAP_KEY
    url = 'https://apis.map.qq.com' + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'test-admin/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'腾讯地图接口网络异常: {e}')
    if data.get('status') != 0:
        # 121 = 当日配额用尽（免费 key 实测会打满），单独给出可理解的提示
        if data.get('status') == 121:
            raise HTTPException(
                status_code=502,
                detail='腾讯地图接口今日调用量已达上限，请明天再试，或在腾讯位置服务控制台提升配额/更换 key')
        raise HTTPException(
            status_code=502,
            detail=f'腾讯地图接口异常({data.get("status")}): {data.get("message")}')
    return data


def _search_pois(city: str, poi_type: str, page_size: int = 10) -> List[Dict[str, Any]]:
    """按类目检索真实 POI。命中内存缓存直接返回（省配额）。"""
    keyword = POI_TYPE_KEYWORD.get(poi_type)
    if not keyword:
        raise HTTPException(status_code=422, detail=f'不支持的地点类型: {poi_type}')
    cache_key = (city, poi_type, page_size)
    hit = _POI_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    data = _tmap_get('/ws/place/v1/search', {
        'keyword': keyword,
        'boundary': f'region({city},0)',
        'page_size': max(1, min(int(page_size), 20)),
        'page_index': 1,
    })
    rows: List[Dict[str, Any]] = []
    for it in (data.get('data') or []):
        loc = it.get('location') or {}
        rows.append({
            'id': it.get('id'),
            'name': (it.get('title') or '').strip(),
            'address': (it.get('address') or '').strip(),
            'category': (it.get('category') or '').strip(),
            'lat': loc.get('lat'),
            'lng': loc.get('lng'),
        })
    rows = [r for r in rows if r['name']]
    _POI_CACHE[cache_key] = (time.time(), rows)
    return rows


def _norm_name(name: str) -> str:
    """地点名归一化：去空白与常见后缀差异，用于回查白名单"""
    return ''.join((name or '').split())


def _match_poi(poi_name: str, whitelist: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把 LLM 输出的地点名回查到白名单：全等 / 互相包含都算命中，其余视为幻觉"""
    key = _norm_name(poi_name)
    if not key:
        return None
    if key in whitelist:
        return whitelist[key]
    for wkey, poi in whitelist.items():
        if key in wkey or wkey in key:
            return poi
    return None


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    """两点球面直线距离（km）。注意是直线距离，实际路程会更长"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _transit_advice(distance_km: float) -> Dict[str, Any]:
    """按距离给交通建议（启发式，纯直线距离推导，不调路线规划接口省配额）：
    ≤1.2km 步行 / ≤3.5km 骑行或公交 / 更远打车"""
    if distance_km <= 1.2:
        minutes = max(5, round(distance_km * 12))  # 步行约 12 分钟/km
        return {'mode': '步行', 'minutes': minutes, 'desc': f'步行约 {minutes} 分钟'}
    if distance_km <= 3.5:
        minutes = max(6, round(distance_km * 5 + 3))  # 骑行约 5 分钟/km + 取车
        return {'mode': '骑行/公交', 'minutes': minutes, 'desc': f'骑行或公交约 {minutes} 分钟'}
    minutes = max(10, round(distance_km * 2.5 + 8))  # 市区均速 24km/h + 上下车
    return {'mode': '打车', 'minutes': minutes, 'desc': f'打车约 {minutes} 分钟'}


# ---------------------------------------------------------------------------
# 二、LLM 行程编排（结构化输出 + grounding 校验）
# ---------------------------------------------------------------------------
class ScheduleItem(BaseModel):
    """LLM 编排出的一个时段安排"""

    day: int = 1
    seq: int = 0
    poi_name: str = ''
    poi_type: str = ''
    time_slot: str = ''
    activity: str = ''
    reason: str = ''
    cost: float = 0.0
    stay_minutes: int = 60

    @field_validator('day', mode='before')
    @classmethod
    def _day(cls, v):
        try:
            return max(1, int(v))
        except Exception:
            return 1

    @field_validator('seq', mode='before')
    @classmethod
    def _seq(cls, v):
        try:
            return max(0, int(v))
        except Exception:
            return 0

    @field_validator('stay_minutes', mode='before')
    @classmethod
    def _stay(cls, v):
        """建议游览时长（分钟），钳制到 15~480，模型不给就默认 60"""
        try:
            return min(480, max(15, int(float(v))))
        except Exception:
            return 60

    @field_validator('cost', mode='before')
    @classmethod
    def _cost(cls, v):
        try:
            return max(0.0, float(v))
        except Exception:
            return 0.0

    @field_validator('poi_name', mode='before')
    @classmethod
    def _require_name(cls, v):
        """空名称直接校验失败 → validate_items 丢弃该条并带原因"""
        s = str(v or '').strip()
        if not s:
            raise ValueError('缺少地点名称')
        return s

    @field_validator('poi_type', 'time_slot', 'activity', 'reason', mode='before')
    @classmethod
    def _s(cls, v):
        if v is None:
            return ''
        if isinstance(v, list):
            return '；'.join(str(x) for x in v if x is not None)
        return str(v).strip()


_PLAN_PROMPT: Dict[str, Any] = {
    'name': '智能出行-行程编排',
    'version': 1,
    'system': (
        '你是一名资深旅行规划师，擅长按天编排「景点 + 美食 + 酒店」的行程。\n'
        '必须遵守：\n'
        '1. 只输出一个 JSON 对象，不要输出任何解释文字，不要用 markdown 代码块包裹。\n'
        '2. 【防幻觉铁律】schedule 里每一项的 poi_name 必须逐字取自输入的「候选地点列表」，'
        '禁止编造、改写、增删字；列表里没有合适地点时宁缺毋滥，直接少排。\n'
        '3. 每天安排 3~5 个地点，按「上午 → 中午 → 下午 → 晚上」推进：景点与餐厅穿插，'
        '地理位置相近的地点排在同一天，避免来回折返；每天最后一项安排 poi_type=hotel 的入住。\n'
        '4. poi_type 只能是 scenic（景点）/ food（美食）/ hotel（酒店）之一。\n'
        '5. time_slot 只能是「上午」「中午」「下午」「晚上」之一。\n'
        '6. cost 为该项预估花费（元，数字）：景点填门票参考价（免费填 0），餐厅填人均，酒店填每晚房价。\n'
        '7. stay_minutes 为该地点建议游览/停留时长（分钟，数字）：大型景区 120~180，普通景点/展馆 60~120，'
        '餐厅 60~90，酒店填 30（办理入住）。\n'
        '8. activity 一句话说明这段时间做什么；reason 一句话说明推荐理由（结合用户偏好）。\n'
        '9. 结合偏好标签与预算档位选点：经济 → 人均低的餐厅与经济型住宿；高端 → 反之。'
        '预算约束只影响选点与 cost 取值，不需要输出预算分析文字。'
    ),
    'user': (
        '【目的地】{{city}}\n'
        '【天数】{{days}} 天\n'
        '【偏好标签】{{preferences}}\n'
        '【预算档位】{{budget}}\n\n'
        '【候选地点列表（poi_name 只能从这里逐字选）】\n{{poi_list}}\n\n'
        '请按下面结构输出 JSON：\n{{schema_text}}'
    ),
    'output_schema': {
        'type': 'object',
        'description': '行程编排结果',
        'properties': {
            'schedule': {
                'type': 'array',
                'description': '按时段推进的编排条目列表',
                'items': {
                    'type': 'object',
                    'properties': {
                        'day': {'type': 'integer', 'description': '第几天，从 1 开始'},
                        'seq': {'type': 'integer', 'description': '当天内的顺序，从 1 开始'},
                        'poi_name': {'type': 'string', 'description': '地点名，逐字取自候选列表'},
                        'poi_type': {'type': 'string', 'description': 'scenic/food/hotel'},
                        'time_slot': {'type': 'string', 'description': '上午/中午/下午/晚上'},
                        'activity': {'type': 'string', 'description': '这段时间的安排说明'},
                        'reason': {'type': 'string', 'description': '推荐理由'},
                        'cost': {'type': 'number', 'description': '预估花费（元）'},
                        'stay_minutes': {'type': 'integer', 'description': '建议游览/停留时长（分钟）'},
                    },
                    'required': ['day', 'poi_name', 'poi_type', 'time_slot'],
                },
            },
        },
        'required': ['schedule'],
    },
}


def _build_poi_list_text(groups: Dict[str, List[Dict[str, Any]]]) -> str:
    """把三类候选 POI 压成紧凑文本（含坐标，供 LLM 判断地理相近）"""
    type_label = {'scenic': '景点', 'food': '美食', 'hotel': '酒店'}
    lines: List[str] = []
    for poi_type, rows in groups.items():
        lines.append(f'【{type_label.get(poi_type, poi_type)}】')
        for r in rows:
            lines.append('- %s | %s | %s | 坐标(%s,%s)' % (
                r['name'], r.get('category') or '-', r.get('address') or '-',
                r.get('lng'), r.get('lat')))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 三、接口
# ---------------------------------------------------------------------------
@router.get('/pois', summary='POI 推荐（景点/美食/酒店，真实腾讯地图数据）')
def travel_pois(
    city: str = Query(..., description='目的地城市，如：杭州'),
    type: str = Query('scenic', description='scenic/food/hotel'),
    size: int = Query(10, ge=1, le=20),
):
    city = (city or '').strip()
    if not city:
        raise HTTPException(status_code=422, detail='目的地城市不能为空')
    rows = _search_pois(city, type, size)
    return {'code': 200, 'msg': 'success', 'data': rows}


class PlanReq(BaseModel):
    """AI 行程规划入参"""

    city: str
    days: int = 1
    preferences: List[str] = []
    budget_level: str = '舒适'
    model_id: Optional[int] = None

    @field_validator('days', mode='before')
    @classmethod
    def _days(cls, v):
        try:
            return min(5, max(1, int(v)))
        except Exception:
            return 1


@router.post('/plan', summary='AI 行程规划（POI 白名单 grounding + 花费汇总）')
def travel_plan(body: PlanReq, request: Request):
    uid = _current_uid(request)
    city = (body.city or '').strip()
    if not city:
        raise HTTPException(status_code=422, detail='目的地城市不能为空')

    # 1) POI 聚合：景点 12 / 美食 12 / 酒店 6（三次检索，命中缓存只算一次配额）
    try:
        groups = {
            'scenic': _search_pois(city, 'scenic', 12),
            'food': _search_pois(city, 'food', 12),
            'hotel': _search_pois(city, 'hotel', 6),
        }
    except HTTPException:
        raise
    empty_types = [t for t, rows in groups.items() if not rows]
    if len(empty_types) == 3:
        raise HTTPException(status_code=502, detail=f'未检索到「{city}」的任何地点，请确认城市名')

    # 2) 白名单（防幻觉的唯一合法来源）
    whitelist: Dict[str, Dict[str, Any]] = {}
    for rows in groups.values():
        for r in rows:
            whitelist[_norm_name(r['name'])] = r

    # 3) LLM 编排（复用模型中心模型链：配额/脱敏/日志/备用模型全继承）
    try:
        with get_db_handler() as db:
            result = ai_runner.run_structured(
                db, uid, 'travel_plan', 'travel_plan',
                prompt_vars={
                    'city': city,
                    'days': body.days,
                    'preferences': '、'.join(body.preferences) if body.preferences else '不限',
                    'budget': body.budget_level or '舒适',
                    'poi_list': _build_poi_list_text(groups),
                },
                list_key='schedule',
                item_model=ScheduleItem,
                model_id=body.model_id,
                prompt_override=_PLAN_PROMPT,
            )
    except AIConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # 4) grounding 校验：逐条回查白名单，回查不到即剔除（幻觉零容忍）
    days: Dict[int, List[Dict[str, Any]]] = {}
    dropped_grounding: List[Dict[str, Any]] = []
    for item in result.get('items', []):
        poi = _match_poi(item.get('poi_name', ''), whitelist)
        if poi is None:
            dropped_grounding.append({'poi_name': item.get('poi_name'), 'reason': '未能在地图检索结果中回查到该地点'})
            continue
        day = int(item.get('day') or 1)
        days.setdefault(day, []).append({
            'poi_name': poi['name'],
            'seq': int(item.get('seq') or 0),  # 当天内顺序：距离链按此排序，必须带出
            'poi_type': item.get('poi_type') or 'scenic',
            'time_slot': item.get('time_slot') or '',
            'activity': item.get('activity') or '',
            'reason': item.get('reason') or '',
            'cost': float(item.get('cost') or 0),
            'stay_minutes': int(item.get('stay_minutes') or 60),
            'address': poi.get('address') or '',
            'category': poi.get('category') or '',
            'lat': poi.get('lat'),
            'lng': poi.get('lng'),
        })

    if not days:
        raise HTTPException(
            status_code=502,
            detail='AI 编排的地点均未能在地图中回查到（已全部拦截），请重试一次；'
                   f'模型原始输出已在调用日志中留痕。丢弃明细：{dropped_grounding[:3]}')

    # 5) 花费汇总（参考价，前端统一标注）
    day_list = [
        {'day': d, 'items': sorted(items, key=lambda x: x['seq'] if x.get('seq') else 0)}
        for d, items in sorted(days.items())
    ]
    # 距离与交通：相邻地点用真实坐标算直线距离（防 AI 编造），交通按距离启发式推导
    for d in day_list:
        prev = None
        for it in d['items']:
            if prev is not None and it.get('lat') is not None and prev.get('lat') is not None:
                dist = round(_haversine_km(prev['lat'], prev['lng'], it['lat'], it['lng']), 1)
                it['distance_km'] = dist
                it['transit'] = _transit_advice(dist)
            else:
                it['distance_km'] = None  # 每天第一个地点：从前一晚酒店/出发地开始，不给距离
                it['transit'] = None
            prev = it
    total_cost = round(sum(it['cost'] for d in day_list for it in d['items']), 2)
    cost_by_type: Dict[str, float] = {}
    for d in day_list:
        for it in d['items']:
            cost_by_type[it['poi_type']] = round(cost_by_type.get(it['poi_type'], 0) + it['cost'], 2)
    poi_count = sum(len(d['items']) for d in day_list)

    model = result.get('model') or {}
    return {
        'code': 200,
        'msg': 'success',
        'data': {
            'city': city,
            'days': body.days,
            'preferences': body.preferences,
            'budget_level': body.budget_level,
            'itinerary': day_list,
            'total_cost': total_cost,
            'cost_by_type': cost_by_type,
            'poi_count': poi_count,
            'dropped_grounding': dropped_grounding,
            'model': {'id': model.get('id'), 'name': model.get('name')},
            'elapsed_ms': result.get('elapsed_ms'),
        },
    }


class PlanSaveReq(BaseModel):
    """保存行程（前端把 /plan 的 data 原样回传）"""

    plan_name: Optional[str] = None
    destination: str
    days: int = 1
    preferences: List[str] = []
    budget_level: str = ''
    itinerary: Any = None
    total_cost: Optional[float] = None
    poi_count: Optional[int] = None

    @field_validator('destination', mode='before')
    @classmethod
    def _require_city(cls, v):
        s = str(v or '').strip()
        if not s:
            raise ValueError('目的地不能为空')
        return s


@router.post('/plans/save', summary='保存行程到「我的行程」')
def travel_plan_save(body: PlanSaveReq, request: Request):
    uid = _current_uid(request)
    if not body.itinerary:
        raise HTTPException(status_code=422, detail='行程内容为空，无法保存')
    plan_name = (body.plan_name or '').strip() or f'{body.destination}{body.days}日游'
    try:
        with get_db_handler() as db:
            # 同一用户同名行程去重：已存在则覆盖更新（保存按钮可反复点）
            existed = db.execute_single_query(
                'SELECT id FROM travel_plans WHERE create_by = %s AND plan_name = %s AND is_del = 0',
                (uid, plan_name),
            )
            itinerary_json = json.dumps(body.itinerary, ensure_ascii=False)
            if existed:
                db.execute_update(
                    'UPDATE travel_plans SET destination=%s, days=%s, preference=%s, budget_level=%s, '
                    'itinerary=%s, total_cost=%s, poi_count=%s WHERE id=%s',
                    (body.destination, body.days,
                     json.dumps(body.preferences, ensure_ascii=False), body.budget_level or '',
                     itinerary_json, body.total_cost, body.poi_count, existed['id']),
                )
                plan_id = existed['id']
                msg = '行程已更新'
            else:
                db.execute_update(
                    'INSERT INTO travel_plans '
                    '(plan_name, destination, days, preference, budget_level, itinerary, total_cost, poi_count, create_by) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (plan_name, body.destination, body.days,
                     json.dumps(body.preferences, ensure_ascii=False), body.budget_level or '',
                     itinerary_json, body.total_cost, body.poi_count, uid),
                )
                row = db.execute_single_query(
                    'SELECT id FROM travel_plans WHERE create_by=%s AND plan_name=%s AND is_del=0',
                    (uid, plan_name),
                )
                plan_id = row['id'] if row else None
                msg = '保存成功'
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f'保存行程失败: {e}')
        raise HTTPException(status_code=500, detail=f'保存行程失败: {e}')
    return {'code': 200, 'msg': msg, 'data': {'id': plan_id}}


@router.get('/plans/list', summary='我的行程列表（仅本人）')
def travel_plan_list(
    request: Request,
    pageNo: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
):
    uid = _current_uid(request)
    try:
        with get_db_handler() as db:
            total_row = db.execute_single_query(
                'SELECT COUNT(*) AS total FROM travel_plans WHERE create_by = %s AND is_del = 0',
                (uid,),
            )
            total = total_row['total'] if total_row else 0
            rows = db.execute_query(
                'SELECT id, plan_name, destination, days, preference, budget_level, '
                'total_cost, poi_count, create_time '
                'FROM travel_plans WHERE create_by = %s AND is_del = 0 '
                'ORDER BY id DESC LIMIT %s OFFSET %s',
                (uid, pageSize, (pageNo - 1) * pageSize),
            )
            for r in rows or []:
                r['create_time'] = _fmt_time(r.get('create_time'))
                # preference 是 JSON 字符串，前端用前解开
                if isinstance(r.get('preference'), str):
                    try:
                        r['preference'] = json.loads(r['preference'])
                    except Exception:  # noqa: BLE001
                        r['preference'] = []
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f'行程列表查询失败: {e}')
        raise HTTPException(status_code=500, detail=f'行程列表查询失败: {e}')
    return {
        'code': 200, 'msg': 'success', 'data': rows or [],
        'totalCount': total, 'total': total,
    }


@router.get('/plans/detail', summary='行程明细（仅本人）')
def travel_plan_detail(request: Request, id: int = Query(..., ge=1)):
    uid = _current_uid(request)
    try:
        with get_db_handler() as db:
            row = db.execute_single_query(
                'SELECT * FROM travel_plans WHERE id = %s AND create_by = %s AND is_del = 0',
                (id, uid),
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f'行程明细查询失败: {e}')
        raise HTTPException(status_code=500, detail=f'行程明细查询失败: {e}')
    if not row:
        raise HTTPException(status_code=404, detail='行程不存在或无权查看')
    row['create_time'] = _fmt_time(row.get('create_time'))
    for field in ('preference', 'itinerary'):
        if isinstance(row.get(field), str):
            try:
                row[field] = json.loads(row[field])
            except Exception:  # noqa: BLE001
                row[field] = None if field == 'preference' else []
    return {'code': 200, 'msg': 'success', 'data': row}


class PlanDeleteReq(BaseModel):
    ids: List[int]


@router.post('/plans/delete', summary='删除行程（软删，仅本人）')
def travel_plan_delete(body: PlanDeleteReq, request: Request):
    uid = _current_uid(request)
    if not body.ids:
        raise HTTPException(status_code=422, detail='未选择要删除的行程')
    try:
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(body.ids))
            deleted = db.execute_update(
                f'UPDATE travel_plans SET is_del = 1 '
                f'WHERE id IN ({placeholders}) AND create_by = %s',
                tuple(body.ids) + (uid,),
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f'删除行程失败: {e}')
        raise HTTPException(status_code=500, detail=f'删除行程失败: {e}')
    return {'code': 200, 'msg': f'已删除 {deleted} 条行程', 'data': None}


# ---------------------------------------------------------------------------
# 四、车次余票 / 票价查询（当前为示例数据，待接入真实 12306 数据源）
# ---------------------------------------------------------------------------
# 说明（2026-09-29）：直连 12306 余票接口（leftTicket/queryG）在本环境被风控拦截
# （缺 RAIL_DEVICEID 设备指纹 + 云服务器 IP 段被拦），前端因 CORS 亦无法直连。
# 故先返回结构化示例数据，把菜单与页面框架搭好；未来只需替换下方 _mock_tickets
# 的取数层（接真实 12306 / 第三方 API），接口契约与前端展示均不变。
@router.get('/tickets', summary='车次余票/票价查询（当前示例数据，待接真实12306数据源）')
def travel_tickets(
    from_station: str = Query(..., description='出发地城市或车站'),
    to_station: str = Query(..., description='目的地城市或车站'),
    date: str = Query('', description='乘车日期 yyyy-MM-dd，默认今天'),
):
    frm = (from_station or '').strip()
    to = (to_station or '').strip()
    if not frm or not to:
        raise HTTPException(status_code=422, detail='出发地/目的地不能为空')
    if not date:
        date = time.strftime('%Y-%m-%d')
    return {'code': 200, 'msg': 'success', 'data': _mock_tickets(frm, to, date)}


def _mock_tickets(frm: str, to: str, date: str) -> Dict[str, Any]:
    """示例车次数据（非真实 12306）。结构对齐未来真实接口，便于后续仅替换取数层。"""
    trains = [
        {
            'train_no': 'G1', 'train_type': '高铁',
            'from_station': f'{frm}南', 'to_station': f'{to}虹桥',
            'depart_time': '06:00', 'arrive_time': '12:00', 'duration': '6小时0分',
            'seats': [
                {'name': '商务座', 'price': 1748, 'tickets': '有票'},
                {'name': '一等座', 'price': 933, 'tickets': '有票'},
                {'name': '二等座', 'price': 553, 'tickets': '有票'},
            ],
        },
        {
            'train_no': 'G103', 'train_type': '高铁',
            'from_station': f'{frm}南', 'to_station': f'{to}虹桥',
            'depart_time': '07:00', 'arrive_time': '13:12', 'duration': '6小时12分',
            'seats': [
                {'name': '商务座', 'price': 1748, 'tickets': '候补'},
                {'name': '一等座', 'price': 933, 'tickets': '有票'},
                {'name': '二等座', 'price': 553, 'tickets': '无票'},
            ],
        },
        {
            'train_no': 'G7', 'train_type': '高铁',
            'from_station': f'{frm}南', 'to_station': f'{to}虹桥',
            'depart_time': '08:00', 'arrive_time': '13:18', 'duration': '5小时18分',
            'seats': [
                {'name': '商务座', 'price': 1748, 'tickets': '有票'},
                {'name': '一等座', 'price': 933, 'tickets': '有票'},
                {'name': '二等座', 'price': 553, 'tickets': '有票'},
            ],
        },
        {
            'train_no': 'D321', 'train_type': '动车',
            'from_station': frm, 'to_station': to,
            'depart_time': '09:30', 'arrive_time': '17:45', 'duration': '8小时15分',
            'seats': [
                {'name': '一等座', 'price': 628, 'tickets': '有票'},
                {'name': '二等座', 'price': 398, 'tickets': '有票'},
            ],
        },
    ]
    return {
        'from_station': frm,
        'to_station': to,
        'date': date,
        'trains': trains,
        'note': '示例数据，待接入真实12306数据源（直连被风控 / 第三方API需key）',
    }
