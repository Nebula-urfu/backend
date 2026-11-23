import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import attempts, connection, engine, now, sessions, users
from .errors import Problem

router = APIRouter(tags=["session"])
PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe())
COOKIE = "nebula_session"
LIFETIME = timedelta(hours=12)
SECURE = os.getenv("SECURE_COOKIES", "true").lower() == "true"
Conn = Annotated[object, Depends(connection, scope="function")]


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class Profile(BaseModel):
    userId: str
    displayName: str
    role: str
    orgId: str
    address: str


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def profile(user):
    return dict(userId=user.id, displayName=user.name, role=user.role, orgId=user.org, address=user.address)


def csrf(token):
    return digest("nebula-csrf:" + token)


def user_session(request: Request, conn: Conn):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 128:
        raise Problem(401, "UNAUTHENTICATED", "Войдите в систему")
    user = conn.execute(
        select(users).join(sessions, sessions.c.user_id == users.c.id)
        .where(sessions.c.id == digest(token), sessions.c.expires > now())
    ).first()
    if not user:
        raise Problem(401, "UNAUTHENTICATED", "Сессия истекла")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf(token), supplied.encode("ascii", "replace").decode()):
            raise Problem(403, "CSRF", "Требуется токен защиты запроса")
    return user


User = Annotated[object, Depends(user_session)]


def allow(user, *roles):
    if user.role not in roles:
        raise Problem(403, "FORBIDDEN", "Действие недоступно для вашей роли")


class SessionProfile(Profile):
    csrfToken: str


@router.post("/session/login", response_model=SessionProfile)
def login(body: Credentials, request: Request, response: Response):
    if request.headers.get("X-Requested-With") != "Nebula":
        raise Problem(403, "CSRF", "Требуется заголовок X-Requested-With")
    username = body.username.strip().lower()
    client = request.client.host if request.client else "unknown"
    keys = sorted((digest("account:" + username), digest("client:" + client)))
    current = now()
    expiry = current + timedelta(minutes=10)
    with engine.begin() as conn:
        conn.execute(delete(attempts).where(attempts.c.expires <= current))
        limits = []
        for key in keys:
            conn.execute(insert(attempts).values(id=key, count=0, expires=expiry).on_conflict_do_nothing())
            limits.append(conn.execute(select(attempts).where(attempts.c.id == key).with_for_update()).one())
        if any(
            row.count >= (8 if row.id == digest("account:" + username) else 50) for row in limits
        ):
            raise Problem(429, "RATE_LIMIT", "Слишком много попыток входа")
        user = conn.execute(select(users).where(users.c.username == username)).first()
        try:
            PASSWORDS.verify(user.password if user else DUMMY_HASH, body.password)
            authenticated = user is not None
        except VerificationError:
            authenticated = False
        if not authenticated:
            conn.execute(update(attempts).where(attempts.c.id.in_(keys)).values(count=attempts.c.count + 1))
        else:
            conn.execute(delete(attempts).where(attempts.c.id == digest("account:" + username)))
            previous = request.cookies.get(COOKIE)
            if previous:
                conn.execute(delete(sessions).where(sessions.c.id == digest(previous)))
            token = secrets.token_urlsafe(32)
            conn.execute(sessions.insert().values(id=digest(token), user_id=user.id, expires=current + LIFETIME))
    if not authenticated:
        raise Problem(401, "INVALID_CREDENTIALS", "Неверный логин или пароль")
    response.set_cookie(COOKIE, token, httponly=True, secure=SECURE, samesite="strict",
                        max_age=int(LIFETIME.total_seconds()), path="/api/v1")
    return {**profile(user), "csrfToken": csrf(token)}


@router.get("/session", response_model=SessionProfile)
def session(request: Request, user: User):
    return {**profile(user), "csrfToken": csrf(request.cookies[COOKIE])}


@router.delete("/session", status_code=204)
def logout(request: Request, response: Response, user: User, conn: Conn):
    conn.execute(delete(sessions).where(sessions.c.id == digest(request.cookies[COOKIE])))
    response.delete_cookie(COOKIE, path="/api/v1", secure=SECURE, httponly=True, samesite="strict")
