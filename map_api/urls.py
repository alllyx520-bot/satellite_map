from django.urls import path
from . import views

urlpatterns = [
    path('satellite/get-img/', views.get_satellite_img_api, name='get_satellite_img'),
    path('satellite/get-sentinel-img/', views.get_sentinel_img_api, name='get_sentinel_img'),
    path('satellite/show-img/', views.show_satellite_image, name='show_satellite_image'),
    path('satellite/progress/', views.get_progress, name='get_progress'),
    path('satellite/cleanup/', views.cleanup_cache, name='cleanup_cache'),
    path('imagery/search/', views.imagery_search, name='imagery_search'),
    path('imagery/scenes/', views.imagery_scene_list, name='imagery_scene_list'),
    path('imagery/scenes/<int:scene_id>/', views.imagery_scene_detail, name='imagery_scene_detail'),
    path('ai/query-region/', views.ai_query_region, name='ai_query_region'),
    path('ai/history/', views.chat_history_list, name='chat_history_list'),
    path('ai/history/<int:history_id>/', views.chat_history_detail, name='chat_history_detail'),
    path('geo/search/', views.geo_search, name='geo_search'),
    path('report/generate/', views.generate_report, name='generate_report'),
    path('report/download/', views.download_report, name='download_report'),
]
