"""테스트 기반 설정.

핵심 규칙 두 가지:

1. **진짜 PostgreSQL 로만 테스트한다.** SQLite 로 바꿔치기하면 제약·락·타입·실행계획이
   전부 달라져서, 통과해도 아무것도 보장하지 못한다.
2. **격리는 트랜잭션 롤백이 기본.** 테스트마다 커넥션 하나를 잡고 바깥 트랜잭션을 연 뒤,
   끝나면 롤백한다. TRUNCATE 보다 훨씬 빠르고 테스트 순서에 영향받지 않는다.
   커밋이 꼭 필요한 테스트(동시성)는 ``clean_db`` 픽스처로 따로 논다.

DB 는 두 경로 중 하나로 준비된다:

- ``TEST_DATABASE_URL`` 환경변수가 있으면 그 DB 를 쓴다 (CI, 로컬 docker compose).
- 없으면 Testcontainers 가 postgres:16-alpine 컨테이너를 띄운다 (Docker 필요).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from dbtestlab.db import create_db_engine, migrate, truncate_all
from dbtestlab.models import Member, Order, OrderStatus

POSTGRES_IMAGE = os.getenv("TEST_POSTGRES_IMAGE", "postgres:16-alpine")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """테스트가 붙을 DB URL. 환경변수 우선, 없으면 Testcontainers."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise RuntimeError(
            "TEST_DATABASE_URL 도 없고 testcontainers 도 설치돼 있지 않습니다.\n"
            "  - 도커가 있으면:  pip install -e '.[dev]'\n"
            "  - 도커가 없으면:  TEST_DATABASE_URL=postgresql+psycopg://... pytest"
        ) from exc

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """세션 전체에서 하나만 쓰는 엔진. 마이그레이션도 여기서 한 번만 돌린다."""
    engine = create_db_engine(database_url, echo=bool(os.getenv("SQL_ECHO")))
    migrate(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """바깥 트랜잭션을 연 커넥션. 테스트가 끝나면 무조건 롤백된다."""
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


@pytest.fixture
def session(connection: Connection) -> Iterator[Session]:
    """기본 ORM 세션.

    ``join_transaction_mode="create_savepoint"`` 덕분에 테스트 안에서 ``session.commit()``
    을 불러도 바깥 트랜잭션은 살아 있고, 끝나면 전부 롤백된다.
    """
    with Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    ) as sess:
        yield sess


@pytest.fixture
def clean_db(engine: Engine) -> Iterator[Engine]:
    """커밋이 필요한 테스트용. 롤백 격리 대신 앞뒤로 TRUNCATE 한다.

    동시성 테스트처럼 서로 다른 커넥션이 서로의 커밋을 봐야 하는 경우에만 쓴다.
    """
    truncate_all(engine)
    try:
        yield engine
    finally:
        truncate_all(engine)


# --------------------------------------------------------------------------------------
# 픽스처: 테스트 데이터
# --------------------------------------------------------------------------------------


@pytest.fixture
def member(session: Session) -> Member:
    m = Member(email="alice@example.com", name="앨리스")
    session.add(m)
    session.flush()
    return m


@pytest.fixture
def make_member(session: Session):
    """이름만 주면 회원을 만들어주는 팩토리. 이메일 충돌은 알아서 피한다."""
    created: list[Member] = []

    def _make(name: str = "회원", email: str | None = None) -> Member:
        m = Member(email=email or f"user{len(created) + 1}@example.com", name=name)
        session.add(m)
        session.flush()
        created.append(m)
        return m

    return _make


@pytest.fixture
def make_order(session: Session):
    def _make(
        member: Member,
        product_name: str = "테스트상품",
        quantity: int = 1,
        unit_price: int = 1000,
        status: OrderStatus = OrderStatus.CREATED,
    ) -> Order:
        order = Order(
            member=member,
            product_name=product_name,
            quantity=quantity,
            amount=Decimal(unit_price) * quantity,
            status=status,
        )
        session.add(order)
        session.flush()
        return order

    return _make
