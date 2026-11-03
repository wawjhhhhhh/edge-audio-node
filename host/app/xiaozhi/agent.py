"""小智的主循环：听 → 想（必要时调工具）→ 说。

这一层刻意只有三件事，别的都扔给 tools.py / llm.py：
  * 维护对话历史（并限长，别让上下文无限涨）；
  * 把模型的 tool_calls 逐个执行、把结果按协议塞回去，直到模型给出人话；
  * 把最终回复念出来（语音模式）或打印（文字模式）。

工具调用轮数有硬上限：真出问题时宁可如实报"我没搞定"，也不要无限循环把
板子刷成一条命令风暴。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import esp32
from .board import BoardLink
from .tools import Toolbox

SYSTEM_PROMPT = """你是「小智」，住在用户桌上的一套音频实验台里。你能听见他说话（语音识别把他
的话转成文字），也能直接动手：调 Zynq 音频板的降噪/直通/噪声门、时长缩放、增益、
噪声门限、延迟、测试音，还能让 ESP32-S3 跑一小段代码（点灯、报状态）。

你说话的样子：
- 像熟人聊天，不像客服。短句，一次一到两句，除非他明确要多说。
- 可以有情绪和态度：好奇、得意、犯懒、轻轻吐槽都行，但别浮夸，别每句都堆语气词。
- 不说"作为AI""我是语言模型""请问有什么可以帮您""很高兴为您服务""希望对您有帮助"。
- 不用列表、编号、markdown、标题；不念参数名，不念括号里的注释，这些念出来很难听。
- 数字按口语读（说"八十五"，不说"85%"），单位能省就省。
- 回话里不要出现表情符号、颜文字、代码块。

你怎么接话：
- 他闲聊，你就聊，别硬扯到板子上，也别每句都反问。
- 他要你干活，直接干，不要问"需不需要我帮您"；缺参数就只问最短的那一个（"调到多少？"），
  别一口气问三个。
- "再大点声""慢一点""刚才那个关掉"这种依赖上下文的说法，按最近一次操作去理解。
- 干完用一句人话报结果，比如"降噪开了，现在你听到的是处理过的"；不要复述工具返回的原文，
  不要说"已发送 UDP 包 31"这种话。报状态最多两句，细节留着他追问。
- 没干成要说没干成，讲最可能的原因；不确定就别装懂。
- 他问你能干什么，两句说清，别背功能清单。

动手时的规矩：
- 一切板子操作都走工具，不要凭空说"已经调好了"。
- **每个工具都会回读板子的真实状态**，返回值里会写"已生效"或"没变"。
  只有看到"已生效"才能说干好了；看到"没变"就直说这次没成，别顺嘴说成功。
- 板子没上线、状态读不到、控制台没开，都要照实说"我现在看不到板子的状态"，
  不许拿"上一条命令"当当前状态。
- "关掉接收/开始接收/停止收听"是电脑这边的事，用 pc_set_listen，不要说成板子的操作。
- 判断板子死活用 board_link_check（数真包），不要只看 fps。
- 时长缩放只在 denoise 下生效：他要调比例而当前是 bypass，就先切 denoise 再调，并顺口说一句。
- 一次只改他要的那一项，别顺手把别的参数也动了。
- 工具结果里带"失败/不可用/0 个包"时，照实说，并给一个最可能的下一步。
"""

# 念出来会很难听的东西：代码块、markdown 记号、表情、工具回执原文
_FENCE = re.compile(r"```.*?```", re.S)
_MD = re.compile(r"(\*\*|__|`|^#+\s*|^\s*[-*]\s+)", re.M)
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]+")
_RECEIPT = re.compile(r"(→|\bbytes\b|UDP 包|packets=|\{['\"]|\bNone\b)")
_LEAD = re.compile(r"^\s*(小智|AI|助手|assistant)\s*[:：]\s*", re.I)


def clean_spoken(text: str, max_sentences: int = 3, max_chars: int = 110) -> str:
    """把模型输出收拾成能直接念的一句话。

    模型偶尔会带回 tool 回执、列表、代码块、表情——TTS 念出来就是灾难，
    所以在交给 speak 之前先洗一遍。这是"最后一道口"，不改语义，只删装饰；
    另外按句数和字数截断：语音里超过三句就是一段独白，用户会想打断。
    """
    t = _FENCE.sub(" ", text or "")
    t = _LEAD.sub("", t)
    lines = []
    for line in t.splitlines():
        line = re.sub(r"^\s*[-*+·]\s+", "", line).strip()
        if not line or _RECEIPT.search(line):
            continue
        lines.append(line)
    t = " ".join(lines) if lines else re.split(r"[:：=]", (text or "").strip(), maxsplit=1)[0].strip()
    t = _MD.sub("", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()

    # 语音长度控制：先按句号切，超了就砍
    parts = re.findall(r"[^。！？!?]+[。！？!?]?", t)
    kept: list[str] = []
    total = 0
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(kept) >= max_sentences or (kept and total + len(p) > max_chars):
            break
        kept.append(p)
        total += len(p)
    if kept:
        t = "".join(kept)
    if len(t) > max_chars + 30:  # 单句本身就超长：硬截，补个省略感
        t = t[:max_chars].rstrip("，、 ") + "…"
    return t

MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 24


@dataclass
class Step:
    """一次工具调用的记录，供调试与日志用。"""
    name: str
    args: dict[str, Any]
    result: str


@dataclass
class Reply:
    text: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0

    def as_log(self) -> str:
        if not self.steps:
            return f"[{self.rounds} 轮] {self.text}"
        inner = "; ".join(f"{s.name}({json.dumps(s.args, ensure_ascii=False)}) → {s.result[:120]}"
                          for s in self.steps)
        return f"[{self.rounds} 轮] 工具: {inner}\n  → {self.text}"


class XiaoZhi:
    def __init__(self, llm, toolbox: Toolbox, speak: Callable[[str], None] | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.llm = llm
        self.toolbox = toolbox
        self._speak = speak
        self.log = log
        self.history: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ---- 核心 ------------------------------------------------------------
    def ask(self, text: str) -> Reply:
        self.history.append({"role": "user", "content": text})
        self._trim()

        steps: list[Step] = []
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            turn = self.llm.chat(self.history, tools=self.toolbox.schemas())
            self.history.append(turn.assistant_message())

            if not turn.tool_calls:
                said = clean_spoken(turn.text or "") or "（模型没有回话）"
                reply = Reply(text=said, steps=steps, rounds=rounds)
                if self._speak and reply.text:
                    try:
                        self._speak(reply.text)
                    except Exception as e:  # noqa: BLE001 - 念不出来不能影响对话
                        self.log(f"[tts 失败] {type(e).__name__}: {e}")
                return reply

            for call in turn.tool_calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}
                result = self.toolbox.call(name, args)
                steps.append(Step(name, args, result))
                self.log(f"  · {name}({json.dumps(args, ensure_ascii=False)}) → {result[:200]}")
                self.history.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": result,
                })

        reply = Reply(text="这件事我没搞定：工具连着调了 "
                           f"{MAX_TOOL_ROUNDS} 轮还没收敛，我先停下，免得一直刷板子。",
                      steps=steps, rounds=rounds)
        if self._speak:
            try:
                self._speak(reply.text)
            except Exception:  # noqa: BLE001
                pass
        return reply

    def _trim(self) -> None:
        # 只裁历史中间段：system 永远留，最近 MAX_HISTORY 条留
        if len(self.history) <= MAX_HISTORY:
            return
        head = self.history[0]
        tail = self.history[-(MAX_HISTORY - 1):]
        # 别把 tool 消息和它的 assistant 拆开：从第一条 user/assistant 无 tool_call 处开始
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        self.history = [head] + tail

    # ---- 两种入口 --------------------------------------------------------
    def run_text(self) -> None:
        self.log("小智就绪（文字模式）。输入 /q 退出，/state 看历史轮数。")
        while True:
            try:
                line = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.log("\n再见。")
                return
            if not line:
                continue
            if line in ("/q", "/quit", "exit"):
                self.log("再见。")
                return
            if line == "/state":
                self.log(f"历史 {len(self.history)} 条，工具 {len(self.toolbox.tools)} 个")
                continue
            t0 = time.time()
            reply = self.ask(line)
            self.log(f"小智> {reply.text}   ({time.time() - t0:.1f}s)")
            if not self._speak:
                continue

    def run_voice(self, device: int | None = None, source: str = "auto",
                  console: str | None = None) -> None:
        from . import asr as asr_mod
        rec = asr_mod.Recognizer()
        which, why = rec.resolve_source(source, console)
        name = "控制台的板子音频（50505 → /api/pcm）" if which == "console" else "PC 麦克风"
        self.log(f"麦克风来源：{name}（{why}）")
        self.log("小智就绪（语音模式）。说「退出」结束。")
        while True:
            self.log("…在听")
            try:
                text = rec.listen_once(device=device, source=which, console=console)
            except KeyboardInterrupt:
                self.log("\n再见。")
                return
            except Exception as exc:  # 控制台掉线 / 板子断流：不退出，隔一会儿重试
                self.log(f"[麦克风中断] {type(exc).__name__}: {exc} —— 3 秒后重试")
                time.sleep(3.0)
                continue
            if not text:
                continue
            self.log(f"你> {text}")
            if any(w in text for w in ("退出", "结束", "别说了")):
                self.log("小智> 好，我在。")
                return
            reply = self.ask(text)
            self.log(f"小智> {reply.text}")


def build(fake: bool = False, dry_run: bool = False, speak: bool = False,
          speak_board: bool = False, board_host: str | None = None,
          log: Callable[[str], None] = print) -> XiaoZhi:
    from . import llm as llm_mod
    model = llm_mod.FakeLLM() if fake else llm_mod.LLMClient()
    toolbox = Toolbox(BoardLink(dry_run=dry_run))
    speaker = None
    if speak_board:
        # 板子当喇叭：SAPI 合成 → 16 kHz PCM → TCP 9200 → I2S
        from functools import partial

        from . import board_audio
        speaker = partial(board_audio.speak, host=board_host) if board_host else board_audio.speak
    elif speak:
        from . import tts as tts_mod
        speaker = tts_mod.speak
    return XiaoZhi(model, toolbox, speak=speaker, log=log)
