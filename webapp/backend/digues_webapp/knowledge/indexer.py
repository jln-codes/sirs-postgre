"""Indexation explicite des fichiers Markdown versionnés du dépôt SIRS."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterable, Protocol

from ..database import open_write_connection
from .repository import Chunk, KnowledgeRepository, StoredDocument


MAX_CHUNK_CHARS = 3_000
ALLOWED_SOURCE_PATTERNS = (
    "README.md",
    "docs/**/*.md",
    "webapp/README.md",
    "webapp/docs/**/*.md",
)
REPOSITORY_ROOT_ENV = "SIRS_REPOSITORY_ROOT"
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")


@dataclass(frozen=True)
class SourceDocument:
    path: str
    title: str
    checksum: str
    content: str
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class IndexReport:
    discovered: int
    created_or_updated: int
    unchanged: int
    deleted: int


class RepositoryWriter(Protocol):
    def fts_config(self) -> str: ...
    def documents(self) -> dict[str, StoredDocument]: ...
    def upsert_document(
        self, *, path: str, title: str, checksum: str, content: str
    ) -> str: ...
    def replace_chunks(
        self, document_id: str, chunks: Iterable[Chunk], *, fts_config: str
    ) -> None: ...
    def delete_documents(self, document_ids: Iterable[str]) -> int: ...


def _is_allowed_path(value: str) -> bool:
    path = PurePosixPath(value)
    in_docs = len(path.parts) >= 2 and path.parts[0] == "docs"
    in_webapp_docs = len(path.parts) >= 3 and path.parts[:2] == ("webapp", "docs")
    return (
        value in {"README.md", "webapp/README.md"}
        or ((in_docs or in_webapp_docs) and path.suffix.lower() == ".md")
    ) and ".." not in path.parts


def _git_tracked_paths(root: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--",
                "README.md",
                "docs",
                "webapp/README.md",
                "webapp/docs",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "La racine documentaire doit être un checkout Git accessible."
        ) from exc
    return tuple(
        path.decode("utf-8")
        for path in result.stdout.split(b"\0")
        if path and _is_allowed_path(path.decode("utf-8"))
    )


def _split_long_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    parts: list[str] = []
    current = ""
    for line in block.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            parts.append(current.rstrip())
            current = ""
        while len(line) > limit:
            available = limit - len(current)
            current += line[:available]
            parts.append(current.rstrip())
            current = ""
            line = line[available:]
        current += line
    if current.strip():
        parts.append(current.rstrip())
    return parts


def _chunks_for_section(heading: str | None, body: str) -> list[tuple[str | None, str]]:
    blocks: list[str] = []
    block_lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker:
            fence = None if fence == marker else marker if fence is None else fence
        if not line.strip() and fence is None:
            if block_lines:
                blocks.append("\n".join(block_lines).strip())
                block_lines = []
            continue
        block_lines.append(line)
    if block_lines:
        blocks.append("\n".join(block_lines).strip())
    chunks: list[tuple[str | None, str]] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        for part in _split_long_block(block, MAX_CHUNK_CHARS):
            separator = 2 if current else 0
            if current and current_size + separator + len(part) > MAX_CHUNK_CHARS:
                chunks.append((heading, "\n\n".join(current)))
                current = []
                current_size = 0
                separator = 0
            current.append(part)
            current_size += separator + len(part)
    if current:
        chunks.append((heading, "\n\n".join(current)))
    return chunks


def chunk_markdown(content: str) -> tuple[Chunk, ...]:
    sections: list[tuple[str | None, str]] = []
    heading_stack: list[tuple[int, str]] = []
    current_heading: str | None = None
    body: list[str] = []
    fence: str | None = None

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append((current_heading, text))

    for line in content.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker:
            fence = None if fence == marker else marker if fence is None else fence
            body.append(line)
            continue
        if fence is not None:
            body.append(line)
            continue
        match = HEADING_PATTERN.match(line)
        if not match:
            body.append(line)
            continue
        flush()
        body = []
        level = len(match.group(1))
        title = match.group(2).strip().rstrip("#").strip()
        heading_stack = [item for item in heading_stack if item[0] < level]
        heading_stack.append((level, title))
        current_heading = " > ".join(item[1] for item in heading_stack)
    flush()

    raw_chunks = [
        item
        for heading, section in sections
        for item in _chunks_for_section(heading, section)
    ]
    return tuple(
        Chunk(ordinal=index, heading=heading, content=text)
        for index, (heading, text) in enumerate(raw_chunks)
        if text.strip()
    )


def _document_title(path: str, content: str) -> str:
    for line in content.splitlines():
        match = HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip().rstrip("#").strip()
    return "README" if path in {"README.md", "webapp/README.md"} else PurePosixPath(path).stem


def discover_documents(
    root: Path, *, tracked_paths: Iterable[str] | None = None
) -> tuple[SourceDocument, ...]:
    resolved_root = root.resolve(strict=True)
    candidates = tuple(tracked_paths) if tracked_paths is not None else _git_tracked_paths(root)
    documents: list[SourceDocument] = []
    for relative_path in sorted(set(candidates)):
        if not _is_allowed_path(relative_path):
            continue
        source = root / PurePosixPath(relative_path)
        if source.is_symlink() or not source.is_file():
            continue
        resolved_source = source.resolve(strict=True)
        if not resolved_source.is_relative_to(resolved_root):
            continue
        content = source.read_text(encoding="utf-8")
        chunks = chunk_markdown(content)
        if not chunks:
            continue
        documents.append(SourceDocument(
            path=relative_path,
            title=_document_title(relative_path, content),
            checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            content=content,
            chunks=chunks,
        ))
    return tuple(documents)


def index_repository(
    root: Path,
    repository: RepositoryWriter,
    *,
    tracked_paths: Iterable[str] | None = None,
) -> IndexReport:
    sources = discover_documents(root, tracked_paths=tracked_paths)
    existing = repository.documents()
    fts_config = repository.fts_config()
    changed = 0
    unchanged = 0
    retained_paths: set[str] = set()
    for source in sources:
        retained_paths.add(source.path)
        stored = existing.get(source.path)
        if stored and stored.checksum == source.checksum:
            unchanged += 1
            continue
        document_id = repository.upsert_document(
            path=source.path,
            title=source.title,
            checksum=source.checksum,
            content=source.content,
        )
        repository.replace_chunks(document_id, source.chunks, fts_config=fts_config)
        changed += 1
    deleted = repository.delete_documents(
        item.id for path, item in existing.items() if path not in retained_paths
    )
    return IndexReport(len(sources), changed, unchanged, deleted)


def main() -> int:
    root = Path(os.getenv(REPOSITORY_ROOT_ENV, Path.cwd()))
    try:
        with open_write_connection() as connection:
            with connection.transaction():
                report = index_repository(root, KnowledgeRepository(connection))
    except Exception as exc:
        print(f"[ERREUR] Indexation documentaire : {exc}")
        return 1
    print(
        "[OK] Documentation SIRS indexée : "
        f"{report.discovered} fichier(s), "
        f"{report.created_or_updated} créé(s)/mis à jour, "
        f"{report.unchanged} inchangé(s), {report.deleted} supprimé(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
