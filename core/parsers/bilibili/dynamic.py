from typing import Any

from msgspec import Struct, convert


class AuthorInfo(Struct):
    """作者信息"""

    name: str
    face: str
    mid: int
    pub_time: str
    pub_ts: int
    sign: str = ""
    # jump_url: str
    # following: bool = False
    # official_verify: dict[str, Any] | None = None
    # vip: dict[str, Any] | None = None
    # pendant: dict[str, Any] | None = None


class VideoArchive(Struct):
    """视频信息"""

    aid: str
    bvid: str
    title: str
    desc: str
    cover: str
    # duration_text: str
    # jump_url: str
    # stat: dict[str, str]
    # badge: dict[str, Any] | None = None


class OpusImage(Struct):
    """图文动态图片信息"""

    url: str
    # width: int
    # height: int
    # size: float
    # aigc: dict[str, Any] | None = None
    # live_url: str | None = None


class OpusSummary(Struct):
    """图文动态摘要"""

    text: str
    # rich_text_nodes: list[dict[str, Any]]


class OpusContent(Struct):
    """图文动态内容"""

    jump_url: str
    pics: list[OpusImage]
    summary: OpusSummary
    title: str | None = None
    # fold_action: list[str] | None = None


class DynamicMajor(Struct):
    """动态主要内容"""

    type: str
    archive: VideoArchive | None = None
    opus: OpusContent | None = None
    live: dict[str, Any] | None = None

    @property
    def title(self) -> str | None:
        """获取标题"""
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.title
        if self.type == "MAJOR_TYPE_LIVE" and self.live:
            return self.live.get("title")
        return None

    @property
    def text(self) -> str | None:
        """获取文本内容（仅从 major 字段提取，用于 content 为空时的回退）"""
        if self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return self.opus.summary.text
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.desc
        if self.type == "MAJOR_TYPE_LIVE" and self.live:
            return self.live.get("title", "")
        return None

    @property
    def image_urls(self) -> list[str]:
        """获取图片URL列表"""
        if self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return [pic.url for pic in self.opus.pics]
        elif self.type == "MAJOR_TYPE_ARCHIVE" and self.archive and self.archive.cover:
            return [self.archive.cover]
        elif self.type == "MAJOR_TYPE_LIVE" and self.live:
            cover = self.live.get("cover")
            return [cover] if cover else []
        return []

    @property
    def cover_url(self) -> str | None:
        """获取封面URL"""
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.cover
        if self.type == "MAJOR_TYPE_LIVE" and self.live:
            return self.live.get("cover")
        return None


class DynamicModule(Struct):
    """动态模块"""

    module_author: AuthorInfo
    module_dynamic: dict[str, Any] | None = None
    module_stat: dict[str, Any] | None = None

    @property
    def author_name(self) -> str:
        """获取作者名称"""
        return self.module_author.name

    @property
    def author_face(self) -> str:
        """获取作者头像URL"""
        return self.module_author.face

    @property
    def pub_ts(self) -> int:
        """获取发布时间戳"""
        return self.module_author.pub_ts

    @property
    def major_info(self) -> dict[str, Any] | None:
        """获取主要内容信息"""
        if self.module_dynamic:
            return self.module_dynamic.get("major")
        return None

    @property
    def content_(self) -> str | None:
        """获取纯文本动态的正文内容（不在 major 字段中）"""
        if self.module_dynamic:
            return self.module_dynamic.get("content")
        return None


class DynamicInfo(Struct):
    """动态信息"""

    id_str: str
    type: str
    visible: bool
    modules: DynamicModule
    basic: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """获取作者名称"""
        return self.modules.author_name

    @property
    def avatar(self) -> str:
        """获取作者头像URL"""
        return self.modules.author_face

    @property
    def timestamp(self) -> int:
        """获取发布时间戳"""
        return self.modules.pub_ts

    @property
    def title(self) -> str | None:
        """获取标题"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.title
        return None

    @property
    def text(self) -> str | None:
        """获取文本内容（多重回退）"""
        # 1. module_dynamic.content（纯文本动态）
        content = self.modules.content_
        if content:
            return content
        # 2. major.opus.summary.text（图文动态）
        major_info = self.modules.major_info
        if major_info:
            try:
                major = convert(major_info, DynamicMajor)
                if major.text:
                    return major.text
            except Exception:
                pass
        # 3. module_dynamic.desc.text（部分动态类型）
        if self.modules.module_dynamic:
            desc = self.modules.module_dynamic.get("desc")
            if isinstance(desc, dict) and desc.get("text"):
                return desc["text"]
            # 4. module_dynamic.description（另一种格式）
            desc2 = self.modules.module_dynamic.get("description")
            if isinstance(desc2, str) and desc2:
                return desc2
            # 5. module_dynamic.dynamic（转发动态的原始文本）
            dynamic_text = self.modules.module_dynamic.get("dynamic")
            if isinstance(dynamic_text, str) and dynamic_text:
                return dynamic_text
        return None

    @property
    def image_urls(self) -> list[str]:
        """获取图片URL列表"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.image_urls
        return []

    @property
    def cover_url(self) -> str | None:
        """获取封面URL"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.cover_url
        return None


class DynamicData(Struct):
    """动态项目"""

    item: DynamicInfo
    orig: DynamicInfo | None = None
