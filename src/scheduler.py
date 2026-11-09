import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.routers.automation import run_schedule_by_id, get_db_handler, cleanup_old_executions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 显式指定时区，避免依赖本地时区探测（tzlocal）的不确定性；
# cron 表达式按 Asia/Shanghai 解释。pytz 已在 venv 中。
try:
    import pytz

    _TZ = pytz.timezone('Asia/Shanghai')
except Exception:
    _TZ = None

scheduler = AsyncIOScheduler(timezone=_TZ)


def _build_trigger(cron_expr):
    """把标准 5 段 crontab 表达式转成 APScheduler CronTrigger；非法/为空返回 None"""
    if not cron_expr or not str(cron_expr).strip():
        return None
    try:
        return CronTrigger.from_crontab(str(cron_expr).strip())
    except Exception as e:
        logger.warning(f'cron 表达式无效，跳过调度: {cron_expr!r} -> {e}')
        return None


async def _run_plan_job(schedule_id: int) -> None:
    """调度器回调：执行单个计划（无 Request 上下文）"""
    try:
        result = await run_schedule_by_id(schedule_id, trigger='schedule')
        logger.info(f'[调度] 计划 {schedule_id} 执行完成: {result}')
    except Exception as e:
        logger.error(f'[调度] 计划 {schedule_id} 执行异常: {e}', exc_info=True)


async def _sync_jobs() -> None:
    """
    重新扫描 api_schedules 中 enabled=1 的计划，按 cron_expr 注册/刷新 cron job，
    并回写 next_run_time。UI 增删改、启停计划后最多 60s 内自动生效，无需重启服务。
    """
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT id, cron_expr FROM api_schedules WHERE enabled = 1'
            )
            # 计算并回写 next_run_time（tz 感知 -> 去 tz 后存入 MySQL DATETIME）
            updates = []
            now = datetime.now(_TZ) if _TZ else datetime.now()
            for r in rows:
                trigger = _build_trigger(r.get('cron_expr'))
                if not trigger:
                    updates.append((None, r['id']))
                    continue
                nxt = trigger.get_next_fire_time(None, now)
                nxt_naive = nxt.replace(tzinfo=None) if nxt else None
                updates.append((nxt_naive, r['id']))
            for nxt_naive, sid in updates:
                db.execute_update(
                    'UPDATE api_schedules SET next_run_time = %s WHERE id = %s',
                    (nxt_naive, sid),
                )
    except Exception as e:
        logger.error(f'[调度] 扫描/同步计划失败: {e}', exc_info=True)
        return

    # 移除旧的计划型 job 后按最新 DB 状态重新注册
    for job in list(scheduler.get_jobs()):
        if job.id.startswith('plan-'):
            scheduler.remove_job(job.id)

    added = 0
    for r in rows:
        trigger = _build_trigger(r.get('cron_expr'))
        if not trigger:
            continue
        scheduler.add_job(
            _run_plan_job,
            trigger,
            args=[r['id']],
            id=f'plan-{r["id"]}',
            replace_existing=True,
            misfire_grace_time=300,  # 服务宕机错过触发点后 5 分钟内不补跑
        )
        added += 1
    logger.info(f'[调度] 已同步 {added} 个启用计划（next_run_time 已回写）')


async def start_scheduler() -> None:
    """随 FastAPI 启动：开启调度器并立即同步一次，之后每 60s 重新扫描"""
    if scheduler.running:
        return
    scheduler.start()
    await _sync_jobs()
    scheduler.add_job(
        _sync_jobs, 'interval', seconds=60, id='_sync_jobs', replace_existing=True
    )
    # A4：每日自动清理早于 90 天的执行记录，避免 api_executions 无限膨胀
    scheduler.add_job(
        cleanup_old_executions, 'interval', hours=24, args=[90],
        id='_cleanup_executions', replace_existing=True,
    )
    logger.info('[调度] APScheduler 已启动（每 60s 重新扫描启用计划；每日清理 90 天前执行记录）')


def stop_scheduler() -> None:
    """随 FastAPI 关闭：停止调度器"""
    try:
        scheduler.shutdown(wait=False)
        logger.info('[调度] APScheduler 已停止')
    except Exception as e:
        logger.warning(f'[调度] 停止调度器异常: {e}')
