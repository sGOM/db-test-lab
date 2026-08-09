"""테스트 기반 설정.

핵심 규칙 세 가지:

1. **진짜 PostgreSQL 로만 테스트한다.** SQLite 로 바꿔치기하면 제약·락·타입·실행계획이
   전부 달라져서, 통과해도 아무것도 보장하지 못한다.
2. **격리는 트랜잭션 롤백이 기본.** 테스트마다 커넥션 하나를 잡고 바깥 트랜잭션을 연 뒤,
   끝나면 롤백한다. TRUNCATE 보다 훨씬 빠르고 테스트 순서에 영향받지 않는다.
   커밋이 꼭 필요한 테스트(동시성)는 ``clean_db`` 픽스처로 따로 논다.
3. **병렬 실행(pytest-xdist)은 워커마다 별도 DB 를 쓴다.** 같은 DB 를 나눠 쓰면
   ``clean_db`` 의 TRUNCATE 가 다른 워커가 쓰는 중인 데이터를 지워버린다.
   워커별 DB 는 세션 시작 때 만들고 끝날 때 지운다.

DB 는 두 경로 중 하나로 준비된다:

- ``TEST_DATABASE_URL`` 환경변수가 있으면 그 DB 를 쓴다 (CI, 로컬 docker compose).
- 없으면 Testcontainers 가 postgres:16-alpine 컨테이너를 띄운다 (Docker 필요).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from dbtestlab.db import create_db_engine, migrate, truncate_all
from dbtestlab.models import Member, Order, OrderStatus

POSTGRES_IMAGE = os.getenv("TEST_POSTGRES_IMAGE", "postgres:16-alpine")

# pytest-xdist 가 워커마다 넣어주는 값(gw0, gw1, ...). 단독 실행이면 없다.
XDIST_WORKER = os.getenv("PYTEST_XDIST_WORKER")

# 워커 DB 를 만들 때 붙는 관리용 DB. 지우려는 DB 자신에는 붙을 수 없다.
MAINTENANCE_DB = "postgres"


def _create_worker_database(base_url: str, worker: str) -> str:
    """``<원본DB>_<워커>`` 를 만들고 그 URL 을 돌려준다 (이미 있으면 다시 만든다)."""
    url = make_url(base_url)
    worker_db = f"{url.database}_{worker}"

    admin = create_engine(url.set(database=MAINTENANCE_DB), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            # FORCE 는 PostgreSQL 13+. 앞선 실행이 남긴 커넥션까지 끊고 지운다.
            conn.execute(text(f'DROP DATABASE IF EXISTS "{worker_db}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{worker_db}"'))
    finally:
        admin.dispose()

    return url.set(database=worker_db).render_as_string(hide_password=False)


def _drop_worker_database(base_url: str, worker: str) -> None:
    url = make_url(base_url)
    worker_db = f"{url.database}_{worker}"

    admin = create_engine(url.set(database=MAINTENANCE_DB), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{worker_db}" WITH (FORCE)'))
    except ProgrammingError:  # pragma: no cover - 정리 실패가 테스트를 깨뜨리진 않게
        pass
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """테스트가 붙을 DB URL. 환경변수 우선, 없으면 Testcontainers.

    xdist 로 돌면 워커마다 DB 를 따로 만든다 — TRUNCATE 격리와 재현 테스트가 만드는
    임시 테이블이 워커끼리 서로를 밟지 않게 하려면 DB 자체를 갈라야 한다.
    """
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        if XDIST_WORKER is None:
            yield url
            return
        try:
            yield _create_worker_database(url, XDIST_WORKER)
        finally:
            _drop_worker_database(url, XDIST_WORKER)
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise RuntimeError(
            "TEST_DATABASE_URL 도 없고 testcontainers 도 설치돼 있지 않습니다.\n"
            "  - 도커가 있으면:  pip install -e '.[dev]'\n"
            "  - 도커가 없으면:  TEST_DATABASE_URL=postgresql+psycopg://... pytest"
        ) from exc

    # Testcontainers 경로는 워커마다 컨테이너가 따로 뜨므로 이미 격리돼 있다.
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
