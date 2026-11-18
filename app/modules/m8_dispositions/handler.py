from flask import Blueprint, request, jsonify
from app.errors import DomainError
from app.pipeline import disposition_preview
bp = Blueprint("dispositions", __name__)


@bp.route("/api/dispositions", methods=["GET", "POST"])
def preview():
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    if payload is None or not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    return jsonify(disposition_preview(payload))


@bp.route("/api/dispositions/<event_id>/history")
def history(event_id):
    from app import store
    records = store.disposition_history(event_id)
    if not records:
        raise DomainError("NOT_FOUND", "处置事件不存在", 404)
    return jsonify({"event_id": event_id, "versions": records})
