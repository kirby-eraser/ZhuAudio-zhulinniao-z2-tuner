"""
diagnose_io.py — 诊断 [Errno 5] Input/Output Error
==================================================
只发只读 READ 帧（0x66 pregain），不写入设备、不改配置持久化状态。
按顺序测试不同连接策略，找出 Windows 上哪种能稳定收发。
"""
from __future__ import annotations

import sys
import traceback

import usb.core
import usb.util
import usb_backend

VID, PID = 0xF122, 0xF113
IFACE, EP_OUT, EP_IN = 3, 0x03, 0x83
FRAME = bytes([0x4B, 0x66, 0, 0, 0, 0x52, 0, 0, 0, 0, 0])


def desc(e) -> str:
    return f"{type(e).__name__}: {e}"


def one_transfer(dev, tag):
    """发一次只读帧并尝试读回，返回 (write_ok, rx_hex, err)"""
    try:
        n = dev.write(EP_OUT, FRAME, 500)
        w = f"write ok ({n} bytes)"
    except Exception as e:
        return False, None, f"WRITE 失败 -> {desc(e)}"
    rx = None
    for _ in range(3):
        try:
            d = bytes(dev.read(EP_IN, 16, 300))
            rx = d.hex(" ")
            break
        except Exception as e:
            rx = f"READ: {desc(e)}"
    return True, rx, None


def check(msg, fn):
    try:
        r = fn()
        print(f"    {msg}: OK {r if r is not None else ''}")
        return True
    except Exception as e:
        print(f"    {msg}: {desc(e)}")
        return False


def main():
    print("=== 后端 ===")
    try:
        b = usb_backend.get_backend()
        print("  backend:", b)
    except Exception as e:
        print("  后端失败:", desc(e))
        return

    dev = usb_backend.find_device(VID, PID)
    if dev is None:
        print("  !! 未找到 Z2 0xf122:0xf113 —— 请确认耳机已插入、且已关闭正在运行的调音台")
        return
    print("  找到设备:", dev)

    print("\n=== 设备当前状态 ===")
    check("当前 configuration", lambda: dev.get_active_configuration())
    try:
        cfg = dev.get_active_configuration()
        for i, intf in enumerate(cfg):
            eps = [(hex(e.bEndpointAddress), e.wMaxPacketSize) for e in intf]
            print(f"    接口 {i}: class=0x{intf.bInterfaceClass:02x}  端点={eps}")
    except Exception as e:
        print("    枚举接口失败:", desc(e))
    check("kernel driver active(接口3)",
          lambda: usb.core.Device.is_kernel_driver_active(dev, IFACE))

    print("\n=== 策略 A：不 set_configuration，先 claim 再收发（推荐） ===")
    try:
        d = usb_backend.find_device(VID, PID)
        check("claim_interface(3)", lambda: usb.util.claim_interface(d, IFACE))
        ok, rx, err = one_transfer(d, "A")
        print("   ", err or f"{ok} rx={rx}")
    except Exception as e:
        print("    策略 A 异常:", desc(e))
    finally:
        try:
            usb.util.release_interface(d, IFACE)
            usb.util.dispose_resources(d)
        except Exception:
            pass

    print("\n=== 策略 B：set_configuration() 后 claim 再收发（现有 connect() 的做法） ===")
    try:
        d = usb_backend.find_device(VID, PID)
        check("set_configuration()", lambda: d.set_configuration())
        check("claim_interface(3)", lambda: usb.util.claim_interface(d, IFACE))
        ok, rx, err = one_transfer(d, "B")
        print("   ", err or f"{ok} rx={rx}")
    except Exception as e:
        print("    策略 B 异常:", desc(e))
    finally:
        try:
            usb.util.release_interface(d, IFACE)
            usb.util.dispose_resources(d)
        except Exception:
            pass

    print("\n=== 策略 C：不 claim，直接收发 ===")
    try:
        d = usb_backend.find_device(VID, PID)
        ok, rx, err = one_transfer(d, "C")
        print("   ", err or f"{ok} rx={rx}")
    except Exception as e:
        print("    策略 C 异常:", desc(e))
    finally:
        try:
            usb.util.dispose_resources(d)
        except Exception:
            pass

    print("\n=== 策略 D：hidapi（走系统原生 HID 驱动，不抢接口） ===")
    try:
        import hid  # type: ignore
        print("  hidapi 可用，枚举 VID/PID:")
        found = [x for x in hid.enumerate(VID, PID)]
        for x in found:
            print("   ", {k: x.get(k) for k in
                          ("vendor_id", "product_id", "usage_page", "usage",
                           "interface_number", "path")})
        for x in found:
            try:
                h = hid.device()
                h.open_path(x["path"])
                n = h.write(FRAME)
                data = h.read(16, timeout_ms=300)
                print(f"    interface {x.get('interface_number')}: write={n} read={data}")
                h.close()
            except Exception as e:
                print(f"    interface {x.get('interface_number')}: {desc(e)}")
    except ImportError:
        print("  未安装 hidapi（`pip install hidapi`），跳过")
    except Exception as e:
        print("  hidapi 失败:", desc(e))

    print("\n=== 诊断结束 ===")


if __name__ == "__main__":
    main()
