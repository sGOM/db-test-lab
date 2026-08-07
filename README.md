# db-test-lab

PostgreSQL 을 **진짜로** 띄워놓고 데이터베이스 동작을 테스트 코드로 확인하는 실습장.

제약조건·트랜잭션·락·실행계획처럼 "돌려보기 전에는 확신할 수 없는" 것들을
`assert` 한 줄로 고정한다. 실제 장애를 재현하고 고치는 케이스도 함께 들어 있다.

```
pytest -q
49 passed in 3.40s
```

## 왜 이 스택인가

| 선택 | 이유 |
|------|------|
| **pytest** | `assert` 그대로 쓰고, 실패하면 값을 알아서 보여준다. 픽스처 조합이 곧 테스트 시나리오가 된다 |
| **SQLAlchemy 2.0** | ORM 과 원시 SQL 을 **같은 트랜잭션 안에서** 섞어 쓸 수 있다. "ORM 이 실제로 어떤 SQL 을 보내는가"를 테스트 대상으로 삼을 수 있다 |
| **Testcontainers** | 진짜 PostgreSQL 16. SQLite 로 바꿔치기하면 제약·락·타입·실행계획이 전부 달라져서 통과해도 아무것도 보장하지 못한다 |
| **psycopg 3** | 예외가 `UniqueViolation` / `ForeignKeyViolation` 처럼 구체적으로 온다. "왜 실패했는지"까지 단언할 수 있다 |

## 빠른 시작

### 1) Docker 로 DB 를 띄워서 (권장, 빠름)

```bash
make install          # .venv 생성 + 의존성 설치
make up               # PostgreSQL 16 기동 (localhost:5433)
make test             # 전체 테스트
make test-fast        # slow 마커 제외
make test-parallel    # xdist 병렬 실행 (워커마다 DB 를 따로 만든다)
make repro            # 통계 오추정 재현 테스트만
make down
```

### 2) Testcontainers 로 (환경변수 없이)

```bash
make install
make test-tc          # pytest 가 알아서 컨테이너를 띄우고 지운다
```

### 3) 이미 있는 DB 에 붙여서

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/db pytest
```

`TEST_DATABASE_URL` 이 있으면 그 DB 를, 없으면 Testcontainers 를 쓴다. 결정 지점은
[`tests/conftest.py`](tests/conftest.py) 의 `database_url` 픽스처 한 곳뿐이다.
`pytest -n` 으로 병렬 실행하면 같은 픽스처가 워커마다 DB 를 따로 만든다.

## 테스트가 다루는 것

| 파일 | 내용 |
|------|------|
| [`test_schema.py`](tests/test_schema.py) | 마이그레이션 적용·멱등성·경로 해석, **ORM 매핑과 실제 스키마의 정합성**, 제약·인덱스 존재 |
| [`test_crud.py`](tests/test_crud.py) | `flush` vs `commit`, 서버 기본값이 채워지는 시점, cascade, `NUMERIC` → `Decimal` 왕복 |
| [`test_constraints.py`](tests/test_constraints.py) | UNIQUE / FK / CHECK / NOT NULL 위반을 실제로 일으키고 **예외 종류까지** 단언 |
| [`test_transactions.py`](tests/test_transactions.py) | 롤백, 세이브포인트 부분 롤백, 제약 위반 후 세션이 잠기는 상태(`PendingRollbackError`) |
| [`test_concurrency.py`](tests/test_concurrency.py) | 갱신 유실, 낙관적 락(`StaleDataError`), 비관적 락(`FOR UPDATE`), 유니크 제약 경쟁, READ COMMITTED |
| [`test_query_performance.py`](tests/test_query_performance.py) | **N+1 을 쿼리 개수로** 잡기, 인덱스를 탈 수 있는지 실행계획으로 확인, 페이징 안정성, 계측기의 커넥션 격리 |
| [`test_planner_stats.py`](tests/test_planner_stats.py) | 통계 오추정 → Nested Loop 폭발 **재현과 해결** (아래) |

## 이 저장소의 핵심: 실행계획이 뒤집히는 순간을 테스트로 고정하기

[`tests/test_planner_stats.py`](tests/test_planner_stats.py) 는 실제 장애
("이벤트 리스트 조회 120초")와 같은 구조를 빈 PostgreSQL 위에서 재현한다.
재현 가이드 원문은 [`docs/stats-misestimation-repro.md`](docs/stats-misestimation-repro.md).

**무슨 일이 일어나는가**

1. 소형 테이블에 **새로운 uuid** 로 1,000행을 적재한다 (웹서버 기동 시 재적재).
2. auto-analyze 는 `autovacuum_naptime`(기본 60초) 뒤에나 돈다 → **통계에 그 값이 없다.**
3. 비-MCV 선택도 공식의 분모가 0 → 선택도 0 → **행 추정치가 최소 1로 clamp** 된다.
4. 옵티마이저는 "outer 가 1행이면 inner 를 한 번만 돌면 된다"고 계산해 **Nested Loop** 을 고른다.
5. 실제 outer 는 1,000행 → **큰 테이블 스캔이 1,000회 반복**된다.

**이 저장소에서 실측한 결과** (evt 30만 건 기준)

| | 추정 행 수 | inner 스캔 반복 | 버퍼 | 실행 시간 |
|---|---|---|---|---|
| ANALYZE 전 | **1** (실제 1,000) | **1,000회** | 1,726,992 | **1,704 ms** |
| ANALYZE 후 | 1,000 | 1회 | 1,742 | **2.0 ms** |

**데이터·쿼리·인덱스는 전혀 바뀌지 않았다. 통계만 바뀌었다.**

```sql
-- ANALYZE 전: Nested Loop, evt 를 1,000번 스캔
->  Index Only Scan using va_pkey on va v  (rows=1)   (actual rows=1000 loops=1)
->  Bitmap Heap Scan on evt e              (rows=56)  (actual rows=50 loops=1000)
                                                                    ^^^^^^^^^^

-- ANALYZE 후: Hash Join, evt 를 1번만 스캔
->  Bitmap Heap Scan on evt e  (rows=56)  (actual rows=50 loops=1)
->  Hash  ->  Seq Scan on va v (rows=1000) (actual rows=1000 loops=1)
```

**해결**은 적재 직후의 명시적 `ANALYZE` 한 줄 —
[`dbtestlab.db.analyze_table()`](src/dbtestlab/db.py). auto-analyze 가 결국 고쳐주지만,
"고쳐주기까지의 공백"이 하필 조회가 몰리는 기동 직후와 겹치는 게 문제였다.
그 공백의 존재 자체도 테스트로 단언한다:

```python
def test_auto_analyze_대상이지만_아직_돌지_않았다(after_restart):
    ...
    assert row.n_mod_since_analyze > trigger_point  # 발동 조건은 이미 넘겼는데
    assert row.last_autoanalyze is None  # 아직 돌지 않았다
```

## 설계 노트

### 격리는 트랜잭션 롤백이 기본

테스트마다 커넥션 하나를 잡고 바깥 트랜잭션을 연 뒤, 끝나면 롤백한다.
`TRUNCATE` 보다 훨씬 빠르고 테스트 순서에 영향받지 않는다.

```python
with Session(bind=connection, join_transaction_mode="create_savepoint") as sess:
    yield sess
```

`join_transaction_mode="create_savepoint"` 덕분에 **테스트 안에서 `commit()` 을 불러도**
바깥 트랜잭션은 살아 있다. 커밋하는 코드도 그대로 테스트할 수 있다는 뜻이다.

커밋이 진짜로 필요한 동시성 테스트만 `clean_db` 픽스처(앞뒤로 `TRUNCATE`)를 쓴다.

### 병렬 실행은 워커마다 DB 를 따로 판다

`pytest -n` 으로 돌리면 워커들이 한 DB 를 나눠 쓰게 되는데, 그러면 `clean_db` 의
`TRUNCATE` 가 **다른 워커가 쓰는 중인 데이터를 지운다.** 재현 테스트가 만드는 임시
테이블도 같은 이유로 밟힌다. 그래서 `database_url` 픽스처가 `PYTEST_XDIST_WORKER` 를
보고 `<원본DB>_gw0` 같은 DB 를 만들었다가 세션이 끝나면 지운다.

재현 테스트(`va`, `evt`)는 한 걸음 더 나가서 전용 `repro` 스키마에 테이블을 만든다.
`truncate_all()` 은 `public` 만 훑으므로 실행 순서와 무관하게 서로를 건드리지 않는다.

### N+1 은 시간이 아니라 개수로 잡는다

`dbtestlab.testing.count_queries()` 가 실행된 SQL 을 센다.
개발 DB 에서는 100ms 라 안 보이지만 운영 데이터에서는 그대로 장애가 되는 종류의 문제다.

```python
with count_queries(session) as counter:
    total_quantity_per_member_eager(session)
assert counter.count == 2
```

계측 대상이 커넥션(또는 커넥션에 바인딩된 세션)이면 **그 커넥션에서 나간 SQL 만** 센다.
엔진 전체에 리스너를 걸면 다른 커넥션·스레드의 쿼리까지 섞여 개수 단언이 조용히 흔들린다.

### 실행계획은 문자열 grep 대신 JSON 으로

`dbtestlab.planner` 가 `EXPLAIN (FORMAT JSON)` 을 노드 트리로 파싱해
`estimated_rows()` / `actual_rows()` / `loops()` / `misestimation_ratio()` 를 제공한다.
플랜 텍스트를 grep 하는 테스트는 PostgreSQL 버전만 올라가도 깨진다.

### 스키마의 단일 소스는 SQL 마이그레이션

`migrations/V{번호}__{설명}.sql` 을 번호순으로 실행하고 `schema_version` 에 기록한다
([`dbtestlab.db.migrate()`](src/dbtestlab/db.py)). Alembic 이 필요해지면 이 함수만 갈아끼우면 된다.
ORM 매핑이 이 스키마와 어긋나면 `test_schema.py` 가 잡는다.

디렉터리 위치는 `migrations_dir()` 이 찾는다 — 환경변수 `DBTESTLAB_MIGRATIONS_DIR` →
패키지에 함께 실린 사본(휠 설치) → 상위 디렉터리 순. 경로를 `parents[2]` 로 고정해두면
소스 체크아웃에서만 동작하고 설치본에서는 조용히 깨진다.

## 구조

```
db-test-lab/
├── migrations/V1__init.sql        # 스키마 (단일 소스)
├── src/dbtestlab/
│   ├── db.py                      # 엔진, 마이그레이션, analyze_table, truncate_all
│   ├── models.py                  # SQLAlchemy 2.0 매핑 (Member, Order)
│   ├── repository.py              # 테스트 대상 쿼리 — "느린 버전"과 "고친 버전"을 나란히
│   ├── planner.py                 # EXPLAIN (FORMAT JSON) 파서
│   └── testing.py                 # count_queries, explain 헬퍼
├── tests/
│   ├── conftest.py                # DB 준비 + 격리 전략 (여기부터 읽으면 된다)
│   └── test_*.py
└── docs/stats-misestimation-repro.md
```

## 요구사항

- Python 3.11+
- PostgreSQL 16 (Docker / Testcontainers / 기존 인스턴스 중 아무거나)
