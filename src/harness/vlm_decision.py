# -*- coding: utf-8 -*-
"""VLM 决策源 — DashScope qwen-vl → 严格 JSON ActionSpec → Harness ActionSpec。

不接真机/ADB；仅做静态决策。每次调用记录：
  screenshot_id、subgoal、candidate_map 版本、模型版本、temperature、prompt、
  原始响应、时间戳、解析结果、Guard 相关字段。

API 不可用 / 返回不符合 schema → 抛异常（VlmUnavailableError / VlmInvalidOutput），
不自动伪造 ActionSpec。
"""
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .schemas import ActionSpec, UiState
from .types import BBox


# ─────────────── 严格 JSON ActionSpec（VLM 输出） ───────────────

class VlmPixelBBox(BaseModel):
    x1: int = Field(ge=0)
    y1: int = Field(ge=0)
    x2: int = Field(gt=0)
    y2: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate(self):
        if self.x1 >= self.x2 or self.y1 >= self.y2:
            raise ValueError("invalid bbox")
        return self


class VlmActionSpec(BaseModel):
    """VLM 必须输出的严格 JSON 动作规格。"""
    action_type: Literal[
        "tap_candidate", "tap_visual", "swipe", "type_text", "remote_key", "media_key",
        "wait", "back", "reveal_controls", "done", "ask_user",
    ]
    candidate_id: Optional[str] = None
    target_role: Optional[str] = None
    bbox_px: Optional[VlmPixelBBox] = None
    direction: Optional[Literal["up", "down", "left", "right"]] = None
    text: Optional[str] = None
    key: Optional[str] = None
    wait_ms: Optional[int] = Field(default=None, ge=100, le=3000)
    sensitive_hint: Optional[str] = None

    @model_validator(mode="after")
    def _validate_action(self):
        if self.action_type == "tap_candidate" and not self.candidate_id:
            raise ValueError("tap_candidate requires candidate_id")
        if self.action_type == "tap_visual" and not self.bbox_px:
            raise ValueError("tap_visual requires bbox_px")
        if self.action_type == "type_text" and not self.text:
            raise ValueError("type_text requires text")
        if self.action_type == "swipe" and not self.direction:
            raise ValueError("swipe requires direction")
        if self.action_type in ("remote_key", "media_key") and not self.key:
            raise ValueError(f"{self.action_type} requires key")
        return self

    def to_action_spec(self, state: UiState) -> ActionSpec:
        """映射为 Harness ActionSpec，填充 fingerprint / screen_version。"""
        cm_version = state.candidate_map.screen_version if state.candidate_map else None
        bbox = None
        if self.bbox_px is not None:
            bbox = BBox(self.bbox_px.x1, self.bbox_px.y1, self.bbox_px.x2, self.bbox_px.y2)
        return ActionSpec(
            action_type=self.action_type,
            candidate_id=self.candidate_id,
            candidate_map_fingerprint=cm_version if self.action_type == "tap_candidate" else None,
            expected_screen_fingerprint=state.fingerprint,
            target_role=self.target_role,
            bbox_px=bbox,
            sensitive_hint=self.sensitive_hint,
            key=self.key,
            text=self.text,
            direction=self.direction,
            wait_ms=self.wait_ms,
        )


# ─────────────── 异常 ───────────────

class VlmUnavailableError(RuntimeError):
    """API 不可用（无 key / 网络失败）。"""


class VlmInvalidOutput(ValueError):
    """VLM 输出不符合 schema / 无法解析。"""


# ─────────────── Prompt ───────────────

VLM_SYSTEM_PROMPT = """你是 Android 中屏 GUI 操作决策器。根据当前截图和候选列表，为用户子目标选择唯一的下一步原子动作。

候选列表每行格式：[id | text | bbox(x1,y1,x2,y2) | source]

必须严格返回以下 JSON，不要任何其他文字、不要 markdown 代码块：
{
  "action_type": "tap_candidate | tap_visual | swipe | type_text | remote_key | media_key | wait | back | reveal_controls | done | ask_user",
  "candidate_id": "候选 id 或 null",
  "target_role": "目标语义描述 或 null",
  "bbox_px": {"x1": 整数, "y1": 整数, "x2": 整数, "y2": 整数} 或 null,
  "direction": "up | down | left | right 或 null",
  "text": "输入文本 或 null",
  "key": "按键名 或 null",
  "wait_ms": 整数 或 null,
  "sensitive_hint": "敏感操作提示 或 null"
}

约束：
- tap_candidate 必须给 candidate_id（候选列表中的真实 id）。
- tap_visual 必须给 bbox_px（像素坐标）。
- 付款、删除、退出登录、发送、订阅、授权、密码、验证码等敏感操作 → action_type="ask_user"。
- 无法可靠定位或截图信息不足 → action_type="ask_user" 或 "wait"。
- 只能判断截图可见控件，不猜位置。
- 禁止生成 shell、脚本或任何非 JSON 内容。"""


def build_vlm_prompt(subgoal: str, candidate_map) -> tuple[str, str]:
    """构造 (system_prompt, user_text)。user_text 含候选列表。"""
    lines = []
    if candidate_map is not None:
        for c in candidate_map.candidates:
            label = c.text or (c.kind or "")
            b = c.bbox_px
            lines.append(f"[{c.candidate_id} | {label} | "
                         f"({b.x1},{b.y1},{b.x2},{b.y2}) | {c.source}]")
    candidate_text = "\n".join(lines) if lines else "(无候选)"
    user_text = f"用户子目标：{subgoal}\n候选列表：\n{candidate_text}"
    return VLM_SYSTEM_PROMPT, user_text


# ─────────────── 决策记录 ───────────────

@dataclass
class VlmDecisionRecord:
    """单次 VLM 决策的完整记录。"""
    screenshot_id: str
    subgoal: str
    candidate_map_version: Optional[str]
    model: str
    temperature: float
    prompt: str
    raw_response: str = ""
    timestamp: str = ""
    parse_ok: bool = False
    error: Optional[str] = None
    action_type: Optional[str] = None
    candidate_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "screenshot_id": self.screenshot_id,
            "subgoal": self.subgoal,
            "candidate_map_version": self.candidate_map_version,
            "model": self.model,
            "temperature": self.temperature,
            "prompt": self.prompt,
            "raw_response": self.raw_response,
            "timestamp": self.timestamp,
            "parse_ok": self.parse_ok,
            "error": self.error,
            "action_type": self.action_type,
            "candidate_id": self.candidate_id,
        }


# ─────────────── VLM 决策源 ───────────────

class QwenVlmDecisionSource:
    """DashScope qwen-vl 决策源，实现 DecisionSource.next_action(state) -> ActionSpec。

    复用 harness-guivlm-main 的 qwen-vl 调用模式（OpenAI 兼容端点 + base64 截图 +
    严格 JSON 解析），但仅在 harness-framework 内实现，不依赖外部项目路径。
    """

    def __init__(self, screenshot_path: str, subgoal: str, candidate_map,
                 *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: str = "qwen-vl-plus", temperature: float = 0.0,
                 max_tokens: int = 512, timeout: int = 30):
        self.screenshot_path = screenshot_path
        self.subgoal = subgoal
        self.candidate_map = candidate_map
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

        self.api_key = api_key or _env_api_key()
        if not self.api_key:
            raise VlmUnavailableError(
                "DASHSCOPE_API_KEY / VLM_API_KEY not set；不伪造 ActionSpec"
            )
        self.base_url = base_url or os_environ(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.records: list = []

    def _build_messages(self):
        system_prompt, user_text = build_vlm_prompt(self.subgoal, self.candidate_map)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{_b64(self.screenshot_path)}"}},
            ]},
        ]

    def next_action(self, state: UiState) -> ActionSpec:
        """调用 VLM 返回单步 ActionSpec。失败抛异常（不伪造）。"""
        from openai import OpenAI  # 惰性 import

        cm_version = state.candidate_map.screen_version if state.candidate_map else None
        screenshot_id = _screenshot_id(self.screenshot_path)
        messages = self._build_messages()
        prompt_text = messages[0]["content"] + "\n" + messages[1]["content"][0]["text"]

        rec = VlmDecisionRecord(
            screenshot_id=screenshot_id, subgoal=self.subgoal,
            candidate_map_version=cm_version, model=self.model,
            temperature=self.temperature, prompt=prompt_text,
        )
        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(
                model=self.model, messages=messages,
                temperature=self.temperature, max_tokens=self.max_tokens,
            )
            content = response.choices[0].message.content or ""
            rec.raw_response = content
            rec.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception as e:  # noqa: BLE001 — API 失败记为失败，不伪造
            rec.raw_response = ""
            rec.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            rec.parse_ok = False
            rec.error = f"API unavailable: {e}"
            self.records.append(rec)
            raise VlmUnavailableError(f"VLM API call failed: {e}") from e

        # 解析严格 JSON
        try:
            data = _extract_json(content)
            if data is None:
                raise VlmInvalidOutput("no JSON in response")
            vlm_action = VlmActionSpec(**data)
        except Exception as e:  # noqa: BLE001
            rec.parse_ok = False
            rec.error = f"schema/parse failed: {e}"
            self.records.append(rec)
            raise VlmInvalidOutput(f"VLM output not a valid ActionSpec: {e}") from e

        rec.parse_ok = True
        rec.action_type = vlm_action.action_type
        rec.candidate_id = vlm_action.candidate_id
        self.records.append(rec)
        return vlm_action.to_action_spec(state)


# ─────────────── 工具函数 ───────────────

def os_environ(key: str, default: str) -> str:
    import os
    return os.environ.get(key, default)


def _env_api_key() -> Optional[str]:
    import os
    _load_dotenv()
    # VLM 专用 key 优先：用户 .env 里名为 `vlm-api-key`；兼容 VLM_API_KEY / DASHSCOPE_API_KEY。
    return (os.environ.get("vlm-api-key")
            or os.environ.get("VLM_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY"))


def _load_dotenv():
    """从项目根目录加载 .env（若存在）。已设置的环境变量优先（override=False）。"""
    import os
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 未安装时仅用环境变量
        return
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _screenshot_id(path: str) -> str:
    import os
    return os.path.splitext(os.path.basename(path))[0]


def _extract_json(text: str) -> Optional[dict]:
    """从文本提取 JSON（支持 ```json 包裹 / 纯 JSON）。"""
    import re
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    else:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0).strip()
    try:
        return json.loads(text)
    except Exception:
        return None
