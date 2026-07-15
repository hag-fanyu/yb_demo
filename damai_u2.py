#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大麦网 uiautomator2 自动化核心模块

通过 uiautomator2 驱动 Android 设备上的大麦 APP，在其内嵌 WebView (H5) 中完成：
  1. 登录（手机号 + 短信验证码）
  2. 搜索演出
  3. 提取登录态 cookies → 注入 DamaiMonitor 查询余票

依赖：
  pip install uiautomator2
  设备需开启 USB 调试并通过 adb 连接
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import uiautomator2 as u2
except ImportError:
    sys.stderr.write(
        "缺少依赖 uiautomator2，请先执行：pip install uiautomator2\n"
    )
    sys.exit(1)


# ─── 常量 ────────────────────────────────────────────────────────────────

DAMAI_PACKAGE = "cn.damai"
DAMAI_ACTIVITY = "cn.damai.homepage.ui.MainActivity"

# 大麦 H5 登录页 URL
H5_LOGIN_URL = "https://passport.damai.cn/login.htm"
# 大麦 H5 首页
H5_HOME_URL = "https://m.damai.cn/"
# 大麦 H5 搜索页
H5_SEARCH_URL = "https://search.damai.cn/search.html"

# 等待超时（秒）
DEFAULT_TIMEOUT = 15
LONG_TIMEOUT = 30


# ─── 核心自动化类 ────────────────────────────────────────────────────────

class DamaiU2Automation:
    """大麦网 uiautomator2 自动化（APP 内 WebView）。"""

    def __init__(self, device_serial: Optional[str] = None, verbose: bool = False):
        """
        Args:
            device_serial: 设备序列号，None 则自动检测
            verbose: 是否输出详细日志
        """
        self.device_serial = device_serial
        self.verbose = verbose
        self.d: Optional[u2.Device] = None
        self._wd = None  # WebDriver (chrome devtools)
        self._native_context: Optional[str] = None
        self._webview_context: Optional[str] = None
        self._cookies: List[Dict[str, Any]] = []

    # ── 日志 ──────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [u2] {msg}")

    @staticmethod
    def _warn(msg: str) -> None:
        sys.stderr.write(f"[warn] {msg}\n")

    # ── 设备连接 ──────────────────────────────────────────────────────
    def connect_device(self) -> None:
        """连接 Android 设备并初始化 uiautomator2。"""
        print("📱 正在连接设备…")

        try:
            if self.device_serial:
                self.d = u2.connect(self.device_serial)
            else:
                self.d = u2.connect()  # 自动检测
        except Exception as e:
            print(f"\n❌ 设备连接失败：{e}")
            print("\n请确认：")
            print("  1. 手机已开启 USB 调试（设置 → 开发者选项 → USB 调试）")
            print("  2. 手机已通过 USB 连接电脑")
            print("  3. 已安装 adb 并在 PATH 中")
            print("  4. 运行 adb devices 确认设备可见")
            print("  5. 如使用无线连接：adb connect <IP:端口>")
            sys.exit(1)

        device_info = self.d.info
        print(f"✅ 已连接设备：{self.d.serial}")
        self._log(f"设备信息：{device_info}")

        # 初始化 ATX agent
        self._log("初始化 ATX agent…")
        try:
            self.d.set_fastinput_ime(True)
        except Exception as e:
            self._warn(f"设置输入法失败（非致命）：{e}")

    # ── APP 启动 ──────────────────────────────────────────────────────
    def launch_damai_app(self) -> None:
        """启动大麦 APP。"""
        print("🚀 正在启动大麦 APP…")
        try:
            self.d.app_start(DAMAI_PACKAGE, DAMAI_ACTIVITY, wait=True)
            time.sleep(3)  # 等待 APP 启动
            self._log("大麦 APP 已启动")
        except Exception as e:
            self._warn(f"启动大麦 APP 失败：{e}")
            # 尝试只用包名启动
            try:
                self.d.app_start(DAMAI_PACKAGE, wait=True)
                time.sleep(3)
                self._log("大麦 APP 已启动（备用方式）")
            except Exception as e2:
                print(f"❌ 无法启动大麦 APP：{e2}")
                print("请确认已安装大麦 APP（包名：cn.damai）")
                sys.exit(1)

    def stop_damai_app(self) -> None:
        """停止大麦 APP。"""
        try:
            self.d.app_stop(DAMAI_PACKAGE)
            self._log("大麦 APP 已停止")
        except Exception:
            pass

    # ── WebView 上下文切换 ────────────────────────────────────────────
    def _get_webviews(self) -> List[str]:
        """获取当前所有 WebView 上下文。"""
        # u2 通过 adb shell dumpsys 获取 webview
        try:
            output = self.d.shell("dumpsys activity top | grep -i webview")[0]
        except Exception:
            output = ""

        # 通过 chrome devtools 获取
        try:
            # u2 2.x 使用 d.webdriver
            wd = self.d.webdriver
            if wd:
                contexts = wd.contexts
                self._log(f"可用上下文：{contexts}")
                return contexts
        except Exception as e:
            self._log(f"通过 webdriver 获取上下文失败：{e}")

        return []

    def switch_to_webview(self) -> bool:
        """切换到 WebView 上下文。

        Returns:
            是否成功切换
        """
        print("🔄 正在切换到 WebView…")

        # 先尝试通过 u2 的 webdriver 接口
        try:
            wd = self.d.webdriver
            contexts = wd.contexts
            self._log(f"可用上下文：{contexts}")

            # 记录 native 上下文
            self._native_context = contexts[0] if contexts else "NATIVE_APP"

            # 找到 webview 上下文
            for ctx in contexts:
                if "webview" in ctx.lower() or "chrome" in ctx.lower():
                    self._webview_context = ctx
                    wd.switch_to(ctx)
                    self._wd = wd
                    print(f"✅ 已切换到 WebView 上下文：{ctx}")
                    return True

            # 如果只有一个上下文，可能 APP 还没加载 WebView
            self._warn("未找到 WebView 上下文，尝试等待后重试…")
            time.sleep(3)

            contexts = wd.contexts
            for ctx in contexts:
                if "webview" in ctx.lower() or "chrome" in ctx.lower():
                    self._webview_context = ctx
                    wd.switch_to(ctx)
                    self._wd = wd
                    print(f"✅ 已切换到 WebView 上下文：{ctx}")
                    return True

        except Exception as e:
            self._warn(f"WebDriver 方式切换失败：{e}")

        # 备用方案：通过 adb 直接操作 Chrome DevTools Protocol
        print("⚠️ WebView 上下文切换失败。")
        print("请尝试：")
        print("  1. 确保大麦 APP 已打开并显示了 H5 页面")
        print("  2. 在手机开发者选项中开启「WebView 调试」")
        print("  3. 重启 APP 后重试")
        return False

    def switch_to_native(self) -> None:
        """切换回 Native 上下文。"""
        if self._wd and self._native_context:
            try:
                self._wd.switch_to(self._native_context)
                self._log("已切换回 Native 上下文")
            except Exception as e:
                self._warn(f"切换回 Native 失败：{e}")

    # ── H5 页面导航 ──────────────────────────────────────────────────
    def navigate_to_url(self, url: str) -> None:
        """在 WebView 中导航到指定 URL。"""
        if not self._wd:
            self._warn("未连接 WebView，无法导航")
            return

        try:
            self._wd.get(url)
            self._log(f"已导航到：{url}")
            time.sleep(2)
        except Exception as e:
            self._warn(f"导航失败：{e}")

    def get_current_url(self) -> str:
        """获取 WebView 当前 URL。"""
        if not self._wd:
            return ""
        try:
            return self._wd.current_url
        except Exception:
            return ""

    # ── 登录流程 ──────────────────────────────────────────────────────
    def navigate_to_login(self) -> bool:
        """导航到登录页面。

        策略：
          1. 先尝试在 APP native 层点击「我的」→ 登录
          2. 备用：直接在 WebView 中打开登录页 URL

        Returns:
            是否成功到达登录页
        """
        print("🔑 正在导航到登录页面…")

        # 策略 1：在 native 层操作
        try:
            self.switch_to_native()
            time.sleep(1)

            # 点击底部「我的」tab
            my_tab = self.d(text="我的")
            if my_tab.exists(timeout=5):
                my_tab.click()
                self._log("已点击「我的」tab")
                time.sleep(2)

                # 查找登录/注册按钮
                login_btn = self.d(textContains="登录")
                if login_btn.exists(timeout=3):
                    login_btn.click()
                    self._log("已点击登录按钮")
                    time.sleep(2)
                    return True

                # 查找头像（未登录时点击头像进入登录）
                avatar = self.d(resourceIdMatches=".*avatar.*|.*user.*icon.*")
                if avatar.exists(timeout=3):
                    avatar.click()
                    self._log("已点击头像进入登录")
                    time.sleep(2)
                    return True

        except Exception as e:
            self._log(f"Native 层导航登录失败：{e}")

        # 策略 2：在 WebView 中直接打开登录页
        print("  尝试通过 H5 页面登录…")
        if self.switch_to_webview():
            self.navigate_to_url(H5_LOGIN_URL)
            time.sleep(3)
            current = self.get_current_url()
            if "passport" in current or "login" in current:
                print("✅ 已到达登录页面")
                return True

        print("⚠️ 无法导航到登录页面，请手动在 APP 中打开登录页后重试")
        return False

    def input_phone(self, phone: str) -> bool:
        """在登录页输入手机号。

        Args:
            phone: 手机号码

        Returns:
            是否成功输入
        """
        print(f"📝 正在输入手机号：{phone}")

        # 先尝试 WebView 方式
        if self._wd:
            try:
                # 查找手机号输入框
                phone_input = self._wd.find_element_by_css_selector(
                    'input[type="tel"], input[name="mobile"], '
                    'input[placeholder*="手机"], input[placeholder*="号码"]'
                )
                if phone_input:
                    phone_input.clear()
                    phone_input.send_keys(phone)
                    self._log("已通过 WebView 输入手机号")
                    return True
            except Exception as e:
                self._log(f"WebView 输入手机号失败：{e}")

        # 备用：Native 层输入
        try:
            self.switch_to_native()
            # 查找手机号输入框
            phone_field = self.d(
                resourceIdMatches=".*phone.*|.*mobile.*|.*account.*"
            )
            if phone_field.exists(timeout=3):
                phone_field.set_text(phone)
                self._log("已通过 Native 输入手机号")
                return True

            # 通过 className 查找 EditText
            edit_fields = self.d(className="android.widget.EditText")
            if edit_fields.exists(timeout=3):
                edit_fields.set_text(phone)
                self._log("已通过 EditText 输入手机号")
                return True

        except Exception as e:
            self._log(f"Native 输入手机号失败：{e}")

        self._warn("无法找到手机号输入框")
        return False

    def click_send_code(self) -> bool:
        """点击发送验证码按钮。

        Returns:
            是否成功点击
        """
        print("📤 正在点击发送验证码…")

        # WebView 方式
        if self._wd:
            try:
                send_btn = self._wd.find_element_by_css_selector(
                    'button:has-text("获取验证码"), '
                    '.send-code, .get-code, '
                    '[class*="send"], [class*="code"]'
                )
                if send_btn:
                    send_btn.click()
                    self._log("已通过 WebView 点击发送验证码")
                    return True
            except Exception as e:
                self._log(f"WebView 点击发送验证码失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            send_btn = self.d(textContains="获取验证码")
            if send_btn.exists(timeout=3):
                send_btn.click()
                self._log("已通过 Native 点击发送验证码")
                return True

            send_btn = self.d(textContains="发送验证码")
            if send_btn.exists(timeout=3):
                send_btn.click()
                self._log("已通过 Native 点击发送验证码")
                return True

            send_btn = self.d(textContains="获取短信")
            if send_btn.exists(timeout=3):
                send_btn.click()
                self._log("已通过 Native 点击发送验证码")
                return True

        except Exception as e:
            self._log(f"Native 点击发送验证码失败：{e}")

        self._warn("无法找到发送验证码按钮")
        return False

    def input_verify_code(self, code: str) -> bool:
        """输入短信验证码。

        Args:
            code: 验证码

        Returns:
            是否成功输入
        """
        print(f"📝 正在输入验证码：{code}")

        # WebView 方式
        if self._wd:
            try:
                code_input = self._wd.find_element_by_css_selector(
                    'input[type="number"], input[name="code"], '
                    'input[name="verifyCode"], '
                    'input[placeholder*="验证码"]'
                )
                if code_input:
                    code_input.clear()
                    code_input.send_keys(code)
                    self._log("已通过 WebView 输入验证码")
                    return True
            except Exception as e:
                self._log(f"WebView 输入验证码失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            # 查找验证码输入框（通常是第二个 EditText 或有特定 resourceId）
            code_field = self.d(
                resourceIdMatches=".*code.*|.*verify.*|.*sms.*"
            )
            if code_field.exists(timeout=3):
                code_field.set_text(code)
                self._log("已通过 Native 输入验证码")
                return True

            # 查找所有 EditText，取第二个（第一个是手机号）
            edit_fields = self.d(className="android.widget.EditText")
            count = edit_fields.count
            if count >= 2:
                edit_fields[count - 1].set_text(code)
                self._log("已通过第二个 EditText 输入验证码")
                return True

        except Exception as e:
            self._log(f"Native 输入验证码失败：{e}")

        self._warn("无法找到验证码输入框")
        return False

    def click_login(self) -> bool:
        """点击登录按钮。

        Returns:
            是否成功点击
        """
        print("🔐 正在点击登录…")

        # WebView 方式
        if self._wd:
            try:
                login_btn = self._wd.find_element_by_css_selector(
                    'button[type="submit"], '
                    'button:has-text("登录"), '
                    '.login-btn, [class*="submit"]'
                )
                if login_btn:
                    login_btn.click()
                    self._log("已通过 WebView 点击登录")
                    return True
            except Exception as e:
                self._log(f"WebView 点击登录失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            login_btn = self.d(text="登录")
            if login_btn.exists(timeout=3):
                login_btn.click()
                self._log("已通过 Native 点击登录")
                return True

            login_btn = self.d(textContains="登录")
            if login_btn.exists(timeout=3):
                login_btn.click()
                self._log("已通过 Native 点击登录")
                return True

        except Exception as e:
            self._log(f"Native 点击登录失败：{e}")

        self._warn("无法找到登录按钮")
        return False

    def wait_login_success(self, timeout: int = LONG_TIMEOUT) -> bool:
        """等待登录成功。

        检测方式：
          1. 页面 URL 变化（离开登录页）
          2. 出现用户头像/昵称元素
          3. 获取到登录态 cookies

        Args:
            timeout: 最大等待秒数

        Returns:
            是否登录成功
        """
        print("⏳ 等待登录完成…")

        start = time.time()
        old_url = self.get_current_url()

        while time.time() - start < timeout:
            # 检查 URL 是否已离开登录页
            current_url = self.get_current_url()
            if current_url and "login" not in current_url and "passport" not in current_url:
                if current_url != old_url:
                    print("✅ 登录成功（页面已跳转）")
                    return True

            # 检查 native 层是否出现用户信息
            try:
                self.switch_to_native()
                if self.d(textContains="我的订单").exists(timeout=1):
                    print("✅ 登录成功（检测到用户信息）")
                    return True
                if self.d(textContains="退出").exists(timeout=1):
                    print("✅ 登录成功（检测到退出按钮）")
                    return True
            except Exception:
                pass

            time.sleep(2)

        self._warn("登录超时")
        return False

    # ── Cookie 提取 ──────────────────────────────────────────────────
    def get_cookies(self) -> List[Dict[str, Any]]:
        """从 WebView 提取 cookies。

        Returns:
            Cookie 列表，每项为 dict（name, value, domain, path, …）
        """
        print("🍪 正在提取 cookies…")

        # 方式 1：通过 Chrome DevTools Protocol
        if self._wd:
            try:
                # 尝试 CDP 方式
                result = self._wd.execute_cdp_cmd("Network.getCookies", {})
                if result and "cookies" in result:
                    self._cookies = result["cookies"]
                    print(f"✅ 已提取 {len(self._cookies)} 个 cookies（CDP 方式）")
                    return self._cookies
            except Exception as e:
                self._log(f"CDP 获取 cookies 失败：{e}")

            # 方式 2：通过 WebDriver 标准接口
            try:
                self._cookies = self._wd.get_cookies()
                if self._cookies:
                    print(f"✅ 已提取 {len(self._cookies)} 个 cookies（WebDriver 方式）")
                    return self._cookies
            except Exception as e:
                self._log(f"WebDriver 获取 cookies 失败：{e}")

        # 方式 3：通过 JavaScript 获取
        if self._wd:
            try:
                cookie_str = self._wd.execute_script("return document.cookie;")
                if cookie_str:
                    self._cookies = self._parse_cookie_string(cookie_str)
                    print(f"✅ 已提取 {len(self._cookies)} 个 cookies（JS 方式）")
                    return self._cookies
            except Exception as e:
                self._log(f"JS 获取 cookies 失败：{e}")

        self._warn("未能提取 cookies")
        return []

    @staticmethod
    def _parse_cookie_string(cookie_str: str) -> List[Dict[str, Any]]:
        """解析 document.cookie 字符串为 cookie 列表。"""
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".damai.cn",
                "path": "/",
            })
        return cookies

    def get_cookie_string(self) -> str:
        """获取 cookie 字符串（key=value; key2=value2 格式）。

        可直接注入到 DamaiMonitor 使用。
        """
        if not self._cookies:
            self.get_cookies()

        parts = []
        for c in self._cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            if name and value:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    def save_cookies(self, path: str = "damai_cookies_u2.json") -> None:
        """保存 cookies 到文件。"""
        if not self._cookies:
            self.get_cookies()

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._cookies, f, ensure_ascii=False, indent=2)
            self._log(f"Cookies 已保存到 {path}（{len(self._cookies)} 个）")
        except Exception as e:
            self._warn(f"保存 cookies 失败：{e}")

    # ── 搜索演出 ──────────────────────────────────────────────────────
    def navigate_to_search(self) -> bool:
        """导航到搜索页面。

        Returns:
            是否成功到达搜索页
        """
        print("🔍 正在导航到搜索页面…")

        # 策略 1：Native 层点击搜索入口
        try:
            self.switch_to_native()

            # 查找搜索框/搜索按钮
            search_entry = self.d(textContains="搜索")
            if search_entry.exists(timeout=3):
                search_entry.click()
                self._log("已点击搜索入口")
                time.sleep(2)
                return True

            # 查找搜索图标
            search_icon = self.d(
                resourceIdMatches=".*search.*|.*home_search.*"
            )
            if search_icon.exists(timeout=3):
                search_icon.click()
                self._log("已点击搜索图标")
                time.sleep(2)
                return True

        except Exception as e:
            self._log(f"Native 搜索导航失败：{e}")

        # 策略 2：WebView 中打开搜索页
        if self.switch_to_webview():
            self.navigate_to_url(H5_SEARCH_URL)
            time.sleep(3)
            return True

        return False

    def input_search_keyword(self, keyword: str) -> bool:
        """输入搜索关键词。

        Args:
            keyword: 搜索关键词

        Returns:
            是否成功输入并触发搜索
        """
        print(f"📝 正在搜索：{keyword}")

        # WebView 方式
        if self._wd:
            try:
                search_input = self._wd.find_element_by_css_selector(
                    'input[type="search"], input[name="keyword"], '
                    'input[placeholder*="搜索"], .search-input input'
                )
                if search_input:
                    search_input.clear()
                    search_input.send_keys(keyword)
                    # 按回车触发搜索
                    from selenium.webdriver.common.keys import Keys
                    search_input.send_keys(Keys.ENTER)
                    self._log("已通过 WebView 输入搜索关键词")
                    time.sleep(3)
                    return True
            except ImportError:
                # 没有 selenium Keys，尝试 JS 方式
                try:
                    self._wd.execute_script(
                        f"document.querySelector('input[type=\"search\"]').value = '{keyword}';"
                        f"document.querySelector('form').submit();"
                    )
                    self._log("已通过 JS 输入搜索关键词")
                    time.sleep(3)
                    return True
                except Exception as e2:
                    self._log(f"JS 搜索失败：{e2}")
            except Exception as e:
                self._log(f"WebView 搜索失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()
            search_field = self.d(
                resourceIdMatches=".*search.*input.*|.*query.*"
            )
            if search_field.exists(timeout=3):
                search_field.set_text(keyword)
                # 点击搜索按钮
                search_btn = self.d(textContains="搜索")
                if search_btn.exists(timeout=2):
                    search_btn.click()
                else:
                    # 按键盘回车
                    self.d.press("enter")
                self._log("已通过 Native 输入搜索关键词")
                time.sleep(3)
                return True

            # 通用 EditText
            edit = self.d(className="android.widget.EditText")
            if edit.exists(timeout=3):
                edit.set_text(keyword)
                self.d.press("enter")
                self._log("已通过 EditText 搜索")
                time.sleep(3)
                return True

        except Exception as e:
            self._log(f"Native 搜索失败：{e}")

        self._warn("无法输入搜索关键词")
        return False

    def get_first_result(self) -> Optional[Dict[str, str]]:
        """获取第一条搜索结果。

        Returns:
            dict with keys: name, item_id, url; or None
        """
        print("📋 正在获取第一条搜索结果…")

        result: Dict[str, str] = {}

        # WebView 方式
        if self._wd:
            try:
                # 查找搜索结果列表中的第一个链接
                first_item = self._wd.find_element_by_css_selector(
                    '.search-result a, .result-list a, '
                    '[class*="item"] a, [class*="card"] a'
                )
                if first_item:
                    result["name"] = first_item.text
                    result["url"] = first_item.get_attribute("href") or ""

                    # 从 URL 提取 item_id
                    href = result.get("url", "")
                    id_match = re.search(r"id=(\d+)", href)
                    if id_match:
                        result["item_id"] = id_match.group(1)

                    self._log(f"第一条结果：{result}")
                    return result if result.get("name") or result.get("item_id") else None

            except Exception as e:
                self._log(f"WebView 获取搜索结果失败：{e}")

        # Native 方式
        try:
            self.switch_to_native()

            # 查找第一个搜索结果项
            first_item = self.d(
                resourceIdMatches=".*item.*name.*|.*title.*|.*result.*"
            )
            if first_item.exists(timeout=5):
                result["name"] = first_item.get_text() or ""

                # 点击进入详情页获取 ID
                first_item.click()
                time.sleep(3)

                # 从当前 URL 获取 ID
                current_url = self.get_current_url()
                id_match = re.search(r"id=(\d+)", current_url)
                if id_match:
                    result["item_id"] = id_match.group(1)
                    result["url"] = current_url

                self._log(f"第一条结果：{result}")
                return result if result.get("name") or result.get("item_id") else None

        except Exception as e:
            self._log(f"Native 获取搜索结果失败：{e}")

        # 备用：通过 WebView 获取页面源码解析
        if self._wd:
            try:
                page_source = self._wd.page_source
                # 提取 item ID
                ids = re.findall(r'item\.htm\?id=(\d+)', page_source)
                names = re.findall(r'"itemName"\s*:\s*"([^"]+)"', page_source)

                if ids:
                    result["item_id"] = ids[0]
                    result["name"] = names[0] if names else ""
                    result["url"] = f"https://item.damai.cn/item.htm?id={ids[0]}"
                    self._log(f"通过页面源码提取结果：{result}")
                    return result

            except Exception as e:
                self._log(f"页面源码解析失败：{e}")

        self._warn("未能获取搜索结果")
        return None

    # ── 完整登录流程 ──────────────────────────────────────────────────
    def login(self, phone: str) -> bool:
        """执行完整的登录流程。

        1. 导航到登录页
        2. 输入手机号
        3. 点击发送验证码
        4. 提示用户输入验证码
        5. 输入验证码并点击登录
        6. 等待登录成功

        Args:
            phone: 手机号码

        Returns:
            是否登录成功
        """
        # Step 1: 导航到登录页
        if not self.navigate_to_login():
            print("❌ 无法到达登录页面")
            return False

        # Step 2: 输入手机号
        if not self.input_phone(phone):
            print("❌ 无法输入手机号")
            return False

        time.sleep(1)

        # Step 3: 点击发送验证码
        if not self.click_send_code():
            print("❌ 无法发送验证码")
            return False

        print("✅ 验证码已发送，请查收短信。")

        # Step 4: 提示用户输入验证码
        max_retries = 3
        for i in range(max_retries):
            code = input(
                f"\n🔑 请输入短信验证码（剩余 {max_retries - i} 次机会）: "
            ).strip()
            if not code:
                print("验证码不能为空，请重新输入。")
                continue

            # Step 5: 输入验证码并登录
            if not self.input_verify_code(code):
                print("❌ 无法输入验证码")
                continue

            time.sleep(1)

            if not self.click_login():
                print("❌ 无法点击登录按钮")
                continue

            # Step 6: 等待登录成功
            if self.wait_login_success():
                return True

            print("❌ 登录失败，验证码可能不正确。")

        print("❌ 验证码输入次数已用完，登录失败。")
        return False

    # ── 清理 ──────────────────────────────────────────────────────────
    def cleanup(self) -> None:
        """清理资源。"""
        try:
            self.switch_to_native()
        except Exception:
            pass
        self._log("已清理资源")
