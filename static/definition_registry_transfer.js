import { request as apiRequest } from './api_client.js';

export async function exportDefinitions(setStatus) {
  try {
    const documentValue = await apiRequest('/api/definitions/export');
    const host = document.getElementById('definition-transfer-document');
    if (host) host.value = JSON.stringify(documentValue, null, 2);
    setStatus('Loaded ' + (documentValue.records?.length || 0)
      + ' tenant-visible definition record(s) for export.');
  } catch (error) {
    setStatus('Definition export failed: ' + error.message);
  }
}

export async function importDefinitions({ canManage, setStatus, refresh }) {
  const raw = document.getElementById('definition-transfer-document')?.value || '';
  let documentValue;
  try {
    documentValue = JSON.parse(raw);
  } catch (error) {
    setStatus('Import document is not valid JSON: ' + error.message);
    return;
  }
  const imported = Array.isArray(documentValue?.records) ? documentValue.records : [];
  if (!imported.length) {
    setStatus('Import document must contain at least one definition record.');
    return;
  }
  const blocked = imported.filter((record) => !canManage(record.scope_type));
  if (blocked.length) {
    const scopes = [...new Set(blocked.map((record) => record.scope_type))].join(', ');
    setStatus('Current actor/assurance cannot import every requested scope ('
      + scopes + '); nothing was submitted.');
    return;
  }
  if (!window.confirm(
    'Import ' + imported.length
      + ' versioned definition record(s) as new inactive drafts? '
      + 'All records are revalidated server-side and none are published automatically.',
  )) return;
  try {
    const response = await apiRequest('/api/definitions/import', {
      method: 'POST',
      body: JSON.stringify({ document: documentValue }),
    });
    setStatus('Imported ' + response.count + ' record(s) as inactive draft revisions.');
    refresh();
  } catch (error) {
    setStatus('Definition import failed: ' + error.message);
  }
}
