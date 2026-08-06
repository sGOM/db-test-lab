"""통계 오추정 → Nested Loop 폭발 재현 및 해결.

실제 장애(이벤트 리스트 조회 120초)와 같은 구조를 빈 PostgreSQL 위에서 재현한다.

    소형 테이블에 **새로운 값**으로 대량 INSERT → auto-analyze 가 돌기 전 공백 →
    그 값의 **행 추정치가 1로 clamp** → **옵티마이저**가 Nested Loop 선택 →
    큰 테이블 스캔이 outer 행 수(1,000회)만큼 반복.

핵심은 "데이터·쿼리·인덱스는 그대로인데 통계만 바뀌면 플랜이 뒤집힌다"는 것이다.
해결은 적재 직후의 명시적 ``ANALYZE`` 한 줄(:func:`dbtestlab.db.analyze_table`).

원본 재현 가이드: ``docs/stats-misestimation-repro.md``
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, text

from dbtestlab.db import analyze_table
from dbtestlab.planner import (
    actual_rows,
    estimated_rows,
    explain,
    loops,
    misestimation_ratio,
    node_types,
    require_node,
)

pytestmark = pytest.mark.slow

# 구 인스턴스(통계에 반영돼 있음) / 새 인스턴스(웹서버 재기동으로 방금 적재됨)
WS_OLD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WS_NEW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

# uuid 하나당 (infra 10종 × item 100종) = 1,000행
ROWS_PER_WS = 10 * 100

# 원본 가이드는 200만 건. 테스트에서는 기본 30만 건으로 줄인다 —
# 오추정과 반복 스캔 구조는 건수와 무관하게 그대로 재현된다.
EVT_ROWS = int(os.getenv("REPRO_EVT_ROWS", "300000"))

# 실제 사건의 "이벤트 리스트 조회"에 해당하는 쿼리. 최근 1일 + 특정 리스트로 좁힌다.
JOIN_SQL = f"""
SELECT count(*)
  FROM evt e
  JOIN va v ON v.ws_id = '{WS_NEW}'
           AND v.infra_id  = e.infra_id
           AND v.item_code = e.item_code
 WHERE e.evt_time >= now() - interval '1 day'
   AND e.list_id = 7
"""

# 조인 순서를 문법 순서로 고정하기 위한 변형 (아래 강제 재현 테스트에서만 사용)
JOIN_SQL_VA_FIRST = f"""
SELECT count(*)
  FROM va v
  JOIN evt e ON v.infra_id  = e.infra_id
            AND v.item_code = e.item_code
 WHERE v.ws_id = '{WS_NEW}'
   AND e.evt_time >= now() - interval '1 day'
   AND e.list_id = 7
"""


def _select_by_ws(ws_id: str) -> str:
    return f"SELECT * FROM va WHERE ws_id = '{ws_id}'"


def _insert_ws(conn: Connection, ws_id: str) -> None:
    """uuid 하나에 대해 1,000행을 적재한다 (웹서버 기동 시 재적재에 해당)."""
    conn.execute(
        text(
            """
            INSERT INTO va (ws_id, infra_id, item_code)
            SELECT CAST(:ws_id AS uuid), inf, item
              FROM generate_series(1, 10) inf, generate_series(1, 100) item
            """
        ),
        {"ws_id": ws_id},
    )


@pytest.fixture(scope="module")
def repro_conn(engine: Engine) -> Iterator[Connection]:
    """재현용 테이블을 만들고 "운영 정상 상태"까지 적재한 커넥션.

    - AUTOCOMMIT: VACUUM 은 트랜잭션 안에서 실행할 수 없고,
      auto-analyze 관련 통계(pg_stat_user_tables)도 커밋돼야 갱신된다.
    - ``autovacuum_enabled = off``: "auto-analyze 가 아직 안 돈 공백"을 고정해
      재현을 결정적으로 만든다.
    """
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    # 병렬 플랜이 끼면 노드 구조와 loops 해석이 흔들린다. 재현 목적상 끈다.
    conn.execute(text("SET max_parallel_workers_per_gather = 0"))

    conn.execute(text("DROP TABLE IF EXISTS evt"))
    conn.execute(text("DROP TABLE IF EXISTS va"))
    conn.execute(
        text(
            """
            CREATE TABLE va (
                ws_id     uuid   NOT NULL,
                infra_id  bigint NOT NULL,
                item_code bigint NOT NULL,
                PRIMARY KEY (ws_id, infra_id, item_code)
            ) WITH (autovacuum_enabled = off)
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE evt (
                id        bigserial PRIMARY KEY,
                infra_id  bigint    NOT NULL,
                item_code bigint    NOT NULL,
                list_id   bigint    NOT NULL,
                evt_time  timestamp NOT NULL
            ) WITH (autovacuum_enabled = off)
            """
        )
    )

    # 이벤트: 최근 100일에 균등 분포. setseed 로 실행마다 같은 분포를 만든다.
    conn.execute(text("SELECT setseed(0.42)"))
    conn.execute(
        text(
            """
            INSERT INTO evt (infra_id, item_code, list_id, evt_time)
            SELECT (i % 10) + 1, (i % 100) + 1, (i % 50) + 1,
                   now() - (random() * interval '100 days')
              FROM generate_series(1, :rows) i
            """
        ),
        {"rows": EVT_ROWS},
    )
    conn.execute(text("CREATE INDEX evt_time_idx ON evt (evt_time DESC)"))

    # 구 인스턴스까지가 "통계가 정상이던 시점"
    _insert_ws(conn, WS_OLD)
    conn.execute(text("VACUUM ANALYZE va"))
    conn.execute(text("VACUUM ANALYZE evt"))

    try:
        yield conn
    finally:
        conn.execute(text("DROP TABLE IF EXISTS evt"))
        conn.execute(text("DROP TABLE IF EXISTS va"))
        conn.close()


@pytest.fixture
def after_restart(repro_conn: Connection) -> Iterator[Connection]:
    """웹서버 재기동 시뮬레이션: 새 uuid 로 1,000행 적재하고 **ANALYZE 하지 않는다**.

    테스트가 끝나면 지우고 ANALYZE 해서 다음 테스트가 같은 출발선에 서게 한다.
    """
    _insert_ws(repro_conn, WS_NEW)

    try:
        yield repro_conn
    finally:
        repro_conn.execute(
            text("DELETE FROM va WHERE ws_id = CAST(:ws_id AS uuid)"), {"ws_id": WS_NEW}
        )
        repro_conn.execute(text("ANALYZE va"))


# --------------------------------------------------------------------------------------
# ① 문제: 통계에 새 값이 없다
# --------------------------------------------------------------------------------------


def test_적재_직후_통계에는_새_uuid가_없다(after_restart: Connection):
    row = after_restart.execute(
        text(
            """
            SELECT n_distinct, most_common_vals::text AS mcv
              FROM pg_stats
             WHERE tablename = 'va' AND attname = 'ws_id'
            """
        )
    ).one()

    # 마지막 ANALYZE 시점에는 구 uuid 뿐이었다 → MCV 도 n_distinct 도 그 시점에 멈춰 있다.
    assert row.n_distinct == 1
    assert WS_OLD in row.mcv
    assert WS_NEW not in row.mcv


def test_auto_analyze_대상이지만_아직_돌지_않았다(after_restart: Connection):
    """ "저절로 고쳐지기까지의 공백" 자체를 단언한다.

    1,000행을 넣었으니 auto-analyze 발동 조건(threshold + scale_factor × reltuples)은
    이미 넘었다. 그런데 autovacuum 이 꺼져 있어(운영에서는 naptime 60초 대기)
    통계는 아직 옛날 것이다. 조회는 바로 이 구간에 들어온다.
    """
    after_restart.execute(text("SELECT pg_stat_force_next_flush()"))
    row = after_restart.execute(
        text(
            """
            SELECT s.n_mod_since_analyze,
                   s.last_autoanalyze,
                   c.reltuples,
                   current_setting('autovacuum_analyze_threshold')::float    AS threshold,
                   current_setting('autovacuum_analyze_scale_factor')::float AS scale_factor
              FROM pg_stat_user_tables s
              JOIN pg_class c ON c.oid = s.relid
             WHERE s.relname = 'va'
            """
        )
    ).one()

    trigger_point = row.threshold + row.scale_factor * row.reltuples

    assert row.n_mod_since_analyze >= ROWS_PER_WS
    assert row.n_mod_since_analyze > trigger_point, "auto-analyze 발동 조건은 이미 넘겼다"
    assert row.last_autoanalyze is None, "그런데 autovacuum 이 꺼져 있어 아직 돌지 않았다"


# --------------------------------------------------------------------------------------
# ② 문제: 같은 쿼리인데 값에 따라 행 추정치가 1,000배 어긋난다
# --------------------------------------------------------------------------------------


def test_MCV에_없는_값은_1행으로_오추정된다(after_restart: Connection):
    """비-MCV 선택도는 ``(1 - sum(freqs)) / (n_distinct - num_mcv)``.

    지금은 MCV 가 전부(빈도 1.0)라 분자가 0 → 선택도 0 → 옵티마이저가 최소 1행으로
    clamp 한다. 실제로는 1,000행인데 1행으로 본다.
    """
    plan_old = explain(after_restart, _select_by_ws(WS_OLD), analyze=True)
    plan_new = explain(after_restart, _select_by_ws(WS_NEW), analyze=True)

    # 실제 행 수는 둘 다 1,000
    assert actual_rows(plan_old) == ROWS_PER_WS
    assert actual_rows(plan_new) == ROWS_PER_WS

    # 통계에 있는 값은 자릿수가 맞고(테이블이 커진 만큼 약간 부풀지만),
    # 통계에 없는 값은 최소 1행으로 clamp 된다.
    assert misestimation_ratio(plan_old) < 2, "MCV 에 있는 값은 대략 맞게 추정된다"
    assert estimated_rows(plan_new) == 1
    assert misestimation_ratio(plan_new) >= 100, "세 자릿수 과소추정"


# --------------------------------------------------------------------------------------
# ③ 문제: 오추정이 조인 플랜을 뒤집고, 큰 테이블 스캔을 1,000번 반복시킨다
# --------------------------------------------------------------------------------------


def test_오추정된_outer가_큰_테이블_스캔을_1000번_반복시킨다(after_restart: Connection):
    """ "outer 1행이면 inner 를 1번만 돌면 된다"는 계산이 1,000배로 어긋나는 구조.

    플랜 선택은 비용 차이로 갈릴 수 있어서, 재현을 결정적으로 만들려고 조인 순서·조인
    방식·머티리얼라이즈를 고정한다. (실제 사건에서는 pushdown 된 조건 덕에 evt 추정이
    39행까지 작아지면서 Nested Loop 이 자연 선택됐다.)
    오추정 자체(rows=1)는 위 테스트처럼 강제 없이도 결정적으로 재현된다.
    """
    knobs = (
        "join_collapse_limit = 1",
        "enable_hashjoin = off",
        "enable_mergejoin = off",
        "enable_material = off",
    )
    for knob in knobs:
        after_restart.execute(text(f"SET {knob}"))
    try:
        plan = explain(after_restart, JOIN_SQL_VA_FIRST, analyze=True, buffers=True)
    finally:
        for knob in knobs:
            after_restart.execute(text(f"RESET {knob.split('=')[0].strip()}"))

    assert "Nested Loop" in node_types(plan)

    va_node = require_node(plan, relation="v")
    evt_node = require_node(plan, relation="e")

    # outer: 추정 1행 vs 실제 1,000행
    assert estimated_rows(va_node) == 1
    assert actual_rows(va_node) == ROWS_PER_WS

    # inner: 동일한 큰 테이블 스캔이 outer 행 수만큼 반복된다
    assert loops(evt_node) == ROWS_PER_WS, (
        f"inner 스캔 반복 횟수가 예상과 다릅니다. 플랜 노드: {node_types(plan)}"
    )


# --------------------------------------------------------------------------------------
# ④ 해결: ANALYZE 한 줄. 데이터·쿼리·인덱스는 그대로다.
# --------------------------------------------------------------------------------------


def test_ANALYZE_후_통계에_새_uuid가_들어온다(after_restart: Connection):
    analyze_table(after_restart, "va")

    row = after_restart.execute(
        text(
            """
            SELECT n_distinct, most_common_vals::text AS mcv, most_common_freqs
              FROM pg_stats
             WHERE tablename = 'va' AND attname = 'ws_id'
            """
        )
    ).one()

    assert row.n_distinct == 2
    assert WS_OLD in row.mcv
    assert WS_NEW in row.mcv
    assert row.most_common_freqs == pytest.approx([0.5, 0.5], rel=0.1)


def test_ANALYZE_가_행_추정치를_교정한다(after_restart: Connection):
    before = explain(after_restart, _select_by_ws(WS_NEW), analyze=True)

    analyze_table(after_restart, "va")  # ← 적재 코드에 추가된 한 줄

    after = explain(after_restart, _select_by_ws(WS_NEW), analyze=True)

    assert estimated_rows(before) == 1
    assert estimated_rows(after) == pytest.approx(ROWS_PER_WS, rel=0.2)
    assert misestimation_ratio(after) == pytest.approx(1.0, rel=0.2)


def test_통계가_정확하면_큰_테이블을_한_번만_스캔한다(after_restart: Connection):
    """오추정 상태에서 폭발했던 그 쿼리가, 통계만 고치면 스캔 1회로 끝난다."""
    analyze_table(after_restart, "va")

    plan = explain(after_restart, JOIN_SQL, analyze=True, buffers=True)

    va_node = require_node(plan, relation="v")
    evt_node = require_node(plan, relation="e")

    assert estimated_rows(va_node) == pytest.approx(ROWS_PER_WS, rel=0.2)
    assert loops(evt_node) == 1, (
        f"큰 테이블 스캔이 반복되고 있습니다. 플랜 노드: {node_types(plan)}"
    )
