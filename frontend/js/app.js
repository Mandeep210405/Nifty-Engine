(function () {
  const $ = (id) => document.getElementById(id);
  const settings = NiftySettings.load();
  let cfg = null, tf = settings.timeframe, ws = null, wsAttempt = 0, chart = null, lastStatus = null;
  let volumeAvailable = false, lastQuote = null;

  const STATE_LABEL = { connecting: "Connecting", connected: "Connected", subscribed: "Connected", stale: "Stale feed",
    disconnected: "Disconnected", reconnecting: "Reconnecting", auth_failed: "Authentication failed",
    market_closed: "Market closed / no ticks", mock: "DEMO feed", error: "Error", backend_down: "Backend offline" };

  const fmtP = (v) => v == null ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const tzOf = () => settings.tz === "local" ? undefined : settings.tz;
  const fmtClock = (ts) => ts ? new Intl.DateTimeFormat("en-GB", { timeZone: tzOf(), hour12: false, hour: "2-digit",
      minute: "2-digit", second: "2-digit" }).format(new Date(ts * 1000)) : "—";

  function setState(state, detail) {
    const pill = $("statePill");
    pill.dataset.state = state; pill.textContent = STATE_LABEL[state] || state; pill.title = detail || "";
    const b = $("stateBanner");
    const msgs = {
      stale: "Feed is stale: no ticks received recently during market hours. The app will reconnect automatically.",
      market_closed: "Market closed or no ticks. The last known price is shown until new ticks arrive.",
      auth_failed: "Authentication with Angel One failed: " + (detail || "") + " Fix .env and restart the server (see README troubleshooting).",
      disconnected: "Feed disconnected. " + (detail || ""), reconnecting: "Reconnecting… " + (detail || ""),
      error: "Feed error: " + (detail || ""), backend_down: "Cannot reach the local backend. Retrying…",
      connecting: "Connecting to the data feed…",
    };
    const m = msgs[state];
    b.classList.toggle("hidden", !m);
    b.classList.toggle("err", ["auth_failed", "error", "backend_down", "disconnected"].includes(state));
    if (m) b.textContent = m;
  }

  function renderQuote(q) {
    lastQuote = q;
    $("price").textContent = fmtP(q.price);
    const cls = q.change == null ? "" : q.change >= 0 ? "up" : "down";
    $("chg").className = "chg " + cls; $("chgPct").className = "chg " + cls;
    $("chg").textContent = q.change == null ? "—" : (q.change >= 0 ? "+" : "") + fmtP(q.change);
    $("chgPct").textContent = q.change_pct == null ? "" : "(" + (q.change_pct >= 0 ? "+" : "") + q.change_pct.toFixed(2) + "%)";
    $("lastTick").textContent = fmtClock(q.exchange_ts) + (q.stale_snapshot ? " (last known)" : "");
    $("ticks").textContent = q.tick_count;
    $("latency").textContent = q.latency_ms == null ? "n/a" : q.latency_ms.toFixed(0) + " ms";
  }

  function renderStatus(s) {
    lastStatus = s;
    setState(s.state, s.detail);
    $("mockBadge").classList.toggle("hidden", !s.mock);
    if (s.latency_ms_avg != null) $("latency").textContent = s.latency_ms_last + " ms (avg " + s.latency_ms_avg + ")";
    else if (s.mock) $("latency").textContent = "n/a (mock)";
    $("ticks").textContent = s.tick_count;
    const rows = [
      ["State", s.state], ["Detail", s.detail || "—"], ["Mode", s.mode], ["Instrument", s.instrument.name + " / " + s.instrument.token],
      ["Instrument check", s.instrument_note], ["Market session (no holidays)", s.market_session_open ? "open" : "closed"],
      ["Last exchange tick", fmtClock(s.last_exchange_ts)], ["Last received", fmtClock(s.last_recv_ts)],
      ["Last tick age", s.last_tick_age_s == null ? "—" : s.last_tick_age_s + " s"],
      ["Latency last / avg", s.latency_ms_last == null ? "n/a" : s.latency_ms_last + " / " + s.latency_ms_avg + " ms"],
      ["Ticks accepted", s.tick_count], ["Dropped", Object.entries(s.dropped).map(([k, v]) => k + ":" + v).join(" ")],
      ["Reconnect attempt", s.reconnect_attempt], ["Volume from feed", s.volume_available ? "yes" : "no"],
      ["History (" + tf + ")", s.history && s.history[tf] ? s.history[tf].state + " " + (s.history[tf].count || "") : "pending"],
      ["Last error", s.last_error || "—"], ["Browser clients", s.clients],
    ];
    $("diagList").innerHTML = "";
    rows.forEach(([k, v]) => { const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k; dd.textContent = String(v); $("diagList").append(dt, dd); });
  }

  function showEmpty(candles, hist) {
    const e = $("empty");
    if (candles.length) { e.classList.add("hidden"); return; }
    e.classList.remove("hidden");
    $("emptyTitle").textContent = "No candles yet";
    $("emptyMsg").textContent = (hist && hist.message) ? hist.message + " Charting begins from live ticks as they arrive."
      : "Waiting for live ticks. Nothing is fabricated; candles appear as data arrives.";
  }

  function onSnapshot(m) {
    if (m.timeframe !== tf) return;
    volumeAvailable = m.volume_available;
    chart.setSnapshot(m.candles, m.indicators);
    NiftyFlow.onSnapshot(m);
    showEmpty(m.candles, m.history_status);
    $("volNote").textContent = m.volume_note + (m.history_status && m.history_status.state === "unsupported" && !m.mock ? " " + m.history_status.message : "");
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + "/ws/market?timeframe=" + encodeURIComponent(tf));
    ws.onopen = () => { wsAttempt = 0; };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      switch (m.type) {
        case "snapshot": onSnapshot(m); break;
        case "candle": if (m.timeframe === tf) { chart.updateCandle(m.candle); $("empty").classList.add("hidden"); if (m.is_new) NiftyFlow.onCandle(); } break;
        case "flow_signal": NiftyFlow.onSignal(m.signal); break;
        case "flow_state": NiftyFlow.onState(m); break;
        case "indicator": if (m.timeframe === tf) chart.updateIndicator(m.name, m.point); break;
        case "quote": renderQuote(m); break;
        case "status": renderStatus(m); break;
        case "history_status": if (m.timeframe === tf) showEmpty([], m.status); break;
        case "error": console.warn("server:", m.message); break;
      }
    };
    ws.onclose = () => {
      setState("backend_down", "");
      const d = Math.min(15000, 1000 * Math.pow(2, wsAttempt++));
      setTimeout(connectWS, d);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  }

  function selectTf(key) {
    tf = key; settings.timeframe = key; if (window.NiftyFlow) NiftyFlow.setTf(key); NiftySettings.save(settings);
    document.querySelectorAll("#tfs button").forEach((b) => b.classList.toggle("active", b.dataset.tf === key));
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ action: "set_timeframe", timeframe: key }));
  }

  function legend(d, t) {
    const el = $("legend");
    if (!d) { el.innerHTML = '<span class="muted">Hover chart for OHLC</span>'; return; }
    const c = d.close >= d.open ? "u" : "d";
    el.innerHTML = '<span class="muted">' + chart.fmtDateTime(t) + '</span>' +
      ["open", "high", "low", "close"].map((k) => '<span class="' + c + '">' + k[0].toUpperCase() + " " + fmtP(d[k]) + "</span>").join("");
  }

  function bindSettings() {
    const map = { setVolume: "volume", setGrid: "grid", setPriceLine: "priceLine", setFollow: "follow" };
    Object.entries(map).forEach(([id, key]) => { $(id).checked = settings[key];
      $(id).onchange = () => { settings[key] = $(id).checked; apply(); }; });
    $("setTz").value = settings.tz; $("setUp").value = settings.up; $("setDown").value = settings.down;
    $("setTz").onchange = () => { settings.tz = $("setTz").value; apply(); };
    $("setUp").onchange = () => { settings.up = $("setUp").value; apply(); };
    $("setDown").onchange = () => { settings.down = $("setDown").value; apply(); };
    $("setDefaults").onclick = () => { Object.assign(settings, NiftySettings.DEFAULTS, { timeframe: tf }); bindSettings(); apply(); };
  }
  function apply() {
    NiftySettings.save(settings); chart.applySettings(settings);
    $("tzLabel").textContent = settings.tz === "Asia/Kolkata" ? "IST" : settings.tz === "UTC" ? "UTC" : "Local";
    document.documentElement.style.setProperty("--up", settings.up); document.documentElement.style.setProperty("--down", settings.down);
    if (lastQuote) renderQuote(lastQuote);
  }
  function toggle(btn, panel, other) {
    const open = $(panel).classList.toggle("hidden") === false;
    btn.setAttribute("aria-expanded", open);
    if (open && other) $(other).classList.add("hidden");
  }

  async function init() {
    try { cfg = await (await fetch("/api/config")).json(); } catch (e) { cfg = { timeframes: [], instrument: { name: "NIFTY 50" }, mock: false }; setState("backend_down"); }
    $("sym").textContent = cfg.instrument.name;
    $("mockBadge").classList.toggle("hidden", !cfg.mock);
    if (!cfg.timeframes.some((t) => t.key === tf)) tf = "1m";
    cfg.timeframes.forEach((t) => { const b = document.createElement("button"); b.textContent = t.label; b.dataset.tf = t.key;
      b.title = t.native_broker_candles ? t.label + " candles" : t.label + " candles are aggregated from live ticks";
      b.onclick = () => selectTf(t.key); $("tfs").append(b); });
    chart = new NiftyChart($("chart"), settings, legend);
    NiftyFlow.init(cfg, chart);
    bindSettings(); apply(); selectTf(tf);
    $("btnFit").onclick = () => chart.fit(); $("btnReset").onclick = () => chart.reset();
    $("btnSettings").onclick = (e) => toggle(e.currentTarget, "settings", "diag");
    $("btnDiag").onclick = (e) => toggle(e.currentTarget, "diag", "settings");
    connectWS();
  }
  init();
})();
