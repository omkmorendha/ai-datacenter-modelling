/* Animated SVG chart primitives + tween hook for the AI-DC stress dashboard.
   Institutional / print aesthetic: hairline rules, mono ticks, two-tone signal. */
/* global React */

const C = {
  ink: "#211e1a",
  graphite: "#6c665d",
  faint: "rgba(33,30,26,0.08)",
  hair: "rgba(33,30,26,0.16)",
  baseSeries: "#3f3a33",
  accent: "#c0492f",       // rust / amber-red signal
  accentSoft: "rgba(192,73,47,0.14)",
  sage: "#5e7150",         // healthy / headroom
  sageSoft: "rgba(94,113,80,0.16)",
  gold: "#9a7b1f",
  paper: "#f4f1ea",
};

const Fmt = {
  money(x) {
    if (x == null || !isFinite(x)) return "n/a";
    const a = Math.abs(x), sign = x < 0 ? "−" : "";
    if (a >= 1e9) return sign + "$" + (a / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return sign + "$" + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return sign + "$" + (a / 1e3).toFixed(1) + "K";
    return sign + "$" + a.toFixed(0);
  },
  pct(x, d = 1) { return (x * 100).toFixed(d) + "%"; },
  num(x, d = 2) { return x == null || !isFinite(x) ? "n/a" : x.toFixed(d); },
  bps(x) { return (x >= 0 ? "+" : "−") + Math.abs(Math.round(x)) + " bps"; },
};

// --- tween hook ----------------------------------------------------------
function _sig(v) {
  if (Array.isArray(v)) return v.map(n => (typeof n === "number" ? n.toFixed(4) : String(n))).join("|");
  return typeof v === "number" ? v.toFixed(4) : String(v);
}
function _lerp(a, b, k) {
  if (Array.isArray(b)) return b.map((bv, i) => {
    const av = Array.isArray(a) && a[i] != null ? a[i] : bv;
    return av + (bv - av) * k;
  });
  const av = typeof a === "number" ? a : b;
  return av + (b - av) * k;
}
function useTween(target, dur = 680) {
  const [, force] = React.useReducer(x => x + 1, 0);
  const cur = React.useRef(target);
  const from = React.useRef(target);
  const sig = _sig(target);
  const prevSig = React.useRef(sig);
  if (sig !== prevSig.current) {
    from.current = cur.current;
    prevSig.current = sig;
  }
  React.useEffect(() => {
    let raf, t0 = null;
    const tick = (ts) => {
      if (t0 == null) t0 = ts;
      const e = Math.min(1, (ts - t0) / dur);
      const k = 1 - Math.pow(1 - e, 3);
      cur.current = _lerp(from.current, target, k);
      force();
      if (e < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [sig]); // eslint-disable-line
  return cur.current;
}

// --- small building blocks ----------------------------------------------
function Tick({ x, y, children, anchor = "middle", cls = "" }) {
  return <text x={x} y={y} textAnchor={anchor} className={"c-tick " + cls}>{children}</text>;
}

// --- vertical grouped/overlay bars (DSCR-ICR by scenario, refi wall) ------
function GroupedBars({ data, series, height = 280, threshold, activeIndex, fmt = (v) => v.toFixed(2), maxOverride }) {
  // data: [{label}], series: [{key,color,name}]
  const padL = 34, padR = 12, padT = 16, padB = 54;
  const [w, setW] = React.useState(640);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const flat = [];
  data.forEach(d => series.forEach(s => flat.push(d[s.key] || 0)));
  const maxV = maxOverride || Math.max(threshold || 0, ...flat) * 1.12 || 1;
  const tw = useTween([].concat(...data.map(d => series.map(s => d[s.key] || 0))));
  const innerW = w - padL - padR, innerH = height - padT - padB;
  const groupW = innerW / data.length;
  const barW = Math.min(26, (groupW - 10) / series.length);
  const y = v => padT + innerH * (1 - v / maxV);
  const ticks = 4;
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={height} className="c-svg">
        {Array.from({ length: ticks + 1 }).map((_, i) => {
          const v = (maxV / ticks) * i;
          return <g key={i}>
            <line x1={padL} x2={w - padR} y1={y(v)} y2={y(v)} stroke={C.faint} />
            <Tick x={padL - 6} y={y(v) + 3} anchor="end">{fmt(v)}</Tick>
          </g>;
        })}
        {threshold != null && (
          <g>
            <line x1={padL} x2={w - padR} y1={y(threshold)} y2={y(threshold)} stroke={C.accent} strokeWidth="1" strokeDasharray="4 3" />
            <Tick x={w - padR} y={y(threshold) - 5} anchor="end" cls="c-accent">min {fmt(threshold)}</Tick>
          </g>
        )}
        {data.map((d, gi) => {
          const gx = padL + groupW * gi + groupW / 2;
          const totalW = barW * series.length + 3 * (series.length - 1);
          return <g key={gi}>
            {activeIndex === gi && <rect x={gx - groupW / 2 + 2} y={padT} width={groupW - 4} height={innerH} fill={C.accentSoft} />}
            {series.map((s, si) => {
              const idx = gi * series.length + si;
              const v = tw[idx] || 0;
              const bx = gx - totalW / 2 + si * (barW + 3);
              return <rect key={si} x={bx} y={y(v)} width={barW} height={Math.max(0, padT + innerH - y(v))}
                fill={s.color} opacity={activeIndex == null || activeIndex === gi ? 1 : 0.45} />;
            })}
            <Tick x={gx} y={height - padB + 16} cls="c-rot">{d.label}</Tick>
          </g>;
        })}
      </svg>
    </div>
  );
}

// --- yield curve (two lines + area) -------------------------------------
function YieldCurve({ curve, height = 220 }) {
  const padL = 36, padR = 16, padT = 14, padB = 30;
  const [w, setW] = React.useState(420);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const baseV = curve.map(c => c.base * 100);
  const shockV = useTween(curve.map(c => c.shocked * 100));
  const allV = baseV.concat(shockV);
  const maxV = Math.max(...allV) * 1.08, minV = Math.min(...allV) * 0.9;
  const innerW = w - padL - padR, innerH = height - padT - padB;
  const x = i => padL + innerW * (i / (curve.length - 1));
  const y = v => padT + innerH * (1 - (v - minV) / (maxV - minV || 1));
  const line = vs => vs.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join(" ");
  const area = shockV.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join(" ")
    + " " + baseV.slice().reverse().map((v, i) => "L" + x(curve.length - 1 - i) + " " + y(v)).join(" ") + " Z";
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={height} className="c-svg">
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => {
          const v = minV + (maxV - minV) * f;
          return <g key={i}>
            <line x1={padL} x2={w - padR} y1={y(v)} y2={y(v)} stroke={C.faint} />
            <Tick x={padL - 6} y={y(v) + 3} anchor="end">{v.toFixed(1)}</Tick>
          </g>;
        })}
        <path d={area} fill={C.accentSoft} />
        <path d={line(baseV)} fill="none" stroke={C.graphite} strokeWidth="1.5" strokeDasharray="3 3" />
        <path d={line(shockV)} fill="none" stroke={C.accent} strokeWidth="2.5" />
        {curve.map((c, i) => <g key={i}>
          <circle cx={x(i)} cy={y(baseV[i])} r="2.4" fill={C.graphite} />
          <circle cx={x(i)} cy={y(shockV[i])} r="3.2" fill={C.accent} />
          <Tick x={x(i)} y={height - 10}>{c.tenor}</Tick>
        </g>)}
      </svg>
    </div>
  );
}

// --- horizontal stacked composition bar ---------------------------------
function StackedBar({ segments, height = 30 }) {
  // segments: [{label,value,color}]
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  const tw = useTween(segments.map(s => s.value));
  const sum = tw.reduce((a, v) => a + v, 0) || 1;
  let acc = 0;
  return (
    <svg width="100%" height={height} className="c-svg" preserveAspectRatio="none" viewBox={`0 0 100 ${height}`}>
      {segments.map((s, i) => {
        const wd = (tw[i] / sum) * 100;
        const xx = acc; acc += wd;
        return <rect key={i} x={xx} y={0} width={Math.max(0, wd - 0.25)} height={height} fill={s.color} />;
      })}
    </svg>
  );
}

// --- horizontal bars (lender, investor alloc, node losses) --------------
function HBars({ data, color = C.accent, height, fmt = Fmt.money, labelW = 130, maxOverride, colorFn }) {
  const rowH = 24, gap = 7, valW = 66;
  const h = height || data.length * (rowH + gap) + 2;
  const [w, setW] = React.useState(360);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const tw = useTween(data.map(d => d.value));
  const maxV = maxOverride || Math.max(...data.map(d => d.value), 1e-9);
  const barX = labelW + 8;
  const trackW = Math.max(10, w - barX - valW);
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={h} className="c-svg">
        {data.map((d, i) => {
          const yy = i * (rowH + gap) + 1;
          const v = tw[i];
          const bw = trackW * Math.max(0, v / maxV);
          return <g key={i}>
            <text x={labelW} y={yy + rowH / 2 + 3} textAnchor="end" className="c-blabel">{d.label}</text>
            <rect x={barX} y={yy + 3} width={bw} height={rowH - 6} fill={colorFn ? colorFn(d, i) : color} rx="1" />
            <text x={w} y={yy + rowH / 2 + 3} dx="-1" textAnchor="end" className="c-bval">{fmt(d.value)}</text>
          </g>;
        })}
      </svg>
    </div>
  );
}

// --- sensitivity multi-line over categorical x --------------------------
function SensLines({ rows, axis, height = 250, threshold = 1.0, xfmt }) {
  const padL = 32, padR = 14, padT = 14, padB = 40;
  const [w, setW] = React.useState(440);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const dscr = useTween(rows.map(r => r.dscr));
  const icr = useTween(rows.map(r => r.icr));
  const allV = rows.map(r => r.dscr).concat(rows.map(r => r.icr)).concat([threshold]);
  const maxV = Math.max(...allV) * 1.1, minV = Math.min(0, ...allV);
  const innerW = w - padL - padR, innerH = height - padT - padB;
  const x = i => padL + innerW * (i / (rows.length - 1));
  const y = v => padT + innerH * (1 - (v - minV) / (maxV - minV || 1));
  const line = vs => vs.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join(" ");
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={height} className="c-svg">
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => {
          const v = minV + (maxV - minV) * f;
          return <g key={i}>
            <line x1={padL} x2={w - padR} y1={y(v)} y2={y(v)} stroke={C.faint} />
            <Tick x={padL - 6} y={y(v) + 3} anchor="end">{v.toFixed(1)}</Tick>
          </g>;
        })}
        <line x1={padL} x2={w - padR} y1={y(threshold)} y2={y(threshold)} stroke={C.accent} strokeWidth="1" strokeDasharray="4 3" />
        <path d={line(icr)} fill="none" stroke={C.graphite} strokeWidth="1.8" strokeDasharray="4 3" />
        <path d={line(dscr)} fill="none" stroke={C.ink} strokeWidth="2.4" />
        {rows.map((r, i) => <g key={i}>
          <circle cx={x(i)} cy={y(dscr[i])} r="3" fill={dscr[i] < threshold ? C.accent : C.ink} />
          <circle cx={x(i)} cy={y(icr[i])} r="2.4" fill={C.graphite} />
          <Tick x={x(i)} y={height - 22}>{xfmt ? xfmt(r.x) : r.x}</Tick>
        </g>)}
      </svg>
    </div>
  );
}

// --- breakpoint headroom bar (current vs breach point) ------------------
function HeadroomBar({ label, current, breach, max, lowerWorse, breachText, fmt }) {
  const [w, setW] = React.useState(300);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const h = 26;
  const cap = max;
  const curT = useTween(Math.max(0, Math.min(current, cap)));
  const bpos = breach == null ? null : Math.max(0, Math.min(breach, cap)) / cap * w;
  const cpos = curT / cap * w;
  // danger when current crosses into the unsafe region
  const danger = breach == null ? false : (lowerWorse ? current <= breach : current >= breach);
  const safeStart = lowerWorse && bpos != null ? bpos : 0;
  const safeW = bpos == null ? w : (lowerWorse ? w - bpos : bpos);
  return (
    <div ref={ref} className="hr-wrap">
      <div className="hr-top">
        <span className="c-blabel">{label}</span>
        <span className={"hr-val" + (danger ? " danger" : "")}>{breachText}</span>
      </div>
      <svg width={w} height={h} className="c-svg">
        <rect x="0" y={h / 2 - 4} width={w} height="8" fill={C.faint} rx="4" />
        {bpos != null && <rect x={safeStart} y={h / 2 - 4} width={Math.max(0, safeW)} height="8" fill={C.sageSoft} rx="4" />}
        {bpos != null && <line x1={bpos} x2={bpos} y1="2" y2={h - 2} stroke={C.sage} strokeWidth="2" />}
        <line x1={cpos} x2={cpos} y1="0" y2={h} stroke={danger ? C.accent : C.ink} strokeWidth="2.5" />
        <circle cx={cpos} cy={h / 2} r="4" fill={danger ? C.accent : C.ink} />
      </svg>
    </div>
  );
}

// --- contagion node heat strip (matrix of cells) ------------------------
function HeatGrid({ data, max, fmt = Fmt.money }) {
  // data: [{label,value}]; color intensity by value/max
  const tw = useTween(data.map(d => d.value));
  const mx = max || Math.max(...data.map(d => d.value), 1e-9);
  return (
    <div className="heat-grid">
      {data.map((d, i) => {
        const inten = Math.min(1, tw[i] / mx);
        const bg = `color-mix(in oklab, ${C.accent} ${(inten * 100).toFixed(0)}%, ${C.paper})`;
        return <div key={i} className="heat-cell" style={{ background: bg }}>
          <span className="heat-label" style={{ color: inten > 0.55 ? "#fbf7f0" : C.graphite }}>{d.label}</span>
          <span className="heat-val" style={{ color: inten > 0.55 ? "#fbf7f0" : C.ink }}>{fmt(d.value)}</span>
        </div>;
      })}
    </div>
  );
}

// --- animated number ----------------------------------------------------
function AnimNum({ value, fmt = (v) => v.toFixed(2) }) {
  const v = useTween(typeof value === "number" && isFinite(value) ? value : 0);
  return <React.Fragment>{isFinite(value) ? fmt(v) : "n/a"}</React.Fragment>;
}

Object.assign(window, {
  C, Fmt, useTween, GroupedBars, YieldCurve, StackedBar, HBars, SensLines,
  HeadroomBar, HeatGrid, AnimNum, Tick,
});
