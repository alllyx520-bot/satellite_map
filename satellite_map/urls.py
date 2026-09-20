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
from django.contrib.staticfiles.views import serve as staticfiles_serve
from map_api import views
from map_api.v3.api import workbench as workbench_v3

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('map_api.urls')),
    path('workbench/', workbench_v3, name='workbench'),
    path('agent/', workbench_v3, name='agent_workbench'),
    path('v3/', workbench_v3, name='workbench_v3'),
    path('legacy/workbench/', views.workbench_view, name='legacy_workbench'),
    path('legacy/', views.index_view, name='legacy_home'),
    path('design/', views.design_view, name='design'),
    path('', workbench_v3, name='index'),
]

# 本地 runserver 在默认生产式 DEBUG 配置下也必须能提供工作台 CSS/JS；
# 生产环境仍由 Nginx/CDN 接管静态文件，不会走到这里。
# insecure=True：让 staticfiles 走 finders（static/ + 各 app 的 static/），编辑源码后刷新即生效，
# 不必先 collectstatic——指向 STATIC_ROOT 会一直返回上次构建的陈旧副本。
urlpatterns += staticfiles_urlpatterns()
urlpatterns += [re_path(r"^static/(?P<path>.*)$", staticfiles_serve, {"insecure": True})]
