"""冒险者公会好感度卡 (Affinity) 插件。

为麦麦引入一套欢乐向的好感度系统：

- ``/好感度 [@某人|名字]`` 生成并发送一张「冒险者公会卡」图片：
  左侧多维属性雷达图（任意多维，只显示最极端的 5 项）+ 好感度环形仪表，
  右侧头像、QQ 昵称、麦麦给的别名与人物印象简介；
- 麦麦可调用工具给某人某一项加减分 / 设定分值，并以【系统通知】RPG 风格播报；
- 麦麦可调用工具主动发送某人的印象卡片图片（与 /卡片 相同卡面）；
- 麦麦可追加 / 覆盖人物简介；持久化模式下可累积长文，发卡时 LLM 精简卡面显示，超存储上限才写回精简；
- 数值无上下限：高分顶出雷达边界、负分穿过圆心凹陷，越界更好玩；
- 数据存于插件本地 SQLite，按 person_id 主键，跨私聊/群聊共用一份；
- 新用户（库中没有）会结合 PersonInfo 印象与最近聊天，由 LLM 冷启动生成参数与简介。

图片支持 animated_webp / static_webp / apng / png / gif / jpg 多格式：
动图由 Pillow 在底图上合成移动高光帧（render 只出 PNG），
且第 0 帧始终是完整静态卡，保证不支持动图的客户端也能看见全部信息。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import shutil
import sqlite3
import tomllib
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.config import validate_plugin_config
from maibot_sdk.types import ToolParameterInfo, ToolParamType

CURRENT_CONFIG_VERSION = "0.2.1"
SHIPPED_CONFIG_TEMPLATE_NAME = "config.default.toml"
_PLUGIN_ROOT = Path(__file__).resolve().parent
_CARD_FONT_FILES = (
    (400, "NotoSansSC-400.woff2"),
    (700, "NotoSansSC-700.woff2"),
)
_FONT_FACE_CSS_CACHE: str | None = None

# --------------------------------------------------------------------------- #
# 默认值（config.toml 留空 / 不写即跟随这里；插件升级改默认时空字段自动跟随新值）
# --------------------------------------------------------------------------- #

# 中间值 / 一切默认分值都是 5。
DEFAULT_SCORE = 5.0
_SCORE_GUIDANCE_FOR_TOOLS = (
    "各维度与总值的默认中间值是 5；"
    "虽无硬性上下限，但一般会在 0–10 之间浮动，少数情况可故意越界以达成夸张效果。"
    "所有子项均为「越高越好」的正向表述：加分表示认可，扣分表示不满。"
)

# 旧版 key / 标签 → 现行 key（开发期兼容，可选）。
LEGACY_DIMENSION_ALIASES: dict[str, str] = {
    "trouble": "peace",
    "麻烦": "peace",
    "麻烦度": "peace",
    "threat": "safety",
    "威胁": "safety",
    "威胁度": "safety",
    "fun": "abstract",
    "有趣": "abstract",
    "steadiness": "chaos",
    "稳重": "chaos",
    "obedience": "rebellion",
    "听话": "rebellion",
    "反骨": "rebellion",
}
DEFAULT_SCALE_MAX = 10.0
DEFAULT_SCALE_MIN = 0.0
DEFAULT_TOTAL_LABEL = "好感度"
DEFAULT_ALLOW_QUERY_OTHERS = True
DEFAULT_PRUNE_REMOVED_DIMENSIONS = False
DEFAULT_RECENT_MESSAGES_LIMIT = 1024
DEFAULT_RADAR_TOP_N = 5
DEFAULT_STORE_PATH = "data/affinity.sqlite3"

DEFAULT_CARD_TITLE = "{bot_name}的印象档案"
DEFAULT_CARD_TEMPLATE = "assets/parchment.html"

# 出厂默认维度集（共 22 项，对齐大阿尔卡纳数量；可在 config.toml 任意增删改）。
# 标签统一为正向表述：加分=认可，扣分=不满；key 变更见 LEGACY_DIMENSION_ALIASES。
DEFAULT_DIMENSIONS: list[dict[str, str]] = [
    {"key": "familiarity", "label": "熟悉", "description": "认识多久、了解多深"},
    {"key": "trust", "label": "信赖", "description": "麦麦对 ta 的信任程度"},
    {"key": "joy", "label": "欢乐", "description": "带来的快乐与整活贡献"},
    {"key": "peace", "label": "省心", "description": "乖巧省心、少添乱"},
    {"key": "clinginess", "label": "贴贴", "description": "亲密与粘人程度"},
    {"key": "abstract", "label": "抽象", "description": "发言抽象整活、不可名状但乐子足"},
    {"key": "quality", "label": "含金量", "description": "发言的质量与信息含量"},
    {"key": "wavelength", "label": "合拍", "description": "和麦麦聊得来、脑电波契合"},
    {"key": "safety", "label": "安心", "description": "相处让人放心、不觉得有威胁"},
    {"key": "generosity", "label": "慷慨", "description": "投喂、发红包、请客的大方程度"},
    {"key": "intelligence", "label": "智力", "description": "脑子转得快不快"},
    {"key": "charm", "label": "魅力", "description": "讨不讨人喜欢"},
    {"key": "vitality", "label": "活跃", "description": "在群里的活跃度与参与感"},
    {"key": "luck", "label": "幸运", "description": "欧不欧、运气好不好"},
    {"key": "chaos", "label": "混沌", "description": "带来的混沌感与活力、不可预测的乐子"},
    {"key": "chuuni", "label": "中二", "description": "中二戏精浓度（高也可能是萌点）"},
    {"key": "fate", "label": "缘分", "description": "和麦麦的羁绊与宿命契合"},
    {"key": "intuition", "label": "直觉", "description": "读空气、猜中麦麦心思"},
    {"key": "rebellion", "label": "不羁", "description": "自由不羁、有主见不按套路出牌"},
    {"key": "mystery", "label": "神秘", "description": "人设留有悬念、让人想继续了解"},
    {"key": "devotion", "label": "虔诚", "description": "对麦麦或本群的忠诚与向心力"},
    {"key": "hope", "label": "希望", "description": "带来的期待感与正向可能"},
]

# 图片输出。
# 只有当用户头像本身是动图（多帧 gif/webp/apng）时才输出动图——把头像各帧合进卡片；
# 否则输出单张静态图。卡片本身不再有任何合成动画效果。
DEFAULT_STATIC_FORMAT = "webp"  # 静态输出：webp / png / jpg
DEFAULT_ANIMATED_FORMAT = "animated_webp"  # 动图输出：animated_webp / apng / gif
DEFAULT_ANIMATE_WITH_AVATAR = True  # 头像是动图时是否输出动图（false 则永远静态）
DEFAULT_MAX_AVATAR_FRAMES = 30  # 动图头像最多取多少帧（超过则均匀抽样）
DEFAULT_AVATAR_FRAME_FALLBACK_MS = 80  # 头像帧未带时长时的兜底每帧时长
DEFAULT_LOOP = 0  # 0 = 无限循环
DEFAULT_JPG_QUALITY = 90
DEFAULT_WEBP_QUALITY = 90
DEFAULT_BACKGROUND_COLOR = "#12131a"  # 仅 jpg / gif 这类无 alpha 的格式会用到
DEFAULT_SEND_AS_EMOJI = False

# 头像占位色键（chroma key）：动图路径下卡片先用此纯色块占位渲染一次，
# 再用 Pillow 把头像每帧贴进色键区域（形状由模板的圆角/边框决定，天然通用）。
AVATAR_CHROMA_HTML_COLOR = "#ff00ff"

# 人物简介。
DEFAULT_DESCRIPTION_SIZE_LIMIT = 256
DEFAULT_IMPRESSION_NOTE_SIZE_LIMIT = 81920
DEFAULT_PERSISTENT_IMPRESSION = True
DEFAULT_COMPACT_MODEL = "planner"
DEFAULT_COMPACT_TEMPERATURE = 0.4
DEFAULT_COMPACT_MAX_TOKENS = 0  # 0 = 自动按上限计算
DEFAULT_MAX_COMPACT_ATTEMPTS = 3
AUTO_COMPACT_MAX_TOKENS_MULTIPLIER = 8

DEFAULT_COMPACT_PROMPT_TEMPLATE = """你是{nickname}。
你的人格设定：{personality}
你的表达风格：{reply_style}

下面这段你对群友「{name}」的印象简介太长了：当前 {used}，必须精简到 {limit_label} 以内。
请你以{nickname}的身份、用你的口吻重写这段简介，保留最核心的人物特征与你的态度，让它更精炼有趣。
只输出精简后的简介正文本身，不要输出任何解释、前言或额外说明。

当前简介：
{description}"""

# 冷启动 / 刷新印象。
DEFAULT_COLD_START_MODEL = "planner"
DEFAULT_COLD_START_TEMPERATURE = 0.75
DEFAULT_COLD_START_MAX_TOKENS = 0

DEFAULT_COLD_START_PROMPT_TEMPLATE = """你是{nickname}。
{personality}
{reply_style}

你要为群友「{name}」{task_intro}一份「好感度档案」。这是一个欢乐向的设定，请完全以你的视角、按你的喜好与脾气来打分，可以主观、可以毒舌、可以偏心，不必客观中立。

评分维度（每项参考 0-10，5 为中间值；分数越高表示你越认可该项；加分=奖励、扣分=不满。各维度均为正向表述，允许极端越界）：
{dimensions_doc}

同时给一个「{total_label}」总分（同样以 0-10 为参考，可越界）。

关于这个人你已知的信息：
{person_identities}
- 你的印象记忆：{memory_points}
- 最近的聊天记录：
  - 请求中quote后面跟的是消息id，指用户对之前的同一id消息进行了引用。
  - 在聊天记录中，不同的人正在互动，（{nickname}也是一位参与的用户），请注意辨别不同用户的身份。
{recent_chat}
{refresh_guidance}{existing_block}
请只输出一个 JSON 对象（不要输出任何额外文字、解释或代码块标记），格式如下：
{{"total": 数字, "scores": {{{scores_keys_doc}}}, "description": "用你的口吻写的人物简介，{size_limit}字以内"}}"""

DEFAULT_REFRESH_GUIDANCE = """【刷新评估】这是一次全面重算，不是微调旧分。请优先依据「印象记忆」与「最近聊天记录」重新判断你对 ta 的真实感受；下方旧档案仅供参考，各项分数与简介都可以明显上升或下降，不要惯性膨胀，也不要死守先入为主的旧印象。"""

# 系统通知。
DEFAULT_NOTIFY_ENABLED = True
DEFAULT_NOTIFY_DIMENSIONS: list[str] = ["total"]  # ["*"]/["all"] = 所有；[] = 不播报
NOTIFY_ALL_TOKENS = frozenset({"*", "all", "全部"})
DEFAULT_NOTIFY_TEMPLATE = """━━━━━━━━━━━━━━
　【系统通知】数值变动
　对象：{name}
　{dimension}　{delta}
　缘由：{reason}
━━━━━━━━━━━━━━"""

QQ_AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
QQ_COMPATIBLE_PLATFORMS = frozenset({"qq", "qqguild", "napcat"})
AVATAR_TIMEOUT_S = 8.0

VALID_STATIC_FORMATS = frozenset({"webp", "static_webp", "png", "jpg", "jpeg"})
VALID_ANIMATED_FORMATS = frozenset({"animated_webp", "apng", "gif"})

# 卡片渲染视口（实际像素会再乘 device_scale_factor）。
CARD_VIEWPORT = {"width": 940, "height": 500}
CARD_DEVICE_SCALE = 2.0
CARD_RADAR_SVG_SIZE = 400


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _text_byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _text_used(text: str, limit_unit: str) -> str:
    if limit_unit == "bytes":
        return f"{_text_byte_len(text)} 字节"
    return f"{len(text)} 字符"


def _text_within_limit(text: str, limit: int, limit_unit: str) -> bool:
    if limit_unit == "bytes":
        return _text_byte_len(text) <= limit
    return len(text) <= limit


def _limit_label(limit: int, limit_unit: str) -> str:
    return f"{limit} 字节" if limit_unit == "bytes" else f"{limit} 字符"


def _truncate_text(text: str, limit: int, limit_unit: str) -> str:
    if _text_within_limit(text, limit, limit_unit):
        return text
    if limit_unit == "bytes":
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        trimmed = encoded[: max(0, limit - 3)].decode("utf-8", errors="ignore").rstrip()
        return trimmed + "…"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _names_equal(a: str, b: str) -> bool:
    left = str(a or "").strip()
    right = str(b or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    if left.isascii() and right.isascii():
        return left.casefold() == right.casefold()
    return False


def _normalize_person_target(raw: str) -> str:
    """清理工具/命令传入的人物指称（@、括号、别名前缀等）。"""
    text = str(raw or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] in "「『\"'" and text[-1] in "」』\"'":
        text = text[1:-1].strip()
    for prefix in ("别名:", "别名：", "群名片:", "群名片："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text


def _parse_group_cardname_entries(raw: Any) -> list[tuple[str, str]]:
    """从 PersonInfo.group_cardname 解析 (group_id, group_cardname) 列表。"""
    if raw is None:
        return []
    parsed: Any = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [("", raw.strip())]
    entries: list[tuple[str, str]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, Mapping):
                group_id = str(item.get("group_id") or "").strip()
                name = str(item.get("group_cardname") or "").strip()
            else:
                group_id, name = "", str(item or "").strip()
            if name:
                entries.append((group_id, name))
    elif isinstance(parsed, Mapping):
        group_id = str(parsed.get("group_id") or "").strip()
        name = str(parsed.get("group_cardname") or "").strip()
        if name:
            entries.append((group_id, name))
    elif isinstance(parsed, str) and parsed.strip():
        entries.append(("", parsed.strip()))
    return entries


def _platform_user_id_label(platform: str) -> str:
    key = str(platform or "").strip().lower()
    if key in {"qq", "onebot", "napcat", "lagrange"}:
        return "QQ 号"
    if key == "telegram":
        return "Telegram ID"
    if key == "discord":
        return "Discord ID"
    if platform:
        return f"{platform} 用户 ID"
    return "用户 ID"


def _collect_person_identity_items(
    ref: PersonRef,
    *,
    stored_display_name: str = "",
) -> list[tuple[str, str]]:
    """汇总所有可用于指代该群友的身份信息，供冷启动 / 刷新提示词使用。"""
    items: list[tuple[str, str]] = []
    seen_names: set[str] = set()

    def add_name(label: str, value: str) -> None:
        text = str(value or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen_names:
            return
        seen_names.add(key)
        items.append((label, text))

    user_id = str(ref.user_id or "").strip()
    if user_id:
        items.append((_platform_user_id_label(ref.platform), user_id))

    add_name("平台昵称", ref.user_nickname)
    add_name("麦麦起的别名", ref.person_name)
    for group_id, card in ref.group_cardname_entries:
        label = f"群名片（群 {group_id}）" if group_id else "群名片"
        add_name(label, card)
    add_name("印象档案显示名", stored_display_name)
    person_id = str(ref.person_id or "").strip()
    if person_id and person_id != user_id:
        items.append(("内部人物 ID", person_id))
    return items


def _format_person_identities(ref: PersonRef, *, stored_display_name: str = "") -> str:
    items = _collect_person_identity_items(ref, stored_display_name=stored_display_name)
    if not items:
        return f"（除称呼「{ref.display_name}」外暂无更多身份信息）"
    lines = [f"以下昵称、号码、群名片等均指同一人「{ref.display_name}」："]
    lines.extend(f"- {label}：{value}" for label, value in items)
    return "\n".join(lines)


def _person_info_matches_alias(info: Mapping[str, Any], name: str) -> bool:
    if not name:
        return False
    if _names_equal(str(info.get("person_name") or ""), name):
        return True
    if _names_equal(str(info.get("user_nickname") or ""), name):
        return True
    return any(_names_equal(card, name) for card in _parse_group_cardnames(info.get("group_cardname")))


def _param(name: str, param_type: ToolParamType, description: str, required: bool = False) -> ToolParameterInfo:
    """构造工具参数定义（ToolParameterInfo 只接受关键字参数）。"""
    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _render(template: str, **values: Any) -> str:
    """用简单的 ``{key}`` 占位符替换渲染模板（避免 str.format 因花括号炸裂）。"""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _render_card_text_template(template: str, *, bot_name: str, person_name: str) -> str:
    """渲染卡片标题类模板，支持 ``{bot_name}`` / ``{botname}`` / ``{person_name}``。"""
    return (
        template.replace("{bot_name}", bot_name)
        .replace("{botname}", bot_name)
        .replace("{person_name}", person_name)
    )


def _fmt_num(value: float) -> str:
    """好看地格式化分值：整数不带小数，否则保留一位小数。"""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _fmt_delta(value: float) -> str:
    """带符号格式化增量。"""
    sign = "+" if value >= 0 else ""
    return f"{sign}{_fmt_num(value)}"


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_color(value: str, default: tuple[int, int, int, int] = (18, 19, 26, 255)) -> tuple[int, int, int, int]:
    """解析 #RGB / #RRGGBB / #RRGGBBAA 颜色，失败返回默认。"""
    raw = str(value or "").strip().lstrip("#")
    try:
        if len(raw) == 3:
            r, g, b = (int(c * 2, 16) for c in raw)
            return r, g, b, 255
        if len(raw) == 6:
            return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), 255
        if len(raw) == 8:
            return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16)
    except ValueError:
        pass
    return default


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """从可能含多余文字的 LLM 输出中提取第一个 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# SVG 生成（几何在 Python，配色由各 HTML 模板的 CSS 类控制）
# --------------------------------------------------------------------------- #
def _radar_sector_class(v0: float, v1: float, scale_max: float) -> str:
    """按相邻两维分值决定扇区着色类。"""
    if v0 < 0 or v1 < 0:
        return "radar-sector-neg"
    if v0 > scale_max or v1 > scale_max:
        return "radar-sector-over"
    return "radar-sector-pos"


def build_radar_svg(dims: list[tuple[str, float]], scale_max: float, size: int = 360) -> str:
    """生成属性雷达图 SVG。

    数值线性映射半径，0 在圆心、scale_max 在外环；超过 scale_max 顶出外环，
    负数则穿过圆心落到对侧（凹陷），刻意制造越界的趣味。

    相邻维度围成的扇区分别上色（负值标红、超高标亮、其余为主题色），
    再叠加上层轮廓线与顶点。

    使用的 CSS 类（由模板着色）：grid、axis、radar-sector-pos/neg/over、
    radar-stroke、radar-vertex、axis-label、axis-value。
    """
    n = len(dims)
    if n == 0 or scale_max <= 0:
        return ""

    # 几何：外环尽量大、viewBox 贴紧标签边界，避免缩放后四周留白。
    outer = size * 0.39
    label_r = outer + size * 0.044
    text_pad = size * 0.05
    top_pad = text_pad + size * 0.022
    half_w = label_r + text_pad
    half_h = label_r + top_pad
    cx = half_w
    cy = half_h
    view_w = 2 * half_w
    view_h = 2 * half_h

    def angle(i: int) -> float:
        return -math.pi / 2.0 + 2.0 * math.pi * i / n

    def point(value: float, i: int) -> tuple[float, float]:
        r = outer * (value / scale_max)
        a = angle(i)
        return cx + r * math.cos(a), cy + r * math.sin(a)

    parts: list[str] = [
        f'<svg viewBox="0 0 {view_w:.1f} {view_h:.1f}" xmlns="http://www.w3.org/2000/svg" '
        f'overflow="visible" preserveAspectRatio="xMidYMid meet">'
    ]

    # 扇区填色（先于网格绘制，作为底色）
    for i in range(n):
        v0 = dims[i][1]
        v1 = dims[(i + 1) % n][1]
        x0, y0 = point(v0, i)
        x1, y1 = point(v1, (i + 1) % n)
        sector_cls = _radar_sector_class(v0, v1, scale_max)
        parts.append(
            f'<polygon class="{sector_cls}" '
            f'points="{cx:.1f},{cy:.1f} {x0:.1f},{y0:.1f} {x1:.1f},{y1:.1f}" />'
        )

    # 网格环：外环（scale_max）与半环（scale_max/2）
    for ring_value in (scale_max, scale_max / 2.0):
        ring_pts = []
        for i in range(n):
            x, y = point(ring_value, i)
            ring_pts.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polygon class="grid" points="{" ".join(ring_pts)}" />')

    # 轴线
    for i in range(n):
        x, y = point(scale_max, i)
        parts.append(f'<line class="axis" x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" />')

    # 数据轮廓（仅描边，不填充）
    data_pts = []
    for i, (_, value) in enumerate(dims):
        x, y = point(value, i)
        data_pts.append(f"{x:.1f},{y:.1f}")
    parts.append(f'<polygon class="radar-stroke" fill="none" points="{" ".join(data_pts)}" />')

    # 顶点
    for i, (_, value) in enumerate(dims):
        x, y = point(value, i)
        parts.append(f'<circle class="radar-vertex" cx="{x:.1f}" cy="{y:.1f}" r="{size*0.012:.1f}" />')

    # 标签与数值
    for i, (label, value) in enumerate(dims):
        a = angle(i)
        lx = cx + label_r * math.cos(a)
        ly = cy + label_r * math.sin(a)
        anchor = "middle"
        if math.cos(a) > 0.25:
            anchor = "start"
        elif math.cos(a) < -0.25:
            anchor = "end"
        dy = 0.0
        if math.sin(a) < -0.4:
            dy = -2.0
        elif math.sin(a) > 0.4:
            dy = size * 0.012
        parts.append(
            f'<text class="axis-label" x="{lx:.1f}" y="{ly + dy:.1f}" '
            f'text-anchor="{anchor}">{_html_escape(label)} '
            f'<tspan class="axis-value">{_html_escape(_fmt_num(value))}</tspan></text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def build_gauge_bar_svg(
    total: float,
    scale_max: float,
    scale_min: float = 0.0,
    width: int = 640,
    height: int = 64,
) -> str:
    """生成好感度横向量表条 SVG（一条，非环形，可双向越界）。

    ``scale_min..scale_max``（默认 0..10）是标准量谱区间；填充与游标不做夹边，
    可越过条带两端甚至溢出视口（``overflow: visible``）。

    使用的 CSS 类：gauge-bar-track、gauge-bar-fill、gauge-bar-neg、gauge-bar-over、
    gauge-bar-knob。
    """
    if scale_max <= scale_min:
        scale_max = scale_min + DEFAULT_SCALE_MAX
    span = scale_max - scale_min

    x_zero = width * 0.08  # 数值 == scale_min
    x_full = width * 0.92  # 数值 == scale_max
    unit = (x_full - x_zero) / span

    bar_h = height * 0.38
    bar_y = height * 0.31
    cy = bar_y + bar_h / 2.0
    r = bar_h / 2.0

    def coord(value: float) -> float:
        return x_zero + (value - scale_min) * unit

    raw_x = coord(total)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="overflow:visible">'
    ]

    # 标准量谱轨道（仅 0..scale_max，无两侧延伸阴影）
    parts.append(
        f'<rect class="gauge-bar-track" x="{x_zero:.1f}" y="{bar_y:.1f}" '
        f'width="{x_full - x_zero:.1f}" height="{bar_h:.1f}" rx="{r:.1f}" />'
    )

    # 填充（可越过轨道端点）
    if total >= scale_min:
        end_x = raw_x
        if end_x > x_zero:
            fill_end = max(end_x, x_zero)
            parts.append(
                f'<rect class="gauge-bar-fill" x="{x_zero:.1f}" y="{bar_y:.1f}" '
                f'width="{fill_end - x_zero:.1f}" height="{bar_h:.1f}" rx="{r:.1f}" />'
            )
        if total > scale_max and end_x > x_full:
            parts.append(
                f'<rect class="gauge-bar-over" x="{x_full:.1f}" y="{bar_y:.1f}" '
                f'width="{end_x - x_full:.1f}" height="{bar_h:.1f}" rx="{r:.1f}" />'
            )
    elif raw_x < x_zero:
        parts.append(
            f'<rect class="gauge-bar-neg" x="{raw_x:.1f}" y="{bar_y:.1f}" '
            f'width="{x_zero - raw_x:.1f}" height="{bar_h:.1f}" rx="{r:.1f}" />'
        )

    # 数值游标（不夹边）
    parts.append(f'<circle class="gauge-bar-knob" cx="{raw_x:.1f}" cy="{cy:.1f}" r="{bar_h*0.62:.1f}" />')

    parts.append("</svg>")
    return "".join(parts)


def _embedded_font_face_css() -> str:
    """从插件内置 woff2 生成 @font-face（data URI），渲染不依赖外网。"""
    global _FONT_FACE_CSS_CACHE
    if _FONT_FACE_CSS_CACHE is not None:
        return _FONT_FACE_CSS_CACHE

    rules: list[str] = []
    font_dir = _PLUGIN_ROOT / "assets" / "fonts"
    for weight, filename in _CARD_FONT_FILES:
        path = font_dir / filename
        if not path.is_file():
            continue
        b64 = b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            "@font-face{"
            "font-family:'Noto Sans SC';"
            f"font-weight:{weight};font-style:normal;font-display:swap;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');"
            "}"
        )
    _FONT_FACE_CSS_CACHE = f"<style>{''.join(rules)}</style>" if rules else ""
    return _FONT_FACE_CSS_CACHE


def _wrap_card_html_for_render(fragment: str) -> str:
    """包成完整 HTML 文档并注入内置中文字体（Playwright 渲染用，无需外网）。"""
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"{_embedded_font_face_css()}"
        "</head><body>"
        f"{fragment}"
        "</body></html>"
    )


def build_legend_html(dims: list[tuple[str, float]]) -> str:
    """生成雷达图维度图例（类：dim-legend、legend-item、legend-name、legend-val）。"""
    if not dims:
        return ""
    items = []
    for label, value in dims:
        items.append(
            '<div class="legend-item">'
            f'<span class="legend-name">{_html_escape(label)}</span>'
            f'<span class="legend-val">{_html_escape(_fmt_num(value))}</span>'
            "</div>"
        )
    return f'<div class="dim-legend">{"".join(items)}</div>'


# --------------------------------------------------------------------------- #
# 图片处理（render 只出 PNG；动图来源于「头像本身是动图」，否则输出静态单张）
# --------------------------------------------------------------------------- #
def _flatten(img: Any, bg: tuple[int, int, int, int]) -> Any:
    """把 RGBA 图压到不透明背景上，返回 RGB。"""
    from PIL import Image

    background = Image.new("RGBA", img.size, bg[:3] + (255,))
    return Image.alpha_composite(background, img).convert("RGB")


def extract_avatar_frames(
    avatar_bytes: bytes,
    *,
    max_frames: int,
    fallback_duration_ms: int,
) -> tuple[list[Any], list[int], bool]:
    """解析头像，返回 (帧列表 RGBA, 每帧时长 ms, 是否动图)。

    静态头像返回单帧、时长 [0]、is_animated=False；多帧头像超过 max_frames 时均匀抽样。
    解析失败返回 ([], [], False)。
    """
    from PIL import Image

    try:
        img = Image.open(BytesIO(avatar_bytes))
    except Exception:
        return [], [], False

    frame_total = int(getattr(img, "n_frames", 1) or 1)
    animated = bool(getattr(img, "is_animated", False)) and frame_total > 1
    if not animated:
        return [img.convert("RGBA")], [0], False

    all_frames: list[Any] = []
    all_durations: list[int] = []
    for index in range(frame_total):
        try:
            img.seek(index)
        except EOFError:
            break
        all_frames.append(img.convert("RGBA"))
        all_durations.append(int(img.info.get("duration", 0) or fallback_duration_ms))

    if len(all_frames) <= 1:
        return all_frames or [img.convert("RGBA")], [0], False

    if len(all_frames) <= max_frames:
        return all_frames, all_durations, True

    # 帧太多：均匀抽样，并把总时长摊到抽出的帧上
    step = len(all_frames) / max_frames
    picked = [all_frames[int(k * step)] for k in range(max_frames)]
    per = max(20, sum(all_durations) // max_frames)
    return picked, [per] * max_frames, True


def _chroma_mask(base_rgba: Any) -> tuple[Any, Optional[tuple[int, int, int, int]]]:
    """从底图中提取洋红色键区域，返回 (mask L, bbox)。

    mask 值 = 像素的「洋红强度」(min(R,B)-G)，越接近纯洋红越接近 255，
    天然带抗锯齿过渡，贴图时不会有硬边。
    """
    from PIL import ImageChops

    r, g, b, _a = base_rgba.split()
    magenta = ImageChops.subtract(ImageChops.darker(r, b), g)  # clamp 到 [0,255]
    mask = magenta.point(lambda v: 0 if v < 40 else (255 if v > 130 else int((v - 40) * 255 / 90)))
    return mask, mask.getbbox()


def composite_avatar_over_card(base_rgba: Any, avatar_rgba: Any) -> Any:
    """把单帧头像贴进底图的色键洞里（形状由色键区域决定，贴合任意圆角/边框）。"""
    from PIL import Image, ImageOps

    mask, box = _chroma_mask(base_rgba)
    if box is None:
        return base_rgba.copy()
    x0, y0, x1, y1 = box
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    # cover 裁剪到洞的尺寸；先把可能透明的头像压到白底，避免透出洋红
    fitted = ImageOps.fit(avatar_rgba.convert("RGBA"), (w, h), method=Image.LANCZOS)
    fitted = Image.alpha_composite(Image.new("RGBA", (w, h), (255, 255, 255, 255)), fitted)
    layer = Image.new("RGBA", base_rgba.size, (0, 0, 0, 0))
    layer.paste(fitted, (x0, y0))
    return Image.composite(layer, base_rgba, mask)


def _save_static(base_rgba: Any, fmt: str, jpg_quality: int, webp_quality: int, bg: tuple) -> tuple[bytes, str]:
    """保存为静态图，返回 (bytes, mime)。"""
    buf = BytesIO()
    fmt = (fmt or DEFAULT_STATIC_FORMAT).strip().lower()
    if fmt not in VALID_STATIC_FORMATS:
        fmt = DEFAULT_STATIC_FORMAT
    if fmt == "png":
        base_rgba.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    if fmt in ("jpg", "jpeg"):
        _flatten(base_rgba, bg).save(buf, format="JPEG", quality=max(1, min(100, jpg_quality)), optimize=True)
        return buf.getvalue(), "image/jpeg"
    # webp / static_webp
    base_rgba.save(buf, format="WEBP", quality=max(1, min(100, webp_quality)), method=6)
    return buf.getvalue(), "image/webp"


def encode_static_card(
    png_bytes: bytes,
    *,
    static_format: str,
    jpg_quality: int,
    webp_quality: int,
    background_color: str,
) -> tuple[bytes, str]:
    """渲染好的卡片 PNG → 静态目标格式。"""
    from PIL import Image

    base = Image.open(BytesIO(png_bytes)).convert("RGBA")
    return _save_static(base, static_format, jpg_quality, webp_quality, _parse_color(background_color))


def encode_animated_card(
    base_png_bytes: bytes,
    avatar_frames: list[Any],
    durations: list[int],
    *,
    animated_format: str,
    loop: int,
    webp_quality: int,
    background_color: str,
) -> tuple[bytes, str]:
    """底图（头像位置为色键）+ 头像各帧 → 动图。逐帧把头像帧贴进卡片色键区域。"""
    from PIL import Image

    base = Image.open(BytesIO(base_png_bytes)).convert("RGBA")
    bg = _parse_color(background_color)
    frames = [composite_avatar_over_card(base, frame) for frame in avatar_frames]
    if not frames:
        return _save_static(base, DEFAULT_STATIC_FORMAT, DEFAULT_JPG_QUALITY, webp_quality, bg)
    durs = [max(20, int(d) or 80) for d in (durations or [80] * len(frames))]
    if len(durs) < len(frames):
        durs += [durs[-1]] * (len(frames) - len(durs))

    fmt = (animated_format or DEFAULT_ANIMATED_FORMAT).strip().lower()
    if fmt not in VALID_ANIMATED_FORMATS:
        fmt = DEFAULT_ANIMATED_FORMAT
    buf = BytesIO()

    if fmt == "apng":
        frames[0].save(
            buf, format="PNG", save_all=True, append_images=frames[1:],
            duration=durs, loop=max(0, loop), default_image=False,
        )
        return buf.getvalue(), "image/apng"

    if fmt == "gif":
        gif_frames = [_flatten(f, bg).convert("P", palette=Image.ADAPTIVE) for f in frames]
        gif_frames[0].save(
            buf, format="GIF", save_all=True, append_images=gif_frames[1:],
            duration=durs, loop=max(0, loop), disposal=2, optimize=False,
        )
        return buf.getvalue(), "image/gif"

    # animated_webp（默认）
    frames[0].save(
        buf, format="WEBP", save_all=True, append_images=frames[1:],
        duration=durs, loop=max(0, loop), quality=max(1, min(100, webp_quality)), method=4,
    )
    return buf.getvalue(), "image/webp"


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class DimensionConfig(PluginConfigBase):
    """单个好感度子项维度。"""

    key: str = Field(default="", description="维度唯一 key（改 label 不会丢数据，改 key 视为新维度）")
    label: str = Field(default="", description="维度显示名")
    description: str = Field(default="", description="维度含义（仅用于喂给 LLM 打分时参考）")


class GeneralSectionConfig(PluginConfigBase):
    __ui_label__ = "好感度"
    __ui_icon__ = "heart"
    __ui_order__ = 1

    total_label: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_TOTAL_LABEL},
        description="好感度总值的显示名。",
    )
    default_score: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_SCORE)},
        description="一切默认值 / 中间值（未生成、未评分、新增维度都用它）。",
    )
    scale_max: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_SCALE_MAX)},
        description="图表显示刻度上限（仅影响显示比例，实际数值无上下限，可越界）。",
    )
    scale_min: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_SCALE_MIN)},
        description="图表显示刻度下限（仅作参考标注）。",
    )
    allow_query_others: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="是否允许通过 @某人 / 名字 查询他人的卡片（false 则只能查自己）。",
    )
    radar_top_n: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_RADAR_TOP_N)},
        description="雷达图最多显示几项（按偏离中间值最远，即最极端的项优先）。",
    )
    recent_messages_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_RECENT_MESSAGES_LIMIT)},
        description="冷启动 / 刷新印象时参考的最近聊天条数。",
    )
    prune_removed_dimensions: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "false"},
        description="从配置删除某维度时，是否同时从数据库清掉该维度的历史分值（默认保留）。",
    )
    store_path: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_STORE_PATH},
        description="SQLite 数据文件路径；相对路径基于插件目录解析（跨私聊/群聊共用）。",
    )


class CardSectionConfig(PluginConfigBase):
    __ui_label__ = "卡片外观"
    __ui_icon__ = "id-card"
    __ui_order__ = 2

    card_title: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_CARD_TITLE},
        description="卡片顶部居中标题。支持占位符 {bot_name}、{person_name}（渲染时替换）。",
    )
    card_template: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_CARD_TEMPLATE},
        description="卡片 HTML 模板路径，相对插件目录解析。内置 assets/parchment.html（羊皮纸）、assets/holo.html（全息科技）、assets/cute.html（可爱贴纸）；可指向自己的 html 自定义。",
    )


class ImageSectionConfig(PluginConfigBase):
    __ui_label__ = "图片格式"
    __ui_icon__ = "image"
    __ui_order__ = 3

    static_format: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_STATIC_FORMAT},
        description="静态卡片的输出格式：webp / png / jpg。（头像不是动图时用这个。）",
    )
    animated_format: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_ANIMATED_FORMAT},
        description="动图卡片的输出格式：animated_webp / apng / gif。（仅当头像本身是动图时才出动图。）",
    )
    animate_with_avatar: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="头像是动图时是否把卡片也做成动图（false 则永远输出静态、只取头像首帧）。",
    )
    max_avatar_frames: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_MAX_AVATAR_FRAMES)},
        description="动图头像最多取多少帧（超过则均匀抽样，控制体积与耗时）。",
    )
    avatar_frame_fallback_ms: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_AVATAR_FRAME_FALLBACK_MS)},
        description="头像帧本身未带时长时的兜底每帧时长（毫秒）。",
    )
    loop: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LOOP)},
        description="动图循环次数，0 为无限循环。",
    )
    jpg_quality: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_JPG_QUALITY)},
        description="jpg 输出质量（1-100）。",
    )
    webp_quality: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_WEBP_QUALITY)},
        description="webp 输出质量（1-100）。",
    )
    background_color: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_BACKGROUND_COLOR},
        description="jpg / gif 这类无透明通道格式的填充底色（#RRGGBB）。",
    )
    send_as_emoji: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "false"},
        description="是否走「表情」通道发送（部分适配器表情通道动图更稳）；默认走图片通道。",
    )


class DescriptionSectionConfig(PluginConfigBase):
    __ui_label__ = "人物简介"
    __ui_icon__ = "file-text"
    __ui_order__ = 4

    size_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_DESCRIPTION_SIZE_LIMIT)},
        description="卡面印象笔记字符上限；persistent_impression 开启时超出会在发卡时用 LLM 精简显示（不改库）。",
    )
    impression_note_size_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_IMPRESSION_NOTE_SIZE_LIMIT)},
        description="印象笔记存储字节上限（UTF-8）；persistent_impression 开启时超出后强制 LLM 精简并写回数据库。",
    )
    persistent_impression: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description=(
            "持久化印象笔记：库中可累积长文（受 impression_note_size_limit 约束）；"
            "发卡时若超 size_limit 则 LLM 精简卡面显示而不改库。"
        ),
    )
    compact_model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_COMPACT_MODEL},
        description="精简简介使用的 LLM 模型任务名。",
    )
    compact_temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COMPACT_TEMPERATURE)},
        description="精简简介的采样温度。",
    )
    compact_max_tokens: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COMPACT_MAX_TOKENS)},
        description="精简简介的最大 token；0 表示自动按上限估算。",
    )
    max_compact_attempts: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_MAX_COMPACT_ATTEMPTS)},
        description="精简仍超限时的最大重试次数。",
    )
    compact_prompt_template: str = Field(
        default="",
        json_schema_extra={"placeholder": DEFAULT_COMPACT_PROMPT_TEMPLATE},
        description="精简简介的提示词模板。占位符：{nickname}{personality}{reply_style}{name}{used}{limit_label}{description}。",
    )


class ColdStartSectionConfig(PluginConfigBase):
    __ui_label__ = "冷启动/刷新"
    __ui_icon__ = "sparkles"
    __ui_order__ = 5

    model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_COLD_START_MODEL},
        description="冷启动 / 刷新印象使用的 LLM 模型任务名。",
    )
    temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COLD_START_TEMPERATURE)},
        description="冷启动 / 刷新的采样温度（高一点更有个性）。",
    )
    max_tokens: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COLD_START_MAX_TOKENS)},
        description="冷启动 / 刷新的最大 token；0 为自动。",
    )
    prompt_template: str = Field(
        default="",
        json_schema_extra={"placeholder": DEFAULT_COLD_START_PROMPT_TEMPLATE},
        description="冷启动 / 刷新提示词模板。占位符：{nickname}{personality}{reply_style}{name}{task_intro}{total_label}{dimensions_doc}{scores_keys_doc}{person_identities}{user_nickname}{group_cardname}{memory_points}{recent_chat}{refresh_guidance}{existing_block}{size_limit}。",
    )
    refresh_guidance: str = Field(
        default="",
        json_schema_extra={"placeholder": DEFAULT_REFRESH_GUIDANCE},
        description="仅刷新印象时插入的评估指引；留空则用内置默认。",
    )
    refresh_temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COLD_START_TEMPERATURE)},
        description="仅刷新印象时使用的采样温度；留空则沿用 temperature。",
    )


class NotifySectionConfig(PluginConfigBase):
    __ui_label__ = "系统通知"
    __ui_icon__ = "bell"
    __ui_order__ = 6

    enabled: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="加减分时是否发送【系统通知】消息。",
    )
    notify_dimensions: list[str] | None = Field(
        default=None,
        json_schema_extra={"placeholder": '["total"]'},
        description='播报哪些维度变化：["total"] 只播报好感度；["*"] 播报所有；[] 不播报。维度用 key 或 total。',
    )
    notify_template: str = Field(
        default="",
        json_schema_extra={"placeholder": DEFAULT_NOTIFY_TEMPLATE},
        description="系统通知文案模板。占位符：{name}{dimension}{delta}{reason}{nickname}；可选 {new_value}。",
    )


class AffinityPluginConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralSectionConfig = Field(default_factory=GeneralSectionConfig)
    card: CardSectionConfig = Field(default_factory=CardSectionConfig)
    image: ImageSectionConfig = Field(default_factory=ImageSectionConfig)
    description: DescriptionSectionConfig = Field(default_factory=DescriptionSectionConfig)
    cold_start: ColdStartSectionConfig = Field(default_factory=ColdStartSectionConfig)
    notify: NotifySectionConfig = Field(default_factory=NotifySectionConfig)
    dimensions: list[DimensionConfig] | None = Field(
        default=None,
        description="好感度子项维度列表（可任意多项）；留空使用内置默认集 A·群友养成。",
    )


# --------------------------------------------------------------------------- #
# 生效配置解析（None / 空 = 跟随代码默认）
# --------------------------------------------------------------------------- #
def _eint(value: int | None, default: int, *, minimum: int | None = None) -> int:
    if value is None:
        return default
    result = int(value)
    if minimum is not None:
        result = max(minimum, result)
    return result


def _efloat(value: float | None, default: float) -> float:
    return default if value is None else float(value)


def _estr(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def _ebool(value: bool | None, default: bool) -> bool:
    return default if value is None else bool(value)


def _etmpl(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value)


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    description: str


def resolve_dimensions(raw: Any) -> list[Dimension]:
    """解析维度列表；空则回退内置默认集。重复 key 取首个。"""
    items: list[Dimension] = []
    seen: set[str] = set()
    source: list[Any] = []
    if isinstance(raw, list) and raw:
        source = raw
    else:
        source = DEFAULT_DIMENSIONS

    for entry in source:
        if isinstance(entry, DimensionConfig):
            key = entry.key.strip()
            label = entry.label.strip()
            desc = entry.description.strip()
        elif isinstance(entry, Mapping):
            key = str(entry.get("key") or "").strip()
            label = str(entry.get("label") or "").strip()
            desc = str(entry.get("description") or "").strip()
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(Dimension(key=key, label=label or key, description=desc))

    if not items:
        items = [Dimension(d["key"], d["label"], d.get("description", "")) for d in DEFAULT_DIMENSIONS]
    return items


# --------------------------------------------------------------------------- #
# 配置落盘辅助（与塑料内存条一致：空壳恢复 + 去 None 持久化）
# --------------------------------------------------------------------------- #
def _is_runner_generated_bare_config(config_path: Path) -> bool:
    if not config_path.exists():
        return True
    try:
        text = config_path.read_text(encoding="utf-8")
        raw = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    if any(line.lstrip().startswith("#") for line in text.splitlines()):
        return False
    general = raw.get("general")
    return not isinstance(general, dict) or not general


def _restore_shipped_config_template(plugin_dir: Path) -> bool:
    config_path = plugin_dir / "config.toml"
    template_path = plugin_dir / SHIPPED_CONFIG_TEMPLATE_NAME
    if not template_path.exists() or not _is_runner_generated_bare_config(config_path):
        return False
    shutil.copy2(template_path, config_path)
    return True


def _load_config_dict_from_disk(plugin_dir: Path) -> dict[str, Any] | None:
    config_path = plugin_dir / "config.toml"
    if not config_path.exists():
        return None
    try:
        loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _strip_none_deep(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, nested in value.items():
            if nested is None:
                continue
            stripped = _strip_none_deep(nested)
            if stripped is None:
                continue
            cleaned[key] = stripped
        return cleaned
    if isinstance(value, list):
        return [_strip_none_deep(item) for item in value if item is not None]
    return value


def _dump_config_for_persist(config: dict[str, Any]) -> dict[str, Any]:
    validated = validate_plugin_config(AffinityPluginConfig, config)
    dumped = validated.model_dump(mode="python", exclude_none=True)
    return _strip_none_deep(dumped)


# --------------------------------------------------------------------------- #
# 存储层：SQLite（按 person_id 主键，跨聊天流共用）
# --------------------------------------------------------------------------- #
@dataclass
class AffinityRecord:
    person_id: str
    platform: str = ""
    user_id: str = ""
    display_name: str = ""
    total: float = DEFAULT_SCORE
    scores: dict[str, float] = field(default_factory=dict)
    description: str = ""
    updated_at: float = 0.0


class AffinityStore:
    """好感度数据的 SQLite 封装。

    所有读写经一把 ``asyncio.Lock`` 串行化，并通过 ``asyncio.to_thread``
    避免阻塞事件循环。``scores`` 以 JSON 文本存储，按维度 key 取用，
    天然支持运行时增删改维度而不丢数据。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS affinity (
                    person_id TEXT PRIMARY KEY,
                    platform TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    display_name TEXT DEFAULT '',
                    total REAL DEFAULT 5.0,
                    scores TEXT DEFAULT '{}',
                    description TEXT DEFAULT '',
                    updated_at REAL DEFAULT 0
                )
                """
            )
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _row_to_record(self, row: tuple) -> AffinityRecord:
        try:
            scores = json.loads(row[5]) if row[5] else {}
        except json.JSONDecodeError:
            scores = {}
        if not isinstance(scores, dict):
            scores = {}
        return AffinityRecord(
            person_id=row[0],
            platform=row[1] or "",
            user_id=row[2] or "",
            display_name=row[3] or "",
            total=float(row[4]) if row[4] is not None else DEFAULT_SCORE,
            scores={str(k): float(v) for k, v in scores.items() if _is_number(v)},
            description=row[6] or "",
            updated_at=float(row[7]) if row[7] is not None else 0.0,
        )

    def _get_sync(self, person_id: str) -> Optional[AffinityRecord]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT person_id, platform, user_id, display_name, total, scores, description, updated_at "
            "FROM affinity WHERE person_id = ?",
            (person_id,),
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def _upsert_sync(self, record: AffinityRecord) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO affinity (person_id, platform, user_id, display_name, total, scores, description, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                platform=excluded.platform,
                user_id=excluded.user_id,
                display_name=excluded.display_name,
                total=excluded.total,
                scores=excluded.scores,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (
                record.person_id,
                record.platform,
                record.user_id,
                record.display_name,
                record.total,
                json.dumps(record.scores, ensure_ascii=False),
                record.description,
                record.updated_at,
            ),
        )
        conn.commit()

    async def get(self, person_id: str) -> Optional[AffinityRecord]:
        return await asyncio.to_thread(self._get_sync, person_id)

    async def find_by_display_name(self, name: str) -> Optional[AffinityRecord]:
        return await asyncio.to_thread(self._find_by_display_name_sync, name)

    def _find_by_display_name_sync(self, name: str) -> Optional[AffinityRecord]:
        clean = str(name or "").strip()
        if not clean:
            return None
        conn = self._connect()
        cur = conn.execute(
            "SELECT person_id, platform, user_id, display_name, total, scores, description, updated_at "
            "FROM affinity WHERE display_name = ? COLLATE NOCASE LIMIT 2",
            (clean,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return self._row_to_record(rows[0])

    async def upsert(self, record: AffinityRecord) -> None:
        await asyncio.to_thread(self._upsert_sync, record)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# 解析后的目标人物
# --------------------------------------------------------------------------- #
@dataclass
class PersonRef:
    person_id: str
    platform: str
    user_id: str
    user_nickname: str = ""
    person_name: str = ""
    group_cardnames: list[str] = field(default_factory=list)
    group_cardname_entries: list[tuple[str, str]] = field(default_factory=list)
    memory_points: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.person_name or self.user_nickname or self.user_id or self.person_id

    @property
    def group_cardname(self) -> str:
        """全部群名片合并为一行（顿号分隔），供 LLM 提示词等使用。"""
        return "、".join(self.group_cardnames) if self.group_cardnames else ""


def _resolve_stream_id(kwargs: dict[str, Any]) -> str:
    """解析当前聊天流 ID。

    Host 在调用工具 / 命令时会把 ``stream_id`` / ``chat_id`` 作为顶层参数传入；
    兜底再看 message 字典的 ``session_id``。
    """
    for key in ("stream_id", "session_id", "chat_id"):
        value = kwargs.get(key)
        if value:
            return str(value).strip()
    msg = kwargs.get("message")
    if isinstance(msg, Mapping):
        sid = msg.get("session_id") or msg.get("stream_id")
        if sid:
            return str(sid).strip()
    return ""


def _caller_identity(kwargs: dict[str, Any]) -> tuple[str, str, str]:
    """解析触发者身份，返回 (platform, user_id, group_id)。

    Host 的工具 / 命令执行器会把 ``platform`` / ``user_id`` / ``group_id`` 作为
    顶层参数传入（工具场景下 ``user_id`` 即当前正在与之对话的发言者）；
    兜底再从 message 字典的 ``message_info`` 取。
    """
    platform = str(kwargs.get("platform") or "").strip()
    user_id = str(kwargs.get("user_id") or "").strip()
    group_id = str(kwargs.get("group_id") or "").strip()

    if not (platform and user_id and group_id):
        msg = kwargs.get("message")
        if isinstance(msg, Mapping):
            platform = platform or str(msg.get("platform") or "").strip()
            info = msg.get("message_info")
            if isinstance(info, Mapping):
                uinfo = info.get("user_info")
                if isinstance(uinfo, Mapping):
                    user_id = user_id or str(uinfo.get("user_id") or "").strip()
                ginfo = info.get("group_info")
                if isinstance(ginfo, Mapping):
                    group_id = group_id or str(ginfo.get("group_id") or "").strip()
    if not platform:
        platform = "qq"
    return platform, user_id, group_id


def _extract_target_user_id(kwargs: dict[str, Any]) -> str:
    """从消息里解析「指向的他人」：优先 @，其次引用回复的发送者。

    命令消息字典的 ``raw_message`` 是组件列表：``at`` 段为
    ``{"type": "at", "data": {"target_user_id": ...}}``，
    ``reply`` 段含 ``data.target_message_sender_id``。
    """
    msg = kwargs.get("message")
    if not isinstance(msg, Mapping):
        return ""
    segments = msg.get("raw_message")
    if not isinstance(segments, list):
        return ""
    reply_uid = ""
    for seg in segments:
        if not isinstance(seg, Mapping):
            continue
        seg_type = str(seg.get("type") or "").lower()
        data = seg.get("data")
        if not isinstance(data, Mapping):
            continue
        if seg_type == "at":
            uid = data.get("target_user_id") or data.get("qq") or data.get("user_id")
            if uid:
                return str(uid).strip()
        elif seg_type == "reply" and not reply_uid:
            uid = data.get("target_message_sender_id")
            if uid:
                reply_uid = str(uid).strip()
    return reply_uid


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class AffinityPlugin(MaiBotPlugin):
    config_model = AffinityPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._store: Optional[AffinityStore] = None
        self._pending: set[asyncio.Task] = set()
        self._gen_locks: dict[str, asyncio.Lock] = {}
        # 配置派生缓存
        self._dimensions: list[Dimension] = resolve_dimensions(None)
        self._total_label = DEFAULT_TOTAL_LABEL
        self._default_score = DEFAULT_SCORE
        self._scale_max = DEFAULT_SCALE_MAX
        self._scale_min = DEFAULT_SCALE_MIN
        self._allow_query_others = DEFAULT_ALLOW_QUERY_OTHERS
        self._radar_top_n = DEFAULT_RADAR_TOP_N
        self._recent_messages_limit = DEFAULT_RECENT_MESSAGES_LIMIT
        self._prune_removed = DEFAULT_PRUNE_REMOVED_DIMENSIONS
        self._store_path = DEFAULT_STORE_PATH

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        if _restore_shipped_config_template(self._plugin_dir):
            restored = _load_config_dict_from_disk(self._plugin_dir)
            if restored is not None:
                self.set_plugin_config(restored)
        self._refresh_config()
        self._store = AffinityStore(self._resolve_store_path())
        self.ctx.logger.info(
            "印象卡片插件已加载：维度=%s，数据=%s",
            "、".join(d.label for d in self._dimensions),
            self._store.path,
        )

    async def on_unload(self) -> None:
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        if self._store is not None:
            self._store.close()
        self.ctx.logger.info("印象卡片插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope != "self":
            return
        self._refresh_config()
        new_path = self._resolve_store_path()
        if self._store is None or self._store.path != new_path:
            if self._store is not None:
                self._store.close()
            self._store = AffinityStore(new_path)
        self.ctx.logger.info("印象卡片插件配置已更新: version=%s", version)

    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        normalized, changed = super().normalize_plugin_config(config_data)
        persistable = _dump_config_for_persist(normalized)
        return persistable, changed or persistable != normalized

    # ------------------------------------------------------------------ #
    # 配置解析
    # ------------------------------------------------------------------ #
    def _refresh_config(self) -> None:
        cfg = self.config
        g = cfg.general
        self._dimensions = resolve_dimensions(cfg.dimensions)
        self._total_label = _estr(g.total_label, DEFAULT_TOTAL_LABEL)
        self._default_score = _efloat(g.default_score, DEFAULT_SCORE)
        self._scale_max = _efloat(g.scale_max, DEFAULT_SCALE_MAX)
        self._scale_min = _efloat(g.scale_min, DEFAULT_SCALE_MIN)
        self._allow_query_others = _ebool(g.allow_query_others, DEFAULT_ALLOW_QUERY_OTHERS)
        self._radar_top_n = _eint(g.radar_top_n, DEFAULT_RADAR_TOP_N, minimum=1)
        self._recent_messages_limit = _eint(g.recent_messages_limit, DEFAULT_RECENT_MESSAGES_LIMIT, minimum=0)
        self._prune_removed = _ebool(g.prune_removed_dimensions, DEFAULT_PRUNE_REMOVED_DIMENSIONS)
        self._store_path = _estr(g.store_path, DEFAULT_STORE_PATH)

    def _resolve_store_path(self) -> Path:
        candidate = Path(self._store_path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._plugin_dir / candidate).resolve()

    def _resolve_under_plugin(self, raw: str, default: str) -> Path:
        candidate = Path(raw or default).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._plugin_dir / candidate).resolve()

    # ------------------------------------------------------------------ #
    # 人物解析
    # ------------------------------------------------------------------ #
    async def _person_from_user(self, platform: str, user_id: str) -> Optional[PersonRef]:
        platform = (platform or "qq").strip()
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        person_id = await self.ctx.person.get_id(platform, user_id)
        if not person_id:
            return None
        return await self._enrich_person(person_id, platform, user_id)

    async def _person_from_name(self, name: str, *, group_id: str = "") -> Optional[PersonRef]:
        name = _normalize_person_target(name)
        if not name:
            return None

        person_id = await self.ctx.person.get_id_by_name(name)
        if person_id:
            ref = await self._enrich_person(str(person_id), "", "")
            if ref:
                return ref

        for field in ("user_nickname", "person_name"):
            info = await self.ctx.db.get(
                model_name="PersonInfo",
                filters={field: name},
                single_result=True,
            )
            if isinstance(info, Mapping) and info.get("person_id"):
                ref = await self._enrich_person(str(info["person_id"]), "", "")
                if ref:
                    return ref

        card_ref = await self._person_from_group_cardname(name, group_id=group_id)
        if card_ref:
            return card_ref

        if self._store is not None:
            record = await self._store.find_by_display_name(name)
            if record is not None:
                ref = await self._enrich_person(record.person_id, record.platform, record.user_id)
                if ref:
                    return ref

        ref = await self._enrich_person(name, "", "")
        return ref

    async def _person_from_group_cardname(self, name: str, *, group_id: str = "") -> Optional[PersonRef]:
        candidates = await self.ctx.db.get(
            model_name="PersonInfo",
            filters={"is_known": True},
            limit=500,
        )
        if not isinstance(candidates, list):
            return None
        matches: list[Mapping[str, Any]] = []
        group_id = str(group_id or "").strip()
        for info in candidates:
            if not isinstance(info, Mapping):
                continue
            for entry_group_id, card in _parse_group_cardname_entries(info.get("group_cardname")):
                if not _names_equal(card, name):
                    continue
                if group_id and entry_group_id and entry_group_id != group_id:
                    continue
                matches.append(info)
                break
        if len(matches) != 1:
            return None
        person_id = str(matches[0].get("person_id") or "").strip()
        if not person_id:
            return None
        return await self._enrich_person(person_id, "", "")

    async def _enrich_person(self, person_id: str, platform: str, user_id: str) -> Optional[PersonRef]:
        info = await self.ctx.db.get(
            model_name="PersonInfo",
            filters={"person_id": person_id},
            single_result=True,
        )
        if not isinstance(info, Mapping):
            if not (platform and user_id):
                return None
            return PersonRef(person_id=person_id, platform=platform, user_id=user_id)
        memory_points = await self._load_memory_points(person_id, info.get("memory_points"))
        return PersonRef(
            person_id=person_id,
            platform=str(info.get("platform") or platform or "qq"),
            user_id=str(info.get("user_id") or user_id or ""),
            user_nickname=str(info.get("user_nickname") or ""),
            person_name=str(info.get("person_name") or ""),
            group_cardnames=_parse_group_cardnames(info.get("group_cardname")),
            group_cardname_entries=_parse_group_cardname_entries(info.get("group_cardname")),
            memory_points=memory_points,
        )

    async def _load_memory_points(self, person_id: str, db_raw: Any) -> list[str]:
        """从 PersonInfo 或 A_Memorix 人物画像加载印象记忆，供冷启动 / 刷新提示词使用。"""
        points = _parse_memory_points(db_raw)
        if points:
            return points
        try:
            value = await self.ctx.person.get_value(person_id, "memory_points")
            if not isinstance(value, dict):
                points = _parse_memory_points(value)
                if points:
                    return points
        except Exception as exc:
            self.ctx.logger.debug("person.get_value(memory_points) 失败 person_id=%s: %s", person_id, exc)
        try:
            profile = await self.ctx.call_capability(
                "memory.get_person_profile",
                person_id=person_id,
                limit=12,
            )
            points = _memory_points_from_profile(profile if isinstance(profile, Mapping) else {})
            if points:
                return points
        except Exception as exc:
            self.ctx.logger.debug("memory.get_person_profile 失败 person_id=%s: %s", person_id, exc)
        return []

    async def _resolve_target(self, target: str, kwargs: dict[str, Any]) -> tuple[Optional[PersonRef], str]:
        """把工具/命令里的 target 解析成 PersonRef。

        target 可为：空（取当前发言者）、纯数字（user_id）、昵称 / 别名 / 群名片 / person_name / person_id。
        """
        platform, caller_uid, group_id = _caller_identity(kwargs)
        target = str(target or "").strip()

        if not target:
            if not caller_uid:
                return None, "没有指定对象，也拿不到当前发言者身份。"
            ref = await self._person_from_user(platform, caller_uid)
            return (ref, "") if ref else (None, "找不到当前发言者的人物信息。")

        if target.isdigit():
            ref = await self._person_from_user(platform, target)
            return (ref, "") if ref else (None, f"找不到 user_id={target} 对应的人物。")

        ref = await self._person_from_name(target, group_id=group_id)
        if ref:
            return ref, ""
        normalized = _normalize_person_target(target)
        return None, f"找不到名为「{normalized}」的人物（可试 QQ 号、昵称、别名或群名片）。"

    # ------------------------------------------------------------------ #
    # 数据存取
    # ------------------------------------------------------------------ #
    def _get_score(self, record: AffinityRecord, key: str) -> float:
        return record.scores.get(key, self._default_score)

    async def _load_or_create(self, ref: PersonRef, stream_id: str) -> AffinityRecord:
        """读取记录；不存在则冷启动生成并落库。"""
        assert self._store is not None
        record = await self._store.get(ref.person_id)
        if record is not None:
            return record
        # 同一个人并发触发时只生成一次
        lock = self._gen_locks.setdefault(ref.person_id, asyncio.Lock())
        async with lock:
            record = await self._store.get(ref.person_id)
            if record is not None:
                return record
            record = await self._generate_record(ref, stream_id, existing=None)
            await self._store.upsert(record)
            self._maybe_schedule_storage_compact(ref.person_id, ref.display_name, record.description)
        return record

    # ------------------------------------------------------------------ #
    # 工具：加 / 减分
    # ------------------------------------------------------------------ #
    @Tool(
        "adjust_score",
        description=(
            "给某个人的好感度或某个子项维度加分 / 减分（delta 可为负）。"
            "这是一个欢乐向的设定，可凭你的主观态度调整。"
            "target 传对方的 QQ 号（user_id）或名字；不传则默认当前发言者。"
            "dimension 传 'total'（好感度总值）或某个维度 key；不传默认 total。"
            f"{_SCORE_GUIDANCE_FOR_TOOLS} "
            "调整后会按配置以【系统通知】播报，并返回该项的新数值。"
        ),
        parameters=[
            _param("target", ToolParamType.STRING, "对象：QQ号(user_id) 或 名字；省略=当前发言者", False),
            _param("dimension", ToolParamType.STRING, "'total' 或维度 key；省略=total", False),
            _param("delta", ToolParamType.FLOAT, "增量，可正可负", True),
            _param("reason", ToolParamType.STRING, "变动缘由（会出现在系统通知里）", False),
        ],
    )
    async def adjust_score(
        self,
        delta: float,
        target: str = "",
        dimension: str = "total",
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        delta_value = _coerce_float(delta, 0.0)
        dim_key, dim_label, dim_error = self._resolve_dimension(dimension)
        if dim_error:
            return {"content": dim_error}

        new_value = await self._apply_delta(ref, dim_key, delta_value)
        await self._maybe_notify(ref, dim_key, dim_label, delta_value, new_value, reason, kwargs)
        return {
            "content": (
                f"已给 {ref.display_name} 的「{dim_label}」{_fmt_delta(delta_value)}，"
                f"当前 {_fmt_num(new_value)}。"
            )
        }

    @Tool(
        "set_score",
        description=(
            "直接把某个人的好感度或某个子项维度设定为指定数值（用于重置 / 校准）。"
            f"target、dimension 规则同 adjust_score。{_SCORE_GUIDANCE_FOR_TOOLS}"
        ),
        parameters=[
            _param("target", ToolParamType.STRING, "对象：QQ号(user_id) 或 名字；省略=当前发言者", False),
            _param("dimension", ToolParamType.STRING, "'total' 或维度 key；省略=total", False),
            _param("value", ToolParamType.FLOAT, "要设定的数值", True),
            _param("reason", ToolParamType.STRING, "变动缘由（会出现在系统通知里）", False),
        ],
    )
    async def set_score(
        self,
        value: float,
        target: str = "",
        dimension: str = "total",
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        new_value = _coerce_float(value, self._default_score)
        dim_key, dim_label, dim_error = self._resolve_dimension(dimension)
        if dim_error:
            return {"content": dim_error}

        old_value, applied = await self._set_value(ref, dim_key, new_value)
        delta = applied - old_value
        await self._maybe_notify(ref, dim_key, dim_label, delta, applied, reason, kwargs)
        return {"content": f"已把 {ref.display_name} 的「{dim_label}」设定为 {_fmt_num(applied)}。"}

    def _resolve_dimension(self, dimension: str) -> tuple[str, str, Optional[str]]:
        """把传入的 dimension 解析成 (key, label, error)。"""
        raw = str(dimension or "total").strip()
        lowered = raw.lower()
        if lowered in ("total", "好感度", "") or raw == self._total_label:
            return "total", self._total_label, None
        canonical = LEGACY_DIMENSION_ALIASES.get(raw) or LEGACY_DIMENSION_ALIASES.get(lowered)
        if canonical:
            raw = canonical
            lowered = canonical.lower()
        for dim in self._dimensions:
            if raw == dim.key or lowered == dim.key.lower() or raw == dim.label:
                return dim.key, dim.label, None
        valid = "、".join(["total"] + [f"{d.key}({d.label})" for d in self._dimensions])
        return "", "", f'未知维度「{dimension}」。可用：{valid}。'

    async def _apply_delta(self, ref: PersonRef, dim_key: str, delta: float) -> float:
        assert self._store is not None
        async with self._store.lock:
            record = await self._store.get(ref.person_id) or self._new_record(ref)
            self._sync_identity(record, ref)
            if dim_key == "total":
                record.total += delta
                new_value = record.total
            else:
                current = record.scores.get(dim_key, self._default_score)
                new_value = current + delta
                record.scores[dim_key] = new_value
            record.updated_at = _now()
            await self._store.upsert(record)
        return new_value

    async def _set_value(self, ref: PersonRef, dim_key: str, value: float) -> tuple[float, float]:
        assert self._store is not None
        async with self._store.lock:
            record = await self._store.get(ref.person_id) or self._new_record(ref)
            self._sync_identity(record, ref)
            if dim_key == "total":
                old = record.total
                record.total = value
            else:
                old = record.scores.get(dim_key, self._default_score)
                record.scores[dim_key] = value
            record.updated_at = _now()
            await self._store.upsert(record)
        return old, value

    def _new_record(self, ref: PersonRef) -> AffinityRecord:
        return AffinityRecord(
            person_id=ref.person_id,
            platform=ref.platform,
            user_id=ref.user_id,
            display_name=ref.display_name,
            total=self._default_score,
            scores={},
            description="",
            updated_at=_now(),
        )

    def _sync_identity(self, record: AffinityRecord, ref: PersonRef) -> None:
        record.platform = ref.platform or record.platform
        record.user_id = ref.user_id or record.user_id
        record.display_name = ref.display_name or record.display_name

    async def _maybe_notify(
        self,
        ref: PersonRef,
        dim_key: str,
        dim_label: str,
        delta: float,
        new_value: float,
        reason: str,
        kwargs: dict[str, Any],
    ) -> None:
        notify = self.config.notify
        if not _ebool(notify.enabled, DEFAULT_NOTIFY_ENABLED):
            return
        notify_dims = notify.notify_dimensions
        notify_list = DEFAULT_NOTIFY_DIMENSIONS if notify_dims is None else list(notify_dims)
        if not notify_list:
            return
        if not any(t.strip().lower() in NOTIFY_ALL_TOKENS for t in notify_list):
            if dim_key not in notify_list:
                return
        stream_id = _resolve_stream_id(kwargs)
        if not stream_id:
            return
        nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        template = _etmpl(notify.notify_template, DEFAULT_NOTIFY_TEMPLATE)
        text = _render(
            template,
            name=ref.display_name,
            dimension=dim_label,
            delta=_fmt_delta(delta),
            new_value=_fmt_num(new_value),
            reason=reason.strip() or "（未说明）",
            nickname=nickname,
        )
        await self.ctx.send.text(text, stream_id)

    # ------------------------------------------------------------------ #
    # 工具：简介
    # ------------------------------------------------------------------ #
    @Tool(
        "append_impression",
        description=(
            "给某个人的人物印象简介追加一段文字（会出现在 ta 的好感度卡右侧）。"
            "target 规则同 adjust_score。"
            "persistent_impression 开启时可累积长文；仅超出存储字节上限时才会写回精简。"
        ),
        parameters=[
            _param("content", ToolParamType.STRING, "要追加的简介文字", True),
            _param("target", ToolParamType.STRING, "对象：QQ号 或 名字；省略=当前发言者", False),
        ],
    )
    async def append_impression(self, content: str, target: str = "", **kwargs: Any) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        return await self._write_description(ref, content, mode="append")

    @Tool(
        "rewrite_impression",
        description=(
            "用新内容【完全覆盖】某个人的人物印象简介。target 规则同 adjust_score。"
            "persistent_impression 开启时超出存储字节上限会在后台写回精简；"
            "关闭时超出 size_limit 会在后台写回精简。"
        ),
        parameters=[
            _param("content", ToolParamType.STRING, "新的完整简介", True),
            _param("target", ToolParamType.STRING, "对象：QQ号 或 名字；省略=当前发言者", False),
        ],
    )
    async def rewrite_impression(self, content: str, target: str = "", **kwargs: Any) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        return await self._write_description(ref, content, mode="rewrite")

    async def _write_description(self, ref: PersonRef, content: str, *, mode: str) -> dict[str, str]:
        assert self._store is not None
        desc_cfg = self.config.description
        size_limit = _eint(desc_cfg.size_limit, DEFAULT_DESCRIPTION_SIZE_LIMIT, minimum=1)
        note_limit = _eint(desc_cfg.impression_note_size_limit, DEFAULT_IMPRESSION_NOTE_SIZE_LIMIT, minimum=1)
        persistent = _ebool(desc_cfg.persistent_impression, DEFAULT_PERSISTENT_IMPRESSION)
        async with self._store.lock:
            record = await self._store.get(ref.person_id) or self._new_record(ref)
            self._sync_identity(record, ref)
            if mode == "append":
                base = record.description.rstrip()
                record.description = (base + ("\n" if base else "") + content.strip()).strip()
            else:
                record.description = content.strip()
            record.updated_at = _now()
            used_chars = len(record.description)
            used_bytes = _text_byte_len(record.description)
            await self._store.upsert(record)
        self._maybe_schedule_storage_compact(ref.person_id, ref.display_name, record.description)
        action = "已追加到" if mode == "append" else "已覆盖"
        if persistent:
            msg = (
                f"{action} {ref.display_name} 的简介，当前 {used_chars} 字符（{used_bytes} 字节）。"
                f" 卡面显示上限 {size_limit} 字符，存储上限 {note_limit} 字节。"
            )
            if used_bytes > note_limit:
                msg += " 已超过存储上限，后台自动精简中。"
        else:
            msg = f"{action} {ref.display_name} 的简介，当前 {used_chars} 字符（上限 {size_limit}）。"
            if used_chars > size_limit:
                msg += " 已超限，后台自动精简中。"
        return {"content": msg}

    def _maybe_schedule_storage_compact(self, person_id: str, name: str, text: str) -> None:
        desc_cfg = self.config.description
        persistent = _ebool(desc_cfg.persistent_impression, DEFAULT_PERSISTENT_IMPRESSION)
        size_limit = _eint(desc_cfg.size_limit, DEFAULT_DESCRIPTION_SIZE_LIMIT, minimum=1)
        note_limit = _eint(desc_cfg.impression_note_size_limit, DEFAULT_IMPRESSION_NOTE_SIZE_LIMIT, minimum=1)
        if persistent:
            if _text_byte_len(text) > note_limit:
                self._schedule(self._safe_compact_description(person_id, name, note_limit, "bytes"))
        elif len(text) > size_limit:
            self._schedule(self._safe_compact_description(person_id, name, size_limit, "chars"))

    async def _safe_compact_description(self, person_id: str, name: str, limit: int, limit_unit: str) -> None:
        try:
            await self._compact_description(person_id, name, limit, limit_unit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("简介精简失败 person_id=%s: %s", person_id, exc, exc_info=True)

    async def _compact_description_text(self, name: str, text: str, limit: int, limit_unit: str) -> str:
        desc_cfg = self.config.description
        model = _estr(desc_cfg.compact_model, DEFAULT_COMPACT_MODEL)
        temperature = _efloat(desc_cfg.compact_temperature, DEFAULT_COMPACT_TEMPERATURE)
        max_tokens = _eint(desc_cfg.compact_max_tokens, DEFAULT_COMPACT_MAX_TOKENS, minimum=0)
        if max_tokens <= 0:
            basis = limit if limit_unit == "chars" else max(limit // 2, 256)
            max_tokens = basis * AUTO_COMPACT_MAX_TOKENS_MULTIPLIER
        attempts = _eint(desc_cfg.max_compact_attempts, DEFAULT_MAX_COMPACT_ATTEMPTS, minimum=1)
        template = _etmpl(desc_cfg.compact_prompt_template, DEFAULT_COMPACT_PROMPT_TEMPLATE)

        nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        personality = await self.ctx.config.get("personality.personality", "") or ""
        reply_style = await self.ctx.config.get("personality.reply_style", "") or ""

        current = text
        best = current
        label = _limit_label(limit, limit_unit)
        for attempt in range(1, attempts + 1):
            prompt = _render(
                template,
                nickname=nickname,
                personality=personality,
                reply_style=reply_style,
                name=name,
                used=_text_used(current, limit_unit),
                limit_label=label,
                description=current,
            )
            result = await self.ctx.llm.generate(
                prompt=prompt, model=model, temperature=temperature, max_tokens=max_tokens
            )
            if not result.get("success"):
                self.ctx.logger.warning("简介精简第 %d 次 LLM 调用失败: %s", attempt, result.get("error"))
                break
            new_desc = (result.get("response") or "").strip()
            if not new_desc:
                break
            if _text_byte_len(new_desc) < _text_byte_len(best):
                best = new_desc
            current = new_desc
            if _text_within_limit(new_desc, limit, limit_unit):
                return new_desc
        return _truncate_text(best, limit, limit_unit)

    async def _compact_description(self, person_id: str, name: str, limit: int, limit_unit: str) -> None:
        assert self._store is not None
        async with self._store.lock:
            record = await self._store.get(person_id)
            if record is None or _text_within_limit(record.description, limit, limit_unit):
                return
            best = await self._compact_description_text(name, record.description, limit, limit_unit)
            record.description = best
            record.updated_at = _now()
            await self._store.upsert(record)

    async def _description_for_card(self, ref: PersonRef, record: AffinityRecord) -> str:
        desc_cfg = self.config.description
        size_limit = _eint(desc_cfg.size_limit, DEFAULT_DESCRIPTION_SIZE_LIMIT, minimum=1)
        persistent = _ebool(desc_cfg.persistent_impression, DEFAULT_PERSISTENT_IMPRESSION)
        text = record.description.strip()
        if not text:
            return "（这个人还很神秘，暂无印象。）"
        if not persistent:
            return text
        if len(text) <= size_limit:
            return text
        try:
            return await self._compact_description_text(ref.display_name, text, size_limit, "chars")
        except Exception as exc:
            self.ctx.logger.warning("卡面简介精简失败: %s", exc)
            return _truncate_text(text, size_limit, "chars")

    # ------------------------------------------------------------------ #
    # 工具：详情 / 刷新
    # ------------------------------------------------------------------ #
    @Tool(
        "get_impression_detail",
        description=(
            "以 Markdown 形式获取某个人的好感度详细信息（总值、各维度分值、简介）。"
            f"target 规则同 adjust_score。{_SCORE_GUIDANCE_FOR_TOOLS} "
            "库中没有该人时会先冷启动生成。"
        ),
        parameters=[
            _param("target", ToolParamType.STRING, "对象：QQ号 或 名字；省略=当前发言者", False),
        ],
    )
    async def get_impression_detail(self, target: str = "", **kwargs: Any) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        stream_id = _resolve_stream_id(kwargs)
        record = await self._load_or_create(ref, stream_id)
        return {"content": self._render_detail_markdown(ref, record)}

    @Tool(
        "refresh_impression",
        description=(
            "结合 PersonInfo 印象记忆、最近聊天与既有数据，用你的口吻重新评估某个人的"
            f"好感度各项分值与简介（既有数据会作为参考一并更新）。target 规则同 adjust_score。"
            f"{_SCORE_GUIDANCE_FOR_TOOLS}"
        ),
        parameters=[
            _param("target", ToolParamType.STRING, "对象：QQ号 或 名字；省略=当前发言者", False),
        ],
    )
    async def refresh_impression(self, target: str = "", **kwargs: Any) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        stream_id = _resolve_stream_id(kwargs)
        record = await self._refresh_record(ref, stream_id)
        return {"content": f"已刷新对 {ref.display_name} 的印象。\n\n" + self._render_detail_markdown(ref, record)}

    @Tool(
        "send_impression_card",
        description=(
            "向当前聊天流发送某人的印象卡片图片（与 /卡片 命令相同的卡面）。"
            "适合在对话中你想主动展示、介绍或总结某人对你的印象档案时使用，"
            "例如对方询问印象、聊到好感相关话题、或你想用卡片回应互动。"
            "target 传 QQ 号、平台昵称、你给的别名（person_name）、群名片或 person_id；"
            "省略则发给当前发言者。"
            "若库中尚无该人档案会先冷启动生成；可选 refresh_first 在发送前重算分值与简介。"
        ),
        parameters=[
            _param(
                "target",
                ToolParamType.STRING,
                "对象：QQ号、昵称、别名(person_name)、群名片或 person_id；省略=当前发言者",
                False,
            ),
            _param(
                "refresh_first",
                ToolParamType.BOOLEAN,
                "发送前是否先刷新评估（同 refresh_impression）",
                False,
            ),
        ],
    )
    async def send_impression_card(
        self,
        target: str = "",
        refresh_first: bool = False,
        **kwargs: Any,
    ) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        stream_id = _resolve_stream_id(kwargs)
        if not stream_id:
            return {"content": "缺少 stream_id，无法在当前聊天发送卡片。"}
        try:
            if refresh_first:
                await self._refresh_record(ref, stream_id)
            await self._generate_and_send_card(ref, stream_id)
        except Exception as exc:
            self.ctx.logger.error("send_impression_card 失败: %s", exc, exc_info=True)
            return {"content": "发送印象卡片时出错了……"}
        action = "已刷新并发送" if refresh_first else "已发送"
        return {"content": f"{action} {ref.display_name} 的印象卡片。"}

    def _render_detail_markdown(self, ref: PersonRef, record: AffinityRecord) -> str:
        lines = [f"## {ref.display_name} 的好感度档案"]
        if ref.user_nickname and ref.user_nickname != ref.display_name:
            lines.append(f"- QQ 昵称：{ref.user_nickname}")
        if ref.person_name:
            lines.append(f"- 别名：{ref.person_name}")
        if ref.group_cardnames:
            lines.append(f"- 别名：{ref.group_cardname}")
        lines.append(f"- **{self._total_label}（总值）：{_fmt_num(record.total)}**")
        lines.append("")
        lines.append("| 维度 | 数值 |")
        lines.append("| --- | --- |")
        for dim in self._dimensions:
            lines.append(f"| {dim.label} | {_fmt_num(self._get_score(record, dim.key))} |")
        lines.append("")
        lines.append("**印象简介：**")
        lines.append(record.description.strip() or "（暂无）")
        return "\n".join(lines)

    async def _inject_impression_context(self, stream_id: str, ref: PersonRef, record: AffinityRecord) -> None:
        """向麦麦的 Maisaka 上下文注入刚发送的印象卡片 Markdown 摘要。"""
        if not stream_id:
            return
        markdown = self._render_detail_markdown(ref, record)
        body = (
            f"[系统·印象卡片] 你刚刚向用户发送了关于「{ref.display_name}」的印象卡片，"
            "以下为卡片中的分值与简介（供你后续对话参考）：\n\n"
            f"{markdown}"
        )
        try:
            await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": body}],
                visible_text=f"已发送 {ref.display_name} 的印象卡片",
                source_kind="plugin:com.0-hz.impression-card",
            )
        except Exception as exc:
            self.ctx.logger.debug("注入印象卡片上下文失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 命令：查询卡片 / 刷新
    # ------------------------------------------------------------------ #
    @Command(
        "impression_card",
        pattern=r"^/(?:卡片|card)(?:\s+(?P<target>.+))?$",
        description="查询印象卡片。/卡片 查自己，/卡片 @某人 或 /卡片 名字 查他人。",
    )
    async def cmd_card(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = _resolve_stream_id(kwargs)
        ref, error = await self._resolve_query_target(kwargs)
        if error or ref is None:
            if stream_id:
                await self.ctx.send.text(error or "找不到这个人。", stream_id)
            return False, error or "解析对象失败", 2
        try:
            await self._generate_and_send_card(ref, stream_id)
        except Exception as exc:
            self.ctx.logger.error("生成印象卡片失败: %s", exc, exc_info=True)
            if stream_id:
                await self.ctx.send.text("生成好感度卡时出错了……", stream_id)
            return False, "生成失败", 2
        return True, "已发送好感度卡", 2

    @Command(
        "impression_refresh",
        pattern=r"^/(?:刷新印象|refresh_impression)(?:\s+(?P<target>.+))?$",
        description="刷新对某人的印象（重算各项分值与简介）。/刷新印象 或 /刷新印象 @某人。",
    )
    async def cmd_refresh(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = _resolve_stream_id(kwargs)
        ref, error = await self._resolve_query_target(kwargs)
        if error or ref is None:
            if stream_id:
                await self.ctx.send.text(error or "找不到这个人。", stream_id)
            return False, error or "解析对象失败", 2
        try:
            await self._refresh_record(ref, stream_id)
            await self._generate_and_send_card(ref, stream_id)
        except Exception as exc:
            self.ctx.logger.error("刷新印象卡片失败: %s", exc, exc_info=True)
            if stream_id:
                await self.ctx.send.text("刷新印象时出错了……", stream_id)
            return False, "刷新失败", 2
        return True, "已刷新并发送", 2

    async def _resolve_query_target(self, kwargs: dict[str, Any]) -> tuple[Optional[PersonRef], str]:
        """命令查询的对象解析：自己 / @他人 / 引用他人 / 名字（受 allow_query_others 约束）。"""
        platform, caller_uid, group_id = _caller_identity(kwargs)
        matched = kwargs.get("matched_groups") or {}
        target_text = _normalize_person_target(str(matched.get("target") or ""))
        at_uid = _extract_target_user_id(kwargs)

        wants_other = bool(at_uid or target_text)
        if wants_other and not self._allow_query_others:
            return None, "当前只允许查询自己的卡片哦。"

        if at_uid:
            ref = await self._person_from_user(platform, at_uid)
            return (ref, "") if ref else (None, "找不到 @ 的那个人。")
        if target_text:
            if target_text.isdigit():
                ref = await self._person_from_user(platform, target_text)
            else:
                ref = await self._person_from_name(target_text, group_id=group_id)
            return (ref, "") if ref else (None, f"找不到「{target_text}」。")

        if not caller_uid:
            return None, "拿不到你的身份信息。"
        ref = await self._person_from_user(platform, caller_uid)
        return (ref, "") if ref else (None, "找不到你的人物信息。")

    # ------------------------------------------------------------------ #
    # 冷启动 / 刷新：调 LLM 生成参数与简介
    # ------------------------------------------------------------------ #
    async def _refresh_record(self, ref: PersonRef, stream_id: str) -> AffinityRecord:
        assert self._store is not None
        existing = await self._store.get(ref.person_id)
        record = await self._generate_record(ref, stream_id, existing=existing)
        await self._store.upsert(record)
        self._maybe_schedule_storage_compact(ref.person_id, ref.display_name, record.description)
        return record

    async def _generate_record(
        self, ref: PersonRef, stream_id: str, *, existing: Optional[AffinityRecord]
    ) -> AffinityRecord:
        cold = self.config.cold_start
        model = _estr(cold.model, DEFAULT_COLD_START_MODEL)
        is_refresh = existing is not None
        if is_refresh and cold.refresh_temperature is not None:
            temperature = _efloat(cold.refresh_temperature, DEFAULT_COLD_START_TEMPERATURE)
        else:
            temperature = _efloat(cold.temperature, DEFAULT_COLD_START_TEMPERATURE)
        max_tokens = _eint(cold.max_tokens, DEFAULT_COLD_START_MAX_TOKENS, minimum=0) or None
        template = _etmpl(cold.prompt_template, DEFAULT_COLD_START_PROMPT_TEMPLATE)
        size_limit = _eint(self.config.description.size_limit, DEFAULT_DESCRIPTION_SIZE_LIMIT, minimum=1)

        nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        personality = await self.ctx.config.get("personality.personality", "") or ""
        reply_style = await self.ctx.config.get("personality.reply_style", "") or ""

        recent_chat = await self._recent_chat_text(stream_id)
        dimensions_doc = "\n".join(
            f"- {d.key}（{d.label}）：{d.description or d.label}" for d in self._dimensions
        )
        scores_keys_doc = ", ".join(f'"{d.key}": 数字' for d in self._dimensions)
        existing_block = self._existing_block(existing)
        refresh_guidance = self._refresh_guidance_block(is_refresh)
        task_intro = "重新评估并更新" if is_refresh else "建立"
        stored_display_name = str(existing.display_name or "").strip() if existing else ""

        prompt = _render(
            template,
            nickname=nickname,
            personality=personality,
            reply_style=reply_style,
            name=ref.display_name,
            task_intro=task_intro,
            total_label=self._total_label,
            dimensions_doc=dimensions_doc,
            scores_keys_doc=scores_keys_doc,
            person_identities=_format_person_identities(ref, stored_display_name=stored_display_name),
            user_nickname=ref.user_nickname or "（未知）",
            group_cardname=ref.group_cardname or "（无）",
            memory_points="；".join(ref.memory_points) if ref.memory_points else "（无）",
            recent_chat=recent_chat or "（无）",
            refresh_guidance=refresh_guidance,
            existing_block=existing_block,
            size_limit=size_limit,
        )

        record = existing or self._new_record(ref)
        self._sync_identity(record, ref)
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self.ctx.logger.warning("%s LLM 调用异常: %s", "刷新印象" if is_refresh else "冷启动", exc, exc_info=True)
            result = {"success": False}

        parsed = _extract_json_object(result.get("response", "")) if result.get("success") else None
        if parsed is None:
            self.ctx.logger.info(
                "%s未拿到有效 JSON，使用默认中间值 person_id=%s",
                "刷新印象" if is_refresh else "冷启动",
                ref.person_id,
            )
            for dim in self._dimensions:
                record.scores.setdefault(dim.key, self._default_score)
            if not record.description:
                record.description = ""
            record.updated_at = _now()
            return record

        record.total = _coerce_float(parsed.get("total"), record.total if existing else self._default_score)
        scores = parsed.get("scores")
        if isinstance(scores, Mapping):
            for dim in self._dimensions:
                if dim.key in scores:
                    record.scores[dim.key] = _coerce_float(scores[dim.key], self._default_score)
                else:
                    record.scores.setdefault(dim.key, self._default_score)
        description = str(parsed.get("description") or "").strip()
        if description:
            record.description = description
        record.updated_at = _now()
        return record

    def _refresh_guidance_block(self, is_refresh: bool) -> str:
        if not is_refresh:
            return ""
        text = _etmpl(self.config.cold_start.refresh_guidance, DEFAULT_REFRESH_GUIDANCE).strip()
        if not text:
            return ""
        return text + "\n"

    def _existing_block(self, existing: Optional[AffinityRecord]) -> str:
        if existing is None:
            return ""
        lines = [
            "你之前对 ta 的档案（仅供参考，不必在旧分附近微调，允许大幅改动）：",
            f"- {self._total_label}：{_fmt_num(existing.total)}",
        ]
        for dim in self._dimensions:
            lines.append(f"- {dim.label}：{_fmt_num(existing.scores.get(dim.key, self._default_score))}")
        if existing.description:
            lines.append(f"- 既有简介：{existing.description}")
        return "\n".join(lines) + "\n"

    async def _recent_chat_text(self, stream_id: str) -> str:
        if not stream_id or self._recent_messages_limit <= 0:
            return ""
        try:
            # 在 Host 端查询并格式化；勿把 get_recent 返回的 dict 再传给 build_readable。
            now = time.time()
            return await self.ctx.message.build_readable(
                messages=None,
                chat_id=stream_id,
                start_time=now - 24 * 3600,
                end_time=now,
                limit=self._recent_messages_limit,
            )
        except Exception as exc:
            self.ctx.logger.debug("获取最近聊天失败: %s", exc)
            return ""

    # ------------------------------------------------------------------ #
    # 卡片生成与发送
    # ------------------------------------------------------------------ #
    async def _generate_and_send_card(self, ref: PersonRef, stream_id: str) -> None:
        if not stream_id:
            return
        record = await self._load_or_create(ref, stream_id)
        img_cfg = self.config.image

        # 头像决定是否出动图：头像本身是动图（且开启）→ 卡片做成动图；否则静态单张。
        avatar_bytes = await self._fetch_avatar_bytes(ref)
        avatar_frames: list[Any] = []
        durations: list[int] = []
        animated = False
        if avatar_bytes:
            try:
                avatar_frames, durations, animated = await asyncio.to_thread(
                    extract_avatar_frames,
                    avatar_bytes,
                    max_frames=_eint(img_cfg.max_avatar_frames, DEFAULT_MAX_AVATAR_FRAMES, minimum=1),
                    fallback_duration_ms=_eint(
                        img_cfg.avatar_frame_fallback_ms, DEFAULT_AVATAR_FRAME_FALLBACK_MS, minimum=10
                    ),
                )
            except Exception as exc:
                self.ctx.logger.debug("解析头像帧失败: %s", exc)
        if animated and not _ebool(img_cfg.animate_with_avatar, DEFAULT_ANIMATE_WITH_AVATAR):
            animated = False  # 用户关闭了动图：仍按静态处理（只用首帧）

        # 渲染依赖 Host 的 render.html2png（Playwright 浏览器）。环境不可用时退化为文字版档案。
        base_png: Optional[bytes] = None
        try:
            if animated:
                # 动图：头像位置先用色键占位渲染一次，随后逐帧贴回
                avatar_html = f'<div class="avatar" style="background-color:{AVATAR_CHROMA_HTML_COLOR};"></div>'
            else:
                avatar_html = self._static_avatar_html(ref, avatar_bytes, avatar_frames)
            html = _wrap_card_html_for_render(await self._build_card_html(ref, record, avatar_html))
            render_result = await self.ctx.render.html2png(
                html,
                selector="#card",
                viewport=CARD_VIEWPORT,
                device_scale_factor=CARD_DEVICE_SCALE,
                omit_background=True,
            )
            if isinstance(render_result, Mapping) and render_result.get("image_base64"):
                base_png = b64decode(render_result["image_base64"])
        except Exception as exc:
            self.ctx.logger.warning("卡片渲染失败，回退文字版: %s", exc)

        if not base_png:
            await self.ctx.send.text(
                "（图片渲染暂不可用，先用文字版档案。Host 浏览器环境就绪后即可出图。）\n\n"
                + self._render_detail_markdown(ref, record),
                stream_id,
            )
            await self._inject_impression_context(stream_id, ref, record)
            return

        bg = _estr(img_cfg.background_color, DEFAULT_BACKGROUND_COLOR)
        webp_q = _eint(img_cfg.webp_quality, DEFAULT_WEBP_QUALITY, minimum=1)
        if animated:
            encoded, _mime = await asyncio.to_thread(
                encode_animated_card,
                base_png,
                avatar_frames,
                durations,
                animated_format=_estr(img_cfg.animated_format, DEFAULT_ANIMATED_FORMAT),
                loop=_eint(img_cfg.loop, DEFAULT_LOOP, minimum=0),
                webp_quality=webp_q,
                background_color=bg,
            )
        else:
            encoded, _mime = await asyncio.to_thread(
                encode_static_card,
                base_png,
                static_format=_estr(img_cfg.static_format, DEFAULT_STATIC_FORMAT),
                jpg_quality=_eint(img_cfg.jpg_quality, DEFAULT_JPG_QUALITY, minimum=1),
                webp_quality=webp_q,
                background_color=bg,
            )

        out_b64 = b64encode(encoded).decode("ascii")
        if _ebool(img_cfg.send_as_emoji, DEFAULT_SEND_AS_EMOJI):
            await self.ctx.send.emoji(out_b64, stream_id)
        else:
            await self.ctx.send.image(out_b64, stream_id)
        await self._inject_impression_context(stream_id, ref, record)

    def _static_avatar_html(self, ref: PersonRef, avatar_bytes: Optional[bytes], frames: list[Any]) -> str:
        """静态路径的头像 HTML：动图头像（被关停动图）取首帧；普通静态头像内嵌原图；都没有则首字占位。"""
        if len(frames) > 1:
            # 原头像是动图但未输出动图：固定取首帧，避免浏览器截到不确定的一帧
            buf = BytesIO()
            frames[0].save(buf, format="PNG")
            uri = f"data:image/png;base64,{b64encode(buf.getvalue()).decode('ascii')}"
            return f'<img class="avatar" src="{uri}" alt="avatar" />'
        if avatar_bytes:
            mime = _sniff_image_mime(avatar_bytes)
            uri = f"data:{mime};base64,{b64encode(avatar_bytes).decode('ascii')}"
            return f'<img class="avatar" src="{uri}" alt="avatar" />'
        initial = (ref.display_name or "?")[:1]
        return f'<div class="avatar avatar-placeholder">{_html_escape(initial)}</div>'

    def _top_dimensions(self, record: AffinityRecord) -> list[tuple[str, float]]:
        """挑出最极端（离中间值最远）的若干维度用于雷达图。"""
        mid = self._default_score
        scored = [(d.label, self._get_score(record, d.key), abs(self._get_score(record, d.key) - mid)) for d in self._dimensions]
        scored.sort(key=lambda x: x[2], reverse=True)
        top = scored[: max(1, self._radar_top_n)]
        # 维持原维度顺序更稳定
        chosen_labels = {label for label, _, _ in top}
        ordered = [(d.label, self._get_score(record, d.key)) for d in self._dimensions if d.label in chosen_labels]
        return ordered

    async def _build_card_html(self, ref: PersonRef, record: AffinityRecord, avatar_html: str) -> str:
        template = self._load_card_template()
        bot_name = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        top_dims = self._top_dimensions(record)
        radar_svg = build_radar_svg(top_dims, self._scale_max, size=CARD_RADAR_SVG_SIZE)
        gauge_bar = build_gauge_bar_svg(record.total, self._scale_max, self._scale_min)
        legend_html = build_legend_html(top_dims)

        alias = ref.person_name or ""
        cardname_text = ref.group_cardname
        person_label = ref.person_name or ref.user_nickname or ref.display_name
        description = await self._description_for_card(ref, record)
        card_title_raw = _estr(self.config.card.card_title, DEFAULT_CARD_TITLE)
        card_title = _render_card_text_template(
            card_title_raw, bot_name=bot_name, person_name=person_label
        )
        impression_title = _render_card_text_template(
            "{bot_name}对{person_name}的印象", bot_name=bot_name, person_name=person_label
        )

        return _render(
            template,
            card_title=_html_escape(card_title),
            avatar_html=avatar_html,
            nickname=_html_escape(ref.user_nickname or ref.display_name),
            alias=_html_escape(alias),
            alias_block=f'<div class="alias">「{_html_escape(alias)}」</div>' if alias else "",
            cardname=_html_escape(cardname_text),
            cardname_block=(
                f'<div class="cardname">别名：{_html_escape(cardname_text)}</div>' if cardname_text else ""
            ),
            bot_name=_html_escape(bot_name),
            impression_title=_html_escape(impression_title),
            total_label=_html_escape(self._total_label),
            total_value=_fmt_num(record.total),
            gauge_bar=gauge_bar,
            radar_svg=radar_svg,
            legend_html=legend_html,
            description=_html_escape(description),
        )

    def _load_card_template(self) -> str:
        path = self._resolve_under_plugin(
            _estr(self.config.card.card_template, DEFAULT_CARD_TEMPLATE), DEFAULT_CARD_TEMPLATE
        )
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            self.ctx.logger.warning("卡片模板读取失败，回退内置默认: %s", path)
            fallback = self._plugin_dir / DEFAULT_CARD_TEMPLATE
            return fallback.read_text(encoding="utf-8")

    async def _fetch_avatar_bytes(self, ref: PersonRef) -> Optional[bytes]:
        """拉取头像原始字节（保留动图）；非 QQ 系平台 / 非数字 user_id / 失败时返回 None。"""
        if ref.platform.strip().lower() not in QQ_COMPATIBLE_PLATFORMS or not ref.user_id.isdigit():
            return None
        url = QQ_AVATAR_URL_TEMPLATE.format(user_id=ref.user_id)
        try:
            async with httpx.AsyncClient(timeout=AVATAR_TIMEOUT_S, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:
            self.ctx.logger.debug("拉取头像失败 user_id=%s: %s", ref.user_id, exc)
            return None
        return data or None

    # ------------------------------------------------------------------ #
    # 后台任务管理
    # ------------------------------------------------------------------ #
    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


# --------------------------------------------------------------------------- #
# 模块级小工具（依赖外部对象，放在类外便于测试）
# --------------------------------------------------------------------------- #
def _now() -> float:
    import time

    return time.time()


def _sniff_image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _parse_memory_point_item(item: Any) -> str:
    text = str(item or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) >= 3:
        content = ":".join(parts[1:-1]).strip()
        return content or text
    return text


def _parse_memory_points(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [point for point in (_parse_memory_point_item(item) for item in raw) if point]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [_parse_memory_point_item(raw)]
        if isinstance(parsed, list):
            return [point for point in (_parse_memory_point_item(item) for item in parsed) if point]
        return [_parse_memory_point_item(parsed)]
    return []


def _memory_points_from_profile(profile: Mapping[str, Any]) -> list[str]:
    if profile.get("success") is False:
        return []
    points: list[str] = []
    seen: set[str] = set()
    for trait in profile.get("traits") or []:
        text = str(trait or "").strip().lstrip("- ").strip()
        if text and text not in seen:
            seen.add(text)
            points.append(text)
    summary = str(profile.get("summary") or "").strip()
    if not points and summary:
        for line in summary.splitlines():
            text = line.strip().lstrip("- ").strip()
            if text and text not in seen:
                seen.add(text)
                points.append(text)
    for item in profile.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("content") or "").strip()
        if text and text not in seen:
            seen.add(text)
            points.append(text)
        if len(points) >= 12:
            break
    return points


def _parse_group_cardnames(raw: Any) -> list[str]:
    """从 PersonInfo 的 group_cardname 字段解析全部群名片。"""
    if raw is None:
        return []
    parsed: Any = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw.strip()]
    names: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, Mapping):
                name = str(item.get("group_cardname") or "").strip()
            else:
                name = str(item or "").strip()
            if name and name not in names:
                names.append(name)
    elif isinstance(parsed, Mapping):
        name = str(parsed.get("group_cardname") or "").strip()
        if name:
            names.append(name)
    elif isinstance(parsed, str) and parsed.strip():
        names.append(parsed.strip())
    return names


def create_plugin() -> AffinityPlugin:
    return AffinityPlugin()
