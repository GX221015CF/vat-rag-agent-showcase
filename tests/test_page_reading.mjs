import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// DOM doubles exercise page-coordinate logic without controlling a browser.
const script = readFileSync(new URL("../src/tax_rag/web/static/app.js", import.meta.url), "utf8")
  .replace(/^import .*$/m, "")
  .replace(/export /g, "");

function environment() {
  const nodes = new Map();
  const message = { id: "round-1-assistant", getBoundingClientRect: () => ({ top: 800 - win.scrollY, bottom: 1600 - win.scrollY }) };
  const composer = { getBoundingClientRect: () => ({ top: 1800 - win.scrollY, bottom: 2000 - win.scrollY }) };
  const latest = { id: "round-2-assistant", setAttribute() {}, focus() { latest.focused = true; }, scrollIntoView() { latest.scrolled = true; } };
  const indicator = { hidden: true };
  const main = { replaceWith() {} };
  const win = {
    scrollY: 900, innerHeight: 600,
    matchMedia: () => ({ matches: true, addEventListener() {} }),
    addEventListener() {}, clearTimeout() {}, clearInterval() {},
    scrollTo({ top }) { win.scrollY = top; },
  };
  const doc = {
    readyState: "loading", activeElement: {}, documentElement: { scrollHeight: 3000 },
    addEventListener() {}, getElementById: (id) => nodes.get(id),
    querySelector(selector) {
      if (selector === "[data-scroll-latest]") return { querySelectorAll: () => [message] };
      if (selector === "[data-answer-form]") return composer;
      if (selector === "[data-new-answer]") return indicator;
      if (selector === "main") return main;
      return null;
    },
    querySelectorAll(selector) { return selector === ".message-assistant" ? [latest] : []; },
  };
  nodes.set(message.id, message);
  const context = vm.createContext({ window: win, document: doc, AbortController });
  vm.runInContext(script, context);
  return { nodes, message, latest, indicator, main, win, doc, context };
}

{
  const e = environment();
  const snapshot = vm.runInContext("captureReading()", e.context);
  assert.equal(snapshot.anchor, "round-1-assistant");
  assert.equal(snapshot.anchorOffset, -100);
  assert.equal(snapshot.nearBottom, false);
  e.win.scrollY = 1200;
  e.context.snapshot = snapshot;
  vm.runInContext("restoreReading(snapshot)", e.context);
  assert.equal(e.win.scrollY, 900);
  e.win.scrollY = 1700;
  assert.equal(vm.runInContext("captureReading().nearBottom", e.context), true);
  vm.runInContext("scrollConversationToLatest()", e.context);
  assert.equal(e.latest.scrolled, true);
  assert.equal(e.latest.focused, true);
  assert.equal(e.indicator.hidden, true);
}

for (const followLatest of [false, true]) {
  const e = environment();
  e.context.fetch = async () => ({ ok: true, text: async () => "updated page" });
  e.context.DOMParser = class { parseFromString() { return { querySelector: () => ({}) }; } };
  vm.runInContext("mount = () => {};", e.context);
  e.win.scrollY = followLatest ? 1700 : 900;
  await vm.runInContext("refreshConversation('/matters/test')", e.context);
  assert.equal(Boolean(e.latest.scrolled), followLatest);
  assert.equal(e.indicator.hidden, followLatest);
  if (!followLatest) assert.equal(e.win.scrollY, 900);
}

for (const text of ["历史文本结论 [证据1]", "直接结论\n需要计算销项税额。适用第五条第一项。 1"]) {
  const e = environment();
  let copied;
  e.context.navigator = { clipboard: { async writeText(value) { copied = value; } } };
  vm.runInContext("notify = () => {};", e.context);
  const copy = { closest: () => ({ querySelector: () => ({ innerText: text }) }) };
  e.context.copyEvent = { target: { closest: (selector) => selector === "[data-copy-answer]" ? copy : null } };
  await vm.runInContext("onClick(copyEvent)", e.context);
  assert.equal(copied, text);
}

{
  const e = environment();
  let copied;
  e.context.navigator = { clipboard: { async writeText(value) { copied = value; } } };
  vm.runInContext("notify = () => {};", e.context);
  const content = { innerText: "旧检索编号4、1、5", dataset: { copyText: "本轮结论[证据1][证据2][证据3]" } };
  const copy = { closest: () => ({ querySelector: () => content }) };
  e.context.copyEvent = { target: { closest: (selector) => selector === "[data-copy-answer]" ? copy : null } };
  await vm.runInContext("onClick(copyEvent)", e.context);
  assert.equal(copied, content.dataset.copyText);
}

{
  const e = environment();
  e.context.fetch = async () => ({ ok: true, text: async () => "updated page" });
  e.context.DOMParser = class { parseFromString() { return { querySelector: () => ({}) }; } };
  vm.runInContext("mount = () => {};", e.context);
  const calculation = { tagName: "DETAILS", open: false };
  e.nodes.set("calculator", calculation);
  await vm.runInContext("refreshConversation('/matters/test', { calculation: true })", e.context);
  assert.equal(calculation.open, true);
  assert.equal(e.win.scrollY, 900);
  assert.equal(Boolean(e.latest.scrolled), false);
}

{
  const e = environment();
  const classes = new Set();
  const summary = { getAttribute: () => "false", focus() { summary.focused = true; } };
  const back = { hidden: true };
  const evidence = {
    id: "evidence-1-chunk", tagName: "DETAILS", open: false,
    classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    querySelector: (selector) => selector === "summary" ? summary : back,
    scrollIntoView() { e.win.scrollY = 2000; },
  };
  const citation = {
    id: "cite-1-point-1-1", hash: "#evidence-1-chunk",
    setAttribute() {}, removeAttribute() {}, focus() { citation.focused = true; },
  };
  e.nodes.set(evidence.id, evidence);
  e.nodes.set(citation.id, citation);
  e.doc.querySelectorAll = (selector) => {
    if (selector.includes("is-selected")) return classes.has("is-selected") ? [evidence] : [];
    if (selector.includes("aria-current")) return [citation];
    if (selector.includes("data-evidence-return")) return [back];
    return [];
  };
  e.context.testEvent = { preventDefault() {}, target: { closest: (selector) => selector === "[data-evidence-link]" ? citation : null } };
  await vm.runInContext("onClick(testEvent)", e.context);
  assert.equal(evidence.open, true);
  assert.equal(classes.has("is-selected"), true);
  assert.equal(back.hidden, false);
  assert.equal(summary.focused, true);
  e.context.testEvent.target.closest = (selector) => selector === "[data-evidence-return]" ? back : null;
  await vm.runInContext("onClick(testEvent)", e.context);
  assert.equal(e.win.scrollY, 900);
  assert.equal(citation.focused, true);
  assert.equal(back.hidden, true);
}

{
  const e = environment();
  const input = { value: "保留待发问题", readOnly: false };
  const error = { hidden: true };
  const button = {};
  let opened = 0;
  let sent = 0;
  const consentForm = { reset() {}, querySelectorAll: () => [] };
  const dialog = { open: false, dataset: { consentRequired: "true" },
    showModal() { this.open = true; opened++; }, close() { this.open = false; },
    querySelector: (selector) => selector === "form" ? consentForm : { focus() {} } };
  const baseQuery = e.doc.querySelector;
  e.doc.querySelector = (selector) => selector === "[data-cloud-dialog]" ? dialog : baseQuery(selector);
  const form = { action: "/messages", dataset: {}, matches: (selector) => selector.includes("[data-answer-form]"),
    querySelector: (selector) => selector === "textarea" ? input : selector === "[data-submit-button]" ? button : selector === "[data-submission-error]" ? error : null,
    setAttribute() {}, removeAttribute() {} };
  e.win.setInterval = () => 1;
  e.context.FormData = class {};
  e.context.fetch = async () => { sent++; return { json: async () => ({ consent_required: true }) }; };
  e.context.formEvent = { target: form, submitter: button, preventDefault() {} };
  vm.runInContext("validateForm = () => true; setBusy = () => () => {};", e.context);
  for (let i = 0; i < 3; i++) {
    await vm.runInContext("submitConversation(formEvent)", e.context);
    assert.equal(dialog.open, true);
    dialog.close();
  }
  assert.equal(sent, 0);
  assert.equal(opened, 3);
  assert.equal(input.value, "保留待发问题");
  dialog.dataset.consentRequired = "false";
  await vm.runInContext("submitConversation(formEvent)", e.context);
  assert.equal(sent, 1);
  assert.equal(dialog.open, true);
  assert.equal(input.readOnly, false);
  assert.equal(input.value, "保留待发问题");
  assert.equal(vm.runInContext("pending", e.context), null);
}

console.log("Page-reading logic passed: capture/restore, follow/latest, reading preservation, calculation refresh, citation return, old/new answer copy, cancelled notice reopening and server reconsent. DOM doubles only.");

{
  const e = environment();
  let submitted = 0;
  const field = {
    value: "甲乙丙", selectionStart: 1, selectionEnd: 2,
    matches: (selector) => selector === "[data-question-input]",
    form: { dataset: {}, requestSubmit() { submitted++; } },
    setRangeText(text, start, end) { this.value = this.value.slice(0, start) + text + this.value.slice(end); this.selectionStart = this.selectionEnd = start + text.length; },
    dispatchEvent() {},
  };
  e.context.Event = class {};
  e.context.key = { key: "Enter", target: field, preventDefault() {} };
  vm.runInContext("handleQuestionKey(key)", e.context);
  assert.equal(submitted, 1);
  e.context.key.ctrlKey = true;
  vm.runInContext("handleQuestionKey(key)", e.context);
  assert.equal(field.value, "甲\n丙");
  assert.equal(field.selectionStart, 2);
  for (const guard of ["repeat", "isComposing"]) {
    e.context.key[guard] = true;
    vm.runInContext("handleQuestionKey(key)", e.context);
    e.context.key[guard] = false;
  }
  assert.equal(field.value, "甲\n丙");
  e.context.key.ctrlKey = false;
  field.value = " \n ";
  vm.runInContext("handleQuestionKey(key)", e.context);
  assert.equal(submitted, 1);
  field.value = "有问题";
  vm.runInContext("pending = {}; handleQuestionKey(key); pending = null;", e.context);
  assert.equal(submitted, 1);
  field.matches = () => false;
  vm.runInContext("handleQuestionKey(key)", e.context);
  assert.equal(submitted, 1);
}
console.log("Question keyboard passed: Enter submit, Ctrl+Enter selection replacement/caret, IME, repeat, blank, busy and field isolation. DOM doubles only.");
