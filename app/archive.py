import csv,io,json


def _payload(form):
    evidence=form.get("evidence") or {}
    return {"version":form.get("version"),"decision":form["decision"],
            "frozen":form["frozen"],"restore":form["restore"],"pending":form["pending"],
            "basis":form["basis"],"evidence_digest":form.get("evidence_digest"),
            "conflicts":evidence.get("conflicts",[]),"violations":evidence.get("violations",[]),
            "differences":evidence.get("differences",[]),
            "unexplained":evidence.get("unexplained",[]),"balanced":evidence.get("balanced")}


def build_archive(run,current=None):
    """导出唯一证据CSV。

    原始行与隔离行照旧逐行留档；每张处置单写本批次的原始结论，
    若该结论已被新版本取代，再追加一行“当前结论”，
    保证归档与处置单同一结论，同时不覆盖旧版和原始证据。
    """
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence"])
    meta=run["manifest"]
    for row in run["stages"][-1]["rows"]:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False)])
    current=current or {}
    for form in run.get("dispositions") or []:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"disposition",
                         json.dumps([],ensure_ascii=False),form["decision"],
                         json.dumps(_payload(form),ensure_ascii=False)])
        latest=current.get(form["event_id"])
        if form.get("superseded") and latest:
            evidence=_payload(latest)
            evidence["supersedes"]={"version":form.get("version"),"run_id":run["run_id"],
                                    "evidence_digest":form.get("evidence_digest")}
            evidence["source_run"]=latest.get("run_id")
            writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"disposition_current",
                             json.dumps([],ensure_ascii=False),latest["decision"],
                             json.dumps(evidence,ensure_ascii=False)])
    return "﻿"+stream.getvalue()
