from contextlib import contextmanager
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from digues_webapp.knowledge.indexer import (
    index_repository,
    chunk_markdown,
    discover_documents,
)
from digues_webapp.knowledge.repository import (
    Chunk,
    KnowledgeRepository,
    StoredDocument,
)
from digues_webapp.knowledge.search import (
    MAX_RESULT_LIMIT,
    KnowledgeSearchError,
    search_sirs_knowledge,
)
from digues_webapp.database import PostgreSQLConfig
from dotenv import load_dotenv


class MemoryRepository:
    def __init__(self):
        self.stored = {}
        self.chunks = {}
        self.next_id = 1

    def fts_config(self):
        return "pg_catalog.french"

    def documents(self):
        return dict(self.stored)

    def upsert_document(self, *, path, title, checksum, content):
        current = self.stored.get(path)
        document_id = current.id if current else str(self.next_id)
        if current is None:
            self.next_id += 1
        self.stored[path] = StoredDocument(document_id, checksum)
        return document_id

    def replace_chunks(self, document_id, chunks, *, fts_config):
        self.chunks[document_id] = tuple(chunks)

    def delete_documents(self, document_ids):
        identifiers = set(document_ids)
        removed = 0
        for path, document in list(self.stored.items()):
            if document.id in identifiers:
                del self.stored[path]
                self.chunks.pop(document.id, None)
                removed += 1
        return removed


class KnowledgeIndexerTest(unittest.TestCase):
    def test_git_discovery_excludes_untracked_and_non_markdown_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "webapp/docs").mkdir(parents=True)
            (root / "README.md").write_text("# Racine\n\nTexte.", encoding="utf-8")
            (root / "docs/tracked.md").write_text("# Suivi\n\nTexte.", encoding="utf-8")
            (root / "docs/untracked.md").write_text("# Non suivi\n\nTexte.", encoding="utf-8")
            (root / "webapp/README.md").write_text("# Webapp\n\nTexte.", encoding="utf-8")
            (root / "webapp/docs/tracked.md").write_text("# Web\n\nTexte.", encoding="utf-8")
            (root / "webapp/docs/swagger.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                [
                    "git", "-C", str(root), "add", "README.md", "docs/tracked.md",
                    "webapp/README.md", "webapp/docs/tracked.md",
                    "webapp/docs/swagger.json",
                ],
                check=True,
            )

            documents = discover_documents(root)

        self.assertEqual(
            [item.path for item in documents],
            ["README.md", "docs/tracked.md", "webapp/README.md", "webapp/docs/tracked.md"],
        )

    def test_discovers_only_explicit_tracked_markdown_corpus(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "README.md": "# SIRS\n\nPrésentation.",
                "docs/guide.md": "# Guide\n\nProcédure.",
                "docs/untracked.md": "# Ignoré\n\nNon versionné.",
                "docs/code.py": "print('no')",
                "webapp/README.md": "# Webapp\n\nDocumentation web.",
                "webapp/docs/interface.md": "# Interface\n\nDocumentation versionnée.",
                "webapp/docs/Modèle_ARPEGE_swagger.json": "{}",
                "private/secret.md": "# Secret\n\nInterdit.",
                "tests/fixture.md": "# Test\n\nInterdit.",
            }
            for path, content in paths.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            documents = discover_documents(root, tracked_paths=(
                "README.md",
                "docs/guide.md",
                "docs/code.py",
                "webapp/README.md",
                "webapp/docs/interface.md",
                "webapp/docs/Modèle_ARPEGE_swagger.json",
                "private/secret.md",
                "tests/fixture.md",
            ))

        self.assertEqual(
            [item.path for item in documents],
            [
                "README.md",
                "docs/guide.md",
                "webapp/README.md",
                "webapp/docs/interface.md",
            ],
        )
        self.assertNotIn("docs/untracked.md", [item.path for item in documents])
        self.assertNotIn(
            "webapp/docs/Modèle_ARPEGE_swagger.json",
            [item.path for item in documents],
        )

    def test_chunking_preserves_heading_order_and_non_empty_text(self):
        chunks = chunk_markdown(
            "# Manuel\n\nIntroduction.\n\n## Architecture\n\nPremier passage.\n\n"
            "### API\n\n- ligne 1\n- ligne 2\n\n```text\n# Pas un titre\n\ncode\n```\n\n"
            "## Exploitation\n\nDernier passage."
        )

        self.assertEqual([item.ordinal for item in chunks], list(range(len(chunks))))
        self.assertEqual(
            [item.heading for item in chunks],
            ["Manuel", "Manuel > Architecture", "Manuel > Architecture > API", "Manuel > Exploitation"],
        )
        self.assertIn("ligne 2", chunks[2].content)
        self.assertIn("# Pas un titre", chunks[2].content)
        self.assertTrue(all(item.content.strip() for item in chunks))

    def test_index_is_idempotent_and_updates_and_deletes_documents(self):
        repository = MemoryRepository()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "README.md").write_text("# SIRS\n\nVersion 1.", encoding="utf-8")
            guide = root / "docs/guide.md"
            guide.write_text("# Guide\n\nTexte.", encoding="utf-8")
            paths = ("README.md", "docs/guide.md")

            first = index_repository(root, repository, tracked_paths=paths)
            first_chunk_count = sum(len(items) for items in repository.chunks.values())
            second = index_repository(root, repository, tracked_paths=paths)
            second_chunk_count = sum(len(items) for items in repository.chunks.values())
            guide.write_text("# Guide\n\nTexte modifié.", encoding="utf-8")
            modified = index_repository(root, repository, tracked_paths=paths)
            guide.unlink()
            deleted = index_repository(root, repository, tracked_paths=("README.md",))

        self.assertEqual((first.created_or_updated, first.unchanged), (2, 0))
        self.assertEqual((second.created_or_updated, second.unchanged), (0, 2))
        self.assertEqual(first_chunk_count, second_chunk_count)
        self.assertEqual((modified.created_or_updated, modified.unchanged), (1, 1))
        self.assertEqual(deleted.deleted, 1)
        self.assertEqual(set(repository.stored), {"README.md"})


class FakeSearchCursor:
    def __init__(self, rows, row=None):
        self.rows = rows
        self.row = row
        self.query = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query = str(query)
        self.params = params

    def executemany(self, query, params):
        self.query = str(query)
        self.params = list(params)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeSearchConnection:
    def __init__(self, rows, row=None):
        self.cursor_instance = FakeSearchCursor(rows, row=row)

    def cursor(self):
        return self.cursor_instance


class KnowledgeSearchTest(unittest.TestCase):
    def test_fts_config_reads_catalogs_and_falls_back_to_simple(self):
        available = FakeSearchConnection([], row=(True,))
        fallback = FakeSearchConnection([], row=(False,))

        self.assertEqual(
            KnowledgeRepository(available).fts_config(),
            "pg_catalog.french",
        )
        self.assertEqual(
            KnowledgeRepository(fallback).fts_config(),
            "pg_catalog.simple",
        )
        query = available.cursor_instance.query
        self.assertIn("SELECT EXISTS", query)
        self.assertIn("FROM pg_catalog.pg_ts_config AS c", query)
        self.assertIn("JOIN pg_catalog.pg_namespace AS n", query)
        self.assertIn("ON n.oid = c.cfgnamespace", query)
        self.assertIn("n.nspname = 'pg_catalog'", query)
        self.assertIn("c.cfgname = %s::name", query)
        self.assertEqual(
            available.cursor_instance.params,
            ("french",),
        )

    def test_chunk_insert_casts_parameter_six_and_all_column_values(self):
        connection = FakeSearchConnection([])
        repository = KnowledgeRepository(connection)

        repository.replace_chunks(
            "00000000-0000-0000-0000-000000000001",
            [Chunk(ordinal=0, heading=None, content="Contenu")],
            fts_config="pg_catalog.french",
        )

        query = connection.cursor_instance.query
        params = connection.cursor_instance.params[0]
        self.assertEqual(
            " ".join(query.split()),
            "INSERT INTO public.knowledge_chunks ( document_id, ordinal, heading, "
            "content, search_vector, metadata ) VALUES ( %s::uuid, %s::integer, "
            "%s::text, %s::text, pg_catalog.to_tsvector( "
            "%s::pg_catalog.regconfig, pg_catalog.concat_ws( ' '::text, %s::text, "
            "%s::text ) ), pg_catalog.jsonb_build_object( 'fts_config'::text, "
            "%s::text ) )",
        )
        self.assertIsNone(params[5])
        self.assertEqual(params[5], params[2])

    def test_search_returns_ranked_structured_results_with_limit(self):
        rows = [
            ("doc-1", "chunk-1", "Guide", "docs/guide.md", "Architecture", "PostgreSQL métier", 0.8),
            ("doc-2", "chunk-2", "README", "README.md", None, "PostgreSQL", 0.4),
        ]
        connection = FakeSearchConnection(rows)
        repository = KnowledgeRepository(connection)

        results = repository.search(
            "PostgreSQL", limit=2, fts_config="pg_catalog.french"
        )

        self.assertEqual([item["rank"] for item in results], [0.8, 0.4])
        self.assertEqual(results[0]["heading"], "Architecture")
        self.assertIn("ts_rank_cd", connection.cursor_instance.query)
        self.assertIn("pg_catalog.websearch_to_tsquery", connection.cursor_instance.query)
        self.assertIn("%s::pg_catalog.regconfig", connection.cursor_instance.query)
        self.assertIn("%s::text", connection.cursor_instance.query)
        self.assertIn("ORDER BY rank DESC", connection.cursor_instance.query)
        self.assertEqual(connection.cursor_instance.params[-1], 2)

    def test_search_bounds_limit_and_returns_no_result(self):
        class Repository:
            def __init__(self, _connection):
                pass

            def fts_config(self):
                return "pg_catalog.simple"

            def search(self, query, *, limit, fts_config):
                self.values = (query, limit, fts_config)
                captured.append(self.values)
                return []

        captured = []

        @contextmanager
        def connection_factory():
            yield object()

        from unittest.mock import patch
        with patch("digues_webapp.knowledge.search.KnowledgeRepository", Repository):
            result = search_sirs_knowledge(
                "terme absent", limit=999, connection_factory=connection_factory
            )

        self.assertEqual(result, {"results": []})
        self.assertEqual(captured[0], ("terme absent", MAX_RESULT_LIMIT, "pg_catalog.simple"))

    def test_search_rejects_empty_or_oversized_query(self):
        for query in ("   ", "x" * 501):
            with self.subTest(length=len(query)), self.assertRaises(KnowledgeSearchError):
                search_sirs_knowledge(query)


class KnowledgePostgreSQLIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg

            load_dotenv(Path(__file__).resolve().parents[2] / "config.env", override=False)
            cls.connection = psycopg.connect(
                **PostgreSQLConfig.from_env().connect_kwargs(autocommit=False)
            )
            with cls.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.knowledge_documents'), "
                    "to_regclass('public.knowledge_chunks')"
                )
                if cursor.fetchone() != ("knowledge_documents", "knowledge_chunks"):
                    raise unittest.SkipTest("Tables knowledge absentes ; exécuter init-schema")
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL local indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.rollback()
            connection.close()

    def setUp(self):
        self.connection.execute("SAVEPOINT knowledge_test")
        self.connection.execute(
            "DELETE FROM public.knowledge_documents WHERE source_type = 'sirs_repository'"
        )

    def tearDown(self):
        self.connection.execute("ROLLBACK TO SAVEPOINT knowledge_test")
        self.connection.execute("RELEASE SAVEPOINT knowledge_test")

    def test_real_index_search_idempotence_and_chunk_relation(self):
        repository = KnowledgeRepository(self.connection)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "README.md").write_text(
                "# SIRS\n\nArchitecture générale PostgreSQL.\n\n## Cartographie\n\nCarte Leaflet.",
                encoding="utf-8",
            )
            (root / "docs/guide.md").write_text(
                "# Guide\n\nProcédure hydraulique documentée.", encoding="utf-8"
            )
            paths = ("README.md", "docs/guide.md")
            first = index_repository(root, repository, tracked_paths=paths)
            second = index_repository(root, repository, tracked_paths=paths)

        results = repository.search(
            "hydraulique", limit=5, fts_config=repository.fts_config()
        )
        self.assertEqual((first.created_or_updated, second.unchanged), (2, 2))
        self.assertEqual(results[0]["path"], "docs/guide.md")
        self.assertGreater(results[0]["rank"], 0)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT chunk.document_id)
                FROM public.knowledge_chunks AS chunk
                JOIN public.knowledge_documents AS document ON document.id = chunk.document_id
                WHERE document.source_type = 'sirs_repository'
                """
            )
            chunk_count, document_count = cursor.fetchone()
        self.assertGreaterEqual(chunk_count, 2)
        self.assertEqual(document_count, 2)


if __name__ == "__main__":
    unittest.main()
