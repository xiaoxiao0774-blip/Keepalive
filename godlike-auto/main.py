#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godlike 服务器自动启动/保活脚本
- 支持多账号轮流操作
- 自动登录 Godlike 翼龙面板 (适配 ultra 节点与折叠表单)
- 自动选择首个服务器实例 -> 判断 Start 按钮状态 -> 启动服务器
- 通过 Kill/Restart 按钮可点击状态验证是否成功上线
"""

import json
import os
import sys
import time
import logging
import argparse
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import notify

# ---------------- 日志配置 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("run.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("godlike-auto")

# 针对 Godlike 面板的 URL 配置 (已修正为你的 ultra 专属节点)
LOGIN_URL = "https://ultra.panel.godlike.host/auth/login"
HOME_URL = "https://ultra.panel.godlike.host"

START_WAIT_TIMEOUT = 120
STEP_WAIT = 3000
LOGIN_PAGE_WAIT = 6000


# ---------------- 账号加载 ----------------
def parse_accounts_string(raw: str):
    accounts = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        email, password = item.split(":", 1)
        email, password = email.strip(), password.strip()
        if email and password:
            accounts.append({"email": email, "password": password})
    return accounts

def load_accounts():
    accounts_env = os.environ.get("ACCOUNTS", "").strip()
    if accounts_env:
        accounts = parse_accounts_string(accounts_env)
        if accounts:
            logger.info(f"从环境变量 ACCOUNTS 加载到 {len(accounts)} 个账号")
            return accounts

    accounts_file = os.environ.get("ACCOUNTS_FILE", "accounts.json")
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        logger.info(f"从文件 {accounts_file} 加载到 {len(data)} 个账号")
        return data

    raise RuntimeError(
        "未配置账号：请设置环境变量 ACCOUNTS（格式 email:password,...）或创建 accounts.json"
    )

# ---------------- 通用辅助 ----------------
def is_clickable(locator) -> bool:
    try:
        if locator.count() == 0:
            return False
        el = locator.first
        if not el.is_visible() or not el.is_enabled():
            return False
        if el.get_attribute("disabled") is not None:
            return False
        aria_disabled = el.get_attribute("aria-disabled")
        if aria_disabled and aria_disabled.lower() == "true":
            return False
        if el.evaluate("el => getComputedStyle(el).pointerEvents") == "none":
            return False
        return True
    except Exception:
        return False

def find_first_visible(page: Page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None

def find_button_by_text(page: Page, texts):
    for text in texts:
        for sel in [
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'[role="button"]:has-text("{text}")',
            f'input[type="submit"][value*="{text}" i]',
            f'input[type="button"][value*="{text}" i]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc, sel, text
            except Exception:
                continue
    return None, None, None

# ---------------- 登录流程 ----------------
def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    # 【新增免疫机制】针对 Godlike 的折叠表单进行穿透点击
    try:
        toggle_loc = page.locator("text='Through login/password'").first
        if toggle_loc.count() > 0 and toggle_loc.is_visible():
            logger.info("检测到折叠的登录表单，正在点击展开...")
            toggle_loc.click()
            page.wait_for_timeout(1500) # 等待输入框动画弹出
    except Exception:
        pass

    email_loc, email_sel = find_first_visible(page, [
        'input[name="username"]',
        'input[type="text"]',
        'input[type="email"]',
        'input[name="email"]',
    ])
    pwd_loc, pwd_sel = find_first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
    ])

    if not email_loc or not pwd_loc:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录表单（邮箱/密码输入框）")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    login_btn, login_sel, txt = find_button_by_text(page, ["Login", "Sign in"])
    if not login_btn:
        login_btn, login_sel = find_first_visible(page, [
            'button[type="submit"]',
        ])
        txt = "submit(fallback)"

    if not login_btn:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录按钮")
        return False

    logger.info(f"点击登录按钮 (text={txt})")
    try:
        login_btn.click()
    except Exception:
        login_btn.first.click(force=True)

    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(STEP_WAIT)

    if "/auth/login" in page.url:
        logger.error("登录后仍在登录页，可能账号密码错误或遭遇人机验证")
        return False

    logger.info("登录成功")
    return True

# ---------------- Godlike 选择服务器实例流程 ----------------
def click_manage_server(page: Page) -> bool:
    logger.info("寻找并进入首个服务器实例 (Server Card)")
    page.wait_for_timeout(STEP_WAIT)

    try:
        # 翼龙面板通常通过 /server/ UUID 访问实例
        page.wait_for_selector('a[href*="/server/"]', timeout=15000)
        server_link = page.locator('a[href*="/server/"]').first
        
        if server_link.count() > 0:
            logger.info("找到服务器实例，点击进入")
            try:
                server_link.click()
            except Exception:
                server_link.first.click(force=True)
                
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            page.wait_for_timeout(6000) # 等待控制台界面渲染
            return True
    except PWTimeout:
        pass

    # 如果已经在实例页面（URL 包含 /server/）
    if "/server/" in page.url:
        logger.info("当前已在服务器实例控制台中")
        return True

    page.screenshot(path=f"debug_dashboard_{int(time.time())}.png")
    logger.error("未找到服务器实例链接，也未处于控制台界面")
    return False

# ---------------- 启动服务器流程 ----------------
def start_server(page: Page, console_lines: list) -> str:
    logger.info("寻找 Start 按钮")
    page.wait_for_timeout(STEP_WAIT)

    try:
        page.wait_for_selector('button:has-text("Start")', timeout=15000)
    except PWTimeout:
        pass

    # 针对 Godlike 面板的绿色 Start 按钮
    start_btn, sel, txt = find_button_by_text(page, ["Start"])
    if not start_btn:
        page.screenshot(path=f"debug_start_{int(time.time())}.png")
        logger.error("未找到 Start 按钮")
        return "no_start"

    clickable = is_clickable(start_btn)
    logger.info(f"Start 按钮可点击状态: {clickable}")

    if not clickable:
        logger.info("Start 按钮不可点击 -> 服务器可能已在线或正在启动中，跳过启动")
        if check_stop_button(page) == "clickable":
            logger.info("Kill/Restart 按钮可点击，服务器确实处于活跃状态")
        return "online"

    logger.info("服务器处于离线状态，点击 Start 启动")
    try:
        start_btn.click()
    except Exception:
        start_btn.first.click(force=True)

    # 舍弃对日志的死等，直接循环探测停止按钮的状态变化
    logger.info(f"等待容器启动（最长 {START_WAIT_TIMEOUT}s）")
    deadline = time.time() + START_WAIT_TIMEOUT
    started = False
    
    while time.time() < deadline:
        if check_stop_button(page) == "clickable":
            started = True
            break
        page.wait_for_timeout(3000)

    if started:
        logger.info("验证成功：Kill/Restart 按钮可点击，服务器已成功上线")
        return "started"
    else:
        logger.warning("验证未通过：等待超时，Kill/Restart 按钮仍不可点击")
        return "offline"

def check_stop_button(page: Page) -> str:
    # 针对 Godlike 面板截图，探测 Restart 或 Kill 按钮
    stop_btn, sel, txt = find_button_by_text(page, ["Kill", "Restart", "Stop"])
    
    if not stop_btn:
        logger.info("未找到 Kill/Restart 按钮")
        return "not_found"

    clickable = is_clickable(stop_btn)
    return "clickable" if clickable else "exists_not_clickable"

# ---------------- 单账号处理 ----------------
def process_account(account: dict, playwright, headless: bool = True) -> dict:
    email = account.get("email", "").strip()
    password = account.get("password", "").strip()
    result = {"email": email, "ok": False, "status": "unknown", "error": ""}

    if not email or not password:
        result["error"] = "账号或密码为空"
        return result

    logger.info(f"========== 开始处理账号: {email} ==========")
    browser = None
    try:
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()

        # 保留底层免疫补丁，预防 SPA 框架崩溃
        page.add_init_script("""
            const originalParse = JSON.parse;
            JSON.parse = function(text, reviver) {
                if (text === null || text === undefined || text === '') return {};
                try { return originalParse(text, reviver); } 
                catch (e) { return {}; }
            };
        """)

        console_lines = []
        page.on("console", lambda msg: console_lines.append(msg.text or ""))

        if not do_login(page, email, password):
            result["error"] = "登录失败"
            return result

        if not click_manage_server(page):
            result["error"] = "未能进入服务器实例面板"
            return result

        status = start_server(page, console_lines)
        result["status"] = status
        result["ok"] = status in ("started", "online")
        return result

    except Exception as e:
        result["error"] = f"异常: {e}"
        logger.exception("处理账号时发生异常")
        return result
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
        logger.info(f"========== 账号 {email} 处理结束: status={result['status']} ==========\n")

# ---------------- 主入口 ----------------
def main():
    parser = argparse.ArgumentParser(description="Godlike 服务器自动启动")
    parser.add_argument("--headed", action="store_true", help="非无头模式（调试用）")
    parser.add_argument("--only", help="只处理指定邮箱的账号")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.only:
        accounts = [a for a in accounts if a.get("email") == args.only]

    logger.info(f"共 {len(accounts)} 个账号待处理")
    results = []
    
    with sync_playwright() as pw:
        for idx, acc in enumerate(accounts, 1):
            logger.info(f"--- 第 {idx}/{len(accounts)} 个账号 ---")
            res = process_account(acc, pw, headless=not args.headed)
            results.append(res)
            if idx < len(accounts):
                time.sleep(5)

    ok = sum(1 for r in results if r["ok"])
    logger.info("================ 结果汇总 ================")
    for r in results:
        flag = "OK" if r["ok"] else "FAIL"
        logger.info(f"[{flag}] {r['email']} | status={r['status']} | {r['error']}")
    logger.info(f"成功 {ok}/{len(results)}")

    if notify.tg_enabled():
        notify.notify_summary(results)

    sys.exit(0 if ok == len(results) and ok > 0 else 1)

if __name__ == "__main__":
    main()
