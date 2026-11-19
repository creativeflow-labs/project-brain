// Explicit approval controls: drafting a response never clicks or sends for users.
function renderDeliveryPreview(bubble, preview, projectId) {
  bubble.replaceChildren();
  const title = document.createElement('h3');
  title.textContent = '네이버웍스 전송문 확인';
  const recipient = document.createElement('p');
  recipient.textContent = `수신자: ${preview.recipient} (${preview.recipient_id}) · PB 봇 · ${preview.part_count}개 메시지`;
  const body = document.createElement('pre');
  body.className = 'delivery-preview-text';
  body.textContent = preview.text;
  const note = document.createElement('p');
  note.textContent = preview.warning || '아직 전송하지 않았습니다. 아래 승인 버튼을 눌러야 보냅니다. 승인 유효시간은 15분입니다.';
  const result = document.createElement('p');
  result.setAttribute('role', 'status');
  const controls = document.createElement('div');
  controls.className = 'connector-button-row';
  const approve = document.createElement('button');
  approve.type = 'button'; approve.className = 'primary-button';
  approve.textContent = '승인하고 나에게 보내기';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'secondary-button'; cancel.textContent = '취소';
  const check = document.createElement('button');
  check.type = 'button'; check.className = 'text-button'; check.textContent = '전송 상태 확인';
  check.hidden = true;
  let requestStarted = false;
  const labels = {sent:'발송 접수 완료 · 수신·읽음 여부 미확인', failed:'발송 실패 · 자동 재전송하지 않습니다.',
    unknown:'결과 확인 필요 · 메신저를 직접 확인해 주세요.', sending:'전송 처리 중이거나 결과 확인이 필요합니다. 자동 재전송하지 않습니다.',
    cancelled:'전송을 취소했습니다.', expired:'승인 시간이 만료됐습니다.'};
  function display(value) {
    result.textContent = `${labels[value.status] || '승인 대기'} (${value.completed_parts}/${value.part_count}건 접수)` + (value.message ? ` ${value.message}` : '');
  }
  async function act(path) {
    if (requestStarted || projectId !== state.projectId) return;
    requestStarted = true; approve.disabled = true; cancel.disabled = true;
    result.textContent = path === 'confirm' ? '승인된 전송문을 보내고 있습니다…' : '취소 중…';
    try {
      display(await api(`/api/actions/${path}`, {method:'POST',
        headers:{'Content-Type':'application/json','X-Project-Brain-Client':'1'},
        body:JSON.stringify({project_id:projectId,action_id:preview.id,approval_token:preview.approval_token})}));
    } catch {
      result.textContent = '요청 결과를 확인하지 못했습니다. 다시 보내지 말고 전송 상태를 확인해 주세요.';
    } finally { check.hidden = false; }
  }
  approve.addEventListener('click', () => act('confirm'));
  cancel.addEventListener('click', () => act('cancel'));
  check.addEventListener('click', async () => {
    if (projectId !== state.projectId) return;
    try { display(await api(`/api/actions/${encodeURIComponent(preview.id)}?project_id=${encodeURIComponent(projectId)}`)); }
    catch { result.textContent = '전송 이력 조회 실패 · 메신저를 직접 확인해 주세요.'; }
  });
  controls.append(approve, cancel, check);
  bubble.append(title, recipient, body, note, controls, result);
}
