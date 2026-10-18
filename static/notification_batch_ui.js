// Presentation only. Inspecting originals never admits, releases or retries work.
import { attachNotificationProcessing } from './notification_processing_ui.js';
import { referenceNode } from './reference_navigation.js';
export function createNotificationBatchUi({ api, addMessage, captureView, loadThread, scheduleRefresh }) {
  const history = new Map();
  const cards = new Map();
  let selectedThread = null;
  let threadGeneration = 0;
  let panelGeneration = 0;
  function selectThread(threadId) {
    if (selectedThread !== threadId) {
      selectedThread = threadId; threadGeneration += 1; panelGeneration += 1;
    }
  }
  function capturePanelView() {
    const current = captureView();
    const thread = threadGeneration, panel = panelGeneration;
    return () => current() && thread === threadGeneration && panel === panelGeneration;
  }
  const labels = {
    prepared: "Admission prepared · guarded recovery required",
    prestart_refused: "Admission refused before worker routing · guarded recovery required",
    native_outcome_unknown: "Shared native start outcome unknown",
    native_input_started: "Shared native input delivered",
    released: "Admission released · originals returned to queue",
  };

  async function load(threadId, after = null) {
    selectThread(threadId);
    const requestCurrent = captureView(), thread = threadGeneration;
    const current = () => requestCurrent() && thread === threadGeneration;
    const path = `/api/threads/${encodeURIComponent(threadId)}/notification-batches`
      + (after ? `?after=${encodeURIComponent(after)}` : "");
    try {
      const result = await api(path);
      if (!current()) return;
      const cached = history.get(threadId);
      const previous = after && cached?.current?.() ? cached.items || [] : [];
      const seen = new Set(previous.map(item => item.id));
      history.set(threadId, {
        current,
        items: [...previous, ...(result.items || []).filter(item => !seen.has(item.id))],
        next: result.next_cursor || null,
        queue: result.queue || null,
      });
    } catch (error) {
      if (current()) history.set(threadId, {
        ...(after && history.get(threadId)?.current?.() ? history.get(threadId) : {items:[]}),
        current, error: error.message,
      });
    }
  }

  function attachRecovery(href, body) {
    const review=document.createElement('button');
    review.type='button';
    review.textContent='Review queue restoration';
    const section=document.createElement('div');
    section.setAttribute('aria-live','polite');
    review.addEventListener('click',async()=>{
      const current=captureView();
      review.disabled=true;
      section.textContent='Loading canonical restoration scope…';
      try {
        const preview=await api(href+'/recovery');
        if (!current()) return;
        section.textContent=`${preview.original_count} originals: ${(preview.original_ids || []).join(', ')}. Original thread: ${preview.original_thread_id}; destination: ${preview.target_thread_id}. ${preview.effect}`;
        if (!preview.can_restore) {
          section.append(document.createTextNode(` ${preview.blocker || 'Restoration is unavailable.'}`));
          return;
        }
        const label=document.createElement('label');
        label.textContent='Reason for returning these originals to the queue';
        const reason=document.createElement('textarea');
        reason.maxLength=1000;
        reason.value=preview.reserved_reason || '';
        label.append(reason);
        const restore=document.createElement('button');
        restore.type='button';
        restore.textContent='Return originals to queue';
        restore.disabled=!reason.value.trim();
        reason.addEventListener('input',()=>{restore.disabled=!reason.value.trim();});
        const result=document.createElement('div');
        restore.addEventListener('click',async()=>{
          const stillCurrent=captureView();
          restore.disabled=true;
          result.textContent='Restoring reviewed originals…';
          try {
            const response=await api(href+'/recovery',{method:'POST',body:JSON.stringify({
              expected_admission_sha256:preview.expected_admission_sha256,reason:reason.value.trim()})});
            if (!stillCurrent()) return;
            result.textContent='Originals returned to the queue. No native turn was started; individual outcomes remain unresolved.';
            await loadThread?.(response.threadId);
            if (stillCurrent()) scheduleRefresh?.(0);
          } catch (error) {
            if (stillCurrent()) {result.textContent=`Restoration refused: ${error.message}`; restore.disabled=false;}
          }
        });
        section.append(label,restore,result);
      } catch (error) {
        if (current()) section.textContent=`Cannot review restoration: ${error.message}`;
      } finally {
        if (current()) review.disabled=false;
      }
    });
    body.append(review,section);
  }

  function attach(href, body, summary=null) {
    if (!body || !href) return;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Inspect batch originals";
    const detail = document.createElement("pre");
    detail.style.whiteSpace = "pre-wrap";
    detail.style.overflowWrap = "anywhere";
    detail.hidden = true;
    detail.setAttribute("aria-live", "polite");
    const proofs=document.createElement('div');
    button.addEventListener("click", async () => {
      const current = captureView();
      button.disabled = true;
      detail.hidden = false;
      detail.textContent = "Loading retained originals…";
      proofs.textContent='';
      try {
        const batch = await api(href);
        if (!current()) return;
        const originals = Array.isArray(batch.originals) ? batch.originals : [];
        const seenProofs=new Set();
        for (const original of originals) {
          for (const [kind,ids] of [['evidence',[...(original.evidence_ids || []),...(original.candidate_evidence_ids || [])]],
            ['verifications',original.verification_ids || []]]) {
            for (const id of ids) {
              const href=`/api/${kind}/${encodeURIComponent(id)}`;
              if (seenProofs.has(href)) continue;
              seenProofs.add(href);
              proofs.append(referenceNode(kind === 'evidence' ? 'evidence' : 'verification', id,
                {projectId:batch.project_id || summary?.project_id,
                  label:`Inspect ${kind === 'evidence' ? 'evidence' : 'verification'} ${id}`}),document.createElement('br'));
            }
          }
        }
        const control = batch.control_snapshot;
        const controls = control ? [
          'Controls retained at admission:',
          `Agent profile record: ${control.agent_profile_record_id}`,
          `Execution definition: ${control.execution_definition_ref?.definition_id} · revision ${control.execution_definition_ref?.revision}`,
          `Authority definition: ${control.authority_definition_ref?.definition_id} · revision ${control.authority_definition_ref?.revision}`,
          ...(control.configuration_pins_json || []).map(raw => {
            const pin = JSON.parse(raw);
            return `Configuration: ${pin.key} = ${pin.value}; record ${pin.record_id || 'default'} · revision ${pin.revision ?? 'default'}`;
          }),
          `Admission bounds: ${control.max_members} originals; ${control.max_context_bytes} UTF8 bytes`,
          '',
        ].join('\n') : '';
        detail.textContent = controls + originals.map(item => {
          const original = item.original || {};
          const {message, ...provenance} = original;
          return [
            `Original ${original.id || "unknown"} · outcome ${item.outcome || "unresolved"}`,
            `Outcome blockers: ${(item.blockers || []).join(', ') || 'none'}`,
            `Other proof warnings: ${(item.warnings || []).join(', ') || 'none'}`,
            `Evidence: ${(item.evidence_ids || []).join(', ') || 'none'}; verification: ${(item.verification_ids || []).join(', ') || 'none'}`,
            `Candidate evidence requiring review: ${(item.candidate_evidence_ids || []).join(', ') || 'none'}`,
            `Requested execution: ${original.execution_id || "none"}`,
            `Created: ${new Date(original.created_at * 1000).toISOString()}`,
            `Source: ${original.source || "unknown"}; work item: ${original.work_item_ref || "none"}`,
            `Retained status: ${item.retained_status || "unknown"}`,
            `Retention integrity: ${item.retention_integrity === 'matches_original' ? 'matches original'
              : item.retention_integrity === 'changed_or_missing' ? 'retained original changed or missing' : 'not verified'}`,
            `Original thread: ${original.thread_id}; admitted thread: ${item.admitted_route?.thread_id}`,
            `Original request:\n${original.message || ""}`,
            `Exact provenance and admitted route:\n${JSON.stringify({...item, original:provenance}, null, 2)}`,
          ].join("\n");
        }).join("\n\n");
        if (!originals.length) detail.textContent = "Retained originals are unavailable. Reconciliation remains required.";
      } catch (error) {
        if (current()) detail.textContent = `Could not inspect batch: ${error.message}`;
      } finally {
        if (current()) button.disabled = false;
      }
    });
    body.append(button, detail, proofs);
    if (summary?.can_capture_results && summary?.native_receipt) {
      const capture=document.createElement('button');
      capture.type='button';
      capture.textContent='Capture completed native proposals';
      const result=document.createElement('div');
      result.setAttribute('aria-live','polite');
      capture.addEventListener('click',async()=>{
        const current=captureView();
        capture.disabled=true;
        result.textContent='Capturing proposals as unverified evidence. No native input will be sent.';
        try {
          const response=await api(href+'/results',{method:'POST',body:JSON.stringify({
            expected_native_turn_id:summary.native_receipt.provider_native_turn_id})});
          if (!current()) return;
          result.textContent=`Captured evidence: ${(response.evidence_ids || []).join(', ')}. Independent review is still required; no native turn was started.`;
          scheduleRefresh?.(0);
        } catch (error) {
          if (current()) result.textContent=`Cannot capture native proposals: ${error.message}`;
        } finally {if (current()) capture.disabled=false;}
      });
      body.append(capture,result);
    }
    if (summary?.can_review_recovery) attachRecovery(href,body);
    if (summary?.can_review_processing) attachNotificationProcessing({api, captureView:capturePanelView,
      scheduleRefresh,projectId:summary.project_id}, href, body);
  }

  function render(threadId) {
    selectThread(threadId);
    panelGeneration += 1;
    for (const card of cards.get(threadId) || []) card?.remove();
    const rendered = [];
    cards.set(threadId, rendered);
    const snapshot = history.get(threadId);
    if (!snapshot) return;
    if (snapshot.current && !snapshot.current()) return;
    if (snapshot.error) {
      rendered.push(addMessage("Notification history unavailable", snapshot.error, "tool", new Date()));
    }
    if (snapshot.queue) {
      const queue = snapshot.queue;
      rendered.push(addMessage("Retained notification queue",
        `${queue.original_count} queued originals; ${queue.provider_pinned_count} carry provider-event pins; `
        + `${queue.without_provider_pin_count} have no provider-event pin.\n`
        + (queue.oldest_age_seconds == null ? "Queue is empty.\n"
          : `Oldest queued original: ${Math.floor(queue.oldest_age_seconds)} seconds.\n`)
        + "Pins do not establish batch eligibility. Current event proof, authority, profile, repository and reply compatibility still require verification. "
        + "Queue age includes requests and handoffs; it is not oldest actionable notification age. Admission history below does not measure useful throughput.",
        "tool", new Date()));
    }
    for (const batch of snapshot.items || []) {
      const receipt = batch.native_receipt;
      const counts = batch.outcome_counts;
      const lines = [
        `Batch: ${batch.id}`,
        `Original count: ${batch.original_count}; oldest age: ${Math.floor(batch.oldest_age_seconds || 0)} seconds`,
        `Original IDs: ${(batch.original_ids || []).join(", ")}`,
        `Recovery: ${batch.recovery_state}`,
        `Historical processed originals: ${batch.processing_count || 0}; review current proof and follow-up eligibility before acting.`,
        counts ? `Original outcomes: ${counts.reconciled} reconciled; ${counts.follow_up_required} require follow-up; ${counts.unresolved} unresolved.`
          : "Every original outcome remains unresolved.",
        "Notification reconciliation does not prove ACK, business acceptance or issue closure. Shared native completion does not prove individual outcomes.",
        `Project: ${batch.project_id}; tenant: ${batch.organization_id}/${batch.workspace_id}`,
        `Agent profile: ${batch.agent_profile_id} · revision ${batch.agent_profile_revision}`,
        `Execution profile: ${batch.execution_profile_id}`,
        `Repository: ${batch.repository_resource_id || "none"}`,
        `Writable repositories: ${(batch.writable_repository_resource_ids || []).join(", ") || "none"}`,
        `Read-only repositories: ${(batch.read_only_repository_resource_ids || []).join(", ") || "none"}`,
        batch.prestart_refusal_code ? `Controller refusal: ${batch.prestart_refusal_code}` : "",
        receipt ? `Shared native turn: ${receipt.provider_native_turn_id}; runtime: ${receipt.provider_id}/${receipt.runtime_id}` : "",
        receipt ? `Requested execution: ${receipt.requested_execution_id}; admitted execution: ${receipt.execution_id}` : "",
      ].filter(Boolean);
      const card = addMessage(labels[batch.phase] || "Unknown batch state · reconciliation required",
        lines.join("\n"), "tool", new Date(batch.oldest_created_at * 1000));
      rendered.push(card);
      attach(batch.detail_href, card?.querySelector(".body"),batch);
    }
    if (snapshot.next) {
      const card = addMessage("More notification admission history", "Older admissions are available.", "tool", new Date());
      rendered.push(card);
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Load older admissions";
      button.addEventListener("click", async () => {
        const current = captureView();
        button.disabled = true;
        await load(threadId, snapshot.next);
        if (current()) render(threadId);
      });
      card?.querySelector(".body")?.append(button);
    }
  }
  return { load, render, attach, selectThread };
}
