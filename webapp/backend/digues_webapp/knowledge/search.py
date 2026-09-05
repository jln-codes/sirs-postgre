"""Recherche plein texte bornée dans la documentation versionnée SIRS."""

from __future__ import annotations

import logging
from typing import Any, Callable, ContextManager

from ..database import open_read_connection
from .repository import KnowledgeRepository


LOGGER = logging.getLogger(__name__)
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 8
MAX_QUERY_CHARS = 500


class KnowledgeSearchError(RuntimeError):
    """Erreur documentaire contrôlée, sans détail PostgreSQL sensible."""


def search_sirs_knowledge(
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    connection_factory: Callable[[], ContextManager[Any]] = open_read_connection,
) -> dict[str, Any]:
    normalized = query.strip()
    if not normalized or len(normalized) > MAX_QUERY_CHARS:
        raise KnowledgeSearchError("La recherche documentaire est vide ou trop longue.")
    bounded_limit = max(1, min(int(limit), MAX_RESULT_LIMIT))
    try:
        with connection_factory() as connection:
            repository = KnowledgeRepository(connection)
            results = repository.search(
                normalized,
                limit=bounded_limit,
                fts_config=repository.fts_config(),
            )
    except KnowledgeSearchError:
        raise
    except Exception as exc:
        LOGGER.error("Recherche documentaire indisponible: %s", type(exc).__name__)
        raise KnowledgeSearchError(
            "La documentation SIRS est temporairement indisponible."
        ) from exc
    return {"results": results}
