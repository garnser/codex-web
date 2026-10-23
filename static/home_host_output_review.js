/** Explicit human review only; denied command output never enters model context here. */
export function mountHomeHostOutputReview(container, { api, artifactId, homeRevision, currentRevision, onReleased = () => {} }) {
  container.replaceChildren();
  const status = document.createElement('p');
  status.textContent = 'Command output is encrypted and denied to model context unless a current output policy or your explicit review releases an excerpt.';
  const preview = document.createElement('button');
  preview.type = 'button';
  preview.textContent = 'Review protected output';
  container.append(status, preview);
  let generation = 0;
  let expiryTimer = null;
  const current = () => currentRevision() === homeRevision && container.isConnected;
  const check = () => { if (!current()) throw new Error('Home selection changed. Reopen this output from the current operator.'); };
  preview.addEventListener('click', async () => {
    const request = ++generation;
    preview.disabled = true;
    for (const old of container.children) { if (old.tagName === 'FORM') old.remove(); }
    try {
      check();
      const result = await api(`/api/home/operator/host-outputs/${encodeURIComponent(artifactId)}/preview?expected_home_revision=${homeRevision}`);
      check();
      if (request !== generation) return;
      if (!Number.isFinite(result.expires_at) || result.expires_at * 1000 <= Date.now()) throw new Error('Protected output preview expired. Request a fresh preview.');
      const expires = result.expires_at * 1000;
      const form = document.createElement('form');
      clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => { text.value = ''; form.remove(); status.textContent = 'Protected output preview expired. Request a fresh preview.'; }, Math.max(0, expires - Date.now()));
      const stream = document.createElement('select');
      for (const value of ['stdout', 'stderr']) { const option = document.createElement('option'); option.value = value; option.textContent = value; stream.append(option); }
      const text = document.createElement('textarea'); text.readOnly = true; text.rows = 12; text.setAttribute('aria-label', 'Protected command output for human review');
      const start = document.createElement('input'); start.type = 'number'; start.min = '0'; start.value = '0'; start.setAttribute('aria-label', 'Excerpt start byte');
      const end = document.createElement('input'); end.type = 'number'; end.min = '1'; end.max = '65536'; end.setAttribute('aria-label', 'Excerpt end byte');
      const classification = document.createElement('select');
      for (const value of ['confidential', 'internal']) { const option = document.createElement('option'); option.value = value; option.textContent = value; classification.append(option); }
      classification.setAttribute('aria-label', 'Reviewed excerpt classification');
      const reason = document.createElement('input'); reason.required = true; reason.maxLength = 512; reason.placeholder = 'Reason this excerpt may enter model context'; reason.setAttribute('aria-label', 'Output review reason');
      const release = document.createElement('button'); release.type = 'submit'; release.textContent = 'Release reviewed excerpt';
      const render = () => { text.value = result.streams[stream.value]; start.value = '0'; end.value = String(Math.min(new TextEncoder().encode(text.value).length, 65536)); };
      stream.addEventListener('change', render); render();
      form.append(stream, text, start, end, classification, reason, release);
      form.addEventListener('submit', async (event) => {
        event.preventDefault(); release.disabled = true;
        try {
          check();
          if (Date.now() >= expires) throw new Error('Protected output preview expired. Request a fresh preview.');
          const raw = new TextEncoder().encode(result.streams[stream.value]);
          const first = Number(start.value), last = Number(end.value);
          if (!Number.isInteger(first) || !Number.isInteger(last) || first < 0 || last <= first || last > raw.length || last > 65536) throw new Error('Choose a nonempty excerpt within the displayed output and 65536-byte limit.');
          const excerpt = raw.slice(first, last);
          new TextDecoder('utf-8', { fatal: true }).decode(excerpt);
          const hash = await crypto.subtle.digest('SHA-256', excerpt);
          check();
          const sha = Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('');
          const released = await api(`/api/home/operator/host-outputs/${encodeURIComponent(artifactId)}/review`, {
            method: 'POST', body: JSON.stringify({ expected_home_revision: homeRevision,
              selection: { stream: stream.value, byte_start: first, byte_end: last, expected_sha256: sha },
              classification: classification.value, reason: reason.value }),
          });
          check();
          status.textContent = 'Reviewed excerpt released. The original output remains protected.';
          form.remove();
          text.value = '';
          clearTimeout(expiryTimer);
          onReleased(released);
        } catch (error) { status.textContent = error.message || 'Output review was refused.'; }
        finally { if (current()) release.disabled = false; }
      });
      container.append(form);
      status.textContent = 'Review the text and select only the bytes authorized for model context. Credentials and other secret content must remain protected.';
    } catch (error) { status.textContent = error.message || 'Output preview was refused.'; }
    finally { if (current()) preview.disabled = false; }
  });
  return () => { generation++; clearTimeout(expiryTimer); container.replaceChildren(); };
}
