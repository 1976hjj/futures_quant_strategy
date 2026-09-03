"""Optional market-data adapters kept outside the research-trust boundary."""

from .akshare import AKShareProvider, normalize_akshare_market
from .baostock import BaoStockProvider, normalize_baostock_market, normalize_baostock_status
from .tushare import TushareProvider

__all__ = [
    "AKShareProvider",
    "BaoStockProvider",
    "TushareProvider",
    "normalize_akshare_market",
    "normalize_baostock_market",
    "normalize_baostock_status",
]
