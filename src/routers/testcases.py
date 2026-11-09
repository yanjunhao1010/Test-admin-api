import json
import os
from fastapi import APIRouter,HTTPException,Request
from src.comment import MySqlconection,yaml_data
from pydantic import BaseModel
from typing import List, Optional
from .auth import parse_token

router = APIRouter(
    prefix='/testcases',
    tags=['testcases'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(data['mysql']['host'],data['mysql']['user'], data['mysql']['password'], data['mysql']['database'])
    return db


# 进程内只尝试一次，避免每次建用例都跑 ALTER
_creator_column_ensured = False


def ensure_creator_column():
    """兼容老库：若 test_cases 缺少 creator_id 字段则自动补齐（幂等）。"""
    global _creator_column_ensured
    if _creator_column_ensured:
        return
    try:
        with get_db_handler() as db:
            db.execute_update(
                "ALTER TABLE test_cases ADD COLUMN creator_id INT NULL COMMENT '创建者用户ID'"
            )
        _creator_column_ensured = True
        print('已为 test_cases 表新增 creator_id 字段')
    except Exception as e:
        # 字段已存在（1060）或无 ALTER 权限时忽略，由人工建表兜底
        _creator_column_ensured = True
        print(f'test_cases.creator_id 已存在或跳过: {e}')


def _resolve_creator_id(request: Request):
    """从登录 token 解析当前用户 uid 作为创建者；解析失败则返回 None。"""
    token = request.headers.get('accessToken')
    if not token:
        return None
    try:
        return parse_token(token)
    except Exception:
        return None


def _parse_id_list(raw):
    """把 '8,9' 解析成 [8, 9]；忽略非数字项，避免把用户输入拼进 SQL"""
    ids = []
    for part in str(raw or '').split(','):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


"""获取用例列表接口"""
@router.get('/testcaseList')
async def testcaseList(pageNo: int = 1, pageSize: int = 20, project_ids: str = '', title: str = ''):
    """project_ids: 逗号分隔的项目 id 列表（含子项目，由左侧项目栏算出）；为空表示不过滤。
    title: 关键字搜索（用例编号 id 精确 / 模块 / 功能点 / 项目名），为空表示不过滤。"""
    try:
        with get_db_handler() as db:
            project_id_list = _parse_id_list(project_ids)
            cond = ''
            cond_args = []
            if project_id_list:
                placeholders = ','.join(['%s'] * len(project_id_list))
                cond = f" AND tc.project_id IN ({placeholders})"
                cond_args = list(project_id_list)
            # 关键字搜索：用例编号(id 精确) / 模块 / 功能点 / 项目名(cate_name)
            t = (title or '').strip()
            if t:
                t_like = f'%{t}%'
                t_parts = [
                    'tc.module LIKE %s',
                    'tc.function_point LIKE %s',
                    'tcp.cate_name LIKE %s',
                ]
                t_args = [t_like, t_like, t_like]
                if t.isdigit():
                    t_parts.append('tc.id = %s')
                    t_args.append(int(t))
                cond += ' AND (' + ' OR '.join(t_parts) + ')'
                cond_args.extend(t_args)

            count_query = (
                "SELECT COUNT(*) as total FROM test_cases tc "
                "LEFT JOIN test_case_project tcp ON tc.project_id = tcp.id "
                "where tc.`status` = 1 and tc.is_del = 0" + cond
            )
            total_result = (
                db.execute_query(count_query, tuple(cond_args))
                if cond_args else db.execute_query(count_query)
            )
            total = total_result[0]['total'] if total_result else 0
            # 计算偏移量
            offset = (pageNo - 1) * pageSize
            query = """
                SELECT 
                    tc.id, 
                    tcp.cate_name AS project,
                    tc.project_id,
                    tc.module, 
                    tc.function_point,
                    tc.priority,
                    tc.pre_condition, 
                    tc.steps,
                    tc.expected_result,
                    tc.remark, 
                    tc.create_time,
                    tc.update_time,
                    tc.ai_generated,
                    tc.ai_draft_id
                FROM test_cases tc
                LEFT JOIN test_case_project tcp ON tc.project_id = tcp.id
                where tc.`status` = 1 and tc.is_del = 0
            """ + cond + " ORDER BY tc.create_time DESC LIMIT %s OFFSET %s"
            testcaseList = db.execute_query(query, tuple(cond_args + [pageSize, offset]))

        return {
            "code": 200,
            "msg": "success",
            "total": total,
            "data": testcaseList
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")


"""F5 相似用例查重请求参数"""
class CaseDupCheckReq(BaseModel):
    project_id: Optional[int] = None
    function_point: str = ''
    pre_condition: str = ''
    steps: str = ''


@router.post('/check-duplicate')
async def check_duplicate(data: CaseDupCheckReq):
    """F5 相似用例查重：按 功能点 + 前置条件 + 步骤(LIKE) 同项目匹配已存在用例。

    口径（已拍板）：同 project_id 下，function_point / pre_condition / steps 三者均以 LIKE 模糊命中
    即视为「相似候选」。功能点为空不查（无意义）。返回命中列表与总数，供添加弹窗给出告警。
    """
    try:
        if not data.function_point:
            return {'code': 200, 'msg': 'success', 'data': {'total': 0, 'duplicates': []}}
        with get_db_handler() as db:
            conds = ['tc.is_del = 0', 'tc.`status` = 1']
            params: list = []
            if data.project_id:
                conds.append('tc.project_id = %s')
                params.append(data.project_id)
            conds.append('tc.function_point LIKE %s')
            params.append('%%%s%%' % data.function_point)
            if data.pre_condition:
                conds.append('tc.pre_condition LIKE %s')
                params.append('%%%s%%' % data.pre_condition)
            if data.steps:
                conds.append('tc.steps LIKE %s')
                params.append('%%%s%%' % data.steps)
            where = ' AND '.join(conds)
            rows = db.execute_query(
                'SELECT tc.id, tc.module, tc.function_point, tc.priority, '
                'tc.pre_condition, tc.steps, tc.expected_result '
                'FROM test_cases tc WHERE ' + where + ' ORDER BY tc.id DESC LIMIT 20',
                tuple(params),
            )
            return {
                'code': 200,
                'msg': 'success',
                'data': {'total': len(rows), 'duplicates': rows},
            }
    except Exception as e:  # noqa: BLE001
        return {'code': 500, 'msg': '查重失败：%s' % e, 'data': {'total': 0, 'duplicates': []}}


"""以下是根据id获取用例详情接口"""
@router.get("/detail")
async def detail(id):
    try:
        with get_db_handler() as db:
            query = """
                    SELECT 
                        tc.id, 
                        tcp.cate_name AS project,
                        tc.project_id,
                        tc.module, 
                        tc.function_point,
                        tc.priority,
                        tc.pre_condition, 
                        tc.steps,
                        tc.expected_result,
                        tc.remark, 
                        tc.create_time,
                        tc.update_time 
                    FROM test_cases tc
                    LEFT JOIN test_case_project tcp ON tc.project_id = tcp.id 
                    WHERE tc.id = %s
            """
        detail = db.execute_single_query(query, (id,))
        return {
            "code": 200,
            "msg": "success",
            "data": detail
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")

"""单个请求参数格式"""
class TestCaseCreate(BaseModel):
    ##project: str
    project_id: int
    module: str
    function_point: str
    priority: int
    pre_condition: str
    steps: str
    expected_result: str
    remark: Optional[str] = None

"""批量请求参数"""
class BatchTestCaseRequest(BaseModel):
    test_cases: List[TestCaseCreate]

@router.post("/add")
async def create(request: Request, data: TestCaseCreate):
    # 调试信息
    print("=== 后端接收数据调试 ===")
    print(f"请求头: {dict(request.headers)}")
    print(f"接收到的数据: {data}")
    print(f"数据字典: {data.dict()}")
    print(f"数据类型: {type(data)}")

    try:
        # 创建者：取当前登录用户（token 中的 uid），前端无需传递，更可靠
        creator_id = _resolve_creator_id(request)

        with get_db_handler() as db:
            ensure_creator_column()
            query = """
              INSERT INTO test_cases (
                    project_id, 
                    module, 
                    function_point, 
                    priority, 
                    pre_condition, 
                    steps, 
                    expected_result, 
                    remark, 
                    creator_id,
                    status, 
                    is_del
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)  
                        """
            params = (
                data.project_id,
                data.module,
                data.function_point,
                data.priority,
                data.pre_condition,
                data.steps,
                data.expected_result,
                data.remark,
                creator_id,
                1,  # status
                0,  # is_del
            )
            db.execute_update(query,params)
        return {
            "code": 200,
            "msg": "success",
            "data": "用例新增成功"

        }

    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")


"""批量新增测试用例"""
@router.post("/adds")
async def adds(request: Request,data:BatchTestCaseRequest):
    # 调试信息
    print("=== 后端接收数据调试 ===")
    print(f"请求头: {dict(request.headers)}")
    print(f"接收到的数据: {data}")
    print(f"数据类型: {type(data)}")
    print(f"用例数量: {len(data.test_cases)}")

    try:
        # 创建者：取当前登录用户（token 中的 uid），批量导入同样归属同一人
        creator_id = _resolve_creator_id(request)

        with get_db_handler() as db:
            ensure_creator_column()
            query = """
              INSERT INTO test_cases (
                    project_id,
                    module,
                    function_point,
                    priority,
                    pre_condition,
                    steps,
                    expected_result,
                    remark,
                    creator_id,
                    status,
                    is_del
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """
            params_list = []
            for case in data.test_cases:
                params = (
                    case.project_id,
                    case.module,
                    case.function_point,
                    case.priority,
                    case.pre_condition,
                    case.steps,
                    case.expected_result,
                    case.remark,
                    creator_id,
                    1,  # status
                    0,  # is_del
                )
                params_list.append(params)
                # print(params)
                print(params_list)
                if params_list:
                    print(f"准备插入用例: {case.function_point}")
                    db.execute_update(query, params)
                success_count = len(params_list)

        return {
            "code": 200,
            "msg": "success",
            "data": {
                "detail": f"成功批量新增 {success_count} 个用例",
                "count": success_count
            }
        }

    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")



"""编辑测试用例"""
class TestCaseCreateUpdate(BaseModel):
    id: int
    project_id: int
    module: str
    function_point: str
    priority: int
    pre_condition: str
    steps: str
    expected_result: str
    remark: Optional[str] = None

@router.post("/update")
async def update(data: TestCaseCreateUpdate):
    try:
        with get_db_handler() as db:
            query = """ 
                UPDATE test_cases 
                SET 
                    project_id = %s, 
                    module = %s, 
                    function_point = %s, 
                    priority = %s, 
                    pre_condition = %s, 
                    steps = %s, 
                    expected_result = %s, 
                    remark = %s, 
                    status = %s, 
                    is_del = %s,
                    update_time = NOW()
                WHERE id = %s
            """
            params = (
                data.project_id,
                data.module,
                data.function_point,
                data.priority,
                data.pre_condition,
                data.steps,
                data.expected_result,
                data.remark,
                1,  # status
                0,  # is_del
                data.id
            )
            db.execute_update(query, params)
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "detail": "更新成功"
            }
        }

    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")

"""删除测试用例"""
class TestCaseDelete(BaseModel):
    ids: list[int]
@router.post("/delete")
async def delete(data:TestCaseDelete):
    print("接收到的数据:", data)
    try:
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(data.ids))
            print(placeholders)
            print(data.ids)
            query = f""" 
                        UPDATE test_cases 
                        SET 
                            status = 0, 
                            is_del = 1           
                        WHERE id IN ({placeholders})
                        """
            params = tuple(data.ids)
            affected_rows = db.execute_update(query, params)
            return {
                "code": 200,
                "msg": "success",
                "data": "成功删除{}条记录".format(affected_rows)
            }
    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")

"""查询用例包列表"""
@router.get("/package")
async def package(pageNo: int = 1,pageSize: int = 20):
    try:
        with get_db_handler() as db:
            count_query = "SELECT COUNT(*) as total FROM case_package where `status` = 1 and is_del = 0"
            total_result = db.execute_query(count_query)
            total = total_result[0]['total'] if total_result else 0
            # 计算偏移量
            offset = (pageNo - 1) * pageSize
            query = """
                SELECT
                id,
                name,
                package_case_content,
                JSON_LENGTH(package_case_content) as case_count,
                (SELECT COUNT(*) FROM test_cases tc
                 WHERE tc.is_del = 0 AND tc.`status` = 1
                 AND JSON_CONTAINS(cp.package_case_content, CAST(tc.id AS JSON))) as valid_count,
                create_time,
                update_time
                FROM case_package cp
                where cp.`status` = 1 and cp.is_del = 0 ORDER BY cp.create_time DESC LIMIT %s OFFSET %s
            """
            packageList = db.execute_query(query, (pageSize,offset))

        return {
            "code": 200,
            "msg": "success",
            "total": total,
            "data": packageList
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")


"""打包测试用例"""
class packageCreate(BaseModel):
    tc_pack_name:str
    case_ids:list[int]

@router.post("/package/add")
async def package_add(data: packageCreate):
    case_ids_json = json.dumps(data.case_ids)
    try:
        with get_db_handler() as db:
            query = """      
                   INSERT INTO case_package (
                        name, 
                        package_case_content,
                        status, 
                        is_del) 
                    VALUES (%s,%s,%s,%s);
                                   """
            params = (
                data.tc_pack_name,
                case_ids_json,
                1,  # status
                0,  # is_del
            )
            db.execute_update(query, params)
            print(params)
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "detail": "打包成功"
            }
        }

    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")


"""查询包用例详情接口"""
@router.get("/package/detail")
async def package_detail(id: int):
    try:
        with get_db_handler() as db:
            query1 = """ 
                    SELECT package_case_content,name,create_time,update_time FROM case_package where id = %s
                    """
            case_id_str = db.execute_query(query1, (id,))
            case_id_lists = json.loads(case_id_str[0]['package_case_content'])
            total_num = len(case_id_lists)
            package_name = case_id_str[0]['name']
            package_create_time = case_id_str[0]['create_time']
            package_update_time= case_id_str[0]['update_time']
            placeholders = ','.join(['%s'] * len(case_id_lists))
            query2 = f""" 
               SELECT               
                    tc.id, 
                    tcp.cate_name as project,
                    tc.module, 
                    tc.function_point,
                    tc.priority,
                    tc.pre_condition, 
                    tc.steps,
                    tc.expected_result,
                    tc.remark,
                    tc.status,
                    tc.create_time,
                    tc.update_time
                    FROM test_cases tc 
                    LEFT JOIN
                    test_case_project tcp
                    ON
                    tc.project_id=tcp.id
                    WHERE tc.`status` = 1 and tc.is_del = 0 and tc.id 
                    in({placeholders})
                 """
            param = tuple(case_id_lists)
            testcaseList = db.execute_query(query2, param)
        return {
            "code": 200,
            "msg": "success",
            "total": total_num,
            "package_name": package_name,
            "package_create_time": package_create_time,
            "package_update_time": package_update_time,
            "data": testcaseList
        }
    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")


"""批量获取用例简要信息（打包弹窗展示：项目/模块/功能点/优先级）"""
@router.get("/batchBrief")
async def batch_brief(ids: str = ""):
    try:
        id_list = [int(x) for x in ids.split(',') if str(x).strip()]
        if not id_list:
            return {"code": 200, "msg": "success", "data": []}
        with get_db_handler() as db:
            placeholders = ','.join(['%s'] * len(id_list))
            query = f"""
                SELECT
                    tc.id,
                    tcp.cate_name as project,
                    tc.module,
                    tc.function_point,
                    tc.priority
                FROM test_cases tc
                LEFT JOIN test_case_project tcp ON tc.project_id = tcp.id
                WHERE tc.id IN ({placeholders})
            """
            rows = db.execute_query(query, tuple(id_list))
        return {
            "code": 200,
            "msg": "success",
            "data": rows
        }
    except Exception as e:
        print(f"错误详情: {str(e)}")
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")


"""逻辑删除包"""
@router.get("/package/delete")
async def package_delete(id: int):
    try:
        with get_db_handler() as db:
            query1 = f""" 
                    UPDATE case_package 
                    SET 
                      is_del=1
                    WHERE id = {id}
                """
            db.execute_update(query1)
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "msg": "删除成功！",
            }
        }
    except Exception as e:
        print(e)

"""查询项目分类列表接口"""
@router.get("/category/project")
async def test_case_project(status: int = 1):
    try:
        # status: 1=仅启用(默认,兼容旧调用) 0=仅禁用 其它值(如 -1)=全部(含启用+禁用)
        if status in (0, 1):
            s_filter = f" AND `status` = {status}"
            s_filter_p = f" AND p1.`status` = {status}"
            s_filter_c = f" AND p2.`status` = {status}"
            s_filter_g = f" AND p3.`status` = {status}"
        else:
            s_filter = s_filter_p = s_filter_c = s_filter_g = ""
        with get_db_handler() as db:
            count_query = f"SELECT COUNT(*) as total FROM test_case_project where is_del = 0 and pid=0{s_filter}"
            total_result = db.execute_query(count_query)
            total = total_result[0]['total'] if total_result else 0
            query1 = f""" 
                     SELECT 
                        JSON_ARRAYAGG(
                            JSON_OBJECT(
                                'id', p1.id,
                                'pid',p1.pid,
                                'name', p1.cate_name,
                                'level', p1.level,
                                'type',p1.type,
                                'status', p1.status,
                                'case_count', (SELECT COUNT(*) FROM test_cases tc
                                               WHERE tc.project_id IN (
                                                   SELECT p1.id
                                                   UNION ALL SELECT p2.id FROM test_case_project p2 WHERE p2.pid = p1.id AND p2.is_del = 0
                                                   UNION ALL SELECT p3.id FROM test_case_project p2 JOIN test_case_project p3 ON p3.pid = p2.id WHERE p2.pid = p1.id AND p2.is_del = 0 AND p3.is_del = 0
                                               ) AND tc.is_del = 0 AND tc.status = 1),
                                'create_time', p1.create_time,
                                'update_time', p1.update_time,
                                'planned_start', p1.planned_start,
                                'planned_end', p1.planned_end,
                                'planned_case_cnt', p1.planned_case_cnt,
                                'milestone', p1.milestone,
                                'children', COALESCE(
                                    (
                                        SELECT 
                                            JSON_ARRAYAGG(
                                                JSON_OBJECT(
                                                    'id', p2.id,
                                                    'pid',p2.pid,
                                                    'name', p2.cate_name,
                                                    'level', p2.level,
                                                    'type',p2.type,
                                                    'status', p2.status,
                                                    'case_count', (SELECT COUNT(*) FROM test_cases tc
                                                                   WHERE tc.project_id IN (
                                                                       SELECT p2.id
                                                                       UNION ALL SELECT p3.id FROM test_case_project p3 WHERE p3.pid = p2.id AND p3.is_del = 0
                                                                   ) AND tc.is_del = 0 AND tc.status = 1),
                                                    'create_time',p2.create_time,
                                                    'update_time', p2.update_time,
                                                    'planned_start', p2.planned_start,
                                                    'planned_end', p2.planned_end,
                                                    'planned_case_cnt', p2.planned_case_cnt,
                                                    'milestone', p2.milestone,
                                                    'children', COALESCE(
                                                        (
                                                            SELECT 
                                                                JSON_ARRAYAGG(
                                                                    JSON_OBJECT(
                                                                        'id', p3.id,
                                                                        'pid',p3.pid,
                                                                        'name', p3.cate_name,
                                                                        'level', p3.level,
                                        'type',p3.type,
                                        'status', p3.status,
                                        'case_count', (SELECT COUNT(*) FROM test_cases tc
                                                       WHERE tc.project_id = p3.id AND tc.is_del = 0 AND tc.status = 1),
                                                                        'create_time', p3.create_time,
                                                                        'update_time', p3.update_time,
                                                                        'planned_start', p3.planned_start,
                                                                        'planned_end', p3.planned_end,
                                                                        'planned_case_cnt', p3.planned_case_cnt,
                                                                        'milestone', p3.milestone
                                                                    )
                                                                )
                                                            FROM test_case_project p3
                                                            WHERE p3.pid = p2.id AND p3.level = 3 AND p3.is_del = 0{s_filter_g}
                                                        ),
                                                        JSON_ARRAY()
                                                    )
                                                )
                                            )
                                        FROM test_case_project p2
                                        WHERE p2.pid = p1.id AND p2.level = 2 AND p2.is_del = 0{s_filter_c}
                                    ),
                                    JSON_ARRAY()
                                )
                            )
                        ) as category_tree
                    FROM test_case_project p1
                    WHERE p1.pid = 0 AND p1.level = 1 AND p1.is_del = 0{s_filter_p}
                    ORDER BY p1.id
            """
            project_tree=db.execute_query(query1)
        return {
            "code": 200,
            "msg": "success",
            "total": total,
            "data": project_tree[0]['category_tree'] or '[]'
        }
    except Exception as e:
        print(e)


class addprojectdata(BaseModel):
    pid: int
    level: int
    name: str
    type: int
    # F6 计划目标字段（可选，仅三级有意义）
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    planned_case_cnt: Optional[int] = None
    milestone: Optional[str] = None
"""新建项目接口"""
@router.post("/category/add")
async def add_project(data:addprojectdata):
    try:
        with get_db_handler() as db:
            query = f"""
            INSERT INTO
                test_case_project(
                pid, 
                cate_name,
                level,
                type,
                planned_start,
                planned_end,
                planned_case_cnt,
                milestone
                ) 
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """
            params=(data.pid, data.name, data.level, data.type,
                   data.planned_start or None, data.planned_end or None,
                   data.planned_case_cnt, data.milestone or None)
            db.execute_update(query,params)
        return {
            "code": 200,
            "msg": "success",
            "data": "新建成功"
        }
    except Exception as e:
        print(e)

"""删除项目"""
@router.get("/category/delete")
async def del_category(id:int):
    try:
        with get_db_handler() as db:
            query = f"""
                    UPDATE test_case_project 
                    SET 
                      is_del=1
                    WHERE id = {id}
                    """
            db.execute_update(query)
        return {
            "code": 200,
            "msg": "success",
            "data": "删除成功！"
        }
    except Exception as e:
        print(e)


class updataprojectdata(BaseModel):
    id: int
    level: Optional[int] = None
    name: Optional[str] = None
    type: Optional[int] = None
    pid: Optional[int] = None
    pidPath: Optional[int] = None
    status: Optional[int] = None
    # F6 计划目标字段（仅三级有意义；None 表示不修改该字段）
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    planned_case_cnt: Optional[int] = None
    milestone: Optional[str] = None
"""编辑项目"""
@router.post("/category/update")
async def update_category(data:updataprojectdata):
    try:
        with get_db_handler() as db:
            # 动态部分更新：只更新调用方实际传入的字段，避免整行覆盖把未传字段清成空
            sets, params = [], []
            if data.level is not None:
                sets.append("level = %s"); params.append(data.level)
            if data.name is not None:
                sets.append("cate_name = %s"); params.append(data.name)
            if data.type is not None:
                sets.append("type = %s"); params.append(data.type)
            # 父级：pidPath 优先（历史调用习惯传数字），pid 兜底
            parent = data.pidPath if data.pidPath is not None else data.pid
            if parent is not None:
                sets.append("pid = %s"); params.append(parent)
            if data.status is not None:
                sets.append("status = %s"); params.append(data.status)
            # F6 计划字段（传入空字符串视为清空 DATE/JSON；None 表示不动）
            if data.planned_start is not None:
                sets.append("planned_start = %s"); params.append(data.planned_start or None)
            if data.planned_end is not None:
                sets.append("planned_end = %s"); params.append(data.planned_end or None)
            if data.planned_case_cnt is not None:
                sets.append("planned_case_cnt = %s"); params.append(data.planned_case_cnt)
            if data.milestone is not None:
                sets.append("milestone = %s"); params.append(data.milestone or None)
            if not sets:
                return {"code": 400, "msg": "missing fields", "data": None}
            params.append(data.id)
            query = f"UPDATE test_case_project SET {', '.join(sets)} WHERE id = %s"
            db.execute_update(query, params)
        return {
            "code": 200,
            "msg": "success",
            "data":"修改成功！"
        }
    except Exception as e:
        print(e)


def _normalize_milestone(val):
    """把 DB 的 milestone（JSON 列返回 list/dict/str 或 None）统一成 list。"""
    if val is None:
        return []
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []
    if isinstance(val, list):
        return val
    return [val]


def _dmin(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a < b else b


def _dmax(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def _calc_project_risk(n):
    """固定阈值风险红绿灯（与方案 5.3 一致）。返回 'red' / 'yellow' / 'green'。"""
    designed = n.get('designed') or 0
    executed = n.get('executed') or 0
    passed = n.get('passed') or 0
    defect_pending = n.get('defect_pending') or 0
    defect_total = n.get('defect_total') or 0
    cov = (executed / designed * 100) if designed > 0 else 0
    pass_rate = (passed / executed * 100) if executed > 0 else None
    defect_rate = (defect_pending / defect_total * 100) if defect_total > 0 else 0
    # 零执行（或与子孙汇总后仍为 0）直接标红
    if executed == 0:
        return 'red'
    if cov < 30 or (pass_rate is not None and pass_rate < 60) or defect_rate > 70:
        return 'red'
    if cov < 70 or (pass_rate is not None and pass_rate < 80) or defect_rate > 40:
        return 'yellow'
    return 'green'


"""智能项目管理进度：带进度字段的整棵三级项目树"""
@router.get('/project/progress')
async def project_progress():
    """返回整棵项目树，每个节点附加进度指标。
    - 三级 = 实测：本节点挂的设计用例 + 本节点名称命中的 task_detail 执行/缺陷明细
    - 一/二级 = 子孙三级汇总
    - executed=0 时 pass_rate=None（前端显示「未执行」而非 0%）
    口径严格来自真实表：设计 test_cases、执行/缺陷 task_detail.defect_status（无 defects 表）。
    """
    try:
        with get_db_handler() as db:
            projects = db.execute_query(
                "SELECT id, pid, cate_name, level, status, type, "
                "planned_start, planned_end, planned_case_cnt, milestone "
                "FROM test_case_project WHERE is_del = 0 ORDER BY id"
            )
            designed_rows = db.execute_query(
                "SELECT project_id, COUNT(*) AS c FROM test_cases "
                "WHERE is_del = 0 AND `status` = 1 GROUP BY project_id"
            )
            details = db.execute_query(
                "SELECT `project`, case_status, defect_status FROM task_detail WHERE `status` = 1"
            )
        designed_map = {}
        for r in designed_rows:
            try:
                designed_map[int(r['project_id'])] = int(r['c'])
            except (TypeError, ValueError):
                continue

        # 执行/缺陷按项目名聚合（名称匹配，P0 接受改名失联风险）
        exec_by_name = {}
        for d in details:
            name = (d.get('project') or '').strip()
            if not name:
                continue
            agg = exec_by_name.setdefault(name, {
                'executed': 0, 'passed': 0, 'failed': 0, 'skipped': 0,
                'defect_pending': 0, 'defect_total': 0, 'defect_dist': {},
            })
            agg['executed'] += 1
            cs = d.get('case_status')
            ds = d.get('defect_status')
            if cs == 2:
                agg['passed'] += 1
            elif cs == 3:
                agg['failed'] += 1
            elif cs == 4:
                agg['skipped'] += 1
            agg['defect_total'] += 1
            if ds == 0:
                agg['defect_pending'] += 1
            ds_key = ds if ds is not None else -1
            agg['defect_dist'][ds_key] = agg['defect_dist'].get(ds_key, 0) + 1

        # 构建节点
        nodes = {}
        for p in projects:
            nid = int(p['id'])
            nodes[nid] = {
                'id': nid,
                'pid': int(p['pid']),
                'name': p['cate_name'],
                'level': int(p['level']),
                'status': int(p['status']),
                'type': int(p.get('type') or p['level']),
                'designed': 0, 'executed': 0, 'passed': 0, 'failed': 0, 'skipped': 0,
                'pass_rate': None, 'defect_pending': 0, 'defect_total': 0,
                'defect_dist': {}, 'children': [],
                'planned_start': p.get('planned_start'),
                'planned_end': p.get('planned_end'),
                'planned_case_cnt': p.get('planned_case_cnt'),
                'milestone': _normalize_milestone(p.get('milestone')),
            }
        # 先建立完整父子关系（仅基于未删除节点）
        for nid, n in nodes.items():
            if n['pid'] in nodes:
                nodes[n['pid']]['children'].append(n)

        # 以一级项目（level==1）为根，仅保留可达子孙。
        # 作用：当某一级/二级项目被删除（is_del=1）时，其下所有后代
        # 全部不返回，避免孤儿节点浮到顶层充当假一级。
        reachable = set()
        def _mark_reachable(n):
            if n['id'] in reachable:
                return
            reachable.add(n['id'])
            for c in n['children']:
                _mark_reachable(c)
        for n in nodes.values():
            if n['level'] == 1:
                _mark_reachable(n)

        pruned = {}
        for nid, n in nodes.items():
            if nid not in reachable:
                continue
            n['children'] = [c for c in n['children'] if c['id'] in reachable]
            pruned[nid] = n
        nodes = pruned

        roots = [n for n in nodes.values() if n['level'] == 1]

        # 设计用例数：project_id 命中（仅三级有意义，父级靠汇总）
        for nid, cnt in designed_map.items():
            if nid in nodes:
                nodes[nid]['designed'] = cnt

        def aggregate(n):
            for c in n['children']:
                aggregate(c)
            if n['level'] == 3:
                agg = exec_by_name.get((n['name'] or '').strip())
                if agg:
                    n['executed'] = agg['executed']
                    n['passed'] = agg['passed']
                    n['failed'] = agg['failed']
                    n['skipped'] = agg['skipped']
                    n['defect_pending'] = agg['defect_pending']
                    n['defect_total'] = agg['defect_total']
                    n['defect_dist'] = dict(agg['defect_dist'])
            else:
                # 父级：初始化计划汇总，再累加子孙（三级实测 → 父级汇总）
                n['planned_case_cnt'] = 0
                n['planned_start'] = None
                n['planned_end'] = None
                n['milestone'] = []
                for c in n['children']:
                    n['executed'] += c['executed']
                    n['passed'] += c['passed']
                    n['failed'] += c['failed']
                    n['skipped'] += c['skipped']
                    n['defect_pending'] += c['defect_pending']
                    n['defect_total'] += c['defect_total']
                    n['planned_case_cnt'] += (c['planned_case_cnt'] or 0)
                    n['planned_start'] = _dmin(n['planned_start'], c['planned_start'])
                    n['planned_end'] = _dmax(n['planned_end'], c['planned_end'])
                    if c['milestone']:
                        n['milestone'].extend(c['milestone'])
                    for k, v in c['defect_dist'].items():
                        n['defect_dist'][k] = n['defect_dist'].get(k, 0) + v
            # 设计用例数向上汇总（父级=自身0+子孙）
            for c in n['children']:
                n['designed'] += c['designed']
            if n['executed'] > 0:
                n['pass_rate'] = round(n['passed'] / n['executed'] * 100, 1)
            else:
                n['pass_rate'] = None
            n['risk_level'] = _calc_project_risk(n)

        for r in roots:
            aggregate(r)

        return {
            "code": 200,
            "msg": "success",
            "data": roots,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"进度聚合失败: {str(e)}")
