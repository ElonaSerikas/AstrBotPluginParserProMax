from re import Match, sub
from time import time
from typing import ClassVar
from uuid import uuid4

import msgspec
from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag
from msgspec import Struct

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Comment, MediaContent
from ..download import Downloader
from .base import BaseParser, ParseException, Platform, handle

from astrbot.api import logger


class WeiBoParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="weibo", display_name="微博")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.weibo
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9"
                ),
                "referer": "https://weibo.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/148.0.7778.179 Safari/537.36"
                ),
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="weibo.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    # https://weibo.com/tv/show/1034:5007449447661594?mid=5007452630158934
    @handle("weibo.com/tv", r"weibo\.com/tv/show/\d{4}:\d+\?mid=(?P<mid>\d+)(?:&.*)?$")
    async def _parse_weibo_tv(self, searched: Match[str]):
        mid = str(searched.group("mid"))
        weibo_id = self._mid2id(mid)
        return await self.parse_weibo_id(weibo_id)

    # https://video.weibo.com/show?fid=1034:5145615399845897
    @handle("video.weibo", r"video\.weibo\.com/show\?fid=(?P<fid>\d+:\d+)(?:&.*)?$")
    async def _parse_video_weibo(self, searched: Match[str]):
        fid = str(searched.group("fid"))
        url = f"https://{searched.group(0)}"
        return await self.parse_fid(fid, url=url)

    # https://m.weibo.cn/status/5234367615996775
    # https://m.weibo.cn/detail/4976424138313924
    # https://m.weibo.cn/{uid}\d+/{wid}[0-9a-zA-Z]+/qq
    @handle("m.weibo.cn", r"weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9a-zA-Z]+)\/?(?:\?.*)?$")
    # https://weibo.com/7207262816/P5kWdcfDe
    @handle("weibo.com", r"weibo\.com/\d+/(?P<wid>[0-9a-zA-Z]+)\/?(?:\?.*)?$")
    async def _parse_m_weibo_cn(self, searched: Match[str]):
        wid = str(searched.group("wid"))
        return await self.parse_weibo_id(wid)

    # https://mapp.api.weibo.cn/fx/233911ddcc6bffea835a55e725fb0ebc.html
    @handle("mapp.api.weibo", r"mapp\.api\.weibo\.cn/fx/[A-Za-z\d]+\.html\/?(?:\?.*)?$")
    async def _parse_mapp_api_weibo(self, searched: Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://weibo.com/u/5783002944
    @handle("weibo.com/u", r"weibo\.com/u/(?P<uid>\d+)(?:\?.*)?$")
    async def _parse_weibo_user(self, searched: Match[str]):
        """解析微博用户主页"""
        uid = searched.group("uid")
        return await self.parse_weibo_user(uid)

    async def parse_weibo_user(self, uid: str):
        """解析微博用户信息"""
        url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        if self.cookiejar.cookies_str:
            headers["Cookie"] = self.cookiejar.cookies_str

        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise ParseException(f"微博用户信息获取失败 HTTP {resp.status}")
            data = await resp.json()

        user_info = data.get("data", {}).get("userInfo", {})
        if not user_info:
            raise ParseException("微博用户信息为空")

        name = user_info.get("screen_name", "")
        avatar = user_info.get("profile_image_url", "")
        description = user_info.get("description", "")
        followers_count = user_info.get("followers_count", 0)
        follow_count = user_info.get("follow_count", 0)
        statuses_count = user_info.get("statuses_count", 0)

        # 获取最近微博
        container_id = user_info.get("tabsInfo", {}).get("tabs", [{}])[0].get("containerid", "")
        contents = []
        if container_id:
            try:
                timeline_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}"
                async with self.session.get(timeline_url, headers=headers) as resp:
                    if resp.status == 200:
                        timeline_data = await resp.json()
                        cards = timeline_data.get("data", {}).get("cards", [])
                        for card in cards[:3]:
                            mblog = card.get("mblog", {})
                            if mblog:
                                text = mblog.get("text", "")
                                if text:
                                    from ..data import TextContent
                                    import re as _re
                                    clean_text = _re.sub(r"<[^>]*>", "", text)
                                    contents.append(TextContent(clean_text[:200]))
                                pics = mblog.get("pics", [])
                                for pic in pics[:2]:
                                    pic_url = pic.get("large", {}).get("url", "")
                                    if pic_url:
                                        contents.extend(self.create_image_contents([pic_url]))
            except Exception:
                pass

        author = self.create_author(
            name, avatar,
            uid=uid,
            description=description or None,
            follower_count=followers_count or None,
        )

        stats = {}
        if statuses_count:
            stats["posts"] = statuses_count
        if follow_count:
            stats["following"] = follow_count

        return self.result(
            title=f"{name} 的微博",
            text=description if description else f"{name} 的微博主页",
            author=author,
            contents=contents,
            url=f"https://weibo.com/u/{uid}",
            stats=stats or None,
            extra={
                "handle": f"@{name}",
                "uid": uid,
            },
        )

    # https://weibo.com/ttarticle/p/show?id=2309404962180771742222
    # https://weibo.com/ttarticle/x/m/show#/id=2309404962180771742222
    @handle("weibo.com/ttarticle", r"id=(?P<id>\d+)")
    # https://card.weibo.com/article/m/show/id/2309404962180771742222
    @handle("weibo.com/article", r"/id/(?P<id>\d+)")
    async def _parse_article(self, searched: Match[str]):
        _id = searched.group("id")
        return await self.parse_article(_id)

    async def parse_article(self, _id: str):
        class UserInfo(Struct):
            screen_name: str
            profile_image_url: str

        class Data(Struct):
            url: str
            title: str
            content: str
            userinfo: UserInfo
            create_at_unix: int

        class Detail(Struct):
            code: str
            msg: str
            data: Data

        url = "https://card.weibo.com/article/m/aj/detail"
        params = {
            "_rid": str(uuid4()),
            "id": _id,
            "_t": int(time() * 1000),
        }

        async with self.session.post(
            url=url,
            data=params,
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"article API {resp.status} {resp.reason}")
            detail = msgspec.json.decode(await resp.read(), type=Detail)

        if detail.msg != "success":
            raise ParseException("请求失败")

        data = detail.data

        soup = BeautifulSoup(data.content, "html.parser")
        contents: list[MediaContent] = []
        text_buffer: list[str] = []

        for element in soup.find_all(["p", "img"]):
            if not isinstance(element, Tag):
                continue

            if element.name == "p":
                text = element.get_text(strip=True)
                # 去除零宽空格
                text = text.replace("\u200b", "")
                if text:
                    text_buffer.append(text)
            elif element.name == "img":
                src = element.get("src")
                if isinstance(src, str):
                    text = "\n\n".join(text_buffer)
                    contents.append(self.create_graphics_content(src, text=text))
                    text_buffer.clear()

        # 尝试获取用户额外信息
        uid = getattr(data.userinfo, 'id', None) or getattr(data.userinfo, 'uid', None)
        description = getattr(data.userinfo, 'description', None) or getattr(data.userinfo, 'remark', None)
        follower_count = getattr(data.userinfo, 'followers_count', None) or getattr(data.userinfo, 'fans_count', None)

        author = self.create_author(
            data.userinfo.screen_name,
            data.userinfo.profile_image_url,
            uid=str(uid) if uid else None,
            description=description,
            follower_count=follower_count,
        )

        end_text = "\n\n".join(text_buffer) if text_buffer else None

        return self.result(
            url=data.url,
            title=data.title,
            author=author,
            timestamp=data.create_at_unix,
            text=end_text,
            contents=contents,
            extra={"article_id": _id, "post_id": _id},
            page_type="article",
        )

    async def parse_fid(self, fid: str, url: str | None = None):
        """
        解析带 fid 的微博视频
        """

        req_url = f"https://h5.video.weibo.com/api/component?page=/show/{fid}"
        headers = {
            "Referer": f"https://h5.video.weibo.com/show/{fid}",
            "Content-Type": "application/x-www-form-urlencoded",
            **self.headers,
        }
        post_content = 'data={"Component_Play_Playinfo":{"oid":"' + fid + '"}}'

        async with self.session.post(
            req_url,
            data=post_content,
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"video API {resp.status} {resp.reason}")
            json_data = await resp.json()

        data = json_data.get("data", {}).get("Component_Play_Playinfo", {})
        if not data:
            raise ParseException("Component_Play_Playinfo 数据为空")
        # 提取作者
        user = data.get("reward", {}).get("user", {})
        author_name = user.get("name", "未知")
        avatar = user.get("profile_image_url")
        # 头像回退
        if not avatar:
            for key in ("profile_image_url_https", "avatar_url", "avatar_hd"):
                avatar = user.get(key)
                if avatar:
                    break
        description = user.get("description")
        uid = user.get("id") or user.get("uid")
        follower_count = user.get("followers_count") or user.get("fans_count")
        author = self.create_author(
            author_name, avatar,
            uid=str(uid) if uid else None,
            description=description,
            follower_count=follower_count,
        )

        # 提取标题和文本
        title, text = data.get("title", ""), data.get("text", "")
        if text:
            text = sub(r"<[^>]*>", "", text)
            text = text.replace("\n\n", "").strip()

        # 获取封面
        cover_url = data.get("cover_image")
        if cover_url:
            cover_url = "https:" + cover_url

        # 获取视频下载链接
        contents = []
        video_url_dict = data.get("urls")
        if video_url_dict and isinstance(video_url_dict, dict):
            # stream_url码率最低，urls中第一条码率最高
            first_mp4_url: str = next(iter(video_url_dict.values()))
            video_url = "https:" + first_mp4_url
        else:
            video_url = data.get("stream_url")

        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 时间戳
        timestamp = data.get("real_date")

        return self.result(
            title=title,
            text=text,
            author=author,
            url=url,
            contents=contents,
            timestamp=timestamp,
            extra={"fid": fid, "post_id": fid},
            page_type="video",
        )

    async def parse_weibo_id(self, weibo_id: str):
        """解析微博 id"""
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": f"https://m.weibo.cn/detail/{weibo_id}",
            "origin": "https://m.weibo.cn",
            "x-requested-with": "XMLHttpRequest",
            "mweibo-pwa": "1",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            **self.headers,
        }
        # 发送 cookie 以避免被风控
        if self.cookiejar.cookies_str:
            headers["cookie"] = self.cookiejar.cookies_str

        # 加时间戳参数，减少被缓存/规则命中的概率
        ts = int(time() * 1000)
        url = f"https://m.weibo.cn/statuses/show?id={weibo_id}&_={ts}"

        # 关键：不带 cookie、不跟随重定向（避免二跳携 cookie）
        try:
            async with self.session.get(
                url=url,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    if resp.status in (403, 418):
                        raise ParseException(
                            f"被风控拦截（{resp.status}），可尝试更换 UA/Referer 或稍后重试"
                        )
                    raise ParseException(f"获取数据失败 {resp.status} {resp.reason}")

                ctype = resp.headers.get("content-type", "")
                if "application/json" not in ctype:
                    raise ParseException(
                        f"获取数据失败 content-type is not application/json (got: {ctype})"
                    )

                # 用 bytes 更稳，避免编码歧义
                # 必须在 async with 块内读取，否则连接已释放会导致 Connection closed
                raw = await resp.read()
        except ParseException:
            raise
        except Exception as e:
            logger.warning(f"[微博] 网络请求失败(weibo_id={weibo_id}): {type(e).__name__}: {e}")
            raise ParseException(f"微博请求失败: {type(e).__name__}") from e

        weibo_data = msgspec.json.decode(raw, type=WeiboResponse).data

        # 保留 raw dict 用于回退字段提取
        try:
            raw_json = msgspec.json.decode(raw)
        except Exception:
            raw_json = None

        # 如果 pics 为空，尝试从 raw JSON 中用 pic_urls 回退
        if not weibo_data.pics and isinstance(raw_json, dict):
            try:
                raw_data = raw_json.get("data", {})
                pic_urls = raw_data.get("pic_urls")
                if isinstance(pic_urls, list) and pic_urls:
                    pics = []
                    for p in pic_urls:
                        url = p.get("url", "")
                        if url:
                            # 将缩略图 URL 转换为大图 URL
                            # 微博缩略图: /thumb150/ 或 /orj360/ 等 → 大图: /large/
                            large_url = re.sub(r"/(thumb\d+|orj\d+|mw\d+)/", "/large/", url)
                            pics.append(Pic(url=url, large=LargeInPic(url=large_url)))
                    if pics:
                        weibo_data.pics = pics
                        logger.debug(f"[微博] 从 pic_urls 补充了 {len(pics)} 张图片")
            except Exception:
                pass

        # 获取热门评论（非阻塞，失败不影响主流程）
        pinned_comment, hot_comment = await self._get_weibo_comments(weibo_id)

        return self.build_weibo_data(weibo_data, pinned_comment=pinned_comment, hot_comment=hot_comment, raw_json=raw_json)

    async def _get_weibo_comments(self, weibo_id: str) -> tuple[Comment | None, Comment | None]:
        """获取微博置顶评论和热评（失败返回 (None, None)）"""
        try:
            headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
            if self.cookiejar.cookies_str:
                headers["cookie"] = self.cookiejar.cookies_str
            url = f"https://m.weibo.cn/comments/hotflow?id={weibo_id}&mid={weibo_id}&max_id_type=0"
            async with self.session.get(url=url, headers=headers, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None, None
                raw = await resp.read()
                data = msgspec.json.decode(raw)
                hot_data = data.get("data", {}).get("data") or []
                if not hot_data:
                    return None, None

                def _build_comment(item: dict) -> Comment:
                    return Comment(
                        author_name=item.get("user", {}).get("screen_name", ""),
                        content=sub(r"<[^>]+>", "", item.get("text", "")),
                        author_avatar=item.get("user", {}).get("profile_image_url", ""),
                        likes=item.get("like_count", 0),
                        is_hot=True,
                    )

                pinned_comment = None
                hot_comment = None
                for c in hot_data:
                    if c.get("isLikedByMblogAuthor") or c.get("is_hot"):
                        if not pinned_comment:
                            pinned_comment = _build_comment(c)
                        elif not hot_comment:
                            hot_comment = _build_comment(c)
                    elif not hot_comment:
                        hot_comment = _build_comment(c)
                # 如果没有找到标记为热门的，第一条作为 pinned
                if not pinned_comment and not hot_comment:
                    pinned_comment = _build_comment(hot_data[0])
                elif pinned_comment and not hot_comment and len(hot_data) > 1:
                    hot_comment = _build_comment(hot_data[1])

                return pinned_comment, hot_comment
        except Exception as e:
            logger.debug(f"[微博] 获取热门评论失败(weibo_id={weibo_id}): {e}")
            return None, None

    def _extract_raw_user(self, raw_json: dict | None) -> dict:
        """从 raw JSON 中提取 user dict，兼容完整响应和单条微博两种格式"""
        if not raw_json:
            return {}
        # 格式1: 完整API响应 {"ok":1, "data": {"user": {...}}}
        raw_user = raw_json.get("data", {}).get("user", {})
        if isinstance(raw_user, dict) and raw_user:
            return raw_user
        # 格式2: 单条微博/转发微博 {"user": {...}}
        raw_user = raw_json.get("user", {})
        return raw_user if isinstance(raw_user, dict) else {}

    def _extract_raw_data(self, raw_json: dict | None) -> dict:
        """从 raw JSON 中提取 data dict，兼容完整响应和单条微博两种格式"""
        if not raw_json:
            return {}
        # 格式1: 完整API响应
        data = raw_json.get("data", {})
        if isinstance(data, dict) and data:
            return data
        # 格式2: 单条微博本身就是 data
        return raw_json

    def build_weibo_data(self, data: "WeiboData", pinned_comment: Comment | None = None, hot_comment: Comment | None = None, raw_json: dict | None = None):
        logger.debug(f"[Weibo] user.avatar={data.user.profile_image_url!r}, pics_count={len(data.pics) if data.pics else 0}, image_urls={data.image_urls!r}")

        # 回退: 如果头像为空，尝试从 raw JSON 中其他字段获取
        if not data.user.profile_image_url and raw_json:
            raw_user = self._extract_raw_user(raw_json)
            for key in ("profile_image_url_https", "avatar_url", "avatar_hd"):
                val = raw_user.get(key)
                if val:
                    data.user.profile_image_url = str(val)
                    logger.debug(f"[微博] 从 raw user.{key} 补充头像: {val[:80]}")
                    break

        # 回退: 如果 pics 为空，尝试从 raw JSON 的 pic_urls 字段获取
        if not data.pics and raw_json:
            raw_data = self._extract_raw_data(raw_json)
            pic_urls = raw_data.get("pic_urls")
            if isinstance(pic_urls, list) and pic_urls:
                pics = []
                for p in pic_urls:
                    purl = p.get("url", "")
                    if purl:
                        large_url = sub(r"/(thumb\d+|orj\d+|mw\d+)/", "/large/", purl)
                        pics.append(Pic(url=purl, large=LargeInPic(url=large_url)))
                if pics:
                    data.pics = pics
                    logger.debug(f"[微博] 从 pic_urls 补充了 {len(pics)} 张图片")

        contents = []

        # 添加视频内容
        if video_url := data.video_url:
            cover_url = data.cover_url
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if image_urls := data.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        # 粉丝数：整数直接用，字符串原样传（不做近似还原）
        follower_count = None
        if raw_json:
            raw_user = self._extract_raw_user(raw_json)
            if isinstance(raw_user, dict):
                for key in ("followers_count", "fans_count"):
                    val = raw_user.get(key)
                    if isinstance(val, int) and val > 0:
                        follower_count = val
                        break
                if not follower_count:
                    for key in ("followers_count_str", "followers_count", "fans_count"):
                        val = raw_user.get(key)
                        if isinstance(val, str) and val.strip():
                            follower_count = val.strip()
                            break
        if not follower_count and data.user.followers_count:
            val = data.user.followers_count
            if isinstance(val, int) and val > 0:
                follower_count = val
            elif isinstance(val, str) and val.strip():
                follower_count = val.strip()

        # 构建作者
        author = self.create_author(
            data.display_name,
            data.user.profile_image_url,
            uid=str(data.user.id),
            description=data.user.description,
            follower_count=follower_count,
        )

        # 平台专属ID
        extra = {"uid": str(data.user.id), "handle": f"weibo:{data.user.id}", "weibo_id": data.bid, "post_id": data.bid}
        repost = None
        if data.retweeted_status:
            # 提取转发微博的 raw JSON（而非原始微博的）
            raw_repost = None
            if raw_json:
                data_dict = raw_json.get("data") if isinstance(raw_json.get("data"), dict) else raw_json
                raw_repost = data_dict.get("retweeted_status") if isinstance(data_dict.get("retweeted_status"), dict) else None
            repost = self.build_weibo_data(data.retweeted_status, raw_json=raw_repost)

        # 统计数据
        stats = {}
        if data.reposts_count is not None:
            stats["reposts"] = data.reposts_count
        if data.comments_count is not None:
            stats["comments"] = data.comments_count
        if data.attitudes_count is not None:
            stats["likes"] = data.attitudes_count
        if data.reads_count is not None:
            stats["views"] = data.reads_count

        return self.result(
            title=data.title,
            text=data.text_content,
            author=author,
            contents=contents,
            timestamp=data.timestamp,
            url=data.url,
            repost=repost,
            stats=stats,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            page_type="weibo",
        )

    def _base62_encode(self, number: int) -> str:
        """将数字转换为 base62 编码"""
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if number == 0:
            return "0"

        result = ""
        while number > 0:
            result = alphabet[number % 62] + result
            number //= 62

        return result

    def _mid2id(self, mid: str) -> str:
        """将微博 mid 转换为 id"""
        from math import ceil

        mid = str(mid)[::-1]  # 反转输入字符串
        size = ceil(len(mid) / 7)  # 计算每个块的大小
        result = []

        for i in range(size):
            # 对每个块进行处理并反转
            s = mid[i * 7 : (i + 1) * 7][::-1]
            # 将字符串转为整数后进行 base62 编码
            s = self._base62_encode(int(s))
            # 如果不是最后一个块并且长度不足4位，进行左侧补零操作
            if i < size - 1 and len(s) < 4:
                s = "0" * (4 - len(s)) + s
            result.append(s)

        result.reverse()  # 反转结果数组
        return "".join(result)  # 将结果数组连接成字符串


class LargeInPic(Struct):
    url: str


class Pic(Struct):
    url: str
    large: LargeInPic


class Urls(Struct):
    mp4_720p_mp4: str | None = None
    mp4_hd_mp4: str | None = None
    mp4_ld_mp4: str | None = None

    def get_video_url(self) -> str | None:
        return self.mp4_720p_mp4 or self.mp4_hd_mp4 or self.mp4_ld_mp4 or None


class PagePic(Struct):
    url: str


class PageInfo(Struct):
    title: str | None = None
    urls: Urls | None = None
    page_pic: PagePic | None = None


class User(Struct):
    id: int
    screen_name: str
    """用户昵称"""
    profile_image_url: str
    """头像"""
    description: str | None = None
    """用户简介"""
    followers_count: str | int | None = None
    """粉丝数（API可能返回字符串如"1.2万"或整数）"""


class WeiboData(Struct):
    user: User
    text: str
    # source: str  # 如 微博网页版
    # region_name: str | None = None

    bid: str
    created_at: str
    """发布时间 格式: `Thu Oct 02 14:39:33 +0800 2025`"""

    status_title: str | None = None
    pics: list[Pic] | None = None
    page_info: PageInfo | None = None
    retweeted_status: "WeiboData | None" = None  # 转发微博
    reposts_count: int | None = None
    comments_count: int | None = None
    attitudes_count: int | None = None
    reads_count: int | str | None = None

    @property
    def title(self) -> str | None:
        return self.page_info.title if self.page_info else None

    @property
    def display_name(self) -> str:
        return self.user.screen_name

    @property
    def text_content(self) -> str:
        # 将 <br /> 转换为 \n
        text = self.text.replace("<br />", "\n")
        # 去除 html 标签
        text = sub(r"<[^>]*>", "", text)
        return text

    @property
    def cover_url(self) -> str | None:
        if self.page_info is None:
            return None
        if self.page_info.page_pic:
            return self.page_info.page_pic.url
        return None

    @property
    def video_url(self) -> str | None:
        if self.page_info and self.page_info.urls:
            return self.page_info.urls.get_video_url()
        return None

    @property
    def image_urls(self) -> list[str]:
        if self.pics:
            return [x.large.url for x in self.pics]
        return []

    @property
    def url(self) -> str:
        return f"https://weibo.com/{self.user.id}/{self.bid}"

    @property
    def timestamp(self) -> int:
        from time import mktime, strptime

        create_at = strptime(self.created_at, "%a %b %d %H:%M:%S %z %Y")
        return int(mktime(create_at))


class WeiboResponse(Struct):
    ok: int
    data: WeiboData
