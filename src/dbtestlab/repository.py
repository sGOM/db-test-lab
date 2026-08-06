"""테스트 대상이 되는 조회/변경 함수들.

의도적으로 "느린 버전"과 "고친 버전"을 나란히 둔다 (예: N+1). 테스트가 둘의 차이를
쿼리 수로 증명한다.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from dbtestlab.models import Member, Order, OrderStatus


class DuplicateEmailError(Exception):
    def __init__(self, email: str) -> None:
        super().__init__(f"이미 사용 중인 이메일입니다: {email}")
        self.email = email


def add_member(session: Session, email: str, name: str) -> Member:
    member = Member(email=email, name=name)
    session.add(member)
    session.flush()  # id 를 지금 받아야 후속 코드가 쓸 수 있다
    return member


def register_member(session: Session, email: str, name: str) -> Member:
    """선(先)조회 후 삽입. 동시 요청에서는 이 체크만으로 부족하다는 걸 테스트가 보여준다."""
    exists = session.scalar(select(Member.id).where(Member.email == email))
    if exists is not None:
        raise DuplicateEmailError(email)
    return add_member(session, email, name)


def place_order(
    session: Session,
    member: Member,
    product_name: str,
    quantity: int,
    unit_price: Decimal | int = 1000,
) -> Order:
    order = Order(
        member=member,
        product_name=product_name,
        quantity=quantity,
        amount=Decimal(unit_price) * quantity,
        status=OrderStatus.CREATED,
    )
    session.add(order)
    session.flush()
    return order


def total_quantity_per_member_n_plus_1(session: Session) -> dict[int, int]:
    """N+1 재현: 회원 1회 + 회원 수만큼 orders 지연 로딩."""
    members = session.scalars(select(Member).order_by(Member.id)).all()
    return {m.id: sum(o.quantity for o in m.orders) for m in members}


def total_quantity_per_member_eager(session: Session) -> dict[int, int]:
    """해결: selectinload 로 orders 를 IN 절 한 방에 가져온다 (총 2쿼리)."""
    members = session.scalars(
        select(Member).options(selectinload(Member.orders)).order_by(Member.id)
    ).all()
    return {m.id: sum(o.quantity for o in m.orders) for m in members}


def total_quantity_per_member_aggregate(session: Session) -> dict[int, int]:
    """더 나은 해결: 집계는 DB 에 시킨다 (1쿼리, 엔티티 적재 없음)."""
    rows = session.execute(
        select(Order.member_id, func.coalesce(func.sum(Order.quantity), 0)).group_by(
            Order.member_id
        )
    ).all()
    return {member_id: int(total) for member_id, total in rows}


def find_order_for_update(session: Session, order_id: int) -> Order | None:
    """비관적 락 (SELECT ... FOR UPDATE). 행 락을 잡고 순차 처리한다."""
    return session.scalars(
        select(Order).where(Order.id == order_id).with_for_update()
    ).one_or_none()


def increase_quantity(session: Session, order_id: int, delta: int) -> Order:
    """락 없이 읽고 쓴다 → 동시 실행 시 갱신 유실(lost update) 후보.

    다만 Order 에는 낙관적 락이 걸려 있어 유실 대신 StaleDataError 로 드러난다.
    """
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError(f"주문이 없습니다. id={order_id}")
    order.quantity += delta
    return order


def increase_quantity_with_lock(session: Session, order_id: int, delta: int) -> Order:
    order = find_order_for_update(session, order_id)
    if order is None:
        raise ValueError(f"주문이 없습니다. id={order_id}")
    order.quantity += delta
    return order
