#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动签到：WorkBuddy (CodeBuddy 国内版) + Trae (Trae Work 积分)。

零第三方依赖，仅使用 Python 标准库。
凭证由 login.py 一次性 OAuth 登录获取并存盘，本脚本负责：
  1. 读盘凭证
  2. token 临期自动刷新
  3. 调签到接口 + 查询积分
  4. 打印结果

用法:
    python checkin.py            # 签到所有已配置账号
    python checkin.py --debug     # 打印详细 HTTP 调试信息
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CRED_DIR = BASE_DIR / "auths"
CRED_DIR.mkdir(exist_ok=True)

# 提前 24 小时判定 token 即将过期，触发刷新
REFRESH_MARGIN = 24 * 3600

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "checkin.log"

DEBUG = False


class _Tee:
    """同时写 stdout 和日志文件，保证任务计划无控制台时也有记录。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def setup_file_log() -> None:
    log_fp = open(LOG_PATH, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_fp)
    sys.stderr = _Tee(sys.__stderr__, log_fp)


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"    [debug] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Trae UI 签到兜底（默认关闭）。
# 9074 根因已确认为设备号问题，修复后 HTTP 签到即可成功，无需 UI 兜底。
# 需要时用命令行开关启用：python checkin.py --ui-fallback
# ---------------------------------------------------------------------------
TRAE_UI_FALLBACK_ENABLED = False

UI_CHECKIN_SCRIPT = BASE_DIR / "trae_ui_checkin.py"
LOGIN_SCRIPT = BASE_DIR / "login.py"

# HTTP 签到失败、需要走 UI 兜底的状态关键词
_UI_FALLBACK_KEYWORDS = (
    "认证失败", "token 失效", "服务端限流", "领取请求失败",
    "查询状态失败", "领取结果: code=",
)


def should_fallback_to_ui(status: str) -> bool:
    """判断 Trae HTTP 签到状态是否需要 UI 兜底。"""
    s = str(status)
    return any(k in s for k in _UI_FALLBACK_KEYWORDS)


def run_ui_checkin_fallback(timeout: int = 420, ready_timeout: int = 120) -> dict:
    """调用 trae_ui_checkin.py 做 UI 签到兜底。

    要求：Trae 桌面端已安装，且定时任务以交互式用户运行（本任务即如此）。
    冷启动 + 自动更新安装实测要几分钟，所以等待窗口给到 timeout 秒。
    返回 {"ok": bool, "status": str}。
    """
    if not UI_CHECKIN_SCRIPT.is_file():
        return {"ok": False, "status": "未找到 trae_ui_checkin.py，无法 UI 兜底"}

    print("  [Trae] HTTP 签到未成功，启动 UI 签到兜底...")
    try:
        env = dict(os.environ)
        # 强制子进程 UTF-8 输出，避免 GBK 控制台导致日志乱码
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [
                sys.executable, str(UI_CHECKIN_SCRIPT),
                "--timeout", str(timeout),
                "--ready-timeout", str(ready_timeout),
            ],
            cwd=str(BASE_DIR),
            # 窗口等待 + 界面就绪等待 + OCR，再留 120s 缓冲
            timeout=timeout + ready_timeout + 120,

            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # 把 UI 脚本的输出透传到日志，方便排查
        for line in out.splitlines():
            if line.strip():
                print(f"    [ui] {line}")
        ok = proc.returncode == 0
        # 从输出里提取状态行
        status = "UI 兜底完成"
        for line in out.splitlines():
            ls = line.strip()
            if ls.startswith("状态:") or ls.startswith("状态："):
                status = f"UI 兜底 {ls.split(':', 1)[-1].split('：', 1)[-1].strip()}"
                break
        else:
            if not ok:
                status = f"UI 兜底失败（退出码 {proc.returncode}）"
        return {"ok": ok, "status": status}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "UI 兜底超时"}
    except Exception as e:
        return {"ok": False, "status": f"UI 兜底异常: {e}"}


def run_trae_relogin(timeout: int = 360) -> bool:
    """token 失效时调用 login.py trae，打开浏览器让用户重新登录。

    交互式终端：输出跟当前窗口；计划任务无控制台时另开窗口。
    成功返回 True（凭证已由 login.py 写盘）。
    """
    if not LOGIN_SCRIPT.is_file():
        print("  [Trae] 未找到 login.py，无法自动重新登录")
        return False

    print("  [Trae] token 失效，启动 python login.py trae ...")
    print("  [Trae] 请在浏览器完成登录（最多 5 分钟）")
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cmd = [sys.executable, str(LOGIN_SCRIPT), "trae"]
        if DEBUG:
            cmd.append("--debug")
        kwargs = {
            "cwd": str(BASE_DIR),
            "timeout": timeout,
            "env": env,
        }
        # 无 TTY（计划任务）时弹出新控制台，否则用户看不到登录提示
        if not getattr(sys.__stdout__, "isatty", lambda: False)():
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        proc = subprocess.run(cmd, **kwargs)
        if proc.returncode == 0:
            print("  [Trae] 重新登录成功")
            return True
        print(f"  [Trae] 重新登录失败（退出码 {proc.returncode}）")
        return False
    except subprocess.TimeoutExpired:
        print("  [Trae] 重新登录超时")
        return False
    except Exception as e:
        print(f"  [Trae] 重新登录异常: {e}")
        return False


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def http_request(method: str, url: str, headers: dict, body: dict | None = None,
                 timeout: int = 30) -> dict:
    """极简 urllib 封装，统一返回 dict。失败抛 RuntimeError。"""
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Content-Length", str(len(data)))

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)

    dbg(f"{method} {url}")
    if body is not None:
        dbg(f"  body: {body}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            dbg(f"  status: {resp.status}")
            dbg(f"  resp:  {raw[:500]}")
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"_raw": raw, "_status": resp.status}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        dbg(f"  HTTPError {e.code}: {raw[:500]}")
        try:
            return json.loads(raw) if raw else {"_status": e.code}
        except json.JSONDecodeError:
            return {"_raw": raw, "_status": e.code}
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败: {e.reason}") from e


# ---------------------------------------------------------------------------
# WorkBuddy (CodeBuddy 国内版)
# ---------------------------------------------------------------------------
class WorkBuddy:
    """腾讯 CodeBuddy 国内版签到。"""

    BASE = "https://www.codebuddy.cn"
    AUTH_BASE = "https://copilot.tencent.com"
    UA = "CLI/2.63.2 CodeBuddy/2.63.2"

    # 会话失效特征
    DEAD_MARKERS = ("Offline user session not found", "12153")

    def __init__(self, cred: dict):
        self.uid = cred.get("uid", "unknown")
        self.access_token = cred["accessToken"]
        self.refresh_token = cred["refreshToken"]
        self.expires_at = cred.get("expiresAt", 0)
        self.domain = cred.get("domain", "codebuddy.cn")

    # --- token 刷新 --------------------------------------------------------
    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_MARGIN

    def refresh(self) -> bool:
        """用 refreshToken 换新 accessToken，成功则更新内存并返回 True。"""
        headers = {"User-Agent": self.UA, "Content-Type": "application/json"}
        body = {"refreshToken": self.refresh_token}
        try:
            data = http_request("POST", f"{self.AUTH_BASE}/v2/plugin/auth/token/refresh",
                                headers, body)
        except RuntimeError as e:
            dbg(f"WorkBuddy 刷新请求失败: {e}")
            return False

        new_access = (data.get("data") or {}).get("accessToken") \
            or (data.get("data") or {}).get("access_token")
        new_refresh = (data.get("data") or {}).get("refreshToken") \
            or (data.get("data") or {}).get("refresh_token") or self.refresh_token
        expires_in = (data.get("data") or {}).get("expiresIn") \
            or (data.get("data") or {}).get("expires_in", 0)

        if not new_access:
            dbg(f"WorkBuddy 刷新返回无 token: {data}")
            return False

        self.access_token = new_access
        self.refresh_token = new_refresh
        self.expires_at = time.time() + (expires_in or 7 * 24 * 3600)
        dbg(f"WorkBuddy token 已刷新，新过期时间 {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.expires_at))}")
        return True

    # --- 业务接口 ----------------------------------------------------------
    def _auth_headers(self) -> dict:
        return {
            "User-Agent": self.UA,
            "Authorization": f"Bearer {self.access_token}",   # 实测需 Bearer 前缀
            "Content-Type": "application/json",
        }

    def check_status(self) -> dict:
        return http_request("POST", f"{self.BASE}/v2/billing/meter/checkin-activity-status",
                            self._auth_headers(), body={})

    def do_checkin(self) -> dict:
        return http_request("POST", f"{self.BASE}/v2/billing/meter/daily-checkin",
                            self._auth_headers(), body={})

    def query_credits(self) -> dict:
        return http_request("POST", f"{self.BASE}/v2/billing/meter/get-user-resource",
                            self._auth_headers(), body={})

    # --- 主流程 ------------------------------------------------------------
    def run(self) -> dict:
        result = {"product": "WorkBuddy", "uid": self.uid}

        if self.needs_refresh():
            print("  [WorkBuddy] token 即将过期，尝试刷新...")
            if not self.refresh():
                result["status"] = "token 失效，请重新登录"
                return result
            persist(self.uid, "workbuddy", self.to_cred())

        # 先查状态
        try:
            st = self.check_status()
            dbg(f"checkin-activity-status: {st}")
            result["raw_status"] = st
        except RuntimeError as e:
            result["status"] = f"查询状态失败: {e}"
            return result

        # 判定会话失效
        raw = json.dumps(st, ensure_ascii=False)
        if any(m in raw for m in self.DEAD_MARKERS):
            result["status"] = "会话已失效，请重新登录 (login.py)"
            return result

        # 执行签到
        try:
            r = self.do_checkin()
            dbg(f"daily-checkin: {r}")
            result["raw_checkin"] = r
            result["status"] = self._parse_checkin_result(r)
        except RuntimeError as e:
            result["status"] = f"签到请求失败: {e}"
            return result

        # 查询积分
        try:
            cr = self.query_credits()
            dbg(f"get-user-resource: {cr}")
            result["raw_credits"] = cr
            result["credits"] = self._parse_credits(cr)
        except RuntimeError as e:
            result["credits"] = f"查询积分失败: {e}"

        return result

    @staticmethod
    def _parse_checkin_result(r: dict) -> str:
        code = r.get("code")
        msg = r.get("message") or r.get("msg") or ""
        data = r.get("data") or {}
        if code in (0, 200) or r.get("success"):
            credit = data.get("credit")
            streak = data.get("streak_days")
            if credit is not None:
                extra = f"，连续{streak}天" if streak else ""
                return f"签到成功 +{credit}积分{extra}"
            return "签到成功"
        # 已签到类提示
        for kw in ("已签到", "已领取", "明日", "already", "claimed", "checked", "today_checked_in"):
            if kw in str(msg) or kw in str(r):
                if data.get("today_checked_in") or data.get("checked_in"):
                    return "今日已签到"
                return f"今日已签到 ({msg})"
        return f"签到结果: code={code} msg={msg}"

    @staticmethod
    def _parse_credits(r: dict) -> str:
        data = r.get("data") or {}
        # get-user-resource 返回 data.Response.Data.TotalDosage
        resp = data.get("Response") or {}
        resp_data = resp.get("Data") or {}
        total = resp_data.get("TotalDosage")
        if total is not None:
            return f"当前积分: {total}"
        # 兜底其他字段名
        credits = (data.get("credits") or data.get("totalCredits")
                   or data.get("balance") or data.get("TotalCount"))
        if credits is None:
            return json.dumps(data, ensure_ascii=False)[:120]
        return f"当前积分: {credits}"

    def to_cred(self) -> dict:
        return {
            "uid": self.uid,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "domain": self.domain,
        }


# ---------------------------------------------------------------------------
# Trae (Trae Work 积分)
# ---------------------------------------------------------------------------
def resolve_trae_device_id(fallback: str = "") -> str:
    """优先用 Trae 桌面客户端本地真实设备 ID（16 位 Aha 数字号）。

    baokun-l/trae-work-checkin 实测：claim 会校验设备标识，x-device-id 必须是
    storage.json 中 `iCubeAuthInfo://icube-dc:{16位数字}` 的 Aha 数字号；
    用 UUID / GUID / hex 设备号会被风控判定为非客户端设备，返回误导性 9074
    「当前参与用户太多」。（storage.json 中 has_device_id_updated_to_aha=true
    印证客户端已把 UUID 升级为 Aha 数字号。）
    """
    # 各客户端 storage.json: %APPDATA%\<客户端>\User\globalStorage\storage.json
    storage_candidates = [
        Path.home() / "AppData" / "Roaming" / "TRAE SOLO CN",
        Path.home() / "AppData" / "Roaming" / "Trae CN",
        Path.home() / "AppData" / "Roaming" / "Trae",
    ]
    # iCubeAuthInfo://icube-dc: 后跟 16 位十进制数字
    aha_key_re = re.compile(r"iCubeAuthInfo://icube-dc:(\d{16})(?!\d)")
    for base in storage_candidates:
        storage = base / "User" / "globalStorage" / "storage.json"
        try:
            if not storage.is_file():
                continue
            text = storage.read_text(encoding="utf-8", errors="replace")
            m = aha_key_re.search(text)
            if m:
                v = m.group(1)
                dbg(f"Trae Aha 设备号来自 {storage}")
                return v
        except OSError:
            continue

    # 兜底：旧版 machineid（UUID，可能触发 9074）
    mid_candidates = [
        Path.home() / "AppData" / "Roaming" / "TRAE SOLO CN" / "machineid",
        Path.home() / "AppData" / "Roaming" / "Trae CN" / "machineid",
        Path.home() / "AppData" / "Roaming" / "Trae" / "machineid",
    ]
    for p in mid_candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    dbg(f"Trae 设备号回退 machineid（UUID，可能触发 9074）: {p}")
                    return v
        except OSError:
            continue
    return fallback


def _user_id_from_jwt(token: str) -> str:
    """从 Cloud-IDE-JWT payload.data.id 提取用户 ID。凭证里 userId 常为空。"""
    if not token or "." not in token:
        return ""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload))
        data = obj.get("data") if isinstance(obj, dict) else None
        uid = ""
        if isinstance(data, dict):
            uid = data.get("id") or data.get("UserID") or ""
        if not uid and isinstance(obj, dict):
            uid = obj.get("UserID") or obj.get("userId") or ""
        return str(uid) if uid else ""
    except Exception:
        return ""


class Trae:
    """字节 Trae IDE 每日 Work 积分领取。"""

    BASE = "https://api.trae.cn"
    AUTH_BASE = "https://api.trae.com.cn"
    UA = "Trae/0.1.43"

    def __init__(self, cred: dict):
        self.user_id = cred.get("userId", "")
        self.access_token = cred["accessToken"]
        self.refresh_token = cred["refreshToken"]
        self.expires_at = cred.get("expiresAt", 0)
        self.device_id = resolve_trae_device_id(cred.get("deviceId", ""))
        self.screen_name = cred.get("screenName", "")
        # 文件名用加载时的名字，避免后来补上 userId 后写出第二份凭证
        self._cred_name = self.screen_name or self.user_id or "trae"
        if not self.user_id:
            self.user_id = _user_id_from_jwt(self.access_token)
        self.refresh_rejected = False

    # --- token 刷新 --------------------------------------------------------
    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_MARGIN

    def refresh(self) -> bool:
        """刷新 token。失败时置 self.refresh_rejected=True 表示被服务端明确拒绝。"""
        self.refresh_rejected = False
        headers = {"Content-Type": "application/json"}
        body = {
            "ClientID": "en1oxy7wnw8j9n",
            "RefreshToken": self.refresh_token,
            "ClientSecret": "-",
            "UserID": self.user_id or "",
        }
        try:
            data = http_request("POST", f"{self.AUTH_BASE}/cloudide/api/v3/trae/oauth/ExchangeToken",
                               headers, body)
        except RuntimeError as e:
            dbg(f"Trae 刷新请求失败: {e}")
            return False

        result = data.get("Result") or data.get("data") or {}
        new_access = result.get("Token") or result.get("AccessToken") or result.get("access_token")
        new_refresh = result.get("RefreshToken") or result.get("refresh_token") or self.refresh_token
        expire_at = result.get("TokenExpireAt") or result.get("expiresAt")

        if not new_access:
            dbg(f"Trae 刷新返回无 token: {data}")
            # 响应里带 Error.Code（如 20101 refresh token is invalid）说明被明确拒绝，
            # 与网络异常区分开：前者需重新登录，后者只需下次再试
            err = ((data.get("ResponseMetadata") or {}).get("Error") or {}).get("Code")
            if err:
                self.refresh_rejected = True
            return False

        self.access_token = new_access
        self.refresh_token = new_refresh
        # TokenExpireAt 是毫秒时间戳，兼容秒
        if expire_at:
            self.expires_at = expire_at / 1000 if expire_at > 1e12 else expire_at
        else:
            self.expires_at = time.time() + 3600

        dbg(f"Trae token 已刷新，新过期时间 {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.expires_at))}")
        return True

    # --- 业务接口 ----------------------------------------------------------
    def _auth_headers(self) -> dict:
        # 对齐参照项目最小头集：Authorization + x-device-id + X-User-Region。
        # 不另加 X-Device-Id 重复头。User-Agent 用官方客户端值，避免 urllib 默认 UA。
        return {
            "Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {self.access_token}",
            "x-device-id": self.device_id,
            "X-User-Region": "CN",
            "User-Agent": self.UA,
        }

    def check_status(self) -> dict:
        return http_request("POST", f"{self.BASE}/trae/api/v2/ug/checkin_credits/status",
                            self._auth_headers(), body={})

    def do_claim(self) -> dict:
        return http_request("POST", f"{self.BASE}/trae/api/v2/ug/checkin_credits/claim",
                            self._auth_headers(), body={})

    def query_credits(self) -> dict:
        return http_request("POST", f"{self.BASE}/trae/api/v2/pay/ide_user_ent_usage",
                            self._auth_headers(), body={})

    def _persist(self) -> None:
        persist(self._cred_name, "trae", self.to_cred())

    def _query_credits_safe(self, result: dict) -> None:
        try:
            cr = self.query_credits()
            dbg(f"usage: {cr}")
            result["raw_credits"] = cr
            parsed = self._parse_credits(cr)
            # 认证失败/错误响应时不把错误 JSON 当积分展示
            if str(parsed).startswith("当前积分"):
                result["credits"] = parsed
        except RuntimeError as e:
            result["credits"] = f"查询积分失败: {e}"

    def _handle_auth_failure(self, result: dict, where: str) -> str | None:
        """1001 时刷新一次。返回 None 表示刷新成功可重试；返回 status 表示应结束。"""
        if self.refresh_rejected:
            return "token 失效，请重新登录 (login.py trae)"
        print(f"  [Trae] {where}认证失败(1001)，尝试刷新 token 后重试...")
        if self.refresh():
            if not self.user_id:
                self.user_id = _user_id_from_jwt(self.access_token)
            self._persist()
            return None
        if self.refresh_rejected:
            return "token 失效，请重新登录 (login.py trae)"
        return f"认证失败，无法确认领取状态（{where}刷新未成功）"

    # --- 主流程 ------------------------------------------------------------
    def run(self) -> dict:
        result = {"product": "Trae", "userId": self.user_id, "screenName": self.screen_name}

        if self.needs_refresh():
            print("  [Trae] token 即将过期，尝试刷新...")
            if self.refresh():
                if not self.user_id:
                    self.user_id = _user_id_from_jwt(self.access_token)
                self._persist()
            elif self.refresh_rejected and self.expires_at <= time.time():
                result["status"] = "token 失效，请重新登录 (login.py trae)"
                return self._finalize(result)
            elif self.refresh_rejected:
                # refresh 被拒，但 access token 尚未过期：继续用旧 token 签到
                print("  [Trae] refreshToken 已失效，改用尚未过期的 accessToken 继续")
                self._persist()
            else:
                print("  [Trae] token 刷新失败，改用现有 token 继续")
                self._persist()
        elif self.device_id:
            self._persist()

        # 先查状态（免费请求）。已签到立即收手，不消耗 claim。
        try:
            st = self.check_status()
            dbg(f"checkin status: {st}")
            result["raw_status"] = st
        except RuntimeError as e:
            result["status"] = f"查询状态失败: {e}"
            return self._finalize(result)

        if st.get("code") == 1001:
            fail = self._handle_auth_failure(result, "查询状态")
            if fail:
                result["status"] = fail
                return self._finalize(result)
            try:
                st = self.check_status()
                dbg(f"checkin status after refresh: {st}")
                result["raw_status"] = st
            except RuntimeError as e:
                result["status"] = f"查询状态失败: {e}"
                return self._finalize(result)
            if st.get("code") == 1001:
                result["status"] = "认证失败，无法确认领取状态"
                return self._finalize(result)

        if st.get("checked_in"):
            cr = st.get("credits", 0)
            result["status"] = f"今日已领取（status 确认）"
            if cr:
                result["status"] += f" +{cr}"
            self._query_credits_safe(result)
            return result

        # 领取积分（服务端可能限流返回 9074，重试几次）
        claim_retries = 3
        r = None
        auth_retried = False
        for attempt in range(1, claim_retries + 1):
            try:
                if attempt > 1:
                    # 重试前再查一次：已签到（手动或其他进程）立即收手
                    st2 = self.check_status()
                    if st2 and st2.get("checked_in"):
                        result["raw_status"] = st2
                        result["status"] = "今日已领取（重试前确认）"
                        self._query_credits_safe(result)
                        return result
                r = self.do_claim()
                dbg(f"claim attempt {attempt}: {r}")
                code = r.get("code")
                if code == 9074 and attempt < claim_retries:
                    wait = 20 * attempt
                    print(f"  [Trae] 服务端限流(9074)，{wait}秒后重试({attempt}/{claim_retries})...")
                    time.sleep(wait)
                    continue
                if code == 1001 and not auth_retried:
                    fail = self._handle_auth_failure(result, "领取")
                    if fail:
                        result["status"] = fail
                        return self._finalize(result)
                    auth_retried = True
                    continue
                break
            except RuntimeError as e:
                result["status"] = f"领取请求失败: {e}"
                return self._finalize(result)

        result["raw_claim"] = r
        result["status"] = self._parse_claim_result(r)

        self._query_credits_safe(result)

        # HTTP 签到未成功时，走桌面端 UI 签到兜底（由开关控制，当前停用）
        return self._finalize(result)

    def _finalize(self, result: dict) -> dict:
        """收尾：HTTP 路径失败且开关开启时走 UI 兜底。"""
        if not TRAE_UI_FALLBACK_ENABLED:
            return result
        if should_fallback_to_ui(result.get("status", "")):
            ui_res = run_ui_checkin_fallback()
            result["ui_fallback"] = ui_res
            if ui_res.get("ok"):
                result["status"] = ui_res["status"]
                try:
                    cr = self.query_credits()
                    dbg(f"usage after ui: {cr}")
                    parsed = self._parse_credits(cr)
                    if str(parsed).startswith("当前积分"):
                        result["credits"] = parsed
                    else:
                        # HTTP 查积分仍失败时，不要把错误 JSON 当积分展示
                        result.pop("credits", None)
                except Exception:
                    result.pop("credits", None)
            else:
                result["status"] = f"{result['status']}；{ui_res['status']}"
        return result

    @staticmethod
    def _parse_claim_result(r: dict) -> str:
        if not r:
            return "领取请求无响应"
        code = r.get("code")
        msg = r.get("message") or r.get("Message") or ""
        data = r.get("data") or r.get("Result") or {}
        if code in (0, 200) or r.get("success") or data.get("success"):
            return "领取成功"
        if code == 9074:
            return f"服务端限流，领取失败 ({msg})"
        for kw in ("已签到", "已领取", "明日再来", "already", "checked", "claimed"):
            if kw in str(msg) or kw in str(r):
                return f"今日已领取 ({msg})"
        if code == 1001:
            # 1001 是服务端认证失败（"not able to authenticate"），
            # 不能当作「今日已领取」，否则会掩盖真实失败。
            return f"认证失败，无法确认领取状态 ({msg})"
        return f"领取结果: code={code} msg={msg}"

    @staticmethod
    def _parse_credits(r: dict) -> str:
        # ide_user_ent_usage 返回的是权益包列表，从中汇总 credits_limit
        packs = r.get("user_entitlement_pack_list") or []
        if packs:
            total = 0
            parts = []
            for pk in packs:
                quota = (pk.get("entitlement_base_info") or {}).get("quota") or pk.get("quota") or {}
                limit = quota.get("credits_limit") or 0
                if limit:
                    name = pk.get("group_name") or pk.get("display_desc") or ""
                    parts.append(f"{name}={limit}")
                    total += limit
            if parts:
                return f"当前积分: {total} ({', '.join(parts[:3])})"
            # 兜底：直接打印第一个包
            return json.dumps(packs[0], ensure_ascii=False)[:120]

        data = r.get("data") or r.get("Result") or {}
        credits = (data.get("credits") or data.get("balance")
                   or data.get("totalCredits") or data.get("workCredits"))
        if credits is None:
            return json.dumps(r, ensure_ascii=False)[:120]
        return f"当前积分: {credits}"

    def to_cred(self) -> dict:
        return {
            "userId": self.user_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "deviceId": self.device_id,
            "screenName": self.screen_name,
        }


# ---------------------------------------------------------------------------
# 凭证读写
# ---------------------------------------------------------------------------
def cred_path(name: str, product: str) -> Path:
    return CRED_DIR / f"{product}-{name}.json"


def persist(name: str, product: str, cred: dict) -> None:
    p = cred_path(name, product)
    p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")


def load_creds() -> list:
    """扫描 auths/ 目录，返回 [(product, cred_dict)] 列表。"""
    items = []
    for p in sorted(CRED_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  跳过无法解析的凭证 {p.name}: {e}")
            continue
        stem = p.stem
        if stem.startswith("workbuddy-"):
            items.append(("workbuddy", data))
        elif stem.startswith("trae-"):
            items.append(("trae", data))
        else:
            print(f"  跳过未知凭证文件 {p.name}")
    return items


def load_trae_cred() -> dict | None:
    """重新扫描 auths/，返回最新一份 Trae 凭证。"""
    for product, cred in load_creds():
        if product == "trae":
            return cred
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> int:
    global DEBUG, TRAE_UI_FALLBACK_ENABLED
    ap = argparse.ArgumentParser(description="WorkBuddy + Trae 每日自动签到")
    ap.add_argument("--debug", action="store_true", help="打印 HTTP 调试信息")
    ap.add_argument("--ui-fallback", action="store_true",
                    help="Trae HTTP 签到失败时启用桌面端 UI 签到兜底（默认关闭）")
    args = ap.parse_args()
    DEBUG = args.debug
    TRAE_UI_FALLBACK_ENABLED = args.ui_fallback
    setup_file_log()

    print(f"=== 自动签到 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    if TRAE_UI_FALLBACK_ENABLED:
        print("（已启用 Trae UI 签到兜底）")
    creds = load_creds()
    if not creds:
        print("未找到任何凭证。请先运行: python login.py")
        print(f"凭证目录: {CRED_DIR}")
        return 1

    overall_ok = True
    for product, cred in creds:
        print(f"\n[{product}]")
        try:
            if product == "workbuddy":
                res = WorkBuddy(cred).run()
            else:
                res = Trae(cred).run()
        except Exception as e:
            print(f"  发生异常: {e}")
            if DEBUG:
                traceback.print_exc()
            overall_ok = False
            continue

        st = str(res.get("status", ""))
        # Trae token 彻底失效：拉起 login.py 浏览器登录，成功后用新凭证再签一次
        if product == "trae" and "token 失效" in st:
            if run_trae_relogin():
                new_cred = load_trae_cred()
                if new_cred:
                    print("  [Trae] 使用新凭证重新签到...")
                    try:
                        res = Trae(new_cred).run()
                    except Exception as e:
                        print(f"  发生异常: {e}")
                        if DEBUG:
                            traceback.print_exc()
                        overall_ok = False
                        continue
                    st = str(res.get("status", ""))
                else:
                    print("  [Trae] 重新登录后仍未找到凭证")

        print(f"  状态: {res.get('status', '未知')}")
        if res.get("credits"):
            print(f"  积分: {res['credits']}")
        # 明确的失败状态：token 失效、认证失败、限流未领到（UI 兜底开启时含兜底失败）
        fail_keywords = ("token 失效", "认证失败", "服务端限流，领取失败")
        if TRAE_UI_FALLBACK_ENABLED:
            fail_keywords += ("UI 兜底失败", "兜底超时", "兜底异常")
        if any(k in st for k in fail_keywords):
            overall_ok = False

    print("\n=== 完成 ===")
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(main())
