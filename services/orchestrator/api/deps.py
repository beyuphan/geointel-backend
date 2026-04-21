"""
api/deps.py — FastAPI Dependency Injection

JWT token doğrulama ve kullanıcı çıkarma.
Tüm korumalı endpoint'ler `get_current_user` dependency'sini kullanır.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
import bcrypt
try:
    from loguru import logger as log
except ImportError:
    import logging
    log = logging.getLogger("deps")

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

JWT_SECRET = os.getenv("JWT_SECRET_KEY", "geointel-dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
REFRESH_TOKEN_EXPIRE_DAYS = 30

# FastAPI Security scheme — mobil app "Authorization: Bearer <token>" gönderir
security = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════════════════════════════════
# PASSWORD UTILS (bcrypt direkt — passlib artık bakımsız)
# ═══════════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """Şifreyi bcrypt ile hashler."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Düz metin şifreyi hash ile karşılaştırır."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN CREATION
# ═══════════════════════════════════════════════════════════════════════════

def create_access_token(user_id: str, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "username": username,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Token'ı decode eder. Geçersiz/süresi dolmuş ise None döner."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        log.debug(f"JWT decode hatası: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    Korumalı endpoint'ler için kullanılır.
    Authorization header'dan JWT token alır, doğrular, user bilgisi döner.
    
    Kullanım:
        @router.get("/protected")
        async def my_endpoint(user: dict = Depends(get_current_user)):
            print(user["user_id"], user["username"])
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Authorization header eksik."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_INVALID", "message": "Geçersiz veya süresi dolmuş token."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_TYPE_ERROR", "message": "Bu endpoint için access token gerekli."},
        )

    return {
        "user_id": payload["sub"],
        "username": payload.get("username", "unknown"),
    }


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """
    Opsiyonel auth — token varsa user döner, yoksa None.
    Hem auth'lu hem auth'suz çalışabilen endpoint'ler için.
    """
    if credentials is None:
        return None

    payload = decode_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        return None

    return {
        "user_id": payload["sub"],
        "username": payload.get("username", "unknown"),
    }
