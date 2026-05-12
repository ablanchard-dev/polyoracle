"use client";

// v0.7.8 Phase 8 — Observability dashboard.
// Shows latency p50/p95 per pipeline path, adaptive close-loop state,
// resolver cache stats. Plus a prominent KILL SWITCH button.
//
// Polls /observability/* every 3s.

import { AlertTriangle, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import {
  LatencyStatus,
  ResolverStatus,
  SchedulerStatus,
  getLatency,
  getResolverStats,
  getScheduler,
  killSwitchFlatten,
} from "@/lib/api";
import CopyEfficiencyPanel from "@/components/CopyEfficiencyPanel";
import UtilizationPanel from "@/components/UtilizationPanel";

export default function ObservabilityPage() {
  const [latency, setLatency] = useState<LatencyStatus | null>(null);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [resolver, setResolver] = useState<ResolverStatus | null>(null);
  const [killing, setKilling] = useState(false);
  const [killResult, setKillResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [l, s, r] = await Promise.all([
        getLatency(),
        getScheduler(),
        getResolverStats(),
      ]);
      setLatency(l);
      setScheduler(s);
      setResolver(r);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);

  async function handleKillSwitch() {
    if (!confirm("KILL SWITCH: flatten ALL open paper positions immediately. Continue?")) {
      return;
    }
    setKilling(true);
    try {
      const result = await killSwitchFlatten();
      setKillResult(`Closed ${result.closed_count} positions: ${result.message}`);
    } catch (e: any) {
      setKillResult(`ERROR: ${e?.message || String(e)}`);
    } finally {
      setKilling(false);
    }
  }

  return (
    <div className="space-y-8">
      <header className="flex items-center justify-between">
        <h1 className="text-3xl font-bold">Observability — v0.7.8</h1>
        <button
          onClick={handleKillSwitch}
          disabled={killing}
          className="flex items-center gap-2 rounded-lg bg-red-600 hover:bg-red-700 px-4 py-2 text-white font-semibold disabled:opacity-50"
        >
          <AlertTriangle className="h-5 w-5" />
          {killing ? "Flattening…" : "KILL SWITCH"}
        </button>
      </header>

      {error && (
        <div className="rounded-lg bg-red-100 border border-red-400 p-3 text-red-700">
          Error: {error}
        </div>
      )}
      {killResult && (
        <div className="rounded-lg bg-yellow-100 border border-yellow-400 p-3 text-yellow-900">
          {killResult}
        </div>
      )}

      {/* Phase A 2026-05-11 — M5 throughput + M1 copy efficiency */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <UtilizationPanel />
        <CopyEfficiencyPanel />
      </section>

      {/* Latency table */}
      <section>
        <h2 className="text-xl font-semibold mb-3 flex items-center gap-2">
          <Zap className="h-5 w-5" />
          Latency budgets (Vision Lock §4)
        </h2>
        {latency && Object.keys(latency.paths).length === 0 ? (
          <p className="text-gray-500">No samples yet — pipeline must run for data to appear.</p>
        ) : (
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="bg-gray-100 dark:bg-gray-800">
                <th className="text-left px-3 py-2">Path</th>
                <th className="text-right px-3 py-2">N</th>
                <th className="text-right px-3 py-2">p50 (ms)</th>
                <th className="text-right px-3 py-2">p95 (ms)</th>
                <th className="text-right px-3 py-2">Max (ms)</th>
                <th className="text-right px-3 py-2">Budget (ms)</th>
                <th className="text-right px-3 py-2">Ratio</th>
                <th className="text-center px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {latency &&
                Object.entries(latency.paths).map(([name, stats]) => (
                  <tr key={name} className="border-b border-gray-200 dark:border-gray-700">
                    <td className="px-3 py-2 font-mono text-xs">{name}</td>
                    <td className="text-right px-3 py-2">{stats.n}</td>
                    <td className="text-right px-3 py-2">{stats.p50.toFixed(0)}</td>
                    <td className="text-right px-3 py-2">{stats.p95.toFixed(0)}</td>
                    <td className="text-right px-3 py-2">{stats.max.toFixed(0)}</td>
                    <td className="text-right px-3 py-2">{stats.budget_ms ?? "—"}</td>
                    <td className="text-right px-3 py-2">
                      {stats.ratio != null ? `${stats.ratio.toFixed(2)}×` : "—"}
                    </td>
                    <td className="text-center px-3 py-2">
                      {stats.breach ? "❌" : "✅"}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Adaptive close-loop scheduler */}
      <section>
        <h2 className="text-xl font-semibold mb-3">Adaptive close-loop</h2>
        {scheduler && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500">Registered positions</div>
              <div className="text-2xl font-mono">{scheduler.registered_positions}</div>
            </div>
            <div className="rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500">Heap size</div>
              <div className="text-2xl font-mono">{scheduler.heap_size}</div>
            </div>
            <div className="col-span-2 md:col-span-2 rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500 mb-1">Bucket intervals</div>
              <div className="text-xs font-mono space-y-1">
                {Object.entries(scheduler.bucket_intervals_s).map(([k, v]) => (
                  <div key={k}>
                    <span className="font-semibold">{k}</span>: {v}s
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </section>

      {/* Resolver cache */}
      <section>
        <h2 className="text-xl font-semibold mb-3">Market metadata resolver cache</h2>
        {resolver && (
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500">Static cache</div>
              <div className="text-2xl font-mono">{resolver.static_cache_size}</div>
              <div className="text-xs text-gray-500">TTL {resolver.ttl.static_s}s</div>
            </div>
            <div className="rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500">Dynamic cache</div>
              <div className="text-2xl font-mono">{resolver.dynamic_cache_size}</div>
              <div className="text-xs text-gray-500">TTL {resolver.ttl.dynamic_s}s</div>
            </div>
            <div className="rounded-lg bg-gray-100 dark:bg-gray-800 p-3">
              <div className="text-xs text-gray-500">NOT_FOUND blacklist</div>
              <div className="text-2xl font-mono">{resolver.not_found_blacklist_size}</div>
              <div className="text-xs text-gray-500">TTL {resolver.ttl.not_found_s}s</div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
