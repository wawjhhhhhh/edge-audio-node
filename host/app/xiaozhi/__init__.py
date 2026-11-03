"""小智 —— 语音/文字驱动的小助手，能直接调 Zynq 音频板和 ESP32-S3。

模块边界：
  board.py   Zynq 控制面（UDP 50507）、链路观测（50505）、控制台 HTTP
  esp32.py   ESP32-S3 的 MicroPython REPL 通道（串口）
  tools.py   给模型看的工具表 + 参数校验 + 分发
  llm.py     DeepSeek / OpenAI 兼容客户端，以及离线可跑的假模型
  asr.py     Vosk 离线中文识别 + 能量端点检测
  tts.py     Windows SAPI 离线合成
  agent.py   主循环（听 → 想 → 调工具 → 说）
  run.py     命令行入口
"""

__all__ = ["board", "esp32", "tools", "llm", "asr", "tts", "agent"]
