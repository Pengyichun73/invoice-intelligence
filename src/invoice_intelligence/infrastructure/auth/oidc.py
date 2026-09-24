"""OIDC JWT verification with bounded JWKS caching."""

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import jwt

from invoice_intelligence.application.errors import UnauthorizedError
from invoice_intelligence.application.ports.auth import AuthContext


class OIDCJWTAuthContextProvider:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        client_id: str,
        algorithms: Sequence[str],
        tenant_claim: str,
        reviewer_claim: str,
        jwks_cache_seconds: float,
        timeout_seconds: float,
        tls_verify: bool,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks_url = jwks_url
        self._client_id = client_id
        self._algorithms = tuple(algorithms)
        self._tenant_claim = tenant_claim
        self._reviewer_claim = reviewer_claim
        self._jwks_cache_seconds = jwks_cache_seconds
        self._client = httpx.AsyncClient(timeout=timeout_seconds, verify=tls_verify)
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0
        self._lock = asyncio.Lock()

    async def authenticate(self, bearer_token: str) -> AuthContext:
        try:
            header = jwt.get_unverified_header(bearer_token)
            kid = _required_string(header, "kid")
            algorithm = _required_string(header, "alg")
            if algorithm not in self._algorithms:
                raise UnauthorizedError("JWT signing algorithm is not allowed")
            key = await self._key(kid)
            claims = jwt.decode(
                bearer_token,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            subject = _required_string(claims, "sub")
            tenant_id = _required_string(claims, self._tenant_claim)
            reviewer_id = _required_string(claims, self._reviewer_claim)
            return AuthContext(
                subject=subject,
                tenant_id=tenant_id,
                reviewer_id=reviewer_id,
                roles=_roles(claims, self._client_id),
                scopes=_scopes(claims),
            )
        except UnauthorizedError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise UnauthorizedError("Bearer token validation failed") from exc

    async def _key(self, kid: str) -> Any:
        key_missing = kid not in self._keys
        if time.monotonic() >= self._keys_expire_at or key_missing:
            await self._refresh_keys(force=key_missing)
        key = self._keys.get(kid)
        if key is None:
            raise UnauthorizedError("JWT signing key is not available")
        return key

    async def _refresh_keys(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() < self._keys_expire_at and self._keys:
                return
            try:
                response = await self._client.get(self._jwks_url)
                response.raise_for_status()
                payload = response.json()
                raw_keys = payload.get("keys") if isinstance(payload, dict) else None
                if not isinstance(raw_keys, list):
                    raise ValueError("OIDC JWKS does not contain a keys array")
                resolved: dict[str, Any] = {}
                for raw_key in raw_keys:
                    if not isinstance(raw_key, dict):
                        continue
                    kid = raw_key.get("kid")
                    if isinstance(kid, str) and kid:
                        resolved[kid] = jwt.PyJWK.from_dict(raw_key).key
                if not resolved:
                    raise ValueError("OIDC JWKS contains no usable signing keys")
                self._keys = resolved
                self._keys_expire_at = time.monotonic() + self._jwks_cache_seconds
            except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                raise UnauthorizedError("OIDC signing keys are unavailable") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _required_string(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnauthorizedError(f"Required JWT claim is missing: {name}")
    return value


def _roles(claims: Mapping[str, Any], client_id: str) -> frozenset[str]:
    values: set[str] = set()
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        values.update(_string_values(realm_access.get("roles")))
    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        client_access = resource_access.get(client_id)
        if isinstance(client_access, dict):
            values.update(_string_values(client_access.get("roles")))
    return frozenset(values)


def _scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    value = claims.get("scope", "")
    if isinstance(value, str):
        return frozenset(part for part in value.split() if part)
    return frozenset(_string_values(value))


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
