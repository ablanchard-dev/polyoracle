"use client";

import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const data = [
  { t: "09:00", pnl: 0 },
  { t: "10:00", pnl: 85 },
  { t: "11:00", pnl: 40 },
  { t: "12:00", pnl: 180 },
  { t: "13:00", pnl: 155 },
  { t: "14:00", pnl: 260 }
];

export function PnlChart() {
  return (
    <div className="h-64 rounded border border-line bg-panel p-4">
      <div className="mb-3 text-sm font-semibold text-slate-200">Paper PnL intraday</div>
      <ResponsiveContainer width="100%" height="85%">
        <AreaChart data={data}>
          <XAxis dataKey="t" stroke="#94a3b8" fontSize={12} />
          <YAxis stroke="#94a3b8" fontSize={12} />
          <Tooltip contentStyle={{ background: "#10131a", border: "1px solid #252b36" }} />
          <Area type="monotone" dataKey="pnl" stroke="#22c55e" fill="#22c55e33" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
