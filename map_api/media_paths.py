"""媒体路径常量与路径穿越防护(Phase 7 拆出)。

SAVE_DIR / REPORT_DIR 的唯一定义处;views.py 与 sentinel_pipeline.py 都从这里导入。
注意:测试用 patch("map_api.views.SAVE_DIR") 只替换 views 模块内的引用
(报告/清理等走 views 全局量的代码路径),与本模块各自独立、互不影响。
"""
import os

from django.conf import settings

# 规范化保存目录
SAVE_DIR = os.path.join(settings.MEDIA_ROOT, 'satellite_imgs')
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

REPORT_DIR = settings.MEDIA_ROOT


def safe_media_path(base_dir, name, allowed_ext):
    """把用户传入的文件名安全地解析为 base_dir 内的路径,防止 ../ 路径穿越。
    去掉路径成分、校验后缀、并确认最终路径确实落在 base_dir 内;不合法返回 None。"""
    name = os.path.basename(name or '')
    if not name or not name.lower().endswith(allowed_ext):
        return None
    full = os.path.realpath(os.path.join(base_dir, name))
    base_real = os.path.realpath(base_dir)
    if full != base_real and not full.startswith(base_real + os.sep):
        return None
    return full
