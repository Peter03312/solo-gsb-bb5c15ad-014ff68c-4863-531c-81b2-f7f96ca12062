"""多时段有限储水供水调度预演：连续等长时段内的储水-补水-分配试算。

业务规则：
1. 请求携带当前管网**修订号**、各水源的**初始储水与库容**，以及**非空、有序**
   （数组顺序即时间顺序）的等长时段清单；每期须**恰好**列出所有水源的补水量
   和所有用水点的需求量（未知/错类设施、重复或缺失条目整单拒绝）。
2. 初始储水不得超过库容；每期先**补水至库容**，超出量计入**溢出**；
   再按现有阀位、单向管段、共享容量及用水点优先级分配（engine.simulate，
   不叠加任何拟关阀门）。
3. 水源本期出水不得超过其**额定上限**，也不得超过**可用储水**
   （本期有效上限 = min(额定上限, 补水后可用储水)）；实际出水扣减储水，
   **欠供不结转**（下期需求以请求中列出的为准，不累计往期缺口）。
4. 预演只读：储水只在内存副本上逐期滚动，**不改变**管网、阀位、
   复水计划或修订号。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .engine import EPS, Network, simulate

# 设施类别中文名（与 derating 模块口径一致）
_TYPE_NAMES = {"source": "水源", "demand": "用水点", "junction": "连接点"}


def _facility_kind(network: Network, fid: str) -> str | None:
    """返回设施类别中文名；管网中完全不存在该编号时返回 None。"""
    node = network.nodes.get(fid)
    if node is not None:
        return _TYPE_NAMES.get(node.type, node.type)
    if fid in network.pipes:
        return "管段"
    if fid in network.valves:
        return "阀门"
    return None


def _check_facility(
    network: Network,
    fid: str,
    loc: str,
    want_label: str,
    errors: list[dict[str, Any]],
) -> bool:
    """校验设施编号存在且类别正确；错误带定位追加到 errors。"""
    kind = _facility_kind(network, fid)
    if kind is None:
        errors.append({
            "location": loc,
            "code": "unknown_facility",
            "facility_id": fid,
            "message": f"未知设施：{fid}（当前管网中不存在该设施）",
        })
        return False
    if kind != want_label:
        errors.append({
            "location": loc,
            "code": "wrong_facility_type",
            "facility_id": fid,
            "message": (
                f"设施类别错误：{fid} 是{kind}，不能作为{want_label}条目"
                f"（请核对编号或改放到对应清单）"
            ),
        })
        return False
    return True


def validate_storage(
    network: Network,
    sources: list[dict[str, Any]],
    periods: list[dict[str, Any]],
) -> tuple[
    dict[str, tuple[float, float]],
    list[tuple[dict[str, float], dict[str, float]]],
    list[dict[str, Any]],
]:
    """基于已提交快照逐条校验储水预演参数。

    返回 (source_config, normalized_periods, errors)：
    - source_config: 水源id -> (初始储水, 库容)，恰好覆盖全部水源；
    - normalized_periods: 每期 (水源id -> 补水量, 用水点id -> 需求量)；
    - errors 非空时整单拒绝（前两者不可用于预演）。
    """
    errors: list[dict[str, Any]] = []
    source_ids = set(network.source_ids())
    demand_ids = set(network.demand_ids())

    # ---- 各水源初始储水与库容：须恰好覆盖全部水源 ----
    config: dict[str, tuple[float, float]] = {}
    seen: set[str] = set()
    for i, item in enumerate(sources):
        loc = f"sources[{i}]"
        sid = item["source_id"]
        if sid in seen:
            errors.append({
                "location": f"{loc}.source_id",
                "code": "duplicate_entry",
                "facility_id": sid,
                "message": f"水源 {sid} 在 sources 中重复出现",
            })
            continue
        seen.add(sid)
        if not _check_facility(network, sid, f"{loc}.source_id", "水源", errors):
            continue
        initial = float(item["initial_storage"])
        capacity = float(item["storage_capacity"])
        # 初始储水不得超过库容（1e-9 容差，吸收浮点序列化误差）
        if initial > capacity + EPS:
            errors.append({
                "location": f"{loc}.initial_storage",
                "code": "storage_above_capacity",
                "facility_id": sid,
                "storage_capacity": capacity,
                "initial_storage": initial,
                "message": (
                    f"水源 {sid} 的初始储水 {initial:g} 超过库容 {capacity:g}；"
                    "初始储水不得大于库容"
                ),
            })
            continue
        config[sid] = (initial, capacity)
    for sid in sorted(source_ids - seen):
        errors.append({
            "location": "sources",
            "code": "missing_entry",
            "facility_id": sid,
            "message": f"缺少水源 {sid} 的初始储水与库容条目（须恰好覆盖全部水源）",
        })

    # ---- 逐时段校验：补水清单恰好覆盖全部水源，需求清单恰好覆盖全部用水点 ----
    normalized: list[tuple[dict[str, float], dict[str, float]]] = []
    for p, period in enumerate(periods):
        ploc = f"periods[{p}]"

        replen: dict[str, float] = {}
        seen_r: set[str] = set()
        for j, item in enumerate(period["replenishment"]):
            loc = f"{ploc}.replenishment[{j}]"
            sid = item["source_id"]
            if sid in seen_r:
                errors.append({
                    "location": f"{loc}.source_id",
                    "code": "duplicate_entry",
                    "facility_id": sid,
                    "message": f"水源 {sid} 在第 {p + 1} 期补水清单中重复出现",
                })
                continue
            seen_r.add(sid)
            if _check_facility(network, sid, f"{loc}.source_id", "水源", errors):
                replen[sid] = float(item["amount"])
        for sid in sorted(source_ids - seen_r):
            errors.append({
                "location": f"{ploc}.replenishment",
                "code": "missing_entry",
                "facility_id": sid,
                "message": f"第 {p + 1} 期补水清单缺少水源 {sid}（每期须列出所有水源）",
            })

        dem: dict[str, float] = {}
        seen_d: set[str] = set()
        for j, item in enumerate(period["demands"]):
            loc = f"{ploc}.demands[{j}]"
            did = item["demand_id"]
            if did in seen_d:
                errors.append({
                    "location": f"{loc}.demand_id",
                    "code": "duplicate_entry",
                    "facility_id": did,
                    "message": f"用水点 {did} 在第 {p + 1} 期需求清单中重复出现",
                })
                continue
            seen_d.add(did)
            if _check_facility(network, did, f"{loc}.demand_id", "用水点", errors):
                dem[did] = float(item["amount"])
        for did in sorted(demand_ids - seen_d):
            errors.append({
                "location": f"{ploc}.demands",
                "code": "missing_entry",
                "facility_id": did,
                "message": f"第 {p + 1} 期需求清单缺少用水点 {did}（每期须列出所有用水点）",
            })

        normalized.append((replen, dem))

    return config, normalized, errors


def _period_network(
    network: Network,
    available: dict[str, float],
    period_demands: dict[str, float],
) -> Network:
    """构造本期内存副本：水源有效上限 = min(额定上限, 可用储水)，需求替换为本期值。"""
    nodes = dict(network.nodes)
    for sid in network.source_ids():
        node = nodes[sid]
        effective = min(node.capacity or 0.0, available[sid])
        nodes[sid] = replace(node, capacity=effective)
    for did in network.demand_ids():
        nodes[did] = replace(nodes[did], demand=period_demands[did])
    return Network(
        nodes=nodes,
        pipes=network.pipes,
        valves=network.valves,
        forbidden_pairs=network.forbidden_pairs,
        valves_by_pipe=network.valves_by_pipe,
    )


def preview(
    network: Network,
    revision: int,
    config: dict[str, tuple[float, float]],
    periods: list[tuple[dict[str, float], dict[str, float]]],
) -> dict[str, Any]:
    """逐期滚动试算：补水（溢出）→ 分配 → 出水扣减储水。

    调用方须先完成修订号校验与 validate_storage 条目校验。
    """
    source_ids = network.source_ids()
    # 期初储水（逐期滚动，只在内存中）
    stored = {sid: initial for sid, (initial, _cap) in config.items()}
    capacities = {sid: cap for sid, (_initial, cap) in config.items()}

    period_results: list[dict[str, Any]] = []
    total_requested = total_supplied = total_shortfall = total_overflow = 0.0

    for p, (replen, period_demands) in enumerate(periods, start=1):
        # 1) 补水至库容，超出量计入溢出
        available: dict[str, float] = {}
        overflow: dict[str, float] = {}
        for sid in source_ids:
            after = stored[sid] + replen[sid]
            overflow[sid] = round(max(0.0, after - capacities[sid]), 9)
            available[sid] = round(min(after, capacities[sid]), 9)

        # 2) 本期分配：沿用现有阀位、单向管段、共享容量与优先级；
        #    水源有效上限 = min(额定上限, 可用储水)
        period_net = _period_network(network, available, period_demands)
        result = simulate(period_net, force_closed=set())
        used = {s["source_id"]: s["used"] for s in result["source_utilization"]}

        # 3) 实际出水扣减储水（欠供不结转，下期需求以请求为准）
        storage_rows: list[dict[str, Any]] = []
        for sid in source_ids:
            output = used.get(sid, 0.0)
            start = round(stored[sid], 9)
            end = round(max(0.0, available[sid] - output), 9)
            stored[sid] = end
            storage_rows.append({
                "source_id": sid,
                "rated_capacity": network.nodes[sid].capacity or 0.0,
                "storage_capacity": capacities[sid],
                "storage_start": start,
                "replenished": round(replen[sid], 9),
                "overflow": overflow[sid],
                "available": available[sid],
                "output": output,
                "storage_end": end,
            })

        period_overflow = round(sum(overflow.values()), 9)
        t = result["totals"]
        total_requested += t["requested"]
        total_supplied += t["supplied"]
        total_shortfall += t["shortfall"]
        total_overflow += period_overflow

        period_results.append({
            "period": p,
            "replenishment": [
                {"source_id": sid, "amount": round(replen[sid], 9)}
                for sid in source_ids
            ],
            "overflow_total": period_overflow,
            "sources": storage_rows,
            "allocations": result["allocations"],
            "totals": t,
            "pipe_utilization": result["pipe_utilization"],
        })

    return {
        "message": "储水调度预演完成：逐期结果为内存试算，"
        "未改变管网、阀位、计划或修订号",
        "revision": revision,
        "closed_valves": sorted(
            vid for vid, v in network.valves.items() if not v.is_open
        ),
        "storage_config": [
            {
                "source_id": sid,
                "rated_capacity": network.nodes[sid].capacity or 0.0,
                "initial_storage": config[sid][0],
                "storage_capacity": config[sid][1],
            }
            for sid in source_ids
        ],
        "periods": period_results,
        "final_storage": [
            {"source_id": sid, "storage": round(stored[sid], 9)}
            for sid in source_ids
        ],
        "totals": {
            "periods": len(periods),
            "requested": round(total_requested, 9),
            "supplied": round(total_supplied, 9),
            "shortfall": round(total_shortfall, 9),
            "overflow": round(total_overflow, 9),
        },
    }
