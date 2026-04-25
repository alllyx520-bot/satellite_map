import os
import sys
import threading
import webbrowser
import time
from django.core.management import execute_from_command_line

def open_browser():
    """延迟 2 秒后，自动调用系统默认浏览器打开网址"""
    time.sleep(2)
    print("\n🌍 正在自动打开浏览器，进入 AI 遥感分析舱...\n")
    webbrowser.open('http://127.0.0.1:8000/')

if __name__ == '__main__':
    # 1. 指定 Django 的配置路径
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'satellite_map.settings')
    
    # 2. 开启一个后台线程，负责倒计时开网页
    threading.Thread(target=open_browser, daemon=True).start()
    
    # 3. 启动 Django 服务器 (打包成 exe 后，必须加 --noreload 禁用热重载)
    print("🚀 系统启动中，请勿关闭此窗口...")
    execute_from_command_line(['manage.py', 'runserver', '127.0.0.1:8000', '--noreload'])