"""KNEMON dashboard and read-side usage ledger analytics."""

from __future__ import annotations

import inspect
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.extra_guards import require_extra
from mnemos.api.persistence_helpers import backend_or_503

router = APIRouter(prefix="/v1/knemon", tags=["knemon"], dependencies=[require_extra("knemon", label="KNEMON")])

_CACHE_TTL_SECONDS = 30  # 30s incremental refresh per task
_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}

_CAPABILITY_MAP = {
    "code_generation": ["coding"],
    "reasoning": ["reasoning", "logic"],
    "architecture_design": ["reasoning"],
    "summarization": ["reasoning"],
    "web_search": ["online", "search"],
}


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


def _conn_from_tx(tx: Any) -> Any:
    return getattr(tx, "conn", tx)


async def _materialize(value: Any) -> Any:
    read = getattr(value, "read", None)
    if callable(read):
        value = await _call(read)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


async def _rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    backend = backend_or_503()
    async with backend.transactional() as tx:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(cursor.execute, sql, params or {})
            fetched = await _call(cursor.fetchall)
            names = [col[0].lower() for col in cursor.description]
            out: list[dict[str, Any]] = []
            for row in fetched or []:
                item: dict[str, Any] = {}
                for name, value in zip(names, row):
                    item[name] = _jsonable(await _materialize(value))
                out.append(item)
            return out
        finally:
            await _call(cursor.close)


async def _one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = await _rows(sql, params)
    return rows[0] if rows else {}


async def _cached(key: tuple[Any, ...], loader):
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    value = await loader()
    _CACHE[key] = (now, value)
    return value


def _avg_cost(row: dict[str, Any]) -> float | None:
    input_cost = row.get("input_cost_per_mtok")
    output_cost = row.get("output_cost_per_mtok")
    if input_cost is None or output_cost is None:
        return None
    return (float(input_cost) + float(output_cost)) / 2.0


def _capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    if isinstance(parsed, dict):
        return [str(k) for k, v in parsed.items() if v]
    return []


def _score(row: dict[str, Any], required_caps: list[str]) -> float:
    weight = float(row.get("graeae_weight") or 0)
    arena = float(row.get("arena_score") or 0)
    capability_bonus = 0.05 if all(cap in _capabilities(row.get("capabilities")) for cap in required_caps) else 0.0
    return weight + (arena / 10000.0) + capability_bonus


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: UserContext = Depends(require_root)) -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@router.get("/summary")
async def summary(_: UserContext = Depends(require_root)):
    async def load():
        return await _one(
            """
            SELECT COUNT(*) AS total_rows,
                   NVL(SUM(est_cost_usd), 0) AS total_cost_usd,
                   NVL(SUM(tokens_in), 0) AS total_tokens_in,
                   NVL(SUM(tokens_out), 0) AS total_tokens_out,
                   COUNT(DISTINCT provider || '/' || model) AS distinct_models,
                   NVL(SUM(CASE WHEN ts >= SYSTIMESTAMP - INTERVAL '1' DAY THEN 1 ELSE 0 END), 0) AS last_24h_rows
            FROM usage_ledger
            """
        )

    return await _cached(("summary",), load)


@router.get("/by_provider")
async def by_provider(_: UserContext = Depends(require_root)):
    async def load():
        return await _rows(
            """
            SELECT provider,
                   COUNT(*) AS row_count,
                   NVL(SUM(est_cost_usd), 0) AS cost_usd,
                   NVL(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens,
                   ROUND(AVG(latency_ms), 2) AS avg_latency_ms
            FROM usage_ledger
            GROUP BY provider
            ORDER BY cost_usd DESC, row_count DESC
            """
        )

    return await _cached(("by_provider",), load)


@router.get("/by_model")
async def by_model(limit: int = Query(20, ge=1, le=200), _: UserContext = Depends(require_root)):
    async def load():
        return await _rows(
            """
            SELECT * FROM (
                SELECT provider,
                       model,
                       COUNT(*) AS row_count,
                       NVL(SUM(est_cost_usd), 0) AS cost_usd,
                       ROUND(AVG(tokens_in), 2) AS avg_tokens_in,
                       ROUND(AVG(tokens_out), 2) AS avg_tokens_out
                FROM usage_ledger
                GROUP BY provider, model
                ORDER BY cost_usd DESC, row_count DESC
            )
            WHERE ROWNUM <= :limit
            """,
            {"limit": limit},
        )

    return await _cached(("by_model", limit), load)


@router.get("/by_caller")
async def by_caller(_: UserContext = Depends(require_root)):
    async def load():
        return await _rows(
            """
            SELECT caller_subsystem,
                   COUNT(*) AS row_count,
                   NVL(SUM(est_cost_usd), 0) AS cost_usd
            FROM usage_ledger
            GROUP BY caller_subsystem
            ORDER BY cost_usd DESC, row_count DESC
            """
        )

    return await _cached(("by_caller",), load)


@router.get("/by_task_kind")
async def by_task_kind(_: UserContext = Depends(require_root)):
    async def load():
        return await _rows(
            """
            SELECT task_kind,
                   COUNT(*) AS row_count,
                   NVL(SUM(est_cost_usd), 0) AS cost_usd
            FROM usage_ledger
            GROUP BY task_kind
            ORDER BY cost_usd DESC, row_count DESC
            """
        )

    return await _cached(("by_task_kind",), load)


@router.get("/timeline")
async def timeline(bucket: str = Query("1h", pattern="^(1h|1d)$"), _: UserContext = Depends(require_root)):
    fmt = "'HH24'" if bucket == "1h" else "'DD'"

    async def load():
        return await _rows(
            f"""
            SELECT TO_CHAR(TRUNC(CAST(SYS_EXTRACT_UTC(ts) AS TIMESTAMP), {fmt}),
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS ts_bucket,
                   COUNT(*) AS row_count,
                   NVL(SUM(est_cost_usd), 0) AS cost_usd
            FROM usage_ledger
            WHERE ts >= SYSTIMESTAMP - INTERVAL '7' DAY
            GROUP BY TRUNC(CAST(SYS_EXTRACT_UTC(ts) AS TIMESTAMP), {fmt})
            ORDER BY TRUNC(CAST(SYS_EXTRACT_UTC(ts) AS TIMESTAMP), {fmt})
            """
        )

    return await _cached(("timeline", bucket), load)


@router.get("/recent")
async def recent(limit: int = Query(50, ge=1, le=500), _: UserContext = Depends(require_root)):
    async def load():
        return await _rows(
            """
            SELECT provider,
                   model,
                   tokens_in + tokens_out + tokens_reasoning AS tokens,
                   est_cost_usd,
                   outcome,
                   ts
            FROM (
                SELECT provider, model, tokens_in, tokens_out, tokens_reasoning,
                       est_cost_usd, outcome, ts
                FROM usage_ledger
                ORDER BY ts DESC
            )
            WHERE ROWNUM <= :limit
            """,
            {"limit": limit},
        )

    return await _cached(("recent", limit), load)


@router.get("/recommendations")
async def recommendations(task_type: str | None = None, _: UserContext = Depends(require_root)):
    async def load():
        known_task_rows = await _rows(
            """
            SELECT DISTINCT task_kind AS task_type
            FROM usage_ledger
            WHERE task_kind IS NOT NULL
            ORDER BY task_kind
            """
        )
        known_task_types = sorted({*list(_CAPABILITY_MAP), *(r["task_type"] for r in known_task_rows)})
        if not task_type:
            return {"task_types": known_task_types}

        required_caps = _CAPABILITY_MAP.get(task_type, ["reasoning"])
        recommended = None
        recommend_error = None
        backend = backend_or_503()
        try:
            async with backend.transactional() as tx:
                model, returned_caps = await backend.consultations_audit.fetch_recommended_model(
                    tx,
                    task_type,
                    10.0,
                    0.80,
                )
                required_caps = returned_caps or required_caps
                recommended = model
        except Exception as exc:
            recommend_error = str(exc)

        registry_rows = await _rows(
            """
            SELECT provider, model_id, display_name, input_cost_per_mtok,
                   output_cost_per_mtok, capabilities, arena_score, arena_rank,
                   graeae_weight, context_window
            FROM model_registry
            WHERE available = 1 AND NVL(deprecated, 0) = 0
            """
        )
        candidates = []
        for row in registry_rows:
            caps = _capabilities(row.get("capabilities"))
            capability_match = all(cap in caps for cap in required_caps)
            candidates.append(
                {
                    "provider": row.get("provider"),
                    "model_id": row.get("model_id"),
                    "display_name": row.get("display_name"),
                    "score": round(_score(row, required_caps), 6),
                    "quality_score": row.get("graeae_weight"),
                    "arena_score": row.get("arena_score"),
                    "arena_rank": row.get("arena_rank"),
                    "cost_per_mtok": _avg_cost(row),
                    "context_window": row.get("context_window"),
                    "capability_match": capability_match,
                    "capabilities": caps,
                }
            )
        candidates.sort(key=lambda c: (c["score"], -(c["cost_per_mtok"] or 999999.0)), reverse=True)
        return {
            "task_type": task_type,
            "known_task_types": known_task_types,
            "required_capabilities": required_caps,
            "recommended": recommended,
            "recommend_error": recommend_error,
            "top_candidates": candidates[:5],
        }

    return await _cached(("recommendations", task_type), load)


_DASHBOARD_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KNEMON Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101419;
      --panel: #171d24;
      --panel-2: #1d2630;
      --line: #303b47;
      --text: #edf2f7;
      --muted: #9aa7b4;
      --accent: #4fb3a3;
      --warn: #d9b45f;
      --bad: #df746d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      padding: 22px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: #0d1116;
    }}
    h1 {{ margin: 0; font-size: 24px; font-weight: 700; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; font-weight: 650; letter-spacing: 0; }}
    .sub {{ margin-top: 4px; color: var(--muted); font-size: 13px; }}
    .auth {{
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: min(480px, 100%);
    }}
    input, select, button {{
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      min-height: 34px;
      border-radius: 6px;
      padding: 7px 10px;
      font: inherit;
    }}
    input {{ width: 100%; }}
    button {{ cursor: pointer; background: var(--panel-2); }}
    main {{ padding: 22px 28px 32px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{ padding: 14px; min-height: 82px; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ margin-top: 8px; font-size: 22px; font-weight: 700; overflow-wrap: anywhere; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    section {{ padding: 14px; min-width: 0; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{
      padding: 8px 7px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
    tr:last-child td {{ border-bottom: 0; }}
    .full {{ grid-column: 1 / -1; }}
    .right {{ text-align: right; }}
    .ok {{ color: var(--accent); }}
    .err, .timeout {{ color: var(--bad); }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }}
    .status {{ color: var(--muted); font-size: 12px; min-height: 18px; margin: 8px 0 0; }}
    .gauges {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .gauge {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #131922; }}
    .gaugeTop {{ display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }}
    .bar {{ margin-top: 8px; height: 8px; border-radius: 999px; background: #27313c; overflow: hidden; }}
    .fill {{ height: 100%; background: var(--accent); }}
    .fill.warn {{ background: var(--warn); }}
    .fill.bad {{ background: var(--bad); }}
    @media (max-width: 1000px) {{
      header {{ align-items: stretch; flex-direction: column; }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .grid {{ grid-template-columns: 1fr; }}
      .gauges {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>KNEMON Dashboard</h1>
      <div class="sub">MNEMOS usage ledger and model-routing visibility</div>
    </div>
    <div class="auth">
      <input id="token" type="password" autocomplete="off" placeholder="Bearer token for JSON refreshes">
      <button id="saveToken">Save</button>
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <div class="summary" id="summary"></div>
    <div class="grid">
      <section class="full"><h2>Sub-Plans</h2><div id="subPlans"></div></section>
      <section class="full"><h2>Utilization</h2><div id="utilization"></div></section>
      <section><h2>Cost Split</h2><div id="costSplit"></div></section>
      <section><h2>By Provider</h2><div id="byProvider"></div></section>
      <section><h2>By Model</h2><div id="byModel"></div></section>
      <section><h2>By Caller</h2><div id="byCaller"></div></section>
      <section><h2>By Task Kind</h2><div id="byTask"></div></section>
      <section class="full"><h2>Last 7 Days</h2><div id="timeline"></div></section>
      <section class="full"><h2>Recent Usage</h2><div id="recent"></div></section>
      <section class="full">
        <h2>Recommendations</h2>
        <div class="toolbar">
          <select id="taskType"></select>
          <button id="loadRecommendation">Load</button>
        </div>
        <div id="recommendations"></div>
      </section>
    </div>
    <div class="status" id="status"></div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const fmtMoney = (v) => "$" + Number(v || 0).toLocaleString(undefined, {{minimumFractionDigits: 4, maximumFractionDigits: 4}});
    const fmtNum = (v) => Number(v || 0).toLocaleString();
    const tokenInput = $("token");
    tokenInput.value = localStorage.getItem("knemonToken") || "";

    function headers() {{
      const token = tokenInput.value.trim();
      return token ? {{Authorization: "Bearer " + token}} : {{}};
    }}
    async function get(path) {{
      const res = await fetch(path, {{headers: headers()}});
      if (!res.ok) throw new Error(path + " -> HTTP " + res.status);
      return await res.json();
    }}
    function table(cols, rows) {{
      const head = cols.map(c => `<th class="${{c.right ? "right" : ""}}">${{c.label}}</th>`).join("");
      const body = rows.map(r => `<tr>${{cols.map(c => `<td class="${{c.right ? "right" : ""}} ${{c.className ? c.className(r) : ""}}">${{c.render ? c.render(r) : (r[c.key] ?? "")}}</td>`).join("")}}</tr>`).join("");
      return `<table><thead><tr>${{head}}</tr></thead><tbody>${{body || `<tr><td colspan="${{cols.length}}">No rows</td></tr>`}}</tbody></table>`;
    }}
    function gauge(rows) {{
      const items = (rows || []).filter(r => r.auth_method === "subscription" || r.msg_cap).slice(0, 12);
      return `<div class="gauges">${{items.map(r => {{
        const pct = r.utilization_pct == null ? 0 : Math.min(100, Number(r.utilization_pct));
        const cls = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "";
        const label = r.utilization_pct == null ? "uncapped" : `${{Number(r.utilization_pct).toFixed(1)}}%`;
        return `<div class="gauge">
          <div class="gaugeTop"><strong>${{r.provider}}/${{r.plan_name}}</strong><span>${{label}}</span></div>
          <div class="bar"><div class="fill ${{cls}}" style="width:${{pct}}%"></div></div>
          <div class="sub">${{fmtNum(r.requests_used)}} / ${{r.msg_cap == null ? "unmetered" : fmtNum(r.msg_cap)}} requests</div>
        </div>`;
      }}).join("") || `<div class="sub">No subscription utilization rows</div>`}}</div>`;
    }}
    async function loadSummary() {{
      const s = await get("/v1/knemon/summary");
      $("summary").innerHTML = [
        ["Rows", fmtNum(s.total_rows)],
        ["Cost", fmtMoney(s.total_cost_usd)],
        ["Tokens In", fmtNum(s.total_tokens_in)],
        ["Tokens Out", fmtNum(s.total_tokens_out)],
        ["Models", fmtNum(s.distinct_models)],
        ["Last 24h", fmtNum(s.last_24h_rows)]
      ].map(([label, value]) => `<div class="metric"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`).join("");
    }}
    async function loadTables() {{
      const [providers, models, callers, tasks, timeline, recent, utilization, costSplit] = await Promise.all([
        get("/v1/knemon/by_provider"),
        get("/v1/knemon/by_model?limit=20"),
        get("/v1/knemon/by_caller"),
        get("/v1/knemon/by_task_kind"),
        get("/v1/knemon/timeline?bucket=1h"),
        get("/v1/knemon/recent?limit=50"),
        get("/v1/knemon/utilization?window=current"),
        get("/v1/knemon/cost_split?period=monthly")
      ]);
      $("subPlans").innerHTML = table([
        {{key:"provider", label:"Provider"}}, {{key:"plan_name", label:"Plan"}},
        {{key:"auth_method", label:"Auth"}}, {{key:"msg_cap", label:"Msg Cap", right:true, render:r=>r.msg_cap == null ? "unmetered" : fmtNum(r.msg_cap)}},
        {{key:"window_end", label:"Window End"}}
      ], utilization);
      $("utilization").innerHTML = gauge(utilization);
      $("costSplit").innerHTML = table([
        {{key:"cost_bucket", label:"Bucket"}}, {{key:"requests", label:"Requests", right:true, render:r=>fmtNum(r.requests)}},
        {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}}
      ], costSplit);
      $("byProvider").innerHTML = table([
        {{key:"provider", label:"Provider"}}, {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}},
        {{key:"tokens", label:"Tokens", right:true, render:r=>fmtNum(r.tokens)}},
        {{key:"avg_latency_ms", label:"Latency", right:true, render:r=>`${{Number(r.avg_latency_ms || 0).toFixed(1)}} ms`}}
      ], providers);
      $("byModel").innerHTML = table([
        {{key:"provider", label:"Provider"}}, {{key:"model", label:"Model"}},
        {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}}
      ], models);
      $("byCaller").innerHTML = table([
        {{key:"caller_subsystem", label:"Caller"}}, {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}}
      ], callers);
      $("byTask").innerHTML = table([
        {{key:"task_kind", label:"Task"}}, {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}}
      ], tasks);
      $("timeline").innerHTML = table([
        {{key:"ts_bucket", label:"UTC Bucket"}}, {{key:"row_count", label:"Rows", right:true, render:r=>fmtNum(r.row_count)}},
        {{key:"cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.cost_usd)}}
      ], timeline);
      $("recent").innerHTML = table([
        {{key:"ts", label:"UTC Time"}}, {{key:"provider", label:"Provider"}}, {{key:"model", label:"Model"}},
        {{key:"tokens", label:"Tokens", right:true, render:r=>fmtNum(r.tokens)}},
        {{key:"est_cost_usd", label:"Cost", right:true, render:r=>fmtMoney(r.est_cost_usd)}},
        {{key:"outcome", label:"Outcome", className:r=>r.outcome}}
      ], recent);
    }}
    async function loadTaskTypes() {{
      const data = await get("/v1/knemon/recommendations");
      $("taskType").innerHTML = (data.task_types || []).map(t => `<option value="${{t}}">${{t}}</option>`).join("");
    }}
    async function loadRecommendation() {{
      const task = $("taskType").value;
      const data = await get("/v1/knemon/recommendations?task_type=" + encodeURIComponent(task));
      const rec = data.recommended || {{}};
      const rows = data.top_candidates || [];
      $("recommendations").innerHTML =
        `<div class="sub">fetch_recommended_model: ${{rec.provider || "none"}}/${{rec.model_id || ""}} | required: ${{(data.required_capabilities || []).join(", ")}}${{data.recommend_error ? " | error: " + data.recommend_error : ""}}</div>` +
        table([
          {{key:"provider", label:"Provider"}}, {{key:"model_id", label:"Model"}},
          {{key:"score", label:"Score", right:true}}, {{key:"cost_per_mtok", label:"$/MTok", right:true, render:r=>r.cost_per_mtok == null ? "unknown" : fmtMoney(r.cost_per_mtok)}},
          {{key:"capability_match", label:"Caps", render:r=>r.capability_match ? "match" : "miss"}}
        ], rows);
    }}
    async function refresh() {{
      $("status").textContent = "Loading...";
      try {{
        await loadSummary();
        await loadTables();
        await loadTaskTypes();
        if ($("taskType").value) await loadRecommendation();
        $("status").textContent = "Updated " + new Date().toISOString();
      }} catch (err) {{
        $("status").textContent = err.message;
      }}
    }}
    $("saveToken").onclick = () => {{ localStorage.setItem("knemonToken", tokenInput.value.trim()); refresh(); }};
    $("refresh").onclick = refresh;
    $("loadRecommendation").onclick = loadRecommendation;
    refresh();
  </script>
</body>
</html>
"""
