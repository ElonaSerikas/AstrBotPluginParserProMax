# 万能解析器 (astrbot_plugin_parser) v2.0

> `/compact` 后读此文件。优先读「关键文件」和「⚠️ 易错点」。

## 项目定位

将 `astrbot_plugin_bilibili` + `astrbot_plugin_music` 合并进原 `astrbot_plugin_parser`，形成统一链接解析/订阅/点歌插件。

```
用户发链接 → on_message(priority=20) → 正则匹配 → 对应parser解析
                                         → PIL/Playwright渲染卡片 → 发送
                                         → event.stop_event() 阻止其他插件
```

## 核心架构

```
main.py                              ← 入口，3插件合一
core/
├── render.py                        ← PIL渲染器 (三阶回退: HTML→PIL→文本)
├── render_html/                     ← Playwright HTML渲染引擎
│   ├── engine.py                    ← 本地 Chromium 截图渲染
│   ├── bridge.py                    ← ParseResult ↔ RenderPayload 互转
│   ├── models.py                    ← RenderPayload 数据模型
│   └── constants.py                 ← 模板注册表
├── templates/                       ← 6个HTML模板 (B站粉风格)
├── parsers/                         ← 24个解析器
│   ├── bilibili/                    ← B站 (视频/动态/专栏/直播/收藏夹)
│   ├── xhs.py                       ← 小红书
│   ├── youtube.py                   ← YouTube
│   └── ...                          ← 其他平台
├── subscriber/                      ← 多平台订阅系统
├── music/                           ← 点歌系统 (7平台)
├── tools/                           ← LLM函数工具 (番剧/热榜)
├── data.py                          ← ParseResult 数据模型
├── sender.py                        ← 消息发送策略
├── download.py                      ← 下载器 (1GB限+品质选择)
└── config.py                        ← ConfigNode 配置系统
```

## 关键文件

| 文件 | 职责 | 行数 |
|------|------|------|
| `main.py` | 插件入口，所有事件处理器，3插件合并 | ~1300 |
| `core/render.py` | PIL渲染器+三阶回退入口 | ~480 |
| `core/render_html/engine.py` | **本地Playwright** HTML→图片 | ~130 |
| `core/render_html/bridge.py` | ParseResult↔RenderPayload+QR码+消毒 | ~215 |
| `core/sender.py` | 发送策略：卡片/合并转发/文本回退 | ~360 |
| `core/config.py` | ConfigNode 配置系统 | ~420 |
| `core/data.py` | ParseResult + 媒体内容数据模型 | ~310 |
| `core/parsers/base.py` | BaseParser + create_author等工厂方法 | ~230 |
| `core/templates/universal_card.html` | 默认1440px卡片HTML模板 | ~550 |

## 常用命令

```bash
# 运行全部测试
cd ~/.astrbot/data/plugins/astrbot_plugin_parser
python -m pytest tests/ -v

# 语法检查
python -c "import ast; ast.parse(open('main.py',encoding='utf-8').read())"

# 验证配置JSON
python -c "import json; json.load(open('_conf_schema.json',encoding='utf-8'))"

# 安装Playwright (HTML渲染需要)
pip install playwright
python -m playwright install chromium
```

## 渲染器回退链

```
render_card(result)
  ├─ HTML: self.use_html && self.html_renderer
  │   → engine.py: Playwright Chromium截图
  │   ⚠️ Playwright数据必须JSON可序列化 (不能有Task/Path对象)
  ├─ PIL: core/render.py _create_card_image()
  │   → 使用 MisansTC-Regular.ttf 字体
  │   ⚠️ 字体文件必须在 core/resources/ 下
  └─ 纯文本: sender.py _build_text_fallback()
      → 标题+简介+封面图+视频
```

## 事件优先级链

```
priority=20:  解析器 on_message / on_search_song / parse_miniapp
                 ↓ 成功解析 → event.stop_event() → AngelHeart跳过
                 ↓ 无法解析 → 继续传递 → AngelHeart正常响应
priority=10:  AngelHeart smart_reply_handler
priority=0:   其他插件
```

## ⚠️ 易错点

### 1. Image.fromFile 不存在
```python
# ❌ 崩
Image.fromFile(path)
# ✅ 对
Image.fromFileSystem(path)
```

### 2. bridge.py avatar 必须是字符串(不能Task/Path)
```python
# ✅ 安全写法
if result.author and result.author.avatar:
    if isinstance(result.author.avatar, str):
        _avatar = result.author.avatar
```

### 3. create_author 签名(关键字参数)
```python
author = self.create_author(name, avatar, uid=str(mid), description=sig)
```

### 4. HTML模板 card_width 是字符串
bridge 接收 str(如"1440px")，ConfigNode 返回 int。直接传。

### 5. parse_miniapp 必须调 stop_event()
```python
finally:
    event.stop_event()
```

### 6. 新解析器用基类session，不自己建
```python
# ✅ 对
self._session.get(...)
```

### 7. 字体文件名: MisansTC-Regular.ttf (注意大小写)
```python
_FONT_FILENAME = "MisansTC-Regular.ttf"
```

### 8. NapCat超时: 合并消息段数
卡片+文字已合并为一条发送。不要拆成多条。

## 当前已知问题

- **YouTube下载**: 依赖yt-dlp+ffmpeg，视频过大触发送限制
- **封面图HTML渲染**: VideoContent.cover 是Task[Path]非URL，HTML渲染出不来
- **stats/uid/description**: 各解析器填充不一致
- **动态正文(result.text)**: 解析器填充不一致
- **小红书短链重定向**: xhslink.com 需要跟随重定向

## 关键数据模型

```
ParseResult:
  platform: Platform     # name + display_name
  author: Author | None  # name + avatar(Path|Task) + uid + description + follower_count
  title: str | None
  text: str | None       # 正文
  stats: dict            # views/likes/coins/favorites/comments/reposts/danmaku
  contents: list[MediaContent]  # Image/Video/Audio/...
  repost: ParseResult | None
  comments: list[Comment]
  pinned_comment: Comment | None
  timestamp: int | None
  url: str | None
```

## 测试

```bash
python -m pytest tests/ -q        # 全部(78项)
python -m pytest tests/test_bridge.py -v  # 核心数据流
```
