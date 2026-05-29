"""Auth endpoints: /auth/guest, /auth/login, /auth/refresh, /auth/logout (T-038)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import TokenPayload, get_current_user, get_db
from src.auth.blacklist import blacklist_token
from src.auth.jwt import (
    JWTError,
    create_access_token,
    create_guest_token,
    create_refresh_token,
    decode_token,
)
from src.auth.password import dummy_verify, hash_password, verify_password
from src.auth.rate_limit import (
    check_email_rate_limit,
    check_ip_rate_limit,
    is_email_locked,
    record_failed_attempt,
    reset_attempt_counters,
)
from src.persistence.repositories.user_repo import UserRepository

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / response models ─────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """POST /auth/register request body."""

    email: str
    password: str


class LoginRequest(BaseModel):
    """POST /auth/login request body."""

    email: str
    password: str


class RefreshRequest(BaseModel):
    """POST /auth/refresh request body."""

    refresh_token: str


class GuestTokenResponse(BaseModel):
    """POST /auth/guest response."""

    access_token: str
    token_type: str = "bearer"
    session_id: str


class TokenResponse(BaseModel):
    """POST /auth/login and /auth/refresh response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── POST /auth/guest ────────────────────────────────────────────────────────


@router.post("/guest", response_model=GuestTokenResponse)
async def issue_guest_token() -> GuestTokenResponse:
    """Issue a guest JWT with a new UUID session_id (FR-15).

    No DB row is created. The session_id identifies the guest session
    within LangGraph checkpoints and Redis quota namespaces.
    """
    session_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    token = create_guest_token(session_id=session_id, jti=jti)
    return GuestTokenResponse(access_token=token, session_id=session_id)


# ── POST /auth/register ─────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create a new user account and return access + refresh tokens (auto-login).

    Returns HTTP 409 if the email is already registered.
    Password must be at least 8 characters.
    """
    if len(body.password) < 8:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "WEAK_PASSWORD",
                "message": "Password must be at least 8 characters.",
                "retryable": False,
                "retry_after_seconds": None,
            },
        )

    password_hash = hash_password(body.password)
    repo = UserRepository(db)

    try:
        user = await repo.create_user(email=body.email.lower().strip(), password_hash=password_hash)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EMAIL_TAKEN",
                "message": "An account with this email already exists.",
                "retryable": False,
                "retry_after_seconds": None,
            },
        ) from exc

    access_jti = str(uuid.uuid4())
    refresh_jti_str = str(uuid.uuid4())
    refresh_jti_uuid = uuid.UUID(refresh_jti_str)

    access_token = create_access_token(str(user.id), access_jti)
    refresh_token_str = create_refresh_token(str(user.id), refresh_jti_str)

    exp_ts: int = decode_token(refresh_token_str)["exp"]
    expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    await repo.store_refresh_token(refresh_jti_uuid, user.id, expires_at)
    await db.commit()

    return TokenResponse(access_token=access_token, refresh_token=refresh_token_str)


# ── POST /auth/login ────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate with email + password; return access + refresh tokens (FR-16).

    Applies per-IP and per-email rate limiting and brute-force soft-lock (FR-30).
    Returns HTTP 401 for both unknown email and wrong password with identical
    response bodies (constant-time behaviour via dummy_verify, FR-16b).
    """
    ip = _client_ip(request)
    ip_hash = _sha256(ip)
    email_hash = _sha256(body.email.lower())

    # Per-IP rate limit (FR-30a)
    ip_ok, ip_retry = await check_ip_rate_limit(ip_hash)
    if not ip_ok:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Too many login attempts from this IP.",
                "retryable": True,
                "retry_after_seconds": ip_retry,
            },
        )

    # Email soft-lock check (FR-30c)
    locked, lock_retry = await is_email_locked(email_hash)
    if locked:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "ACCOUNT_LOCKED",
                "message": "Account temporarily locked due to failed login attempts.",
                "retryable": True,
                "retry_after_seconds": lock_retry,
            },
        )

    # Per-email rate limit (FR-30b)
    email_ok, email_retry = await check_email_rate_limit(email_hash)
    if not email_ok:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Too many login attempts for this account.",
                "retryable": True,
                "retry_after_seconds": email_retry,
            },
        )

    # Constant-time user lookup: always verify even when email not found
    repo = UserRepository(db)
    user = await repo.get_user_by_email(body.email)
    if user is None:
        dummy_verify()
        await record_failed_attempt(email_hash)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, user.password_hash):
        await record_failed_attempt(email_hash)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Successful login — reset counters and issue tokens
    await reset_attempt_counters(email_hash)
    await repo.reset_failed_logins(user.id)

    access_jti = str(uuid.uuid4())
    refresh_jti_str = str(uuid.uuid4())
    refresh_jti_uuid = uuid.UUID(refresh_jti_str)

    access_token = create_access_token(str(user.id), access_jti)
    refresh_token_str = create_refresh_token(str(user.id), refresh_jti_str)

    exp_ts: int = decode_token(refresh_token_str)["exp"]
    expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    await repo.store_refresh_token(refresh_jti_uuid, user.id, expires_at)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token_str)


# ── POST /auth/refresh ───────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Rotate a refresh token: revoke old, issue new access + refresh pair (FR-16).

    Returns HTTP 401 on token reuse (old refresh token already revoked).
    """
    try:
        claims = decode_token(body.refresh_token)
    except JWTError as err:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token") from err

    if claims.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    jti_str: str = claims.get("jti", "")
    user_id_str: str = claims.get("user_id", "")
    try:
        jti_uuid = uuid.UUID(jti_str)
    except ValueError as err:
        raise HTTPException(status_code=401, detail="Invalid token") from err

    repo = UserRepository(db)
    stored = await repo.get_valid_refresh_token(jti_uuid)
    if stored is None:
        raise HTTPException(status_code=401, detail="Refresh token invalid or already used")

    # Revoke old; issue new
    await repo.revoke_refresh_token(jti_uuid)

    new_access_jti = str(uuid.uuid4())
    new_refresh_jti_str = str(uuid.uuid4())
    new_refresh_jti_uuid = uuid.UUID(new_refresh_jti_str)

    new_access_token = create_access_token(user_id_str, new_access_jti)
    new_refresh_token = create_refresh_token(user_id_str, new_refresh_jti_str)

    exp_ts2: int = decode_token(new_refresh_token)["exp"]
    new_expires_at = datetime.fromtimestamp(exp_ts2, tz=timezone.utc)
    await repo.store_refresh_token(new_refresh_jti_uuid, stored.user_id, new_expires_at)

    return TokenResponse(access_token=new_access_token, refresh_token=new_refresh_token)


# ── POST /auth/logout ────────────────────────────────────────────────────────


@router.post("/logout", status_code=204)
async def logout(
    current_user: TokenPayload = Depends(get_current_user),
) -> None:
    """Blacklist the current access token JTI (FR-16d).

    Subsequent requests with the same token receive HTTP 401.
    """
    if current_user.is_guest or not current_user.jti:
        return

    exp: int = current_user.claims.get("exp", 0)
    now = int(datetime.now(timezone.utc).timestamp())
    ttl = max(1, exp - now)
    await blacklist_token(current_user.jti, ttl_seconds=ttl)
