# -*- coding: utf-8 -*-
"""
企业微信群机器人推送（接口自动化执行告警）。

仅用于「接口自动化任务执行完毕且存在未通过接口」时，把告警推到企业微信群。
失败一律只 warning，不阻断主业务流程（与 notifications.notify 同一纪律）。
"""
import logging
import os

import httpx
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

# 复用 notifications 的 DB handler（同源 yaml 配置，避免重复连接逻辑；
# notifications 不反向依赖本模块，无循环 import 风险）。
from src.routers.notifications import get_db_handler  # noqa: E402

WECHAT_PUSH_KEY = 'wechat_push_enabled'


def is_wechat_push_enabled() -> bool:
    """读取 sys_config 表开关。

    未配置 / 读库失败 一律默认开启（True），保证历史行为不变、且开关故障
    时不会静默吞掉告警（fail-open 而非 fail-closed）。
    """
    try:
        with get_db_handler() as db:
            rows = db.execute_query(
                'SELECT cfg_value FROM `sys_config` WHERE cfg_key = %s', (WECHAT_PUSH_KEY,))
            if not rows:
                return True
            v = (rows[0]['cfg_value'] or '1').strip().lower()
            return v not in ('0', 'false', 'no', 'off')
    except Exception as e:  # noqa: BLE001
        logger.warning('读取微信推送开关失败，默认开启: %s', e)
        return True


def get_wechat_cfg() -> dict:
    """读取 config.yml 的 wechat 段；读不到返回空 dict。"""
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return data.get('wechat') or {}
    except Exception as e:  # noqa: BLE001
        logger.warning('读取 wechat 配置失败: %s', e)
        return {}


async def notify_wechat(title: str, content: str) -> bool:
    """
    向企业微信群机器人推送 markdown 消息。

    webhook 地址优先级：环境变量 WECHAT_WEBHOOK_URL > config.yml wechat.webhook_url。
    两者皆空则跳过（视为未启用）。

    总开关：sys_config.wechat_push_enabled = 0 时整体关闭（fail-open：读不到默认开）。
    """
    if not is_wechat_push_enabled():
        logger.info('微信推送总开关已关闭（sys_config.wechat_push_enabled=0），跳过推送')
        return False
    url = os.getenv('WECHAT_WEBHOOK_URL') or get_wechat_cfg().get('webhook_url')
    if not url:
        logger.warning('未配置 WECHAT_WEBHOOK_URL / wechat.webhook_url，跳过微信推送')
        return False
    payload = {
        'msgtype': 'markdown',
        'markdown': {'content': '### %s\n%s' % (title, content)},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload)
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {}
            if body.get('errcode', 0) != 0:
                logger.warning('微信推送返回非成功: status=%s body=%s', r.status_code, body)
                return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning('微信推送失败(不影响主流程): %s', e)
        return False
