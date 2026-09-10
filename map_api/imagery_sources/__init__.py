"""影像源 provider 注册表；调用点统一走 get_provider,不再直接实例化具体类。"""
from .earth_search import EarthSearchProvider
from .esri import EsriProvider
from .mapbox import MapboxProvider
from .tianditu import TiandituProvider

_PROVIDERS = {
    "earth_search": EarthSearchProvider,
    "esri": EsriProvider,
    "mapbox": MapboxProvider,
    "tianditu": TiandituProvider,
}


def get_provider(name, **kwargs):
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"未知的影像源 provider: {name}")
    return provider_cls(**kwargs)
