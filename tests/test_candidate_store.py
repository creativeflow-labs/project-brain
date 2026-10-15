from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.services.search_core.candidate_store import (
    _korean_prefix_query, _postgres_candidate_rows,
)
from app.services.search_core.types import QueryPlan


def test_korean_prefix_query_only_accepts_complete_hangul_terms():
    query = _korean_prefix_query(["담당자", "담당자", "김민수", "a", "sk", "KDP", "a'|b", "김"])
    compiled = query.compile(dialect=postgresql.dialect())
    assert list(compiled.params.values()) == ["'담당자':* | '김민수':*"]
    assert _korean_prefix_query(["sk", "KDP", "magiclight.ai", "김", "x:* | y"]) is None


def test_prefix_supplement_keeps_project_limit_and_exact_priority():
    statements = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    plan = QueryPlan(original_question="담당자는?", resolved_question="담당자는?",
                     search_queries=["담당자"], entity_terms=[])
    _postgres_candidate_rows(SimpleNamespace(execute=execute), "only-this-project", plan, [])
    assert len(statements) == 1  # No additional DB round trip.
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "chunks.project_id =" in sql
    assert "only-this-project" in compiled.params.values()
    assert "LIMIT" in sql
    assert "to_tsquery('simple'" in sql
    assert "CASE WHEN" in sql  # Supplemental score cannot reorder exact ties.
    order = sql.split("ORDER BY", 1)[1]
    assert order.index("@@") < order.index("ts_rank_cd")


def test_latin_lookup_keeps_original_lexical_query_shape():
    statements = []
    db = SimpleNamespace(execute=lambda statement: (
        statements.append(statement) or SimpleNamespace(all=lambda: [])
    ))
    plan = QueryPlan(original_question="KDP", resolved_question="KDP",
                     search_queries=["KDP"])
    _postgres_candidate_rows(db, "project", plan, [])
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert " to_tsquery(" not in sql
    assert "CASE WHEN" not in sql
