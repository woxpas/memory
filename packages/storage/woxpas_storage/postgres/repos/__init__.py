"""Repository classes. Each takes an asyncpg.Connection acquired from a
PostgresPool.tenant_session() — RLS context is the caller's responsibility."""

from woxpas_storage.postgres.repos.canonical import CanonicalBlockRepo
from woxpas_storage.postgres.repos.chunks import ChunkRepo, SpanRepo
from woxpas_storage.postgres.repos.conflicts import ConflictRepo, compute_dedup_key
from woxpas_storage.postgres.repos.documents import DocumentRepo
from woxpas_storage.postgres.repos.entity_facts import EntityProfileFactRepo
from woxpas_storage.postgres.repos.extraction import (
    EntityRepo,
    IdeaRepo,
    RelationRepo,
    TaskRepo,
)
from woxpas_storage.postgres.repos.pipeline_runs import PipelineRunRepo
from woxpas_storage.postgres.repos.predicates import PredicateRepo
from woxpas_storage.postgres.repos.scopes import DocScopeRepo, ScopeRepo
from woxpas_storage.postgres.repos.sources import SourceCredentialRepo, SourceRepo
from woxpas_storage.postgres.repos.timeline import TimelineRepo

__all__ = [
    "CanonicalBlockRepo",
    "ChunkRepo",
    "ConflictRepo",
    "DocScopeRepo",
    "DocumentRepo",
    "EntityProfileFactRepo",
    "EntityRepo",
    "IdeaRepo",
    "PipelineRunRepo",
    "PredicateRepo",
    "RelationRepo",
    "ScopeRepo",
    "SourceCredentialRepo",
    "SourceRepo",
    "SpanRepo",
    "TaskRepo",
    "TimelineRepo",
    "compute_dedup_key",
]
