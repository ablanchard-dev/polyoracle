import type { Metadata } from "next";
import type { ReactNode } from "react";
import { NavBar } from "@/components/NavBar";
import "./globals.css";

export const metadata: Metadata = {
  title: "POLYORACLE",
  description: "Smart-money audit bot for Polymarket"
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <NavBar />
        {children}
      </body>
    </html>
  );
}
