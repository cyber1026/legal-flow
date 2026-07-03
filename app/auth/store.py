"""PostgreSQL-backed user store."""

from __future__ import annotations

from app.auth.models import UserInDB, UserPublic
from app.datetime_utils import to_utc_datetime
from app.db import get_conn


def _row_to_user_in_db(row) -> UserInDB:
    return UserInDB(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        created_at=to_utc_datetime(row["created_at"]),
    )


class UserStore:
    @staticmethod
    def create(email: str, password_hash: str) -> UserPublic:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(email, password_hash) VALUES(%s, %s) "
                "RETURNING id, email, created_at",
                (email.lower(), password_hash),
            )
            row = cur.fetchone()
        return UserPublic(
            id=row["id"],
            email=row["email"],
            created_at=to_utc_datetime(row["created_at"]),
        )

    @staticmethod
    def get_by_email(email: str) -> UserInDB | None:
        # 邮箱统一以小写形式存储 / 查询，避免大小写带来的重复账号。
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE email = %s",
                (email.lower(),),
            )
            row = cur.fetchone()
        return _row_to_user_in_db(row) if row else None

    @staticmethod
    def get_by_id(user_id: int) -> UserInDB | None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        return _row_to_user_in_db(row) if row else None


__all__ = ["UserStore"]
