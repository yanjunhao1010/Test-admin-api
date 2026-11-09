"""首页工作台聚合接口。

一次请求返回首页全部区块数据：顶部 8 张统计卡（含「较昨日」）、自动化执行趋势
（按触发方式切 tab）、右侧「工作待办」、右下「数据统计」。

口径说明：
- 所有查询走同步 pymysql；本文件路由必须写同步 `def`，写成 `async def` 会占住事件
  循环把整个后端冻死（`ai_workbench` 已踩过这个坑）。
- 口径与 `automation.py` 的看板页保持一致（接口用例 / 场景用例 / days 取值）。
- 「工作待办」4 张卡一律按**当前登录用户**过滤，且**不做 admin 豁免**（超管同样只看自己名下的
  数据）——这是产品决策，与首页其它区块的全局口径不同。字段：任务 `task_performer_id`、
  缺陷同源任务的 `task_performer_id`、AI 草稿 `ai_case_drafts.run_by`、新增用例
  `test_cases.creator_id`（均为 uid，不是用户名）。
- 「较昨日」= 昨天（D-1）当日新增量，不是「今天比昨天多几条」。
"""
import json
import logging
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from src.comment import MySqlconection, yaml_data
from .auth import parse_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/home',
    tags=['home'])

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


# 接口用例 = 未配 dataset 的接口（与 automation.dashboard 同一常量口径）
_NO_DATASET = "(dataset IS NULL OR TRIM(dataset) IN ('', '[]', 'null'))"
_YESTERDAY = "DATE_SUB(CURDATE(), INTERVAL 1 DAY)"


def _int(v) -> int:
    """COUNT/SUM 结果兜底转 int（SUM 空集返回 NULL，Decimal 需转 int）"""
    return int(v or 0)


def _safe_div(a: int, b: int):
    """安全除法：分母为 0 返回 None（前端显示 —，不显示 0 误导）"""
    return round(a / b * 100, 2) if b else None


@router.get('/dashboard')
def home_dashboard(request: Request, days: int = 7):
    """首页工作台聚合数据。

    days 控制执行趋势的回溯天数（仅接受 7/14/30，其它值回落到 7）。
    """
    try:
        if days not in (7, 14, 30):
            days = 7
        start_day = date.today() - timedelta(days=days - 1)
        window_start = f'{start_day} 00:00:00'
        # 上一个同长度周期的起止（趋势图「较上期」对比用）
        prev_start_day = start_day - timedelta(days=days)
        prev_start = f'{prev_start_day} 00:00:00'

        with get_db_handler() as db:
            # ------------------------------------------------------------------
            # 当前登录用户：工作待办 4 张卡一律按当前登录用户过滤，不做 admin 豁免
            # （超管同样只看自己名下的数据）。解析不到登录态时用 -1 兜底 → 4 张卡
            # 全为 0，避免把全局数据当成「我的」展示。
            # ------------------------------------------------------------------
            uid = None
            token = request.headers.get('accessToken')
            if token:
                try:
                    uid = parse_token(token)
                except Exception as e:
                    logger.warning(f"解析当前 token 失败: {e}")
            performer_id = uid if uid else -1

            # ------------------------------------------------------------------
            # 顶部 8 卡（5 张真实 + 3 张灰态）
            # ------------------------------------------------------------------
            project_total = _int(db.execute_query(
                'SELECT COUNT(*) c FROM test_case_project WHERE is_del=0 AND level=3'
            )[0]['c'])
            project_new = _int(db.execute_query(
                f'SELECT COUNT(*) c FROM test_case_project '
                f'WHERE is_del=0 AND level=3 AND DATE(create_time)={_YESTERDAY}'
            )[0]['c'])

            case_total = _int(db.execute_query(
                'SELECT COUNT(*) c FROM test_cases WHERE is_del=0'
            )[0]['c'])
            case_new = _int(db.execute_query(
                f'SELECT COUNT(*) c FROM test_cases '
                f'WHERE is_del=0 AND DATE(create_time)={_YESTERDAY}'
            )[0]['c'])

            group_total = _int(db.execute_query(
                "SELECT COUNT(DISTINCT IFNULL(NULLIF(TRIM(`group`),''),'未分组')) c "
                'FROM api_interfaces'
            )[0]['c'])
            # group 是 api_interfaces 的字符串字段（非实体表，没有自己的 create_time），
            # 「较昨日」只能定义为：昨天新建的接口里，该分组在昨天之前从未出现过的数量。
            group_new = _int(db.execute_query(
                f"""
                SELECT COUNT(DISTINCT IFNULL(NULLIF(TRIM(i.`group`),''),'未分组')) c
                FROM api_interfaces i
                WHERE DATE(i.create_time)={_YESTERDAY}
                  AND NOT EXISTS (
                    SELECT 1 FROM api_interfaces j
                    WHERE IFNULL(NULLIF(TRIM(j.`group`),''),'未分组')
                          = IFNULL(NULLIF(TRIM(i.`group`),''),'未分组')
                      AND j.create_time < {_YESTERDAY}
                  )
                """
            )[0]['c'])

            api_total = _int(db.execute_query(
                'SELECT COUNT(*) c FROM api_interfaces'
            )[0]['c'])
            api_new = _int(db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE DATE(create_time)={_YESTERDAY}'
            )[0]['c'])

            api_case_total = _int(db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces WHERE {_NO_DATASET}'
            )[0]['c'])
            api_case_new = _int(db.execute_query(
                f'SELECT COUNT(*) c FROM api_interfaces '
                f'WHERE {_NO_DATASET} AND DATE(create_time)={_YESTERDAY}'
            )[0]['c'])

            cards = [
                {'key': 'project', 'label': '测试项目', 'value': project_total, 'unit': '个',
                 'icon': 'folder-tree', 'color': '#4d8af0', 'link': '/tcmanagement/projects',
                 'delta': {'value': project_new, 'label': '较昨日'}},
                {'key': 'case', 'label': '测试用例', 'value': case_total, 'unit': '条',
                 'icon': 'clipboard-list', 'color': '#3fae6a', 'link': '/tcmanagement/test_cases',
                 'delta': {'value': case_new, 'label': '较昨日'}},
                {'key': 'group', 'label': '接口分组', 'value': group_total, 'unit': '个',
                 'icon': 'layer-group', 'color': '#e6a23c', 'link': '/automation/interfaces',
                 'delta': {'value': group_new, 'label': '较昨日'}},
                {'key': 'apiCase', 'label': '接口用例', 'value': api_case_total, 'unit': '条',
                 'icon': 'plug', 'color': '#7c5cff', 'link': '/automation/interfaces',
                 'delta': {'value': api_case_new, 'label': '较昨日'}},
                {'key': 'uiCase', 'label': 'UI自动化用例', 'value': None, 'unit': '条',
                 'icon': 'mobile-screen', 'color': '#C0C4CC', 'link': None,
                 'unavailable': True, 'delta': None},
                {'key': 'api', 'label': '接口总数', 'value': api_total, 'unit': '个',
                 'icon': 'diagram-project', 'color': '#4d8af0', 'link': '/automation/interfaces',
                 'delta': {'value': api_new, 'label': '较昨日'}},
                {'key': 'ui', 'label': 'UI', 'value': None, 'unit': '个',
                 'icon': 'display', 'color': '#C0C4CC', 'link': None,
                 'unavailable': True, 'delta': None},
                {'key': 'app', 'label': 'APP自动化', 'value': None, 'unit': '个',
                 'icon': 'mobile', 'color': '#C0C4CC', 'link': None,
                 'unavailable': True, 'delta': None},
            ]

            # ------------------------------------------------------------------
            # 执行趋势：按天 + 触发方式分组，后端补零日
            # ------------------------------------------------------------------
            trend_rows = db.execute_query(
                """
                SELECT DATE(run_time) d, `trigger` t, COUNT(*) n, SUM(success) ok
                FROM api_executions
                WHERE run_time >= %s
                GROUP BY DATE(run_time), `trigger`
                """,
                (window_start,),
            )
            days_list = [str(start_day + timedelta(days=i)) for i in range(days)]
            buckets = {
                key: {d: {'total': 0, 'success': 0} for d in days_list}
                for key in ('all', 'manual', 'schedule')
            }
            for r in trend_rows:
                day = str(r['d'])
                trig = (r.get('t') or 'manual').strip()
                n, ok = _int(r['n']), _int(r['ok'])
                for key in ('all', trig):
                    if key in buckets and day in buckets[key]:
                        buckets[key][day]['total'] += n
                        buckets[key][day]['success'] += ok

            # 上一同长度周期（「较上期」分母）；trigger 为空时按 manual 处理
            def _prev_sum(trigger=None):
                sql = ('SELECT COUNT(*) n FROM api_executions '
                       'WHERE run_time >= %s AND run_time < %s')
                args = [prev_start, window_start]
                if trigger:
                    sql += ' AND `trigger` = %s'
                    args.append(trigger)
                return _int(db.execute_query(sql, tuple(args))[0]['n'])

            prev_all = _prev_sum()
            prev_manual = _prev_sum('manual')
            prev_schedule = _prev_sum('schedule')
            prev_map = {'all': prev_all, 'manual': prev_manual, 'schedule': prev_schedule}

            series = {}
            for key in ('all', 'manual', 'schedule'):
                pts = [{'day': d,
                        'success': buckets[key][d]['success'],
                        'fail': buckets[key][d]['total'] - buckets[key][d]['success']}
                       for d in days_list]
                total = sum(p['success'] + p['fail'] for p in pts)
                ok = sum(p['success'] for p in pts)
                series[key] = {
                    'summary': {
                        'total': total,
                        'success': ok,
                        'fail': total - ok,
                        'pass_rate': _safe_div(ok, total) or 0,
                        'delta_ratio': _safe_div(total - prev_map[key], prev_map[key]),
                    },
                    'points': pts,
                }

            trend = {
                'days': days,
                'range': days_list,
                'tabs': [
                    {'key': 'all', 'label': '全部'},
                    {'key': 'manual', 'label': '手动'},
                    {'key': 'schedule', 'label': '定时'},
                ],
                'series': series,
            }

            # ------------------------------------------------------------------
            # 工作待办（4 卡）
            # ------------------------------------------------------------------
            # 4 张卡一律带 performer_id 过滤（与 taskcases.tasksLists / defects.defect_list
            # 的任务归属判定同源，区别只是首页不豁免 admin）
            my_task = _int(db.execute_query(
                'SELECT COUNT(*) c FROM task_management '
                'WHERE status = 1 AND is_del = 0 AND task_status <> 4 '
                'AND task_performer_id = %s',
                (performer_id,),
            )[0]['c'])

            # 必须与 /defects/list 的 statistics.total 同口径（只取 total，
            # 不用 4 个状态桶相加——库里存在 defect_status=4 不进任何桶的数据）
            defect_total = _int(db.execute_query(
                'SELECT COUNT(*) c FROM task_detail td '
                'LEFT JOIN task_management tm ON tm.id = td.task_id '
                'WHERE td.case_status = 3 AND td.status = 1 AND tm.task_status = 4 '
                'AND tm.task_performer_id = %s',
                (performer_id,),
            )[0]['c'])

            # run_by / creator_id 存的都是 uid；creator_id 为 NULL 的历史数据无法归属，
            # 不计入「我今日新增的用例」（与列表页「按创建人筛」行为一致）
            ai_draft = _int(db.execute_query(
                'SELECT COUNT(*) c FROM ai_case_drafts WHERE status = 0 AND run_by = %s',
                (performer_id,),
            )[0]['c'])
            new_case_today = _int(db.execute_query(
                'SELECT COUNT(*) c FROM test_cases '
                'WHERE is_del = 0 AND DATE(create_time) = CURDATE() AND creator_id = %s',
                (performer_id,),
            )[0]['c'])

            todo = [
                {'key': 'myTask', 'label': '我的待办任务', 'value': my_task,
                 'icon': 'list-check', 'color': '#4d8af0',
                 'link': '/taskmanagement/tasklists'},
                {'key': 'defect', 'label': '我的任务缺陷', 'value': defect_total,
                 'icon': 'bug', 'color': '#e5534b',
                 'link': '/taskmanagement/defectlists'},
                {'key': 'aiDraft', 'label': '我的待入库草稿', 'value': ai_draft,
                 'icon': 'robot', 'color': '#7c5cff',
                 'link': '/aiwork/drafts'},
                {'key': 'newCase', 'label': '我今日新增用例', 'value': new_case_today,
                 'icon': 'file-circle-plus', 'color': '#3fae6a',
                 'link': '/tcmanagement/test_cases'},
            ]

            # ------------------------------------------------------------------
            # 数据统计（接口自动化补充维度，与 8 卡不重复）
            # ------------------------------------------------------------------
            scene_rows = db.execute_query(
                f'SELECT dataset FROM api_interfaces WHERE NOT {_NO_DATASET}'
            )
            scene_case = 0
            for r in scene_rows:
                ds = r.get('dataset')
                if isinstance(ds, str):
                    try:
                        ds = json.loads(ds)
                    except Exception:
                        ds = None
                if isinstance(ds, list):
                    scene_case += len(ds)
            env_count = _int(db.execute_query(
                'SELECT COUNT(*) c FROM api_environments'
            )[0]['c'])
            schedule_count = _int(db.execute_query(
                'SELECT COUNT(*) c FROM api_schedules'
            )[0]['c'])
            exec_total = _int(db.execute_query(
                'SELECT COUNT(*) c FROM api_executions'
            )[0]['c'])

            statistics = [
                {'key': 'sceneCase', 'label': '场景用例', 'value': scene_case, 'unit': '条'},
                {'key': 'env', 'label': '测试环境', 'value': env_count, 'unit': '个'},
                {'key': 'schedule', 'label': '定时计划', 'value': schedule_count, 'unit': '个'},
                {'key': 'execTotal', 'label': '执行总数', 'value': exec_total, 'unit': '次'},
            ]

            return {
                'code': 200, 'msg': 'success',
                'data': {
                    'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'cards': cards,
                    'trend': trend,
                    'todo': todo,
                    'statistics': statistics,
                },
            }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'首页统计失败: {str(e)}')
