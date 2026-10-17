# app/services/youtube_service.py
"""
YouTube 다운로드 (yt-dlp).
  - probe(urls): 링크별 제목·길이·썸네일·가능한 화질·자막 언어 조회 (재생목록은 항목으로 펼침)
  - youtube_download_task(job_id): 작업 문서에 저장된 옵션으로 여러 영상을 순서대로 받아
    static/results/<owner>/youtube_<job_id>/ 에 저장. 진행 상황은 작업 로그/진행률로 표시.
YouTube 의 JS 검증 처리를 위해 deno 를 사용한다 (PATH 또는 ~/.deno/bin/deno, .env YTDLP_DENO_PATH 로 지정 가능).
봇 확인이 걸리면 .env YTDLP_COOKIES 에 cookies.txt 경로를 지정한다.
"""

import os
import re
import time
import shutil
import zipfile
import threading
import unicodedata
from urllib.parse import urlparse, parse_qs, urlunparse

import yt_dlp

from app.core.config import settings
from app.services.job_manager import JobManager

VIDEO_FORMATS = ("mp4", "webm", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac")
SUB_ONLY = "subs"  # 자막만 받기
QUALITY_CHOICES = ("best", "2160", "1440", "1080", "720", "480", "360", "240", "144")
AUDIO_QUALITY_CHOICES = ("320", "256", "192", "128", "96")
MAX_URLS = 50
MAX_PARALLEL_JOBS = 2

_job_slots = threading.Semaphore(MAX_PARALLEL_JOBS)


def _deno_path():
    p = (
        os.getenv("YTDLP_DENO_PATH")
        or shutil.which("deno")
        or os.path.expanduser("~/.deno/bin/deno")
    )
    return p if p and os.path.exists(p) else None


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 5,
    }
    deno = _deno_path()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    cookies = os.getenv("YTDLP_COOKIES")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def clean_url(url: str) -> str:
    """추적 파라미터 제거. 재생목록 링크(list=)는 유지."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower()
    if "youtube.com" in host:
        q = parse_qs(p.query)
        keep = []
        if q.get("v"):
            keep.append(f"v={q['v'][0]}")
        if q.get("list") and (not q.get("v") or p.path.startswith("/playlist")):
            keep.append(f"list={q['list'][0]}")
        if keep:
            return urlunparse(p._replace(query="&".join(keep), fragment=""))
        return urlunparse(p._replace(fragment=""))
    if "youtu.be" in host:
        return urlunparse(p._replace(query="", fragment=""))
    return url


def parse_urls(text) -> list:
    if isinstance(text, (list, tuple)):
        raw = text
    else:
        raw = re.split(r"[\s,]+", text or "")
    out, seen = [], set()
    for r in raw:
        u = clean_url(r)
        if u and u not in seen and re.search(r"(youtube\.com|youtu\.be)", u):
            seen.add(u)
            out.append(u)
    return out[:MAX_URLS]


def _summarize(info: dict) -> dict:
    formats = info.get("formats") or []
    heights = sorted(
        {
            f.get("height")
            for f in formats
            if f.get("height") and f.get("vcodec") not in (None, "none")
        },
        reverse=True,
    )
    subs = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "heights": heights,
        "subtitles": [s for s in subs if s != "live_chat"],
        "auto_captions": auto,
        "language": info.get("language"),
    }


def probe(urls: list) -> list:
    """링크별 정보. 재생목록이면 항목을 펼친다(화질·자막은 첫 항목 기준 요약 없이 기본 목록)."""
    results = []
    with yt_dlp.YoutubeDL(
        {**_base_opts(), "skip_download": True, "extract_flat": "in_playlist"}
    ) as ydl:
        for url in urls:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                results.append({"url": url, "error": _friendly_error(e)})
                continue
            if info.get("_type") == "playlist":
                for entry in info.get("entries") or []:
                    if not entry:
                        continue
                    vid = entry.get("id")
                    results.append(
                        {
                            "id": vid,
                            "url": entry.get("url")
                            if (entry.get("url") or "").startswith("http")
                            else f"https://www.youtube.com/watch?v={vid}",
                            "title": entry.get("title"),
                            "uploader": entry.get("uploader") or entry.get("channel"),
                            "duration": entry.get("duration"),
                            "thumbnail": (entry.get("thumbnails") or [{}])[-1].get(
                                "url"
                            )
                            if entry.get("thumbnails")
                            else None,
                            "heights": [],
                            "subtitles": [],
                            "auto_captions": [],
                            "playlist": info.get("title"),
                        }
                    )
            else:
                results.append(_summarize(info))
    return results


def _friendly_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return "YouTube 가 봇 확인을 요구합니다. 서버 .env 의 YTDLP_COOKIES 에 cookies.txt 를 지정해 주세요."
    if "Private video" in msg:
        return "비공개 영상입니다."
    if "Video unavailable" in msg:
        return "볼 수 없는 영상입니다 (삭제·지역 제한 등)."
    if "age" in msg.lower() and "confirm" in msg.lower():
        return "연령 확인이 필요한 영상입니다. YTDLP_COOKIES 설정이 필요합니다."
    return msg.replace("ERROR: ", "")[:300]


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "video")
    name = re.sub(r"[\U00010000-\U0010ffff]", "", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return re.sub(r"\s+", " ", name).strip()[:150] or "video"


def normalize_options(o: dict) -> dict:
    fmt = (o.get("format") or "mp4").lower()
    if fmt not in VIDEO_FORMATS + AUDIO_FORMATS + (SUB_ONLY,):
        fmt = "mp4"
    quality = str(o.get("quality") or "best")
    if quality not in QUALITY_CHOICES:
        quality = "best"
    aq = str(o.get("audio_quality") or "192")
    if aq not in AUDIO_QUALITY_CHOICES:
        aq = "192"
    langs = [
        re.sub(r"[^\w\-]", "", str(lang))[:20]
        for lang in (o.get("sub_langs") or [])
        if str(lang).strip()
    ][:20]
    sub_fmt = (o.get("sub_format") or "srt").lower()
    if sub_fmt not in ("srt", "vtt"):
        sub_fmt = "srt"
    want_subs = bool(o.get("subtitles")) or fmt == SUB_ONLY
    return {
        "format": fmt,
        "quality": quality,
        "audio_quality": aq,
        "subtitles": want_subs,
        "sub_langs": langs or (["ko", "en"] if want_subs else []),
        "auto_subs": bool(o.get("auto_subs", True)),
        "sub_format": sub_fmt,
        "embed_subs": bool(o.get("embed_subs")) and fmt in ("mp4", "mkv", "webm"),
        "embed_thumbnail": fmt in ("mp3", "m4a", "opus", "flac")
        or (bool(o.get("embed_thumbnail", True)) and fmt in ("mp4", "mkv")),
        "add_metadata": bool(o.get("add_metadata", True)),
    }


def _format_selector(o: dict) -> dict:
    fmt, q = o["format"], o["quality"]
    if fmt == SUB_ONLY:
        return {"skip_download": True}
    if fmt in AUDIO_FORMATS:
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o["audio_quality"]
        return {"format": "bestaudio/best", "postprocessors": [pp]}
    h = "" if q == "best" else f"[height<={q}]"
    if fmt == "mp4":
        # mp4 호환(H.264/AAC 우선) → 없으면 아무 코덱 → 단일 파일
        # 해상도 우선. 같은 해상도면 format_sort 로 H.264/AAC 를 고른다 (YouTube 는 H.264 를 1080p 까지만 제공 → 그 이상은 VP9/AV1)
        sel = f"bv*{h}+ba/b{h}/b"
    elif fmt == "webm":
        sel = f"bv*{h}[ext=webm]+ba[ext=webm]/bv*{h}+ba/b{h}/b"
    else:
        sel = f"bv*{h}+ba/b{h}/b"
    # 같은 조건이면 해상도 → 프레임레이트 순으로, mp4 는 H.264 를 우선
    sort = ["res", "fps"] + (["vcodec:h264", "acodec:aac"] if fmt == "mp4" else [])
    return {"format": sel, "merge_output_format": fmt, "format_sort": sort}


def build_opts(o: dict, out_dir: str, hook) -> dict:
    opts = {**_base_opts(), **_format_selector(o)}
    opts["outtmpl"] = {
        "default": os.path.join(out_dir, "%(title).150B [%(id)s].%(ext)s")
    }
    opts["progress_hooks"] = [hook]
    opts["windowsfilenames"] = True
    pps = list(opts.get("postprocessors", []))
    if o["subtitles"]:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = o["auto_subs"]
        opts["subtitleslangs"] = o["sub_langs"]
        opts["subtitlesformat"] = (
            f"{o['sub_format']}/vtt/best" if o["sub_format"] == "srt" else "vtt/best"
        )
        if o["sub_format"] == "srt":
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
        if o["embed_subs"]:
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
    if o["add_metadata"] and o["format"] != SUB_ONLY:
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    if o["embed_thumbnail"]:
        # 앨범 커버/썸네일: 가장 큰 썸네일을 jpg 로 바꿔 파일에 넣는다 (mp3 는 ID3 APIC '앞표지')
        opts["writethumbnail"] = True
        pps.append(
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        )
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if pps:
        opts["postprocessors"] = pps
    return opts


def youtube_download_task(job_id: str):
    job = JobManager.get_job(job_id)
    if not job:
        return
    owner = job["owner"]
    urls = job.get("yt_urls") or []
    o = normalize_options(job.get("yt_options") or {})
    out_dir = os.path.join(settings.RESULT_DIR, owner, f"youtube_{job_id}")
    os.makedirs(out_dir, exist_ok=True)
    total = len(urls)

    with _job_slots:
        JobManager.start_processing(job_id)
        label = {"subs": "자막만"}.get(o["format"], o["format"].upper())
        JobManager.update_progress(
            job_id,
            0,
            100,
            f"다운로드 시작: {total}개 · {label}"
            + (
                f" · {o['quality']}p"
                if o["format"] in VIDEO_FORMATS and o["quality"] != "best"
                else ""
            ),
        )
        done, failed = 0, []
        for i, url in enumerate(urls, 1):
            state = {"last": 0.0, "file": None, "phase": ""}

            def hook(d, i=i, state=state):
                now = time.time()
                if d.get("status") == "downloading":
                    tb = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    frac = (d.get("downloaded_bytes") or 0) / tb if tb else 0
                    overall = int(((i - 1) + min(frac, 1) * 0.95) / total * 100)
                    if now - state["last"] > 1.0:
                        state["last"] = now
                        name = os.path.basename(d.get("filename") or "")
                        speed = d.get("_speed_str", "").strip()
                        eta = d.get("_eta_str", "").strip()
                        JobManager.update_progress(
                            job_id,
                            overall,
                            100,
                            f"[{i}/{total}] {int(frac * 100)}% · {speed} · 남은 시간 {eta} · {name[:60]}",
                        )
                elif d.get("status") == "finished" and state["phase"] != "post":
                    state["phase"] = "post"
                    JobManager.update_progress(
                        job_id,
                        int((i - 0.05) / total * 100),
                        100,
                        f"[{i}/{total}] 받기 완료 · 변환/병합 중...",
                    )

            try:
                with yt_dlp.YoutubeDL(build_opts(o, out_dir, hook)) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = (info or {}).get("title") or url
                    if not job.get("yt_thumbnail") and (info or {}).get("thumbnail"):
                        job["yt_thumbnail"] = info["thumbnail"]
                        JobManager.update_fields(
                            job_id, {"yt_thumbnail": info["thumbnail"]}
                        )
                done += 1
                JobManager.update_progress(
                    job_id,
                    int(i / total * 100),
                    100,
                    f"[{i}/{total}] 완료: {title[:80]}",
                )
            except Exception as e:
                failed.append(url)
                JobManager.update_progress(
                    job_id,
                    int(i / total * 100),
                    100,
                    f"[{i}/{total}] 실패: {_friendly_error(e)} ({url})",
                )

        # 임시/부산물 정리
        for n in os.listdir(out_dir):
            if n.endswith((".part", ".ytdl", ".webp", ".jpg", ".png")) or ".temp." in n:
                try:
                    os.remove(os.path.join(out_dir, n))
                except OSError:
                    pass
        files = sorted(
            n for n in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, n))
        )
        if not files:
            JobManager.mark_failed(
                job_id,
                "받은 파일이 없습니다. " + (f"실패 {len(failed)}개" if failed else ""),
            )
            shutil.rmtree(out_dir, ignore_errors=True)
            return
        JobManager.update_fields(job_id, {"yt_files": files})
        if len(files) == 1:
            result_url = f"/api/youtube/file/{job_id}/{files[0]}"
        else:
            zip_path = os.path.join(settings.RESULT_DIR, owner, f"youtube_{job_id}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for n in files:
                    zf.write(os.path.join(out_dir, n), n)
            result_url = f"/api/youtube/zip/{job_id}"
        size = sum(os.path.getsize(os.path.join(out_dir, n)) for n in files)
        JobManager.update_progress(
            job_id,
            100,
            100,
            f"완료: 성공 {done}/{total}"
            + (f" · 실패 {len(failed)}" if failed else "")
            + f" · 파일 {len(files)}개 · {size / 1048576:.1f} MB",
        )
        JobManager.mark_completed(job_id, result_url)


def job_dir(owner: str, job_id: str) -> str:
    return os.path.join(settings.RESULT_DIR, owner, f"youtube_{job_id}")
