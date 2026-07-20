"""鉴权:PBKDF2 口令哈希 + JWT(HS256 自实现,不依赖第三方 crypto)。"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import AppUser

_ITER = 200_000


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload: dict, secret: str) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    sig = _b64u(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def jwt_decode(token: str, secret: str) -> dict:
    try:
        header_b64, body_b64, sig = token.split(".")
    except ValueError as e:
        raise ValueError("token 格式错误") from e
    signing_input = f"{header_b64}.{body_b64}".encode()
    expected = _b64u(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ValueError("签名无效")
    payload = json.loads(_b64u_dec(body_b64))
    if payload.get("exp", 0) < time.time():
        raise ValueError("token 已过期")
    return payload


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _ITER)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except ValueError:
        return False


def create_token(user: AppUser) -> str:
    payload = {
        "sub": str(user.id), "username": user.username, "role": user.role,
        "exp": int((datetime.utcnow() + timedelta(hours=settings.jwt_expire_hours)).timestamp()),
    }
    return jwt_encode(payload, settings.jwt_secret)


def current_user(request: Request, db: Session = Depends(get_session)) -> AppUser:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer token")
    try:
        data = jwt_decode(auth[7:], settings.jwt_secret)
    except ValueError as e:
        raise HTTPException(401, f"token 无效: {e}") from e
    user = db.get(AppUser, int(data["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "用户不存在或已停用")
    return user


def require_roles(*roles: str):
    def dep(user: AppUser = Depends(current_user)) -> AppUser:
        if user.role != "admin" and user.role not in roles:
            raise HTTPException(403, f"需要角色: {roles}")
        return user
    return dep
