import time
from app.config import ROOT,MODULES
from app.errors import DomainError
from app.values import scalar
from app.manifest import digest,manifest
from app import store
from app.modules.m1_accesses.rules import evaluate as stage1
from app.modules.m2_resources.rules import evaluate as stage2
from app.modules.m3_grants.rules import evaluate as stage3
from app.modules.m4_conflicts.rules import evaluate as stage4
from app.modules.m5_partitions.rules import evaluate as stage5
from app.modules.m6_audit.rules import evaluate as stage6
from app.modules.m7_seals.rules import evaluate as stage7
from app.modules.m8_dispositions.rules import build_for_run,scope_conflicts
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]


def load_sample(name):
    if name not in {"dirty.csv","clean.csv","legacy.csv"}: raise DomainError("BAD_SAMPLE","样例不存在",404)
    return (ROOT/"sample_data"/name).read_bytes()


def calculate(raw,params=None,stop=6):
    params=params or {};started=time.monotonic();context={"parameters":params,"raw":raw,"quarantine":[]};stages=[];rows=[]
    for index,stage in enumerate(STAGES[:stop+1]):
        result=stage(raw if index==0 else rows,context)
        result=scalar(result);rows=result["rows"]
        context["quarantine"].extend(result.get("quarantine",[]))
        if index==0: context["intake"]=result["metrics"]
        result["cumulative_quarantine"]=list(context["quarantine"])
        context[MODULES[index][0]]=result
        stages.append(result)
    return {"run_id":digest(raw,params)[:20],"stages":stages,"manifest":manifest(raw,params,started),"parameters":params}


def disposition_stage(run):
    """在封存批次上生成终止事件处置单，并校验归档结论一致。"""
    forms=build_for_run(run)
    conflicts=scope_conflicts(forms,run)
    return {"rows":forms,"quarantine":[],"metrics":_disposition_metrics(forms,conflicts)}


def archive_verdict(result,forms,conflicts):
    """归档结论与处置单同源：冻结、恢复、待核及依据。

    归档只封存证据：行数未对平、或冻结范围内存在未解释放行时才拒绝归档。
    未解释差异本身不阻止封存，但已在处置单中阻断恢复，并随证据留档。
    """
    blocked=[]
    balanced=all(stage["metrics"].get("balanced",True) for stage in result["stages"]
                 if "balanced" in stage["metrics"])
    if not balanced:
        blocked.append("来源行未对平")
    if conflicts:
        blocked.append("冻结范围内存在事件时刻之后的放行记录且未解释：%s"
                       % "、".join("%s/%s"%(c["event_id"],c["access_event_id"]) for c in conflicts))
    return {"run_id":result["run_id"],"input_sha256":result["manifest"]["input_sha256"],
            "rule_version":result["manifest"]["rule_version"],
            "ready":not blocked,"blocked_by":blocked,
            "dispositions":[{"event_id":f["event_id"],"version":f.get("version"),"decision":f["decision"],
                             "frozen":f["frozen"],"restore":f["restore"],"pending":f["pending"],
                             "basis":f["basis"],"evidence_digest":f["evidence_digest"],
                             "unexplained":f["evidence"]["unexplained"]} for f in forms],
            "scope_conflicts":conflicts}


def execute(raw,params=None):
    result=calculate(raw,params)
    stage=disposition_stage(result)
    conflicts=stage["metrics"]["scope_conflicts"]
    result["dispositions"]=stage["rows"]
    result["disposition_metrics"]=stage["metrics"]
    result["scope_conflicts"]=stage["metrics"].get("conflict_rows",[])
    result["archive"]=archive_verdict(result,stage["rows"],result["scope_conflicts"])
    saved=store.save(result["run_id"],result)
    return saved


def stage_preview(index,payload):
    if "rows" in payload and index>0:
        if not isinstance(payload["rows"],list) or any(not isinstance(row,dict) for row in payload["rows"]): raise DomainError("BAD_ROWS","记录需要对象列表")
        if not isinstance(payload.get("context",{}),dict): raise DomainError("BAD_CONTEXT","上下文需要对象")
        return scalar(STAGES[index](payload["rows"],payload.get("context",{"parameters":{},"quarantine":[]})))
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    return calculate(raw,payload.get("parameters",{}),index)["stages"][-1]


def disposition_preview(payload):
    """处置单直接基于完整批次计算，可按样例、CSV 或已存批次查看。"""
    if not isinstance(payload,dict): raise DomainError("BAD_BODY","请求需要对象")
    run_id=payload.get("run_id")
    if run_id:
        run=store.get(run_id)
        forms=run.get("dispositions")
        conflicts=run.get("scope_conflicts")
        if forms is None:
            stage=disposition_stage(run); forms=stage["rows"]; conflicts=stage["metrics"]["conflict_rows"]
        return {"run_id":run_id,"rows":forms,"metrics":_disposition_metrics(forms,conflicts or []),
                "manifest":run["manifest"],"version":run.get("version")}
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    run=calculate(raw,payload.get("parameters",{}))
    stage=disposition_stage(run)
    return {"run_id":run["run_id"],"rows":stage["rows"],"metrics":stage["metrics"],
            "manifest":run["manifest"]}


def _disposition_metrics(forms,conflicts=None):
    actionable=[f for f in forms if f["frozen"]["kind"]!="none"]
    conflicts=conflicts or []
    return {"events":len(forms),
            "freeze_principal":sum(f["decision"]=="principal" for f in forms),
            "freeze_partitions":sum(f["decision"]=="partitions" for f in forms),
            "noop":sum(f["decision"]=="noop" for f in forms),
            "pending_partitions":sum(len(f["pending"]) for f in forms),
            "frozen_partitions":sum(len(f["frozen"]["partitions"]) for f in actionable),
            "restorable":sum(f["restore"]["allowed"] for f in actionable),
            "scope_conflicts":len(conflicts),
            "conflict_rows":conflicts}
