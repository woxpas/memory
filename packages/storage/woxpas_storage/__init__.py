"""woxpas-storage — Postgres (RLS), Qdrant, encrypted object store.

Single import point for the rest of the system. Each subpackage is self-contained
so the API gateway can construct only what it needs (e.g., test runs may skip Qdrant).
"""

from woxpas_storage.envelope import (
    FreshTenantSecrets,
    UnlockedTenant,
    WrappedDek,
    derive_kek,
    derive_kek_from_params,
    generate_dek,
    hash_verifier,
    init_tenant_secrets,
    make_kdf_params,
    needs_rehash,
    unwrap_dek,
    verify_passphrase,
    wrap_dek,
)
from woxpas_storage.health import aggregate_health
from woxpas_storage.objstore import ObjectStore
from woxpas_storage.postgres import (
    CanonicalBlockRepo,
    ChunkRepo,
    ConflictRepo,
    DocScopeRepo,
    DocumentRepo,
    EntityProfileFactRepo,
    EntityRepo,
    IdeaRepo,
    OutboxPublisher,
    OutboxRepo,
    PipelineRunRepo,
    PostgresPool,
    PredicateRepo,
    RelationRepo,
    ScopeRepo,
    SourceCredentialRepo,
    SourceRepo,
    SpanRepo,
    TaskRepo,
    TimelineRepo,
    compute_dedup_key,
)
from woxpas_storage.qdrant import (
    ChunkPoint,
    Collection,
    QdrantClient,
    ScopePoint,
    SummaryPoint,
    Vec,
)
from woxpas_storage.settings import (
    ObjectStoreSettings,
    PostgresSettings,
    QdrantSettings,
    StorageSettings,
)

__all__ = [
    "CanonicalBlockRepo",
    "ChunkPoint",
    "ChunkRepo",
    "Collection",
    "ConflictRepo",
    "DocScopeRepo",
    "DocumentRepo",
    "EntityProfileFactRepo",
    "EntityRepo",
    # envelope
    "FreshTenantSecrets",
    "IdeaRepo",
    # objstore
    "ObjectStore",
    "ObjectStoreSettings",
    "OutboxPublisher",
    "OutboxRepo",
    "PipelineRunRepo",
    # postgres
    "PostgresPool",
    "PostgresSettings",
    "PredicateRepo",
    # qdrant
    "QdrantClient",
    "QdrantSettings",
    "RelationRepo",
    "ScopePoint",
    "ScopeRepo",
    "SourceCredentialRepo",
    "SourceRepo",
    "SpanRepo",
    # settings
    "StorageSettings",
    "SummaryPoint",
    "TaskRepo",
    "TimelineRepo",
    "UnlockedTenant",
    "Vec",
    "WrappedDek",
    # health
    "aggregate_health",
    "compute_dedup_key",
    "derive_kek",
    "derive_kek_from_params",
    "generate_dek",
    "hash_verifier",
    "init_tenant_secrets",
    "make_kdf_params",
    "needs_rehash",
    "unwrap_dek",
    "verify_passphrase",
    "wrap_dek",
]
