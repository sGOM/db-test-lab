# 통계 오추정 → Nested Loop 폭발 재현 가이드

빈 PostgreSQL 인스턴스만으로 "이벤트 리스트 120초" 사건과 동일한 문제를 재현하고 확인하는 스크립트.
프로젝트 코드나 운영 DB 없이 psql만으로 10분 안에 끝난다.

**재현하는 현상**: 소형 테이블에 새로운 uuid로 대량 insert한 직후(ANALYZE 전),
그 uuid 조건이 1행으로 오추정되어 Nested Loop이 선택되고, 큰 테이블 스캔이 1,000회 반복된다.
ANALYZE 한 번으로 Hash Join(스캔 1회)으로 바뀌며 수십~수백 배 빨라진다.

## 실제 사건과의 대응

| 재현 스크립트 | 실제 (zng) |
|---|---|
| `va` 테이블 | `zng_valid_alert_myitem_code` |
| `evt` 테이블 (200만 행) | `rt_ems_event` |
| `ws_id` uuid | `z_wsinstanceid` (wsInstanceId) |
| "새 uuid B로 1,000행 insert" | 웹서버 기동 시 `AlertService.postConstruct()`의 재적재 |
| `autovacuum_enabled = off` | auto-analyze가 돌기 전의 공백 시간대 |

---

## 0. 준비 — PostgreSQL 하나 띄우기

로컬에 PostgreSQL이 없으면 Docker로:

```bash
docker run --name pg-repro -e POSTGRES_PASSWORD=repro -p 5433:5432 -d postgres:16
docker exec -it pg-repro psql -U postgres
```

이후 모든 SQL은 psql 안에서 실행한다.

## 1. 스키마 생성

`autovacuum_enabled = off`가 핵심이다 — 운영에서 "auto-analyze가 아직 안 돈 공백"에 해당하는
상태를 고정해서 재현을 결정적으로 만든다.

```sql
create table va (
    ws_id     uuid   not null,
    infra_id  bigint not null,
    item_code bigint not null,
    primary key (ws_id, infra_id, item_code)
) with (autovacuum_enabled = off);

create table evt (
    id        bigserial primary key,
    infra_id  bigint    not null,
    item_code bigint    not null,
    list_id   bigint    not null,
    evt_time  timestamp not null
) with (autovacuum_enabled = off);
```

## 2. 데이터 적재 — "운영 정상 상태" 만들기

이벤트 200만 건(최근 100일 균등 분포)과, 구 인스턴스 uuid `aaaa...`의 1,000행을 넣고
**여기까지는 ANALYZE를 해준다** (운영에서 통계가 정상이던 시점):

```sql
-- 이벤트 200만 건 (30초~1분 소요)
insert into evt (infra_id, item_code, list_id, evt_time)
select (i % 10) + 1, (i % 100) + 1, (i % 50) + 1,
       now() - (random() * interval '100 days')
from generate_series(1, 2000000) i;

create index evt_time_idx on evt (evt_time desc);

-- 구 인스턴스 A: (infra 10종 × item 100종) = 1,000행
insert into va
select 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', inf, item
from generate_series(1, 10) inf, generate_series(1, 100) item;

vacuum analyze va;
vacuum analyze evt;
```

## 3. "웹서버 재기동" 시뮬레이션 — 새 uuid 적재, ANALYZE 없음

```sql
insert into va
select 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', inf, item
from generate_series(1, 10) inf, generate_series(1, 100) item;
-- 일부러 ANALYZE 하지 않는다
```

## 4. 문제 확인 ① — 통계에 새 uuid가 없다

```sql
select n_distinct, most_common_vals, most_common_freqs
from pg_stats
where tablename = 'va' and attname = 'ws_id';
```

기대 결과: `most_common_vals`에 `aaaa...`만 있고 빈도 1.0, `n_distinct = 1`.
→ 선택도 공식 `(1 - sum(freqs)) / (n_distinct - num_mcv) = (1 - 1.0) / 0` → 0으로 처리 → 최소 1행 clamp.

## 5. 문제 확인 ② — 같은 쿼리, 값에 따라 추정이 극단적으로 다름

```sql
explain select * from va where ws_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
explain select * from va where ws_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
```

기대 결과 (수치는 환경마다 다름):

```
-- A (MCV에 있음): 정확
Seq Scan on va  (cost=... rows=1000 ...)

-- B (MCV에 없음): 실제 1,000행인데 1행으로 오추정
Index Only Scan using va_pkey on va  (cost=... rows=1 ...)
```

## 6. 문제 확인 ③ — 오추정이 조인 플랜을 뒤집는다

실제 사건의 이벤트 리스트 조회에 해당하는 쿼리:

```sql
explain (analyze, buffers)
select count(*)
from evt e
join va v on v.ws_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
         and v.infra_id  = e.infra_id
         and v.item_code = e.item_code
where e.evt_time >= now() - interval '1 day'
  and e.list_id = 7;
```

기대 결과 — va가 outer인 **Nested Loop**이 선택되고, evt 스캔이 **1,000회 반복**된다:

```
Nested Loop  (actual time=... rows=...)
  Join Filter: ((v.infra_id = e.infra_id) AND (v.item_code = e.item_code))
  ->  Index Only Scan using va_pkey on va v
        (rows=1)  (actual ... rows=1000 ...)     <- 추정 1 vs 실제 1000
  ->  Bitmap Heap Scan on evt e
        (actual ... loops=1000)                  <- 동일 스캔 1,000회 반복
        Recheck Cond: (evt_time >= ...)
```

실행 시간 수 초~수십 초 (환경에 따라 다름). 플래너 입장에서는
"outer 1행이면 inner를 1번만 돌면 된다"는 계산이었으나 실제 outer가 1,000행이라 1,000배 폭발.

이것이 실제 사건에서 `rt_ems_event` 비트맵 스캔(회당 128ms)이 939회 반복되어 120초가 된 구조다.

> **만약 Nested Loop이 선택되지 않으면**: 플랜 선택은 비용 계산의 미세한 차이로 갈릴 수 있다.
> 이 경우에도 ④⑤의 오추정(rows=1)은 결정적으로 재현되며, 조인 플랜까지 뒤집어 보려면
> `set enable_hashjoin = off;`로 강제해 NL의 실행 시간을 확인한 뒤 `reset enable_hashjoin;` 하거나,
> evt 건수를 늘리고(`generate_series(1, 5000000)`) 시간 조건을 좁혀 다시 시도한다.
> 실제 사건에서는 pushdown된 조건 덕에 evt 추정이 39행까지 작아지면서 NL이 자연 선택되었다.

## 7. 조치와 검증 — ANALYZE 한 줄

```sql
analyze va;
```

4~6을 다시 실행하면:

```sql
-- ④ 통계: most_common_vals에 bbbb... 추가, 빈도 ~0.5, n_distinct = 2
-- ⑤ 추정: B도 rows=1000 으로 교정
-- ⑥ 플랜: Hash Join으로 전환, evt 스캔 1회
Hash Join  (actual time=...)
  ->  Bitmap Heap Scan on evt e  (... loops=1)   <- 스캔 1회
  ->  Hash
        ->  Index Only Scan using va_pkey on va v (rows=1000)
```

실행 시간이 수십 ms 수준으로 떨어진다. **데이터·쿼리·인덱스는 그대로이고 통계만 바뀌었다**는 점이 핵심.

## 8. 보너스 — auto-analyze가 결국 고쳐주는 것 확인 (선택)

3번을 다시 재현한 뒤 autovacuum을 켜고 기다리면 저절로 교정되는 것도 볼 수 있다:

```sql
alter table va set (autovacuum_enabled = on);
-- 1~2분 대기 (autovacuum_naptime 기본 60초)
select last_autoanalyze from pg_stat_user_tables where relname = 'va';
```

이 "저절로 고쳐지기까지의 공백"이 운영에서는 웹서버 기동 직후 = 조회가 시작되는 시점과
겹치기 때문에, 실제 조치는 적재 직후 코드에서 명시적 ANALYZE를 실행하는 것이었다 (PR #14278).

## 9. 정리

```bash
docker rm -f pg-repro
```

---

## 참고

- 비-MCV 선택도 공식: https://www.postgresql.org/docs/current/row-estimation-examples.html
- 최소 1행 clamp (`clamp_row_est`): https://github.com/postgres/postgres/blob/master/src/backend/optimizer/path/costsize.c
- auto-analyze 발동 조건: https://www.postgresql.org/docs/current/routine-vacuuming.html#AUTOVACUUM
- 실제 사건의 진단 절차·플랜 읽는 법: `docs/query-plan-misestimation-runbook.md`
