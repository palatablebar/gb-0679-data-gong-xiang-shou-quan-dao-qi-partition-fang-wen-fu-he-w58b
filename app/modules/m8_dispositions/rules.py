"""终止事件的处置单规则。

冻结范围只有两种取舍：

* 停用整个主体：立即控险，但会中断仍在授权窗内的作业；
* 只收失效分区：误伤更少，但只有“主体全部授权均已失效、资源别名可规范化、
  分区可按授权窗穷举”三者同时成立时才是完备的。

因此本模块先证明“只收失效分区”完备；证明不了就升级为停用整个主体，
把无法判定的分区列入待核。处置单同时给出冻结、恢复、待核及依据，
审计归档复用同一结论（见 archived_conflicts 与 app.archive）。
"""
import hashlib
import json

from app.values import instant
from app.errors import DomainError
from app.modules.m2_resources.rules import RESOURCES
from app.modules.m3_grants.rules import GRANTS, validate_grants

POLICY_VERSION = "2026.11.18-r1"
MAX_MONTHS = 240

DECISION_LABELS = {
    "principal": "停用整个主体",
    "partitions": "只收失效分区",
    "noop": "无需处置",
}

DEFAULT_TERMINATIONS = [
    {"event_id": "EV-001", "principal": "analyst", "resource": "订单",
     "effective_at": "2026-11-18T00:00:00Z", "note": "订单共享终止，月中到达"},
    {"event_id": "EV-002", "principal": "auditor", "resource": "客户",
     "effective_at": "2026-12-01T00:00:00Z", "note": "客户共享到期，分区窗末端对齐"},
    {"event_id": "EV-003", "principal": "guest", "resource": "orders",
     "effective_at": "2026-11-18T00:00:00Z", "note": "主体无任何授权"},
    {"event_id": "EV-004", "principal": "analyst", "resource": "订单明细",
     "effective_at": "2026-11-18T00:00:00Z", "note": "资源别名未登记"},
]


def _month_index(value):
    if (not isinstance(value, str) or len(value) != 7 or value[4] != "-"
            or not value[:4].isdigit() or not value[5:].isdigit()):
        raise DomainError("BAD_PARTITION", "分区必须为YYYY-MM")
    month = int(value[5:])
    if not 1 <= month <= 12:
        raise DomainError("BAD_PARTITION", "分区月份必须在01-12")
    return int(value[:4]) * 12 + month - 1


def month_span(start, end):
    """[start,end) 的月份列表；与授权窗一致，不含 end 月。"""
    first, last = _month_index(start), _month_index(end)
    if first >= last:
        raise DomainError("BAD_GRANT", "授权分区窗无效")
    if last - first > MAX_MONTHS:
        raise DomainError("BAD_GRANT", "授权分区窗过大")
    return ["%04d-%02d" % (index // 12, index % 12 + 1) for index in range(first, last)]


def canonical_resource(value):
    return RESOURCES.get(value)


def normalize_event(raw):
    if not isinstance(raw, dict):
        raise DomainError("BAD_EVENT", "处置事件需要对象")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise DomainError("BAD_EVENT", "处置事件需要事件号")
    principal = raw.get("principal")
    if not isinstance(principal, str) or not principal.strip():
        raise DomainError("BAD_EVENT", "处置事件需要主体")
    resource = raw.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise DomainError("BAD_EVENT", "处置事件需要资源")
    effective_at = instant(raw.get("effective_at"))
    note = raw.get("note") or ""
    return {
        "event_id": event_id.strip(),
        "principal": principal.strip(),
        "resource": resource.strip(),
        "resource_canonical": canonical_resource(resource.strip()),
        "effective_at": effective_at.isoformat(),
        "note": str(note),
    }


def normalize_events(raw_events):
    if not isinstance(raw_events, list):
        raise DomainError("BAD_EVENTS", "处置事件需要列表")
    events = []
    seen = set()
    for raw in raw_events:
        event = normalize_event(raw)
        if event["event_id"] in seen:
            raise DomainError("DUPLICATE_EVENT", "处置事件号重复")
        seen.add(event["event_id"])
        events.append(event)
    return events


def _grant_view(grant):
    return {
        "id": grant["id"],
        "principal": grant["principal"],
        "resource": grant["resource"],
        "resource_canonical": canonical_resource(grant["resource"]),
        "start": instant(grant["start"]).isoformat(),
        "end": instant(grant["end"]).isoformat(),
        "from_partition": grant["from_partition"],
        "to_partition": grant["to_partition"],
        "effect": grant["effect"],
        "partitions": month_span(grant["from_partition"], grant["to_partition"]),
    }


def _union_partitions(grants):
    return sorted({p for grant in grants for p in month_span(grant["from_partition"], grant["to_partition"])})


def _explained(explanations, event_id):
    value = explanations.get(str(event_id))
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _relevant_rows(rows, principal):
    return [row for row in rows if row.get("principal") == principal]


def _evidence(rows, principal, explanations):
    relevant = _relevant_rows(rows, principal)
    conflicts = sorted({str(row.get("event_id")) for row in relevant if row.get("conflict")})
    violations = sorted({str(row.get("event_id")) for row in relevant if row.get("violation") == "越权放行"})
    differences = sorted({str(row.get("event_id")) for row in relevant if row.get("status") == "差异"})
    touched = set(violations) | set(differences) | set(conflicts)
    scoped = {key: value for key, value in explanations.items() if str(key) in touched}
    unexplained = sorted({eid for eid in set(violations) | set(differences)
                          if not _explained(explanations, eid)})
    return {
        "conflicts": conflicts,
        "violations": violations,
        "differences": differences,
        "unexplained": unexplained,
        "explanations": {str(k): str(v) for k, v in sorted(scoped.items())},
    }


def _pending(partitions, reason, reviewed):
    reviewed_set = set(reviewed or [])
    return [{"partition": partition, "reason": reason,
             "reviewed": partition in reviewed_set} for partition in partitions]


def _restore(decision, frozen, pending, restorable, blockers):
    conditions = [
        "确认事件时刻之后冻结分区没有放行记录",
        "逐条解释越权放行与对账差异并完成复核",
        "待核分区全部核实后方可解除主体冻结",
        "冲突授权先修正授权版本再恢复",
    ]
    if decision == "noop":
        return {"scope": "不适用（无需冻结）", "partitions": [], "allowed": False,
                "blocked_by": blockers, "conditions": conditions}
    if decision == "principal":
        return {"scope": "先恢复仍在授权内且已核实的待核分区，其余分区保持冻结",
                "partitions": restorable, "allowed": not blockers,
                "blocked_by": blockers, "conditions": conditions}
    return {"scope": "恢复冻结分区", "partitions": list(frozen["partitions"]),
            "allowed": not blockers, "blocked_by": blockers, "conditions": conditions}


def disposition_for(event, grants, rows, explanations, reviewed, balanced, run):
    principal = event["principal"]
    effective = instant(event["effective_at"])
    principal_grants = [g for g in grants if g["principal"] == principal]
    views = [_grant_view(g) for g in principal_grants]
    unresolved = [g for g in views if g["resource_canonical"] is None]
    canon = event["resource_canonical"]

    evidence = _evidence(rows, principal, explanations)
    evidence.update({
        "run_id": run.get("run_id"),
        "input_sha256": (run.get("manifest") or {}).get("input_sha256"),
        "rule_version": (run.get("manifest") or {}).get("rule_version"),
        "policy_version": POLICY_VERSION,
        "balanced": balanced,
        "grants": views,
        "reviewed_partitions": sorted(reviewed or []),
    })

    if not views:
        decision = "noop"
        frozen = {"kind": "none", "scope": "无", "partitions": []}
        pending = []
        basis = ["主体在授权清单中没有任何授权，默认拒绝已生效，无需冻结分区。"]
        if evidence["violations"]:
            basis.append("但该主体已有越权放行 %s，需在访问对账中单独处置，本单不重复冻结。"
                         % "、".join(evidence["violations"][:8]))
    else:
        subject_live = [g for g in views
                        if instant(g["start"]) <= effective < instant(g["end"])
                        or instant(g["start"]) > effective]
        unresolved_live = [g for g in subject_live if g["resource_canonical"] is None]
        in_scope = [g for g in views if canon is not None and g["resource_canonical"] == canon]
        live_in_scope = [g for g in in_scope if g in subject_live]

        if (canon is None and subject_live) or live_in_scope or unresolved_live:
            decision = "principal"
            pending_grants = subject_live if canon is None else live_in_scope + unresolved_live
            affected = sorted({g["id"] for g in pending_grants})
            reason = "授权在事件时刻仍生效，只收失效分区会中断授权内作业或漏掉跨月访问"
            pending = _pending(_union_partitions(pending_grants), reason, reviewed)
            frozen = {"kind": "principal", "scope": "整个主体：全部资源与全部分区",
                      "partitions": _union_partitions(views)}
            basis = [
                "事件要求终止主体 %s 的共享，但授权 %s 在 %s 仍生效。"
                % (principal, " / ".join(affected) or "（别名未登记）", event["effective_at"]),
                "授权分区窗与访问时刻解耦，同一分区可在后续月份被访问；"
                "只按事件资源收失效分区无法覆盖别名与跨月访问。",
                "因此停用整个主体控险，受影响分区列入待核，核实前不恢复。",
            ]
            if canon is None:
                basis.append("事件资源「%s」未登记别名，无法确定终止范围，按最保守的主体冻结处理。"
                             % event["resource"])
            elif unresolved_live:
                basis.append("授权清单中存在无法规范化的资源别名且仍生效，分区枚举不完整，必须按主体冻结。")
        else:
            # 主体的全部授权均已失效：可以证明分区枚举完备。
            if canon is not None and not in_scope and not unresolved:
                decision = "noop"
                frozen = {"kind": "none", "scope": "无", "partitions": []}
                pending = []
                basis = [
                    "主体在该事件资源上没有授权，其他资源的授权不在本事件范围内。",
                    "默认拒绝已生效，冻结或恢复都不会改变访问结果。",
                ]
            else:
                decision = "partitions"
                if canon is None or unresolved:
                    target = views
                    basis = [
                        "主体全部授权均已失效，按主体合并全部分区窗作为冻结集，"
                        "别名与跨月访问均已被覆盖。",
                        "存在未登记资源别名，无法按事件资源缩小范围，因此取全部分区并集。",
                    ]
                else:
                    target = in_scope
                    basis = [
                        "主体在事件资源上的授权 %s 均已失效，只收这些分区即可完备覆盖，"
                        "不会中断其他资源的授权内作业。"
                        % " / ".join(g["id"] for g in in_scope),
                        "授权按主体与规范化资源枚举，别名已归一；"
                        "访问时刻与分区月解耦，冻结集按分区窗并集覆盖跨月访问。",
                    ]
                frozen = {"kind": "partitions", "scope": "事件资源的下述失效分区",
                          "partitions": _union_partitions(target)}
                pending = []

    restorable = []
    unresolved_name = decision == "principal" and (canon is None or bool(unresolved_live))
    if decision == "principal" and not unresolved_name:
        authorized = set(_union_partitions(subject_live))
        restorable = [item["partition"] for item in pending
                      if item["reviewed"] and item["partition"] in authorized]

    blockers = []
    if decision != "noop" and not balanced:
        blockers.append("来源行未对平，全部处置单暂停恢复")
    if decision != "noop" and evidence["unexplained"]:
        shown = "、".join(evidence["unexplained"][:8])
        blockers.append("存在未解释差异：%s%s" % (shown, " 等" if len(evidence["unexplained"]) > 8 else ""))
    if decision == "principal":
        if unresolved_name:
            blockers.append("事件资源或授权别名未登记，无法判定可恢复分区")
            basis.append("恢复前须先登记别名，否则不得解除主体冻结。")
        elif restorable:
            basis.append("恢复按分区放行：已核实且仍在授权窗内的分区可解除冻结，"
                         "其余分区保持冻结直至授权版本修正。")
        else:
            blockers.append("待核分区均未核实，暂不恢复任何分区")
            basis.append("恢复前须逐分区核实；核实前整个主体保持冻结。")
    elif decision == "partitions" and not balanced:
        basis.append("来源行未对平，本处置单只冻结、不恢复。")

    restore = _restore(decision, frozen, pending, restorable, blockers)
    form = {
        "event_id": event["event_id"],
        "principal": principal,
        "resource": event["resource"],
        "resource_canonical": canon,
        "effective_at": event["effective_at"],
        "note": event["note"],
        "decision": decision,
        "decision_label": DECISION_LABELS[decision],
        "frozen": frozen,
        "restore": restore,
        "pending": pending,
        "basis": basis,
        "evidence": evidence,
    }
    form["evidence_digest"] = _digest(form)
    return form


def _digest(form):
    """版本摘要只覆盖“结论 + 冲突/对账证据”。

    同一输入重跑时摘要不变，复用版本；冲突或对账证据变化时摘要变化，
    追加新版本。批次号与输入摘要只随版本留档，不参与摘要，
    避免无关行变化把同一结论误判成新结论。
    """
    evidence = form["evidence"]
    core = {
        "event_id": form["event_id"],
        "principal": form["principal"],
        "resource": form["resource"],
        "resource_canonical": form["resource_canonical"],
        "effective_at": form["effective_at"],
        "decision": form["decision"],
        "frozen": form["frozen"],
        "restore": form["restore"],
        "pending": form["pending"],
        "basis": form["basis"],
        "reconcile": {
            "balanced": evidence["balanced"],
            "conflicts": evidence["conflicts"],
            "violations": evidence["violations"],
            "differences": evidence["differences"],
            "unexplained": evidence["unexplained"],
            "explanations": evidence["explanations"],
            "reviewed_partitions": evidence["reviewed_partitions"],
        },
    }
    encoded = json.dumps(core, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_for_run(run):
    stages = run.get("stages") or []
    if len(stages) < 7:
        raise DomainError("BAD_RUN", "批次尚未走完七个阶段")
    rows = stages[6]["rows"]
    audit = stages[5]["metrics"]
    params = run.get("parameters") or {}
    grants = validate_grants(params.get("grants") or GRANTS)
    events = normalize_events(params.get("terminations") or DEFAULT_TERMINATIONS)
    explanations = params.get("explanations") or {}
    if not isinstance(explanations, dict):
        raise DomainError("BAD_EXPLANATIONS", "差异解释需要事件号到说明的对象")
    reviewed = params.get("reviewed_partitions") or {}
    if not isinstance(reviewed, dict):
        raise DomainError("BAD_REVIEWS", "待核分区核实记录需要对象")
    balanced = bool(audit.get("balanced", True))
    return [disposition_for(event, grants, rows, explanations,
                            reviewed.get(event["event_id"]) or [], balanced, run)
            for event in events]


def scope_conflicts(dispositions, run):
    """返回与冻结结论矛盾的封存行：冻结范围内、事件时刻之后仍被放行。"""
    stages = run.get("stages") or []
    if len(stages) < 7:
        return []
    rows = stages[6]["rows"]
    params = run.get("parameters") or {}
    explanations = params.get("explanations") or {}
    hits = []
    for form in dispositions:
        if form["frozen"]["kind"] == "none":
            continue
        effective = instant(form["effective_at"])
        for row in rows:
            if row.get("principal") != form["principal"] or row.get("observed") != "allowed":
                continue
            at = row.get("at")
            if not at or instant(at) < effective:
                continue
            if (form["frozen"]["kind"] == "partitions"
                    and row.get("partition") not in form["frozen"]["partitions"]):
                continue
            if _explained(explanations, row.get("event_id")):
                continue
            hits.append({"event_id": form["event_id"], "access_event_id": str(row.get("event_id")),
                         "partition": row.get("partition"), "access_time": at})
    return hits
