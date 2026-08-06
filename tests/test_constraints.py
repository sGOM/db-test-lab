"""제약조건 테스트.

애플리케이션 검증만 믿으면 동시 요청·직접 SQL·배치 잡에서 뚫린다.
"제약이 DB 에 실제로 걸려 있는가"를 위반을 일으켜 확인한다.

에러를 ``IntegrityError`` 로 뭉뚱그리지 않고 psycopg 의 구체 예외까지 단언한다 —
"중복이라 실패"와 "FK 가 없어 실패"는 애플리케이션이 다르게 처리해야 하기 때문이다.
"""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from dbtestlab.models import Member
from dbtestlab.repository import DuplicateEmailError, register_member


def test_이메일이_중복되면_유니크_제약에_걸린다(session, member):
    session.add(Member(email=member.email, name="다른사람"))

    with pytest.raises(IntegrityError) as exc_info:
        session.flush()

    assert isinstance(exc_info.value.orig, psycopg.errors.UniqueViolation)
    assert "uk_member_email" in str(exc_info.value.orig)
    session.rollback()


def test_없는_회원의_주문은_외래키_제약에_걸린다(connection):
    savepoint = connection.begin_nested()

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            text(
                """
                INSERT INTO orders (member_id, product_name, quantity, amount, status)
                VALUES (999999, '유령상품', 1, 1000, 'CREATED')
                """
            )
        )

    assert isinstance(exc_info.value.orig, psycopg.errors.ForeignKeyViolation)
    assert "fk_orders_member" in str(exc_info.value.orig)
    savepoint.rollback()


def test_수량이_0이면_체크_제약에_걸린다(connection, session, member):
    session.flush()
    savepoint = connection.begin_nested()

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            text(
                """
                INSERT INTO orders (member_id, product_name, quantity, amount, status)
                VALUES (:member_id, '수량0상품', 0, 0, 'CREATED')
                """
            ),
            {"member_id": member.id},
        )

    assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation)
    assert "ck_orders_quantity" in str(exc_info.value.orig)
    savepoint.rollback()


def test_필수값이_비면_NOT_NULL_제약에_걸린다(connection):
    savepoint = connection.begin_nested()

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(text("INSERT INTO member (email) VALUES ('noname@example.com')"))

    assert isinstance(exc_info.value.orig, psycopg.errors.NotNullViolation)
    savepoint.rollback()


def test_애플리케이션_검증은_먼저_걸러준다(session, member):
    """정상 경로에서는 DB 까지 가기 전에 도메인 예외로 끝난다.

    다만 이건 "빠른 실패"일 뿐 동시성 안전장치가 아니다 —
    ``test_concurrency.py`` 가 이 검증이 뚫리는 경우를 보여준다.
    """
    with pytest.raises(DuplicateEmailError):
        register_member(session, member.email, "다른사람")
