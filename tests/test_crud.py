"""기본기: 저장·조회·수정·삭제와 그 과정에서 헷갈리기 쉬운 것들.

특히 ``flush`` 와 ``commit`` 의 차이, 서버 기본값이 언제 채워지는지,
연관 객체의 cascade 가 실제로 DB 까지 내려가는지를 확인한다.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from dbtestlab.models import Member, Order, OrderStatus


def test_저장하고_다시_읽는다(session):
    member = Member(email="bob@example.com", name="밥")
    session.add(member)
    session.flush()

    found = session.get(Member, member.id)

    assert found is member  # 같은 세션이면 동일성 맵(identity map) 덕에 같은 객체
    assert found.email == "bob@example.com"


def test_flush_는_SQL을_보내지만_커밋은_아니다(session):
    member = Member(email="carol@example.com", name="캐롤")
    session.add(member)

    assert member.id is None  # flush 전에는 PK 가 없다

    session.flush()

    assert member.id is not None  # INSERT 는 나갔다 (아직 커밋은 안 됨)
    assert session.scalar(select(func.count()).select_from(Member)) == 1


def test_서버_기본값은_refresh_해야_보인다(session):
    member = Member(email="dave@example.com", name="데이브")
    session.add(member)
    session.flush()

    # created_at 은 DB 의 now() 가 채운다 → 파이썬 객체에는 아직 없다
    session.refresh(member)

    assert member.created_at is not None
    assert member.created_at.tzinfo is not None  # TIMESTAMPTZ


def test_수정은_flush_시점에_UPDATE_로_나간다(session, member):
    member.name = "앨리스2"
    session.flush()
    session.expire_all()

    assert session.get(Member, member.id).name == "앨리스2"


def test_회원을_지우면_주문도_같이_지워진다(session, member, make_order):
    make_order(member, quantity=2)
    make_order(member, quantity=3)
    assert session.scalar(select(func.count()).select_from(Order)) == 2

    session.delete(member)
    session.flush()

    assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_금액은_Decimal_로_왕복한다(session, member, make_order):
    order = make_order(member, quantity=3, unit_price=1999)
    session.expire_all()

    reloaded = session.get(Order, order.id)

    # float 로 오면 5997.0 근처의 오차가 생긴다. NUMERIC → Decimal 이어야 한다.
    assert reloaded.amount == Decimal("5997.00")
    assert isinstance(reloaded.amount, Decimal)


def test_상태값은_문자열로_저장되고_Enum_으로_돌아온다(session, member, make_order):
    order = make_order(member, status=OrderStatus.PAID)
    session.expire_all()

    raw = session.execute(
        select(Order.__table__.c.status).where(Order.__table__.c.id == order.id)
    ).scalar_one()
    reloaded = session.get(Order, order.id)

    assert raw == "PAID"
    assert reloaded.status is OrderStatus.PAID


@pytest.mark.parametrize(
    ("quantities", "expected"),
    [
        ([], 0),
        ([1], 1),
        ([1, 2, 3], 6),
    ],
)
def test_회원별_주문_수량_합계(session, member, make_order, quantities, expected):
    for quantity in quantities:
        make_order(member, quantity=quantity)
    session.flush()

    total = session.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0)).where(Order.member_id == member.id)
    )

    assert total == expected
