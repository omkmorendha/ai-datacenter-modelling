/* Control rail: scenario presets + live shock-override sliders + sweep. */
/* global React, Model, Fmt, C */

const SLIDERS = [
  { key: "sofr", label: "SOFR shock", min: -50, max: 400, step: 25, unit: "bps", read: s => s.rates.sofr_bps },
  { key: "_10y", label: "10Y Treasury", min: -50, max: 400, step: 25, unit: "bps", read: s => s.rates.dgs10_bps },
  { key: "_30y", label: "30Y Treasury", min: -50, max: 400, step: 25, unit: "bps", read: s => s.rates.dgs30_bps },
  { key: "spread", label: "Credit spread", min: 0, max: 500, step: 25, unit: "bps", read: s => s.credit.credit_spread_bps },
  { key: "power", label: "Power price", min: -20, max: 100, step: 5, unit: "%", read: s => Math.round(s.power.power_price_pct * 100) },
  { key: "util", label: "Utilization", min: -40, max: 10, step: 5, unit: "pp", read: s => Math.round(s.ai_demand.utilization_delta * 100) },
];

const SEVERITY = {
  base_case: 0, high_for_longer: 1, treasury_term_premium_shock: 2,
  iran_oil_inflation_shock: 2, ai_revenue_disappointment: 3,
  severe_private_credit_liquidity_crunch: 3, combined_severe_case: 4,
};

function overridesFromScenario(sc) {
  const o = {};
  SLIDERS.forEach(s => { o[s.key] = s.read(sc); });
  return o;
}

function Slider({ cfg, value, presetValue, onChange }) {
  const pct = (v) => ((v - cfg.min) / (cfg.max - cfg.min)) * 100;
  const modified = value !== presetValue;
  const disp = cfg.unit === "bps"
    ? (value >= 0 ? "+" : "−") + Math.abs(value) + " bps"
    : (value >= 0 ? "+" : "−") + Math.abs(value) + (cfg.unit === "%" ? "%" : " pp");
  return (
    <div className="sld">
      <div className="sld-head">
        <span className="sld-label">{cfg.label}{modified && <span className="sld-dot" title="modified from preset" />}</span>
        <span className={"sld-val" + (modified ? " mod" : "")}>{disp}</span>
      </div>
      <div className="sld-track-wrap">
        <input type="range" min={cfg.min} max={cfg.max} step={cfg.step} value={value}
          onChange={e => onChange(cfg.key, Number(e.target.value))}
          style={{ "--fill": pct(value) + "%" }} />
        <span className="sld-ghost" style={{ left: pct(presetValue) + "%" }} title={"preset " + presetValue} />
      </div>
    </div>
  );
}

function ControlRail({ presetKey, overrides, presetOverrides, capacity, onCapacity, onSelectPreset, onOverride, onReset, sweep, onSweep, dirty }) {
  return (
    <aside className="rail">
      <div className="rail-sec">
        <div className="rail-eyebrow">Scenario</div>
        <ul className="scen-list">
          {Model.SCENARIO_ORDER.map(k => {
            const sc = Model.SCENARIOS[k];
            const active = k === presetKey;
            return <li key={k}>
              <button className={"scen-btn" + (active ? " active" : "")} onClick={() => onSelectPreset(k)}>
                <span className="scen-name">{sc.label}</span>
                <span className="sev-dots">
                  {[0, 1, 2, 3].map(i => <span key={i} className={"sev" + (i < SEVERITY[k] ? " on" : "")} />)}
                </span>
              </button>
            </li>;
          })}
        </ul>
      </div>

      <div className="rail-sec">
        <div className="rail-eyebrow">Project size</div>
        <div className="cap-row">
          <button className="cap-btn" onClick={() => onCapacity(Math.max(10, capacity - 10))}>−</button>
          <div className="cap-val"><b>{capacity}</b><span>MW</span></div>
          <button className="cap-btn" onClick={() => onCapacity(Math.min(2000, capacity + 10))}>+</button>
        </div>
      </div>

      <div className="rail-sec">
        <div className="rail-eyebrow-row">
          <span className="rail-eyebrow">Manual shock overrides</span>
          {dirty && <button className="reset-btn" onClick={onReset}>reset</button>}
        </div>
        {SLIDERS.map(cfg => (
          <Slider key={cfg.key} cfg={cfg} value={overrides[cfg.key]} presetValue={presetOverrides[cfg.key]}
            onChange={onOverride} />
        ))}
      </div>

      <div className="rail-sec">
        <button className={"sweep-btn" + (sweep ? " on" : "")} onClick={onSweep}>
          <span className="sweep-ico">{sweep ? "■" : "▶"}</span>
          {sweep ? "Stop severity sweep" : "Auto-sweep stress 0→max"}
        </button>
      </div>
    </aside>
  );
}

Object.assign(window, { SLIDERS, SEVERITY, overridesFromScenario, ControlRail, Slider });
