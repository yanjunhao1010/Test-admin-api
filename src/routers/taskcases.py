import json
import logging
import os
from fastapi import APIRouter,HTTPException,Request
from src.comment import MySqlconection,yaml_data
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from .auth import parse_token
# 通知中心：任务派送 / 缺陷派送触发点（仅追加调用，不阻断主业务）
from src.routers.notifications import notify

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/tasks',
    tags=['tasks'])

# 配置文件路径：src/config/config.yml（基于文件位置推导，避免硬编码绝对路径）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)


def get_db_handler():
    """数据库连接依赖注入"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(data['mysql']['host'],data['mysql']['user'], data['mysql']['password'], data['mysql']['database'])
    return db


"""查询单个任务详情"""
@router.get('/taskDetail')
async def get_task_detail(id: str):
    try:
        with get_db_handler() as db:
            query=f"""
                SELECT 
                id, 
                task_name,
                task_performer, 
                tc_nums,
                passed_nums,
                failed_nums,
                completed_nums,
                skiped_nums,
                task_status,
                priority,
                begin_time,
                end_time,
                create_time,
                update_time 
                FROM task_management
                WHERE id={id}
                """
        detail = db.execute_query(query)
        return {
            "code": 200,
            "msg": "success",
            "data": detail
        }
    except Exception as e:
        logger.error(e)

"""获取测试用例任务列表"""
@router.get('/tasksLists')
async def get_tasks_list(request: Request, pageNo: int = 1, pageSize: int = 20,
                         task_name: str = '', task_status: int = None, priority: int = None):
    try:
        with get_db_handler() as db:
            result_data = []
            # 当前登录用户：从 token 解析 uid；超管（角色含 'admin'）查看全部，
            # 其余账号仅看 task_performer_id = 自己的任务（task_performer_id 必定有值，比 realname 可靠）
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

            where = "status = 1 and is_del = 0"
            params = []
            if performer_id:
                where += " AND task_performer_id = %s"
                params.append(performer_id)
            if task_name:
                where += " AND task_name LIKE %s"
                params.append(f"%{task_name}%")
            if task_status is not None:
                where += " AND task_status = %s"
                params.append(task_status)
            if priority is not None:
                where += " AND priority = %s"
                params.append(priority)

            count_query = f"SELECT COUNT(*) as total FROM task_management WHERE {where}"
            total_result = db.execute_query(count_query, tuple(params))
            total = total_result[0]['total'] if total_result else 0
            # 计算偏移量
            offset = (pageNo - 1) * pageSize
            query = f"""
                SELECT 
                id, 
                task_name,
                task_performer, 
                tc_nums,
                passed_nums,
                failed_nums,
                completed_nums,
                skiped_nums,
                task_status,
                priority,
                begin_time,
                end_time,
                create_time,
                update_time 
                FROM task_management  
                WHERE {where} ORDER BY create_time DESC LIMIT %s OFFSET %s
            """
            tasksList = db.execute_query(query, tuple(params + [pageSize, offset]))
            for task in tasksList:
                completion_rate = (task['completed_nums'] / task['tc_nums'] * 100) if task['tc_nums'] > 0 else 0
                pass_rate = (task['passed_nums'] / task['completed_nums'] * 100) if task['completed_nums'] > 0 else 0
                failure_rate = (task['failed_nums'] / task['completed_nums'] * 100) if task['completed_nums'] > 0 else 0
                skip_rate = (task['skiped_nums'] / task['tc_nums'] * 100) if task['tc_nums'] > 0 else 0

                # 构建任务数据，包含比率信息
                task_data = {
                    'task_info': task,
                    'rate': {
                        'completion_rate': round(completion_rate, 2),
                        'skip_rate': round(skip_rate, 2),
                        'pass_rate': round(pass_rate, 2),
                        'failure_rate': round(failure_rate, 2)
                    }
                }
                result_data.append(task_data)

        return {
            "code": 200,
            "msg": "success",
            "total": total,
            "data": result_data
        }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")


"""定义入参的模型"""
class addTask(BaseModel):
    task_name: str
    task_performer_ids: int
    package_ids: List[int]
    priority: int

"""新建测试任务接口"""
@router.post('/createTask')
async def create_task(request: Request, data: addTask):
    package_ids_str = ','.join(str(id) for id in data.package_ids)
    try:
        with get_db_handler() as db:
            query1 = f""" 
                SELECT 
                    u.realname,
                    COALESCE(JSON_LENGTH(cp.package_case_content, '$'), 0) as tc_nums
                FROM users u
                LEFT JOIN case_package cp ON cp.id IN({package_ids_str})
                WHERE u.id = {data.task_performer_ids}
                            """
            case_id_list = db.execute_query(query1)
            total_num = sum(item['tc_nums'] for item in case_id_list)
            query2 = """
                    INSERT INTO 
                        `task_management` 
                        (
                        task_name,
                        task_performer, 
                        task_performer_id,
                        package_id,
                        tc_nums, 
                        passed_nums, 
                        failed_nums, 
                        completed_nums,
                        skiped_nums,
                        priority, 
                        status, 
                        task_status, 
                        is_del) 
                        VALUES
                         (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                    """
            params = (
                data.task_name,
                case_id_list[0]['realname'],
                data.task_performer_ids,
                json.dumps(data.package_ids),
                total_num,
                0,
                0,
                0,
                0,
                data.priority,
                1,
                0,
                0
            )
            db.execute_update(query2, params)
            # 任务派送通知：给被指派人推一条（type=1）
            try:
                lid = db.execute_query('SELECT LAST_INSERT_ID() AS lid')[0]['lid']
                sender = parse_token(request.headers.get('accessToken')) if request.headers.get('accessToken') else None
                notify(
                    data.task_performer_ids, 1, '您有新的测试任务',
                    '任务「%s」已指派给您，请关注执行进度' % data.task_name,
                    'task', lid, '/task/detail/%s' % lid, sender,
                )
            except Exception as ne:  # noqa: BLE001
                logger.warning('createTask 派送通知失败（不影响建任务）: %s', ne)
        return {
            "code": 200,
            "msg": "success",
            "data":  {
                "detail": "创建成功"
            }
        }
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")

"""激活测试任务，就是插入测试用例"""
@router.get('/ActivateTasks')
async def activate_tasks(id:int):
    try:
        with get_db_handler() as db:
            query1 = f"""
                SELECT package_id FROM task_management WHERE id = {id}
            """
            re = db.execute_query(query1)
            package_ids = tuple(json.loads(re[0]['package_id']))
            placeholders = ','.join(['%s'] * len(package_ids))
            query2 = f"""
        
                SELECT package_case_content FROM case_package WHERE id in({placeholders})
        
                """
            re = db.execute_query(query2,(package_ids))
            pack_set = [i['package_case_content'] for i in re]
            case_set_list=[]
            for pack in pack_set:
                case_set_list.extend(json.loads(pack))
            query3 = f"""
                        INSERT INTO 
                            task_detail (
                                task_id, 
                                project,
                                module,
                                function_point,
                                priority,
                                pre_condition,
                                steps,
                                expected_result,
                                remark
                            )
                            SELECT               
                                %s, 
                                tcp.cate_name as project,
                                tc.module, 
                                tc.function_point,
                                tc.priority,
                                tc.pre_condition, 
                                tc.steps, 
                                tc.expected_result,
                                tc.remark
                            FROM test_cases tc 
                            LEFT JOIN
                            test_case_project tcp
                            ON
                            tc.project_id=tcp.id
                            WHERE FIND_IN_SET(tc.id, %s) > 0
                        """
            params = (
                id,
                ','.join(str(i) for i in set(case_set_list))
            )
            db.execute_update(query3, params)
            ###更新任务执行中状态
            query4 = f"""
                update task_management 
                set
                    task_status = 1,
                    begin_time = NOW()
                where id = {id}
            """
            db.execute_update(query4)

            return {
                "code": 200,
                "msg": "success",
                "data": {
                    "detail":"激活成功"
                }
            }
    except Exception as e:
        logger.error(e)

"""查询测试任务详情列表"""
@router.get('/TasksDetail')
async def task_detail(id:int,pageNo: int = 1,pageSize: int = 20):
    try:
        with get_db_handler() as db:
            query1 = f""" 
                SELECT
                  COUNT(CASE WHEN case_status in(2,3) and status = 1 THEN 1 END) as completed_count,
                  COUNT(CASE WHEN case_status = 2 and status = 1 THEN 1 END) as passed_count,
                  COUNT(CASE WHEN case_status = 3 and status = 1 THEN 1 END) as failed_count,
                  COUNT(CASE WHEN case_status = 4 and status = 1 THEN 1 END) as skiped_count,
                  COUNT(CASE WHEN status = 0 THEN 1 END) as pending_count,
                  COUNT(*) as total_count
              FROM task_detail
              WHERE task_id = {id}
            """
            status = db.execute_query(query1)
            completed_count = status[0]['completed_count']
            passed_count = status[0]['passed_count']
            failed_count = status[0]['failed_count']
            skiped_count = status[0]['skiped_count']
            total_count = status[0]['total_count']
            completion_rate = (completed_count / total_count * 100) if total_count > 0 else 0
            pass_rate = (passed_count / completed_count * 100) if completed_count > 0 else 0
            failure_rate = (failed_count / completed_count * 100) if completed_count > 0 else 0
            skip_rate = (skiped_count / total_count * 100) if total_count > 0 else 0
            offset = (pageNo - 1) * pageSize
            query2 = f"""
                SELECT
                    id,
                    task_id, 
                    project,
                    module,
                    function_point,
                    priority,
                    pre_condition,
                    steps,
                    expected_result,
                    final_result,
                    remark,
                    case_status,
                    status,
                    create_time,
                    update_time
                FROM task_detail
                WHERE task_id = %s LIMIT %s OFFSET %s
            """
            task_detail_list = db.execute_query(query2,((id,pageSize, offset)))

            return {
                "code": 200,
                "msg": "success",
                "data": {
                    "detail":task_detail_list,
                    "completed_count":completed_count,
                    "passed_count":passed_count,
                    "failed_count":failed_count,
                    "skiped_count":skiped_count,
                    "total_count":total_count,
                    "completion_rate":round(completion_rate, 0),
                    "pass_rate":round(pass_rate, 0),
                    "failure_rate":round(failure_rate, 0),
                    "skip_rate":round(skip_rate, 0)
                }
            }
    except Exception as e:
        logger.error(e)

"""更新任务里-用例状态"""
@router.get('/caselist/updateCaseStatus')
async def task_detail( taskId:int,caseId:int,case_status:int,remark:str,final_result:str):
    try:
        with get_db_handler() as db:
            """先确认当条用例的状态是否已经执行过,0:没执行，1:已执行"""
            query1 = f"""
                SELECT `status`,case_status FROM task_detail WHERE id = {caseId}
            """
            re = db.execute_query(query1)
            td_status=re[0]['status']
            td_cases_status = re[0]['case_status']

            """用例点击2(通过)，且status=0，需要同步更新用例状态"""
            if case_status == 2:
                query1 = f"""
                    UPDATE task_detail 
                    SET 
                        case_status = %s,
                        status = 1,
                        final_result = %s,
                        remark =%s
                    WHERE id = %s
                """
                params = (case_status,final_result,remark,caseId)
                db.execute_update(query1, params)
                if td_status == 0:
                    query1_2 = f"""
                        UPDATE task_management
                        SET
                            passed_nums = passed_nums + 1,
                            completed_nums = completed_nums+1
                        WHERE id = {taskId}
                        """
                    db.execute_update(query1_2)


                elif td_status == 1:
                    query1_3 = f"""
                        UPDATE task_management
                        SET
                            passed_nums = passed_nums + 1,
                            failed_nums = failed_nums - 1   
                        WHERE id = {taskId}
                        """
                    db.execute_update(query1_3)
                return {
                    "code": 200,
                    "msg": "success",
                    "data": '更新成功'
                }

            ####1.用例点击3(失败)，且status=0，相当于直接执行失败情景
            ####2.用例点击3(失败)，但是status = 1，就是之前已执行过通过，这时候点击失败的情急
            elif case_status == 3:####这是失败数
                query4 = f"""
                    UPDATE task_detail 
                    SET 
                        case_status = %s,
                        status = 1,
                        final_result = %s,
                        remark =%s
                    WHERE id = %s
    
                """
                params = (case_status, final_result, remark, caseId)
                db.execute_update(query4, params)
                if td_status == 0:
                    query4_2 = f"""
                        UPDATE task_management
                        SET
                            failed_nums = failed_nums + 1,
                            completed_nums = completed_nums+1
                        WHERE id = {taskId}
                        """
                    db.execute_update(query4_2)

                if td_status == 1:
                    query4_2 = f"""
                             UPDATE task_management
                             SET
                                 passed_nums = passed_nums - 1,
                                 failed_nums = failed_nums + 1            
                             WHERE id = {taskId}
                             """
                    db.execute_update(query4_2)

                return {
                    "code": 200,
                    "msg": "success",
                    "data": '更新成功'
                }


            ####1.跳过操作，且status=0，相当于直接执行跳过情景，该用例不算已完成，把 详情表：case_status=4，status=1，任务表：跳过数+1，完成数不加
            elif case_status == 4:
                query3 = f"""
                              UPDATE task_management
                              SET
                                  skiped_nums = skiped_nums + 1
                              WHERE id = {taskId}
                              """
                db.execute_update(query3)

                query3_1 = f""" 
                    UPDATE task_detail 
                    SET 
                        case_status = %s,
                        final_result = %s,
                        status = 1,
                        remark =%s      
                    WHERE id = %s
                """
                params = (case_status,final_result,remark,caseId)
                db.execute_update(query3_1, params)

                return {
                    "code": 200,
                    "msg": "success",
                    "data": '更新成功'
                }

            ####1.用例就是初始状态status=0,点击执行，把详情表case_status=1就可以
            ####2.用例是已经跳过状态status=1，点击执行，把详情表case_status=1，任务表：跳过数-1，
            elif case_status == 1:
                if td_status == 0:
                    query = f"""
                        UPDATE task_detail 
                        SET
                            case_status = %s,
                            final_result = %s,
                            status = 0,
                            remark =%s
                        WHERE id = %s
                    """
                    params = (case_status, final_result, remark, caseId)
                    db.execute_update(query, params)
                elif td_status == 1 and td_cases_status == 4:
                    query1 = f"""
                                UPDATE task_management
                                SET
                                    skiped_nums = skiped_nums - 1
                                WHERE id = {taskId}
                    """
                    db.execute_update(query1)

                    query1_1 = f"""
                        UPDATE task_detail 
                        SET
                            case_status = %s,
                            final_result = %s,
                            status = 0,
                            remark =%s
                        WHERE id = %s
                    """
                    params = (case_status, final_result, remark, caseId)
                    db.execute_update(query1_1, params)
                elif td_status == 1 and td_cases_status == 3:
                    query1 = f"""
                                UPDATE task_management
                                SET
                                    failed_nums = failed_nums - 1,
                                    completed_nums = completed_nums-1
                                WHERE id = {taskId}
                    """
                    db.execute_update(query1)

                    query1_1 = f"""
                        UPDATE task_detail 
                        SET
                            case_status = %s,
                            final_result = %s,
                            status = 0,
                            remark =%s
                        WHERE id = %s
                    """
                    params = (case_status, final_result, remark, caseId)
                    db.execute_update(query1_1, params)

                return {
                    "code": 200,
                    "msg": "success",
                    "data": '更新成功'
                }
    except Exception as e:
        logger.error(e)

"""开始执行任务，就是批量把状态修改成case_status=1"""
@router.get("/startTask")
async def startTask(taskId:int):
    try:
        with get_db_handler() as db:
            query1 = f"""
                UPDATE task_detail 
                SET 
                    case_status = 1,
                    `status`=0
                WHERE  task_id = {taskId}
            """
            db.execute_update(query1)
            query1_1 = f"""
                UPDATE task_management 
                SET 
                    task_status = 2,
                    completed_nums = 0,
                    passed_nums = 0,
                    failed_nums = 0,
                    skiped_nums = 0,
                    end_time = NULL
                WHERE  id = {taskId}
            """
            db.execute_update(query1_1)
        return {
            "code": 200,
            "msg": "success",
            "data":"任务开启成功"
        }
    except Exception as e:
        logger.error(e)

"""完成任务接口，更新任务表的状态"""
@router.get("/completeTask")
async def completeTask(taskId:int):
    try:
        with get_db_handler() as db:
            query = f"""
                    UPDATE task_management
                    SET
                        task_status = 4,
                        end_time = NOW()
                    WHERE id = {taskId}
                    """
            db.execute_update(query)
            # 缺陷派送通知：任务完成时，给负责人推「N条失败用例待处理」（type=2）
            # 缺陷本质 = 任务的失败用例（case_status=3），责任人复用 task_performer_id，不加独立字段。
            try:
                row = db.execute_query(
                    'SELECT task_performer_id, task_name FROM task_management WHERE id = %s', (taskId,))
                performer = row[0]['task_performer_id'] if row else None
                cnt_row = db.execute_query(
                    'SELECT COUNT(*) AS c FROM task_detail '
                    'WHERE task_id = %s AND case_status = 3 AND status = 1', (taskId,))
                defect_cnt = cnt_row[0]['c'] if cnt_row else 0
                if performer and defect_cnt:
                    notify(
                        performer, 2, '任务有%d条失败用例待处理' % defect_cnt,
                        '任务「%s」已完成，存在 %d 条失败用例（缺陷）待处理'
                        % (row[0]['task_name'], defect_cnt),
                        'defect', taskId, '/task/detail/%s' % taskId,
                    )
            except Exception as ne:  # noqa: BLE001
                logger.warning('completeTask 缺陷通知失败（不影响完成任务）: %s', ne)
            return {
                "code": 200,
                "msg": "success",
                "data":"任务已完成"
            }
    except Exception as e:
        logger.error(e)

"""生成测试报告接口"""
@router.get("/createReport")
async def createReport(id:int):
    try:
        with get_db_handler() as db:
            query1 = """
                    SELECT 
                        task_name,
                        task_performer, 
                        task_status,
                        begin_time,
                        end_time
                        FROM task_management
                    WHERE id = %s
                """
            task_msg = db.execute_query(query1, (id,))

            query2 = """
                    SELECT 
                    id,
                    priority,
                    module,
                    function_point,
                    final_result,
                    pre_condition,
                    steps,
                    expected_result
                    FROM 
                    task_detail 
                    WHERE task_id = %s AND case_status = 3

                    """
            failedCases=db.execute_query(query2, (id,))####失败测试用例数组

            query3 = """
                     SELECT 
                          COUNT(module) as moduleNum,
                          module as moduleName,
                          SUM(CASE WHEN case_status = 2 and status = 1 THEN 1 ELSE 0 END) as passNum,
                          SUM(CASE WHEN case_status = 3 and status = 1 THEN 1 ELSE 0 END) as failedNum,
                          SUM(CASE WHEN case_status = 4 and status = 1 THEN 1 ELSE 0 END) as skipNum
                        FROM task_detail 
                        WHERE task_id = %s
                        GROUP BY module
                     """
            modules = db.execute_query(query3, (id,))
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "task_msg":task_msg,
                "failedCases":failedCases,
                "modules":modules,
            }
        }
    except Exception as e:
        logger.error(e)