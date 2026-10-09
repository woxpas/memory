"""API gateway settings."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore")

    host: str = "127.0.0.1"           # localhost-only by default
    port: int = 7800
    workers: int = 1                  # uvicorn workers; raise after profiling

    # Auth mode:
    #   - 'jwt'                  production: requires WOXPAS_JWT_PUBLIC_KEY /
    #                            WOXPAS_JWT_AUDIENCE / WOXPAS_JWT_ISSUER
    #   - 'dev'                  local: trusts X-Woxpas-Tenant-Id header
    #                            (warns on every request)
    #   - 'single_tenant_local'  local single-user installs: auto-uses
    #                            WOXPAS_DEV_TENANT_ID_DEFAULT when no header
    #                            is sent. STILL allows the header to override
    #                            (useful for ops). Pillar-3 (security): only
    #                            safe when API_HOST is loopback-only AND the
    #                            install really is single-user. Defaults still
    #                            require a header in stricter modes.
    auth_mode: str = "dev"

    # Limits
    max_upload_bytes: int = 50 * 1024 * 1024   # 50 MB per file at v0.1
    request_timeout_s: int = 60
    enable_docs: bool = False                  # OpenAPI/swagger off in production

    # Logging
    log_level:  str = "INFO"
    log_format: str = "json"

    # Phase 5a.5+ — HTTP API audit log integration. Mirror MCP layer's
    # AuditWriter levels so direct HTTP callers (woxpas-cli, custom
    # clients, browser) get the same audit trail agent calls produce.
    audit_level: str = "full"        # full | errors_only | off


class JwtSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WOXPAS_JWT_", extra="ignore")

    public_key: SecretStr = SecretStr("")     # PEM string; loaded by API on startup
    audience:   str = "woxpas"
    issuer:     str = "woxpas"
    algorithms: list[str] = ["RS256", "ES256"]
