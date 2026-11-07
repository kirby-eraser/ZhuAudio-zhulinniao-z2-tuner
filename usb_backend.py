"""
usb_backend.py — USB 后端自动探测（解决 Windows 上 pyusb 找不到 libusb 的问题）
============================================================================
pyusb 默认会去系统里找 libusb-1.0.dll；Windows 上通常没有，
于是 usb.core.find() 抛 "No backend available"。

本模块按优先级尝试：
  1. 系统自带 libusb-1.0.dll（若用户已装 / 已放 PATH）
  2. libusb-package 随包自带的 DLL（pip install libusb-package 即可，最省事）
  3. libusb0 / openusb 后端
  4. 都没有 -> 抛带明确指引的错误

所有 find 都显式传入 backend，不依赖 pyusb 的隐式全局状态。
"""

from __future__ import annotations

import usb.core
import usb.backend.libusb1
import usb.backend.libusb0
import usb.backend.openusb


def get_backend():
    """返回一个可用的 usb 后端对象，找不到则抛 RuntimeError。"""
    # 1) 系统 DLL
    b = usb.backend.libusb1.get_backend()
    if b is not None:
        return b

    # 2) libusb-package 自带的 DLL（推荐）
    try:
        import libusb_package
        b = usb.backend.libusb1.get_backend(
            find_library=libusb_package.find_library)
        if b is not None:
            return b
    except ImportError:
        pass

    # 3) 其它后端
    for mod in (usb.backend.libusb0, usb.backend.openusb):
        try:
            b = mod.get_backend()
            if b is not None:
                return b
        except Exception:
            pass

    raise RuntimeError(
        "找不到可用的 USB 后端。请任选其一：\n"
        "  A) pip install libusb-package   （推荐，自带 DLL，无需手动配置）\n"
        "  B) 下载 libusb-1.0.dll 放到 PATH 或 Python 目录\n"
        "  C) 用 Zadig 给目标设备绑定 WinUSB 驱动"
    )


def find_all():
    """列出所有 USB 设备（显式传入后端）。"""
    return usb.core.find(find_all=True, backend=get_backend())


def find_device(vid: int, pid: int):
    """按 VID/PID 查找单个设备。"""
    return usb.core.find(idVendor=vid, idProduct=pid, backend=get_backend())
