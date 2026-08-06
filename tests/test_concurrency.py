"""동시성 테스트.

여기서만은 롤백 격리를 쓸 수 없다 — 두 세션이 서로의 **커밋**을 봐야 하기 때문이다.
그래서 ``clean_db`` 픽스처(앞뒤로 TRUNCATE)를 쓰고 진짜로 커밋한다.

다루는 것: 갱신 유실(lost update), 낙관적 락, 비관적 락, 유니크 제약 경쟁.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from dbtestlab.models import Member, Order, OrderStatus
from dbtestlab.repository import increase_quantity, increase_quantity_with_lock

pytestmark = pytest.mark.concurrency


@pytest.fixture
def seeded(clean_db: Engine) -> tuple[Engine, int]:
    """회원 1명 + 수량 1인 주문 1건을 커밋해 둔다."""
    factory = sessionmaker(bind=clean_db, expire_on_commit=False)
    with factory() as session:
        member = Member(email="race@example.com", name="레이스")
        session.add(member)
        session.flush()
        order = Order(
            member=member,
            product_name="경쟁상품",
            quantity=1,
            amount=Decimal(1000),
            status=OrderStatus.CREATED,
        )
        session.add(order)
        session.commit()
        return clean_db, order.id


def test_락_없이_읽고_쓰면_갱신이_유실된다(seeded):
    """고전적인 lost update. 두 트랜잭션이 같은 값을 읽고 각자 +1 하면 결과는 +1.

    ORM 밖(원시 SQL, 배치 잡)에서 벌어지는 상황이라 낙관적 락도 못 막는다.
    """
    engine, order_id = seeded

    with engine.connect() as conn_a, engine.connect() as conn_b:
        read = text("SELECT quantity FROM orders WHERE id = :id")
        write = text("UPDATE orders SET quantity = :quantity WHERE id = :id")

        qty_a = conn_a.execute(read, {"id": order_id}).scalar_one()
        qty_b = conn_b.execute(read, {"id": order_id}).scalar_one()  # 같은 값을 읽었다

        conn_a.execute(write, {"quantity": qty_a + 1, "id": order_id})
        conn_a.commit()
        conn_b.execute(write, {"quantity": qty_b + 1, "id": order_id})
        conn_b.commit()

    with engine.connect() as conn:
        final = conn.execute(
            text("SELECT quantity FROM orders WHERE id = :id"), {"id": order_id}
        ).scalar_one()

    assert final == 2, "2가 아니라 3을 기대했다면, 그게 바로 갱신 유실이다"


def test_낙관적_락이_뒤늦은_커밋을_거부한다(seeded):
    """Order 에 version 컬럼이 걸려 있어 UPDATE 에 ``WHERE version = 읽은값`` 이 붙는다.

    나중에 커밋하는 쪽은 영향 행 0 → StaleDataError. 유실 대신 **실패**로 드러난다.
    """
    engine, order_id = seeded
    factory = sessionmaker(bind=engine)

    with factory() as session_a, factory() as session_b:
        increase_quantity(session_a, order_id, 1)
        increase_quantity(session_b, order_id, 1)  # 같은 version 을 읽었다

        session_a.commit()

        with pytest.raises(StaleDataError):
            session_b.commit()


def test_비관적_락이면_동시_증가가_유실되지_않는다(seeded):
    """SELECT ... FOR UPDATE 로 행 락을 잡으면 두 스레드가 순서대로 처리된다."""
    engine, order_id = seeded
    factory = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with factory() as session:
                barrier.wait(timeout=10)  # 최대한 같은 순간에 달려들게 한다
                increase_quantity_with_lock(session, order_id, 1)
                session.commit()
        except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 본 스레드로 옮긴다
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    with factory() as session:
        assert session.get(Order, order_id).quantity == 3  # 1 + 1 + 1


def test_유니크_제약이_중복_가입_경쟁을_막는다(clean_db: Engine):
    """``register_member`` 의 "있는지 조회하고 없으면 삽입"은 동시 요청에서 둘 다 통과한다.

    두 스레드가 **둘 다 조회를 마친 뒤에** 삽입하도록 배리어로 맞춘다. 이게 운영에서
    실제로 벌어지는 인터리빙이다. 최종 방어선은 DB 의 유니크 제약이고, 애플리케이션은
    그 예외를 잡아 처리하면 된다.

    (한 스레드에서 두 세션으로 흉내내면 뒤늦은 INSERT 가 앞 트랜잭션의 커밋을 기다리며
    영영 멈춘다 — PostgreSQL 이 유니크 충돌 가능성을 커밋 시점까지 판단할 수 없어서다.)
    """
    email = "same@example.com"
    factory = sessionmaker(bind=clean_db)
    checked = threading.Barrier(2, timeout=10)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        # register_member() 와 같은 순서지만, 조회와 삽입 사이에 배리어를 끼운다.
        with factory() as session:
            try:
                assert session.scalar(select(Member.id).where(Member.email == email)) is None
                checked.wait()  # 둘 다 "없음"을 본 시점에서 출발
                session.add(Member(email=email, name=name))
                session.commit()
                result = "가입성공"
            except IntegrityError as exc:
                assert isinstance(exc.orig, psycopg.errors.UniqueViolation)
                result = "중복거부"
            with lock:
                outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("먼저", "나중")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["가입성공", "중복거부"]

    with factory() as session:
        assert len(session.scalars(select(Member)).all()) == 1


def test_커밋하지_않은_변경은_다른_트랜잭션에_보이지_않는다(seeded):
    """READ COMMITTED 기본 동작 확인."""
    engine, order_id = seeded

    with engine.connect() as conn_a, engine.connect() as conn_b:
        conn_a.execute(text("UPDATE orders SET quantity = 99 WHERE id = :id"), {"id": order_id})

        seen_by_b = conn_b.execute(
            text("SELECT quantity FROM orders WHERE id = :id"), {"id": order_id}
        ).scalar_one()
        assert seen_by_b == 1

        conn_a.commit()

        # 같은 트랜잭션 안에서 다시 읽으면 이제는 보인다 (READ COMMITTED 라서)
        assert (
            conn_b.execute(
                text("SELECT quantity FROM orders WHERE id = :id"), {"id": order_id}
            ).scalar_one()
            == 99
        )
