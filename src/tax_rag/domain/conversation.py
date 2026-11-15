from __future__ import annotations

import re

from tax_rag.domain.scope_router import route_scope


def answer_task_for_question(question: str) -> dict:
    """Describe the requested result without inferring transaction facts or changing retrieval."""
    if re.search(r"哪几种|有哪些|哪些情况|哪些行为|比较|区别|流程|步骤|更正|红字", question):
        return {"requested_result": "按用户要求列举、比较或说明流程", "conclusion_item_limit": 6,
                "organization": "每个类别或独立问题一项，先给所问结果，不重复总述。"}
    goals = []
    if re.search(r"何时|什么时候|发生时间", question):
        goals.append("首句直接给具体日期或时点，随后合并事实与条款，不另列应税性质。")
    if re.search(r"抵扣|进项", question):
        goals.append("首句回答抵扣资格，合并必要凭证和用途条件，不展开未问的更正或红字流程。")
    if re.search(r"销售额.{0,8}(按什么|怎么|如何|确定|包括)|计税依据|计价依据", question):
        goals.append("首句给销售额确定方式或含税口径，再对应事实与条款。")
    if re.search(r"税率|征收率|13%.*1%|1%.*13%", question):
        goals.append("先给能确定的税率结论或必要分类追问，未知事实不展开整套目录。")
    if not goals and re.search(r"要交|要缴|销项|缴纳|交.*税", question):
        goals.append("先给缴税或销项处理结论，只解释该判断所需条件，不扩展未问的进项抵扣资格。")
    task = {"requested_result": goals or ["先给当前交易的处理方向，缺决定性事实则给条件判断并具体追问。"],
            "conclusion_item_limit": max(1, len(goals)),
            "organization": "先给本案适用的处理和条款，不从排除其他条款开头。在上述要点数内合并结论与必要事实。追问集中在follow_up_question，条件区不重复。",
            "fact_check": "逐项核对用户是否明确主体、来源、对价、用途、身份和期间。未知且影响当前判断的事实用如果保留并具体追问，不能从服务范围或员工一词补成公司。"}
    if "小规模" in question and re.search(r"免税|免增值税|免征", question):
        task["required_fact_checks"] = ["月或季计税期间", "整期全部不含税销售额", "是否就全部或部分交易开专票或放弃免税"]
        task["fact_check"] += "逐项从用户原话核对上述免税条件，未明确的在同一追问收集，已明确的不重复问。不能仅凭未达起征点就给无条件免税结论。未放弃免税是本次的选择状态，不能推导为法律禁止选择放弃免税，用户未问将来选择时省去这类延伸。"
    purchased_goods = re.search(r"外购|购进|购入|买来|购买", question)
    self_manufactured_goods = re.search(r"自产|委托加工", question)
    if re.search(r"福利|个人消费", question) and not re.search(r"进项|抵扣", question) and (purchased_goods or not self_manufactured_goods):
        task.setdefault("required_fact_checks", []).extend(["实际发放主体", "自产、委托加工或外购来源", "是否向员工收费或从工资扣款"])
        task["fact_check"] += "逐项核对发放事实，用户已明确的不再问。福利用途并不确认对价情况；对价未明时，结论保留条件，追问必须包含是否收费或扣工资。"
    return task


def consultation_context(question: str, history: list) -> tuple[str, list[dict]]:
    """Keep prior turns labelled as dialogue, and carry user facts into short follow-ups."""
    preceding = [dict(row) for row in history]
    user_facts = []
    for row in preceding:
        if row["role"] != "USER":
            continue
        text = row["content"][:600]
        if re.search(r"^(换个问题|新问题|另一个问题|重新开始)", text):
            user_facts = []
        status = route_scope(text).status
        if status == "OUT_OF_SCOPE":
            user_facts = []
        elif status == "IN_SCOPE" or user_facts:
            user_facts.append(text)
    dialogue = [{"role": "user", "content": fact} for fact in user_facts]
    if preceding and preceding[-1]["role"] == "ASSISTANT":
        dialogue.append({"role": "assistant", "content": preceding[-1]["content"][:1800]})
    reset = re.search(r"^(换个问题|新问题|另一个问题|重新开始)", question)
    follow_up = re.search(r"^(那|那么|如果|这个|这种|它|改成|改为|是的|不是|对的|直接|已经|之前|还没|没有|用于|外购|自产|员工|不收|不从|不扣|无偿|收费|免费|我们是|我是|受托方|委托方|收回后|加工费|202\d)|还需要|需要补充|补充哪些|补充什么|具体告诉我|具体要我|缺少哪些事实", question)
    if reset:
        return question, []
    answering = preceding and preceding[-1]["role"] == "ASSISTANT" and "需要你确认" in preceding[-1]["content"] and not re.search(r"[?？]|怎么|如何|哪", question)
    if user_facts and (follow_up or len(question.strip()) <= 12 or answering):
        query = "增值税咨询，前文用户信息：" + "；".join(user_facts) + "。当前问题：" + question
        if "外购" in question and any("员工" in fact or "福利" in fact for fact in user_facts):
            query += "。增值税外购货物用于员工福利，核对当前所问的交易处理"
        return query, dialogue
    return question, []
