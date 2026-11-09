"""api_key 的 Fernet 加解密与掩码展示。

密钥来源：环境变量 AI_SECRET_KEY（由 add_ai_workbench_tables.py 生成并写入 .env）。
注意：更换 AI_SECRET_KEY 会导致库中已存密文全部无法解密，需配套写重加密脚本。
"""
import os
from typing import Optional

from .errors import AIConfigError

_ENV_LOADED = False


def _ensure_env() -> None:
    """兜底加载项目根 .env：独立脚本（非 main.py 启动）也能拿到 AI_SECRET_KEY"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    if os.getenv('AI_SECRET_KEY'):
        return
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, '.env')
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and os.getenv(k) is None:
                    os.environ[k] = v
    except Exception:
        return


def _fernet():
    _ensure_env()
    key = (os.getenv('AI_SECRET_KEY') or '').strip()
    if not key:
        raise AIConfigError('未配置 AI_SECRET_KEY，无法加解密 API Key（请先运行 add_ai_workbench_tables.py 生成）')
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode())
    except Exception as e:  # noqa: BLE001
        raise AIConfigError('AI_SECRET_KEY 非法，需为 Fernet 生成的 44 位 base64 字符串：%s' % e)


def encrypt(plain: str) -> str:
    """明文 key → 密文（存库）"""
    if plain is None:
        plain = ''
    return _fernet().encrypt(plain.encode('utf-8')).decode('utf-8')


def decrypt(token: Optional[str]) -> str:
    """密文 → 明文 key；解密失败通常是 AI_SECRET_KEY 被换过"""
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except AIConfigError:
        raise
    except Exception:  # noqa: BLE001
        raise AIConfigError('API Key 解密失败：AI_SECRET_KEY 可能已被更换，请到模型中心重新填写 Key')


def mask_key(plain: str) -> str:
    """只回后 4 位，接口一律用这个返回，绝不回明文"""
    if not plain:
        return ''
    tail = plain[-4:] if len(plain) >= 4 else plain
    return '****' + tail


def is_mask_value(value: str) -> bool:
    """判断前端提交的是否是掩码值（编辑时留空/回传掩码都表示「不修改」）"""
    if value is None:
        return True
    v = str(value).strip()
    return v == '' or v.startswith('****')
