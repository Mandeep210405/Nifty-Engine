// Thin wrapper around TradingView Lightweight Charts v5 (addSeries API).
(function () {
  const LWC = window.LightweightCharts;

  function tzName(tz) { return tz === "local" ? undefined : tz; }

  function NiftyChart(container, settings, onCrosshair) {
    this.settings = settings;
    this.lastTime = null;
    this.chart = LWC.createChart(container, {
      autoSize: true,
      layout: { background: { type: "solid", color: "#1b1f26" }, textColor: "#8a93a3", fontFamily: "system-ui,sans-serif" },
      grid: { vertLines: { color: "#262b34" }, horzLines: { color: "#262b34" } },
      rightPriceScale: { borderColor: "#2c323d", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#2c323d", timeVisible: true, secondsVisible: true, rightOffset: 6 },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      handleScroll: true, handleScale: true,
    });
    this.candles = this.chart.addSeries(LWC.CandlestickSeries, {
      upColor: settings.up, downColor: settings.down, borderVisible: false,
      wickUpColor: settings.up, wickDownColor: settings.down, priceLineVisible: settings.priceLine,
    });
    this.volume = this.chart.addSeries(LWC.HistogramSeries, {
      priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false,
    });
    this.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    this.indicatorSeries = {};
    this.times = [];
    this.markerPlugin = LWC.createSeriesMarkers ? LWC.createSeriesMarkers(this.candles, []) : null;
    this.lines = [];
    this.chart.subscribeClick((p) => { if (this.onClick && p && p.time) this.onClick(p.time, p); });
    this.chart.subscribeCrosshairMove((p) => {
      const d = p && p.seriesData ? p.seriesData.get(this.candles) : null;
      onCrosshair(d || null, p && p.time);
    });
    this.applySettings(settings);
  }

  NiftyChart.prototype.fmtTime = function (t, withSeconds) {
    const opts = { timeZone: tzName(this.settings.tz), hour12: false, hour: "2-digit", minute: "2-digit" };
    if (withSeconds) opts.second = "2-digit";
    return new Intl.DateTimeFormat("en-GB", opts).format(new Date(t * 1000));
  };
  NiftyChart.prototype.fmtDateTime = function (t) {
    return new Intl.DateTimeFormat("en-GB", { timeZone: tzName(this.settings.tz), hour12: false, year: "numeric",
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(t * 1000));
  };

  NiftyChart.prototype.applySettings = function (s) {
    this.settings = s;
    const tz = tzName(s.tz);
    const self = this;
    this.chart.applyOptions({
      grid: { vertLines: { visible: s.grid, color: "#262b34" }, horzLines: { visible: s.grid, color: "#262b34" } },
      localization: { timeFormatter: (t) => self.fmtDateTime(t),
                      priceFormatter: (p) => p.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) },
      timeScale: {
        tickMarkFormatter: (t, type) => {
          const o = { timeZone: tz, hour12: false };
          if (type === 0) return new Intl.DateTimeFormat("en-GB", { ...o, year: "numeric" }).format(new Date(t * 1000));
          if (type === 1) return new Intl.DateTimeFormat("en-GB", { ...o, month: "short" }).format(new Date(t * 1000));
          if (type === 2) return new Intl.DateTimeFormat("en-GB", { ...o, day: "2-digit", month: "short" }).format(new Date(t * 1000));
          return self.fmtTime(t, type === 4);
        },
      },
    });
    this.candles.applyOptions({ upColor: s.up, downColor: s.down, wickUpColor: s.up, wickDownColor: s.down,
                                priceLineVisible: s.priceLine });
    this.volume.applyOptions({ visible: s.volume });
    if (this._lastCandles) this._volData(this._lastCandles);
  };

  NiftyChart.prototype._volData = function (candles) {
    const s = this.settings;
    this.volume.setData(candles.filter((c) => c.volume != null).map((c) => ({
      time: c.time, value: c.volume, color: (c.close >= c.open ? s.up : s.down) + "88" })));
  };

  NiftyChart.prototype.setSnapshot = function (candles, indicators) {
    this._lastCandles = candles;
    this.times = candles.map((c) => c.time);
    this.candles.setData(candles.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
    this._volData(candles);
    this.lastTime = candles.length ? candles[candles.length - 1].time : null;
    Object.values(this.indicatorSeries).forEach((sr) => this.chart.removeSeries(sr));
    this.indicatorSeries = {};
    Object.entries(indicators || {}).forEach(([name, pts]) => {
      const sr = this.chart.addSeries(LWC.LineSeries, { color: "#e0a526", lineWidth: 1, priceLineVisible: false, title: name });
      sr.setData(pts);
      this.indicatorSeries[name] = sr;
    });
    this.fit();
  };

  NiftyChart.prototype.updateCandle = function (c) {
    if (this.lastTime !== null && c.time < this.lastTime) return;   // never update older than newest
    this.candles.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
    if (!this.times.length || this.times[this.times.length - 1] < c.time) this.times.push(c.time);
    if (c.volume != null) {
      this.volume.update({ time: c.time, value: c.volume, color: (c.close >= c.open ? this.settings.up : this.settings.down) + "88" });
    }
    this.lastTime = c.time;
    if (this.settings.follow) this.chart.timeScale().scrollToRealTime();
  };

  NiftyChart.prototype.updateIndicator = function (name, p) {
    const sr = this.indicatorSeries[name];
    if (sr) sr.update(p);
  };

  NiftyChart.prototype.setMarkers = function (list) { if (this.markerPlugin) this.markerPlugin.setMarkers(list); };
  NiftyChart.prototype.clearLines = function () { this.lines.forEach((l) => this.candles.removePriceLine(l)); this.lines = []; };
  NiftyChart.prototype.addLine = function (price, title, color) {
    this.lines.push(this.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title }));
  };
  NiftyChart.prototype.scrollToTime = function (t) {
    const ts = this.chart.timeScale(), r = ts.getVisibleRange();
    if (!r || t < r.from || t > r.to) { const span = r ? r.to - r.from : 3600; ts.setVisibleRange({ from: t - span / 2, to: t + span / 2 }); }
  };

  NiftyChart.prototype.fit = function () { this.chart.timeScale().fitContent(); };
  NiftyChart.prototype.reset = function () {
    this.chart.priceScale("right").applyOptions({ autoScale: true });
    this.chart.timeScale().resetTimeScale();
    this.chart.timeScale().scrollToRealTime();
  };

  window.NiftyChart = NiftyChart;
})();
