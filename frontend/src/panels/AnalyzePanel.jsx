import { useRef, useState } from 'react';
import { api } from '../api.js';

function Metric({ label, value }) {
  return (
    <div className="metric">
      <div className="metric-value">{value}</div>
      <div className="metric-label">{label}</div>
    </div>
  );
}

function FindingRow({ f }) {
  return (
    <tr>
      <td><span className={`badge sev-${f.severity.toLowerCase()}`}>{f.severity}</span></td>
      <td>{f.line}</td>
      <td className="mono">{f.category}</td>
      <td>{f.message}</td>
      <td className="muted small">{f.suggestion}</td>
    </tr>
  );
}

export default function AnalyzePanel(props) {
  const { source, setSource, filename, setFilename, analysis, setAnalysis } = props;
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [repoPath, setRepoPath] = useState('');
  const [repo, setRepo] = useState(null);
  const [repoError, setRepoError] = useState('');
  const fileRef = useRef(null);

  async function runAnalyze() {
    setError(''); setLoading(true); setRepo(null);
    try {
      setAnalysis(await api.analyze(source, filename));
    } catch (e) {
      setError(e.message);
      setAnalysis(null);
    } finally {
      setLoading(false);
    }
  }

  async function onFile(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    setError('');
    setFilename(file.name);
    try {
      const text = await file.text();
      setSource(text);
      setAnalysis(await api.analyzeUpload(file));
    } catch (e) {
      setError(e.message);
    }
    e.target.value = '';
  }

  async function runRepo() {
    setRepoError(''); setRepo(null);
    try {
      setRepo(await api.repository(repoPath));
    } catch (e) {
      setRepoError(e.message);
    }
  }

  const m = analysis && analysis.metrics;
  const allFindings = analysis
    ? [...analysis.findings, ...analysis.flow_findings]
    : [];

  return (
    <div className="panel">
      <section className="card">
        <h2>Source code</h2>
        <div className="row">
          <input
            value={filename}
            onChange={(e) => setFilename(e.target.value)}
            className="input filename"
          />
          <button onClick={() => fileRef.current && fileRef.current.click()} className="btn">
            Upload .py file
          </button>
          <input ref={fileRef} type="file" accept=".py" hidden onChange={onFile} />
          <button onClick={runAnalyze} disabled={loading || !source.trim()} className="btn primary">
            {loading ? 'Analyzing…' : 'Analyze'}
          </button>
        </div>
        <textarea
          className="code-input" rows={12} spellCheck={false}
          value={source} onChange={(e) => setSource(e.target.value)}
        />
        {error && <p className="error">Error: {error}</p>}
      </section>

      {analysis && (
        <>
          <section className="card">
            <h2>Metrics</h2>
            <div className="metrics-grid">
              <Metric label="lines (code)" value={`${m.total_lines} (${m.code_lines})`} />
              <Metric label="functions" value={m.num_functions} />
              <Metric label="classes" value={m.num_classes} />
              <Metric label="imports" value={m.num_imports} />
              <Metric label="max nesting" value={m.max_nesting_depth} />
              <Metric label="findings" value={allFindings.length} />
            </div>
            <div className="complexity">
              {analysis.complexity.functions.map((f) => (
                <span key={f.qualified_name} className="complexity-chip">
                  {f.qualified_name}: <b>{f.complexity}</b>
                  <span className={`rank rank-${f.rank.toLowerCase()}`}>{f.rank}</span>
                </span>
              ))}
            </div>
          </section>

          <section className="card">
            <h2>Findings ({allFindings.length})</h2>
            {allFindings.length === 0 ? (
              <p className="muted">No findings — clean code.</p>
            ) : (
              <table>
                <thead>
                  <tr><th>severity</th><th>line</th><th>category</th><th>message</th><th>suggestion</th></tr>
                </thead>
                <tbody>{allFindings.map((f, i) => <FindingRow key={i} f={f} />)}</tbody>
              </table>
            )}
          </section>

          <section className="card">
            <h2>AST structure</h2>
            <pre className="tree">{analysis.structure}</pre>
          </section>
        </>
      )}

      <section className="card">
        <h2>Repository analysis</h2>
        <div className="row">
          <input
            className="input"
            placeholder="/workspaces/codemorph or any local path"
            value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)}
          />
          <button onClick={runRepo} disabled={!repoPath.trim()} className="btn">
            Analyze repository
          </button>
        </div>
        {repoError && <p className="error">Error: {repoError}</p>}
        {repo && <RepoReport repo={repo} />}
      </section>
    </div>
  );
}

function RepoReport({ repo }) {
  return (
    <div>
      <div className="metrics-grid">
        <Metric label="files (analyzed)" value={`${repo.files_analyzed}/${repo.files_discovered}`} />
        <Metric label="functions" value={repo.totals.functions} />
        <Metric label="lines (code)" value={repo.totals.lines_code} />
        <Metric label="findings" value={repo.totals.findings} />
      </div>
      {repo.highest_complexity && (
        <p>
          Highest complexity: <b className="mono">{repo.highest_complexity.function}</b>
          {' '}in <span className="mono">{repo.highest_complexity.file}</span>
          {' '}(complexity {repo.highest_complexity.complexity})
        </p>
      )}
      <h3>High-risk files</h3>
      <ol className="risk-list">
        {repo.high_risk_files.filter((f) => f.risk_score > 0).map((f) => (
          <li key={f.path}>
            <span className="mono">{f.path}</span>
            <span className="badge risk">risk {f.risk_score}</span>
          </li>
        ))}
      </ol>
      {repo.dependencies.length > 0 && (
        <>
          <h3>Internal dependencies ({repo.dependencies.length})</h3>
          <ul className="dep-list">
            {repo.dependencies.map((d, i) => (
              <li key={i} className="mono">{d.source} → {d.target}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}