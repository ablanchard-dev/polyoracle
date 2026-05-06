"use client";

import { useEffect, useState } from "react";
import { Market, getMarkets } from "@/lib/api";

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export default function MarketsPage() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMarkets().then(setMarkets).catch((err) => setError(err instanceof Error ? err.message : "load failed"));
  }, []);

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Markets</h1>
        <p className="text-sm text-slate-400">Top Polymarket markets ranked by opportunity score (real Gamma data with mock fallback).</p>
      </header>
      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}
      <div className="rounded border border-line bg-panel p-4">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[960px] text-left text-sm">
            <thead className="text-xs uppercase text-slate-400">
              <tr>
                <th className="pb-3">Market</th>
                <th className="pb-3">Category</th>
                <th className="pb-3">YES</th>
                <th className="pb-3">NO</th>
                <th className="pb-3">Volume 24h</th>
                <th className="pb-3">Liquidity</th>
                <th className="pb-3">Spread</th>
                <th className="pb-3">Score</th>
                <th className="pb-3">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {markets.map((market) => (
                <tr key={market.id}>
                  <td className="py-3 pr-3 text-slate-100">{market.question}</td>
                  <td className="py-3 text-xs text-slate-400">{market.category ?? "—"}</td>
                  <td className="py-3">{market.yes_price?.toFixed(2) ?? "—"}</td>
                  <td className="py-3">{market.no_price?.toFixed(2) ?? "—"}</td>
                  <td className="py-3">{money.format(market.volume_24h)}</td>
                  <td className="py-3">{money.format(market.liquidity)}</td>
                  <td className="py-3">{(market.spread * 100).toFixed(1)}%</td>
                  <td className="py-3 font-semibold text-accent">{market.opportunity_score.toFixed(1)}</td>
                  <td className="py-3 text-xs uppercase text-slate-500">{market.data_source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </main>
  );
}
