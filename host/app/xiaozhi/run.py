"""命令行入口。

  python -m work.xiaozhi.run --text                # 文字对话（真模型，需要 key）
  python -m work.xiaozhi.run --text --fake         # 文字对话（假模型，完全离线）
  python -m work.xiaozhi.run --text --fake --dry   # 假模型 + 不发 UDP，给板子断电时用
  python -m work.xiaozhi.run --voice --speak       # 语音闭环（要麦克风和喇叭）
  python -m work.xiaozhi.run --probe               # 环境自检：TTS/ASR/板子/ESP32
  python -m work.xiaozhi.run --selftest            # 离线回归（不需要板子、不需要 key）
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="小智", description="语音助手：聊天 + 直接调板子")
    ap.add_argument("--text", action="store_true", help="文字对话（默认）")
    ap.add_argument("--voice", action="store_true", help="语音对话（麦克风 + ASR + TTS）")
    ap.add_argument("--speak", action="store_true", help="把回复念出来（PC 喇叭）")
    ap.add_argument("--board", action="store_true",
                    help="用板子的喇叭说（ESP32 TCP 9200）")
    ap.add_argument("--board-ip", default=None,
                    help="ESP32 的地址；不给就用环境变量 XIAOZHI_BOARD_HOST，再不然用代码里的默认值。"
                         "真值看串口 boot.py 打印的 `boot wifi OK <ip>`")
    ap.add_argument("--fake", action="store_true", help="用离线假模型，不调远端")
    ap.add_argument("--dry", action="store_true", help="只打印要下发什么，不真的发 UDP")
    ap.add_argument("--device", type=int, default=None, help="麦克风设备号（sounddevice）")
    ap.add_argument("--mic", default="auto", choices=["auto", "console", "device"],
                    help="语音来源：auto=优先用控制台的板子音频，device=PC 麦克风")
    ap.add_argument("--console", default=None, help="控制台地址，默认 http://127.0.0.1:8770")
    ap.add_argument("--probe", action="store_true", help="环境自检")
    ap.add_argument("--selftest", action="store_true", help="离线回归测试")
    ap.add_argument("--model", default=None, help="模型名，默认 deepseek-chat")
    ap.add_argument("--base-url", default=None, help="OpenAI 兼容端点")
    args = ap.parse_args(argv)

    if args.selftest:
        from .selftest import run_all
        return 0 if run_all() else 1

    if args.probe:
        from .probe import print_probe
        print_probe()
        return 0

    from . import agent

    bot = agent.build(fake=args.fake, dry_run=args.dry,
                      speak=args.speak or args.voice,
                      speak_board=args.board, board_host=args.board_ip, log=print)
    # 真模型：允许命令行覆盖端点/模型名
    if not args.fake and (args.model or args.base_url):
        from .llm import LLMClient
        bot.llm = LLMClient(model=args.model or bot.llm.model,
                            base_url=args.base_url or bot.llm.base_url)

    if not args.fake and not getattr(bot.llm, "ready", False):
        print("没有 API key。可以做两件事：")
        print("  1) set DEEPSEEK_API_KEY=...  或写进 work/xiaozhi/key.txt")
        print("  2) 先用假模型跑通闭环：python -m work.xiaozhi.run --text --fake")
        return 2

    if args.voice:
        bot.run_voice(device=args.device, source=args.mic, console=args.console)
    else:
        bot.run_text()
    return 0


if __name__ == "__main__":
    sys.exit(main())
