"""
统一登录系统。

策略：
1. 优先尝试二维码扫码登录（平台支持时）
2. 二维码不可用则返回引导链接，指导用户手动获取 Cookie
"""

from abc import ABC, abstractmethod
from typing import Optional

from astrbot.api import logger


class LoginResult:
    """登录结果"""
    success: bool
    message: str
    credential: Optional[dict] = None
    qrcode_path: Optional[str] = None  # 二维码图片路径
    guide_url: Optional[str] = None    # 引导链接

    def __init__(self, success: bool, message: str, **kwargs):
        self.success = success
        self.message = message
        for k, v in kwargs.items():
            setattr(self, k, v)


class BaseLoginManager(ABC):
    """登录管理器基类"""

    platform: str = ""

    @abstractmethod
    async def login_with_qrcode(self) -> LoginResult:
        """尝试二维码登录"""
        ...

    @abstractmethod
    async def get_login_guide(self) -> LoginResult:
        """返回 Cookie 获取引导"""
        ...

    async def try_login(self) -> LoginResult:
        """统一登录入口：QR优先 → Cookie引导"""
        try:
            result = await self.login_with_qrcode()
            if result.success or result.qrcode_path:
                return result
        except Exception as e:
            logger.warning(f"[{self.platform}] QR登录失败: {e}")

        return await self.get_login_guide()


class BiliLoginManager(BaseLoginManager):
    """B站登录管理器"""

    platform = "bilibili"

    def __init__(self, bili_client, data_manager):
        self.client = bili_client
        self.dm = data_manager

    async def login_with_qrcode(self) -> LoginResult:
        """B站 QR 扫码登录"""
        from bilibili_api import login_v2
        import os
        import tempfile

        login_obj = login_v2.QrCodeLogin()
        await login_obj.generate_qrcode()
        qr_path = os.path.join(tempfile.gettempdir(), "bilibili_qrcode.png")
        # login_v2 saves QR to disk internally

        return LoginResult(
            success=False,
            message="请使用 Bilibili App 扫描二维码登录",
            qrcode_path=qr_path,
            _login_obj=login_obj,
        )

    async def check_qr_state(self, login_obj) -> Optional[LoginResult]:
        """轮询二维码状态"""
        from bilibili_api import login_v2

        try:
            while True:
                state = await login_obj.check_state()
                if state == login_v2.QrCodeLoginEvents.DONE:
                    credential = login_obj.get_credential()
                    self.client.credential = credential
                    cred_dict = self.client.get_credential_dict()
                    if cred_dict:
                        await self.dm.set_credential(cred_dict)
                        return LoginResult(True, "✅ B站登录成功！")
                    return LoginResult(False, "❌ 登录失败：无法获取凭据。")

                elif state == login_v2.QrCodeLoginEvents.TIMEOUT:
                    return LoginResult(False, "❌ 登录超时，请重新执行登录。")

                import asyncio
                await asyncio.sleep(2)
        except Exception as e:
            return LoginResult(False, f"❌ 登录失败: {e}")

    async def get_login_guide(self) -> LoginResult:
        """B站 Cookie 获取引导"""
        return LoginResult(
            success=False,
            message="B站二维码登录不可用，请手动获取 Cookie",
            guide_url="https://github.com/Soulter/astrbot_plugin_bilibili#%E7%99%BB%E5%BD%95",
        )


class CookieLoginManager(BaseLoginManager):
    """通用 Cookie 登录管理器（面向不支持 QR 的平台）"""

    platform = ""

    def __init__(self, platform: str, cookie_jar, guide_url: str = ""):
        self.platform = platform
        self.cookie_jar = cookie_jar
        self._guide_url = guide_url

    async def login_with_qrcode(self) -> LoginResult:
        return LoginResult(False, f"{self.platform} 不支持二维码登录")

    async def get_login_guide(self) -> LoginResult:
        urls = {
            "pixiv": "https://www.pixiv.net/",
            "twitter": "https://twitter.com/",
            "youtube": "https://www.youtube.com/",
            "instagram": "https://www.instagram.com/",
        }
        url = self._guide_url or urls.get(self.platform, "")
        guide = (
            f"🔑 {self.platform} 登录引导\n"
            f"1. 在浏览器中打开 {url or '对应平台网站'}\n"
            f"2. 登录你的账号\n"
            f"3. 按 F12 打开开发者工具 → Application → Cookies\n"
            f"4. 复制 Cookie 字符串\n"
            f"5. 在插件配置中填入 {self.platform}_cookies"
        )
        return LoginResult(success=False, message=guide, guide_url=url)
