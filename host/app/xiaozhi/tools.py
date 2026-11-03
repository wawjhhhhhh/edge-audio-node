"""小智能调用的工具集（MCP 风格的注册表）。

设计原则，三条，都是被现实逼出来的：

1. **每个工具要么真做成事，要么说清楚为什么没做成。**
   板子断电时 `board_link_check` 会照实返回 packets=0，而不是"成功"。
2. **参数校验在本地做，不指望远端。**
   固件对越界值是"拒绝并打印原因"（不钳位），所以百分比先在这里挡住，
   免得 LLM 以为调成了、听感却一点没变。
3. **时基这类跨工具的隐形约束要写进 description。**
   `board_set_time_scale` 只在 denoise 模式下生效——LLM 不会自己知道，
   所以工具描述里明写，并且 mode 不是 denoise 时返回值里再提醒一次。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import esp32
from .board import BoardLink, GAIN_BYTES, NOISE_BYTES, TS_MAX, TS_MIN

Handler = Callable[..., str]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Toolbox:
    def __init__(self, link: BoardLink) -> None:
        self.link = link
        self.tools: dict[str, Tool] = {}
        self._register_all()

    # ---- 注册 ------------------------------------------------------------
    def _register_all(self) -> None:
        self.add(Tool(
            name="board_set_mode",
            description=(
                "切换 Zynq 音频板的处理模式。bypass=直通（不降噪、不做时长缩放），"
                "denoise=RNNoise 降噪（只有这个模式下时长缩放才真正生效），"
                "gate=噪声门（不用 RNNoise，算力最省），"
                "pl=FPGA 里的谱减降噪（跑在 PL，不占 ARM 算力）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["bypass", "denoise", "gate", "pl"],
                             "description": "处理模式"},
                },
                "required": ["mode"],
            },
            handler=self.board_set_mode,
        ))
        self.add(Tool(
            name="board_set_time_scale",
            description=(
                f"设置音频时长缩放比例（{TS_MIN}–{TS_MAX} 百分比）。100=原速，"
                "50=把 10 ms 的内容拉长到 20 ms（听感变慢、音高不变）。"
                "只在 denoise 模式下有效；板子算力上限约 49.8 帧/秒，"
                "所以 100% 时上限约 49 fps、50% 时约 99 fps。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "percent": {"type": "integer", "minimum": TS_MIN, "maximum": TS_MAX,
                                "description": "时长百分比，50..100"},
                },
                "required": ["percent"],
            },
            handler=self.board_set_time_scale,
        ))
        self.add(Tool(
            name="board_set_gain",
            description="设置输出增益档位（Q15）：0dB / -2.5dB / -6dB / -12dB。",
            parameters={
                "type": "object",
                "properties": {
                    "step": {"type": "string", "enum": ["0dB", "-2.5dB", "-6dB", "-12dB"]},
                },
                "required": ["step"],
            },
            handler=self.board_set_gain,
        ))
        self.add(Tool(
            name="board_set_noise",
            description="设置板子内部叠加的本底噪声幅度（用于演示降噪效果）：0 / 300 / 800 / 1500。",
            parameters={
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "enum": [0, 300, 800, 1500]},
                },
                "required": ["level"],
            },
            handler=self.board_set_noise,
        ))
        self.add(Tool(
            name="board_set_latency",
            description="设置播放延迟阶梯（帧数，一帧 10 ms）：20 帧=200 ms 更跟手，30 帧=300 ms 更抗抖动。",
            parameters={
                "type": "object",
                "properties": {"frames": {"type": "integer", "enum": [20, 30]}},
                "required": ["frames"],
            },
            handler=self.board_set_latency,
        ))
        self.add(Tool(
            name="board_tone",
            description="开/关板子的内置测试音（1 kHz 左右），用来确认播放通路。",
            parameters={
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
            handler=self.board_tone,
        ))
        self.add(Tool(
            name="board_link_check",
            description=(
                "实测板子是否在发音频：在 50505 端口真收几秒钟包并统计。"
                "packets=0 就是板子没上线（断电/网线/IP 不对），"
                "这是唯一不会骗人的链路判据。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "minimum": 0.5, "maximum": 10.0,
                                "description": "观测时长，默认 2 秒"},
                },
                "required": [],
            },
            handler=self.board_link_check,
        ))
        self.add(Tool(
            name="board_status",
            description=(
                "读板子自己报的真实状态（模式/时长比例/增益/延迟/测试音），"
                "以及 PC 控制台的收听状态。板子没上线时如实说没上线。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self.board_status,
        ))
        self.add(Tool(
            name="pc_set_listen",
            description=(
                "开关 PC 控制台的接收（相当于界面上点「开始/停止收听」）。"
                "注意这是电脑这边的事，不是板子的事：板子一直在发，电脑选择收不收。"
            ),
            parameters={
                "type": "object",
                "properties": {"on": {"type": "boolean", "description": "true=开始接收，false=停止接收"}},
                "required": ["on"],
            },
            handler=self.pc_set_listen,
        ))
        self.add(Tool(
            name="esp32_run",
            description=(
                "在 ESP32-S3 的 MicroPython REPL 里执行一段代码并返回打印结果。"
                "用于点灯、读 GPIO、跑 WiFi 扫描等。板子没插或串口不通时会明确报错。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "MicroPython 代码，可多行"},
                    "port": {"type": "string", "description": "串口名，默认自动找能进 MicroPython REPL 的那个（这块板子是 COM7）"},
                },
                "required": ["code"],
            },
            handler=self.esp32_run,
        ))
        self.add(Tool(
            name="esp32_info",
            description="读 ESP32-S3 的基本信息：固件实现、flash 大小、主频、剩余内存、MAC。",
            parameters={
                "type": "object",
                "properties": {"port": {"type": "string", "description": "串口名，默认自动找能进 MicroPython REPL 的那个（这块板子是 COM7）"}},
                "required": [],
            },
            handler=self.esp32_info,
        ))

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    # ---- 分发 ------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"没有这个工具：{name}（可用：{', '.join(sorted(self.tools))}）"
        try:
            return tool.handler(**(args or {}))
        except TypeError as e:
            return f"参数不对：{e}"
        except ValueError as e:
            return f"参数被拒绝：{e}"
        except Exception as e:  # 工具失败不能把整个会话炸掉
            return f"{name} 执行失败：{type(e).__name__}: {e}"

    # ---- 具体实现 --------------------------------------------------------
    # 回读确认是这一层的核心：发出去 ≠ 生效。板子状态包（50508）250 ms 一包，
    # 发完等半拍再读，就能把"说干了"和"真干了"分开。
    def _snapshot(self):
        """读一次板子真实状态；返回 (人话摘要, 状态字典或 None)。"""
        st = self.link.status()
        if st is None:
            return "读不到板子状态（50508 没回包，控制台也没起来）", None
        if not st.get("online", True):
            return f"板子状态包已停 {st.get('age')} 秒", None
        return (
            "板子回报 mode=%s ts=%s gain=%s dly=%s tone=%s"
            % (st.get("mode"), st.get("ts_rate"), st.get("gain"),
               st.get("dly_frames"), st.get("tone")),
            st,
        )

    def _confirm(self, sent: str, field: str, want, label: str) -> str:
        time.sleep(0.4)                       # 状态包 250 ms 一包，等一拍
        summary, st = self._snapshot()
        if st is None:
            return f"{sent}；{summary} —— 所以不能确认它生效"
        if st.get(field) == want:
            return f"{sent}；{summary} —— {label}已生效"
        return (f"{sent}；但{summary}，{label}没变 —— 这次没生效"
                "（UDP 不重传，可能板子没收到）")

    def board_set_mode(self, mode: str) -> str:
        key = mode.strip().lower()
        r = self.link.set_mode(key)
        # 固件收到 '1' 时 g_dsp_mode 立即变，状态包下一拍就能看到
        return self._confirm(r.as_text(), "mode", key if key != "denoise" else "denoise", "模式")

    def board_set_time_scale(self, percent: int) -> str:
        r = self.link.set_time_scale(percent)
        time.sleep(0.4)
        summary, st = self._snapshot()
        if st is None:
            return f"{r.as_text()}；{summary}"
        extra = ""
        if st.get("mode") != "denoise":
            extra = "（注意：板子现在不在 denoise，比例只是记下来了，不会拉伸）"
        elif st.get("ts_rate") != int(percent):
            extra = f"（板子回报 ts={st.get('ts_rate')}，和要的 {percent} 不一致）"
        return f"{r.as_text()}；{summary}{extra}"

    def board_set_gain(self, step: str) -> str:
        table = {"0dB": 32768, "-2.5dB": 24576, "-6dB": 16384, "-12dB": 8192}
        if step not in table:
            raise ValueError(f"增益档位只能是 {list(table)}，收到 {step!r}")
        r = self.link.set_gain(table[step])
        return self._confirm(r.as_text(), "gain", table[step], "增益")

    def board_set_noise(self, level: int) -> str:
        r = self.link.set_noise(level)
        # 状态包里没有噪声档位（只有 rn_target），所以这里老实说读不回来
        time.sleep(0.4)
        summary, _st = self._snapshot()
        return (f"{r.as_text()}；{summary}"
                f"；噪声档位（{level}）不在状态包里，只能靠听感确认")

    def board_set_latency(self, frames: int) -> str:
        r = self.link.set_latency(frames)
        return self._confirm(r.as_text(), "dly_frames", int(frames), "延迟")

    def board_tone(self, on: bool) -> str:
        r = self.link.tone(bool(on))
        return self._confirm(r.as_text(), "tone", bool(on), "测试音")

    def board_link_check(self, seconds: float = 2.0) -> str:
        obs = self.link.watch(seconds)
        if not obs["online"]:
            return (f"{obs['window_s']} 秒内 50505 上收到 0 个包 → 板子没在发音频"
                    "（断电、网线、或 IP 不是 169.254.191.50）")
        src = ", ".join(f"{k}×{v}" for k, v in obs["sources"].items())
        return (f"板子在线：{obs['packets']} 个包 / {obs['window_s']} s "
                f"≈ {obs['frames_per_s']} 帧每秒；来源 {src}；序号跳变 {obs['seq_gaps']} 次")

    def board_status(self) -> str:
        summary, st = self._snapshot()
        if st is None:
            state = self.link.console_state()
            if state is None:
                return f"{summary}；PC 控制台也没在运行"
            return (f"{summary}；控制台在跑：listening={state.get('listening')} "
                    f"link={state.get('link')} fps={state.get('fps')}")
        listening = st.get("listening")
        return (f"{summary}；PC 监听={'开' if listening else '关' if listening is not None else '未知'}"
                f"（状态来自 {st.get('source')}）")

    def pc_set_listen(self, on: bool) -> str:
        ack = self.link.console_cmd("listen", v=bool(on))
        if ack is None:
            return "PC 控制台没在运行（8770 连不上），所以没法切换接收"
        time.sleep(0.5)
        state = self.link.console_state() or {}
        now = state.get("listening")
        if now is True or now is False:
            if bool(now) == bool(on):
                return f"电脑端接收已{'开' if on else '关'}（控制台回报 listening={now}）"
            return f"发了命令，但控制台回报 listening={now}，和要的不一致"
        return f"控制台回了 {ack}，但读不到 listening 字段"

    def esp32_run(self, code: str, port: str | None = None) -> str:
        try:
            return esp32.run(code, port=port)
        except esp32.EspError as e:
            return f"ESP32 通道不可用：{e}"

    def esp32_info(self, port: str | None = None) -> str:
        try:
            info = esp32.info(port=port)
        except esp32.EspError as e:
            return f"ESP32 通道不可用：{e}"
        keep = ("IMPL", "PLATFORM", "FLASH", "FREQ", "MEM_FREE", "UNIQUE")
        return "ESP32-S3：" + ", ".join(f"{k}={info.get(k, '?')}" for k in keep)
