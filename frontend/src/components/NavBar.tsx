"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "Dashboard" },
  { href: "/wallets", label: "Smart Wallets" },
  { href: "/trades", label: "Trade Audit" },
  { href: "/markets", label: "Markets" },
  { href: "/signals", label: "Signals" },
  { href: "/paper", label: "Paper Trading" },
  { href: "/edge", label: "Edge Validation" },
  { href: "/control", label: "Control Room" },
  { href: "/observability", label: "Observability" }
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <nav className="border-b border-line bg-[#0a0d12]">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-1 px-5 py-2 text-sm">
        <span className="mr-3 text-xs font-semibold uppercase tracking-wide text-accent">POLYORACLE v0.4</span>
        {links.map((link) => {
          const active = pathname === link.href;
          return (
            <Link
              key={link.href}
              href={link.href}
              className={`rounded px-3 py-1 transition ${active ? "bg-accent/20 text-accent" : "text-slate-300 hover:bg-slate-800"}`}
            >
              {link.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
