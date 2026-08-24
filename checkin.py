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
import json
import os
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
# Trae UI 签到兜底
# ---------------------------------------------------------------------------
UI_CHECKIN_SCRIPT = BASE_DIR / "trae_ui_checkin.py"

# HTTP 签到失败、需要走 UI 兜底的状态关键词
_UI_FALLBACK_KEYWORDS = (
    "认证失败", "token 失效", "服务端限流", "领取请求失败",
    "查询状态失败", "领取结果: code=",
)


def should_fallback_to_ui(status: str) -> bool:
    """判断 Trae HTTP 签到状态是否需要 UI 兜底。"""
    s = str(status)
    return any(k in s for k in _UI_FALLBACK_KEYWORDS)


def run_ui_checkin_fallback(timeout: int = 180) -> dict:
    """调用 trae_ui_checkin.py 做 UI 签到兜底。

    要求：Trae 桌面端已安装，且定时任务以交互式用户运行（本任务即如此）。
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
            [sys.executable, str(UI_CHECKIN_SCRIPT), "--timeout", str(timeout)],
            cwd=str(BASE_DIR),
            timeout=timeout + 60,          # 给脚本本身留缓冲
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
    """优先用 Trae 桌面客户端本地真实设备 ID。

    baokun-l/trae-work-checkin 实测：claim 会校验设备标识，自造 deviceId
    可能被拒，却返回误导性 9074「当前参与用户太多」。
    """
    candidates = [
        Path.home() / "AppData" / "Roaming" / "TRAE SOLO CN" / "machineid",
        Path.home() / "AppData" / "Roaming" / "Trae CN" / "machineid",
        Path.home() / "AppData" / "Roaming" / "Trae" / "machineid",
    ]
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    return fallback


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

    # --- token 刷新 --------------------------------------------------------
    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_MARGIN

    def refresh(self) -> bool:
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
        return {
            "Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {self.access_token}",   # 注意前缀
            "x-device-id": self.device_id,
            "X-Device-Id": self.device_id,
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

    # --- 主流程 ------------------------------------------------------------
    def run(self) -> dict:
        result = {"product": "Trae", "userId": self.user_id, "screenName": self.screen_name}

        if self.needs_refresh():
            print("  [Trae] token 即将过期，尝试刷新...")
            if not self.refresh():
                result["status"] = "token 失效，请重新登录"
                return self._finalize(result)
            persist(self.screen_name or self.user_id or "trae", "trae", self.to_cred())
        elif self.device_id:
            persist(self.screen_name or self.user_id or "trae", "trae", self.to_cred())

        # 先查状态
        try:
            st = self.check_status()
            dbg(f"checkin status: {st}")
            result["raw_status"] = st
        except RuntimeError as e:
            result["status"] = f"查询状态失败: {e}"
            return self._finalize(result)

        # 领取积分（服务端可能限流返回 9074，重试几次）
        claim_retries = 3
        r = None
        auth_retried = False
        for attempt in range(1, claim_retries + 1):
            try:
                r = self.do_claim()
                dbg(f"claim attempt {attempt}: {r}")
                code = r.get("code")
                if code == 9074 and attempt < claim_retries:
                    wait = 20 * attempt
                    print(f"  [Trae] 服务端限流(9074)，{wait}秒后重试({attempt}/{claim_retries})...")
                    time.sleep(wait)
                    continue
                if code == 1001 and not auth_retried:
                    # 认证失败：token 可能已失效，刷新后立即重试一次
                    print("  [Trae] 服务端认证失败(1001)，尝试刷新 token 后重试...")
                    auth_retried = True
                    if self.refresh():
                        persist(self.screen_name or self.user_id or "trae", "trae", self.to_cred())
                        continue
                    dbg("token 刷新失败，保留原结果")
                break
            except RuntimeError as e:
                result["status"] = f"领取请求失败: {e}"
                return self._finalize(result)

        result["raw_claim"] = r
        result["status"] = self._parse_claim_result(r)

        # 查询积分
        try:
            cr = self.query_credits()
            dbg(f"usage: {cr}")
            result["raw_credits"] = cr
            result["credits"] = self._parse_credits(cr)
        except RuntimeError as e:
            result["credits"] = f"查询积分失败: {e}"

        # HTTP 签到未成功时，走桌面端 UI 签到兜底
        return self._finalize(result)

    def _finalize(self, result: dict) -> dict:
        """收尾：HTTP 路径失败时统一走 UI 兜底。"""
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
            # 不能当作「今日已领取」，否则会掩盖真实失败、跳过兜底。
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


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> int:
    global DEBUG
    ap = argparse.ArgumentParser(description="WorkBuddy + Trae 每日自动签到")
    ap.add_argument("--debug", action="store_true", help="打印 HTTP 调试信息")
    args = ap.parse_args()
    DEBUG = args.debug
    setup_file_log()

    print(f"=== 自动签到 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
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

        print(f"  状态: {res.get('status', '未知')}")
        if res.get("credits"):
            print(f"  积分: {res['credits']}")
        st = str(res.get("status", ""))
        # 明确的失败状态：token 失效、认证失败且兜底也没救回来、限流未领到
        if any(k in st for k in ("token 失效", "认证失败", "UI 兜底失败",
                                 "服务端限流，领取失败", "兜底超时", "兜底异常")):
            overall_ok = False

    print("\n=== 完成 ===")
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(main())
