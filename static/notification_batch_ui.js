// Presentation only. Inspecting originals never admits, releases or retries work.
export function createNotificationBatchUi({ api, addMessage, captureView }) {
  const history = new Map();
  const cards = new Map();
  const labels = {
    prepared: "Admission prepared · guarded recovery required",
    prestart_refused: "Admission refused before worker routing · guarded recovery required",
    native_outcome_unknown: "Shared native start outcome unknown",
    native_input_started: "Shared native input delivered",
    released: "Admission released · originals returned to queue",
  };

  async function load(threadId, after = null) {
    const current = captureView();
    const path = `/api/threads/${encodeURIComponent(threadId)}/notification-batches`
      + (after ? `?after=${encodeURIComponent(after)}` : "");
    try {
      const result = await api(path);
      if (!current()) return;
      const previous = after ? history.get(threadId)?.items || [] : [];
      const seen = new Set(previous.map(item => item.id));
      history.set(threadId, {
        items: [...previous, ...(result.items || []).filter(item => !seen.has(item.id))],
        next: result.next_cursor || null,
      });
    } catch (error) {
      if (current()) history.set(threadId, {
        ...(after ? history.get(threadId) : {items:[]}), error: error.message,
      });
    }
  }

  function attach(href, body) {
    if (!body || !href) return;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Inspect batch originals";
    const detail = document.createElement("pre");
    detail.style.whiteSpace = "pre-wrap";
    detail.style.overflowWrap = "anywhere";
    detail.hidden = true;
    detail.setAttribute("aria-live", "polite");
    button.addEventListener("click", async () => {
      const current = captureView();
      button.disabled = true;
      detail.hidden = false;
      detail.textContent = "Loading retained originals…";
      try {
        const batch = await api(href);
        if (!current()) return;
        const originals = Array.isArray(batch.originals) ? batch.originals : [];
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
    body.append(button, detail);
  }

  function render(threadId) {
    for (const card of cards.get(threadId) || []) card?.remove();
    const rendered = [];
    cards.set(threadId, rendered);
    const snapshot = history.get(threadId);
    if (!snapshot) return;
    if (snapshot.error) {
      rendered.push(addMessage("Notification history unavailable", snapshot.error, "tool", new Date()));
    }
    for (const batch of snapshot.items || []) {
      const receipt = batch.native_receipt;
      const lines = [
        `Batch: ${batch.id}`,
        `Original count: ${batch.original_count}; oldest age: ${Math.floor(batch.oldest_age_seconds || 0)} seconds`,
        `Original IDs: ${(batch.original_ids || []).join(", ")}`,
        `Recovery: ${batch.recovery_state}`,
        "Every original outcome remains unresolved. Shared admission or native completion does not prove success or ACK.",
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
      attach(batch.detail_href, card?.querySelector(".body"));
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
  return { load, render, attach };
}
