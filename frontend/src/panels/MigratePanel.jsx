import { useState } from 'react';
import { api } from '../api.js';

export default function MigratePanel({ source, filename, migration, setMigration, setTab }) {
  const [error, setError] = useState('');
  const [llm, setLlm] = useState(null);
  const [llmError, setLlmError] = useState('');
  const [loading, setLoading] = useState('');

  async function run(kind) {
    const isLlm = kind === 'llm';
    if (isLlm) { setLlmError(''); setLlm(null); } else { setError(''); }
    setLoading(kind);
    try {
      if (isLlm) setLlm(await api.llmMigrate(source, filename));
      else setMigration(await api.migrate(source, filename));
    } catch (e) {
      if (isLlm) setLlmError(e.message); else setError(e.message);
    } finally {
      setLoading('');
    }
  }

  return (
    <div className="panel">
      <section className="card">
        <h2>Deterministic migration (Phase 4)</h2>
        <p className="muted">
          Traceable, syntax-preserving transformations applied to the analyzed source.
          Each change records type, location, original, replacement, and reason.
        </p>
        <button className="btn primary" disabled={loading !== ''} onClick={() => run('det')}>
          {loading === 'det' ? 'Migrating…' : 'Run deterministic migration'}
        </button>
        {error && <p className="error">Error: {error}</p>}
        {migration && <MigrationReport migration={migration} setTab={setTab} />}
      </section>

      <section className="card">
        <h2>LLM-assisted migration (Phase 7)</h2>
        <p className="muted">
          The LLM receives the analysis context and machine-checkable constraints; its
          output passes the full rejection pipeline: syntax gate → structural guard →
          sandboxed differential tests → equivalence estimate. Rejected generations
          return the original unchanged.
        </p>
        <button className="btn" disabled={loading !== ''} onClick={() => run('llm')}>
          {loading === 'llm' ? 'Generating…' : 'Run LLM migration'}
        </button>
        {llmError && <p className="error">Error: {llmError}</p>}
        {llm && <LLMReport result={llm} setMigration={setMigration} setTab={setTab} />}
      </section>
    </div>
  );
}

function MigrationReport({ migration, setTab }) {
  return (
    <div>
      <p>
        <b>{migration.transformation_count}</b> transformation(s),{' '}
        {migration.applied ? 'applied' : (migration.rejected_reason ? 'rejected' : 'no-op')}
        {migration.rejected_reason && (
          <span className="error"> — {migration.rejected_reason}</span>
        )}
      </p>
      {migration.transformations.map((t, i) => (
        <div key={i} className="transform">
          <div className="transform-head">
            <span className="mono">{t.kind}</span>
            <span className={`badge risk-${t.risk.toLowerCase()}`}>{t.risk}</span>
            <span className="muted">line {t.line}</span>
          </div>
          {(t.original || '').split('\n').map((line, j) => (
            <div key={`o${j}`} className="diff-line removed">- {line}</div>
          ))}
          {(t.replacement || '').split('\n').map((line, j) => (
            <div key={`r${j}`} className="diff-line added">+ {line}</div>
          ))}
          <p className="muted small">{t.reason}</p>
        </div>
      ))}
      {migration.applied && (
        <>
          <h3>Migrated source</h3>
          <pre className="code">{migration.migrated_source}</pre>
          <button className="btn primary" onClick={() => setTab('verify')}>
            Verify &amp; compare →
          </button>
        </>
      )}
    </div>
  );
}

function LLMReport({ result, setMigration, setTab }) {
  if (result.status !== 'ACCEPTED') {
    return (
      <div className="llm-status">
        <p><b>Status: {result.status}</b></p>
        {result.rejection_reason && <p className="error">{result.rejection_reason}</p>}
        {result.status === 'NOT_CONFIGURED' && (
          <p className="muted">
            To enable: set <code>CODEMORPH_LLM_PROVIDER=openai</code> and{' '}
            <code>CODEMORPH_LLM_API_KEY</code> in the backend environment, then restart
            the server. Without a provider, this button demonstrates the rejection
            pipeline's NOT_CONFIGURED path.
          </p>
        )}
      </div>
    );
  }
  return (
    <div>
      <p>
        <b>ACCEPTED</b> by model <span className="mono">{result.model}</span>
        {result.flagged && <span className="badge sev-high"> FLAGGED — review required</span>}
      </p>
      <p className="muted">
        Findings: {result.findings_before} → {result.findings_after}
      </p>
      {result.warnings.length > 0 && (
        <p className="warning">⚠ {result.warnings.join(' | ')}</p>
      )}
      <h3>Migrated source</h3>
      <pre className="code">{result.migrated_source}</pre>
      <button
        className="btn primary"
        onClick={() => {
          setMigration({
            applied: true,
            transformation_count: 0,
            transformations: [],
            migrated_source: result.migrated_source,
            syntax_valid: true,
            structural_guard_passed: true,
            rejected_reason: null,
          });
          setTab('verify');
        }}
      >
        Verify &amp; compare →
      </button>
    </div>
  );
}