import re
from random import choice
from typing import ClassVar, TypeAlias

import msgspec
from msgspec import Struct, field

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Comment, Platform
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class KuaiShouParser(BaseParser):
    """快手解析器"""

    # 平台信息
    platform: ClassVar[Platform] = Platform(name="kuaishou", display_name="快手")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.kuaishou
        self.ios_headers.update({"Referer": "https://v.kuaishou.com/"})
        self.cookiejar = CookieJar(config, self.mycfg, domain="kuaishou.com")
        if self.cookiejar.cookies_str:
            self.ios_headers["cookie"] = self.cookiejar.cookies_str

    async def _get_comments(self, photo_id: str) -> tuple[Comment | None, Comment | None]:
        """获取置顶评论和热评（失败返回 (None, None)）"""
        try:
            url = "https://www.kuaishou.com/graphql"
            payload = {
                "operationName": "commentListQuery",
                "variables": {
                    "photoId": photo_id,
                    "pcursor": "",
                },
                "query": "query commentListQuery($photoId: String, $pcursor: String) {"
                         " shortVideoCommentList(photoId: $photoId, pcursor: $pcursor) {"
                         "   commentList {"
                         "     commentId"
                         "     authorId"
                         "     authorName"
                         "     content"
                         "     headUrl"
                         "     likeCount"
                         "   }"
                         " }"
                         "}",
            }
            async with self.session.post(url, json=payload, headers=self.ios_headers) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()

            comments = data.get("data", {}).get("shortVideoCommentList", {}).get("commentList", [])
            if not comments:
                return None, None

            def _build_comment(c: dict) -> Comment | None:
                content = c.get("content", "")
                if not content:
                    return None
                return Comment(
                    author_name=c.get("authorName", ""),
                    content=content,
                    author_avatar=c.get("headUrl", ""),
                    likes=c.get("likeCount", 0),
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None
            for c in comments[:5]:
                built = _build_comment(c)
                if not built:
                    continue
                if not pinned_comment:
                    pinned_comment = built
                elif not hot_comment:
                    hot_comment = built
                    break

            return pinned_comment, hot_comment
        except Exception:
            return None, None

    # https://v.kuaishou.com/2yAnzeZ
    @handle("v.kuaishou", r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    # https://www.kuaishou.com/short-video/3xhjgcmir24m4nm
    @handle("kuaishou", r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    # https://v.m.chenzhongtech.com/fw/photo/3xburnkmj3auazc
    @handle("chenzhongtech", r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+")
    async def _parse_v_kuaishou(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        real_url = await self.get_redirect_url(url, headers=self.ios_headers)

        if len(real_url) <= 0:
            raise ParseException("failed to get location url from url")

        # /fw/long-video/ 返回结果不一样, 统一替换为 /fw/photo/ 请求
        real_url = real_url.replace("/fw/long-video/", "/fw/photo/")

        async with self.session.get(real_url, headers=self.ios_headers) as resp:
            if resp.status >= 400:
                raise ParseException(f"获取页面失败 {resp.status}")
            response_text = await resp.text()

        pattern = r"window\.INIT_STATE\s*=\s*(.*?)</script>"
        matched = re.search(pattern, response_text)

        if not matched:
            raise ParseException("failed to parse video JSON info from HTML")

        json_str = matched.group(1).strip()
        init_state = msgspec.json.decode(json_str, type=KuaishouInitState)
        photo = next(
            (d.photo for d in init_state.values() if d.photo is not None), None
        )
        if photo is None:
            raise ParseException("window.init_state don't contains videos or pics")

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url := photo.video_url:
            contents.append(
                self.create_video_content(
                    video_url, photo.cover_url, photo.duration, headers=self.ios_headers
                )
            )

        # 添加图片内容
        if img_urls := photo.img_urls:
            contents.extend(
                self.create_image_contents(img_urls, headers=self.ios_headers)
            )

        # 构建作者
        follower_count = None
        if photo.fan_count:
            follower_count = photo.fan_count if isinstance(photo.fan_count, int) else photo.fan_count.strip() or None

        author = self.create_author(
            photo.name,
            photo.head_url,
            uid=photo.user_id or None,
            description=photo.user_text or None,
            follower_count=follower_count,
        )

        # 统计数据
        stats = {}
        if photo.view_count is not None:
            stats["views"] = photo.view_count
        if photo.like_count is not None:
            stats["likes"] = photo.like_count
        if photo.comment_count is not None:
            stats["comments"] = photo.comment_count
        if photo.favorite_count is not None:
            stats["favorites"] = photo.favorite_count
        if photo.share_count is not None:
            stats["reposts"] = photo.share_count

        # 额外信息
        extra = {
            "uid": str(photo.user_id or ""),
            "handle": f"ks:{photo.user_id}" if photo.user_id else "",
        }
        if photo.photo_id:
            extra["video_id"] = photo.photo_id

        # 获取评论
        pinned_comment, hot_comment = await self._get_comments(photo.photo_id) if photo.photo_id else (None, None)

        return self.result(
            title=photo.caption,
            text=photo.caption,
            url=url,
            author=author,
            contents=contents,
            timestamp=photo.timestamp // 1000,
            stats=stats,
            extra=extra,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
        )


class CdnUrl(Struct):
    cdn: str
    url: str | None = None


class Atlas(Struct):
    music_cdn_list: list[CdnUrl] = field(name="musicCdnList", default_factory=list)
    cdn_list: list[CdnUrl] = field(name="cdnList", default_factory=list)
    size: list[dict] = field(name="size", default_factory=list)
    img_route_list: list[str] = field(name="list", default_factory=list)

    @property
    def img_urls(self):
        if len(self.cdn_list) == 0 or len(self.img_route_list) == 0:
            return []
        cdn = choice(self.cdn_list).cdn
        return [f"https://{cdn}/{url}" for url in self.img_route_list]


class ExtParams(Struct):
    atlas: Atlas = field(default_factory=Atlas)


class Photo(Struct):
    # 标题
    caption: str
    timestamp: int
    duration: int = 0
    user_name: str = field(default="未知用户", name="userName")
    user_id: str = field(default="", name="userId")
    head_url: str | None = field(default=None, name="headUrl")
    user_text: str = field(default="", name="userText")
    """用户签名/简介"""
    cover_urls: list[CdnUrl] = field(name="coverUrls", default_factory=list)
    main_mv_urls: list[CdnUrl] = field(name="mainMvUrls", default_factory=list)
    ext_params: ExtParams = field(name="ext_params", default_factory=ExtParams)
    view_count: int | None = field(default=None, name="viewCount")
    like_count: int | None = field(default=None, name="likeCount")
    comment_count: int | None = field(default=None, name="commentCount")
    favorite_count: int | None = field(default=None, name="favoriteCount")
    share_count: int | None = field(default=None, name="shareCount")
    photo_id: str = field(default="", name="photoId")
    fan_count: str | int | None = field(default=None, name="fanCount")

    @property
    def name(self) -> str:
        return self.user_name.replace("\u3164", "").strip()

    @property
    def cover_url(self):
        return choice(self.cover_urls).url if len(self.cover_urls) != 0 else None

    @property
    def video_url(self):
        return choice(self.main_mv_urls).url if len(self.main_mv_urls) != 0 else None

    @property
    def img_urls(self):
        return self.ext_params.atlas.img_urls


class TusjohData(Struct):
    result: int
    photo: Photo | None = None


KuaishouInitState: TypeAlias = dict[str, TusjohData]
