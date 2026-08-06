"""트랜잭션 동작 테스트.

롤백이 정말 되는지, 세이브포인트가 부분 롤백을 해주는지,
그리고 예외가 났을 때 세션이 어떤 상태가 되는지.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, PendingRollbackError

from dbtestlab.models import Member


def _member_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Member))


def test_롤백하면_흔적이_남지_않는다(session):
    session.add(Member(email="rollback@example.com", name="롤백"))
    session.flush()
    assert _member_count(session) == 1

    session.rollback()

    assert _member_count(session) == 0


def test_세이브포인트로_일부만_되돌린다(session):
    session.add(Member(email="keep@example.com", name="유지"))
    session.flush()

    savepoint = session.begin_nested()
    session.add(Member(email="drop@example.com", name="취소"))
    session.flush()
    assert _member_count(session) == 2

    savepoint.rollback()

    assert _member_count(session) == 1
    assert session.scalar(select(Member.name)) == "유지"


def test_제약_위반_후에는_롤백해야_세션을_다시_쓸_수_있다(session, member):
    session.add(Member(email=member.email, name="중복"))
    with pytest.raises(IntegrityError):
        session.flush()

    # 롤백 전에는 어떤 쿼리도 받지 않는다 (PostgreSQL 의 aborted transaction 상태)
    with pytest.raises(PendingRollbackError):
        _member_count(session)

    session.rollback()

    assert _member_count(session) == 0  # 롤백이 member 픽스처까지 되돌렸다


def test_세이브포인트를_쓰면_제약_위반에도_앞_작업이_살아있다(session, member):
    """ "이 한 건만 실패해도 배치 전체는 계속" 패턴."""
    savepoint = session.begin_nested()
    session.add(Member(email=member.email, name="중복"))
    with pytest.raises(IntegrityError):
        session.flush()
    savepoint.rollback()

    session.add(Member(email="next@example.com", name="다음"))
    session.flush()

    assert _member_count(session) == 2


def test_커밋해도_테스트_격리는_유지된다(session):
    """conftest 가 바깥 트랜잭션을 잡고 있어서, 테스트 안의 commit 은 세이브포인트까지만 간다.

    덕분에 커밋하는 코드도 그대로 테스트할 수 있고, 테스트가 끝나면 전부 사라진다.
    """
    session.add(Member(email="committed@example.com", name="커밋"))
    session.commit()

    assert _member_count(session) == 1
    # 실제 정리는 conftest 의 connection 픽스처가 바깥 트랜잭션을 롤백하며 해준다.
