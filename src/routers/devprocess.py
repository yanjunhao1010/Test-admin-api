# -*- coding: utf-8 -*-
"""
研发流程可视化（智能流程看板）+ 需求变更影响分析 后端路由。

覆盖：
  F1 看板列表 / F2 新建流程实例 / F3 详情 / F4 完成并流转 / F5 改负责人备注
  F6 节点定义可配置 / F8 跳过 / F9 终止归档 / F15 受控回退
  F12 功能点规范化 / F13 需求变更录入 / F14 半自动影响分析
权限：admin 看全部；非 admin 只看自己发起或负责(节点 assignee)的流程。
负责人全程自由分配，不绑定角色。
"""
import os
import re
import time
import uuid
import mimetypes
import logging
from typing import List, Optional
from fastapi import APIRouter, Request, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from src.comment import MySqlconection, yaml_data
from src.routers.auth import parse_token
from src.routers.notifications import notify

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/devprocess', tags=['devprocess'])

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    # 配置走缓存：原来每个请求都读一次 config.yml + 解析，纯浪费
    data = MySqlconection.load_config_cached(CONFIG_PATH)
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )


# ---------- 鉴权/行级权限 ----------
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


def _envelope(data=None, total=None, msg='success', code=200):
    if total is not None:
        return {'code': code, 'msg': msg, 'total': total, 'data': data}
    return {'code': code, 'msg': msg, 'data': data}


# ---------- 请求模型 ----------
class CreateProcessReq(BaseModel):
    title: str
    project_id: Optional[int] = None
    first_assignee_id: Optional[int] = None
    first_assignee_role: str = ''
    planned_finish_at: Optional[str] = None


class AdvanceReq(BaseModel):
    process_id: int
    node_index: int
    completer_id: Optional[int] = None
    remark: str = ''
    next_assignee_id: Optional[int] = None
    next_assignee_role: str = ''


class UpdateNodeReq(BaseModel):
    process_id: int
    node_index: int
    assignee_id: Optional[int] = None
    assignee_role: str = ''
    remark: str = ''


class UpdateProcessReq(BaseModel):
    """修改研发流程的项目名称"""
    id: int
    title: Optional[str] = None


class SkipReq(BaseModel):
    process_id: int
    node_index: int
    next_assignee_id: Optional[int] = None
    next_assignee_role: str = ''


class TerminateReq(BaseModel):
    process_id: int


class ReopenReq(BaseModel):
    process_id: int
    node_index: int
    operator_id: Optional[int] = None
    reason: str = ''


class NodeDefSaveReq(BaseModel):
    id: Optional[int] = None
    node_index: Optional[int] = None
    node_key: str
    node_name: str
    is_required: int = 1


class NodeDefDeleteReq(BaseModel):
    id: int


class FunctionPointSaveReq(BaseModel):
    id: Optional[int] = None
    name: str
    project_id: Optional[int] = None
    module: str = ''
    description: str = ''
    baseline: str = ''


class FunctionPointDeleteReq(BaseModel):
    id: int


class ChangeSaveReq(BaseModel):
    id: Optional[int] = None
    title: str
    project_id: Optional[int] = None
    change_type: int = 1
    description: str = ''
    creator_id: Optional[int] = None
    function_point_ids: List[int] = []
    new_function_points: List[dict] = []


class LinkDispositionReq(BaseModel):
    link_id: Optional[int] = None
    change_id: Optional[int] = None
    function_point_id: Optional[int] = None
    case_id: Optional[int] = None
    disposition: int = 0
    note: str = ''
    handled_by: Optional[int] = None


# ---------- F10：节点关联引用 ----------
class NodeLinkAddReq(BaseModel):
    process_id: int
    node_index: int
    ref_type: int            # 1 测试用例 / 2 测试任务(缺陷) / 3 需求变更 / 4 需求分析
    ref_id: int
    ref_title: str = ''
    note: str = ''


class NodeLinkDeleteReq(BaseModel):
    id: int


class AttachmentDeleteReq(BaseModel):
    id: int


# ---------- 附件存储（F10 扩展：关联引用支持文件上传） ----------
# 存储根目录：默认 <项目根>/uploads/devprocess
#   本地：D:\pyproject\Fast_api_project\uploads\devprocess
#   生产：/opt/test-admin-api/uploads/devprocess
# 可用环境变量 DEVPROCESS_UPLOAD_DIR 覆盖（例如挂独立数据盘）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD_ROOT = os.environ.get('DEVPROCESS_UPLOAD_DIR') or os.path.join(_PROJECT_ROOT, 'uploads', 'devprocess')
MAX_UPLOAD_MB = int(os.environ.get('DEVPROCESS_UPLOAD_MAX_MB', '20'))
# 允许的扩展名白名单（避免把 .exe / .sh 之类可执行文件传上服务器）
ALLOWED_EXT = {
    '.md', '.txt', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.pdf', '.ppt', '.pptx',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.mp4', '.mov',
    '.json', '.xml', '.html', '.htm', '.log', '.sql', '.yaml', '.yml', '.zip', '.rar', '.7z',
}


def _safe_file_name(name: str) -> str:
    """只保留文件名本身，去掉路径分隔符与危险字符（防目录穿越）。"""
    name = os.path.basename((name or '').strip().replace('\\', '/'))
    name = re.sub(r'[\x00-\x1f/:*?"<>|]', '_', name)
    return name[:200] or 'unnamed'


def _abs_store_path(rel_path: str) -> Optional[str]:
    """把库里的相对路径还原为绝对路径，并确保仍落在 UPLOAD_ROOT 内（防穿越）。"""
    if not rel_path:
        return None
    root = os.path.realpath(UPLOAD_ROOT)
    target = os.path.realpath(os.path.join(root, rel_path))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def _can_access_process(db, uid, pid) -> bool:
    """行级权限：admin 看全部；否则仅「流程发起人」或「任一节点负责人」可见。

    流程必须存在且未删除（admin 同样要求），否则改不存在的 id 会 UPDATE 0 行
    却返回「已更新」——静默假成功。
    """
    if not uid:
        return False
    if _is_admin(db, uid):
        rows = db.execute_query(
            "SELECT id FROM dev_process dp WHERE dp.id=%s AND dp.is_del=0", (pid,)
        )
    else:
        rows = db.execute_query(
            "SELECT dp.id FROM dev_process dp WHERE dp.id=%s AND dp.is_del=0 AND ("
            "dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n "
            "WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0))",
            (pid, uid, uid),
        )
    return bool(rows)


def _insert_id(db, sql: str, params) -> int:
    """INSERT 并返回自增 id（DatabaseHandler.execute_update 只返回影响行数）。"""
    db._ensure_connection()  # noqa: SLF001  同项目内复用连接
    with db.connection.cursor() as cur:
        cur.execute(sql, params)
        new_id = cur.lastrowid
    db.connection.commit()
    return new_id


# ---------- 节点派送通知（notifications.type = 5） ----------
NOTIFY_TYPE_PROCESS = 5
PROCESS_LINK = '/devprocess/list'


def _notify_node_owner(db, pid, node_index, receiver_id, sender_uid, action='指派'):
    """把节点指派给某位负责人时，给他发一条站内通知。

    失败只记日志、绝不阻断主业务（与 notifications.notify 同一纪律）。
    receiver_id 为空（没指定负责人）时直接跳过。
    """
    if not receiver_id:
        return False
    try:
        proc = db.execute_single_query(
            "SELECT code, title FROM dev_process WHERE id=%s AND is_del=0", (pid,))
        if not proc:
            return False
        node = db.execute_single_query(
            "SELECT node_name FROM dev_process_node WHERE process_id=%s AND node_index=%s AND is_del=0",
            (pid, node_index))
        node_name = (node or {}).get('node_name') or '节点'
        notify(
            receiver_id, NOTIFY_TYPE_PROCESS,
            '您有新的研发流程节点待处理',
            '流程「%s」（%s）的「%s」节点已%s给您，请及时处理。'
            % (proc['title'], proc['code'], node_name, action),
            'devprocess', pid, '%s?id=%s' % (PROCESS_LINK, pid), sender_uid,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning('节点派送通知失败(pid=%s node=%s): %s', pid, node_index, e)
        return False


# ---------- 流程：F1~F5 / F8 / F9 / F15 ----------
@router.get('/list')
async def list_processes(pageNo: int = 1, pageSize: int = 20, keyword: str = '', status: str = '', request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            admin = _is_admin(db, uid)
            conds = ['dp.is_del=0']
            args = []
            if keyword:
                conds.append('dp.title LIKE %s')
                args.append(f'%{keyword}%')
            if status != '':
                conds.append('dp.status=%s')
                args.append(int(status))
            if not admin:
                conds.append(
                    '(dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n '
                    'WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0))'
                )
                args.extend([uid, uid])
            where = ' AND '.join(conds)
            total = db.execute_query(f"SELECT COUNT(*) c FROM dev_process dp WHERE {where}", tuple(args))[0]['c']
            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                f"""SELECT dp.id, dp.code, dp.title, dp.project_id, dp.creator_id, dp.current_node,
                           dp.progress, dp.status, dp.create_time,
                           (SELECT node_name FROM dev_process_node n WHERE n.process_id=dp.id
                              AND n.node_index=dp.current_node AND n.is_del=0) AS current_node_name,
                           (SELECT u.realname FROM dev_process_node n LEFT JOIN users u ON u.id=n.assignee_id
                              WHERE n.process_id=dp.id AND n.node_index=dp.current_node AND n.is_del=0) AS current_assignee
                    FROM dev_process dp WHERE {where} ORDER BY dp.create_time DESC LIMIT %s OFFSET %s""",
                tuple(args + [pageSize, offset]),
            )
            # 补充创建人姓名 + 项目名
            for r in rows:
                cr = db.execute_query("SELECT realname FROM users WHERE id=%s", (r['creator_id'],))
                r['creator_name'] = cr[0]['realname'] if cr else ''
                if r['project_id']:
                    pr = db.execute_query("SELECT cate_name FROM test_case_project WHERE id=%s", (r['project_id'],))
                    r['project_name'] = pr[0]['cate_name'] if pr else ''
                else:
                    r['project_name'] = ''
            # 一次性取本页所有流程的节点链（看板按节点链渲染，避免逐条查详情造成 N+1）
            if rows:
                ids = [r['id'] for r in rows]
                ph = ','.join(['%s'] * len(ids))
                node_rows = db.execute_query(
                    f"""SELECT n.process_id, n.node_index, n.node_name, n.status, n.assignee_id,
                               d.is_required, u.realname AS assignee_name
                        FROM dev_process_node n
                        LEFT JOIN dev_process_node_def d ON d.node_index = n.node_index
                        LEFT JOIN users u ON u.id = n.assignee_id
                        WHERE n.process_id IN ({ph}) AND n.is_del = 0
                        ORDER BY n.process_id, n.node_index""",
                    tuple(ids),
                )
                nmap = {}
                for nr in node_rows:
                    nmap.setdefault(nr['process_id'], []).append(nr)
                for r in rows:
                    r['nodes'] = nmap.get(r['id'], [])
            return _envelope(rows, total)
    except Exception as e:
        return _envelope(None, 0, f'查询失败: {e}', 500)


@router.post('/create')
async def create_process(data: CreateProcessReq, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            defs = db.execute_query("SELECT * FROM dev_process_node_def ORDER BY node_index")
            if not defs:
                return _envelope(None, None, '节点定义为空，请先在节点配置中初始化', 500)
            total = len(defs)
            # 生成编号 DP-YYYYMMDD-XXX
            today = db.execute_query("SELECT DATE_FORMAT(NOW(),'%%Y%%m%%d') AS d")[0]['d']
            cnt = db.execute_query(
                "SELECT COUNT(*) c FROM dev_process WHERE code LIKE %s", (f'DP-{today}-%',)
            )[0]['c']
            code = f'DP-{today}-{cnt+1:03d}'
            db.execute_update(
                "INSERT INTO dev_process (code, title, project_id, creator_id, current_node, progress, status, planned_finish_at) "
                "VALUES (%s,%s,%s,%s,1,0,0,%s)",
                (code, data.title, data.project_id, uid, data.planned_finish_at),
            )
            pid = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
            for i, d in enumerate(defs):
                is_first = (i == 0)
                db.execute_update(
                    """INSERT INTO dev_process_node
                       (process_id, node_index, node_key, node_name, assignee_id, assignee_role, status, started_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s, %s)""",
                    (pid, d['node_index'], d['node_key'], d['node_name'],
                     data.first_assignee_id if is_first else None,
                     data.first_assignee_role if is_first else '',
                     1 if is_first else 0,
                     None),
                )
            # 第一节点置「进行中」并开始计时
            if defs:
                db.execute_update(
                    "UPDATE dev_process_node SET started_at=NOW() WHERE process_id=%s AND node_index=%s",
                    (pid, defs[0]['node_index']),
                )
            # 指定了首节点负责人 → 派送通知
            if data.first_assignee_id:
                _notify_node_owner(db, pid, defs[0]['node_index'],
                                   data.first_assignee_id, uid, '指派')
            return _envelope({'id': pid, 'code': code, 'total': total}, None, '新建成功')
    except Exception as e:
        return _envelope(None, None, f'新建失败: {e}', 500)


@router.post('/updateProcess')
async def update_process(data: UpdateProcessReq, request: Request = None):
    """修改研发流程的项目名称（标题）。

    编号 code 由系统按规则生成，不在这里改，避免破坏编号语义与追溯。
    """
    try:
        uid = _uid(request)
        if not uid:
            return _envelope(None, None, '未登录', 401)
        title = (data.title or '').strip()
        if not title:
            return _envelope(None, None, '请填写项目名称', 400)
        with get_db_handler() as db:
            if not _can_access_process(db, uid, data.id):
                return _envelope(None, None, '流程不存在或无权操作', 404)
            db.execute_update(
                "UPDATE dev_process SET title=%s, update_time=NOW() WHERE id=%s AND is_del=0",
                (title, data.id),
            )
            return _envelope(None, None, '已更新')
    except Exception as e:
        return _envelope(None, None, f'更新失败: {e}', 500)


@router.get('/detail')
async def process_detail(process_id: int, request: Request = None):
    try:
        with get_db_handler() as db:
            dp = db.execute_single_query("SELECT * FROM dev_process WHERE id=%s AND is_del=0", (process_id,))
            if not dp:
                return _envelope(None, None, '流程不存在', 404)
            nodes = db.execute_query(
                """SELECT n.*, u1.realname AS assignee_name, u2.realname AS completer_name,
                          u3.realname AS reopened_name, d.is_required
                   FROM dev_process_node n
                   LEFT JOIN users u1 ON u1.id=n.assignee_id
                   LEFT JOIN users u2 ON u2.id=n.completer_id
                   LEFT JOIN users u3 ON u3.id=n.reopened_by
                   LEFT JOIN dev_process_node_def d ON d.node_index=n.node_index
                   WHERE n.process_id=%s AND n.is_del=0 ORDER BY n.node_index""",
                (process_id,),
            )
            defs = db.execute_query("SELECT * FROM dev_process_node_def ORDER BY node_index")
            members = db.execute_query(
                """SELECT u.id, u.realname, GROUP_CONCAT(r.code) AS role_codes
                   FROM users u LEFT JOIN user_roles ur ON ur.user_id=u.id
                   LEFT JOIN roles r ON r.id=ur.role_id GROUP BY u.id ORDER BY u.id"""
            )
            for m in members:
                m['role_code'] = (m.pop('role_codes') or '') or ''
            links = db.execute_query(
                "SELECT * FROM dev_process_node_links WHERE process_id=%s AND is_del=0 ORDER BY node_index, id",
                (process_id,),
            )
            # 节点附件（关联引用里的文件上传）
            attachments = db.execute_query(
                "SELECT a.*, u.realname AS uploader_name FROM dev_process_attachments a "
                "LEFT JOIN users u ON u.id = a.create_by "
                "WHERE a.process_id=%s AND a.is_del=0 ORDER BY a.node_index, a.id",
                (process_id,),
            )
            return _envelope({'process': dp, 'nodes': nodes, 'defs': defs, 'members': members,
                              'links': links, 'attachments': attachments})
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


def _total_defs(db):
    return db.execute_query("SELECT COUNT(*) c FROM dev_process_node_def")[0]['c']


def _recompute_progress(db, pid):
    total = _total_defs(db)
    done = db.execute_query(
        "SELECT COUNT(*) c FROM dev_process_node WHERE process_id=%s AND status IN (2,3) AND is_del=0", (pid,)
    )[0]['c']
    progress = round(done / total * 100) if total else 0
    db.execute_update("UPDATE dev_process SET progress=%s WHERE id=%s", (progress, pid))
    return progress


@router.post('/advance')
async def advance_node(data: AdvanceReq, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            node = db.execute_single_query(
                "SELECT * FROM dev_process_node WHERE process_id=%s AND node_index=%s AND is_del=0",
                (data.process_id, data.node_index),
            )
            if not node:
                return _envelope(None, None, '节点不存在', 404)
            if node['status'] != 1:
                return _envelope(None, None, '该节点未进行中，无法完成', 400)
            total = _total_defs(db)
            db.execute_update(
                "UPDATE dev_process_node SET status=2, completer_id=%s, finished_at=NOW(), remark=%s "
                "WHERE process_id=%s AND node_index=%s",
                (data.completer_id or uid, data.remark, data.process_id, data.node_index),
            )
            if data.node_index < total:
                db.execute_update(
                    """UPDATE dev_process_node
                       SET status=1, started_at=NOW(),
                           assignee_id=COALESCE(%s, assignee_id),
                           assignee_role=COALESCE(NULLIF(%s,''), assignee_role)
                       WHERE process_id=%s AND node_index=%s""",
                    (data.next_assignee_id, data.next_assignee_role, data.process_id, data.node_index + 1),
                )
                db.execute_update("UPDATE dev_process SET current_node=%s WHERE id=%s",
                                 (data.node_index + 1, data.process_id))
                # 通知下一节点负责人（只有明确选了负责人才发）
                if data.next_assignee_id:
                    _notify_node_owner(db, data.process_id, data.node_index + 1,
                                       data.next_assignee_id, uid, '指派')
            else:
                db.execute_update("UPDATE dev_process SET status=1, current_node=%s, completed_at=NOW() WHERE id=%s",
                                 (total, data.process_id))
            progress = _recompute_progress(db, data.process_id)
            return _envelope({'progress': progress, 'current_node': min(data.node_index + 1, total)}, None, '已流转')
    except Exception as e:
        return _envelope(None, None, f'流转失败: {e}', 500)


@router.post('/updateNode')
async def update_node(data: UpdateNodeReq, request: Request = None):
    try:
        with get_db_handler() as db:
            node = db.execute_single_query(
                "SELECT * FROM dev_process_node WHERE process_id=%s AND node_index=%s AND is_del=0",
                (data.process_id, data.node_index),
            )
            if not node:
                return _envelope(None, None, '节点不存在', 404)
            db.execute_update(
                "UPDATE dev_process_node SET assignee_id=%s, assignee_role=%s, remark=%s "
                "WHERE process_id=%s AND node_index=%s",
                (data.assignee_id, data.assignee_role, data.remark, data.process_id, data.node_index),
            )
            # 改负责人 = 重新指派，通知新的负责人（换了人才发，避免重复打扰）
            if data.assignee_id and data.assignee_id != node.get('assignee_id'):
                _notify_node_owner(db, data.process_id, data.node_index,
                                   data.assignee_id, _uid(request), '改派')
            return _envelope(None, None, '已更新')
    except Exception as e:
        return _envelope(None, None, f'更新失败: {e}', 500)


@router.post('/skip')
async def skip_node(data: SkipReq, request: Request = None):
    try:
        with get_db_handler() as db:
            node = db.execute_single_query(
                "SELECT n.*, d.is_required FROM dev_process_node n "
                "LEFT JOIN dev_process_node_def d ON d.node_index=n.node_index "
                "WHERE n.process_id=%s AND n.node_index=%s AND n.is_del=0",
                (data.process_id, data.node_index),
            )
            if not node:
                return _envelope(None, None, '节点不存在', 404)
            if node['status'] != 1:
                return _envelope(None, None, '该节点未进行中，无法跳过', 400)
            if node['is_required'] == 1:
                return _envelope(None, None, '必走节点不可跳过', 400)
            total = _total_defs(db)
            db.execute_update("UPDATE dev_process_node SET status=3 WHERE process_id=%s AND node_index=%s",
                             (data.process_id, data.node_index))
            if data.node_index < total:
                db.execute_update(
                    """UPDATE dev_process_node
                       SET status=1, started_at=NOW(),
                           assignee_id=COALESCE(%s, assignee_id),
                           assignee_role=COALESCE(NULLIF(%s,''), assignee_role)
                       WHERE process_id=%s AND node_index=%s""",
                    (data.next_assignee_id, data.next_assignee_role, data.process_id, data.node_index + 1),
                )
                db.execute_update("UPDATE dev_process SET current_node=%s WHERE id=%s",
                                 (data.node_index + 1, data.process_id))
                # 跳过同样会把下一节点推进为进行中，通知其负责人
                if data.next_assignee_id:
                    _notify_node_owner(db, data.process_id, data.node_index + 1,
                                       data.next_assignee_id, _uid(request), '指派')
            else:
                db.execute_update("UPDATE dev_process SET status=1, current_node=%s, completed_at=NOW() WHERE id=%s",
                                 (total, data.process_id))
            progress = _recompute_progress(db, data.process_id)
            return _envelope({'progress': progress, 'current_node': min(data.node_index + 1, total)}, None, '已跳过')
    except Exception as e:
        return _envelope(None, None, f'跳过失败: {e}', 500)


@router.post('/terminate')
async def terminate_process(data: TerminateReq, request: Request = None):
    try:
        with get_db_handler() as db:
            db.execute_update("UPDATE dev_process SET status=2 WHERE id=%s", (data.process_id,))
            return _envelope(None, None, '已终止')
    except Exception as e:
        return _envelope(None, None, f'终止失败: {e}', 500)


@router.post('/reopen')
async def reopen_node(data: ReopenReq, request: Request = None):
    """F15 受控回退：重开已完成节点 N，N 置进行中，N 之后整段回退待开始。"""
    try:
        with get_db_handler() as db:
            node = db.execute_single_query(
                "SELECT * FROM dev_process_node WHERE process_id=%s AND node_index=%s AND is_del=0",
                (data.process_id, data.node_index),
            )
            if not node:
                return _envelope(None, None, '节点不存在', 404)
            if node['status'] != 2:
                return _envelope(None, None, '仅已完成节点可重开', 400)
            total = _total_defs(db)
            # N 置进行中（清空完成信息，记重开审计）
            db.execute_update(
                """UPDATE dev_process_node
                   SET status=1, started_at=NOW(), finished_at=NULL, completer_id=NULL,
                       reopen_count=reopen_count+1, reopened_by=%s, reopened_at=NOW(), reopen_reason=%s
                   WHERE process_id=%s AND node_index=%s""",
                (data.operator_id, data.reason, data.process_id, data.node_index),
            )
            # N 之后整段回退待开始（保留 assignee 可重指派）
            if data.node_index < total:
                db.execute_update(
                    """UPDATE dev_process_node
                       SET status=0, started_at=NULL, finished_at=NULL, completer_id=NULL
                       WHERE process_id=%s AND node_index>%s""",
                    (data.process_id, data.node_index),
                )
            db.execute_update("UPDATE dev_process SET current_node=%s WHERE id=%s",
                             (data.node_index, data.process_id))
            progress = _recompute_progress(db, data.process_id)
            # 重开 = 节点重新回到该负责人手上，通知其回来处理
            if node.get('assignee_id'):
                _notify_node_owner(db, data.process_id, data.node_index,
                                   node['assignee_id'], _uid(request), '重开')
            return _envelope({'progress': progress, 'current_node': data.node_index}, None, '已重开')
    except Exception as e:
        return _envelope(None, None, f'重开失败: {e}', 500)


# ---------- 公共下拉数据 ----------
@router.get('/members')
async def members():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                """SELECT u.id, u.realname, GROUP_CONCAT(r.code) AS role_codes
                   FROM users u LEFT JOIN user_roles ur ON ur.user_id=u.id
                   LEFT JOIN roles r ON r.id=ur.role_id GROUP BY u.id ORDER BY u.id"""
            )
            for m in rows:
                m['role_code'] = (m.pop('role_codes') or '') or ''
            return _envelope(rows)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.get('/projects')
async def projects():
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                "SELECT id, cate_name, level FROM test_case_project WHERE is_del=0 ORDER BY id"
            )
            return _envelope(rows)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


# ---------- 节点定义配置：F6 ----------
@router.get('/nodeDef/list')
async def node_def_list():
    try:
        with get_db_handler() as db:
            rows = db.execute_query("SELECT * FROM dev_process_node_def ORDER BY node_index")
            return _envelope(rows)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.post('/nodeDef/save')
async def node_def_save(data: NodeDefSaveReq):
    try:
        with get_db_handler() as db:
            # 唯一性校验（排除自身）
            dup = db.execute_query(
                "SELECT COUNT(*) c FROM dev_process_node_def WHERE node_key=%s AND id<>%s",
                (data.node_key, data.id or 0),
            )[0]['c']
            if dup:
                return _envelope(None, None, '节点标识已存在', 400)
            if data.id:
                db.execute_update(
                    "UPDATE dev_process_node_def SET node_key=%s, node_name=%s, is_required=%s WHERE id=%s",
                    (data.node_key, data.node_name, data.is_required, data.id),
                )
                return _envelope(None, None, '已更新')
            else:
                idx = data.node_index
                if not idx:
                    maxr = db.execute_query("SELECT MAX(node_index) m FROM dev_process_node_def")[0]['m']
                    idx = (maxr or 0) + 1
                db.execute_update(
                    "INSERT INTO dev_process_node_def (node_index, node_key, node_name, is_required, sort) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (idx, data.node_key, data.node_name, data.is_required, idx),
                )
                return _envelope(None, None, '已新增')
    except Exception as e:
        return _envelope(None, None, f'保存失败: {e}', 500)


@router.post('/nodeDef/delete')
async def node_def_delete(data: NodeDefDeleteReq):
    try:
        with get_db_handler() as db:
            ref = db.execute_query(
                "SELECT COUNT(*) c FROM dev_process_node WHERE node_index IN "
                "(SELECT node_index FROM dev_process_node_def WHERE id=%s) AND is_del=0",
                (data.id,),
            )[0]['c']
            if ref:
                return _envelope(None, None, '已有流程使用该节点定义，无法删除', 400)
            db.execute_update("DELETE FROM dev_process_node_def WHERE id=%s", (data.id,))
            return _envelope(None, None, '已删除')
    except Exception as e:
        return _envelope(None, None, f'删除失败: {e}', 500)


# ---------- 功能点：F12 ----------
@router.get('/functionPoints')
async def function_points_list(pageNo: int = 1, pageSize: int = 20, keyword: str = '', project_id: str = ''):
    try:
        with get_db_handler() as db:
            conds = ['fp.is_del=0']
            args = []
            if keyword:
                conds.append('fp.name LIKE %s')
                args.append(f'%{keyword}%')
            if project_id not in ('', None):
                conds.append('fp.project_id=%s')
                args.append(int(project_id))
            where = ' AND '.join(conds)
            total = db.execute_query(f"SELECT COUNT(*) c FROM function_points fp WHERE {where}", tuple(args))[0]['c']
            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                f"""SELECT fp.*, (SELECT COUNT(*) FROM test_cases tc WHERE tc.function_point_id=fp.id AND tc.is_del=0) AS case_count,
                           (SELECT cate_name FROM test_case_project p WHERE p.id=fp.project_id) AS project_name
                    FROM function_points fp WHERE {where} ORDER BY fp.id DESC LIMIT %s OFFSET %s""",
                tuple(args + [pageSize, offset]),
            )
            return _envelope(rows, total)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.post('/functionPoints/save')
async def function_points_save(data: FunctionPointSaveReq):
    try:
        with get_db_handler() as db:
            if data.id:
                db.execute_update(
                    "UPDATE function_points SET name=%s, project_id=%s, module=%s, description=%s, baseline=%s WHERE id=%s",
                    (data.name, data.project_id, data.module, data.description, data.baseline, data.id),
                )
                return _envelope(None, None, '已更新')
            db.execute_update(
                "INSERT INTO function_points (name, project_id, module, description, baseline) VALUES (%s,%s,%s,%s,%s)",
                (data.name, data.project_id, data.module, data.description, data.baseline),
            )
            return _envelope(None, None, '已新增')
    except Exception as e:
        return _envelope(None, None, f'保存失败: {e}', 500)


@router.post('/functionPoints/delete')
async def function_points_delete(data: FunctionPointDeleteReq):
    try:
        with get_db_handler() as db:
            ref = db.execute_query(
                "SELECT COUNT(*) c FROM test_cases WHERE function_point_id=%s AND is_del=0", (data.id,)
            )[0]['c'] + db.execute_query(
                "SELECT COUNT(*) c FROM change_function_points WHERE function_point_id=%s", (data.id,)
            )[0]['c']
            if ref:
                return _envelope(None, None, '该功能点已被用例或变更引用，无法删除', 400)
            db.execute_update("UPDATE function_points SET is_del=1 WHERE id=%s", (data.id,))
            return _envelope(None, None, '已删除')
    except Exception as e:
        return _envelope(None, None, f'删除失败: {e}', 500)


# ---------- 需求变更 + 影响分析：F13 / F14 ----------
@router.get('/changes')
async def changes_list(pageNo: int = 1, pageSize: int = 20, project_id: str = '', status: str = ''):
    try:
        with get_db_handler() as db:
            conds = []
            args = []
            if project_id not in ('', None):
                conds.append('rc.project_id=%s')
                args.append(int(project_id))
            if status != '':
                conds.append('rc.status=%s')
                args.append(int(status))
            where = (' WHERE ' + ' AND '.join(conds)) if conds else ''
            total = db.execute_query(f"SELECT COUNT(*) c FROM requirement_changes rc {where}", tuple(args))[0]['c']
            offset = (pageNo - 1) * pageSize
            rows = db.execute_query(
                f"""SELECT rc.*, u.realname AS creator_name,
                           (SELECT COUNT(*) FROM change_function_points cfp WHERE cfp.change_id=rc.id) AS fp_count,
                           (SELECT COUNT(*) FROM change_case_links ccl WHERE ccl.change_id=rc.id) AS link_count
                    FROM requirement_changes rc LEFT JOIN users u ON u.id=rc.creator_id
                    {where} ORDER BY rc.create_time DESC LIMIT %s OFFSET %s""",
                tuple(args + [pageSize, offset]),
            )
            return _envelope(rows, total)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.post('/changes/save')
async def changes_save(data: ChangeSaveReq, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            if data.id:
                db.execute_update(
                    "UPDATE requirement_changes SET title=%s, project_id=%s, change_type=%s, description=%s WHERE id=%s",
                    (data.title, data.project_id, data.change_type, data.description, data.id),
                )
                cid = data.id
                # 重新计算影响：先清旧 links + cfp
                db.execute_update("DELETE FROM change_case_links WHERE change_id=%s", (cid,))
                db.execute_update("DELETE FROM change_function_points WHERE change_id=%s", (cid,))
            else:
                db.execute_update(
                    "INSERT INTO requirement_changes (title, project_id, change_type, description, creator_id, status) "
                    "VALUES (%s,%s,%s,%s,%s,0)",
                    (data.title, data.project_id, data.change_type, data.description, data.creator_id or uid),
                )
                cid = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
            # 涉及功能点
            involved = []  # (fp_id, is_new)
            for fpid in data.function_point_ids:
                db.execute_update(
                    "INSERT INTO change_function_points (change_id, function_point_id, is_new) VALUES (%s,%s,0)",
                    (cid, fpid),
                )
                involved.append((fpid, 0))
            for nf in data.new_function_points:
                db.execute_update(
                    "INSERT INTO function_points (name, project_id, module) VALUES (%s,%s,%s)",
                    (nf.get('name'), nf.get('project_id'), nf.get('module', '')),
                )
                nfid = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
                db.execute_update(
                    "INSERT INTO change_function_points (change_id, function_point_id, is_new) VALUES (%s,%s,1)",
                    (cid, nfid),
                )
                involved.append((nfid, 1))
            # 半自动影响分析：生成候选处置链接
            for fpid, is_new in involved:
                if is_new:
                    # 新增功能点：无存量用例，建议新增
                    db.execute_update(
                        "INSERT INTO change_case_links (change_id, function_point_id, case_id, disposition, note) "
                        "VALUES (%s,%s,NULL,2,'新增功能点，建议补充测试用例')",
                        (cid, fpid),
                    )
                else:
                    cases = db.execute_query(
                        "SELECT id FROM test_cases WHERE function_point_id=%s AND is_del=0", (fpid,)
                    )
                    if cases:
                        for c in cases:
                            db.execute_update(
                                "INSERT INTO change_case_links (change_id, function_point_id, case_id, disposition) "
                                "VALUES (%s,%s,%s,0)",
                                (cid, fpid, c['id']),
                            )
                    else:
                        db.execute_update(
                            "INSERT INTO change_case_links (change_id, function_point_id, case_id, disposition, note) "
                            "VALUES (%s,%s,NULL,2,'改动功能点但暂无用例覆盖，建议补充')",
                            (cid, fpid),
                        )
            db.execute_update("UPDATE requirement_changes SET status=1 WHERE id=%s", (cid,))
            return _envelope({'id': cid}, None, '已保存并生成影响分析')
    except Exception as e:
        return _envelope(None, None, f'保存失败: {e}', 500)


@router.get('/changes/detail')
async def changes_detail(change_id: int):
    try:
        with get_db_handler() as db:
            rc = db.execute_single_query("SELECT * FROM requirement_changes WHERE id=%s", (change_id,))
            if not rc:
                return _envelope(None, None, '变更不存在', 404)
            fps = db.execute_query(
                """SELECT cfp.id, cfp.function_point_id, cfp.is_new, fp.name, fp.project_id
                   FROM change_function_points cfp LEFT JOIN function_points fp ON fp.id=cfp.function_point_id
                   WHERE cfp.change_id=%s""",
                (change_id,),
            )
            links = db.execute_query(
                """SELECT ccl.*,
                          CONCAT_WS(' / ', NULLIF(tc.module,''), NULLIF(tc.function_point,'')) AS case_name,
                          tc.module, tc.function_point, fp.name AS fp_name
                   FROM change_case_links ccl
                   LEFT JOIN test_cases tc ON tc.id=ccl.case_id
                   LEFT JOIN function_points fp ON fp.id=ccl.function_point_id
                   WHERE ccl.change_id=%s ORDER BY ccl.function_point_id, ccl.case_id""",
                (change_id,),
            )
            return _envelope({'change': rc, 'fps': fps, 'links': links})
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.post('/changes/link/disposition')
async def link_disposition(data: LinkDispositionReq, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            link_id = data.link_id
            if not link_id and data.change_id and (data.function_point_id or data.case_id):
                # 按 change+fp+case 定位（case 可为空，用于新增建议行）
                row = db.execute_single_query(
                    "SELECT id FROM change_case_links WHERE change_id=%s AND function_point_id=%s AND "
                    "(case_id=%s OR (case_id IS NULL AND %s=1))",
                    (data.change_id, data.function_point_id, data.case_id, 1 if not data.case_id else 0),
                )
                link_id = row['id'] if row else None
            if not link_id:
                return _envelope(None, None, '处置记录不存在', 404)
            db.execute_update(
                "UPDATE change_case_links SET disposition=%s, note=%s, handled_by=%s, handled_at=NOW() WHERE id=%s",
                (data.disposition, data.note, data.handled_by or uid, link_id),
            )
            return _envelope(None, None, '已记录处置')
    except Exception as e:
        return _envelope(None, None, f'更新失败: {e}', 500)


# ---------- F10：节点关联引用（用例/缺陷/需求变更 一键跳转） ----------
@router.post('/node/link/add')
async def node_link_add(data: NodeLinkAddReq, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            db.execute_update(
                "INSERT INTO dev_process_node_links (process_id, node_index, ref_type, ref_id, ref_title, note, create_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (data.process_id, data.node_index, data.ref_type, data.ref_id,
                 data.ref_title, data.note, uid),
            )
            return _envelope(None, None, '已关联')
    except Exception as e:
        return _envelope(None, None, f'关联失败: {e}', 500)


@router.post('/node/link/delete')
async def node_link_delete(data: NodeLinkDeleteReq):
    try:
        with get_db_handler() as db:
            db.execute_update("UPDATE dev_process_node_links SET is_del=1 WHERE id=%s", (data.id,))
            return _envelope(None, None, '已移除')
    except Exception as e:
        return _envelope(None, None, f'移除失败: {e}', 500)


@router.get('/refs/search')
async def refs_search(ref_type: int = 1, keyword: str = ''):
    """F10：按类型搜索可关联的引用对象（用例/任务/变更/需求分析）。"""
    try:
        with get_db_handler() as db:
            kw = f'%{keyword}%'
            if ref_type == 1:
                # test_cases 表无标题列，用例标题由「所属模块 / 测试功能点」组合
                rows = db.execute_query(
                    "SELECT id, CONCAT_WS(' / ', NULLIF(module,''), NULLIF(function_point,'')) AS title "
                    "FROM test_cases WHERE is_del=0 "
                    "AND (module LIKE %s OR function_point LIKE %s) "
                    "ORDER BY id DESC LIMIT 50", (kw, kw))
            elif ref_type == 2:
                rows = db.execute_query(
                    "SELECT id, task_name AS title FROM task_management WHERE is_del=0 AND task_name LIKE %s "
                    "ORDER BY id DESC LIMIT 50", (kw,))
            elif ref_type == 4:
                # 需求分析（AI 工作台的需求文档）
                rows = db.execute_query(
                    "SELECT id, title FROM ai_requirements WHERE is_del=0 AND title LIKE %s "
                    "ORDER BY id DESC LIMIT 50", (kw,))
            else:
                rows = db.execute_query(
                    "SELECT id, title FROM requirement_changes WHERE title LIKE %s "
                    "ORDER BY id DESC LIMIT 50", (kw,))
            return _envelope(rows)
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


# ---------- 附件：上传 / 列表 / 下载 / 删除 ----------
@router.post('/attachment/upload')
async def attachment_upload(request: Request, file: UploadFile = File(...),
                            process_id: int = Form(...), node_index: int = Form(...),
                            note: str = Form('')):
    """把文件上传并挂到某个流程节点上（关联引用 → 上传文件）。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            if not _can_access_process(db, uid, process_id):
                return _envelope(None, None, '无权访问该流程', 403)
            node = db.execute_single_query(
                "SELECT id FROM dev_process_node WHERE process_id=%s AND node_index=%s AND is_del=0",
                (process_id, node_index),
            )
            if not node:
                return _envelope(None, None, '节点不存在', 404)

            raw = await file.read()
            if not raw:
                return _envelope(None, None, '文件内容为空', 400)
            limit = MAX_UPLOAD_MB * 1024 * 1024
            if len(raw) > limit:
                return _envelope(None, None, f'文件超过 {MAX_UPLOAD_MB}MB 上限', 400)

            origin = _safe_file_name(file.filename or '')
            ext = os.path.splitext(origin)[1].lower()
            if ext not in ALLOWED_EXT:
                return _envelope(None, None, f'不支持的文件类型：{ext or "无扩展名"}', 400)

            rel_dir = time.strftime('%Y%m%d')
            abs_dir = os.path.join(UPLOAD_ROOT, rel_dir)
            try:
                os.makedirs(abs_dir, exist_ok=True)
            except OSError as e:
                return _envelope(None, None, f'创建存储目录失败：{e}', 500)
            store_name = uuid.uuid4().hex + ext
            abs_path = os.path.join(abs_dir, store_name)
            try:
                with open(abs_path, 'wb') as fh:
                    fh.write(raw)
            except OSError as e:
                return _envelope(None, None, f'写入文件失败：{e}', 500)
            rel_path = f'{rel_dir}/{store_name}'

            ctype = file.content_type or mimetypes.guess_type(origin)[0] or 'application/octet-stream'
            new_id = _insert_id(
                db,
                "INSERT INTO dev_process_attachments (process_id, node_index, file_name, file_path, "
                "file_size, file_ext, content_type, note, create_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (process_id, node_index, origin, rel_path, len(raw), ext.lstrip('.'),
                 ctype[:150], (note or '')[:300], uid),
            )
            return _envelope({'id': new_id, 'file_name': origin, 'file_size': len(raw),
                              'file_ext': ext.lstrip('.'), 'node_index': node_index,
                              'process_id': process_id, 'note': (note or '')[:300]},
                             None, '已上传')
    except Exception as e:
        return _envelope(None, None, f'上传失败: {e}', 500)


@router.get('/attachment/list')
async def attachment_list(process_id: int, node_index: Optional[int] = None, request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            if not _can_access_process(db, uid, process_id):
                return _envelope(None, None, '无权访问该流程', 403)
            sql = ("SELECT a.*, u.realname AS uploader_name FROM dev_process_attachments a "
                   "LEFT JOIN users u ON u.id = a.create_by WHERE a.process_id=%s AND a.is_del=0")
            args = [process_id]
            if node_index is not None:
                sql += " AND a.node_index=%s"
                args.append(node_index)
            sql += " ORDER BY a.node_index, a.id"
            return _envelope(db.execute_query(sql, tuple(args)))
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)


@router.get('/attachment/download')
async def attachment_download(id: int, request: Request = None):
    """按 id 下载附件（鉴权与行级权限同上传）。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT * FROM dev_process_attachments WHERE id=%s AND is_del=0", (id,))
            if not row:
                return _envelope(None, None, '附件不存在', 404)
            if not _can_access_process(db, uid, row['process_id']):
                return _envelope(None, None, '无权访问该流程', 403)
            abs_path = _abs_store_path(row['file_path'])
            if not abs_path or not os.path.isfile(abs_path):
                return _envelope(None, None, '附件文件已丢失', 404)
            return FileResponse(
                abs_path,
                media_type=row['content_type'] or 'application/octet-stream',
                filename=row['file_name'] or 'attachment',
            )
    except Exception as e:
        return _envelope(None, None, f'下载失败: {e}', 500)


@router.post('/attachment/delete')
async def attachment_delete(data: AttachmentDeleteReq, request: Request = None):
    """解除附件关联（软删；磁盘文件保留，避免误删不可恢复）。"""
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            row = db.execute_single_query(
                "SELECT * FROM dev_process_attachments WHERE id=%s AND is_del=0", (data.id,))
            if not row:
                return _envelope(None, None, '附件不存在', 404)
            if not _can_access_process(db, uid, row['process_id']):
                return _envelope(None, None, '无权访问该流程', 403)
            db.execute_update("UPDATE dev_process_attachments SET is_del=1 WHERE id=%s", (data.id,))
            return _envelope(None, None, '已移除')
    except Exception as e:
        return _envelope(None, None, f'移除失败: {e}', 500)


# ---------- F11：统计报表 ----------
@router.get('/report')
async def process_report(request: Request = None):
    try:
        uid = _uid(request)
        with get_db_handler() as db:
            # 行级权限：非 admin 只统计自己相关流程
            cond, cargs = [], []
            if not _is_admin(db, uid):
                cond.append(
                    '(dp.creator_id=%s OR EXISTS (SELECT 1 FROM dev_process_node n '
                    'WHERE n.process_id=dp.id AND n.assignee_id=%s AND n.is_del=0))')
                cargs.extend([uid, uid])
            where = (' WHERE ' + ' AND '.join(cond)) if cond else ''
            scope_join = (' AND ' + ' AND '.join(cond)) if cond else ''

            # 总览
            ov = db.execute_query(
                f"SELECT COUNT(*) total, SUM(status=0) in_progress, SUM(status=1) completed, "
                f"SUM(status=2) `terminated` FROM dev_process dp {where}", tuple(cargs))[0]
            total = ov['total'] or 0
            completed = ov['completed'] or 0
            archive_rate = round(float(completed) / float(total) * 100) if total else 0

            # 按时归档率（仅统计已设计划日期且已归档的）
            ot = db.execute_query(
                f"SELECT COUNT(*) planned_cnt, SUM(dp.completed_at <= dp.planned_finish_at) on_time_cnt "
                f"FROM dev_process dp WHERE 1=1 {scope_join} AND dp.status=1 AND dp.completed_at IS NOT NULL "
                f"AND dp.planned_finish_at IS NOT NULL", tuple(cargs))[0]
            planned_cnt = ot['planned_cnt'] or 0
            on_time_cnt = ot['on_time_cnt'] or 0
            on_time_rate = round(float(on_time_cnt) / float(planned_cnt) * 100) if planned_cnt else None

            # 各节点平均停留时长（已完成节点 started->finished）
            avg_rows = db.execute_query(
                f"""SELECT n.node_index, d.node_name, COUNT(*) cnt,
                           AVG(TIMESTAMPDIFF(SECOND, n.started_at, n.finished_at)) avg_sec
                    FROM dev_process_node n
                    JOIN dev_process dp ON dp.id=n.process_id
                    LEFT JOIN dev_process_node_def d ON d.node_index=n.node_index
                    WHERE n.status=2 AND n.started_at IS NOT NULL AND n.finished_at IS NOT NULL AND n.is_del=0
                          {scope_join}
                    GROUP BY n.node_index, d.node_name
                    ORDER BY avg_sec DESC""",
                tuple(cargs))
            node_stay = []
            for r in avg_rows:
                node_stay.append({
                    'node_index': r['node_index'],
                    'node_name': r['node_name'] or ('节点' + str(r['node_index'])),
                    'count': r['cnt'],
                    'avg_hours': round(float(r['avg_sec'] or 0) / 3600.0, 1),
                })

            # 卡点 Top5（平均停留最长）
            bottleneck = node_stay[:5]

            # 停留最久的未完成流程 Top5
            stuck = db.execute_query(
                f"""SELECT dp.id, dp.code, dp.title, n.node_index, d.node_name,
                           TIMESTAMPDIFF(DAY, n.started_at, NOW()) stuck_days, n.started_at
                    FROM dev_process dp
                    JOIN dev_process_node n ON n.process_id=dp.id AND n.node_index=dp.current_node
                    LEFT JOIN dev_process_node_def d ON d.node_index=n.node_index
                    WHERE dp.status=0 AND n.status=1 AND n.started_at IS NOT NULL
                          {scope_join}
                    ORDER BY stuck_days DESC LIMIT 5""",
                tuple(cargs))

            return _envelope({
                'overview': {
                    'total': total, 'in_progress': ov['in_progress'] or 0,
                    'completed': completed, 'terminated': ov['terminated'] or 0,
                    'archive_rate': archive_rate, 'on_time_rate': on_time_rate,
                    'planned_cnt': planned_cnt,
                },
                'node_stay': node_stay,
                'bottleneck': bottleneck,
                'stuck': stuck,
            })
    except Exception as e:
        return _envelope(None, None, f'查询失败: {e}', 500)
