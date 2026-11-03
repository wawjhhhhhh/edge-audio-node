import wave, numpy as np, os
d = r"D:\deepseek herness\材料库\04-实测记录\音频样本\2026-10-27_三档语音对比"
for f in ["01_旁路.wav","02_轻量门.wav","03_RNNoise_48fps.wav"]:
    p = os.path.join(d,f); w = wave.open(p); n=w.getnframes(); sr=w.getframerate(); ch=w.getnchannels()
    x = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1,ch)[:,0].astype(np.float64)
    rms = np.sqrt((x**2).mean()); peak=np.abs(x).max()
    # 噪声底：取最安静 10% 帧的 rms
    fr = x[:len(x)//480*480].reshape(-1,480); e = np.sqrt((fr**2).mean(1))
    floor = np.percentile(e,10); loud=np.percentile(e,90)
    print(f"{f:26s} {n/sr:6.2f}s {sr}Hz ch{ch}  rms={20*np.log10(rms/32768+1e-9):6.1f} dBFS peak={20*np.log10(peak/32768+1e-9):6.1f}  floor={20*np.log10(floor/32768+1e-9):6.1f} loud={20*np.log10(loud/32768+1e-9):6.1f}  帧数={len(fr)}")
