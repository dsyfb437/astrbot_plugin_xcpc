# -*- coding: utf-8 -*-
"""插件的核心包。

这里只放一个东西：按**加载方式**导入 `platforms.*` 的辅助函数。

为什么需要它 —— 同一个文件会被两种方式加载：

  * AstrBot 按**包**加载：`astrbot/core/star/star_manager.py` 里拼出
    `path = "data.plugins." + 插件目录名 + "." + "main"`，
    再 `__import__(path, fromlist=["main"])`。所以 `core/sync.py` 的模块名是
    `data.plugins.astrbot_plugin_xcpc.core.sync`，此时 **`platforms` 不是顶层模块**，
    写 `from platforms.codeforces import Codeforces` 会
    `ModuleNotFoundError: No module named 'platforms'`。
  * `tests/` 和 `selftest.py` 按**顶层模块**加载：把插件目录塞进 `sys.path` 之后
    `from core import accounts`，此时 `core` 是顶层包，
    相对导入 `..platforms` 会是 "attempted relative import beyond top-level package"。

这个差异真实炸过一次：真机上账号绑定页显示
「读状态失败：No module named 'platforms'」——
而全部测试都是绿的，因为测试跑的是第二种加载方式。
所以别再写成裸的 `from platforms.x import Y`，一律走 `import_platform()`。
"""

from __future__ import annotations

import importlib


def import_platform(name: str):
    """导入 `platforms.<name>`，两种加载方式都能用。

    判据是 `__package__` 里有没有点：包加载时它是
    `data.plugins.astrbot_plugin_xcpc.core`（有点），顶层加载时是 `core`（没点）。

    **不用 try/except ImportError 兜底** —— 那样会把 `platforms/` 里真实的
    ImportError 一起吞掉，然后报一句"没有名为 platforms 的模块"，
    把"我写错了"伪装成"环境不对"。
    """
    pkg = __package__ or ""
    if "." in pkg:
        return importlib.import_module("..platforms." + name, pkg)
    return importlib.import_module("platforms." + name)
