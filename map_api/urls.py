from django.urls import path
from . import views

urlpatterns = [
    path('satellite/get-img/', views.get_satellite_img_api, name='get_satellite_img'),
    path('satellite/show-img/', views.show_satellite_image, name='show_satellite_image'),
    path('ai/query-region/', views.ai_query_region, name='ai_query_region'),
    path('geo/search/', views.geo_search, name='geo_search'),
]