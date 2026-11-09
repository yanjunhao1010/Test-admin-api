"""全局配置（ai_settings 表）读取，带进程内缓存。

配额与脱敏开关都放这里，避免写死在代码里——运营能在「AI工作台 → 设置」页改。
"""
import time
from typing import Any, Dict, Optional

DEFAULTS = {
    # 全局兜底上限；模型中心里各自填的 max_tokens 优先（推理模型生成慢，需要单独压小）
    'per_call_max_tokens': '4096',
    'daily_call_limit': '200',
    'ai_mask_enabled': '1',
    'ai_mask_refill': '0',
    'ai_mask_rules': '',
}

CACHE_TTL = 5.0  # 秒；配置改动后最多 5 秒生效
_cache: Dict[str, str] = {}
_cache_ts = 0.0


def clear_cache() -> None:
    global _cache_ts
    _cache_ts = 0.0


def load(db, force: bool = False) -> Dict[str, str]:
    """读取全部配置（合并默认值）"""
    global _cache, _cache_ts
    now = time.time()
    if not force and _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache
    data = dict(DEFAULTS)
    try:
        rows = db.execute_query('SELECT cfg_key, cfg_value FROM ai_settings')
        for r in rows or []:
            data[r['cfg_key']] = r['cfg_value']
    except Exception:
        # 表还没建或查询失败时退回默认值，不让配置读取拖垮业务
        pass
    _cache = data
    _cache_ts = now
    return data


def get(db, key: str, default: Optional[str] = None) -> Optional[str]:
    cfg = load(db)
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return DEFAULTS.get(key, default)


def get_int(db, key: str, default: int = 0) -> int:
    try:
        return int(str(get(db, key, str(default))).strip())
    except Exception:
        return default


def get_bool(db, key: str, default: bool = False) -> bool:
    v = str(get(db, key, '1' if default else '0')).strip().lower()
    return v in ('1', 'true', 'yes', 'on')


def set_value(db, key: str, value: Any, uid: Optional[int] = None) -> None:
    """写入单个配置项（存在则更新），写完清缓存立即生效"""
    if isinstance(value, (dict, list)):
        value = __import__('json').dumps(value, ensure_ascii=False)
    db.execute_update(
        'INSERT INTO ai_settings (cfg_key, cfg_value, update_by) VALUES (%s, %s, %s) '
        'ON DUPLICATE KEY UPDATE cfg_value = VALUES(cfg_value), update_by = VALUES(update_by)',
        (key, '' if value is None else str(value), uid),
    )
    clear_cache()
