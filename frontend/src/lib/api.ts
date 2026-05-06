export type BotStatus = {
  mode: string;
  running: boolean;
  paused: boolean;
  kill_switch_active: boolean;
  live_enabled: boolean;
  live_blocked_reason: string | null;
  paper_capital: number;
  paper_pnl: number;
  exposure: number;
  open_positions: number;
  active_signals: number;
  last_error: string | null;
};

export type CycleSummary = {
  duration_ms: number;
  markets_scanned: number;
  wallets_audited: number;
  trades_audited: number;
  signals_generated: number;
  paper_trades_opened: number;
  rejected_signals: number;
  rejection_reasons: Record<string, number>;
  last_error: string | null;
  completed_at?: string | null;
};

export type BotLoopStatus = {
  mode: string;
  running: boolean;
  paused: boolean;
  kill_switch_active: boolean;
  last_cycle_at: string | null;
  last_cycle_duration_ms: number;
  cycles_count: number;
  errors_count: number;
  markets_scanned: number;
  wallets_audited: number;
  trades_audited: number;
  signals_generated: number;
  paper_trades_opened: number;
  rejected_signals: number;
  last_error: string | null;
  last_cycle_summary?: CycleSummary | Record<string, never>;
};

export type Market = {
  id: string;
  question: string;
  category: string | null;
  yes_price: number | null;
  no_price: number | null;
  volume_24h: number;
  volume_7d: number;
  spread: number;
  liquidity: number;
  open_interest: number;
  deadline: string | null;
  opportunity_score: number;
  clob_token_ids: string | null;
  data_source: string;
  raw_slug: string | null;
};

export type Signal = {
  id: string;
  market_id: string;
  outcome: string;
  signal_type: string;
  score: number;
  confidence: number;
  reason: string;
  action: string;
  status: string;
  decision?: string;
  proposed_size_usd?: number;
  copyable_edge?: number;
  created_at: string;
};

export type AppSettings = {
  local_first: boolean;
  storage_backend: string;
  sqlite_path: string;
  duckdb_enabled: boolean;
  postgres_enabled: boolean;
  redis_enabled: boolean;
  live_enabled: boolean;
  paper_trading_enabled: boolean;
  mock_data_enabled: boolean;
  max_risk_per_trade: number;
  max_daily_loss: number;
  max_weekly_loss: number;
  max_spread: number;
  min_liquidity: number;
  min_signal_score: number;
};

export type StorageStatus = {
  local_first: boolean;
  storage_backend: string;
  sqlite_path: string;
  sqlite_exists: boolean;
  data_root: string;
  data_root_exists: boolean;
  duckdb_enabled: boolean;
  postgres_enabled: boolean;
  redis_enabled: boolean;
  mock_data_enabled: boolean;
  paper_trading_enabled: boolean;
};

export type EdgeReport = {
  conclusion: string;
  metrics: Record<string, number>;
  baseline?: Record<string, number>;
  strategies?: Record<string, Record<string, number>>;
  wallet_breakdown?: Array<{ wallet: string; trades: number; edge: number; tier: string }>;
  category_breakdown?: Array<{ category: string; trades: number; edge: number }>;
  no_trade_decision_log?: Array<{
    id: string;
    reason_code: string;
    market_id: string | null;
    signal_id: string | null;
    wallet_address: string | null;
    details: string | null;
    saved_loss_estimate: number;
    created_at: string;
  }>;
  note: string;
};

export type WalletAudit = {
  address: string;
  smart_score: number;
  whale_score: number;
  reliability: number;
  copyability: number;
  confidence: number;
  tier: string;
  specialty: string | null;
  pnl: number;
  roi: number;
  win_rate: number;
  sample_size: number;
  suspicious: boolean;
  data_source: string;
  audit_at: string | null;
};

export type DiscoveryAuditReport = {
  started_at: string;
  finished_at: string;
  duration_ms: number;
  requested_limit: number;
  data_source: string;
  ranking_basis_detected: string;
  discovery_errors: string[];
  top_wallets_count: number;
  audited_wallets_count: number;
  failed_wallets_count: number;
  tier_breakdown: Record<string, number>;
  suspicious_count: number;
  insufficient_data_count: number;
  average_win_rate: number | null;
  median_win_rate: number | null;
  average_resolved_market_sample_size: number;
  wallets_with_reliable_win_rate_count: number;
  wallets_with_insufficient_win_rate_count: number;
  warnings: string[];
  conclusion: string;
  rationale: string;
  csv_path: string;
  json_path: string;
  audited_wallets?: Array<Record<string, unknown>>;
};

export type WalletWinRate = {
  address: string;
  resolved_market_win_rate: number | null;
  trade_level_win_rate: number | null;
  market_sample_size: number;
  resolved_winning_markets: number;
  resolved_losing_markets: number;
  unresolved_markets_count: number;
  win_rate_confidence: string;
  win_rate_warnings: string[];
  data_source: string;
  explanation: string;
};

export type TopWallet = {
  address: string;
  pnl: number;
  roi: number;
  win_rate: number;
  market_count: number;
  volume: number;
  specialty: string | null;
  last_trade_at: string | null;
  data_source: string;
};

export type TradeAudit = {
  id: string;
  trade_id: string;
  wallet_address: string;
  market_id: string;
  question: string | null;
  outcome: string | null;
  side: string | null;
  price: number;
  size: number;
  notional_usd: number;
  estimated_spread: number;
  estimated_slippage: number;
  wallet_score: number;
  wallet_tier: string;
  market_liquidity_score: number;
  orderbook_quality: string;
  copy_delay_seconds: number;
  price_deterioration: number;
  copyable_edge: number;
  trade_quality_score: number;
  decision: string;
  reasons: string | null;
  warnings: string | null;
  audited_at: string | null;
};

export type TradeCluster = {
  id: string;
  market_id: string;
  outcome: string | null;
  side: string | null;
  wallet_count: number;
  trade_count: number;
  notional_usd: number;
  average_price: number;
  average_wallet_score: number;
  started_at: string | null;
  ended_at: string | null;
  confidence: number;
  detected_at: string | null;
};

export type SmartMoneyEvent = {
  id: string;
  event_type: string;
  wallet_address: string | null;
  market_id: string | null;
  outcome: string | null;
  notional_usd: number;
  price: number;
  confidence: number;
  summary: string | null;
  created_at: string | null;
};

export type PaperPerformance = {
  open_positions: number;
  closed_trades: number;
  realized_pnl: number;
  win_rate: number;
  daily_pnl: number;
  weekly_pnl: number;
  exposure: number;
  capital: number;
  auto_enabled: boolean;
};

export type PaperPosition = {
  id: string;
  market_id: string;
  outcome: string;
  side: string;
  quantity: number;
  average_price: number;
  notional_usd: number;
  realized_pnl: number;
  status: string;
  opened_at: string;
  closed_at: string | null;
  signal_id: string | null;
  wallet_address: string | null;
  auto: boolean;
  close_reason: string | null;
};

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers
    }
  });
  if (!response.ok) {
    throw new Error(`API error ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function getBotStatus() {
  return request<BotStatus>("/bot/status");
}

export function postBotAction(action: "start" | "pause" | "stop" | "kill-switch") {
  return request<{ status: BotStatus; message: string }>(`/bot/${action}`, { method: "POST" });
}

export function getBotLoopStatus() {
  return request<BotLoopStatus>("/bot/loop/status");
}

export function postBotMode(mode: "research" | "paper" | "off") {
  return request<Record<string, unknown>>(`/bot/mode/${mode}`, { method: "POST" });
}

export function startAudit() {
  return request<Record<string, unknown>>("/bot/audit/start", { method: "POST" });
}

export function stopAudit() {
  return request<Record<string, unknown>>("/bot/audit/stop", { method: "POST" });
}

export function runAuditOnce() {
  return request<Record<string, unknown>>("/bot/audit/run-once", { method: "POST" });
}

export type PollingStatus = {
  running: boolean;
  started_at: string | null;
  last_poll_at: string | null;
  wallets_polled_count: number;
  trades_detected_total: number;
  paper_trades_opened: number;
  polling_errors: number;
  session_trades_detected: number;
  session_paper_trades: number;
  session_errors: number;
  cohort_size: number;
  interval_seconds: number;
  rate_limit_calls_per_sec: number;
};

export function getPollingStatus() {
  return request<PollingStatus>("/bot/polling/status");
}

export function startPolling() {
  return request<PollingStatus>("/bot/polling/start", { method: "POST" });
}

export function stopPolling() {
  return request<PollingStatus>("/bot/polling/stop", { method: "POST" });
}

export function getMarkets() {
  return request<Market[]>("/markets");
}

export function getSignals() {
  return request<Signal[]>("/signals");
}

export function getSettings() {
  return request<AppSettings>("/settings");
}

export function getStorageStatus() {
  return request<StorageStatus>("/storage/status");
}

export function getEdgeReport() {
  return request<EdgeReport>("/edge/report");
}

export function getEdgeStrategies() {
  return request<Record<string, Record<string, number>>>("/edge/strategies");
}

export function getNoTradeLog(limit = 100) {
  return request<Array<{
    id: string;
    reason_code: string;
    market_id: string | null;
    signal_id: string | null;
    wallet_address: string | null;
    details: string | null;
    saved_loss_estimate: number;
    created_at: string;
  }>>(`/edge/no-trade-log?limit=${limit}`);
}

export function getSignalDecisions() {
  return request<Record<string, number>>("/signals/decisions");
}

export function getTradableMarkets() {
  return request<Market[]>("/markets/tradable");
}

export function getTopWallets(limit = 100) {
  return request<TopWallet[]>(`/wallets/top?limit=${limit}`);
}

export function getAuditedWallets(limit = 100) {
  return request<WalletAudit[]>(`/wallets/audited?limit=${limit}`);
}

export function getWalletDetail(address: string) {
  return request<Record<string, unknown>>(`/wallets/${address}`);
}

export function getWalletAudit(address: string) {
  return request<WalletAudit>(`/wallets/${address}/audit`);
}

export function runWalletAuditBatch(limit = 50) {
  return request<{ audited: number; results: Record<string, unknown>[] }>(`/wallets/audit/run-batch`, {
    method: "POST",
    body: JSON.stringify({ limit })
  });
}

export function getAuditedTrades(limit = 200) {
  return request<TradeAudit[]>(`/trades/audited?limit=${limit}`);
}

export function getTradeClusters(limit = 50) {
  return request<TradeCluster[]>(`/trades/clusters?limit=${limit}`);
}

export function getSmartMoneyEvents(limit = 100) {
  return request<SmartMoneyEvent[]>(`/trades/smart-money?limit=${limit}`);
}

export function runTradeAudit() {
  return request<{ audited: number }>(`/trades/audit/run`, {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function getPaperPositions() {
  return request<PaperPosition[]>("/paper/positions");
}

export function getPaperTrades() {
  return request<PaperPosition[]>("/paper/trades");
}

export function getPaperPerformance() {
  return request<PaperPerformance>("/paper/performance");
}

export function postWalletWatch(address: string, reason?: string) {
  return request<Record<string, string>>("/wallets/watchlist", {
    method: "POST",
    body: JSON.stringify({ address, reason })
  });
}

export function postWalletBlacklist(address: string, reason?: string) {
  return request<Record<string, string>>("/wallets/blacklist", {
    method: "POST",
    body: JSON.stringify({ address, reason })
  });
}

export function getDiscoveryAuditLatest() {
  return request<DiscoveryAuditReport | { conclusion: string; rationale?: string; csv_path: string; json_path: string }>(
    "/wallets/discovery/audit"
  );
}

export function runDiscoveryAudit(limit = 100, batchSize = 25) {
  return request<DiscoveryAuditReport>("/wallets/discovery/audit", {
    method: "POST",
    body: JSON.stringify({ limit, batch_size: batchSize })
  });
}

export function getDiscoveryExports() {
  return request<{
    exports_dir: string;
    csv_path: string;
    csv_exists: boolean;
    json_path: string;
    json_exists: boolean;
    latest_report_summary: Record<string, unknown> | null;
  }>("/wallets/discovery/export");
}

export function getWalletWinRate(address: string) {
  return request<WalletWinRate>(`/wallets/${address}/winrate`);
}

// ---------------- v0.5 market-first discovery ----------------

export type MarketFirstWallet = {
  address: string;
  market_first_score: number;
  composite_score: number;
  tier: string;
  status: string;
  resolved_market_win_rate: number | null;
  win_rate_confidence: string;
  resolved_markets_traded: number;
  resolved_winning_markets: number;
  resolved_losing_markets: number;
  unresolved_markets_count?: number;
  best_category: string | null;
  category_win_rates: Record<string, { wins: number; losses: number; sample: number; win_rate: number }>;
  recent_activity_score: number;
  copyability_score: number;
  total_resolved_notional: number;
  average_position_size: number;
  median_position_size: number;
  warnings: string[];
  reasons: string[];
  data_source: string;
};

export type MarketFirstReport = {
  started_at: string;
  finished_at: string;
  duration_ms: number;
  days_back: number;
  markets_scanned: number;
  markets_usable: number;
  markets_rejected: number;
  rejection_reasons: Record<string, number>;
  wallets_discovered: number;
  wallets_with_medium_high_confidence: number;
  tier_breakdown: Record<string, number>;
  status_breakdown: Record<string, number>;
  average_win_rate: number | null;
  median_win_rate: number | null;
  average_resolved_market_sample_size: number;
  api_errors: number;
  warnings: string[];
  conclusion: string;
  rationale: string;
  data_source: string;
  csv_paths: { wallets: string; markets: string; rejected: string };
  json_path: string;
  top_wallets: MarketFirstWallet[];
};

export function getMarketFirstStatus() {
  return request<{ available: boolean; exports: Record<string, unknown>; summary: Record<string, unknown> | null }>(
    "/discovery/market-first/status"
  );
}

export function getMarketFirstReport() {
  return request<MarketFirstReport | { available: false; message: string; exports: Record<string, unknown> }>(
    "/discovery/market-first/report"
  );
}

export function runMarketFirstDiscovery(daysBack = 90, maxMarkets = 100, tradesPerMarket = 1000) {
  return request<MarketFirstReport>("/discovery/market-first/run", {
    method: "POST",
    body: JSON.stringify({ days_back: daysBack, max_markets: maxMarkets, trades_per_market: tradesPerMarket })
  });
}

export function getMarketFirstTopWallets(limit = 50) {
  return request<MarketFirstWallet[]>(`/discovery/market-first/wallets?limit=${limit}`);
}

// ---------------- v0.5.1 candidate validation ----------------

export type CandidateRow = {
  address: string;
  previous_sample: number | null;
  previous_win_rate: number | null;
  expanded_sample: number;
  expanded_win_rate: number | null;
  discovery_sample: number;
  discovery_win_rate: number | null;
  validation_sample: number;
  validation_win_rate: number | null;
  best_category: string | null;
  category_concentration_share: number;
  market_correlation_warning: boolean;
  market_correlation_share: number;
  data_quality_warning: boolean;
  candidate_status: string;
  final_recommendation: string;
  warnings: string[];
};

export type ValidationReport = {
  started_at: string;
  duration_ms: number;
  days_back: number;
  max_markets: number;
  split_ratio: number;
  markets_scanned: number;
  markets_usable: number;
  discovery_markets: number;
  validation_markets: number;
  wallets_in_full: number;
  previous_candidates: number;
  expanded_kept: number;
  elite: number;
  strong: number;
  candidate_elite: number;
  biased_sample: number;
  failed_validation: number;
  dropped: number;
  survivor_bias_warning: boolean;
  survivor_bias_loser_market_share: number;
  short_window_crypto_dominance: boolean;
  short_window_crypto_share: number;
  api_errors: number;
  json_path: string;
  csv_path: string;
  rows: CandidateRow[];
  top_validated: CandidateRow[];
};

export function getValidationLatest() {
  return request<ValidationReport | { available: false; message: string; exports: Record<string, unknown> }>(
    "/discovery/market-first/validate"
  );
}

export function runValidation(daysBack = 365, maxMarkets = 300, splitRatio = 0.7) {
  return request<ValidationReport>("/discovery/market-first/validate", {
    method: "POST",
    body: JSON.stringify({ days_back: daysBack, max_markets: maxMarkets, split_ratio: splitRatio })
  });
}

// ---------------- v0.5.2 risk modes ----------------

export type RiskProfile = {
  name: "SAFE" | "AGGRESSIVE" | "FULL_PAPER";
  description: string;
  allowed_statuses: string[];
  min_sample_size: number;
  require_medium_high_confidence: boolean;
  max_risk_per_trade: number;
  max_wallet_exposure: number;
  max_market_exposure: number;
  max_total_exposure: number;
  max_open_positions: number | null;
  max_daily_trades: number | null;
  no_daily_trade_count_limit: boolean;
  require_copyable_edge: boolean;
  require_orderbook_quality: boolean;
  require_liquidity_check: boolean;
  require_spread_check: boolean;
  live_allowed: boolean;
  notes: string[];
};

export function getRiskMode() {
  return request<{ mode: string; live_allowed: boolean; available_modes: string[]; profile: RiskProfile }>("/risk/mode");
}

export function setRiskMode(mode: "SAFE" | "AGGRESSIVE" | "FULL_PAPER", by?: string) {
  return request<{ mode: string; updated_at: string; updated_by: string | null }>("/risk/mode", {
    method: "POST",
    body: JSON.stringify({ mode, by })
  });
}

export function getRiskProfiles() {
  return request<{ profiles: RiskProfile[]; active_default: string }>("/risk/profiles");
}

// ---------------- v0.5.4 validated paper universe ----------------

export type UniverseSummary = {
  available?: boolean;
  message?: string;
  generated_at?: string;
  sources?: string[];
  total_entries?: number;
  elite_count?: number;
  strong_count?: number;
  allowed_safe_count?: number;
  allowed_aggressive_count?: number;
  allowed_full_paper_count?: number;
  excluded_outlier_count?: number;
  excluded_biased_count?: number;
  excluded_failed_count?: number;
  excluded_candidate_count?: number;
  csv_path?: string;
  latest_csv_path?: string;
  json_path?: string;
  top_addresses?: string[];
};

export function getUniverseLatest() {
  return request<UniverseSummary>("/discovery/universe/latest");
}

export function mergeUniverse(suffix?: string) {
  return request<UniverseSummary>("/discovery/universe/merge", {
    method: "POST",
    body: JSON.stringify({ suffix })
  });
}

// ---------------- v0.7.8 Phase 8 — observability ----------------

export type LatencyPathStats = {
  p50: number;
  p95: number;
  max: number;
  n: number;
  breach: boolean;
  budget_ms: number | null;
  ratio: number | null;
};

export type LatencyStatus = {
  paths: Record<string, LatencyPathStats>;
  budgets: Record<string, number>;
};

export type SchedulerStatus = {
  registered_positions: number;
  heap_size: number;
  bucket_intervals_s: Record<string, number>;
};

export type ResolverStatus = {
  static_cache_size: number;
  dynamic_cache_size: number;
  not_found_blacklist_size: number;
  ttl: { static_s: number; dynamic_s: number; not_found_s: number };
};

export function getLatency() {
  return request<LatencyStatus>("/observability/latency");
}

export function getLatencyReport() {
  return request<{ report_md: string }>("/observability/latency/report");
}

export function getScheduler() {
  return request<SchedulerStatus>("/observability/scheduler");
}

export function getResolverStats() {
  return request<ResolverStatus>("/observability/resolver");
}

export function killSwitchFlatten() {
  return request<{ closed_count: number; closed_ids: string[]; message: string }>(
    "/observability/kill-switch-flatten",
    { method: "POST" }
  );
}

// v0.7.8 P6 — Manual weekly reclass + promotion candidates

export type ReclassDecisionEntry = {
  address: string;
  previous: string | null;
  new: string;
  reason: string;
};

export type ReclassResult = {
  started_at: string;
  finished_at: string;
  cohort_before: number;
  cohort_after: number;
  promoted_count: number;
  demoted_count: number;
  unchanged: number;
  rolled_back: boolean;
  errors: string[];
  db_backup_path: string | null;
  summary_md: string;
  promoted: ReclassDecisionEntry[];
  demoted: ReclassDecisionEntry[];
};

export type PromotionCandidate = {
  address: string;
  wins: number;
  losses: number;
  wins_plus_losses: number;
  win_rate: number;
  trades_to_elite: number;
};

export type PromotionCandidatesResponse = {
  count: number;
  candidates: PromotionCandidate[];
};

export function runReclass(dryRun: boolean = false) {
  return request<ReclassResult>("/discovery/reclass/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  });
}

export function getPromotionCandidates() {
  return request<PromotionCandidatesResponse>("/discovery/reclass/promotion-candidates");
}
