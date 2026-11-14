import { apiFetch } from '../lib/endpoints';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Clock, Database } from 'lucide-react';
import { useI18n } from '../i18n';


type FreshnessItem = {
  symbol: string;
  exchange: string;
  trade_type: string;
  timeframe: string;
  start: string;
  last: string;
  rows: number;
  lag_days?: number;
  stale?: boolean;
};

type ExperimentProgress = {
  label: string;
  start: string;
  min_bars: number;
  timeframe: string;
  accumulated: number;
  remaining: number;
  coverage: number;
  ready: boolean;
  eta: string | null;
  as_of?: string;
};

type FreshnessPayload = {
  generated_at: string;
  summary: {
    files: number;
    assets_1d: number;
    stale: number;
    latest: string | null;
    stale_symbols: string[];
    timeframes: Record<string, number>;
    stale_by_timeframe: Record<string, number>;
    streams?: number;
    unreadable?: { count: number; paths: string[]; details: { path: string; reason: string }[] };
  };
  items: FreshnessItem[];
  experiment: ExperimentProgress | null;
};

export function FreshnessPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const { t } = useI18n();
  const [payload, setPayload] = useState<FreshnessPayload | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await apiFetch('/api/data/freshness');
      if (!response.ok) throw new Error(response.statusText);
      setPayload(await response.json());
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const grouped = useMemo(() => {
    const buckets: Record<string, FreshnessItem[]> = {};
    for (const item of payload?.items || []) {
      (buckets[item.timeframe] ||= []).push(item);
    }
    return Object.entries(buckets).sort((a, b) => b[1].length - a[1].length);
  }, [payload]);

  if (error) {
    return (
      <div className="rounded-xl border border-rose-900/60 bg-rose-950/20 p-4 text-xs text-rose-300">
        {t('data.freshnessUnavailable')}: {error}
      </div>
    );
  }

  if (!payload) {
    return (
      <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
        {t('common.loading')}
      </div>
    );
  }

  const { summary, experiment } = payload;
  const stale = summary.stale > 0;

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-center gap-3">
        <Database className="h-4 w-4 text-sky-400" />
        <h2 className="text-sm font-bold">{t('data.freshness')}</h2>
        <span className="text-[11px] text-muted-foreground">{payload.generated_at.replace('T', ' ')}</span>

        <div className="ml-auto flex flex-wrap items-center gap-4 text-[11px]">
          <span className="text-muted-foreground">
            {t('data.files')} <span className="font-mono text-foreground">{summary.files}</span>
          </span>
          <span className="text-muted-foreground">
            {t('data.assets1d')} <span className="font-mono text-foreground">{summary.assets_1d}</span>
          </span>
          {(summary.streams ?? 0) > 0 && (
            <span className="text-muted-foreground">
              {t('data.streams')} <span className="font-mono text-foreground">{summary.streams}</span>
            </span>
          )}
          <span className="text-muted-foreground">
            {t('data.latest')} <span className="font-mono text-foreground">{summary.latest || '—'}</span>
          </span>
          <span className={`flex items-center gap-1 ${stale ? 'text-rose-400' : 'text-emerald-400'}`}>
            {stale ? <AlertTriangle className="h-3.5 w-3.5" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
            {stale ? `${summary.stale} ${t('data.stale')}` : t('data.allFresh')}
          </span>
          <button
            onClick={() => setOpen((value) => !value)}
            className="flex items-center gap-1 rounded border border-border px-2 py-1 hover:bg-secondary"
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {t('data.details')}
          </button>
        </div>
      </div>

      {(summary.unreadable?.count ?? 0) > 0 && (
        <div className="mt-3 rounded-lg border border-rose-900/60 bg-rose-950/20 p-3 text-[11px] text-rose-300">
          <div className="flex items-center gap-1 font-medium">
            <AlertTriangle className="h-3.5 w-3.5" />
            {t('data.unreadable', { count: summary.unreadable?.count ?? 0 })}
          </div>
          <ul className="mt-1 space-y-0.5 font-mono text-[10px] text-rose-300/80">
            {(summary.unreadable?.details || []).slice(0, 3).map((entry) => (
              <li key={entry.path} className="truncate">
                {entry.path} — {entry.reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {stale && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-rose-400/90">
          <span>
            {t('data.staleList', {
              symbols:
                summary.stale_symbols.slice(0, 8).join(', ') +
                (summary.stale_symbols.length > 8 ? ` +${summary.stale_symbols.length - 8}` : ''),
            })}
          </span>
          {Object.entries(summary.stale_by_timeframe || {})
            .sort((a, b) => b[1] - a[1])
            .map(([timeframe, count]) => (
              <span key={timeframe} className="rounded bg-rose-950/40 px-1.5 py-0.5 font-mono">
                {timeframe}×{count}
              </span>
            ))}
        </div>
      )}

      {experiment && (
        <div className="mt-4 rounded-lg border border-border bg-secondary/10 p-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Clock className="h-3.5 w-3.5 text-amber-400" />
            <span className="font-medium">{experiment.label}</span>
            <span className="text-muted-foreground">
              {experiment.start} · {experiment.timeframe}
            </span>
            <span className={`ml-auto font-mono ${experiment.ready ? 'text-emerald-400' : 'text-amber-300'}`}>
              {experiment.accumulated} / {experiment.min_bars}
            </span>
          </div>
          <div className="mt-2 h-1.5 w-full rounded-full bg-secondary">
            <div
              className={`h-1.5 rounded-full transition-all ${experiment.ready ? 'bg-emerald-500' : 'bg-amber-500'}`}
              style={{ width: `${Math.round((experiment.coverage || 0) * 100)}%` }}
            />
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            {experiment.ready
              ? t('data.experimentReady')
              : t('data.experimentPending', { remaining: experiment.remaining, eta: experiment.eta || '—' })}
          </p>
        </div>
      )}

      {open && (
        <div className="mt-4 space-y-4">
          {grouped.map(([timeframe, items]) => (
            <div key={timeframe}>
              <p className="mb-1 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                <span>
                  {timeframe} · {items.length}
                </span>
                {items.some((item) => item.stale) && (
                  <span className="flex items-center gap-1 text-rose-400">
                    <AlertTriangle className="h-3 w-3" />
                    {items.filter((item) => item.stale).length} {t('data.stale')}
                  </span>
                )}
              </p>
              <div className="max-h-64 overflow-auto rounded-lg border border-border">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-card">
                    <tr className="text-left text-muted-foreground">
                      <th className="px-3 py-1.5 font-medium">{t('data.symbol')}</th>
                      <th className="px-3 py-1.5 font-medium">{t('data.first')}</th>
                      <th className="px-3 py-1.5 font-medium">{t('data.last')}</th>
                      <th className="px-3 py-1.5 font-medium">{t('data.rows')}</th>
                      <th className="px-3 py-1.5 font-medium">{t('data.lag')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={`${item.symbol}-${item.timeframe}-${item.trade_type}`} className="border-t border-border/60">
                        <td className="px-3 py-1.5 font-mono">{item.symbol}</td>
                        <td className="px-3 py-1.5 text-muted-foreground">{item.start}</td>
                        <td className={`px-3 py-1.5 font-mono ${item.stale ? 'text-rose-400' : 'text-foreground'}`}>
                          {item.last}
                        </td>
                        <td className="px-3 py-1.5 text-muted-foreground">{item.rows}</td>
                        <td className={`px-3 py-1.5 ${item.stale ? 'text-rose-400' : 'text-muted-foreground'}`}>
                          {item.lag_days ?? '—'}d
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
