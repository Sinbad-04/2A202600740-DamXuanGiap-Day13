"""Build a self-contained 6-panel observability dashboard from data/logs.jsonl.

Reads structured logs produced by the app, computes the Layer-2 metrics defined
in docs/dashboard-spec.md, overlays SLO thresholds from config/slo.yaml, and
renders a single static HTML file (Chart.js via CDN) with auto-refresh.

Usage:
    python scripts/build_dashboard.py
    # open docs/dashboard.html in a browser
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

LOG_PATH = Path("data/logs.jsonl")
SLO_PATH = Path("config/slo.yaml")
OUT_PATH = Path("docs/dashboard.html")


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    items = sorted(values)
    idx = max(0, min(len(items) - 1, round((p / 100) * len(items) + 0.5) - 1))
    return float(items[idx])


def load_records() -> list[dict]:
    if not LOG_PATH.exists():
        raise SystemExit(f"{LOG_PATH} not found. Run the app + load_test first.")
    recs = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return [r for r in recs if r.get("service") == "api"]


def bucket(ts: str) -> str:
    # ISO ts -> per-second bucket label "HH:MM:SS"
    return ts[11:19] if len(ts) >= 19 else ts


def build_data(records: list[dict]) -> dict:
    received = [r for r in records if r.get("event") == "request_received"]
    success = [r for r in records if r.get("event") == "response_sent"]
    failed = [r for r in records if r.get("event") == "request_failed"]

    latency = [r["latency_ms"] for r in success if r.get("latency_ms") is not None]
    cost = [r.get("cost_usd", 0.0) for r in success]
    tokens_in = [r.get("tokens_in", 0) for r in success]
    tokens_out = [r.get("tokens_out", 0) for r in success]
    quality = [r.get("quality_score") for r in success if r.get("quality_score") is not None]

    # cumulative cost over request sequence
    cum, running = [], 0.0
    for c in cost:
        running += c
        cum.append(round(running, 6))

    # traffic + error rate per time bucket
    traffic = Counter(bucket(r["ts"]) for r in received if r.get("ts"))
    errors = Counter(bucket(r["ts"]) for r in failed if r.get("ts"))
    buckets = sorted(set(traffic) | set(errors))
    traffic_series = [traffic.get(b, 0) for b in buckets]
    error_rate_series = [
        round(100 * errors.get(b, 0) / traffic.get(b, 1), 1) for b in buckets
    ]
    error_breakdown = Counter(r.get("error_type", "unknown") for r in failed)

    total = len(received)
    n_fail = len(failed)
    return {
        "req_index": list(range(1, len(success) + 1)),
        "latency": latency,
        "p50": round(percentile(latency, 50)),
        "p95": round(percentile(latency, 95)),
        "p99": round(percentile(latency, 99)),
        "cum_cost": cum,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "quality": quality,
        "buckets": buckets,
        "traffic": traffic_series,
        "error_rate": error_rate_series,
        "error_breakdown": dict(error_breakdown),
        "total_requests": total,
        "total_failed": n_fail,
        "overall_error_rate": round(100 * n_fail / total, 1) if total else 0.0,
        "total_cost": round(sum(cost), 4),
        "avg_quality": round(sum(quality) / len(quality), 3) if quality else 0.0,
    }


def load_slo() -> dict:
    if not SLO_PATH.exists():
        return {}
    return yaml.safe_load(SLO_PATH.read_text(encoding="utf-8")).get("slis", {})


def render(d: dict, slo: dict) -> str:
    lat_slo = slo.get("latency_p95_ms", {}).get("objective", 3000)
    err_slo = slo.get("error_rate_pct", {}).get("objective", 2)
    cost_slo = slo.get("daily_cost_usd", {}).get("objective", 2.5)
    qual_slo = slo.get("quality_score_avg", {}).get("objective", 0.75)
    payload = json.dumps({**d, "lat_slo": lat_slo, "err_slo": err_slo,
                          "cost_slo": cost_slo, "qual_slo": qual_slo})
    return TEMPLATE.replace("__DATA__", payload)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="20"/>
<title>Day 13 Observability Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body{margin:0;background:#0f1115;color:#e6e6e6;font-family:Segoe UI,Arial,sans-serif}
  header{padding:16px 24px;border-bottom:1px solid #262a33}
  h1{margin:0;font-size:20px}
  .sub{color:#8b93a7;font-size:13px;margin-top:4px}
  .kpis{display:flex;gap:24px;margin-top:10px;flex-wrap:wrap}
  .kpi{background:#171a21;border:1px solid #262a33;border-radius:8px;padding:8px 14px}
  .kpi b{font-size:18px;display:block}
  .kpi span{color:#8b93a7;font-size:12px}
  .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;padding:20px 24px}
  .panel{background:#171a21;border:1px solid #262a33;border-radius:10px;padding:14px}
  .panel h2{margin:0 0 4px;font-size:14px}
  .panel .meta{color:#8b93a7;font-size:12px;margin-bottom:8px}
  canvas{max-height:240px}
</style>
</head>
<body>
<header>
  <h1>Day 13 — Observability Dashboard</h1>
  <div class="sub">Auto-refresh 20s · source: data/logs.jsonl · dashed line = SLO threshold</div>
  <div class="kpis">
    <div class="kpi"><b id="k_req"></b><span>Total requests</span></div>
    <div class="kpi"><b id="k_err"></b><span>Error rate</span></div>
    <div class="kpi"><b id="k_cost"></b><span>Total cost (USD)</span></div>
    <div class="kpi"><b id="k_qual"></b><span>Avg quality</span></div>
  </div>
</header>
<div class="grid">
  <div class="panel"><h2>1. Latency per request (ms)</h2><div class="meta" id="m_lat"></div><canvas id="c_lat"></canvas></div>
  <div class="panel"><h2>2. Traffic (requests / sec)</h2><div class="meta">request count per second bucket</div><canvas id="c_traf"></canvas></div>
  <div class="panel"><h2>3. Error rate (%)</h2><div class="meta" id="m_err"></div><canvas id="c_err"></canvas></div>
  <div class="panel"><h2>4. Cost over time (USD, cumulative)</h2><div class="meta" id="m_cost"></div><canvas id="c_cost"></canvas></div>
  <div class="panel"><h2>5. Tokens in / out per request</h2><div class="meta">unit: tokens</div><canvas id="c_tok"></canvas></div>
  <div class="panel"><h2>6. Quality score per request</h2><div class="meta" id="m_qual"></div><canvas id="c_qual"></canvas></div>
</div>
<script>
const D = __DATA__;
const GRID = {color:'#262a33'}, TICK = {color:'#8b93a7'};
const sloLine = (val,n)=>({label:'SLO',data:Array(n).fill(val),borderColor:'#e0567a',borderDash:[6,4],pointRadius:0,borderWidth:1.5});
const axes = (yt)=>({scales:{x:{grid:GRID,ticks:TICK},y:{grid:GRID,ticks:TICK,title:{display:true,text:yt,color:'#8b93a7'}}},plugins:{legend:{labels:{color:'#e6e6e6'}}}});

document.getElementById('k_req').textContent = D.total_requests;
document.getElementById('k_err').textContent = D.overall_error_rate + '%';
document.getElementById('k_cost').textContent = '$' + D.total_cost;
document.getElementById('k_qual').textContent = D.avg_quality;
document.getElementById('m_lat').textContent = `P50 ${D.p50}ms · P95 ${D.p95}ms · P99 ${D.p99}ms · SLO ${D.lat_slo}ms`;
document.getElementById('m_err').textContent = `breakdown: ${JSON.stringify(D.error_breakdown)} · SLO ${D.err_slo}%`;
document.getElementById('m_cost').textContent = `total $${D.total_cost} · daily budget SLO $${D.cost_slo}`;
document.getElementById('m_qual').textContent = `avg ${D.avg_quality} · SLO ${D.qual_slo}`;

new Chart(c_lat,{type:'line',data:{labels:D.req_index,datasets:[
  {label:'latency ms',data:D.latency,borderColor:'#4f9dff',backgroundColor:'rgba(79,157,255,.15)',fill:true,tension:.25,pointRadius:2},
  sloLine(D.lat_slo,D.latency.length)]},options:axes('ms')});

new Chart(c_traf,{type:'bar',data:{labels:D.buckets,datasets:[
  {label:'requests',data:D.traffic,backgroundColor:'#3fb98a'}]},options:axes('req/s')});

new Chart(c_err,{type:'line',data:{labels:D.buckets,datasets:[
  {label:'error %',data:D.error_rate,borderColor:'#e0567a',backgroundColor:'rgba(224,86,122,.15)',fill:true,stepped:true,pointRadius:2},
  sloLine(D.err_slo,D.buckets.length)]},options:axes('%')});

new Chart(c_cost,{type:'line',data:{labels:D.req_index,datasets:[
  {label:'cumulative cost',data:D.cum_cost,borderColor:'#f0a93b',backgroundColor:'rgba(240,169,59,.15)',fill:true,tension:.25,pointRadius:2}]},options:axes('USD')});

new Chart(c_tok,{type:'line',data:{labels:D.req_index,datasets:[
  {label:'tokens in',data:D.tokens_in,borderColor:'#6ec1e4',pointRadius:2,tension:.25},
  {label:'tokens out',data:D.tokens_out,borderColor:'#c08bf0',pointRadius:2,tension:.25}]},options:axes('tokens')});

new Chart(c_qual,{type:'line',data:{labels:D.req_index,datasets:[
  {label:'quality',data:D.quality,borderColor:'#3fb98a',backgroundColor:'rgba(63,185,138,.15)',fill:true,tension:.25,pointRadius:2},
  sloLine(D.qual_slo,D.quality.length)]},options:{...axes('score (0-1)'),scales:{...axes('score (0-1)').scales,y:{...axes('score').scales.y,min:0,max:1}}}});
</script>
</body>
</html>
"""


def main() -> None:
    records = load_records()
    data = build_data(records)
    slo = load_slo()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(data, slo), encoding="utf-8")
    print(f"Dashboard written to {OUT_PATH}")
    print(f"  requests={data['total_requests']} failed={data['total_failed']} "
          f"error_rate={data['overall_error_rate']}% cost=${data['total_cost']} "
          f"P95={data['p95']}ms avg_quality={data['avg_quality']}")


if __name__ == "__main__":
    main()
