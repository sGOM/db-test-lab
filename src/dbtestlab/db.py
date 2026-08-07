"""엔진 생성과 마이그레이션 적용.

마이그레이션은 도구 없이 ``migrations/V{번호}__{설명}.sql`` 파일을 번호순으로 실행하는
최소 구현이다. 무엇이 언제 적용됐는지는 ``schema_version`` 테이블에 남는다.
Alembic 을 얹고 싶으면 이 함수만 갈아끼우면 된다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text

MIGRATIONS_DIR_ENV = "DBTESTLAB_MIGRATIONS_DIR"
_FILENAME_RE = re.compile(r"^V(?P<version>\d+)__(?P<name>.+)\.sql$")


def _has_migrations(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("V*.sql"))


@cache
def migrations_dir() -> Path:
    """마이그레이션 디렉터리를 찾는다.

    ``parents[2]`` 로 고정하면 소스 체크아웃에서만 동작하고 휠로 설치하면 깨진다.
    실제로 파일이 있는 곳을 순서대로 찾는다:

    1. ``DBTESTLAB_MIGRATIONS_DIR`` 환경변수 (명시적 지정이 언제나 이긴다)
    2. 패키지와 함께 배포된 ``dbtestlab/migrations`` (휠 설치 — pyproject 의 force-include)
    3. 패키지 상위 디렉터리의 ``migrations`` (소스 체크아웃 / editable 설치)
    """
    override = os.getenv(MIGRATIONS_DIR_ENV)
    if override:
        directory = Path(override).expanduser().resolve()
        if not _has_migrations(directory):
            raise RuntimeError(
                f"{MIGRATIONS_DIR_ENV} 가 가리키는 곳에 V*.sql 이 없습니다: {directory}"
            )
        return directory

    here = Path(__file__).resolve()
    for candidate in (here.parent / "migrations", *(p / "migrations" for p in here.parents)):
        if _has_migrations(candidate):
            return candidate

    raise RuntimeError(
        f"마이그레이션 디렉터리를 찾지 못했습니다. {MIGRATIONS_DIR_ENV} 환경변수로 직접 지정하세요."
    )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """테스트용 엔진.

    ``pool_pre_ping`` 은 컨테이너가 잠깐 끊겨도 죽은 커넥션을 걸러준다.
    ``future`` 스타일(2.0)은 SQLAlchemy 2.x 의 기본값이라 따로 지정하지 않는다.
    """
    return create_engine(url, echo=echo, pool_pre_ping=True, future=True)


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    directory = directory if directory is not None else migrations_dir()
    migrations: list[Migration] = []
    for path in sorted(directory.glob("V*.sql")):
        matched = _FILENAME_RE.match(path.name)
        if matched is None:
            raise ValueError(f"마이그레이션 파일명이 규칙에 맞지 않습니다: {path.name}")
        migrations.append(
            Migration(
                version=int(matched.group("version")),
                name=matched.group("name"),
                path=path,
            )
        )
    migrations.sort(key=lambda m: m.version)

    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError(f"마이그레이션 버전이 중복됩니다: {versions}")
    return migrations


def migrate(engine: Engine, directory: Path | None = None) -> list[Migration]:
    """아직 적용되지 않은 마이그레이션만 순서대로 실행하고, 적용한 목록을 돌려준다."""
    migrations = discover_migrations(directory)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        applied = set(conn.scalars(text("SELECT version FROM schema_version")).all())

    newly_applied: list[Migration] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        # 마이그레이션 1개 = 트랜잭션 1개. 중간에 실패하면 그 파일만 통째로 롤백된다.
        with engine.begin() as conn:
            conn.execute(text(migration.sql))
            conn.execute(
                text("INSERT INTO schema_version (version, name) VALUES (:version, :name)"),
                {"version": migration.version, "name": migration.name},
            )
        newly_applied.append(migration)

    return newly_applied


def analyze_table(target: Engine | Connection, table: str) -> None:
    """대량 적재 직후 통계를 즉시 갱신한다.

    auto-analyze 는 autovacuum_naptime(기본 60초) 뒤에나 돈다. 그 공백에 새 값으로
    조회가 들어오면 옵티마이저가 행 수를 1로 오추정해 Nested Loop 을 고르고,
    큰 테이블 스캔이 outer 행 수만큼 반복된다. 적재 코드가 직접 ANALYZE 를 부르면
    그 공백 자체가 사라진다.

    ``tests/test_planner_stats.py`` 가 이 함수의 효과를 재현·검증한다.
    """
    if not table.replace("_", "").isalnum():
        raise ValueError(f"테이블명이 올바르지 않습니다: {table!r}")

    if isinstance(target, Connection):
        # ANALYZE 는 VACUUM 과 달리 트랜잭션 안에서도 실행할 수 있다.
        target.execute(text(f"ANALYZE {table}"))
        return

    with target.begin() as conn:
        conn.execute(text(f"ANALYZE {table}"))


def truncate_all(engine: Engine) -> None:
    """schema_version 을 뺀 모든 테이블을 비우고 시퀀스를 되돌린다.

    커밋이 필요한 테스트(동시성 등)에서 롤백 격리를 못 쓸 때 쓰는 청소기.
    """
    with engine.begin() as conn:
        tables = conn.scalars(
            text(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename <> 'schema_version'
                """
            )
        ).all()
        if not tables:
            return
        joined = ", ".join(f'"{name}"' for name in tables)
        conn.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
