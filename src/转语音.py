# -*- coding: utf-8 -*-
"""
指定 txt 文件 -> 多段 MP3（edge-tts，防截断） + 自动重试 + 并发提速 + 可选合并

依赖：
  pip install edge-tts

可选：
  安装 ffmpeg 后可自动无损合并为一个 MP3
  ffmpeg -version

用法：
  python tts_longtext_to_mp3_fast.py D:\path\your_script.txt
  python tts_longtext_to_mp3_fast.py .\script.txt

输出：
  parts_YYYYMMDD_<txt名>\part_0001.mp3 ...
  output_full_YYYYMMDD_<txt名>.mp3
"""

import os
import re
import sys
import shutil
import asyncio
import subprocess
from datetime import datetime
from typing import List, Tuple

import edge_tts

from pipeline_config import config_value, project_path

# ===================== 可调参数区 =====================
VOICE = "zh-CN-YunyangNeural"
RATE = "+50%"      # 1.5x
PITCH = "+0Hz"     # 不想传就改成 None
VOLUME = "+5%"

# 单段最大字符数：适当调大，减少分段数量
MAX_CHARS_PER_CHUNK = 7000

# 句子切分：优先在这些标点后切
SENT_SPLIT_PATTERN = r"(?<=[。！？；])"

# 自动重试参数：比原版更激进一些，兼顾速度
MAX_RETRIES = 2
INITIAL_RETRY_DELAY = 1.5
RETRY_BACKOFF = 1.6
RETRY_JITTER = 0.4

# 并发数：建议先 3，网络稳定可试 4 或 5
CONCURRENCY = 3
# =====================================================


def load_text(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到文件：{path}")
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    return txt.replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_text_for_speech(text: str) -> str:
    """
    最小口播化：
    - 空行 -> 强停顿
    - 普通换行 -> 轻停顿
    - 收敛空白和连续标点
    """
    t = text.strip()
    if not t:
        return ""

    t = re.sub(r"\n\s*\n+", "。\n", t)
    t = re.sub(r"\n+", "，", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*，\s*", "，", t)
    t = re.sub(r"[，]{2,}", "，", t)
    t = re.sub(r"[。]{2,}", "。", t)

    if t and t[-1] not in "。！？":
        t += "。"
    return t


def split_to_chunks(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> List[str]:
    """
    按句子优先切块，避免单次合成太长。
    """
    sentences = re.split(SENT_SPLIT_PATTERN, text)
    sentences = [s.strip() for s in sentences if s and s.strip()]

    chunks: List[str] = []
    buf = ""

    for s in sentences:
        if len(s) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            start = 0
            while start < len(s):
                chunks.append(s[start:start + max_chars])
                start += max_chars
            continue

        if not buf:
            buf = s
        elif len(buf) + len(s) <= max_chars:
            buf += s
        else:
            chunks.append(buf)
            buf = s

    if buf:
        chunks.append(buf)

    return chunks


def safe_stem(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", stem)
    return stem[:60] if len(stem) > 60 else stem


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def merge_mp3_with_ffmpeg(parts: List[str], merged_path: str, ffmpeg: str) -> None:
    """
    ffmpeg concat 无损拼接 mp3（不重新编码）
    使用 ASCII + 相对路径，绕开 BOM 和中文绝对路径乱码。
    """
    work_dir = os.path.dirname(os.path.abspath(merged_path)) or "."
    list_path = os.path.join(work_dir, "concat_list.txt")

    lines = []
    for p in parts:
        rel = os.path.relpath(os.path.abspath(p), work_dir).replace("\\", "/")
        lines.append(f"file '{rel}'")

    with open(list_path, "w", encoding="ascii", errors="ignore") as f:
        f.write("\n".join(lines) + "\n")

    cmd = [
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        merged_path
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 合并失败：\n{r.stderr}")


async def synth_chunk_once(text: str, out_path: str) -> None:
    kwargs = dict(text=text, voice=VOICE, rate=RATE, volume=VOLUME)
    if PITCH is not None:
        kwargs["pitch"] = PITCH
    communicate = edge_tts.Communicate(**kwargs)
    await communicate.save(out_path)


async def synth_chunk_with_retry(text: str, out_path: str, index: int, total: int) -> str:
    """
    单段合成 + 自动重试
    """
    import random

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)

            print(f"[{index}/{total}] 合成第 {attempt} 次尝试：{out_path}")
            await synth_chunk_once(text, out_path)

            if not os.path.exists(out_path):
                raise RuntimeError("输出文件不存在")
            if os.path.getsize(out_path) <= 0:
                raise RuntimeError("输出文件大小为 0")

            return out_path

        except Exception as e:
            last_err = e
            if attempt >= MAX_RETRIES:
                break

            delay = INITIAL_RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))
            delay *= (1 + random.uniform(-RETRY_JITTER, RETRY_JITTER))
            delay = max(0.8, delay)

            print(
                f"[{index}/{total}] 第 {attempt} 次失败：{e}\n"
                f"将在 {delay:.1f} 秒后重试..."
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"分段合成最终失败：{out_path}\n最后错误：{last_err}")


async def worker(
    semaphore: asyncio.Semaphore,
    text: str,
    out_path: str,
    index: int,
    total: int
) -> Tuple[int, str]:
    async with semaphore:
        print(f"[{index}/{total}] 开始处理，chars={len(text)}")
        result_path = await synth_chunk_with_retry(text, out_path, index, total)
        return index, result_path


async def main(txt_file: str) -> int:
    raw = load_text(txt_file)
    text = normalize_text_for_speech(raw)
    if not text:
        print("txt 内容为空。")
        return 2

    chunks = split_to_chunks(text, MAX_CHARS_PER_CHUNK)

    date_tag = datetime.now().strftime("%Y%m%d")
    name_tag = safe_stem(txt_file)
    output_root = project_path(config_value("files", "output_dir", "data/output"))
    output_root.mkdir(parents=True, exist_ok=True)

    out_dir = str(output_root / f"parts_{date_tag}_{name_tag}")
    os.makedirs(out_dir, exist_ok=True)

    total = len(chunks)
    print(f"共切分为 {total} 段。")
    print(f"并发数：{CONCURRENCY}")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = []

    for i, ch in enumerate(chunks, 1):
        out_path = os.path.join(out_dir, f"part_{i:04d}.mp3")
        tasks.append(worker(semaphore, ch, out_path, i, total))

    results = await asyncio.gather(*tasks)

    # 按序恢复 part_paths，避免并发完成顺序打乱
    results.sort(key=lambda x: x[0])
    part_paths = [path for _, path in results]

    print(f"已生成分段 MP3：{len(part_paths)} 个，目录：{out_dir}")

    merged_mp3 = str(output_root / f"output_full_{date_tag}_{name_tag}.mp3")

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        print("检测到 ffmpeg，开始无损拼接为单个 MP3 ...")
        merge_mp3_with_ffmpeg(part_paths, merged_mp3, ffmpeg)
        print(f"拼接完成：{merged_mp3}")
    else:
        print("未检测到 ffmpeg：已保留分段 MP3，暂不生成总 MP3。")

    print("完成。")
    print(f"分段目录：{out_dir}")
    if ffmpeg:
        print(f"总音频：{merged_mp3}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python tts_longtext_to_mp3_fast.py <要转语音的txt文件路径>")
        print(r"示例：python tts_longtext_to_mp3_fast.py D:\股票学习\script.txt")
        sys.exit(1)

    txt_path = sys.argv[1]
    try:
        sys.exit(asyncio.run(main(txt_path)))
    except Exception as e:
        print(f"运行失败：{e}")
        sys.exit(99)
