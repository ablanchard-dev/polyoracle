import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        panel: "#10131a",
        line: "#252b36",
        accent: "#22c55e",
        warning: "#f59e0b",
        danger: "#ef4444"
      }
    }
  },
  plugins: []
};

export default config;
