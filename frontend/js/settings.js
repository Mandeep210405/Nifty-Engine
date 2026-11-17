// Chart preferences, persisted per-browser (localStorage; failures are non-fatal).
(function () {
  const KEY = "nifty-live-engine.settings.v1";
  const DEFAULTS = { volume: true, grid: true, priceLine: true, follow: true, tz: "Asia/Kolkata",
                     up: "#26a69a", down: "#ef5350", timeframe: "1m" };
  function load() {
    try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || "{}")); }
    catch (e) { return Object.assign({}, DEFAULTS); }
  }
  function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* ignore */ } }
  window.NiftySettings = { DEFAULTS, load, save };
})();
