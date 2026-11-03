"""语音识别（离线）：Vosk 小模型 + 能量端点检测。

为什么是 Vosk：`huggingface.co` 在这台机器上是超时的，Whisper 那套（faster-whisper
要下模型）基本没法装；Vosk 的模型站 `alphacephei.com` 可达，中文小模型 42 MB，
纯 CPU 就能跑，16 kHz 单声道正好是我们要的格式。

端点检测用最朴素的 RMS 门限，不上 VAD 模型：这个场景是"按钮式对话"，
环境一旦有持续噪声（板子的测试音、风扇），模型级 VAD 反而更难调。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / ".tmp" / "asr_models"
MODEL_NAME = "vosk-model-small-cn-0.22"
SAMPLE_RATE = 16000
BLOCK = 4000            # 250 ms —— vosk 接受任意长度，这个粒度端点响应够快

RMS_START = 320         # 起说门限（int16 幅度）
RMS_KEEP = 200          # 续说门限，比 START 低，避免句尾被切断
SILENCE_BLOCKS = 4      # 4 × 250 ms = 1 s 静音算说完
MAX_UTTERANCE_BLOCKS = 40   # 10 s 硬上限，防止一直说下去不结束


def ensure_model() -> Path:
    """模型目录存在就返回；有 zip 就解压；都没有就报一个能照做的错。"""
    target = MODEL_DIR / MODEL_NAME
    if (target / "am" / "final.mdl").exists() or (target / "conf").exists():
        return target
    zips = sorted(MODEL_DIR.glob("*.zip")) if MODEL_DIR.exists() else []
    for z_path in zips:
        # 半截文件（下载中途被杀）不是 zip，直接当"没有"处理，别抛 BadZipFile
        if z_path.stat().st_size < 10_000_000 or not zipfile.is_zipfile(z_path):
            continue
        with zipfile.ZipFile(z_path) as z:
            z.extractall(MODEL_DIR)
        if target.exists():
            return target
    raise FileNotFoundError(
        f"没有 ASR 模型：期望 {target}。先下载 "
        f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip 到 {MODEL_DIR} 再运行。")


def resolve_source(source: str = "auto", console: str | None = None) -> tuple[str, str]:
    """把 auto 解析成明确的一路麦克风。返回 (来源, 一句人话理由)。

    console = 用控制台正在处理的那一路（板子 ES8388 → UDP 50505 → /api/pcm），
    device  = 直接开 PC 麦克风。auto 优先控制台：只有控制台在收听，
              小智才"听得到板子听到的东西"，也不会和它抢同一个设备。
    """
    if source in ("console", "device"):
        return source, "按参数指定"
    from .console_mic import DEFAULT_BASE, ConsoleMic
    mic = ConsoleMic(console or DEFAULT_BASE)
    ok, why = mic.available()
    if ok:
        return "console", why
    return "device", "回退 PC 麦克风：" + why


class Recognizer:
    """把一段 PCM（或麦克风流）转成中文文本。"""

    def __init__(self, model_path: Path | None = None) -> None:
        from vosk import Model, KaldiRecognizer, SetLogLevel  # 延迟导入，纯文字模式不需要
        SetLogLevel(-1)
        self.model = Model(str(model_path or ensure_model()))
        self._rec_factory = KaldiRecognizer
        self._sr = SAMPLE_RATE

    def new_stream(self):
        rec = self._rec_factory(self.model, self._sr)
        rec.SetWords(False)
        return rec

    # ---- 离线文件 --------------------------------------------------------
    def transcribe_wav(self, wav_path: str | Path) -> str:
        import wave
        with wave.open(str(wav_path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("只支持 16-bit 单声道 WAV")
            rec = self.new_stream()
            parts: list[str] = []
            while True:
                data = w.readframes(BLOCK)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    parts.append(json.loads(rec.Result()).get("text", ""))
            parts.append(json.loads(rec.FinalResult()).get("text", ""))
        return " ".join(p for p in parts if p).strip()

    # ---- 麦克风 ----------------------------------------------------------
    def _blocks(self, source: str, device: int | None, console: str):
        """按来源产出 16 kHz 单声道块：console = 控制台的板子麦克风，device = PC 麦克风。"""
        if source == "console":
            from .console_mic import ConsoleMic, ConsoleMicError
            mic = ConsoleMic(console)
            ok, why = mic.available()
            if not ok:
                raise ConsoleMicError(why)
            yield from mic.blocks(BLOCK)
            return
        import sounddevice as sd
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK, device=device,
                               dtype="int16", channels=1) as stream:
            while True:
                data, _overflow = stream.read(BLOCK)
                yield bytes(data)

    def resolve_source(self, source: str = "auto", console: str | None = None) -> tuple[str, str]:
        """把 auto 解析成明确的一路。返回 (来源, 一句人话理由)。"""
        return resolve_source(source, console)

    def listen_once(self, timeout_s: float = 12.0, device: int | None = None,
                    source: str = "auto", console: str | None = None) -> str:
        """阻塞到"说完一句"，返回识别文本；一直没人说话就返回空串。"""
        import numpy as np

        rec = self.new_stream()
        said = False
        silence = 0
        blocks = 0
        parts: list[str] = []
        which, _why = self.resolve_source(source, console)
        for frame in self._blocks(which, device, console or ""):
            if blocks >= MAX_UTTERANCE_BLOCKS:
                break
            blocks += 1
            rms = float(np.sqrt(np.mean(np.frombuffer(frame, dtype=np.int16).astype(np.float32) ** 2)))
            if not said:
                if rms < RMS_START:
                    if blocks * BLOCK / SAMPLE_RATE > timeout_s:
                        return ""
                    continue
                said = True
            else:
                silence = silence + 1 if rms < RMS_KEEP else 0
            if rec.AcceptWaveform(frame):
                parts.append(json.loads(rec.Result()).get("text", ""))
            if said and silence >= SILENCE_BLOCKS:
                break
        parts.append(json.loads(rec.FinalResult()).get("text", ""))
        return " ".join(p for p in parts if p).strip()


def probe() -> dict:
    try:
        path = ensure_model()
        from vosk import Model
        Model(str(path))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "model": str(path)}
