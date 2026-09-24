from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from invoice_intelligence.api.routes.documents import router as documents_router
from invoice_intelligence.api.routes.evaluations import router as evaluations_router
from invoice_intelligence.api.routes.field_semantics import router as field_semantics_router
from invoice_intelligence.api.routes.memory import router as memory_router
from invoice_intelligence.api.routes.reviews import router as reviews_router
from invoice_intelligence.api.routes.runs import router as runs_router
from invoice_intelligence.api.routes.training import router as training_router
from invoice_intelligence.api.security import (
    enforce_request_security,
    permission_for_request,
)
from invoice_intelligence.application.errors import ForbiddenError, UnauthorizedError
from invoice_intelligence.application.ports.auth import AuthContext, SecurityAuditEvent
from invoice_intelligence.application.services.authorization import (
    AuthorizationPolicy,
    Permission,
)
from invoice_intelligence.config.settings import Environment, Settings
from invoice_intelligence.infrastructure.auth.oidc import OIDCJWTAuthContextProvider
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_repository import (
    SQLAlchemyBusinessRepository,
)
from invoice_intelligence.infrastructure.serialization.pydantic import (
    PydanticExtractionStateCodec,
)


class _AuthProvider:
    async def authenticate(self, bearer_token: str) -> AuthContext:
        assert bearer_token == "signed-token"
        return AuthContext(
            subject="subject-1",
            tenant_id="tenant-a",
            reviewer_id="reviewer-a",
            roles=frozenset({"invoice-reader"}),
            scopes=frozenset(),
        )


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[SecurityAuditEvent] = []

    async def record(self, event: SecurityAuditEvent) -> None:
        self.events.append(event)


def _request(
    path: str, headers: list[tuple[bytes, bytes]], *, method: str = "GET"
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def _dependencies(sink: _AuditSink) -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(
            api_prefix="/api/v1",
            environment=Environment.PRODUCTION,
            dev_tenant_id=None,
        ),
        authorization_policy=AuthorizationPolicy(),
        auth_context_provider=_AuthProvider(),
        security_audit_sink=sink,
    )


def test_authorization_policy_maps_roles_and_never_uses_reviewer_input() -> None:
    context = AuthContext(
        subject="subject-1",
        tenant_id="tenant-a",
        reviewer_id="reviewer-from-token",
        roles=frozenset({"invoice-reviewer"}),
        scopes=frozenset(),
    )
    policy = AuthorizationPolicy()

    policy.require(context, Permission.REVIEW_SUBMIT)
    with pytest.raises(ForbiddenError):
        policy.require(context, Permission.INDEX_ACTIVATE)
    trusted = policy.trusted_tenant_context(context, trace_id="trace-1")
    assert trusted.tenant_id == "tenant-a"
    assert trusted.actor_id == "reviewer-from-token"


def test_route_permission_matrix_covers_sensitive_actions() -> None:
    assert (
        permission_for_request("GET", "/api/v1/runs/run-1", "/api/v1")
        == Permission.DOCUMENT_READ
    )
    assert (
        permission_for_request("POST", "/api/v1/reviews/run-1", "/api/v1")
        == Permission.REVIEW_SUBMIT
    )
    assert (
        permission_for_request("GET", "/api/v1/reviews", "/api/v1")
        == Permission.REVIEW_READ
    )
    assert (
        permission_for_request(
            "POST", "/api/v1/reviews/review-1/claim", "/api/v1"
        )
        == Permission.REVIEW_SUBMIT
    )
    assert (
        permission_for_request(
            "POST", "/api/v1/memory/indexes/v1/activate", "/api/v1"
        )
        == Permission.INDEX_ACTIVATE
    )
    for method, path in (
        ("POST", "/api/v1/training/jobs"),
        ("GET", "/api/v1/training/jobs/job-1"),
        ("POST", "/api/v1/training/jobs/job-1/cancel"),
        ("POST", "/api/v1/training/jobs/job-1/retry"),
    ):
        assert permission_for_request(method, path, "/api/v1") == Permission.TRAINING_SUBMIT
    assert (
        permission_for_request("POST", "/api/v1/evaluations/suite-jobs", "/api/v1")
        == Permission.EVALUATION_RUN
    )


def test_every_business_route_has_an_explicit_permission() -> None:
    for router in (
        documents_router,
        evaluations_router,
        runs_router,
        reviews_router,
        memory_router,
        field_semantics_router,
        training_router,
    ):
        for route in router.routes:
            path = "/api/v1" + re.sub(r"\{[^}]+\}", "resource", route.path)
            for method in route.methods or ():
                assert permission_for_request(method, path, "/api/v1") is not None, (
                    method,
                    route.path,
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/api/v1/training/jobs"),
        ("GET", "/api/v1/training/jobs/job-1"),
        ("POST", "/api/v1/training/jobs/job-1/cancel"),
        ("POST", "/api/v1/training/jobs/job-1/retry"),
    ),
)
async def test_training_requires_submit_permission_and_audits_denial(
    method: str, path: str
) -> None:
    sink = _AuditSink()
    request = _request(path, [(b"authorization", b"Bearer signed-token")], method=method)

    async def unreachable(_: Request) -> Response:
        pytest.fail("Training route executed without training:submit")

    response = await enforce_request_security(request, unreachable, _dependencies(sink))

    assert response.status_code == 403
    assert sink.events[-1].event_type == "authorization_denied"
    assert sink.events[-1].required_permission == "training:submit"
    assert sink.events[-1].tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_training_cross_tenant_not_found_is_404_and_audited() -> None:
    sink = _AuditSink()
    request = _request("/api/v1/training/jobs/foreign-job", [])
    request.state.auth_context = AuthContext(
        subject="trainer-1",
        tenant_id="tenant-a",
        reviewer_id="trainer-1",
        roles=frozenset({"trainer"}),
        scopes=frozenset(),
    )

    async def not_found(_: Request) -> Response:
        return Response(status_code=404)

    response = await enforce_request_security(request, not_found, _dependencies(sink))

    assert response.status_code == 404
    assert request.state.trusted_tenant_context.tenant_id == "tenant-a"
    assert sink.events[-1].event_type == "resource_not_found_or_cross_tenant"
    assert sink.events[-1].required_permission == "training:submit"


@pytest.mark.asyncio
async def test_cross_tenant_style_not_found_is_404_and_security_audited() -> None:
    sink = _AuditSink()
    request = _request(
        "/api/v1/runs/foreign-run",
        [(b"authorization", b"Bearer signed-token")],
    )

    async def not_found(_: Request) -> Response:
        return Response(status_code=404)

    response = await enforce_request_security(request, not_found, _dependencies(sink))

    assert response.status_code == 404
    assert request.state.trusted_tenant_context.tenant_id == "tenant-a"
    assert sink.events[-1].event_type == "resource_not_found_or_cross_tenant"


@pytest.mark.asyncio
async def test_document_repository_requires_resource_id_and_tenant_id() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DocumentRow.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            DocumentRow(
                document_id="document-1",
                tenant_id="tenant-a",
                storage_uri="documents/document-1",
                mime_type="image/png",
                checksum="a" * 64,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    repository = SQLAlchemyBusinessRepository(engine, PydanticExtractionStateCodec())
    try:
        assert await repository.get_document("document-1", "tenant-a") is not None
        assert await repository.get_document("document-1", "tenant-b") is None
    finally:
        repository.close()


def test_oidc_rbac_migration_creates_and_seeds_security_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    migration = import_module("migrations.versions.20260923_0023_oidc_rbac")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {
            "auth_users",
            "auth_tenants",
            "auth_roles",
            "auth_permissions",
            "security_audit_events",
        }.issubset(tables)
        assert connection.scalar(text("SELECT count(*) FROM auth_permissions")) == 12
        assert connection.scalar(text("SELECT count(*) FROM auth_roles")) == 9
        migration.downgrade()


def test_production_rejects_development_identity_and_insecure_oidc() -> None:
    with pytest.raises(ValueError, match="dev_tenant_id"):
        Settings(
            _env_file=None,
            environment="production",
            dev_tenant_id="demo-tenant",
            auth_mode="oidc",
            oidc_issuer="https://id.example/realms/invoice",
            oidc_audience="invoice-intelligence-api",
            oidc_jwks_url="https://id.example/realms/invoice/certs",
        )
    with pytest.raises(ValueError, match="issuer must use HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            dev_tenant_id=None,
            auth_mode="oidc",
            oidc_issuer="http://id.example/realms/invoice",
            oidc_audience="invoice-intelligence-api",
            oidc_jwks_url="https://id.example/realms/invoice/certs",
        )


@pytest.mark.asyncio
async def test_oidc_provider_validates_signature_issuer_audience_and_claims() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "key-1", "alg": "RS256", "use": "sig"})
    provider = OIDCJWTAuthContextProvider(
        issuer="https://id.example/realms/invoice",
        audience="invoice-intelligence-api",
        jwks_url="https://id.example/realms/invoice/protocol/openid-connect/certs",
        client_id="invoice-intelligence-api",
        algorithms=("RS256",),
        tenant_claim="tenant_id",
        reviewer_claim="reviewer_id",
        jwks_cache_seconds=300,
        timeout_seconds=1,
        tls_verify=True,
    )
    provider._keys = {"key-1": jwt.PyJWK.from_dict(public_jwk).key}
    provider._keys_expire_at = float("inf")
    now = datetime.now(UTC)
    claims = {
        "iss": "https://id.example/realms/invoice",
        "aud": "invoice-intelligence-api",
        "sub": "subject-1",
        "tenant_id": "tenant-a",
        "reviewer_id": "reviewer-a",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "resource_access": {
            "invoice-intelligence-api": {"roles": ["document:read"]}
        },
        "scope": "review:read",
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    try:
        context = await provider.authenticate(token)
        assert context.tenant_id == "tenant-a"
        assert context.reviewer_id == "reviewer-a"
        assert context.roles == frozenset({"document:read"})
        assert context.scopes == frozenset({"review:read"})

        wrong_audience = jwt.encode(
            {**claims, "aud": "other-api"},
            private_key,
            algorithm="RS256",
            headers={"kid": "key-1"},
        )
        with pytest.raises(UnauthorizedError):
            await provider.authenticate(wrong_audience)
    finally:
        await provider.aclose()
