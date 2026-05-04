#!/usr/bin/env python3
#analyze_agent.py
#reads the perfetto traces and battery drain logs produced by manafa.sh,
#extracts hardware power rail data and cpu thread utilization, generates
#box plots across the n=5 statistical runs, then sends the structured
#summary to an llm to identify and rank energy hotspots

import os
import glob
import json
import re
import matplotlib
matplotlib.use("Agg")  #non-interactive backend so this runs without a display
import matplotlib.pyplot as plt
from perfetto.trace_processor import TraceProcessor

#output directories relative to this script's location
RESULTS_DIR  = os.path.join(os.path.dirname(__file__), "results")
PERFETTO_DIR = os.path.join(RESULTS_DIR, "perfetto")
BSTATS_DIR   = os.path.join(RESULTS_DIR, "batterystats")
OUT_DIR      = os.path.join(RESULTS_DIR, "analysis")
os.makedirs(OUT_DIR, exist_ok=True)

#run IDs are the unix timestamps embedded in each trace filename
#youtube was profiled in energy mode (power rails), chrome in method mode (callstack sampling)
ENERGY_RUN_IDS = [
    "1777869093", "1777869177", "1777869285", "1777869363", "1777869438"
]
METHOD_RUN_IDS = [
    "1777869802", "1777869905", "1777869984", "1777870075", "1777870166"
]

#maps human-readable labels to the perfetto counter track names
#the pixel 9 pro xl exposes these via the android.power data source
POWER_RAILS = {
    "CPU Big":    "power.rails.cpu.big",
    "CPU Little": "power.rails.cpu.little",
    "CPU Mid":    "power.rails.cpu.mid",
    "Display":    "power.rails.display",
    "GPU":        "power.rails.gpu",
    "Modem":      "power.rails.modem",
    "Memory":     "power.rails.memory.interface",
}

#how many chrome threads to include in the box plot and llm summary
TOP_N_THREADS = 8

#github models gives free access to gpt-4o-mini via a personal access token
GITHUB_TOKEN_ENV       = "GITHUB_TOKEN"
GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com"
GITHUB_MODEL           = "gpt-4o-mini"


#trace filenames follow the pattern trace-<run_id>-<device_id>.perfetto-trace
def find_trace(run_id: str) -> str:
    matches = glob.glob(os.path.join(PERFETTO_DIR, f"trace-{run_id}-*.perfetto-trace"))
    if not matches:
        raise FileNotFoundError(f"No trace found for run {run_id}")
    return matches[0]

#drain logs are written by batterystats_service.sh at the end of each run
def find_drain_log(run_id: str) -> str:
    matches = glob.glob(os.path.join(BSTATS_DIR, f"bstats-drain-{run_id}-*.log"))
    return matches[0] if matches else None

def read_drain(path: str) -> float | None:
    #parse the battery_drain=N% line, return None if file is missing
    if path is None:
        return None
    with open(path) as f:
        for line in f:
            m = re.match(r"battery_drain=(\d+)%", line.strip())
            if m:
                return float(m.group(1))
    return None


def extract_power_rails(trace_path: str) -> dict[str, float]:
    #query all power.rails.* counter tracks and average over the trace window
    #values come out in microwatts so we divide by 1000 to get milliwatts
    tp = TraceProcessor(trace=trace_path)
    sql = """
        SELECT ct.name as rail, AVG(c.value) as avg_uw
        FROM __intrinsic_counter c
        JOIN counter_track ct ON c.track_id = ct.id
        WHERE ct.name LIKE 'power.rails.%'
        GROUP BY ct.name
    """
    result = tp.query(sql)
    rails = {row.rail: row.avg_uw / 1000.0 for row in result}
    tp.close()
    return rails


def extract_cpu_threads(trace_path: str) -> list[dict]:
    #sched slices tell us exactly how long each thread was scheduled on cpu
    #we filter to chrome processes and sum duration per thread to get total cpu cost
    tp = TraceProcessor(trace=trace_path)
    sql = """
        SELECT t.name as thread_name, p.name as process_name,
               SUM(ss.dur)/1e9 as cpu_sec
        FROM __intrinsic_sched_slice ss
        JOIN __intrinsic_thread t ON ss.utid = t.id
        JOIN __intrinsic_process p ON t.upid = p.id
        WHERE p.name LIKE '%chrome%' OR p.name LIKE '%chromium%'
        GROUP BY t.id, t.name, p.name
        ORDER BY cpu_sec DESC
        LIMIT 20
    """
    result = tp.query(sql)
    rows = [{"thread": row.thread_name, "process": row.process_name,
             "cpu_sec": round(row.cpu_sec, 3)} for row in result]
    tp.close()
    return rows[:TOP_N_THREADS]


def extract_hotspot_functions(trace_path: str) -> dict:
    #linux.perf callstack unwinding on chrome often fails because v8's jit-compiled
    #javascript doesn't preserve frame pointers, so most samples have no resolved
    #callsite. we extract what did resolve and report the resolution rate so the
    #llm can factor in the confidence level of this data.
    tp = TraceProcessor(trace=trace_path)

    total = list(tp.query('SELECT COUNT(*) as n FROM __intrinsic_perf_sample'))[0].n
    resolved = list(tp.query(
        'SELECT COUNT(*) as n FROM __intrinsic_perf_sample WHERE callsite_id IS NOT NULL'
    ))[0].n

    #top leaf functions across all resolved samples — not filtered to chrome only
    #because most chrome samples have null callsite; kernel/art frames that did
    #resolve still indicate what the cpu was doing on chrome's behalf
    sql = """
        SELECT spf.name as func_name, COUNT(*) as samples
        FROM __intrinsic_perf_sample ps
        JOIN __intrinsic_stack_profile_callsite spc ON ps.callsite_id = spc.id
        JOIN __intrinsic_stack_profile_frame spf ON spc.frame_id = spf.id
        WHERE spf.name IS NOT NULL AND spf.name != ""
        GROUP BY spf.name
        ORDER BY samples DESC
        LIMIT 15
    """
    rows = [{"function": row.func_name, "samples": row.samples}
            for row in tp.query(sql)]
    tp.close()

    return {
        "total_samples": total,
        "resolved_samples": resolved,
        "resolution_rate_pct": round(resolved / total * 100, 1) if total > 0 else 0,
        "top_functions": rows,
    }


def plot_power_rails(rail_data: dict[str, list[float]]):
    #log scale is necessary here because the modem rail dominates by orders of
    #magnitude and makes everything else invisible on a linear axis
    labels = list(rail_data.keys())
    values = [rail_data[k] for k in labels]

    fig, ax = plt.subplots(figsize=(10, 6))
    bp = ax.boxplot(values, patch_artist=True, notch=False)

    colors = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
              "#59a14f", "#edc948", "#b07aa1"]
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.8)

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
    ax.set_yscale("log")
    ax.set_ylabel("Average Power (mW, log scale)")
    ax.set_title("Power Rail Distribution — YouTube Energy Mode (n=5 runs)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout(pad=1.5)

    out = os.path.join(OUT_DIR, "boxplot_power_rails.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_cpu_threads(thread_data: dict[str, list[float]]):
    #each box shows the spread of cpu time for that thread across the 5 runs
    #wider boxes mean more variability in workload between runs
    labels = list(thread_data.keys())
    values = [thread_data[k] for k in labels]

    fig, ax = plt.subplots(figsize=(10, 5))
    bp = ax.boxplot(values, patch_artist=True, notch=False)
    for patch in bp["boxes"]:
        patch.set_facecolor("#4e79a7")
        patch.set_alpha(0.75)

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("CPU Time (seconds)")
    ax.set_title("CPU Time per Thread — Chrome Method Tracing (n=5 runs)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()

    out = os.path.join(OUT_DIR, "boxplot_cpu_threads.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_hotspot_functions(func_summary: dict, thread_summary: dict):
    #combine thread cpu time (reliable, n=5) with resolved function frames (sparse but real)
    #into one horizontal bar chart so the report has a single figure showing both layers
    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(10, 8),
                                          gridspec_kw={"height_ratios": [2, 1]})

    #top panel: mean cpu time per chrome thread, sorted descending
    thread_names = list(thread_summary.keys())
    thread_means  = [thread_summary[n]["mean_sec"] for n in thread_names]
    sorted_pairs  = sorted(zip(thread_means, thread_names), reverse=True)
    t_means, t_names = zip(*sorted_pairs)

    bars = ax_top.barh(t_names[::-1], t_means[::-1], color="#4e79a7", alpha=0.8)
    ax_top.set_xlabel("Mean CPU Time (seconds, n=5 runs)")
    ax_top.set_title("Energy Hotspot Analysis — Chrome Method Tracing")
    ax_top.grid(axis="x", linestyle="--", alpha=0.4)
    #annotate each bar with the exact mean value
    for bar, val in zip(bars, t_means[::-1]):
        ax_top.text(val + 0.05, bar.get_y() + bar.get_height() / 2,
                    f"{val:.2f}s", va="center", fontsize=8)

    #bottom panel: top resolved perf functions by sample count
    #these are sparse (0.7% resolution) but show what the cpu was executing
    funcs = func_summary["top_functions"][:10]
    f_names   = [f["function"][:50] for f in funcs]  # truncate long c++ names
    f_samples = [f["samples"] for f in funcs]

    ax_bot.barh(f_names[::-1], f_samples[::-1], color="#e15759", alpha=0.75)
    ax_bot.set_xlabel(f"Perf Samples (of {func_summary['resolved_samples']} resolved / {func_summary['total_samples']} total)")
    ax_bot.set_title(f"Resolved Callstack Functions ({func_summary['resolution_rate_pct']}% frame resolution — JIT limits unwinding)")
    ax_bot.grid(axis="x", linestyle="--", alpha=0.4)

    plt.tight_layout(pad=2.0)
    out = os.path.join(OUT_DIR, "hotspot_functions.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_battery_drain(energy_drains: list[float], method_drains: list[float]):
    #batterystats only has 1% resolution so 30-second runs rarely show any drain
    #included for completeness but the power rail data is the real energy signal
    fig, ax = plt.subplots(figsize=(6, 5))
    bp = ax.boxplot([energy_drains, method_drains], patch_artist=True)
    for patch, color in zip(bp["boxes"], ["#e15759", "#4e79a7"]):
        patch.set_facecolor(color)
        patch.set_alpha(0.8)
    ax.set_xticks([1, 2])
    ax.set_xticklabels(["YouTube\n(energy mode)", "Chrome\n(method mode)"])
    ax.set_ylabel("Battery Drain (%)")
    ax.set_title("Battery Drain Comparison (n=5 runs each)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()

    out = os.path.join(OUT_DIR, "boxplot_battery_drain.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def llm_hotspot_analysis(rail_summary: dict, thread_summary: dict, drain_summary: dict, func_summary: dict) -> str:
    token = os.environ.get(GITHUB_TOKEN_ENV)
    if not token:
        return "[GITHUB_TOKEN not set — set it to enable LLM analysis]"

    from openai import OpenAI
    #github models exposes gpt-4o-mini through an openai-compatible endpoint
    #so we can use the standard client with a different base url
    client = OpenAI(base_url=GITHUB_MODELS_ENDPOINT, api_key=token)

    #send the full structured data so the llm reasons from actual numbers
    #not generic knowledge about android apps
    prompt = f"""You are an Android performance engineer analyzing on-device profiling data.

## Power Rail Data (YouTube, energy mode, n=5 runs)
Average power draw per hardware rail (mW):
{json.dumps(rail_summary, indent=2)}

## CPU Thread Data (Chrome, method tracing, n=5 runs)
Average CPU time per thread (seconds over 30s trace):
{json.dumps(thread_summary, indent=2)}

## Battery Drain
{json.dumps(drain_summary, indent=2)}

## Function-Level Callstack Data (Chrome, method tracing, n=1 representative run)
linux.perf sampled {func_summary["total_samples"]} times; {func_summary["resolved_samples"]} resolved ({func_summary["resolution_rate_pct"]}%).
Low resolution is expected — Chrome's V8 JIT engine does not preserve frame pointers.
Top resolved leaf functions by sample count:
{json.dumps(func_summary["top_functions"], indent=2)}

Based on all of the above:
1. Identify the top 3 energy hotspots across hardware and software — combine the power rail, thread, and function data to give a complete ranked picture. Be specific about magnitudes.
2. For each hotspot, explain WHY it draws energy (the underlying mechanism — hardware behaviour, thread scheduling, or function-level CPU activity).
3. Suggest one concrete optimization a developer could make to reduce that hotspot.
4. Interpret what the resolved function names (nterp_helper, ExecuteNterpImpl, art::InvokeVirtual, etc.) reveal about Chrome's runtime behaviour, even given the low resolution rate.
5. Note any surprising or unexpected findings.

Keep the response structured with numbered hotspots. Be concise — this is for a capstone report."""

    response = client.chat.completions.create(
        model=GITHUB_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return response.choices[0].message.content


def main():
    print("=" * 60)
    print(" on-device-manafa LLM Energy Hotspot Agent")
    print("=" * 60)

    #step 1: open each of the 5 youtube energy traces and pull power rail averages
    print("\n[1/4] Extracting power rail data from energy traces...")
    rail_per_run: list[dict[str, float]] = []
    energy_drains: list[float] = []

    for run_id in ENERGY_RUN_IDS:
        trace = find_trace(run_id)
        print(f"  {os.path.basename(trace)}")
        rail_per_run.append(extract_power_rails(trace))
        drain = read_drain(find_drain_log(run_id))
        energy_drains.append(drain if drain is not None else 0.0)

    #pivot from per-run dicts into per-rail lists so each rail becomes one box
    rail_data: dict[str, list[float]] = {label: [] for label in POWER_RAILS}
    for run_rails in rail_per_run:
        for label, track_name in POWER_RAILS.items():
            rail_data[label].append(run_rails.get(track_name, 0.0))

    rail_summary = {
        label: {
            "mean_mw": round(sum(v) / len(v), 1),
            "min_mw":  round(min(v), 1),
            "max_mw":  round(max(v), 1),
            "values":  [round(x, 1) for x in v],
        }
        for label, v in rail_data.items()
    }

    #step 2: open each of the 5 chrome method traces and get cpu time per thread
    print("\n[2/4] Extracting CPU thread data from method traces...")
    all_thread_runs: list[list[dict]] = []
    method_drains: list[float] = []

    for run_id in METHOD_RUN_IDS:
        trace = find_trace(run_id)
        print(f"  {os.path.basename(trace)}")
        all_thread_runs.append(extract_cpu_threads(trace))
        drain = read_drain(find_drain_log(run_id))
        method_drains.append(drain if drain is not None else 0.0)

    #build a stable thread name list ordered by first appearance across runs
    #threads that only show up in some runs get 0 filled in for the missing ones
    thread_names: list[str] = []
    seen: set[str] = set()
    for run in all_thread_runs:
        for t in run:
            if t["thread"] not in seen:
                thread_names.append(t["thread"])
                seen.add(t["thread"])
    thread_names = thread_names[:TOP_N_THREADS]

    thread_data: dict[str, list[float]] = {n: [] for n in thread_names}
    for run in all_thread_runs:
        run_map = {t["thread"]: t["cpu_sec"] for t in run}
        for name in thread_names:
            thread_data[name].append(run_map.get(name, 0.0))

    thread_summary = {
        name: {
            "mean_sec": round(sum(v) / len(v), 3),
            "min_sec":  round(min(v), 3),
            "max_sec":  round(max(v), 3),
            "values":   [round(x, 3) for x in v],
        }
        for name, v in thread_data.items()
    }

    drain_summary = {
        "youtube_energy_drain_pct": energy_drains,
        "chrome_method_drain_pct":  method_drains,
        "youtube_mean_drain":       round(sum(energy_drains) / len(energy_drains), 2),
        "chrome_mean_drain":        round(sum(method_drains) / len(method_drains), 2),
    }

    #extract function-level callstack data from the first method trace as representative
    #we use one trace here because the resolved frames are sparse and consistent across runs
    print("\n  extracting function-level callstack data from representative trace...")
    func_summary = extract_hotspot_functions(find_trace(METHOD_RUN_IDS[0]))
    print(f"  {func_summary['resolved_samples']}/{func_summary['total_samples']} samples resolved ({func_summary['resolution_rate_pct']}%)")

    #step 3: generate all plots
    print("\n[3/4] Generating plots...")
    plot_power_rails(rail_data)
    plot_cpu_threads(thread_data)
    plot_hotspot_functions(func_summary, thread_summary)
    plot_battery_drain(energy_drains, method_drains)

    #step 4: send hardware, thread, and function data together so the llm can
    #synthesize all three layers into a complete ranked hotspot analysis
    print("\n[4/4] Querying LLM for hotspot analysis...")
    analysis = llm_hotspot_analysis(rail_summary, thread_summary, drain_summary, func_summary)

    #save everything to one json file so the report can reference exact numbers
    summary = {
        "power_rails":      rail_summary,
        "cpu_threads":      thread_summary,
        "battery_drain":    drain_summary,
        "hotspot_functions": func_summary,
        "llm_analysis":     analysis,
    }
    json_out = os.path.join(OUT_DIR, "hotspot_analysis.json")
    with open(json_out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  saved: {json_out}")

    print("\n" + "=" * 60)
    print(" LLM Energy Hotspot Analysis")
    print("=" * 60)
    print(analysis)
    print("=" * 60)
    print(f"\nAll outputs written to: {OUT_DIR}")


if __name__ == "__main__":
    main()
