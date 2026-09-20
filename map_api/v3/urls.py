from django.urls import path, re_path
from . import api
from .asset_api import urlpatterns as asset_patterns

urlpatterns = [
    path("conversations/", api.conversations),
    path("conversations/<uuid:conversation_id>/", api.conversation),
    path("conversations/<uuid:conversation_id>/messages/", api.messages),
    path("conversations/<uuid:conversation_id>/events/", api.events),
    path("runs/<int:run_id>/", api.run),
    path("runs/<int:run_id>/actions/", api.actions),
    path("observations/", api.observations),
    path("evidence/", api.evidence),
    path("artifacts/", api.artifacts),
    path("artifacts/<int:artifact_id>/download", api.artifact_download),
    path("capabilities/", api.capabilities),
    path("places/", api.places),
]
urlpatterns += asset_patterns
# Accept canonical slashless URLs and Django-style trailing slashes without a
# redirect that could drop a POST body. Both names resolve to the same handler.
for route in list(urlpatterns):
    pattern = str(route.pattern)
    alternate = pattern.rstrip("/") if pattern.endswith("/") else pattern + "/"
    urlpatterns.append(path(alternate, route.callback))
