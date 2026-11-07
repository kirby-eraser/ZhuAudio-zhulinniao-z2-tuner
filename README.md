# 竹林鸟 霜华 Z2 · PC 调音台

> 给 **竹林鸟霜华 Z2（Type-C 带 DAC 版）** 用的开源 Windows 调音工具。
> 官方只有安卓 App，本项目把「EQ 调音 / 云端音效 / 恢复出厂」搬到了 PC。

**非官方项目**，与竹林鸟 / 昆腾微无任何关联。仅供个人设备使用；写入会改变耳机音色，**首次使用前请先备份寄存器**。

---

## 建议的仓库信息

| | 内容 |
|---|---|
| 仓库名 | `zhulinniao-z2-tuner` |
| 简介（中文） | 竹林鸟霜华 Z2（Type-C DAC）的开源 PC 调音台：逆向 KTMicro USB HID 协议，支持 6 段参数 EQ、官方/社区音效预设、寄存器备份与一键恢复出厂。 |
| Description (EN) | Open-source Windows EQ tuner for Zhulinniao Z2 USB-C DAC earbuds — reverse-engineered KTMicro HID protocol, 6-band PEQ, official & community presets, register backup and factory reset. |
| Topics | `equalizer` `usb-hid` `reverse-engineering` `python` `tkinter` `zhulinniao` `ktmicro` `dac` |

---

## 大致原理（3 分钟版）

**1. 它不是「驱动」，也不缺驱动。**
Z2 是有线 Type-C 耳机，内置 DAC/DSP 芯片。插到电脑上会被识别成标准 **USB Audio Class 2.0** 设备，系统自带驱动就能出声。真正缺的是官方安卓 App 独占的 **EQ 调音** 功能。

**2. 控制通道是一条 USB HID 接口。**
设备 `VID:PID = 0xF122:0xF113`，厂商字符串 `-- KTMicro --`（昆腾微，不是部分评测写的恒玄）。除音频接口外，它还有一个 **HID 接口（Interface 3）**，端点 `OUT 0x03 / IN 0x83`，用来读写 DSP 寄存器。Windows 自带 HID 驱动即可访问，**不需要 Zadig、不需要管理员权限**。

**3. 通信帧与寄存器。**
每次事务 11 字节：`Report ID 0x4B` + `[reg, 0,0,0, cmd, 0, v0..v3]`，value 为 uint32 小端。

| 命令 | 值 | 含义 |
|---|---|---|
| READ | `0x52` | 读寄存器 |
| WRITE | `0x57` | 写寄存器 |
| COMMIT | `0x53` | **应用并保存到 Flash** |
| CLEAR | `0x43` | 清除 |

参数编码：增益 `int16 ÷ 10 dB`、频率 `uint16 Hz`（原样写，无 2X 补偿）、Q `uint16 ÷ 1000`、滤波类型 `0=PK / 3=LSQ / 4=HSQ`。
寄存器布局：`0x34` 组 A 使能、`0x35–0x40` 组 A **6 段 EQ**、`0x41/0x42–0x4D` 组 B、`0x56` 预设槽、`0x66` 全局增益。出厂默认频点为 `61/122/184/248/316/392 Hz`，Q 全 `0.700`，增益全 0。

官方 App 只调 **5 段、只写组 A、写完一次 COMMIT 即保存**（无独立烧录命令）。所以本工具默认也只写组 A，组 B 保留给录音/备用通道。

**4. 云端音效。**
官方服务 `zln-app.zhuaudio.cn`，请求头带 `timestamp` + `sign = HMAC-SHA1(key, key+timestamp)`；登录用 5 个参数且密码为 `HMAC-SHA1(key, 明文)`。官方音效库免登录（25 条），社区 EQ 需登录。本项目**只读云端**，不上传、不点赞、不修改任何云端内容。协议细节由官方 APK 反编译逐字节核对，并参考了 `devicePEQ` / `ktmicro-tools` / `ja11-config` 等开源实现。

---

## 功能

- 6 段参数 EQ：频率 / 增益 / Q / 滤波类型，带官方参数范围校验（20–20000 Hz、±12 dB、Q 0.2–10）
- **5 条内置预设**：出厂默认（恢复默认）、澎湃低频、清晰人声、通透高频、均衡还原
- **官方云端音效库 25 条**（免登录，离线快照已去重为 **21 条**兜底）+ **社区 EQ**（登录后可下载别人的调音）
- 预设导入 / 导出 `.json`，方便分享
- 256 个寄存器全量备份 + 一键恢复
- 两套界面：推荐 **`gui_app.py`（仿官方 App 风格）**，另有 **`gui.py`（极简稳定版）** 作为备选
- 单文件 `exe`，免装 Python

---

## 环境要求

| | 要求 |
|---|---|
| 系统 | Windows 10 / 11（设备通过 USB-C 连接） |
| Python（源码运行） | **3.10+**（用了 `str \| None` / `list[T]` 类型标注），推荐 3.11–3.13 |
| 依赖 | `pyusb>=1.2.1`、`libusb-package>=1.0.26` |
| 其它 | **无需** Zadig、无需 `libusb-1.0.dll` 手动放置、无需管理员权限。`libusb-package` 自带 DLL |

```powershell
python -m pip install -r requirements.txt
python gui_app.py      # App 风格版（推荐）
# python gui.py       # 极简稳定版（备选）
```

> Linux / macOS 理论上可用（pyusb + libusb），但界面与设备访问只在 Windows 实机验证过。

### 打包成单文件 exe（可选）

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name Z2Tuner ^
  --icon assets/app.ico --add-data "assets;assets" ^
  --collect-all libusb_package --collect-all usb ^
  --hidden-import protocol --hidden-import usb_backend --hidden-import cloud_eq gui_app.py
```

产物在 `dist\Z2Tuner.exe`。也可直接双击项目自带的 `build_exe.bat`（默认打包 App 风格版）。

---

## 操作流程（第一次用）

1. 插上 Z2，打开调音台（exe 或 `python gui.py`）。
2. 点 **连接设备** → 会做一次真实读写验证，状态灯变绿。
3. 点 **备份寄存器**（生成 `regs_backup.json`）——**务必保留**，这是写错后唯一的还原依据。
4. 点 **读取 EQ**，看耳机里当前的曲线。
5. 在「官方音效库」**点一条预设**（或手动拖滑块），参数会直接载入左侧并显示在曲线上。
6. 点顶部 **应用并保存** → 写入 + COMMIT。
7. 想验证掉电保持：拔下 Type-C → 等 2 秒 → 插回 → **读取 EQ**，改动应仍在。
8. 想回到出厂：选列表最上面的 **★ 出厂默认（恢复默认）** → **应用并保存**。

> 拔插设备后若报 `[Errno 5]`，点 **重新连接** 或关掉软件重开即可（USB 句柄失效，工具已内置自动重连）。

---

## 每个按钮 / 控件的作用

### 顶部工具栏

| 控件 | 作用 |
|---|---|
| **连接设备** | 接管 HID 接口 3，并做一次真实读写验证；不是只 claim 就算成功 |
| **重新连接** | 释放旧句柄并重新枚举设备。拔插耳机、报 I/O 错误时用 |
| **读取 EQ** | 只读，把耳机里当前 6 段 EQ 和全局增益读进滑块 |
| **备份寄存器** | 只读扫描 0x00–0xFF 全部 256 个寄存器，保存为 `regs_backup.json`（双读校验） |
| **试听(写RAM)** | 把滑块参数写进寄存器并立即试听，但**不执行 COMMIT**，掉电不保留；用于对比试听，想保存请点「应用并保存」 |
| **应用并保存** | 写入 + `COMMIT 0x53`，这是官方唯一的「保存」动作，断电后仍生效 |
| **恢复备份** | 从 `regs_backup.json` 把 EQ 相关寄存器写回去 |
| **全部归零** | 把 6 段增益设为 0 dB（只改界面，需再点应用并保存才写入） |

### 左侧 EQ 区

| 控件 | 作用 |
|---|---|
| 6 行 **频率 / 增益 / Q / 类型** | 参数均衡器。频率 20–20000 Hz，增益 ±12 dB，Q 0.2–10；类型 PK / LSQ / HSQ |
| **全局增益 Pregain** | 整体音量偏移（整数 dB），防止大增益削波 |
| ☐ **两组寄存器都写** | 默认**不勾**。勾选后同时写组 B（0x42 起），一般不需要；官方 App 从不写组 B |
| ☐ **官方兼容模式** | 只写组 A 前 5 段、第 6 段保持设备原值，完全复刻官方 App。载入云端预设时会自动勾上；载入「出厂默认」时自动取消以写满 6 段 |

### 右侧「官方音效库」页

| 控件 | 作用 |
|---|---|
| **搜索** | 按名称过滤预设（内置 + 云端） |
| **刷新在线** | 重新拉取官方云端音效库（25 条，离线快照已去重为 21 条）；断网自动用内置离线快照 |
| 预设列表 | ★ 开头的是内置预设（含出厂默认），高亮显示；**单击即载入左侧参数并显示在曲线上**，下方看频段详情 |
| **导出** | 把当前滑块参数存成 `.json` 分享给别人 |
| **导入** | 读取别人的 `.json` 预设并载入滑块 |

> 选好预设、确认曲线无误后，再点顶部工具栏的 **应用并保存** 写入耳机（社区 EQ 同理）。

> 5 段预设原样写入；9 段等更多段的预设会按对数频率重采样到 Z2 的 `62/250/1000/3000/8000 Hz`。

### 右侧「社区 EQ」页

| 控件 | 作用 |
|---|---|
| **账号 / 区号 / 密码** | 官方 App 账号。区号默认 `86`；密码按官方协议加密后直连官方服务器，**不写日志、不存盘、只在内存** |
| **显示** | 勾选后密码框显示明文，方便确认没打错 |
| **登录方式** | 密码登录 / 验证码登录 |
| **发验证码** | 验证码模式下向手机发送短信验证码 |
| **登录** | 用密码或验证码登录；失败时提示条会直接显示服务端原文（账户不存在 / 密码错误…） |
| **状态** | 只显示「未登录 / 已登录」，**不显示 token** |
| **手动填 Token** | 已有 token 时可直接粘贴使用（弹窗输入，仅内存） |
| **刷新社区列表** | 拉取别人分享的 EQ（实测 `type=0` 返回 `data.allData`） |
| 预设列表 | **单击即载入左侧参数并显示在曲线上**（列表本身已含完整数据，无需额外下载） |

### 底部

| 控件 | 作用 |
|---|---|
| **提示条** | 一行人话状态（已连接 / 已保存 / 登录失败原因…），四色状态点 |
| **详细记录** | 展开完整技术日志，排障时才需要看 |

---

## 目录结构

```
zhulinniao-z2-tuner/
├── README.md                  ← 本文件
├── requirements.txt           运行依赖
├── build_exe.bat              一键打包 exe（默认 App 风格版）
├── gui_app.py                 调音台界面（仿官方 App 风格，推荐）
├── gui.py                     调音台界面（极简稳定版，备选）
├── 竹林鸟Z2调音台_App风格版.spec   App 风格版打包配置
├── protocol.py                HID 通信层（帧 / 寄存器 / 备份恢复）
├── usb_backend.py             libusb 后端自动探测
├── cloud_eq.py                官方云端接口客户端（只读）
├── probe.py                   USB 设备探测（只读，排障用）
├── diagnose_io.py             USB I/O 分步诊断（只读）
└── assets/
    ├── app.ico                exe 图标（官方 App 图标）
    ├── logo.png / earphone*.png   界面素材（来自官方 App）
    ├── presets_offline.json   官方音效离线快照（已去重，21 条）
    └── builtin_presets.json   5 条内置预设（含出厂默认）
```

---

## 注意事项

- **先备份再写入**。`COMMIT` 会把 EQ 存进芯片 Flash，虽然可用「★ 出厂默认」恢复，但备份仍是保险。
- 云端功能**只读**：不调用 `eq/save`、`eq/like`、`eq/delMyEq` 等写入接口。
- 账号密码只发往官方服务器，不写日志、不存配置文件、只保留在本次运行内存。
- 内置预设中除「出厂默认」外，4 条调音曲线为**参考值**（官方 App 的资源里只有名称，参数由运行时下发），可在滑块上自行微调。
- 本项目不含官方 APK、不含任何账号数据；发布时请勿把 `base.apk`、反编译产物或寄存器备份上传。

## 致谢

协议逆向参考了以下开源项目（KTMicro / KT02H20 系列 HID 调音）：

- [jeromeof/devicePEQ](https://github.com/jeromeof/devicePEQ)
- [gxcreator/ktmicro-tools](https://github.com/gxcreator/ktmicro-tools)
- [Ircama/ja11-config](https://github.com/Ircama/ja11-config)


