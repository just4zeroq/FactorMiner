"""
FactorMiner 主配置文件
"""

import os
from pathlib import Path
from typing import Dict, Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 数据目录
DATA_DIR = PROJECT_ROOT / "data"

# 创建必要的目录
for dir_path in [DATA_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# 日志配置
LOGGING_CONFIG = {
    'level': 'INFO',
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    'file': PROJECT_ROOT / "logs" / "factor_miner.log"
}

# 创建日志目录
LOGGING_CONFIG['file'].parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 网络代理：数据下载 / 交易所元数据探测时连接币安、OKX 等境外交易所
# ---------------------------------------------------------------------------
# 默认走本机 HTTP 代理（Clash / Clash Verge / v2rayN 常用端口）。
# 可用环境变量调整，无需改代码：
#   FACTORMINER_PROXY=http://127.0.0.1:7890  覆盖代理地址，也支持 socks5://
#   FACTORMINER_NO_PROXY=1                   关闭代理，改为直连
DEFAULT_PROXY_URL = "http://127.0.0.1:7890"


def get_proxy_url() -> Optional[str]:
    """返回当前生效的代理地址；禁用代理时返回 None。"""
    if os.getenv("FACTORMINER_NO_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    url = (os.getenv("FACTORMINER_PROXY") or DEFAULT_PROXY_URL).strip()
    return url or None


def ccxt_proxy_config() -> Dict[str, str]:
    """生成 ccxt 4.x 的代理配置片段。

    注意：ccxt 4.x 中 httpProxy / httpsProxy / socksProxy / proxies 互斥，
    同时设置多个会抛 InvalidProxySettings，因此这里只返回单个键。
    交易所 API 均为 HTTPS，用 httpsProxy 即可覆盖 http 与 https 请求。
    """
    url = get_proxy_url()
    if not url:
        return {}
    if url.startswith(("socks5://", "socks5h://", "socks4://", "socks://")):
        return {"socksProxy": url}  # socks 需要额外安装 pysocks
    return {"httpsProxy": url}


def apply_proxy_to_exchange(exchange, proxy_url: Optional[str] = None) -> None:
    """在 requests.Session 上再补一层代理，兼容较早的 ccxt 4.x 版本。"""
    if proxy_url is None:
        proxy_url = get_proxy_url()
    if not proxy_url or proxy_url.startswith("socks"):
        return
    session = getattr(exchange, "session", None)
    if session is None or not hasattr(session, "proxies"):
        return
    session.proxies.update({"http": proxy_url, "https": proxy_url})
    # 避免系统环境变量里的其它代理（例如工具注入的 HTTP_PROXY）覆盖配置
    if hasattr(session, "trust_env"):
        session.trust_env = False
