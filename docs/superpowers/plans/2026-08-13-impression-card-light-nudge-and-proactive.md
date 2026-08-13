# Impression Card Light Nudge + Periodic Reminder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional small LLM delta-nudge before `/卡片` (and as `nudge_impression`), plus an independent 6-hour Maisaka reminder in active chats.

**Architecture:** Keep the existing single-file plugin. Add pure helpers + SQLite `proactive_tick` + one `_nudge_record` engine used by `/卡片` and the new tool. A RSS-style asyncio loop appends a roster briefing and calls `maisaka.proactive.trigger`. No Host/SDK edits.

**Tech Stack:** Python 3.10+, `maibot-plugin-sdk` (sibling `../maibot-plugin-sdk`), plugin-local SQLite, Host capabilities `llm.generate` / `message.*` / `chat.get_all_streams` / `maisaka.*`. Offline verification: `PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py`.

**Spec:** `docs/superpowers/specs/2026-08-13-impression-card-light-nudge-and-proactive-design.md`

## Global Constraints

- Plugin-only; do not edit `MaiBot/` or `maibot-plugin-sdk/`
- Do not split `plugin.py`
- Do not hand-edit a user's live `config.toml`; bump `CURRENT_CONFIG_VERSION` to `"0.3.0"` and follow empty-field defaults
- Plugin / `_manifest.json` version `0.3.0`
- User-facing text, log messages, WebUI `description`, default prompts: 简体中文
- `light_refresh.enabled` gates **only** `/卡片`; `nudge_impression` stays callable
- `nudge_impression` must **not** cold-start; no record → error string
- No 【系统通知】 on the light-nudge path
- Light-nudge LLM timeout is existing `[general] llm_rpc_timeout_ms` (default 120000); no new timeout knob
- `max_abs_delta` default `0` means no clamp
- `send_impression_card` / `/刷新印象` / `refresh_impression` behavior unchanged (`refresh_first` stays heavy)
- No cooldown on `/卡片`; no extra “正在刷新…” chat line
- First-seen stream: stamp `last_fired_at = now`, do not fire
- Loop poll default 300s; interval default 6 hours
- Do not restart MaiBot / docker

## File map

| File | Role |
|---|---|
| `plugin.py` | Defaults, config models, helpers, store table, engine, tool, `/卡片` hook, poll loop |
| `tests/smoke_test.py` | Offline tests; extend `main()`; fix maisaka capability scan |
| `config.default.toml` | Commented `[light_refresh]` and `[proactive]` placeholders; `config_version = "0.3.0"` |
| `_manifest.json` | `version` `0.3.0`; new capabilities |
| `README.md` | Document both features |
| `CHANGELOG.md` | `[0.3.0]` entry |

---

### Task 1: Config schema, versions, shipped template

**Files:**
- Modify: `plugin.py` (`CURRENT_CONFIG_VERSION`, new defaults, `LightRefreshSectionConfig`, `ProactiveSectionConfig`, `AffinityPluginConfig`, `_refresh_config`, `AffinityPlugin.__init__`)
- Modify: `config.default.toml`
- Modify: `_manifest.json` (`version` only in this task)
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: existing `PluginConfigBase` / `Field` / `_ebool` / `_eint` / `_efloat` / `_estr` / `_etmpl` pattern
- Produces: `CURRENT_CONFIG_VERSION = "0.3.0"`; `self.config.light_refresh` and `self.config.proactive`; cached `_light_refresh_enabled`, `_light_recent_messages_limit`, `_light_recent_hours`, `_light_max_abs_delta`, `_proactive_enabled`, `_proactive_interval_hours`, `_proactive_poll_seconds`, `_proactive_max_briefing_people`, `_proactive_max_streams_per_tick`

- [ ] **Step 1: Write the failing tests**

Add to `tests/smoke_test.py` (and call them from `main()` before `print("\n全部冒烟测试通过")`):

```python
def test_config_version_0_3_0() -> None:
    assert affinity.CURRENT_CONFIG_VERSION == "0.3.0"
    default_config = affinity.AffinityPlugin.build_default_config()
    assert default_config["plugin"]["config_version"] == "0.3.0"
    shipped = tomllib.loads((PLUGIN_DIR / "config.default.toml").read_text(encoding="utf-8"))
    assert shipped["plugin"]["config_version"] == "0.3.0"
    print("ok: config_version 0.3.0")


def test_light_refresh_and_proactive_config_defaults() -> None:
    default_config = affinity.AffinityPlugin.build_default_config()
    assert "light_refresh" in default_config
    assert "proactive" in default_config
    lr = default_config["light_refresh"]
    pr = default_config["proactive"]
    # Optional fields stay None so empty TOML follows code defaults
    assert lr["enabled"] is None
    assert lr["recent_messages_limit"] is None
    assert lr["recent_hours"] is None
    assert lr["max_abs_delta"] is None
    assert lr["model"] is None or lr["model"] in ("", None)
    assert pr["enabled"] is None
    assert pr["interval_hours"] is None
    assert pr["poll_seconds"] is None
    schema = affinity.AffinityPlugin.build_config_schema()
    assert schema["sections"]["light_refresh"]["title"] == "查询微调"
    assert schema["sections"]["proactive"]["title"] == "定期提醒"
    inst = affinity.create_plugin()
    inst._refresh_config()
    assert inst._light_refresh_enabled is True
    assert inst._light_recent_messages_limit == 48
    assert inst._light_recent_hours == 6
    assert inst._light_max_abs_delta == 0.0
    assert inst._proactive_enabled is True
    assert inst._proactive_interval_hours == 6
    assert inst._proactive_poll_seconds == 300
    assert inst._proactive_max_briefing_people == 12
    assert inst._proactive_max_streams_per_tick == 5
    print("ok: light_refresh / proactive config defaults")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /mnt/klein/work/maibot-plugins/maibot-impression-card-plugin
PYTHONPATH=../maibot-plugin-sdk python -c "import tests.smoke_test as t; t.test_config_version_0_3_0()"
```

Expected: FAIL (`CURRENT_CONFIG_VERSION` is still `"0.2.3"` or AttributeError on missing sections).

- [ ] **Step 3: Add defaults and config models in `plugin.py`**

Next to the other `DEFAULT_*` constants (after `DEFAULT_REFRESH_ADMIN_ONLY` is fine):

```python
CURRENT_CONFIG_VERSION = "0.3.0"

DEFAULT_LIGHT_REFRESH_ENABLED = True
DEFAULT_LIGHT_RECENT_MESSAGES_LIMIT = 48
DEFAULT_LIGHT_RECENT_HOURS = 6
DEFAULT_LIGHT_TEMPERATURE = 0.4
DEFAULT_LIGHT_MAX_TOKENS = 0
DEFAULT_LIGHT_MAX_ABS_DELTA = 0.0
DEFAULT_LIGHT_MODEL = ""  # empty → cold_start.model

DEFAULT_PROACTIVE_ENABLED = True
DEFAULT_PROACTIVE_INTERVAL_HOURS = 6
DEFAULT_PROACTIVE_POLL_SECONDS = 300
DEFAULT_PROACTIVE_MAX_BRIEFING_PEOPLE = 12
DEFAULT_PROACTIVE_MAX_STREAMS_PER_TICK = 5
```

Leave prompt/intent template constants for later tasks (empty-string `_etmpl` fallbacks can land with the prompt task). For this task, still declare the config fields `prompt_template` / `intent_template` / `briefing_template` as `str = Field(default="")` so the schema exists.

After `NotifySectionConfig`, add:

```python
class LightRefreshSectionConfig(PluginConfigBase):
    __ui_label__ = "查询微调"
    __ui_icon__ = "sparkle"
    __ui_order__ = 7

    enabled: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="查询 /卡片 时是否先用小上下文 LLM 微调分值与简介。关闭则恢复仅渲染已存档案。",
    )
    recent_messages_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LIGHT_RECENT_MESSAGES_LIMIT)},
        description="微调时参考的最近聊天条数（当前聊天流）。",
    )
    recent_hours: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LIGHT_RECENT_HOURS)},
        description="微调时只看最近多少小时的聊天。",
    )
    model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": "planner"},
        description="微调使用的 LLM 模型任务名；留空则沿用 cold_start.model。",
    )
    temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LIGHT_TEMPERATURE)},
        description="微调采样温度。",
    )
    max_tokens: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LIGHT_MAX_TOKENS)},
        description="微调最大 token；0 为自动。",
    )
    max_abs_delta: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LIGHT_MAX_ABS_DELTA)},
        description="单次每个维度增量绝对值上限；0 表示不限制。",
    )
    prompt_template: str = Field(
        default="",
        json_schema_extra={"placeholder": ""},
        description="微调提示词模板；留空使用内置默认。",
    )


class ProactiveSectionConfig(PluginConfigBase):
    __ui_label__ = "定期提醒"
    __ui_icon__ = "clock"
    __ui_order__ = 8

    enabled: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="是否定期向麦麦规划器提醒检查印象卡片（与查询微调开关独立）。",
    )
    interval_hours: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_PROACTIVE_INTERVAL_HOURS)},
        description="每个聊天流的提醒间隔（小时）。只对间隔内有过消息的聊天触发。",
    )
    poll_seconds: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_PROACTIVE_POLL_SECONDS)},
        description="扫描聊天流的间隔（秒）。",
    )
    max_briefing_people: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_PROACTIVE_MAX_BRIEFING_PEOPLE)},
        description="写入规划器简报的最近发言者人数上限。",
    )
    max_streams_per_tick: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_PROACTIVE_MAX_STREAMS_PER_TICK)},
        description="每次扫描最多唤醒多少个聊天流。",
    )
    intent_template: str = Field(
        default="",
        description="proactive.trigger 的 intent 模板；留空使用内置默认。",
    )
    briefing_template: str = Field(
        default="",
        description="注入规划器上下文的简报模板；留空使用内置默认。",
    )
```

Add both to `AffinityPluginConfig`:

```python
    light_refresh: LightRefreshSectionConfig = Field(default_factory=LightRefreshSectionConfig)
    proactive: ProactiveSectionConfig = Field(default_factory=ProactiveSectionConfig)
```

In `AffinityPlugin.__init__`, initialize the cached fields to the `DEFAULT_*` values above.

In `_refresh_config`, after the general block:

```python
        lr = cfg.light_refresh
        self._light_refresh_enabled = _ebool(lr.enabled, DEFAULT_LIGHT_REFRESH_ENABLED)
        self._light_recent_messages_limit = _eint(
            lr.recent_messages_limit, DEFAULT_LIGHT_RECENT_MESSAGES_LIMIT, minimum=0
        )
        self._light_recent_hours = _eint(lr.recent_hours, DEFAULT_LIGHT_RECENT_HOURS, minimum=1)
        self._light_max_abs_delta = _efloat(lr.max_abs_delta, DEFAULT_LIGHT_MAX_ABS_DELTA)
        pr = cfg.proactive
        self._proactive_enabled = _ebool(pr.enabled, DEFAULT_PROACTIVE_ENABLED)
        self._proactive_interval_hours = _eint(
            pr.interval_hours, DEFAULT_PROACTIVE_INTERVAL_HOURS, minimum=1
        )
        self._proactive_poll_seconds = _eint(
            pr.poll_seconds, DEFAULT_PROACTIVE_POLL_SECONDS, minimum=30
        )
        self._proactive_max_briefing_people = _eint(
            pr.max_briefing_people, DEFAULT_PROACTIVE_MAX_BRIEFING_PEOPLE, minimum=1
        )
        self._proactive_max_streams_per_tick = _eint(
            pr.max_streams_per_tick, DEFAULT_PROACTIVE_MAX_STREAMS_PER_TICK, minimum=1
        )
```

`create_plugin()` already calls `_refresh_config` only in `on_load`. The test calls `inst._refresh_config()` after `create_plugin()`; `self.config` must exist. `create_plugin()` + `config_model` usually injects defaults. If `_refresh_config` fails because config is empty, the test's `create_plugin()` path already works today for `_refresh_config` in other tests (`test_command_admin_permission`). Follow that.

- [ ] **Step 4: Update `config.default.toml` and `_manifest.json`**

Set `config_version = "0.3.0"`. Append commented sections (same style as `[notify]`):

```toml
[light_refresh]
# enabled = true
# recent_messages_limit = 48
# recent_hours = 6
# model = ""
# temperature = 0.4
# max_tokens = 0
# max_abs_delta = 0
# prompt_template = ""

[proactive]
# enabled = true
# interval_hours = 6
# poll_seconds = 300
# max_briefing_people = 12
# max_streams_per_tick = 5
# intent_template = ""
# briefing_template = ""
```

In `_manifest.json` set `"version": "0.3.0"`. Do not add capabilities yet (loop/engine not calling them).

- [ ] **Step 5: Run smoke tests**

```bash
cd /mnt/klein/work/maibot-plugins/maibot-impression-card-plugin
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: PASS (including the two new tests).

- [ ] **Step 6: Commit**

```bash
git add plugin.py tests/smoke_test.py config.default.toml _manifest.json
git commit -m "$(cat <<'EOF'
Add light-refresh and proactive config schema (v0.3.0).

EOF
)"
```

---

### Task 2: Pure nudge apply helpers

**Files:**
- Modify: `plugin.py` (module-level helpers near `_extract_json_object` / `_coerce_float`)
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: `AffinityRecord`, `Dimension`, `_coerce_float`, `_is_number`
- Produces:
  - `should_light_nudge_on_card(*, enabled: bool, has_record: bool) -> bool`
  - `should_ignore_nudge_description(*, persistent_impression: bool, description: str, size_limit: int) -> bool`
  - `parse_nudge_payload(parsed: Optional[Mapping[str, Any]], *, allowed_keys: frozenset[str], max_abs_delta: float) -> tuple[dict[str, float], Optional[str]]`
  - `apply_nudge_to_record(record: AffinityRecord, deltas: Mapping[str, float], new_description: Optional[str], *, ignore_description: bool, default_score: float) -> tuple[AffinityRecord, list[tuple[str, float]], bool]`

- [ ] **Step 1: Write the failing tests**

```python
def test_should_light_nudge_on_card() -> None:
    assert affinity.should_light_nudge_on_card(enabled=True, has_record=True) is True
    assert affinity.should_light_nudge_on_card(enabled=True, has_record=False) is False
    assert affinity.should_light_nudge_on_card(enabled=False, has_record=True) is False
    print("ok: should_light_nudge_on_card")


def test_parse_and_apply_nudge_payload() -> None:
    allowed = frozenset({"total", "joy", "trust"})
    rec = affinity.AffinityRecord(
        person_id="p1",
        total=5.0,
        scores={"joy": 5.0, "trust": 5.0},
        description="旧简介",
    )
    parsed = {"deltas": {"total": 0.3, "joy": -0.2, "nope": 9, "trust": "x"}, "description": "新简介"}
    deltas, desc = affinity.parse_nudge_payload(parsed, allowed_keys=allowed, max_abs_delta=0.0)
    assert deltas["total"] == 0.3
    assert deltas["joy"] == -0.2
    assert "nope" not in deltas
    assert "trust" not in deltas  # non-numeric ignored
    assert desc == "新简介"
    rec2, applied, note_changed = affinity.apply_nudge_to_record(
        rec, deltas, desc, ignore_description=False, default_score=5.0
    )
    assert rec2.total == 5.3
    assert rec2.scores["joy"] == 4.8
    assert rec2.scores["trust"] == 5.0
    assert rec2.description == "新简介"
    assert note_changed is True
    assert ("total", 0.3) in applied and ("joy", -0.2) in applied

    none_d, none_desc = affinity.parse_nudge_payload(None, allowed_keys=allowed, max_abs_delta=0.0)
    assert none_d == {} and none_desc is None
    rec3, applied3, note3 = affinity.apply_nudge_to_record(
        rec, {}, None, ignore_description=False, default_score=5.0
    )
    assert rec3.total == 5.0 and rec3.description == "旧简介" and applied3 == [] and note3 is False
    print("ok: parse/apply nudge")


def test_nudge_max_abs_delta_and_long_note_guard() -> None:
    allowed = frozenset({"total"})
    deltas, _ = affinity.parse_nudge_payload(
        {"deltas": {"total": 9.0}}, allowed_keys=allowed, max_abs_delta=2.0
    )
    assert deltas["total"] == 2.0
    deltas0, _ = affinity.parse_nudge_payload(
        {"deltas": {"total": 9.0}}, allowed_keys=allowed, max_abs_delta=0.0
    )
    assert deltas0["total"] == 9.0
    short = "短"
    long = "x" * 300
    assert affinity.should_ignore_nudge_description(
        persistent_impression=True, description=long, size_limit=256
    ) is True
    assert affinity.should_ignore_nudge_description(
        persistent_impression=True, description=short, size_limit=256
    ) is False
    assert affinity.should_ignore_nudge_description(
        persistent_impression=False, description=long, size_limit=256
    ) is False
    rec = affinity.AffinityRecord(person_id="p", total=1.0, scores={}, description=long)
    rec2, _, note_changed = affinity.apply_nudge_to_record(
        rec, {"total": 1.0}, "会被忽略", ignore_description=True, default_score=5.0
    )
    assert rec2.total == 2.0
    assert rec2.description == long
    assert note_changed is False
    print("ok: max_abs_delta and long-note guard")
```

- [ ] **Step 2: Run to verify fail**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "import tests.smoke_test as t; t.test_should_light_nudge_on_card()"
```

Expected: FAIL `AttributeError: should_light_nudge_on_card`.

- [ ] **Step 3: Implement helpers in `plugin.py`**

Place after `_coerce_float`:

```python
def should_light_nudge_on_card(*, enabled: bool, has_record: bool) -> bool:
    return bool(enabled) and bool(has_record)


def should_ignore_nudge_description(
    *, persistent_impression: bool, description: str, size_limit: int
) -> bool:
    if not persistent_impression:
        return False
    return len(description) > int(size_limit)


def parse_nudge_payload(
    parsed: Optional[Mapping[str, Any]],
    *,
    allowed_keys: frozenset[str],
    max_abs_delta: float,
) -> tuple[dict[str, float], Optional[str]]:
    if not isinstance(parsed, Mapping):
        return {}, None
    raw_deltas = parsed.get("deltas")
    deltas: dict[str, float] = {}
    if isinstance(raw_deltas, Mapping):
        for key, value in raw_deltas.items():
            name = str(key).strip()
            if name not in allowed_keys:
                continue
            if not _is_number(value):
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                num = float(value)
            if max_abs_delta > 0:
                limit = float(max_abs_delta)
                num = max(-limit, min(limit, num))
            if num == 0:
                continue
            deltas[name] = num
    description_raw = parsed.get("description")
    description: Optional[str]
    if description_raw is None:
        description = None
    else:
        text = str(description_raw).strip()
        description = text or None
    return deltas, description


def apply_nudge_to_record(
    record: AffinityRecord,
    deltas: Mapping[str, float],
    new_description: Optional[str],
    *,
    ignore_description: bool,
    default_score: float,
) -> tuple[AffinityRecord, list[tuple[str, float]], bool]:
    applied: list[tuple[str, float]] = []
    for key, delta in deltas.items():
        if key == "total":
            record.total = float(record.total) + float(delta)
        else:
            old = float(record.scores.get(key, default_score))
            record.scores[key] = old + float(delta)
        applied.append((key, float(delta)))
    note_changed = False
    if new_description and not ignore_description:
        record.description = new_description
        note_changed = True
    if applied or note_changed:
        record.updated_at = _now()
    return record, applied, note_changed
```

Place `apply_nudge_to_record` **after** `_now` (around line 3249) because it stamps `updated_at`. Keep `should_light_nudge_on_card`, `should_ignore_nudge_description`, and `parse_nudge_payload` next to `_coerce_float`.

`apply_nudge_to_record` signature must include `default_score: float`. For non-`total` keys use `old = float(record.scores.get(key, default_score))`. `"trust": "x"` is skipped because `float("x")` raises in `parse_nudge_payload`.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin.py tests/smoke_test.py
git commit -m "$(cat <<'EOF'
Add delta parse/apply helpers for impression light nudge.

EOF
)"
```

---

### Task 3: Light-nudge engine, `/卡片`, `nudge_impression` tool

**Files:**
- Modify: `plugin.py` (prompt constant, `_nudge_record`, `_light_recent_chat_text`, `cmd_card`, new `@Tool`, `refresh_impression` description, `_impression_help_text`)
- Modify: `README.md` (light-nudge + tool)
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: Task 1 caches; Task 2 helpers; existing `_extract_json_object`, `_load_or_create`, `_generate_and_send_card`, `_resolve_target`, `_gen_locks`, `ctx.llm.generate`
- Produces:
  - `DEFAULT_LIGHT_REFRESH_PROMPT_TEMPLATE: str`
  - `DEFAULT_LIGHT_REFRESH_GUIDANCE` is **not** used (light path has its own prompt)
  - `async def _nudge_record(self, ref: PersonRef, stream_id: str) -> Optional[tuple[AffinityRecord, list[tuple[str, float]], bool]]` — `None` if no stored record
  - `@Tool("nudge_impression")` method `nudge_impression(self, target: str = "", **kwargs) -> dict[str, str]`
  - `cmd_card` calls nudge when `should_light_nudge_on_card` is true

- [ ] **Step 1: Write the failing tests**

```python
def test_light_nudge_prompt_placeholders() -> None:
    tmpl = affinity.DEFAULT_LIGHT_REFRESH_PROMPT_TEMPLATE
    for key in (
        "nickname", "personality", "reply_style", "name", "total_label",
        "scale_min", "scale_max", "default_score", "dimensions_doc",
        "current_scores_block", "current_note", "note_policy",
        "person_identities", "recent_chat", "size_limit",
    ):
        assert "{" + key + "}" in tmpl, key
    assert "deltas" in tmpl
    print("ok: light nudge prompt placeholders")


def test_nudge_impression_tool_declared() -> None:
    inst = affinity.create_plugin()
    names = {c.get("name") for c in inst.get_components()}
    assert "nudge_impression" in names
    refresh = next(c for c in inst.get_components() if c.get("name") == "refresh_impression")
    desc = str((refresh.get("metadata") or {}).get("description") or "")
    assert "nudge_impression" in desc
    help_text = inst._impression_help_text()
    assert "nudge_impression" in help_text
    print("ok: nudge_impression tool declared")
```

- [ ] **Step 2: Run to verify fail**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "import tests.smoke_test as t; t.test_light_nudge_prompt_placeholders()"
```

Expected: FAIL (`DEFAULT_LIGHT_REFRESH_PROMPT_TEMPLATE` missing).

- [ ] **Step 3: Add prompt constant and engine**

After the compact/cold-start prompt constants, add this exact template (keep EN/JA out of scope; this plugin’s prompts are zh-CN only):

```python
DEFAULT_LIGHT_REFRESH_PROMPT_TEMPLATE = """你是印象卡片模块，是名为「{nickname}」的人工智能生命体的一部分。
{nickname}的人格设定：{personality}
{nickname}的表达风格：{reply_style}

这是一次**小幅微调**，不是全面重算。请根据「最近聊天」对群友「{name}」的已有档案做增量调整。
不要给出全新绝对分数；只输出相对变化。没有变化的维度请省略。可以主观、可以毒舌，但优先小步调整。

评分参考区间 {scale_min}–{scale_max}，中间值 {default_score}；实际可越界。各维度均为正向表述。
{dimensions_doc}

当前档案（这是基准，请在此上加减）：
{current_scores_block}
当前印象笔记：
{current_note}
{note_policy}
关于这个人：
{person_identities}
最近的聊天记录（当前会话、较短窗口）：
  - quote 后是消息 id。请辨别不同用户；{nickname} 也是参与者。
{recent_chat}

请只输出一个 JSON 对象（不要额外文字或代码块标记）：
{{"deltas": {{"total": 数字, "维度key": 数字}}, "description": "可选；省略或空字符串表示保留原笔记。若要改笔记，请在原文基础上微调，不要另起炉灶。{size_limit}字以内"}}
deltas 里只放需要改的 key（含 total）。"""
```

Add `_light_recent_chat_text` next to `_recent_chat_text`:

```python
    async def _light_recent_chat_text(self, stream_id: str) -> str:
        if not stream_id or self._light_recent_messages_limit <= 0:
            return ""
        try:
            now = time.time()
            start = now - self._light_recent_hours * 3600
            return await self.ctx.message.build_readable(
                messages=None,
                chat_id=stream_id,
                start_time=start,
                end_time=now,
                limit=self._light_recent_messages_limit,
            )
        except Exception as exc:
            self.ctx.logger.debug("获取微调用最近聊天失败: %s", exc)
            return ""
```

Add `_nudge_record` next to `_refresh_record`. Behavior:

1. `assert self._store is not None`
2. Lock with `self._gen_locks.setdefault(ref.person_id, asyncio.Lock())`
3. `existing = await self._store.get(ref.person_id)`; if `None`, return `None`
4. `self._sync_identity(existing, ref)`
5. For each dimension, `existing.scores.setdefault(dim.key, self._default_score)`
6. Build `current_scores_block` lines: `{total_label}: {total}` then each `{label}: {score}`
7. `size_limit = _eint(self.config.description.size_limit, DEFAULT_DESCRIPTION_SIZE_LIMIT, minimum=1)`
8. `persistent = _ebool(self.config.description.persistent_impression, DEFAULT_PERSISTENT_IMPRESSION)`
9. `ignore = should_ignore_nudge_description(persistent_impression=persistent, description=existing.description, size_limit=size_limit)`
10. `note_policy` = if ignore: `【笔记策略】当前笔记长于卡面上限，这是持久印象期刊。禁止返回 description（或返回也会被忽略）。只调整 deltas。` else: `【笔记策略】可以返回微调后的完整笔记，或省略以保留。`
11. `current_note` = full description if not ignore; else `existing.description[:size_limit] + "…"` (or `（无）`)
12. Personality via `ctx.config.get` same as `_generate_record`
13. `model = _estr(self.config.light_refresh.model, "") or _estr(self.config.cold_start.model, DEFAULT_COLD_START_MODEL)`
14. `temperature = _efloat(self.config.light_refresh.temperature, DEFAULT_LIGHT_TEMPERATURE)`
15. `max_tokens = _eint(self.config.light_refresh.max_tokens, DEFAULT_LIGHT_MAX_TOKENS, minimum=0) or None`
16. `template = _etmpl(self.config.light_refresh.prompt_template, DEFAULT_LIGHT_REFRESH_PROMPT_TEMPLATE)`
17. `allowed = frozenset({"total", *(d.key for d in self._dimensions)})`
18. Call `ctx.llm.generate(prompt=..., model=..., temperature=..., max_tokens=..., timeout_ms=self._llm_rpc_timeout_ms)`
19. On exception: log warning, return `(existing, [], False)` without write (slash path still has the old record). **Wait:** spec says `/卡片` failure → keep stored record. Tool failure → error string, no write. So `_nudge_record` should distinguish. Return a small dataclass:

```python
@dataclass
class NudgeOutcome:
    record: Optional[AffinityRecord]
    applied: list[tuple[str, float]]
    note_changed: bool
    error: str = ""
    missing: bool = False
    failed: bool = False
```

Define `NudgeOutcome` near `AffinityRecord`.

- If no row: `NudgeOutcome(record=None, applied=[], note_changed=False, missing=True, error="还没有这个人的印象档案。")`
- If LLM/JSON fails: `NudgeOutcome(record=existing, applied=[], note_changed=False, failed=True, error="微调印象失败，已保留原档案。")` and **do not upsert**
- If success with empty deltas and no note change: still OK; optional skip upsert if nothing changed (still fine to upsert `updated_at` only when something changed — spec step 9 says `updated_at = now` after apply; `apply_nudge_to_record` already only stamps when applied or note_changed)
- On success with changes: upsert, `_maybe_schedule_storage_compact` if note changed

20. `parsed = _extract_json_object(result.get("response", "")) if result.get("success") else None`
21. `deltas, desc = parse_nudge_payload(parsed, allowed_keys=allowed, max_abs_delta=self._light_max_abs_delta)`
22. If `parsed is None`: failed path above
23. `apply_nudge_to_record(..., ignore_description=ignore, default_score=self._default_score)`
24. Return `NudgeOutcome(record=existing, applied=..., note_changed=..., error="")`

- [ ] **Step 4: Wire `cmd_card` and the tool**

In `cmd_card`, inside the existing `try`, **before** `_generate_and_send_card`:

```python
            if self._store is not None and should_light_nudge_on_card(
                enabled=self._light_refresh_enabled,
                has_record=await self._store.get(ref.person_id) is not None,
            ):
                await self._nudge_record(ref, stream_id)
```

Avoid double `get` if easy: `existing = await self._store.get(...)` then `if should_light_nudge_on_card(enabled=self._light_refresh_enabled, has_record=existing is not None): await self._nudge_record(...)`. `_nudge_record` will get again under the lock; that is OK.

Add the tool next to `refresh_impression`:

```python
    @Tool(
        "nudge_impression",
        description=(
            "用较小的近期聊天上下文，对某人已有印象档案做增量微调（deltas，可选微调简介）。"
            "适合「最近有点变化、但不必全面重算」的场合。没有档案时不要调用（请让用户先 /卡片，或改用 refresh_impression 做冷启动）。"
            "target 规则同 adjust_score。"
        ),
        parameters=[
            _param("target", ToolParamType.STRING, "对象：QQ号 或 名字；省略=当前发言者", False),
        ],
    )
    async def nudge_impression(self, target: str = "", **kwargs: Any) -> dict[str, str]:
        ref, error = await self._resolve_target(target, kwargs)
        if error or ref is None:
            return {"content": error or "解析对象失败。"}
        stream_id = _resolve_stream_id(kwargs)
        outcome = await self._nudge_record(ref, stream_id)
        if outcome.missing or outcome.record is None:
            return {"content": outcome.error or "还没有这个人的印象档案。"}
        if outcome.failed:
            return {"content": outcome.error or "微调失败，已保留原档案。"}
        lines = [f"已微调对 {ref.display_name} 的印象。"]
        if outcome.applied:
            lines.append("增量：")
            labels = {"total": self._total_label, **{d.key: d.label for d in self._dimensions}}
            for key, delta in outcome.applied:
                lines.append(f"- {labels.get(key, key)}　{_fmt_delta(delta)}")
        else:
            lines.append("分值无变化。")
        lines.append("笔记：" + ("已微调" if outcome.note_changed else "未改"))
        lines.append("")
        lines.append(self._render_detail_markdown(ref, outcome.record, scale_note=True))
        return {"content": "\n".join(lines)}
```

Prepend to `refresh_impression` description (the string in `@Tool(...)`): `小幅更新请优先用 nudge_impression；本工具是结合长期记忆的全面重算。`

Update `_impression_help_text` commands/tools lines to mention: `/卡片` 默认会先小幅微调（可在配置关闭）；工具 `nudge_impression`。

- [ ] **Step 5: README**

In `README.md` 功能一览, under `/卡片`, add that when enabled (default) it runs a small LLM delta nudge first; `[light_refresh] enabled = false` restores render-only. Add `nudge_impression` to the tools table. Note: no 【系统通知】; long persistent notes are not overwritten.

- [ ] **Step 6: Run smoke tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: PASS. `test_manifest_capabilities_cover_usage` still passes because this task does not call `chat.*` / `maisaka.proactive` yet. `llm.generate` and `message.build_readable` are already declared.

- [ ] **Step 7: Commit**

```bash
git add plugin.py tests/smoke_test.py README.md
git commit -m "$(cat <<'EOF'
Nudge impression scores on /卡片 and via nudge_impression.

EOF
)"
```

---

### Task 4: `proactive_tick` store, eligibility, briefing formatter

**Files:**
- Modify: `plugin.py` (`AffinityStore._connect` + tick methods; module-level speaker/briefing helpers)
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: same SQLite file as affinity
- Produces:
  - table `proactive_tick(stream_id TEXT PRIMARY KEY, last_fired_at REAL NOT NULL)`
  - `async def get_proactive_last_fired(self, stream_id: str) -> Optional[float]`
  - `async def upsert_proactive_last_fired(self, stream_id: str, last_fired_at: float) -> None`
  - `is_proactive_time_due(last_fired_at: Optional[float], now: float, interval_s: float) -> bool` — `False` when `last_fired_at is None` (first seen is not due)
  - `@dataclass(frozen=True) class SpeakerStub: platform: str; user_id: str; nickname: str`
  - `extract_speakers_newest_first(messages: Sequence[Any], *, bot_user_id: str, max_people: int) -> list[SpeakerStub]`
  - `@dataclass(frozen=True) class BriefingPerson: display_name: str; has_card: bool; total: Optional[float] = None; updated_at: Optional[float] = None`
  - `format_proactive_briefing(people: Sequence[BriefingPerson], *, total_label: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
def test_proactive_tick_store_and_due() -> None:
    import asyncio
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ic-tick-"))
    store = affinity.AffinityStore(tmp / "a.db")
    assert asyncio.run(store.get_proactive_last_fired("s1")) is None
    assert affinity.is_proactive_time_due(None, now=1000.0, interval_s=100.0) is False
    asyncio.run(store.upsert_proactive_last_fired("s1", 100.0))
    assert asyncio.run(store.get_proactive_last_fired("s1")) == 100.0
    assert affinity.is_proactive_time_due(100.0, now=150.0, interval_s=100.0) is False
    assert affinity.is_proactive_time_due(100.0, now=200.0, interval_s=100.0) is True
    store.close()
    print("ok: proactive_tick store")


def test_extract_speakers_and_briefing() -> None:
    messages = [
        {
            "timestamp": "100",
            "platform": "qq",
            "message_info": {"user_info": {"user_id": "bot", "user_nickname": "麦麦"}},
        },
        {
            "timestamp": "300",
            "platform": "qq",
            "message_info": {"user_info": {"user_id": "1", "user_nickname": "甲"}},
        },
        {
            "timestamp": "200",
            "platform": "qq",
            "message_info": {"user_info": {"user_id": "2", "user_nickname": "乙"}},
        },
        {
            "timestamp": "400",
            "platform": "qq",
            "message_info": {"user_info": {"user_id": "1", "user_nickname": "甲"}},
        },
    ]
    speakers = affinity.extract_speakers_newest_first(messages, bot_user_id="bot", max_people=12)
    assert [s.user_id for s in speakers] == ["1", "2"]
    capped = affinity.extract_speakers_newest_first(messages, bot_user_id="bot", max_people=1)
    assert [s.user_id for s in capped] == ["1"]
    people = [
        affinity.BriefingPerson(display_name="甲", has_card=True, total=8.5, updated_at=1.0),
        affinity.BriefingPerson(display_name="乙", has_card=False),
    ]
    text = affinity.format_proactive_briefing(people, total_label="好感度")
    assert "甲" in text and "8.5" in text and "乙" in text and "尚无档案" in text
    print("ok: speakers and briefing")
```

`tests/smoke_test.py` already uses `asyncio` in `test_impression_feedback`; add `import asyncio` at the top of the test file if not present (it currently does `import asyncio` inside that test — check and use a top-level import if you add more).

- [ ] **Step 2: Run to verify fail**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "import tests.smoke_test as t; t.test_proactive_tick_store_and_due()"
```

Expected: FAIL missing `get_proactive_last_fired`.

- [ ] **Step 3: Implement store + helpers**

In `AffinityStore._connect`, after creating `affinity`, also:

```python
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_tick (
                    stream_id TEXT PRIMARY KEY,
                    last_fired_at REAL NOT NULL
                )
                """
            )
```

Add sync/async get/upsert analogous to affinity (`asyncio.to_thread`, same `self.lock` if affinity methods use the lock — `get` uses `self.lock`? It uses `to_thread` without wrapping the lock on get... `AffinityStore` has `self.lock` but `_get_sync` does not acquire it; `_upsert_sync` neither. Follow existing: no extra lock, `check_same_thread=False`.

```python
    def _get_proactive_last_fired_sync(self, stream_id: str) -> Optional[float]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT last_fired_at FROM proactive_tick WHERE stream_id = ?",
            (stream_id,),
        )
        row = cur.fetchone()
        return float(row[0]) if row is not None else None

    def _upsert_proactive_last_fired_sync(self, stream_id: str, last_fired_at: float) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO proactive_tick (stream_id, last_fired_at)
            VALUES (?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET last_fired_at=excluded.last_fired_at
            """,
            (stream_id, float(last_fired_at)),
        )
        conn.commit()

    async def get_proactive_last_fired(self, stream_id: str) -> Optional[float]:
        return await asyncio.to_thread(self._get_proactive_last_fired_sync, stream_id)

    async def upsert_proactive_last_fired(self, stream_id: str, last_fired_at: float) -> None:
        await asyncio.to_thread(self._upsert_proactive_last_fired_sync, stream_id, last_fired_at)
```

Helpers (near other dataclasses):

```python
def is_proactive_time_due(last_fired_at: Optional[float], now: float, interval_s: float) -> bool:
    if last_fired_at is None:
        return False
    return (now - float(last_fired_at)) >= float(interval_s)


def _message_timestamp(msg: Mapping[str, Any]) -> float:
    try:
        return float(msg.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _message_user(msg: Mapping[str, Any]) -> tuple[str, str, str]:
    info = msg.get("message_info") if isinstance(msg.get("message_info"), Mapping) else {}
    user = info.get("user_info") if isinstance(info, Mapping) else {}
    if not isinstance(user, Mapping):
        user = {}
    platform = str(msg.get("platform") or "").strip()
    user_id = str(user.get("user_id") or msg.get("user_id") or "").strip()
    nickname = str(user.get("user_nickname") or user.get("user_cardname") or user_id).strip()
    return platform, user_id, nickname


def extract_speakers_newest_first(
    messages: Sequence[Any],
    *,
    bot_user_id: str,
    max_people: int,
) -> list[SpeakerStub]:
    rows: list[tuple[float, SpeakerStub]] = []
    bot = str(bot_user_id or "").strip()
    for item in messages:
        if not isinstance(item, Mapping):
            continue
        platform, user_id, nickname = _message_user(item)
        if not user_id or user_id == bot:
            continue
        rows.append((_message_timestamp(item), SpeakerStub(platform=platform, user_id=user_id, nickname=nickname or user_id)))
    rows.sort(key=lambda r: r[0], reverse=True)
    seen: set[tuple[str, str]] = set()
    out: list[SpeakerStub] = []
    for _, speaker in rows:
        key = (speaker.platform, speaker.user_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(speaker)
        if len(out) >= max(1, int(max_people)):
            break
    return out


def format_proactive_briefing(people: Sequence[BriefingPerson], *, total_label: str) -> str:
    lines = ["【印象卡片 · 定期提醒 · 内部简报】", "以下最近发言者仅供你判断要不要微调印象；不要对用户朗读这份名单。", ""]
    if not people:
        lines.append("（最近窗口内没有可列出的发言者）")
        return "\n".join(lines)
    for person in people:
        if person.has_card:
            total = _fmt_num(person.total) if person.total is not None else "—"
            when = ""
            if person.updated_at:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(person.updated_at))
                when = f"，上次更新 {when}"
            lines.append(f"- {person.display_name}：{total_label} {total}{when}")
        else:
            lines.append(f"- {person.display_name}：尚无档案")
    return "\n".join(lines)
```

Define `SpeakerStub` and `BriefingPerson` as frozen dataclasses next to `PersonRef`.

- [ ] **Step 4: Run smoke tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin.py tests/smoke_test.py
git commit -m "$(cat <<'EOF'
Add proactive tick storage and briefing helpers.

EOF
)"
```

---

### Task 5: Poll loop, Maisaka trigger, manifest capabilities, changelog

**Files:**
- Modify: `plugin.py` (`on_load` / `on_unload` / `on_config_update`, loop methods, intent constant)
- Modify: `_manifest.json` `capabilities`
- Modify: `tests/smoke_test.py` (`test_manifest_capabilities_cover_usage`)
- Modify: `README.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1 proactive caches; Task 4 store/helpers; Task 3 `nudge_impression` (mentioned in intent only)
- Produces:
  - `DEFAULT_PROACTIVE_INTENT_TEMPLATE: str`
  - `self._poll_task: Optional[asyncio.Task]`
  - `self._poll_stop: Optional[asyncio.Event]`
  - `_restart_proactive_loop` / `_stop_proactive_loop` / `_proactive_loop` / `_proactive_poll_once`
  - Manifest: `maisaka.proactive.trigger`, `chat.get_all_streams`, `message.count_new`, `message.get_by_time_in_chat`, `message.get_recent`

- [ ] **Step 1: Write the failing tests**

Replace the maisaka special-case in `test_manifest_capabilities_cover_usage` with:

```python
    used = set()
    for proxy, method in re.findall(r"self\.ctx\.([a-z_]+)\.([a-z_0-9]+)", source):
        if proxy in proxy_to_capability:
            used.add(f"{proxy_to_capability[proxy]}.{method}")
    for a, b in re.findall(r"self\.ctx\.maisaka\.([a-z_]+)\.([a-z_0-9]+)", source):
        used.add(f"maisaka.{a}.{b}")
```

Add:

```python
def test_proactive_intent_mentions_silence_and_nudge() -> None:
    text = affinity.DEFAULT_PROACTIVE_INTENT_TEMPLATE
    assert "nudge_impression" in text
    assert "不要" in text or "不必" in text
    print("ok: proactive intent")
```

After Step 1, `test_manifest_capabilities_cover_usage` will still pass until the loop calls new ctx methods; then it will FAIL until `_manifest.json` is updated. Write the capability list assertion too:

```python
def test_manifest_declares_proactive_capabilities() -> None:
    import json
    declared = set(json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))["capabilities"])
    needed = {
        "maisaka.proactive.trigger",
        "maisaka.context.append",
        "chat.get_all_streams",
        "message.count_new",
        "message.get_by_time_in_chat",
        "message.get_recent",
    }
    assert needed <= declared, needed - declared
    print("ok: manifest proactive capabilities")
```

- [ ] **Step 2: Run to verify the new manifest test fails**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "import tests.smoke_test as t; t.test_manifest_declares_proactive_capabilities()"
```

Expected: FAIL missing `maisaka.proactive.trigger` (and others).

- [ ] **Step 3: Implement the loop**

Constants:

```python
DEFAULT_PROACTIVE_INTENT_TEMPLATE = """【内部提醒·印象卡片】这不是用户消息，用户看不到这份意图。

请根据内部上下文里的「印象卡片定期简报」，判断这个聊天里有没有人的印象需要小幅更新。
- 默认不要在聊天里说话。只有当你决定发送印象卡片时才可以发言。
- 小幅更新请调用 nudge_impression，不要用 refresh_impression，除非发生了明显的重大变化。
- 什么都不做完全可以。
"""
```

`__init__`: `self._poll_task: Optional[asyncio.Task] = None` and `self._poll_stop: Optional[asyncio.Event] = None`.

Loop control (mirror RSS reader, names as specified):

```python
    def _restart_proactive_loop(self) -> None:
        self._stop_proactive_loop()
        if not self.config.plugin.enabled or not self._proactive_enabled:
            return
        self._poll_stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._proactive_loop())

    def _stop_proactive_loop(self) -> None:
        if self._poll_stop is not None:
            self._poll_stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _proactive_loop(self) -> None:
        assert self._poll_stop is not None
        while not self._poll_stop.is_set():
            try:
                await self._proactive_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("印象卡片定期提醒扫描异常: %s", exc, exc_info=True)
            interval = max(30, self._proactive_poll_seconds)
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue
```

`_proactive_poll_once`:

1. If not enabled or `_store is None`: return
2. `streams = await self.ctx.chat.get_all_streams(platform="all_platforms")` — if this is a list, use it; if a mapping with `"streams"`, use that list. Normalize to a list of dicts with `stream_id`. On exception: log and return
3. `now = time.time()`; `interval_s = self._proactive_interval_hours * 3600`; `fired = 0`
4. For each stream:
   - `stream_id = str(item.get("stream_id") or item.get("session_id") or "").strip()`; skip if empty
   - `last = await self._store.get_proactive_last_fired(stream_id)`
   - If `last is None`: `await self._store.upsert_proactive_last_fired(stream_id, now)`; continue
   - If not `is_proactive_time_due(last, now, interval_s)`: continue
   - `count = await self.ctx.message.count_new(stream_id, str(now - interval_s))` — if count is a mapping, use `count.get("count", 0)`. If exception or `int(count) < 1`: continue
   - `await self._fire_proactive_stream(stream_id, now, interval_s)`
   - `fired += 1`; if `fired >= self._proactive_max_streams_per_tick`: break

`_fire_proactive_stream`:

1. Try `messages = await self.ctx.message.get_by_time_in_chat(stream_id, str(now - interval_s), str(now))`. If that fails, `messages = await self.ctx.message.get_recent(stream_id, limit=max(20, self._proactive_max_briefing_people * 3))`
2. Normalize messages to a list
3. `bot_uid = str(await self.ctx.config.get("bot.qq_account", "") or "")` — if empty, also try skipping by nickname later; if still empty, pass `bot_user_id=""` (extract helper only skips exact id match)
4. `speakers = extract_speakers_newest_first(messages, bot_user_id=bot_uid, max_people=self._proactive_max_briefing_people)`
5. Build `BriefingPerson` list: for each speaker, `person_id = await self.ctx.person.get_id(speaker.platform or "qq", speaker.user_id)`; `record = await self._store.get(person_id)` if person_id else None; if record: `BriefingPerson(display_name=record.display_name or speaker.nickname, has_card=True, total=record.total, updated_at=record.updated_at)` else `BriefingPerson(display_name=speaker.nickname, has_card=False)`
6. `body = format_proactive_briefing(...)` ; if `briefing_template` is non-empty, `_render` it with `{briefing}` `{count}` — default path uses `format_proactive_briefing` only (YAGNI: ignore custom template placeholders beyond `{briefing}` if you implement `_etmpl` + `_render(template, briefing=body)`)
7. `await self.ctx.maisaka.context.append(stream_id=stream_id, segments=[{"type": "text", "content": body}], visible_text="印象卡片定期提醒", source_kind="plugin:com.0-hz.impression-card")`
8. `intent = _etmpl(self.config.proactive.intent_template, DEFAULT_PROACTIVE_INTENT_TEMPLATE)`
9. `await self.ctx.maisaka.proactive.trigger(stream_id=stream_id, intent=intent, reason="impression_card_periodic", metadata={"plugin": "impression-card"})`
10. Always `await self._store.upsert_proactive_last_fired(stream_id, now)` in a `finally` so failures do not retry every poll
11. Log exceptions per stream; do not raise

`on_load`: after creating `_store`, call `self._restart_proactive_loop()`.

`on_unload`: `_stop_proactive_loop()` then existing pending cancel / store close.

`on_config_update`: after `_refresh_config` and store path handling, `_restart_proactive_loop()`.

- [ ] **Step 4: Manifest + docs**

Add to `_manifest.json` `capabilities` (keep existing ones):

```json
    "maisaka.proactive.trigger",
    "chat.get_all_streams",
    "message.count_new",
    "message.get_recent",
    "message.get_by_time_in_chat"
```

`message.build_readable` is already implied via existing `message.build_readable` declaration — keep `message.build_readable` as today.

`CHANGELOG.md` new section:

```markdown
## [0.3.0] - 2026-08-13

### 新增

- `/卡片` 默认在渲染前用较小上下文做 LLM 增量微调（可在 `[light_refresh] enabled` 关闭）
- 工具 `nudge_impression`：同样的增量微调，供规划器在定期提醒时使用
- `[proactive]`：默认每 6 小时对有过消息的聊天流提醒规划器是否微调印象（默认少说话）
```

README: document `[proactive]` (interval, poll 300s, active-chat rule, silence).

- [ ] **Step 5: Run smoke tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: PASS, including capability scan (loop now references `ctx.chat.get_all_streams`, `ctx.message.count_new`, `ctx.message.get_by_time_in_chat`, `ctx.message.get_recent`, `ctx.maisaka.proactive.trigger`).

If `person.get_id` is already declared, good. The loop uses it; it is already in the manifest.

- [ ] **Step 6: Commit**

```bash
git add plugin.py tests/smoke_test.py _manifest.json README.md CHANGELOG.md
git commit -m "$(cat <<'EOF'
Wake Maisaka on a 6-hour cadence to consider impression nudges.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| `/卡片` light nudge, self or others, no cooldown | 3 |
| Toggle restores render-only | 1 + 3 (`should_light_nudge_on_card`) |
| Small context, no knowledge.search | 3 (`_light_recent_chat_text`) |
| Deltas + optional description | 2 + 3 |
| `max_abs_delta` 0 = no clamp | 2 |
| Timeout via `llm_rpc_timeout_ms` | 3 |
| Long persistent note guard | 2 + 3 |
| No 【系统通知】 | 3 (do not call `_maybe_notify`) |
| Cold-start only, no extra nudge | 3 |
| `nudge_impression` tool, no cold-start | 3 |
| `refresh_impression` description sentence | 3 |
| `send_impression_card` unchanged | (no task edits that method) |
| Loop 300s / 6h / active chats | 5 |
| First-seen stamp, no fire | 4 + 5 |
| Briefing + silence intent | 4 + 5 |
| `max_streams_per_tick` | 5 |
| Skip bot in roster | 4 |
| Stamp `last_fired_at` even on trigger failure | 5 `finally` |
| Manifest capabilities + smoke scan | 5 |
| README / CHANGELOG / help | 3 + 5 |
| config 0.3.0 / plugin 0.3.0 | 1 |
| No Host/SDK / no plugin.py split | all |

No TBD placeholders. Helper names used in later tasks match Task 2/4 produces blocks (`NudgeOutcome`, `should_light_nudge_on_card`, `parse_nudge_payload`, `apply_nudge_to_record`, `is_proactive_time_due`, `extract_speakers_newest_first`, `format_proactive_briefing`, `SpeakerStub`, `BriefingPerson`).
