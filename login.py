#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
首次登录：用 OAuth 浏览器流程一次性获取 token，存到 auths/ 目录供 checkin.py 使用。

零第三方依赖，仅使用 Python 标准库。

工作原理:
  1. 启动本地 HTTP 服务器监听回调端口
  2. 打开浏览器到登录页，你在浏览器里扫码 / 验证码完成登录
  3. 登录成功后平台会跳转到本地回调地址，本脚本从 URL 里解析出 token
  4. (Trae) 再用 refreshToken 换一次 accessToken，拿到完整的凭证信息
  5. 存盘到 auths/{product}-{name}.json

用法:
    python login.py workbuddy        # 登录 WorkBuddy (CodeBuddy 国内版)
    python login.py trae              # 登录 Trae
    python login.py trae --debug       # 打印调试信息

注意:
    登录过程中浏览器会跳转到一个 127.0.0.1 的地址，那是正常的——
    本脚本就在监听那个地址捕获 token。登录完成后浏览器页面会显示成功提示。
"""

import argparse
import json
import os
import secrets
import sys
import time
import threading
import traceback
import urllib.request
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CRED_DIR = BASE_DIR / "auths"
CRED_DIR.mkdir(exist_ok=True)

DEBUG = False


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"  [debug] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 本地回调服务器
# ---------------------------------------------------------------------------
class CallbackHandler(BaseHTTPRequestHandler):
    """接收 OAuth 回调，把 query 存到 server.auth_result。"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.server.auth_result = dict(query)  # type: ignore[attr-defined]

        dbg(f"收到回调: {self.path}")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = (
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>✅ 登录成功</h2>"
            "<p>已捕获登录凭证，现在可以关闭此页面并回到命令行。</p>"
            "</body></html>"
        )
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, fmt, *args):
        if DEBUG:
            print(f"  [http] {fmt % args}")


def wait_for_callback(port: int, timeout: int = 300) -> dict | None:
    """启动本地服务器，最多等 timeout 秒拿到回调结果。"""
    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server.auth_result = None  # type: ignore[attr-defined]
    server.timeout = 1
    print(f"  本地回调服务器监听 http://127.0.0.1:{port}/ ...")

    start = time.time()
    result = None
    while time.time() - start < timeout:
        server.handle_request()
        if server.auth_result:
            result = server.auth_result
            break

    server.server_close()
    return result


def gen_device_id() -> str:
    """生成 32 位 hex 设备 ID。"""
    return secrets.token_hex(16)


def gen_machine_id() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# HTTP 小工具（与 checkin.py 同款）
# ---------------------------------------------------------------------------
def http_request(method: str, url: str, headers: dict, body: dict | None = None,
                 timeout: int = 30) -> dict:
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Content-Length", str(len(data)))
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            dbg(f"  {method} {url} -> {resp.status} {raw[:300]}")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        dbg(f"  {method} {url} -> HTTP {e.code} {raw[:300]}")
        try:
            return json.loads(raw) if raw else {"_status": e.code}
        except json.JSONDecodeError:
            return {"_raw": raw, "_status": e.code}


# ---------------------------------------------------------------------------
# WorkBuddy 登录
# ---------------------------------------------------------------------------
def login_workbuddy() -> int:
    """
    WorkBuddy 用 OAuth 设备授权流程：
      1. POST /v2/plugin/auth/state 拿 state + authUrl
      2. 浏览器打开 authUrl 完成腾讯 SSO 登录
      3. 轮询 /v2/plugin/auth/token 直到拿到 token
    """
    AUTH_BASE = "https://copilot.tencent.com"
    UA = "CLI/2.63.2 CodeBuddy/2.63.2"

    print("  请求授权 state...")
    state_resp = http_request("POST", f"{AUTH_BASE}/v2/plugin/auth/state?platform=CLI",
                             {"User-Agent": UA, "Content-Type": "application/json"}, body={})
    state_data = state_resp.get("data") or {}
    state = state_data.get("state")
    auth_url = state_data.get("authUrl")

    if not state or not auth_url:
        print(f"  无法获取授权 state，返回: {state_resp}")
        return 1

    print(f"  授权 state: {state[:8]}...")
    print(f"\n  即将打开浏览器，请在浏览器里完成腾讯账号登录。")
    print(f"  登录地址: {auth_url}")
    webbrowser.open(auth_url)

    print(f"\n  轮询等待登录完成（最多 5 分钟）...")
    headers = {"User-Agent": UA}
    for i in range(60):  # 每 5 秒轮询一次，最多 5 分钟
        time.sleep(5)
        try:
            token_resp = http_request("GET", f"{AUTH_BASE}/v2/plugin/auth/token?state={state}",
                                      headers)
        except Exception as e:
            dbg(f"轮询异常: {e}")
            continue

        token_data = token_resp.get("data") or {}
        access_token = token_data.get("accessToken") or token_data.get("access_token")
        if access_token:
            refresh_token = token_data.get("refreshToken") or token_data.get("refresh_token")
            expires_in = token_data.get("expiresIn") or token_data.get("expires_in", 0)
            domain = token_data.get("domain", "codebuddy.cn")

            # 从 token 里简单提取 uid（JWT payload 段）
            uid = "workbuddy"
            try:
                import base64
                payload = access_token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                uid = str(decoded.get("preferred_username")
                           or decoded.get("uid")
                           or decoded.get("userId")
                           or decoded.get("sub")
                           or "workbuddy")
            except Exception:
                pass

            cred = {
                "uid": uid,
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "expiresAt": time.time() + (expires_in or 7 * 24 * 3600),
                "domain": domain,
            }
            p = CRED_DIR / f"workbuddy-{uid}.json"
            p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n  ✅ WorkBuddy 登录成功")
            print(f"  UID: {uid}")
            print(f"  域: {domain}")
            print(f"  过期: {time.strftime('%Y-%m-%d %H:%M', time.localtime(cred['expiresAt']))}")
            print(f"  凭证已保存: {p}")
            return 0

        dbg(f"第 {i+1} 次轮询，尚未登录")

    print("\n  超时：5 分钟内未完成登录，请重试。")
    return 1


# ---------------------------------------------------------------------------
# Trae 登录
# ---------------------------------------------------------------------------
def login_trae() -> int:
    """
    Trae 用 OAuth 浏览器回调流程：
      1. 生成 deviceId/machineId，构造登录 URL，启动本地回调服务器
      2. 浏览器打开登录 URL，手机号/验证码登录
      3. 登录后跳转到 127.0.0.1 回调，URL query 含 refreshToken/userInfo/userJwt
      4. 用 refreshToken 换 accessToken
      5. 存盘
    """
    PORT = 18080
    AUTH_BASE = "https://api.trae.com.cn"

    device_id = gen_device_id()
    machine_id = gen_machine_id()

    params = {
        "client_id": "en1oxy7wnw8j9n",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": "2.3.62834",
        "auth_callback_url": f"http://127.0.0.1:{PORT}/authorize",
        "x_app_version": "0.1.43",
        "x_app_type": "stable",
        "device_id": device_id,
        "machine_id": machine_id,
    }
    login_url = "https://www.trae.cn/authorization?" + urllib.parse.urlencode(params)

    print(f"\n  即将打开浏览器，请在浏览器里完成 Trae 账号登录（手机号+验证码）。")
    print(f"  登录地址: {login_url}")
    print(f"  本地回调: http://127.0.0.1:{PORT}/authorize")
    print(f"  等待回调（最多 5 分钟）...\n")

    # 启动服务器并打开浏览器
    threading.Thread(target=lambda: webbrowser.open(login_url), daemon=True).start()

    result = wait_for_callback(PORT, timeout=300)
    if not result:
        print("  超时：5 分钟内未收到登录回调，请重试。")
        return 1

    dbg(f"回调参数: {result}")

    # 回调 query 格式（实测）：含 refreshToken / data(同refreshToken) / refreshExpireAt / host / userRegion
    # 部分版本也含 userInfo / userJwt（URL编码的JSON）。统一从 refreshToken 换 accessToken。
    refresh_token = result.get("refreshToken") or result.get("refresh_token") or result.get("data")
    user_info_raw = result.get("userInfo")
    user_jwt_raw = result.get("userJwt")

    user_info = {}
    if user_info_raw:
        try:
            user_info = json.loads(urllib.parse.unquote(user_info_raw))
        except (json.JSONDecodeError, TypeError):
            dbg(f"userInfo 解析失败: {user_info_raw}")

    user_jwt = {}
    if user_jwt_raw:
        try:
            user_jwt = json.loads(urllib.parse.unquote(user_jwt_raw))
        except (json.JSONDecodeError, TypeError):
            dbg(f"userJwt 解析失败: {user_jwt_raw}")

    # 先尝试直接从回调的 userJwt 取 token（部分版本有）
    access_token = user_jwt.get("Token") or user_jwt.get("token") or user_jwt.get("AccessToken")
    user_id = user_info.get("UserID") or user_jwt.get("UserID") or ""
    screen_name = user_info.get("ScreenName") or user_info.get("screenName") or ""
    bound_device_id = device_id  # 默认用登录时生成的

    # 用 refreshToken 换 accessToken（统一走，拿完整的 token/userId/deviceId）
    if not access_token and refresh_token:
        print("  用 refreshToken 换 accessToken...")
        body = {
            "ClientID": "en1oxy7wnw8j9n",
            "RefreshToken": refresh_token,
            "ClientSecret": "-",
            "UserID": user_id or "",
        }
        exch = http_request("POST", f"{AUTH_BASE}/cloudide/api/v3/trae/oauth/ExchangeToken",
                           {"Content-Type": "application/json"}, body)
        exch_result = exch.get("Result") or exch.get("data") or {}
        access_token = exch_result.get("Token") or exch_result.get("AccessToken")
        new_refresh = exch_result.get("RefreshToken")
        if new_refresh:
            refresh_token = new_refresh
        # BoundDeviceID 是服务端绑定到该 refreshToken 的设备 ID
        bound_device_id = exch_result.get("BoundDeviceID") or bound_device_id
        expire_at = exch_result.get("TokenExpireAt")
        dbg(f"ExchangeToken 结果: {exch}")
        if not access_token:
            print(f"  ⚠️ ExchangeToken 未返回 Token，完整响应: {json.dumps(exch, ensure_ascii=False)[:400]}")
    elif access_token:
        # 已从回调直接拿到 token，估算过期
        expire_at = user_jwt.get("TokenExpireAt") or result.get("refreshExpireAt")
    else:
        expire_at = None

    if not access_token or not refresh_token:
        print(f"  ❌ 无法从回调中提取 token。")
        print(f"  回调参数: {result}")
        return 1

    # 计算 expiresAt（TokenExpireAt / refreshExpireAt 通常是毫秒时间戳）
    expires_at = time.time() + 3600  # 默认 1 小时
    if expire_at:
        expires_at = expire_at / 1000 if expire_at > 1e12 else expire_at

    name = screen_name or user_id or "trae"
    cred = {
        "userId": user_id,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "deviceId": bound_device_id,
        "screenName": screen_name,
        "machineId": machine_id,
    }
    p = CRED_DIR / f"trae-{name}.json"
    p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n  ✅ Trae 登录成功")
    print(f"  用户: {screen_name or user_id or '(未知)'}")
    print(f"  过期: {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}")
    print(f"  凭证已保存: {p}")
    print(f"\n  提示: deviceId = {bound_device_id}")
    print(f"  （Trae 按设备去重，请勿改动此值，否则可能影响签到）")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    global DEBUG
    ap = argparse.ArgumentParser(description="首次 OAuth 登录，获取签到凭证")
    ap.add_argument("product", choices=["workbuddy", "trae"], help="要登录的产品")
    ap.add_argument("--debug", action="store_true", help="打印调试信息")
    args = ap.parse_args()
    DEBUG = args.debug

    print(f"\n=== {args.product} 登录 ===\n")
    try:
        if args.product == "workbuddy":
            return login_workbuddy()
        else:
            return login_trae()
    except KeyboardInterrupt:
        print("\n  已取消。")
        return 130
    except Exception as e:
        print(f"\n  ❌ 登录失败: {e}")
        if DEBUG:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
