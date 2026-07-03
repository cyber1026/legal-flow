"""FastAPI dependencies: extract & validate the current user from a JWT."""

from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.auth.models import UserPublic
from app.auth.security import decode_access_token
from app.auth.store import UserStore

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _credentials_error(detail: str = "无效的认证凭据") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(token: str | None = Depends(oauth2_scheme)) -> UserPublic:
    if not token:
        raise _credentials_error("缺少认证令牌")
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise _credentials_error("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise _credentials_error("无效的认证令牌") from exc

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise _credentials_error()
    try:
        user_id = int(user_id_str)
    except (TypeError, ValueError) as exc:
        raise _credentials_error() from exc

    user = UserStore.get_by_id(user_id)
    if not user:
        raise _credentials_error("用户不存在")
    return UserPublic(id=user.id, email=user.email, created_at=user.created_at)


__all__ = ["get_current_user", "oauth2_scheme"]
