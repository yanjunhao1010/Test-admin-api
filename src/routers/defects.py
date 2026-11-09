import logging
import os
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from src.comment import MySqlconection, yaml_data
from .auth import parse_token

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/defects',
    tags=['defects'])

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


# 缺陷处理状态映射（与 task_detail.defect_status 列一一对应）
DEFECT_STATUS_TEXT = {
    0: '待处理',
    1: '修复中',
    2: '已修复',
    3: '已关闭',
    4: '已拒绝',
}


"""缺陷管理第一版：缺陷 = 任务中执行结果为「失败」(case_status=3) 的测试用例
维度 = 按任务 (task_id) 归集，每个任务下列出全部失败用例，并附缺陷处理状态。
"""
@router.get('/list')
async def defect_list(request: Request):
    try:
        with get_db_handler() as db:
            # 当前登录用户：超管(角色含 'admin')查看全部，其余账号仅看所属任务的缺陷。
            # 借用 taskcases 的过滤思路，按 task_management.task_performer_id（用户 id）匹配，
            # 比用 task_performer 名字可靠（名字可能重复/为空）。
            performer_id = None
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
                            performer_id = uid
                except Exception as e:
                    logger.warning(f"解析当前用户失败，按查看全部处理: {e}")
                    performer_id = None

            # 缺陷仅在「任务已完成」(task_management.task_status = 4) 时才汇总出现；
            # 未完成任务(执行中/已开始)的失败用例不进入缺陷列表。
            where = "td.case_status = 3 AND td.status = 1 AND tm.task_status = 4"
            params = []
            if performer_id:
                where += " AND tm.task_performer_id = %s"
                params.append(performer_id)

            query = """
                SELECT
                    td.id,
                    td.task_id,
                    tm.task_name,
                    tm.task_performer,
                    td.project,
                    td.module,
                    td.function_point,
                    td.priority,
                    td.expected_result,
                    td.final_result,
                    td.remark,
                    td.defect_status,
                    td.create_time,
                    td.update_time
                FROM task_detail td
                LEFT JOIN task_management tm ON tm.id = td.task_id
                WHERE {}
                ORDER BY td.task_id, td.id
            """.format(where)
            rows = db.execute_query(query, tuple(params))

            # 按任务维度聚合失败用例
            tasks_map = {}
            stats = {'total': 0, 'pending': 0, 'fixing': 0, 'fixed': 0, 'closed': 0}
            for r in rows:
                ds = r.get('defect_status') or 0
                stats['total'] += 1
                if ds == 0:
                    stats['pending'] += 1
                elif ds == 1:
                    stats['fixing'] += 1
                elif ds == 2:
                    stats['fixed'] += 1
                elif ds == 3:
                    stats['closed'] += 1

                tid = r['task_id']
                if tid not in tasks_map:
                    tasks_map[tid] = {
                        'task_id': tid,
                        'task_name': r['task_name'],
                        'task_performer': r['task_performer'],
                        'failed_count': 0,
                        'cases': [],
                    }
                tasks_map[tid]['failed_count'] += 1
                tasks_map[tid]['cases'].append({
                    'id': r['id'],
                    'project': r['project'],
                    'module': r['module'],
                    'function_point': r['function_point'],
                    'priority': r['priority'],
                    'expected_result': r['expected_result'],
                    'final_result': r['final_result'] or '',
                    'remark': r['remark'],
                    'defect_status': ds,
                    'defect_status_text': DEFECT_STATUS_TEXT.get(ds, '未知'),
                    'create_time': r['create_time'],
                    'update_time': r['update_time'],
                })

            tasks = list(tasks_map.values())
            return {
                'code': 200,
                'msg': 'success',
                'data': {
                    'statistics': stats,
                    'tasks': tasks,
                },
            }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'数据库查询失败: {str(e)}')


class DefectStatusUpdate(BaseModel):
    id: int
    defect_status: int
    final_result: Optional[str] = ''
    handle_result: Optional[str] = ''


"""更新单条失败用例的缺陷处理状态与失败原因"""
@router.post('/update_status')
async def update_defect_status(data: DefectStatusUpdate):
    try:
        with get_db_handler() as db:
            query = """
                UPDATE task_detail
                SET defect_status = %s,
                    final_result = %s,
                    remark = %s,
                    update_time = NOW()
                WHERE id = %s
            """
            db.execute_update(query, (data.defect_status, data.final_result or '', data.handle_result or '', data.id))
            return {
                'code': 200,
                'msg': 'success',
                'data': {'detail': '更新成功'},
            }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f'数据库更新失败: {str(e)}')
