/* =============================================================================
 * AI Data-Center Macro Stress Model — JavaScript engine
 *
 * A faithful port of omkmorendha/ai-datacenter-modelling (Python) to the browser.
 * Pure functions, no dependencies. Verified to reproduce the repo's
 * example_output.txt to the dollar (WACD, DSCR/ICR, EBITDA, refinancing wall,
 * contagion funding-contraction, breakpoints).
 *
 * Units: *_bps additive basis points; *_pct multiplicative; *_delta additive.
 * All rates are decimals (0.045 = 4.5%).
 * ========================================================================== */
(function (root) {
  "use strict";

  // --- helpers -------------------------------------------------------------
  const bps = (b) => b / 10000;
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const safeDiv = (a, b, d = 0) => (b === 0 || !isFinite(b) ? d : a / b);

  // --- base assumptions (from data/assumptions/base_assumptions.yaml) -------
  const A = {
    datacenter_capex_per_mw: 9_000_000,
    datacenter_project_leverage: 0.55,
    datacenter_utilization: 0.85,
    datacenter_contracted_revenue_per_mw_year: 2_400_000,
    datacenter_merchant_revenue_share: 0.15,
    datacenter_power_cost_per_mwh: 68,
    datacenter_pue: 1.25,
    datacenter_opex_ex_power_per_mw_year: 150_000,
    datacenter_maintenance_capex_pct_of_revenue: 0.05,
    datacenter_principal_amortization_rate: 0.02,
    base_credit_spread_bps: 200,
    base_refinancing_probability: 0.90,
    thresholds: {
      dscr_min: 1.20, icr_min: 1.50, ebitda_margin_min: 0.45,
      fcf_after_capex_min: 0.0, min_utilization: 0.60,
    },
    representative_financing_stack_shares: {
      hyperscaler_retained_earnings: 0.20,
      corporate_bonds: 0.15,
      bank_credit_facility: 0.12,
      syndicated_loans: 0.10,
      private_credit: 0.13,
      abs_cmbs_securitization: 0.10,
      project_finance_spv: 0.08,
      lease_obligations: 0.07,
      equity_jv: 0.05,
    },
    refinancing_maturity_distribution: {
      2026: 0.08, 2027: 0.12, 2028: 0.18, 2029: 0.22,
      2030: 0.18, 2031: 0.12, 2032: 0.10,
    },
    // private credit
    private_credit_ai_datacenter_outstanding: 250e9,
    private_credit_floating_rate_share: 0.85,
    private_credit_hedge_ratio: 0.20,
    private_credit_average_spread_bps: 550,
    private_credit_average_ltv: 0.60,
    private_credit_average_dscr: 1.45,
    private_credit_default_probability: 0.03,
    private_credit_recovery_rate: 0.55,
    private_credit_investor_base: {
      bdcs: 0.18, private_credit_funds: 0.30, insurers: 0.22, pensions: 0.15,
      banks: 0.08, sovereign_wealth: 0.05, other: 0.02,
    },
  };

  const BASE_RATES = { sofr: 0.0433, "2y": 0.043, "10y": 0.045, "30y": 0.047 };

  const HOURS_PER_YEAR = 8760;

  // --- scenarios (from data/assumptions/scenarios.yaml) --------------------
  // Each entry: label, description, and the shock groups (defaults 0).
  function S(o) {
    return Object.assign({
      rates: { sofr_bps: 0, dgs2_bps: 0, dgs10_bps: 0, dgs30_bps: 0, term_premium_bps: 0 },
      credit: { credit_spread_bps: 0, refinancing_probability_delta: 0, securitization_open: true, securitization_haircut_delta: 0, cap_rate_bps: 0 },
      power: { power_price_pct: 0 },
      ai_demand: { utilization_delta: 0, revenue_per_mw_pct: 0 },
      private_credit: { nav_haircut: 0, default_probability_mult: 1, recovery_rate_delta: 0, new_lending_capacity_pct: 0 },
      equity: { valuation_haircut: 0 },
      inflation: { inflation_expectations_bps: 0 },
      capex: { capex_cut_pct: 0 },
    }, o, {
      rates: Object.assign({ sofr_bps: 0, dgs2_bps: 0, dgs10_bps: 0, dgs30_bps: 0, term_premium_bps: 0 }, o.rates),
      credit: Object.assign({ credit_spread_bps: 0, refinancing_probability_delta: 0, securitization_open: true, securitization_haircut_delta: 0, cap_rate_bps: 0 }, o.credit),
      power: Object.assign({ power_price_pct: 0 }, o.power),
      ai_demand: Object.assign({ utilization_delta: 0, revenue_per_mw_pct: 0 }, o.ai_demand),
      private_credit: Object.assign({ nav_haircut: 0, default_probability_mult: 1, recovery_rate_delta: 0, new_lending_capacity_pct: 0 }, o.private_credit),
      equity: Object.assign({ valuation_haircut: 0 }, o.equity),
      inflation: Object.assign({ inflation_expectations_bps: 0 }, o.inflation),
      capex: Object.assign({ capex_cut_pct: 0 }, o.capex),
    });
  }

  const SCENARIOS = {
    base_case: S({
      name: "base_case", label: "Base case",
      description: "SOFR/10Y/30Y unchanged, modest spreads, stable power, high AI utilization, securitization open.",
    }),
    high_for_longer: S({
      name: "high_for_longer", label: "High for longer",
      description: "SOFR +100, 10Y/30Y +75, spreads +75, power +10%, utilization unchanged.",
      rates: { sofr_bps: 100, dgs2_bps: 90, dgs10_bps: 75, dgs30_bps: 75, term_premium_bps: 25 },
      credit: { credit_spread_bps: 75, refinancing_probability_delta: -0.05, securitization_open: true, securitization_haircut_delta: 0.02, cap_rate_bps: 50 },
      power: { power_price_pct: 0.10 },
      private_credit: { nav_haircut: 0.02, default_probability_mult: 1.2, recovery_rate_delta: 0.0, new_lending_capacity_pct: -0.10 },
      equity: { valuation_haircut: 0.05 },
    }),
    treasury_term_premium_shock: S({
      name: "treasury_term_premium_shock", label: "Treasury term-premium shock",
      description: "SOFR +50, 10Y +150, 30Y +200, spreads +150, securitization haircut up, cap rates rise, refi worsens.",
      rates: { sofr_bps: 50, dgs2_bps: 75, dgs10_bps: 150, dgs30_bps: 200, term_premium_bps: 125 },
      credit: { credit_spread_bps: 150, refinancing_probability_delta: -0.20, securitization_open: true, securitization_haircut_delta: 0.08, cap_rate_bps: 150 },
      private_credit: { nav_haircut: 0.05, default_probability_mult: 1.4, recovery_rate_delta: -0.05, new_lending_capacity_pct: -0.20 },
      equity: { valuation_haircut: 0.10 },
      inflation: { inflation_expectations_bps: 25 },
    }),
    iran_oil_inflation_shock: S({
      name: "iran_oil_inflation_shock", label: "Iran / oil inflation shock",
      description: "Geopolitical oil spike: SOFR +100, 10Y +100, 30Y +125, power +25%, infl exp +50, spreads +100.",
      rates: { sofr_bps: 100, dgs2_bps: 100, dgs10_bps: 100, dgs30_bps: 125, term_premium_bps: 50 },
      credit: { credit_spread_bps: 100, refinancing_probability_delta: -0.10, securitization_open: true, securitization_haircut_delta: 0.05, cap_rate_bps: 75 },
      power: { power_price_pct: 0.25 },
      private_credit: { nav_haircut: 0.04, default_probability_mult: 1.3, recovery_rate_delta: -0.03, new_lending_capacity_pct: -0.15 },
      equity: { valuation_haircut: 0.08 },
      inflation: { inflation_expectations_bps: 50 },
    }),
    ai_revenue_disappointment: S({
      name: "ai_revenue_disappointment", label: "AI revenue disappointment",
      description: "Utilization -20%, revenue/MW -15%, spreads +100, equity -25%, capex cuts begin.",
      rates: { dgs10_bps: -25, dgs30_bps: -25 },
      credit: { credit_spread_bps: 100, refinancing_probability_delta: -0.15, securitization_open: true, securitization_haircut_delta: 0.05, cap_rate_bps: 100 },
      ai_demand: { utilization_delta: -0.20, revenue_per_mw_pct: -0.15 },
      private_credit: { nav_haircut: 0.08, default_probability_mult: 1.8, recovery_rate_delta: -0.05, new_lending_capacity_pct: -0.20 },
      equity: { valuation_haircut: 0.25 },
      capex: { capex_cut_pct: -0.20 },
    }),
    severe_private_credit_liquidity_crunch: S({
      name: "severe_private_credit_liquidity_crunch", label: "Severe private-credit liquidity crunch",
      description: "SOFR +150, spreads +250, PC NAV -15%, PD doubles, recovery falls, new lending -40%, securitization partly closed.",
      rates: { sofr_bps: 150, dgs2_bps: 100, dgs10_bps: 50, dgs30_bps: 50, term_premium_bps: 50 },
      credit: { credit_spread_bps: 250, refinancing_probability_delta: -0.35, securitization_open: false, securitization_haircut_delta: 0.15, cap_rate_bps: 150 },
      private_credit: { nav_haircut: 0.15, default_probability_mult: 2.0, recovery_rate_delta: -0.15, new_lending_capacity_pct: -0.40 },
      equity: { valuation_haircut: 0.15 },
    }),
    combined_severe_case: S({
      name: "combined_severe_case", label: "Combined severe case",
      description: "Treasury shock + AI disappointment + power shock + private-credit liquidity crunch, stacked.",
      rates: { sofr_bps: 150, dgs2_bps: 125, dgs10_bps: 150, dgs30_bps: 200, term_premium_bps: 125 },
      credit: { credit_spread_bps: 300, refinancing_probability_delta: -0.45, securitization_open: false, securitization_haircut_delta: 0.20, cap_rate_bps: 200 },
      power: { power_price_pct: 0.25 },
      ai_demand: { utilization_delta: -0.20, revenue_per_mw_pct: -0.15 },
      private_credit: { nav_haircut: 0.20, default_probability_mult: 2.5, recovery_rate_delta: -0.15, new_lending_capacity_pct: -0.50 },
      equity: { valuation_haircut: 0.35 },
      inflation: { inflation_expectations_bps: 50 },
      capex: { capex_cut_pct: -0.30 },
    }),
  };

  const SCENARIO_ORDER = [
    "base_case", "high_for_longer", "treasury_term_premium_shock",
    "iran_oil_inflation_shock", "ai_revenue_disappointment",
    "severe_private_credit_liquidity_crunch", "combined_severe_case",
  ];

  function refBps(rates, ref) {
    const m = {
      sofr: rates.sofr_bps, "2y": rates.dgs2_bps, dgs2: rates.dgs2_bps,
      "10y": rates.dgs10_bps, dgs10: rates.dgs10_bps,
      "30y": rates.dgs30_bps, dgs30: rates.dgs30_bps,
      fixed: 0, fixed_coupon: 0, custom: 0,
    };
    return m[ref] != null ? m[ref] : 0;
  }

  // --- financing stack -----------------------------------------------------
  // profile: [rateType, ref, hedge, swap, lender, mtmSens, spreadAddBps, amort]
  const PROFILES = {
    hyperscaler_retained_earnings: ["fixed", "fixed", 0, 0, "hyperscaler", 0.0, -50, "interest_only"],
    corporate_bonds: ["fixed", "fixed", 0, 0, "bond_market", 0.07, 0, "bullet"],
    bank_credit_facility: ["floating", "sofr", 0.10, 0.10, "bank", 0.02, 25, "bullet"],
    syndicated_loans: ["floating", "sofr", 0.15, 0.20, "bank", 0.03, 75, "amortizing"],
    private_credit: ["floating", "sofr", 0.15, 0.05, "private_credit", 0.04, 350, "bullet"],
    abs_cmbs_securitization: ["fixed", "fixed", 0, 0, "securitization", 0.06, 150, "amortizing"],
    project_finance_spv: ["floating", "sofr", 0.40, 0.30, "bank", 0.05, 225, "amortizing"],
    lease_obligations: ["fixed", "fixed", 0, 0, "other", 0.03, 100, "amortizing"],
    equity_jv: ["fixed", "fixed", 0, 0, "other", 0.0, 0, "interest_only"],
  };

  const LENDER_LABEL = {
    bank: "Bank", private_credit: "Private credit", bond_market: "Bond market",
    securitization: "Securitization", insurer: "Insurer", pension: "Pension",
    hyperscaler: "Hyperscaler", other: "Other",
  };
  const SOURCE_LABEL = {
    hyperscaler_retained_earnings: "Hyperscaler retained earnings",
    corporate_bonds: "Corporate bonds",
    bank_credit_facility: "Bank credit facility",
    syndicated_loans: "Syndicated loans",
    private_credit: "Private credit",
    abs_cmbs_securitization: "ABS / CMBS securitization",
    project_finance_spv: "Project finance / SPV",
    lease_obligations: "Lease obligations",
    equity_jv: "Equity / JV",
  };

  function buildStack(totalDebt, baseSpreadBps) {
    const shares = A.representative_financing_stack_shares;
    const wallYears = Object.keys(A.refinancing_maturity_distribution)
      .map(Number).sort((a, b) => a - b);
    const sources = [];
    let i = 0;
    for (const name in shares) {
      const p = PROFILES[name];
      if (!p) { i++; continue; }
      const [rateType, ref, hedge, swap, lender, mtm, spreadAdd, amort] = p;
      const amount = totalDebt * shares[name];
      let baseRate, fixedCoupon;
      if (ref === "fixed") {
        baseRate = BASE_RATES["10y"];
        fixedCoupon = baseRate + bps(baseSpreadBps + spreadAdd);
      } else {
        baseRate = BASE_RATES[ref] != null ? BASE_RATES[ref] : BASE_RATES.sofr;
        fixedCoupon = 0;
      }
      const maturity = wallYears[i % wallYears.length];
      sources.push({
        name, label: SOURCE_LABEL[name] || name, amount,
        rateType, ref, baseRate, fixedCoupon,
        creditSpreadBps: baseSpreadBps + spreadAdd,
        maturityYear: maturity, amort,
        hedge, swap, lender, mtmSens: mtm,
      });
      i++;
    }
    return sources;
  }

  function effFloatingFrac(s) {
    if (s.rateType === "fixed") return 0;
    return 1 - Math.min(1, s.hedge + s.swap);
  }

  function allInRate(s, sc) {
    const spread = bps(s.creditSpreadBps);
    if (s.rateType === "fixed" || s.ref === "fixed") {
      const base = s.fixedCoupon ? s.fixedCoupon : s.baseRate;
      return base + spread; // fixed coupons don't reprice live
    }
    let rateShock = 0, spreadShock = 0;
    if (sc) {
      rateShock = bps(refBps(sc.rates, s.ref));
      spreadShock = bps(sc.credit.credit_spread_bps);
    }
    const frac = effFloatingFrac(s);
    return s.baseRate + frac * rateShock + spread + spreadShock;
  }

  function mtmLoss(s, sc) {
    if (!sc || s.mtmSens === 0) return 0;
    const rateMoveBps = Math.abs(refBps(sc.rates, s.ref));
    const spreadMoveBps = Math.abs(sc.credit.credit_spread_bps);
    const units = (rateMoveBps + spreadMoveBps) / 100;
    return s.amount * (s.mtmSens * units);
  }

  function stackSummary(sources, sc) {
    const total = sources.reduce((a, s) => a + s.amount, 0);
    const wacd = total === 0 ? 0 : sources.reduce((a, s) => a + s.amount * allInRate(s, sc), 0) / total;
    const interest = sources.reduce((a, s) => a + s.amount * allInRate(s, sc), 0);
    const floatNet = sources.reduce((a, s) => a + s.amount * effFloatingFrac(s), 0);
    const floatGross = sources.filter(s => s.rateType === "floating").reduce((a, s) => a + s.amount, 0);
    const lender = {};
    sources.forEach(s => { lender[s.lender] = (lender[s.lender] || 0) + s.amount; });
    const wall = {};
    sources.forEach(s => { wall[s.maturityYear] = (wall[s.maturityYear] || 0) + s.amount; });
    const mtm = sc ? sources.reduce((a, s) => a + mtmLoss(s, sc), 0) : 0;
    return {
      total_debt: total, wacd, annual_interest_expense: interest,
      floating_exposure_net: floatNet,
      floating_share_net: safeDiv(floatNet, total),
      floating_share_gross: safeDiv(floatGross, total),
      exposure_by_lender_type: lender, refinancing_wall: wall, mtm_loss: mtm,
    };
  }

  // --- data-center project -------------------------------------------------
  function buildProject(capacityMw) {
    const capexPerMw = A.datacenter_capex_per_mw;
    const leverage = A.datacenter_project_leverage;
    const totalDebt = capacityMw * capexPerMw * leverage;
    return {
      capacityMw,
      capexPerMw,
      utilization: A.datacenter_utilization,
      revPerMw: A.datacenter_contracted_revenue_per_mw_year,
      merchantShare: A.datacenter_merchant_revenue_share,
      merchantPremium: 0,
      powerCostPerMwh: A.datacenter_power_cost_per_mwh,
      pue: A.datacenter_pue,
      opexExPowerPerMw: A.datacenter_opex_ex_power_per_mw_year,
      maintCapexPct: A.datacenter_maintenance_capex_pct_of_revenue,
      principalAmortRate: A.datacenter_principal_amortization_rate,
      leverage,
      sources: buildStack(totalDebt, A.base_credit_spread_bps),
      totalDebt,
    };
  }

  function effUtil(p, sc) { return clamp(p.utilization + (sc ? sc.ai_demand.utilization_delta : 0), 0, 1); }
  function effRevPerMw(p, sc) { return p.revPerMw * (1 + (sc ? sc.ai_demand.revenue_per_mw_pct : 0)); }
  function effPowerPrice(p, sc) { return p.powerCostPerMwh * (1 + (sc ? sc.power.power_price_pct : 0)); }

  function revenue(p, sc) {
    const u = effUtil(p, sc), rpm = effRevPerMw(p, sc);
    const base = p.capacityMw * rpm * u;
    const merchant = base * p.merchantShare * (1 + p.merchantPremium);
    const contracted = base * (1 - p.merchantShare);
    return contracted + merchant;
  }
  function powerExpense(p, sc) {
    const u = effUtil(p, sc), price = effPowerPrice(p, sc);
    return p.capacityMw * p.pue * HOURS_PER_YEAR * u * price;
  }
  function opexExPower(p) { return p.capacityMw * p.opexExPowerPerMw; }
  function ebitda(p, sc) { return revenue(p, sc) - powerExpense(p, sc) - opexExPower(p); }
  function ebitdaMargin(p, sc) { return safeDiv(ebitda(p, sc), revenue(p, sc), 0); }
  function interestExpense(p, sc) { return p.sources.reduce((a, s) => a + s.amount * allInRate(s, sc), 0); }
  function principalDue(p) {
    const amortDebt = p.sources.filter(s => s.amort === "amortizing").reduce((a, s) => a + s.amount, 0);
    return amortDebt * p.principalAmortRate;
  }
  function debtService(p, sc) { return interestExpense(p, sc) + principalDue(p); }
  function dscr(p, sc) { return safeDiv(ebitda(p, sc), debtService(p, sc), Infinity); }
  function icr(p, sc) { return safeDiv(ebitda(p, sc), interestExpense(p, sc), Infinity); }
  function maintCapex(p, sc) { return revenue(p, sc) * p.maintCapexPct; }
  function fcfAfterCapex(p, sc) { return ebitda(p, sc) - interestExpense(p, sc) - maintCapex(p, sc); }

  function projectMetrics(p, sc) {
    const t = A.thresholds;
    const d = dscr(p, sc), ic = icr(p, sc), m = ebitdaMargin(p, sc), fcf = fcfAfterCapex(p, sc);
    return {
      revenue: revenue(p, sc), power_expense: powerExpense(p, sc), opex_ex_power: opexExPower(p),
      ebitda: ebitda(p, sc), ebitda_margin: m,
      interest_expense: interestExpense(p, sc), debt_service: debtService(p, sc),
      dscr: d, icr: ic, fcf_after_capex: fcf,
      total_capex: p.capacityMw * p.capexPerMw, total_debt: p.totalDebt,
      flags: {
        dscr_danger: d < t.dscr_min, icr_danger: ic < t.icr_min,
        margin_danger: m < t.ebitda_margin_min, fcf_danger: fcf < t.fcf_after_capex_min,
      },
    };
  }

  // --- treasury ------------------------------------------------------------
  function treasurySummary(sc) {
    const tsi = (sc.rates.sofr_bps + sc.rates.dgs10_bps + sc.rates.dgs30_bps + sc.rates.term_premium_bps) / 100;
    let openness = sc.credit.securitization_open ? 0.9 : 0.2;
    openness -= sc.credit.securitization_haircut_delta;
    openness -= Math.abs(Math.min(0, sc.credit.refinancing_probability_delta));
    openness = clamp(openness, 0, 1);
    return {
      treasury_stress_index: tsi,
      securitization_openness_score: openness,
      refinancing_cost_shock: bps(sc.rates.dgs10_bps + sc.credit.credit_spread_bps),
      discount_rate_shock: bps(sc.rates.dgs10_bps + sc.rates.term_premium_bps),
      cap_rate_shock: bps(sc.credit.cap_rate_bps),
    };
  }

  function yieldCurve(sc) {
    const defs = [["SOFR", "sofr", "sofr_bps"], ["2Y", "2y", "dgs2_bps"], ["10Y", "10y", "dgs10_bps"], ["30Y", "30y", "dgs30_bps"]];
    return defs.map(([label, key, attr]) => {
      const lvl = BASE_RATES[key];
      return { tenor: label, base: lvl, shocked: lvl + bps(sc.rates[attr]) };
    });
  }

  // --- private credit ------------------------------------------------------
  function pcNetFloating() {
    const gross = A.private_credit_ai_datacenter_outstanding * A.private_credit_floating_rate_share;
    return gross * (1 - A.private_credit_hedge_ratio);
  }
  function pcStressedPD(sc) { return clamp(A.private_credit_default_probability * sc.private_credit.default_probability_mult, 0, 1); }
  function pcStressedRecovery(sc) { return clamp(A.private_credit_recovery_rate + sc.private_credit.recovery_rate_delta, 0, 1); }
  function pcExpectedLoss(sc) { return A.private_credit_ai_datacenter_outstanding * pcStressedPD(sc) * (1 - pcStressedRecovery(sc)); }
  function pcMtmLoss(sc) { return A.private_credit_ai_datacenter_outstanding * Math.max(0, sc.private_credit.nav_haircut); }
  function pcLiquidityStress(sc) {
    const w = 0.25;
    const nav = clamp(sc.private_credit.nav_haircut, 0, 1);
    const def = clamp((sc.private_credit.default_probability_mult - 1) / 2, 0, 1);
    const lend = clamp(Math.abs(sc.private_credit.new_lending_capacity_pct), 0, 1);
    const spread = clamp(bps(sc.credit.credit_spread_bps) / 0.05, 0, 1);
    return clamp(w * (nav + def + lend + spread), 0, 1);
  }
  function pcInvestorAllocation(sc) {
    const totalLoss = Math.max(pcExpectedLoss(sc), pcMtmLoss(sc));
    const out = {};
    for (const k in A.private_credit_investor_base) out[k] = A.private_credit_investor_base[k] * totalLoss;
    return out;
  }
  function pcRedemptionGating(sc) {
    const openEnd = (A.private_credit_investor_base.bdcs || 0) + (A.private_credit_investor_base.private_credit_funds || 0);
    return clamp(pcLiquidityStress(sc) * openEnd, 0, 1);
  }
  function privateCreditSummary(sc) {
    return {
      outstanding_usd: A.private_credit_ai_datacenter_outstanding,
      net_floating_exposure_usd: pcNetFloating(),
      stressed_pd: pcStressedPD(sc), stressed_recovery: pcStressedRecovery(sc),
      expected_loss_realized_usd: pcExpectedLoss(sc),
      mark_to_market_loss_usd: pcMtmLoss(sc),
      liquidity_stress_score: pcLiquidityStress(sc),
      redemption_gating_stress_proxy: pcRedemptionGating(sc),
      lending_capacity_usd_delta: A.private_credit_ai_datacenter_outstanding * sc.private_credit.new_lending_capacity_pct,
      lending_capacity_pct: sc.private_credit.new_lending_capacity_pct,
      investor_loss_allocation_usd: pcInvestorAllocation(sc),
    };
  }

  // --- contagion -----------------------------------------------------------
  const NODES = [
    "hyperscalers", "datacenter_operators", "private_credit_funds", "bdcs",
    "insurers", "pensions", "banks", "corporate_bond_funds",
    "securitization_investors", "utilities", "chip_suppliers", "pe_funds_lps",
  ];
  const NODE_LABEL = {
    hyperscalers: "Hyperscalers", datacenter_operators: "DC operators",
    private_credit_funds: "Private credit funds", bdcs: "BDCs", insurers: "Insurers",
    pensions: "Pensions", banks: "Banks", corporate_bond_funds: "Corp bond funds",
    securitization_investors: "Securitization", utilities: "Utilities",
    chip_suppliers: "Chip suppliers", pe_funds_lps: "PE fund LPs",
  };
  const EXPOSURES = {
    private_credit_funds: { bdcs: 0.4, insurers: 0.3, pensions: 0.25, banks: 0.15 },
    bdcs: { private_credit_funds: 0.5 },
    datacenter_operators: { private_credit_funds: 0.3, banks: 0.2, securitization_investors: 0.2, corporate_bond_funds: 0.15, utilities: 0.15, hyperscalers: 0.25 },
    banks: { datacenter_operators: 0.25, hyperscalers: 0.1, private_credit_funds: 0.15 },
    insurers: { private_credit_funds: 0.2, securitization_investors: 0.25 },
    pensions: { private_credit_funds: 0.2, pe_funds_lps: 0.2 },
    securitization_investors: { datacenter_operators: 0.3 },
    corporate_bond_funds: { hyperscalers: 0.2, datacenter_operators: 0.2 },
    utilities: { datacenter_operators: 0.15 },
    chip_suppliers: { hyperscalers: 0.4, datacenter_operators: 0.2 },
    pe_funds_lps: { datacenter_operators: 0.3, private_credit_funds: 0.2 },
    hyperscalers: { datacenter_operators: 0.1 },
  };
  const FUNDING_NODES = ["banks", "private_credit_funds", "bdcs", "securitization_investors", "corporate_bond_funds", "insurers"];

  function shockVec(shock) { return NODES.map(n => shock[n] || 0); }
  function matVec(x) {
    return NODES.map((to) => {
      const fw = EXPOSURES[to] || {};
      let s = 0;
      NODES.forEach((from, j) => { if (fw[from]) s += fw[from] * x[j]; });
      return s;
    });
  }
  function propagate(shock, rounds = 3, decay = 0.6) {
    let x = shockVec(shock);
    const total = x.slice();
    for (let r = 0; r < rounds; r++) {
      x = matVec(x).map(v => decay * v);
      for (let i = 0; i < total.length; i++) total[i] += x[i];
    }
    const out = {};
    NODES.forEach((n, i) => out[n] = total[i]);
    return out;
  }
  function firstOrder(shock) {
    const y = matVec(shockVec(shock));
    const out = {};
    NODES.forEach((n, i) => out[n] = y[i]);
    return out;
  }
  function buildShock(sc, p) {
    const scale = A.private_credit_ai_datacenter_outstanding;
    const shock = {};
    // private credit book
    shock.private_credit_funds = pcMtmLoss(sc);
    const el = pcExpectedLoss(sc);
    shock.bdcs = (shock.bdcs || 0) + el * 0.3;
    shock.insurers = (shock.insurers || 0) + el * 0.2;
    shock.pensions = (shock.pensions || 0) + el * 0.15;
    shock.securitization_investors = (shock.securitization_investors || 0) + el * 0.15;
    // datacenter operators: EBITDA compression proxy (project.mark_to_market_loss absent)
    const eBase = ebitda(p, SCENARIOS.base_case);
    const eStr = ebitda(p, sc);
    shock.datacenter_operators = Math.max(0, eBase - eStr);
    // fallback fills (hyperscaler.mark_to_market_loss absent -> always fallback)
    const navH = Math.abs(sc.private_credit.nav_haircut);
    const creditBps = Math.abs(sc.credit.credit_spread_bps);
    const rateBps = Math.abs(sc.rates.sofr_bps);
    const eqH = Math.abs(sc.equity.valuation_haircut);
    const demand = Math.abs(sc.ai_demand.utilization_delta);
    const securNotional = scale * 0.3, mtmPerUnit = (creditBps / 10000) * 5.0;
    if (shock.banks == null) shock.banks = scale * 0.25 * (rateBps + creditBps) / 10000 * 3.0;
    if (shock.corporate_bond_funds == null) shock.corporate_bond_funds = scale * 0.15 * (rateBps + creditBps) / 10000 * 5.0;
    if (shock.pe_funds_lps == null) shock.pe_funds_lps = scale * 0.10 * eqH;
    if (shock.hyperscalers == null) shock.hyperscalers = 200e9 * demand;
    if (shock.utilities == null) shock.utilities = scale * 0.05 * demand;
    if (shock.chip_suppliers == null) shock.chip_suppliers = 150e9 * Math.abs(sc.capex.capex_cut_pct);
    return shock;
  }
  function fundingContraction(shock, mult = 5.0) {
    const total = propagate(shock);
    const fl = FUNDING_NODES.reduce((a, n) => a + (total[n] || 0), 0);
    return fl * mult;
  }
  function capexSlowdown(shock) {
    const scale = A.private_credit_ai_datacenter_outstanding;
    const fc = fundingContraction(shock);
    const total = propagate(shock);
    const direct = (total.hyperscalers || 0) + (total.datacenter_operators || 0);
    return clamp(safeDiv(fc, scale) + safeDiv(direct, scale), 0, 1);
  }
  function liquidityStressScore(shock) {
    const scale = A.private_credit_ai_datacenter_outstanding;
    const priv = ["private_credit_funds", "bdcs", "pe_funds_lps", "pensions", "insurers"];
    const total = propagate(shock);
    const agg = priv.reduce((a, n) => a + (total[n] || 0), 0);
    return clamp(safeDiv(agg, scale), 0, 1);
  }
  function contagionSummary(sc, p) {
    const shock = buildShock(sc, p);
    const fo = firstOrder(shock);
    const total = propagate(shock);
    const grand = Object.values(total).reduce((a, b) => a + b, 0);
    const heat = NODES.map(n => ({
      node: n, label: NODE_LABEL[n],
      first_order: fo[n] || 0, total_loss: total[n] || 0,
      share: safeDiv(total[n] || 0, grand),
    }));
    return {
      first_order: fo, propagated: total,
      funding_contraction: fundingContraction(shock),
      capex_slowdown: capexSlowdown(shock),
      liquidity_stress: liquidityStressScore(shock),
      heatmap: heat,
    };
  }

  // --- run scenario --------------------------------------------------------
  function runScenario(sc, p) {
    p = p || buildProject(100);
    return {
      scenario: sc.name, label: sc.label, description: sc.description,
      project: projectMetrics(p, sc),
      financing_stack: stackSummary(p.sources, sc),
      treasury: treasurySummary(sc),
      private_credit: privateCreditSummary(sc),
      contagion: contagionSummary(sc, p),
      yield_curve: yieldCurve(sc),
    };
  }

  // --- sensitivity ---------------------------------------------------------
  const SENS_VALUES = {
    sofr_bps: [0, 50, 100, 150, 200, 300],
    dgs10_bps: [0, 50, 100, 150, 200, 300],
    credit_spread_bps: [0, 100, 200, 300, 400, 500],
    power_price_pct: [0.0, 0.1, 0.25, 0.5, 0.75, 1.0],
    utilization_delta: [0.0, -0.05, -0.1, -0.2, -0.3, -0.4],
    revenue_per_mw_pct: [0.0, -0.05, -0.1, -0.2, -0.3, -0.4],
  };
  function applyAxis(base, axis, v) {
    const sc = JSON.parse(JSON.stringify(base));
    if (["sofr_bps", "dgs2_bps", "dgs10_bps", "dgs30_bps", "term_premium_bps"].includes(axis)) sc.rates[axis] += v;
    else if (axis === "credit_spread_bps") sc.credit.credit_spread_bps += v;
    else if (axis === "power_price_pct") sc.power.power_price_pct += v;
    else if (axis === "utilization_delta") sc.ai_demand.utilization_delta += v;
    else if (axis === "revenue_per_mw_pct") sc.ai_demand.revenue_per_mw_pct += v;
    return sc;
  }
  function sensitivityTable(p, axis, base) {
    base = base || SCENARIOS.base_case;
    const vals = SENS_VALUES[axis] || [0, 50, 100, 150, 200];
    return vals.map(v => {
      const sc = applyAxis(base, axis, v);
      const m = projectMetrics(p, sc);
      return { x: v, dscr: m.dscr, icr: m.icr, ebitda_margin: m.ebitda_margin, fcf_after_capex: m.fcf_after_capex, wacd: stackSummary(p.sources, sc).wacd };
    });
  }

  // --- breakpoints (bisection) --------------------------------------------
  function bisect(isSafe, lo, hi, tol, increasingWorse = true) {
    if (increasingWorse) {
      if (!isSafe(lo)) return lo;
      if (isSafe(hi)) return null;
    } else {
      if (!isSafe(hi)) return hi;
      if (isSafe(lo)) return null;
    }
    for (let k = 0; k < 60; k++) {
      const mid = 0.5 * (lo + hi);
      const safe = isSafe(mid);
      if (increasingWorse) { if (safe) lo = mid; else hi = mid; }
      else { if (safe) hi = mid; else lo = mid; }
      if (Math.abs(hi - lo) < tol) break;
    }
    return 0.5 * (lo + hi);
  }
  function computeBreakpoints(p, base) {
    base = base || SCENARIOS.base_case;
    const t = A.thresholds;
    const withAxis = (axis, v) => applyAxis(base, axis, v);
    const maxSofr = bisect(b => dscr(p, withAxis("sofr_bps", b)) >= t.dscr_min, 0, 12000, 1, true);
    const max10y = bisect(b => icr(p, withAxis("dgs10_bps", b)) >= t.icr_min, 0, 12000, 1, true);
    const maxPower = bisect(pc => ebitdaMargin(p, withAxis("power_price_pct", pc)) >= t.ebitda_margin_min, 0, 3, 0.001, true);
    const minUtilDelta = bisect(d => dscr(p, withAxis("utilization_delta", d)) >= t.dscr_min, -0.9, 0, 0.001, false);
    const minUtil = minUtilDelta == null ? null : Math.max(0, p.utilization + minUtilDelta);
    const maxSpread = bisect(b => fcfAfterCapex(p, withAxis("credit_spread_bps", b)) >= 0, 0, 12000, 1, true);
    return {
      max_sofr_shock_bps_before_dscr_breach: maxSofr,
      max_10y_shock_bps_before_refi_uneconomic: max10y,
      max_power_price_pct_before_margin_breach: maxPower,
      min_utilization_before_covenant_breach: minUtil,
      max_credit_spread_bps_before_fcf_negative: maxSpread,
      thresholds_used: { dscr_min: t.dscr_min, icr_min: t.icr_min, ebitda_margin_min: t.ebitda_margin_min },
      base_levels: { utilization: p.utilization },
    };
  }

  function refinancingGap(p, sc) {
    sc = sc || SCENARIOS.base_case;
    const baseRefi = A.base_refinancing_probability;
    const refiProb = clamp(baseRefi + sc.credit.refinancing_probability_delta, 0, 1);
    const wall = stackSummary(p.sources, sc).refinancing_wall;
    const out = {};
    Object.keys(wall).map(Number).sort((a, b) => a - b).forEach(y => {
      out[y] = { maturing_principal: wall[y], refi_probability: refiProb, principal_at_risk: wall[y] * (1 - refiProb) };
    });
    return out;
  }

  // --- exports -------------------------------------------------------------
  root.Model = {
    A, BASE_RATES, SCENARIOS, SCENARIO_ORDER, LENDER_LABEL, SOURCE_LABEL, NODE_LABEL, NODES,
    makeScenario: S,
    buildProject, runScenario, projectMetrics, stackSummary,
    treasurySummary, yieldCurve, privateCreditSummary, contagionSummary,
    sensitivityTable, SENS_VALUES, computeBreakpoints, refinancingGap,
    ebitda, revenue, dscr, icr, fcfAfterCapex, ebitdaMargin,
    allInRate, effFloatingFrac,
  };
})(window);
