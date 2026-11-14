import { createContext, useContext, useEffect, useMemo, useState } from 'react';

export type Language = 'zh' | 'en' | 'de';

type TranslationValues = Record<string, string | number>;
type I18nContextValue = {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: string, values?: TranslationValues) => string;
};

const translations: Record<Language, Record<string, string>> = {
  zh: {
    'nav.dashboard': '指挥中心', 'nav.launchpad': '任务发射台', 'nav.data': '数据下载', 'nav.inspector': '因子审查',
    'engine.online': '引擎在线', 'engine.offline': '引擎离线', 'common.refresh': '刷新', 'common.loading': '加载中…', 'common.save': '保存', 'common.none': '暂无',
    'home.kicker': '研究指挥台', 'home.title': '让挖掘结果成为研究决策。', 'home.subtitle': '监控运行中的引擎，评估因子档案，并从高潜结果直接进入审查。',
    'home.startMining': '开始挖掘', 'home.reviewArchive': '审查档案', 'home.snapshot': '快照 {time}', 'home.syncing': '正在同步研究状态…',
    'home.factorArchive': '因子档案', 'home.factorArchiveNote': '已持久化的因子工件', 'home.reviewCoverage': '审查覆盖率', 'home.reviewNote': '{count} 个因子已超出发现阶段',
    'home.bestIc': '最高观测 IC', 'home.noFactors': '暂无因子', 'home.taskCompletion': '任务完成率', 'home.runningNote': '当前 {count} 个任务运行中',
    'home.researchPulse': '研究脉冲', 'home.highestFitness': '最高适应度候选', 'home.openInspector': '打开审查台', 'home.factor': '因子', 'home.miner': '引擎', 'home.fitness': '适应度', 'home.ic': 'IC',
    'home.noArchivedFactors': '尚无已持久化因子。', 'home.archiveComposition': '档案构成', 'home.minerDistribution': '挖掘器分布', 'home.archiveEmpty': '首个挖掘任务完成后将在此显示档案。',
    'home.discovered': '已发现', 'home.reviewedLive': '审查中 / 已上线', 'home.executionPulse': '执行脉冲', 'home.taskStatus': '任务状态', 'home.running': '运行中', 'home.completed': '已完成', 'home.noFactorResult': '无因子', 'home.failed': '失败',
    'home.openTracker': '打开任务追踪器', 'home.latestExecution': '最近执行', 'home.miningActivity': '挖掘活动', 'home.seeAllTasks': '查看全部任务', 'home.noTaskActivity': '启动挖掘任务后将在此显示执行时间线。',
    'inspector.kicker': '研究档案', 'inspector.title': '因子审查台', 'inspector.subtitle': '浏览持久化因子工件、核验其逻辑，并记录审查状态。',
    'inspector.refreshCatalog': '刷新目录', 'inspector.catalog': '因子目录', 'inspector.search': '搜索 ID、哈希或表达式…', 'inspector.allMiners': '全部挖掘器', 'inspector.allStates': '全部状态', 'inspector.newest': '最新',
    'inspector.selectFactor': '选择一个已持久化因子', 'inspector.selectHint': '目录来自本地元数据，而非易失的挖掘任务。选择因子以审查已存储的逻辑与状态。',
    'inspector.noMatches': '没有符合筛选条件的已持久化因子。', 'inspector.logicHash': '逻辑哈希', 'inspector.saveStatus': '保存状态', 'inspector.saving': '保存中',
    'inspector.fitness': '适应度', 'inspector.rankIc': 'Rank IC', 'inspector.turnover': '换手率', 'inspector.created': '创建时间', 'inspector.researcher': '研究员', 'inspector.lineage': '谱系父代', 'inspector.noneRecorded': '未记录',
    'inspector.logicReference': '逻辑引用', 'inspector.modelVersion': '模型版本', 'inspector.outputChannel': '输出通道', 'inspector.weightsArtifact': '权重工件', 'inspector.available': '可用', 'inspector.missing': '缺失',
    'inspector.source': '源码', 'inspector.sourceMissing': '因子来源目录中缺少源码工件。', 'inspector.noActions': '未保存动作轨迹。', 'inspector.agentArtifact': 'Agent 工件',
    'inspector.nnPhaseOne': '第一期会核验模型工件与所选输出通道。特征归因与基于面值的 Tearsheet 图表将在第二期审查快照完成后提供。',
    'inspector.tearsheetStatus': 'Tearsheet 数据状态', 'inspector.noSyntheticCharts': '第一期不会以合成图表替代真实数据。只有可核验的快照存在时，才会展示因子分布、滚动 IC 与收益分析。',
    'inspector.tearsheet': '真实数据 Tearsheet', 'inspector.realSnapshot': '指标完全由持久化因子值与未来收益快照计算', 'inspector.crossAssetMethod': '逐期截面 IC 与分位组合', 'inspector.sequentialMethod': '时序滚动 IC 与全样本分位收益',
    'inspector.observations': '观测数', 'inspector.latestRollingIc': '最新滚动 IC', 'inspector.meanTurnover': '平均换手', 'inspector.quantileSpread': '分位收益差', 'inspector.rollingIc': '滚动 IC', 'inspector.quantileReturn': '分位平均未来收益',
    'inspector.dataLineage': '数据血缘', 'inspector.comparison': '因子比较', 'inspector.factorsSelected': '个已选因子', 'inspector.batchReview': '批量更新状态', 'inspector.compare': '比较', 'inspector.selectForReview': '选择用于批量审查', 'inspector.snapshotReady': '快照就绪', 'inspector.snapshotRequired': '尚无可用快照。请重新执行挖掘后再审查真实 Tearsheet。',
    'status.DISCOVERED': '已发现', 'status.INSPECTED': '已审查', 'status.PAPER_TRADING': '模拟盘', 'status.LIVE': '实盘中', 'status.RETIRED': '已退役',
    'nav.backtest': '组合回测',
    'bt.kicker': '组合验证', 'bt.title': '组合回测', 'bt.subtitle': '把因子库里的候选按权重合成一个多空组合，用事件驱动引擎跑出净值、回撤与成本。',
    'bt.selectFactors': '选择因子', 'bt.searchFactors': '搜索因子…', 'bt.selectAll': '全选可用', 'bt.clear': '清空', 'bt.selected': '已选 {count} 个',
    'bt.parameters': '回测参数', 'bt.start': '开始日期', 'bt.end': '结束日期', 'bt.commission': '手续费', 'bt.slippage': '滑点', 'bt.funding': '资金费/日', 'bt.rebalance': '调仓周期（根）', 'bt.long': '多头数', 'bt.short': '空头数', 'bt.cash': '初始资金',
    'bt.weighting': '权重方式', 'bt.weightEqual': '等权', 'bt.weightIc': '按 |RankIC|', 'bt.weightCustom': '自定义', 'bt.weightHint': '存储的线性组合因子会自动展开为成员并沿用拟合权重。',
    'bt.run': '运行回测', 'bt.running': '回测中…', 'bt.needFactors': '至少选择一个因子。', 'bt.noSnapshot': '无快照', 'bt.snapshotOnly': '仅显示有因子值快照的因子',
    'bt.result': '回测结果', 'bt.empty': '选择因子并运行，结果会出现在这里。', 'bt.metrics': '核心指标', 'bt.equity': '净值曲线', 'bt.equityAxis': '组合净值', 'bt.drawdownAxis': '回撤',
    'bt.totalReturn': '累计收益', 'bt.maxDrawdown': '最大回撤', 'bt.sharpe': '夏普', 'bt.cagr': '年化收益', 'bt.volatility': '波动率', 'bt.trades': '成交笔数', 'bt.costs': '总成本', 'bt.turnover': '平均换手', 'bt.scope': '区间', 'bt.assets': '标的数',
    'bt.effectiveWeights': '实际生效权重', 'bt.expanded': '展开的合成因子', 'bt.history': '历史运行', 'bt.noHistory': '尚无回测记录。', 'bt.failed': '失败', 'bt.viewRun': '查看', 'bt.openInspector': '在审查台打开',
  },
  en: {
    'nav.dashboard': 'Dashboard', 'nav.launchpad': 'Launchpad', 'nav.data': 'Data Downloader', 'nav.inspector': 'Factor Inspector',
    'engine.online': 'Engine Online', 'engine.offline': 'Engine offline', 'common.refresh': 'Refresh', 'common.loading': 'Loading…', 'common.save': 'Save', 'common.none': 'None',
    'home.kicker': 'Research Control Room', 'home.title': 'Turn mining output into a research decision.', 'home.subtitle': 'Monitor the live engine, assess the factor archive, and move directly from a promising result to inspection.',
    'home.startMining': 'Start Mining', 'home.reviewArchive': 'Review Archive', 'home.snapshot': 'Snapshot {time}', 'home.syncing': 'Synchronizing research state…',
    'home.factorArchive': 'Factor archive', 'home.factorArchiveNote': 'persisted factor artifacts', 'home.reviewCoverage': 'Review coverage', 'home.reviewNote': '{count} factors past discovery',
    'home.bestIc': 'Best observed IC', 'home.noFactors': 'no factors yet', 'home.taskCompletion': 'Task completion', 'home.runningNote': '{count} task(s) running now',
    'home.researchPulse': 'Research Pulse', 'home.highestFitness': 'Highest fitness candidates', 'home.openInspector': 'Open Inspector', 'home.factor': 'Factor', 'home.miner': 'Engine', 'home.fitness': 'Fitness', 'home.ic': 'IC',
    'home.noArchivedFactors': 'No persisted factor is available yet.', 'home.archiveComposition': 'Archive composition', 'home.minerDistribution': 'Miner distribution', 'home.archiveEmpty': 'The archive will appear after the first completed mining task.',
    'home.discovered': 'Discovered', 'home.reviewedLive': 'In review / live', 'home.executionPulse': 'Execution pulse', 'home.taskStatus': 'Task status', 'home.running': 'Running', 'home.completed': 'Completed', 'home.noFactorResult': 'No factors', 'home.failed': 'Failed',
    'home.openTracker': 'Open task tracker', 'home.latestExecution': 'Latest execution', 'home.miningActivity': 'Mining activity', 'home.seeAllTasks': 'See all tasks', 'home.noTaskActivity': 'Launch a mining task to populate the execution timeline.',
    'inspector.kicker': 'Research Archive', 'inspector.title': 'Factor Inspector', 'inspector.subtitle': 'Browse persisted factor artifacts, verify their logic, and record review status.',
    'inspector.refreshCatalog': 'Refresh catalog', 'inspector.catalog': 'Factor Catalog', 'inspector.search': 'Search ID, hash, expression…', 'inspector.allMiners': 'All miners', 'inspector.allStates': 'All states', 'inspector.newest': 'Newest',
    'inspector.selectFactor': 'Select a persisted factor', 'inspector.selectHint': 'The catalog is read from local metadata, not from transient mining tasks. Choose a factor to inspect its stored logic and review state.',
    'inspector.noMatches': 'No persisted factors match this filter.', 'inspector.logicHash': 'Logic hash', 'inspector.saveStatus': 'Save status', 'inspector.saving': 'Saving',
    'inspector.fitness': 'Fitness', 'inspector.rankIc': 'Rank IC', 'inspector.turnover': 'Turnover', 'inspector.created': 'Created', 'inspector.researcher': 'Researcher', 'inspector.lineage': 'Lineage parents', 'inspector.noneRecorded': 'None recorded',
    'inspector.logicReference': 'Logic Reference', 'inspector.modelVersion': 'Model version', 'inspector.outputChannel': 'Output channel', 'inspector.weightsArtifact': 'Weights artifact', 'inspector.available': 'Available', 'inspector.missing': 'Missing',
    'inspector.source': 'Source', 'inspector.sourceMissing': 'The source artifact is missing from factor_db/sources.', 'inspector.noActions': 'No action trajectory was stored.', 'inspector.agentArtifact': 'Agent artifact',
    'inspector.nnPhaseOne': 'Phase I verifies the model artifact and selected output channel. Feature attribution and value-based Tearsheet charts require the Phase II analysis snapshot.',
    'inspector.tearsheetStatus': 'Tearsheet data status', 'inspector.noSyntheticCharts': 'Phase I intentionally does not substitute synthetic charts. Value distributions, rolling IC and return analysis will appear only after a verifiable snapshot is available.',
    'inspector.tearsheet': 'Real-data Tearsheet', 'inspector.realSnapshot': 'Metrics are calculated only from persisted factor-value and forward-return snapshots', 'inspector.crossAssetMethod': 'Per-period cross-sectional IC and quantile portfolios', 'inspector.sequentialMethod': 'Time-series rolling IC and full-sample quantile returns',
    'inspector.observations': 'Observations', 'inspector.latestRollingIc': 'Latest rolling IC', 'inspector.meanTurnover': 'Mean turnover', 'inspector.quantileSpread': 'Quantile spread', 'inspector.rollingIc': 'Rolling IC', 'inspector.quantileReturn': 'Mean forward return by quantile',
    'inspector.dataLineage': 'Data lineage', 'inspector.comparison': 'Factor comparison', 'inspector.factorsSelected': 'factor(s) selected', 'inspector.batchReview': 'Batch update status', 'inspector.compare': 'Compare', 'inspector.selectForReview': 'Select for batch review', 'inspector.snapshotReady': 'Snapshot ready', 'inspector.snapshotRequired': 'No usable snapshot yet. Re-run mining before reviewing a real Tearsheet.',
    'status.DISCOVERED': 'Discovered', 'status.INSPECTED': 'Inspected', 'status.PAPER_TRADING': 'Paper trading', 'status.LIVE': 'Live', 'status.RETIRED': 'Retired',
    'nav.backtest': 'Portfolio Backtest',
    'bt.kicker': 'Portfolio Validation', 'bt.title': 'Portfolio Backtest', 'bt.subtitle': 'Blend stored factors into one long/short book with explicit weights and run it through the event-driven engine.',
    'bt.selectFactors': 'Select factors', 'bt.searchFactors': 'Search factors…', 'bt.selectAll': 'Select all usable', 'bt.clear': 'Clear', 'bt.selected': '{count} selected',
    'bt.parameters': 'Backtest parameters', 'bt.start': 'Start date', 'bt.end': 'End date', 'bt.commission': 'Commission', 'bt.slippage': 'Slippage', 'bt.funding': 'Funding / day', 'bt.rebalance': 'Rebalance (bars)', 'bt.long': 'Long legs', 'bt.short': 'Short legs', 'bt.cash': 'Initial cash',
    'bt.weighting': 'Weighting', 'bt.weightEqual': 'Equal', 'bt.weightIc': 'By |RankIC|', 'bt.weightCustom': 'Custom', 'bt.weightHint': 'Stored linear-combination factors are expanded into their members with the fitted weights.',
    'bt.run': 'Run backtest', 'bt.running': 'Running…', 'bt.needFactors': 'Select at least one factor.', 'bt.noSnapshot': 'No snapshot', 'bt.snapshotOnly': 'Only factors with a stored value snapshot',
    'bt.result': 'Result', 'bt.empty': 'Pick factors and run; the result will appear here.', 'bt.metrics': 'Key metrics', 'bt.equity': 'Equity curve', 'bt.equityAxis': 'Portfolio value', 'bt.drawdownAxis': 'Drawdown',
    'bt.totalReturn': 'Total return', 'bt.maxDrawdown': 'Max drawdown', 'bt.sharpe': 'Sharpe', 'bt.cagr': 'CAGR', 'bt.volatility': 'Volatility', 'bt.trades': 'Trades', 'bt.costs': 'Total costs', 'bt.turnover': 'Avg turnover', 'bt.scope': 'Window', 'bt.assets': 'Assets',
    'bt.effectiveWeights': 'Effective weights', 'bt.expanded': 'Expanded combination', 'bt.history': 'Run history', 'bt.noHistory': 'No backtest has been run yet.', 'bt.failed': 'Failed', 'bt.viewRun': 'View', 'bt.openInspector': 'Open in Inspector',
  },
  de: {
    'nav.dashboard': 'Dashboard', 'nav.launchpad': 'Startbereich', 'nav.data': 'Daten-Download', 'nav.inspector': 'Faktor-Inspektor',
    'engine.online': 'Engine online', 'engine.offline': 'Engine offline', 'common.refresh': 'Aktualisieren', 'common.loading': 'Wird geladen…', 'common.save': 'Speichern', 'common.none': 'Keine',
    'home.kicker': 'Research Control Room', 'home.title': 'Machen Sie aus Mining-Ergebnissen eine Forschungsentscheidung.', 'home.subtitle': 'Überwachen Sie die laufende Engine, bewerten Sie das Faktorarchiv und wechseln Sie direkt zur Prüfung vielversprechender Ergebnisse.',
    'home.startMining': 'Mining starten', 'home.reviewArchive': 'Archiv prüfen', 'home.snapshot': 'Snapshot {time}', 'home.syncing': 'Forschungsstatus wird synchronisiert…',
    'home.factorArchive': 'Faktorarchiv', 'home.factorArchiveNote': 'gespeicherte Faktor-Artefakte', 'home.reviewCoverage': 'Prüfabdeckung', 'home.reviewNote': '{count} Faktoren nach der Entdeckung',
    'home.bestIc': 'Bester beobachteter IC', 'home.noFactors': 'noch keine Faktoren', 'home.taskCompletion': 'Aufgabenabschluss', 'home.runningNote': '{count} Aufgabe(n) laufen aktuell',
    'home.researchPulse': 'Forschungspuls', 'home.highestFitness': 'Kandidaten mit höchster Fitness', 'home.openInspector': 'Inspektor öffnen', 'home.factor': 'Faktor', 'home.miner': 'Engine', 'home.fitness': 'Fitness', 'home.ic': 'IC',
    'home.noArchivedFactors': 'Noch kein gespeicherter Faktor vorhanden.', 'home.archiveComposition': 'Archivzusammensetzung', 'home.minerDistribution': 'Miner-Verteilung', 'home.archiveEmpty': 'Das Archiv erscheint nach dem ersten abgeschlossenen Mining-Auftrag.',
    'home.discovered': 'Entdeckt', 'home.reviewedLive': 'In Prüfung / live', 'home.executionPulse': 'Ausführungspuls', 'home.taskStatus': 'Aufgabenstatus', 'home.running': 'Läuft', 'home.completed': 'Abgeschlossen', 'home.noFactorResult': 'Keine Faktoren', 'home.failed': 'Fehlgeschlagen',
    'home.openTracker': 'Task-Tracker öffnen', 'home.latestExecution': 'Letzte Ausführung', 'home.miningActivity': 'Mining-Aktivität', 'home.seeAllTasks': 'Alle Aufgaben', 'home.noTaskActivity': 'Starten Sie einen Mining-Auftrag, um die Ausführungszeitleiste zu füllen.',
    'inspector.kicker': 'Forschungsarchiv', 'inspector.title': 'Faktor-Inspektor', 'inspector.subtitle': 'Durchsuchen Sie gespeicherte Faktor-Artefakte, prüfen Sie ihre Logik und dokumentieren Sie den Prüfstatus.',
    'inspector.refreshCatalog': 'Katalog aktualisieren', 'inspector.catalog': 'Faktorkatalog', 'inspector.search': 'ID, Hash oder Ausdruck suchen…', 'inspector.allMiners': 'Alle Miner', 'inspector.allStates': 'Alle Status', 'inspector.newest': 'Neueste',
    'inspector.selectFactor': 'Gespeicherten Faktor auswählen', 'inspector.selectHint': 'Der Katalog wird aus lokalen Metadaten gelesen, nicht aus flüchtigen Mining-Aufgaben. Wählen Sie einen Faktor zur Prüfung seiner gespeicherten Logik und seines Status.',
    'inspector.noMatches': 'Keine gespeicherten Faktoren entsprechen diesem Filter.', 'inspector.logicHash': 'Logik-Hash', 'inspector.saveStatus': 'Status speichern', 'inspector.saving': 'Wird gespeichert',
    'inspector.fitness': 'Fitness', 'inspector.rankIc': 'Rank IC', 'inspector.turnover': 'Umschlag', 'inspector.created': 'Erstellt', 'inspector.researcher': 'Forscher', 'inspector.lineage': 'Abstammung', 'inspector.noneRecorded': 'Nicht erfasst',
    'inspector.logicReference': 'Logikreferenz', 'inspector.modelVersion': 'Modellversion', 'inspector.outputChannel': 'Ausgabekanal', 'inspector.weightsArtifact': 'Gewichtsartefakt', 'inspector.available': 'Verfügbar', 'inspector.missing': 'Fehlt',
    'inspector.source': 'Quelle', 'inspector.sourceMissing': 'Das Quellartefakt fehlt in factor_db/sources.', 'inspector.noActions': 'Keine Aktionssequenz wurde gespeichert.', 'inspector.agentArtifact': 'Agent-Artefakt',
    'inspector.nnPhaseOne': 'Phase I prüft das Modellartefakt und den gewählten Ausgabekanal. Merkmalsattribution und Tearsheet-Charts auf Wertbasis benötigen den Analyse-Snapshot aus Phase II.',
    'inspector.tearsheetStatus': 'Tearsheet-Datenstatus', 'inspector.noSyntheticCharts': 'Phase I ersetzt keine echten Daten durch synthetische Charts. Werteverteilungen, Rolling IC und Renditeanalyse erscheinen erst mit einem überprüfbaren Snapshot.',
    'inspector.tearsheet': 'Tearsheet mit echten Daten', 'inspector.realSnapshot': 'Kennzahlen werden ausschließlich aus gespeicherten Faktor- und Forward-Return-Snapshots berechnet', 'inspector.crossAssetMethod': 'Periodischer Querschnitts-IC und Quantilportfolios', 'inspector.sequentialMethod': 'Zeitreihen-Rolling-IC und Quantilrenditen der Gesamtstichprobe',
    'inspector.observations': 'Beobachtungen', 'inspector.latestRollingIc': 'Letzter Rolling IC', 'inspector.meanTurnover': 'Durchschnittlicher Umschlag', 'inspector.quantileSpread': 'Quantilspanne', 'inspector.rollingIc': 'Rolling IC', 'inspector.quantileReturn': 'Mittlere Forward-Rendite nach Quantil',
    'inspector.dataLineage': 'Datenherkunft', 'inspector.comparison': 'Faktorvergleich', 'inspector.factorsSelected': 'Faktor(en) ausgewählt', 'inspector.batchReview': 'Status gesammelt aktualisieren', 'inspector.compare': 'Vergleichen', 'inspector.selectForReview': 'Für Sammelprüfung auswählen', 'inspector.snapshotReady': 'Snapshot bereit', 'inspector.snapshotRequired': 'Noch kein nutzbarer Snapshot. Starten Sie Mining erneut, bevor Sie ein echtes Tearsheet prüfen.',
    'status.DISCOVERED': 'Entdeckt', 'status.INSPECTED': 'Geprüft', 'status.PAPER_TRADING': 'Papierhandel', 'status.LIVE': 'Live', 'status.RETIRED': 'Ausgemustert',
    'nav.backtest': 'Portfolio-Backtest',
    'bt.kicker': 'Portfoliovalidation', 'bt.title': 'Portfolio-Backtest', 'bt.subtitle': 'Kombinieren Sie gespeicherte Faktoren mit expliziten Gewichten zu einem Long/Short-Buch und prüfen Sie es im Event-Engine.',
    'bt.selectFactors': 'Faktoren wählen', 'bt.searchFactors': 'Faktoren suchen…', 'bt.selectAll': 'Alle nutzbaren', 'bt.clear': 'Leeren', 'bt.selected': '{count} gewählt',
    'bt.parameters': 'Backtest-Parameter', 'bt.start': 'Startdatum', 'bt.end': 'Enddatum', 'bt.commission': 'Kommission', 'bt.slippage': 'Slippage', 'bt.funding': 'Funding / Tag', 'bt.rebalance': 'Rebalance (Bars)', 'bt.long': 'Long-Positionen', 'bt.short': 'Short-Positionen', 'bt.cash': 'Startkapital',
    'bt.weighting': 'Gewichtung', 'bt.weightEqual': 'Gleich', 'bt.weightIc': 'Nach |RankIC|', 'bt.weightCustom': 'Benutzerdefiniert', 'bt.weightHint': 'Gespeicherte Linearkombinationen werden mit ihren gefitteten Gewichten in Mitglieder aufgelöst.',
    'bt.run': 'Backtest starten', 'bt.running': 'Läuft…', 'bt.needFactors': 'Mindestens einen Faktor wählen.', 'bt.noSnapshot': 'Kein Snapshot', 'bt.snapshotOnly': 'Nur Faktoren mit gespeichertem Wert-Snapshot',
    'bt.result': 'Ergebnis', 'bt.empty': 'Faktoren wählen und starten; das Ergebnis erscheint hier.', 'bt.metrics': 'Kennzahlen', 'bt.equity': 'Kapitalkurve', 'bt.equityAxis': 'Portfoliowert', 'bt.drawdownAxis': 'Drawdown',
    'bt.totalReturn': 'Gesamtrendite', 'bt.maxDrawdown': 'Max. Drawdown', 'bt.sharpe': 'Sharpe', 'bt.cagr': 'CAGR', 'bt.volatility': 'Volatilität', 'bt.trades': 'Trades', 'bt.costs': 'Gesamtkosten', 'bt.turnover': 'Umschlag', 'bt.scope': 'Zeitraum', 'bt.assets': 'Assets',
    'bt.effectiveWeights': 'Effektive Gewichte', 'bt.expanded': 'Aufgelöste Kombination', 'bt.history': 'Verlauf', 'bt.noHistory': 'Noch kein Backtest durchgeführt.', 'bt.failed': 'Fehlgeschlagen', 'bt.viewRun': 'Ansehen', 'bt.openInspector': 'Im Inspektor öffnen',
  },
};

function browserLanguage(): Language {
  const saved = window.localStorage.getItem('factorminer-language');
  if (saved === 'zh' || saved === 'en' || saved === 'de') return saved;
  const browser = navigator.language.toLowerCase();
  if (browser.startsWith('de')) return 'de';
  if (browser.startsWith('zh')) return 'zh';
  return 'en';
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguage] = useState<Language>(browserLanguage);

  useEffect(() => {
    window.localStorage.setItem('factorminer-language', language);
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : language;
  }, [language]);

  const value = useMemo<I18nContextValue>(() => ({
    language,
    setLanguage,
    t: (key, values = {}) => {
      const source = translations[language][key] || translations.en[key] || key;
      return source.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? `{${name}}`));
    },
  }), [language]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside I18nProvider');
  return context;
}
