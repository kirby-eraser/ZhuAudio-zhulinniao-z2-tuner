"""
protocol.py — 竹林鸟 霜华 Z2 (Type-C DAC / KTMicro) PC 通信层【官方协议已核对】
============================================================================
协议来源（按可信度排序）
  1. 官方安卓 App `base.apk` 反编译（com.yscoco.zhuaudio；协议类被 R8 混淆成
     `com.bumptech.glide.l`，日志 TAG = "UsbHidEqManager"）——逐字节确认
  2. 本机对 VID 0xF122:0xF113 实机只读验证（probe / scan / verify）
  3. 开源交叉印证：devicePEQ / ktmicro-tools / ja11-config

物理通道
  USB 复合设备：接口 0/1/2 = UAC2 音频（Windows 自带驱动直接出声）
                接口 3     = HID 类私有控制通道
  EP OUT 0x03 / EP IN 0x83；App 用 bulkTransfer 收发 11 字节，PC 用 pyusb 等价

官方帧格式（11 字节，来自 glide/l.r(byte cmd, int reg, int value)）
  [0]=0x4B (Report ID)  [1]=reg  [2..4]=0  [5]=cmd  [6]=0
  [7..10]=value，uint32 小端
  CMD: 0x52 READ / 0x57 WRITE / 0x53 COMMIT(应用+保存) / 0x43 CLEAR(仅开源工具用)
  应答同帧，数据在 recv[7..10]（官方 glide/l.l([B)I 取 recv[7..10]）
  App 仅在 0x52/0x57/0x53 时检查 recv[7] 是否为 0x4F('O') 或 value 低字节，
  不匹配只打日志、不报错，因此本层不强制 ACK 校验。

官方 EQ 编码（glide/l.n() 读 / glide/l.s() 写）
  5 段，段 i 用两个寄存器：baseA+2i 写 gain|freq<<16，baseB+2i 写 Q
    gain = signed16 / 10 dB（App clamp 到 ±12 dB）
    freq = uint16，直接写 Hz（App clamp 到 20..20000）——**无 2X 补偿**
    Q    = uint16 / 1000（App clamp 到 0.2..10）
  base 随设备型号选择：eqIndex==0 → 0x1A/0x1B；否则 0x35/0x36 或 0x26/0x27
  写完 5 段后发一次 COMMIT(0x53) 即「应用并保存」，**没有独立的烧录命令**。

Z2 实机寄存器布局（verify_apk_protocol.py 严格只读复核）
  0x00-0x2C 固件信息（"REG:0231" / "-- KTMicro --" / "Z2" / 序列号 / VID:PID）
  0x34      组 A 使能 (=1)      0x35-0x40 组 A：6 段 ×2 寄存器
  0x41      组 B 使能 (=1)      0x42-0x4D 组 B：6 段 ×2 寄存器（值与 A 相同）
  0x56      当前预设槽 (=3)     0x60      魔数 0x12345678
  0x66      全局增益 pregain
  默认 EQ：61 / 122 / 184 / 248 / 316 / 392 Hz，Q=0.700，gain=0 dB

⚠️ 官方只写 5 段、只写组 A、不碰使能位；本工具额外支持第 6 段与组 B
   （实机确认这些寄存器确实是 EQ）。旧的 official=True「仅5段」模式已废弃：
   实机验证只写 5 段会让 COMMIT(0x53) 保存失败，现统一写满 6 段。
⚠️ 写入会改变耳机音色；首次使用请先 backup() 留底，必要时 restore() 恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time

import usb.core
import usb.util
import usb_backend


class USBIOError(RuntimeError):
    """USB 传输层错误（已尝试自动恢复仍失败），附带可操作的中文提示。"""


# libusb 常见可恢复错误码：
#   5  LIBUSB_ERROR_IO        端点 STALL / 设备忙 / 句柄失效
#   19 LIBUSB_ERROR_NO_DEVICE 设备重新枚举或已拔出
#   32 LIBUSB_ERROR_PIPE      端点 halted
#   110 LIBUSB_ERROR_TIMEOUT  瞬时超时
RECOVERABLE_ERRNOS = {5, 19, 32, 110}


def _is_recoverable(exc: BaseException) -> bool:
    """判断是否值得做 clear_halt / 重连后重试。"""
    if isinstance(exc, usb.core.USBError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in RECOVERABLE_ERRNOS

# ---------------------------------------------------------------------------
# 设备 & 协议常量
# ---------------------------------------------------------------------------
Z2_VID, Z2_PID = 0xF122, 0xF113
CTRL_INTERFACE = 3
EP_OUT, EP_IN = 0x03, 0x83
REPORT_ID = 0x4B
FRAME_LEN = 11

CMD_READ, CMD_WRITE, CMD_COMMIT, CMD_CLEAR = 0x52, 0x57, 0x53, 0x43

ENABLE_REGS = (0x34, 0x41)
SLOT_REG, PREGAIN_REG = 0x56, 0x66
# (起始寄存器, 段数)；段 i 用 base+2i (gain/freq) 与 base+2i+1 (Q/type)
EQ_GROUPS = ((0x35, 6), (0x42, 6))
BANDS_PER_GROUP = 6
OFFICIAL_BANDS = 5            # 官方 App 只调 5 段
OFFICIAL_BASE = (0x35, 0x36)  # Z2 所属的 KTMicro 布局

TYPE_PK, TYPE_LSQ, TYPE_HSQ = 0, 3, 4
TYPE_NAMES = {0: "PK", 3: "LSQ", 4: "HSQ"}

# 官方 App 的 clamp 范围（EqUsbSettingActivity.t()）
GAIN_MIN, GAIN_MAX = -12.0, 12.0
FREQ_MIN, FREQ_MAX = 20, 20000
Q_MIN, Q_MAX = 0.2, 10.0

DEFAULT_BANDS = [(61, 0.0), (122, 0.0), (184, 0.0),
                 (248, 0.0), (316, 0.0), (392, 0.0)]

@dataclass
class Band:
    freq: float = 100.0     # Hz
    gain: float = 0.0       # dB
    q: float = 0.7
    type: int = TYPE_PK     # 0=Peak, 3=LowShelf, 4=HighShelf


def clamp_band(b: Band) -> Band:
    """按官方 App 的范围做限幅，避免把越界值写进 DSP。"""
    return Band(freq=float(min(FREQ_MAX, max(FREQ_MIN, int(round(b.freq))))),
                gain=float(min(GAIN_MAX, max(GAIN_MIN, b.gain))),
                q=float(min(Q_MAX, max(Q_MIN, b.q))),
                type=int(b.type) & 0xFF)


def _u16le(v: int) -> bytes:
    v = int(round(v)) & 0xFFFF
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


def _s16le(v: float) -> bytes:
    return _u16le(round(v * 10))


class Z2Device:
    def __init__(self, vid: int = Z2_VID, pid: int = Z2_PID):
        self.vid, self.pid = vid, pid
        self.dev = None
        self._claimed = False

    # ---- 连接 ---------------------------------------------------------
    def _release_handle(self):
        """释放当前 USB 句柄（失效句柄上的调用一律吞掉异常）。"""
        d, self.dev = self.dev, None
        claimed, self._claimed = self._claimed, False
        if d is None:
            return
        try:
            if claimed:
                usb.util.release_interface(d, CTRL_INTERFACE)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(d)
        except Exception:
            pass

    def connect(self) -> bool:
        """打开 Z2 的 HID 控制接口。

        注意：不再无条件调用 set_configuration()——设备已由 Windows 配置好，
        重设会重置正在播放的 UAC 音频接口，正是 [Errno 5] 的常见诱因。
        """
        dev = usb_backend.find_device(self.vid, self.pid)
        if dev is None:
            raise RuntimeError("未找到 Z2，请确认耳机已插入 USB-C 口")
        self.dev = dev
        try:
            if dev.get_active_configuration() is None:
                dev.set_configuration()
        except Exception:
            pass
        try:
            usb.util.claim_interface(dev, CTRL_INTERFACE)
            self._claimed = True
        except Exception:
            # 实测：Windows 上即便 claim 失败，直接 write/read 仍可能可用，
            # 因此 here 不致命，交给 ping() 做真实收发验证。
            self._claimed = False
        self.drain()
        return True

    def reconnect(self) -> bool:
        """强制重连：用于处理设备重新枚举 / COMMIT 后软复位导致的旧句柄失效。"""
        self._release_handle()
        # 设备重新枚举可能需要一点时间
        time.sleep(0.45)
        last = None
        for _ in range(12):
            try:
                return self.connect()
            except Exception as e:
                last = e
                time.sleep(0.3)
        raise RuntimeError(
            f"重新连接 Z2 失败：{last}。请拔下耳机再插回，"
            "并关闭其它可能占用 HID 接口的程序（官方 PC 驱动 / devicePEQ 网页 / 另一个调音台窗口）。")

    def ping(self) -> bool:
        """真实收发一次只读帧，验证当前句柄确实可用（claim 成功不代表能通信）。"""
        try:
            return self.read_reg32(PREGAIN_REG) is not None
        except Exception:
            return False

    def _recover(self, level: int):
        """出错后的分级自愈：先清端点 halt，再整体重连。"""
        if level == 1 and self.dev is not None:
            for ep in (EP_OUT, EP_IN):
                try:
                    self.dev.clear_halt(ep)
                except Exception:
                    pass
            time.sleep(0.06)
        else:
            self.reconnect()

    def disconnect(self):
        self._release_handle()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.disconnect()
        return False

    # ---- 低层帧（与官方 glide/l.r 完全一致） ---------------------------
    def drain(self, tries: int = 8):
        """清空 IN 端点的残留回包，避免读到上一条命令的数据。"""
        for _ in range(tries):
            try:
                self.dev.read(EP_IN, 16, 20)
            except Exception:
                return

    def _transaction(self, cmd: int, reg: int, value: int = 0) -> bytes | None:
        """一次完整事务：发 11 字节帧并读回应答 recv[7..10]。

        自愈策略（针对 [Errno 5] / 设备重新枚举 / 端点 STALL）：
          第 1 次失败 → clear_halt(OUT/IN) 后整帧重发
          第 2 次失败 → 释放句柄、重新枚举并重连后整帧重发
          第 3 次失败 → 抛 USBIOError，附可操作的中文指引
        普通读超时不算错误，返回 None（请求-应答模式的正常情况）。
        """
        v = value & 0xFFFFFFFF
        frame = bytes([REPORT_ID, reg & 0xFF, 0, 0, 0, cmd & 0xFF, 0,
                       v & 0xFF, (v >> 8) & 0xFF,
                       (v >> 16) & 0xFF, (v >> 24) & 0xFF])
        last = None
        # READ 需要等固件准备数据；WRITE/COMMIT 的应答给短超时即可，
        # 否则 12 个 EQ 寄存器会等十几秒。
        read_timeout = 300 if cmd == CMD_READ else 60
        read_tries = 4 if cmd == CMD_READ else 3
        for attempt in range(3):
            try:
                self.dev.write(EP_OUT, frame, 500)
                for _ in range(read_tries):
                    try:
                        rx = bytes(self.dev.read(EP_IN, 16, read_timeout))
                    except Exception as e:
                        # 仅把 IO/NO_DEVICE/PIPE 视为需要恢复；超时就继续等
                        if _is_recoverable(e) and \
                                getattr(e, "errno", None) in (5, 19, 32):
                            raise
                        continue
                    if len(rx) >= FRAME_LEN and rx[0] == REPORT_ID \
                            and rx[1] == reg:
                        return rx[7:11]
                return None
            except Exception as e:
                last = e
                if cmd in (CMD_COMMIT, CMD_CLEAR):
                    # 提交类命令可能已生效、且设备常在此刻软复位，绝不重发
                    try:
                        self._recover(1)
                    except Exception:
                        pass
                    return None
                if _is_recoverable(e) and attempt < 2:
                    self._recover(attempt + 1)
                    continue
                raise USBIOError(
                    f"USB 传输失败（{last}）；已自动尝试清除端点 halt 并重新枚举设备。\n"
                    "请依次检查：① 拔下 Z2 再插回；② 关闭其它占用 HID 的程序"
                    "（官方 PC 驱动、devicePEQ 网页、另一个调音台窗口）；"
                    "③ 暂停正在播放的音乐再重试；④ 右键以管理员身份运行。") from e
        return None

    def transfer(self, cmd: int, reg: int, value: int = 0) -> int | None:
        """官方语义的单次事务：发 11 字节帧，返回 uint32 应答（无应答返回 None）。"""
        d = self._transaction(cmd, reg, value)
        return int.from_bytes(d, "little") if d is not None else None

    def read_reg(self, reg: int) -> bytes | None:
        return self._transaction(CMD_READ, reg)

    def read_reg32(self, reg: int) -> int | None:
        d = self.read_reg(reg)
        return int.from_bytes(d, "little") if d is not None else None

    def write_reg(self, reg: int, data4: bytes):
        assert len(data4) == 4
        # 官方 r() 每次写都会读回一次应答，_transaction 已消费，不会在 IN 端点堆积
        self._transaction(CMD_WRITE, reg, int.from_bytes(data4[:4], "little"))

    def write_reg32(self, reg: int, value: int):
        self.write_reg(reg, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def command(self, cmd: int):
        """发全局命令（COMMIT / CLEAR）。reg=0, value=0。"""
        self._transaction(cmd, 0)

    # ---- 高层 EQ -------------------------------------------------------
    def get_eq(self, group: int = 0) -> list[Band]:
        """读取 EQ（默认组 A）。返回 6 个 Band。"""
        base, count = EQ_GROUPS[group]
        bands = []
        for i in range(count):
            gf = self.read_reg(base + 2 * i)
            qt = self.read_reg(base + 2 * i + 1)
            if gf is None or qt is None:
                raise RuntimeError(f"读取第 {i} 段寄存器超时")
            gain = int.from_bytes(gf[0:2], "little", signed=True) / 10.0
            freq = int.from_bytes(gf[2:4], "little", signed=False)
            q = int.from_bytes(qt[0:2], "little", signed=False) / 1000.0
            bands.append(Band(freq=float(freq), gain=gain, q=q, type=qt[2]))
        return bands

    def set_eq(self, bands: list, groups: tuple = (0, 1),
               official: bool = False, clamp: bool = True):
        """写入 EQ（只写寄存器，需再 commit()/burn_eq() 才应用并保存）。

        ⚠️ official 参数已废弃，仅为兼容旧调用而保留，内部不再区分模式。
        原因：旧的「官方兼容(仅5段)」只写 5 段、只写组 A，且 Q 寄存器漏写
        type 字节，实机 COMMIT(0x53) 会失败；GUI 已移除该勾选项。
        现在统一写满 6 段、每组都写完整的 Q/type 4 字节，保存才稳定。
        """
        norm: list[Band] = []
        for b in bands[:BANDS_PER_GROUP]:
            if isinstance(b, Band):
                norm.append(b)
            elif isinstance(b, (tuple, list)):
                norm.append(Band(freq=float(b[0]), gain=float(b[1]),
                                 q=float(b[2]) if len(b) > 2 else 0.7,
                                 type=int(b[3]) if len(b) > 3 else TYPE_PK))
        count = BANDS_PER_GROUP
        while len(norm) < count:
            norm.append(Band(freq=1000.0, gain=0.0, q=0.7))
        norm = norm[:count]
        if clamp:
            norm = [clamp_band(b) for b in norm]

        for gi in groups:
            base, _ = EQ_GROUPS[gi]
            for i, b in enumerate(norm):
                self.write_reg(base + 2 * i, _s16le(b.gain) + _u16le(b.freq))
                self.write_reg(base + 2 * i + 1,
                               _u16le(b.q * 1000) + bytes([b.type & 0xFF, 0]))
                time.sleep(0.02)
        return norm

    def enable_eq(self, enable: bool = True):
        """打开/关闭两组 EQ 使能位（官方不写，仅在需要时手动调用）。"""
        for reg in ENABLE_REGS:
            self.write_reg(reg, bytes([1 if enable else 0, 0, 0, 0]))

    def set_pregain(self, db: float):
        v = int(round(db)) & 0xFF
        self.write_reg(PREGAIN_REG, bytes([v, 0, 0, 0]))

    def get_pregain(self) -> float:
        d = self.read_reg(PREGAIN_REG)
        if d is None:
            return 0.0
        v = d[0]
        return float(v - 256 if v > 127 else v)

    def commit(self):
        """COMMIT 0x53 —— 官方 App 唯一的「应用 + 保存」动作，无独立烧录命令。

        部分固件在 COMMIT 后会软复位 / 重新枚举，旧 USB 句柄随即失效
        （表现为之后读取弹 [Errno 5]）。这里主动探测并把句柄刷新到新设备。
        """
        self.command(CMD_COMMIT)
        time.sleep(0.45)
        try:
            if not self.ping():
                self.reconnect()
        except Exception:
            try:
                self.reconnect()
            except Exception:
                pass  # 设备真的被拔了也不应让「应用并保存」报错

    def burn_eq(self, set_enable: bool = False):
        """兼容旧名：把当前寄存器提交到 DSP 并保存。

        官方 App 不写使能位（Z2 出厂 0x34/0x41 已为 1），故默认不写；
        需要强制开启时传 set_enable=True。
        """
        if set_enable:
            self.enable_eq(True)
        self.commit()

    # ---- 备份 / 恢复（写入前务必备份） ---------------------------------
    def backup(self, path: str = "regs_backup.json") -> dict:
        """只读备份 0x00-0xFF 全部寄存器；每个寄存器连读 2 次校验，避免错位。"""
        self.drain()
        data = {}
        for reg in range(0x100):
            a = self.read_reg(reg)
            b = self.read_reg(reg)
            if a is not None and b is not None and a != b:
                c = self.read_reg(reg)          # 三次取一致值，避免残留错位
                a = c if c in (a, b) else b
            data[f"{reg:02x}"] = list(a) if a is not None else None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.drain()
        return data

    def restore(self, path: str = "regs_backup.json",
                regs: list[int] | None = None, commit: bool = True) -> int:
        """从备份 JSON 恢复寄存器（危险操作，仅在写错后使用）。

        regs=None 表示恢复全部 0x00-0xFF；也可只给 EQ 区 reg 列表。
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        targets = regs if regs is not None else range(0x100)
        n = 0
        for reg in targets:
            key = reg if isinstance(reg, str) else f"{reg:02x}"
            d = data.get(key)
            if d is None:
                continue
            r = int(key, 16) if isinstance(key, str) else int(reg)
            self.write_reg(r, bytes(d[:4]))
            n += 1
            time.sleep(0.01)
        if commit:
            self.commit()
        return n


if __name__ == "__main__":
    with Z2Device() as z:
        print("Z2 已连接，读取当前 EQ（只读）:")
        for i, b in enumerate(z.get_eq()):
            print(f"  Band{i + 1}: {b.freq:>6.0f} Hz  {b.gain:+.1f} dB  "
                  f"Q={b.q:.3f}  {TYPE_NAMES.get(b.type, b.type)}")
        print(f"  Pregain: {z.get_pregain():+.0f} dB")
        print("  组 B 前两段:", [(b.freq, b.gain, b.q) for b in z.get_eq(1)[:2]])
