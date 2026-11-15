from __future__ import annotations

import threading
import time
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

from tax_rag.domain.answer_contract import AnswerValidationError, render_answer
from tax_rag.domain.conversation import consultation_context, answer_task_for_question
from tax_rag.domain.matter_state import MatterStateError, MatterStatus, RouteStatus
from tax_rag.domain.scope_router import ScopeDecision, route_scope
from tax_rag.services.calculation_service import calculation_row_to_dict
from tax_rag.services.deepseek_service import DeepSeekError
from tax_rag.services.evidence_service import consultation_date, enrich_evidence, required_policy_evidence
from tax_rag.storage.runtime_store import RuntimeStore


@dataclass(frozen=True)
class SubmissionResult:
    matter: Any
    user_message: Any
    assistant_message: Any
    decision: ScopeDecision
    generation_id: str | None
    hits: list[Any]
    warning: bool
    closed: bool
    deepseek_status: str | None
    model_id: str | None
    retrieval_query: str = ""
    elapsed_seconds: float = 0.0
    error_code: str | None = None


class MatterService:
    def __init__(self, store: RuntimeStore, retrieval_service: Any, max_rounds: int,
                 warning_round: int, notice_version: str, llm_service: Any | None = None) -> None:
        self.store = store
        self.retrieval_service = retrieval_service
        self.max_rounds = max_rounds
        self.warning_round = warning_round
        self.notice_version = notice_version
        self.llm_service = llm_service
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()

    @contextmanager
    def _processing(self, matter_id: str):
        with self._pending_lock:
            if matter_id in self._pending:
                raise MatterStateError("本咨询正在生成回答，请等待完成后继续")
            self._pending.add(matter_id)
        try:
            yield
        finally:
            with self._pending_lock:
                self._pending.discard(matter_id)

    def create_matter(self):
        return self.store.create_matter(self.notice_version)

    def accept_notice(self, matter_id: str):
        return self.store.accept_notice(matter_id, self.notice_version)

    def _check_notice(self, matter):
        if matter["status"] == "DRAFT":
            raise MatterStateError("请先确认云端传输提示后再继续")
        if matter["notice_version"] != self.notice_version:
            raise MatterStateError("云端传输提示已更新，请新建事项并重新确认后再继续")

    def submit(self, matter_id: str, content: str, submission_token: str | None = None) -> SubmissionResult:
        with self._processing(matter_id):
            self._check_notice(self.store.get_matter(matter_id))
            matter, user_message = self.store.begin_round(matter_id, content, self.max_rounds, submission_token=submission_token)
            return self._respond(matter, user_message, retry=False)

    def retry(self, matter_id: str, user_message_id: str) -> SubmissionResult:
        with self._processing(matter_id):
            matter = self.store.get_matter(matter_id)
            self._check_notice(matter)
            message = self.store.get_message(user_message_id)
            if message["matter_id"] != matter_id or message["role"] != "USER" or message["round_number"] != matter["round_count"]:
                raise MatterStateError("只能重试当前咨询最新一轮失败的问题")
            generation = self.store.latest_generation(matter_id)
            route = self.store.latest_route(matter_id)
            failed_model = generation is not None and generation["user_message_id"] == user_message_id and generation["status"] != "SUCCESS"
            failed_retrieval = route is not None and route["user_message_id"] == user_message_id and "RETRIEVAL_UNAVAILABLE" in route["route_reason"]
            if not (failed_model or failed_retrieval):
                raise MatterStateError("最新问题已有有效回答，或无需重试模型")
            return self._respond(matter, message, retry=True)

    def _respond(self, matter, user_message, *, retry: bool) -> SubmissionResult:
        started = time.monotonic()
        matter_id = matter["matter_id"]
        content = user_message["content"]
        history = [row for row in self.store.list_messages(matter_id) if row["round_number"] < user_message["round_number"]]
        retrieval_query, dialogue = consultation_context(content, history)
        decision = route_scope(retrieval_query)
        generation_id = None
        hits = []
        query_plan = None
        channels = {}
        supplementary_queries = []
        deepseek_status = None
        model_id = None
        error_code = None
        as_of = date.today()
        response = ""
        if decision.status == RouteStatus.IN_SCOPE:
            try:
                as_of = consultation_date(retrieval_query)
                if as_of < date(2026, 1, 1):
                    error_code = "EVIDENCE_PERIOD_UNSUPPORTED"
                    response = "当前资料以2026年施行规则为核心。该历史交易需要当时有效的增值税暂行条例、细则及配套政策，请补充历史依据后核对。"
                else:
                    if hasattr(self.retrieval_service, "search_with_trace"):
                        generation, hits, channels, planned = self.retrieval_service.search_with_trace(retrieval_query)
                        query_plan = planned.as_dict()
                    else:
                        generation, hits, channels = self.retrieval_service.search(retrieval_query)
                    generation_id = generation["generation_id"]
                    deduction_question = any(term in retrieval_query for term in ("进项", "抵扣", "转出"))
                    purchased_welfare = any(term in retrieval_query for term in ("外购", "购进", "购入", "买来", "购买")) and any(term in retrieval_query for term in ("员工", "福利", "个人消费"))
                    broad_treatment = re.search(r"怎么处理|如何处理|怎样处理", content)
                    if deduction_question or (purchased_welfare and broad_treatment):
                        supplement = "中华人民共和国增值税法第二十二条，购进并用于集体福利或者个人消费的货物进项税额"
                        _generation, additional, _channels = self.retrieval_service.search(supplement)
                        supplementary_queries.append(supplement)
                        selected = next((hit for hit in additional if hit.document_title == "中华人民共和国增值税法" and hit.anchor == "第二十二条"), None)
                        if selected is not None and all(hit.chunk_id != selected.chunk_id for hit in hits):
                            hits = [*hits, selected]
                    required = required_policy_evidence(self.retrieval_service, retrieval_query)
                    required_ids = {hit.chunk_id for hit in required}
                    hits = [*required, *(hit for hit in hits if hit.chunk_id not in required_ids)]
                    hits = enrich_evidence(self.retrieval_service, hits, as_of)
                    if not hits:
                        error_code = "EVIDENCE_INSUFFICIENT"
                        response = "当前活动资料没有返回适用期间内的充分证据。请补充商品、交易安排或所需法条，以便继续核对。"
            except ValueError:
                error_code = "INVALID_POLICY_DATE"
                response = "交易日期无法识别，请按实际年月日补充。"
            except Exception as exc:
                hits = []
                error_code = "RETRIEVAL_UNAVAILABLE"
                response = "正式活动索引本轮暂时不可用，请检查本机Ollama、bge-m3和活动知识库后点击重试。错误类型为" + type(exc).__name__ + "。"
        elif decision.status == RouteStatus.OUT_OF_SCOPE:
            error_code = "OUT_OF_SCOPE"
            response = "当前范围为境内一般纳税人一般计税及小规模纳税人销售货物基础咨询。" + decision.reason.split("；")[0] + "。"
        else:
            error_code = "NEED_SCOPE_CONFIRMATION"
            response = "请说明想核对的增值税政策或货物交易，例如视同销售、抵扣、发票或计税时间。具体案例可先描述发生了什么。"
        trace = {"retrieval_query": retrieval_query, "policy_as_of": as_of.isoformat(),
                 "answer_task": answer_task_for_question(content),
                 "context_turns": len(dialogue), "channels": {name: len(rows) for name, rows in channels.items()},
                 "query_plan": query_plan, "supplementary_queries": supplementary_queries}
        route_reason = decision.reason + "；查询=" + retrieval_query
        if error_code:
            route_reason += "；错误=" + error_code
        if query_plan is not None:
            route_reason += "；查询规划=" + query_plan["planner_version"]
        retrieval_run_id = self.store.record_retrieval(matter_id, user_message["message_id"], decision.status, route_reason, generation_id, hits)
        if decision.status == RouteStatus.IN_SCOPE and hits:
            if self.llm_service is None:
                response = f"已从正式活动索引检索到{len(hits)}条证据，但DeepSeek适配器未启用。请核对原文依据。"
                error_code = "ADAPTER_DISABLED"
            else:
                model_id = self.llm_service.model_id
                latest_calculation = calculation_row_to_dict(self.store.latest_calculation(matter_id))
                generated = None
                try:
                    generated = self.llm_service.generate(content, hits, latest_calculation, conversation_context=dialogue, as_of=as_of.isoformat())
                    response = render_answer(generated.answer, hits)
                    trace.update({"model_latency_seconds": generated.latency_seconds, "api_attempts": generated.api_attempts})
                    if generated.thinking_mode is not None:
                        trace["thinking_mode"] = generated.thinking_mode
                    if generated.syntax_recovery:
                        trace["syntax_recovery"] = generated.syntax_recovery
                    if generated.optional_fields_defaulted:
                        trace["optional_fields_defaulted"] = list(generated.optional_fields_defaulted)
                    model_id = generated.model_id
                    deepseek_status = "SUCCESS"
                    self.store.record_generation(matter_id, user_message["message_id"], retrieval_run_id,
                        generated.model_id, generated.prompt_version, "SUCCESS", generated.request_context_hash,
                        generated.response_id, {"schema_version": "answer-record-v1", "answer": generated.answer,
                        "raw_answer_text": generated.raw_answer_text,
                        "calculation_snapshot": latest_calculation, "trace": trace}, None)
                except AnswerValidationError as exc:
                    trace["validation_error"] = str(exc)
                    trace["response_diagnostics"] = getattr(exc, "diagnostics", {})
                    deepseek_status = "VALIDATION_FAILED"
                    error_code = "OUTPUT_CONTRACT_INVALID"
                    response = f"已保留{len(hits)}条正式证据，但DeepSeek输出未通过引用与结构校验。请点击重试，或展开本轮依据核对原文。"
                except DeepSeekError as exc:
                    trace["response_diagnostics"] = exc.diagnostics
                    deepseek_status = "FAILED"
                    error_code = exc.code
                    hints = {"API_KEY_MISSING": "后端未读取到DEEPSEEK_API_KEY，请检查本机环境后重启服务",
                             "TIMEOUT": "DeepSeek响应超时，可稍后重试", "HTTP_401": "DeepSeek密钥认证失败，请检查后端密钥配置",
                             "EFFECTIVE_DATE_MISMATCH": "模型回答中的文件施行日期与核验来源不一致，本轮回答已拦截，请重试或核对原文",
                             "HTTP_402": "DeepSeek账户余额不足", "HTTP_429": "DeepSeek请求受限，请稍后重试",
                             "TRUNCATED_RESPONSE": "模型回答达到输出上限，请缩小问题范围或重试"}
                    response = f"已保留{len(hits)}条正式证据。" + hints.get(exc.code, "DeepSeek解释生成失败，请稍后重试") + f"（{exc.code}）。"
                except Exception:
                    deepseek_status = "FAILED"
                    error_code = "UNEXPECTED_ADAPTER_ERROR"
                    response = f"已保留{len(hits)}条正式证据，回答组件发生错误。请重试或展开本轮依据核对。"
                if deepseek_status != "SUCCESS":
                    self.store.record_generation(matter_id, user_message["message_id"], retrieval_run_id,
                        model_id, self.llm_service.prompt_version, deepseek_status, None, None,
                        {"trace": trace}, error_code)
        warning = matter["round_count"] == self.warning_round
        closed = matter["status"] == MatterStatus.CLOSED
        if warning:
            response += " 当前为第7轮，可开始新咨询继续其他问题。"
        if closed:
            response += " 本咨询已达到8轮上限，可开始新咨询。"
        if retry:
            assistant_message = self.store.replace_assistant_message(matter_id, matter["round_count"], response)
        else:
            assistant_message = self.store.add_assistant_message(matter_id, matter["round_count"], response)
        return SubmissionResult(matter, user_message, assistant_message, decision, generation_id, hits,
            warning, closed, deepseek_status, model_id, retrieval_query,
            round(time.monotonic() - started, 3), error_code)
