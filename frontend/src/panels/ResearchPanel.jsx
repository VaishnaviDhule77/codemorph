import { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function ResearchPanel() {
  const [experiments, setExperiments] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.experiments()
      .then((d) => setExperiments(d.experiments))
      .catch((e) => setError(e.message));
  }, []);

  return (
    <div className="panel">
      <section className="card">
        <h2>Research question</h2>
        <blockquote className="quote">
          Can static program analysis combined with LLM-assisted code transformation
          improve the reliability of automated code migration compared with LLM-only
          transformation?
        </blockquote>
        <h3>Hypothesis</h3>
        <p>
          Static analysis improves LLM migration reliability by (a) constraining
          generation with structural context and machine-checkable constraints, and
          (b) validating output through the syntax gate, structural guard, sandboxed
          differential testing, and equivalence estimation.
        </p>
        <h3>Design</h3>
        <table className="design">
          <tbody>
            <tr>
              <th>Independent variable</th>
              <td>Migration approach: LLM-only vs static-analysis + LLM (CodeMorph)</td>
            </tr>
            <tr>
              <th>Dependent variables</th>
              <td>
                syntax success rate, test pass rate, behavioral equivalence,
                introduced defects, processing time
              </td>
            </tr>
            <tr>
              <th>Benchmark</th>
              <td>Reproducible set of small Python migration tasks (Phase 10)</td>
            </tr>
          </tbody>
        </table>
        <p className="muted">
          Every number in this panel comes from executed experiments only — results
          are never fabricated.
        </p>
      </section>

      <section className="card">
        <h2>Experiment results</h2>
        {error && <p className="error">Error: {error}</p>}
        {experiments === null && <p className="muted">Loading…</p>}
        {experiments !== null && experiments.length === 0 && (
          <p className="muted">
            No stored experiment results yet. Run the Phase 10 evaluation (benchmark
            + comparison runner) to populate this table — the API refuses to return
            numbers that were not measured.
          </p>
        )}
        {experiments !== null && experiments.length > 0 && (
          <ExperimentTable experiments={experiments} />
        )}
      </section>
    </div>
  );
}

function ExperimentTable({ experiments }) {
  const keys = Object.keys(experiments[0]);
  return (
    <table>
      <thead>
        <tr>
          {keys.map((k) => (
            <th key={k}>{k.replace(/_/g, ' ')}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {experiments.map((row, i) => (
          <tr key={i}>
            {keys.map((k) => (
              <td key={k}>{String(row[k])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}