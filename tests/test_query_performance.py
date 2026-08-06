"""쿼리 성능을 "느낌"이 아니라 숫자로 고정한다.

- N+1 은 실행 시간이 아니라 **쿼리 개수**로 잡는다. 개발 DB 에서는 100ms 라 안 보이지만
  운영 데이터에서는 그대로 장애가 된다.
- 인덱스는 "만들었다"가 아니라 **실행계획이 그 인덱스를 탈 수 있는가**로 확인한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from dbtestlab.models import Order, OrderStatus
from dbtestlab.planner import explain, node_types
from dbtestlab.repository import (
    total_quantity_per_member_aggregate,
    total_quantity_per_member_eager,
    total_quantity_per_member_n_plus_1,
)
from dbtestlab.testing import count_queries

MEMBER_COUNT = 3
ORDERS_PER_MEMBER = 2


@pytest.fixture
def members_with_orders(session, make_member, make_order):
    """회원 3명 × 주문 2건. 만든 뒤 동일성 맵을 비워 실제 조회가 일어나게 한다."""
    for index in range(MEMBER_COUNT):
        member = make_member(name=f"회원{index}")
        for _ in range(ORDERS_PER_MEMBER):
            make_order(member, quantity=index + 1)
    session.flush()
    session.expunge_all()  # 이걸 안 하면 캐시에 다 있어서 쿼리가 안 나간다
    return session


def test_지연로딩은_N더하기1_쿼리를_만든다(members_with_orders):
    session = members_with_orders

    with count_queries(session) as counter:
        result = total_quantity_per_member_n_plus_1(session)

    assert len(result) == MEMBER_COUNT
    assert counter.count == 1 + MEMBER_COUNT, f"실행된 쿼리:\n{counter.dump()}"


def test_selectinload_는_두_방으로_끝낸다(members_with_orders):
    session = members_with_orders

    with count_queries(session) as counter:
        result = total_quantity_per_member_eager(session)

    assert len(result) == MEMBER_COUNT
    assert counter.count == 2, f"실행된 쿼리:\n{counter.dump()}"


def test_집계는_DB에_시키면_한_방이다(members_with_orders):
    session = members_with_orders

    with count_queries(session) as counter:
        result = total_quantity_per_member_aggregate(session)

    assert len(result) == MEMBER_COUNT
    assert counter.count == 1, f"실행된 쿼리:\n{counter.dump()}"


def test_세_방식의_결과는_모두_같다(members_with_orders):
    session = members_with_orders

    naive = total_quantity_per_member_n_plus_1(session)
    session.expunge_all()
    eager = total_quantity_per_member_eager(session)
    aggregated = total_quantity_per_member_aggregate(session)

    assert naive == eager == aggregated


def test_회원별_주문_조회는_인덱스를_탈_수_있다(session, member, make_order):
    """작은 테이블에서는 순차 스캔이 더 싸서 플래너가 인덱스를 안 쓴다.

    그래서 ``enable_seqscan = off`` 로 "이 조건에 이 인덱스를 쓸 수 있는가"만 확인한다.
    인덱스를 지우거나 컬럼 순서를 바꾸면 이 테스트가 깨진다.
    """
    make_order(member)
    session.flush()
    session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = explain(
        session.connection(),
        f"SELECT * FROM orders WHERE member_id = {member.id}",
    )

    assert "idx_orders_member_id" in str(plan), f"플랜 노드: {node_types(plan)}"


def test_상태별_최신순_조회는_복합_인덱스를_탈_수_있다(session, member, make_order):
    make_order(member, status=OrderStatus.CREATED)
    session.flush()
    session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = explain(
        session.connection(),
        "SELECT * FROM orders WHERE status = 'CREATED' ORDER BY ordered_at DESC LIMIT 10",
    )

    assert "idx_orders_status_ordered_at" in str(plan), f"플랜 노드: {node_types(plan)}"


def test_페이징은_정렬이_있어야_결과가_안정적이다(session, make_member, make_order):
    """ORDER BY 없는 LIMIT/OFFSET 은 순서가 보장되지 않는다 — 페이지 간 중복·누락의 원인."""
    member = make_member()
    for index in range(5):
        make_order(member, product_name=f"상품{index}")
    session.flush()

    page_1 = session.scalars(select(Order).order_by(Order.id).limit(2).offset(0)).all()
    page_2 = session.scalars(select(Order).order_by(Order.id).limit(2).offset(2)).all()

    assert [o.product_name for o in page_1] == ["상품0", "상품1"]
    assert [o.product_name for o in page_2] == ["상품2", "상품3"]
    assert not set(page_1) & set(page_2)
