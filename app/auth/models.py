"""Pydantic models for authentication."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime


class UserInDB(UserPublic):
    password_hash: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


__all__ = ["UserCreate", "UserLogin", "UserPublic", "UserInDB", "Token"]
