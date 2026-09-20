"""影像源 provider 注册表；调用点统一走 get_provider,不再直接实例化具体类。"""
from .earth_search import COLLECTION_PROFILES, EarthSearchProvider
from .esri import EsriProvider
from .mapbox import MapboxProvider
from .planetary_computer import PlanetaryComputerProvider
from .tianditu import TiandituProvider

_PROVIDERS = {
    "earth_search": EarthSearchProvider,
    "esri": EsriProvider,
    "mapbox": MapboxProvider,
    "planetary_computer": PlanetaryComputerProvider,
    "tianditu": TiandituProvider,
}


def get_provider(name, **kwargs):
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"未知的影像源 provider: {name}")
    return provider_cls(**kwargs)


def get_provider_for_collection(collection, **kwargs):
    """按 collection profile 的 provider 字段路由;默认 earth_search。"""
    profile = COLLECTION_PROFILES.get(collection) or {}
    return get_provider(profile.get("provider", "earth_search"), **kwargs)
