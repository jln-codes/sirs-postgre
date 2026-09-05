"""Persistance PostgreSQL du corpus documentaire SIRS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


SOURCE_TYPE = "sirs_repository"
FTS_CONFIG_SCHEMA = "pg_catalog"
FRENCH_FTS_NAME = "french"
FRENCH_FTS_CONFIG = f"{FTS_CONFIG_SCHEMA}.{FRENCH_FTS_NAME}"
FALLBACK_FTS_CONFIG = f"{FTS_CONFIG_SCHEMA}.simple"


@dataclass(frozen=True)
class StoredDocument:
    id: str
    checksum: str


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str | None
    content: str


class KnowledgeRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def fts_config(self) -> str:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_ts_config AS c
                    JOIN pg_catalog.pg_namespace AS n
                      ON n.oid = c.cfgnamespace
                    WHERE n.nspname = 'pg_catalog'
                      AND c.cfgname = %s::name
                )
                """,
                (FRENCH_FTS_NAME,),
            )
            row = cursor.fetchone()
        return FRENCH_FTS_CONFIG if row and bool(row[0]) else FALLBACK_FTS_CONFIG

    def documents(self) -> dict[str, StoredDocument]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text, path, checksum
                FROM public.knowledge_documents
                WHERE source_type = %s::text
                """,
                (SOURCE_TYPE,),
            )
            return {
                str(path): StoredDocument(id=str(document_id), checksum=str(checksum))
                for document_id, path, checksum in cursor.fetchall()
            }

    def upsert_document(
        self, *, path: str, title: str, checksum: str, content: str
    ) -> str:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.knowledge_documents (
                    source_type, path, title, checksum, content, indexed_at, updated_at
                )
                VALUES (
                    %s::text, %s::text, %s::text, %s::text, %s::text,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (source_type, path) DO UPDATE SET
                    title = EXCLUDED.title,
                    checksum = EXCLUDED.checksum,
                    content = EXCLUDED.content,
                    indexed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id::text
                """,
                (SOURCE_TYPE, path, title, checksum, content),
            )
            row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"Indexation impossible pour {path}.")
        return str(row[0])

    def replace_chunks(
        self, document_id: str, chunks: Iterable[Chunk], *, fts_config: str
    ) -> None:
        values = list(chunks)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.knowledge_chunks WHERE document_id = %s::uuid",
                (document_id,),
            )
            cursor.executemany(
                """
                INSERT INTO public.knowledge_chunks (
                    document_id, ordinal, heading, content, search_vector, metadata
                )
                VALUES (
                    %s::uuid, %s::integer, %s::text, %s::text,
                    pg_catalog.to_tsvector(
                        %s::pg_catalog.regconfig,
                        pg_catalog.concat_ws(
                            ' '::text, %s::text, %s::text
                        )
                    ),
                    pg_catalog.jsonb_build_object(
                        'fts_config'::text, %s::text
                    )
                )
                """,
                [
                    (
                        document_id,
                        chunk.ordinal,
                        chunk.heading,
                        chunk.content,
                        fts_config,
                        chunk.heading,
                        chunk.content,
                        fts_config,
                    )
                    for chunk in values
                ],
            )

    def delete_documents(self, document_ids: Iterable[str]) -> int:
        identifiers = list(document_ids)
        if not identifiers:
            return 0
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.knowledge_documents WHERE id = ANY(%s::uuid[])",
                (identifiers,),
            )
            return int(cursor.rowcount)

    def search(self, query: str, *, limit: int, fts_config: str) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                WITH requested_query AS (
                    SELECT pg_catalog.websearch_to_tsquery(
                        %s::pg_catalog.regconfig,
                        %s::text
                    ) AS value
                )
                SELECT
                    document.id::text,
                    chunk.id::text,
                    document.title,
                    document.path,
                    chunk.heading,
                    chunk.content,
                    ts_rank_cd(chunk.search_vector, requested_query.value) AS rank
                FROM public.knowledge_chunks AS chunk
                JOIN public.knowledge_documents AS document
                  ON document.id = chunk.document_id
                CROSS JOIN requested_query
                WHERE document.source_type = %s::text
                  AND chunk.search_vector @@ requested_query.value
                ORDER BY rank DESC, document.path, chunk.ordinal
                LIMIT %s::integer
                """,
                (fts_config, query, SOURCE_TYPE, limit),
            )
            rows = cursor.fetchall()
        return [
            {
                "document_id": str(row[0]),
                "chunk_id": str(row[1]),
                "title": str(row[2]),
                "path": str(row[3]),
                "heading": str(row[4]) if row[4] is not None else None,
                "content": str(row[5]),
                "rank": float(row[6]),
            }
            for row in rows
        ]
