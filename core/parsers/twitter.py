import json
import re
from datetime import datetime
from typing import Any, ClassVar

from twikit import Client
from twikit.tweet import Tweet

from astrbot.api import logger

from ..config import PluginConfig
from ..data import Comment, ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="Twitter / X")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self._client: Client | None = None

    async def _ensure_client(self) -> Client:
        """获取已认证的 twikit Client（延迟初始化 + Cookie 注入）"""
        if self._client is not None:
            return self._client
        client = Client("en-US")
        # 尝试从配置加载 Cookie 进行认证
        cookie_str = getattr(self.mycfg, "cookies", None)
        if cookie_str:
            try:
                cookies = json.loads(cookie_str)
                if isinstance(cookies, dict):
                    client.set_cookies(cookies)
                    logger.debug("[Twitter] 已从配置加载 Cookie 认证")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"[Twitter] Cookie 解析失败，将使用无认证模式: {e}")
        self._client = client
        return client

    async def _get_hot_reply(self, client: Client, tweet_id: str) -> tuple[Comment | None, Comment | None]:
        """获取推文的置顶回复和热评（失败返回 (None, None)）"""
        try:
            replies = await client.get_tweet_replies(tweet_id)
            if not replies:
                return None, None
            # 按点赞数排序，取前两条
            sorted_replies = sorted(replies, key=lambda r: getattr(r, "favorite_count", 0) or 0, reverse=True)

            def _build_comment(reply) -> Comment | None:
                user = getattr(reply, "user", None)
                author_name = ""
                author_avatar = ""
                if user:
                    author_name = getattr(user, "name", None) or getattr(user, "screen_name", "")
                    author_avatar = getattr(user, "profile_image_url", "") or ""
                content = getattr(reply, "full_text", None) or getattr(reply, "text", "") or ""
                if not content:
                    return None
                ts = None
                created_at = getattr(reply, "created_at", None)
                if isinstance(created_at, datetime):
                    ts = int(created_at.timestamp())
                elif isinstance(created_at, str):
                    try:
                        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                        ts = int(dt.timestamp())
                    except (ValueError, TypeError):
                        pass
                return Comment(
                    author_name=author_name,
                    content=content,
                    author_avatar=author_avatar,
                    likes=getattr(reply, "favorite_count", 0) or 0,
                    timestamp=ts,
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None
            for r in sorted_replies[:2]:
                built = _build_comment(r)
                if not built:
                    continue
                if not pinned_comment:
                    pinned_comment = built
                elif not hot_comment:
                    hot_comment = built
                    break

            if pinned_comment:
                logger.debug(f"Twitter 置顶评论获取成功: {pinned_comment.author_name}: {pinned_comment.content[:50]}")
            if hot_comment:
                logger.debug(f"Twitter 热评获取成功: {hot_comment.author_name}: {hot_comment.content[:50]}")
            return pinned_comment, hot_comment
        except Exception as e:
            logger.debug(f"Twitter 获取回复失败(tweet_id={tweet_id}): {e}")
            return None, None

    # Twitter/X 用户主页
    @handle("twitter.com/user", r"https?://(?:www\.)?twitter\.com/(?P<username>[0-9a-zA-Z_]{1,20})\/?(?:\?.*)?$")
    @handle("x.com/user", r"https?://x\.com/(?P<username>[0-9a-zA-Z_]{1,20})\/?(?:\?.*)?$")
    async def _parse_twitter_user(self, searched: re.Match[str]):
        """解析 Twitter/X 用户主页"""
        username = searched.group("username")
        return await self.parse_twitter_user(username)

    async def parse_twitter_user(self, username: str):
        """解析 Twitter/X 用户信息"""
        client = await self._ensure_client()
        try:
            user = await client.get_user_by_screen_name(username)
            if not user:
                raise ParseException(f"Twitter 用户 @{username} 不存在")

            name = getattr(user, "name", "")
            avatar = getattr(user, "profile_image_url_https", "") or getattr(user, "profile_image_url", "")
            description = getattr(user, "description", "")
            followers = getattr(user, "followers_count", 0)
            following = getattr(user, "friends_count", 0)
            statuses = getattr(user, "statuses_count", 0)
            user_id = getattr(user, "id", "")

            # 替换 _normal 为 _400x400 获取大头像
            if avatar and "_normal" in avatar:
                avatar = avatar.replace("_normal", "_400x400")

            author = self.create_author(
                name, avatar,
                uid=str(user_id) if user_id else None,
                description=description or None,
                follower_count=followers or None,
            )

            stats = {}
            if statuses:
                stats["posts"] = statuses
            if following:
                stats["following"] = following

            return self.result(
                title=f"@{username} - Twitter",
                text=description if description else f"@{username} 的 Twitter 主页",
                author=author,
                contents=[],
                url=f"https://x.com/{username}",
                stats=stats or None,
                extra={
                    "handle": f"@{username}",
                    "uid": str(user_id) if user_id else "",
                },
            )
        except Exception as e:
            raise ParseException(f"Twitter 用户解析失败: {e}")

    @handle("twitter.com", r"https?://(?:www\.)?twitter\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)\/?(?:\?.*)?$")
    @handle("x.com", r"https?://x\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)\/?(?:\?.*)?$")
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        url = searched.group(0)
        tweet_id = searched.group("tweet_id")

        client = await self._ensure_client()
        try:
            tweet = await client.get_tweet_by_id(tweet_id)
            result = self._tweet_to_result(tweet, url, tweet_id=tweet_id)
            # 获取置顶评论和热评
            pinned, hot = await self._get_hot_reply(client, tweet_id)
            if pinned:
                result.pinned_comment = pinned
            if hot:
                result.hot_comment = hot
            return result
        except Exception as e:
            logger.warning(f"twikit 获取推文失败(tweet_id={tweet_id}): {e}")
            # 回退: 尝试从 syndication API 获取
            result = await self._fallback_syndication(tweet_id, url)
            # 回退路径也尝试获取热门回复（twikit 可能部分可用）
            try:
                pinned, hot = await self._get_hot_reply(client, tweet_id)
                if pinned:
                    result.pinned_comment = pinned
                if hot:
                    result.hot_comment = hot
            except Exception:
                pass
            return result

    async def _fallback_syndication(self, tweet_id: str, url: str) -> ParseResult:
        """twikit 失败时的回退：依次尝试 fxtwitter → oEmbed → syndication API"""
        from bs4 import BeautifulSoup

        # 回退 1: fxtwitter API（可靠的第三方推文代理，返回完整媒体）
        try:
            fxtwitter_url = f"https://api.fxtwitter.com/2/status/{tweet_id}"
            async with self.session.get(fxtwitter_url, proxy=self.proxy) as resp:
                if resp.status != 200:
                    logger.debug(f"Twitter fxtwitter 返回 {resp.status}(tweet_id={tweet_id})")
                    raise Exception(f"fxtwitter HTTP {resp.status}")
                data = await resp.json()
                tweet_data = data.get("status", data)
                text = tweet_data.get("text", "")
                author_data = tweet_data.get("author", {})
                author_name = author_data.get("name", "")
                screen_name = author_data.get("screen_name", "")
                avatar = author_data.get("avatar_url", "")

                author = self.create_author(
                    author_name, avatar=avatar,
                    uid=str(author_data.get("id", "")) if author_data.get("id") else None,
                    description=author_data.get("description", ""),
                ) if author_name else None

                # 提取媒体
                image_urls = []
                video_url = None
                cover_url = None

                media = tweet_data.get("media") or {}
                if isinstance(media, dict):
                    photos = media.get("photos", []) or media.get("photo", [])
                    for photo in photos:
                        if isinstance(photo, dict):
                            img_url = photo.get("url") or photo.get("direct_url", "")
                            if img_url:
                                image_urls.append(img_url)
                        elif isinstance(photo, str):
                            image_urls.append(photo)
                    videos = media.get("videos", []) or media.get("video", [])
                    if isinstance(videos, list) and videos:
                        v = videos[0]
                        if isinstance(v, dict):
                            video_url = v.get("url") or v.get("direct_url", "")
                            cover_url = v.get("thumbnail_url", "")
                    elif isinstance(videos, dict):
                        video_url = videos.get("url") or videos.get("direct_url", "")
                        cover_url = videos.get("thumbnail_url", "")
                elif isinstance(media, list):
                    for m in media:
                        if isinstance(m, dict):
                            m_type = m.get("type", "")
                            if m_type == "photo":
                                img_url = m.get("url") or m.get("direct_url", "")
                                if img_url:
                                    image_urls.append(img_url)
                            elif m_type in ("video", "gif"):
                                video_url = m.get("url") or m.get("direct_url", "")
                                cover_url = m.get("thumbnail_url", "")

                contents = []
                if image_urls:
                    contents.extend(self.create_image_contents(image_urls))
                if video_url:
                    contents.append(self.create_video_content(video_url, cover_url, 0))

                # 时间戳
                timestamp = None
                ts_str = tweet_data.get("created_at", "")
                if ts_str:
                    try:
                        from datetime import timezone
                        dt = datetime.strptime(ts_str, "%a %b %d %H:%M:%S %z %Y")
                        timestamp = int(dt.timestamp())
                    except (ValueError, TypeError):
                        pass

                # 统计数据
                stats = {}
                for attr, key in (
                    ("likes", "likes"), ("retweets", "reposts"),
                    ("replies", "comments"), ("views", "views"),
                    ("quotes", "quotes"), ("bookmarks", "bookmarks"),
                ):
                    val = tweet_data.get(attr)
                    if val is not None:
                        stats[key] = int(val)

                extra = {"tweet_id": tweet_id, "post_id": tweet_id, "handle": f"@{screen_name}"} if screen_name else {"tweet_id": tweet_id, "post_id": tweet_id}
                # fxtwitter 可能返回 follower_count
                followers = author_data.get("followers_count") or author_data.get("followers")
                if followers and author:
                    author.follower_count = int(followers) if isinstance(followers, (int, float)) or (isinstance(followers, str) and followers.isdigit()) else followers

                # 处理引用推文
                repost = None
                quote_data = tweet_data.get("quote")
                if isinstance(quote_data, dict) and quote_data.get("text"):
                    q_author = quote_data.get("author", {})
                    q_text = quote_data.get("text", "")
                    q_author_obj = self.create_author(
                        q_author.get("name", ""), avatar=q_author.get("avatar_url"),
                        uid=str(q_author.get("id", "")) if q_author.get("id") else None,
                    ) if q_author.get("name") else None
                    q_contents: list[Any] = []
                    q_media = quote_data.get("media") or {}
                    if isinstance(q_media, dict):
                        for photo in q_media.get("photos", []) or []:
                            if isinstance(photo, dict) and photo.get("url"):
                                q_contents.extend(self.create_image_contents([photo["url"]]))
                    repost = self.result(title=q_text[:100], text=q_text, author=q_author_obj, contents=q_contents)

                logger.info(f"Twitter fxtwitter 回退成功(tweet_id={tweet_id})")
                return self.result(
                    title=text[:100] if text else None,
                    text=text,
                    author=author,
                    url=url,
                    contents=contents,
                    timestamp=timestamp,
                    stats=stats or None,
                    extra=extra,
                    repost=repost,
                )
        except Exception as e:
            logger.debug(f"Twitter fxtwitter 回退失败(tweet_id={tweet_id}): {e}")

        # 回退 2: oEmbed API（只返回文本，无媒体）
        try:
            oembed_url = f"https://publish.twitter.com/oembed?url={url}&omit_script=true"
            async with self.session.get(oembed_url, headers=self.headers, proxy=self.proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    html = data.get("html", "")
                    author_name = data.get("author_name", "")
                    author_url = data.get("author_url", "")

                    if html:
                        soup = BeautifulSoup(html, "html.parser")
                        text = soup.get_text("\n", strip=True)

                        uid_match = re.search(r"/(\d+)$", author_url) if author_url else None
                        uid = uid_match.group(1) if uid_match else None
                        handle_match = re.search(r"twitter\.com/([a-zA-Z0-9_]+)", author_url) if author_url else None
                        handle = f"@{handle_match.group(1)}" if handle_match else ""

                        author = self.create_author(author_name, uid=uid) if author_name else None

                        extra = {"tweet_id": tweet_id, "post_id": tweet_id, "handle": handle} if handle else {"tweet_id": tweet_id, "post_id": tweet_id}
                        logger.info(f"Twitter oEmbed 回退成功(tweet_id={tweet_id})")
                        return self.result(
                            title=text[:100] if text else None,
                            text=text,
                            author=author,
                            url=url,
                            extra=extra,
                        )
        except Exception as e:
            logger.debug(f"Twitter oEmbed 回退失败(tweet_id={tweet_id}): {e}")

        # 回退 3: syndication API
        try:
            syndication_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en"
            async with self.session.get(syndication_url, headers=self.headers, proxy=self.proxy) as resp:
                if resp.status == 200:
                    tweet_data = await resp.json()
                    text = tweet_data.get("text", "")
                    author_name = tweet_data.get("user", {}).get("name", "")
                    screen_name = tweet_data.get("user", {}).get("screen_name", "")
                    avatar = tweet_data.get("user", {}).get("profile_image_url_https", "")

                    author = self.create_author(author_name, avatar=avatar, uid=str(tweet_data.get("user", {}).get("id", "")), description=tweet_data.get("user", {}).get("description", "")) if author_name else None

                    image_urls = []
                    photos = tweet_data.get("photos", [])
                    for photo in photos:
                        img_url = photo.get("url", "")
                        if img_url:
                            image_urls.append(img_url)

                    contents = []
                    if image_urls:
                        contents.extend(self.create_image_contents(image_urls))

                    video = tweet_data.get("video", {})
                    if video:
                        video_url = video.get("url", "")
                        if video_url:
                            contents.append(self.create_video_content(video_url, video.get("thumbnail", {}).get("url", ""), 0))

                    extra = {"tweet_id": tweet_id, "post_id": tweet_id, "handle": f"@{screen_name}"} if screen_name else {"tweet_id": tweet_id, "post_id": tweet_id}
                    logger.info(f"Twitter syndication 回退成功(tweet_id={tweet_id})")
                    return self.result(
                        title=text[:100] if text else None,
                        text=text,
                        author=author,
                        url=url,
                        contents=contents,
                        extra=extra,
                    )
        except Exception as e:
            logger.debug(f"Twitter syndication 回退失败(tweet_id={tweet_id}): {e}")

        logger.error(f"Twitter 所有回退均失败(tweet_id={tweet_id})")
        return self.result(
            title="[解析失败]",
            url=url,
        )

    def _tweet_to_result(self, tweet: Tweet, url: str, tweet_id: str = "") -> ParseResult:
        """将 twikit Tweet 对象转换为 ParseResult"""
        user = getattr(tweet, "user", None)

        # 作者
        screen_name = ""
        display_name = ""
        if user:
            display_name = getattr(user, "name", "") or ""
            screen_name = getattr(user, "screen_name", None) or ""
            author = self.create_author(
                display_name,
                getattr(user, "profile_image_url", None),
                uid=str(user.id) if getattr(user, "id", None) else None,
                description=getattr(user, "description", None),
                follower_count=getattr(user, "followers_count", None),
            )
        else:
            author = None

        # 推文文本（优先使用 full_text，回退到 text）
        text = getattr(tweet, 'full_text', None) or getattr(tweet, 'text', None) or ""

        # 平台专属ID（@handle）+ tweet_id
        extra = {}
        if tweet_id:
            extra["tweet_id"] = tweet_id
            extra["post_id"] = tweet_id
        if screen_name:
            extra["handle"] = f"@{screen_name}"
        if user and getattr(user, "id", None):
            extra["uid"] = str(user.id)

        # 媒体内容
        contents: list[Any] = []
        media = getattr(tweet, 'media', None) or []
        for m in media:
            m_type = m.get("type") if isinstance(m, dict) else getattr(m, "type", "")
            if m_type == "photo":
                m_url = m.get("media_url_https", "") if isinstance(m, dict) else getattr(m, "media_url_https", "") or getattr(m, "media_url", "")
                if m_url:
                    contents.extend(self.create_image_contents([m_url]))
            elif m_type in ("video", "animated_gif"):
                # 视频 URL 需要从 video_info.variants 提取最高码率 MP4
                video_url = self._extract_video_url(m)
                cover = m.get("cover_url") if isinstance(m, dict) else getattr(m, "cover_url", None) or getattr(m, "thumbnail_url", None)
                if video_url:
                    contents.append(self.create_video_content(video_url, cover, 0))

        # 发布时间
        created_at = getattr(tweet, 'created_at', None)
        timestamp = None
        if isinstance(created_at, datetime):
            timestamp = int(created_at.timestamp())
        elif isinstance(created_at, str):
            try:
                dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                timestamp = int(dt.timestamp())
            except (ValueError, TypeError):
                pass

        # 统计数据
        stats = {}
        for attr, key in (
            ("favorite_count", "likes"),
            ("retweet_count", "reposts"),
            ("reply_count", "comments"),
            ("view_count", "views"),
            ("quote_count", "quotes"),
            ("bookmark_count", "bookmarks"),
        ):
            val = getattr(tweet, attr, None)
            if val is not None:
                stats[key] = int(val)

        # 被转发/引用的内容
        repost = None
        retweeted = getattr(tweet, "retweeted_tweet", None)
        if retweeted:
            repost = self._tweet_to_result(retweeted, url)
        else:
            quoted = getattr(tweet, "quote", None)
            if quoted:
                repost = self._tweet_to_result(quoted, url)

        return self.result(
            title=text[:100] if text else None,
            text=text,
            author=author,
            url=url,
            contents=contents,
            timestamp=timestamp,
            stats=stats or None,
            extra=extra,
            repost=repost,
            page_type="tweet",
        )

    @staticmethod
    def _extract_video_url(media) -> str | None:
        """从 Twitter media 对象中提取最高码率 MP4 视频 URL"""
        # 获取 video_info
        video_info = None
        if isinstance(media, dict):
            video_info = media.get("video_info")
        else:
            video_info = getattr(media, "video_info", None)
        if not video_info:
            return None

        # 从 variants 中提取 MP4 URL（取最高码率）
        variants = []
        if isinstance(video_info, dict):
            variants = video_info.get("variants", [])
        else:
            variants = getattr(video_info, "variants", []) or []

        best_url = None
        best_bitrate = -1
        for v in variants:
            if isinstance(v, dict):
                content_type = v.get("content_type", "")
                url = v.get("url", "")
                bitrate = v.get("bitrate", 0)
            else:
                content_type = getattr(v, "content_type", "")
                url = getattr(v, "url", "")
                bitrate = getattr(v, "bitrate", 0)
            if content_type == "video/mp4" and url and bitrate >= best_bitrate:
                best_url = url
                best_bitrate = bitrate

        return best_url
