from django.urls import path
from . import views

urlpatterns = [
    path('satellite/get-img/', views.get_satellite_img_api, name='get_satellite_img'),
    path('satellite/show-img/', views.show_satellite_image, name='show_satellite_image'),
    path('satellite/progress/', views.get_progress, name='get_progress'),
    path('ai/query-region/', views.ai_query_region, name='ai_query_region'),
    path('ai/history/', views.chat_history_list, name='chat_history_list'),
    path('ai/history/<int:history_id>/', views.chat_history_detail, name='chat_history_detail'),
    path('geo/search/', views.geo_search, name='geo_search'),
    path('report/generate/', views.generate_report, name='generate_report'),
    path('report/download/', views.download_report, name='download_report'),
]
