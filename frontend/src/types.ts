export type Mode = "DEMO" | "PAPER";

export interface BrokerErrorInfo {
  kind: string;
  message: string;
  status_code?: number | null;
  hint?: string | null;
  label?: string;
}

export interface ConnectionInfo {
  mode: Mode;
  connection: "unknown" | "ok" | "disconnected" | "error";
  reconciled: boolean;
  state_clear: boolean;
  issues: string[];
  last_ok_at: string | null;
  last_attempt_at: string | null;
  last_error: BrokerErrorInfo | null;
  last_error_at: string | null;
}

export interface ScalperSymbolView {
  status?: string;
  quote?: { bid: number; ask: number; spread_pct: number; at: string; fresh: boolean; source: string; via?: "stream" | "poll" };
  bars_ready?: number;
  bars_needed?: number;
  last_eval?: { bar_time: string; close: number; sma_short: number | null; sma_long: number | null; signal: string };
  trade?: {
    id: number;
    qty: number;
    entry_price: number;
    stop_price: number;
    target_price: number;
    deadline: string;
    entry_time: string;
    broker_stop?: boolean;
    bid?: number;
    pnl_pct?: number;
  };
}

export interface ScalperState {
  status?: string;
  feed?: "stream" | "poll" | "mixed";
  market_open?: boolean | null;
  symbols?: Record<string, ScalperSymbolView>;
}

export interface BotState {
  running: boolean;
  mode: Mode | null;
  last_tick_at: string | null;
  last_status: string;
  strategy: "scalp" | "sma" | null;
  scalper: ScalperState | null;
  pause_meaning: string;
}

export interface Status {
  mode: Mode;
  server_time: string;
  orders_enabled: boolean;
  bot: BotState;
  connection: ConnectionInfo;
  market: { is_open: boolean; next_open: string | null; next_close: string | null; simulated: boolean } | null;
  data_source: string;
  resume_after_restart: boolean;
  notify_configured: boolean;
  streams: StreamsInfo;
}

export interface StreamState {
  state: "off" | "connecting" | "live" | "reconnecting" | "unavailable";
  url: string;
  connected_since: string | null;
  last_message_at: string | null;
  last_error: string | null;
  retry_at: string | null;
  connects: number;
  failures: number;
}

export interface StreamsInfo {
  enabled: boolean;
  available: boolean;
  note: string;
  market: StreamState & { symbols: string[]; subscribed: string[] };
  orders: StreamState & { events: number; last_event: string | null };
}

export interface Account {
  status: string;
  currency: string;
  cash: number;
  equity: number;
  last_equity: number | null;
  buying_power: number | null;
  non_marginable_buying_power: number | null;
  trading_blocked: boolean;
  account_blocked: boolean;
  trade_suspended_by_user: boolean;
}

export interface Position {
  symbol: string;
  qty: number;
  qty_available: number;
  side: string;
  avg_entry_price: number;
  market_value: number;
  cost_basis: number;
  unrealized_pl: number;
  current_price: number | null;
}

export interface OrderRow {
  broker_order_id: string | null;
  client_order_id: string;
  symbol: string;
  side: "buy" | "sell";
  type: string;
  time_in_force: string;
  qty: number | null;
  notional: number | null;
  filled_qty: number;
  filled_avg_price: number | null;
  status: string;
  source: string;
  created_at: string | null;
  filled_at: string | null;
  updated_at: string | null;
  cancelable: boolean;
  message: string | null;
  from_broker: boolean;
}

export interface DailyRisk {
  day: string;
  data_ok: boolean;
  reason?: string;
  baseline?: number;
  baseline_source?: string;
  equity?: number;
  pnl?: number;
  limit: number;
  locked: boolean;
  locked_at?: string | null;
  reset_rule: string;
}

export interface RiskLimits {
  max_order_value: number;
  max_total_invested: number;
  max_open_positions: number;
  max_orders_per_day: number;
  max_buys_per_hour: number;
  max_buys_per_day: number;
  max_daily_loss: number;
  loss_streak_limit: number;
  loss_pause_min: number;
  max_spread_pct: number;
  price_buffer_pct: number;
}

export interface BotParams {
  strategy: "scalp" | "sma";
  symbol: string;
  symbols: string[];
  broker_stop: boolean;
  stop_limit_offset_pct: number;
  stock_stop_loss_pct: number;
  stock_take_profit_pct: number;
  stock_max_hold_min: number;
  stock_fee_pct: number;
  crypto_outside_session_only: boolean;
  trend_filter: boolean;
  trend_minutes: number;
  short_window: number;
  long_window: number;
  order_value: number;
  max_bar_age_s: number;
  bar_seconds: number;
  poll_interval_s: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  max_hold_min: number;
  fee_pct: number;
}

export interface Trade {
  id: number;
  symbol: string;
  status: "entering" | "open" | "exiting" | "closed" | "canceled";
  entry_time: string | null;
  entry_qty: number | null;
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  exit_time: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  gross_pnl: number | null;
  fees_est: number | null;
  net_pnl: number | null;
  note: string | null;
  stop_cid?: string | null;
  created_at: string;
}

export interface TradeSummary {
  trades_today: number;
  wins_today: number;
  losses_today: number;
  net_today: number;
  loss_pause: { until: string; reason: string } | null;
}

export interface Dashboard {
  mode: Mode;
  connection: ConnectionInfo;
  account: Account | null;
  positions: Position[];
  orders: OrderRow[];
  fetched_at: string | null;
  daily: DailyRisk;
  orders_today: number;
  limits: RiskLimits;
}

export interface BarDto {
  t: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export type Timeframe = "1Min" | "5Min" | "15Min" | "1Hour" | "1Day";

export interface BarsResponse {
  mode: Mode;
  symbol: string;
  timeframe: Timeframe;
  asset_class: "crypto" | "us_equity";
  source: string;
  feed: string;
  fetched_at: string | null;
  cached: boolean;
  bars: BarDto[];
  last_bar_at: string | null;
  last_complete_at: string | null;
  status: "ok" | "stale" | "empty" | "error" | "market_closed";
  message: string;
  error: BrokerErrorInfo | null;
  market_open: boolean | null;
  age_s?: number | null;
  strategy?: { short_window: number; long_window: number; session_open: string | null; session_filter: boolean };
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
  blocking: boolean;
}

export interface OrderPreview {
  mode: Mode;
  symbol: string;
  side: "buy" | "sell";
  type: string;
  qty: string | null;
  notional: string | null;
  time_in_force: string;
  currency: string | null;
  est_price: number | null;
  est_price_source: string | null;
  est_price_at: string | null;
  est_value: number | null;
  asset_name: string | null;
  market_open: boolean | null;
  checks: Check[];
  warnings: string[];
  allowed: boolean;
}

export interface Decision {
  id: number;
  ts: string;
  symbol: string;
  bar_time: string | null;
  close: number | null;
  sma_short: number | null;
  sma_long: number | null;
  signal: "buy" | "sell" | "none";
  action: string;
  reason: string;
}

export interface BotInfo {
  mode: Mode;
  state: BotState;
  params: BotParams;
  roundtrip_cost_pct: number;
  defaults: { scalp: BotParams; sma: BotParams };
  trades: Trade[];
  summary: TradeSummary;
  decisions: Decision[];
  disclaimer: string;
}

export interface EmergencyResult {
  mode: Mode;
  bot_paused: boolean;
  canceled: { id: string; symbol: string; ok: boolean; error?: string }[];
  closed: { symbol: string; status: string }[];
  not_closed: { symbol: string; reason: string }[];
  cancel_error?: string;
  error?: string;
}

export interface EventRow {
  id: number;
  ts: string;
  level: "info" | "warning" | "error";
  category: string;
  message: string;
}

export interface ConnectionTest {
  ok: boolean;
  mode: "PAPER";
  host: string;
  auth_mode: string | null;
  checked_at: string;
  account_status?: string;
  currency?: string;
  cash?: number;
  equity?: number;
  error?: BrokerErrorInfo;
}

export interface SymbolSuggestion {
  symbol: string;
  name: string;
  asset_class: "crypto" | "us_equity";
  group: string;
}

export interface SymbolsResponse {
  mode: Mode;
  query: string;
  items: SymbolSuggestion[];
  note: string | null;
}

export interface BacktestRequest {
  symbol: string;
  strategy: "scalp" | "sma";
  days: number;
  short_window: number;
  long_window: number;
  order_value: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  max_hold_min: number;
  fee_pct: number;
  spread_pct: number;
  apply_risk_limits: boolean;
  trend_filter: boolean;
  trend_minutes: number;
}

export interface BacktestMetrics {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  gross_pnl: number;
  fees: number;
  net_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  max_drawdown: number;
  avg_hold_min: number | null;
  exposure_pct: number | null;
  exit_reasons: Record<string, number>;
  blocked_entries: Record<string, number>;
  buy_hold_pct: number | null;
  net_pct_of_order_value: number;
}

export interface BacktestTrade {
  entry_time: string;
  entry_price: number;
  qty: number;
  exit_time: string;
  exit_price: number;
  reason: string;
  hold_min: number;
  gross: number;
  fees: number;
  net: number;
}

export interface BacktestResult {
  id: number;
  mode: Mode;
  request: BacktestRequest;
  created_at: string;
  period: { start: string; end: string };
  source: string;
  assumptions: string[];
  metrics: BacktestMetrics;
  trades: BacktestTrade[];
  equity: { t: string; equity: number }[];
  bars: BarDto[];
  display_timeframe_min: number;
  bar_count: number;
  warning?: string;
}

export interface BacktestRun {
  id: number;
  created_at: string;
  symbol: string;
  strategy: "scalp" | "sma";
  request: BacktestRequest;
  metrics: BacktestMetrics;
}

export interface NotifySettings {
  enabled: boolean;
  paper_only: boolean;
  trades: boolean;
  warnings: boolean;
  bot: boolean;
  daily_summary: boolean;
  summary_time: string;
}

export interface NotifyInfo {
  configured: boolean;
  server: string | null;
  token_set: boolean;
  config_error: string | null;
  timezone: string;
  settings: NotifySettings;
  sent_total: number;
  dropped: number;
  last_sent_at: string | null;
  last_error: string | null;
}

export interface ResumeInfo {
  enabled: boolean;
  last_run: { mode: Mode | null; bot: boolean; updated_at: string | null };
  status: string | null;
  pending: boolean;
}
