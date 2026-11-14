import { apiFetch } from '../lib/endpoints';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import ReactECharts from 'echarts-for-react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Layers,
  Loader2,
  Play,
  RefreshCw,
  Scale,
  TrendingDown,
} from 'lucide-react';
import { useI18n } from '../i18n';


type Metrics = Record<string, number>;

type FactorSummary = {
  factor_id: string;
  miner_type: string;
  lifecycle_status: string;
  metrics: Metrics;
  display: string;
  logic_kind: string;
  snapshot_available?: boolean;
};

type RunSummary = {
  run_id: string;
  status: string;
  created_at?: string;
  finished_at?: string;
  error?: string | null;
  n_factors?: number;
  scope?: string;
  total_return_pct?: number;
  sharpe?: number;
  max_drawdown_pct?: number;
};

type RunDetail = RunSummary & {
  summary?: Record<string, any> | null;
  equity?: {
    timestamps: string[];
    equity: number[];
    cumulative_return?: number[] | null;
    drawdown: number[];
    points: number;
    total_points: number;
    downsampled: boolean;
  };
  request?: Record<string, any>;
};

type WeightMode = 'equal' | 'ic' | 'custom';

const DEFAULTS = {
  start: '',
  end: '',
  commission: '0.0004',
  slippage: '0.0005',
  funding: '0.0001',
  rebalance: '5',
  nLong: '5',
  nShort: '5',
  cash: '1000000',
};

function fmt(value?: number, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function pct(value?: number) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}%` : '—';
}

function money(value?: number) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toFixed(0);
}

const STATUS_STYLES: Record<string, string> = {
  queued: 'bg-slate-700/40 text-slate-300 border-slate-600',
  running: 'bg-sky-900/40 text-sky-300 border-sky-700',
  completed: 'bg-emerald-900/40 text-emerald-300 border-emerald-700',
  failed: 'bg-rose-900/40 text-rose-300 border-rose-700',
};

export function Backtest() {
  const { t } = useI18n();
  const [factors, setFactors] = useState<FactorSummary[]>([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [weightMode, setWeightMode] = useState<WeightMode>('equal');
  const [customWeights, setCustomWeights] = useState<Record<string, string>>({});
  const [params, setParams] = useState({ ...DEFAULTS });
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [active, setActive] = useState<RunDetail | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingCatalog, setLoadingCatalog] = useState(true);

  const loadCatalog = useCallback(async () => {
    setLoadingCatalog(true);
    try {
      const response = await apiFetch(`/api/factors?limit=500&sort_by=created_at`);
      const payload = await response.json();
      setFactors(payload.factors || []);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoadingCatalog(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const response = await apiFetch(`/api/backtest/runs?limit=20`);
      const payload = await response.json();
      setRuns(payload.runs || []);
    } catch {
      /* transient: the engine may be starting */
    }
  }, []);

  useEffect(() => {
    loadCatalog();
    loadRuns();
  }, [loadCatalog, loadRuns]);

  const openRun = useCallback(async (runId: string) => {
    try {
      const response = await apiFetch(`/api/backtest/runs/${encodeURIComponent(runId)}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || response.statusText);
      setActive(payload);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  // Poll while a run is queued or running.
  useEffect(() => {
    if (!active || !['queued', 'running'].includes(active.status)) return;
    const timer = window.setTimeout(async () => {
      await openRun(active.run_id);
      await loadRuns();
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [active, openRun, loadRuns]);

  const usable = useMemo(
    () => factors.filter((factor) => factor.snapshot_available !== false),
    [factors],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return usable;
    return usable.filter((factor) =>
      [factor.factor_id, factor.display, factor.miner_type, factor.lifecycle_status]
        .join(' ')
        .toLowerCase()
        .includes(needle),
    );
  }, [usable, query]);

  const toggle = (factorId: string) => {
    setSelected((current) =>
      current.includes(factorId)
        ? current.filter((id) => id !== factorId)
        : [...current, factorId],
    );
  };

  const resolveWeights = (): Record<string, number> | null => {
    if (weightMode === 'equal') return null;
    const byId = new Map(factors.map((factor) => [factor.factor_id, factor]));
    const weights: Record<string, number> = {};
    for (const factorId of selected) {
      if (weightMode === 'custom') {
        const raw = Number(customWeights[factorId]);
        weights[factorId] = Number.isFinite(raw) ? raw : 1;
      } else {
        const metrics = byId.get(factorId)?.metrics || {};
        const ic = Number(metrics.RankIC ?? metrics.IC ?? 0);
        weights[factorId] = Math.abs(Number.isFinite(ic) ? ic : 0);
      }
    }
    const total = Object.values(weights).reduce((acc, value) => acc + Math.abs(value), 0);
    if (total <= 0) return null;
    // Normalise on the client so the payload is readable in the run history.
    return Object.fromEntries(
      Object.entries(weights).map(([key, value]) => [key, Number((value / total).toFixed(6))]),
    );
  };

  const submit = async () => {
    if (!selected.length) {
      setError(t('bt.needFactors'));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        factor_ids: selected,
        commission_rate: Number(params.commission),
        slippage_rate: Number(params.slippage),
        funding_rate: Number(params.funding),
        rebalance_every: Number(params.rebalance),
        n_long: Number(params.nLong),
        n_short: Number(params.nShort),
        initial_cash: Number(params.cash),
      };
      if (params.start) body.start = params.start;
      if (params.end) body.end = params.end;
      const weights = resolveWeights();
      if (weights) body.weights = weights;

      const response = await apiFetch(`/api/backtest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || response.statusText);
      setActive({ ...payload, request: body });
      await loadRuns();
    } catch (err) {
      setError(String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const chartOption = useMemo(() => {
    const equity = active?.equity;
    if (!equity || !equity.timestamps?.length) return null;
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { top: 6, textStyle: { color: '#cbd5e1' } },
      grid: { top: 44, left: 68, right: 62, bottom: 36 },
      xAxis: {
        type: 'category',
        data: equity.timestamps,
        axisLabel: { color: '#94a3b8', hideOverlap: true },
        axisLine: { lineStyle: { color: '#334155' } },
      },
      yAxis: [
        {
          type: 'value',
          name: t('bt.equityAxis'),
          nameTextStyle: { color: '#94a3b8' },
          axisLabel: { color: '#94a3b8', formatter: (value: number) => money(value) },
          splitLine: { lineStyle: { color: '#1e293b' } },
          scale: true,
        },
        {
          type: 'value',
          name: t('bt.drawdownAxis'),
          nameTextStyle: { color: '#94a3b8' },
          axisLabel: { color: '#94a3b8', formatter: (value: number) => `${(value * 100).toFixed(1)}%` },
          splitLine: { show: false },
          max: 0,
        },
      ],
      series: [
        {
          name: t('bt.equityAxis'),
          type: 'line',
          showSymbol: false,
          smooth: true,
          data: equity.equity,
          lineStyle: { width: 2, color: '#38bdf8' },
          itemStyle: { color: '#38bdf8' },
          areaStyle: { color: 'rgba(56,189,248,0.12)' },
        },
        {
          name: t('bt.drawdownAxis'),
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          data: equity.drawdown,
          lineStyle: { width: 1, color: '#fb7185' },
          itemStyle: { color: '#fb7185' },
          areaStyle: { color: 'rgba(251,113,133,0.16)' },
        },
      ],
    };
  }, [active, t]);

  const summary = active?.summary || {};
  const effectiveWeights: Record<string, number> = summary.effective_weights || {};
  const expanded: Record<string, any> = summary.expanded_combinations || {};
  const metricCards = [
    { label: t('bt.totalReturn'), value: pct(summary.total_return_pct), icon: TrendingDown },
    { label: t('bt.maxDrawdown'), value: pct(summary.max_drawdown_pct), icon: TrendingDown },
    { label: t('bt.sharpe'), value: fmt(summary.sharpe, 2), icon: Activity },
    { label: t('bt.cagr'), value: pct(summary.cagr), icon: Activity },
    { label: t('bt.volatility'), value: pct(summary.volatility), icon: Activity },
    { label: t('bt.trades'), value: fmt(summary.num_trades, 0), icon: Layers },
    { label: t('bt.costs'), value: money(summary.total_costs), icon: Scale },
    { label: t('bt.turnover'), value: fmt(summary.avg_turnover, 4), icon: RefreshCw },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">{t('bt.kicker')}</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight">{t('bt.title')}</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t('bt.subtitle')}</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => { loadCatalog(); loadRuns(); }}
            className="flex items-center gap-2 rounded-md border border-border bg-secondary/40 px-3 py-2 text-xs font-medium hover:bg-secondary"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loadingCatalog ? 'animate-spin' : ''}`} />
            {t('common.refresh')}
          </button>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="break-all">{error}</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
        {/* ---------------- factor picker ---------------- */}
        <div className="rounded-xl border border-border bg-card/60 p-4">
          <div className="flex items-center gap-2">
            <Layers className="h-4 w-4 text-sky-400" />
            <h2 className="text-sm font-bold">{t('bt.selectFactors')}</h2>
            <span className="ml-auto text-xs text-muted-foreground">
              {t('bt.selected', { count: selected.length })}
            </span>
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">{t('bt.snapshotOnly')}</p>

          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('bt.searchFactors')}
            className="mt-3 w-full rounded-md border border-border bg-background/60 px-3 py-2 text-xs outline-none focus:border-primary"
          />

          <div className="mt-2 flex gap-2 text-[11px]">
            <button
              onClick={() => setSelected(usable.slice(0, 12).map((factor) => factor.factor_id))}
              className="rounded border border-border px-2 py-1 hover:bg-secondary"
            >
              {t('bt.selectAll')}
            </button>
            <button
              onClick={() => setSelected([])}
              className="rounded border border-border px-2 py-1 hover:bg-secondary"
            >
              {t('bt.clear')}
            </button>
          </div>

          <div className="mt-3 max-h-[420px] space-y-1 overflow-auto pr-1">
            {filtered.map((factor) => {
              const checked = selected.includes(factor.factor_id);
              return (
                <label
                  key={factor.factor_id}
                  className={`flex cursor-pointer items-start gap-2 rounded-md border p-2 text-xs transition-colors ${
                    checked
                      ? 'border-primary/60 bg-primary/10'
                      : 'border-transparent hover:border-border hover:bg-secondary/40'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(factor.factor_id)}
                    className="mt-0.5 h-3.5 w-3.5 accent-sky-500"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-[11px] text-foreground">{factor.factor_id}</span>
                    <span className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-muted-foreground">
                      <span>{factor.miner_type}</span>
                      <span>{factor.lifecycle_status}</span>
                      <span>IC {fmt(factor.metrics?.RankIC ?? factor.metrics?.IC, 4)}</span>
                    </span>
                  </span>
                </label>
              );
            })}
            {!filtered.length && (
              <p className="p-3 text-xs text-muted-foreground">{t('inspector.noMatches')}</p>
            )}
          </div>
        </div>

        {/* ---------------- parameters + result ---------------- */}
        <div className="space-y-5">
          <div className="rounded-xl border border-border bg-card/60 p-5">
            <div className="flex items-center gap-2">
              <Scale className="h-4 w-4 text-emerald-400" />
              <h2 className="text-sm font-bold">{t('bt.parameters')}</h2>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
              {[
                { key: 'start', label: t('bt.start'), type: 'date' },
                { key: 'end', label: t('bt.end'), type: 'date' },
                { key: 'commission', label: t('bt.commission'), type: 'number' },
                { key: 'slippage', label: t('bt.slippage'), type: 'number' },
                { key: 'funding', label: t('bt.funding'), type: 'number' },
                { key: 'rebalance', label: t('bt.rebalance'), type: 'number' },
                { key: 'nLong', label: t('bt.long'), type: 'number' },
                { key: 'nShort', label: t('bt.short'), type: 'number' },
                { key: 'cash', label: t('bt.cash'), type: 'number' },
              ].map((field) => (
                <label key={field.key} className="text-[11px] text-muted-foreground">
                  {field.label}
                  <input
                    type={field.type}
                    value={(params as Record<string, string>)[field.key]}
                    onChange={(event) =>
                      setParams((current) => ({ ...current, [field.key]: event.target.value }))
                    }
                    className="mt-1 w-full rounded-md border border-border bg-background/60 px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary"
                  />
                </label>
              ))}
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <span className="text-[11px] text-muted-foreground">{t('bt.weighting')}</span>
              <div className="flex overflow-hidden rounded-md border border-border">
                {([
                  ['equal', t('bt.weightEqual')],
                  ['ic', t('bt.weightIc')],
                  ['custom', t('bt.weightCustom')],
                ] as [WeightMode, string][]).map(([mode, label]) => (
                  <button
                    key={mode}
                    onClick={() => setWeightMode(mode)}
                    className={`px-3 py-1.5 text-xs ${
                      weightMode === mode ? 'bg-primary/20 text-primary' : 'hover:bg-secondary'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="text-[10px] text-muted-foreground">{t('bt.weightHint')}</span>
              <button
                onClick={submit}
                disabled={submitting}
                className="ml-auto flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-xs font-bold text-primary-foreground hover:opacity-90 disabled:opacity-50"
              >
                {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                {submitting ? t('bt.running') : t('bt.run')}
              </button>
            </div>

            {weightMode === 'custom' && selected.length > 0 && (
              <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-3">
                {selected.map((factorId) => (
                  <label key={factorId} className="text-[10px] text-muted-foreground">
                    <span className="font-mono">{factorId}</span>
                    <input
                      type="number"
                      step="0.1"
                      value={customWeights[factorId] ?? '1'}
                      onChange={(event) =>
                        setCustomWeights((current) => ({ ...current, [factorId]: event.target.value }))
                      }
                      className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 text-xs text-foreground outline-none focus:border-primary"
                    />
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* result */}
          <div className="rounded-xl border border-border bg-card/60 p-5">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-violet-400" />
              <h2 className="text-sm font-bold">{t('bt.result')}</h2>
              {active && (
                <span className={`ml-auto rounded border px-2 py-0.5 text-[10px] uppercase ${STATUS_STYLES[active.status] || STATUS_STYLES.queued}`}>
                  {active.status}
                </span>
              )}
            </div>

            {!active && <p className="mt-4 text-xs text-muted-foreground">{t('bt.empty')}</p>}

            {active && ['queued', 'running'].includes(active.status) && (
              <div className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t('bt.running')} — {active.run_id}
              </div>
            )}

            {active?.status === 'failed' && (
              <div className="mt-4 rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-200">
                <p className="font-bold">{t('bt.failed')}</p>
                <p className="mt-1 break-all">{active.error}</p>
              </div>
            )}

            {active?.status === 'completed' && (
              <div className="mt-4 space-y-4">
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  {metricCards.map((card) => (
                    <div key={card.label} className="rounded-lg border border-border bg-secondary/10 p-3">
                      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{card.label}</p>
                      <p className="mt-1 text-lg font-semibold">{card.value}</p>
                    </div>
                  ))}
                </div>

                {chartOption ? (
                  <div className="h-72 rounded-lg border border-border bg-background/30 p-2">
                    <ReactECharts option={chartOption} style={{ height: '100%', width: '100%' }} />
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">{t('bt.empty')}</p>
                )}

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <div className="rounded-lg border border-border bg-secondary/10 p-3">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{t('bt.scope')}</p>
                    <p className="mt-1 font-mono text-xs">{summary.scope || '—'}</p>
                    <p className="mt-2 text-[10px] uppercase tracking-wide text-muted-foreground">{t('bt.assets')}</p>
                    <p className="mt-1 text-xs">{summary.n_assets ?? '—'}</p>
                  </div>
                  <div className="rounded-lg border border-border bg-secondary/10 p-3">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{t('bt.effectiveWeights')}</p>
                    <div className="mt-1 space-y-1">
                      {Object.entries(effectiveWeights).length ? (
                        Object.entries(effectiveWeights).map(([factorId, weight]) => (
                          <div key={factorId} className="flex items-center justify-between gap-2 text-[11px]">
                            <Link
                              to={`/inspector?factor=${encodeURIComponent(factorId)}`}
                              className="font-mono text-primary hover:underline"
                            >
                              {factorId}
                            </Link>
                            <span className="font-mono">{Number(weight).toFixed(4)}</span>
                          </div>
                        ))
                      ) : (
                        <p className="text-xs text-muted-foreground">{t('common.none')}</p>
                      )}
                    </div>
                    {Object.keys(expanded).length > 0 && (
                      <p className="mt-2 text-[10px] text-muted-foreground">
                        {t('bt.expanded')}: {Object.keys(expanded).join(', ')}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* history */}
          <div className="rounded-xl border border-border bg-card/60 p-5">
            <div className="flex items-center gap-2">
              <Clock className="h-4 w-4 text-amber-400" />
              <h2 className="text-sm font-bold">{t('bt.history')}</h2>
              <span className="ml-auto text-xs text-muted-foreground">{runs.length}</span>
            </div>
            {!runs.length && <p className="mt-3 text-xs text-muted-foreground">{t('bt.noHistory')}</p>}
            <div className="mt-3 space-y-1">
              {runs.map((run) => (
                <button
                  key={run.run_id}
                  onClick={() => openRun(run.run_id)}
                  className={`flex w-full items-center gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                    active?.run_id === run.run_id
                      ? 'border-primary/60 bg-primary/10'
                      : 'border-transparent hover:border-border hover:bg-secondary/40'
                  }`}
                >
                  <CheckCircle2 className={`h-3.5 w-3.5 shrink-0 ${run.status === 'completed' ? 'text-emerald-400' : run.status === 'failed' ? 'text-rose-400' : 'text-sky-400'}`} />
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-[11px]">{run.run_id}</span>
                    <span className="text-[10px] text-muted-foreground">
                      {run.scope || '—'} · {run.n_factors ?? '—'} factors
                    </span>
                  </span>
                  <span className="shrink-0 text-right font-mono text-[11px]">
                    <span className={Number(run.total_return_pct) >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                      {pct(run.total_return_pct)}
                    </span>
                    <span className="ml-2 text-muted-foreground">SR {fmt(run.sharpe, 2)}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
