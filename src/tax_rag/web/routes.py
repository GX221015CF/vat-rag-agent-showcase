from __future__ import annotations

import hmac
import json
import re
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from tax_rag.domain.calculation import CalculationInputError
from tax_rag.domain.matter_state import (
    DuplicateSubmissionError,
    MatterStateError,
    MatterStatus,
)
from tax_rag.services.calculation_service import calculation_row_to_dict
from tax_rag.web.answer_view import build_answer_views


bp = Blueprint("main", __name__)
SUBMISSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


def browser_consent_current():
    browser_id = session.get("browser_id", "")
    return bool(browser_id) and services()[0].browser_consent_current(browser_id, current_app.config["NOTICE_VERSION"])


def accept_browser_consent():
    session.setdefault("browser_id", secrets.token_urlsafe(32))
    services()[0].record_browser_consent(session["browser_id"], current_app.config["NOTICE_VERSION"])


def sync_matter_consent(matter_id):
    row = services()[0].get_matter(matter_id)
    if browser_consent_current() and row["status"] in {MatterStatus.DRAFT, MatterStatus.ACTIVE, MatterStatus.CLOSED}:
        services()[1].accept_notice(matter_id)
    return services()[0].get_matter(matter_id)


@bp.app_context_processor
def consent_context():
    return {"cloud_confirmed": browser_consent_current()}


@bp.post("/cloud-notice")
def confirm_cloud_notice():
    require_csrf()
    if request.form.get("accepted") != "yes":
        return jsonify(ok=False, message="请主动勾选云端传输确认"), 400
    matter_id = request.form.get("matter_id")
    if matter_id:
        get_matter_or_404(services()[0], matter_id)
    accept_browser_consent()
    if matter_id:
        sync_matter_consent(matter_id)
    if wants_json():
        return jsonify(ok=True, notice_version=current_app.config["NOTICE_VERSION"])
    return redirect(url_for("main.matter", matter_id=matter_id) if matter_id else url_for("main.index"))


def wants_json() -> bool:
    return request.accept_mimetypes.best == "application/json"


def submission_response(result):
    if not wants_json():
        return redirect(url_for("main.matter", matter_id=result.matter["matter_id"]))
    return jsonify(
        ok=result.error_code is None,
        url=url_for("main.matter", matter_id=result.matter["matter_id"]),
        round_number=result.matter["round_count"],
        message=result.assistant_message["content"],
        generation_id=result.generation_id,
        model_id=result.model_id,
        deepseek_status=result.deepseek_status,
        retrieval_query=result.retrieval_query,
        elapsed_seconds=result.elapsed_seconds,
        error_code=result.error_code,
        evidence=[{"evidence_id": hit.chunk_id, "title": hit.document_title, "anchor": hit.anchor,
                   "text": hit.text, "source": hit.source} for hit in result.hits],
    )


def services():
    return (
        current_app.extensions["runtime_store"],
        current_app.extensions["matter_service"],
        current_app.extensions["retrieval_service"],
        current_app.extensions["calculation_service"],
    )


def require_csrf() -> None:
    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        abort(400, "表单已失效，请刷新页面后重试")


def require_submission_token() -> str:
    token = request.form.get("submission_token", "")
    if not SUBMISSION_TOKEN_PATTERN.fullmatch(token):
        abort(400, "提交令牌无效，请刷新页面后重试")
    return token


def get_matter_or_404(store, matter_id: str):
    try:
        return store.get_matter(matter_id)
    except KeyError:
        abort(404)


@bp.get("/")
def index():
    store, _matter_service, retrieval_service, _calculation_service = services()
    try:
        generation = retrieval_service.active_generation()
    except Exception:
        generation = None
    matters = []
    draft = session.get("draft_question", {})
    for row in store.list_matters():
        item = dict(row)
        question = item["first_question"] or (
            draft.get("content", "") if draft.get("matter_id") == item["matter_id"] else ""
        )
        item["display_title"] = " ".join(question.split()) or (
            "未开始的咨询" if item["status"] == MatterStatus.DRAFT else "新咨询"
        )
        item["display_time"] = datetime.fromisoformat(item["updated_at"]).astimezone(
            timezone(timedelta(hours=8))
        ).strftime("%m-%d %H:%M")
        matters.append(item)
    return render_template(
        "index.html",
        matters=matters,
        generation=generation,
        model_id=current_app.extensions["llm_service"].model_id,
    )


@bp.post("/matters")
def create_matter():
    require_csrf()
    _store, matter_service, _retrieval_service, _calculation_service = services()
    if "accepted" in request.form and request.form.get("accepted") != "yes":
        abort(400, "请主动确认云端传输提示")
    matter = matter_service.create_matter()
    if request.form.get("accepted") == "yes":
        accept_browser_consent()
    sync_matter_consent(matter["matter_id"])
    question = request.form.get("content", "").strip()
    if question:
        session["draft_question"] = {"matter_id": matter["matter_id"], "content": question[:1000]}
    if wants_json():
        return jsonify(ok=True, url=url_for("main.matter", matter_id=matter["matter_id"]))
    return redirect(url_for("main.matter", matter_id=matter["matter_id"]))


@bp.route("/matters/<matter_id>/notice", methods=["GET", "POST"])
def notice(matter_id: str):
    store, matter_service, _retrieval_service, _calculation_service = services()
    matter = get_matter_or_404(store, matter_id)
    if request.method == "POST":
        require_csrf()
        if request.form.get("accepted") != "yes":
            flash("必须主动勾选接受后才能进入本事项。", "error")
            return render_template("notice.html", matter=matter, notice_text=current_app.config["NOTICE_TEXT"]), 400
        try:
            accept_browser_consent()
            matter_service.accept_notice(matter_id)
        except MatterStateError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.matter", matter_id=matter_id))
        return redirect(url_for("main.matter", matter_id=matter_id))
    return redirect(url_for("main.matter", matter_id=matter_id))


@bp.get("/matters/<matter_id>")
def matter(matter_id: str):
    store, _matter_service, _retrieval_service, _calculation_service = services()
    get_matter_or_404(store, matter_id)
    matter_row = sync_matter_consent(matter_id)
    hits = []
    for row in store.latest_hits(matter_id):
        hits.append(
            {
                **dict(row),
                "source": json.loads(row["source_json"]),
                "channel_ranks": json.loads(row["channel_ranks_json"]),
            }
        )
    messages = store.list_messages(matter_id)
    evidence_by_round = {}
    for message in messages:
        if message["role"] == "ASSISTANT":
            run = store.retrieval_for_round(matter_id, message["round_number"])
            evidence_by_round[message["round_number"]] = [
                {**dict(hit), "source": json.loads(hit["source_json"])}
                for hit in store.hits_for_retrieval_run(run["retrieval_run_id"])
            ] if run else []
    generation_run = store.latest_generation(matter_id)
    route = store.latest_route(matter_id)
    retry_message = None
    user_messages = [message for message in messages if message["role"] == "USER"]
    if user_messages:
        latest_user = user_messages[-1]
        if generation_run is not None and generation_run["user_message_id"] == latest_user["message_id"] and generation_run["status"] != "SUCCESS":
            retry_message = latest_user
        elif route is not None and "RETRIEVAL_UNAVAILABLE" in route["route_reason"]:
            retry_message = latest_user
    structured_answers = build_answer_views(store, matter_id)
    for answer in structured_answers.values():
        for hit in answer["cited_evidence"]:
            hit["source"] = json.loads(hit["source_json"])
    return render_template(
        "matter.html",
        matter=matter_row,
        messages=messages,
        evidence_by_round=evidence_by_round,
        retry_message=retry_message,
        structured_answers=structured_answers,
        route=route,
        hits=hits,
        calculation=calculation_row_to_dict(store.latest_calculation(matter_id)),
        generation_run=generation_run,
        model_id=current_app.extensions["llm_service"].model_id,
        notice_current=matter_row["notice_version"] == current_app.config["NOTICE_VERSION"],
        submission_token=secrets.token_urlsafe(24),
        draft_question=session.get("draft_question", {}).get("content", "") if session.get("draft_question", {}).get("matter_id") == matter_id else "",
    )


@bp.post("/matters/<matter_id>/messages")
def submit_message(matter_id: str):
    require_csrf()
    store, matter_service, _retrieval_service, _calculation_service = services()
    get_matter_or_404(store, matter_id)
    if not browser_consent_current():
        return jsonify(ok=False, consent_required=True, message="请先确认本浏览器会话的云端传输提示"), 409
    sync_matter_consent(matter_id)
    submission_token = require_submission_token()
    try:
        result = matter_service.submit(
            matter_id,
            request.form.get("content", ""),
            submission_token=submission_token,
        )
        if session.get("draft_question", {}).get("matter_id") == matter_id:
            session.pop("draft_question", None)
    except DuplicateSubmissionError as exc:
        if wants_json():
            return jsonify(ok=False, message=str(exc), url=url_for("main.matter", matter_id=matter_id)), 409
        flash(str(exc), "warning")
        return redirect(url_for("main.matter", matter_id=matter_id))
    except (MatterStateError, ValueError) as exc:
        if wants_json():
            return jsonify(ok=False, message=str(exc)), 409
        flash(str(exc), "error")
        return redirect(url_for("main.matter", matter_id=matter_id))
    if result.warning:
        flash("当前已到第7轮，下一轮后事项将关闭。", "warning")
    if result.closed:
        flash("本事项已完成第8轮，现已关闭并转为只读。", "warning")
    return submission_response(result)


@bp.post("/matters/<matter_id>/messages/<message_id>/retry")
def retry_message(matter_id: str, message_id: str):
    require_csrf()
    store, matter_service, _retrieval_service, _calculation_service = services()
    get_matter_or_404(store, matter_id)
    if not browser_consent_current():
        return jsonify(ok=False, consent_required=True, message="请先确认本浏览器会话的云端传输提示"), 409
    sync_matter_consent(matter_id)
    try:
        result = matter_service.retry(matter_id, message_id)
    except (MatterStateError, KeyError) as exc:
        if wants_json():
            return jsonify(ok=False, message=str(exc)), 409
        flash(str(exc), "error")
        return redirect(url_for("main.matter", matter_id=matter_id))
    return submission_response(result)


@bp.get("/matters/<matter_id>/evidence/<int:round_number>/<chunk_id>")
def evidence(matter_id: str, round_number: int, chunk_id: str):
    store, _matter_service, retrieval_service, _calculation_service = services()
    get_matter_or_404(store, matter_id)
    run = store.retrieval_for_round(matter_id, round_number)
    if run is None:
        abort(404)
    hit = next((row for row in store.hits_for_retrieval_run(run["retrieval_run_id"]) if row["chunk_id"] == chunk_id), None)
    if hit is None:
        abort(404)
    active_match = None
    if hasattr(retrieval_service, "connect_readonly"):
        with closing(retrieval_service.connect_readonly()) as connection:
            generation = retrieval_service.active_generation(connection)
            current = connection.execute("SELECT c.text FROM chunks c JOIN index_members i USING(chunk_id) WHERE i.generation_id=? AND c.chunk_id=?", (generation["generation_id"], chunk_id)).fetchone()
            active_match = current is not None and current["text"] == hit["text"]
    return jsonify(evidence_id=chunk_id, generation_id=run["generation_id"], document_title=hit["document_title"],
                   anchor=hit["anchor"], text=hit["text"], source=json.loads(hit["source_json"]), active_text_matches=active_match)


@bp.get("/health")
def health():
    _store, _matter_service, retrieval_service, _calculation_service = services()
    try:
        generation = dict(retrieval_service.active_generation())
    except Exception:
        return jsonify(ok=False, pipeline="vat-web-rag-v1", error="活动知识库不可用"), 503
    return jsonify(ok=True, pipeline="vat-web-rag-v1", generation=generation,
                   model=current_app.extensions["llm_service"].model_id)


def require_current_notice(matter_row) -> None:
    if matter_row["notice_version"] != current_app.config["NOTICE_VERSION"]:
        raise MatterStateError("云端传输提示已更新，请新建事项并重新确认")


@bp.post("/matters/<matter_id>/calculations/single")
def calculate_single(matter_id: str):
    require_csrf()
    store, _matter_service, _retrieval_service, calculation_service = services()
    matter_row = get_matter_or_404(store, matter_id)
    try:
        require_current_notice(matter_row)
        if request.form.get("confirmed") != "yes":
            raise CalculationInputError("请先确认金额和税率均已核实")
        calculation_service.single(
            matter_id,
            request.form.get("amount", ""),
            request.form.get("amount_type", ""),
            request.form.get("tax_rate_percent", ""),
        )
        flash("单笔Decimal计算已完成；中间值和结果哈希已保存。", "success")
    except (MatterStateError, CalculationInputError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.matter", matter_id=matter_id))


@bp.post("/matters/<matter_id>/calculations/period")
def calculate_period(matter_id: str):
    require_csrf()
    store, _matter_service, _retrieval_service, calculation_service = services()
    matter_row = get_matter_or_404(store, matter_id)
    try:
        require_current_notice(matter_row)
        if request.form.get("confirmed") != "yes":
            raise CalculationInputError("请先确认各项税额均已核实")
        calculation_service.period(
            matter_id,
            request.form.get("output_tax", ""),
            request.form.get("deductible_input_tax", ""),
            request.form.get("input_tax_transfer_out", ""),
            request.form.get("prior_credit", ""),
        )
        flash("期间汇总Decimal计算已完成；中间值和结果哈希已保存。", "success")
    except (MatterStateError, CalculationInputError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.matter", matter_id=matter_id))


@bp.get("/knowledge")
def knowledge():
    _store, _matter_service, retrieval_service, _calculation_service = services()
    overview = retrieval_service.knowledge_overview()
    return render_template(
        "knowledge.html",
        generation=overview["generation"],
        documents=overview["documents"],
    )
