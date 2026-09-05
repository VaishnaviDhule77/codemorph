const BASE = '/api';

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data.detail === 'string') detail = data.detail;
      else if (data.detail && data.detail.message)
        detail = `line ${data.detail.line}: ${data.detail.message}`;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function upload(file) {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${BASE}/analyze/upload`, { method: 'POST', body: form });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      if (typeof data.detail === 'string') detail = data.detail;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  analyze: (source, filename) => post('/analyze', { source, filename }),
  analyzeUpload: (file) => upload(file),
  repository: (path) => post('/repository', { path }),
  migrate: (source, filename) => post('/migrate', { source, filename }),
  llmMigrate: (source, filename) => post('/llm-migrate', { source, filename }),
  verify: (original, migrated, filename, runTests = true) =>
    post('/verify', { original, migrated, filename, run_tests: runTests }),
  diff: (original, migrated) => post('/diff', { original, migrated }),
  pipeline: (source, filename) => post('/pipeline', { source, filename }),
  experiments: () => get('/experiments'),
  health: () => get('/health'),
};