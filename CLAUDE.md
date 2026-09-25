# CLAUDE.md

진짜 PostgreSQL 16 위에서 DB 동작(제약·트랜잭션·락·실행계획)을 pytest `assert` 로 고정하는 실습장.
설계 이유와 테스트 목록은 [`README.md`](README.md) 에 있다. 여기엔 규칙만 둔다.

## 명령

```bash
make install        # .venv + 의존성 (uv 없으면 venv)
make up             # PostgreSQL localhost:5433 (포트가 겹치면 DB_TEST_LAB_PG_PORT)
make test           # 전체 / make test-fast (slow 제외) / make test-parallel (xdist)
make test-tc        # TEST_DATABASE_URL 없이 Testcontainers 로
make lint           # ruff check + format --check (CI 와 동일)
```

## 지킬 것

- **스키마 단일 소스는 `migrations/V{번호}__{설명}.sql`.** 기존 파일은 고치지 않고 새 번호를 추가한다. ORM(`models.py`)과 어긋나면 `test_schema.py` 가 잡는다.
- **격리는 바깥 트랜잭션 롤백**(`join_transaction_mode="create_savepoint"`). 진짜 커밋이 필요한 테스트만 `clean_db` 픽스처 + `concurrency` 마커.
- 마커는 `--strict-markers` — 새 마커는 `pyproject.toml` 에 먼저 등록한다.
- 재현 테스트용 테이블은 `repro` 스키마에 만든다(`truncate_all()` 은 `public` 만 비운다).
- N+1 은 시간 말고 `count_queries()` 로 쿼리 **개수**를 단언한다. 계측은 엔진이 아니라 커넥션/세션 단위.
- 실행계획은 텍스트 grep 금지 — `dbtestlab.planner` 의 JSON 노드 트리로 단언한다.
- DB 선택 분기는 `tests/conftest.py` 의 `database_url` 픽스처 한 곳뿐이다. 다른 곳에 두지 않는다.
