"""woxpas-pipeline — ingestion pipeline stages and worker."""

from woxpas_pipeline.av import AvStage
from woxpas_pipeline.chunk import ChunkStage
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.detect import DetectStage
from woxpas_pipeline.embed import EmbedIndexStage
from woxpas_pipeline.extract import ExtractStage
from woxpas_pipeline.ingress import DuplicateDocument, IngressStage
from woxpas_pipeline.settings import ClamAVSettings, DevDekSettings, PipelineSettings
from woxpas_pipeline.summarize import SummarizeStage
from woxpas_pipeline.supersession import SupersessionStage

__all__ = [
    "AvStage",
    "ChunkStage",
    "ClamAVSettings",
    "DetectStage",
    "DevDekSettings",
    "DuplicateDocument",
    "EmbedIndexStage",
    "ExtractStage",
    "IngressStage",
    "PipelineContext",
    "PipelineSettings",
    "SummarizeStage",
    "SupersessionStage",
]
