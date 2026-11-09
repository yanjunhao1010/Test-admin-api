# -*- coding: utf-8 -*-
"""
登录与用户信息接口

与前端 src/api/user.js、src/config/permission.js 一一对应：
    POST /api/publicKey   返回 RSA 公钥（与前端 encrypt.js 的私钥配套，用于登录密码加密）
    POST /api/login       校验账号密码，返回 accessToken
    POST /api/userInfo    根据 accessToken 返回用户信息 + 权限码（驱动菜单与按钮渲染）
    POST /api/logout      退出登录（无状态 token，服务端无需失效，仅返回成功）

说明：
- 登录密码走 RSA 加密（loginRSA=true）。前端用 /publicKey 公钥加密后放在 body.param 里，
  body 形如 { param: "<base64密文>" }；未加密时也可直接传 { username, password }。
- 私钥与前端 src/utils/encrypt.js 中的硬编码私钥是同一对（开发期演示用，已暴露在客户端）。
- token 为自签名的 HMAC-SHA256（base64url(header).base64url(payload).base64url(sig)），
  无需额外依赖；生产可替换为 JWT。payload 含 uid 与 exp。
- 权限码复用 rbac._user_permission_codes：角色权限并集 + 角色标识，
  前端 mutation 据此过滤 asyncRoutes（meta.permissions）与 v-permissions 按钮。
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from src.comment import MySqlconection, yaml_data
from src.routers.rbac import _user_permission_codes

logging_import = __import__('logging')
logger = logging_import.getLogger(__name__)

router = APIRouter(tags=['auth'])

# ---------------- 配置 ----------------
# token 签名密钥：优先读环境变量，缺省为开发期占位值（生产务必通过环境变量覆盖）
SECRET = os.getenv('JWT_SECRET', 'dev-only-secret-change-me')
# token 有效期（秒）：默认 7 天
TOKEN_TTL = int(os.getenv('TOKEN_TTL', str(60 * 60 * 24 * 7)))

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.yml'
)

# RSA 密钥对：运行期生成并缓存（登录密码加密仅用于单次请求，无需持久化）。
# 说明：前端 src/utils/encrypt.js 中的 privateKey 仅用于其未使用的 decryptedData，
# 登录流程前端只用后端下发的公钥加密，因此前后端无需共享同一把私钥。
_PRIV_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


# ---------------- 工具 ----------------
def get_db_handler():
    """数据库连接（与 routers/users.py 等保持一致）"""
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'],
        data['mysql']['user'],
        data['mysql']['password'],
        data['mysql']['database'],
    )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def get_public_key_b64() -> str:
    """从运行期私钥派生对应的 SubjectPublicKeyInfo 公钥（base64，去掉 PEM 头尾）"""
    der = _PRIV_KEY.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    lines = der.decode().strip().splitlines()
    return ''.join(lines[1:-1])


def create_token(user_id: int) -> str:
    header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64url_encode(
        json.dumps({'uid': user_id, 'exp': int(time.time()) + TOKEN_TTL}).encode()
    )
    signing = f'{header}.{payload}'.encode('ascii')
    sig = hmac.new(SECRET.encode('utf-8'), signing, hashlib.sha256).digest()
    return f'{header}.{payload}.{_b64url_encode(sig)}'


def parse_token(token: Optional[str]) -> Optional[int]:
    """校验签名与有效期，返回 user_id；失败返回 None"""
    if not token or token.count('.') != 2:
        return None
    header, payload, sig = token.split('.')
    signing = f'{header}.{payload}'.encode('ascii')
    expected = hmac.new(SECRET.encode('utf-8'), signing, hashlib.sha256).digest()
    expected_sig = _b64url_encode(expected)
    if not hmac.compare_digest(expected_sig, sig):
        return None
    try:
        data = json.loads(_b64url_decode(payload).decode('utf-8'))
    except Exception:
        return None
    if data.get('exp', 0) < int(time.time()):
        return None
    return data.get('uid')


def _decrypt_login_param(param: str) -> dict:
    """用 RSA 私钥解密前端传来的 param（PKCS1 v1.5，与 JSEncrypt 对应）。

    param 是 JSEncrypt 输出的 base64 密文字符串（JSON 中只能放字符串），需先 base64 解码。
    """
    cipher = base64.b64decode(param)
    plain = _PRIV_KEY.decrypt(cipher, padding.PKCS1v15())
    return json.loads(plain.decode('utf-8'))


# ---------------- 请求模型 ----------------
class LoginIn(BaseModel):
    """登录入参：RSA 加密时只有 param；明文时带 username/password"""
    username: str = ''
    password: str = ''
    param: Optional[str] = None


class UserInfoIn(BaseModel):
    """用户信息入参：前端以 tokenName(accessToken) 作为字段名"""
    accessToken: str = ''


# ---------------- 接口 ----------------
@router.post('/publicKey', summary='获取 RSA 公钥')
async def public_key():
    return {'code': 200, 'msg': 'success', 'data': {'publicKey': get_public_key_b64()}}


@router.post('/login', summary='登录')
async def login(body: LoginIn):
    username = body.username
    password = body.password

    # RSA 加密分支：前端 encrypt.js 把 {username,password} 加密后放在 param
    if body.param:
        try:
            creds = _decrypt_login_param(body.param)
            username = creds.get('username', '')
            password = creds.get('password', '')
        except Exception as e:
            logger.warning(f'登录参数解密失败: {e}')
            return {'code': 500, 'msg': '登录参数解密失败，请重试', 'data': None}

    if not username or not password:
        return {'code': 500, 'msg': '请输入用户名和密码', 'data': None}

    try:
        with get_db_handler() as db:
            row = db.execute_single_query(
                'SELECT id, username, password_hash, status FROM users WHERE username=%s',
                (username,),
            )
            if not row:
                return {'code': 500, 'msg': '帐户或密码不正确', 'data': None}
            if row.get('status') == 0:
                return {'code': 500, 'msg': '账号已被禁用，请联系管理员', 'data': None}
            stored_hash = row.get('password_hash') or ''
            if not stored_hash or not bcrypt.checkpw(
                password.encode('utf-8'), stored_hash.encode('utf-8')
            ):
                return {'code': 500, 'msg': '帐户或密码不正确', 'data': None}
            uid = row['id']
    except Exception as e:
        logger.error(f'登录查询失败: {e}')
        raise HTTPException(status_code=500, detail=f'登录失败: {str(e)}')

    token = create_token(uid)
    return {'code': 200, 'msg': 'success', 'data': {'accessToken': token}}


@router.post('/userInfo', summary='获取用户信息')
async def user_info(body: UserInfoIn):
    uid = parse_token(body.accessToken) if body.accessToken else None
    if uid is None:
        return {'code': 401, 'msg': '登录已过期，请重新登录', 'data': None}

    try:
        with get_db_handler() as db:
            row = db.execute_single_query(
                'SELECT id, username, realname, avatar FROM users WHERE id=%s', (uid,)
            )
            if not row:
                return {'code': 401, 'msg': '用户不存在', 'data': None}
            # 复用现有 RBAC 权限码计算（角色权限并集 + 角色标识）
            permissions: List[str] = _user_permission_codes(db, uid)
    except Exception as e:
        logger.error(f'查询用户信息失败: {e}')
        raise HTTPException(status_code=500, detail=f'获取用户信息失败: {str(e)}')

    return {
        'code': 200,
        'msg': 'success',
        'data': {
            'permissions': permissions,
            'username': row.get('realname') or row.get('username') or '',
            'avatar': row.get('avatar') or '',
        },
    }


@router.post('/logout', summary='退出登录')
async def logout():
    # 无状态 token，服务端无需失效
    return {'code': 200, 'msg': 'success', 'data': None}
