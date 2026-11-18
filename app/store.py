import sqlite3, json
from app.config import DATA_DIR
from app.errors import DomainError


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核')")
    db.execute("""CREATE TABLE IF NOT EXISTS dispositions (
        event_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        digest TEXT NOT NULL,
        run_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (event_id,version))""")
    return db


def _assign_versions(db, forms, run_id):
    """为处置单分配版本。

    同一事件、同一证据摘要视为重试，复用原版本，不重复生成；
    结论输入（冲突、对账、事件、解释）变化时摘要变化，追加新版本。
    历史版本与原始证据永不覆盖。
    """
    assigned = []
    for form in forms:
        digest = form["evidence_digest"]
        row = db.execute(
            "SELECT version,digest FROM dispositions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (form["event_id"],)).fetchone()
        if row and row[1] == digest:
            version = row[0]
        else:
            version = (row[0] + 1) if row else 1
            while True:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO dispositions(event_id,version,digest,run_id,payload) VALUES (?,?,?,?,?)",
                    (form["event_id"], version, digest, run_id, json.dumps(form, ensure_ascii=False)))
                if cursor.rowcount == 1:
                    break
                existing = db.execute("SELECT digest FROM dispositions WHERE event_id=? AND version=?",
                                      (form["event_id"], version)).fetchone()
                if existing and existing[0] == digest:
                    break
                version += 1
        assigned.append({**form, "version": version})
    return assigned


def _latest_map(db, event_ids):
    latest = {}
    for event_id in event_ids:
        row = db.execute(
            "SELECT version,digest,run_id,payload,created_at FROM dispositions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (event_id,)).fetchone()
        if row:
            latest[event_id] = {**json.loads(row[3]), "version": row[0], "digest": row[1],
                                "run_id": row[2], "issued_at": row[4]}
    return latest


def save(run_id, payload):
    with connect() as db:
        existing = db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
        if not existing:
            forms = payload.get("dispositions") or []
            assigned = _assign_versions(db, forms, run_id)
            payload = {**payload, "dispositions": assigned}
            archive = payload.get("archive")
            if isinstance(archive, dict):
                by_event = {form["event_id"]: form for form in assigned}
                archive["dispositions"] = [
                    {**item, "version": by_event.get(item["event_id"], {}).get("version")}
                    for item in archive.get("dispositions", [])]
            db.execute("INSERT INTO runs(id,payload) VALUES (?,?)",
                       (run_id, json.dumps(payload, ensure_ascii=False)))
    return get(run_id)


def get(run_id):
    with connect() as db:
        row = db.execute("SELECT payload,version,decision FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        payload = json.loads(row[0])
        forms = payload.get("dispositions") or []
        for form in forms:
            latest = db.execute(
                "SELECT version,digest FROM dispositions WHERE event_id=? ORDER BY version DESC LIMIT 1",
                (form["event_id"],)).fetchone()
            if latest:
                form["superseded"] = latest[1] != form["evidence_digest"]
                form["latest_version"] = latest[0]
            else:
                form["superseded"] = False
                form["latest_version"] = form.get("version", 1)
    return {**payload, "version": row[1], "decision": row[2]}


def latest():
    with connect() as db:
        row = db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None


def history():
    with connect() as db:
        return [{"run_id": i, "version": v, "decision": d}
                for i, v, d in db.execute("SELECT id,version,decision FROM runs ORDER BY rowid DESC")]


def disposition_history(event_id):
    with connect() as db:
        rows = db.execute(
            "SELECT version,digest,run_id,payload,created_at FROM dispositions WHERE event_id=? ORDER BY version",
            (event_id,)).fetchall()
    return [{"event_id": event_id, "version": v, "digest": d, "run_id": r,
             "issued_at": c, "form": json.loads(p)} for v, d, r, p, c in rows]


def latest_dispositions(event_ids=None):
    with connect() as db:
        return _latest_map(db, event_ids or [])


def review(run_id, version, decision):
    if decision not in {"已复核", "退回复核"}:
        raise DomainError("BAD_DECISION", "复核状态无效")
    with connect() as db:
        cursor = db.execute("UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?",
                            (decision, run_id, version))
        if cursor.rowcount != 1:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise DomainError("NOT_FOUND", "批次不存在", 404)
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get(run_id)
