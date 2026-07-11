"""离线冒烟测试：不依赖 MaiBot Host，验证配置、SVG 与图片编码逻辑。

运行方式（在插件目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import sys
import tomllib
from io import BytesIO
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as affinity  # noqa: E402


def _flatten_values(value: object) -> list[object]:
    if isinstance(value, dict):
        items: list[object] = []
        for nested in value.values():
            items.extend(_flatten_values(nested))
        return items
    if isinstance(value, list):
        items = []
        for nested in value:
            items.extend(_flatten_values(nested))
        return items
    return [value]


def test_plugin_importable() -> None:
    inst = affinity.create_plugin()
    assert inst is not None
    default_config = type(inst).build_default_config()
    assert default_config["plugin"]["config_version"] == affinity.CURRENT_CONFIG_VERSION
    # 标量字段默认应为 None（占位空值，便于升级跟随新默认）
    assert default_config["general"]["total_label"] is None
    assert default_config["image"]["static_format"] is None
    assert default_config["image"]["animated_format"] is None
    print("ok: plugin importable, defaults are placeholders")


def test_manifest_capabilities_cover_usage() -> None:
    """从源码里推导出实际用到的 ctx 能力，确保 _manifest.json 已声明（避免 db.get vs database.get 这类错配）。"""
    import json
    import re

    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    # 代理名 → Host 能力前缀（db 实际是 database）
    proxy_to_capability = {
        "db": "database", "llm": "llm", "render": "render", "person": "person",
        "message": "message", "config": "config", "send": "send", "emoji": "emoji",
        "chat": "chat", "knowledge": "knowledge",
    }
    used = set()
    for proxy, method in re.findall(r"self\.ctx\.([a-z_]+)\.([a-z_0-9]+)", source):
        if proxy in proxy_to_capability:
            used.add(f"{proxy_to_capability[proxy]}.{method}")

    if "maisaka.context.append" in source:
        used.add("maisaka.context.append")

    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("capabilities", []))
    missing = used - declared
    assert not missing, f"_manifest.json 缺少能力声明：{sorted(missing)}"
    # 不应再出现错误的 db.* 命名
    assert not any(c.startswith("db.") for c in declared), declared
    print(f"ok: manifest declares all {len(used)} used capabilities")


def test_config_toml_consistent() -> None:
    default_config = affinity.AffinityPlugin.build_default_config()
    config_data = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    for section, value in config_data.items():
        assert section in default_config, f"config.toml 中存在未知配置节：{section}"
        if isinstance(value, dict):
            for field_name in value:
                assert field_name in default_config[section], f"未知字段：{section}.{field_name}"
    print("ok: config.toml consistent with model")


def test_config_schema_general_section() -> None:
  schema = affinity.AffinityPlugin.build_config_schema()
  general = schema["sections"]["general"]
  assert general["title"] == "好感度"
  field_names = set(general["fields"])
  assert {"admin_qq_ids", "refresh_admin_only", "recent_messages_limit", "dimensions"} <= field_names
  assert "general" not in schema["sections"] or schema["sections"].get("general", {}).get("title") != "通用设置"
  print("ok: WebUI general section exposes affinity settings")


def test_config_dimensions_migration() -> None:
    legacy = {
        "plugin": {"enabled": True, "config_version": affinity.CURRENT_CONFIG_VERSION},
        "general": {"total_label": "好感度"},
        "dimensions": [{"key": "a", "label": "甲"}],
    }
    migrated = affinity._migrate_config_dict(legacy)
    assert migrated["general"]["dimensions"] == [{"key": "a", "label": "甲"}]
    assert "dimensions" not in migrated
    hoisted = affinity._hoist_dimensions_for_toml(migrated)
    assert hoisted["dimensions"] == [{"key": "a", "label": "甲"}]
    assert "dimensions" not in hoisted.get("general", {})
    print("ok: dimensions migrate between root TOML and general model")




def test_webui_blank_optional_scalars_normalize() -> None:
    """WebUI 清空 Optional 数值字段会提交空字符串，应视为留空跟随内置默认。"""
    inst = affinity.create_plugin()
    normalized, _ = inst.normalize_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": affinity.CURRENT_CONFIG_VERSION},
            "general": {"radar_top_n": "", "default_score": "  ", "allow_query_others": ""},
            "image": {"jpg_quality": ""},
        }
    )
    assert all(value is not None for value in _flatten_values(normalized))
    assert "radar_top_n" not in normalized.get("general", {})
    assert "jpg_quality" not in normalized.get("image", {})
    print("ok: webui blank optional scalars normalize to defaults")


def test_none_values_never_persisted() -> None:
    """模型默认 None 不得出现在落盘字典中（tomlkit / WebUI 切换启用状态无法序列化 None）。"""
    inst = affinity.create_plugin()
    normalized, _ = inst.normalize_plugin_config({})
    assert all(value is not None for value in _flatten_values(normalized))
    print("ok: empty input normalizes without None for persist")


def test_resolve_dimensions_default() -> None:
    dims = affinity.resolve_dimensions(None)
    keys = [d.key for d in dims]
    assert len(keys) == 22, keys
    assert keys[:5] == ["familiarity", "trust", "joy", "peace", "clinginess"]
    assert {"abstract", "quality", "intelligence", "chaos", "chuuni"} <= set(keys)
    # 空列表也回退默认
    assert affinity.resolve_dimensions([]) == dims
    # 自定义 + 去重
    custom = affinity.resolve_dimensions(
        [{"key": "a", "label": "甲"}, {"key": "a", "label": "重复"}, {"key": "b", "label": "乙"}]
    )
    assert [d.key for d in custom] == ["a", "b"]
    print("ok: resolve_dimensions default (22) & dedup")


def test_effective_helpers() -> None:
    assert affinity._eint(None, 5) == 5
    assert affinity._eint(3, 5) == 3
    assert affinity._efloat(None, 5.0) == 5.0
    assert affinity._estr(None, "x") == "x"
    assert affinity._estr("  ", "x") == "x"
    assert affinity._estr(" y ", "x") == "y"
    assert affinity._ebool(None, True) is True
    assert affinity._ebool(False, True) is False
    print("ok: effective config helpers")


def test_svg_generation() -> None:
    dims = [("熟悉", 8.0), ("信赖", 3.0), ("欢乐", 12.0), ("省心", -4.0), ("贴贴", 6.0)]
    radar = affinity.build_radar_svg(dims, scale_max=10.0)
    assert radar.startswith("<svg") and radar.endswith("</svg>")
    assert "radar-sector-pos" in radar and "radar-sector-neg" in radar and "axis-label" in radar
    # 越界 / 负值不应导致空输出
    assert affinity.build_radar_svg([("x", 99.0)], scale_max=10.0).startswith("<svg")
    assert affinity.build_radar_svg([], scale_max=10.0) == ""

    # 量表条：标准范围、向右越界、向左（负值）越界
    gauge_norm = affinity.build_gauge_bar_svg(7.0, 10.0, 0.0)
    assert gauge_norm.startswith("<svg") and "gauge-bar-fill" in gauge_norm
    assert "gauge-bar-tick-label" not in gauge_norm
    assert "gauge-bar-arrow" not in gauge_norm
    assert "gauge-bar-zone" not in gauge_norm
    gauge_over = affinity.build_gauge_bar_svg(13.0, 10.0, 0.0)
    assert "gauge-bar-over" in gauge_over
    gauge_neg = affinity.build_gauge_bar_svg(-2.0, 10.0, 0.0)
    assert "gauge-bar-neg" in gauge_neg
    # 极端越界：游标不再夹边，不出现箭头
    assert "gauge-bar-arrow" not in affinity.build_gauge_bar_svg(99.0, 10.0, 0.0)
    assert "gauge-bar-arrow" not in affinity.build_gauge_bar_svg(-99.0, 10.0, 0.0)
    print("ok: svg generation (radar sectors + gauge bar overflow)")


def test_embedded_font_css() -> None:
    css = affinity._embedded_font_face_css()
    assert "@font-face" in css and "Noto Sans SC" in css
    assert "fonts.googleapis.com" not in css
    wrapped = affinity._wrap_card_html_for_render('<div id="card">x</div>')
    assert wrapped.startswith("<!DOCTYPE html>")
    assert "fonts.googleapis.com" not in wrapped
    print("ok: embedded font css (offline, no google fonts)")


def test_templates_render_clean() -> None:
    placeholders = [
        "card_title", "avatar_html", "nickname", "alias_block", "cardname_block",
        "bot_name", "impression_title",
        "total_label", "total_value", "gauge_bar", "radar_svg", "legend_html", "description",
    ]
    values = {p: f"<{p}>" for p in placeholders}
    for name in ("parchment.html", "holo.html", "cute.html"):
        template = (PLUGIN_DIR / "assets" / name).read_text(encoding="utf-8")
        rendered = affinity._render(template, **values)
        # 渲染后不应残留任何已知占位符
        for p in placeholders:
            assert "{" + p + "}" not in rendered, f"{name} 残留占位符 {p}"
        assert 'id="card"' in rendered
    print("ok: all card templates render without leftover placeholders")


def _card_png(magenta_box=None) -> bytes:
    """造一张测试卡片 PNG；magenta_box 给定时在该处画洋红占位（模拟头像色键洞）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 196, 116], radius=12, fill=(40, 80, 160, 255))
    if magenta_box is not None:
        draw.ellipse(magenta_box, fill=(255, 0, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _animated_avatar_gif(frames=4) -> bytes:
    """造一个多帧 GIF 头像。"""
    from PIL import Image

    imgs = []
    for i in range(frames):
        shade = 30 + i * 50
        imgs.append(Image.new("RGB", (64, 64), (shade, 20, 200 - i * 30)))
    buf = BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=70, loop=0)
    return buf.getvalue()


def _static_avatar_png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), (200, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_avatar_frame_extraction() -> None:
    # 静态头像 → 单帧、非动图
    frames, durs, animated = affinity.extract_avatar_frames(
        _static_avatar_png(), max_frames=30, fallback_duration_ms=80
    )
    assert len(frames) == 1 and animated is False
    # 动图头像 → 多帧、is_animated
    frames, durs, animated = affinity.extract_avatar_frames(
        _animated_avatar_gif(4), max_frames=30, fallback_duration_ms=80
    )
    assert animated is True and len(frames) == 4 and len(durs) == 4
    # 抽样：帧数超过上限
    frames, durs, animated = affinity.extract_avatar_frames(
        _animated_avatar_gif(10), max_frames=4, fallback_duration_ms=80
    )
    assert animated is True and len(frames) == 4
    # 坏数据不炸
    assert affinity.extract_avatar_frames(b"not-an-image", max_frames=30, fallback_duration_ms=80) == ([], [], False)
    print("ok: avatar frame extraction (static / animated / sampled / garbage)")


def test_chroma_composite() -> None:
    from PIL import Image

    base = Image.open(BytesIO(_card_png(magenta_box=[20, 20, 100, 100]))).convert("RGBA")
    mask, box = affinity._chroma_mask(base)
    assert box is not None
    avatar = Image.new("RGBA", (64, 64), (0, 200, 0, 255))  # 纯绿头像
    out = affinity.composite_avatar_over_card(base, avatar)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    r, g, b, _ = out.getpixel((cx, cy))
    # 洞中心应被绿色头像替换，不再是洋红
    assert g > 150 and r < 100 and b < 100, (r, g, b)
    print("ok: chroma-key composites avatar into placeholder hole")


def test_static_encoding() -> None:
    png = _card_png()
    cases = {"webp": b"RIFF", "png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff"}
    for fmt, magic in cases.items():
        data, mime = affinity.encode_static_card(
            png, static_format=fmt, jpg_quality=85, webp_quality=85, background_color="#12131a"
        )
        assert data[: len(magic)] == magic, f"{fmt} 魔数不符: {data[:8]!r}"
        assert mime
    webp, _ = affinity.encode_static_card(
        png, static_format="webp", jpg_quality=85, webp_quality=85, background_color="#12131a"
    )
    assert webp[8:12] == b"WEBP"
    print("ok: static encoding webp/png/jpg")


def test_animated_encoding() -> None:
    base = _card_png(magenta_box=[20, 20, 100, 100])
    frames, durs, _ = affinity.extract_avatar_frames(_animated_avatar_gif(4), max_frames=30, fallback_duration_ms=80)
    cases = {"animated_webp": b"RIFF", "apng": b"\x89PNG\r\n\x1a\n", "gif": b"GIF8"}
    for fmt, magic in cases.items():
        data, mime = affinity.encode_animated_card(
            base, frames, durs, animated_format=fmt, loop=0, webp_quality=85, background_color="#12131a"
        )
        assert data[: len(magic)] == magic, f"{fmt} 魔数不符: {data[:8]!r}"
        assert mime
    print("ok: animated encoding animated_webp/apng/gif (avatar frames composited)")


def test_helpers() -> None:
    assert affinity._fmt_num(5.0) == "5"
    assert affinity._fmt_num(5.5) == "5.5"
    guidance = affinity._score_guidance_text(default_score=7.0, scale_min=0.0, scale_max=20.0)
    assert "7" in guidance and "0–20" in guidance
    radar_mid = affinity.build_radar_svg([("A", 5.0)], scale_max=10.0, scale_min=0.0)
    assert radar_mid.startswith("<svg")
    radar_span = affinity.build_radar_svg([("A", 5.0)], scale_max=15.0, scale_min=-5.0)
    assert radar_span.startswith("<svg")
    assert affinity._fmt_delta(3) == "+3"
    assert affinity._fmt_delta(-2.5) == "-2.5"
    assert affinity._extract_json_object('前言 {"a": 1} 后语') == {"a": 1}
    assert affinity._extract_json_object("```json\n{\"x\": 2}\n```") == {"x": 2}
    assert affinity._extract_json_object("no json here") is None
    assert affinity._parse_memory_points('["甲", "乙"]') == ["甲", "乙"]
    assert affinity._parse_memory_points('["性格:爱开玩笑:1.0", "爱好:写代码:0.8"]') == [
        "爱开玩笑",
        "写代码",
    ]
    assert affinity._memory_points_from_profile(
        {"success": True, "summary": "活泼\n话多", "traits": ["活泼", "话多"], "evidence": []}
    ) == ["活泼", "话多"]
    assert affinity._memory_points_from_profile(
        {
            "success": True,
            "summary": "",
            "traits": [],
            "evidence": [{"content": "经常深夜发消息"}],
        }
    ) == ["经常深夜发消息"]
    assert affinity._memory_points_from_knowledge_content(
        "你知道这些知识: 1. 爱开玩笑\n2. 写代码"
    ) == ["爱开玩笑", "写代码"]
    assert affinity._memory_points_from_knowledge_content("你不太了解有关 foo 的知识") == []
    assert affinity._memory_search_queries(
        affinity.PersonRef(
            person_id="p1",
            platform="qq",
            user_id="99",
            user_nickname="Nick",
            person_name="Alias",
            group_cardnames=["CardA"],
        ),
        include_facets=True,
    ) == ["Alias", "Nick", "CardA", "99", "人物印象", "性格特点", "兴趣爱好", "行为习惯", "社交关系", "经历事件"]
    assert affinity._memory_search_queries(
        affinity.PersonRef(
            person_id="p1",
            platform="qq",
            user_id="99",
            user_nickname="Nick",
            person_name="Alias",
            group_cardnames=["CardA"],
        ),
        include_facets=False,
    ) == ["Alias", "Nick", "CardA", "99"]
    assert affinity._format_memory_block(["甲", "乙"], max_chars=100) == "  - 甲\n  - 乙"
    assert affinity._format_memory_block([], max_chars=100) == "  （无）"
    assert affinity._merge_memory_points(["甲"], ["乙", "甲"], max_items=10) == ["甲", "乙"]
    assert affinity._parse_group_cardnames(
        '[{"group_id": "1", "group_cardname": "小麦"}, {"group_id": "2", "group_cardname": "大麦"}]'
    ) == ["小麦", "大麦"]
    assert affinity._parse_group_cardnames("单群名片") == ["单群名片"]
    assert affinity._text_byte_len("你好") == 6
    assert affinity._text_within_limit("abc", 3, "chars")
    assert affinity._limit_label(256, "chars") == "256 字符"
    assert affinity._limit_label(81920, "bytes") == "81920 字节"
    assert affinity._truncate_text("abcdef", 4, "chars") == "abc…"
    assert affinity._normalize_person_target("@kes") == "kes"
    assert affinity._normalize_person_target("「demonte」") == "demonte"
    assert affinity._normalize_person_target("别名：kes") == "kes"
    assert affinity._names_equal("Kes", "kes")
    ref = affinity.PersonRef(
        person_id="pid-abc",
        platform="qq",
        user_id="123456789",
        user_nickname="DUser",
        person_name="demonte",
        group_cardname_entries=[("111", "小麦"), ("222", "大麦")],
        group_cardnames=["小麦", "大麦"],
    )
    identities = affinity._format_person_identities(ref, stored_display_name="小麦")
    assert "123456789" in identities
    assert "DUser" in identities
    assert "demonte" in identities
    assert "群 111" in identities
    assert "均指同一人" in identities
    info = {"person_name": "demonte", "user_nickname": "DUser", "group_cardname": '[{"group_cardname": "kes"}]'}
    assert affinity._person_info_matches_alias(info, "kes")
    assert affinity._person_info_matches_alias(info, "demonte")
    entries = affinity._parse_group_cardname_entries(info["group_cardname"])
    assert entries == [("", "kes")]
    assert affinity.DEFAULT_DESCRIPTION_SIZE_LIMIT == 256
    assert affinity.DEFAULT_IMPRESSION_NOTE_SIZE_LIMIT == 81920
    assert affinity.DEFAULT_RECENT_MESSAGES_LIMIT == 512
    assert affinity.DEFAULT_PERSISTENT_IMPRESSION is True
    assert affinity._sniff_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    print("ok: misc helpers")


def test_identity_resolution() -> None:
    # 顶层 kwargs（命令 / 工具执行器实际传入的形态）
    platform, uid, gid = affinity._caller_identity(
        {"platform": "qq", "user_id": "123", "group_id": "456"}
    )
    assert (platform, uid, gid) == ("qq", "123", "456")
    # 兜底从 message 字典的 message_info 取
    platform, uid, gid = affinity._caller_identity(
        {"message": {"platform": "qq", "message_info": {"user_info": {"user_id": "789"}, "group_info": {"group_id": "111"}}}}
    )
    assert (uid, gid) == ("789", "111")
    # @ 段：raw_message 里 type=at → data.target_user_id
    at_uid = affinity._extract_target_user_id(
        {"message": {"raw_message": [{"type": "text", "data": {}}, {"type": "at", "data": {"target_user_id": "999"}}]}}
    )
    assert at_uid == "999"
    # 无 @ 时回退到引用回复的发送者
    reply_uid = affinity._extract_target_user_id(
        {"message": {"raw_message": [{"type": "reply", "data": {"target_message_sender_id": "888"}}]}}
    )
    assert reply_uid == "888"
    print("ok: identity resolution from top-level kwargs / message dict / at / reply")


def test_command_admin_permission() -> None:
    inst = affinity.create_plugin()
    inst._admin_qq_ids = affinity._parse_admin_qq_ids(["10001", "10002"])
    inst._refresh_admin_only = True
    kwargs = {"platform": "qq", "user_id": "10001"}
    assert inst._refresh_permission_error(kwargs) == ""
    kwargs = {"platform": "qq", "user_id": "99999"}
    assert inst._refresh_permission_error(kwargs) == "只有管理员可以使用 /刷新印象 哦。"
    inst._refresh_admin_only = False
    assert inst._refresh_permission_error(kwargs) == ""
    inst._refresh_admin_only = True
    inst._admin_qq_ids = frozenset()
    assert "admin_qq_ids" in inst._refresh_permission_error(kwargs)
    assert affinity._parse_admin_qq_ids([" 123 ", "abc", "456"]) == frozenset({"123", "456"})
    assert affinity.DEFAULT_COLD_START_TEMPERATURE == 0.3
    assert affinity.DEFAULT_REFRESH_ADMIN_ONLY is True
    legacy = {"general": {"commands_admin_only": False}}
    migrated = affinity._migrate_config_dict(legacy)
    assert migrated["general"]["refresh_admin_only"] is False
    assert "commands_admin_only" not in migrated["general"]
    print("ok: refresh impression admin permission")


def test_select_radar_dimensions() -> None:
    dims = [("A", 9.0), ("B", 8.0), ("C", 5.5), ("D", 1.0), ("E", 5.0)]
    assert affinity.select_radar_dimensions(dims, top_n=3) == [("A", 9.0), ("B", 8.0), ("C", 5.5)]
    with_neg = [("A", 9.0), ("B", 8.0), ("C", 5.0), ("D", 4.0), ("E", -2.0)]
    assert affinity.select_radar_dimensions(with_neg, top_n=5) == [
        ("A", 9.0),
        ("B", 8.0),
        ("C", 5.0),
        ("D", 4.0),
        ("E", -2.0),
    ]
    assert affinity.select_radar_dimensions(with_neg, top_n=3) == [("A", 9.0), ("B", 8.0), ("E", -2.0)]
    print("ok: select_radar_dimensions")


def test_parse_radar_top_n_arg() -> None:
    assert affinity._parse_radar_top_n_arg("") == ("", "")
    assert affinity._parse_radar_top_n_arg("demonte") == ("demonte", "")
    assert affinity._parse_radar_top_n_arg("demonte 雷达:8") == ("demonte", "8")
    assert affinity._parse_radar_top_n_arg("雷达维度:3") == ("", "3")
    assert affinity._parse_radar_top_n_arg("top_n:5") == ("", "5")
    print("ok: radar top_n arg parsing")


def test_resolve_radar_top_n() -> None:
    inst = affinity.create_plugin()
    inst._radar_top_n = 5
    n, err = inst._resolve_radar_top_n("")
    assert not err and n == 5
    n, err = inst._resolve_radar_top_n("3")
    assert not err and n == 3
    n, err = inst._resolve_radar_top_n("", override=7)
    assert not err and n == 7
    n, err = inst._resolve_radar_top_n("abc")
    assert err and n == 5
    print("ok: resolve radar top_n")


def test_top_dimensions_selection() -> None:
    inst = affinity.create_plugin()
    inst._dimensions = affinity.resolve_dimensions(None)
    inst._default_score = 5.0
    inst._radar_top_n = 3
    record = affinity.AffinityRecord(
        person_id="p1",
        scores={"familiarity": 5.0, "trust": 9.0, "joy": 1.0, "peace": 5.5, "clinginess": 8.0},
    )
    top = inst._top_dimensions(record)
    labels = {label for label, _ in top}
    assert labels == {"信赖", "贴贴", "省心"}, labels
    record.scores["joy"] = -2.0
    top = inst._top_dimensions(record)
    labels = {label for label, _ in top}
    assert labels == {"信赖", "贴贴", "欢乐"}, labels
    print("ok: top dimension selection picks highest and lowest negative")


def test_impression_feedback() -> None:
    """简介写入：空内容不得静默覆盖/清空，且容忍 impression/text 别名。"""
    import asyncio
    import tempfile

    inst = affinity.AffinityPlugin()
    inst.set_plugin_config(affinity.AffinityPlugin.build_default_config())
    tmp = Path(tempfile.mkdtemp(prefix="ic-smoke-"))
    inst._store = affinity.AffinityStore(tmp / "a.db")
    ref = affinity.PersonRef(person_id="p1", platform="qq", user_id="123", person_name="测试君")
    rec = inst._new_record(ref)
    rec.description = "原有的重要印象"
    asyncio.run(inst._store.upsert(rec))

    # 空 rewrite 必须被拒绝且不清空
    asyncio.run(inst._write_description(ref, "   ", mode="rewrite"))
    assert asyncio.run(inst._store.get("p1")).description == "原有的重要印象", "空 rewrite 不得清空简介"
    # 真实 rewrite 仍可写入
    asyncio.run(inst._write_description(ref, "全新的简介", mode="rewrite"))
    assert asyncio.run(inst._store.get("p1")).description == "全新的简介"
    # 空 append 必须被拒绝
    asyncio.run(inst._write_description(ref, "", mode="append"))
    assert asyncio.run(inst._store.get("p1")).description == "全新的简介", "空 append 不应改动简介"
    # 别名归一化
    assert affinity._coalesce_text("", {"impression": "x"}, "impression") == "x"
    assert affinity._coalesce_text("p", {"impression": "x"}, "impression") == "p"


def main() -> None:
    test_plugin_importable()
    test_impression_feedback()
    test_manifest_capabilities_cover_usage()
    test_config_toml_consistent()
    test_config_schema_general_section()
    test_config_dimensions_migration()
    test_none_values_never_persisted()
    test_webui_blank_optional_scalars_normalize()
    test_resolve_dimensions_default()
    test_effective_helpers()
    test_svg_generation()
    test_embedded_font_css()
    test_templates_render_clean()
    test_avatar_frame_extraction()
    test_chroma_composite()
    test_static_encoding()
    test_animated_encoding()
    test_helpers()
    test_parse_radar_top_n_arg()
    test_resolve_radar_top_n()
    test_select_radar_dimensions()
    test_identity_resolution()
    test_command_admin_permission()
    test_top_dimensions_selection()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
