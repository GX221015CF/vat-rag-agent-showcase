from __future__ import annotations

import os
import atexit
import secrets
from pathlib import Path

from flask import Flask, abort, request, session

from tax_rag.config import PROJECT_ROOT, load_toml, project_path
from tax_rag.services.calculation_service import CalculationService
from tax_rag.services.deepseek_service import DeepSeekService
from tax_rag.services.matter_service import MatterService
from tax_rag.services.retrieval_service import ActiveRetrievalService
from tax_rag.storage.runtime_store import RuntimeStore
from tax_rag.web.routes import bp


def ensure_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise RuntimeError("Flask本地密钥文件无效")
        return value
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    return value


def create_app(test_config: dict | None = None) -> Flask:
    runtime = load_toml("runtime.toml")
    template_dir = PROJECT_ROOT / "src" / "tax_rag" / "web" / "templates"
    static_dir = PROJECT_ROOT / "src" / "tax_rag" / "web" / "static"
    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    configured_secret = (test_config or {}).get("SECRET_KEY")
    app.config.update(
        SECRET_KEY=configured_secret
        or ensure_secret(project_path(runtime["runtime"]["secret_path"])),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=16 * 1024,
        WEB_HOST=runtime["web"]["host"],
        WEB_PORT=int(runtime["web"]["port"]),
        NOTICE_VERSION=runtime["notice"]["version"],
        NOTICE_TEXT=(PROJECT_ROOT / runtime["notice"]["text_path"]).read_text(encoding="utf-8"),
        CLEANUP_BEFORE_REQUEST=bool(runtime["retention"]["cleanup_before_request"]),
        CLEANUP_ON_STARTUP=bool(runtime["retention"]["cleanup_on_startup"]),
    )
    if test_config:
        app.config.update(test_config)

    runtime_db = Path(app.config.get("RUNTIME_DATABASE_PATH") or project_path(runtime["runtime"]["database_path"]))
    store = RuntimeStore(
        runtime_db,
        PROJECT_ROOT / "src" / "tax_rag" / "storage" / "runtime_schema.sql",
        int(runtime["retention"]["raw_data_days"]),
    )
    retrieval_service = app.config.get("RETRIEVAL_SERVICE") or ActiveRetrievalService()
    llm_service = app.config.get("LLM_SERVICE") or DeepSeekService()
    calculation_service = CalculationService(store)
    matter_service = MatterService(
        store,
        retrieval_service,
        int(runtime["matter"]["max_rounds"]),
        int(runtime["matter"]["warning_round"]),
        runtime["notice"]["version"],
        llm_service,
    )
    app.extensions["runtime_store"] = store
    app.extensions["retrieval_service"] = retrieval_service
    app.extensions["llm_service"] = llm_service
    app.extensions["calculation_service"] = calculation_service
    app.extensions["matter_service"] = matter_service
    if hasattr(retrieval_service, "close"):
        atexit.register(retrieval_service.close)

    if app.config["CLEANUP_ON_STARTUP"]:
        store.cleanup_expired()

    @app.before_request
    def security_and_cleanup() -> None:
        if request.remote_addr not in {None, "127.0.0.1", "::1"} and not app.testing:
            abort(403, "P2A只允许本机访问")
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if app.config["CLEANUP_BEFORE_REQUEST"] and request.endpoint != "static":
            store.cleanup_expired()

    @app.after_request
    def secure_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
    return app
