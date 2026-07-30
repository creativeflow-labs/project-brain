const state = {
  projects: [],
  projectId: localStorage.getItem("projectBrainProjectId") || "",
  threadIds: {}
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => element.className = "toast", 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data.detail || data || `HTTP ${response.status}`);
  return data;
}

function currentProjectRequired() {
  if (!state.projectId) {
    toast("프로젝트를 먼저 만들어 주세요.", true);
    $("#project-dialog").showModal();
    return false;
  }
  return true;
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    $("#system-status").textContent = "정상 작동";
    $("#system-description").textContent =
      `v${health.version} · ${health.database} · ${health.answer_mode === "generative" ? "생성형 답변" : "API 키 없는 추출형 답변"}`;
  } catch {
    $("#system-status").textContent = "연결 오류";
    $("#system-description").textContent = "API 서버 상태를 확인해 주세요.";
  }
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  if (!state.projects.length) {
    $("#project-select").innerHTML = `<option value="">프로젝트 없음</option>`;
    $("#project-dialog").showModal();
    return;
  }
  if (!state.projects.some(project => project.id === state.projectId)) state.projectId = state.projects[0].id;
  $("#project-select").innerHTML = state.projects
    .map(project => `<option value="${project.id}" ${project.id === state.projectId ? "selected" : ""}>${escapeHtml(project.name)}</option>`)
    .join("");
  localStorage.setItem("projectBrainProjectId", state.projectId);
  await Promise.all([loadDocuments(), loadNotionStatus()]);
}

async function loadDocuments() {
  if (!state.projectId) return;
  const documents = await api(`/api/projects/${state.projectId}/documents`);
  if (!documents.length) {
    $("#documents-list").innerHTML = `<div class="list-empty">아직 등록된 자료가 없습니다.</div>`;
    return;
  }
  $("#documents-list").innerHTML = documents.map(document => {
    const type = (document.filename?.split(".").pop() || document.source_type).slice(0, 5).toUpperCase();
    const source = sourceLabel(document.source_type);
    const warning = document.warnings_json.length ? `<span class="warning-badge">확인 필요 ${document.warnings_json.length}</span>` : "";
    return `<div class="document-row">
      <div class="file-badge">${escapeHtml(type)}</div>
      <div class="document-title"><strong>${escapeHtml(document.title)}</strong><span>${source} · ${formatDate(document.created_at)}</span></div>
      ${warning}
      <button class="delete-button" data-delete="${document.id}" title="삭제">×</button>
    </div>`;
  }).join("");
  $$("[data-delete]").forEach(button => button.addEventListener("click", () => deleteDocument(button.dataset.delete)));
}

function sourceLabel(source) {
  return {
    upload: "직접 업로드", notion: "Notion", notion_attachment: "Notion 첨부",
    gmail: "Gmail", gmail_attachment: "Gmail 첨부"
  }[source] || source;
}

async function deleteDocument(id) {
  if (!confirm("이 자료와 검색 인덱스를 삭제할까요?")) return;
  await api(`/api/documents/${id}`, { method: "DELETE" });
  toast("자료를 삭제했습니다.");
  await loadDocuments();
}

async function uploadFiles(files) {
  if (!currentProjectRequired() || !files.length) return;
  const supported = [...files].filter(file => /\.(pdf|docx|txt|md)$/i.test(file.name));
  if (supported.length !== files.length) toast("지원하지 않는 형식은 제외했습니다.", true);
  let success = 0;
  for (const [index, file] of supported.entries()) {
    $("#upload-progress").textContent = `${index + 1}/${supported.length} · ${file.name} 처리 중`;
    const form = new FormData();
    form.append("project_id", state.projectId);
    form.append("file", file);
    try {
      await api("/api/documents/upload", { method: "POST", body: form });
      success += 1;
    } catch (error) {
      toast(`${file.name}: ${error.message}`, true);
    }
  }
  $("#upload-progress").textContent = `${success}개 파일 처리 완료`;
  await loadDocuments();
}

function renderMessage(role, text) {
  const empty = $(".empty-chat");
  if (empty) empty.remove();
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  wrapper.innerHTML = `<div><div class="message-label">${role === "user" ? "질문" : "PROJECT BRAIN"}</div><div class="bubble"></div></div>`;
  wrapper.querySelector(".bubble").textContent = text;
  $("#messages").append(wrapper);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return wrapper;
}

function renderEvidence(citations) {
  $("#evidence-count").textContent = citations.length;
  if (!citations.length) {
    $("#evidence-list").innerHTML = `<div class="panel-empty">사용된 근거가 없습니다.</div>`;
    return;
  }
  $("#evidence-list").innerHTML = citations.map((item, index) => `<article class="evidence-card">
    <div class="evidence-meta"><span class="source-tag">${escapeHtml(sourceLabel(item.source_type))}</span><span class="score">${Math.round(item.score * 100)}%</span></div>
    <h3>[${index + 1}] ${escapeHtml(item.title)}</h3>
    <small>${escapeHtml(item.locator || "본문")}</small>
    <p>${escapeHtml(item.excerpt)}</p>
    ${item.source_url ? `<a href="${escapeAttribute(item.source_url)}" target="_blank" rel="noreferrer">원본 열기 ↗</a>` : ""}
  </article>`).join("");
}

function renderCurrentState(currentState) {
  const panel = $("#state-summary");
  if (!currentState) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const statusLabel = {
    supported: "근거 확인",
    conflict: "충돌 확인",
    insufficient: "확인 필요"
  }[currentState.evidence_status] || "확인 필요";
  const timeline = [...(currentState.timeline || [])]
    .reverse()
    .slice(0, 5)
    .map(event => `<li class="timeline-item${event.decisive ? " decisive" : ""}">
      <span class="timeline-dot"></span>
      <div><strong>${escapeHtml(event.label)}</strong><small>${escapeHtml(sourceLabel(event.source_type))}${event.occurred_at ? ` · ${escapeHtml(formatDate(event.occurred_at))}` : " · 시점 미확인"}</small><p>${escapeHtml(event.excerpt)}</p></div>
    </li>`).join("");
  panel.hidden = false;
  panel.innerHTML = `<div class="state-heading">
      <div><span class="state-kicker">CURRENT STATE</span><strong>${escapeHtml(currentState.label)}</strong></div>
      <span class="state-verdict ${escapeHtml(currentState.evidence_status)}">${escapeHtml(statusLabel)}</span>
    </div>
    ${currentState.as_of ? `<div class="state-asof">기준 ${escapeHtml(formatDate(currentState.as_of))}</div>` : ""}
    <p class="state-reason">${escapeHtml(currentState.reason)}</p>
    ${currentState.next_action ? `<div class="next-action"><span>다음 확인</span>${escapeHtml(currentState.next_action)}</div>` : ""}
    ${timeline ? `<ol class="timeline">${timeline}</ol>` : ""}`;
}

async function askQuestion(event) {
  event.preventDefault();
  if (!currentProjectRequired()) return;
  const input = $("#question");
  const question = input.value.trim();
  if (!question) return;
  renderMessage("user", question);
  input.value = "";
  const button = $("#ask-form button");
  button.disabled = true;
  button.textContent = "근거 검색 중…";
  const pending = renderMessage("assistant", "프로젝트 자료에서 근거를 찾고 있습니다.");
  try {
    const result = await api("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: state.projectId,
        question,
        top_k: 6,
        thread_id: state.threadIds[state.projectId] || null
      })
    });
    state.threadIds[state.projectId] = result.thread_id;
    pending.querySelector(".bubble").textContent = result.answer;
    renderCurrentState(result.current_state);
    renderEvidence(result.citations);
  } catch (error) {
    pending.querySelector(".bubble").textContent = `오류: ${error.message}`;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = `질문하기 <span>↑</span>`;
  }
}

function renderNotionStatus(status) {
  const panel = $("#notion-status");
  const syncButton = $("#notion-sync");
  const forceButton = $("#notion-force-sync");
  panel.className = `connector-status ${status.status || ""}`;
  if (!status.connected) {
    panel.innerHTML = `<strong>연결되지 않음</strong><span>토큰을 한 번 등록하면 페이지 주소 없이 동기화할 수 있습니다.</span>`;
    syncButton.disabled = true;
    forceButton.disabled = true;
    return;
  }
  syncButton.disabled = status.status === "running";
  forceButton.disabled = status.status === "running";
  const statusLabel = {
    connected: "연결됨 · 첫 동기화 전",
    running: "동기화 진행 중",
    success: "동기화 정상",
    partial: "일부 항목 확인 필요",
    failed: "동기화 실패"
  }[status.status] || "연결됨";
  const details = [];
  if (status.last_success_at) details.push(`마지막 성공 ${formatDate(status.last_success_at)}`);
  if (Number.isFinite(status.processed_pages)) details.push(`처리 ${status.processed_pages}개`);
  if (Number.isFinite(status.changed_pages)) details.push(`변경 ${status.changed_pages}개`);
  if (status.error_count) details.push(`오류 ${status.error_count}개`);
  if (status.discovery_incomplete) details.push("탐색 범위 확인 필요");
  if (status.auto_sync_enabled) details.push(`${status.sync_interval_minutes}분마다 자동`);
  panel.innerHTML = `<strong>${escapeHtml(statusLabel)}</strong><span>${escapeHtml(details.join(" · ") || "연결 정보가 안전하게 저장되었습니다.")}</span>
    ${status.last_error ? `<span>${escapeHtml(status.last_error)}</span>` : ""}`;
}

async function loadNotionStatus() {
  if (!state.projectId) return;
  try {
    const status = await api(`/api/connectors/notion/status?project_id=${encodeURIComponent(state.projectId)}`);
    renderNotionStatus(status);
  } catch (error) {
    renderNotionStatus({ connected: false, status: "failed" });
    toast(error.message, true);
  }
}

async function connectNotion(event) {
  event.preventDefault();
  if (!currentProjectRequired()) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/connectors/notion/connect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: state.projectId,
        access_token: $("#notion-token").value.trim(),
        auth_mode: $("#notion-auth-mode").value,
        auto_sync_enabled: $("#notion-auto-sync").checked,
        sync_interval_minutes: Number($("#notion-sync-interval").value)
      })
    });
    $("#notion-token").value = "";
    toast("Notion 연결을 안전하게 저장했습니다. 첫 동기화를 시작합니다.");
    await syncNotion(false);
  } catch (error) {
    toast(error.message, true);
    await loadNotionStatus();
  } finally {
    button.disabled = false;
  }
}

async function syncNotion(force = false) {
  if (!currentProjectRequired()) return;
  const buttons = [$("#notion-sync"), $("#notion-force-sync")];
  buttons.forEach(button => button.disabled = true);
  renderNotionStatus({ connected: true, status: "running" });
  try {
    const result = await api("/api/connectors/notion/sync", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: state.projectId, force })
    });
    const summary = `Notion: 처리 ${result.processed_pages || 0}, 변경 ${result.changed_pages || 0}, 첨부 ${result.changed_attachments || 0}`;
    toast(summary, Boolean(result.error_count));
    renderNotionStatus(result);
    await loadDocuments();
  } catch (error) {
    toast(error.message, true);
  } finally {
    await loadNotionStatus();
  }
}

async function connectGmail() {
  if (!currentProjectRequired()) return;
  try {
    const result = await api(`/api/connectors/gmail/auth-url?project_id=${encodeURIComponent(state.projectId)}`);
    window.location.href = result.authorization_url;
  } catch (error) { toast(error.message, true); }
}

async function syncGmail(event) {
  event.preventDefault();
  if (!currentProjectRequired()) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api("/api/connectors/gmail/sync", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: state.projectId, query: $("#gmail-query").value,
        max_results: Number($("#gmail-max").value)
      })
    });
    toast(`Gmail: 본문 ${result.messages}, 첨부 ${result.attachments}, 변경 없음 ${result.unchanged}`);
    if (result.errors.length) toast(result.errors[0], true);
    await loadDocuments();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;" })[char]);
}
function escapeAttribute(value) { return escapeHtml(value); }
function formatDate(value) { return new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); }

function bindEvents() {
  $$(".nav-item").forEach(button => button.addEventListener("click", () => {
    $$(".nav-item, .view").forEach(element => element.classList.remove("active"));
    button.classList.add("active");
    $(`#view-${button.dataset.view}`).classList.add("active");
    if (button.dataset.view === "sources") loadDocuments();
  }));
  $("#project-select").addEventListener("change", async event => {
    state.projectId = event.target.value;
    delete state.threadIds[state.projectId];
    localStorage.setItem("projectBrainProjectId", state.projectId);
    $("#messages").innerHTML = `<div class="empty-chat"><div class="spark">✦</div><h2>새 프로젝트의 기억을 불러왔습니다.</h2><p>등록된 자료를 바탕으로 질문해 보세요.</p></div>`;
    renderEvidence([]);
    renderCurrentState(null);
    await Promise.all([loadDocuments(), loadNotionStatus()]);
  });
  $("#new-project-button").addEventListener("click", () => $("#project-dialog").showModal());
  $("#close-dialog").addEventListener("click", () => $("#project-dialog").close());
  $("#project-form").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const project = await api("/api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $("#project-name").value, description: $("#project-description").value })
      });
      state.projectId = project.id;
      $("#project-dialog").close();
      event.target.reset();
      await loadProjects();
      toast("프로젝트를 만들었습니다.");
    } catch (error) { toast(error.message, true); }
  });
  $("#file-select-button").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", event => uploadFiles(event.target.files));
  const drop = $("#upload-form");
  ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.remove("dragging"); }));
  drop.addEventListener("drop", event => uploadFiles(event.dataTransfer.files));
  $("#refresh-documents").addEventListener("click", loadDocuments);
  $("#ask-form").addEventListener("submit", askQuestion);
  $$(".suggestions button").forEach(button => button.addEventListener("click", () => { $("#question").value = button.textContent; $("#question").focus(); }));
  $("#notion-connect-form").addEventListener("submit", connectNotion);
  $("#notion-sync").addEventListener("click", () => syncNotion(false));
  $("#notion-force-sync").addEventListener("click", () => syncNotion(true));
  $("#gmail-connect").addEventListener("click", connectGmail);
  $("#gmail-sync-form").addEventListener("submit", syncGmail);
}

async function init() {
  bindEvents();
  await Promise.all([loadHealth(), loadProjects()]);
  const params = new URLSearchParams(location.search);
  const gmailStatus = params.get("gmail");
  if (gmailStatus === "connected") {
    toast("Gmail 연결이 완료되었습니다.");
    history.replaceState({}, "", "/");
  } else if (gmailStatus === "error") {
    toast("Gmail 연결에 실패했습니다. 다시 연결해 주세요.", true);
    history.replaceState({}, "", "/");
  }
}
init().catch(error => toast(error.message, true));
