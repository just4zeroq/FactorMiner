import logging
import pandas as pd
from typing import Dict, List, Any
from core.data_feed.naming import data_path, resolve_data_root
from core.evaluation.targets import ForwardReturnTarget, target_from_config

logger = logging.getLogger(__name__)

class RealDataClient:
    def __init__(self, config: Dict):
        self.config = config
        self.data_feed_config = config.get("data_feeds", {})
        
        self.exchange = self.data_feed_config.get("exchange", "binance")
        self.instrument_type = self.data_feed_config.get("instrument_type", "futures")
        self.timeframe = self.data_feed_config.get("timeframe", "1m")
        # ``data_feeds.data_root`` is the single source of truth.  Honouring it
        # here keeps the panel the miner reads identical to the directory the
        # ``doctor`` preflight and ``refresh`` command validated; before this the
        # client silently hardcoded ``cwd/data`` and a ``--data-root`` override
        # validated a directory nothing ever read.
        self.data_root = resolve_data_root(self.data_feed_config.get("data_root"))
        self.pairs = self.data_feed_config.get("pairs", [])
        self.required_streams = self.data_feed_config.get("required_streams", [])
        # Crypto-native streams (funding_rate / open_interest / basis /
        # liquidations) to merge as extra columns.  Empty -> OHLCV only,
        # fully backward compatible.
        self.crypto_streams = list(self.data_feed_config.get("crypto_streams", []) or [])
        self.mine_periods = self.data_feed_config.get("mine_period", [])
        self.test_periods = self.data_feed_config.get("test_period", [])
        self.mining_mode = self.data_feed_config.get("mining_mode", "sequential_single")
        self.target_spec = target_from_config(config)
        
        # 预先加载好的缓存
        self._mine_data = None
        self._test_data = None
        # Pre-mining quality gate: validated once, on the first data access.
        self._quality_checked = False
        self.quality_report = None
        
        # 在初始化时一次性加载数据
        self._load_all_data()

    def _load_all_data(self):
        if not self.pairs:
            logger.warning("No pairs specified in config data_feeds.")
            return

        if self.mining_mode == "sequential_single":
            primary_pair = self.pairs[0]
            if len(self.pairs) > 1:
                logger.info(f"Mining mode is 'sequential_single'. Only using the first pair: {primary_pair}")
            
            self._mine_data = self._load_periods_for_pair(primary_pair, self.mine_periods)
            self._test_data = self._load_periods_for_pair(primary_pair, self.test_periods)
            
        elif self.mining_mode == "cross_asset":
            logger.info("Mining mode is 'cross_asset'. Loading and pivoting data for all pairs.")

            def load_and_pivot(periods):
                dfs = []
                loaded_pairs = []
                for pair in self.pairs:
                    df = self._load_periods_for_pair(pair, periods)
                    if df.empty:
                        logger.warning(
                            "Cross-asset: %s has no data in the requested periods, "
                            "it will be excluded from the universe.", pair
                        )
                        continue
                    # 只保留配置声明的字段 + 前瞻收益标签，避免历史文件里的
                    # level_0 / index 等脏列被误当成特征参与截面计算。
                    streams = [c for c in self.required_streams if c in df.columns]
                    if not streams:
                        streams = [
                            c for c in df.columns
                            if c not in ('asset', 'date', 'time', 'timestamp', 'returns')
                        ]
                    extra = [
                        c for c in self.crypto_streams
                        if c in df.columns and c not in streams
                    ]
                    keep = streams + extra + (['returns'] if 'returns' in df.columns else [])
                    part = df[keep].copy()
                    part['asset'] = pair
                    dfs.append(part)
                    loaded_pairs.append(pair)

                if not dfs:
                    logger.error("Cross-asset: none of the configured pairs has data.")
                    return None

                long_df = pd.concat(dfs).reset_index()
                date_col = long_df.columns[0]
                long_df = long_df.rename(columns={date_col: 'date'})
                # 同一 (date, asset) 出现多条时保留最后一条，否则 pivot 会抛
                # "Index contains duplicate entries, cannot reshape"。
                duplicates = int(long_df.duplicated(subset=['date', 'asset']).sum())
                if duplicates:
                    logger.warning(
                        "Cross-asset: dropping %d duplicate (date, asset) rows before pivot.",
                        duplicates,
                    )
                    long_df = long_df.drop_duplicates(subset=['date', 'asset'], keep='last')

                # 统一所有特征的 index / columns，保证 sandbox 与 evaluator 的
                # 严格对齐校验能通过；缺失格以 NaN 表示，不静默丢行。
                all_dates = pd.DatetimeIndex(sorted(long_df['date'].unique()))
                all_assets = sorted(loaded_pairs)
                pivot_dict = {}
                for feature in [c for c in long_df.columns if c not in ('date', 'asset')]:
                    wide = long_df.pivot(index='date', columns='asset', values=feature)
                    pivot_dict[feature] = wide.reindex(index=all_dates, columns=all_assets)

                ref = pivot_dict.get('close', next(iter(pivot_dict.values())))
                logger.info(
                    "Cross-asset data ready: %d assets x %d timestamps, close coverage %.1f%%",
                    len(all_assets), len(all_dates), ref.notna().mean().mean() * 100,
                )
                for asset in all_assets:
                    cov = ref[asset].notna().mean()
                    if cov < 0.90:
                        logger.warning(
                            "Cross-asset: %s coverage is only %.1f%%. Check its listing "
                            "date or re-download the same range as the other pairs.",
                            asset, cov * 100,
                        )
                return pivot_dict

            self._mine_data = load_and_pivot(self.mine_periods)
            self._test_data = load_and_pivot(self.test_periods)
            
        else:
            logger.warning(f"Mining mode '{self.mining_mode}' is not fully supported yet. Falling back to sequential_single.")
            primary_pair = self.pairs[0]
            self._mine_data = self._load_periods_for_pair(primary_pair, self.mine_periods)
            self._test_data = self._load_periods_for_pair(primary_pair, self.test_periods)

    def _load_periods_for_pair(self, pair: str, periods: List[List[str]]) -> pd.DataFrame:
        if not periods:
            return pd.DataFrame()
            
        file_path = data_path(
            self.data_root, self.exchange, pair, self.timeframe, self.instrument_type
        )
        
        if not file_path.exists():
            # The downloader writes under the project's canonical ``data/`` tree.
            # When ``data_root`` points somewhere else, an implicit download
            # would land in a directory this client never reads -- the run would
            # then die on a phantom "file still missing" error after a
            # multi-minute download. Refuse up front instead.
            canonical = resolve_data_root()
            if self.data_root.resolve() != canonical.resolve():
                logger.error(
                    "Data file not found: %s (data_root=%s differs from the downloader's "
                    "%s; run `factorminer refresh --data-root %s` first).",
                    file_path, self.data_root, canonical, self.data_root,
                )
                return pd.DataFrame()

            logger.warning(f"Data file not found: {file_path}. Attempting to download automatically...")
            
            # 收集所有需要的数据段以计算最大的时间范围
            all_periods = self.mine_periods + self.test_periods
            if not all_periods:
                logger.error(f"Cannot download {pair}: no periods specified.")
                return pd.DataFrame()
                
            all_dates = []
            for p in all_periods:
                if len(p) >= 2:
                    all_dates.append(pd.to_datetime(p[0]))
                    all_dates.append(pd.to_datetime(p[1]))
                    
            if not all_dates:
                return pd.DataFrame()
                
            # 预留滚动窗口预热与标签尾部余量：否则首批因子的 ts_* 算子全为
            # NaN，最后几根 bar 的前瞻收益也无从计算。按 120 根 bar 估算。
            try:
                bar = pd.Timedelta(self.timeframe)
            except (ValueError, TypeError):
                bar = pd.Timedelta(days=1)
            margin = max(bar * 120, pd.Timedelta(days=1))
            start_date = (min(all_dates) - margin).strftime('%Y-%m-%d')
            end_date = (max(all_dates) + margin).strftime('%Y-%m-%d')

            from core.data_feed.data_downloader import DataDownloader
            downloader = DataDownloader()

            res = downloader.download_ohlcv(
                symbol=pair,
                timeframe=self.timeframe,
                start_date=start_date,
                end_date=end_date,
                trade_type=self.instrument_type,
                exchange_id=self.exchange,
            )
            
            if not file_path.exists():
                logger.error(f"Failed to auto-download {pair} data or file still missing.")
                return pd.DataFrame()
            else:
                logger.info(f"Successfully auto-downloaded data for {pair}!")
            
        logger.info(f"Loading data from {file_path}")
        df = pd.read_feather(file_path)
        # 历史写入残留的辅助列不能参与因子计算（NN 会把全表当特征）
        df = df.drop(columns=[c for c in ('level_0', 'index') if c in df.columns])

        # 标准化索引
        if 'date' in df.columns:
            df.set_index('date', inplace=True)
        elif 'time' in df.columns:
            df.set_index('time', inplace=True)
        elif 'timestamp' in df.columns:
            df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('date', inplace=True)
            df.drop('timestamp', axis=1, inplace=True)
            
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
            
        # 根据时间段进行过滤并拼接
        sliced_dfs = []
        for period in periods:
            if len(period) == 2:
                start_dt, end_dt = pd.to_datetime(period[0]), pd.to_datetime(period[1])
                sliced_df = df[(df.index >= start_dt) & (df.index <= end_dt)].copy()
                sliced_df["returns"] = self.target_spec.build(sliced_df)
                sliced_dfs.append(sliced_df)
                
        if sliced_dfs:
            final_df = pd.concat(sliced_dfs)
            final_df = self._drop_unclosed_last_bar(final_df)
            final_df = self._attach_crypto_streams(final_df, pair)
            return final_df
            
        return pd.DataFrame()

    def _drop_unclosed_last_bar(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop a trailing bar that has not finished forming yet (D-05).

        The last bar in a freshly-refreshed file is frequently the *current,
        still-forming* candle: its OHLCV is partial and its forward return is
        undefined. Feeding it into mining lets a factor "see" a half-built bar
        whose close will still move — a look-ahead-flavoured artifact. This
        trims it at the DATA layer, once, so every caller (mining, backtest,
        inspector) inherits the same guard instead of each script re-implementing
        it. It is a no-op for historical data (all bars already closed).

        The rule itself lives in :mod:`core.data_feed.bar_clock` so the holdout
        window planner and the freshness report stay on the same definition.
        """
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return df
        from core.data_feed.bar_clock import bar_delta, drop_unclosed_last_bar

        trimmed = drop_unclosed_last_bar(df, self.timeframe)
        if len(trimmed) != len(df):
            logger.info(
                "Dropping unclosed last bar %s (closes at %s, now %s) for %s/%s",
                df.index.max(),
                df.index.max() + bar_delta(self.timeframe),
                pd.Timestamp.now(tz="UTC").tz_convert(None),
                self.exchange,
                self.timeframe,
            )
        return trimmed

    def _attach_crypto_streams(self, df: pd.DataFrame, pair: str) -> pd.DataFrame:
        """Merge stored crypto streams as extra columns, aligned past-only.

        Missing files are skipped, so enabling ``crypto_streams`` on a machine
        that has not downloaded them yet degrades gracefully to OHLCV-only.
        """
        if df.empty or not self.crypto_streams:
            return df
        from core.data_feed.crypto_streams import load_crypto_streams

        data_root = self.data_root
        try:
            streams = load_crypto_streams(
                data_root,
                self.exchange,
                pair,
                self.timeframe,
                self.instrument_type,
                pd.DatetimeIndex(df.index),
                streams=self.crypto_streams,
            )
        except Exception as exc:
            logger.warning("Failed to attach crypto streams for %s: %s", pair, exc)
            return df

        for name, series in streams.items():
            if name not in df.columns:
                df[name] = series.values
        if streams:
            logger.info(
                "Attached crypto streams for %s: %s",
                pair, ", ".join(sorted(streams)),
            )
        return df

    def _panel_view(self) -> Any:
        """The mining panel without the target column (no quality-gate loop)."""
        if isinstance(self._mine_data, pd.DataFrame):
            return self._mine_data.drop(columns=["returns"], errors="ignore")
        if isinstance(self._mine_data, dict):
            return {key: value for key, value in self._mine_data.items() if key != "returns"}
        return self._mine_data

    def _ensure_quality_checked(self) -> None:
        """Run the panel quality gate exactly once per client.

        Doing it lazily (rather than in ``__init__``) keeps the download-only
        code paths cheap, while still failing before the first evaluation.
        """
        if self._quality_checked:
            return
        self._quality_checked = True
        quality_cfg = self.config.get("data_quality") or {}
        if not quality_cfg.get("check_on_load", True):
            return
        try:
            from core.data_feed.panel_validator import enforce, validate_panel

            report = validate_panel(
                self._panel_view(),
                self.get_returns(),
                timeframe=self.timeframe,
                quality_cfg=quality_cfg,
            )
            self.quality_report = report.as_dict()
            enforce(report, quality_cfg)
        except Exception as exc:
            # A validator bug must never take mining down with it, but the
            # strict-mode rejection has to surface.
            from core.data_feed.panel_validator import DataQualityError

            if isinstance(exc, DataQualityError):
                raise
            logger.warning("Panel quality check skipped: %s", exc)

    def get_data(self) -> Any:
        if self._mine_data is None:
            logger.warning("Mine data is empty!")
            return None
        self._ensure_quality_checked()

        if isinstance(self._mine_data, pd.DataFrame):
            if self._mine_data.empty:
                logger.warning("Mine data is empty DataFrame!")
            return self._mine_data.drop(columns=['returns'], errors='ignore')
        elif isinstance(self._mine_data, dict):
            return {k: v for k, v in self._mine_data.items() if k != 'returns'}
            
        return self._mine_data

    def get_returns(self) -> Any:
        if self.mining_mode == "cross_asset":
            return self._mine_data.get('returns') if isinstance(self._mine_data, dict) else pd.DataFrame()
            
        if isinstance(self._mine_data, pd.DataFrame) and 'returns' in self._mine_data.columns:
            return self._mine_data['returns']
        return pd.Series(dtype=float)

    def get_test_data(self) -> Any:
        if isinstance(self._test_data, pd.DataFrame):
            return self._test_data.drop(columns=['returns'], errors='ignore')
        elif isinstance(self._test_data, dict):
            return {k: v for k, v in self._test_data.items() if k != 'returns'}
            
        return self._test_data

    def get_test_returns(self) -> Any:
        if self.mining_mode == "cross_asset":
            return self._test_data.get('returns') if isinstance(self._test_data, dict) else pd.DataFrame()
            
        if isinstance(self._test_data, pd.DataFrame) and 'returns' in self._test_data.columns:
            return self._test_data['returns']
        return pd.Series(dtype=float)

    def get_target_spec(self) -> ForwardReturnTarget:
        return self.target_spec

    def get_forward_return_definition(self) -> str:
        return self.target_spec.definition()
