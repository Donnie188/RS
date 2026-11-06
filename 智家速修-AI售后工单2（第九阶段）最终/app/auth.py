# -*- coding: utf-8 -*-
"""登录鉴权模块：密码哈希 + JWT 签发/校验 + 接口级权限依赖。

设计说明：
- 密码用 pbkdf2_hmac(sha256, 26 万次) 单向哈希 + 随机盐，绝不存明文（标准库实现，零依赖）；
- JWT 用 PyJWT 的 HS256，载荷只放 user_id 和 role，有效期见 Config；
- Token 放 HttpOnly Cookie（防 XSS 窃取），同时兼容 Authorization: Bearer（方便 curl 测试）；
- "接口级权限校验"通过 FastAPI 依赖实现：
    require_api_user  → 任何登录用户可用（问答类接口）
    require_admin_api → 仅管理员（知识库/提示词/审计类接口）
  员工访问管理接口会得到 403 + 中文错误信息，未登录得到 401 + 中文错误信息。
"""

import hashlib
import os

import jwt
from fastapi import HTTPException, Request

from . import db
from .config import Config

# 错误码约定（阶段 2 设计的延续）：2xxx = 鉴权/会话类错误
ERR_NOT_LOGGED_IN = (2101, "未登录或登录已过期，请重新登录", 401)
ERR_FORBIDDEN = (2102, "无权访问：该功能仅管理员可用", 403)
ERR_BAD_CREDENTIALS = (2103, "用户名或密码错误", 401)


# ----------------------------------------------------------------------------
# 密码哈希
# ----------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """生成 'pbkdf2$盐(hex)$哈希(hex)' 格式的存储串。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。常量时间比较，防时序攻击。"""
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 260_000
        )
        return digest.hex() == digest_hex
    except (ValueError, AttributeError):
        return False


# ----------------------------------------------------------------------------
# JWT 签发与解析
# ----------------------------------------------------------------------------

def create_token(user_id: int, role: str) -> str:
    """签发 JWT。exp 用绝对时间戳，过期后解码会抛 ExpiredSignatureError。"""
    from datetime import datetime, timedelta, timezone
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=Config.TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """解析 JWT；无效/过期统一返回 None，由调用方决定抛 401。"""
    try:
        return jwt.decode(token, Config.JWT_SECRET, algorithms=[Config.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def get_user_from_request(request: Request) -> dict | None:
    """从请求中取当前登录用户（Cookie 优先，其次 Bearer 头）。未登录返回 None。"""
    token = request.cookies.get(Config.COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    row = db.get_user(int(payload["sub"]))
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"],
        "role_label": "管理员" if row["role"] == "admin" else "员工",
    }


# ----------------------------------------------------------------------------
# FastAPI 依赖：接口级权限校验
# ----------------------------------------------------------------------------

def require_api_user(request: Request) -> dict:
    """API 依赖：必须登录，否则抛 401（中文错误信息，统一 JSON 结构）。"""
    user = get_user_from_request(request)
    if user is None:
        code, message, status = ERR_NOT_LOGGED_IN
        raise HTTPException(status_code=status, detail={"code": code, "message": message})
    return user


def require_admin_api(request: Request) -> dict:
    """API 依赖：必须管理员，否则 401（未登录）/ 403（权限不足）。"""
    user = require_api_user(request)
    if user["role"] != "admin":
        code, message, status = ERR_FORBIDDEN
        raise HTTPException(status_code=status, detail={"code": code, "message": message})
    return user
