"""Reversibly quarantine active knowledge that fails the strict reusable-memory gate."""

from pathlib import Path

from knowledge_base import KnowledgeRepository, _is_reusable_pair


def main() -> None:
    repository = KnowledgeRepository(Path(__file__).parent / "data" / "analytics.db")
    with repository._connect() as db:
        rows = db.execute(
            """SELECT a.id,a.organization_scope,v.canonical_question,v.answer
               FROM knowledge_articles a JOIN knowledge_article_versions v
               ON v.article_id=a.id AND v.version=a.active_version
               WHERE a.status='active'"""
        ).fetchall()
    rejected = [row for row in rows if not _is_reusable_pair(row["canonical_question"], row["answer"])]
    for row in rejected:
        repository.set_status(
            row["organization_scope"], row["id"], "disabled", "application:strict-quality-audit"
        )
    print({"checked": len(rows), "disabled": len(rejected), "remaining_active": len(rows) - len(rejected)})


if __name__ == "__main__":
    main()
