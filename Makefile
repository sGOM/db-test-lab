.DEFAULT_GOAL := help

VENV      ?= .venv
PYTHON    ?= $(VENV)/bin/python
PYTEST    ?= $(PYTHON) -m pytest
COMPOSE_DB_URL := postgresql+psycopg://postgres:postgres@localhost:5433/dbtestlab

.PHONY: help
help:  ## 사용 가능한 명령 보기
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install:  ## 가상환경 만들고 의존성 설치
	uv venv $(VENV) || python3 -m venv $(VENV)
	uv pip install --python $(PYTHON) -e '.[dev]'

.PHONY: up
up:  ## 로컬 PostgreSQL 기동 (docker compose)
	docker compose up -d --wait

.PHONY: down
down:  ## 로컬 PostgreSQL 종료
	docker compose down -v

.PHONY: test
test:  ## 전체 테스트 (docker compose 로 띄운 DB 사용)
	TEST_DATABASE_URL=$(COMPOSE_DB_URL) $(PYTEST)

.PHONY: test-fast
test-fast:  ## slow 마커를 뺀 빠른 테스트만
	TEST_DATABASE_URL=$(COMPOSE_DB_URL) $(PYTEST) -m "not slow"

.PHONY: test-tc
test-tc:  ## Testcontainers 로 DB를 직접 띄워서 테스트 (TEST_DATABASE_URL 미지정)
	$(PYTEST)

.PHONY: repro
repro:  ## 통계 오추정 → Nested Loop 폭발 재현 테스트만 실행
	TEST_DATABASE_URL=$(COMPOSE_DB_URL) $(PYTEST) tests/test_planner_stats.py -v

.PHONY: lint
lint:  ## ruff 검사
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

.PHONY: psql
psql:  ## 로컬 DB 에 psql 접속
	PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d dbtestlab
