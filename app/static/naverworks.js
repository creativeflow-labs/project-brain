// Isolated connector setup. Does not modify chat, search, or other connectors.
(() => {
  const panel = document.querySelector('#works-status');
  const connectForm = document.querySelector('#works-connect-form');
  const downloadForm = document.querySelector('#works-download-form');
  const disconnectButton = document.querySelector('#works-disconnect');
  let busy = false;
  function show(text) { panel.replaceChildren(Object.assign(document.createElement('span'), {textContent: text})); }
  function enabled(connected) {
    downloadForm.querySelectorAll('button').forEach(button => { button.disabled = busy || !connected; });
    disconnectButton.disabled = busy || !connected;
    connectForm.querySelector('button').disabled = busy;
  }
  async function post(path, body) {
    const response = await fetch(`/api/connectors/naverworks/${path}`, {
      method: 'POST', headers: {'Content-Type':'application/json', 'X-Project-Brain-Client':'1'},
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      const value = await response.json().catch(() => ({}));
      throw new Error(typeof value.detail === 'string' ? value.detail : '입력 항목 또는 요청 상태를 확인해 주세요.');
    }
    return response;
  }
  async function refresh() {
    const projectId = state.projectId;
    if (!projectId) { show('프로젝트를 먼저 선택해 주세요.'); enabled(false); return; }
    try {
      const value = await api(`/api/connectors/naverworks/status?project_id=${encodeURIComponent(projectId)}`);
      if (projectId !== state.projectId) return;
      show(value.message + (value.last_sync_at ? ` · ${formatDate(value.last_sync_at)} · 최근 ${value.last_sync_count}건` : ''));
      if (!downloadForm.elements.channel_id.value) downloadForm.elements.channel_id.value = value.channel_id || '';
      if (!downloadForm.elements.channel_name.value) downloadForm.elements.channel_name.value = value.channel_name || '';
      const localDate = date => new Date(date.getTime() - date.getTimezoneOffset()*60000).toISOString().slice(0,16);
      const now = new Date();
      if (!downloadForm.elements.start.value) downloadForm.elements.start.value = localDate(new Date(now.getTime()-7*86400000));
      if (!downloadForm.elements.end.value) downloadForm.elements.end.value = localDate(now);
      enabled(value.connected);
    } catch { if (projectId === state.projectId) { show('연결 상태를 확인하지 못했습니다.'); enabled(false); } }
  }
  connectForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !currentProjectRequired()) return;
    const projectId = state.projectId;
    const values = Object.fromEntries(new FormData(connectForm));
    busy = true; enabled(false); show('네이버웍스 인증 확인 중 · 메시지는 읽지 않습니다.');
    try {
      await post('connect', {project_id:projectId, ...values});
      connectForm.reset();
      toast('인증 정보를 저장했습니다. 메시지 조회 권한은 다운로드로 별도 확인해 주세요.');
    } catch (error) { toast(error.message, true); }
    finally { connectForm.reset(); busy = false; await refresh(); }
  });
  downloadForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !currentProjectRequired()) return;
    const projectId = state.projectId;
    const values = Object.fromEntries(new FormData(downloadForm));
    const start = new Date(values.start), end = new Date(values.end);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || end <= start || end - start > 31*86400000) {
      toast('시작·종료 시각을 확인해 주세요. 조회 기간은 최대 31일입니다.', true); return;
    }
    const syncing = event.submitter?.dataset.action === 'sync';
    busy = true; enabled(false); show(syncing ? '메시지 동기화 중 · 검색 인덱스에 반영합니다.' : '원본 CSV 다운로드 중 · 검색에는 반영하지 않습니다.');
    try {
      const response = await post(syncing ? 'sync' : 'download', {project_id:projectId, channel_id:values.channel_id.trim(), channel_name:values.channel_name.trim(), start:start.toISOString(), end:end.toISOString()});
      if (syncing) {
        const result = await response.json();
        if (projectId !== state.projectId) return;
        toast(`네이버웍스 ${result.processed_messages}건 확인 · 검색 반영 ${result.changed_messages}건 · 기존 ${result.unchanged_messages}건`);
        await loadDocuments();
        return;
      }
      const blob = await response.blob();
      if (projectId !== state.projectId) return;
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = 'naverworks-messages.csv'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`CSV ${response.headers.get('X-Message-Rows')}행 확인 · 검색 적재는 아직 하지 않았습니다.`);
    } catch (error) { toast(error.message, true); }
    finally { busy = false; await refresh(); }
  });
  disconnectButton.addEventListener('click', async () => {
    if (busy || !currentProjectRequired() || !confirm('현재 프로젝트의 네이버웍스 인증 정보를 삭제할까요? 다운로드 파일은 삭제하지 않습니다.')) return;
    busy = true; enabled(false);
    try { await post('disconnect', {project_id:state.projectId}); connectForm.reset(); }
    catch (error) { toast(error.message, true); }
    finally { busy = false; await refresh(); }
  });
  document.querySelector('#project-select').addEventListener('change', () => {
    connectForm.reset(); downloadForm.reset(); enabled(false); refresh();
  });
  document.querySelector('[data-view="connectors"]').addEventListener('click', refresh);
})();
