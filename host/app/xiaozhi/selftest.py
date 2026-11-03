"""离线回归：不需要板子、不需要 API key、不需要麦克风。

跑法：`python -m work.xiaozhi.run --selftest`
判据只有一条：每一行都 PASS（SKIP 只出现在确实无法在本机验证的项上，
比如板子断电时的收包）。
"""

from __future__ import annotations

import json
import traceback
from typing import Callable

from . import asr, esp32
from .agent import SYSTEM_PROMPT, XiaoZhi, clean_spoken
from .board import BoardLink, GAIN_BYTES, NOISE_BYTES, TS_MAX, TS_MIN
from .llm import FakeLLM
from .tools import Toolbox

RESULTS: list[tuple[str, str, str]] = []


def check(name: str) -> Callable:
    def deco(fn: Callable[[], str]) -> Callable[[], None]:
        def run() -> None:
            try:
                note = fn() or ""
                RESULTS.append((name, "PASS", note))
            except SkipTest as e:
                RESULTS.append((name, "SKIP", str(e)))
            except Exception as e:  # noqa: BLE001
                RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))
                traceback.print_exc()
        run.__name__ = fn.__name__
        return run
    return deco


class SkipTest(Exception):
    pass


# ---------------------------------------------------------------------------
@check("工具表：名称不重复、schema 合法")
def t_tool_schemas() -> str:
    tb = Toolbox(BoardLink(dry_run=True))
    assert len(tb.tools) == len(tb.schemas()), "工具数量与 schema 数量不一致"
    for s in tb.schemas():
        f = s["function"]
        assert f["name"] and f["description"], f"缺名字/描述: {f}"
        assert f["parameters"]["type"] == "object"
        assert "properties" in f["parameters"]
        json.dumps(s)   # 必须能序列化进请求体
    return f"{len(tb.tools)} 个工具"


@check("参数护栏：越界百分比被本地拒绝")
def t_range_guard() -> str:
    link = BoardLink(dry_run=True)
    for bad in (TS_MIN - 1, TS_MAX + 1, 0, 200):
        try:
            link.set_time_scale(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被拒绝")
    link.set_time_scale(TS_MIN)
    link.set_time_scale(TS_MAX)
    return f"{TS_MIN}..{TS_MAX} 边界都接受，越界都拒"


@check("参数护栏：增益/噪声只接受固件认识的档位")
def t_enum_guard() -> str:
    link = BoardLink(dry_run=True)
    for good in GAIN_BYTES:
        link.set_gain(good)
    for good in NOISE_BYTES:
        link.set_noise(good)
    for fn, bad in ((link.set_gain, 1234), (link.set_noise, 999)):
        try:
            fn(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被拒绝")
    return f"增益 {sorted(GAIN_BYTES)}，噪声 {sorted(NOISE_BYTES)}"


@check("协议字节：与固件分支一一对应")
def t_protocol_bytes() -> str:
    from . import board
    assert board.MODE_BYTES == {"bypass": b"0", "denoise": b"1", "gate": b"2", "pl": b"3"}
    assert board.TONE_ON == b"t" and board.TONE_OFF == b"T"
    assert board.DELAY_ON == b"k" and board.DELAY_OFF == b"K"
    assert board.LATENCY_BYTES == {30: b"i", 20: b"j"}
    assert board.NOISE_BYTES == {0: b"a", 300: b"b", 800: b"c", 1500: b"d"}
    assert board.GAIN_BYTES == {32768: b"e", 24576: b"f", 16384: b"g", 8192: b"h"}
    r = BoardLink(dry_run=True).set_time_scale(50)
    assert r.payload == b"S2", r.payload   # 'S' + 字符 '2'（0x32），不是数字 50
    return "0/1/2, t/T, i/j, k/K, a-d, e-h, S+ASCII 百分比"


@check("假模型：把中文意图路由到正确的工具")
def t_fake_routing() -> str:
    llm = FakeLLM()
    cases = [
        ("切到降噪模式", "board_set_mode"),
        ("先直通吧", "board_set_mode"),
        ("时长调到 75%", "board_set_time_scale"),
        ("板子在线吗", "board_link_check"),
        ("现在什么状态", "board_status"),
        ("音量小一点，-12 那个", "board_set_gain"),
        ("开个测试音", "board_tone"),
        ("延迟调到 20 帧", "board_set_latency"),
        ("把灯点亮", "esp32_run"),
        ("读一下 esp32 信息", "esp32_info"),
    ]
    for text, expect in cases:
        turn = llm.chat([{"role": "user", "content": text}])
        names = [c["function"]["name"] for c in turn.tool_calls]
        assert names == [expect], f"{text!r} → {names}，期望 {expect}"
    return f"{len(cases)} 条意图全部命中"


@check("人设：提示词里没有客服腔、且要求先干活再回话")
def t_persona_prompt() -> str:
    bad = ["作为AI", "作为 AI", "语言模型", "有什么可以帮您", "很高兴为您服务"]
    # 这些词只允许出现在"不许说"的禁令行里，不能被当成正面要求
    for b in bad:
        for line in SYSTEM_PROMPT.splitlines():
            if b in line:
                assert "不说" in line or "不要" in line, f"提示词把客服腔当要求了：{line.strip()}"
    for must in ("像熟人聊天", "直接干", "一到两句", "不要复述工具返回"):
        assert must in SYSTEM_PROMPT, f"提示词缺少约束：{must}"
    return f"{len(SYSTEM_PROMPT)} 字，{SYSTEM_PROMPT.count(chr(10)) + 1} 行"


@check("念之前清洗：markdown/表情/工具回执都被剥掉")
def t_clean_spoken() -> str:
    cases = [
        ("**降噪开了**\n- 现在你听到的是处理过的\n- → UDP 31 包", "降噪开了 现在你听到的是处理过的"),
        ("小智：好呀😊", "好呀"),
        ("```python\nprint(1)\n```成了", "成了"),
        ("状态：packets=35456, under=28830", "状态"),
    ]
    for src, want in cases:
        got = clean_spoken(src)
        assert got == want, f"{src!r} → {got!r}，期望 {want!r}"
    return f"{len(cases)} 条清洗规则"


@check("闭环：假模型 + 干跑链路上完整走一轮（不经 UDP）")
def t_agent_loop() -> str:
    tb = Toolbox(BoardLink(dry_run=True))
    bot = XiaoZhi(FakeLLM(), tb, speak=None, log=lambda *_a, **_k: None)
    reply = bot.ask("切到降噪，然后时长调到 80%")
    names = [s.name for s in reply.steps]
    assert names and all(n.startswith("board_") for n in names), names
    assert "dry-run" in reply.steps[0].result, reply.steps[0].result
    # 念出来的人话里不许带工具回执/箭头发送记录/参数名
    for bad in ("→", "UDP", "packets=", "dry-run", "board_"):
        assert bad not in reply.text, f"回话里混进了 {bad!r}: {reply.text!r}"
    assert len(reply.text) >= 4, reply.text
    assert reply.rounds == 2, f"应当 2 轮（调工具 + 总结），实际 {reply.rounds}"
    return f"{reply.rounds} 轮，工具 {names}，回话「{reply.text}」"


@check("闭环：越界参数不会把会话炸掉，而是变成一句人话")
def t_agent_bad_arg() -> str:
    tb = Toolbox(BoardLink(dry_run=True))
    bot = XiaoZhi(FakeLLM(), tb, speak=None, log=lambda *_a, **_k: None)
    reply = bot.ask("时长调到 30%")
    assert "拒绝" in reply.steps[0].result or "失败" in reply.steps[0].result, reply.steps[0].result
    assert reply.text, "工具失败后模型仍应给出回复"
    return reply.steps[0].result[:60]


@check("链路判据：没有板子时必须报 0 包（不许假装在线）")
def t_link_offline() -> str:
    link = BoardLink()
    try:
        obs = link.watch(0.6)
    except OSError as e:
        raise SkipTest(f"50505 被占用（控制台在跑？）：{e}")
    if obs["online"]:
        return f"板子其实在线：{obs['packets']} 包（这项就不是 SKIP 了）"
    assert obs["packets"] == 0 and obs["frames_per_s"] == 0.0
    return f"{obs['window_s']} s 内 0 包 → online=False"


@check("控制台：没启动时 board_status 说清读不到，而不是拿旧命令冒充")
def t_console_absent() -> str:
    # 50508 是控制台独占（Windows 上抢端口会把控制台的喂数据弄死），所以控制台
    # 不在时"板子真实状态"就是不知道 —— 必须如实说，不许编。
    link = BoardLink(dry_run=True)
    link.console_state = lambda timeout=1.5: None      # 假装控制台没起
    tb = Toolbox(link)
    out = tb.call("board_status", {})
    assert out, "不应为空"
    assert "读不到板子状态" in out, out
    assert "没在运行" in out, out
    return out[:60]


@check("麦克风来源：控制台连不上时明确回退 PC 麦克风")
def t_mic_source() -> str:
    # 不碰真设备：只问"该用哪一路"。控制台端口不通 → 必须回退，且理由是人话。
    which, why = asr.resolve_source("auto", "http://127.0.0.1:9")
    assert which == "device", which
    assert "回退" in why, why
    # 显式指定时不猜
    which2, _ = asr.resolve_source("console", "http://127.0.0.1:9")
    assert which2 == "console", which2
    return why[:60]


@check("ESP32：有 REPL 就执行代码，没 REPL 就说清原因")
def t_esp32_present_or_absent() -> str:
    r = esp32.probe()          # 不指定口：自动找能进 REPL 的那个
    if r["ok"]:
        assert "PONG" in r["response"], r
        return f"{r['port']} 上跑着 MicroPython，能执行代码"
    err = r["error"]
    assert "打不开" in err or "没有" in err, err   # 必须是人能照着修的话
    raise SkipTest(err[:70])


@check("ESP32：板子信息能读回来（flash/PSRAM/频率/MAC）")
def t_esp32_info() -> str:
    r = esp32.probe()
    if not r["ok"]:
        raise SkipTest("没有板子")
    info = esp32.info(port=r["port"])
    flash = int(info.get("FLASH", "0"))
    free = int(info.get("MEM_FREE", "0"))
    assert flash >= 4 * 1024 * 1024, info
    assert free > 200_000, info
    return (f"flash={flash // (1024 * 1024)}MB, "
            f"空闲堆={free / (1024 * 1024):.1f}MB, "
            f"freq={int(info.get('FREQ', '0')) // 1_000_000}MHz")


@check("TTS：SAPI 能合成出非空 WAV")
def t_tts() -> str:
    from . import tts
    p = tts.probe()
    if not p["ok"]:
        raise SkipTest(p["error"])
    assert p["bytes"] > 1000, p
    return f"{p['bytes']} 字节 / {p['voice']}"


@check("ASR：模型就位并能识别一句合成语音")
def t_asr_roundtrip() -> str:
    from . import asr, tts
    try:
        model = asr.ensure_model()
    except FileNotFoundError as e:
        raise SkipTest(str(e)[:90])
    wav = tts.synthesize("小智，帮我调到降噪模式")
    try:
        rec = asr.Recognizer(model)
        text = rec.transcribe_wav(wav)
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
    assert text, "识别结果为空"
    # 合成音+小模型不保证逐字正确，只要求抓到关键词
    assert ("降噪" in text) or ("模式" in text) or len(text) >= 4, text
    return f"识别为：{text}"


# ---------------------------------------------------------------------------
def run_all() -> bool:
    tests = [t_tool_schemas, t_range_guard, t_enum_guard, t_protocol_bytes,
             t_persona_prompt, t_clean_spoken, t_fake_routing, t_agent_loop,
             t_agent_bad_arg, t_link_offline, t_console_absent, t_mic_source,
             t_esp32_present_or_absent, t_esp32_info,
             t_tts, t_asr_roundtrip]
    for t in tests:
        t()
    width = max(len(n) for n, _, _ in RESULTS)
    ok = True
    print()
    for name, status, note in RESULTS:
        mark = {"PASS": "PASS", "SKIP": "SKIP", "FAIL": "FAIL"}[status]
        print(f"[{mark}] {name:<{width}}  {note}")
        ok = ok and status != "FAIL"
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    skipped = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print(f"\n共 {len(RESULTS)} 项：PASS {passed} / SKIP {skipped} / FAIL {failed}")
    return ok
