"""도메인 모델 (SQLAlchemy 2.0 선언형).

스키마의 단일 소스는 ``migrations/*.sql`` 이다. 이 모듈의 매핑은 그 스키마를 그대로
반영해야 하며, 어긋나면 ``tests/test_schema.py`` 가 잡아낸다.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderStatus(enum.StrEnum):
    CREATED = "CREATED"
    PAID = "PAID"
    CANCELED = "CANCELED"


class Member(Base):
    """회원. email 에 유니크 제약이 있어 제약 위반 테스트의 소재가 된다."""

    __tablename__ = "member"
    __table_args__ = (UniqueConstraint("email", name="uk_member_email"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # 기본 지연 로딩(lazy="select") → N+1 을 재현할 수 있다. 해결편은 selectinload/joinedload.
    orders: Mapped[list[Order]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Member(id={self.id!r}, email={self.email!r})"


class Order(Base):
    """주문.

    - 테이블명이 ``orders`` 인 이유: ``order`` 는 SQL 예약어다.
    - ``version`` 컬럼을 ``version_id_col`` 로 걸어 낙관적 락이 켜져 있다.
      UPDATE 에 ``WHERE version = :읽은값`` 이 붙고, 영향 행이 0이면 ``StaleDataError``.
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_orders_quantity"),
        Index("idx_orders_member_id", "member_id"),
        Index("idx_orders_status_ordered_at", "status", "ordered_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("member.id", name="fk_orders_member"), nullable=False
    )
    product_name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", native_enum=False, length=20),
        nullable=False,
        default=OrderStatus.CREATED,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # version 컬럼 정의 뒤에 와야 한다 (클래스 본문에서 이름을 그대로 참조).
    __mapper_args__ = {"version_id_col": version}

    member: Mapped[Member] = relationship(back_populates="orders")

    def __repr__(self) -> str:
        return f"Order(id={self.id!r}, product_name={self.product_name!r}, qty={self.quantity!r})"
