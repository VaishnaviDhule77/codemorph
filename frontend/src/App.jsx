import { useState } from 'react';
import AnalyzePanel from './panels/AnalyzePanel.jsx';
import MigratePanel from './panels/MigratePanel.jsx';
import VerifyPanel from './panels/VerifyPanel.jsx';
import ResearchPanel from './panels/ResearchPanel.jsx';

const SAMPLE = `import os

def greet(name):
    return 'Hello %s!' % (name,)

def bump(x):
    x = x + 1
    x = x + 1
    x = x + 1
    return x
`;

export default function App() {
  const [tab, setTab] = useState('analyze');
  const [source, setSource] = useState(SAMPLE);
  const [filename, setFilename] = useState('legacy.py');
  const [analysis, setAnalysis] = useState(null);
  const [migration, setMigration] = useState(null);

  return (
    <div className="app">
      <header className="header">
        <h1>Code<span className="accent">Morph</span></h1>
        <p className="tagline">
          AI-assisted code migration &amp; semantic-equivalence analysis
        </p>
      </header>
      <nav className="tabs">
        {[
          ['analyze', 'Analyze'],
          ['migrate', 'Migrate'],
          ['verify', 'Verify & Compare'],
          ['research', 'Research'],
        ].map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? 'tab active' : 'tab'}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      <main>
        {tab === 'analyze' && (
          <AnalyzePanel
            source={source} setSource={setSource}
            filename={filename} setFilename={setFilename}
            analysis={analysis} setAnalysis={setAnalysis}
          />
        )}
        {tab === 'migrate' && (
          <MigratePanel
            source={source} filename={filename}
            migration={migration} setMigration={setMigration}
            setTab={setTab}
          />
        )}
        {tab === 'verify' && (
          <VerifyPanel source={source} filename={filename} migration={migration} />
        )}
        {tab === 'research' && <ResearchPanel />}
      </main>
      <footer className="footer">
        Equivalence scores are empirical estimates from multiple signals — not formal proofs.
      </footer>
    </div>
  );
}