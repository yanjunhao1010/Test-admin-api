"""通知中心（Notification Center）路由

覆盖 4 类派送/完成通知：
  1 任务派送 / 2 缺陷派送 / 3 定时任务完成 / 4 AI 生成完成

行级权限：严格按 receiver_uid 过滤，admin 不豁免（与 taskcases/defects 同源口径）。

对外：
  GET  /notifications/list       当前用户通知列表（支持 type / is_read 筛选 + 分页）
  GET  /notifications/unread-count  当前用户未读数（铃铛红点用）
  POST /notifications/read       标记单条已读 {id}
  POST /notifications/read-all   全部已读

对内（供其它路由调用，不阻断主业务）：
  notify(receiver_uid, type, title, content='', biz_type='', biz_id='', link='', sender_uid=None)
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.comment import MySqlconection, yaml_data
from src.routers.auth import parse_token

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

router = APIRouter(
    prefix='/notifications',
    tags=['notifications'],
)

# 通知类型文案（前端也用同一套，保持一致）
TYPE_LABELS = {
    1: '任务派送',
    2: '缺陷派送',
    3: '定时任务完成',
    4: 'AI生成完成',
    5: '流程节点派送',
}


def get_db_handler():
    # 配置走缓存：派送通知现在被高频调用（流程每次流转都发），原来每次都读 config.yml + 解析，纯浪费
    data = MySqlconection.load_config_cached(CONFIG_PATH)
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )


def _uid(request: Request) -> int:
    """从 accessToken 头解析当前用户 uid；失败返回 0。"""
    token = request.headers.get('accessToken')
    if not token:
        return 0
    try:
        return parse_token(token) or 0
    except Exception:
        return 0


def notify(receiver_uid, ntype, title, content='', biz_type='', biz_id='', link='', sender_uid=None):
    """写入一条通知。任何异常仅记日志、绝不影响调用方主业务。

    Args:
        receiver_uid: 接收人 uid（必填，0/None 直接跳过）
        ntype:        类型 1/2/3/4
        title:        标题（截断 200）
        content:      正文（截断 500）
        biz_type:     task/defect/schedule/ai
        biz_id:       业务对象 id（转字符串，截断 64）
        link:         前端跳转路由（截断 255）
        sender_uid:   触发人 uid（可选）
    """
    if not receiver_uid:
        return
    try:
        with get_db_handler() as db:
            db.execute_update(
                'INSERT INTO `notifications` '
                '(type, receiver_uid, sender_uid, title, content, biz_type, biz_id, link) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (
                    int(ntype),
                    int(receiver_uid),
                    int(sender_uid) if sender_uid else None,
                    (title or '')[:200],
                    (content or '')[:500],
                    (biz_type or '')[:32],
                    str(biz_id)[:64],
                    (link or '')[:255],
                ),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning('notify insert failed (receiver=%s type=%s): %s', receiver_uid, ntype, e)


# ---------------------------------------------------------------------------
# 查询 / 操作接口（行级严格按 receiver_uid）
# ---------------------------------------------------------------------------
@router.get('/list')
async def list_notifications(
    request: Request,
    type: int = 0,
    is_read: int = -1,
    page: int = 1,
    pageSize: int = 20,
):
    uid = _uid(request)
    if not uid:
        return {'code': 401, 'msg': '未登录', 'data': []}
    try:
        with get_db_handler() as db:
            where = ['receiver_uid = %s']
            params = [uid]
            if type:
                where.append('`type` = %s')
                params.append(type)
            if is_read == 0 or is_read == 1:
                where.append('is_read = %s')
                params.append(is_read)
            w = ' AND '.join(where)
            total_row = db.execute_query(
                'SELECT COUNT(*) AS c FROM `notifications` WHERE %s' % w, params)
            total = total_row[0]['c'] if total_row else 0
            offset = (max(page, 1) - 1) * pageSize
            rows = db.execute_query(
                'SELECT * FROM `notifications` WHERE %s '
                'ORDER BY create_time DESC LIMIT %%s OFFSET %%s' % w,
                params + [pageSize, offset],
            )
            return {'code': 200, 'msg': 'success', 'data': {'total': total, 'list': rows}}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 500, 'msg': '查询通知失败：%s' % e, 'data': []}


@router.get('/unread-count')
async def unread_count(request: Request):
    uid = _uid(request)
    if not uid:
        return {'code': 200, 'msg': 'success', 'data': {'count': 0}}
    try:
        with get_db_handler() as db:
            row = db.execute_query(
                'SELECT COUNT(*) AS c FROM `notifications` '
                'WHERE receiver_uid = %s AND is_read = 0', (uid,))
            count = row[0]['c'] if row else 0
            return {'code': 200, 'msg': 'success', 'data': {'count': count}}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 200, 'msg': 'success', 'data': {'count': 0}}


class _ReadReq(BaseModel):
    id: int


@router.post('/read')
async def read_one(request: Request, data: _ReadReq):
    uid = _uid(request)
    if not uid:
        return {'code': 401, 'msg': '未登录'}
    try:
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE `notifications` SET is_read = 1 WHERE id = %s AND receiver_uid = %s',
                (data.id, uid),
            )
        return {'code': 200, 'msg': 'success'}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 500, 'msg': '标记已读失败：%s' % e}


@router.post('/read-all')
async def read_all(request: Request):
    uid = _uid(request)
    if not uid:
        return {'code': 401, 'msg': '未登录'}
    try:
        with get_db_handler() as db:
            db.execute_update(
                'UPDATE `notifications` SET is_read = 1 '
                'WHERE receiver_uid = %s AND is_read = 0', (uid,))
        return {'code': 200, 'msg': 'success'}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 500, 'msg': '标记全部已读失败：%s' % e}
