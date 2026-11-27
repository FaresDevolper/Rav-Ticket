"""يحول PCM (48kHz, استيريو, 16-bit) إلى WAV (16kHz, مونو, 16-bit) (مطابق لـ audio.js)."""
import math
import struct
import sys
from array import array


def to_wav_16k_mono(pcm: bytes):
    """يرجع (wav_bytes, rms, seconds)."""
    frames = len(pcm) // 4
    out_len = frames // 3
    if out_len <= 0:
        return b"", 0.0, 0.0

    samples = array("h")
    samples.frombytes(pcm[: out_len * 3 * 4])
    if sys.byteorder == "big":
        samples.byteswap()

    # كل عينة خارجة = متوسط 3 فريمات استيريو (6 قيم)، تقريب نصف لفوق مثل Math.round
    cols = [samples[k::6] for k in range(6)]
    mono = array("h", [(a + b + c + d + e + f + 3) // 6 for a, b, c, d, e, f in zip(*cols)])

    sum_sq = 0
    peak = 0
    for s in mono:
        sum_sq += s * s
        a = -s if s < 0 else s
        if a > peak:
            peak = a

    rms = math.sqrt(sum_sq / out_len) if out_len else 0.0
    seconds = out_len / 16000

    # رفع الصوت الخافت (حد أقصى 6 أضعاف) لتحسين دقة التعرف
    gain = 1.0
    if 0 < peak < 12000:
        gain = min(6.0, 12000 / peak)

    if gain != 1.0:
        out = array("h", [max(-32768, min(32767, math.floor(s * gain + 0.5))) for s in mono])
    else:
        out = mono
    if sys.byteorder == "big":
        out.byteswap()
    data = out.tobytes()

    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data, rms, seconds
