"""
模拟验证流程脚本：
1. 创建验证会话（scraper内部API）
2. 打印前端填码链接
3. 提交验证码（模拟用户填码）
4. 循环查询状态，确认 code_available=True
5. 调用 consume 获取验证码

用法：
  python scripts/simulate_verify_flow.py [account_id]

默认 account_id=9
"""
from __future__ import annotations

import json
import sys
import time
from urllib import error, request as urllib_request


BASE_URL = "https://aycmzl.com/api/v1"
SCRAPER_API_KEY = "change-me-scraper-key"  # 与 .env.prod 保持一致
FRONTEND_BASE = "https://aycmzl.com"

ACCOUNT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 9


def scraper_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib_request.Request(url=url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Scraper-Key", SCRAPER_API_KEY)
    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def user_request(method: str, path: str, body: dict | None = None) -> dict:
    """无需鉴权的用户侧接口（前端调用）"""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib_request.Request(url=url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def main():
    print(f"=== 模拟验证流程 account_id={ACCOUNT_ID} ===\n")

    # 1. 创建验证会话
    print("[1] 创建验证会话...")
    session = scraper_request("POST", "/shops/internal/verification-sessions", {
        "account_id": ACCOUNT_ID,
        "source": "SCRAPER",
    })
    request_id = session["request_id"]
    verify_token = session["verify_token"]
    expires_at = session.get("expires_at", "?")
    print(f"    request_id  = {request_id}")
    print(f"    verify_token= {verify_token}")
    print(f"    expires_at  = {expires_at}")
    print()

    # 2. 打印填码链接
    link = f"{FRONTEND_BASE}/verify-code/{verify_token}"
    print(f"[2] 填码链接（可发给用户）：\n    {link}\n")

    # 3. 让操作者输入验证码（也可走真实前端页面）
    code = input("[3] 请输入短信验证码（直接回传到后端）: ").strip()
    if not code:
        print("未输入验证码，退出。")
        return

    # 4. 通过用户侧 API 提交（模拟前端页面行为）
    print(f"[4] 提交验证码 {code!r} ...")
    try:
        resp = user_request("POST", f"/shops/verify/{verify_token}", {"code": code})
        print(f"    提交结果: {resp}")
    except RuntimeError as e:
        print(f"    提交失败: {e}")
        return
    print()

    # 5. 轮询状态直到 code_available=True
    print("[5] 轮询 scraper 侧状态（等待 scraper 消费）...")
    for i in range(30):
        status = scraper_request("GET", f"/shops/internal/verification-sessions/{request_id}")
        ca = status.get("code_available", False)
        st = status.get("status", "?")
        print(f"    [{i+1:02d}] status={st} code_available={ca}")
        if ca:
            print("    ✓ 验证码已可被 scraper 消费！")
            break
        time.sleep(2)
    else:
        print("    超时，code_available 仍为 False")
        return
    print()

    # 6. Consume（模拟 scraper 读取验证码）
    print("[6] 调用 consume（scraper 读取验证码）...")
    result = scraper_request("POST", f"/shops/internal/verification-sessions/{request_id}/consume", {})
    consumed_code = result.get("code", "?")
    print(f"    consume 返回验证码: {consumed_code!r}")
    print()
    print("=== 全流程验证完成 ===")
    print(f"  实际验证码: {consumed_code}")
    print("  下一步：scraper 会将此验证码填入京东页面并提交登录。")


if __name__ == "__main__":
    main()
