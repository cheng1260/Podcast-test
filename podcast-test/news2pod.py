#!/usr/bin/env python3
"""
news2pod —— 把你 AI Agent 每天产出的新闻，做成一个私人播客。

原理：iOS 的播客 App 支持「通过 URL 关注节目」(Apple Podcasts / Overcast / Pocket Casts
等都可)。只要这台机器上有一个 feed.xml + mp3，手机就能像订阅节目一样每天自动收到新单集：
自动下载、推送通知、断点续听、锁屏/CarPlay/AirPods 可控。不用每次手动保存、转码、传文件。

流程：
  1. 读取 inbox/*.md|*.txt 里 Agent 当天产出的新闻（按天合并成一期；也可 --text 直接喂）
  2. 清洗成适合朗读的口播稿（去链接、去 markdown、长句分段）
  3. edge-tts 合成 mp3（免费、无需 API key）→ 拼接 + 响度归一化
  4. 重写 feed.xml（保留最近 N 期）
  5. 由本机 http 服务对外提供，手机通过 Tailscale / 内网 IP 访问

用法：
  python news2pod.py --root ./site --text "今天的三条新闻……"
  python news2pod.py --root ./site --inbox ./inbox          # 处理 inbox 里所有文件，处理后移到 done/
  python news2pod.py --root ./site --inbox ./inbox --serve   # 生成完顺便起服务（阻塞）
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# ---------------- 可调参数 ----------------
VOICE = os.environ.get("N2P_VOICE", "zh-CN-XiaoxiaoNeural")  # 女声；男声 zh-CN-YunxiNeural
RATE = os.environ.get("N2P_RATE", "+12%")                   # 播报语速，新闻建议 +10%~+20%
TITLE = os.environ.get("N2P_TITLE", "我的每日新闻")
LANGUAGE = "zh-cn"
KEEP = int(os.environ.get("N2P_KEEP", "7"))                 # 保留最近几期（旧的自动删 mp3）
URL_TEMPLATE = os.environ.get("N2P_URL_TEMPLATE", "")        # 例如 https://x.com/{owner}/{repo}/raw/{branch}
CHUNK = 1200                                                # 单次 TTS 文本上限（字符）
# ------------------------------------------

AUDIO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="1400" viewBox="0 0 {w} 1400">
<rect width="{w}" height="1400" fill="#1c202c"/>
{art}
</svg>"""


def log(*a):
    print(*a, flush=True)


# ---------- 文本清洗：让 TTS 读起来像人话 ----------

def clean(text: str) -> str:
    t = text.replace("\r\n", "\n")
    t = re.sub(r"```.*?```", "", t, flags=re.S)                    # 代码块去掉
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)                      # 图片
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)                  # 链接只留文字
    t = re.sub(r"https?://\S+", "", t)                              # 裸链接
    t = re.sub(r"<[^>]+>", "", t)                                   # html 标签
    t = re.sub(r"[*_`#>|]+", "", t)                                 # markdown 符号
    t = re.sub(r"^[\s\-•·]+", "", t, flags=re.M)                    # 列表符号
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def trim_to_seconds(script: str, max_minutes: float) -> str:
    """按每秒约 4.4 个汉字估算，超出时长就在句号处截断，并补一句收尾。"""
    limit = int(max_minutes * 60 * 4.4)
    if len(script) <= limit:
        return script
    cut = script[:limit]
    pos = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"))
    cut = cut[:pos + 1] if pos > limit * 0.6 else cut
    return cut + "\n以上就是今天的全部内容，明天见。"


def to_script(text: str) -> str:
    """把新闻正文整理成口播稿：加开场，段落之间留停顿。"""
    body = clean(text)
    if not body:
        return ""
    today = datetime.now().strftime("%m月%d日")
    head = f"{today}新闻简报。"
    if re.match(r"^\s*\d{1,2}月\d{1,2}日新闻简报", body):   # 稿子自带开场就不重复加
        head = ""
    paras = [p.strip() for p in body.split("\n") if p.strip()]
    # 单段太长时按句号切开，避免 TTS 一口气读不完、断句奇怪
    out = []
    for p in paras:
        if len(p) <= 160:
            out.append(p)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？!?])", p):
            if len(buf) + len(sent) > 160 and buf:
                out.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            out.append(buf)
    return (head + "\n" + "\n".join(out)).strip()


def chunks(text: str, size: int = CHUNK) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}".strip()
    if cur:
        parts.append(cur)
    return parts


# ---------- 语音合成 ----------

def tts_chunk(text: str, path: Path, retries: int = 3) -> None:
    """合成一段音频，同时用 edge-tts 的句边界信息产出 SRT 字幕（无需额外 API）。"""
    import asyncio
    import edge_tts

    for i in range(retries):
        try:
            srt = path.with_suffix(".srt")

            async def _run():
                comm = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="SentenceBoundary")
                buf, cues = bytearray(), []
                # 全部收进内存再落盘：避免异步生成器清理时文件已关闭
                async for item in comm.stream():
                    if item["type"] == "audio":
                        buf.extend(item["data"])
                    elif item["type"] in ("SentenceBoundary", "WordBoundary"):
                        cues.append((item["offset"], item["duration"], item["text"]))
                return bytes(buf), cues

            audio, cues = asyncio.run(_run())
            path.write_bytes(audio)
            # 100ns 单位 -> 秒
            with srt.open("w", encoding="utf-8") as sub:
                for n, (off, dur, txt) in enumerate(cues, 1):
                    t0, t1 = off / 1e7, (off + dur) / 1e7
                    sub.write(f"{n}\n{fmt_srt(t0)} --> {fmt_srt(t1)}\n{txt}\n\n")
            if path.exists() and path.stat().st_size > 2000 and cues:
                return
        except Exception as e:                                     # 网络抖动/限流
            log(f"  TTS 重试 {i + 1}: {e}")
        path.unlink(missing_ok=True)
        path.with_suffix(".srt").unlink(missing_ok=True)
    raise RuntimeError("TTS 连续失败，稍后重跑即可（已生成的部分不会重复消耗）")


def fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def fmt_vtt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    out = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        def sec(x):
            h, m, rest = x.replace(",", ".").split(":")
            return int(h) * 3600 + int(m) * 60 + float(rest)
        a, b = [sec(v.strip()) for v in lines[1].split("-->")]
        out.append((a, b, " ".join(lines[2:])))
    return out


def write_captions(cues: list[tuple[float, float, str]], root: Path, stamp: str) -> None:
    """按拼接后的时间轴输出标准 WebVTT 与 SRT，供播客 App 读取。"""
    d = root / "transcripts"
    d.mkdir(exist_ok=True)
    # edge-tts 的 duration 偶尔会略微越过下一句起点，统一裁到下一句开始，避免字幕重叠
    fixed = []
    for i, (a, b, txt) in enumerate(cues):
        end = min(b, cues[i + 1][0]) if i + 1 < len(cues) else b
        if end <= a:
            end = a + 0.3
        fixed.append((a, end, txt))
    vtt = ["WEBVTT", ""]
    srt = []
    for n, (a, b, txt) in enumerate(fixed, 1):
        vtt += [f"{fmt_vtt(a)} --> {fmt_vtt(b)}", txt, ""]
        srt += [str(n), f"{fmt_srt(a)} --> {fmt_srt(b)}", txt, ""]
    (d / f"{stamp}.vtt").write_text("\n".join(vtt), encoding="utf-8")
    (d / f"{stamp}.srt").write_text("\n".join(srt), encoding="utf-8")


def make_audio(script: str, out: Path, tmp: Path):
    parts, all_cues, offset = [], [], 0.0
    segs = chunks(script)
    for i, c in enumerate(segs):
        p = tmp / f"{i:03d}.mp3"
        log(f"  合成第 {i + 1}/{len(segs)} 段 …")
        tts_chunk(c, p)
        parts.append(p)
        for a, b, txt in parse_srt(p.with_suffix(".srt")):
            all_cues.append((a + offset, b + offset, txt))
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        offset += float(r.stdout.strip() or 0)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    # 拼接 + EBU R128 响度归一到 -16 LUFS（播客标准，路上听着不忽大忽小）
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1", "-b:a", "64k", str(out)],
        check=True,
    )
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0), all_cues


# ---------- feed ----------

def esc(s: str) -> str:
    return html.escape(s, quote=False)


def apply_template(tpl: str, repo_slug: str, branch: str) -> str:
    """N2P_URL_TEMPLATE 支持 {repo}(owner/name) 与 {name}(仅仓库名) 两种占位。"""
    if not tpl:
        return ""
    owner = repo_slug.split("/")[0] if "/" in repo_slug else ""
    return tpl.format(repo=repo_slug, owner=owner, name=repo_slug.split("/")[-1], branch=branch).rstrip("/")


def build_feed(site_url: str, episodes: list[dict], cover: Path | None) -> str:
    items = []
    base = site_url.rsplit("/episodes", 1)[0] if "/episodes" in site_url else site_url
    for e in sorted(episodes, key=lambda x: x["published"], reverse=True):
        pub = format_datetime(e["published"])
        secs = int(e["duration"])
        notes = esc(e.get("notes", "")).replace("\n", "<br/>")
        cover_tag, trans_tag = "", ""
        if cover:
            cover_tag = (f'\n      <itunes:image href="{base}/{cover.name}"/>')
        if e.get("transcript"):
            t = e["transcript"]
            trans_tag = (f'\n      <podcast:transcript url="{base}/transcripts/{t}.vtt" type="text/vtt" language="zh-CN"/>'
                         f'\n      <podcast:transcript url="{base}/transcripts/{t}.srt" type="application/x-subrip" language="zh-CN"/>')
        items.append(f"""    <item>
      <title>{esc(e['title'])}</title>
      <description>{notes}</description>
      <link>{esc(e.get('link') or site_url)}</link>
      <guid isPermaLink="false">{esc(e['guid'])}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{site_url}/episodes/{e['file']}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{secs // 60}:{secs % 60:02d}</itunes:duration>{cover_tag}{trans_tag}
    </item>""")
    image = ""
    if cover:
        image = (f'\n    <image><url>{site_url}/{cover.name}</url><title>{esc(TITLE)}</title>'
                 f'<link>{site_url}</link></image>\n    <itunes:image href="{site_url}/{cover.name}"/>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{esc(TITLE)}</title>
    <link>{site_url}</link>
    <description>AI 生成的每日新闻简报（私人订阅，未提交目录）</description>
    <language>{LANGUAGE}</language>
    <itunes:author>Personal</itunes:author>
    <itunes:block>Yes</itunes:block>
    <itunes:explicit>false</itunes:explicit>{image}
{chr(10).join(items)}
  </channel>
</rss>
"""


def parse_feed(path: Path) -> list[dict]:
    """从已有 feed 读回历史单集，实现增量更新。"""
    if not path.exists():
        return []
    import xml.etree.ElementTree as ET
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    eps = []
    for item in ET.parse(path).getroot().iter("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        dur = item.findtext("itunes:duration", "0", ns) or "0"
        secs = 0
        for f in dur.split(":"):
            secs = secs * 60 + int(f or 0)
        eps.append({
            "guid": item.findtext("guid") or "",
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
            "published": datetime.strptime(item.findtext("pubDate"), "%a, %d %b %Y %H:%M:%S %z"),
            "file": Path(enc.get("url", "")).name,
            "bytes": int(enc.get("length", "0")),
            "duration": secs,
            "notes": html.unescape(re.sub(r"<img[^>]*>", "", item.findtext("description") or "")),
            "transcript": (item.find("{%s}transcript" % "https://podcastindex.org/namespace/1.0").get("url", "").rsplit("/", 1)[-1].removesuffix(".vtt")
                           if item.find("{%s}transcript" % "https://podcastindex.org/namespace/1.0") is not None else ""),
        })
    return eps


def ensure_cover(root: Path) -> Path | None:
    """iOS/各播客 App 对没有封面的订阅表现不一，给个简单的 1400x1400 封面。"""
    p = root / "cover.png"
    if p.exists():
        return p
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    im = Image.new("RGB", (1400, 1400), (28, 32, 44))
    d = ImageDraw.Draw(im)
    for i, h in enumerate([120, 220, 300, 180, 260, 140, 200]):
        x = 430 + i * 80
        d.rounded_rectangle((x, 700 - h // 2, x + 34, 700 + h // 2), 17, fill=(240, 180, 80))
    d.rounded_rectangle((470, 900, 930, 916), 8, fill=(90, 96, 112))
    im.save(p)
    return p


# ---------- main ----------

def unself(root: Path) -> None:
    """把自己和 bundle.txt 存进站点目录，供定时任务下次自举（云端沙箱每次都会销毁）。"""
    here = Path(__file__).resolve().parent
    for name in ("news2pod.py", "bundle.txt"):
        src = here / name
        if src.exists() and src.resolve() != (root / name).resolve():
            shutil.copy2(src, root / name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="./site", help="站点目录（feed.xml / episodes / cover 都在这里）")
    ap.add_argument("--inbox", help="待处理文本所在目录，处理完移到 done/")
    ap.add_argument("--text", help="直接传入新闻正文")
    ap.add_argument("--text-file", help="从文件读取新闻正文（长文本用这个，避免命令行转义问题）")
    ap.add_argument("--site-url", default=os.environ.get("N2P_URL", ""),
                    help="手机能访问到的根地址，如 http://100.64.0.5:8777 或内网 IP")
    ap.add_argument("--raw", help="另存原始条目清单（带来源链接）到该路径，供网页版查看")
    ap.add_argument("--repo", default=os.environ.get("N2P_REPO", ""), help="owner/name，配合 URL_TEMPLATE 使用")
    ap.add_argument("--branch", default=os.environ.get("N2P_BRANCH", "main"))
    ap.add_argument("--serve", action="store_true", help="生成后启动 http 服务（阻塞）")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    eps_dir = root / "episodes"
    root.mkdir(parents=True, exist_ok=True)
    eps_dir.mkdir(exist_ok=True)

    texts: list[str] = []
    if args.text:
        texts.append(args.text)
    if args.text_file:
        texts.append(Path(args.text_file).read_text(encoding="utf-8", errors="ignore"))
    if args.inbox:
        inbox = Path(args.inbox)
        for f in sorted(inbox.glob("*")):
            if f.is_file() and f.suffix.lower() in (".md", ".txt"):
                texts.append(f.read_text(encoding="utf-8", errors="ignore"))
                (inbox / "done").mkdir(exist_ok=True)
                shutil.move(str(f), str(inbox / "done" / f.name))
    if not texts:
        log("没有新内容，仅刷新 feed。")
    else:
        script = to_script("\n\n".join(texts))
        cap = float(os.environ.get("N2P_MAX_MINUTES", "0") or 0)
        if cap > 0:
            script = trim_to_seconds(script, cap)
        if not script:
            log("内容为空，跳过。")
            return 0
        today = datetime.now()
        if os.environ.get("N2P_STAMP"):                      # 允许外部指定日期，便于补跑
            today = datetime.strptime(os.environ["N2P_STAMP"], "%Y-%m-%d")
        stamp = today.strftime("%Y%m%d")
        fname = f"{stamp}.mp3"
        out = eps_dir / fname
        log(f"生成 {fname}（{len(script)} 字）…")
        with tempfile.TemporaryDirectory() as tmp:
            dur, cues = make_audio(script, out, Path(tmp))
        write_captions(cues, root, stamp)
        log(f"  字幕 {len(cues)} 条 → transcripts/{stamp}.vtt / .srt")
        log(f"  → {out.stat().st_size / 1024:.0f} KB, {dur / 60:.1f} 分钟")

        feed_path = root / "feed.xml"
        eps = parse_feed(feed_path)
        eps = [e for e in eps if e["guid"] != f"{TITLE}/{stamp}"]
        eps.append({
            "guid": f"{TITLE}/{stamp}",
            "title": today.strftime("%m-%d") + " " + "一二三四五六日"[today.weekday()] + " · 每日简报",
            "link": "",
            "published": today.astimezone(),
            "file": fname,
            "bytes": out.stat().st_size,
            "duration": dur,
            "notes": script[:8000],
            "transcript": stamp,
        })
        # 只保留最近 KEEP 期，旧 mp3 一并删除（手机端已下载的不受影响）
        eps.sort(key=lambda e: e["published"], reverse=True)
        for old in eps[KEEP:]:
            (eps_dir / old["file"]).unlink(missing_ok=True)
        eps = eps[:KEEP]

        url = (args.site_url or "").rstrip("/")
        if not url and URL_TEMPLATE:
            url = apply_template(URL_TEMPLATE, args.repo, args.branch)
            log(f"按模板生成音频地址：{url}")
        if not url:
            log("警告：未设置 --site-url，feed 里的音频地址会拼成相对路径，部分客户端不接受。")
        (root / "feed.xml").write_text(build_feed(url, eps, ensure_cover(root)), encoding="utf-8")
        if args.raw:
            shutil.copy2(args.raw, root / "latest.md")
            log(f"原始清单已存为 {root / 'latest.md'}")
        if os.environ.get("N2P_SELFUPDATE", "1") == "1":
            unself(root)
        log(f"feed 已更新：{root / 'feed.xml'}（共 {len(eps)} 期）")

    if args.serve:
        import http.server
        import functools

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        log(f"服务中：http://0.0.0.0:{args.port}/feed.xml   （Ctrl+C 停止）")
        http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
