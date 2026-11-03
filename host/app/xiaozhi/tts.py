"""语音合成（离线）：Windows SAPI → WAV → winsound 播放。

选 SAPI 而不是云 TTS 的原因很实际：这台机器到 api.openai.com 是超时的，
云 TTS 大概率同样不通；而 SAPI 是系统自带、断网可用、中文是 Huihui（zh-CN）。
代价是音色机械，等有了 key 和带宽可以换成远端 TTS，接口保持不变。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
import winsound
from pathlib import Path

VOICE_NAME = "Microsoft Huihui Desktop"

_PS_TEMPLATE = r"""
Add-Type -AssemblyName System.Speech
$sp = New-Object System.Speech.Synthesis.SpeechSynthesizer
$text = Get-Content -LiteralPath '{txt}' -Raw -Encoding UTF8
try {{ $sp.SelectVoice('{voice}') }} catch {{ }}
[void]$sp.SetOutputToWaveFile('{wav}')
$sp.Speak($text)
$sp.Dispose()
"""


def _powershell() -> str:
    # 5.1 的 powershell.exe 一定在；pwsh 不一定
    for cand in ("powershell.exe", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"):
        if os.path.exists(cand) or cand == "powershell.exe":
            return cand
    return "powershell.exe"


def synthesize(text: str, out_path: str | Path | None = None) -> Path:
    """把文本合成为 16-bit PCM WAV，返回文件路径。"""
    if not text.strip():
        raise ValueError("要合成的文本是空的")
    if out_path is None:
        out_path = Path(tempfile.gettempdir()) / f"xiaozhi_tts_{uuid.uuid4().hex[:8]}.wav"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    txt = out_path.with_suffix(".txt")
    # utf-8-sig：PowerShell 5.1 的 Get-Content -Encoding UTF8 见到 BOM 才不会把中文读成乱码
    txt.write_text(text, encoding="utf-8-sig")
    script = _PS_TEMPLATE.format(txt=str(txt).replace("'", "''"),
                                 wav=str(out_path).replace("'", "''"),
                                 voice=VOICE_NAME)
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"SAPI 合成失败 rc={proc.returncode}：{(proc.stderr or '').strip()[:300]}")
    if out_path.stat().st_size < 1000:
        raise RuntimeError(f"合成出来的 WAV 只有 {out_path.stat().st_size} 字节，不像有效音频")
    return out_path


def speak(text: str, blocking: bool = True) -> Path:
    """合成并播放；返回 WAV 路径（不删，方便事后回听）。"""
    wav = synthesize(text)
    flags = winsound.SND_FILENAME | (0 if blocking else winsound.SND_ASYNC)
    winsound.PlaySound(str(wav), flags)
    if blocking:
        # winsound 阻塞播放已经等完了；这里只做个兜底，防止 SND_ASYNC 被误用
        time.sleep(0)
    return wav


def probe() -> dict:
    """不自曝错误的自检：返回可用与否 + 一句话原因。"""
    try:
        wav = synthesize("自检")
    except Exception as e:  # noqa: BLE001 - 自检要把任何失败都变成字符串
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    size = wav.stat().st_size
    try:
        wav.unlink()
    except OSError:
        pass
    return {"ok": True, "bytes": size, "voice": VOICE_NAME}
