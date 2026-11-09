# -*- coding: utf-8 -*-
"""系统配置（sys_config 键值表）读写接口。

当前用途：企业微信推送总开关（wechat_push_enabled）。
设计：通用 key-value，后续其它系统级开关可复用同一套接口。

权限：读取/写入均要求登录（accessToken 解析出 uid）。
      本接口控制的是「全局」设置，生产环境应进一步限制仅管理员可写；
      当前测试平台内部使用，先放宽到「登录即可」，如需收紧再补角色校验。
"""
import logging
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.comment import MySqlconection, yaml_data
from src.routers.auth import parse_token

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

router = APIRouter(prefix='/system', tags=['system'])

WECHAT_KEY = 'wechat_push_enabled'


def get_db_handler():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )


def _uid(request: Request) -> int:
    token = request.headers.get('accessToken')
    if not token:
        return 0
    try:
        return parse_token(token) or 0
    except Exception:
        return 0


def _read_bool(key: str, default: bool = True) -> bool:
    with get_db_handler() as db:
        rows = db.execute_query(
            'SELECT cfg_value FROM `sys_config` WHERE cfg_key = %s', (key,))
        if not rows:
            return default
        v = (rows[0]['cfg_value'] or '1').strip().lower()
        return v not in ('0', 'false', 'no', 'off')


@router.get('/wechat-push')
async def get_wechat_push(request: Request):
    uid = _uid(request)
    if not uid:
        return {'code': 401, 'msg': '未登录', 'data': None}
    try:
        enabled = _read_bool(WECHAT_KEY, default=True)
        return {'code': 200, 'msg': 'success', 'data': {'enabled': enabled}}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 500, 'msg': '读取微信推送开关失败：%s' % e, 'data': None}


class _SwitchReq(BaseModel):
    enabled: bool


@router.post('/wechat-push')
async def set_wechat_push(request: Request, data: _SwitchReq):
    uid = _uid(request)
    if not uid:
        return {'code': 401, 'msg': '未登录'}
    try:
        val = '1' if data.enabled else '0'
        with get_db_handler() as db:
            db.execute_update(
                'INSERT INTO `sys_config` (cfg_key, cfg_value, remark) '
                'VALUES (%s, %s, %s) '
                'ON DUPLICATE KEY UPDATE cfg_value = %s, updated_at = NOW()',
                (WECHAT_KEY, val, '接口自动化执行告警→企业微信推送总开关：1开 0关', val),
            )
        return {'code': 200, 'msg': 'success', 'data': {'enabled': data.enabled}}
    except Exception as e:  # noqa: BLE001
        logger.error(e)
        return {'code': 500, 'msg': '保存微信推送开关失败：%s' % e}
