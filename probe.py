"""
probe.py — 竹林鸟 霜华 Z2 USB 设备探测器
============================================================================
用途：插上 Z2 后运行本脚本，列出所有 USB 设备，并详细打印 Z2 的配置描述符，
帮你确认：
  1. Z2 的 VID / PID（填进 protocol.py 的 Z2_VID / Z2_PID）
  2. 哪个接口是"厂商私有控制通道"（安卓 App 走的那条）
  3. 端点的地址/方向/类型（填进 protocol.py 的 CTRL_INTERFACE / EP_OUT / EP_IN）

本脚本只读取设备描述符，不会向设备写任何东西，绝对安全。

运行：
    python probe.py
（需要 libusb 后端：pip install libusb-package 即可，自带 DLL；
 实机验证 HID 接口 3 可直接 claim，不需要 Zadig 绑定 WinUSB）
"""

from __future__ import annotations

import sys
import usb.core
import usb.util
import usb_backend


def _safe_str(dev, index):
    try:
        return usb.util.get_string(dev, index) or ""
    except Exception:
        return ""


def list_all_devices():
    print("=" * 70)
    print("本机 USB 设备清单（插拔 Z2 前后各跑一次，对比差异即可定位）")
    print("=" * 70)
    try:
        devices = list(usb_backend.find_all())
    except RuntimeError as e:
        print(f"[!] {e}")
        return
    print(f"共枚举到 {len(devices)} 个 USB 设备\n")
    for dev in devices:
        vid, pid = dev.idVendor, dev.idProduct
        manuf = _safe_str(dev, dev.iManufacturer)
        product = _safe_str(dev, dev.iProduct)
        serial = _safe_str(dev, dev.iSerialNumber)
        print(f"  {vid:#06x}:{pid:#06x}  {manuf!r} / {product!r}  sn={serial!r}")


def dump_device(vid: int, pid: int):
    dev = usb_backend.find_device(vid, pid)
    if dev is None:
        print(f"[!] 未找到 {vid:#06x}:{pid:#06x}")
        return
    print("=" * 70)
    print(f"设备 {vid:#06x}:{pid:#06x}")
    print(f"  厂商: {_safe_str(dev, dev.iManufacturer)!r}")
    print(f"  产品: {_safe_str(dev, dev.iProduct)!r}")
    print(f"  序列: {_safe_str(dev, dev.iSerialNumber)!r}")
    print(f"  bcdUSB: {dev.bcdUSB:#06x}  bDeviceClass: {dev.bDeviceClass:#04x}")
    print(f"  配置数: {dev.bNumConfigurations}")
    try:
        cfg = dev.get_active_configuration()
    except Exception as e:
        print(f"  无法读取配置（可能需要 WinUSB 绑定）: {e}")
        return

    for interface in cfg:
        print("-" * 70)
        print(f"  接口 {interface.bInterfaceNumber}: "
              f"类={interface.bInterfaceClass:#04x} "
              f"子类={interface.bInterfaceSubClass:#04x} "
              f"协议={interface.bInterfaceProtocol:#04x} "
              f"({interface.bAlternateSetting})")
        try:
            print(f"    接口名: {usb.util.get_string(dev, interface.iInterface)!r}")
        except Exception:
            pass
        for ep in interface:
            direction = "IN " if (ep.bEndpointAddress & 0x80) else "OUT"
            etype = ep.bmAttributes & 0x03
            type_name = {0: "CTRL", 1: "ISO", 2: "BULK", 3: "INT"}.get(etype, "?")
            print(f"    端点 {direction} 0x{ep.bEndpointAddress:02x} "
                  f"类型={type_name} 最大包={ep.wMaxPacketSize}")
        # 若是 HID 接口，尝试打印 HID 描述符（常含厂商自定义报告）
        if interface.bInterfaceClass == 0x03:
            print("    [HID 接口] 提示：App 可能通过 HID 特征报告发 EQ 指令")
    print("=" * 70)
    print("下一步：把上面的 VID/PID、私有接口号、端点地址填进 protocol.py 顶部常量。")


def main():
    if len(sys.argv) == 3:
        vid = int(sys.argv[1], 0)
        pid = int(sys.argv[2], 0)
        dump_device(vid, pid)
    else:
        list_all_devices()
        print()
        print("用法：python probe.py <VID> <PID>   （例如 python probe.py 0x2E8C 0xXXXX）")
        print("如果只知道是 Z2 但不知道 VID/PID，先插上耳机运行 python probe.py 看清单。")


if __name__ == "__main__":
    main()
