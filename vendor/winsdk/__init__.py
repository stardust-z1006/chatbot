# -*- coding: utf-8 -*-
"""winsdk 兼容适配层（Python 3.13 专用）。

背景
----
微软官方的 ``winsdk`` 只发到 1.0.0b10，轮子最高支持 cp312，Python 3.13 装不上
（源码编译要 MSVC 和 scikit-build，普通机器没有）。它的继任者是 pywinrt，把
命名空间拆成了 ``winrt.windows.*``。

wechatauto 里只有「OCR 识别」一条路径用到 winsdk，一共三个类。这里把那三个
名字原样转发到 winrt 上，于是 ``from winsdk.windows.media.ocr import OcrEngine``
在 3.13 下也能成立。

用法：把 ``vendor/`` 放进 sys.path（run.py 已经做了），不需要装任何东西。
"""
