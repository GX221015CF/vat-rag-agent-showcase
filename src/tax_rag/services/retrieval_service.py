from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

from tax_rag.config import load_toml, project_path
from tax_rag.retrieval.hybrid import retrieve_with_plan


class ActiveRetrievalService:
    def __init__(
        self,
        *,
        knowledge_path: Path | None = None,
        chroma_source_path: Path | None = None,
        chroma_snapshot_root: Path | None = None,
    ) -> None:
        self.app = load_toml("app.toml")
        self.runtime = load_toml("runtime.toml")
        self.retrieval_config = load_toml("retrieval.toml")
        self.knowledge_path = knowledge_path or project_path(self.app["project"]["sqlite_path"])
        self.chroma_source_path = chroma_source_path or project_path(self.app["project"]["chroma_path"])
        self.chroma_snapshot_root = chroma_snapshot_root or project_path(
            self.runtime["runtime"]["chroma_snapshot_root"]
        )
        self._query_copy_lock = threading.Lock()
        self._query_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._query_copy_root: Path | None = None

    def chroma_snapshot(self, generation_id: str) -> Path:
        """Return the formal runtime snapshot used only as an isolated-copy source."""
        target = self.chroma_snapshot_root / generation_id
        if target.exists():
            return target
        self.chroma_snapshot_root.mkdir(parents=True, exist_ok=True)
        temporary = self.chroma_snapshot_root / f".{generation_id}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copytree(self.chroma_source_path, temporary)
            temporary.replace(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return target

    def isolated_chroma_copy(self, generation_id: str) -> Path:
        """Return a process-local query copy; Chroma may write metadata only there."""
        source = self.chroma_snapshot(generation_id)
        with self._query_copy_lock:
            if self._query_tempdir is None:
                self._query_tempdir = tempfile.TemporaryDirectory(
                    prefix="vat-rag-query-",
                    ignore_cleanup_errors=True,
                )
                self._query_copy_root = Path(self._query_tempdir.name)
            if self._query_copy_root is None:
                raise RuntimeError("查询隔离目录初始化失败")
            target = self._query_copy_root / generation_id
            if target.exists():
                return target
            temporary = self._query_copy_root / f".{generation_id}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copytree(source, temporary)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return target

    def close(self) -> None:
        """Release the process-local query copy without touching formal snapshots."""
        with self._query_copy_lock:
            temporary_directory = self._query_tempdir
            self._query_tempdir = None
            self._query_copy_root = None
        if temporary_directory is not None:
            temporary_directory.cleanup()

    def connect_readonly(self) -> sqlite3.Connection:
        uri = f"file:{self.knowledge_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def active_generation(self, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        owns_connection = connection is None
        connection = connection or self.connect_readonly()
        try:
            row = connection.execute(
                """
                SELECT g.* FROM index_generations g
                JOIN runtime_state r ON r.key='active_generation_id' AND r.value=g.generation_id
                WHERE g.status='ACTIVE'
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("没有可用的正式活动索引")
            return row
        finally:
            if owns_connection:
                connection.close()

    def _search_with_plan(self, query: str):
        connection = self.connect_readonly()
        try:
            generation = self.active_generation(connection)
            chroma_path = self.isolated_chroma_copy(generation["generation_id"])
            hits, channels, query_plan = retrieve_with_plan(
                connection,
                chroma_path,
                generation["collection_name"],
                generation["embedding_model"],
                self.app["embedding"]["base_url"],
                query,
                self.retrieval_config,
            )
            return generation, hits, channels, query_plan
        finally:
            connection.close()

    def search(self, query: str):
        generation, hits, channels, _query_plan = self._search_with_plan(query)
        return generation, hits, channels

    def search_with_trace(self, query: str):
        """Return the deterministic query plan for diagnostics without persisting it."""
        return self._search_with_plan(query)

    def knowledge_overview(self) -> dict:
        connection = self.connect_readonly()
        try:
            generation = self.active_generation(connection)
            documents = connection.execute(
                """
                SELECT d.document_id,d.title,d.legal_level,d.approval_status,
                       v.source_filename,v.source_sha256,v.parser_rule_version,
                       COUNT(c.chunk_id) AS chunk_count
                FROM documents d
                JOIN document_versions v ON v.version_id=d.current_version_id
                JOIN chunks c ON c.version_id=v.version_id
                GROUP BY d.document_id,d.title,d.legal_level,d.approval_status,
                         v.source_filename,v.source_sha256,v.parser_rule_version
                ORDER BY d.title
                """
            ).fetchall()
            return {"generation": generation, "documents": documents}
        finally:
            connection.close()
