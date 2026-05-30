/* App shell: state, sweep engine, layout. */
/* global React, ReactDOM, Model, Fmt, SLIDERS, overridesFromScenario, ControlRail,
   Headline, CoveragePanel, TreasuryPanel, FinancingPanel, RefiPanel,
   SensitivityPanel, BreakpointsPanel, PrivateCreditPanel, ContagionPanel, SEVERITY */

const { useState, useMemo, useEffect, useRef, useCallback } = React;

// Build an effective Scenario object from a preset + manual overrides.
function effectiveScenario(presetKey, ov) {
  const base = Model.SCENARIOS[presetKey];
  const sc = JSON.parse(JSON.stringify(base));
  sc.rates.sofr_bps = ov.sofr;
  sc.rates.dgs10_bps = ov._10y;
  sc.rates.dgs30_bps = ov._30y;
  sc.credit.credit_spread_bps = ov.spread;
  sc.power.power_price_pct = ov.power / 100;
  sc.ai_demand.utilization_delta = ov.util / 100;
  sc.name = base.name + "*";
  return sc;
}

function App() {
  const [presetKey, setPresetKey] = useState("base_case");
  const [capacity, setCapacity] = useState(100);
  const presetOverrides = useMemo(() => overridesFromScenario(Model.SCENARIOS[presetKey]), [presetKey]);
  const [overrides, setOverrides] = useState(presetOverrides);
  const [sweep, setSweep] = useState(false);
  const sweepRef = useRef(0);

  // when preset changes, snap overrides to that preset
  useEffect(() => { setOverrides(presetOverrides); }, [presetOverrides]);

  const dirty = useMemo(() => SLIDERS.some(s => overrides[s.key] !== presetOverrides[s.key]), [overrides, presetOverrides]);

  const project = useMemo(() => Model.buildProject(capacity), [capacity]);
  const baseProject = useMemo(() => Model.buildProject(capacity), [capacity]);

  const effSc = useMemo(() => effectiveScenario(presetKey, overrides), [presetKey, overrides]);
  const res = useMemo(() => Model.runScenario(effSc, project), [effSc, project]);
  const baseRes = useMemo(() => Model.runScenario(Model.SCENARIOS.base_case, baseProject), [baseProject]);

  // DSCR/ICR for every preset (for the coverage chart), at the active capacity
  const byScenario = useMemo(() => Model.SCENARIO_ORDER.map(k => {
    const m = Model.projectMetrics(project, Model.SCENARIOS[k]);
    return { key: k, dscr: m.dscr, icr: m.icr };
  }), [project]);

  const onOverride = useCallback((key, val) => {
    setSweep(false);
    setOverrides(o => ({ ...o, [key]: val }));
  }, []);
  const onReset = useCallback(() => { setSweep(false); setOverrides(presetOverrides); }, [presetOverrides]);
  const onSelectPreset = useCallback((k) => { setSweep(false); setPresetKey(k); }, []);

  // --- severity sweep: ramp overrides 0 -> combined_severe over time ------
  useEffect(() => {
    if (!sweep) return;
    setPresetKey("base_case");
    const target = Model.SCENARIOS.combined_severe_case;
    const targetOv = overridesFromScenario(target);
    let t0 = null, raf;
    const dur = 5200;
    const tick = (ts) => {
      if (t0 == null) t0 = ts;
      let k = ((ts - t0) % (dur * 2)) / dur;
      if (k > 1) k = 2 - k; // ping-pong
      sweepRef.current = k;
      const ov = {};
      SLIDERS.forEach(s => {
        const a = 0, b = targetOv[s.key];
        ov[s.key] = Math.round((a + (b - a) * k) / s.step) * s.step;
      });
      setOverrides(ov);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [sweep]);

  const stressLabel = useMemo(() => {
    // crude live severity read for the header chip
    const d = res.project.dscr;
    if (res.project.flags.dscr_danger || res.project.flags.fcf_danger) return { t: "Covenant breach", tone: "crit" };
    if (d < 1.6) return { t: "Elevated stress", tone: "warn" };
    if (d < 2.2) return { t: "Moderate stress", tone: "mid" };
    return { t: "Within tolerance", tone: "ok" };
  }, [res]);

  return (
    <div className="app">
      <ControlRail
        presetKey={presetKey} overrides={overrides} presetOverrides={presetOverrides}
        capacity={capacity} onCapacity={setCapacity}
        onSelectPreset={onSelectPreset} onOverride={onOverride} onReset={onReset}
        sweep={sweep} onSweep={() => setSweep(s => !s)} dirty={dirty} />

      <main className="stage">
        <header className="masthead">
          <div className="mast-left">
            <div className="kicker">Macro-financial stress model · v0</div>
            <h1>AI Data-Center Buildout — Stress Desk</h1>
            <p className="dek">A scenario-based stress test of the mixed capital structure financing the AI data-center buildout — coverage, refinancing, private-credit losses and network contagion. Every figure is live; charts morph as you move the controls.</p>
          </div>
          <div className="mast-right">
            <div className={"status-chip " + stressLabel.tone}>
              <span className="status-dot" />{stressLabel.t}
            </div>
            <div className="active-scen">
              <span className="as-label">Active scenario</span>
              <span className="as-name">{Model.SCENARIOS[presetKey].label}{dirty && <em> · modified</em>}{sweep && <em> · sweeping</em>}</span>
            </div>
          </div>
        </header>

        <Headline res={res} base={baseRes} />

        <div className="grid">
          <CoveragePanel project={project} effSc={effSc} presetKey={presetKey} byScenario={byScenario} />
          <TreasuryPanel res={res} />
          <FinancingPanel project={project} res={res} />
          <RefiPanel project={project} effSc={effSc} />
          <SensitivityPanel project={project} />
          <BreakpointsPanel project={project} effSc={effSc} />
          <PrivateCreditPanel res={res} />
          <ContagionPanel res={res} />
        </div>

        <footer className="footnote">
          <span>Faithful browser port of <code>omkmorendha/ai-datacenter-modelling</code>. Stylized, illustrative figures — not investment advice.</span>
          <span>Thresholds: DSCR 1.20× · ICR 1.50× · EBITDA margin 45% · Refi base prob. 90%</span>
        </footer>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
