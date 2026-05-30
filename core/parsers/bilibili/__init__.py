import asyncio
from re import Match
from typing import ClassVar

from bilibili_api import request_settings, select_client
from bilibili_api.opus import Opus
from bilibili_api.video import Video, VideoCodecs, VideoQuality
from msgspec import convert

from astrbot.api import logger

from ...config import PluginConfig
from ...data import Comment, ImageContent, MediaContent, Platform, SendGroup
from ...exception import DownloadException, DurationLimitException
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    handle,
)
from .login import BilibiliLogin

# 选择客户端
select_client("curl_cffi")
# 模拟浏览器，第二参数数值参考 curl_cffi 文档
# https://curl-cffi.readthedocs.io/en/latest/impersonate.html
request_settings.set("impersonate", "chrome146")


class BilibiliParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="哔哩哔哩")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.bilibili
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )

        self.video_quality = getattr(
            VideoQuality, str(self.mycfg.video_quality).upper(), VideoQuality._720P
        )
        self.video_codecs = getattr(
            VideoCodecs, str(self.mycfg.video_codecs).upper(), VideoCodecs.AVC
        )

        self.login = BilibiliLogin(config)

    async def _get_follower_count(self, mid: int) -> int | None:
        """获取用户粉丝数（失败返回 None，不影响主流程）"""
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(mid, credential=self.login._credential)
            info = await u.get_relation_info()
            return info.get("follower", 0)
        except Exception as e:
            logger.debug(f"[B站] 获取粉丝数失败(mid={mid}): {e}")
            return None

    async def _get_user_sign(self, mid: int) -> str | None:
        """获取用户签名/简介（失败返回 None，不影响主流程）"""
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(mid, credential=self.login._credential)
            info = await u.get_user_info()
            return info.get("sign") or None
        except Exception as e:
            logger.debug(f"[B站] 获取用户签名失败(mid={mid}): {e}")
            return None

    async def _get_user_extra_info(self, mid: int) -> dict:
        """获取用户扩展信息（粉丝数、签名、认证、关注数、获赞数）

        Returns:
            dict: {
                "follower_count": int | None,
                "sign": str | None,
                "official_title": str,
                "official_role": int,
                "following": int,
                "like_num": int,
            }
        """
        result = {
            "follower_count": None,
            "sign": None,
            "official_title": "",
            "official_role": 0,
            "following": 0,
            "like_num": 0,
            "total_views": 0,
        }
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(mid, credential=self.login._credential)
            # 并发获取用户信息和关系信息
            user_info, relation_info = await asyncio.gather(
                u.get_user_info(),
                u.get_relation_info(),
                return_exceptions=True,
            )
            if not isinstance(user_info, Exception):
                result["sign"] = user_info.get("sign") or None
                official = user_info.get("official", {})
                result["official_title"] = official.get("title", "")
                result["official_role"] = official.get("role", 0)
            if not isinstance(relation_info, Exception):
                result["follower_count"] = relation_info.get("follower", 0)
                result["following"] = relation_info.get("following", 0)
            # 获取获赞数和总播放量
            try:
                up_stat = await u.get_up_stat()
                result["like_num"] = up_stat.get("likes", 0)
                result["total_views"] = up_stat.get("archive", {}).get("view", 0) if isinstance(up_stat.get("archive"), dict) else 0
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[B站] 获取用户扩展信息失败(mid={mid}): {e}")
        return result

    async def _get_pinned_comment(self, avid: int, type_: "CommentResourceType | None" = None) -> tuple[Comment | None, Comment | None]:
        """获取置顶评论和热评，返回 (pinned, hot)。失败返回 (None, None)"""
        try:
            from bilibili_api import comment as bili_comment
            from bilibili_api.comment import CommentResourceType, OrderType
            if type_ is None:
                type_ = CommentResourceType.VIDEO
            resp = await bili_comment.get_comments(
                oid=avid,
                type_=type_,
                page_index=1,
                order=OrderType.LIKE,
                credential=self.login._credential,
            )
            replies = resp.get("replies") or []
            if not replies:
                return None, None
            pinned = None
            hot = None
            for r in replies:
                member = r.get("member", {})
                content_data = r.get("content", {})
                cmt = Comment(
                    author_name=member.get("uname", ""),
                    content=content_data.get("message", ""),
                    author_avatar=member.get("avatar", ""),
                    likes=r.get("like", 0),
                    is_pinned=bool(r.get("top", False)),
                    is_hot=True,
                )
                if hot is None:
                    hot = cmt
                if cmt.is_pinned:
                    pinned = cmt
            return pinned, hot
        except Exception as e:
            logger.debug(f"[B站] 获取置顶评论失败(avid={avid}): {e}")
            return None, None

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})\/?(?:\?p=(?P<page_num>\d{1,3}))?(?:\?.*)?$",
    )
    @handle(
        "m.bili/BV",
        r"m\.bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})\/?(?:\?p=(?P<page_num>\d{1,3}))?(?:\?.*)?$",
    )
    async def _parse_bv(self, searched: Match[str]):
        """解析视频信息"""
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("bm", r"^bm(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s(?P<page_num>\d{1,3}))?$")
    async def _parse_bv_bm(self, searched: Match[str]):
        bvid = searched.group("bvid")
        page = int(searched.group("page_num") or 1)
        _, a_url = await self.extract_download_urls(bvid=bvid, page_index=page - 1)
        if not a_url:
            raise ParseException("未找到音频链接")
        audio = self.create_audio_content(a_url)
        return self.result(
            title=f"BiliBili_audio_{bvid}",
            contents=[audio],
            url=a_url,
        )

    @handle("/list/", r"bilibili\.com/list/ml\d+\?.*bvid=(?P<bvid>BV[0-9a-zA-Z]{10})")
    async def _parse_list_with_bvid(self, searched: Match[str]):
        """解析合集链接（提取 bvid 参数作为视频解析）"""
        bvid = str(searched.group("bvid"))
        return await self.parse_video(bvid=bvid)

    @handle("/list/oid", r"bilibili\.com/list/ml\d+\?.*oid=(?P<avid>\d+)")
    async def _parse_list_with_oid(self, searched: Match[str]):
        """解析合集链接（提取 oid 参数作为 avid 解析）"""
        avid = int(searched.group("avid"))
        return await self.parse_video(avid=avid)

    @handle("/festival/", r"bilibili\.com/festival/[^?]+\?.*bvid=(?P<bvid>BV[0-9a-zA-Z]{10})")
    async def _parse_festival(self, searched: Match[str]):
        """解析活动页面链接（提取 bvid 参数作为视频解析）"""
        bvid = str(searched.group("bvid"))
        return await self.parse_video(bvid=bvid)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})\/?(?:\?p=(?P<page_num>\d{1,3}))?(?:\?.*)?$",
    )
    async def _parse_av(self, searched: Match[str]):
        """解析视频信息"""
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)\/?(?:\?.*)?$")
    @handle("m.bilibili", r"m\.bilibili\.com/dynamic/(?P<dynamic_id>\d+)\/?(?:\?.*)?$")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)\/?(?:\?.*)?$")
    async def _parse_dynamic(self, searched: Match[str]):
        """解析动态信息"""
        dynamic_id = int(searched.group("dynamic_id"))
        url = searched.group(0)
        return await self.parse_dynamic(dynamic_id, url=url)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)\/?(?:\?.*)?$")
    async def _parse_live(self, searched: Match[str]):
        """解析直播信息"""
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        """解析收藏夹信息"""
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)\/?(?:\?.*)?$")
    @handle("m.bili/read", r"m\.bilibili\.com/read/cv(?P<read_id>\d+)\/?(?:\?.*)?$")
    async def _parse_read(self, searched: Match[str]):
        """解析专栏信息"""
        read_id = int(searched.group("read_id"))
        return await self.parse_read_with_opus(read_id)

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)\/?(?:\?.*)?$")
    @handle("m.bili/opus", r"m\.bilibili\.com/opus/(?P<opus_id>\d+)\/?(?:\?.*)?$")
    async def _parse_opus(self, searched: Match[str]):
        """解析图文动态信息"""
        opus_id = int(searched.group("opus_id"))
        return await self.parse_opus(opus_id)

    @handle("/bangumi/", r"bilibili\.com/bangumi/play/ep(?P<ep_id>\d+)\/?(?:\?.*)?$")
    async def _parse_bangumi(self, searched: Match[str]):
        """解析番剧/影视"""
        from bilibili_api.bangumi import Episode

        ep_id = int(searched.group("ep_id"))
        url = searched.group(0)
        try:
            ep = Episode(epid=ep_id, credential=self.login._credential)
            detail = await ep.get_detail()

            title = detail.get("title", "")
            season_info = detail.get("season_info", {})
            ep_info = detail.get("episode_info", {})

            # 番剧名
            season_title = season_info.get("title", title)
            # 集标题
            ep_title = ep_info.get("share_copy", "") or ep_info.get("long_title", "")
            # 封面
            cover = ep_info.get("cover", "") or season_info.get("cover", "")
            # 简介
            desc = ep_info.get("desc", "") or season_info.get("evaluate", "")
            # 发布时间（pub_time 为 Unix 时间戳）
            pub_time = ep_info.get("pub_time") or ep_info.get("release_date")
            timestamp = int(pub_time) if pub_time and isinstance(pub_time, (int, float)) and pub_time > 0 else None
            # UP主 / 字幕组
            up_info = detail.get("up_info", {})
            up_name = up_info.get("uname", "")
            up_avatar = up_info.get("avatar", "")
            up_mid = up_info.get("mid", "")

            # 并发获取UP主扩展信息 和 置顶评论/热评
            user_extra, pinned_comment, hot_comment = {}, None, None
            aux_tasks = []
            if up_mid:
                aux_tasks.append(self._get_user_extra_info(up_mid))
            from bilibili_api.comment import CommentResourceType as _CRT
            aux_tasks.append(self._get_pinned_comment(ep_id, type_=_CRT.BANGUMI))
            if aux_tasks:
                aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
                for r in aux_results:
                    if isinstance(r, Exception):
                        logger.debug(f"[B站番剧] 辅助 API 调用失败: {r}")
                    elif isinstance(r, dict):
                        user_extra = r
                    elif isinstance(r, tuple) and len(r) == 2:
                        pinned_comment, hot_comment = r

            author = self.create_author(
                up_name or "bilibili",
                avatar=up_avatar or None,
                uid=str(up_mid) if up_mid else None,
                description=user_extra.get("sign"),
                follower_count=user_extra.get("follower_count"),
            )

            contents = []
            if cover:
                contents.append(ImageContent(url=cover))

            # 统计
            stat = detail.get("stat", {})
            stats = {}
            if stat.get("views"):
                stats["views"] = stat["views"]
            if stat.get("favorites"):
                stats["favorites"] = stat["favorites"]
            if stat.get("likes"):
                stats["likes"] = stat["likes"]
            if stat.get("coins"):
                stats["coins"] = stat["coins"]
            if stat.get("danmaku"):
                stats["danmaku"] = stat["danmaku"]
            if stat.get("reply"):
                stats["comments"] = stat["reply"]
            if stat.get("share"):
                stats["reposts"] = stat["share"]
            # UP主扩展信息
            if user_extra.get("following"):
                stats["following"] = user_extra["following"]
            if user_extra.get("like_num"):
                stats["user_likes"] = user_extra["like_num"]
            if user_extra.get("total_views"):
                stats["total_views"] = user_extra["total_views"]

            display_title = f"{season_title}"
            if ep_title:
                display_title += f" - {ep_title}"

            return self.result(
                title=display_title,
                text=desc or None,
                author=author,
                url=url,
                contents=contents,
                timestamp=timestamp,
                stats=stats or None,
                pinned_comment=pinned_comment,
                hot_comment=hot_comment,
                extra={
                    "ep_id": ep_id,
                    "season_id": season_info.get("season_id", ""),
                    "type": "bangumi",
                    "handle": f"ep{ep_id}",
                    "post_id": str(ep_id),
                    "uid": str(up_mid) if up_mid else "",
                    "official_title": user_extra.get("official_title", ""),
                },
                page_type="bangumi",
            )
        except Exception as e:
            logger.warning(f"[B站] 番剧解析失败(ep={ep_id}): {e}")
            return self.result(title=f"番剧 ep{ep_id}", url=url, text=f"解析失败: {e}")

    @handle("/bangumi/", r"bilibili\.com/bangumi/play/ss(?P<season_id>\d+)\/?(?:\?.*)?$")
    async def _parse_bangumi_season(self, searched: Match[str]):
        """解析番剧季页面"""
        from bilibili_api.bangumi import Bangumi

        season_id = int(searched.group("season_id"))
        url = searched.group(0)
        try:
            bangumi = Bangumi(ssid=season_id, credential=self.login._credential)
            detail = await bangumi.get_meta()

            season_title = detail.get("title", "")
            evaluate = detail.get("evaluate", "")
            cover = detail.get("cover", "")

            # UP主信息
            up_info = detail.get("up_info", {})
            up_name = up_info.get("uname", "")
            up_avatar = up_info.get("avatar", "")
            up_mid = up_info.get("mid", "")

            # 获取UP主扩展信息
            user_extra = {}
            if up_mid:
                user_extra = await self._get_user_extra_info(up_mid)

            author = self.create_author(
                up_name or "bilibili",
                avatar=up_avatar or None,
                uid=str(up_mid) if up_mid else None,
                description=user_extra.get("sign"),
                follower_count=user_extra.get("follower_count"),
            )

            contents = []
            if cover:
                contents.append(ImageContent(url=cover))

            # 统计
            stat = detail.get("stat", {})
            stats = {}
            if stat.get("views"):
                stats["views"] = stat["views"]
            if stat.get("favorites"):
                stats["favorites"] = stat["favorites"]
            if stat.get("likes"):
                stats["likes"] = stat["likes"]
            if stat.get("danmaku"):
                stats["danmaku"] = stat["danmaku"]
            # UP主扩展信息
            if user_extra.get("following"):
                stats["following"] = user_extra["following"]
            if user_extra.get("like_num"):
                stats["user_likes"] = user_extra["like_num"]
            if user_extra.get("total_views"):
                stats["total_views"] = user_extra["total_views"]

            return self.result(
                title=season_title,
                text=evaluate or None,
                author=author,
                url=url,
                contents=contents,
                stats=stats or None,
                extra={
                    "season_id": season_id,
                    "type": "bangumi_season",
                    "handle": f"ss{season_id}",
                    "post_id": str(season_id),
                    "uid": str(up_mid) if up_mid else "",
                    "official_title": user_extra.get("official_title", ""),
                },
                page_type="bangumi_season",
            )
        except Exception as e:
            logger.warning(f"[B站] 番剧季解析失败(ss={season_id}): {e}")
            return self.result(title=f"番剧 ss{season_id}", url=url, text=f"解析失败: {e}")

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_num (int): 页码
        """

        from bilibili_api.exceptions import ResponseCodeException

        from .video import AIConclusion, VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)
        # 转换为 msgspec struct
        try:
            raw_info = await video.get_info()
        except ResponseCodeException as e:
            if e.code == -101:
                raise ParseException("B站账号未登录，请执行 /bili_login 重新扫码登录") from e
            raise ParseException(f"B站 API 错误 ({e.code}): {e.msg}") from e
        video_info = convert(raw_info, VideoInfo)
        # 获取简介
        text = f"简介: {video_info.desc}" if video_info.desc else None
        # 处理分 p
        page_info = video_info.extract_info_with_page(page_num)

        # 获取 AI 总结（认证失败时降级，不影响主流程）
        ai_summary: str = ""
        if self.login._credential:
            try:
                cid = await video.get_cid(page_info.index)
                ai_conclusion = await video.get_ai_conclusion(cid)
                ai_conclusion = convert(ai_conclusion, AIConclusion)
                ai_summary = ai_conclusion.summary
            except Exception as e:
                logger.debug(f"[B站] AI 总结获取失败(可能凭证过期): {e}")
                ai_summary = ""
        else:
            ai_summary = ""

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # 视频下载 task
        async def download_video():
            output_path = self.cfg.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                return output_path
            v_url, a_url = await self.extract_download_urls(
                video=video, page_index=page_info.index
            )
            if page_info.duration > self.cfg.max_duration:
                raise DurationLimitException
            if a_url is not None:
                return await self.downloader.download_av_and_merge(
                    v_url,
                    a_url,
                    output_path=output_path,
                    headers=self.headers,
                    proxy=self.proxy,
                    max_size_mb=self.cfg.max_video_size,
                )
            else:
                return await self.downloader.streamd(
                    v_url,
                    file_name=output_path.name,
                    headers=self.headers,
                    proxy=self.proxy,
                    max_size_mb=self.cfg.max_video_size,
                )

        video_task = asyncio.create_task(download_video())
        video_content = self.create_video_content(
            video_task,
            page_info.cover,
            page_info.duration,
        )

        stats = {
            "views": video_info.stat.view,
            "danmaku": video_info.stat.danmaku,
            "likes": video_info.stat.like,
            "favorites": video_info.stat.favorite,
            "coins": video_info.stat.coin,
            "comments": video_info.stat.reply or 0,
            "reposts": video_info.stat.share or 0,
        }

        # 并发获取用户扩展信息和置顶评论（非阻塞，失败不影响主流程）
        user_extra, pinned_comment, hot_comment = {}, None, None
        tasks = []
        mid = video_info.owner.mid
        if mid:
            tasks.append(self._get_user_extra_info(mid))
        video_aid = getattr(video_info, 'aid', None) or getattr(video_info, 'bvid', None)
        if video_aid:
            tasks.append(self._get_pinned_comment(video_aid))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站] 辅助 API 调用失败: {r}")
                elif isinstance(r, dict):
                    user_extra = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r

        # 添加用户扩展信息到 stats
        if user_extra.get("following"):
            stats["following"] = user_extra["following"]
        if user_extra.get("like_num"):
            stats["user_likes"] = user_extra["like_num"]
        if user_extra.get("total_views"):
            stats["total_views"] = user_extra["total_views"]

        # 创建作者
        author = self.create_author(
            video_info.owner.name,
            video_info.owner.face,
            uid=str(mid) if mid else None,
            description=user_extra.get("sign"),
            follower_count=user_extra.get("follower_count"),
        )

        extra = {
            "uid": str(mid), "info": ai_summary,
            "handle": f"av{getattr(video_info, 'aid', '')}", "bvid": video_info.bvid, "post_id": video_info.bvid,
        }
        if user_extra.get("official_title"):
            extra["official_title"] = user_extra["official_title"]

        # 提取视频头衔/徽章（如"每周必看"、"排行榜"、"入站必刷"）
        badge_text = self._extract_video_badge(video_info)
        if badge_text:
            extra["video_badge"] = badge_text

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra=extra,
            page_type="video",
            stats=stats,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
        )

    def _extract_video_badge(self, video_info) -> str:
        """提取视频头衔/徽章文本"""
        # 尝试从 badge 字段提取
        badge = getattr(video_info, 'badge', None)
        if badge:
            if isinstance(badge, dict):
                text = badge.get("text", "")
                if text:
                    return text
            elif isinstance(badge, str):
                return badge
        # 尝试从 honor 字段提取
        honor = getattr(video_info, 'honor', None)
        if honor and isinstance(honor, dict):
            honor_reply = honor.get("honor_reply", {})
            honor_icon = honor_reply.get("honor_icon", {})
            text = honor_icon.get("text", "")
            if text:
                return text
        # 尝试从 label 字段提取
        label = getattr(video_info, 'label', None)
        if label:
            if isinstance(label, dict):
                text = label.get("text", "")
                if text:
                    return text
            elif isinstance(label, str):
                return label
        return ""

    async def parse_dynamic(self, dynamic_id: int, url: str = ""):
        """解析动态信息

        Args:
            url (str): 动态链接
        """
        from bilibili_api.dynamic import Dynamic
        from bilibili_api.exceptions import ResponseCodeException

        from .dynamic import DynamicData

        dynamic_ = Dynamic(dynamic_id, await self.login.credential)
        try:
            raw_dynamic = await dynamic_.get_info()
        except ResponseCodeException as e:
            if e.code == -101:
                raise ParseException("B站账号未登录，请执行 /bili_login 重新扫码登录") from e
            raise ParseException(f"B站 API 错误 ({e.code}): {e.msg}") from e
        dynamic_data = convert(raw_dynamic, DynamicData)
        dynamic_info = dynamic_data.item
        mid = dynamic_info.modules.module_author.mid
        author = self.create_author(
            dynamic_info.name,
            dynamic_info.avatar,
            uid=str(mid),
            description=dynamic_info.modules.module_author.sign or None,
        )

        # 并发获取用户扩展信息和置顶评论（非阻塞，失败不影响主流程）
        user_extra, pinned_comment, hot_comment = {}, None, None
        aux_tasks = []
        if mid:
            aux_tasks.append(self._get_user_extra_info(mid))
        # 动态评论 oid = dynamic_id
        from bilibili_api.comment import CommentResourceType as _CRT
        aux_tasks.append(self._get_pinned_comment(dynamic_id, type_=_CRT.DYNAMIC))
        if aux_tasks:
            aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
            for r in aux_results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站动态] 辅助 API 调用失败: {r}")
                elif isinstance(r, dict):
                    user_extra = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r
        if user_extra.get("follower_count") is not None:
            author.follower_count = user_extra["follower_count"]

        # 下载图片
        contents: list[MediaContent] = []
        for image_url in dynamic_info.image_urls:
            img_task = self.downloader.download_img(
                image_url, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(img_task, source_url=image_url))

        # 如果有封面（视频动态），添加到内容列表
        cover = dynamic_info.cover_url
        if cover and cover not in dynamic_info.image_urls:
            img_task = self.downloader.download_img(
                cover, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(img_task, source_url=cover))

        # 处理转发（repost）
        repost = None
        if dynamic_data.orig:
            orig = dynamic_data.orig
            logger.debug(f"[B站动态] orig.type={orig.type}, orig.modules.module_dynamic={orig.modules.module_dynamic}")
            orig_contents: list[MediaContent] = []
            try:
                for image_url in orig.image_urls:
                    img_task = self.downloader.download_img(
                        image_url, headers=self.headers, proxy=self.proxy
                    )
                    orig_contents.append(ImageContent(img_task, source_url=image_url))
                orig_cover = orig.cover_url
                if orig_cover and orig_cover not in orig.image_urls:
                    img_task = self.downloader.download_img(
                        orig_cover, headers=self.headers, proxy=self.proxy
                    )
                    orig_contents.append(ImageContent(img_task, source_url=orig_cover))
            except Exception as e:
                logger.warning(f"[B站动态] 获取转发内容媒体失败: {e}")

            # 原动态作者信息（使用 _get_user_extra_info 获取完整信息）
            orig_mid = orig.modules.module_author.mid
            orig_user_extra = await self._get_user_extra_info(orig_mid) if orig_mid else {}
            orig_author = self.create_author(
                orig.name, orig.avatar,
                uid=str(orig_mid),
                description=orig_user_extra.get("sign") or orig.modules.module_author.sign or None,
                follower_count=orig_user_extra.get("follower_count"),
            )

            # 原动态统计数据
            orig_stats: dict[str, int] = {}
            orig_module_stat = orig.modules.module_stat
            if orig_module_stat:
                for key, stat_key in (
                    ("like", "likes"), ("comment", "comments"),
                    ("forward", "reposts"), ("favorite", "favorites"),
                    ("coin", "coins"),
                ):
                    val = orig_module_stat.get(key)
                    if isinstance(val, dict):
                        count = val.get("count", 0)
                    elif isinstance(val, (int, float)):
                        count = int(val)
                    else:
                        continue
                    if count:
                        orig_stats[stat_key] = int(count)

            # 原动态置顶评论和热评
            orig_pinned, orig_hot = None, None
            try:
                from bilibili_api.comment import CommentResourceType as _CRT2
                orig_pinned, orig_hot = await self._get_pinned_comment(
                    int(orig.id_str), type_=_CRT2.DYNAMIC
                )
            except Exception as e:
                logger.debug(f"[B站动态] 获取原动态评论失败: {e}")

            # 尝试从 module_dynamic 直接提取文本（防止属性访问静默返回 None）
            orig_text = orig.text
            orig_title = orig.title
            if not orig_text and orig.modules.module_dynamic:
                # 回退：直接从 raw dict 提取
                orig_text = orig.modules.module_dynamic.get("desc", {}).get("text") if isinstance(orig.modules.module_dynamic.get("desc"), dict) else None
            logger.debug(f"[B站动态] orig_title={orig_title}, orig_text={orig_text}, orig_contents={len(orig_contents)}")

            # 原动态专属ID
            orig_type = orig.type if hasattr(orig, 'type') else ""
            orig_handle = f"opus/{orig.id_str}" if "OPUS" in str(orig_type).upper() else f"t{orig.id_str}"

            repost = self.result(
                title=orig_title,
                text=orig_text,
                author=orig_author,
                contents=orig_contents,
                timestamp=orig.timestamp,
                url=f"https://www.bilibili.com/dynamic/{orig.id_str}",
                stats=orig_stats or None,
                pinned_comment=orig_pinned,
                hot_comment=orig_hot,
                extra={"uid": str(orig_mid), "handle": orig_handle, "post_id": orig.id_str},
                page_type="dynamic",
            )

        # 提取统计数据
        stats: dict[str, int] = {}
        module_stat = dynamic_info.modules.module_stat
        if module_stat:
            for key, stat_key in (
                ("like", "likes"),
                ("comment", "comments"),
                ("forward", "reposts"),
                ("favorite", "favorites"),
                ("coin", "coins"),
            ):
                val = module_stat.get(key)
                if isinstance(val, dict):
                    count = val.get("count", 0)
                elif isinstance(val, (int, float)):
                    count = int(val)
                else:
                    continue
                if count:
                    stats[stat_key] = int(count)
        # 添加用户扩展信息到 stats
        if user_extra.get("following"):
            stats["following"] = user_extra["following"]
        if user_extra.get("like_num"):
            stats["user_likes"] = user_extra["like_num"]
        if user_extra.get("total_views"):
            stats["total_views"] = user_extra["total_views"]

        # 专属ID: 动态用 t{id}，图文用 opus/{id}
        dyn_type = dynamic_info.type if hasattr(dynamic_info, 'type') else ""
        if "OPUS" in str(dyn_type).upper():
            handle = f"opus/{dynamic_id}"
        else:
            handle = f"t{dynamic_id}"

        extra = {"uid": str(mid), "handle": handle, "post_id": str(dynamic_id)}
        if user_extra.get("official_title"):
            extra["official_title"] = user_extra["official_title"]

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            url=url,
            author=author,
            contents=contents,
            repost=repost,
            stats=stats or None,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            page_type="dynamic",
        )

    async def parse_opus(self, opus_id: int):
        """解析图文动态信息

        Args:
            opus_id (int): 图文动态 id
        """
        opus = Opus(opus_id, await self.login.credential)
        url = f"https://www.bilibili.com/opus/{opus_id}"
        return await self._parse_opus_obj(opus, url=url, opus_id=opus_id)

    async def parse_read_with_opus(self, read_id: int):
        """解析专栏信息, 使用 Opus 接口
        Args:
            read_id (int): 专栏 id
        """
        from bilibili_api.article import Article

        article = Article(read_id)
        url = f"https://www.bilibili.com/read/cv{read_id}"
        from bilibili_api.comment import CommentResourceType as _CRT
        return await self._parse_opus_obj(await article.turn_to_opus(), url=url, opus_id=read_id, comment_type=_CRT.ARTICLE)

    async def _parse_opus_obj(self, bili_opus: Opus, url: str | None = None, opus_id: int | None = None, comment_type: "CommentResourceType | None" = None):
        """解析图文动态信息
        Args:
            bili_opus (Opus): 图文动态对象
            url (str | None): 来源链接
            opus_id (int | None): 图文动态 ID（用于获取评论）
            comment_type: 评论资源类型（默认 DYNAMIC）
        Returns:
            ParseResult: 解析结果
        """
        from .opus import ImageNode, OpusItem, TextNode

        opus_info = await bili_opus.get_info()
        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")
        # 转换为结构体
        opus_data = convert(opus_info, OpusItem)
        logger.debug(f"opus_data: {opus_data}")
        mid = None
        for module in opus_data.item.modules:
            if module.module_author is not None:
                mid = module.module_author.mid
                break

        # 并发获取用户扩展信息和置顶评论
        user_extra, pinned_comment, hot_comment = {}, None, None
        aux_tasks = []
        if mid:
            aux_tasks.append(self._get_user_extra_info(mid))
        if opus_id:
            from bilibili_api.comment import CommentResourceType as _CRT
            aux_tasks.append(self._get_pinned_comment(opus_id, type_=comment_type or _CRT.DYNAMIC))
        if aux_tasks:
            aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
            for r in aux_results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站图文] 辅助 API 调用失败: {r}")
                elif isinstance(r, dict):
                    user_extra = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r

        author = self.create_author(
            *opus_data.name_avatar,
            uid=str(getattr(opus_data, 'uid', None) or getattr(opus_data, 'mid', None) or ''),
            description=user_extra.get("sign") or getattr(opus_data, 'author_sign', None) or None,
            follower_count=user_extra.get("follower_count"),
        )
        # 按顺序处理图文内容：文本和图片分开处理
        contents: list[MediaContent] = []
        first_image_url = None
        text_parts: list[str] = []
        for node in opus_data.gen_text_img():
            if isinstance(node, ImageNode):
                if first_image_url is None:
                    first_image_url = node.url
                # 图片作为 ImageContent 添加（模板通过 image_urls 渲染）
                img_task = self.downloader.download_img(
                    node.url, headers=self.headers, proxy=self.proxy
                )
                contents.append(ImageContent(img_task, source_url=node.url))
            elif isinstance(node, TextNode):
                text_parts.append(node.text)
        # 所有文本合并为 result.text（模板通过 {{ text }} 渲染）
        current_text = "".join(text_parts).strip()

        # 图文封面图: 用第一张图片
        extra: dict = {"uid": str(mid or ''), "handle": f"opus/{opus_id}", "post_id": str(opus_id)} if opus_id else {"uid": str(mid or '')}
        if first_image_url:
            extra["cover_url"] = first_image_url
        if user_extra.get("official_title"):
            extra["official_title"] = user_extra["official_title"]

        # 提取统计数据
        stats: dict[str, int] = {}
        for module in opus_data.item.modules:
            if module.module_stat is not None:
                stat_data = module.module_stat
                if stat_data.like:
                    stats["likes"] = stat_data.like.get("count", 0)
                if stat_data.comment:
                    stats["comments"] = stat_data.comment.get("count", 0)
                if stat_data.forward:
                    stats["reposts"] = stat_data.forward.get("count", 0)
                if stat_data.favorite:
                    stats["favorites"] = stat_data.favorite.get("count", 0)
                if stat_data.coin:
                    stats["coins"] = stat_data.coin.get("count", 0)
                break
        # 添加用户扩展信息到 stats
        if user_extra.get("following"):
            stats["following"] = user_extra["following"]
        if user_extra.get("like_num"):
            stats["user_likes"] = user_extra["like_num"]
        if user_extra.get("total_views"):
            stats["total_views"] = user_extra["total_views"]

        return self.result(
            title=opus_data.title,
            author=author,
            timestamp=opus_data.timestamp,
            contents=contents,
            text=current_text.strip(),
            url=url,
            stats=stats,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            page_type="opus",
        )

    async def _get_live_extra_stats(self, room) -> dict:
        """并发获取直播间额外统计数据（高能榜、大航海），失败返回空 dict"""
        result: dict = {}
        tasks = {
            "gaonengbang": room.get_gaonengbang(page=1),
            "dahanghai": room.get_dahanghai(page=1),
        }
        done = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, val in zip(tasks.keys(), done):
            if isinstance(val, Exception):
                logger.debug(f"[B站直播] 获取{key}失败: {val}")
                continue
            if key == "gaonengbang" and isinstance(val, dict):
                result["high_energy_users"] = val.get("onlineNum", 0)
                # 高能榜排名列表（前3名）
                rank_items = val.get("OnlineRankItem", [])
                if rank_items:
                    result["top3_rank"] = [
                        {"name": item.get("name", ""), "score": item.get("score", 0)}
                        for item in rank_items[:3]
                    ]
            elif key == "dahanghai" and isinstance(val, dict):
                info = val.get("info", {})
                result["fleet_total"] = info.get("num", 0)
                # 统计各等级舰长数
                guard_counts = {1: 0, 2: 0, 3: 0}  # 1=总督, 2=提督, 3=舰长
                for member in val.get("list", []):
                    level = member.get("guard_level", 0)
                    if level in guard_counts:
                        guard_counts[level] += 1
                result["governor"] = guard_counts[1]   # 总督
                result["admiral"] = guard_counts[2]     # 提督
                result["captain"] = guard_counts[3]     # 舰长
        return result

    async def parse_live(self, room_id: int):
        """解析直播信息

        Args:
            room_id (int): 直播 id

        Returns:
            ParseResult: 解析结果
        """
        from bilibili_api.live import LiveRoom

        from .live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=await self.login.credential)
        info_dict = await room.get_room_info()

        try:
            room_data = convert(info_dict, RoomData)
        except Exception as e:
            logger.warning(f"[B站直播] 数据转换失败(room={room_id}): {e}")
            # fallback: 直接从 dict 提取关键信息
            room_info = info_dict.get("room_info", {})
            return self.result(
                url=f"https://live.bilibili.com/{room_id}",
                title=room_info.get("title", f"直播间 {room_id}"),
                text=room_info.get("description", ""),
                extra={"room_id": room_id, "type": "live"},
            )
        contents: list[MediaContent] = []
        # 下载封面
        if cover := room_data.cover:
            cover_task = self.downloader.download_img(
                cover, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(cover_task, source_url=cover))

        # 下载关键帧
        if keyframe := room_data.keyframe:
            keyframe_task = self.downloader.download_img(
                keyframe, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(keyframe_task, source_url=keyframe))

        # 从 room_info 和 anchor_info 提取额外数据
        room_info = info_dict.get("room_info", {})
        anchor_info = info_dict.get("anchor_info", {})
        medal_info = anchor_info.get("medal_info", {})
        live_info = anchor_info.get("live_info", {})
        relation_info = anchor_info.get("relation_info", {})

        # 并发获取主播扩展信息和额外统计数据
        uid = room_data.uid
        user_extra, extra_stats = {}, {}
        aux_tasks = []
        if uid:
            aux_tasks.append(self._get_user_extra_info(uid))
        aux_tasks.append(self._get_live_extra_stats(room))
        if aux_tasks:
            aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
            for r in aux_results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站直播] 辅助 API 调用失败: {r}")
                elif isinstance(r, dict) and "follower_count" in r:
                    user_extra = r
                elif isinstance(r, dict):
                    extra_stats = r

        author = self.create_author(
            room_data.name, room_data.avatar,
            uid=str(uid),
            description=user_extra.get("sign"),
            follower_count=user_extra.get("follower_count"),
        )

        # 构建统计数据
        stats: dict[str, int] = {}
        if room_data.online:
            stats["views"] = room_data.online
        # 直播间关注数
        room_attention = room_info.get("attention") or relation_info.get("attention")
        if room_attention:
            stats["attention"] = room_attention
        # 用户扩展信息
        if user_extra.get("following"):
            stats["following"] = user_extra["following"]
        if user_extra.get("like_num"):
            stats["user_likes"] = user_extra["like_num"]
        if user_extra.get("total_views"):
            stats["total_views"] = user_extra["total_views"]
        # 粉丝团成员数
        fansclub = medal_info.get("fansclub")
        if fansclub:
            stats["fansclub"] = fansclub
        # 高能榜数据
        if extra_stats.get("high_energy_users"):
            stats["high_energy_users"] = extra_stats["high_energy_users"]
        # 大航海数据
        if extra_stats.get("fleet_total"):
            stats["fleet_total"] = extra_stats["fleet_total"]
        if extra_stats.get("governor"):
            stats["governor"] = extra_stats["governor"]
        if extra_stats.get("admiral"):
            stats["admiral"] = extra_stats["admiral"]
        if extra_stats.get("captain"):
            stats["captain"] = extra_stats["captain"]

        # 直播状态判断
        live_start_time = room_info.get("live_start_time", 0)
        is_living = bool(live_start_time and live_start_time > 0)
        live_status = "🔴 直播中" if is_living else "⚫ 未开播"

        # 尝试获取回放信息（仅在未开播时）
        playback_url = None
        if not is_living:
            try:
                play_info = await room.get_room_play_info()
                if play_info:
                    durl = play_info.get("durl", [])
                    if durl:
                        playback_url = durl[0].get("url") if isinstance(durl, list) else None
            except Exception:
                pass

        # 直播间简介
        room_desc = room_info.get("description", "") or room_info.get("desc", "")

        detail_text = room_data.detail
        if room_desc:
            detail_text = f"简介: {room_desc}\n{detail_text}"
        if not is_living:
            detail_text = f"{live_status}\n{detail_text}"
            if playback_url:
                detail_text += f"\n回放: {playback_url}"

        # 添加直播专属字段到 stats（模板通过 stats.xxx 渲染）
        stats["is_living"] = "直播中" if is_living else "未开播"
        if live_info.get("level"):
            stats["live_level"] = live_info["level"]
        if extra_stats.get("top3_rank"):
            stats["top3_rank"] = ", ".join(
                f"{r['name']}({r['score']})" for r in extra_stats["top3_rank"][:3]
            )

        # 构建 extra 字段
        extra = {
            "handle": f"live:{room_id}",
            "uid": str(uid) if uid else "",
            "is_living": is_living,
            "post_id": str(room_id),
            "live_level": live_info.get("level"),
            "area_id": room_info.get("area_id"),
            "parent_area_id": room_info.get("parent_area_id"),
        }
        if user_extra.get("official_title"):
            extra["official_title"] = user_extra["official_title"]
        if extra_stats.get("top3_rank"):
            extra["top3_rank"] = extra_stats["top3_rank"]

        url = f"https://live.bilibili.com/{room_id}"
        return self.result(
            url=url,
            title=room_data.title,
            text=detail_text,
            contents=contents,
            author=author,
            timestamp=live_start_time or None,
            stats=stats or None,
            extra=extra,
            page_type="live",
        )

    # space.bilibili.com/{mid}、/dynamic、/upload/video、/upload/opus、/upload/audio、/lists、/lists/{id}、/favlist
    @handle("space.bilibili.com", r"space\.bilibili\.com/(?P<mid>\d+)(?:/(?P<sub>dynamic|upload/(?:video|opus|audio)|lists(?:/\d+)?|favlist))?(?:\?.*)?$")
    async def _parse_space(self, searched: Match[str]):
        """解析 B站用户主页（根据子路径分发）"""
        mid = int(searched.group("mid"))
        sub = searched.group("sub") or ""
        url = searched.group(0)
        # 如果是 favlist 子路径，提取 fid 参数并转发给 parse_favlist
        if sub == "favlist":
            import re as _re
            fid_match = _re.search(r'[?&]fid=(\d+)', url)
            if fid_match:
                return await self.parse_favlist(int(fid_match.group(1)))
            # 无 fid 参数，尝试获取用户的收藏夹列表
            return await self.parse_space(mid, sub="favlist", url=url)
        return await self.parse_space(mid, sub=sub, url=url)

    async def _get_space_user_info(self, mid: int) -> dict:
        """获取用户主页基础信息（UP主认证、粉丝数、关注数、获赞数、签名、头像）"""
        from bilibili_api import user as bili_user

        u = bili_user.User(mid, credential=self.login._credential)
        # 并发获取用户信息和关系信息
        user_info, relation_info = await asyncio.gather(
            u.get_user_info(),
            u.get_relation_info(),
            return_exceptions=True,
        )
        if isinstance(user_info, Exception):
            raise ParseException(f"B站用户信息获取失败: {user_info}")

        name = user_info.get("name", "")
        face = user_info.get("face", "")
        sign = user_info.get("sign", "")
        official = user_info.get("official", {})
        official_title = official.get("title", "")
        official_role = official.get("role", 0)  # 0=无, 1=个人认证, 2=机构认证

        fans = 0
        following = 0
        if not isinstance(relation_info, Exception):
            fans = relation_info.get("follower", 0)
            following = relation_info.get("following", 0)

        # 获赞数（需要额外 API）
        like_num = 0
        try:
            up_stat = await u.get_up_stat()
            like_num = up_stat.get("likes", 0)
        except Exception:
            pass

        return {
            "name": name, "face": face, "sign": sign,
            "fans": fans, "following": following, "like_num": like_num,
            "official_title": official_title, "official_role": official_role,
        }

    def _build_space_author(self, mid: int, user_data: dict):
        """从用户数据构建 Author"""
        return self.create_author(
            user_data["name"], user_data["face"],
            uid=str(mid),
            description=user_data["sign"] or None,
            follower_count=user_data["fans"] or None,
        )

    def _build_space_extra(self, mid: int, user_data: dict, page_type: str) -> dict:
        """构建 space 通用 extra 字段"""
        extra = {
            "handle": f"UID:{mid}",
            "uid": str(mid),
            "page_type": page_type,
        }
        if user_data["official_title"]:
            extra["official_title"] = user_data["official_title"]
            extra["official_role"] = user_data["official_role"]
        return extra

    def _build_space_stats(self, user_data: dict) -> dict:
        """构建 space 通用 stats"""
        stats = {}
        if user_data["like_num"]:
            stats["likes"] = user_data["like_num"]
        if user_data["following"]:
            stats["following"] = user_data["following"]
        if user_data["fans"]:
            stats["followers"] = user_data["fans"]
        return stats

    def _build_space_text(self, user_data: dict, content_summary: str = "") -> str:
        """构建 space 通用文本摘要"""
        parts = []
        if user_data["sign"]:
            parts.append(user_data["sign"])
        if user_data["official_title"]:
            parts.append(f"认证: {user_data['official_title']}")
        parts.append(f"关注: {user_data['following']} | 粉丝: {user_data['fans']} | 获赞: {user_data['like_num']}")
        if content_summary:
            parts.append(content_summary)
        return "\n".join(parts)

    def _extract_video_from_item(self, item: dict) -> dict | None:
        """从动态/视频列表项中提取视频信息"""
        # 动态格式
        modules = item.get("modules", {})
        dynamic_desc = modules.get("module_dynamic", {})
        major = dynamic_desc.get("major", {})
        archive = major.get("archive", {})
        if archive:
            return {
                "title": archive.get("title", ""),
                "cover": archive.get("cover", ""),
                "desc": archive.get("desc", ""),
                "bvid": archive.get("bvid", ""),
                "aid": archive.get("aid", ""),
                "stat": {
                    "views": archive.get("stat", {}).get("view", 0),
                    "danmaku": archive.get("stat", {}).get("danmaku", 0),
                    "likes": archive.get("stat", {}).get("like", 0),
                    "coins": archive.get("stat", {}).get("coin", 0),
                    "favorites": archive.get("stat", {}).get("favorite", 0),
                    "reposts": archive.get("stat", {}).get("share", 0),
                    "comments": archive.get("stat", {}).get("reply", 0),
                },
                "pub_ts": modules.get("module_author", {}).get("pub_ts", 0),
            }
        # 视频列表格式（get_videos 返回）
        if "title" in item and "bvid" in item:
            stat = item.get("stat", {})
            return {
                "title": item.get("title", ""),
                "cover": item.get("pic", ""),
                "desc": item.get("description", ""),
                "bvid": item.get("bvid", ""),
                "aid": str(item.get("aid", "")),
                "stat": {
                    "views": stat.get("view", 0),
                    "danmaku": stat.get("danmaku", 0),
                    "likes": stat.get("like", 0),
                    "coins": stat.get("coin", 0),
                    "favorites": stat.get("favorite", 0),
                    "reposts": stat.get("share", 0),
                    "comments": stat.get("reply", 0),
                },
                "pub_ts": item.get("pubdate", 0),
            }
        return None

    def _format_video_summary(self, videos: list[dict]) -> str:
        """格式化视频列表摘要"""
        if not videos:
            return ""
        lines = [f"\n最近 {len(videos)} 个视频:"]
        for v in videos[:5]:
            views = v.get("stat", {}).get("views", 0)
            lines.append(f"· {v['title']} ({views}播放)")
        return "\n".join(lines)

    async def _fetch_space_videos(self, mid: int, count: int = 5, order: str = "pubdate") -> list[dict]:
        """获取用户最新/最热视频"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            vid_order = bili_user.VideoOrder.VIEW if order == "click" else bili_user.VideoOrder.PUBDATE
            resp = await u.get_videos(ps=count, pn=1, order=vid_order)
            vlist = resp.get("list", {}).get("vlist", [])
            return [v for item in vlist if (v := self._extract_video_from_item(item))]
        except Exception as e:
            logger.debug(f"[B站空间] 获取视频失败(mid={mid}): {e}")
            return []

    async def _fetch_space_top_videos(self, mid: int) -> list[dict]:
        """获取用户置顶/代表作视频"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            resp = await u.get_top_videos()
            vlist = resp.get("data", {}).get("top", []) if isinstance(resp.get("data"), dict) else resp.get("data", [])
            if isinstance(vlist, dict):
                vlist = vlist.get("top", [])
            return [v for item in vlist if (v := self._extract_video_from_item(item))]
        except Exception as e:
            logger.debug(f"[B站空间] 获取置顶视频失败(mid={mid}): {e}")
            return []

    async def _fetch_space_dynamics(self, mid: int, count: int = 5) -> tuple[list[dict], dict | None]:
        """获取用户最新动态，返回 (动态列表, 置顶动态)"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            resp = await u.get_dynamics_new()
            items = resp.get("items", [])
            pinned = None
            dynamics = []
            for item in items:
                modules = item.get("modules", {})
                # 检查是否置顶
                if modules.get("module_author", {}).get("is_top", False):
                    pinned = item
                else:
                    dynamics.append(item)
            return dynamics[:count], pinned
        except Exception as e:
            logger.debug(f"[B站空间] 获取动态失败(mid={mid}): {e}")
            return [], None

    def _extract_dynamic_from_item(self, item: dict) -> dict:
        """从动态项中提取标准化动态信息"""
        modules = item.get("modules", {})
        dynamic_desc = modules.get("module_dynamic", {})
        major = dynamic_desc.get("major", {})
        author = modules.get("module_author", {})

        title = ""
        cover = ""
        text = dynamic_desc.get("desc", {}).get("text", "")
        stat = modules.get("module_stat", {})

        # 从 major 提取标题和封面
        if major.get("type") == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive", {})
            title = archive.get("title", "")
            cover = archive.get("cover", "")
        elif major.get("type") == "MAJOR_TYPE_OPUS":
            opus = major.get("opus", {})
            title = opus.get("title", "")
            pics = opus.get("pics", [])
            if pics:
                cover = pics[0].get("url", "")
            if not text:
                text = opus.get("summary", {}).get("text", "")
        elif major.get("type") == "MAJOR_TYPE_DRAW":
            draw = major.get("draw", {})
            items = draw.get("items", [])
            if items:
                cover = items[0].get("src", "")

        return {
            "title": title,
            "cover": cover,
            "text": text,
            "pub_ts": author.get("pub_ts", 0),
            "stat": {
                "likes": stat.get("like", {}).get("count", 0) if isinstance(stat.get("like"), dict) else stat.get("like", 0),
                "reposts": stat.get("forward", {}).get("count", 0) if isinstance(stat.get("forward"), dict) else stat.get("forward", 0),
                "comments": stat.get("comment", {}).get("count", 0) if isinstance(stat.get("comment"), dict) else stat.get("comment", 0),
            },
        }

    async def _fetch_space_articles(self, mid: int, count: int = 5) -> list[dict]:
        """获取用户最新图文专栏"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            resp = await u.get_articles(ps=count, pn=1)
            articles = resp.get("articles", [])
            results = []
            for a in articles[:count]:
                results.append({
                    "title": a.get("title", ""),
                    "cover": a.get("image_urls", [""])[0] if a.get("image_urls") else "",
                    "text": a.get("summary", ""),
                    "pub_ts": a.get("publish_time", 0),
                    "stat": {
                        "likes": a.get("stats", {}).get("like", 0),
                        "favorites": a.get("stats", {}).get("favorite", 0),
                        "reposts": a.get("stats", {}).get("share", 0),
                        "comments": a.get("stats", {}).get("reply", 0),
                    },
                })
            return results
        except Exception as e:
            logger.debug(f"[B站空间] 获取图文失败(mid={mid}): {e}")
            return []

    async def _fetch_space_audios(self, mid: int, count: int = 5) -> list[dict]:
        """获取用户最新音频"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            resp = await u.get_audios(ps=count, pn=1)
            songs = resp.get("data", {}).get("songs", []) if isinstance(resp.get("data"), dict) else resp.get("data", [])
            results = []
            for s in songs[:count]:
                results.append({
                    "title": s.get("title", ""),
                    "cover": s.get("cover", ""),
                    "text": s.get("intro", ""),
                    "pub_ts": s.get("passtime", 0),
                    "stat": {
                        "likes": s.get("statistic", {}).get("like", 0) if isinstance(s.get("statistic"), dict) else 0,
                        "favorites": s.get("statistic", {}).get("collect", 0) if isinstance(s.get("statistic"), dict) else 0,
                    },
                })
            return results
        except Exception as e:
            logger.debug(f"[B站空间] 获取音频失败(mid={mid}): {e}")
            return []

    async def _fetch_channel_videos(self, mid: int, list_id: int, count: int = 5) -> list[dict]:
        """获取合集/系列中的视频"""
        from bilibili_api import user as bili_user
        try:
            u = bili_user.User(mid, credential=self.login._credential)
            # 尝试 season
            try:
                resp = await u.get_channel_videos_season(sid=list_id, ps=count)
                vlist = resp.get("archives", [])
                return [v for item in vlist if (v := self._extract_video_from_item(item))]
            except Exception:
                pass
            # 尝试 series
            resp = await u.get_channel_videos_series(sid=list_id, ps=count)
            vlist = resp.get("archives", [])
            return [v for item in vlist if (v := self._extract_video_from_item(item))]
        except Exception as e:
            logger.debug(f"[B站空间] 获取合集视频失败(mid={mid}, list={list_id}): {e}")
            return []

    async def parse_space(self, mid: int, sub: str = "", url: str = ""):
        """解析 B站用户主页信息（根据子路径分发到不同处理逻辑）

        Args:
            mid: 用户ID
            sub: URL子路径（如 "dynamic"、"upload/video"、"lists/123"）
            url: 原始URL
        """
        from ..data import SendGroup

        # 确定页面类型
        if "upload/video" in sub:
            page_type = "video"
        elif "upload/opus" in sub:
            page_type = "opus"
        elif "upload/audio" in sub:
            page_type = "audio"
        elif sub.startswith("lists/"):
            page_type = "list"
        elif sub == "dynamic":
            page_type = "dynamic"
        else:
            page_type = "main"

        # 获取用户基础信息（所有页面类型共用）
        user_data = await self._get_space_user_info(mid)
        author = self._build_space_author(mid, user_data)
        extra = self._build_space_extra(mid, user_data, page_type)
        stats = self._build_space_stats(user_data)
        contents = []
        content_summary = ""

        # 根据页面类型获取不同内容
        if page_type == "main":
            # 主页: 置顶视频 + 播放量最多的5个视频
            top_videos, hot_videos = await asyncio.gather(
                self._fetch_space_top_videos(mid),
                self._fetch_space_videos(mid, count=5, order="click"),
            )
            # 合并去重（置顶优先）
            seen_bvids = set()
            all_videos = []
            for v in top_videos:
                if v["bvid"] not in seen_bvids:
                    seen_bvids.add(v["bvid"])
                    all_videos.append(v)
            for v in hot_videos:
                if v["bvid"] not in seen_bvids and len(all_videos) < 6:
                    seen_bvids.add(v["bvid"])
                    all_videos.append(v)
            for v in all_videos:
                if v["cover"]:
                    contents.append(ImageContent(url=v["cover"]))
            stats["videos"] = len(all_videos)
            content_summary = self._format_video_summary(all_videos)
            extra["video_count"] = len(all_videos)

        elif page_type == "dynamic":
            # 动态页: 置顶动态 + 最新5个动态
            dynamics, pinned = await self._fetch_space_dynamics(mid, count=5)
            if pinned:
                pinned_info = self._extract_dynamic_from_item(pinned)
                if pinned_info["cover"]:
                    contents.append(ImageContent(url=pinned_info["cover"]))
            for d in dynamics:
                info = self._extract_dynamic_from_item(d)
                if info["cover"]:
                    contents.append(ImageContent(url=info["cover"]))
            total = len(dynamics) + (1 if pinned else 0)
            stats["dynamics"] = total
            content_summary = f"\n最近 {total} 条动态"
            extra["dynamic_count"] = total

        elif page_type == "video":
            # 视频页: 最新5个视频
            videos = await self._fetch_space_videos(mid, count=5)
            for v in videos:
                if v["cover"]:
                    contents.append(ImageContent(url=v["cover"]))
            stats["videos"] = len(videos)
            content_summary = self._format_video_summary(videos)
            extra["video_count"] = len(videos)

        elif page_type == "opus":
            # 图文专栏页: 最新5个图文
            articles = await self._fetch_space_articles(mid, count=5)
            for a in articles:
                if a["cover"]:
                    contents.append(ImageContent(url=a["cover"]))
            stats["articles"] = len(articles)
            content_summary = f"\n最近 {len(articles)} 篇图文"
            extra["article_count"] = len(articles)

        elif page_type == "audio":
            # 音频页: 最新5个音频
            audios = await self._fetch_space_audios(mid, count=5)
            for a in audios:
                if a["cover"]:
                    contents.append(ImageContent(url=a["cover"]))
            stats["audios"] = len(audios)
            content_summary = f"\n最近 {len(audios)} 个音频"
            extra["audio_count"] = len(audios)

        elif page_type == "list":
            # 合集页: 合集中最新5个视频
            list_id = int(sub.split("/")[1]) if "/" in sub else 0
            videos = await self._fetch_channel_videos(mid, list_id, count=5)
            for v in videos:
                if v["cover"]:
                    contents.append(ImageContent(url=v["cover"]))
            stats["videos"] = len(videos)
            content_summary = self._format_video_summary(videos)
            extra["list_id"] = list_id
            extra["video_count"] = len(videos)

        # 构建最终结果
        page_titles = {
            "main": "的主页", "dynamic": "的动态", "video": "的视频",
            "opus": "的图文", "audio": "的音频", "list": "的合集",
        }

        return self.result(
            title=f"{user_data['name']} {page_titles.get(page_type, '的主页')}",
            text=self._build_space_text(user_data, content_summary),
            author=author,
            contents=contents,
            url=url or f"https://space.bilibili.com/{mid}",
            stats=stats or None,
            extra=extra,
            send_groups=[SendGroup(render_card=True)],
        )

    async def parse_favlist(self, fav_id: int):
        """解析收藏夹信息

        Args:
            fav_id (int): 收藏夹 id

        Returns:
            list[GraphicsContent]: 图文内容列表
        """
        from bilibili_api.favorite_list import get_video_favorite_list_content

        from .favlist import FavData

        # 只会取一页，20 个
        fav_dict = await get_video_favorite_list_content(fav_id)

        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")

        favdata = convert(fav_dict, FavData)

        # 统计
        stats: dict[str, int] = {}
        media_count = favdata.info.media_count if hasattr(favdata.info, "media_count") else len(favdata.medias)
        if media_count:
            stats["views"] = media_count

        # 粉丝数和签名（并发获取）
        follower_count, user_sign = None, None
        if favdata.info.upper.mid:
            follower_count, user_sign = await asyncio.gather(
                self._get_follower_count(favdata.info.upper.mid),
                self._get_user_sign(favdata.info.upper.mid),
                return_exceptions=False,
            )

        # 构建文本摘要
        text_parts = []
        if favdata.info.intro:
            text_parts.append(favdata.info.intro)
        text_parts.append(f"共 {media_count} 个内容")
        for fav in favdata.medias[:5]:
            text_parts.append(f"· {fav.title}")

        return self.result(
            title=favdata.title,
            text="\n".join(text_parts),
            timestamp=favdata.timestamp,
            url=f"https://space.bilibili.com/{favdata.info.upper.mid}/favlist?fid={fav_id}",
            author=self.create_author(
                favdata.info.upper.name, favdata.info.upper.face,
                uid=str(favdata.info.upper.mid),
                description=favdata.info.upper.sign or user_sign or None,
                follower_count=follower_count,
            ),
            contents=[
                self.create_graphics_content(fav.cover, fav.desc)
                for fav in favdata.medias
            ],
            stats=stats or None,
            extra={"handle": f"fav/{fav_id}", "uid": str(favdata.info.upper.mid)},
            send_groups=[SendGroup(render_card=True)],
        )

    async def _get_video(
        self, *, bvid: str | None = None, avid: int | None = None
    ) -> Video:
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
        """
        if avid:
            return Video(aid=avid, credential=await self.login.credential)
        elif bvid:
            return Video(bvid=bvid, credential=await self.login.credential)
        else:
            raise ParseException("avid 和 bvid 至少指定一项")

    async def extract_download_urls(
        self,
        video: Video | None = None,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_index: int = 0,
    ) -> tuple[str, str | None]:
        """解析视频下载链接

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_index (int): 页索引 = 页码 - 1
        """

        from bilibili_api.video import (
            AudioStreamDownloadURL,
            VideoDownloadURLDataDetecter,
            VideoStreamDownloadURL,
        )

        if video is None:
            video = await self._get_video(bvid=bvid, avid=avid)

        # 获取下载数据
        download_url_data = await video.get_download_url(page_index=page_index)
        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=self.video_quality,
            codecs=[self.video_codecs],
            no_dolby_video=True,
            no_hdr=True,
        )
        video_stream = streams[0]
        if not isinstance(video_stream, VideoStreamDownloadURL):
            raise DownloadException("未找到可下载的视频流")
        logger.debug(
            f"视频流质量: {video_stream.video_quality.name}, 编码: {video_stream.video_codecs}"
        )

        audio_stream = streams[1]
        if not isinstance(audio_stream, AudioStreamDownloadURL):
            return video_stream.url, None
        logger.debug(f"音频流质量: {audio_stream.audio_quality.name}")
        return video_stream.url, audio_stream.url



