"""Authentication routes: register / login / me."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg.errors import UniqueViolation

from app.api.schemas.auth import Token, UserCreate, UserLogin, UserPublic
from app.auth.deps import get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.auth.store import UserStore

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate) -> UserPublic:
    if UserStore.get_by_email(payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已注册",
        )
    try:
        user = UserStore.create(payload.email, hash_password(payload.password))
    except UniqueViolation as exc:
        # 并发注册场景下兜底，依赖数据库唯一约束
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已注册",
        ) from exc
    return user


@router.post("/login", response_model=Token)
async def login(payload: UserLogin) -> Token:
    user = UserStore.get_by_email(payload.email)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )
    token = create_access_token(user.id)
    return Token(
        access_token=token,
        user=UserPublic(id=user.id, email=user.email, created_at=user.created_at),
    )


@router.get("/me", response_model=UserPublic)
async def me(current: UserPublic = Depends(get_current_user)) -> UserPublic:
    return current


__all__ = ["router"]
