// Institutional-activity / options-flow layer: markers on the existing chart, filters, explanation panel, validation.
// All figures come from the server; provenance tags (OBSERVED / CALCULATED / ESTIMATED / UNAVAILABLE) are shown as sent.
(function () {
  const $ = (id) => document.getElementById(id);
  const KEY = "nifty-live-engine.flow.v1";
  const UP = "#26a69a", DOWN = "#ef5350", BLUE = "#5b9cf5", ORANGE = "#f0a030";
  const TYPES = {
    AGG_BUY: { c: UP, shape: "arrowUp", text: "AGG", name: "Aggressive buying (estimated)", group: "Aggressor" },
    AGG_SELL: { c: DOWN, shape: "arrowDown", text: "AGG", name: "Aggressive selling (estimated)", group: "Aggressor" },
    CALL_BUY: { c: UP, shape: "square", text: "CALL", name: "CALL buying", group: "Options" },
    CALL_SELL: { c: DOWN, shape: "square", text: "CALL", name: "CALL selling", group: "Options" },
    PUT_BUY: { c: UP, shape: "square", text: "PUT", name: "PUT buying", group: "Options" },
    PUT_SELL: { c: DOWN, shape: "square", text: "PUT", name: "PUT selling", group: "Options" },
    LONG_BUILDUP: { c: UP, shape: "square", text: "LONG", name: "Long buildup", group: "Futures positioning" },
    SHORT_BUILDUP: { c: DOWN, shape: "square", text: "SHORT", name: "Short buildup", group: "Futures positioning" },
    SHORT_COVERING: { c: BLUE, shape: "square", text: "SC", name: "Short covering", group: "Futures positioning" },
    LONG_UNWINDING: { c: ORANGE, shape: "square", text: "LU", name: "Long unwinding", group: "Futures positioning" },
    BUY_PRESSURE: { c: UP, shape: "circle", text: "BUY", name: "Buying pressure", group: "Pressure score" },
    SELL_PRESSURE: { c: DOWN, shape: "circle", text: "SELL", name: "Selling pressure", group: "Pressure score" },
    BUYER_ABSORPTION: { c: UP, shape: "circle", text: "ABS", name: "Possible buyer absorption", group: "Absorption" },
    SELLER_ABSORPTION: { c: DOWN, shape: "circle", text: "ABS", name: "Possible seller absorption", group: "Absorption" },
    BULL_TRAP: { c: ORANGE, shape: "square", text: "BT", name: "Possible bull trap", group: "Traps" },
    BEAR_TRAP: { c: ORANGE, shape: "square", text: "BR", name: "Possible bear trap", group: "Traps" },
  };
  const DEFAULTS = { off: [], minStrength: 50, tf: "all", strike: "all", expiry: "all", minQuality: "Poor" };
  const QRANK = { Good: 2, Fair: 1, Poor: 0 };

  let cfg, chart, getTf, settings = load(), signals = new Map(), byTime = new Map(), flowState = {}, curTf = "1m";
  let selected = null, raf = 0;

  function load() { try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) { return Object.assign({}, DEFAULTS); } }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(settings)); } catch (e) { /* ignore */ } }
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmt = (v, d = 2) => v == null ? "n/a" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const tfInfo = (key) => cfg.timeframes.find((t) => t.key === key);
  const bucket = (ts, tf) => Math.floor((ts - tf.anchor) / tf.seconds) * tf.seconds + tf.anchor;
  const clock = (ts) => chart.fmtDateTime(ts);

  // ---- signal -> chart candle mapping ------------------------------------------------------------
  function chartTime(sig) {
    const ct = tfInfo(curTf); if (!ct) return null;
    const t = ct.seconds >= sig.tf_seconds ? bucket(sig.time, ct) : bucket(sig.time + sig.tf_seconds - 1, ct);
    return t;
  }
  function visible(sig) {
    if (settings.off.includes(sig.type)) return false;
    if (sig.strength < settings.minStrength) return false;
    if (settings.tf !== "all" && sig.tf !== settings.tf) return false;
    if (QRANK[sig.data_quality] < QRANK[settings.minQuality]) return false;
    const opt = sig.instrument === "CE" || sig.instrument === "PE";
    if (opt && settings.strike !== "all" && String(sig.strike) !== settings.strike) return false;
    if (opt && settings.expiry !== "all" && sig.expiry !== settings.expiry) return false;
    return true;
  }
  function rebuild() {
    byTime = new Map();
    const have = new Set(chart.times);
    signals.forEach((s) => {
      if (!visible(s)) return;
      const t = chartTime(s);
      if (t == null || !have.has(t)) return;
      if (!byTime.has(t)) byTime.set(t, []);
      byTime.get(t).push(s);
    });
    const markers = [];
    const labels = barSpacing() >= 16;          // text labels only when zoomed in enough to stay readable
    [...byTime.keys()].sort((a, b) => a - b).forEach((t) => {
      const evs = byTime.get(t);
      [1, -1, 0].forEach((dir) => {
        const grp = evs.filter((e) => Math.sign(e.direction) === dir);
        if (!grp.length) return;
        grp.sort((a, b) => b.strength - a.strength);
        const top = grp[0], ui = TYPES[top.type];
        markers.push({ time: t, position: dir >= 0 ? "belowBar" : "aboveBar", shape: ui.shape, color: ui.c, size: grp.length > 1 ? 2 : 1,
                       text: labels ? ui.text + (grp.length > 1 ? " +" + (grp.length - 1) : "") : "", id: t + ":" + dir });
      });
    });
    chart.setMarkers(markers);
    renderLegendCount(markers.length);
  }
  function barSpacing() {
    const r = chart.chart.timeScale().getVisibleLogicalRange(), w = $("chart").clientWidth;
    return r && r.to > r.from ? w / (r.to - r.from) : 20;
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; rebuild(); }); }
  function renderLegendCount(n) { const c = $("flowCount"); if (c) c.textContent = n + " marker(s), " + signals.size + " signal(s) loaded"; }

  // ---- inbound data ---------------------------------------------------------------------------------
  function onSnapshot(m) {
    curTf = m.timeframe;
    signals = new Map((m.flow_signals || []).map((s) => [s.id, s]));
    Object.entries(m.flow_state || {}).forEach(([k, v]) => { flowState[k] = v; }); renderStrip();
    $("flowStrip").classList.toggle("hidden", !m.flow_enabled);
    $("btnFlow").classList.toggle("hidden", !m.flow_enabled); $("btnVal").classList.toggle("hidden", !m.flow_enabled);
    refreshStrikes(); schedule();
  }
  function onSignal(s) { signals.set(s.id, s); refreshStrikes(); schedule(); }
  function onState(m) { flowState[m.tf] = m.state; renderStrip(); }
  function onCandle() { schedule(); }

  // ---- strip ------------------------------------------------------------------------------------------
  function flowTf() { const f = cfg.flow.timeframes; return f.includes(curTf) ? curTf : (settings.tf !== "all" && f.includes(settings.tf) ? settings.tf : f[0]); }
  function gauge(label, score, lab, color) {
    const g = el("span", "g"); g.append(el("span", "muted", label));
    const bar = el("span", "bar"), i = el("i"); i.style.width = Math.min(100, score || 0) + "%"; i.style.background = color; bar.append(i); g.append(bar);
    g.append(el("b", "", fmt(score, 0)), el("span", "muted", lab || "")); return g;
  }
  function renderStrip() {
    const s = flowState[flowTf()], strip = $("flowStrip"); strip.replaceChildren();
    if (!s || s.time == null) { strip.append(el("span", "muted", "Flow: waiting for the first closed " + flowTf() + " bar…")); return; }
    strip.append(el("b", "", "FLOW " + flowTf()));
    if (s.suppressed) strip.append(el("span", "muted", "pressure score suppressed: " + (s.reason || "insufficient data")));
    else { strip.append(gauge("Buy", s.buy_score, s.buy_label, UP), gauge("Sell", s.sell_score, s.sell_label, DOWN)); }
    strip.append(el("span", "muted", "futures: " + (s.positioning ? s.positioning.replaceAll("_", " ").toLowerCase() : "no qualifying move")));
    if (s.basis != null) strip.append(el("span", "muted", "basis " + fmt(s.basis)));
    strip.append(el("span", "muted", "regime " + (s.regime || "unknown").replace("|", " / ").replaceAll("_", " ")));
    strip.title = "Click for the score breakdown. Scores describe recent activity; they are not predictions.";
  }
  function showBreakdown() {
    const s = flowState[flowTf()]; if (!s) return;
    const p = $("detail"); p.replaceChildren(); p.classList.remove("hidden"); closeBtn(p);
    p.append(el("h4", "", "Pressure score breakdown · " + flowTf()));
    p.append(row("Buy score", fmt(s.buy_score, 0) + " (" + s.buy_label + ")"), row("Sell score", fmt(s.sell_score, 0) + " (" + s.sell_label + ")"),
             row("Weight with real data", fmt(s.available_weight, 0) + "%"));
    p.append(componentTable(s.components || []));
    if (s.options && s.options.length) {
      p.append(el("h5", "", "Option contracts (current window)"));
      const t = el("table", "t"); t.innerHTML = "<tr><th>Strike</th><th>Label</th><th>Buy</th><th>Sell</th></tr>";
      s.options.forEach((o) => { const r = t.insertRow(); [fmt(o.strike, 0) + " " + o.kind, o.label.replace(" ACTIVITY", "").toLowerCase(), fmt(o.buy_score, 0), fmt(o.sell_score, 0)].forEach((x) => r.insertCell().textContent = x); });
      p.append(t);
    }
    p.append(el("div", "note", "Unavailable components score 0 and lower data quality; nothing is invented. Scores are not probabilities and do not identify participants."));
  }
  function componentTable(cs) {
    const t = el("table", "t"); t.innerHTML = "<tr><th>Component</th><th>Wt</th><th>Buy</th><th>Sell</th></tr>";
    cs.forEach((c) => {
      const r = t.insertRow(); r.title = (c.raw || "") + " [" + c.provenance + "] " + (c.note || "");
      [c.name.replaceAll("_", " ") + (c.available ? "" : " (n/a)"), fmt(c.weight, 0), fmt(c.buy_points, 1), fmt(c.sell_points, 1)].forEach((x) => r.insertCell().textContent = x);
    });
    return t;
  }

  // ---- detail panel -----------------------------------------------------------------------------------
  const row = (k, v) => { const r = el("div", "row"); r.append(el("span", "muted", k), el("span", "", v == null ? "n/a" : String(v))); return r; };
  function closeBtn(p) { const b = el("button", "close", "×"); b.onclick = () => { p.classList.add("hidden"); chart.clearLines(); }; p.append(b); }
  function evBlock(e) {
    const d = el("div", "ev " + e.state); const nm = el("span", "nm", e.name); const tg = el("span", "tag " + e.provenance, e.provenance); nm.append(tg);
    d.append(nm, el("div", "", e.value)); if (e.note) d.append(el("div", "nt", e.note)); return d;
  }
  async function openCandle(t) {
    const evs = byTime.get(t); if (!evs || !evs.length) return;
    const p = $("detail"); p.replaceChildren(); p.classList.remove("hidden"); $("flowPanel").classList.add("hidden"); $("valPanel").classList.add("hidden"); closeBtn(p);
    p.append(el("h4", "", "Events on candle " + clock(t)));
    p.append(el("div", "note", evs.length + " event(s) (several are combined into this list instead of overlapping labels). Click one for the full analysis."));
    evs.sort((a, b) => b.strength - a.strength).forEach((s) => {
      const b = el("button", "evl"); const ui = TYPES[s.type]; b.style.borderLeft = "4px solid " + ui.c;
      b.append(el("span", "s", fmt(s.strength, 0) + " · " + s.tf)); b.append(document.createTextNode(s.headline));
      b.onclick = () => openSignal(s.id); p.append(b);
    });
    if (evs.length === 1) openSignal(evs[0].id);
  }
  async function openSignal(id) {
    let d; try { d = await (await fetch("/api/flow/signal/" + encodeURIComponent(id))).json(); } catch (e) { return; }
    selected = d; const ui = TYPES[d.type];
    const p = $("detail"); p.replaceChildren(); p.classList.remove("hidden"); closeBtn(p);
    const h = el("div", "big", d.headline); h.style.color = ui.c; p.append(h, el("div", "muted", d.label + (d.mode === "mock" ? " · MOCK DATA" : "")));
    p.append(row("Candle (signal bar)", clock(d.time)), row("Observation timeframe", d.tf), row("Underlying NIFTY (bar close)", fmt(d.underlying_price)));
    if (d.strike) p.append(row("Strike / expiry", fmt(d.strike, 0) + " " + d.instrument + " / " + d.expiry));
    if (d.premium != null) p.append(row("Premium at detection", fmt(d.premium)));
    if (d.vol_change != null && (d.instrument === "CE" || d.instrument === "PE")) p.append(row("Window volume (units)", fmt(d.vol_change, 0)));
    if (d.oi_change != null) p.append(row("OI change", fmt(d.oi_change, 0)));
    const sr = el("div", "row"); sr.append(el("span", "muted", "Signal strength"), el("span", "big", fmt(d.strength, 0) + "/100 · " + d.strength_label)); p.append(sr);
    const q = el("div", "row"); const qv = el("span", "q-" + d.data_quality, d.data_quality); q.append(el("span", "muted", "Data quality"), qv); p.append(q);
    (d.details.quality_reasons || []).forEach((r) => p.append(el("div", "note", "• " + r)));
    p.append(row("Market regime at signal", (d.regime || "").replace("|", " / ").replaceAll("_", " ")));
    p.append(el("h5", "", "Interpretation"), el("div", "", d.interpretation));
    const sup = d.evidence.filter((e) => e.state === "supports"), con = d.evidence.filter((e) => e.state === "conflicts"), neu = d.evidence.filter((e) => e.state === "neutral");
    p.append(el("h5", "", "Supporting evidence")); (sup.length ? sup : []).forEach((e) => p.append(evBlock(e))); if (!sup.length) p.append(el("div", "note", "none"));
    p.append(el("h5", "", "Conflicting evidence")); con.forEach((e) => p.append(evBlock(e))); if (!con.length) p.append(el("div", "note", "none recorded"));
    if (neu.length) { p.append(el("h5", "", "Other data")); neu.forEach((e) => p.append(evBlock(e))); }
    if (d.type.endsWith("PRESSURE")) {
      p.append(el("h5", "", "Component scores")); const t = el("table", "t"); t.innerHTML = "<tr><th>Component</th><th>Wt</th><th>Buy</th><th>Sell</th></tr>";
      Object.entries(d.components).forEach(([k, c]) => { const r = t.insertRow(); [k.replaceAll("_", " ") + (c.available ? "" : " (n/a)"), fmt(c.weight, 0), fmt(c.buy_points, 1), fmt(c.sell_points, 1)].forEach((x) => r.insertCell().textContent = x); });
      p.append(t, row("Buy / sell score", fmt(d.details.buy_score, 0) + " / " + fmt(d.details.sell_score, 0)));
    }
    chart.clearLines();
    if (d.details.level != null) {
      chart.addLine(d.details.level, (d.details.level_name || "level"), ORANGE);
      if (d.details.breakout_time) {
        p.append(el("h5", "", "Trap sequence"), row("Breakout candle", clock(d.details.breakout_time)), row("Failed-breakout candle", clock(d.details.failure_time)),
                 row("Confirmation candle", clock(d.details.confirmation_time)));
        ["breakout", "failure", "confirmation"].forEach((k) => { const x = d.details[k]; if (x) p.append(row(k + " volume / OI%", (x.fut_volume_rel == null ? "n/a" : x.fut_volume_rel + "x") + " / " + (x.oi_change_pct == null ? "n/a" : x.oi_change_pct + "%"))); });
      }
    }
    p.append(el("div", "note", "Figures are tagged OBSERVED (feed), CALCULATED (arithmetic on feed data) or ESTIMATED (model inference). This is a description of market activity, not a trade recommendation."));
    chart.scrollToTime(d.time);
  }

  // ---- filter panel ---------------------------------------------------------------------------------
  function refreshStrikes() {
    const p = $("flowPanel"); if (p.classList.contains("hidden")) return;
    const ss = $("fStrike"), ee = $("fExpiry"); if (!ss) return;
    const strikes = [...new Set([...signals.values()].filter((s) => s.strike).map((s) => String(s.strike)))].sort((a, b) => a - b);
    const exps = [...new Set([...signals.values()].filter((s) => s.expiry).map((s) => s.expiry))];
    const fill = (sel, vals, cur) => { sel.replaceChildren(new Option("All", "all")); vals.forEach((v) => sel.add(new Option(v, v))); sel.value = vals.includes(cur) ? cur : "all"; };
    fill(ss, strikes, settings.strike); fill(ee, exps, settings.expiry);
  }
  async function openFlowPanel() {
    const p = $("flowPanel"); p.replaceChildren(); closeBtn(p);
    p.append(el("h4", "", "Flow signals"));
    const sel = (id, label, opts, cur, cb) => { const l = el("label", "", label); const s = el("select"); s.id = id; opts.forEach(([v, t]) => s.add(new Option(t, v))); s.value = cur; s.onchange = () => cb(s.value); l.append(s); return l; };
    p.append(sel("fTf", "Observation timeframe", [["all", "All"], ...cfg.flow.timeframes.map((t) => [t, t])], settings.tf, (v) => { settings.tf = v; save(); schedule(); renderStrip(); }));
    p.append(sel("fStrike", "Strike (options)", [["all", "All"]], "all", (v) => { settings.strike = v; save(); schedule(); }));
    p.append(sel("fExpiry", "Expiry (options)", [["all", "All"]], "all", (v) => { settings.expiry = v; save(); schedule(); }));
    p.append(sel("fQual", "Minimum data quality", [["Poor", "Any"], ["Fair", "Fair or better"], ["Good", "Good only"]], settings.minQuality, (v) => { settings.minQuality = v; save(); schedule(); }));
    const l = el("label", "", "Min strength "); const r = el("input"); r.type = "range"; r.min = 0; r.max = 90; r.step = 5; r.value = settings.minStrength; const rv = el("b", "", String(settings.minStrength));
    r.oninput = () => { settings.minStrength = +r.value; rv.textContent = r.value; save(); schedule(); }; l.append(r, rv); p.append(l);
    const groups = {}; Object.entries(TYPES).forEach(([k, v]) => (groups[v.group] = groups[v.group] || []).push(k));
    Object.entries(groups).forEach(([g, ks]) => {
      p.append(el("h5", "", g)); const grid = el("div", "grid2");
      ks.forEach((k) => { const lb = el("label"); const cb = el("input"); cb.type = "checkbox"; cb.checked = !settings.off.includes(k);
        cb.onchange = () => { settings.off = cb.checked ? settings.off.filter((x) => x !== k) : [...settings.off, k]; save(); schedule(); };
        const dot = el("span", "", "●"); dot.style.color = TYPES[k].c; lb.append(cb, dot, document.createTextNode(TYPES[k].name)); grid.append(lb); });
      p.append(grid);
    });
    p.append(el("div", "note", "Green = buying-side activity, red = selling-side, blue = short covering, orange = long unwinding / traps. Markers sit above the bar for bearish implications and below for bullish ones (so a green PUT marker above a bar is put BUYING). Markers appear after a bar closes and never repaint."));
    p.append(el("div", "note", "", ));
    const fc = el("div", "note"); fc.id = "flowCount"; p.append(fc);
    p.append(el("h5", "", "Data sources"));
    const box = el("div"); p.append(box);
    try {
      const s = await (await fetch("/api/flow/status")).json();
      box.append(row("Futures", s.futures ? s.futures.symbol : "n/a"), row("Option expiry (single)", s.option_expiry || "n/a"), row("ATM strike", s.atm_strike ? fmt(s.atm_strike, 0) : "n/a"),
                 row("NFO tokens subscribed", s.subscribed_tokens + " / " + s.subscription_cap), row("Broker Greeks", s.greeks), row("Instrument master", s.master),
                 row("Derivative ticks", s.counters.deriv_ticks), row("Dropped (late/dup/ooo/malformed)", [s.late_ticks_dropped, s.counters.duplicate, s.counters.out_of_order, s.counters.malformed].join(" / ")),
                 row("Crossed-depth ticks / invalid OI", s.counters.crossed_depth + " / " + s.counters.oi_invalid), row("Fut-vs-spot timestamp skew (s)", s.ts_skew_fut_vs_spot_s));
      s.caveats.forEach((c) => box.append(el("div", "note", "• " + c)));
    } catch (e) { box.append(el("div", "note", "status unavailable")); }
    refreshStrikes(); $("fStrike").value = settings.strike; $("fExpiry").value = settings.expiry; rebuild();
  }

  // ---- validation panel -----------------------------------------------------------------------------
  async function openValidation() {
    const p = $("valPanel"); p.replaceChildren(); closeBtn(p); p.append(el("h4", "", "Historical validation"));
    const hs = cfg.flow.horizons; let h = String(hs[2] || hs[0]);
    const lab = el("label", "", "Horizon (bars after signal) "); const sel = el("select"); hs.forEach((x) => sel.add(new Option(x, x))); sel.value = h; lab.append(sel); p.append(lab);
    const body = el("div"); p.append(body);
    async function draw() {
      body.replaceChildren(); h = sel.value;
      let v; try { v = await (await fetch("/api/flow/validation?group_by=type")).json(); } catch (e) { body.append(el("div", "note", "unavailable")); return; }
      if (v.mode === "mock") body.append(el("div", "banner err", v.warning)); else body.append(el("div", "note", v.warning));
      body.append(el("div", "note", "Entry = close of signal bar. Costs: " + v.assumptions.cost_bps_round_trip + " bps round trip + " + v.assumptions.slippage_bps_per_side + " bps slippage per side (config assumptions). False signal = no gross move in the signalled direction at the horizon."));
      const t = el("table", "t"); t.innerHTML = "<tr><th>Type</th><th>n</th><th>hit%</th><th>false%</th><th>net bps</th><th>MFE</th><th>MAE</th></tr>";
      Object.entries(v.groups).forEach(([k, g]) => { const s = g[h]; if (!s) return; const r = t.insertRow();
        [k, s.n + (s.sufficient_sample ? "" : "*"), Math.round(s.hit_rate_net * 100), Math.round(s.false_signal_rate * 100), s.mean_net_bps, s.mean_mfe_bps, s.mean_mae_bps].forEach((x) => r.insertCell().textContent = x);
        r.style.cursor = "pointer"; r.onclick = () => walk(k); });
      body.append(t, el("div", "note", "* fewer than " + v.assumptions.min_sample_for_ci + " samples: not statistically meaningful. Click a row for walk-forward (out-of-sample) results."));
      if (v.frequency.length) { const f = v.frequency.map((x) => x.type + "/" + x.tf + ": " + x.signals + " (" + (x.per_session == null ? "?" : x.per_session) + "/session)").join("; "); body.append(el("div", "note", "Frequency: " + f)); }
      body.append(el("div", "", "")); body.lastChild.id = "wfOut";
    }
    async function walk(type) {
      const out = $("wfOut"); out.replaceChildren(el("div", "note", "Running walk-forward…"));
      const tfk = settings.tf !== "all" ? settings.tf : cfg.flow.timeframes[0];
      const w = await (await fetch("/api/flow/validation/walkforward?type=" + type + "&tf=" + tfk + "&horizon=" + sel.value)).json();
      out.replaceChildren(el("h5", "", "Walk-forward · " + type + " · " + tfk + " · " + sel.value + " bars"));
      if (w.status !== "ok") { out.append(el("div", "note", w.message || w.status)); return; }
      const o = w.pooled_oos; out.append(row("Out-of-sample signals", o.n), row("OOS hit rate (net)", Math.round(o.hit_rate_net * 100) + "%"), row("OOS mean net bps", o.mean_net_bps),
                                         row("95% CI (net bps)", o.net_mean_ci95_bps ? o.net_mean_ci95_bps.join(" … ") : "sample too small"), row("Folds", w.folds.length));
      out.append(el("div", "note", w.note));
    }
    sel.onchange = draw; draw();
  }

  function toggle(id, other, fn) {
    const p = $(id), open = p.classList.toggle("hidden") === false;
    other.forEach((o) => $(o).classList.add("hidden")); if (open && fn) fn(); return open;
  }

  function init(config, c) {
    cfg = config; chart = c;
    if (!cfg.flow || !cfg.flow.enabled) { $("flowStrip").classList.add("hidden"); $("btnFlow").classList.add("hidden"); $("btnVal").classList.add("hidden"); return; }
    chart.onClick = (t) => openCandle(t);
    let lastLab = null; chart.chart.timeScale().subscribeVisibleLogicalRangeChange(() => { const l = barSpacing() >= 16; if (l !== lastLab) { lastLab = l; schedule(); } });
    $("btnFlow").onclick = () => { toggle("flowPanel", ["valPanel", "detail"], openFlowPanel); $("btnFlow").setAttribute("aria-expanded", !$("flowPanel").classList.contains("hidden")); };
    $("btnVal").onclick = () => { toggle("valPanel", ["flowPanel", "detail"], openValidation); };
    $("flowStrip").onclick = showBreakdown;
    renderStrip();
  }
  window.NiftyFlow = { init, onSnapshot, onSignal, onState, onCandle, setTf: (tf) => { curTf = tf; renderStrip(); schedule(); } };
})();
