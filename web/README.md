# AI Data-Center Stress Desk — web dashboard

A single-screen, interactive command center that ports the Python stress model
in this repo to the browser. Pick a preset scenario or drive the manual
override sliders, and every chart morphs smoothly as the inputs change.

**Live:** https://ai-modelling.omkmorendha.com

## What it is

This is a faithful, dependency-free JavaScript port of the Python model
(`src/model/`). The engine in `engine.js` reproduces the repo's
`example_output.txt` **to the dollar** — WACD, DSCR/ICR, EBITDA, the
refinancing wall, the contagion funding-contraction, and all breakpoints all
match the Python output exactly. (Verified: base-case WACD 8.55%, EBITDA
$125.71M, DSCR 2.75, funding contraction $15.37B; breakpoints +4123 SOFR /
+3439 spread / 53.6% power / 42.2% utilization.)

## Panels

1. **Headline coverage** — DSCR, ICR, EBITDA margin, WACD, FCF after capex,
   with live deltas vs. base case and breach flags.
2. **Project & coverage** — DSCR/ICR across all seven scenarios + an annual
   economics waterfall.
3. **Treasury & SOFR curve** — risk-free repricing, Treasury Stress Index,
   securitization openness.
4. **Mixed financing stack** — the blended capital structure, gross vs. net
   floating exposure, exposure by lender type.
5. **Refinancing wall** — maturity wall vs. scenario-adjusted principal at risk.
6. **Sensitivity sweep** — one-axis stress sweeps from the base case.
7. **Breakpoints / headroom** — room on each axis before a metric breaks.
8. **Private credit sector** — the $250B AI-DC book: losses, liquidity stress,
   investor-base loss allocation.
9. **Network contagion** — stylized exposure-matrix transmission, 3-round
   propagation, propagated loss by node.

You can also resize the representative build (MW) and hit **Auto-sweep** to ramp
stress 0→max and watch everything animate.

## Running locally

No build step. It's plain HTML/CSS + React (via CDN) with in-browser Babel.
Serve the directory over HTTP (opening `index.html` via `file://` is blocked by
CORS for the `.jsx` modules):

```bash
cd web
python3 -m http.server 8000
# then open http://localhost:8000
```

## Files

| File          | Purpose                                                            |
| ------------- | ----------------------------------------------------------------- |
| `index.html`  | Shell — loads fonts, React, Babel-standalone, and the modules.    |
| `styles.css`  | Institutional / print-style design system.                        |
| `engine.js`   | Verified JS port of the Python model (pure functions, no deps).   |
| `charts.jsx`  | Animated SVG chart primitives + the tween hook.                   |
| `controls.jsx`| Scenario presets, override sliders, capacity stepper, sweep.      |
| `panels.jsx`  | The nine dashboard panels.                                        |
| `app.jsx`     | App shell: state, the severity-sweep engine, and layout.          |

## Deployment

Deployed to Cloudflare Pages with Wrangler:

```bash
cd web
npx wrangler pages deploy . --project-name ai-modelling
```

The custom domain `ai-modelling.omkmorendha.com` is attached to the Pages
project (the `omkmorendha.com` zone is on Cloudflare).
