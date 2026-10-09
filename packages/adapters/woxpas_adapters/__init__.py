"""woxpas-adapters — capture-surface adapters that produce IngestEnvelopes.

Adapters never touch the database, vector store, or object store. They take
channel-specific input and yield (envelope, content). The pipeline does the rest.
"""

from woxpas_adapters.base import Adapter, IngestPayload
from woxpas_adapters.file import FileAdapter

__all__ = ["Adapter", "FileAdapter", "IngestPayload"]
