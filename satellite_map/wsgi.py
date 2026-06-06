"""
WSGI config for satellite_map project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application
from .env import load_project_env

load_project_env()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "satellite_map.settings")

application = get_wsgi_application()
