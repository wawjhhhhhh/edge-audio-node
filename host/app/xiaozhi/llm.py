"""远端大模型客户端（DeepSeek / 任何 OpenAI 兼容端点）+ 一个可以离线跑的假模型。

为什么直接用 urllib 而不是 SDK：
  * 这台机器上 `httpx` 的依赖是断的（缺 anyio / httpcore），装 SDK 会连带踩坑；
  * 我们只需要 `/chat/completions` 一个端点，工具调用的报文结构自己拼得出来。

`FakeLLM` 不是玩具：它让"语音/文字 → 工具调用 → 板子"整条闭环在没有 API key、
没有网络的时候也能回归测试，换真模型时只换一个对象。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class Turn:
    """一轮模型输出：要么是话，要么是工具调用（也可能两者都有）。"""
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class LLMError(RuntimeError):
    pass


def _key_from_env() -> str | None:
    for name in ("DEEPSEEK_API_KEY", "XIAOZHI_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip()
    # 也允许放在仓库里的一个文本文件（已在 .gitignore 里）
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key.txt")
    if os.path.exists(here):
        v = open(here, encoding="utf-8").read().strip()
        if v:
            return v
    return None


class LLMClient:
    """OpenAI 兼容的 /chat/completions 客户端（非流式，够用且好调试）。"""

    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_BASE,
                 model: str = DEFAULT_MODEL, timeout: float = 60.0,
                 temperature: float = 0.9, max_tokens: int = 400,
                 frequency_penalty: float = 0.25) -> None:
        self.api_key = api_key or _key_from_env()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.frequency_penalty = frequency_penalty

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> Turn:
        if not self.api_key:
            raise LLMError(
                "没有 API key：设置 DEEPSEEK_API_KEY 环境变量，"
                "或把 key 写进 work/xiaozhi/key.txt（已 gitignore）")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "frequency_penalty": self.frequency_penalty,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"连不上 {self.base_url}：{e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"返回不是 JSON：{e}") from e

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"返回结构不对：{json.dumps(data, ensure_ascii=False)[:400]}") from e
        return Turn(text=msg.get("content") or "",
                    tool_calls=msg.get("tool_calls") or [],
                    raw=data)


class FakeLLM:
    """确定性的假模型：按关键词产出工具调用，用来离线回归整条闭环。"""

    MODE_RULES = [
        (("降噪", "rnnoise", "denoise"), "denoise"),
        (("直通", "bypass", "原声"), "bypass"),
        (("噪声门", "gate", "门限"), "gate"),
    ]
    GAIN_RULES = [
        (("-12", "最小", "很轻"), "-12dB"),
        (("-6", "小声"), "-6dB"),
        (("-2.5", "-2", "稍小"), "-2.5dB"),
        (("0db", "0 db", "原始音量", "正常音量"), "0dB"),
    ]

    def __init__(self) -> None:
        self.api_key = "fake"
        self.model = "fake-rules"

    @property
    def ready(self) -> bool:
        return True

    # ---- 工具调用的报文格式与真模型一致，上层代码不用区分 ----------------
    @staticmethod
    def _call(name: str, args: dict[str, Any], idx: int = 0) -> dict[str, Any]:
        return {
            "id": f"call_{idx}_{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }

    def _plan(self, text: str) -> Turn | None:
        t = text.lower()
        pct = re.search(r"(\d{2,3})\s*%", text)
        if pct is None:
            pct = re.search(r"(?:到|成|为)\s*(\d{2,3})", text)
        if pct and any(k in t for k in ("慢", "快", "时长", "比例", "缩放", "speed", "scale")):
            return Turn(tool_calls=[self._call("board_set_time_scale",
                                              {"percent": int(pct.group(1))})])

        for keys, mode in self.MODE_RULES:
            if any(k in t for k in keys):
                return Turn(tool_calls=[self._call("board_set_mode", {"mode": mode})])

        if any(k in t for k in ("在线", "在不在", "连上", "通不通", "link")):
            return Turn(tool_calls=[self._call("board_link_check", {"seconds": 2.0})])

        if any(k in t for k in ("状态", "怎么样", "情况", "status")):
            return Turn(tool_calls=[self._call("board_status", {})])

        if any(k in t for k in ("增益", "音量", "gain")):
            for keys, step in self.GAIN_RULES:
                if any(k in t for k in keys):
                    return Turn(tool_calls=[self._call("board_set_gain", {"step": step})])
            return Turn(tool_calls=[self._call("board_set_gain", {"step": "-6dB"})])

        if any(k in t for k in ("测试音", "tone", "嘀")):
            on = not any(k in t for k in ("关", "停", "off"))
            return Turn(tool_calls=[self._call("board_tone", {"on": on})])

        if "延迟" in t or "latency" in t:
            frames = 20 if any(k in t for k in ("20", "跟手", "小")) else 30
            return Turn(tool_calls=[self._call("board_set_latency", {"frames": frames})])

        if any(k in t for k in ("灯", "led", "点个亮")):
            return Turn(tool_calls=[self._call(
                "esp32_run",
                {"code": "import machine, neopixel, time\n"
                         "np = neopixel.NeoPixel(machine.Pin(48), 1)\n"
                         "np[0] = (0, 32, 0)\n"
                         "np.write()\n"
                         "print('led green')"})])

        if any(k in t for k in ("esp32", "s3", "模块信息")):
            return Turn(tool_calls=[self._call("esp32_info", {})])
        return None

    SPOKEN = {
        "board_set_mode": "模式切好了。",
        "board_set_time_scale": "速度调好了。",
        "board_set_gain": "音量动了。",
        "board_set_noise": "噪声门限改了。",
        "board_set_latency": "延迟档位换好了。",
        "board_tone": "测试音处理好了。",
        "board_link_check": "我数了一下包，情况如上。",
        "board_status": "状态我看了。",
        "esp32_run": "板子上那段跑完了。",
        "esp32_info": "这块芯片的底细我查了。",
    }

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> Turn:
        last = messages[-1]
        if last.get("role") == "tool":
            # 工具都跑完了：说人话（念之前 agent.clean_spoken 还会再洗一遍）
            names = [m.get("name", "") for m in messages if m.get("role") == "tool"]
            tails = [self.SPOKEN.get(n, "弄好了。") for n in names]
            return Turn(text="".join(dict.fromkeys(tails)))

        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = m.get("content") or ""
                break
        plan = self._plan(user_text)
        if plan is not None:
            return plan
        return Turn(text=f"（假模型回话）我听到了：{user_text[:40]}。命令我干活就直接说，"
                         "比如「切到降噪」「时长调到 80%」「板子在线吗」。")
