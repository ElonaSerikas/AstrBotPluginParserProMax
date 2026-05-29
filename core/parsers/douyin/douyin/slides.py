from random import choice

from msgspec import Struct, field


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    uid: str = ""
    unique_id: str = ""
    short_id: str = ""
    signature: str = ""
    follower_count: int = 0
    avatar_thumb: Avatar = None


class Statistics(Struct):
    digg_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0
    play_count: int = 0


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]
    statistics: Statistics | None = None

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str:
        return choice(self.author.avatar_thumb.url_list)

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images]

    @property
    def dynamic_urls(self) -> list[str]:
        return [choice(image.video.play_addr.url_list) for image in self.images if image.video]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)
