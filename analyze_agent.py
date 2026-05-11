#!/usr/bin/env python3
#analyze_agent.py
#reads the perfetto traces and battery drain logs produced by manafa.sh,
#extracts hardware power rail data and cpu thread utilization, generates
#box plots across statistical runs, then sends the structured summary to
#an llm to identify and rank energy hotspots

import os
import glob
import json
import re
import matplotlib
matplotlib.use("Agg")  #non-interactive backend so this runs without a display
import matplotlib.pyplot as plt
from collections import defaultdict
from perfetto.trace_processor import TraceProcessor

#output directories relative to this script's location
RESULTS_DIR  = os.path.join(os.path.dirname(__file__), "results")
PERFETTO_DIR = os.path.join(RESULTS_DIR, "perfetto")
BSTATS_DIR   = os.path.join(RESULTS_DIR, "batterystats")
OUT_DIR      = os.path.join(RESULTS_DIR, "analysis")
os.makedirs(OUT_DIR, exist_ok=True)

#youtube profiled in energy mode (power rails + scheduling), n=15 total
#original 5 runs + 10 new runs (batch 1)
ENERGY_RUN_IDS = [
    "1777869093", "1777869177", "1777869285", "1777869363", "1777869438",
    "1778138313", "1778138388", "1778138507", "1778138595", "1778138682",
    "1778138761", "1778138841", "1778138920", "1778138999", "1778139076",
]

#chrome profiled in energy mode (power rails + scheduling), n=15 (batch 2)
CHROME_ENERGY_RUN_IDS = [
    "1778139524", "1778139642", "1778139723", "1778139799", "1778139878",
    "1778139954", "1778140025", "1778140097", "1778140187", "1778140258",
    "1778140330", "1778140408", "1778140485", "1778140567", "1778140640",
]

#youtube profiled in legacy mode (cpu freq + scheduling, NO power rails), n=15 (batch 3)
#legacy traces have no .perfetto-trace extension
LEGACY_RUN_IDS = [
    "1778212071", "1778212174", "1778212248", "1778212324", "1778212397",
    "1778212472", "1778212553", "1778212630", "1778212705", "1778212784",
    "1778212872", "1778212950", "1778213030", "1778213107", "1778213187",
]

#chrome profiled in method mode (callstack sampling + scheduling), n=10 total
#original 5 runs + 5 new runs (batch 4); trace-1778213760 excluded (bad run)
METHOD_RUN_IDS = [
    "1777869802", "1777869905", "1777869984", "1777870075", "1777870166",
    "1778213676", "1778213872", "1778214049", "1778214126", "1778214207",
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

#representative single-run traces for each mode (used in cross-mode comparison charts)
MODE_TRACES = {
    "energy": "1777761894",
    "memory": "1777762139",
    "both":   "1777762261",
    "method": "1777762423",
}

#emulator validation results
EMULATOR_RESULTS = {
    "legacy": "real",
    "energy": "dummy",
    "memory": "flat",
    "both":   "dummy",
    "method": "real",
}

#waydroid container traces — collected on ubuntu 26.04 arm64 vm via multipass on apple m1
WAYDROID_TRACES = {
    "legacy": os.path.join(os.path.dirname(__file__), "results/perfetto/legacy_waydroid.perfetto-trace"),
    "energy": os.path.join(os.path.dirname(__file__), "results/perfetto/energy_waydroid.perfetto-trace"),
    "memory": os.path.join(os.path.dirname(__file__), "results/perfetto/memory_waydroid.perfetto-trace"),
    "both":   os.path.join(os.path.dirname(__file__), "results/perfetto/both_waydroid.perfetto-trace"),
    "method": os.path.join(os.path.dirname(__file__), "results/perfetto/method_waydroid.perfetto-trace"),
}

#waydroid data quality scores: 1=real, 0.5=partial, 0=dummy values 
WAYDROID_SCORES = {
    "legacy": 0.0,   #cpu_frequency ftrace event blocked in container
    "energy": 0.5,   #sched_switch real (17k slices), power rails dummy (0 tracks)
    "memory": 1.0,   #meminfo counters fully real (847 samples)
    "both":   0.5,   #memory real, power rails dummy
    "method": 0.5,   #sched + per-process memory real, linux.perf callstack blocked
}

GITHUB_TOKEN_ENV       = "GITHUB_TOKEN"
GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com"
GITHUB_MODEL           = "gpt-4o-mini"


def find_trace(run_id: str) -> str:
    #energy/method/memory/both traces have .perfetto-trace extension
    matches = glob.glob(os.path.join(PERFETTO_DIR, f"trace-{run_id}-*.perfetto-trace"))
    if matches:
        return matches[0]
    #legacy traces have no extension — glob any file matching the prefix
    matches = [
        p for p in glob.glob(os.path.join(PERFETTO_DIR, f"trace-{run_id}-*"))
        if os.path.isfile(p) and not p.endswith(".perfetto-trace")
    ]
    if not matches:
        raise FileNotFoundError(f"No trace found for run {run_id}")
    return matches[0]


def find_drain_log(run_id: str) -> str:
    matches = glob.glob(os.path.join(BSTATS_DIR, f"bstats-drain-{run_id}-*.log"))
    return matches[0] if matches else None


def read_drain(path: str) -> float | None:
    if path is None:
        return None
    with open(path) as f:
        for line in f:
            m = re.match(r"battery_drain=(\d+)%", line.strip())
            if m:
                return float(m.group(1))
    return None


def extract_power_rails(trace_path: str) -> dict[str, float]:
    # Android power.stats HAL reports power rail counters as cumulative energy
    # accumulators in microwatt-seconds (µWs). Energy for a session =
    # (last_value - first_value) × 1e-6 [J]. Average power = energy / duration [W].
    # Reference: Android power.stats HAL AIDL spec; same approach used in E-MANAFA.
    tp = TraceProcessor(trace=trace_path)
    duration_s = list(tp.query(
        'SELECT (end_ts - start_ts)/1e9 as dur FROM _trace_bounds'
    ))[0].dur
    sql = """
        SELECT ct.name as rail,
               (MAX(c.value) - MIN(c.value)) * 1e-6 as energy_j
        FROM __intrinsic_counter c
        JOIN counter_track ct ON c.track_id = ct.id
        WHERE ct.name LIKE 'power.rails.%'
        GROUP BY ct.name
    """
    result = tp.query(sql)
    rails = {row.rail: row.energy_j / duration_s * 1000 for row in result}
    tp.close()
    return rails


def extract_cpu_threads(trace_path: str) -> list[dict]:
    #sched slices tell us exactly how long each thread was scheduled on cpu
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


def extract_thread_energy(trace_path: str) -> dict:
    tp = TraceProcessor(trace=trace_path)

    duration_s = list(tp.query(
        'SELECT (end_ts - start_ts)/1e9 as dur FROM _trace_bounds'
    ))[0].dur

    # cumulative µWs accumulators: energy = (last - first) × 1e-6 [J]
    cpu_rail_sql = """
        SELECT ct.name,
               (MAX(c.value) - MIN(c.value)) * 1e-6 as energy_j
        FROM __intrinsic_counter c
        JOIN counter_track ct ON c.track_id = ct.id
        WHERE ct.name IN (
            'power.rails.cpu.big', 'power.rails.cpu.little', 'power.rails.cpu.mid'
        )
        GROUP BY ct.name
    """
    rail_energy_j = {row.name: row.energy_j for row in tp.query(cpu_rail_sql)}
    total_cpu_j = sum(rail_energy_j.values())
    rail_mw = {k: v / duration_s * 1000 for k, v in rail_energy_j.items()}

    #energy mode only has sched_switch events; left join lets us fall back to thread name
    sched_sql = """
        SELECT COALESCE(p.name, t.name) as proc, SUM(ss.dur)/1e9 as cpu_s
        FROM __intrinsic_sched_slice ss
        JOIN __intrinsic_thread t ON ss.utid = t.id
        LEFT JOIN __intrinsic_process p ON t.upid = p.id
        WHERE COALESCE(p.name, t.name) IS NOT NULL
          AND COALESCE(p.name, t.name) != ''
          AND COALESCE(p.name, t.name) != 'swapper'
        GROUP BY COALESCE(p.name, t.name)
        ORDER BY cpu_s DESC
        LIMIT 20
    """
    sched_rows = [(row.proc, row.cpu_s) for row in tp.query(sched_sql)]
    total_sched_s = sum(r[1] for r in sched_rows)
    tp.close()

    per_process = []
    for proc, cpu_s in sched_rows:
        fraction = cpu_s / total_sched_s if total_sched_s > 0 else 0
        energy_j = fraction * total_cpu_j
        per_process.append({
            "process":                proc,
            "cpu_sec":                round(cpu_s, 3),
            "cpu_fraction":           round(fraction, 4),
            "estimated_cpu_energy_j": round(energy_j, 4),
        })

    return {
        "trace_duration_s":   round(duration_s, 2),
        "total_cpu_energy_j": round(total_cpu_j, 4),
        "cpu_rail_power_mw":  {k: round(v, 1) for k, v in rail_mw.items()},
        "per_process":        per_process,
    }


def extract_legacy_cpu_freq(trace_path: str) -> dict[str, float]:
    #ftrace cpu_frequency events are stored as per-cpu counter tracks
    #values are in kHz; divide by 1000 to get MHz
    tp = TraceProcessor(trace=trace_path)
    try:
        sql = """
            SELECT 'CPU ' || CAST(cct.cpu AS TEXT) AS cpu_name,
                   AVG(c.value) / 1e6 AS avg_mhz
            FROM __intrinsic_counter c
            JOIN cpu_counter_track cct ON c.track_id = cct.id
            GROUP BY cct.cpu
            ORDER BY avg_mhz DESC
        """
        rows = list(tp.query(sql))
        if rows and any(r.avg_mhz > 0 for r in rows):
            tp.close()
            return {r.cpu_name: round(r.avg_mhz, 1) for r in rows if r.avg_mhz > 0}
    except Exception:
        pass

    try:
        #fallback: search all counter tracks by name for anything frequency-related
        sql = """
            SELECT ct.name, AVG(c.value) / 1000.0 AS avg_mhz
            FROM __intrinsic_counter c
            JOIN counter_track ct ON c.track_id = ct.id
            WHERE LOWER(ct.name) LIKE '%freq%'
            GROUP BY ct.name
            ORDER BY avg_mhz DESC
        """
        rows = list(tp.query(sql))
        tp.close()
        return {r.name: round(r.avg_mhz, 0) for r in rows if r.avg_mhz > 0}
    except Exception:
        tp.close()
        return {}


def extract_waydroid_results() -> dict:
    results = {}
    for mode, path in WAYDROID_TRACES.items():
        tp = TraceProcessor(trace=path)

        rail_tracks = list(tp.query(
            "SELECT COUNT(*) as n FROM counter_track WHERE name LIKE 'power.rails.%'"
        ))[0].n

        sched_slices = list(tp.query('SELECT COUNT(*) as n FROM __intrinsic_sched_slice'))[0].n

        try:
            perf_samples = list(tp.query('SELECT COUNT(*) as n FROM __intrinsic_perf_sample'))[0].n
        except Exception:
            perf_samples = 0

        mem_counters = list(tp.query(
            "SELECT COUNT(*) as n FROM __intrinsic_counter c "
            "JOIN counter_track ct ON c.track_id=ct.id "
            "WHERE ct.name IN ('MemTotal','MemFree','MemAvailable','Buffers','Cached')"
        ))[0].n

        tp.close()
        trace_size_mb = round(os.path.getsize(path) / 1e6, 3)

        results[mode] = {
            "trace_size_mb":     trace_size_mb,
            "power_rail_tracks": rail_tracks,
            "sched_slices":      sched_slices,
            "perf_samples":      perf_samples,
            "mem_counters":      mem_counters,
            "score":             WAYDROID_SCORES[mode],
        }
    return results


def extract_hotspot_functions(trace_path: str) -> dict:
    tp = TraceProcessor(trace=trace_path)

    total = list(tp.query('SELECT COUNT(*) as n FROM __intrinsic_perf_sample'))[0].n
    resolved = list(tp.query(
        'SELECT COUNT(*) as n FROM __intrinsic_perf_sample WHERE callsite_id IS NOT NULL'
    ))[0].n

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
        "total_samples":       total,
        "resolved_samples":    resolved,
        "resolution_rate_pct": round(resolved / total * 100, 1) if total > 0 else 0,
        "top_functions":       rows,
    }


def _stddev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((x - mean) ** 2 for x in values) / n) ** 0.5


def compute_cold_start_test(energy_runs: list[dict]) -> dict:
    #run 0 is the cold-start run (app process not cached); runs 1..N-1 are warm
    #we test whether the total cpu energy of run 0 differs from the warm mean
    #using a one-sample t-test: H0 = run-0 value drawn from the warm distribution
    from scipy import stats
    total_j = [r["total_cpu_energy_j"] for r in energy_runs]
    run0_j   = total_j[0]
    warm_j   = total_j[1:]
    n_warm   = len(warm_j)
    warm_mean = sum(warm_j) / n_warm
    warm_std  = _stddev(warm_j) * (n_warm / (n_warm - 1)) ** 0.5  #sample std
    #one-sample t-test: is run-0 consistent with the warm distribution?
    t_stat, p_val = stats.ttest_1samp(warm_j, run0_j)
    #95% CI for the warm mean
    se = warm_std / n_warm ** 0.5
    ci_lo = warm_mean - 1.96 * se
    ci_hi = warm_mean + 1.96 * se
    return {
        "run0_total_cpu_j":   round(run0_j, 4),
        "warm_mean_j":        round(warm_mean, 4),
        "warm_std_j":         round(warm_std, 4),
        "warm_95ci":          [round(ci_lo, 4), round(ci_hi, 4)],
        "t_stat":             round(t_stat, 3),
        "p_value":            round(p_val, 4),
        "run0_is_outlier":    int(p_val < 0.05),
    }


def compute_rail_confidence_intervals(rail_data: dict[str, list[float]]) -> dict:
    #95% CI for each rail's mean power using the normal approximation
    result = {}
    for rail, vals in rail_data.items():
        n = len(vals)
        mean = sum(vals) / n
        std  = _stddev(vals) * (n / (n - 1)) ** 0.5 if n > 1 else 0.0
        se   = std / n ** 0.5
        result[rail] = {
            "mean_mw":    round(mean, 1),
            "std_mw":     round(std, 1),
            "ci95_lo_mw": round(mean - 1.96 * se, 1),
            "ci95_hi_mw": round(mean + 1.96 * se, 1),
            "n":          n,
        }
    return result


def compute_ranking_stability(energy_runs: list[dict], top_k: int = 3) -> dict:
    top_k_sets = []
    for run in energy_runs:
        sorted_procs = sorted(run["per_process"],
                              key=lambda p: p["estimated_cpu_energy_j"], reverse=True)
        top_k_sets.append(set(p["process"] for p in sorted_procs[:top_k]))
    if top_k_sets:
        intersection = top_k_sets[0].intersection(*top_k_sets[1:])
    else:
        intersection = set()
    return {
        "top_k":                          top_k,
        "n_runs":                         len(energy_runs),
        "processes_in_top_k_every_run":   list(intersection),
        "stability_rate":                 round(len(intersection) / top_k, 2),
    }


def plot_mode_comparison():
    modes       = ["legacy", "energy", "memory", "both", "method"]
    mode_colors = ["#76b7b2", "#e15759", "#f28e2b", "#b07aa1", "#4e79a7"]

    #60-second run sizes: legacy and energy from n=15 statistical runs (means);
    #memory and both from original single validation runs (no 60s batch collected);
    #method from n=10 statistical runs (mean)
    file_sizes_mb = {
        "legacy": 14.5,   #mean of 15 YouTube legacy 60s runs
        "energy": 6.0,    #mean of 15 YouTube energy 60s runs
        "memory": 0.012,  #single validation run (short)
        "both":   0.066,  #single validation run (short)
        "method": 44.0,   #mean of 10 Chrome method 60s runs
    }

    capabilities = {
        "CPU freq\n& sched": [1, 1, 1, 1, 1],
        "Power\nrails":      [0, 1, 0, 1, 0],
        "Memory\nstats":     [0, 0, 1, 1, 1],
        "Callstack\n(perf)": [0, 0, 0, 0, 1],
    }

    real_scores     = {"legacy": 1.0, "energy": 1.0, "memory": 1.0, "both": 1.0, "method": 1.0}
    emulator_scores = {"legacy": 1.0, "energy": 0.0, "memory": 0.5, "both": 0.0, "method": 1.0}
    waydroid_scores = WAYDROID_SCORES

    import numpy as np
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(11, 13))
    fig.suptitle("Profiling Mode Comparison — on-device-manafa (Pixel 9 Pro XL)", fontsize=12, fontweight="bold")

    sizes = [file_sizes_mb[m] for m in modes]
    bars = ax1.bar(modes, sizes, color=mode_colors, alpha=0.85, edgecolor="white")
    ax1.set_ylabel("Trace File Size (MB)")
    ax1.set_title("Data Richness — Trace Size per Mode (representative 60s run, real device)")
    ax1.set_yscale("log")
    ax1.grid(axis="y", linestyle="--", alpha=0.4)
    for bar, val in zip(bars, sizes):
        ax1.text(bar.get_x() + bar.get_width()/2, val * 1.3,
                 f"{val:.3f}MB" if val < 1 else f"{val:.1f}MB",
                 ha="center", va="bottom", fontsize=8)

    cap_matrix = np.array([capabilities[ds] for ds in capabilities])
    ax2.imshow(cap_matrix, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax2.set_xticks(range(len(modes)))
    ax2.set_xticklabels(modes)
    ax2.set_yticks(range(len(capabilities)))
    ax2.set_yticklabels(list(capabilities.keys()), fontsize=9)
    ax2.set_title("Data Source Capabilities per Mode")
    for i in range(len(capabilities)):
        for j in range(len(modes)):
            val = cap_matrix[i, j]
            ax2.text(j, i, "✓" if val == 1 else "✗", ha="center", va="center",
                     fontsize=14, color="white" if val == 1 else "gray")

    x = np.arange(len(modes))
    w = 0.25
    ax3.bar(x - w,   [real_scores[m]     for m in modes], w, label="Real device (Pixel 9 Pro XL)", color="#4e79a7", alpha=0.85)
    ax3.bar(x,       [emulator_scores[m] for m in modes], w, label="Emulator (Android Studio)",    color="#f28e2b", alpha=0.85)
    waydroid_display = [max(waydroid_scores[m], 0.04) for m in modes]
    ax3.bar(x + w, waydroid_display, w, label="Waydroid (Linux container)", color="#59a14f", alpha=0.85)
    for i, m in enumerate(modes):
        if waydroid_scores[m] == 0.0:
            ax3.text(x[i] + w, 0.06, "blocked", ha="center", va="bottom", fontsize=6.5, color="#333333")
    ax3.set_xticks(x)
    ax3.set_xticklabels(modes)
    ax3.set_yticks([0, 0.5, 1.0])
    ax3.set_yticklabels(["dummy (0)", "partial (0.5)", "real data (1.0)"])
    ax3.set_title("Data Quality per Mode — Real Device vs Emulator vs Waydroid Container")
    ax3.legend(loc="lower right")
    ax3.grid(axis="y", linestyle="--", alpha=0.4)

    plt.tight_layout()
    out = os.path.join(OUT_DIR, "mode_comparison.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_power_rails(rail_data: dict[str, list[float]]):
    n = len(next(iter(rail_data.values())))
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
    ax.set_title(f"Power Rail Distribution — YouTube Energy Mode (n={n} runs, 60s each)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout(pad=1.5)

    out = os.path.join(OUT_DIR, "boxplot_power_rails.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_cpu_threads(thread_data: dict[str, list[float]]):
    n = len(next(iter(thread_data.values())))
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
    ax.set_title(f"CPU Time per Thread — Chrome Method Tracing (n={n} runs, 60s each)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()

    out = os.path.join(OUT_DIR, "boxplot_cpu_threads.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_hotspot_functions(func_summary: dict, thread_summary: dict):
    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(10, 8),
                                          gridspec_kw={"height_ratios": [2, 1]})

    thread_names = list(thread_summary.keys())
    thread_means  = [thread_summary[n]["mean_sec"] for n in thread_names]
    sorted_pairs  = sorted(zip(thread_means, thread_names), reverse=True)
    t_means, t_names = zip(*sorted_pairs)

    bars = ax_top.barh(t_names[::-1], t_means[::-1], color="#4e79a7", alpha=0.8)
    ax_top.set_xlabel("Mean CPU Time (seconds, n=10 runs, 60s each)")
    ax_top.set_title("Energy Hotspot Analysis — Chrome Method Tracing")
    ax_top.grid(axis="x", linestyle="--", alpha=0.4)
    for bar, val in zip(bars, t_means[::-1]):
        ax_top.text(val + 0.05, bar.get_y() + bar.get_height() / 2,
                    f"{val:.2f}s", va="center", fontsize=8)

    funcs = func_summary["top_functions"][:10]
    f_names   = [f["function"][:50] for f in funcs]
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


def plot_thread_energy(thread_energy_runs: list[dict], label: str, out_name: str):
    n = len(thread_energy_runs)
    min_runs = n // 2  #process must appear in at least half the runs to be plotted

    proc_energies: dict[str, list[float]] = defaultdict(list)
    for run in thread_energy_runs:
        for p in run["per_process"]:
            proc_energies[p["process"]].append(p["estimated_cpu_energy_j"])

    stable = {k: v for k, v in proc_energies.items() if len(v) >= min_runs}
    means  = {k: sum(v) / len(v) for k, v in stable.items()}
    sorted_procs = sorted(means.items(), key=lambda x: x[1], reverse=True)[:12]

    labels = [p[0][:28] for p in sorted_procs]
    values = [p[1] for p in sorted_procs]

    fig, ax = plt.subplots(figsize=(11, 7))
    bars = ax.barh(labels[::-1], values[::-1], color="#e15759", alpha=0.82)
    ax.set_xlabel(f"Estimated CPU Energy (Joules, mean of n={n} runs, 60s each)")
    ax.set_title(f"Per-Process CPU Energy Allocation — {label}\n"
                 "(power rail × scheduling fraction — proportional split model)")
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    for bar, val in zip(bars, values[::-1]):
        ax.text(val + 0.0005, bar.get_y() + bar.get_height()/2,
                f"{val:.3f}J", va="center", fontsize=7)
    plt.tight_layout()
    plt.subplots_adjust(left=0.22)

    out = os.path.join(OUT_DIR, out_name)
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_app_energy_comparison(yt_runs: list[dict], chrome_runs: list[dict]):
    def top_processes(runs: list[dict], top_k: int = 8) -> list[tuple]:
        n = len(runs)
        min_runs = n // 2
        proc_energies: dict[str, list[float]] = defaultdict(list)
        for run in runs:
            for p in run["per_process"]:
                proc_energies[p["process"]].append(p["estimated_cpu_energy_j"])
        stable = {k: v for k, v in proc_energies.items() if len(v) >= min_runs}
        means  = {k: sum(v) / len(v) for k, v in stable.items()}
        return sorted(means.items(), key=lambda x: x[1], reverse=True)[:top_k]

    yt_top  = top_processes(yt_runs)
    ch_top  = top_processes(chrome_runs)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))

    yt_labels = [p[0][:25] for p in yt_top]
    yt_values = [p[1] for p in yt_top]
    ax1.barh(yt_labels[::-1], yt_values[::-1], color="#e15759", alpha=0.82)
    ax1.set_xlabel(f"Estimated CPU Energy (J, n={len(yt_runs)} runs)")
    ax1.set_title("YouTube — Energy Mode")
    ax1.grid(axis="x", linestyle="--", alpha=0.4)
    for i, val in enumerate(yt_values[::-1]):
        ax1.text(val + 0.0005, i, f"{val:.3f}J", va="center", fontsize=7)

    ch_labels = [p[0][:25] for p in ch_top]
    ch_values = [p[1] for p in ch_top]
    ax2.barh(ch_labels[::-1], ch_values[::-1], color="#4e79a7", alpha=0.82)
    ax2.set_xlabel(f"Estimated CPU Energy (J, n={len(chrome_runs)} runs)")
    ax2.set_title("Chrome — Energy Mode")
    ax2.grid(axis="x", linestyle="--", alpha=0.4)
    for i, val in enumerate(ch_values[::-1]):
        ax2.text(val + 0.0005, i, f"{val:.3f}J", va="center", fontsize=7)

    fig.suptitle("Per-Process CPU Energy Attribution — YouTube vs Chrome\n"
                 "(proportional-split attribution yields app-specific process rankings)",
                 fontsize=11, fontweight="bold")
    plt.tight_layout()
    plt.subplots_adjust(left=0.18)

    out = os.path.join(OUT_DIR, "app_energy_comparison.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_legacy_vs_energy_comparison(cpu_freq_per_run: list[dict], rail_data: dict[str, list[float]]):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    #aggregate CPU frequency across all legacy runs
    freq_agg: dict[str, list[float]] = defaultdict(list)
    for run_freqs in cpu_freq_per_run:
        for cpu, mhz in run_freqs.items():
            freq_agg[cpu].append(mhz)

    if freq_agg:
        freq_means = {k: sum(v) / len(v) for k, v in freq_agg.items()}
        sorted_cpus = sorted(freq_means.items(), key=lambda x: x[1], reverse=True)[:8]
        cpu_labels = [p[0][:22] for p in sorted_cpus]
        cpu_values = [p[1] for p in sorted_cpus]

        ax1.barh(cpu_labels[::-1], cpu_values[::-1], color="#76b7b2", alpha=0.85)
        ax1.set_xlabel(f"Mean CPU Frequency (MHz, n={len(cpu_freq_per_run)} runs)")
        for i, val in enumerate(cpu_values[::-1]):
            ax1.text(val + 5, i, f"{val:.0f}MHz", va="center", fontsize=8)
    else:
        ax1.text(0.5, 0.5, "No CPU frequency data\nfound in legacy traces",
                 ha="center", va="center", transform=ax1.transAxes, fontsize=10)

    ax1.set_title("Legacy Mode: CPU Frequency\n(activity proxy — not calibrated to Watts)")
    ax1.grid(axis="x", linestyle="--", alpha=0.4)

    #power rail means from energy traces
    n_energy = len(next(iter(rail_data.values())))
    rail_means = [(sum(rail_data[k]) / len(rail_data[k]), k) for k in rail_data]
    rail_means.sort(reverse=True)
    r_values = [p[0] for p in rail_means]
    r_labels = [p[1] for p in rail_means]

    ax2.barh(r_labels[::-1], r_values[::-1], color="#e15759", alpha=0.85)
    ax2.set_xlabel(f"Mean Power (mW, n={n_energy} runs)")
    ax2.set_title("Energy Mode: Hardware Power Rails\n(calibrated Watts — directly actionable)")
    ax2.grid(axis="x", linestyle="--", alpha=0.4)
    for i, val in enumerate(r_values[::-1]):
        ax2.text(val + 2, i, f"{val:.0f}mW", va="center", fontsize=8)

    fig.suptitle("Legacy Mode vs Energy Mode — What Power Rail Data Adds\n"
                 "(same YouTube workload: left shows CPU activity proxy; right shows actual hardware energy)",
                 fontsize=11, fontweight="bold")
    plt.tight_layout()

    out = os.path.join(OUT_DIR, "legacy_vs_energy_comparison.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  saved: {out}")
    return out


def plot_battery_drain(energy_drains: list[float], method_drains: list[float]):
    fig, ax = plt.subplots(figsize=(6, 5))
    bp = ax.boxplot([energy_drains, method_drains], patch_artist=True)
    for patch, color in zip(bp["boxes"], ["#e15759", "#4e79a7"]):
        patch.set_facecolor(color)
        patch.set_alpha(0.8)
    ax.set_xticks([1, 2])
    ax.set_xticklabels([f"YouTube\n(energy mode, n={len(energy_drains)})",
                        f"Chrome\n(method mode, n={len(method_drains)})"])
    ax.set_ylabel("Battery Drain (%)")
    ax.set_title("Battery Drain Comparison (60s runs each)\n"
                 "BatteryStats has 1% resolution — power rail data is the primary energy signal")
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
    client = OpenAI(base_url=GITHUB_MODELS_ENDPOINT, api_key=token)

    prompt = f"""You are an Android performance engineer analyzing on-device profiling data.

## Power Rail Data (YouTube, energy mode, n=15 runs, 60s each)
Average power draw per hardware rail (mW):
{json.dumps(rail_summary, indent=2)}

## CPU Thread Data (Chrome, method tracing, n=10 runs, 60s each)
Average CPU time per thread (seconds over 60s trace):
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
4. Interpret what the resolved function names reveal about Chrome's runtime behaviour, even given the low resolution rate.
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

    #step 1: youtube energy traces (n=15) — power rails + per-process energy
    print(f"\n[1/5] Extracting power rail data from {len(ENERGY_RUN_IDS)} YouTube energy traces...")
    rail_per_run: list[dict[str, float]] = []
    energy_drains: list[float] = []

    for run_id in ENERGY_RUN_IDS:
        trace = find_trace(run_id)
        print(f"  {os.path.basename(trace)}")
        rail_per_run.append(extract_power_rails(trace))
        drain = read_drain(find_drain_log(run_id))
        energy_drains.append(drain if drain is not None else 0.0)

    rail_data: dict[str, list[float]] = {label: [] for label in POWER_RAILS}
    for run_rails in rail_per_run:
        for label, track_name in POWER_RAILS.items():
            rail_data[label].append(run_rails.get(track_name, 0.0))

    n_yt = len(ENERGY_RUN_IDS)
    rail_summary = {
        label: {
            "mean_mw": round(sum(v) / len(v), 1),
            "std_mw":  round(_stddev(v), 1),
            "min_mw":  round(min(v), 1),
            "max_mw":  round(max(v), 1),
            "values":  [round(x, 1) for x in v],
        }
        for label, v in rail_data.items()
    }

    print(f"\n  computing per-process energy allocation (YouTube, n={n_yt})...")
    yt_energy_runs = [extract_thread_energy(find_trace(r)) for r in ENERGY_RUN_IDS]
    yt_mean_cpu_j = round(sum(r["total_cpu_energy_j"] for r in yt_energy_runs) / n_yt, 4)
    print(f"  mean total cpu energy: {yt_mean_cpu_j}J")
    yt_stability = compute_ranking_stability(yt_energy_runs, top_k=3)
    print(f"  top-3 ranking stability: {yt_stability['processes_in_top_k_every_run']} appear in all {n_yt} runs")
    yt_cold_start = compute_cold_start_test(yt_energy_runs)
    print(f"  cold-start test: run0={yt_cold_start['run0_total_cpu_j']}J, warm mean={yt_cold_start['warm_mean_j']}J, p={yt_cold_start['p_value']} (outlier={yt_cold_start['run0_is_outlier']})")
    rail_ci = compute_rail_confidence_intervals(rail_data)
    for rail, ci in rail_ci.items():
        print(f"  {rail}: {ci['mean_mw']}mW [95% CI {ci['ci95_lo_mw']}--{ci['ci95_hi_mw']}]")

    #step 2: chrome energy traces (n=15) — per-process energy for chrome
    print(f"\n[2/5] Extracting per-process energy from {len(CHROME_ENERGY_RUN_IDS)} Chrome energy traces...")
    chrome_energy_runs = [extract_thread_energy(find_trace(r)) for r in CHROME_ENERGY_RUN_IDS]
    n_ch_e = len(CHROME_ENERGY_RUN_IDS)
    chrome_mean_cpu_j = round(sum(r["total_cpu_energy_j"] for r in chrome_energy_runs) / n_ch_e, 4)
    print(f"  mean total cpu energy: {chrome_mean_cpu_j}J")
    chrome_stability = compute_ranking_stability(chrome_energy_runs, top_k=3)
    print(f"  top-3 ranking stability: {chrome_stability['processes_in_top_k_every_run']} appear in all {n_ch_e} runs")

    #step 3: youtube legacy traces (n=15) — cpu frequency (no power rails)
    print(f"\n[3/5] Extracting CPU frequency from {len(LEGACY_RUN_IDS)} YouTube legacy traces...")
    cpu_freq_per_run: list[dict[str, float]] = []
    for run_id in LEGACY_RUN_IDS:
        trace = find_trace(run_id)
        print(f"  {os.path.basename(trace)}")
        freqs = extract_legacy_cpu_freq(trace)
        cpu_freq_per_run.append(freqs)
        if freqs:
            top_cpu = max(freqs, key=freqs.get)
            print(f"    top cpu: {top_cpu} @ {freqs[top_cpu]:.0f}MHz")
        else:
            print(f"    no frequency data found")

    #step 4: chrome method traces (n=10) — cpu threads + hotspot functions
    print(f"\n[4/5] Extracting CPU thread data from {len(METHOD_RUN_IDS)} Chrome method traces...")
    all_thread_runs: list[list[dict]] = []
    method_drains: list[float] = []

    for run_id in METHOD_RUN_IDS:
        trace = find_trace(run_id)
        print(f"  {os.path.basename(trace)}")
        all_thread_runs.append(extract_cpu_threads(trace))
        drain = read_drain(find_drain_log(run_id))
        method_drains.append(drain if drain is not None else 0.0)

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

    n_method = len(METHOD_RUN_IDS)
    thread_summary = {
        name: {
            "mean_sec": round(sum(v) / len(v), 3),
            "std_sec":  round(_stddev(v), 3),
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

    print("\n  extracting function-level callstack data from representative trace...")
    func_summary = extract_hotspot_functions(find_trace(METHOD_RUN_IDS[0]))
    print(f"  {func_summary['resolved_samples']}/{func_summary['total_samples']} samples resolved ({func_summary['resolution_rate_pct']}%)")

    #trace file size measurements
    energy_sizes_mb   = [os.path.getsize(find_trace(r)) / 1e6 for r in ENERGY_RUN_IDS]
    method_sizes_mb   = [os.path.getsize(find_trace(r)) / 1e6 for r in METHOD_RUN_IDS]
    chrome_e_sizes_mb = [os.path.getsize(find_trace(r)) / 1e6 for r in CHROME_ENERGY_RUN_IDS]
    legacy_sizes_mb   = [os.path.getsize(find_trace(r)) / 1e6 for r in LEGACY_RUN_IDS]

    print("\n  extracting waydroid container results...")
    waydroid_results = extract_waydroid_results()
    for mode, res in waydroid_results.items():
        print(f"  waydroid {mode}: {res['trace_size_mb']}MB, sched={res['sched_slices']} slices, mem={res['mem_counters']} counters")

    data_richness = {
        "youtube_energy_mean_mb": round(sum(energy_sizes_mb) / len(energy_sizes_mb), 2),
        "chrome_method_mean_mb":  round(sum(method_sizes_mb) / len(method_sizes_mb), 2),
        "chrome_energy_mean_mb":  round(sum(chrome_e_sizes_mb) / len(chrome_e_sizes_mb), 2),
        "legacy_mean_mb":         round(sum(legacy_sizes_mb) / len(legacy_sizes_mb), 2),
        "emulator_validation":    EMULATOR_RESULTS,
        "waydroid_validation":    WAYDROID_SCORES,
    }
    print(f"  youtube energy:  mean {data_richness['youtube_energy_mean_mb']}MB per trace")
    print(f"  chrome energy:   mean {data_richness['chrome_energy_mean_mb']}MB per trace")
    print(f"  chrome method:   mean {data_richness['chrome_method_mean_mb']}MB per trace")
    print(f"  legacy:          mean {data_richness['legacy_mean_mb']}MB per trace")

    #step 5: generate all plots
    print("\n[5/5] Generating plots...")
    plot_mode_comparison()
    plot_power_rails(rail_data)
    plot_cpu_threads(thread_data)
    plot_thread_energy(yt_energy_runs,     "YouTube Energy Mode",  "thread_energy_joules.png")
    plot_thread_energy(chrome_energy_runs, "Chrome Energy Mode",   "chrome_energy_joules.png")
    plot_app_energy_comparison(yt_energy_runs, chrome_energy_runs)
    plot_legacy_vs_energy_comparison(cpu_freq_per_run, rail_data)
    plot_hotspot_functions(func_summary, thread_summary)
    plot_battery_drain(energy_drains, method_drains)

    print("\n[LLM] Querying LLM for hotspot analysis...")
    analysis = llm_hotspot_analysis(rail_summary, thread_summary, drain_summary, func_summary)

    summary = {
        "run_counts": {
            "youtube_energy": n_yt,
            "chrome_energy":  n_ch_e,
            "youtube_legacy": len(LEGACY_RUN_IDS),
            "chrome_method":  n_method,
        },
        "power_rails":         rail_summary,
        "cpu_threads":         thread_summary,
        "battery_drain":       drain_summary,
        "youtube_energy_j":    yt_energy_runs,
        "chrome_energy_j":     chrome_energy_runs,
        "hotspot_functions":   func_summary,
        "data_richness":       data_richness,
        "waydroid_results":    waydroid_results,
        "ranking_stability":   {
            "youtube": yt_stability,
            "chrome":  chrome_stability,
        },
        "cold_start_test":     yt_cold_start,
        "rail_ci_95":          rail_ci,
        "llm_analysis":        analysis,
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
