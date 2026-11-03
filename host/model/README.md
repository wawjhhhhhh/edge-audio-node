# 模型与算法来源

赛题要求写清"AI PC 侧模型的来源、精度与推理后端"。本作品的 AI 部分只有两处，其余是纯 DSP：

| 环节 | 模型 / 算法 | 来源与版本 | 精度 / 后端 | 是否随包 | 许可 |
|---|---|---|---|---|---|
| 降噪（PS 侧，默认档 1） | **RNNoise** 混合 DSP + 小型 GRU | Valin, *A Hybrid DSP/Deep Learning Approach to Real-Time Full-Band Speech Enhancement*, IEEE MMSP 2018；C 实现 `rnnoise` 0.1.1（xiph） | 浮点·定点混合，Cortex-A9 单核，10 ms 帧 | 头文件随包；库需自行编译（见 §4） | BSD-3 |
| 降噪（PS 侧，档 2） | 轻量噪声门 | 自写（阈值 + 攻放时间常数） | 定点整数 | 是 | 本项目 |
| 降噪（PL 侧，档 3） | **谱减**（FFT-512 + 18 张增益表） | 自写 RTL + Python 黄金模型（`fpga/src/denoise/*.py`） | 16 bit 定点，无学习权重 | 是（RTL 与生成脚本） | 本项目 |
| 语音识别（PC 侧，可选） | **Vosk small-cn-0.22** | alphacep/vosk-model-small-cn-0.22 | 小模型（约 42 MB），CPU | **否**（体积大，需另下） | Apache-2.0 |
| 语音合成（PC 侧） | Windows **SAPI**（系统音色，如 Huihui） | 操作系统内置 | 16 kHz 单声道 | 否（系统组件） | 随系统 |
| 语音合成（可选） | **edge-tts** 神经语音 `zh-TW-HsiaoChenNeural` | Microsoft Edge 在线语音服务 | 24 kHz 单声道 mp3 | 否（在线） | 服务条款 |
| 对话（PC 侧） | **DeepSeek chat**（在线 API） | `api.deepseek.com`，模型名见 `host/app/xiaozhi/llm.py` | 云端推理，无本地权重 | 否（需要 key） | 服务条款 |

## 1. Vosk 模型（唯一需要手工下载的东西）

```powershell
# 约 42 MB，解压后目录名必须是 vosk-model-small-cn-0.22
curl.exe -L -o vosk-cn.zip https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
Expand-Archive vosk-cn.zip -DestinationPath host\model\
```

代码里的查找顺序（`host/app/xiaozhi/asr.py`）：环境变量 `VOSK_MODEL` → `host/model/vosk-model-small-cn-0.22` → 仓库根的 `.tmp/asr_models/`。找不到时语音识别功能自动跳过，控制台的文字对话不受影响。

## 2. 大模型 API key

`host/app/xiaozhi/llm.py` 按顺序读环境变量 `DEEPSEEK_API_KEY` / `XIAOZHI_API_KEY` / `OPENAI_API_KEY`，再退到 `host/app/xiaozhi/key.txt`（**该文件被 `.gitignore` 忽略，不随包提供**，需要自己新建，内容只有一行 key）。

没有 key 时：控制台的文字对话会返回明确的 401 错误提示，其余功能（采集、降噪、A/B、双 DAC）不受影响。

## 4. RNNoise 库怎么来（重建固件时才需要）

`host/firmware/app_ea_main.c:28` 只 `#include "rnnoise.h"`，接口头文件已随包放在 `host/firmware/third_party/rnnoise/rnnoise.h`；**静态库要自己编**：

```bash
# 上游 0.1.1，与原始工程 work/rnnoise-0.1.1.tar.gz 同源
git clone https://github.com/xiph/rnnoise.git
cd rnnoise && ./autogen.sh && ./configure --host=arm-xilinx-eabi && make
# 得到 librnnoise.a，链接进 Vitis 的 app_ea 组件
```

RNNoise 的模型权重大约 78 MB（`rnnoise_data.c`），只存在于库内部，不单独分发。**不改固件就不用编它**——`fpga/build/output/app_ea_fpu.elf`（633 976 B，SHA256 前缀 `10DD461D1834`）已经是编译好的、在板上验证过的固件，直接下载即可。

## 5. 明确没有用到的东西
- 没有本地部署的大语言模型（显存/体积都用不上，比赛现场也不需要）。
- 没有用生成式模型合成"假"的降噪效果音频——`metrics/evidence/` 里的录音全部来自板子实测。
- PL 侧谱减不是神经网络，是经典谱减法的定点实现，因此不存在"训练集/精度"问题，只有"定点误差"问题（与 Python 黄金模型逐点比对，误差 ≤ 1 LSB）。
