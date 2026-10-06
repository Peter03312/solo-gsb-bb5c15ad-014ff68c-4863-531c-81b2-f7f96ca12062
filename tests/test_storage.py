"""端到端测试：多时段有限储水供水调度预演 POST /api/storage/preview。

使用独立临时 SQLite，不依赖其它测试模块的数据状态。
"""
from __future__ import annotations

import os
import tempfile

import pytest

# 独立运行本文件时（不经过 test_api）确保有数据库文件；与其它模块同进程时共用
os.environ.setdefault(
    "WATER_DB_PATH",
    tempfile.NamedTemporaryFile(suffix=".db", delete=False).name,
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

init_db()


# 演示管网（同 tests/test_api.py）：
#   S1 --P1(cap10,V1)--> D1(需求10, 优先级9)
#   S2 --P2(cap6)------> J1 --P3(cap4,V3)--> D2(需求8, 优先级1)
#                            \--P6(cap6)----> D3(需求6, 优先级1)
#   P5(cap5,V5, 关闭): S1 -> S2
def valid_network():
    return {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 6},
        ],
        "demands": [
            {"id": "D1", "demand": 10, "priority": 9},
            {"id": "D2", "demand": 8, "priority": 1},
            {"id": "D3", "demand": 6, "priority": 1},
        ],
        "junctions": [{"id": "J1"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
            {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 6},
            {"id": "P3", "from_node": "J1", "to_node": "D2", "capacity": 4},
            {"id": "P6", "from_node": "J1", "to_node": "D3", "capacity": 6},
            {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5},
        ],
        "valves": [
            {"id": "V1", "pipe_id": "P1", "is_open": True},
            {"id": "V3", "pipe_id": "P3", "is_open": True},
            {"id": "V5", "pipe_id": "P5", "is_open": False},
        ],
        "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}],
    }


def storage_payload(rev, periods, sources=None):
    return {
        "revision": rev,
        "sources": sources if sources is not None else [
            {"source_id": "S1", "initial_storage": 15, "storage_capacity": 15},
            {"source_id": "S2", "initial_storage": 5, "storage_capacity": 8},
        ],
        "periods": periods,
    }


def full_period(r1, r2, d1, d2, d3):
    return {
        "replenishment": [
            {"source_id": "S1", "amount": r1},
            {"source_id": "S2", "amount": r2},
        ],
        "demands": [
            {"demand_id": "D1", "amount": d1},
            {"demand_id": "D2", "amount": d2},
            {"demand_id": "D3", "amount": d3},
        ],
    }


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client):
    r = client.put("/api/network", json=valid_network())
    assert r.status_code == 200, r.text
    revision = r.json()["revision"]
    yield client, revision


def test_requires_network_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/storage/preview", json=storage_payload(0, [full_period(0, 0, 1, 1, 1)]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    # 重新录入供后续用例
    client.put("/api/network", json=valid_network())


def test_multi_period_rollover_and_totals(loaded):
    """两期滚动：第 1 期补水溢出 + 满负荷供水；第 2 期储水耗尽、按可用储水限供。"""
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(rev, [
        full_period(0, 4, 10, 8, 6),   # S2: 5+4=9 > 库容8 -> 溢出1，可用8
        full_period(0, 0, 10, 8, 6),   # 无补水，吃库存
    ]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision"] == rev
    assert len(data["periods"]) == 2

    # ---- 第 1 期：S1 有效上限 min(10, 15)=10；S2 min(6, 8)=6 ----
    p1 = data["periods"][0]
    assert p1["period"] == 1
    assert p1["overflow_total"] == pytest.approx(1)
    s1, s2 = p1["sources"]
    assert s1["source_id"] == "S1"
    assert s1["storage_start"] == 15 and s1["replenished"] == 0
    assert s1["overflow"] == 0 and s1["available"] == 15
    assert s1["output"] == pytest.approx(10) and s1["storage_end"] == pytest.approx(5)
    assert s2["source_id"] == "S2"
    assert s2["storage_start"] == 5 and s2["replenished"] == 4
    assert s2["overflow"] == pytest.approx(1) and s2["available"] == pytest.approx(8)
    assert s2["output"] == pytest.approx(6) and s2["storage_end"] == pytest.approx(2)

    alloc = {a["demand_id"]: a for a in p1["allocations"]}
    assert alloc["D1"]["supplied"] == pytest.approx(10)
    assert alloc["D2"]["supplied"] == pytest.approx(4)   # 共享管段 P3 容量 4
    assert alloc["D3"]["supplied"] == pytest.approx(2)   # S2 余量 2
    assert p1["totals"] == {"requested": 24.0, "supplied": 16.0, "shortfall": 8.0}

    # ---- 第 2 期：S1 仅剩 5、S2 仅剩 2，出水受可用储水限制 ----
    p2 = data["periods"][1]
    s1, s2 = p2["sources"]
    assert s1["storage_start"] == pytest.approx(5)
    assert s1["output"] == pytest.approx(5) and s1["storage_end"] == 0
    assert s2["storage_start"] == pytest.approx(2)
    assert s2["output"] == pytest.approx(2) and s2["storage_end"] == 0
    alloc = {a["demand_id"]: a for a in p2["allocations"]}
    assert alloc["D1"]["supplied"] == pytest.approx(5)   # 储水限制，而非额定 10
    assert alloc["D1"]["shortfall_reason_code"] == "SOURCE_LIMIT"
    assert alloc["D2"]["supplied"] == pytest.approx(2)
    assert alloc["D3"]["supplied"] == 0
    assert p2["totals"] == {"requested": 24.0, "supplied": 7.0, "shortfall": 17.0}

    # ---- 全程汇总与期末储水 ----
    assert data["totals"] == {
        "periods": 2, "requested": 48.0, "supplied": 23.0,
        "shortfall": 25.0, "overflow": 1.0,
    }
    final = {s["source_id"]: s["storage"] for s in data["final_storage"]}
    assert final == {"S1": 0.0, "S2": 0.0}


def test_shortfall_not_carried_over(loaded):
    """欠供不结转：第 2 期需求以请求列出值为准，不累计往期缺口。"""
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(rev, [
        full_period(0, 0, 10, 8, 6),   # D2 欠 4、D3 欠 4
        full_period(0, 0, 10, 8, 6),   # 需求仍是 8/6，而非 12/10
    ]))
    assert r.status_code == 200, r.text
    p2 = r.json()["periods"][1]
    alloc = {a["demand_id"]: a for a in p2["allocations"]}
    assert alloc["D2"]["requested"] == pytest.approx(8)
    assert alloc["D3"]["requested"] == pytest.approx(6)
    assert p2["totals"]["requested"] == pytest.approx(24)


def test_replenishment_restores_supply(loaded):
    """第 2 期补水后恢复供水能力；补水不超过库容。"""
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(rev, [
        full_period(0, 0, 10, 8, 6),   # S1 -> 5, S2 -> 0（5 用尽）
        full_period(3, 20, 10, 8, 6),  # S1 补 3 -> 8；S2 补 20 溢出 12 -> 可用 8
    ]))
    assert r.status_code == 200, r.text
    p2 = r.json()["periods"][1]
    s1, s2 = p2["sources"]
    assert s1["storage_start"] == pytest.approx(5)
    assert s1["available"] == pytest.approx(8)
    assert s2["overflow"] == pytest.approx(12)
    assert s2["available"] == pytest.approx(8)
    alloc = {a["demand_id"]: a for a in p2["allocations"]}
    assert alloc["D1"]["supplied"] == pytest.approx(8)   # min(额定10, 可用8)
    assert alloc["D2"]["supplied"] == pytest.approx(4)
    assert alloc["D3"]["supplied"] == pytest.approx(2)
    assert r.json()["totals"]["overflow"] == pytest.approx(12)


def test_zero_initial_storage_and_zero_capacity(loaded):
    """初始储水/库容为 0 合法：无补水期该水源不出水。"""
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(
        rev,
        [full_period(0, 0, 10, 8, 6)],
        sources=[
            {"source_id": "S1", "initial_storage": 0, "storage_capacity": 0},
            {"source_id": "S2", "initial_storage": 0, "storage_capacity": 0},
        ],
    ))
    assert r.status_code == 200, r.text
    p1 = r.json()["periods"][0]
    assert p1["totals"]["supplied"] == 0
    assert all(s["output"] == 0 for s in p1["sources"])


def test_stale_revision_rejected(loaded):
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(
        rev - 1, [full_period(0, 0, 10, 8, 6)]
    ))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    d = err["details"][0]
    assert d["location"] == "revision"
    assert d["seen_revision"] == rev - 1 and d["current_revision"] == rev
    # 过期修订号不返回任何预演结果
    assert "periods" not in r.json() and "totals" not in r.json()


def test_unknown_and_wrong_type_facility_located(loaded):
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(
        rev,
        [full_period(0, 0, 10, 8, 6)],
        sources=[
            {"source_id": "GHOST", "initial_storage": 1, "storage_capacity": 2},
            {"source_id": "P1", "initial_storage": 1, "storage_capacity": 2},
            {"source_id": "D1", "initial_storage": 1, "storage_capacity": 2},
        ],
    ))
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_storage"
    details = body["details"]
    locs = {(d["location"], d["code"]) for d in details}
    assert ("sources[0].source_id", "unknown_facility") in locs
    assert ("sources[1].source_id", "wrong_facility_type") in locs
    assert ("sources[2].source_id", "wrong_facility_type") in locs
    # 三个非法条目之外，S1/S2 均未列出 -> 缺失条目也一次性报出
    missing = [d for d in details if d["code"] == "missing_entry"]
    assert {d["facility_id"] for d in missing} == {"S1", "S2"}
    assert "periods" not in r.json()


def test_duplicate_and_missing_source_entries(loaded):
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(
        rev,
        [full_period(0, 0, 10, 8, 6)],
        sources=[
            {"source_id": "S1", "initial_storage": 1, "storage_capacity": 2},
            {"source_id": "S1", "initial_storage": 1, "storage_capacity": 2},
        ],
    ))
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    codes = {(d["location"], d["code"]) for d in details}
    assert ("sources[1].source_id", "duplicate_entry") in codes
    assert ("sources", "missing_entry") in codes  # S2 缺失


def test_initial_storage_above_capacity_located(loaded):
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(
        rev,
        [full_period(0, 0, 10, 8, 6)],
        sources=[
            {"source_id": "S1", "initial_storage": 11, "storage_capacity": 10},
            {"source_id": "S2", "initial_storage": 5, "storage_capacity": 8},
        ],
    ))
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "sources[0].initial_storage"
    assert d["code"] == "storage_above_capacity"
    assert d["storage_capacity"] == 10 and d["initial_storage"] == 11


def test_period_entries_validated(loaded):
    """每期须恰好列出所有水源补水与所有用水点需求：缺失/重复/未知/错类一次报出。"""
    client, rev = loaded
    r = client.post("/api/storage/preview", json={
        "revision": rev,
        "sources": [
            {"source_id": "S1", "initial_storage": 5, "storage_capacity": 5},
            {"source_id": "S2", "initial_storage": 5, "storage_capacity": 5},
        ],
        "periods": [
            {
                "replenishment": [
                    {"source_id": "S1", "amount": 1},
                    {"source_id": "S1", "amount": 2},   # 重复
                    {"source_id": "D2", "amount": 1},   # 错类（用水点）
                ],
                "demands": [
                    {"demand_id": "D1", "amount": 1},
                    {"demand_id": "NOPE", "amount": 1},  # 未知
                    {"demand_id": "S1", "amount": 1},    # 错类（水源）
                ],
            },
            full_period(0, 0, 1, 1, 1),  # 第 2 期完全合法，仍整单拒绝
        ],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    got = {(d["location"], d["code"]) for d in details}
    assert ("periods[0].replenishment[1].source_id", "duplicate_entry") in got
    assert ("periods[0].replenishment[2].source_id", "wrong_facility_type") in got
    assert ("periods[0].replenishment", "missing_entry") in got        # 缺 S2
    assert ("periods[0].demands[1].demand_id", "unknown_facility") in got
    assert ("periods[0].demands[2].demand_id", "wrong_facility_type") in got
    missing_demands = {
        d["facility_id"] for d in details
        if d["location"] == "periods[0].demands" and d["code"] == "missing_entry"
    }
    assert missing_demands == {"D2", "D3"}
    assert "periods" not in r.json()


def test_negative_and_nan_rejected_422(loaded):
    client, rev = loaded
    bad = storage_payload(rev, [full_period(0, 0, 10, 8, 6)])
    bad["sources"][0]["initial_storage"] = -1
    r = client.post("/api/storage/preview", json=bad)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("initial_storage")

    bad = storage_payload(rev, [full_period(-2, 0, 10, 8, 6)])
    r = client.post("/api/storage/preview", json=bad)
    assert r.status_code == 422

    # NaN/Infinity 不是合法 JSON 数值：以原始请求体发送，由服务端 422 拒绝
    r = client.post(
        "/api/storage/preview",
        content=(
            '{"revision": %d, "sources": ['
            '{"source_id": "S1", "initial_storage": NaN, "storage_capacity": 1},'
            '{"source_id": "S2", "initial_storage": 0, "storage_capacity": 1}],'
            '"periods": [{"replenishment": [], "demands": []}]}' % rev
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_empty_periods_rejected_422(loaded):
    client, rev = loaded
    r = client.post("/api/storage/preview", json=storage_payload(rev, []))
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("periods")


def test_preview_does_not_mutate_state(loaded):
    client, rev = loaded
    before_net = client.get("/api/network").json()
    before_status = client.get("/api/status").json()
    r = client.post("/api/storage/preview", json=storage_payload(rev, [
        full_period(0, 4, 10, 8, 6),
        full_period(0, 0, 10, 8, 6),
    ]))
    assert r.status_code == 200
    # 管网、阀位、修订号与现状分派均不受预演影响
    after_net = client.get("/api/network").json()
    assert after_net == before_net
    assert after_net["revision"] == rev
    assert client.get("/api/status").json() == before_status
