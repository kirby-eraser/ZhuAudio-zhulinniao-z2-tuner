"""
cloud_eq.py — 竹林鸟官方云端 EQ 接口客户端（由 base.apk 逆向）
============================================================
服务端：https://zln-app.zhuaudio.cn  (ThinkPHP)

公共请求头（来自 APK 的 `Lib/a;.a()` 拦截器）：
    timestamp = 秒级 Unix 时间
    sign      = HMAC-SHA1(key=SECRET, msg=SECRET + timestamp).hexdigest()
    token     = 登录 token（仅社区接口需要）
    lang / packageName 可选

密钥（APK `Lib/a;` 常量）：5de86bec5a6104b19d1b4e54050c50bf
App 常量（`com.yscoco.zhuaudio.App`）：appId / appKey

接口（参数名全部来自 APK 的 Retrofit 注解 `Lxb/b;`）：
  /index.php/home/common/soundList   官方音效库，**无需登录**（keyword）
  /index.php/home/account/login      登录：loginType / areaCode / userName / vcode / password
  /index.php/home/account/sendVerify 发验证码：areaCode / userName / vcodeType / lang
  /index.php/home/eq/list            社区 EQ 列表（pageIndex/pageSize/keyword/type/gType）
  /index.php/home/eq/download        社区 EQ 详情（eq_id）
  /index.php/home/eq/save|like|...   写入类接口，本工具不调用

**密码加密（2026-11-06 从 APK 逐字节确认）**：
  `SecretUtil.genHmacSha1(data, key)` = HMAC-SHA1(key=SECRET, msg=data)，
  输出为小写 hex。登录页 `g1.onClick` 把明文密码经此函数变换后作为 password，
  **不是 MD5、不是明文**。密钥与请求头签名用的是同一个 SECRET。
  官方登录页固定 `areaCode=86`（`const/16 v2, 86`），且必须 POST form body。

⚠️ 仅读取公开/用户自己的数据；不上传、不点赞、不修改云端内容。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://zln-app.zhuaudio.cn"
SECRET = "5de86bec5a6104b19d1b4e54050c50bf"
APP_ID = "8f226e51712480e6d31070685d500aea"
APP_KEY = "c40e35c83eab807c8db59092cfa7379a"
PKG = "com.zhuaudio.audio"

OFFICIAL_SOUNDS = "/index.php/home/common/soundList"
LOGIN = "/index.php/home/account/login"
SEND_VERIFY = "/index.php/home/account/sendVerify"
COMMUNITY_LIST = "/index.php/home/eq/list"
COMMUNITY_DOWNLOAD = "/index.php/home/eq/download"

# Z2 / 官方 App 使用的 5 个频点（手机 App 实机写入值）
Z2_TARGET_FREQS = (62, 250, 1000, 3000, 8000)


class CloudError(Exception):
    """云端接口错误（含官方返回的中文 message）。"""


def _headers(token: str | None = None) -> dict:
    ts = str(int(time.time()))
    sign = hmac.new(SECRET.encode(), (SECRET + ts).encode(), hashlib.sha1).hexdigest()
    h = {
        "User-Agent": "okhttp/3.12.0",
        "Accept": "application/json, text/plain, */*",
        "timestamp": ts,
        "sign": sign,
        "lang": "zh",
        "packageName": PKG,
    }
    if token:
        h["token"] = token
    return h


def request(path: str, params: dict | None = None,
            token: str | None = None, timeout: float = 12.0,
            method: str = "GET") -> dict:
    """官方 App 用 Retrofit @FormUrlEncoded + @POST，参数走 form body；
    只读列表接口 GET 也兼容（实测）。method="POST" 时按官方方式发。"""
    params = params or {}
    headers = _headers(token)
    body = None
    if method.upper() == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(params).encode("utf-8")
        url = BASE + path
    else:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method.upper())
    # 官方服务器偶发 SSL 握手超时（_ssl.c:1064）。这类网络波动重试 2 次即可，
    # 避免一次抖动就在界面上闪出错误提示；HTTP 4xx/5xx 不重试。
    raw = None
    last_net = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            last_net = None
            break
        except urllib.error.HTTPError as e:
            raise CloudError(f"服务端 HTTP {e.code}：{e.reason}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_net = e
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
                continue
    if last_net is not None:
        raise CloudError(f"网络请求失败（已重试 2 次）：{last_net}") from last_net
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise CloudError(f"服务端返回非 JSON：{raw[:120]}") from e
    code = data.get("errorCode")
    if code not in (0, "0", None):
        # 原样带出官方中文提示，便于用户区分「账户不存在 / 密码错误 / 验证码错误」
        raise CloudError(f"{data.get('message') or '未知错误'}（错误码 {code}）")
    return data


def _num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace("+", "").strip())
    except (ValueError, TypeError):
        return default


def _seq(v) -> list:
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                return list(json.loads(s))
            except ValueError:
                pass
        return [x.strip() for x in re.split(r"[,，\s]+", s) if x.strip()]
    return []


@dataclass
class Preset:
    id: str = ""
    name: str = "未命名"
    freq: list[float] = field(default_factory=list)
    gain: list[float] = field(default_factory=list)
    q: list[float] = field(default_factory=list)
    pregain: float = 0.0
    desc: str = ""
    image: str = ""
    source: str = "official"       # official / community / local
    raw: dict = field(default_factory=dict)

    @property
    def band_count(self) -> int:
        return len(self.freq)

    def to_bands(self, max_bands: int = 5) -> list[tuple[float, float, float]]:
        """转成 (freq, gain, q) 列表，适配 Z2。

        ≤max_bands 段：忠实沿用预设自身频点；
        >max_bands 段：按 log(频率) 线性插值增益，重采样到官方 5 个频点。
        """
        pts = []
        for f, g, q in zip(self.freq, self.gain, self.q):
            if f and f > 0:
                pts.append((float(f), float(g), float(q)))
        pts.sort(key=lambda x: x[0])
        if not pts:
            return []
        if len(pts) <= max_bands:
            return [(f, g, q) for f, g, q in pts]

        xs = [math.log10(f) for f, _, _ in pts]
        ys = [g for _, g, _ in pts]
        qs = [q for _, _, q in pts]

        def interp(x):
            if x <= xs[0]:
                return ys[0], qs[0]
            if x >= xs[-1]:
                return ys[-1], qs[-1]
            for i in range(len(xs) - 1):
                if xs[i] <= x <= xs[i + 1]:
                    t = (x - xs[i]) / (xs[i + 1] - xs[i])
                    return (ys[i] + t * (ys[i + 1] - ys[i]),
                            qs[i] + t * (qs[i + 1] - qs[i]))
            return ys[-1], qs[-1]

        out = []
        for f in Z2_TARGET_FREQS[:max_bands]:
            g, q = interp(math.log10(f))
            out.append((float(f), round(g, 2), round(q, 3)))
        return out

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "freq": self.freq,
                "gain": self.gain, "q": self.q, "pregain": self.pregain,
                "desc": self.desc, "image": self.image, "source": self.source}


def parse_preset(item: dict, source: str = "official") -> Preset | None:
    """把官方 soundList / 社区 EQ 的一条记录解析成 Preset。"""
    if not isinstance(item, dict):
        return None
    freq_raw = _seq(item.get("freq") or item.get("frequencies") or item.get("eqFreq"))
    gain_raw = _seq(item.get("sound") or item.get("gain") or item.get("eqGain")
                    or item.get("eq") or item.get("sounds"))
    q_raw = _seq(item.get("q") or item.get("eqQ") or item.get("Q"))
    if not freq_raw or not gain_raw:
        return None
    freq, gain, q = [], [], []
    for i, f in enumerate(freq_raw):
        fv = _num(f)
        if fv is None or fv <= 0:
            continue
        gv = _num(gain_raw[i]) if i < len(gain_raw) else None
        if gv is None:
            continue
        qv = _num(q_raw[i]) if i < len(q_raw) else None
        freq.append(fv)
        gain.append(gv)
        q.append(qv if qv and qv > 0 else 1.0)
    if not freq:
        return None
    return Preset(
        id=str(item.get("id") or item.get("eqId") or item.get("sound_id") or ""),
        name=str(item.get("name") or item.get("title") or item.get("eqName") or "未命名"),
        freq=freq, gain=gain, q=q,
        pregain=_num(item.get("main_gain") or item.get("mainGain"), 0.0) or 0.0,
        desc=str(item.get("desc") or item.get("note") or item.get("description") or ""),
        image=str(item.get("image") or item.get("cover") or ""),
        source=source, raw=item,
    )


def fetch_official() -> list[Preset]:
    """拉取官方音效库（无需登录）。官方用 POST + keyword，GET 也兼容。"""
    try:
        d = request(OFFICIAL_SOUNDS, {"keyword": ""}, method="POST")
    except CloudError:
        d = request(OFFICIAL_SOUNDS)
    items = d.get("data") or []
    out = [p for p in (parse_preset(x, "official") for x in items) if p]
    return out


def load_offline(path: str | Path, source: str = "official") -> list[Preset]:
    """断网兜底：加载随程序附带的音效库快照（也可用于内置预设）。"""
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
        return [p for p in (parse_preset(x, source) for x in items) if p]
    except Exception:
        return []


def _find_token(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "token" in str(k).lower() and isinstance(v, str) and len(v) > 8:
                return v
        for v in obj.values():
            t = _find_token(v)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = _find_token(v)
            if t:
                return t
    return None


def encrypt_password(plain: str) -> str:
    """官方密码变换：SecretUtil.genHmacSha1(明文密码, SECRET) → 小写 hex。

    APK `g1.onClick` 把密码输入框内容传进 genHmacSha1，结果作为
    `password` 字段；SecretUtil 内部用第二个参数作 HMAC key。
    """
    return hmac.new(SECRET.encode(), plain.encode("utf-8"),
                    hashlib.sha1).hexdigest().lower()


def send_verify_code(username: str, area_code: str = "86",
                     vcode_type: str = "2", lang: str = "zh") -> dict:
    """发送登录短信验证码（官方 sendVerify 接口）。"""
    p = {"areaCode": (area_code or "86").strip().lstrip("+") or "86",
         "userName": username.strip(), "vcodeType": vcode_type, "lang": lang}
    return request(SEND_VERIFY, p, method="POST")


def login(username: str, password: str = "", area_code: str = "86",
          use_vcode: bool = False, vcode: str = "",
          fallback: bool = True) -> str:
    """登录官方账号，返回 token。

    参数完全复刻 APK 的 `Lxb/b;.z()`：
      loginType=1(密码)/2(验证码), areaCode=86, userName, vcode, password
    密码为 HMAC-SHA1(SECRET, 明文) 的小写 hex；POST form body。

    兼容兜底：若服务端明确回「密码错误」，再试明文 / MD5（最多 3 次请求），
    其余错误（账户不存在、验证码错误等）立即返回，不做无谓重试。
    账号密码只发往官方服务器，不写入日志或配置文件。
    """
    un = (username or "").strip()
    if not un:
        raise CloudError("请填写账号")
    ac = (area_code or "86").strip().lstrip("+") or "86"
    base = {"loginType": "2" if use_vcode else "1",
            "areaCode": ac, "userName": un,
            "vcode": (vcode or "").strip() if use_vcode else ""}
    if use_vcode:
        if not base["vcode"]:
            raise CloudError("请填写短信验证码")
        candidates = [("验证码登录", "")]
    else:
        if not password:
            raise CloudError("请填写密码")
        md = hashlib.md5(password.encode("utf-8")).hexdigest()
        candidates = [("HMAC-SHA1", encrypt_password(password))]
        if fallback:
            candidates += [("明文", password), ("MD5", md)]

    notes = []
    for label, pwd in candidates:
        p = dict(base)
        p["password"] = pwd
        try:
            d = request(LOGIN, p, method="POST")
        except CloudError as e:
            msg = str(e)
            notes.append(f"{label} → {msg}")
            # 只有密码类错误才换加密方式重试，其它错误直接给用户看
            if "密码" in msg or "password" in msg.lower():
                continue
            break
        tok = _find_token(d.get("data"))
        if tok:
            return tok
        notes.append(f"{label} → 服务端未返回 token")
        break
    raise CloudError("；".join(notes) or "登录失败")


def fetch_community(token: str, page: int = 1, limit: int = 50,
                    keyword: str = "", eq_type: str = "0",
                    g_type: str = "") -> list[Preset]:
    """社区 EQ 广场列表（需有效 token）。

    参数名来自 APK 注解 `Lxb/b;.C()`：pageIndex/pageSize/keyword/type/gType。
    **`type` 必填**（服务端 3010「type不能为空」），`type=0` = 社区广场全部；
    返回结构实测为 `data.allData`（另有 `data.pageInfo`），
    每条含 freq / q / sound / type 四个 JSON 字符串数组。
    """
    p = {"pageIndex": page, "pageSize": limit, "keyword": keyword,
         "type": eq_type or "0", "gType": g_type}
    d = request(COMMUNITY_LIST, p, token=token, method="POST")
    data = d.get("data")
    if isinstance(data, dict):
        items = (data.get("allData") or data.get("list") or data.get("data")
                 or data.get("rows") or data.get("records") or [])
    else:
        items = data or []
    out = [q for q in (parse_preset(x, "community") for x in items) if q]
    return out


def download_community(token: str, eq_id: str) -> Preset | None:
    """下载单条社区 EQ 详情（需有效 token）；官方参数名 eq_id。"""
    try:
        d = request(COMMUNITY_DOWNLOAD, {"eq_id": eq_id},
                    token=token, method="POST")
    except CloudError:
        return None
    data = d.get("data")
    item = data[0] if isinstance(data, list) and data else data
    p = parse_preset(item if isinstance(item, dict) else {}, "community")
    if p:
        p.id = str(eq_id)
    return p


if __name__ == "__main__":
    print("官方音效库（在线）:")
    try:
        ps = fetch_official()
        for p in ps[:8]:
            print(f"  {p.name:<16} {p.band_count} 段  pregain={p.pregain:+.1f}  "
                  f"→ Z2: {p.to_bands()[:2]} ...")
        print(f"  共 {len(ps)} 条")
    except CloudError as e:
        print("  失败:", e)
