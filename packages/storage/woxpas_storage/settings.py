"""Settings for storage clients.

Loaded from environment variables (via pydantic-settings). The boot service
injects secrets into the process environment after passphrase entry; this module
never reads from .env in production.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host:     str = "postgres"
    port:     int = 5432
    db:       str = "woxpas"
    user:     str = "woxpas"
    password: SecretStr = SecretStr("")
    app_role: str = "woxpas_app"           # SET ROLE after acquire (least privilege)

    pool_min_size:        int = 4
    pool_max_size:        int = 32
    pool_max_queries:     int = 50000
    pool_max_inactive_connection_lifetime: float = 300.0
    statement_cache_size: int = 1024
    command_timeout_s:    float = 30.0

    def dsn(self) -> str:
        pw = self.password.get_secret_value()
        return f"postgresql://{self.user}:{pw}@{self.host}:{self.port}/{self.db}"


class QdrantSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QDRANT_", extra="ignore")

    host:     str = "qdrant"
    port:     int = 6333
    grpc_port: int = 6334
    api_key:  SecretStr = SecretStr("")
    prefer_grpc: bool = True               # gRPC for hot internal hops
    timeout_s: float = 10.0


class ObjectStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINIO_", extra="ignore")

    endpoint:        str = "minio:9000"
    root_user:       str = "woxpas"
    root_password:   SecretStr = SecretStr("")
    use_tls:         bool = False
    region:          str = "us-east-1"
    bucket_raw:        str = "woxpas-raw"
    bucket_canonical:  str = "woxpas-canonical"
    bucket_summaries:  str = "woxpas-summaries"

    @property
    def endpoint_url(self) -> str:
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{self.endpoint}"


class StorageSettings(BaseSettings):
    """Top-level aggregator. Convenient single-import in clients."""
    model_config = SettingsConfigDict(extra="ignore")

    postgres:  PostgresSettings  = Field(default_factory=PostgresSettings)
    qdrant:    QdrantSettings    = Field(default_factory=QdrantSettings)
    objstore:  ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
