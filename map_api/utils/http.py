"""HTTP 公共工具；request_proxies 全项目唯一实现(此前三处重复定义)。"""
import os


def request_proxies():
    """默认遵循当前进程代理；仅显式要求时才强制直连。"""
    direct = os.environ.get("SATELLITESENSE_DIRECT_HTTP", "").strip().lower()
    return {"http": None, "https": None} if direct in {"1", "true", "yes"} else None
