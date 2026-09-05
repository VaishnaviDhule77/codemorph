import { useState } from 'react';
import { api } from '../api.js';

export default function VerifyPanel({ source, filename, migration }) {
  const [original, setOriginal] = useState(source);
  const [migrated, setMigrated] = useState(
    migration && migration.migrated_source ? migration.migrated_source : ''
  );
  const [report, setReport] = useState(null);
  const [rows, setRows] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function run() {
    setError(''); setLoading(true); setReport(null);
    try {
      const [r, d] = await Promise.all([
        api.verify(original, migrated, filename),
        api.diff(original, migrated),
      ]);
      setReport(r);
      setRows(d.rows);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="panel">
      <section className="card">
        <h2>Behavioral verification &amp; comparison</h2>
        <p className="muted">
          Both versions run on identical generated test cases in isolated sandboxes;
          return values, exception types, and stdout are compared, then combined with
          the static signals into the equivalence estimate.
        </p>
        <div className="verify-inputs">
          <div>
            <h3>Original</h3>
            <textarea
              className="code-input" rows={10} spellCheck={false}
              value={original} onChange={(e) => setOriginal(e.target.value)}
            />
          </div>
          <div>
            <h3>Migrated {migration && migration.applied ? '(pre-filled from migration)' : ''}</h3>
            <textarea
              className="code-input" rows={10} spellCheck={false}
              value={migrated} onChange={(e) => setMigrated(e.target.value)}
            />
          </div>
        </div>
        <button
          className="btn primary"
          disabled={loading || !original.trim() || !migrated.trim()}
          onClick={run}
        >
          {loading ? 'Verifying…' : 'Run verification'}
        </button>
        {error && <p className="error">Error: {error}</p>}
      </section>

      {report && (
        <>
          <section className="card">
            <h2>Equivalence estimate</h2>
            <div className="score-header">
              <div className="score-big">{report.score}%</div>
              <div>
                <div className="score-label">{report.label}</div>
                <p className="muted small">{report.disclaimer}</p>
              </div>
            </div>
            {report.signals.map((s) => (
              <div key={s.name} className="signal">
                <span className="signal-name">{s.name.replace('_', ' ')}</span>
                <div className="signal-bar">
                  <div
                    className="signal-fill"
                    style={{ width: `${Math.round(s.score * 100)}%` }}
                  />
                </div>
                <span className="signal-value">{Math.round(s.score * 100)}%</span>
              </div>
            ))}
          </section>

          <section className="card">
            <h2>Sandboxed test results</h2>
            {report.verification ? (
              <table>
                <thead>
                  <tr><th>function</th><th>case</th><th>result</th><th>detail</th></tr>
                </thead>
                <tbody>
                  {report.verification.outcomes.map((o, i) => (
                    <tr key={i}>
                      <td className="mono">{o.case.function}</td>
                      <td>{o.case.description}</td>
                      <td>
                        <span className={`badge ${o.status.toLowerCase()}`}>{o.status}</span>
                      </td>
                      <td className="muted mono small">{o.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">No test run (static-only estimate).</p>
            )}
          </section>

          <section className="card">
            <h2>Side-by-side comparison</h2>
            <DiffView rows={rows} />
          </section>
        </>
      )}
    </div>
  );
}

function DiffView({ rows }) {
  return (
    <div className="diff">
      <div className="diff-pane">
        <div className="diff-pane-title">Original</div>
        {rows.map((r, i) => (
          <div key={i} className="diff-row">
            <span className="diff-num">{r.old === null ? '' : r.old}</span>
            <span
              className={
                'diff-text ' +
                (r.type === 'removed' ? 't-removed' :
                 r.type === 'changed' ? 't-changed' : 't-same')
              }
            >
              {r.old_text === null ? '' : r.old_text}
            </span>
          </div>
        ))}
      </div>
      <div className="diff-pane">
        <div className="diff-pane-title">Migrated</div>
        {rows.map((r, i) => (
          <div key={i} className="diff-row">
            <span className="diff-num">{r.new === null ? '' : r.new}</span>
            <span
              className={
                'diff-text ' +
                (r.type === 'added' ? 't-added' :
                 r.type === 'changed' ? 't-changed' : 't-same')
              }
            >
              {r.new_text === null ? '' : r.new_text}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}