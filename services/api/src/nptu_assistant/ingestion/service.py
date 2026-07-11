from __future__ import annotations

from pathlib import Path
from typing import Protocol

import yaml

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.ingestion.chunking import TextChunk, chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import SUPPORTED_EXTENSIONS, parse_document
from nptu_assistant.providers.protocols import EmbeddingProvider


class DocumentRepository(Protocol):
    def has_hash(self, canonical_url: str, digest: str) -> bool: ...

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None: ...


class DocumentIngestionService:
    def __init__(
        self,
        directory: Path,
        repository: DocumentRepository,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._directory = directory
        self._repository = repository
        self._embedding_provider = embedding_provider

    def run(self, source_names: list[str] | None = None) -> IngestionSummary:
        del source_names
        summary = IngestionSummary()
        if not self._directory.exists():
            return summary
        for path in sorted(self._directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                metadata = self._load_metadata(path)
                raw_text = parse_document(path)
                digest = content_hash(raw_text)
                if self._repository.has_hash(str(metadata.source_url), digest):
                    summary.skipped += 1
                    continue
                chunks = chunk_text(raw_text)
                embeddings = self._embedding_provider.embed([chunk.content for chunk in chunks])
                self._repository.save(metadata, raw_text, chunks, embeddings)
                summary.created += 1
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return summary

    @staticmethod
    def _load_metadata(path: Path) -> DocumentMetadata:
        candidates = [path.with_suffix(".yaml"), path.with_suffix(".yml")]
        metadata_path = next((candidate for candidate in candidates if candidate.exists()), None)
        if metadata_path is None:
            raise ValueError("缺少同名 YAML metadata")
        payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("metadata 必須是 YAML object")
        return DocumentMetadata.model_validate(payload)
