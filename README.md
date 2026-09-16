# Polyoracle

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-frontend-000000?logo=nextdotjs&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

Research bot for Polymarket prediction markets: it looks for wallets that win
consistently, checks whether that record holds out of sample, and paper-trades the
rare signals that survive a copyable-edge filter.

**Status: research / paper only.** Live trading is locked. The point of the project
is the engineering and the refusal to trust an edge before it is measured, not a
profit claim.

## What it does

```text
public Polymarket data (Gamma, CLOB, Data API)
  -> market-first discovery      scan resolved markets, rebuild each wallet's record
  -> win-rate engine             win rate with a confidence level from sample size
  -> out-of-sample validation    a wallet must keep its rate on markets it was not ranked on
  -> signal engine               new trades from validated wallets become signals
  -> risk + capital allocator    spread, liquidity, exposure caps, kill switch
  -> paper trading engine        simulated fills, positions, PnL, no-trade log
```

About 33k lines of Python (FastAPI + SQLModel) behind a Next.js dashboard (markets,
wallets, signals, paper trades, control panel).

## Safety

- `LIVE_ENABLED=false` by default, and the compliance check refuses live without an
  allowed jurisdiction.
- The bot loop refuses the `LIVE` mode outright, and no API route switches to it.
- The live order path targets a Polymarket SDK v2 that was never released; the tests
  that cover it are skipped and say so. Nothing in this repository can place a real
  order.
- No VPN, no geographic workaround, no private key in code. Polymarket is not
  reachable from some countries (France included); the app then runs on clearly
  tagged mock data (`data_source=mock`).

## Run

```bash
docker compose up --build        # backend :8000, frontend :3000, postgres, redis
curl http://localhost:8000/health
```

CI starts this stack from a clean checkout on every push.

Running the backend **without Docker** (SQLite) needs the seeded wallet pool: the
startup guard refuses an empty `marketfirstwalletrecord` table on purpose, so a
wrong or empty database can never boot silently. The pool is not in the repository.

## Tests

```bash
cd backend
pip install -r requirements.txt
pytest
```

930+ tests: the win-rate engine, wallet classification, out-of-sample validation,
the capital allocator, the paper engine, the database guard and every safety lock.
For example, a 100% win rate on 99 resolved markets can never reach ELITE, and an
API outage with mock data disabled raises instead of writing fake markets.

## Known limitations

- **Win rate is not edge.** Wallet ranking uses win rate, sample size, category
  consistency and activity. It does not adjust for entry price: a wallet that buys
  outcomes at 0.95 wins often and earns little. The copy filter caps the entry price,
  but the ELITE label itself does not measure expected value.
- The results of the author's 3-year discovery run (146,990 wallets evaluated,
  52 ELITE after out-of-sample validation) come from a local database that is not in
  the repository, so they cannot be reproduced from a clone. Details in
  [docs/HISTORY.md](docs/HISTORY.md).
- **Paper PnL on unresolved markets is not marked to market.** The close loop that runs
  closes resolved markets at 1.0 / 0.0, but it is called without a price source: a
  position still open after 24 h on an unresolved market is closed at its entry price
  (only synthetic fees and slippage apply), and the take-profit / stop-loss rules never
  fire in the bot loop. Only resolved markets carry a real result. Wiring a mark price
  from the CLOB was not possible here (Polymarket is not reachable from France).
- No live trading, by design, until 30+ days of paper results show positive
  expectancy after spread and slippage.

## History

The full development log (versions 0.4 to 0.7.9, every rule change, audits and
endpoints) is in [docs/HISTORY.md](docs/HISTORY.md).

## License

MIT
