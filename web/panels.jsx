/* Dashboard panels for the AI-DC stress model. */
/* global React, Model, Fmt, C, GroupedBars, YieldCurve, StackedBar, HBars, SensLines, HeadroomBar, HeatGrid, AnimNum, useTween */

const STACK_COLORS = {
  hyperscaler_retained_earnings: "#2f2b26",
  corporate_bonds: "#574f44",
  bank_credit_facility: "#7d7568",
  syndicated_loans: "#a39884",
  private_credit: "#c0492f",
  abs_cmbs_securitization: "#b07c4a",
  project_finance_spv: "#8a6d3b",
  lease_obligations: "#5e7150",
  equity_jv: "#869376",
};

function Panel({ title, sub, children, wide, span, className = "" }) {
  return (
    <section className={"panel " + className} style={span ? { gridColumn: "span " + span } : null}>
      <header className="panel-h">
        <h2 className="panel-title">{title}</h2>
        {sub && <p className="panel-sub">{sub}</p>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

function Legend({ items }) {
  return <div className="legend">
    {items.map((it, i) => <span key={i} className="leg-item">
      <span className="leg-sw" style={it.dash ? { background: "none", borderTop: "2px dashed " + it.color, height: 0, marginTop: 7 } : { background: it.color }} />
      {it.label}</span>)}
  </div>;
}

function Stat({ label, value, tone }) {
  return <div className={"stat" + (tone ? " " + tone : "")}>
    <div className="stat-v">{value}</div>
    <div className="stat-l">{label}</div>
  </div>;
}

// 1 --- headline coverage strip -----------------------------------------
function Headline({ res, base }) {
  const pm = res.project, fs = res.financing_stack;
  const tiles = [
    { k: "DSCR", v: pm.dscr, fmt: v => v.toFixed(2), danger: pm.flags.dscr_danger, base: base.project.dscr, hint: "EBITDA / debt service" },
    { k: "ICR", v: pm.icr, fmt: v => v.toFixed(2), danger: pm.flags.icr_danger, base: base.project.icr, hint: "EBITDA / interest" },
    { k: "EBITDA margin", v: pm.ebitda_margin, fmt: v => Fmt.pct(v, 1), danger: pm.flags.margin_danger, base: base.project.ebitda_margin, hint: "EBITDA / revenue" },
    { k: "WACD", v: fs.wacd, fmt: v => Fmt.pct(v, 2), danger: false, base: base.financing_stack.wacd, hint: "Wtd. avg cost of debt", up: true },
    { k: "FCF a. capex", v: pm.fcf_after_capex, fmt: Fmt.money, danger: pm.flags.fcf_danger, base: base.project.fcf_after_capex, hint: "EBITDA − int − maint" },
  ];
  return (
    <div className="headline">
      {tiles.map((t, i) => {
        const delta = t.v - t.base;
        const worse = t.up ? delta > 0 : delta < 0;
        const flat = Math.abs(delta) < 1e-9;
        const dtxt = t.k === "FCF a. capex" ? Fmt.money(Math.abs(delta))
          : (t.k.includes("margin") || t.k === "WACD") ? Fmt.pct(Math.abs(delta), 2)
          : Math.abs(delta).toFixed(2);
        return <div key={i} className={"tile" + (t.danger ? " danger" : "")}>
          <div className="tile-k">{t.k}{t.danger && <span className="flag">breach</span>}</div>
          <div className="tile-v"><AnimNum value={t.v} fmt={v => isFinite(t.v) ? t.fmt(v) : "n/a"} /></div>
          <div className="tile-meta">
            <span className={"tile-delta" + (flat ? " flat" : worse ? " worse" : " better")}>
              {flat ? "— at base" : (worse ? "▼ " : "▲ ") + dtxt}
            </span>
            <span className="tile-hint">{t.hint}</span>
          </div>
        </div>;
      })}
    </div>
  );
}

// 2 --- project & coverage ----------------------------------------------
const SHORT_LABEL = {
  base_case: "Base", high_for_longer: "High·long", treasury_term_premium_shock: "Term prem",
  iran_oil_inflation_shock: "Oil/infl", ai_revenue_disappointment: "AI rev",
  severe_private_credit_liquidity_crunch: "PC crunch", combined_severe_case: "Combined",
};
function CoveragePanel({ project, effSc, presetKey, byScenario }) {
  const pm = Model.projectMetrics(project, effSc);
  const activeIdx = Model.SCENARIO_ORDER.indexOf(presetKey);
  const data = byScenario.map(d => ({
    label: SHORT_LABEL[d.key],
    DSCR: d.dscr, ICR: Math.min(d.icr, 12),
  }));
  const econ = [
    ["Revenue", pm.revenue, false],
    ["Power expense", -pm.power_expense, true],
    ["Opex ex-power", -pm.opex_ex_power, true],
    ["EBITDA", pm.ebitda, false, true],
    ["Interest expense", -pm.interest_expense, true],
    ["Maint. capex", -(pm.revenue * project.maintCapexPct), true],
    ["FCF after capex", pm.fcf_after_capex, false, true],
  ];
  const maxAbs = Math.max(...econ.map(e => Math.abs(e[1])));
  return (
    <Panel title="Project & coverage" span={4} sub={`Representative ${project.capacityMw} MW build · ${Fmt.pct(project.leverage,0)} leverage`}>
      <div className="cov-grid">
        <div className="cov-chart">
          <Legend items={[{ label: "DSCR", color: C.ink }, { label: "ICR (capped 12×)", color: C.graphite }]} />
          <GroupedBars data={data} activeIndex={activeIdx}
            series={[{ key: "DSCR", color: C.ink }, { key: "ICR", color: C.graphite }]}
            threshold={1.2} height={272} fmt={v => v.toFixed(1)} />
          <p className="micro">Highlighted group = active scenario incl. your live overrides. Dashed line = 1.20× DSCR covenant floor.</p>
        </div>
        <div className="econ">
          <div className="econ-head"><span>Annual economics</span></div>
          {econ.map((e, i) => (
            <div key={i} className={"econ-row" + (e[3] ? " tot" : "")}>
              <span className="econ-label">{e[0]}</span>
              <div className="econ-bar-wrap">
                <div className={"econ-bar" + (e[2] ? " neg" : "") + (e[3] ? " accentbar" : "")}
                  style={{ width: (Math.abs(e[1]) / maxAbs * 100) + "%" }} />
              </div>
              <span className="econ-val"><AnimNum value={e[1]} fmt={Fmt.money} /></span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

// 3 --- treasury / SOFR --------------------------------------------------
function TreasuryPanel({ res }) {
  const t = res.treasury, curve = res.yield_curve;
  return (
    <Panel title="Treasury & SOFR curve" span={2} sub="Risk-free repricing + Treasury Stress Index">
      <Legend items={[{ label: "Base", color: C.graphite, dash: true }, { label: "Shocked", color: C.accent }]} />
      <YieldCurve curve={curve} height={196} />
      <div className="stat-row three">
        <Stat label="Treasury Stress Index" value={<AnimNum value={t.treasury_stress_index} fmt={v => v.toFixed(2)} />} tone={t.treasury_stress_index > 3 ? "warn" : null} />
        <Stat label="Securitization openness" value={<AnimNum value={t.securitization_openness_score} fmt={v => v.toFixed(2)} />} tone={t.securitization_openness_score < 0.4 ? "warn" : null} />
        <Stat label="Refi cost shock" value={<AnimNum value={t.refinancing_cost_shock} fmt={v => Fmt.pct(v, 2)} />} />
      </div>
    </Panel>
  );
}

// 4 --- financing stack --------------------------------------------------
function FinancingPanel({ project, res }) {
  const fs = res.financing_stack;
  const segs = project.sources.map(s => ({ name: s.name, label: s.label, value: s.amount, color: STACK_COLORS[s.name] }));
  const lenders = Object.keys(fs.exposure_by_lender_type)
    .map(k => ({ label: Model.LENDER_LABEL[k] || k, value: fs.exposure_by_lender_type[k] }))
    .sort((a, b) => b.value - a.value);
  return (
    <Panel title="Mixed financing stack" span={3} sub="Not all floating private credit — a blended capital structure">
      <StackedBar segments={segs} height={26} />
      <div className="stack-legend">
        {segs.map((s, i) => <span key={i} className="leg-item sm">
          <span className="leg-sw" style={{ background: s.color }} />{s.label}
          <em>{Fmt.pct(s.value / fs.total_debt, 0)}</em></span>)}
      </div>
      <div className="float-row">
        <FloatBar label="Floating — gross" value={fs.floating_share_gross} color={C.graphite} />
        <FloatBar label="Floating — net of hedges" value={fs.floating_share_net} color={C.accent} />
      </div>
      <p className="micro">Only the <b>net</b> floating slice ({Fmt.pct(fs.floating_share_net, 1)}) reprices live with SOFR; hedges &amp; swaps fix another {Fmt.pct(fs.floating_share_gross - fs.floating_share_net, 1)}.</p>
      <div className="lender-block">
        <div className="block-eyebrow">Exposure by lender type</div>
        <HBars data={lenders} color={C.graphite} labelW={112} fmt={Fmt.money} />
      </div>
    </Panel>
  );
}

function FloatBar({ label, value, color }) {
  const v = useTween(value);
  return <div className="floatbar">
    <div className="fb-top"><span>{label}</span><b>{Fmt.pct(value, 1)}</b></div>
    <div className="fb-track"><div className="fb-fill" style={{ width: (v * 100) + "%", background: color }} /></div>
  </div>;
}

// 5 --- refinancing wall -------------------------------------------------
function RefiPanel({ project, effSc }) {
  const gap = Model.refinancingGap(project, effSc);
  const years = Object.keys(gap);
  const data = years.map(y => ({ label: y, mat: gap[y].maturing_principal, risk: gap[y].principal_at_risk }));
  const refiProb = years.length ? gap[years[0]].refi_probability : 0;
  return (
    <Panel title="Refinancing wall" span={3} sub="Maturity wall vs. scenario-adjusted principal at risk">
      <Legend items={[{ label: "Maturing", color: "#cfc8bb" }, { label: "At risk", color: C.accent }]} />
      <GroupedBars data={data}
        series={[{ key: "mat", color: "#cfc8bb" }, { key: "risk", color: C.accent }]}
        height={228} fmt={v => "$" + (v / 1e6).toFixed(0) + "M"} />
      <div className="stat-row two">
        <Stat label="Refi probability" value={<AnimNum value={refiProb} fmt={v => Fmt.pct(v, 0)} />} tone={refiProb < 0.6 ? "warn" : null} />
        <Stat label="Total at risk" value={<AnimNum value={data.reduce((a, d) => a + d.risk, 0)} fmt={Fmt.money} />} tone={refiProb < 0.6 ? "warn" : null} />
      </div>
      <p className="micro">At risk = maturing × (1 − refi prob). Base 90%, adjusted by scenario's refi-probability delta.</p>
    </Panel>
  );
}

// 6 --- sensitivity ------------------------------------------------------
const AXES = [
  { key: "sofr_bps", label: "SOFR", fmt: v => (v >= 0 ? "+" : "") + v },
  { key: "dgs10_bps", label: "10Y", fmt: v => (v >= 0 ? "+" : "") + v },
  { key: "credit_spread_bps", label: "Spread", fmt: v => (v >= 0 ? "+" : "") + v },
  { key: "power_price_pct", label: "Power", fmt: v => (v * 100).toFixed(0) + "%" },
  { key: "utilization_delta", label: "Util", fmt: v => (v * 100).toFixed(0) },
  { key: "revenue_per_mw_pct", label: "Rev/MW", fmt: v => (v * 100).toFixed(0) + "%" },
];
function SensitivityPanel({ project }) {
  const [axis, setAxis] = React.useState("sofr_bps");
  const rows = Model.sensitivityTable(project, axis);
  const cfg = AXES.find(a => a.key === axis);
  return (
    <Panel title="Sensitivity sweep" span={3} sub="One-axis stress sweep from the base case">
      <div className="axis-tabs">
        {AXES.map(a => <button key={a.key} className={"axis-tab" + (a.key === axis ? " on" : "")} onClick={() => setAxis(a.key)}>{a.label}</button>)}
      </div>
      <Legend items={[{ label: "DSCR", color: C.ink }, { label: "ICR", color: C.graphite, dash: true }, { label: "1.0× floor", color: C.accent, dash: true }]} />
      <SensLines rows={rows} axis={axis} height={224} threshold={1.0} xfmt={cfg.fmt} />
    </Panel>
  );
}

// 7 --- breakpoints ------------------------------------------------------
function BreakpointsPanel({ project, effSc }) {
  const bp = Model.computeBreakpoints(project);
  const rows = [
    { label: "SOFR → DSCR breach", cur: effSc.rates.sofr_bps, breach: bp.max_sofr_shock_bps_before_dscr_breach, max: 5000, lowerWorse: false,
      txt: bp.max_sofr_shock_bps_before_dscr_breach == null ? "no breach" : "+" + Math.round(bp.max_sofr_shock_bps_before_dscr_breach) + " bps" },
    { label: "Spread → FCF<0", cur: effSc.credit.credit_spread_bps, breach: bp.max_credit_spread_bps_before_fcf_negative, max: 4000, lowerWorse: false,
      txt: bp.max_credit_spread_bps_before_fcf_negative == null ? "no breach" : "+" + Math.round(bp.max_credit_spread_bps_before_fcf_negative) + " bps" },
    { label: "Power → margin breach", cur: effSc.power.power_price_pct, breach: bp.max_power_price_pct_before_margin_breach, max: 1.0, lowerWorse: false,
      txt: bp.max_power_price_pct_before_margin_breach == null ? "no breach" : "+" + Math.round(bp.max_power_price_pct_before_margin_breach * 100) + "%" },
    { label: "Min util → covenant", cur: Math.max(0, project.utilization + effSc.ai_demand.utilization_delta), breach: bp.min_utilization_before_covenant_breach, max: 1.0, lowerWorse: true,
      txt: bp.min_utilization_before_covenant_breach == null ? "no breach" : Fmt.pct(bp.min_utilization_before_covenant_breach, 1) },
  ];
  return (
    <Panel title="Breakpoints · headroom" span={3} sub="Room on each axis before a metric breaks · ● = your current shock">
      <div className="hr-list">
        {rows.map((r, i) => <HeadroomBar key={i} label={r.label} current={r.cur} breach={r.breach}
          max={r.max} lowerWorse={r.lowerWorse} breachText={r.txt} />)}
      </div>
      <p className="micro">Green = safe band up to the breakpoint (│). The ● marker is where your active scenario currently sits on that axis.</p>
    </Panel>
  );
}

// 8 --- private credit ---------------------------------------------------
function PrivateCreditPanel({ res }) {
  const pc = res.private_credit;
  const alloc = Object.keys(pc.investor_loss_allocation_usd)
    .map(k => ({ label: PC_LABEL[k] || k, value: pc.investor_loss_allocation_usd[k] }))
    .sort((a, b) => b.value - a.value);
  return (
    <Panel title="Private credit sector" span={3} sub="$250B AI-DC book · losses &amp; liquidity stress">
      <div className="stat-row three">
        <Stat label="Stressed PD" value={<AnimNum value={pc.stressed_pd} fmt={v => Fmt.pct(v, 1)} />} tone={pc.stressed_pd > 0.05 ? "warn" : null} />
        <Stat label="Recovery" value={<AnimNum value={pc.stressed_recovery} fmt={v => Fmt.pct(v, 0)} />} />
        <Stat label="Expected loss" value={<AnimNum value={pc.expected_loss_realized_usd} fmt={Fmt.money} />} tone={pc.expected_loss_realized_usd > 8e9 ? "warn" : null} />
      </div>
      <div className="dual-meter">
        <Meter label="Liquidity stress" value={pc.liquidity_stress_score} />
        <Meter label="Redemption gating proxy" value={pc.redemption_gating_stress_proxy} />
      </div>
      <div className="lender-block">
        <div className="block-eyebrow">Loss allocation by investor base</div>
        <HBars data={alloc} colorFn={() => C.accent} labelW={120} fmt={Fmt.money} />
      </div>
    </Panel>
  );
}
const PC_LABEL = { bdcs: "BDCs", private_credit_funds: "Private credit funds", insurers: "Insurers", pensions: "Pensions", banks: "Banks", sovereign_wealth: "Sovereign wealth", other: "Other" };

function Meter({ label, value }) {
  const v = useTween(value);
  const danger = value > 0.5;
  return <div className="meter">
    <div className="meter-top"><span>{label}</span><b className={danger ? "danger" : ""}>{Fmt.pct(value, 0)}</b></div>
    <div className="meter-track">
      <div className="meter-fill" style={{ width: (v * 100) + "%", background: danger ? C.accent : C.gold }} />
      {[0.25, 0.5, 0.75].map(t => <span key={t} className="meter-notch" style={{ left: (t * 100) + "%" }} />)}
    </div>
  </div>;
}

// 9 --- contagion --------------------------------------------------------
function ContagionPanel({ res }) {
  const cg = res.contagion;
  const heat = cg.heatmap.slice().sort((a, b) => b.total_loss - a.total_loss)
    .map(h => ({ label: h.label, value: h.total_loss }));
  const maxLoss = Math.max(...heat.map(h => h.value), 1e-9);
  return (
    <Panel title="Network contagion" span={6} sub="Stylized exposure-matrix transmission across the AI-DC financing ecosystem · 3-round propagation">
      <div className="contagion-grid">
        <div className="contagion-left">
          <div className="stat-row three big">
            <Stat label="System funding contraction" value={<AnimNum value={cg.funding_contraction} fmt={Fmt.money} />} tone={cg.funding_contraction > 1e11 ? "warn" : null} />
            <Stat label="Capex slowdown" value={<AnimNum value={cg.capex_slowdown} fmt={v => Fmt.pct(v, 0)} />} tone={cg.capex_slowdown > 0.4 ? "warn" : null} />
            <Stat label="Liquidity stress" value={<AnimNum value={cg.liquidity_stress} fmt={v => Fmt.pct(v, 0)} />} tone={cg.liquidity_stress > 0.4 ? "warn" : null} />
          </div>
          <p className="micro pad">Funding contraction applies a 5× multiplier to propagated losses across bank, private-credit, BDC, securitization, corporate-bond and insurer nodes. Capex slowdown is bounded at 100%.</p>
        </div>
        <div className="contagion-right">
          <div className="block-eyebrow">Propagated loss by node <span className="muted">(total, incl. 2nd/3rd-order)</span></div>
          <HeatGrid data={heat} max={maxLoss} fmt={Fmt.money} />
        </div>
      </div>
    </Panel>
  );
}

Object.assign(window, {
  Panel, Legend, Stat, Headline, CoveragePanel, TreasuryPanel, FinancingPanel,
  RefiPanel, SensitivityPanel, BreakpointsPanel, PrivateCreditPanel, ContagionPanel,
  STACK_COLORS,
});
