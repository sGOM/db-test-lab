"""테스트에서 재사용하는 계측 도구.

다른 프로젝트에도 그대로 복사해 쓸 수 있게 앱 코드와 분리해 둔다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement

_WHITESPACE = re.compile(r"\s+")


@dataclass
class QueryCounter:
    """실행된 SQL 문을 모아 세는 계측기.

    N+1 은 "느리다"가 아니라 "쿼리가 N+1개 나간다"로 증명해야 회귀를 잡을 수 있다.
    """

    statements: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.statements)

    def count_matching(self, keyword: str) -> int:
        upper = keyword.upper()
        return sum(1 for s in self.statements if upper in s.upper())

    def __repr__(self) -> str:
        return f"QueryCounter(count={self.count})"

    def dump(self) -> str:
        return "\n".join(f"{i:>3}. {s}" for i, s in enumerate(self.statements, start=1))


@contextmanager
def count_queries(target: Engine | Connection | Session) -> Iterator[QueryCounter]:
    """블록 안에서 실행된 SQL 문을 센다.

    커넥션(또는 커넥션에 바인딩된 세션)을 주면 **그 커넥션에서 나간 SQL 만** 센다.
    엔진에 리스너를 걸면 같은 엔진을 쓰는 다른 커넥션·스레드의 쿼리까지 섞여 들어와서
    개수 단언이 조용히 흔들린다.

    사용 예::

        with count_queries(session) as counter:
            repository.total_quantity_per_member_eager(session)
        assert counter.count == 2
    """
    listen_target = _resolve_listen_target(target)
    counter = QueryCounter()

    def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter.statements.append(_WHITESPACE.sub(" ", statement).strip())

    event.listen(listen_target, "before_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(listen_target, "before_cursor_execute", _on_execute)


def _resolve_listen_target(target: Engine | Connection | Session) -> Engine | Connection:
    """리스너를 걸 대상. 좁힐 수 있으면 커넥션까지 좁힌다."""
    if isinstance(target, Engine | Connection):
        return target
    if isinstance(target, Session):
        # 세션이 커넥션에 바인딩돼 있으면 그 커넥션만 본다 (테스트 격리 픽스처가 이 경우).
        # 엔진에 바인딩돼 있으면 실행 전까지 커넥션이 정해지지 않으므로 엔진에 건다.
        return target.get_bind()
    raise TypeError(f"Engine/Connection/Session 만 지원합니다: {type(target)!r}")


def explain(session: Session, stmt: ClauseElement | str, *, analyze: bool = False) -> str:
    """실행 계획을 문자열로 돌려준다. 인덱스를 실제로 타는지 단언할 때 쓴다.

    ``analyze=True`` 면 실제로 실행하므로 DML 에는 쓰지 말 것.
    """
    if isinstance(stmt, str):
        compiled_sql = stmt
    else:
        dialect = session.get_bind().dialect
        compiled_sql = str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))

    prefix = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN"
    rows = session.execute(text(f"{prefix} {compiled_sql}")).all()
    return "\n".join(row[0] for row in rows)


def table_names(conn: Connection) -> set[str]:
    return set(
        conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
    )
