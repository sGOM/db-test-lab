"""스키마 자체를 테스트한다.

마이그레이션이 제대로 돌았는지, 그리고 ORM 매핑이 실제 DB 스키마와 어긋나지 않았는지.
이 파일이 깨지면 "테스트는 통과하는데 운영에서 컬럼이 없다"류의 사고를 미리 잡은 것이다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, inspect, text

from dbtestlab.db import MIGRATIONS_DIR_ENV, discover_migrations, migrate, migrations_dir
from dbtestlab.models import Base


def test_마이그레이션이_기록된다(connection):
    rows = connection.execute(
        text("SELECT version, name FROM schema_version ORDER BY version")
    ).all()

    assert [r.version for r in rows] == [1]
    assert rows[0].name == "init"


def test_마이그레이션은_멱등하다(engine: Engine):
    # 이미 적용된 마이그레이션은 두 번 실행되지 않는다.
    newly_applied = migrate(engine)

    assert newly_applied == []


def test_마이그레이션_디렉터리를_설치_형태와_무관하게_찾는다():
    """경로를 ``parents[2]`` 로 고정하면 소스 체크아웃에서만 동작한다.

    실제로 ``V*.sql`` 이 있는 곳을 찾아야 휠 설치·다른 작업 디렉터리에서도 깨지지 않는다.
    """
    directory = migrations_dir()

    assert directory.is_dir()
    assert (directory / "V1__init.sql").is_file()
    assert [m.version for m in discover_migrations()] == [1]


def test_마이그레이션_디렉터리는_환경변수로_바꿀_수_있다(tmp_path, monkeypatch):
    (tmp_path / "V1__init.sql").write_text("SELECT 1", encoding="utf-8")
    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))
    migrations_dir.cache_clear()

    try:
        assert migrations_dir() == tmp_path.resolve()
    finally:
        monkeypatch.delenv(MIGRATIONS_DIR_ENV)
        migrations_dir.cache_clear()


def test_마이그레이션_디렉터리가_비어있으면_바로_알려준다(tmp_path, monkeypatch):
    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))  # V*.sql 이 없는 빈 디렉터리
    migrations_dir.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="V\\*.sql"):
            migrations_dir()
    finally:
        monkeypatch.delenv(MIGRATIONS_DIR_ENV)
        migrations_dir.cache_clear()


def test_테이블이_모두_존재한다(connection):
    inspector = inspect(connection)

    actual = set(inspector.get_table_names())

    assert {"member", "orders", "schema_version"} <= actual


def test_ORM_매핑과_실제_스키마가_일치한다(connection):
    """모델에 선언한 컬럼이 DB 에 그대로 있는지 (이름·널 허용 여부)."""
    inspector = inspect(connection)

    for table_name, table in Base.metadata.tables.items():
        db_columns = {c["name"]: c for c in inspector.get_columns(table_name)}

        assert set(table.columns.keys()) == set(db_columns), (
            f"{table_name} 컬럼 목록 불일치: "
            f"모델={sorted(table.columns.keys())} DB={sorted(db_columns)}"
        )

        for column in table.columns:
            assert column.nullable == db_columns[column.name]["nullable"], (
                f"{table_name}.{column.name} 널 허용 여부 불일치"
            )


def test_제약조건이_걸려있다(connection):
    inspector = inspect(connection)

    unique_names = {c["name"] for c in inspector.get_unique_constraints("member")}
    check_names = {c["name"] for c in inspector.get_check_constraints("orders")}
    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("orders")}

    assert "uk_member_email" in unique_names
    assert "ck_orders_quantity" in check_names
    assert "fk_orders_member" in fk_names


def test_인덱스가_걸려있다(connection):
    inspector = inspect(connection)

    index_names = {idx["name"] for idx in inspector.get_indexes("orders")}

    assert {"idx_orders_member_id", "idx_orders_status_ordered_at"} <= index_names
