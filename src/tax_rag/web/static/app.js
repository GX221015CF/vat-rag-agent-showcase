import { attachAccordion } from "./accordion-motion.mjs";

const disclosures = new Map();
const animations = new Set();
const media = window.matchMedia("(prefers-reduced-motion: reduce)");
let listenerScope;
let pending = null;
let returnPoint = null;
let noticeTimer = null;
let noticeAnimation = null;
let consentOpener = null;
let confirmingConsent = false;

function openCloudNotice(opener) {
  const dialog = document.querySelector("[data-cloud-dialog]");
  if (!dialog || dialog.open) return;
  if (pending) { notify("请等待当前请求完成后再开始新咨询。"); return; }
  consentOpener = opener;
  const form = dialog.querySelector("form");
  form.reset();
  form.querySelectorAll(".field-error").forEach((error) => error.remove());
  form.querySelectorAll("[aria-invalid]").forEach((field) => { field.removeAttribute("aria-invalid"); field.removeAttribute("aria-describedby"); });
  dialog.showModal();
  dialog.querySelector("h2").focus();
}

function clearEvidenceSelection() {
  document.querySelectorAll(".evidence-card.is-selected").forEach((card) => card.classList.remove("is-selected"));
  document.querySelectorAll("[data-evidence-link][aria-current]").forEach((link) => link.removeAttribute("aria-current"));
  document.querySelectorAll("[data-evidence-return]").forEach((link) => { link.hidden = true; });
}


function motion(node, frames, duration, finish = () => {}) {
  if (media.matches || !node.animate) { finish(); return null; }
  const animation = node.animate(frames, { duration, easing: "ease-out" });
  animations.add(animation);
  animation.onfinish = () => { animations.delete(animation); finish(); };
  return animation;
}

function cancelAnimations() {
  for (const animation of animations) { animation.onfinish = null; animation.cancel(); }
  animations.clear();
}

function removeNotice() {
  window.clearTimeout(noticeTimer);
  noticeTimer = null;
  noticeAnimation?.cancel();
  noticeAnimation = null;
  document.querySelector("[data-notice-host]")?.replaceChildren();
}

function notify(text) {
  removeNotice();
  const host = document.querySelector("[data-notice-host]");
  if (!host) return;
  const message = document.createElement("div");
  message.className = "transient-notice";
  const label = document.createElement("span");
  label.textContent = text;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "text-button";
  close.textContent = "关闭";
  close.addEventListener("click", removeNotice, { once: true });
  message.append(label, close);
  host.append(message);
  const expire = () => { window.clearTimeout(noticeTimer); noticeTimer = window.setTimeout(removeNotice, 3500); };
  message.addEventListener("pointerenter", () => window.clearTimeout(noticeTimer));
  message.addEventListener("pointerleave", expire);
  message.addEventListener("focusin", () => window.clearTimeout(noticeTimer));
  message.addEventListener("focusout", (event) => { if (!message.contains(event.relatedTarget)) expire(); });
  noticeAnimation = motion(message, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], 240);
  expire();
}

function disposeDisclosures() {
  for (const { observer, player } of disclosures.values()) { observer.disconnect(); player.destroy(); }
  disclosures.clear();
}

export function mount() {
  disposeDisclosures();
  const root = document.querySelector("main");
  if (!root) return;
  root.querySelectorAll("details").forEach((details, index) => {
    const summary = details.querySelector(":scope > summary");
    if (!summary) return;
    if (!details.id) details.id = `${details.parentElement.closest("[id]")?.id || "page"}-disclosure-${index}`;
    let region = details.querySelector(":scope > .accordion-region");
    if (!region) {
      region = document.createElement("div");
      region.className = "accordion-region";
      const content = document.createElement("div");
      content.className = "accordion-content";
      [...details.childNodes].filter((node) => node !== summary).forEach((node) => content.append(node));
      region.append(content);
      details.append(region);
    }
    region.id = `${details.id}-content`;
    summary.setAttribute("aria-controls", region.id);
    summary.setAttribute("aria-expanded", String(details.open));
    const player = attachAccordion({ button: summary, region, content: region.firstElementChild, duration: Number(details.dataset.accordion || 240) });
    const observer = new MutationObserver(() => {
      details.open = !region.hidden;
    });
    observer.observe(region, { attributes: true, attributeFilter: ["hidden"] });
    disclosures.set(details, { player, observer, summary, region });
    if (details.classList.contains("evidence-card")) details.classList.toggle("is-selected", details.open);
  });
  root.querySelectorAll("form").forEach((form, index) => {
    form.noValidate = true;
    if (!form.id) form.id = `form-${index}`;
    form.querySelectorAll("input:not([type='hidden']), textarea, select").forEach((field) => {
      if (!field.id) field.id = `${form.id}-${field.name}`;
    });
  });
}

function setDisclosure(details, open, animate = true) {
  const entry = disclosures.get(details);
  if (!entry) { details.open = open; return; }
  if (open) details.open = true;
  entry.player.setOpen(open, { animate });
}

function openPath(target) {
  const ancestors = [];
  for (let node = target; node; node = node.parentElement) if (node.tagName === "DETAILS") ancestors.push(node);
  ancestors.reverse().forEach((node) => setDisclosure(node, true, false));
}

export function captureReading() {
  const conversation = document.querySelector("[data-scroll-latest]");
  const anchor = conversation && [...conversation.querySelectorAll(".message")].find((message) => {
    const bounds = message.getBoundingClientRect();
    return bounds.bottom > 0 && bounds.top < window.innerHeight;
  });
  const composer = document.querySelector("[data-answer-form]")?.getBoundingClientRect();
  return {
    open: [...disclosures].filter(([, entry]) => entry.summary.getAttribute("aria-expanded") === "true").map(([node]) => node.id),
    windowTop: window.scrollY,
    anchor: anchor?.id,
    anchorOffset: anchor?.getBoundingClientRect().top || 0,
    nearBottom: document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 96 ||
      Boolean(composer && composer.top < window.innerHeight && composer.bottom > 0),
    focus: document.activeElement?.id,
    selected: document.querySelector(".evidence-card.is-selected")?.id,
    currentCitation: document.querySelector("[data-evidence-link][aria-current]")?.id,
  };
}

function restoreReading(snapshot, exact = false) {
  for (const [details] of disclosures) {
    if (exact || snapshot.open.includes(details.id)) setDisclosure(details, snapshot.open.includes(details.id), false);
  }
  const anchor = document.getElementById(snapshot.anchor);
  window.scrollTo({ top: anchor ? window.scrollY + anchor.getBoundingClientRect().top - snapshot.anchorOffset : snapshot.windowTop, behavior: "instant" });
  clearEvidenceSelection();
  const selected = document.getElementById(snapshot.selected);
  if (selected) selected.classList.add("is-selected");
  const citation = document.getElementById(snapshot.currentCitation);
  if (citation) {
    citation.setAttribute("aria-current", "true");
    const back = selected?.querySelector("[data-evidence-return]");
    if (back && returnPoint) back.hidden = false;
  }
}

function scrollConversationToLatest() {
  const answers = document.querySelectorAll(".message-assistant");
  const latest = answers[answers.length - 1];
  if (latest) {
    latest.setAttribute("tabindex", "-1");
    latest.focus({ preventScroll: true });
    latest.scrollIntoView({ block: "start", behavior: "instant" });
  }
  const indicator = document.querySelector("[data-new-answer]");
  if (indicator) indicator.hidden = true;
}

export async function refreshConversation(url, { calculation = false } = {}) {
  const response = await fetch(url, { headers: { Accept: "text/html" }, signal: pending?.abort.signal });
  if (!response.ok) throw new Error("页面刷新失败，请保留当前问题并刷新查看结果。");
  const page = new DOMParser().parseFromString(await response.text(), "text/html");
  const updated = page.querySelector("main");
  if (!updated) throw new Error("页面返回异常，请刷新当前咨询查看结果。");
  const snapshot = captureReading();
  const drafts = [...document.querySelectorAll("[data-calculation-form] input, [data-calculation-form] select")].filter((field) => field.type !== "hidden").map((field) => ({ id: field.id, value: field.value, checked: field.checked }));
  disposeDisclosures();
  cancelAnimations();
  document.querySelector("main").replaceWith(updated);
  mount();
  for (const draft of drafts) {
    const field = document.getElementById(draft.id);
    if (field) { field.value = draft.value; field.checked = draft.checked; }
  }
  restoreReading(snapshot);
  if (calculation) {
    const calculator = document.getElementById("calculator");
    if (calculator) setDisclosure(calculator, true, false);
  } else if (snapshot.nearBottom) {
    scrollConversationToLatest();
  } else {
    const indicator = document.querySelector("[data-new-answer]");
    if (indicator) indicator.hidden = false;
  }
  const oldFocus = document.getElementById(snapshot.focus);
  if (oldFocus) oldFocus.focus({ preventScroll: true });
}

function fieldMessage(field) {
  if (field.type === "checkbox" && field.required && !field.checked) return "请确认以上输入。";
  if (field.required && !field.value.trim()) return field.tagName === "TEXTAREA" ? "请填写问题。" : "请填写此项。";
  if (field.closest("[data-calculation-form]") && field.tagName === "INPUT" && field.type !== "checkbox" && field.value.trim()) {
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(field.value.trim())) return "请填写大于或等于0的数字。";
    if (field.name === "tax_rate_percent" && Number(field.value) > 100) return "税率不能大于100%。";
  }
  if (!field.validity.valid) return field.validationMessage;
  return "";
}

function showFieldError(field, message) {
  const id = `${field.id}-error`;
  let error = document.getElementById(id);
  if (!message) {
    field.removeAttribute("aria-invalid");
    const remaining = (field.getAttribute("aria-describedby") || "").split(" ").filter((value) => value && value !== id);
    if (remaining.length) field.setAttribute("aria-describedby", remaining.join(" ")); else field.removeAttribute("aria-describedby");
    error?.remove();
    return;
  }
  field.setAttribute("aria-invalid", "true");
  field.setAttribute("aria-describedby", [...new Set([...(field.getAttribute("aria-describedby") || "").split(" "), id])].filter(Boolean).join(" "));
  if (!error) {
    error = document.createElement("p");
    error.id = id;
    error.className = "field-error";
    error.setAttribute("role", "alert");
    (field.closest("label") || field).insertAdjacentElement("afterend", error);
    motion(error, [{ opacity: 0, transform: "translateY(-3px)" }, { opacity: 1, transform: "translateY(0)" }], 240);
  }
  error.textContent = message;
}

function validateForm(form) {
  let first;
  for (const field of form.querySelectorAll("input:not([type='hidden']), textarea, select")) {
    const error = fieldMessage(field);
    showFieldError(field, error);
    if (error && !first) first = field;
  }
  if (first) { openPath(first); first.focus(); }
  return !first;
}

function setBusy(form, busy) {
  const button = form.querySelector("[data-submit-button], button[type='submit'], button.primary");
  if (!button) return () => {};
  const original = [...button.childNodes];
  const disabled = button.disabled;
  const label = document.createElement("span");
  label.textContent = form.matches("[data-retry-form]") ? "正在重试…" : form.matches("[data-calculation-form]") ? "计算中…" : "正在生成…";
  const spinner = document.createElement("span");
  spinner.className = "button-spinner";
  spinner.setAttribute("aria-hidden", "true");
  button.replaceChildren(spinner, label);
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  return () => { button.replaceChildren(...original); button.disabled = disabled; button.removeAttribute("aria-busy"); };
}

export async function submitConversation(event) {
  const form = event.target;
  if (form.matches("[data-new-consultation]")) {
    if (pending || !validateForm(form)) { event.preventDefault(); return; }
    if (document.querySelector("[data-cloud-dialog]")?.dataset.consentRequired === "true") {
      event.preventDefault(); openCloudNotice(event.submitter); return;
    }
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = "true";
    const button = form.querySelector("button[type='submit'], button.primary, button.secondary");
    if (button) button.disabled = true;
    if (form.elements.content?.value.trim()) {
      event.preventDefault();
      try {
        const response = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } });
        const result = await response.json();
        if (!response.ok || !result.url) throw new Error(result.message || "咨询创建失败");
        await refreshConversation(result.url);
        window.history.pushState({}, "", result.url);
        document.querySelector("[data-cloud-confirm] input[name='matter_id']").value = result.url.split("/").pop();
        document.querySelector("[data-answer-form]")?.requestSubmit();
      } catch (error) { delete form.dataset.submitting; if (button) button.disabled = false; notify(error.message); }
    }
    return;
  }
  if (form.matches("[data-cloud-confirm]")) {
    event.preventDefault();
    if (confirmingConsent || !validateForm(form)) return;
    confirmingConsent = true;
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    const error = form.querySelector("[data-consent-error]");
    error.hidden = true;
    try {
      const response = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "确认失败，请重试。");
      const dialog = document.querySelector("[data-cloud-dialog]");
      dialog.dataset.consentRequired = "false";
      consentOpener = document.querySelector("[data-question-input]");
      dialog.close();
      consentOpener?.focus({ preventScroll: true });
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { confirmingConsent = false; button.disabled = false; button.removeAttribute("aria-busy"); }
    return;
  }
  if (!form.matches("[data-answer-form], [data-retry-form], [data-calculation-form]")) {
    if (!validateForm(form)) event.preventDefault();
    return;
  }
  event.preventDefault();
  if (pending || !validateForm(form)) return;
  if (!form.matches("[data-calculation-form]") && document.querySelector("[data-cloud-dialog]")?.dataset.consentRequired === "true") {
    openCloudNotice(event.submitter); return;
  }
  const request = { abort: new AbortController(), timer: null };
  pending = request;
  const payload = new FormData(form);
  const calculation = form.matches("[data-calculation-form]");
  const releaseButton = setBusy(form, true);
  const textarea = form.querySelector("textarea");
  if (textarea) textarea.readOnly = true;
  form.setAttribute("aria-busy", "true");
  const status = !calculation && document.querySelector("[data-submission-status]");
  const elapsed = document.querySelector("[data-submission-elapsed]");
  const detail = document.querySelector("[data-submission-detail]");
  let error = form.querySelector("[data-submission-error]");
  if (!error) { error = document.createElement("div"); error.className = "alert alert-error"; error.setAttribute("role", "alert"); form.append(error); }
  error.hidden = true;
  if (status) status.hidden = false;
  const startedAt = Date.now();
  request.timer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    if (elapsed) elapsed.textContent = `已等待 ${seconds} 秒`;
    if (seconds >= 90 && detail) detail.textContent = "仍在等待返回，完成后会显示回答或具体错误。";
    else if (seconds >= 45 && detail) detail.textContent = "仍在生成，请稍候。";
  }, 1000);
  try {
    const response = await fetch(form.action, { method: "POST", body: payload, headers: { Accept: "application/json" }, signal: request.abort.signal });
    const result = await response.json();
    if (result.consent_required) {
      document.querySelector("[data-cloud-dialog]").dataset.consentRequired = "true";
      request.consentRequired = true;
      throw new Error("请确认弹窗中的云端传输告知，再发送当前问题。");
    }
    if (result.url) await refreshConversation(result.url, { calculation });
    else {
      for (const [name, message] of Object.entries(result.field_errors || {})) {
        const field = form.elements.namedItem(name);
        if (field) showFieldError(field, message);
      }
      throw new Error(result.message || "请求未完成，请保留输入并重试。");
    }
  } catch (failure) {
    if (failure.name !== "AbortError") { error.textContent = failure.message || "连接中断，请重试当前问题。"; error.hidden = false; }
  } finally {
    window.clearInterval(request.timer);
    if (pending === request) pending = null;
    releaseButton();
    form.removeAttribute("aria-busy");
    if (textarea) textarea.readOnly = false;
    if (status) status.hidden = true;
    if (request.consentRequired) openCloudNotice(form.querySelector("[data-submit-button]"));
  }
}

async function onClick(event) {
  if (event.target.closest("[data-cloud-cancel]")) {
    if (confirmingConsent) return;
    document.querySelector("[data-cloud-dialog]")?.close();
    return;
  }
  const button = event.target.closest("button.primary");
  if (button && !button.disabled && !media.matches) {
    const rect = button.getBoundingClientRect();
    const size = Math.min(240, Math.max(rect.width, rect.height) * 2);
    const ripple = document.createElement("span");
    ripple.className = "button-ripple";
    ripple.setAttribute("aria-hidden", "true");
    Object.assign(ripple.style, { width: `${size}px`, height: `${size}px`, left: `${event.detail ? event.clientX - rect.left - size / 2 : (rect.width - size) / 2}px`, top: `${event.detail ? event.clientY - rect.top - size / 2 : (rect.height - size) / 2}px` });
    button.append(ripple);
    motion(ripple, [{ transform: "scale(0)", opacity: .12 }, { transform: "scale(1)", opacity: 0 }], 480, () => ripple.remove());
  }
  const summary = event.target.closest("summary");
  if (summary && disclosures.has(summary.parentElement)) {
    event.preventDefault();
    if (summary.parentElement.classList.contains("evidence-card")) {
      const back = summary.parentElement.querySelector("[data-evidence-return]");
      const canReturn = back && !back.hidden && returnPoint;
      clearEvidenceSelection();
      summary.parentElement.classList.add("is-selected");
      if (canReturn) back.hidden = false;
    }
    setDisclosure(summary.parentElement, summary.getAttribute("aria-expanded") !== "true");
  }
  const citation = event.target.closest("[data-evidence-link]");
  if (citation) {
    const target = document.getElementById(citation.hash.slice(1));
    if (target) {
      event.preventDefault();
      returnPoint = { ...captureReading(), citation: citation.id };
      clearEvidenceSelection();
      openPath(target);
      target.classList.add("is-selected");
      citation.setAttribute("aria-current", "true");
      const back = target.querySelector("[data-evidence-return]");
      if (back) back.hidden = false;
      target.querySelector("summary")?.focus({ preventScroll: true });
      target.scrollIntoView({ behavior: media.matches ? "instant" : "smooth", block: "center" });
    }
  }
  if (event.target.closest("[data-evidence-return]") && returnPoint) {
    event.preventDefault();
    const saved = returnPoint;
    clearEvidenceSelection();
    document.querySelectorAll("[data-evidence-return]").forEach((link) => { link.hidden = true; });
    restoreReading(saved, true);
    document.getElementById(saved.citation)?.focus({ preventScroll: true });
    returnPoint = null;
  }
  const close = event.target.closest("[data-evidence-close]");
  if (close) setDisclosure(close.closest(".evidence-card"), false);
  if (event.target.closest("[data-open-calculator]")) {
    const calculator = document.getElementById("calculator");
    if (calculator) { event.preventDefault(); openPath(calculator); calculator.querySelector("summary")?.focus({ preventScroll: true }); calculator.scrollIntoView({ block: "start", behavior: media.matches ? "instant" : "smooth" }); }
  }
  if (event.target.closest("[data-new-answer]")) scrollConversationToLatest();
  const copy = event.target.closest("[data-copy-answer]");
  if (copy) {
    const article = copy.closest(".message");
    const content = article.querySelector(".answer-main, .message-content");
    const text = content?.dataset?.copyText ?? content?.innerText.trim();
    try { await navigator.clipboard.writeText(text || ""); notify("回答已复制"); } catch { notify("复制未完成，请选中文字复制"); }
  }
}

function activate() {
  confirmingConsent = false;
  const consentButton = document.querySelector("[data-cloud-confirm] button[type='submit']");
  if (consentButton) { consentButton.disabled = false; consentButton.removeAttribute("aria-busy"); }
  document.querySelectorAll("[data-new-consultation]").forEach((form) => { delete form.dataset.submitting; const button = form.querySelector("button.primary"); if (button) button.disabled = false; });
  listenerScope?.abort();
  listenerScope = new AbortController();
  const options = { signal: listenerScope.signal };
  document.addEventListener("submit", submitConversation, options);
  document.addEventListener("click", onClick, options);
  document.addEventListener("input", (event) => { if (event.target.matches("input, textarea, select") && event.target.getAttribute("aria-invalid") === "true") showFieldError(event.target, fieldMessage(event.target)); }, options);
  document.addEventListener("change", (event) => { if (event.target.matches("input, select") && event.target.getAttribute("aria-invalid") === "true") showFieldError(event.target, fieldMessage(event.target)); }, options);
  document.addEventListener("keydown", handleQuestionKey, options);
  document.addEventListener("scroll", () => {
    const answers = document.querySelectorAll(".message-assistant");
    const bounds = answers[answers.length - 1]?.getBoundingClientRect();
    if (bounds && bounds.top >= 0 && bounds.top < window.innerHeight) {
      const indicator = document.querySelector("[data-new-answer]");
      if (indicator) indicator.hidden = true;
    }
  }, options);
  const dialog = document.querySelector("[data-cloud-dialog]");
  dialog?.addEventListener("cancel", (event) => { if (confirmingConsent) event.preventDefault(); }, options);
  dialog?.addEventListener("close", () => {
    if (consentOpener?.isConnected) consentOpener.focus({ preventScroll: true });
    consentOpener = null;
  }, options);
  mount();
  if (dialog?.dataset.consentRequired === "true" && document.querySelector("[data-new-consultation], [data-answer-form]")) openCloudNotice(document.querySelector("[data-question-input]"));
}

export function handleQuestionKey(event) {
  const field = event.target;
  if (event.key !== "Enter" || !field.matches("[data-question-input]") || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (event.repeat || pending || confirmingConsent || field.readOnly || field.form?.dataset.submitting) return;
  if (event.ctrlKey) {
    field.setRangeText("\n", field.selectionStart, field.selectionEnd, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  if (event.shiftKey || event.altKey || event.metaKey || !field.value.trim()) return;
  field.form?.requestSubmit();
}

export function dispose() {
  listenerScope?.abort();
  pending?.abort.abort();
  window.clearInterval(pending?.timer);
  pending = null;
  disposeDisclosures();
  cancelAnimations();
  removeNotice();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", activate, { once: true });
else activate();
window.addEventListener("pagehide", dispose);
window.addEventListener("pageshow", (event) => { if (event.persisted) activate(); });
media.addEventListener?.("change", cancelAnimations);
