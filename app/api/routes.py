from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.config import FRONTEND_DIR
from app.flow import validation as flow_validation
from app.flow.models import SIGNAL_TYPES
from app.timeframes import TIMEFRAME_KEYS, TIMEFRAMES

log = logging.getLogger(__name__)
router = APIRouter()


def _engine(obj):
    return obj.app.state.engine


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")


@router.get("/api/health")
async def health(request: Request):
    st = _engine(request).status()
    return {"backend": "ok", "feed_state": st["state"], "feed_detail": st["detail"], "mode": st["mode"],
            "latest_tick_age_s": st["last_tick_age_s"], "instrument": st["instrument"],
            "instrument_verified": st["instrument_verified"], "market_session_open": st["market_session_open"],
            "tick_count": st["tick_count"]}


@router.get("/api/status")
async def status(request: Request):
    return _engine(request).status()


@router.get("/api/config")
async def config(request: Request):
    e = _engine(request)
    return {"mock": e.s.is_mock, "instrument": e.inst.public(), "stale_after_s": e.s.stale_after_s,
            "timeframes": [{"key": t.key, "label": t.label, "seconds": t.seconds,
                            "native_broker_candles": t.angel_interval is not None, "anchor": t.anchor}
                           for t in TIMEFRAMES.values()],
            "indicators": list(e.s.enabled_indicators),
            "flow": {"enabled": e.flow is not None, "timeframes": list(e.s.flow.timeframes),
                     "horizons": list(e.s.flow.horizons),
                     "signal_types": {k: {"tag": v[0], "group": v[1], "direction": v[2]} for k, v in SIGNAL_TYPES.items()}}}


@router.get("/api/history")
async def history(request: Request, timeframe: str = Query("1m"), limit: int = Query(1500, ge=1, le=6000)):
    if timeframe not in TIMEFRAMES:
        raise HTTPException(422, f"Unsupported timeframe '{timeframe}'. Allowed: {', '.join(TIMEFRAME_KEYS)}")
    snap = _engine(request).snapshot(timeframe, limit)
    return {k: snap[k] for k in ("timeframe", "instrument", "candles", "volume_available", "volume_note",
                                 "history_status", "mock")}


@router.websocket("/ws/market")
async def ws_market(ws: WebSocket):
    engine = ws.app.state.engine
    await ws.accept()
    tf = ws.query_params.get("timeframe", "1m")
    if tf not in TIMEFRAMES:
        tf = "1m"
    q = engine.add_client()

    async def send_snapshot():
        await ws.send_json(engine.snapshot(tf))

    async def reader():
        nonlocal tf
        while True:
            try:
                data = json.loads(await ws.receive_text())
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue
            if isinstance(data, dict) and data.get("action") == "set_timeframe":
                new = data.get("timeframe")
                if new in TIMEFRAMES:
                    tf = new
                    await send_snapshot()
                else:
                    await ws.send_json({"type": "error", "message": f"Unsupported timeframe {new!r}"})

    reader_task = asyncio.create_task(reader())
    try:
        await send_snapshot()
        await ws.send_json({"type": "status", **engine.status()})
        while True:
            msg = await q.get()
            t = msg.get("type")
            if t == "resync":
                if msg.get("timeframe") in ("*", tf):
                    await send_snapshot()
                continue
            if t in ("candle", "indicator", "history_status") and msg.get("timeframe") != tf:
                continue
            await ws.send_json(msg)
            if reader_task.done():
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
        engine.remove_client(q)


# ------------------------------------------------------------------------------------------ flow API
def _flow(request: Request):
    f = _engine(request).flow
    if f is None:
        raise HTTPException(404, "Flow analysis is disabled (FLOW_ENABLED=false)")
    return f


@router.get("/api/flow/status")
async def flow_status(request: Request):
    return _flow(request).status()


@router.get("/api/flow/state")
async def flow_state(request: Request, tf: str = Query("1m")):
    f = _flow(request)
    if tf not in f.fs.timeframes:
        raise HTTPException(422, f"Flow timeframes: {', '.join(f.fs.timeframes)}")
    return f._brief_state(tf)


@router.get("/api/flow/signals")
async def flow_signals(request: Request, tf: str | None = None, since: int | None = None,
                       limit: int = Query(300, ge=1, le=3000), full: bool = False):
    f = _flow(request)
    rows = [d for d in f.signals if (tf is None or d["tf"] == tf) and (since is None or d["time"] >= since)][-limit:]
    return {"mode": f.mode, "signals": rows if full else [__import__("app.flow.engine", fromlist=["brief"]).brief(d) for d in rows]}


@router.get("/api/flow/signal/{sid}")
async def flow_signal(request: Request, sid: str):
    d = _flow(request).signal_detail(sid)
    if d is None:
        raise HTTPException(404, "Unknown signal id")
    return d


@router.get("/api/flow/validation")
async def flow_validation_report(request: Request, group_by: str = Query("type"), mode: str | None = None):
    f = _flow(request)
    if group_by not in ("type", "tf", "regime"):
        raise HTTPException(422, "group_by must be type, tf or regime")
    mode = mode or f.mode
    await asyncio.to_thread(f.flush_store)
    rows = await asyncio.to_thread(f.store.signal_outcome_rows, mode)
    counts = await asyncio.to_thread(f.store.signal_counts, mode)
    rep = flow_validation.summarize(rows, counts, f.fs, group_by)
    rep["mode"] = mode
    rep["outcome_rows"] = len(rows)
    rep["warning"] = ("MOCK data: statistics are synthetic and meaningless." if mode == "mock" else
                      "Small samples and in-sample figures must not be read as expected performance. Use walk-forward for out-of-sample results.")
    return rep


@router.get("/api/flow/validation/walkforward")
async def flow_walkforward(request: Request, type: str = Query(...), tf: str = Query("1m"), horizon: int = Query(5),
                           mode: str | None = None):
    f = _flow(request)
    if type not in SIGNAL_TYPES:
        raise HTTPException(422, "Unknown signal type")
    mode = mode or f.mode
    await asyncio.to_thread(f.flush_store)
    rows = await asyncio.to_thread(f.store.signal_outcome_rows, mode)
    rep = flow_validation.walk_forward(rows, f.fs, sig_type=type, tf=tf, horizon=horizon)
    rep["mode"] = mode
    return rep
