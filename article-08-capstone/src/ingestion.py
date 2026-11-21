"""Ingestion layer: contract enforcement, content-hash dedup, publish to documents.incoming."""

from dataclasses import dataclass
from typing import Optional, Protocol

from pydantic import ValidationError

from .contracts import DocumentEnvelope, IncomingDocument
from .messaging import Publisher
from .observability import MetricsRegistry


class SeenStore(Protocol):
    def add_if_new(self, key: str) -> bool: ...
    def discard(self, key: str) -> None: ...


class InMemorySeenStore:
    """Swap for a Postgres/Redis-backed store (INSERT ... ON CONFLICT DO NOTHING) in prod."""

    def __init__(self):
        self._seen: set[str] = set()

    def add_if_new(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def discard(self, key: str) -> None:
        self._seen.discard(key)


@dataclass
class IngestResult:
    accepted: bool
    document_id: Optional[str] = None
    duplicate: bool = False
    errors: Optional[list] = None
    envelope: Optional[DocumentEnvelope] = None


class IngestionService:
    def __init__(self, publisher: Publisher, topic: str = "documents.incoming",
                 seen: Optional[SeenStore] = None, metrics: Optional[MetricsRegistry] = None):
        self.publisher, self.topic = publisher, topic
        self.seen = seen or InMemorySeenStore()
        self.metrics = metrics or MetricsRegistry()

    def ingest_raw(self, payload: dict) -> IngestResult:
        """Entry for untrusted payloads: a doc missing source_type/source_id/timestamp is rejected."""
        try:
            doc = IncomingDocument(**payload)
        except ValidationError as exc:
            self.metrics.inc("ingest_documents_total", outcome="rejected")
            return IngestResult(accepted=False, errors=[
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()])
        return self.ingest(doc)

    def ingest(self, doc: IncomingDocument) -> IngestResult:
        document_id = f"{doc.pipeline}:{doc.content_hash[:32]}"   # content-addressed => idempotent
        if not self.seen.add_if_new(document_id):
            self.metrics.inc("ingest_documents_total", outcome="duplicate", source=doc.source_type.value)
            return IngestResult(accepted=False, document_id=document_id, duplicate=True)
        envelope = DocumentEnvelope(
            document_id=document_id, source_type=doc.source_type, source_id=doc.source_id,
            document_size=doc.document_size, timestamp=doc.timestamp, team=doc.team,
            pipeline=doc.pipeline, mime_type=doc.mime_type, content_b64=doc.content_b64)
        self.publisher.publish(self.topic, document_id, envelope.model_dump(mode="json"))
        self.metrics.inc("ingest_documents_total", outcome="accepted", source=doc.source_type.value)
        return IngestResult(accepted=True, document_id=document_id, envelope=envelope)

    def release(self, document_id: str) -> None:
        """Forget a document whose downstream processing failed, so a retry is not treated as a
        duplicate. Without this, any post-ingest failure (quota 429, gateway 5xx, crash) would
        make the document permanently unprocessable: every retry returns outcome=duplicate."""
        self.seen.discard(document_id)
