import json
import re
from typing import Any, ClassVar
from urllib.parse import urlparse, urlunparse

from msgspec import Struct, convert, field

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Comment
from ..download import Downloader
from .base import BaseParser, ParseException, Platform, handle


def _clean_xhs_image_url(url: str) -> str:
    """清理小红书图片URL，去除水印和压缩参数，获取原图直链"""
    if not url:
        return url
    parsed = urlparse(url)
    # 去掉查询参数（imageView2/resize/watermark 等都在 query 里）
    clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return clean


def _parse_fans_count(fans: str) -> int | str | None:
    """解析粉丝数：纯数字转 int，'1.2万' 等原样返回字符串"""
    if not fans:
        return None
    fans = fans.strip()
    if fans.isdigit():
        return int(fans)
    # 非纯数字（如 "1.2万"）直接返回原字符串
    return fans if fans else None


class XHSParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="xhs", display_name="小红书")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.xhs
        self.cookies = self.mycfg.cookies
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                )
            }
        )
        self.ios_headers.update(
            {
                "origin": "https://www.xiaohongshu.com",
                "x-requested-with": "XMLHttpRequest",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="xiaohongshu.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str
            self.ios_headers["cookie"] = self.cookiejar.cookies_str

    # https://www.xiaohongshu.com/live/6837f50b000000001d02a1e8
    @handle(
        "xiaohongshu.com/live",
        r"live/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]+)?)\/?(?:\?.*)?$",
    )
    async def _parse_live(self, searched: re.Match[str]):
        """解析小红书直播/直播回放（复用 explore 解析流程）"""
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")
        try:
            return await self.parse_explore(f"{xhs_domain}/explore/{query}", xhs_id)
        except Exception as e:
            logger.warning(f"[XHS直播] parse_explore failed, error: {e}, fallback to parse_discovery")
            return await self.parse_discovery(f"{xhs_domain}/discovery/item/{query}", note_id=xhs_id)

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    # https://www.xiaohongshu.com/user/profile/5e5ae8680000000001008f45
    @handle("xiaohongshu.com/user/profile", r"xiaohongshu\.com/user/profile/(?P<user_id>[0-9a-fA-F]+)(?:\?.*)?$")
    async def _parse_xhs_user(self, searched: re.Match[str]):
        """解析小红书用户主页"""
        user_id = searched.group("user_id")
        return await self.parse_xhs_user(user_id)

    async def parse_xhs_user(self, user_id: str):
        """解析小红书用户信息"""
        url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        async with self.session.get(url, headers=self.headers) as resp:
            html = await resp.text()

        json_obj = self._extract_initial_state_json(html)

        # 提取用户信息
        user_data = json_obj.get("user", {}).get("userPageData", {})
        if not user_data:
            # 尝试其他路径
            user_data = json_obj.get("user", {}).get("info", {})
        if not user_data:
            raise ParseException("小红书用户信息为空")

        nickname = user_data.get("nickname", "") or user_data.get("nick_name", "")
        avatar = user_data.get("avatar", "") or user_data.get("imageb", "")
        desc = user_data.get("desc", "") or user_data.get("description", "")
        red_id = user_data.get("red_id", "") or user_data.get("redId", "")
        fans = user_data.get("fans", "") or user_data.get("fansCount", "")

        # 获取用户笔记
        contents = []
        notes_data = json_obj.get("user", {}).get("notes", [])
        if not notes_data:
            notes_data = json_obj.get("user", {}).get("feedList", [])
        for note in notes_data[:3]:
            note_title = note.get("title", "") or note.get("displayTitle", "")
            note_cover = note.get("cover", {}).get("url", "") or note.get("imageList", [{}])[0].get("url", "")
            if note_title:
                from ..data import TextContent
                contents.append(TextContent(note_title[:200]))
            if note_cover:
                contents.extend(self.create_image_contents([note_cover]))

        # 构建作者
        author = self.create_author(
            nickname, avatar,
            uid=user_id,
            description=desc or None,
            follower_count=_parse_fans_count(fans),
        )

        extra = {"user_id": user_id}
        if red_id:
            extra["handle"] = f"小红书号 {red_id}"

        return self.result(
            title=f"{nickname} 的主页",
            text=desc if desc else f"{nickname} 的小红书主页",
            author=author,
            contents=contents,
            url=url,
            extra=extra,
        )

    # https://www.xiaohongshu.com/discovery/item/68e8e3fa00000000030342ec?app_platform=android&ignoreEngage=true&app_version=9.6.0&share_from_user_hidden=true&xsec_source=app_share&type=normal&xsec_token=CBW9rwIV2qhcCD-JsQAOSHd2tTW9jXAtzqlgVXp6c52Sw%3D&author_share=1&xhsshare=QQ&shareRedId=ODs3RUk5ND42NzUyOTgwNjY3OTo8S0tK&apptime=1761372823&share_id=3b61945239ac403db86bea84a4f15124&share_channel=qq
    @handle(
        "xiaohongshu.com",
        r"(?:explore|discovery/item)/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]+)?)\/?(?:\?.*)?$",
    )
    async def _parse_common(self, searched: re.Match[str]):
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")

        try:
            return await self.parse_explore(f"{xhs_domain}/explore/{query}", xhs_id)
        except Exception as e:
            logger.warning(
                f"parse_explore failed, error: {e}, fallback to parse_discovery"
            )
            return await self.parse_discovery(f"{xhs_domain}/discovery/item/{query}", note_id=xhs_id)

    async def parse_explore(self, url: str, xhs_id: str):
        async with self.session.get(url, headers=self.headers) as resp:
            html = await resp.text()
            logger.debug(f"url: {resp.url} | status: {resp.status}")

        json_obj = self._extract_initial_state_json(html)

        # ["note"]["noteDetailMap"][xhs_id]["note"]
        note_id = xhs_id
        note_data = json_obj.get("note", {}).get("noteDetailMap", {}).get(xhs_id, {}).get("note", {})
        if not note_data:
            raise ParseException("can't find note detail in json_obj")

        class Image(Struct):
            urlDefault: str
            urlSizeLarge: str | None = None

        class User(Struct):
            nickname: str
            avatar: str
            user_id: str = ""
            red_id: str = ""
            desc: str = ""
            fans: str = ""

        class InteractInfo(Struct):
            likedCount: str = "0"
            collectedCount: str = "0"
            commentCount: str = "0"
            shareCount: str = "0"
            viewCount: str = "0"

        class NoteInfo(Struct):
            interactInfo: InteractInfo | None = None

        class NoteDetail(Struct):
            type: str
            title: str
            desc: str
            user: User
            imageList: list[Image] = field(default_factory=list)
            video: Video | None = None
            time: int = 0
            noteInfo: NoteInfo | None = None

            @property
            def nickname(self) -> str:
                return self.user.nickname

            @property
            def avatar_url(self) -> str:
                return self.user.avatar

            @property
            def image_urls(self) -> list[str]:
                return [_clean_xhs_image_url(item.urlSizeLarge or item.urlDefault) for item in self.imageList]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        note_detail = convert(note_data, type=NoteDetail)
        raw_user = note_data.get("user", {})
        logger.debug(f"[XHS] user fields: nickname={note_detail.nickname!r}, red_id={note_detail.user.red_id!r}, user_id={note_detail.user.user_id!r}, desc={note_detail.user.desc!r}, fans={note_detail.user.fans!r}")
        logger.debug(f"[XHS] raw user keys: {list(raw_user.keys()) if isinstance(raw_user, dict) else 'N/A'}")
        logger.debug(f"[XHS] noteInfo={note_data.get('noteInfo')!r}, interactInfo={note_data.get('interactInfo')!r}")

        # 回退: 如果 fans 为空，尝试其他字段名
        if not note_detail.user.fans and isinstance(raw_user, dict):
            for alt_key in ("fansCount", "fanscount", "fans_count", "fansCountStr"):
                alt_val = raw_user.get(alt_key)
                if alt_val:
                    note_detail.user.fans = str(alt_val)
                    break
        # 回退: 如果 desc 为空，尝试其他字段名
        if not note_detail.user.desc and isinstance(raw_user, dict):
            for alt_key in ("description", "userDesc", "user_desc"):
                alt_val = raw_user.get(alt_key)
                if alt_val:
                    note_detail.user.desc = str(alt_val)
                    break
        # 回退: 如果 red_id 为空，尝试其他字段名
        if not note_detail.user.red_id and isinstance(raw_user, dict):
            for alt_key in ("redId", "redid", "xhs_id"):
                alt_val = raw_user.get(alt_key)
                if alt_val:
                    note_detail.user.red_id = str(alt_val)
                    break

        contents = []
        # 添加视频内容
        if video_url := note_detail.video_url:
            # 使用第一张图片作为封面
            cover_url = note_detail.image_urls[0] if note_detail.image_urls else None
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        elif image_urls := note_detail.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        # 提取统计数据（interactInfo 可能在 noteInfo 内或 note 顶层）
        stats = {}
        interact = None
        if note_detail.noteInfo and note_detail.noteInfo.interactInfo:
            interact = note_detail.noteInfo.interactInfo
        elif note_data.get("interactInfo"):
            interact = convert(note_data["interactInfo"], InteractInfo)
        if interact:
            stats = {
                "likes": interact.likedCount,
                "favorites": interact.collectedCount,
                "comments": interact.commentCount,
                "reposts": interact.shareCount,
                "views": interact.viewCount,
            }

        # 尝试提取置顶评论和热评
        pinned_comment, hot_comment = None, None
        try:
            cmt_data = json_obj.get("note", {}).get("noteDetailMap", {}).get(xhs_id, {}).get("comment", {})
            cmts = cmt_data.get("comments", [])
            if isinstance(cmts, list):
                first_hot = None
                for c in cmts:
                    u = c.get("user_info", {})
                    cmt = Comment(
                        author_name=u.get("nickname", ""),
                        content=c.get("content", ""),
                        author_avatar=u.get("avatar"),
                        likes=c.get("like_count", 0),
                        timestamp=c.get("time", 0),
                        is_hot=True,
                    )
                    if c.get("pinned"):
                        cmt.is_pinned = True
                        pinned_comment = cmt
                    if first_hot is None:
                        first_hot = cmt
                # 热评：第一条非置顶的评论，若无则第一条
                if pinned_comment and first_hot and first_hot is not pinned_comment:
                    hot_comment = first_hot
                elif not pinned_comment:
                    pinned_comment = first_hot
        except Exception:
            pass

        # 如果评论或用户数据缺失，尝试 Playwright 渲染
        if not pinned_comment and not note_detail.user.fans:
            logger.debug("[小红书] HTML 数据不完整，尝试 Playwright 渲染")
            pw_json = await self._fetch_with_playwright(url)
            if pw_json:
                pw_note = pw_json.get("note", {}).get("noteDetailMap", {}).get(xhs_id, {}).get("note", {})
                if pw_note:
                    # 补充用户数据
                    pw_user = pw_note.get("user", {})
                    if pw_user and not note_detail.user.fans:
                        note_detail.user.fans = pw_user.get("fans", "") or pw_user.get("fansCount", "")
                        if not note_detail.user.desc:
                            note_detail.user.desc = pw_user.get("desc", "") or pw_user.get("description", "")
                        if not note_detail.user.red_id:
                            note_detail.user.red_id = pw_user.get("red_id", "") or pw_user.get("redId", "")
                    # 补充评论
                    pw_cmt = pw_json.get("note", {}).get("noteDetailMap", {}).get(xhs_id, {}).get("comment", {})
                    pw_cmts = pw_cmt.get("comments", [])
                    if pw_cmts and not pinned_comment:
                        first_hot = None
                        for c in pw_cmts:
                            u = c.get("user_info", {})
                            cmt = Comment(
                                author_name=u.get("nickname", ""),
                                content=c.get("content", ""),
                                author_avatar=u.get("avatar"),
                                likes=c.get("like_count", 0),
                                timestamp=c.get("time", 0),
                                is_hot=True,
                            )
                            if c.get("pinned"):
                                cmt.is_pinned = True
                                pinned_comment = cmt
                            if first_hot is None:
                                first_hot = cmt
                        if pinned_comment and first_hot and first_hot is not pinned_comment:
                            hot_comment = first_hot
                        elif not pinned_comment:
                            pinned_comment = first_hot

        # 构建作者
        author = self.create_author(
            note_detail.nickname,
            note_detail.avatar_url,
            uid=note_detail.user.user_id or None,
            description=note_detail.user.desc or None,
            follower_count=_parse_fans_count(note_detail.user.fans),
        )

        # 平台专属ID（小红书号）— 优先使用 red_id（用户自定义ID），回退到 user_id（内部ID）
        extra = {"note_id": note_id, "post_id": note_id}
        uid = note_detail.user.red_id or note_detail.user.user_id
        if uid:
            extra["handle"] = f"小红书号 {uid}"
        if note_detail.user.user_id:
            extra["uid"] = note_detail.user.user_id

        return self.result(
            title=note_detail.title,
            text=note_detail.desc,
            author=author,
            contents=contents,
            stats=stats,
            timestamp=note_detail.time // 1000 if note_detail.time else None,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            url=url,
            page_type="note",
        )

    async def parse_discovery(self, url: str, note_id: str = ""):
        async with self.session.get(
            url,
            headers=self.ios_headers,
            allow_redirects=True,
        ) as resp:
            html = await resp.text()

        json_obj = self._extract_initial_state_json(html)
        note_data = json_obj.get("noteData")
        if not note_data:
            raise ParseException("can't find noteData in json_obj")
        preload_data = note_data.get("normalNotePreloadData", {})
        note_data = note_data.get("data", {}).get("noteData", {})
        if not note_data:
            raise ParseException("can't find noteData in noteData.data")

        class Image(Struct):
            url: str
            urlSizeLarge: str | None = None

        class User(Struct):
            nickName: str
            avatar: str
            userId: str = ""
            redId: str = ""
            desc: str = ""
            fans: str = ""

        class InteractInfo(Struct):
            likedCount: str = "0"
            collectedCount: str = "0"
            commentCount: str = "0"
            shareCount: str = "0"
            viewCount: str = "0"

        class NoteData(Struct):
            type: str
            title: str
            desc: str
            user: User
            time: int
            lastUpdateTime: int
            imageList: list[Image] = []  # 有水印
            video: Video | None = None
            interactInfo: InteractInfo | None = None

            @property
            def image_urls(self) -> list[str]:
                return [_clean_xhs_image_url(item.urlSizeLarge or item.url) for item in self.imageList]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        class NormalNotePreloadData(Struct):
            title: str
            desc: str
            imagesList: list[Image] = []  # 无水印, 但只有一只，用于视频封面

            @property
            def image_urls(self) -> list[str]:
                return [_clean_xhs_image_url(item.urlSizeLarge or item.url) for item in self.imagesList]

        note_data = convert(note_data, type=NoteData)
        logger.debug(f"[XHS-discovery] user fields: nickName={note_data.user.nickName!r}, redId={note_data.user.redId!r}, userId={note_data.user.userId!r}, desc={note_data.user.desc!r}")

        contents = []
        if video_url := note_data.video_url:
            if preload_data:
                preload_data = convert(preload_data, type=NormalNotePreloadData)
                img_urls = preload_data.image_urls
            else:
                img_urls = note_data.image_urls
            contents.append(self.create_video_content(video_url, img_urls[0]))
        elif img_urls := note_data.image_urls:
            contents.extend(self.create_image_contents(img_urls))

        # 提取统计数据
        stats = {}
        if note_data.interactInfo:
            interact = note_data.interactInfo
            stats = {
                "likes": interact.likedCount,
                "favorites": interact.collectedCount,
                "comments": interact.commentCount,
                "reposts": interact.shareCount,
                "views": interact.viewCount,
            }

        # 尝试提取置顶评论/热评
        pinned_comment, hot_comment = None, None
        try:
            cmt_data = json_obj.get("noteData", {}).get("comment", {})
            cmts = cmt_data.get("comments", [])
            if isinstance(cmts, list):
                first_hot = None
                for c in cmts:
                    u = c.get("user_info", {})
                    cmt = Comment(
                        author_name=u.get("nickname", ""),
                        content=c.get("content", ""),
                        author_avatar=u.get("avatar"),
                        likes=c.get("like_count", 0),
                        timestamp=c.get("time", 0),
                        is_hot=True,
                    )
                    if c.get("pinned"):
                        cmt.is_pinned = True
                        pinned_comment = cmt
                    if first_hot is None:
                        first_hot = cmt
                if pinned_comment and first_hot and first_hot is not pinned_comment:
                    hot_comment = first_hot
                elif not pinned_comment:
                    pinned_comment = first_hot
        except Exception:
            pass

        # 平台专属ID（小红书号）— 优先使用 redId（用户自定义ID），回退到 userId（内部ID）
        extra = {"note_id": note_id, "post_id": note_id} if note_id else {}
        uid = note_data.user.redId or note_data.user.userId
        if uid:
            extra["handle"] = f"小红书号 {uid}"

        return self.result(
            title=note_data.title,
            author=self.create_author(
                note_data.user.nickName,
                note_data.user.avatar,
                uid=note_data.user.userId or None,
                description=note_data.user.desc or None,
                follower_count=_parse_fans_count(note_data.user.fans),
            ),
            contents=contents,
            text=note_data.desc,
            stats=stats,
            timestamp=note_data.time // 1000,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            url=url,
            page_type="note",
        )

    def _extract_initial_state_json(self, html: str) -> dict[str, Any]:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        json_str = matched.group(1).replace("undefined", "null")
        return json.loads(json_str)

    async def _fetch_with_playwright(self, url: str) -> dict[str, Any] | None:
        """用 Playwright 渲染页面，等待数据加载后提取 __INITIAL_STATE__"""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                )
                if self.cookies:
                    await context.add_cookies([
                        {"name": k.strip(), "value": v.strip(), "domain": ".xiaohongshu.com", "path": "/"}
                        for k, v in (c.split("=", 1) for c in self.cookies.split(";") if "=" in c)
                    ])
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=30000)
                # 等待评论区加载
                try:
                    await page.wait_for_selector(".note-comment, .comment-list, [class*='comment']", timeout=5000)
                except Exception:
                    pass
                html = await page.content()
                await browser.close()
            return self._extract_initial_state_json(html)
        except Exception as e:
            logger.debug(f"[小红书] Playwright 渲染失败: {e}")
            return None


class Stream(Struct):
    h264: list[dict[str, Any]] | None = None
    h265: list[dict[str, Any]] | None = None
    av1: list[dict[str, Any]] | None = None
    h266: list[dict[str, Any]] | None = None


class Media(Struct):
    stream: Stream


class Video(Struct):
    media: Media

    @property
    def video_url(self) -> str | None:
        stream = self.media.stream

        # h264 有水印，h265 无水印
        if stream.h265:
            return stream.h265[0]["masterUrl"]
        elif stream.h264:
            return stream.h264[0]["masterUrl"]
        elif stream.av1:
            return stream.av1[0]["masterUrl"]
        elif stream.h266:
            return stream.h266[0]["masterUrl"]
        return None
