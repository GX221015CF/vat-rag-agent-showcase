from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tax_rag.domain.matter_state import (
    DuplicateSubmissionError,
    MatterStateError,
    MatterStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RuntimeStore:
    def __init__(self, path: Path, schema_path: Path, retention_days: int = 7) -> None:
        self.path = path
        self.schema_path = schema_path
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(self.schema_path.read_text(encoding="utf-8"))
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            with connection:
                if "submission_token" not in columns:
                    connection.execute(
                        "ALTER TABLE messages ADD COLUMN submission_token TEXT"
                    )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_matter_submission_token
                    ON messages(matter_id, submission_token)
                    WHERE submission_token IS NOT NULL
                    """
                )
        finally:
            connection.close()

    def _audit(
        self,
        connection: sqlite3.Connection,
        matter_id: str,
        event_type: str,
        detail: dict[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(event_id,matter_ref_hash,event_type,detail_hash,created_at) VALUES(?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                hash_text(matter_id),
                event_type,
                hash_text(json.dumps(detail, sort_keys=True, ensure_ascii=False)),
                iso(now),
            ),
        )

    def create_matter(self, notice_version: str, now: datetime | None = None) -> sqlite3.Row:
        now = now or utc_now()
        matter_id = uuid.uuid4().hex
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO matters(matter_id,status,round_count,notice_version,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (matter_id, MatterStatus.DRAFT, 0, notice_version, iso(now), iso(now)),
                )
                self._audit(connection, matter_id, "MATTER_CREATED", {"notice_version": notice_version}, now)
            return self.get_matter(matter_id)
        finally:
            connection.close()

    def get_matter(self, matter_id: str) -> sqlite3.Row:
        connection = self.connect()
        try:
            row = connection.execute("SELECT * FROM matters WHERE matter_id=?", (matter_id,)).fetchone()
            if row is None:
                raise KeyError(matter_id)
            return row
        finally:
            connection.close()

    def list_matters(self, limit: int = 20) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(
                """
                SELECT m.*, (
                    SELECT content FROM messages
                    WHERE matter_id=m.matter_id AND role='USER'
                    ORDER BY round_number,created_at,message_id LIMIT 1
                ) AS first_question
                FROM matters m WHERE m.status!='EXPIRED'
                ORDER BY m.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()

    def record_browser_consent(self, browser_id: str, notice_version: str) -> None:
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO browser_consents VALUES(?,?,?) ON CONFLICT(session_hash) DO UPDATE SET notice_version=excluded.notice_version,accepted_at=excluded.accepted_at",
                    (hash_text(browser_id), notice_version, iso(utc_now())),
                )
        finally:
            connection.close()

    def browser_consent_current(self, browser_id: str, notice_version: str) -> bool:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT 1 FROM browser_consents WHERE session_hash=? AND notice_version=? AND accepted_at>=?",
                (hash_text(browser_id), notice_version, iso(utc_now() - timedelta(days=self.retention_days))),
            ).fetchone() is not None
        finally:
            connection.close()

    def accept_notice(self, matter_id: str, notice_version: str, now: datetime | None = None) -> sqlite3.Row:
        now = now or utc_now()
        connection = self.connect()
        try:
            with connection:
                row = connection.execute("SELECT * FROM matters WHERE matter_id=?", (matter_id,)).fetchone()
                if row is None:
                    raise KeyError(matter_id)
                if row["status"] == MatterStatus.DRAFT or (row["status"] in {MatterStatus.ACTIVE, MatterStatus.CLOSED} and row["notice_version"] != notice_version):
                    connection.execute(
                        "UPDATE matters SET status=CASE WHEN status='DRAFT' THEN 'ACTIVE' ELSE status END,notice_version=?,notice_accepted_at=?,updated_at=? WHERE matter_id=?",
                        (notice_version, iso(now), iso(now), matter_id),
                    )
                    self._audit(connection, matter_id, "NOTICE_ACCEPTED", {"notice_version": notice_version}, now)
                elif row["status"] not in {MatterStatus.ACTIVE, MatterStatus.CLOSED}:
                    raise MatterStateError("当前事项不能接受提示")
            return self.get_matter(matter_id)
        finally:
            connection.close()

    def begin_round(
        self,
        matter_id: str,
        content: str,
        max_rounds: int,
        submission_token: str | None = None,
        now: datetime | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        now = now or utc_now()
        content = content.strip()
        if not content:
            raise ValueError("问题不能为空")
        if len(content) > 4000:
            raise ValueError("单条问题不能超过4000个字符")
        if submission_token is not None and not 32 <= len(submission_token) <= 128:
            raise ValueError("提交令牌无效，请刷新页面后重试")
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            matter = connection.execute("SELECT * FROM matters WHERE matter_id=?", (matter_id,)).fetchone()
            if matter is None:
                connection.rollback()
                raise KeyError(matter_id)
            if submission_token is not None:
                existing = connection.execute(
                    """
                    SELECT message_id FROM messages
                    WHERE matter_id=? AND submission_token=? AND role='USER'
                    """,
                    (matter_id, submission_token),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    raise DuplicateSubmissionError(
                        "这个问题已经提交，系统正在处理或已经完成，没有重复新增轮次。"
                    )
            if matter["status"] != MatterStatus.ACTIVE or matter["notice_accepted_at"] is None:
                connection.rollback()
                raise MatterStateError("请先接受本事项的云端传输提示")
            next_round = matter["round_count"] + 1
            if next_round > max_rounds:
                connection.rollback()
                raise MatterStateError("本事项已达到8轮上限")
            message_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO messages(
                    message_id,matter_id,role,round_number,content,submission_token,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    matter_id,
                    "USER",
                    next_round,
                    content,
                    submission_token,
                    iso(now),
                ),
            )
            status = MatterStatus.CLOSED if next_round == max_rounds else MatterStatus.ACTIVE
            closed_at = iso(now) if status == MatterStatus.CLOSED else None
            connection.execute(
                "UPDATE matters SET round_count=?,status=?,updated_at=?,closed_at=? WHERE matter_id=?",
                (next_round, status, iso(now), closed_at, matter_id),
            )
            self._audit(connection, matter_id, "USER_ROUND_ACCEPTED", {"round": next_round}, now)
            connection.commit()
            return self.get_matter(matter_id), self.get_message(message_id)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def add_assistant_message(
        self,
        matter_id: str,
        round_number: int,
        content: str,
        now: datetime | None = None,
    ) -> sqlite3.Row:
        now = now or utc_now()
        message_id = uuid.uuid4().hex
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO messages(message_id,matter_id,role,round_number,content,created_at) VALUES(?,?,?,?,?,?)",
                    (message_id, matter_id, "ASSISTANT", round_number, content, iso(now)),
                )
                self._audit(connection, matter_id, "ASSISTANT_LOCAL_RESPONSE", {"round": round_number}, now)
            return self.get_message(message_id)
        finally:
            connection.close()

    def get_message(self, message_id: str) -> sqlite3.Row:
        connection = self.connect()
        try:
            row = connection.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            return row
        finally:
            connection.close()

    def replace_assistant_message(self, matter_id: str, round_number: int, content: str):
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT message_id FROM messages WHERE matter_id=? AND round_number=? AND role='ASSISTANT' ORDER BY created_at DESC LIMIT 1",
                (matter_id, round_number),
            ).fetchone()
            if row is None:
                return self.add_assistant_message(matter_id, round_number, content)
            with connection:
                connection.execute("UPDATE messages SET content=? WHERE message_id=?", (content, row["message_id"]))
            return self.get_message(row["message_id"])
        finally:
            connection.close()

    def retrieval_for_round(self, matter_id: str, round_number: int):
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT r.* FROM retrieval_runs r JOIN messages m ON m.message_id=r.user_message_id WHERE r.matter_id=? AND m.round_number=? ORDER BY r.created_at DESC LIMIT 1",
                (matter_id, round_number),
            ).fetchone()
        finally:
            connection.close()

    def list_messages(self, matter_id: str) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM messages WHERE matter_id=? ORDER BY created_at,message_id",
                (matter_id,),
            ).fetchall()
        finally:
            connection.close()

    def record_retrieval(
        self,
        matter_id: str,
        user_message_id: str,
        route_status: str,
        route_reason: str,
        generation_id: str | None,
        hits: list[Any],
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run_id = uuid.uuid4().hex
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO retrieval_runs(retrieval_run_id,matter_id,user_message_id,route_status,route_reason,generation_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (run_id, matter_id, user_message_id, route_status, route_reason, generation_id, iso(now)),
                )
                for rank, hit in enumerate(hits, start=1):
                    connection.execute(
                        "INSERT INTO retrieval_hits(retrieval_run_id,rank,chunk_id,document_title,anchor,text,source_json,rrf_score,channel_ranks_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            rank,
                            hit.chunk_id,
                            hit.document_title,
                            hit.anchor,
                            hit.text,
                            json.dumps(hit.source, ensure_ascii=False),
                            str(hit.score),
                            json.dumps(hit.ranks or {}, ensure_ascii=False),
                            iso(now),
                        ),
                    )
                self._audit(
                    connection,
                    matter_id,
                    "RETRIEVAL_COMPLETED" if generation_id else "RETRIEVAL_SKIPPED",
                    {"route_status": route_status, "hit_count": len(hits), "generation_id": generation_id},
                    now,
                )
            return run_id
        finally:
            connection.close()

    def latest_hits(self, matter_id: str) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            run = connection.execute(
                "SELECT retrieval_run_id,route_status,route_reason,generation_id FROM retrieval_runs WHERE matter_id=? ORDER BY created_at DESC LIMIT 1",
                (matter_id,),
            ).fetchone()
            if run is None:
                return []
            return connection.execute(
                "SELECT * FROM retrieval_hits WHERE retrieval_run_id=? ORDER BY rank",
                (run["retrieval_run_id"],),
            ).fetchall()
        finally:
            connection.close()

    def latest_route(self, matter_id: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT route_status,route_reason,generation_id,user_message_id FROM retrieval_runs WHERE matter_id=? ORDER BY created_at DESC LIMIT 1",
                (matter_id,),
            ).fetchone()
        finally:
            connection.close()

    def record_calculation(
        self,
        matter_id: str,
        result: Any,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        payload = result.as_dict()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        run_id = uuid.uuid4().hex
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO calculation_runs(
                        calculation_run_id,matter_id,calculation_type,formula_version,
                        rounding_rule_version,input_json,intermediate_json,output_json,
                        result_hash,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        matter_id,
                        result.calculation_type,
                        result.formula_version,
                        result.rounding_rule_version,
                        json.dumps(result.inputs, ensure_ascii=False, sort_keys=True),
                        json.dumps(result.intermediates, ensure_ascii=False, sort_keys=True),
                        json.dumps(result.outputs, ensure_ascii=False, sort_keys=True),
                        hash_text(canonical),
                        iso(now),
                    ),
                )
                self._audit(
                    connection,
                    matter_id,
                    "DECIMAL_CALCULATION_COMPLETED",
                    {
                        "calculation_type": result.calculation_type,
                        "formula_version": result.formula_version,
                        "rounding_rule_version": result.rounding_rule_version,
                        "result_hash": hash_text(canonical),
                    },
                    now,
                )
            return run_id
        finally:
            connection.close()

    def latest_calculation(self, matter_id: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM calculation_runs WHERE matter_id=? ORDER BY created_at DESC,calculation_run_id DESC LIMIT 1",
                (matter_id,),
            ).fetchone()
        finally:
            connection.close()

    def record_generation(
        self,
        matter_id: str,
        user_message_id: str,
        retrieval_run_id: str,
        model_id: str,
        prompt_version: str,
        status: str,
        request_context_hash: str | None,
        response_id: str | None,
        response: dict[str, Any] | None,
        error_code: str | None,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run_id = uuid.uuid4().hex
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO generation_runs(
                        generation_run_id,matter_id,user_message_id,retrieval_run_id,
                        model_id,prompt_version,status,request_context_hash,response_id,
                        response_json,error_code,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        matter_id,
                        user_message_id,
                        retrieval_run_id,
                        model_id,
                        prompt_version,
                        status,
                        request_context_hash,
                        response_id,
                        json.dumps(response, ensure_ascii=False, sort_keys=True)
                        if response is not None
                        else None,
                        error_code,
                        iso(now),
                    ),
                )
                self._audit(
                    connection,
                    matter_id,
                    "DEEPSEEK_GENERATION_COMPLETED" if status == "SUCCESS" else "DEEPSEEK_GENERATION_FAILED",
                    {
                        "model_id": model_id,
                        "prompt_version": prompt_version,
                        "status": status,
                        "error_code": error_code,
                    },
                    now,
                )
            return run_id
        finally:
            connection.close()

    def latest_generation(self, matter_id: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM generation_runs WHERE matter_id=? ORDER BY created_at DESC,generation_run_id DESC LIMIT 1",
                (matter_id,),
            ).fetchone()
        finally:
            connection.close()

    def list_successful_generations(self, matter_id: str) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(
                """
                SELECT gr.*,um.round_number,rr.generation_id
                FROM generation_runs gr
                JOIN messages um ON um.message_id=gr.user_message_id
                JOIN retrieval_runs rr ON rr.retrieval_run_id=gr.retrieval_run_id
                WHERE gr.matter_id=? AND gr.status='SUCCESS' AND gr.response_json IS NOT NULL
                ORDER BY um.round_number,gr.created_at,gr.generation_run_id
                """,
                (matter_id,),
            ).fetchall()
        finally:
            connection.close()

    def hits_for_retrieval_run(self, retrieval_run_id: str) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM retrieval_hits WHERE retrieval_run_id=? ORDER BY rank",
                (retrieval_run_id,),
            ).fetchall()
        finally:
            connection.close()

    def cleanup_expired(self, now: datetime | None = None) -> dict[str, int]:
        now = now or utc_now()
        cutoff = iso(now - timedelta(days=self.retention_days))
        connection = self.connect()
        try:
            with connection:
                generation_count = connection.execute(
                    "SELECT COUNT(*) FROM generation_runs WHERE created_at < ?", (cutoff,)
                ).fetchone()[0]
                calculation_count = connection.execute(
                    "SELECT COUNT(*) FROM calculation_runs WHERE created_at < ?", (cutoff,)
                ).fetchone()[0]
                hit_count = connection.execute(
                    "SELECT COUNT(*) FROM retrieval_hits WHERE created_at < ?", (cutoff,)
                ).fetchone()[0]
                run_count = connection.execute(
                    "SELECT COUNT(*) FROM retrieval_runs WHERE created_at < ?", (cutoff,)
                ).fetchone()[0]
                message_count = connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at < ?", (cutoff,)
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM generation_runs WHERE created_at < ?", (cutoff,)
                )
                connection.execute(
                    "DELETE FROM calculation_runs WHERE created_at < ?", (cutoff,)
                )
                connection.execute(
                    "DELETE FROM retrieval_hits WHERE created_at < ?", (cutoff,)
                )
                connection.execute(
                    "DELETE FROM retrieval_runs WHERE created_at < ?", (cutoff,)
                )
                connection.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
                connection.execute("DELETE FROM browser_consents WHERE accepted_at < ?", (cutoff,))
                matter_count = connection.execute(
                    "SELECT COUNT(*) FROM matters WHERE updated_at < ? AND status!='EXPIRED'", (cutoff,)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE matters SET status='EXPIRED' WHERE updated_at < ? AND status!='EXPIRED'",
                    (cutoff,),
                )
            return {
                "messages_deleted": message_count,
                "retrieval_runs_deleted": run_count,
                "retrieval_hits_deleted": hit_count,
                "generation_runs_deleted": generation_count,
                "calculation_runs_deleted": calculation_count,
                "matters_expired": matter_count,
            }
        finally:
            connection.close()
