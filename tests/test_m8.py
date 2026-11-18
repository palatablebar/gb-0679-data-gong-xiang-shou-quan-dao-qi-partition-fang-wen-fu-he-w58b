import pytest

from app import store
from app.errors import DomainError
from app.pipeline import calculate, disposition_stage, execute, load_sample
from app.modules.m8_dispositions.rules import (
    DEFAULT_TERMINATIONS, build_for_run, month_span, normalize_events, scope_conflicts,
    disposition_for,
)
from app.modules.m3_grants.rules import GRANTS


def _forms(sample="dirty.csv", params=None):
    return disposition_stage(calculate(load_sample(sample), params))["rows"]


def _by_event(forms):
    return {form["event_id"]: form for form in forms}


def test_real_termid_forms_cover_all_events():
    forms = _forms()
    assert [f["event_id"] for f in forms] == [e["event_id"] for e in DEFAULT_TERMINATIONS]


def test_live_grant_forces_principal_freeze():
    form = _by_event(_forms())["EV-001"]
    assert form["decision"] == "principal"
    assert form["frozen"]["kind"] == "principal"
    assert form["pending"] and all(item["reviewed"] is False for item in form["pending"])


def test_expired_only_grant_allows_partition_freeze():
    form = _by_event(_forms())["EV-002"]
    assert form["decision"] == "partitions"
    assert form["frozen"]["kind"] == "partitions"
    assert form["frozen"]["partitions"] == month_span("2026-01", "2026-12")


def test_terminated_subject_without_grants_is_noop():
    form = _by_event(_forms())["EV-003"]
    assert form["decision"] == "noop" and form["frozen"]["kind"] == "none"


def test_unknown_alias_upgrades_to_principal_freeze():
    form = _by_event(_forms())["EV-004"]
    assert form["decision"] == "principal"
    assert form["resource_canonical"] is None
    assert any("未登记别名" in line for line in form["basis"])


def test_alias_counts_as_same_resource_for_partition_scope():
    """只在别名资源上有失效授权时，按规范化资源收分区，不误伤其他资源。"""
    grants = [
        {"id": "A-01", "principal": "analyst", "resource": "订单",
         "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z",
         "from_partition": "2026-01", "to_partition": "2026-03", "effect": "allow"},
        {"id": "A-02", "principal": "analyst", "resource": "customers",
         "start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z",
         "from_partition": "2026-01", "to_partition": "2026-12", "effect": "allow"},
    ]
    event = normalize_events([{"event_id": "E", "principal": "analyst", "resource": "orders",
                               "effective_at": "2026-06-01T00:00:00Z"}])[0]
    form = disposition_for(event, grants, [], {}, [], True, {"run_id": "r", "manifest": {}})
    assert form["decision"] == "partitions"
    assert form["frozen"]["partitions"] == month_span("2026-01", "2026-03")


def test_cross_month_access_is_covered_by_partition_union():
    grants = [GRANTS[2]]
    event = normalize_events([{"event_id": "E", "principal": "auditor", "resource": "客户",
                               "effective_at": "2026-12-01T00:00:00Z"}])[0]
    form = disposition_for(event, grants, [], {}, [], True, {"run_id": "r", "manifest": {}})
    assert "2026-11" in form["frozen"]["partitions"]


def test_unexplained_difference_blocks_restore():
    form = _by_event(_forms())["EV-002"]
    assert form["restore"]["allowed"] is True
    explained = {eid: "已核实" for eid in form["evidence"]["differences"]}
    form2 = disposition_for(
        normalize_events([{"event_id": "EV-002", "principal": "auditor", "resource": "客户",
                           "effective_at": "2026-12-01T00:00:00Z"}])[0],
        GRANTS, calculate(load_sample("dirty.csv"))["stages"][6]["rows"], explained, [], True,
        {"run_id": "r", "manifest": {}})
    assert form2["restore"]["allowed"] is True
    assert form2["evidence"]["unexplained"] == []


def test_reviewed_partitions_restore_only_authorized_subset():
    """主体冻结下按分区放行：只有已核实且仍在授权窗内的分区可恢复。"""
    grants = [
        # 事件时刻仍生效
        {"id": "L-01", "principal": "analyst", "resource": "orders",
         "start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z",
         "from_partition": "2026-10", "to_partition": "2026-12", "effect": "allow"},
        # 早已失效，冻结但不可恢复
        {"id": "L-02", "principal": "analyst", "resource": "orders",
         "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z",
         "from_partition": "2026-01", "to_partition": "2026-04", "effect": "allow"},
    ]
    event = normalize_events([{"event_id": "E", "principal": "analyst", "resource": "orders",
                               "effective_at": "2026-11-18T00:00:00Z"}])[0]
    form = disposition_for(event, grants, [], {}, [], True, {"run_id": "r", "manifest": {}})
    assert form["decision"] == "principal"
    assert form["frozen"]["partitions"] == ["2026-01", "2026-02", "2026-03", "2026-10", "2026-11"]
    assert [item["partition"] for item in form["pending"]] == ["2026-10", "2026-11"]
    assert form["restore"]["allowed"] is False
    assert any("均未核实" in item for item in form["restore"]["blocked_by"])

    reviewed = disposition_for(event, grants, [], {}, ["2026-10", "2026-11"], True,
                               {"run_id": "r", "manifest": {}})
    assert reviewed["restore"]["allowed"] is True
    assert reviewed["restore"]["partitions"] == ["2026-10", "2026-11"]
    assert set(reviewed["restore"]["partitions"]) < set(reviewed["frozen"]["partitions"])


def test_unbalanced_blocks_all_restore():
    rows = calculate(load_sample("dirty.csv"))["stages"][6]["rows"]
    event = normalize_events([{"event_id": "EV-Y", "principal": "auditor", "resource": "客户",
                               "effective_at": "2026-12-01T00:00:00Z"}])[0]
    form = disposition_for(event, GRANTS, rows, {}, [], False, {"run_id": "r", "manifest": {}})
    assert form["decision"] == "partitions"
    assert form["frozen"]["partitions"]
    assert form["restore"]["allowed"] is False
    assert any("未对平" in item for item in form["restore"]["blocked_by"])


def test_noop_needs_no_restore():
    rows = calculate(load_sample("dirty.csv"))["stages"][6]["rows"]
    event = normalize_events([{"event_id": "EV-Z", "principal": "guest", "resource": "orders",
                               "effective_at": "2026-11-18T00:00:00Z"}])[0]
    form = disposition_for(event, GRANTS, rows, {}, [], False, {"run_id": "r", "manifest": {}})
    assert form["decision"] == "noop"
    assert form["restore"]["partitions"] == [] and form["restore"]["allowed"] is False


def test_scope_conflict_detects_post_termination_access():
    run = calculate(load_sample("dirty.csv"))
    forms = build_for_run(run)
    # 事件时刻之前的事件被放行不算冲突
    assert scope_conflicts(forms, run) == []
    rows = run["stages"][6]["rows"]
    leaked = {**rows[0], "event_id": "99999", "principal": "auditor", "partition": "2026-05",
              "observed": "allowed", "at": "2026-12-05T00:00:00+00:00"}
    run2 = {**run, "stages": run["stages"][:6] + [{"rows": rows + [leaked]}]}
    hits = scope_conflicts(forms, run2)
    assert any(hit["access_event_id"] == "99999" for hit in hits)


def test_explained_post_termination_access_not_conflict():
    run = calculate(load_sample("dirty.csv"))
    forms = build_for_run(run)
    rows = run["stages"][6]["rows"]
    leaked = {**rows[0], "event_id": "88888", "principal": "auditor", "partition": "2026-05",
              "observed": "allowed", "at": "2026-12-05T00:00:00+00:00"}
    run2 = calculate(load_sample("dirty.csv"), {"explanations": {"88888": "补录已核实"}})
    run2 = {**run2, "stages": run2["stages"][:6] + [{"rows": run2["stages"][6]["rows"] + [leaked]}]}
    assert scope_conflicts(forms, run2) == []


def test_same_event_retry_reuses_version(client):
    first = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    again = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    assert first["run_id"] == again["run_id"]
    assert [f["version"] for f in again["dispositions"]] == [f["version"] for f in first["dispositions"]]
    assert len(store.history()) == 1


def test_unrelated_input_change_does_not_bump_version(client):
    """批次变化但处置证据不变时，同事件不重复生成版本。"""
    first = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    raw = load_sample("dirty.csv").decode("utf-8-sig")
    extra = raw.rstrip("\n") + "\n00999,ops,orders,2026-05,2026-11-16T00:00:00Z,blocked,0,无关主体\n"
    second = client.post("/api/runs", json={"csv": extra}).get_json()
    assert second["run_id"] != first["run_id"]
    assert [f["version"] for f in second["dispositions"]] == [f["version"] for f in first["dispositions"]]


def test_failed_run_keeps_old_versions_and_evidence(client):
    first = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    before = client.get("/api/dispositions/EV-001/history").get_json()["versions"]
    assert client.post("/api/runs", json={"csv": "bad"}).status_code == 422
    after = client.get("/api/dispositions/EV-001/history").get_json()["versions"]
    assert [item["version"] for item in after] == [item["version"] for item in before]
    assert len(store.history()) == 1
    assert first["archive"]["ready"] is True


def test_conclusion_change_appends_version_and_keeps_old(client):
    first = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    old = {f["event_id"]: f for f in first["dispositions"]}
    second = client.post("/api/runs", json={"sample": "dirty.csv",
                                            "parameters": {"explanations": {"00001": "已核实"}}}).get_json()
    new = {f["event_id"]: f for f in second["dispositions"]}
    assert new["EV-001"]["version"] == old["EV-001"]["version"] + 1
    history = client.get("/api/dispositions/EV-001/history").get_json()["versions"]
    assert [item["version"] for item in history] == [1, 2]
    assert history[0]["form"]["evidence"]["explanations"] == {}
    old_view = client.get("/api/runs/" + first["run_id"]).get_json()
    superseded = {f["event_id"]: f for f in old_view["dispositions"]}
    assert superseded["EV-001"]["superseded"] is True
    assert superseded["EV-001"]["version"] == 1


def test_reconciliation_change_flips_restore_and_appends_version(client):
    """对账证据变化会让处置单同步改口：原本可恢复的处置单被未解释差异阻断。"""
    clean = load_sample("clean.csv").decode("utf-8-sig")
    restless = clean.replace("00003,auditor,客户,2026-05,2026-11-16T00:00:00Z,allowed,1",
                             "00003,auditor,客户,2026-05,2026-11-16T00:00:00Z,blocked,0")
    assert restless != clean
    first = client.post("/api/runs", json={"sample": "clean.csv"}).get_json()
    before = {f["event_id"]: f for f in first["dispositions"]}["EV-002"]
    assert before["restore"]["allowed"] is True
    second = client.post("/api/runs", json={"csv": restless}).get_json()
    after = {f["event_id"]: f for f in second["dispositions"]}["EV-002"]
    assert after["restore"]["allowed"] is False
    assert "00003" in after["evidence"]["differences"]
    assert "00003" in after["evidence"]["unexplained"]
    assert after["version"] == before["version"] + 1
    history = client.get("/api/dispositions/EV-002/history").get_json()["versions"]
    assert [item["version"] for item in history] == [1, 2]


def test_archive_export_carries_same_disposition(client):
    import csv, io
    payload = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    assert payload["archive"]["ready"] is True
    response = client.get("/api/runs/" + payload["run_id"] + "/archive")
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    forms = [row for row in rows[1:] if row[3] == "disposition"]
    assert len(forms) == len(payload["dispositions"])
    assert {row[5] for row in forms} == {form["decision"] for form in payload["dispositions"]}


def test_archive_blocked_when_scope_conflict(client):
    """冻结范围内、事件时刻之后的放行未解释时，归档与处置单结论不一致，拒绝导出。"""
    csv = ("event_id,principal,resource,partition,access_time,observed,reported,note\n"
           "00001,analyst,orders,2026-05,2026-12-05T00:00:00Z,allowed,1,终止后放行\n"
           "00002,auditor,customers,2026-05,2026-11-16T00:00:00Z,allowed,1,正常\n")
    payload = client.post("/api/runs", json={"csv": csv}).get_json()
    assert payload["archive"]["ready"] is False
    assert payload["archive"]["scope_conflicts"]
    response = client.get("/api/runs/" + payload["run_id"] + "/archive")
    assert response.status_code == 409
    assert response.get_json()["code"] == "ARCHIVE_BLOCKED"


def test_duplicate_event_id_rejected():
    with pytest.raises(DomainError):
        normalize_events([{"event_id": "E", "principal": "a", "resource": "orders",
                           "effective_at": "2026-11-18T00:00:00Z"}] * 2)


def test_event_requires_offset():
    with pytest.raises(DomainError):
        normalize_events([{"event_id": "E", "principal": "a", "resource": "orders",
                           "effective_at": "2026-11-18T00:00:00"}])


def test_bad_partition_span_rejected():
    with pytest.raises(DomainError):
        month_span("2026-01", "2026-01")


def test_disposition_api_and_page(client):
    assert client.get("/api/dispositions").status_code == 200
    assert client.get("/dispositions").status_code == 200
    assert client.get("/api/dispositions/NOPE/history").status_code == 404


def test_disposition_api_for_stored_run(client):
    payload = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    response = client.post("/api/dispositions", json={"run_id": payload["run_id"]}).get_json()
    assert len(response["rows"]) == len(payload["dispositions"])
    assert response["metrics"]["events"] == len(payload["dispositions"])
