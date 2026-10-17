"""JWT bearer authentication with scope checks.

Phase 1: tokens are signed locally (HS256) by /auth/token for development.
Phase 4: swap to Microsoft Entra ID (OIDC) - validate RS256 tokens against the
tenant's JWKS endpoint. The `require_scope` dependency stays the same, which is
why scopes follow SMART on FHIR naming (system/Coverage.read).
"""
import os
import time

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me-in-env-file-32b+")
ALGORITHM = "HS256"
ISSUER = "lshp-dev-auth"
AUDIENCE = "prior-auth-api"

bearer = HTTPBearer(auto_error=False)


def issue_token(client_id: str, scopes: list[str], ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode({"sub": client_id, "iss": ISSUER, "aud": AUDIENCE, "iat": now,
                       "exp": now + ttl_seconds, "scope": " ".join(scopes)}, SECRET, ALGORITHM)


def current_claims(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return jwt.decode(creds.credentials, SECRET, algorithms=[ALGORITHM],
                          audience=AUDIENCE, issuer=ISSUER)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}")


def require_scope(scope: str):
    def checker(claims: dict = Depends(current_claims)) -> dict:
        if scope not in claims.get("scope", "").split():
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing scope {scope}")
        return claims
    return checker
