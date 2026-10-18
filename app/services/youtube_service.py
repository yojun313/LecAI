# app/services/youtube_service.py
"""
YouTube 다운로드 (yt-dlp).
  - probe(urls): 링크별 제목·길이·썸네일·가능한 화질·자막 언어 조회 (재생목록·채널은 항목으로 펼침)
  - search(query, kind, sort, page): YouTube 검색 (영상/채널/재생목록)
  - list_collection(url, tabs): 채널(동영상·Shorts·라이브 탭) 또는 재생목록의 전체 영상 목록
  - youtube_download_task(job_id): 작업 문서에 저장된 옵션으로 여러 영상을 순서대로 받아
    static/results/<owner>/youtube_<job_id>/ 에 저장. 진행 상황은 작업 로그/진행률로 표시.
YouTube 의 JS 검증 처리를 위해 deno 를 사용한다 (PATH 또는 ~/.deno/bin/deno, .env YTDLP_DENO_PATH 로 지정 가능).
봇 확인이 걸리면 .env YTDLP_COOKIES 에 cookies.txt 경로를 지정한다.
"""

import io
import os
import base64
import re
import time
import shutil
import zipfile
import threading
import unicodedata
from urllib.parse import urlparse, parse_qs, urlunparse, quote_plus

import yt_dlp

from app.core.config import settings
from app.services.job_manager import JobManager
from app.db import history_col

VIDEO_FORMATS = ("mp4", "webm", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac")
SUB_ONLY = "subs"  # 자막만 받기
QUALITY_CHOICES = ("best", "2160", "1440", "1080", "720", "480", "360", "240", "144")
AUDIO_QUALITY_CHOICES = ("320", "256", "192", "128", "96")
MAX_URLS = 50  # 링크 입력창에서 한 번에 조회하는 링크 수
MAX_DOWNLOAD_URLS = 5000  # 다운로드 작업 하나에 담을 수 있는 영상 수 (채널 전체 받기)
CHANNEL_MAX = 5000  # 채널 탭 하나에서 불러오는 최대 영상 수
SEARCH_PAGE = 24
CHANNEL_TABS = ("videos", "shorts", "streams")
ARCHIVE_NAME = ".archive.txt"  # 이어받기용 (이미 받은 영상 ID 기록)
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


def parse_urls(text, limit: int = MAX_URLS) -> list:
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
    return out[:limit]


_CHANNEL_PATH = re.compile(
    r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/([^/?#]*))?"
)


def channel_base(url: str) -> str | None:
    """채널 링크(@handle, /channel/ID, /c/, /user/)면 탭을 뗀 채널 주소, 아니면 None. '@handle' 만 써도 된다."""
    url = (url or "").strip()
    if re.fullmatch(r"@[\w.\-]+", url):
        return "https://www.youtube.com/" + url
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urlparse(url)
    if not re.search(r"(^|\.)youtube\.com$", p.netloc.lower()):
        return None
    m = _CHANNEL_PATH.match(p.path)
    return f"https://www.youtube.com/{m.group(1)}" if m else None


def is_playlist_url(url: str) -> bool:
    p = urlparse(clean_url(url))
    return "youtube.com" in p.netloc.lower() and p.path.startswith("/playlist")


def _https(u):
    if not u:
        return None
    return "https:" + u if u.startswith("//") else u


def _video_thumb(vid):
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None


def _flat_entry(e: dict, **extra) -> dict | None:
    """extract_flat 항목 → 화면용 요약. 영상/채널/재생목록 구분."""
    if not e:
        return None
    ie = e.get("ie_key") or ""
    url = e.get("url") or ""
    eid = e.get("id")
    if ie == "YoutubeTab" or "/channel/" in url or "/@" in url:
        if "list=" in url or (eid or "").startswith(("PL", "OL", "UU", "RD")):
            kind = "playlist"
        else:
            kind = "channel"
    else:
        kind = "video"
    thumbs = e.get("thumbnails") or []
    if kind == "video":
        thumb = _video_thumb(eid)
        url = f"https://www.youtube.com/watch?v={eid}"
        if "/shorts/" in (e.get("url") or "") or extra.get("tab") == "shorts":
            extra.setdefault("short", True)
    else:
        thumb = _https(thumbs[-1]["url"]) if thumbs else None
    return {
        "kind": kind,
        "id": eid,
        "url": url,
        "title": e.get("title"),
        "uploader": e.get("channel") or e.get("uploader"),
        "channel_url": e.get("uploader_url") or e.get("channel_url"),
        "handle": e.get("uploader_id")
        if str(e.get("uploader_id") or "").startswith("@")
        else None,
        "duration": e.get("duration"),
        "view_count": e.get("view_count"),
        "followers": e.get("channel_follower_count"),
        "count": e.get("playlist_count"),
        "live": e.get("live_status") == "is_live",
        "thumbnail": thumb,
        "description": (e.get("description") or "")[:200],
        **extra,
    }


# 검색 필터: YouTube 결과 페이지의 sp 값 (protobuf → base64).
#   1: 정렬(0 관련도, 3 조회수) · 2: 필터 { 1: 업로드 시기(2 오늘 … 5 올해), 2: 종류(1 영상, 2 채널, 3 재생목록) }
# 최신순 정렬은 YouTube 가 더 이상 지원하지 않아 '업로드 시기' 필터로 대신한다.
_SEARCH_KIND = {"video": 1, "channel": 2, "playlist": 3}
_SEARCH_PERIOD = {"any": 0, "today": 2, "week": 3, "month": 4, "year": 5}


def _search_sp(kind: str, sort: str, period: str) -> str:
    inner = b""
    if kind == "video" and _SEARCH_PERIOD.get(period):
        inner += bytes([0x08, _SEARCH_PERIOD[period]])
    inner += bytes([0x10, _SEARCH_KIND[kind]])
    raw = b""
    if kind == "video" and sort == "views":
        raw += bytes([0x08, 0x03])
    raw += bytes([0x12, len(inner)]) + inner
    return quote_plus(base64.b64encode(raw).decode())


def search(
    query: str,
    kind: str = "video",
    sort: str = "relevance",
    page: int = 1,
    period: str = "any",
):
    query = (query or "").strip()[:200]
    if not query:
        return {"items": [], "has_more": False}
    kind = kind if kind in _SEARCH_KIND else "video"
    page = max(1, min(int(page or 1), 20))
    sp = _search_sp(kind, sort, period if period in _SEARCH_PERIOD else "any")
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={sp}"
    start, end = (page - 1) * SEARCH_PAGE + 1, page * SEARCH_PAGE
    with yt_dlp.YoutubeDL(
        {
            **_base_opts(),
            "skip_download": True,
            "extract_flat": True,
            "playlist_items": f"{start}-{end}",
        }
    ) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(_friendly_error(e))
    entries = [x for x in (info.get("entries") or []) if x]
    items = [i for i in (_flat_entry(x) for x in entries) if i]
    # 영상 검색에도 가끔 채널/재생목록이 섞인다 → 요청한 종류만
    items = [i for i in items if i["kind"] == kind]
    return {"items": items, "has_more": len(entries) >= SEARCH_PAGE, "page": page}


class _SilentLogger:
    """채널에 없는 탭(Shorts·라이브) 조회 오류가 서버 로그에 찍히지 않게. 오류는 예외로 따로 처리한다."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _pick_thumb(thumbs, ident):
    for t in thumbs or []:
        if t.get("id") == ident:
            return _https(t.get("url"))
    return None


def list_collection(url: str, tabs=("videos",)) -> dict:
    """채널 또는 재생목록의 영상 전체(flat). 채널은 고른 탭(동영상/Shorts/라이브)을 합친다."""
    base = channel_base(url)
    opts = {
        **_base_opts(),
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": CHANNEL_MAX,
        "logger": _SilentLogger(),
    }
    items, seen, tab_counts, meta, errors = [], set(), {}, None, []
    targets = []
    if base:
        tabs = [t for t in (tabs or []) if t in CHANNEL_TABS] or ["videos"]
        targets = [(t, f"{base}/{t}") for t in tabs]
    elif is_playlist_url(url):
        targets = [("playlist", clean_url(url))]
    else:
        raise ValueError(
            "채널 링크(@핸들, /channel/...) 또는 재생목록 링크를 넣어 주세요."
        )

    truncated = False
    with yt_dlp.YoutubeDL(opts) as ydl:
        for tab, target in targets:
            try:
                info = ydl.extract_info(target, download=False)
            except Exception as e:
                msg = _friendly_error(e)
                # 탭이 없는 채널(Shorts/라이브 없음)은 0개로 처리
                if "does not have a" in msg:
                    tab_counts[tab] = 0
                else:
                    errors.append(msg)
                continue
            if meta is None:
                meta = info
            entries = [x for x in (info.get("entries") or []) if x]
            if len(entries) >= CHANNEL_MAX:
                truncated = True
            n = 0
            for x in entries:
                it = _flat_entry(x, tab=tab)
                if not it or it["kind"] != "video" or it["id"] in seen:
                    continue
                seen.add(it["id"])
                if not it.get("uploader"):
                    it["uploader"] = info.get("channel") or info.get("uploader")
                items.append(it)
                n += 1
            tab_counts[tab] = n
    if meta is None:
        if not errors and tab_counts:
            names = {"videos": "동영상", "shorts": "Shorts", "streams": "라이브"}
            raise RuntimeError(
                "이 채널에는 "
                + "·".join(names.get(t, t) for t in tab_counts)
                + " 탭이 없습니다."
            )
        raise RuntimeError(errors[0] if errors else "목록을 불러오지 못했습니다.")

    thumbs = meta.get("thumbnails") or []
    if base:
        name = (
            meta.get("channel")
            or meta.get("uploader")
            or re.sub(r" - \w+$", "", meta.get("title") or "")
        )
        info = {
            "type": "channel",
            "url": meta.get("uploader_url") or base,
            "channel_url": meta.get("channel_url"),
            "title": name,
            "handle": meta.get("uploader_id"),
            "avatar": _pick_thumb(thumbs, "avatar_uncropped"),
            "banner": _pick_thumb(thumbs, "banner_uncropped"),
            "followers": meta.get("channel_follower_count"),
            "description": (meta.get("description") or "")[:400],
        }
    else:
        info = {
            "type": "playlist",
            "url": clean_url(url),
            "title": meta.get("title"),
            "handle": None,
            "uploader": meta.get("channel") or meta.get("uploader"),
            "channel_url": meta.get("uploader_url") or meta.get("channel_url"),
            "avatar": None,
            "banner": None,
            "followers": None,
            "description": (meta.get("description") or "")[:400],
        }
    return {
        "info": info,
        "items": items,
        "tabs": tab_counts,
        "truncated": truncated,
        "errors": errors,
    }


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
            if channel_base(url):
                try:
                    col = list_collection(url, ("videos",))
                except Exception as e:
                    results.append({"url": url, "error": str(e)})
                    continue
                for it in col["items"]:
                    results.append(
                        {
                            **it,
                            "heights": [],
                            "subtitles": [],
                            "auto_captions": [],
                            "playlist": col["info"]["title"],
                        }
                    )
                continue
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
                            "thumbnail": _video_thumb(vid),
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
    # 이어받기: 이미 받은 영상은 건너뛴다 (중지·실패 후 다시 시작할 때)
    opts["download_archive"] = os.path.join(out_dir, ARCHIVE_NAME)
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


def _cancel_requested(job_id: str) -> bool:
    d = history_col.find_one({"id": job_id}, {"_id": 0, "yt_cancel": 1})
    return bool(d and d.get("yt_cancel"))


def _set_live(job_id: str, progress: int, text: str):
    """초 단위 진행 상황은 로그에 쌓지 않고 한 필드에 덮어쓴다 (채널 전체처럼 긴 작업에서 로그가 커지지 않게)."""
    JobManager.update_fields(
        job_id, {"progress": max(0, min(progress, 100)), "yt_live": text}
    )


def youtube_download_task(job_id: str):
    job = JobManager.get_job(job_id)
    if not job:
        return
    owner = job["owner"]
    urls = job.get("yt_urls") or []
    o = normalize_options(job.get("yt_options") or {})
    out_dir = job_dir(owner, job_id)
    os.makedirs(out_dir, exist_ok=True)
    total = len(urls)

    with _job_slots:
        if _cancel_requested(job_id):
            _finish(job_id, owner, out_dir, 0, total, [], cancelled=True)
            return
        JobManager.start_processing(job_id)
        resumed = os.path.exists(os.path.join(out_dir, ARCHIVE_NAME))
        label = {"subs": "자막만"}.get(o["format"], o["format"].upper())
        JobManager.update_progress(
            job_id,
            0,
            100,
            ("이어받기" if resumed else "다운로드 시작")
            + f": {total}개 · {label}"
            + (
                f" · {o['quality']}p"
                if o["format"] in VIDEO_FORMATS and o["quality"] != "best"
                else ""
            ),
        )
        done, failed, cancelled = 0, [], False
        for i, url in enumerate(urls, 1):
            if _cancel_requested(job_id):
                cancelled = True
                break
            state = {"last": 0.0, "phase": ""}

            def hook(d, i=i, state=state):
                now = time.time()
                if d.get("status") == "downloading":
                    if now - state["last"] < 1.0:
                        return
                    state["last"] = now
                    if _cancel_requested(job_id):
                        raise yt_dlp.utils.DownloadCancelled("사용자가 중지했습니다.")
                    tb = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    frac = (d.get("downloaded_bytes") or 0) / tb if tb else 0
                    overall = int(((i - 1) + min(frac, 1) * 0.95) / total * 100)
                    name = os.path.basename(d.get("filename") or "")
                    speed = d.get("_speed_str", "").strip()
                    eta = d.get("_eta_str", "").strip()
                    _set_live(
                        job_id,
                        overall,
                        f"[{i}/{total}] {int(frac * 100)}% · {speed} · 남은 시간 {eta} · {name[:60]}",
                    )
                elif d.get("status") == "finished" and state["phase"] != "post":
                    state["phase"] = "post"
                    _set_live(
                        job_id,
                        int((i - 0.05) / total * 100),
                        f"[{i}/{total}] 받기 완료 · 변환/병합 중...",
                    )

            try:
                _set_live(
                    job_id, int((i - 1) / total * 100), f"[{i}/{total}] 준비 중..."
                )
                with yt_dlp.YoutubeDL(build_opts(o, out_dir, hook)) as ydl:
                    info = ydl.extract_info(url, download=True)
                if not info:
                    # 이어받기: 이미 받은 영상
                    done += 1
                    continue
                title = info.get("title") or url
                if not job.get("yt_thumbnail") and info.get("thumbnail"):
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
            except yt_dlp.utils.DownloadCancelled:
                cancelled = True
                break
            except Exception as e:
                failed.append(url)
                JobManager.update_progress(
                    job_id,
                    int(i / total * 100),
                    100,
                    f"[{i}/{total}] 실패: {_friendly_error(e)} ({url})",
                )
        _finish(job_id, owner, out_dir, done, total, failed, cancelled=cancelled)


def list_job_files(out_dir: str) -> list:
    if not os.path.isdir(out_dir):
        return []
    return sorted(
        n
        for n in os.listdir(out_dir)
        if not n.startswith(".") and os.path.isfile(os.path.join(out_dir, n))
    )


def _finish(job_id, owner, out_dir, done, total, failed, cancelled=False):
    # 임시/부산물 정리
    if os.path.isdir(out_dir):
        for n in os.listdir(out_dir):
            if n.endswith((".part", ".ytdl", ".webp", ".jpg", ".png")) or ".temp." in n:
                try:
                    os.remove(os.path.join(out_dir, n))
                except OSError:
                    pass
    files = list_job_files(out_dir)
    JobManager.update_fields(
        job_id,
        {
            "yt_files": files,
            "yt_failed": len(failed),
            "yt_done": done,
            "yt_cancelled": cancelled,
            "yt_cancel": False,
            "yt_live": "",
        },
    )
    if not files:
        JobManager.mark_failed(
            job_id,
            "사용자가 중지했습니다."
            if cancelled
            else "받은 파일이 없습니다. " + (f"실패 {len(failed)}개" if failed else ""),
        )
        return
    # 여러 파일 ZIP 은 요청할 때 바로 스트리밍한다 (채널 전체처럼 큰 작업도 디스크를 두 배로 쓰지 않음)
    if len(files) == 1:
        result_url = f"/api/youtube/file/{job_id}/{files[0]}"
    else:
        result_url = f"/api/youtube/zip/{job_id}"
    size = sum(os.path.getsize(os.path.join(out_dir, n)) for n in files)
    JobManager.update_progress(
        job_id,
        100,
        100,
        ("중지됨" if cancelled else "완료")
        + f": 성공 {done}/{total}"
        + (f" · 실패 {len(failed)}" if failed else "")
        + f" · 파일 {len(files)}개 · {size / 1048576:.1f} MB",
    )
    JobManager.mark_completed(job_id, result_url)


class _ZipSink(io.RawIOBase):
    """ZipFile 이 쓰는 바이트를 모아 두었다가 스트리밍으로 내보낸다 (seek 불가 → data descriptor 사용)."""

    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream_zip(entries):
    """entries: [(경로, zip 안 이름)] → ZIP 바이트 조각 제너레이터 (압축 없이 저장, ZIP64)."""
    sink = _ZipSink()
    used = set()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, name in entries:
            arc, (base, ext), k = name, os.path.splitext(name), 2
            while arc in used:
                arc = f"{base} ({k}){ext}"
                k += 1
            used.add(arc)
            zi = zipfile.ZipInfo.from_file(path, arc)
            zi.compress_type = zipfile.ZIP_STORED
            with open(path, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
                    if len(sink.buf) >= 1 << 20:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


def job_dir(owner: str, job_id: str) -> str:
    return os.path.join(settings.RESULT_DIR, owner, f"youtube_{job_id}")
