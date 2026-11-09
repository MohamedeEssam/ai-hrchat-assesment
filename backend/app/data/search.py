from functools import lru_cache

from fastembed import TextEmbedding
from pgvector.psycopg import register_vector

from app.core.auth import RunContext
from app.core.db import tx_with_context, fetch_all


@lru_cache(maxsize=1)
def _embedder() -> TextEmbedding:
    return TextEmbedding(model_name="BAAI/bge-small-en-v1.5")


# Cosine distance (0 = same meaning). Measured on the seed data: matching documents score about
# 0.13-0.31, unrelated ones 0.38-0.56, and small talk such as "hello" over 0.5.
MAX_DISTANCE = 0.5
# Also drop hits much weaker than the best one, so "late policy" does not bring back the handbook.
RELATIVE_MARGIN = 0.08


def embed_query(text: str) -> list[float]:
    vec = next(_embedder().embed([text]))
    return vec.tolist()


def search_policy(ctx: RunContext, query: str, limit: int = 4) -> list[dict]:
    """Top policy chunks the caller may see, keeping only the ones relevant to the query."""
    with tx_with_context(ctx) as conn:
        register_vector(conn)
        vec_values = embed_query(query)
        vec = "[" + ",".join(str(v) for v in vec_values) + "]"
        rows = fetch_all(
            conn,
            """
            SELECT d.id AS document_id, d.title, c.text, (c.embedding <=> %s::vector) AS distance
            FROM chunks c
            JOIN documents d ON d.id = c.document_id AND d.company_id = c.company_id
            WHERE c.company_id = current_setting('app.company_id', true)
              AND (
                d.visibility = 'everyone'
                OR current_setting('app.role', true) = ANY(d.allowed_roles)
              )
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (vec, vec, limit),
        )
    if not rows:
        return []
    cutoff = min(MAX_DISTANCE, float(rows[0]["distance"]) + RELATIVE_MARGIN)
    return [
        {"document_id": r["document_id"], "title": r["title"], "text": r["text"]}
        for r in rows
        if float(r["distance"]) <= cutoff
    ]
