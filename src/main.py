import logging
import os
import uvicorn
from contextlib import asynccontextmanager


# 零依赖加载项目根目录 .env（避免提交密钥，也避免引入 python-dotenv 依赖）
# 必须在导入 routers 之前执行：routers/AIS.py 在模块导入阶段就会读取 DEEPSEEK_API_KEY
# 构造 OpenAI 客户端，若晚于其导入执行，服务端将始终读不到 Key 并固定返回 500。
def _load_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if not os.path.exists(env_path):
        env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and os.getenv(key) is None:
                os.environ[key] = value


_load_env()

from fastapi import FastAPI, Request, Depends, HTTPException  # noqa: E402
from fastapi.security import APIKeyHeader  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from routers import testcases, AIS, taskcases, users, uploadfiles, rbac, menus, auth, defects, automation, ai_workbench, home, notifications, system, departments, travel, kb, devprocess, product_points, code_watch  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    # 启动：开启接口自动化定时调度器（APScheduler 随事件循环启动）
    try:
        from src.scheduler import start_scheduler

        await start_scheduler()
    except Exception as e:
        # 调度器启动失败不应拖垮主服务
        logger.error(f'启动自动化调度器失败（不影响主服务）: {e}', exc_info=True)
    # 启动：清理上一进程残留的「分析中」需求文档与 AI 在途锁。
    # 进程被杀/重启时，正在跑的 AI 请求不可能继续，其 status=1 / ai_inflight 行必为无主孤儿；
    # 若不复位，前端会按这些状态永久禁用按钮（刷新/重进都点不动）。
    # 新进程启动时尚无任何在途任务，故全表复位安全且幂等。
    try:
        from src.routers.ai_workbench import get_db_handler

        with get_db_handler() as db:
            db.execute_update(
                "UPDATE ai_requirements SET status = 0, error_msg = '进程重启，分析中断' "
                "WHERE status = 1 AND is_del = 0")
            db.execute_update("DELETE FROM ai_inflight")
            # 阶段三 R8：异步任务同理。进程被杀/重启时后台线程不可能继续，
            # status=0/1 的行必为无主僵尸；不复位会导致该需求永远「正在分析」，
            # 复用逻辑会一直把它们接回来，用户再也提交不了新任务。
            db.execute_update(
                "UPDATE ai_async_tasks SET status = 3, error_msg = '进程重启，分析中断', "
                "cur_batch = '已中断', finish_time = NOW(), update_time = NOW() "
                "WHERE status IN (0, 1)")
            db.execute_update(
                "INSERT INTO ai_async_task_logs (task_id, level, stage, message, create_time) "
                "SELECT task_id, 'error', COALESCE(NULLIF(cur_stage,''),'decompose'), "
                "'进程重启，分析中断', NOW() FROM ai_async_tasks "
                "WHERE status = 3 AND error_msg = '进程重启，分析中断' "
                "AND finish_time >= DATE_SUB(NOW(), INTERVAL 10 SECOND)")
    except Exception as e:
        logger.error(f'清理残留 AI 在途状态失败（不影响主服务）: {e}', exc_info=True)
    yield
    # 关闭：停止调度器
    try:
        from src.scheduler import stop_scheduler

        stop_scheduler()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)

# 全局 API Key 鉴权：前端通过 X-API-Key 请求头携带，密钥来自环境变量 API_KEY
# 未配置 API_KEY 时视为开发模式放行；配置后必须携带正确的 X-API-Key
api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)
EXPECTED_API_KEY = os.getenv('API_KEY', '')

###测试一下上传代码
async def verify_api_key(request: Request) -> None:
    if not EXPECTED_API_KEY:
        return
    provided = await api_key_header(request)
    if provided != EXPECTED_API_KEY:
        raise HTTPException(status_code=403, detail='缺少或错误的 API Key')


@app.middleware('http')
async def add_process_time_header(request, call_next):
    response = await call_next(request)
    response.headers['X-Custom-Header'] = 'FastAPI'
    return response


# 统一错误信封：将 HTTPException 与未捕获异常收敛为 { code, msg, data: null }
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={'code': exc.status_code, 'msg': exc.detail, 'data': None},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(f'未捕获异常: {exc}', exc_info=True)
    return JSONResponse(
        status_code=500,
        content={'code': 500, 'msg': '服务器内部错误', 'data': None},
    )
app.include_router(testcases.router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(AIS.router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(taskcases.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 测试用例知识库：列表/详情/CRUD/收录/引用（一期）
app.include_router(kb.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 缺陷管理：按任务归集失败用例（case_status=3）
app.include_router(defects.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 接口自动化执行：接口管理 / 执行计划 / 执行总结
app.include_router(automation.router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(users.router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(uploadfiles.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# RBAC 权限管理：角色、权限点、分配关系
app.include_router(rbac.roles_router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(rbac.permissions_router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(rbac.user_roles_router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 菜单管理：与 rbac.permissions_router 同表（permissions），
# menus 提供含路由字段的完整树 + 增删改，供配置页的「菜单管理」使用
app.include_router(menus.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 登录与用户信息：publicKey / login / userInfo / logout
app.include_router(auth.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# AI 工作台：模型中心 / AI生成用例 / AI用例库 / 需求分析 / 执行总结 / 日志与设置
app.include_router(ai_workbench.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 首页工作台：一次聚合顶部统计卡 / 执行趋势 / 工作待办 / 数据统计
app.include_router(home.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 通知中心：任务派送 / 缺陷派送 / 定时任务完成 / AI 生成完成
app.include_router(notifications.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 系统配置：企业微信推送总开关等（sys_config 键值表）
app.include_router(system.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 部门管理：CRUD（用户「部门」下拉读取部门表）
app.include_router(departments.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 智能出行：POI 推荐 / AI 行程规划 / 我的行程
app.include_router(travel.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 研发流程可视化（智能流程看板）+ 需求变更影响分析
app.include_router(devprocess.router, prefix='/api', dependencies=[Depends(verify_api_key)])
app.include_router(product_points.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 代码变更影响分析：监听 dev 分支 → 静态分析影响面 → 输出回归范围与建议（只读，不执行用例）
app.include_router(code_watch.router, prefix='/api', dependencies=[Depends(verify_api_key)])
# 允许跨域的前端来源：通过环境变量 CORS_ORIGINS 配置（英文逗号分隔）。
# 未配置时回退到本地开发地址，保证本地开发体验不变。
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        'CORS_ORIGINS',
        'http://localhost:8091,http://127.0.0.1:8091,'
        'http://localhost:8090,http://127.0.0.1:8090,'
        'http://127.0.0.10:8090,http://192.168.1.15:8090',
    ).split(',')
    if origin.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
    expose_headers=['*'],  # 暴露所有头部
)


if __name__ == '__main__':
    uvicorn.run(
        'main:app',
        host=os.getenv('APP_HOST', '127.0.0.1'),
        port=int(os.getenv('APP_PORT', '8090')),
    )
