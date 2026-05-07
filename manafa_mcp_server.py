#!/usr/bin/env python3
#manafa_mcp_server.py
#exposes on-device-manafa profiling data as MCP tools so any mcp-compatible
#llm client can query traces interactively instead of running a one-shot script
#compatible clients: cursor, zed, claude, continue, claude desktop, cline
#
#usage: venv/bin/python manafa_mcp_server.py
#then register this server in your mcp client config— see README for the json snippet

import os
import glob
import json
import re
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from perfetto.trace_processor import TraceProcessor

RESULTS_DIR  = Path(__file__).parent / "results"
PERFETTO_DIR = RESULTS_DIR / "perfetto"
BSTATS_DIR   = RESULTS_DIR / "batterystats"
ANALYSIS_DIR = RESULTS_DIR / "analysis"

#run IDs for the n=5 statistical benchmark runs
ENERGY_RUN_IDS = [
    "1777869093", "1777869177", "1777869285", "1777869363", "1777869438"
]
METHOD_RUN_IDS = [
    "1777869802", "1777869905", "1777869984", "1777870075", "1777870166"
]

POWER_RAILS = {
    "CPU Big":    "power.rails.cpu.big",
    "CPU Little": "power.rails.cpu.little",
    "CPU Mid":    "power.rails.cpu.mid",
    "Display":    "power.rails.display",
    "GPU":        "power.rails.gpu",
    "Modem":      "power.rails.modem",
    "Memory":     "power.rails.memory.interface",
}

EMULATOR_RESULTS = {
    "legacy": "real — cpu freq + scheduling captured correctly",
    "energy": "dummy — power rails all 0 uW, no real power hardware in emulator",
    "memory": "flat — meminfo counters visible but report fixed values",
    "both":   "dummy — neither power rails nor memory stats are real",
    "method": "real — cpu scheduling + callstack sampling both active",
}

mcp = FastMCP("on-device-manafa")


def _find_trace(run_id: str) -> str:
    matches = glob.glob(str(PERFETTO_DIR / f"trace-{run_id}-*.perfetto-trace"))
    if not matches:
        raise FileNotFoundError(f"no trace found for run {run_id}")
    return matches[0]

def _read_drain(run_id: str) -> float:
    matches = glob.glob(str(BSTATS_DIR / f"bstats-drain-{run_id}-*.log"))
    if not matches:
        return 0.0
    with open(matches[0]) as f:
        for line in f:
            m = re.match(r"battery_drain=(\d+)%", line.strip())
            if m:
                return float(m.group(1))
    return 0.0


@mcp.tool()
def list_runs() -> str:
    """list all available profiling runs with their mode, file size, and battery drain"""
    runs = []
    for run_id in ENERGY_RUN_IDS:
        path = _find_trace(run_id)
        runs.append({
            "run_id":    run_id,
            "mode":      "energy",
            "app":       "YouTube",
            "size_mb":   round(os.path.getsize(path) / 1e6, 2),
            "drain_pct": _read_drain(run_id),
        })
    for run_id in METHOD_RUN_IDS:
        path = _find_trace(run_id)
        runs.append({
            "run_id":    run_id,
            "mode":      "method",
            "app":       "Chrome",
            "size_mb":   round(os.path.getsize(path) / 1e6, 2),
            "drain_pct": _read_drain(run_id),
        })
    return json.dumps(runs, indent=2)


@mcp.tool()
def get_power_rails(run_id: str = "") -> str:
    """get hardware power rail readings (mW) from an energy-mode trace.
    if run_id is empty, returns the mean across all 5 youtube energy runs.
    power rails are direct hardware measurements from the pixel 9 pro xl's
    android.power data source — not software estimates."""
    ids = [run_id] if run_id else ENERGY_RUN_IDS
    all_rails: list[dict] = []

    for rid in ids:
        tp = TraceProcessor(trace=_find_trace(rid))
        result = tp.query("""
            SELECT ct.name as rail, AVG(c.value)/1000.0 as avg_mw
            FROM __intrinsic_counter c
            JOIN counter_track ct ON c.track_id = ct.id
            WHERE ct.name LIKE 'power.rails.%'
            GROUP BY ct.name
        """)
        rails = {row.rail: round(row.avg_mw, 1) for row in result}
        tp.close()
        all_rails.append(rails)

    if len(all_rails) == 1:
        return json.dumps(all_rails[0], indent=2)

    #average across runs
    all_keys = set(k for r in all_rails for k in r)
    mean_rails = {
        k: round(sum(r.get(k, 0) for r in all_rails) / len(all_rails), 1)
        for k in sorted(all_keys)
    }
    return json.dumps({"mean_across_5_runs": mean_rails, "n": len(all_rails)}, indent=2)


@mcp.tool()
def get_cpu_threads(run_id: str = "") -> str:
    """get cpu thread utilization (seconds) from a method-tracing trace.
    if run_id is empty, returns means across all 5 chrome method runs.
    uses kernel sched_slice data — shows exactly how long each thread was on cpu."""
    ids = [run_id] if run_id else METHOD_RUN_IDS
    all_runs: list[list[dict]] = []

    for rid in ids:
        tp = TraceProcessor(trace=_find_trace(rid))
        result = tp.query("""
            SELECT t.name as thread_name, p.name as process_name,
                   SUM(ss.dur)/1e9 as cpu_sec
            FROM __intrinsic_sched_slice ss
            JOIN __intrinsic_thread t ON ss.utid = t.id
            JOIN __intrinsic_process p ON t.upid = p.id
            WHERE p.name LIKE '%chrome%' OR p.name LIKE '%chromium%'
            GROUP BY t.id, t.name, p.name
            ORDER BY cpu_sec DESC
            LIMIT 10
        """)
        rows = [{"thread": row.thread_name, "cpu_sec": round(row.cpu_sec, 3)}
                for row in result]
        tp.close()
        all_runs.append(rows)

    if len(all_runs) == 1:
        return json.dumps(all_runs[0], indent=2)

    #average cpu_sec per thread across runs
    from collections import defaultdict
    thread_vals: dict[str, list[float]] = defaultdict(list)
    for run in all_runs:
        for t in run:
            thread_vals[t["thread"]].append(t["cpu_sec"])
    means = {k: round(sum(v)/len(v), 3) for k, v in thread_vals.items()}
    sorted_means = dict(sorted(means.items(), key=lambda x: x[1], reverse=True))
    return json.dumps({"mean_cpu_sec_across_5_runs": sorted_means, "n": len(all_runs)}, indent=2)


@mcp.tool()
def get_thread_energy_joules(run_id: str = "") -> str:
    """compute per-process cpu energy in joules from an energy-mode trace.
    method: total cpu rail power (mW) × trace duration (s) = cpu energy budget (J),
    then split proportionally by each process's fraction of scheduled cpu time.
    if run_id is empty, averages across all 5 youtube energy runs.
    this analysis is not available in e-manafa's offline (-d) mode."""
    ids = [run_id] if run_id else ENERGY_RUN_IDS
    all_results = []

    for rid in ids:
        tp = TraceProcessor(trace=_find_trace(rid))
        #trace duration in seconds — needed to convert avg power (mW) to total energy (J)
        duration_s = list(tp.query(
            'SELECT (end_ts - start_ts)/1e9 as dur FROM _trace_bounds'
        ))[0].dur

        #perfetto stores power rails in nW, dividing by 1e6 gives actual mW
        rail_rows = tp.query("""
            SELECT ct.name, AVG(c.value)/1e6 as avg_mw
            FROM __intrinsic_counter c JOIN counter_track ct ON c.track_id=ct.id
            WHERE ct.name IN (
                'power.rails.cpu.big','power.rails.cpu.little','power.rails.cpu.mid'
            )
            GROUP BY ct.name
        """)
        total_cpu_mw = sum(row.avg_mw for row in rail_rows)
        total_cpu_j  = total_cpu_mw / 1000.0 * duration_s  #mW→W, W×s=J

        #left join because energy traces don't capture fork events so upid is often NULL
        #swapper is the idle kernel task — excluded so fractions reflect active work only
        sched_rows = list(tp.query("""
            SELECT COALESCE(p.name, t.name) as proc, SUM(ss.dur)/1e9 as cpu_s
            FROM __intrinsic_sched_slice ss
            JOIN __intrinsic_thread t ON ss.utid = t.id
            LEFT JOIN __intrinsic_process p ON t.upid = p.id
            WHERE COALESCE(p.name, t.name) IS NOT NULL
              AND COALESCE(p.name, t.name) != ''
              AND COALESCE(p.name, t.name) != 'swapper'
            GROUP BY COALESCE(p.name, t.name)
            ORDER BY cpu_s DESC LIMIT 15
        """))
        tp.close()

        total_sched_s = sum(r.cpu_s for r in sched_rows)
        #each process gets a fraction of the total cpu energy proportional to its cpu time
        per_proc = [
            {
                "process":    r.proc,
                "cpu_sec":    round(r.cpu_s, 3),
                "fraction":   round(r.cpu_s / total_sched_s, 4) if total_sched_s else 0,
                "energy_j":   round((r.cpu_s / total_sched_s) * total_cpu_j, 5) if total_sched_s else 0,
            }
            for r in sched_rows
        ]
        all_results.append({
            "run_id":           rid,
            "duration_s":       round(duration_s, 2),
            "total_cpu_energy_j": round(total_cpu_j, 4),
            "per_process":      per_proc,
        })

    if len(all_results) == 1:
        return json.dumps(all_results[0], indent=2)
    return json.dumps(all_results, indent=2)


@mcp.tool()
def get_hotspot_functions(run_id: str = "") -> str:
    """get resolved linux.perf callstack function frames from a method-tracing trace.
    note: chrome's v8 jit engine doesn't preserve frame pointers so resolution is low (~0.7%).
    the functions that did resolve (art interpreter, kernel scheduler) still indicate
    what the cpu was executing on chrome's behalf."""
    rid = run_id if run_id else METHOD_RUN_IDS[0]
    tp  = TraceProcessor(trace=_find_trace(rid))

    total    = list(tp.query('SELECT COUNT(*) as n FROM __intrinsic_perf_sample'))[0].n
    resolved = list(tp.query(
        'SELECT COUNT(*) as n FROM __intrinsic_perf_sample WHERE callsite_id IS NOT NULL'
    ))[0].n

    result = tp.query("""
        SELECT spf.name as func_name, COUNT(*) as samples
        FROM __intrinsic_perf_sample ps
        JOIN __intrinsic_stack_profile_callsite spc ON ps.callsite_id = spc.id
        JOIN __intrinsic_stack_profile_frame spf ON spc.frame_id = spf.id
        WHERE spf.name IS NOT NULL AND spf.name != ''
        GROUP BY spf.name ORDER BY samples DESC LIMIT 20
    """)
    funcs = [{"function": row.func_name, "samples": row.samples} for row in result]
    tp.close()

    return json.dumps({
        "run_id":           rid,
        "total_samples":    total,
        "resolved_samples": resolved,
        "resolution_pct":   round(resolved / total * 100, 1) if total else 0,
        "top_functions":    funcs,
    }, indent=2)


@mcp.tool()
def get_battery_drain() -> str:
    """get battery drain percentages from all 10 benchmark runs (5 youtube + 5 chrome).
    note: android's batterystats has 1% resolution so 30s runs often show 0%.
    the power rail counter data from get_power_rails() is the primary energy signal."""
    results = {}
    for rid in ENERGY_RUN_IDS:
        results[f"youtube_{rid}"] = _read_drain(rid)
    for rid in METHOD_RUN_IDS:
        results[f"chrome_{rid}"] = _read_drain(rid)
    return json.dumps(results, indent=2)


@mcp.tool()
def get_mode_comparison() -> str:
    """return the validated capability matrix for all 5 profiling modes.
    shows which data sources each mode captures and whether it gives real
    vs dummy data on an android studio emulator vs a real device."""
    modes = {
        "legacy": {
            "data_sources": ["cpu_frequency", "cpu_scheduling"],
            "trace_size_mb_approx": 0.008,
            "real_device": "real",
            "emulator":    EMULATOR_RESULTS["legacy"],
        },
        "energy": {
            "data_sources": ["power_rails", "cpu_scheduling"],
            "trace_size_mb_approx": 6.8,
            "real_device": "real",
            "emulator":    EMULATOR_RESULTS["energy"],
        },
        "memory": {
            "data_sources": ["memory_stats", "cpu_scheduling"],
            "trace_size_mb_approx": 0.012,
            "real_device": "real",
            "emulator":    EMULATOR_RESULTS["memory"],
        },
        "both": {
            "data_sources": ["power_rails", "memory_stats", "cpu_scheduling"],
            "trace_size_mb_approx": 0.066,
            "real_device": "real",
            "emulator":    EMULATOR_RESULTS["both"],
        },
        "method": {
            "data_sources": ["linux_perf_callstack", "memory_stats", "cpu_scheduling"],
            "trace_size_mb_approx": 33.0,
            "real_device": "real",
            "emulator":    EMULATOR_RESULTS["method"],
        },
    }
    return json.dumps(modes, indent=2)


@mcp.tool()
def query_trace(run_id: str, sql: str) -> str:
    """run a custom perfetto sql query against any trace by run_id.
    useful for asking arbitrary questions about the trace data that
    the other tools don't cover. perfetto exposes scheduling, counters,
    processes, threads, slices, and more via sql."""
    tp = TraceProcessor(trace=_find_trace(run_id))
    try:
        rows = [dict(row.__dict__) for row in tp.query(sql)]
        result = rows[:100]  #cap at 100 rows to avoid flooding the context
    except Exception as e:
        result = {"error": str(e)}
    finally:
        tp.close()
    return json.dumps(result, indent=2)


@mcp.tool()
def get_analysis_summary() -> str:
    """return the pre-computed analysis summary from the last run of analyze_agent.py.
    includes power rail stats, cpu thread stats, per-process energy in joules,
    hotspot functions, and the llm-generated hotspot analysis."""
    json_path = ANALYSIS_DIR / "hotspot_analysis.json"
    if not json_path.exists():
        return '{"error": "no analysis found — run analyze_agent.py first"}'
    with open(json_path) as f:
        return f.read()


if __name__ == "__main__":
    mcp.run()
