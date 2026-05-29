import asyncio
from re import Match
from typing import ClassVar

from bilibili_api import request_settings, select_client
from bilibili_api.opus import Opus
from bilibili_api.video import Video, VideoCodecs, VideoQuality
from msgspec import convert

from astrbot.api import logger

from ...config import PluginConfig
from ...data import Comment, ImageContent, MediaContent, Platform
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
            # UP主 / 字幕组
            up_info = detail.get("up_info", {})
            up_name = up_info.get("uname", "")
            up_avatar = up_info.get("avatar", "")
            up_mid = up_info.get("mid", "")

            author = self.create_author(
                up_name or "bilibili",
                avatar=up_avatar or None,
                uid=str(up_mid) if up_mid else None,
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

            display_title = f"{season_title}"
            if ep_title:
                display_title += f" - {ep_title}"

            return self.result(
                title=display_title,
                text=desc or None,
                author=author,
                url=url,
                contents=contents,
                stats=stats or None,
                extra={
                    "ep_id": ep_id,
                    "season_id": season_info.get("season_id", ""),
                    "type": "bangumi",
                },
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
            bangumi = Bangumi(season_id=season_id, credential=self.login._credential)
            detail = await bangumi.get_meta()

            season_title = detail.get("title", "")
            evaluate = detail.get("evaluate", "")
            cover = detail.get("cover", "")

            # UP主信息
            up_info = detail.get("up_info", {})
            up_name = up_info.get("uname", "")
            up_avatar = up_info.get("avatar", "")
            up_mid = up_info.get("mid", "")

            author = self.create_author(
                up_name or "bilibili",
                avatar=up_avatar or None,
                uid=str(up_mid) if up_mid else None,
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
                },
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
        # up
        author = self.create_author(
            video_info.owner.name,
            video_info.owner.face,
            uid=str(video_info.owner.mid) if hasattr(video_info.owner, 'mid') else None,
            description=video_info.owner.sign or None,
        )
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

        # 并发获取粉丝数和置顶评论（非阻塞，失败不影响主流程）
        follower_count, pinned_comment, hot_comment = None, None, None
        tasks = []
        if video_info.owner.mid:
            tasks.append(self._get_follower_count(video_info.owner.mid))
        if video_info.aid:
            tasks.append(self._get_pinned_comment(video_info.aid))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站] 辅助 API 调用失败: {r}")
                elif isinstance(r, int):
                    follower_count = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r
        if follower_count is not None:
            author.follower_count = follower_count

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra={"info": ai_summary, "handle": f"av{video_info.aid}", "bvid": video_info.bvid},
            stats=stats,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
        )

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

        # 并发获取粉丝数和置顶评论（非阻塞，失败不影响主流程）
        follower_count, pinned_comment, hot_comment = None, None, None
        aux_tasks = []
        if mid:
            aux_tasks.append(self._get_follower_count(mid))
        # 动态评论 oid = dynamic_id
        from bilibili_api.comment import CommentResourceType as _CRT
        aux_tasks.append(self._get_pinned_comment(dynamic_id, type_=_CRT.DYNAMIC))
        if aux_tasks:
            aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
            for r in aux_results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站动态] 辅助 API 调用失败: {r}")
                elif isinstance(r, int):
                    follower_count = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r
        if follower_count is not None:
            author.follower_count = follower_count

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

            # 原动态作者信息
            orig_mid = orig.modules.module_author.mid
            orig_follower = await self._get_follower_count(orig_mid) if orig_mid else None
            orig_author = self.create_author(
                orig.name, orig.avatar,
                uid=str(orig_mid),
                description=orig.modules.module_author.sign or None,
                follower_count=orig_follower,
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

            # 尝试从 module_dynamic 直接提取文本（防止属性访问静默返回 None）
            orig_text = orig.text
            orig_title = orig.title
            if not orig_text and orig.modules.module_dynamic:
                # 回退：直接从 raw dict 提取
                orig_text = orig.modules.module_dynamic.get("desc", {}).get("text") if isinstance(orig.modules.module_dynamic.get("desc"), dict) else None
            logger.debug(f"[B站动态] orig_title={orig_title}, orig_text={orig_text}, orig_contents={len(orig_contents)}")
            repost = self.result(
                title=orig_title,
                text=orig_text,
                author=orig_author,
                contents=orig_contents,
                timestamp=orig.timestamp,
                url=f"https://www.bilibili.com/dynamic/{orig.id_str}",
                stats=orig_stats or None,
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

        # 专属ID: 动态用 t{id}，图文用 opus/{id}
        dyn_type = dynamic_info.type if hasattr(dynamic_info, 'type') else ""
        if "OPUS" in str(dyn_type).upper():
            handle = f"opus/{dynamic_id}"
        else:
            handle = f"t{dynamic_id}"

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
            extra={"handle": handle},
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

        # 并发获取粉丝数和置顶评论
        follower_count, pinned_comment, hot_comment = None, None, None
        aux_tasks = []
        if mid:
            aux_tasks.append(self._get_follower_count(mid))
        if opus_id:
            from bilibili_api.comment import CommentResourceType as _CRT
            aux_tasks.append(self._get_pinned_comment(opus_id, type_=comment_type or _CRT.DYNAMIC))
        if aux_tasks:
            aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
            for r in aux_results:
                if isinstance(r, Exception):
                    logger.debug(f"[B站图文] 辅助 API 调用失败: {r}")
                elif isinstance(r, int):
                    follower_count = r
                elif isinstance(r, tuple) and len(r) == 2:
                    pinned_comment, hot_comment = r

        author = self.create_author(
            *opus_data.name_avatar,
            uid=opus_data.uid,
            description=opus_data.author_sign,
            follower_count=follower_count,
        )
        # 按顺序处理图文内容（参考 parse_read 的逻辑）
        contents: list[MediaContent] = []
        first_image_url = None
        current_text = ""
        for node in opus_data.gen_text_img():
            if isinstance(node, ImageNode):
                if first_image_url is None:
                    first_image_url = node.url
                contents.append(
                    self.create_graphics_content(
                        node.url, current_text.strip(), node.alt
                    )
                )
                current_text = ""
            elif isinstance(node, TextNode):
                current_text += node.text

        # 图文封面图: 用第一张图片
        extra: dict = {"handle": f"opus/{opus_id}"} if opus_id else {}
        if first_image_url:
            extra["cover_url"] = first_image_url

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
        )

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
            anchor_info = info_dict.get("anchor_info", {})
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

        # 并发获取主播信息（签名 + 粉丝数）
        uid = room_data.uid
        description, follower_count = None, None
        if uid:
            try:
                from bilibili_api import user as bili_user
                u = bili_user.User(uid, credential=self.login._credential)
                user_info = await u.get_user_info()
                description = user_info.get("sign") or None
            except Exception as e:
                logger.debug(f"[B站直播] 获取主播信息失败(uid={uid}): {e}")
            follower_count = await self._get_follower_count(uid)

        author = self.create_author(
            room_data.name, room_data.avatar,
            uid=str(uid),
            description=description,
            follower_count=follower_count,
        )

        stats: dict[str, int] = {}
        if room_data.online:
            stats["views"] = room_data.online

        # 直播状态判断
        is_living = room_data.live_time and room_data.live_time > 0
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

        detail_text = room_data.detail
        if not is_living:
            detail_text = f"{live_status}\n{detail_text}"
            if playback_url:
                detail_text += f"\n回放: {playback_url}"

        url = f"https://live.bilibili.com/{room_id}"
        return self.result(
            url=url,
            title=room_data.title,
            text=detail_text,
            contents=contents,
            author=author,
            timestamp=room_data.live_time or None,
            stats=stats or None,
            extra={"handle": f"live:{room_id}", "uid": str(uid), "is_living": is_living} if uid else {"handle": f"live:{room_id}", "is_living": is_living},
        )

    # space.bilibili.com/{mid}、space.bilibili.com/{mid}/dynamic、space.bilibili.com/{mid}/upload/video
    @handle("space.bilibili.com", r"space\.bilibili\.com/(?P<mid>\d+)(?:/(?:dynamic|upload/video))?(?:\?.*)?$")
    async def _parse_space(self, searched: Match[str]):
        """解析 B站用户主页"""
        mid = int(searched.group("mid"))
        return await self.parse_space(mid)

    async def parse_space(self, mid: int):
        """解析 B站用户主页信息"""
        import aiohttp

        # 并发获取用户信息和动态
        user_url = f"https://api.bilibili.com/x/web-interface/card?mid={mid}&photo=false"
        dynamic_url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={mid}&offset=&timezone_offset=-480"

        async with aiohttp.ClientSession() as session:
            user_task = session.get(user_url, headers=self.headers)
            dynamic_task = session.get(dynamic_url, headers=self.headers)
            user_resp, dynamic_resp = await asyncio.gather(user_task, dynamic_task)

            user_data = await user_resp.json()
            dynamic_data = await dynamic_resp.json()

        if user_data.get("code") != 0:
            raise ParseException(f"B站用户信息获取失败: {user_data.get('message', '')}")

        card = user_data.get("data", {}).get("card", {})
        name = card.get("name", "")
        face = card.get("face", "")
        sign = card.get("sign", "")
        fans = card.get("fans", 0)
        mid_str = str(mid)

        # 获取粉丝数
        follower_count = None
        if fans:
            follower_count = fans

        # 构建作者
        author = self.create_author(
            name, face,
            uid=mid_str,
            description=sign or None,
            follower_count=follower_count,
        )

        # 解析最近动态
        contents = []
        items = dynamic_data.get("data", {}).get("items", [])
        for item in items[:5]:
            modules = item.get("modules", {})
            dynamic_author = modules.get("module_author", {})
            dynamic_desc = modules.get("module_dynamic", {})
            major = dynamic_desc.get("major", {})

            # 动态文本
            desc_text = dynamic_desc.get("desc", {}).get("text", "")
            if desc_text:
                from ..data import TextContent
                contents.append(TextContent(desc_text[:200]))

            # 动态图片
            draw = major.get("draw", {})
            if draw:
                imgs = draw.get("items", [])
                for img in imgs[:3]:
                    img_url = img.get("src", "")
                    if img_url:
                        contents.extend(self.create_image_contents([img_url]))

            # 动态视频
            archive = major.get("archive", {})
            if archive:
                cover = archive.get("cover", "")
                if cover:
                    contents.extend(self.create_image_contents([cover]))

        # 统计数据
        stats = {}
        like = user_data.get("data", {}).get("like_num", 0)
        if like:
            stats["likes"] = like

        # 动态数量
        dynamic_count = len(items)

        return self.result(
            title=f"{name} 的主页",
            text=f"{sign}" if sign else f"{name} 的 B站主页",
            author=author,
            contents=contents,
            url=f"https://space.bilibili.com/{mid}",
            stats=stats or None,
            extra={
                "handle": f"UID:{mid}",
                "uid": mid_str,
                "dynamic_count": dynamic_count,
            },
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

        # 粉丝数
        follower_count = None
        if favdata.info.upper.mid:
            follower_count = await self._get_follower_count(favdata.info.upper.mid)

        return self.result(
            title=favdata.title,
            text=favdata.info.intro or None,
            timestamp=favdata.timestamp,
            url=f"https://space.bilibili.com/{favdata.info.upper.mid}/favlist?fid={fav_id}",
            author=self.create_author(
                favdata.info.upper.name, favdata.info.upper.face,
                uid=str(favdata.info.upper.mid),
                description=favdata.info.upper.sign or None,
                follower_count=follower_count,
            ),
            contents=[
                self.create_graphics_content(fav.cover, fav.desc)
                for fav in favdata.medias
            ],
            stats=stats or None,
            extra={"handle": f"fav/{fav_id}", "uid": str(favdata.info.upper.mid)},
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



