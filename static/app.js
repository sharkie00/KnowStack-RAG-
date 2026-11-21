const state = {
  data: null,
  last: null,
  view: "chat",
  busy: false,
  eval: null,
  courseId: null,
  editingConcept: null,
  fillingConcept: null,
  notes: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function pct(v) {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    throw new Error(`服务返回异常（${res.status}）`);
  }
  if (!res.ok) throw new Error(data?.error || `请求失败（${res.status}）`);
  return data;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ---------------- state ---------------- */

async function loadState(courseId) {
  const qs = courseId ? `?course=${encodeURIComponent(courseId)}` : "";
  state.data = await api(`/api/state${qs}`);
  state.courseId = state.data.courseId;
  renderState();
}

function renderState() {
  const d = state.data;
  if (!d) return;
  $("#courseName").textContent = d.course;
  $("#brandSubtitle").textContent = `${d.course} · 笔记 RAG Demo`;
  document.title = `知栈 KnowStack · ${d.course}笔记 RAG Demo`;
  $("#input").placeholder = `问一个${d.course}问题，例如：${d.suggestions[0] || "这个概念是什么？"}`;
  const select = $("#courseSelect");
  select.innerHTML = (d.courses || [])
    .map(
      (c) =>
        `<option value="${escapeHtml(c.id)}" ${c.id === d.courseId ? "selected" : ""}>${escapeHtml(c.name)}</option>`
    )
    .join("");
  select.onchange = () => {
    window.location.href = `/?course=${encodeURIComponent(select.value)}`;
  };
  $("#courseStats").innerHTML =
    `${(d.courses || []).length} 门课程 · ${d.stats.docs} 份笔记 · ${d.stats.chunks} 个片段<br />${d.stats.concepts} 个概念 · 覆盖率 ${pct(d.stats.coverage)}`;
  $("#overallCov").textContent = pct(d.stats.coverage);
  updateMode(d.mode);

  const list = $("#conceptList");
  if (!d.concepts.length) {
    list.innerHTML = `<div class="side-note">还没有概念。导入一份带 <b>## 小节标题</b> 的 Markdown 笔记，系统会自动抽取概念。</div>`;
  } else {
    list.innerHTML = d.concepts
      .map((c) => {
        const p = Math.round(c.coverage * 100);
        const cls = p < 40 ? "low" : p < 70 ? "warn" : "";
        return `<div class="concept-row" data-concept="${escapeHtml(c.name)}" title="追问「${escapeHtml(c.name)}」">
          <span class="concept-name">${escapeHtml(c.name)}</span>
          <span class="concept-pct">${p}%</span>
          <span class="concept-bar"><i class="${cls}" style="width:${p}%"></i></span>
        </div>`;
      })
      .join("");
    $$(".concept-row").forEach((row) => {
      row.addEventListener("click", () => {
        switchView("chat");
        ask(`请解释「${row.dataset.concept}」，并指出我的笔记里缺什么。`);
      });
    });
  }

  const sug = $("#suggestions");
  sug.innerHTML = d.suggestions
    .map((s) => `<button class="suggestion">${escapeHtml(s)}</button>`)
    .join("");
  $$(".suggestion").forEach((btn) => {
    btn.addEventListener("click", () => ask(btn.textContent));
  });

  const intro = $("#evalIntro");
  if (intro) {
    intro.textContent = d.evalCount
      ? `当前课程 ${d.evalCount} 道自建题，指标用于回归，不用于对外宣称效果。`
      : "当前课程还没有评测题；导入笔记后可在 eval/questions.json 里补充。";
  }

  const kicker = $("#welcomeKicker");
  if (kicker) kicker.textContent = `课程知识库 · ${d.stats.docs} 份笔记 · ${d.stats.chunks} 个片段`;
}

function updateMode(mode) {
  const pill = $("#modePill");
  pill.classList.remove("offline", "llm");
  if (mode === "llm") {
    pill.classList.add("llm");
    pill.textContent = "LLM 已连接";
  } else if (mode === "offline-fallback") {
    pill.classList.add("offline");
    pill.textContent = "LLM 失败 · 已降级";
  } else {
    pill.classList.add("offline");
    pill.textContent = "离线抽取";
  }
}

/* ---------------- markdown ---------------- */

function inline(s) {
  const safe = escapeHtml(s);
  return safe
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d+)\]/g, '<button class="cite" data-idx="$1">[$1]</button>');
}

function formatText(raw) {
  const lines = raw.split("\n");
  let html = "";
  let para = [];
  let inList = false;

  const flushPara = () => {
    if (para.length) {
      html += `<p>${para.map(inline).join("<br />")}</p>`;
      para = [];
    }
  };
  const closeList = () => {
    if (inList) {
      html += "</ul>";
      inList = false;
    }
  };

  for (const line of lines) {
    const t = line.trim();
    if (!t) {
      flushPara();
      closeList();
      continue;
    }
    const m = t.match(/^[-•]\s+(.*)$/);
    if (m) {
      flushPara();
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(m[1])}</li>`;
      continue;
    }
    closeList();
    para.push(t);
  }
  flushPara();
  closeList();
  return html;
}

function markdown(raw) {
  let out = "";
  let last = 0;
  const re = /```[^\n]*\n([\s\S]*?)(?:```|$)/g;
  let m;
  while ((m = re.exec(raw)) !== null) {
    out += formatText(raw.slice(last, m.index));
    out += `<pre><code>${escapeHtml(m[1])}</code></pre>`;
    last = re.lastIndex;
  }
  out += formatText(raw.slice(last));
  return out;
}

/* ---------------- chat ---------------- */

function addUser(text) {
  const el = document.createElement("div");
  el.className = "message user";
  el.innerHTML = `<div class="msg-body"><div class="bubble">${escapeHtml(text).replace(/\n/g, "<br />")}</div></div>`;
  $("#chat").appendChild(el);
  scrollChat();
}

function addAssistant(bodyHtml) {
  const el = document.createElement("div");
  el.className = "message assistant";
  el.innerHTML = `<div class="msg-avatar">知</div><div class="msg-body">${bodyHtml}</div>`;
  $("#chat").appendChild(el);
  scrollChat();
  return el;
}

function scrollChat() {
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

function renderConflict(c) {
  const claims = (c.claims || [])
    .map(
      (x) => `<div class="claim">
        <b>${escapeHtml(x.side || "来源")} · ${escapeHtml(x.title || "")}</b>
        ${escapeHtml(x.text || "")}
        <div class="src">权威度 ${x.authority ?? "—"}</div>
      </div>`
    )
    .join("");
  return `<div class="conflict">
    <div class="conflict-head">⚠ 检测到笔记与教材不一致</div>
    <div class="conflict-advice">${escapeHtml(c.advice || "")}</div>
    <div class="claims">${claims}</div>
  </div>`;
}

function renderGaps(gaps) {
  return gaps
    .slice(0, 2)
    .map(
      (g) => `<div class="gap-card"><b>缺口提示 · ${escapeHtml(g.concept)}</b><br />
        ${escapeHtml(g.reason || "")}。${escapeHtml(g.suggestion || "")}</div>`
    )
    .join("");
}

function renderAnswer(res) {
  const conf = res.confidence === "high" ? "高" : res.confidence === "medium" ? "中" : "低";
  const foot = `<div class="answer-foot">
      <span class="tag ${res.confidence === "low" ? "amber" : "green"}">置信度 ${conf}</span>
      <span class="tag">${res.citations.length} 条引用</span>
      <span class="tag">${res.latency_ms} ms</span>
      <span class="tag">${res.mode === "llm" ? "LLM 生成" : res.mode === "offline-fallback" ? "LLM 降级" : "离线抽取"}</span>
    </div>`;
  let html = `<div class="bubble">${markdown(res.answer)}${foot}</div>`;
  if (res.conflicts && res.conflicts.length) html += renderConflict(res.conflicts[0]);
  if (res.gaps && res.gaps.length) html += renderGaps(res.gaps);
  return html;
}

async function ask(question) {
  const q = String(question || "").trim();
  if (!q || state.busy) return;
  state.busy = true;
  $("#sendBtn").disabled = true;
  $("#input").value = "";
  $("#input").style.height = "auto";
  const welcome = $("#welcome");
  if (welcome) welcome.remove();
  addUser(q);
  const loading = addAssistant(
    `<div class="bubble"><span class="typing"><i></i><i></i><i></i></span> 正在检索笔记…</div>`
  );
  try {
    const res = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question: q, courseId: state.courseId }),
    });
    loading.remove();
    addAssistant(renderAnswer(res));
    state.last = res;
    renderEvidence(res.citations || []);
    updateMode(res.mode);
  } catch (e) {
    loading.remove();
    addAssistant(`<div class="bubble"><p>出错了：${escapeHtml(e.message)}</p></div>`);
  } finally {
    state.busy = false;
    $("#sendBtn").disabled = false;
    $("#input").focus();
  }
}

/* ---------------- evidence ---------------- */

function renderEvidence(citations) {
  $("#evidenceCount").textContent = citations.length;
  const list = $("#evidenceList");
  if (!citations.length) {
    list.innerHTML = `<div class="empty"><div class="empty-icon">∅</div>本次回答没有引用：可能是拒答，或只做了覆盖率诊断。</div>`;
    return;
  }
  list.innerHTML = citations
    .map((c) => {
      const isAuto = c.sourceType === "auto";
      const auth = isAuto ? "补全" : c.authority >= 3 ? "教材" : c.authority === 2 ? "讲义" : "笔记";
      return `<div class="evidence-card" id="ev-${c.index}">
        <div class="ev-top">
          <span class="idx">${c.index}</span>
          <span class="ev-title">${escapeHtml(c.title)}</span>
          <span class="auth ${c.authority >= 3 ? "textbook" : ""} ${isAuto ? "auto" : ""}">${auth}</span>
        </div>
        <div class="ev-sec">${escapeHtml(c.section || "")}</div>
        <blockquote class="ev-quote">${escapeHtml(c.quote || "")}</blockquote>
        <button class="link-btn">查看原文</button>
      </div>`;
    })
    .join("");

  $$("#evidenceList .link-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".evidence-card");
      const quote = card.querySelector(".ev-quote");
      quote.classList.toggle("open");
      btn.textContent = quote.classList.contains("open") ? "收起原文" : "查看原文";
    });
  });
}

function focusCitation(idx) {
  const card = document.getElementById(`ev-${idx}`);
  if (!card || !card.offsetParent) {
    const c = state.last?.citations?.find((x) => x.index === idx);
    if (c) toast(`[${idx}] ${c.title} · ${c.section}`);
    return;
  }
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1600);
}

/* ---------------- coverage ---------------- */

function renderCoverage() {
  if (!state.data) return;
  const overall = Math.round(state.data.stats.coverage * 100);
  const ring = $("#coverageRing");
  ring.style.setProperty("--pct", overall);
  $("#ringValue").textContent = `${overall}%`;

  if (!state.data.concepts.length) {
    $("#coverageGrid").innerHTML = `<div class="empty-state">
      <b>这门课还没有概念</b>
      导入一份带 <code>## 小节标题</code> 的 Markdown 笔记，系统会自动抽取概念并计算覆盖率。
      <div style="margin-top:14px"><button class="primary-btn" id="emptyUpload">＋ 导入笔记</button></div>
    </div>`;
    $("#emptyUpload").addEventListener("click", openUpload);
    return;
  }

  $("#coverageGrid").innerHTML = state.data.concepts
    .map((c) => {
      const dims = [
        ["定义", c.dims.definition, "概念是什么、边界在哪、关键性质是什么"],
        ["例子", c.dims.example, "一个具体场景、对照或运行示例"],
        ["推导", c.dims.derivation, "为什么成立、步骤、复杂度或可运行代码"],
        ["练习", c.dims.practice, "一道能作答的题，附答案要点"],
      ];
      const dimHtml = dims
        .map(
          ([label, on, tip]) => `<div class="dim ${on ? "on-label" : ""}" title="${escapeHtml(tip)}">
            <i class="${on ? "on" : ""}"></i>${label}
          </div>`
        )
        .join("");
      const missing = c.missing.length
        ? `<div class="missing">缺：${c.missing.map(escapeHtml).join("、")}</div>`
        : `<div class="missing none">四维齐全</div>`;
      return `<div class="cov-card">
        <div class="cov-head"><b>${escapeHtml(c.name)}</b><span>${Math.round(c.coverage * 100)}%</span></div>
        <div class="dims">${dimHtml}</div>
        ${missing}
        <div class="cov-suggestion">${escapeHtml(c.suggestion || "这块已经比较完整，可以做综合题。")}</div>
        <div class="cov-actions">
          <button class="ghost-btn" data-ask="${escapeHtml(c.name)}">追问</button>
          <button class="ghost-btn" data-edit="${escapeHtml(c.id)}" data-name="${escapeHtml(c.name)}">编辑笔记</button>
          <button class="ghost-btn" data-fill="${escapeHtml(c.id)}" data-name="${escapeHtml(c.name)}">补全</button>
        </div>
      </div>`;
    })
    .join("");

  $$("#coverageGrid [data-ask]").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchView("chat");
      ask(`请解释「${btn.dataset.ask}」，并指出我的笔记里缺什么。`);
    });
  });
  $$("#coverageGrid [data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => openNotes(btn.dataset.edit, btn.dataset.name));
  });
  $$("#coverageGrid [data-fill]").forEach((btn) => {
    btn.addEventListener("click", () => openFill(btn.dataset.fill, btn.dataset.name));
  });
}

/* ---------------- eval ---------------- */

function renderEval(result) {
  const m = result.metrics;
  if (!m || !m.count) {
    $("#metricGrid").innerHTML = `<div class="metric-card placeholder">当前课程还没有评测题，导入笔记后可在 eval/questions.json 中补充。</div>`;
    $("#evalTableWrap").innerHTML = "";
    return;
  }
  const cards = [
    ["通过率", pct(m.pass_rate), m.pass_rate >= 0.8],
    ["概念召回", pct(m.avg_recall), (m.avg_recall ?? 0) >= 0.8],
    ["关键词覆盖", pct(m.avg_keyword), (m.avg_keyword ?? 0) >= 0.7],
    ["引用有效", pct(m.citation_valid), m.citation_valid >= 0.9],
    ["拒答准确", pct(m.refusal_accuracy), (m.refusal_accuracy ?? 0) >= 0.8],
    ["平均延迟", `${m.avg_latency_ms} ms`, m.avg_latency_ms < 50],
  ];
  $("#metricGrid").innerHTML = cards
    .map(
      ([label, value, good]) =>
        `<div class="metric-card"><div class="label">${label}</div><div class="value ${good ? "good" : ""}">${value}</div></div>`
    )
    .join("");

  const rows = result.rows
    .map(
      (r) => `<tr>
        <td>${r.id}</td>
        <td>${escapeHtml(r.question)}</td>
        <td>${r.recall == null ? "—" : pct(r.recall)}</td>
        <td>${r.keywords == null ? "—" : pct(r.keywords)}</td>
        <td>${r.citation ? "✓" : "—"}</td>
        <td><span class="badge ${r.pass ? "pass" : "fail"}">${r.pass ? "PASS" : "FAIL"}</span></td>
      </tr>`
    )
    .join("");
  $("#evalTableWrap").innerHTML = `<table>
    <thead><tr><th>题号</th><th>问题</th><th>召回</th><th>关键词</th><th>引用</th><th>结果</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function runEval() {
  const btn = $("#runEvalBtn");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    const result = await api("/api/eval/run", {
      method: "POST",
      body: JSON.stringify({ courseId: state.courseId }),
    });
    state.eval = result;
    renderEval(result);
    toast("评测完成");
  } catch (e) {
    toast(`评测失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "运行评测";
  }
}

/* ---------------- views ---------------- */

function switchView(view) {
  state.view = view;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $(".layout").classList.toggle("no-evidence", view !== "chat");
  $("#evidencePanel").style.display = view === "chat" ? "" : "none";
  if (view === "coverage") renderCoverage();
  if (view === "chat") setTimeout(scrollChat, 30);
}

/* ---------------- modals ---------------- */

function openModal(id) {
  $(`#${id}`).classList.add("open");
}
function closeModal(id) {
  $(`#${id}`).classList.remove("open");
}

function applyState(next) {
  if (!next) return;
  state.data = next;
  state.courseId = next.courseId;
  renderState();
  if (state.view === "coverage") renderCoverage();
}

function fillNoteFields(note) {
  $("#noteTitle").value = note.title;
  $("#noteText").value = note.text;
}

async function openNotes(conceptId, name) {
  try {
    const r = await api(`/api/notes?conceptId=${encodeURIComponent(conceptId)}`);
    if (!r.notes.length) {
      toast("这条知识点还没有笔记");
      return;
    }
    state.editingConcept = conceptId;
    state.notes = r.notes;
    $("#notesTitle").textContent = `编辑笔记 · ${name}`;
    $("#noteSelect").innerHTML = r.notes
      .map(
        (n) =>
          `<option value="${escapeHtml(n.id)}">${escapeHtml(n.title)}${n.sourceType === "auto" ? "（补全卡）" : ""}</option>`
      )
      .join("");
    fillNoteFields(r.notes[0]);
    openModal("notesModal");
  } catch (e) {
    toast(`读取笔记失败：${e.message}`);
  }
}

async function saveNote() {
  const id = $("#noteSelect").value;
  const title = $("#noteTitle").value.trim();
  const text = $("#noteText").value.trim();
  if (!id || text.length < 10) {
    toast("正文太短，至少 10 字");
    return;
  }
  try {
    const r = await api(`/api/notes/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ title, text }),
    });
    applyState(r.state);
    closeModal("notesModal");
    toast("笔记已保存，索引与覆盖率已刷新");
  } catch (e) {
    toast(`保存失败：${e.message}`);
  }
}

function openFill(conceptId, name) {
  const concept = state.data.concepts.find((c) => c.id === conceptId);
  if (!concept) return;
  state.fillingConcept = conceptId;
  $("#fillTitle").textContent = `补全知识点 · ${name}`;
  const missing = concept.missing || [];
  $("#fillHint").textContent = missing.length
    ? `当前缺：${missing.join("、")}。保存后会新增一张补全卡。`
    : "四维齐全；仍可补充更具体的例子、推导或练习。";
  $("#fillExample").value = missing.includes("例子")
    ? `例如，在「${name}」的典型题目中，先写出定义或公式，再代入条件求解。`
    : "";
  $("#fillDerivation").value = missing.includes("推导或代码")
    ? `因为「${name}」的成立需要满足定义中的条件，所以使用前要先验证前提。`
    : "";
  $("#fillPractice").value = missing.includes("练习")
    ? `练习：用自己的话解释「${name}」，给出一个例子，并说明适用条件。`
    : "";
  openModal("fillModal");
}

async function saveFill() {
  if (!state.fillingConcept) return;
  try {
    const r = await api(`/api/concepts/${encodeURIComponent(state.fillingConcept)}/fill`, {
      method: "POST",
      body: JSON.stringify({
        example: $("#fillExample").value.trim(),
        derivation: $("#fillDerivation").value.trim(),
        practice: $("#fillPractice").value.trim(),
      }),
    });
    applyState(r.state);
    closeModal("fillModal");
    toast("补全卡已保存");
  } catch (e) {
    toast(`补全失败：${e.message}`);
  }
}

function openConcept() {
  $("#conceptName").value = "";
  $("#conceptDefinition").value = "";
  openModal("conceptModal");
}

async function saveConcept() {
  const name = $("#conceptName").value.trim();
  if (!name) {
    toast("请填写知识点名称");
    return;
  }
  try {
    const r = await api("/api/concepts", {
      method: "POST",
      body: JSON.stringify({
        courseId: state.courseId,
        name,
        definition: $("#conceptDefinition").value.trim(),
      }),
    });
    applyState(r.state);
    closeModal("conceptModal");
    switchView("coverage");
    toast("知识点已新增");
  } catch (e) {
    toast(`新增失败：${e.message}`);
  }
}

function openSettings() {
  const d = state.data || {};
  $("#llmEnabled").checked = d.mode === "llm";
  $("#baseUrl").value = "https://api.deepseek.com/v1";
  $("#modelName").value = d.model || "deepseek-chat";
  $("#apiKey").value = "";
  $("#apiKey").placeholder = d.hasKey
    ? "已保存，留空表示保持不变"
    : "sk-…（只保存在当前服务内存）";
  openModal("settingsModal");
}

async function saveSettings() {
  const body = {
    enabled: $("#llmEnabled").checked,
    base_url: $("#baseUrl").value.trim(),
    model: $("#modelName").value.trim(),
  };
  if ($("#apiKey").value.trim()) body.api_key = $("#apiKey").value.trim();
  try {
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    await loadState(state.courseId);
    closeModal("settingsModal");
    toast(r.mode === "llm" ? "已切换为 LLM 生成" : "已保存，当前为离线模式");
  } catch (e) {
    toast(`保存失败：${e.message}`);
  }
}

function openUpload() {
  $("#docTitle").value = "";
  $("#docText").value = "";
  $("#docAuthority").value = "1";
  $("#uploadCourseHint").textContent = `导入到：${state.data?.course || "当前课程"}`;
  openModal("uploadModal");
}

async function submitUpload() {
  const title = $("#docTitle").value.trim();
  const text = $("#docText").value.trim();
  if (text.length < 20) {
    toast("内容太短，至少 20 字");
    return;
  }
  try {
    const r = await api("/api/ingest", {
      method: "POST",
      body: JSON.stringify({
        title: title || "未命名笔记",
        text,
        authority: Number($("#docAuthority").value),
        courseId: state.courseId,
      }),
    });
    if (r.state) {
      state.data = r.state;
      state.courseId = r.state.courseId;
      renderState();
    } else {
      await loadState(state.courseId);
    }
    closeModal("uploadModal");
    switchView("chat");
    toast(`已导入 ${r.chunks} 个片段，立即可检索`);
  } catch (e) {
    toast(`导入失败：${e.message}`);
  }
}

function openCourse() {
  $("#newCourseName").value = "";
  $("#newCourseDesc").value = "";
  openModal("courseModal");
}

async function submitCourse() {
  const name = $("#newCourseName").value.trim();
  if (!name) {
    toast("请填写课程名");
    return;
  }
  try {
    const r = await api("/api/courses", {
      method: "POST",
      body: JSON.stringify({ name, description: $("#newCourseDesc").value.trim() }),
    });
    window.location.href = `/?course=${encodeURIComponent(r.course.id)}`;
  } catch (e) {
    toast(`创建失败：${e.message}`);
  }
}

/* ---------------- events ---------------- */

function bind() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  $("#sendBtn").addEventListener("click", () => ask($("#input").value));
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask($("#input").value);
    }
  });
  $("#input").addEventListener("input", () => {
    const el = $("#input");
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  });
  $("#chat").addEventListener("click", (e) => {
    const btn = e.target.closest(".cite");
    if (btn) focusCitation(Number(btn.dataset.idx));
  });
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#uploadBtn").addEventListener("click", openUpload);
  $("#newCourseBtn").addEventListener("click", openCourse);
  $("#addConceptBtn").addEventListener("click", openConcept);
  $("#coverageHelp").addEventListener("click", () => openModal("coverageModal"));
  $("#saveSettings").addEventListener("click", saveSettings);
  $("#submitUpload").addEventListener("click", submitUpload);
  $("#submitCourse").addEventListener("click", submitCourse);
  $("#saveNote").addEventListener("click", saveNote);
  $("#saveFill").addEventListener("click", saveFill);
  $("#saveConcept").addEventListener("click", saveConcept);
  $("#noteSelect").addEventListener("change", () => {
    const note = state.notes.find((n) => n.id === $("#noteSelect").value);
    if (note) fillNoteFields(note);
  });
  $("#runEvalBtn").addEventListener("click", runEval);
  $$("[data-close]").forEach((btn) =>
    btn.addEventListener("click", () => closeModal(btn.dataset.close))
  );
  $$(".modal").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target === m) m.classList.remove("open");
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $$(".modal.open").forEach((m) => m.classList.remove("open"));
  });
}

async function init() {
  bind();
  try {
    const cid = new URLSearchParams(window.location.search).get("course");
    await loadState(cid || undefined);
  } catch (e) {
    toast(`加载失败：${e.message}`);
  }
}

init();
