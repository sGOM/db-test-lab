"""실행계획(EXPLAIN) 을 구조체로 다루는 헬퍼.

플랜을 문자열로 grep 하면 테스트가 금세 깨진다. ``EXPLAIN (FORMAT JSON)`` 으로 받아
노드 단위로 "추정 행 수 / 실제 행 수 / 반복 횟수"를 꺼내 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Connection, text


def explain(
    conn: Connection,
    sql: str,
    *,
    analyze: bool = False,
    buffers: bool = False,
) -> dict[str, Any]:
    """최상위 플랜 노드를 dict 로 돌려준다.

    ``analyze=True`` 는 쿼리를 실제로 실행한다 (DML 에 쓰지 말 것).
    """
    options = ["FORMAT JSON"]
    if analyze:
        options.append("ANALYZE")
    if buffers:
        options.append("BUFFERS")

    result = conn.execute(text(f"EXPLAIN ({', '.join(options)}) {sql}")).scalar_one()
    # psycopg 가 json 을 이미 파싱해 준다. 드라이버가 문자열로 주는 경우만 방어.
    if isinstance(result, str):
        import json

        result = json.loads(result)
    return result[0]["Plan"]


def iter_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """플랜 트리를 위에서 아래로 순회한다."""
    yield node
    for child in node.get("Plans", []):
        yield from iter_nodes(child)


def node_types(node: dict[str, Any]) -> list[str]:
    return [n["Node Type"] for n in iter_nodes(node)]


def find_node(
    node: dict[str, Any],
    *,
    node_type: str | None = None,
    relation: str | None = None,
) -> dict[str, Any] | None:
    """조건에 맞는 첫 노드. ``relation`` 은 테이블명 또는 alias 로 찾는다."""
    for candidate in iter_nodes(node):
        if node_type is not None and candidate["Node Type"] != node_type:
            continue
        if relation is not None and relation not in (
            candidate.get("Relation Name"),
            candidate.get("Alias"),
        ):
            continue
        return candidate
    return None


def require_node(
    node: dict[str, Any],
    *,
    node_type: str | None = None,
    relation: str | None = None,
) -> dict[str, Any]:
    found = find_node(node, node_type=node_type, relation=relation)
    if found is None:
        raise AssertionError(
            f"플랜에서 노드를 찾지 못했습니다 (node_type={node_type!r}, relation={relation!r}). "
            f"실제 노드: {node_types(node)}"
        )
    return found


def estimated_rows(node: dict[str, Any]) -> int:
    """옵티마이저가 예상한 행 수 (1회 실행 기준)."""
    return int(node["Plan Rows"])


def actual_rows(node: dict[str, Any]) -> int:
    """실제로 나온 행 수 (1회 실행 기준 평균). ``analyze=True`` 여야 채워진다."""
    return int(node["Actual Rows"])


def loops(node: dict[str, Any]) -> int:
    """이 노드가 몇 번 반복 실행됐는지. Nested Loop 폭발의 증거."""
    return int(node["Actual Loops"])


def misestimation_ratio(node: dict[str, Any]) -> float:
    """추정 대비 실제 배율. 1.0 이면 정확, 1000.0 이면 1000배 과소추정."""
    estimated = max(estimated_rows(node), 1)
    return actual_rows(node) / estimated
