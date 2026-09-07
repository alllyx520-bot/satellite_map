"""
URL configuration for satellite_map project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.conf import settings
from django.views.static import serve
from map_api import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('map_api.urls')),
    path('workbench/', views.workbench_view, name='workbench'),
    path('design/', views.design_view, name='design'),
    path('', views.index_view, name='index'),
]

# 本地 runserver 在默认生产式 DEBUG 配置下也必须能提供工作台 CSS/JS；
# 生产环境仍由 Nginx/CDN 接管静态文件。
urlpatterns += staticfiles_urlpatterns()
urlpatterns += [re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_ROOT})]
