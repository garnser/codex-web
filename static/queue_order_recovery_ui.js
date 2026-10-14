export function createQueueOrderRecoveryUi({api,addMessage,loadThread,scheduleRefresh,captureView}) {
  const previews=new Map();
  async function load(threadId){
    const current=captureView();previews.delete(threadId);
    try{
      const preview=await api(`/api/threads/${encodeURIComponent(threadId)}/queue/order-recovery`);
      if(current()&&Array.isArray(preview?.current_order))previews.set(threadId,{preview,current});
    }catch{/* Existing servers or denied scope have no repair preview. */}
  }
  function render(threadId){
    const entry=previews.get(threadId);if(!entry?.current()||entry.preview.current_order.length<2)return;
    const preview=entry.preview;
    const lines=[`Current order: ${preview.current_order.join(' → ')}`,
      preview.proposed_order?`Original admission order with explicit retry priority: ${preview.proposed_order.join(' → ')}`:'',
      ...(preview.blockers||[]).map(item=>`${item.code}: ${item.message}`),
      'Repair changes scheduling order only. Unknown native outcomes remain unknown; this does not retry any request.'];
    const card=addMessage('Queue order recovery',lines.filter(Boolean).join('\n'),'tool',new Date());
    const body=card?.querySelector('.body');if(!body||!preview.can_repair)return;
    const label=document.createElement('label');label.textContent='Reason for order repair ';
    const reason=document.createElement('input');reason.maxLength=1000;reason.setAttribute('aria-label','Queue order repair reason');label.append(reason);
    const button=document.createElement('button');button.type='button';button.textContent='Repair scheduling order';button.disabled=true;
    let submitted=false;
    reason.addEventListener('input',()=>{button.disabled=submitted||!reason.value.trim();});
    const status=document.createElement('div');status.setAttribute('role','status');
    button.addEventListener('click',async()=>{
      if(submitted||!entry.current())return;submitted=true;button.disabled=true;status.textContent='Checking current queue and admission provenance…';
      try{
        const response=await api(`/api/threads/${encodeURIComponent(threadId)}/queue/order-recovery`,{
          method:'POST',body:JSON.stringify({expected_snapshot_sha256:preview.snapshot_sha256,reason:reason.value.trim()})});
        if(!entry.current())return;
        status.textContent=response.audit_recorded===false?'Order changed; audit confirmation unavailable. Inspect the queue before another action.':'Scheduling order repaired. Native outcomes were preserved.';
        await loadThread(threadId);if(entry.current())scheduleRefresh(0);
      }catch(error){if(entry.current()){status.textContent='Repair was not confirmed: '+error.message+'. Reload the preview before retrying.';}}
    });
    body.append(label,button,status);
  }
  return {load,render};
}
