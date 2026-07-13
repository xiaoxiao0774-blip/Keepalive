#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godlike 服务器自动启动/保活脚本
- 直达目标容器控制台，智能处理登录重定向
- 采用 DOM 视觉特征判定登录态（无视 SPA 路由欺骗）
- 【核心特化】采用实体键盘 Enter 键强制提交表单，绕过所有前端框架拦截
- 适配 ultra 节点与折叠表单穿透
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

# 【核心配置】用户提供的精准容器直达链接
SERVER_URL = "https://ultra.panel.godlike.host/server/fa33dea8"

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

# ---------------- 核心流程：直达与智能登录 ----------------
def access_and_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"尝试直达服务器面板: {SERVER_URL}")
    try:
        page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    needs_login = False
    if "login" in page.url.lower():
        needs_login = True
    elif page.locator("text=Login to continue").count() > 0:
        needs_login = True
    elif page.locator("text=Through Login/Password").count() > 0:
        needs_login = True
    elif page.locator("text=Authorization").count() > 0:
        needs_login = True

    if not needs_login:
        logger.info("未检测到登录框特征，判定为已成功直达控制面板（跳过登录）")
        return True

    logger.info("系统要求身份验证，正在处理登录表单...")
    
    try:
        toggle_loc = page.locator("text=Through Login/Password").first
        if toggle_loc.count() > 0 and toggle_loc.is_visible():
            logger.info("展开折叠的账号密码输入框...")
            toggle_loc.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass

    email_loc, email_sel = find_first_visible(page, [
        'input[name="user"]',
        'input[name="username"]',
        'input[type="email"]',
        'input[type="text"]',
    ])
    pwd_loc, pwd_sel = find_first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
    ])

    if not email_loc or not pwd_loc:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录表单，前端渲染异常或遭人机拦截")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    
    # 【核心降维打击】为了防止前端框架忽略 fill 事件，采用按键级输入密码
    pwd_loc.click() 
    pwd_loc.press_sequentially(password, delay=50) # 模拟真人键盘输入
    page.wait_for_timeout(500)

    # 【核心降维打击】彻底废弃点击按钮，直接在密码框敲击实体回车键强制提交
    logger.info("模拟实体键盘敲击 Enter (回车) 键强制提交表单...")
    try:
        pwd_loc.press("Enter")
    except Exception as e:
        logger.error(f"回车提交异常: {e}")
        return False

    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(STEP_WAIT)

    if page.locator("text=Login to continue").count() > 0 or "login" in page.url.lower():
        page.screenshot(path=f"debug_login_failed_{int(time.time())}.png")
        logger.error("登录后仍在登录页，验证失败（可能密码错误或被防火墙拦截）")
        return False

    logger.info("登录成功，等待面板渲染...")
    page.wait_for_timeout(5000)
    return True

# ---------------- 启动服务器流程 ----------------
def start_server(page: Page) -> str:
    logger.info("寻找 Start 按钮")
    page.wait_for_timeout(STEP_WAIT)

    try:
        page.wait_for_selector('button:has-text("Start")', timeout=15000)
    except PWTimeout:
        pass

    start_btn, sel, txt = find_button_by_text(page, ["Start"])
    if not start_btn:
        page.screenshot(path=f"debug_start_{int(time.time())}.png")
        logger.error("未找到 Start 按钮，面板可能未加载")
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
    stop_btn, sel, txt = find_button_by_text(page, ["Kill", "Restart", "Stop"])
    if not stop_btn:
        return "not_found"
    return "clickable" if is_clickable(stop_btn) else "exists_not_clickable"

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

        console_lines = []
        page.on("console", lambda msg: console_lines.append(msg.text or ""))

        if not access_and_login(page, email, password):
            result["error"] = "登录或访问面板失败"
            return result

        status = start_server(page)
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
