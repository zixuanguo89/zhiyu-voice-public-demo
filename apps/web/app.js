const $ = (id) => document.getElementById(id);
const label = {call:"打电话", records:"通话记录", booking:"预约后台"};
const states = {VERIFYING:"等待身份确认",COLLECTING:"收集需求",OFFERING:"选择时段",CONFIRMING:"等待确认",
  EXECUTING:"正在办理",RECONCILING:"结果核实中",COMPLETED:"办理完成",HANDOFF:"已登记人工",CLOSED:"通话已结束"};
const kinds = {"session.disclosed":"AI 身份与来意说明","user.received":"用户发言","assistant.replied":"客服回复",
  "identity.verified":"身份核验通过","identity.rejected":"核验未通过","appointment.read":"查询预约",
  "bank.guidance":"说明到店办理须知",
  "intent.updated":"预约意图更新","confirmation.offered":"复述并请求确认","confirmation.revoked":"原确认已失效",
  "confirmation.accepted":"用户明确确认","operation.prepared":"保存操作请求","operation.sent":"提交预约操作",
  "operation.unknown":"等待核实回执","operation.reconciled":"查询原操作回执","operation.receipt":"业务回执",
  "result.written":"结构化回写","handoff.created":"登记人工工单","handoff.updated":"更新人工工单",
  "session.closed":"结束通话","session.written":"会话结果回写"};
let credential = JSON.parse(sessionStorage.getItem("zhiyu") || "null");
let snapshot, adminToken = "", view = "call", features = {}, sent = false, transport, pendingRefresh;
let chatKey = "", toastTimer;
let dialing = false, hangingUp = false, voiceDuration = 0;
const deskToken = localStorage.getItem("zhiyu.desk") ||
  "desk_" + (crypto.randomUUID() + crypto.randomUUID()).replaceAll("-","");
localStorage.setItem("zhiyu.desk",deskToken);
let selectedCall, recordCursor = 0, recordEvents = [], recordLoading, bookingLoading;
let callListKey = "", bookingKey = "", nextBefore = null, olderBefore, olderCalls = [];

function node(tag, value, classes) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  if (classes) el.className = classes;
  return el;
}
function notify(text) {
  $("notice").hidden = false;
  $("notice").textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("notice").hidden = true; }, 4500);
}
async function api(path, {method="GET", token="", body, headers={}} = {}) {
  const response = await fetch(path, {method, headers: {
    ...(body === undefined ? {} : {"Content-Type":"application/json"}),
    ...(token ? {Authorization:"Bearer " + token} : {}), ...headers
  }, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.code || data.detail || "服务暂不可用");
  return data;
}
function session(path="", options={}) {
  if (!credential) throw new Error("请先发起会话");
  return api("/api/sessions/" + credential.session_id + path, {token:credential.token, ...options});
}
function admin(path, options={}) {
  return api("/api/admin" + path, {token:adminToken, ...options});
}
function desk(path, options={}) {
  return api(path,{...options, headers:{"X-Desk-Token":deskToken,...options.headers}});
}
function chip(el, text, tone="") {
  el.textContent = text;
  el.className = "chip" + (tone ? " " + tone : "");
}
function button(text, run) {
  const b = node("button", text);
  b.type = "button";
  b.addEventListener("click", run);
  return b;
}
function go(next, callId) {
  view = next;
  Object.keys(label).forEach((key) => { $(key).hidden = key !== view; });
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $("breadcrumb").textContent = "工作台 / " + label[view];
  if (view === "records") {
    if (callId) selectCall(callId);
    loadRecords().catch((e) => notify(e.message));
  }
  if (view === "booking") loadBooking().catch((e) => notify(e.message));
}
function updatePartial(text) {
  $("partial").textContent = text;
  $("partial").hidden = !text;
  $("record-partial").textContent = text;
  $("record-partial").hidden = !text || selectedCall !== credential?.session_id;
}
document.querySelectorAll("[data-view]").forEach((b) => b.onclick = () => go(b.dataset.view));

function formatSlot(slot) {
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",month:"short",day:"numeric",
    hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(new Date(slot.start));
}
function renderClock() {
  const running = transport?.readyAt == null ? 0 : performance.now() - transport.readyAt;
  const seconds = Math.floor((voiceDuration + running) / 1000);
  const time = [Math.floor(seconds / 60), seconds % 60].map((part) => String(part).padStart(2,"0")).join(":");
  if ($("call-clock").textContent !== time) $("call-clock").textContent = time;
}
function renderDialer() {
  const usable = snapshot && !snapshot.closed;
  const online = transport?.readyAt != null;
  const muted = online && transport.microphoneMuted;
  const phase = hangingUp ? "connecting" : online ? "connected" : dialing || transport ? "connecting"
    : usable ? "text" : snapshot?.closed ? "ended" : "idle";
  const caption = hangingUp ? "正在挂断" : online ? muted ? "已接通 · 静音" : "已接通"
    : transport ? "正在接通语音" : dialing ? "正在呼出"
    : usable ? "文字通话中" : snapshot?.closed ? "通话已结束" : "待呼出";
  $("phone-face").dataset.phase = phase;
  if ($("call-state").textContent !== caption) $("call-state").textContent = caption;
  $("dial-controls").hidden = Boolean(usable);
  $("call-controls").hidden = !usable;
  $("start").disabled = dialing || !features.microphone;
  $("start-label").textContent = snapshot?.closed ? "重新呼出" : "呼出电话";
  $("text-start").disabled = dialing;
  $("close").disabled = !usable || hangingUp;
  $("mic").disabled = !usable || dialing || hangingUp || !features.microphone || Boolean(transport && !online);
  $("mic-label").textContent = online ? muted ? "取消静音" : "静音" : "接通语音";
  $("mic").setAttribute("aria-pressed", String(Boolean(muted)));
  renderClock();
}
function render(s) {
  snapshot = s;
  const usable = s && !s.closed;
  chip($("stage"), s ? states[s.state] || "通话中" : "未开始", s?.pending ? "warn" : "");
  $("call-meta").textContent = s ? `会话 ${s.session_id.slice(-8)} · ${s.turn} 轮用户发言` : "尚未呼出";
  $("open-current-record").disabled = !s;
  const reply = s?.reply || "接通后，这里会显示客服的最新回复。";
  if ($("call-response").textContent !== reply) $("call-response").textContent = reply;
  renderDialer();
  $("compose-panel").hidden = !usable;
  $("send").disabled = !usable || sent;
  renderCancelActions(s);
  $("utterance").disabled = !usable;
  const prompts = {name:"例如：我叫林小满",phone:"输入 11 位演示手机号",confirm:"例如：是我本人"};
  $("utterance").placeholder = prompts[s?.intake_stage] || "输入消息";
}
function renderCancelActions(s) {
  const available = Boolean(s && !s.closed && s.offer?.action === "cancel");
  $("cancel-actions").hidden = !available;
  $("confirm-cancel").disabled = $("keep-appointment").disabled = !available || sent;
}
function renderChat(events) {
  const lines = events.filter((event) => ["session.disclosed","user.received","assistant.replied"].includes(event.event_type));
  const key = lines.at(-1)?.event_id;
  if (key === chatKey) return;
  chatKey = key;
  $("messages").replaceChildren(...lines.map((event) => {
    const user = event.event_type === "user.received";
    const bubble = node("div",undefined,"message" + (user ? " user" : ""));
    bubble.append(node("small",user ? "客户" : "智语 · AI 客服"),node("p",event.payload.text || event.payload.reply));
    return bubble;
  }));
  $("messages").scrollTop = $("messages").scrollHeight;
}
async function refresh() {
  if (pendingRefresh || !credential) return pendingRefresh;
  const id = credential.session_id;
  pendingRefresh = (async () => {
    const s = await session();
    if (id !== credential?.session_id || s.turn < (snapshot?.turn || 0)) return;
    render(s);
    if (s.closed && transport) stopVoice();
  })().finally(() => { pendingRefresh = null; });
  return pendingRefresh;
}
async function start(withVoice) {
  if (dialing || (snapshot && !snapshot.closed)) return;
  dialing = true;
  voiceDuration = 0;
  renderDialer();
  const call = withVoice ? new Voice() : null;
  try {
    if (call) call.prepare();
    const data = await desk("/api/sessions",{method:"POST",body:{
      fault:$("fault").value || null,scenario:"bank_card",customer_intake:true
    }});
    if (pendingRefresh) await pendingRefresh.catch(() => {});
    credential = {session_id:data.session_id,token:data.token};
    sessionStorage.setItem("zhiyu",JSON.stringify(credential));
    render(data.snapshot);
    await refresh();
    if (call) await connectVoice(call);
    else {
      $("mic-status").textContent = "可发送文字，或接通语音继续办理。";
      $("compose-panel").open = true;
      $("utterance").focus();
    }
  } catch (e) {
    if (call) await call.close();
    notify(e.message);
  } finally {
    dialing = false;
    renderDialer();
  }
}
async function send(text) {
  if (!snapshot || snapshot.closed || sent || !text.trim()) return;
  sent = true;
  $("send").disabled = true;
  renderCancelActions(snapshot);
  transport?.interruptPlayback();
  try {
    const event_id = "evt_" + crypto.randomUUID().replaceAll("-","");
    const result = await session("/turns",{method:"POST",body:{text,event_id,expected_turn:snapshot.turn}});
    if (!result.processed) notify("会话已更新，请查看当前进度后重试。");
    await refresh();
  } catch (e) { notify(e.message); }
  finally {
    sent = false;
    $("send").disabled = !snapshot || snapshot.closed;
    renderCancelActions(snapshot);
  }
}
$("compose").onsubmit = (event) => {
  event.preventDefault();
  const text = $("utterance").value;
  $("utterance").value = "";
  send(text);
};
$("start").onclick = () => start(true);
$("text-start").onclick = () => start(false);
$("open-current-record").onclick = () => go("records",credential?.session_id);
$("confirm-cancel").onclick = () => send("确认取消");
$("keep-appointment").onclick = () => send("保留原来的预约");
$("close").onclick = async () => {
  if (hangingUp) return;
  hangingUp = true;
  renderDialer();
  try {
    await stopVoice();
    await session("/close",{method:"POST"});
    await refresh();
    notify("通话已结束，对话和结果已存入通话记录。");
  } catch (e) { notify(e.message); }
  finally {
    hangingUp = false;
    renderDialer();
  }
};

function renderQuality(report) {
  const status = {PASS:"通过",REVIEW:"待复核",FAIL:"未通过"};
  const tone = (value) => value === "PASS" ? "" : value === "FAIL" ? "fail" : "warn";
  const box = $("quality");
  box.replaceChildren(node("span","综合 · " + status[report.status],"chip " + tone(report.status)),
    node("p",`回执覆盖率 ${report.coverage === null ? "不适用" : (report.coverage * 100).toFixed(0) + "%"}`,"hint"));
  for (const check of report.rules) {
    const item = node("div",undefined,"check");
    item.append(node("span",status[check.status],"chip " + tone(check.status)),node("p",check.reason,"hint"));
    check.event_ids.forEach((id) => {
      const link = node("a",id.slice(-6));
      link.href = "#" + id;
      link.onclick = () => { const row = $(id); if (row) row.open = true; };
      item.append(link);
    });
    box.append(item);
  }
  box.append(node("p",`语义评审 · ${status[report.semantic.status]} · ${report.semantic.reason}`,"hint"));
}
function formatStamp(timestamp) {
  return new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"short",day:"numeric",
    hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(new Date(timestamp));
}
function selectCall(id) {
  if (selectedCall === id) return;
  selectedCall = id;
  recordCursor = 0;
  recordEvents = [];
  chatKey = "";
  $("record-title").textContent = "通话 " + id.slice(-8);
  $("record-meta").textContent = "正在载入通话记录…";
  $("record-status").hidden = true;
  $("record-evidence").hidden = true;
  $("record-evidence").open = false;
  $("events").replaceChildren();
  $("quality").replaceChildren();
  $("messages").replaceChildren();
  updatePartial($("partial").hidden ? "" : $("partial").textContent);
}
function recordBadge(call) {
  if (!call.closed) return "通话中";
  return {completed:"已办结",pending:"待核实",handed_off:"已转人工",not_completed:"未办成",
    consulted:"咨询结束",unverified:"未确认身份"}[call.result?.status] || "已挂断";
}
function customerLabel(record) {
  return record.customer_name && record.phone_tail ? `${record.customer_name} · 尾号 ${record.phone_tail}` : "";
}
function appendEvidence(event) {
  const item = node("details");
  item.id = event.event_id;
  const heading = node("summary");
  heading.append(node("span",String(event.authoritative_sequence),"chip"),
    node("strong",kinds[event.event_type] || event.event_type),
    node("small",`第 ${event.turn_id} 轮 · ${formatStamp(event.occurred_at)}`));
  item.append(heading,node("pre",JSON.stringify(event.payload,null,2)));
  $("events").append(item);
}
async function loadQuality() {
  if (!selectedCall || !$("record-evidence").open) return;
  const id = selectedCall;
  $("semantic").disabled = !adminToken;
  const report = await desk("/api/desk/calls/" + id + "/quality");
  if (id === selectedCall) renderQuality(report);
}
async function loadRecords() {
  if (recordLoading) return recordLoading;
  recordLoading = (async () => {
    const page = await desk("/api/desk/calls");
    if (view !== "records") return;
    nextBefore = olderBefore === undefined ? page.next_before : olderBefore;
    const records = [...new Map([...page.items,...olderCalls].map((item) => [item.session_id,item])).values()];
    if (!selectedCall && records.length) selectCall(records[0].session_id);
    const listKey = JSON.stringify([selectedCall,records]);
    if (listKey !== callListKey) {
      callListKey = listKey;
      const rows = records.map((call) => {
        const id = call.session_id;
        const row = button("", () => {
          selectCall(id);
          loadRecords().catch((e) => notify(e.message));
        });
        row.className = "record-row";
        row.setAttribute("aria-pressed", String(id === selectedCall));
        row.append(node("strong","通话 " + id.slice(-8)),node("span",recordBadge(call),"chip"),
          node("small",[customerLabel(call),formatStamp(call.created_at),call.turns + " 轮"].filter(Boolean).join(" · ")));
        return row;
      });
      $("record-list").replaceChildren(...(rows.length ? rows : [node("p","拨出第一通电话后，记录会出现在这里。","hint")]));
    }
    $("older-records").hidden = nextBefore == null;
    if (!selectedCall) return;
    const id = selectedCall;
    const data = await desk(`/api/desk/calls/${id}?after=${recordCursor}`);
    if (selectedCall !== id || view !== "records") return;
    const {call, events} = data;
    $("record-meta").textContent = [customerLabel(call),formatStamp(call.created_at),
      call.turns + " 轮用户发言"].filter(Boolean).join(" · ");
    chip($("record-status"),recordBadge(call),call.result?.status === "pending" ? "warn" : "");
    $("record-status").hidden = false;
    $("record-evidence").hidden = false;
    if (!events.length) return;
    recordCursor = events.at(-1).authoritative_sequence;
    recordEvents.push(...events);
    renderChat(recordEvents);
    events.forEach(appendEvidence);
    await loadQuality();
  })().finally(() => { recordLoading = null; });
  return recordLoading;
}
$("record-evidence").ontoggle = () => loadQuality().catch((e) => notify(e.message));
$("older-records").onclick = async () => {
  const cursor = nextBefore;
  if (cursor == null) return;
  $("older-records").disabled = true;
  try {
    const page = await desk("/api/desk/calls?before=" + cursor);
    olderCalls.push(...page.items);
    olderBefore = page.next_before;
    await loadRecords();
  } catch (e) { notify(e.message); }
  finally { $("older-records").disabled = false; }
};
$("semantic").onclick = async () => {
  const id = selectedCall;
  $("semantic").disabled = true;
  try {
    const report = await desk("/api/sessions/" + id + "/quality/semantic",
      {method:"POST",headers:{"X-Admin-Token":adminToken}});
    if (id === selectedCall) renderQuality(report);
  }
  catch (e) { notify(e.message); }
  finally { $("semantic").disabled = !adminToken; }
};
function renderTable(id, columns, rows, empty) {
  if (!rows.length) {
    $(id).replaceChildren(node("p",empty,"hint"));
    return;
  }
  const table = node("table",undefined,"booking-table");
  const head = node("thead"), names = node("tr"), body = node("tbody");
  columns.forEach((column) => names.append(node("th",column)));
  head.append(names);
  rows.forEach((cells) => {
    const row = node("tr");
    cells.forEach((value) => {
      const cell = node("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      row.append(cell);
    });
    body.append(row);
  });
  table.append(head,body);
  $(id).replaceChildren(table);
}
function openRecordButton(id) {
  const link = button("通话 " + id.slice(-8), () => go("records",id));
  link.className = "link-btn";
  return link;
}
function activityText(event) {
  const p = event.payload;
  const params = p.params || p;
  if (event.event_type === "appointment.read") return p.appointment.id + " · 当前 " + p.appointment.slot;
  if (event.event_type === "identity.verified") return "模拟客户 " + p.scope;
  if (event.event_type === "bank.guidance") return {
    documents:"到店材料",branch:"网点信息",review:"现场审核",scope:"可办理业务",
  }[p.topic] || "";
  if (p.ticket) return "工单 " + p.ticket.slice(-8);
  if (p.result?.slot) return p.result.id + " · " + p.result.slot + " · 版本 " + p.result.version;
  if (params.appointment_id) return params.appointment_id + (params.target_slot ? " → " + params.target_slot : "");
  if (p.operation_id) return "操作 " + p.operation_id.slice(-8);
  return p.code || p.reason || "";
}
function renderBooking(data) {
  const operationStates = {PREPARED:"待提交",SENT:"已提交",UNKNOWN:"待核实",SUCCEEDED:"成功",REJECTED:"拒绝"};
  renderTable("booking-table",["预约","业务与网点","客户","当前时段","状态","版本"],data.appointments.map((a) => [
    a.id,a.branch ? a.service + " · " + a.branch : a.service,
    customerLabel(a) || (a.branch ? "演示客户 " + a.owner : a.owner),a.slot + " · " + formatSlot(a),
    node("span",a.status === "active" ? "有效" : "已取消","chip"),a.version
  ]),"首次呼出后会创建本工作台的预约数据。");
  $("booking-slots").replaceChildren(...(data.slots.length ? data.slots.map((slot) => {
    const row = node("div",undefined,"stock-row");
    row.append(node("strong",slot.id),node("span",formatSlot(slot) + (slot.branch ? " · 银行网点" : "")),
      node("span",slot.available ? "可预约" : "已占用",slot.available ? "chip" : "chip warn"));
    return row;
  }) : [node("p","暂无时段。","hint")]));
  renderTable("booking-operations",["操作","预约","请求","处理状态","发送次数","回执","来源"],data.operations.map((op) => [
    op.operation_id.slice(-8),op.request.appointment_id,
    op.request.action === "cancel" ? "取消预约" : (op.request.action === "book" ? "新建预约 · " : "改至 ") + op.request.target_slot,
    node("span",operationStates[op.state] || op.state,op.state === "SUCCEEDED" ? "chip" : "chip warn"),
    op.attempts,op.receipt?.receipt_id?.slice(-8) || op.receipt?.code || "待返回",openRecordButton(op.session_id)
  ]),"AI 尚未提交预约操作。");
  $("booking-activity").replaceChildren(...(data.activity.length ? data.activity.map((event) => {
    const row = node("div",undefined,"activity-row");
    row.append(node("small",formatStamp(event.occurred_at)),node("strong",kinds[event.event_type] || event.event_type),
      node("span",activityText(event)),openRecordButton(event.session_id));
    return row;
  }) : [node("p","通话中的业务动作会显示在这里。","hint")]));
  renderTable("booking-handoffs",["工单","跟进事项","来源"],data.handoffs.map((item) => [
    item.ticket.slice(-8),item.follow_up,openRecordButton(item.session_id)
  ]),"暂时没有人工工单。");
}
async function loadBooking() {
  if (bookingLoading) return bookingLoading;
  bookingLoading = (async () => {
    const data = await desk("/api/desk/booking");
    const key = JSON.stringify(data);
    if (key !== bookingKey && view === "booking") {
      bookingKey = key;
      renderBooking(data);
    }
  })().finally(() => { bookingLoading = null; });
  return bookingLoading;
}
$("admin-form").onsubmit = async (event) => {
  event.preventDefault();
  adminToken = $("admin-key").value.trim();
  $("admin-key").value = "";
  try { await loadPolicies(); }
  catch (e) { adminToken = ""; $("admin-content").hidden = true; notify(e.message === "FORBIDDEN" ? "控制台口令不正确。" : e.message); }
};
async function loadPolicies() {
  const data = await admin("/policies");
  $("admin-content").hidden = false;
  $("active-policy").textContent = "当前策略 " + data.active;
  $("policies").replaceChildren(...data.policies.map((policy) => {
    const row = node("details",undefined,"version");
    const summary = node("summary",`${policy.id} · ${policy.status}`);
    const evidence = policy.evidence;
    row.append(summary,node("p",policy.config.clarification,"hint"));
    if (evidence.reasons) row.append(node("p",evidence.reasons.join(" · "),"hint"));
    if (policy.parent) row.append(node("pre",JSON.stringify({parent:policy.parent,...evidence},null,2)));
    if (policy.status === "eligible") row.append(button("发布候选",() => switchPolicy(policy.id,"publish")));
    if (policy.parent && policy.id === data.active) row.append(button("回滚版本",() => switchPolicy(policy.id,"rollback")));
    return row;
  }));
  $("history").replaceChildren(...(data.history.length ? data.history.map((item) => node("p",
    `${new Date(item.at*1000).toLocaleString("zh-CN")} · ${item.action} · ${item.policy}`,"hint")) :
    [node("p","暂无版本操作。","hint")]));
}
async function switchPolicy(id, action) {
  try { await admin("/policies/" + id + "/" + action,{method:"POST"}); await loadPolicies(); }
  catch (e) { notify(e.message); }
}
async function runJob(kind) {
  $("regression").disabled = $("evolve").disabled = true;
  $("job-result").hidden = true;
  try {
    const {job} = await admin("/jobs",{method:"POST",body:{kind}});
    $("job-status").textContent = "任务运行中";
    for (let attempt=0; attempt<100; attempt++) {
      await new Promise((resolve) => setTimeout(resolve,650));
      const result = await admin("/jobs/" + job);
      if (result.status === "done" || result.status === "failed") {
        $("job-status").textContent = result.status === "done" ? "任务结束 · " + job.slice(-8) : "任务未通过 · " + job.slice(-8);
        $("job-result").textContent = JSON.stringify(result.detail,null,2);
        $("job-result").hidden = false;
        await loadPolicies();
        return;
      }
    }
    $("job-status").textContent = "任务仍在后台运行 · " + job.slice(-8);
  } catch (e) { notify(e.message); }
  finally { $("regression").disabled = $("evolve").disabled = false; }
}
$("regression").onclick = () => runJob("regression");
$("evolve").onclick = () => runJob("evolution");

class Voice {
  constructor() {
    this.sources = new Set();
    this.generation = 0;
    this.ignoreThrough = -1;
    this.sequence = 0;
    this.next = 0;
    this.active = false;
    this.loudFrames = 0;
    this.endOfSpeech = null;
    this.speaking = false;
    this.firstPlayTimer = null;
    this.closed = false;
    this.readyAt = null;
    this.microphoneMuted = false;
    this.heardInput = false;
    this.nextMeterAt = 0;
  }
  prepare() {
    if (this.context) return;
    this.context = new AudioContext();
    this.resumed = this.context.resume();
    this.resumed.catch(() => {});
  }
  async start() {
    this.prepare();
    await this.resumed;
    if (this.closed) return;
    this.stream = await navigator.mediaDevices.getUserMedia({audio:{
      channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true
    }});
    if (this.closed) {
      this.stream.getTracks().forEach((track) => track.stop());
      return;
    }
    this.track = this.stream.getAudioTracks()[0];
    $("mic-input").hidden = false;
    $("mic-input-hint").textContent = "请说一句话，确认音量条随声音变化。";
    this.startInputMeter();
    this.connectTimeout = setTimeout(() => {
      if (transport !== this || this.readyAt !== null) return;
      $("mic-status").textContent = "语音接通超时。可点「接通语音」重试，或继续文字办理。";
      stopVoice(false);
    },15000);
    if (features.voice_transport === "websocket") {
      await this.startPublicVoice();
      return;
    }
    this.peer = new RTCPeerConnection({iceServers:[]});
    this.stream.getAudioTracks().forEach((track) => this.peer.addTrack(track,this.stream));
    this.channel = this.peer.createDataChannel("voice");
    this.channel.binaryType = "arraybuffer";
    this.channel.onmessage = ({data}) => typeof data === "string" ? this.message(JSON.parse(data)) : this.play(data);
    this.channel.onopen = () => this.channel.send("ready");
    this.peer.onconnectionstatechange = () => {
      if (transport === this && ["failed","disconnected"].includes(this.peer.connectionState)) {
        stopVoice();
        notify("语音连接已断开，可重新开启麦克风。");
      }
    };
    await this.peer.setLocalDescription(await this.peer.createOffer());
    if (this.peer.iceGatheringState !== "complete") {
      await new Promise((resolve) => {
        const finish = () => { if (this.peer.iceGatheringState === "complete") resolve(); };
        this.peer.addEventListener("icegatheringstatechange",finish);
        setTimeout(resolve,2500);
      });
    }
    if (this.closed) return;
    const answer = await session("/rtc",{method:"POST",body:{sdp:this.peer.localDescription.sdp,greet:!sessionStorage.getItem("voice-" + snapshot.session_id)}});
    if (this.closed) return;
    await this.peer.setRemoteDescription(answer);
    if (!this.closed) sessionStorage.setItem("voice-" + snapshot.session_id,"1");
  }
  async startPublicVoice() {
    await this.context.audioWorklet.addModule("/assets/voice-capture.js");
    if (this.closed) return;
    const {session_id,token} = credential;
    const key = "voice-" + session_id;
    const endpoint = new URL(`/api/sessions/${session_id}/voice`, location.href);
    endpoint.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = this.socket = new WebSocket(endpoint);
    socket.binaryType = "arraybuffer";
    socket.onmessage = ({data}) => typeof data === "string" ? this.message(JSON.parse(data)) : this.play(data);
    socket.onopen = () => {
      socket.send(JSON.stringify({token,greet:!sessionStorage.getItem(key)}));
      socket.send("ready");
    };
    socket.onclose = () => {
      if (transport !== this || this.closed) return;
      $("mic-status").textContent = "语音连接已结束，可重新连接或继续文字办理。";
      stopVoice(false);
    };
    const source = this.context.createMediaStreamSource(this.stream);
    this.capture = new AudioWorkletNode(this.context,"voice-capture");
    this.capture.port.onmessage = ({data}) => {
      if (socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 8192) socket.send(data);
    };
    source.connect(this.capture).connect(this.context.destination);
  }
  sendControl(value) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(value);
    else if (this.channel?.readyState === "open") this.channel.send(value);
  }
  startInputMeter() {
    const input = this.context.createMediaStreamSource(this.stream);
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 256;
    const silent = this.context.createGain();
    silent.gain.value = 0;
    input.connect(this.analyser).connect(silent).connect(this.context.destination);
    this.samples = new Float32Array(this.analyser.fftSize);
    this.timer = setInterval(() => this.vad(),25);
  }
  message(data) {
    if (transport !== this) return;
    if (data.type === "ready") {
      clearTimeout(this.connectTimeout);
      if (this.socket) sessionStorage.setItem("voice-" + snapshot.session_id,"1");
      this.readyAt ??= performance.now();
      $("mic-status").textContent = "麦克风已连接，可直接交谈。";
      renderDialer();
    }
    if (data.type === "failure") notify(data.message);
    if (data.type === "asr_ended") {
      $("mic-status").textContent = "识别已结束，可重新连接或改用文本。";
      stopVoice(false);
    }
    if (data.type === "partial") {
      updatePartial(data.text ? "识别中 · " + data.text : "");
    }
    if (data.type === "state") refresh().catch((e) => notify(e.message));
    if (data.type === "cut") {
      this.generation = data.generation;
      this.flush();
    }
    if (data.type === "speech") {
      this.generation = data.generation;
      this.sequence = 0;
      this.active = this.generation > this.ignoreThrough;
    }
    if (data.type === "speech_end" && data.generation === this.generation) this.active = false;
  }
  play(data) {
    if (data.byteLength < 10 || data.byteLength % 2) return;
    const header = new DataView(data);
    const generation = header.getUint32(0,true), sequence = header.getUint32(4,true);
    if (!this.active || generation !== this.generation || sequence !== this.sequence) return;
    this.sequence++;
    const length = (data.byteLength-8)/2;
    const buffer = this.context.createBuffer(1,length,24000);
    const output = buffer.getChannelData(0);
    for (let i=0;i<length;i++) output[i] = header.getInt16(8+2*i,true)/32768;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const start = Math.max(this.next,this.context.currentTime + .035);
    this.sources.add(source);
    source.onended = () => { source.disconnect(); this.sources.delete(source); };
    source.start(start);
    this.next = start + buffer.duration;
    if (sequence === 0 && this.endOfSpeech !== null) {
      const end = this.endOfSpeech;
      this.endOfSpeech = null;
      this.firstPlayTimer = setTimeout(() => this.timing("first_play",performance.now()-end),
        (start-this.context.currentTime)*1000);
    }
  }
  timing(kind, ms) {
    if (ms >= 0) this.sendControl(JSON.stringify({type:"timing",kind,ms:Math.round(ms)}));
  }
  vad() {
    this.analyser.getFloatTimeDomainData(this.samples);
    const rms = Math.sqrt(this.samples.reduce((sum,x) => sum+x*x,0)/this.samples.length);
    this.updateMeter(rms);
    if (rms > .025) {
      if (this.loudFrames++ === 0) this.onset = performance.now();
      if (this.loudFrames === 3) {
        this.speaking = true;
        if (this.sources.size || this.active) {
          this.interruptPlayback();
          this.timing("barge_stop",performance.now()-this.onset);
        }
      }
    } else {
      if (this.speaking) this.endOfSpeech = performance.now();
      this.loudFrames = 0;
      this.speaking = false;
    }
  }
  updateMeter(rms) {
    if (this.closed) return;
    const now = performance.now();
    if (rms > .004) this.heardInput = true;
    if (now < this.nextMeterAt) return;
    this.nextMeterAt = now + 100;
    const meter = $("mic-level");
    meter.value = Math.max(Math.min(1, rms / .12), meter.value * .65);
    const hint = $("mic-input-hint");
    let message = "请说一句话，确认音量条随声音变化。";
    if (this.microphoneMuted) {
      meter.value = 0;
      message = "您已静音，仍可以听到客服播报。";
    } else if (this.track.muted || this.track.readyState === "ended") {
      message = "浏览器已暂停麦克风输入，请重新开启麦克风。";
    } else if (this.heardInput) {
      message = "已检测到麦克风输入。";
    } else if (this.readyAt !== null && now - (this.unmutedAt ?? this.readyAt) >= 4000) {
      message = "还没检测到声音。如果说话时音量条始终为零，请检查 Windows 麦克风静音。";
    }
    if (hint.textContent !== message) hint.textContent = message;
  }
  setMicrophoneMuted(muted) {
    this.microphoneMuted = muted;
    this.track.enabled = !muted;
    this.heardInput = false;
    this.unmutedAt = performance.now();
    this.updateMeter(0);
    renderDialer();
  }
  interruptPlayback() {
    this.ignoreThrough = this.generation;
    this.flush();
    this.sendControl("barge");
  }
  flush() {
    clearTimeout(this.firstPlayTimer);
    for (const source of this.sources) source.stop();
    this.sources.clear();
    this.active = false;
    this.next = 0;
  }
  async close() {
    this.closed = true;
    clearInterval(this.timer);
    clearTimeout(this.connectTimeout);
    this.flush();
    this.peer?.close();
    this.socket?.close();
    this.capture?.port.close();
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.context && this.context.state !== "closed") await this.context.close();
  }
}
async function stopVoice(reset=true) {
  const old = transport;
  if (old?.readyAt != null) voiceDuration += performance.now() - old.readyAt;
  transport = null;
  $("mic-input").hidden = true;
  $("mic-level").value = 0;
  if (reset) $("mic-status").textContent = features.microphone ? "麦克风已关闭。" : "云语音待配置，当前可使用文本。";
  updatePartial("");
  renderDialer();
  if (old) await old.close();
}
async function connectVoice(call = new Voice()) {
  if (hangingUp || !snapshot || snapshot.closed || transport) {
    await call.close();
    return;
  }
  transport = call;
  renderDialer();
  $("mic-status").textContent = "正在建立语音连接…";
  try { await call.start(); }
  catch (e) {
    if (transport !== call) return;
    await stopVoice(false);
    $("mic-status").textContent = e.name === "NotAllowedError"
      ? "浏览器尚未授予麦克风权限，请在地址栏允许后重试。"
      : "麦克风连接失败，请检查设备后重试。";
    notify("麦克风连接失败 · " + e.message);
  }
}
$("mic").onclick = () => {
  if (transport?.readyAt != null) transport.setMicrophoneMuted(!transport.microphoneMuted);
  else if (!transport) connectVoice();
};
async function init() {
  try {
    features = await api("/health");
    chip($("environment"),features.microphone ? "云语音已配置 · 业务沙箱" : "文本模式 · 业务沙箱");
    $("fault").disabled = !features.demo_faults;
    if (features.public_demo) {
      document.querySelector(".strategy-panel").hidden = true;
      $("public-notice").hidden = false;
    }
    $("mic-status").textContent = features.microphone ? "可开启麦克风进行语音通话。" : "云语音待配置，当前可使用文本。";
    if (credential) {
      try {
        await desk("/api/desk/calls/" + credential.session_id + "/link",{method:"POST",token:credential.token});
        await refresh();
      }
      catch { credential = null; sessionStorage.removeItem("zhiyu"); render(null); }
    }
    renderDialer();
  } catch { chip($("environment"),"服务不可用","fail"); }
  setInterval(() => {
    if (credential && !sent) refresh().catch(() => {});
    if (view === "records") loadRecords().catch(() => {});
    if (view === "booking") loadBooking().catch(() => {});
  },850);
  setInterval(renderClock,250);
}
init();
