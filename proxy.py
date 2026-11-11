#!/usr/bin/env python3
"""Lightweight search and SponsorBlock API for a YouTube-embedded web player.

Video playback is served directly by YouTube's official embed player. This
server does not extract, proxy, remux, cache, or deliver video streams.
"""

from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests
import yt_dlp
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("clearplay")
app = Flask(__name__)
CORS(app)

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{20,30}$")
RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
YT_WEB_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}
YT_WEB_COOKIES = {"CONSENT": "YES+1"}
SPONSOR_CATEGORIES = [
    "sponsor", "selfpromo", "interaction", "intro", "outro", "preview",
    "music_offtopic", "filler",
]


@app.get("/")
@app.get("/watch")
def index():
    return send_from_directory(os.path.join(app.root_path, "static"), "index.html")


def _fetch_search_meta(query):
    """Best-effort extract relative publish time & short view text from YouTube search page."""
    meta = {}
    try:
        resp = requests.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers=YT_WEB_HEADERS,
            cookies=YT_WEB_COOKIES,
            timeout=(2.5, 4),
        )
        if resp.status_code != 200:
            return meta
        match = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text)
        if not match:
            return meta
        data = json.loads(match.group(1))
    except Exception:
        return meta

    stack = [data]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            vr = obj.get("videoRenderer")
            if isinstance(vr, dict):
                vid = vr.get("videoId")
                if vid and VIDEO_ID.fullmatch(vid):
                    pub = " ".join(((vr.get("publishedTimeText") or {}).get("simpleText") or "").split())
                    vw = " ".join(
                        (
                            (vr.get("shortViewCountText") or {}).get("simpleText")
                            or (vr.get("viewCountText") or {}).get("simpleText")
                            or ""
                        ).split()
                    )
                    if pub or vw:
                        meta[vid] = {"published_text": pub, "views_text": vw}
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    return meta


def _entry_published_iso(entry):
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    u = str(entry.get("upload_date") or "")
    if len(u) == 8 and u.isdigit():
        return f"{u[:4]}-{u[4:6]}-{u[6:8]}T00:00:00+00:00"
    return ""


@app.get("/api/search")
def search():
    query = request.args.get("q", "").strip()
    try:
        offset = max(0, min(int(request.args.get("offset", "0")), 500))
    except ValueError:
        return jsonify({"error": "Invalid result offset."}), 400
    if not query:
        return jsonify({"error": "Enter a search term."}), 400
    if len(query) > 200:
        return jsonify({"error": "Search term is too long."}), 400
    try:
        page_end = offset + 11
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": True,
            "socket_timeout": 8,
            "playliststart": 1,
            "playlistend": page_end,
        }

        def _yt_extract():
            with yt_dlp.YoutubeDL(options) as ydl:
                videos = ydl.extract_info(f"ytsearch{page_end}:{query}", download=False)
                playlists = {}
                try:
                    playlist_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D"
                    playlists = ydl.extract_info(playlist_url, download=False) or {}
                except Exception:
                    log.warning("Playlist search skipped for query: %s", query)
            return videos, playlists

        with ThreadPoolExecutor(max_workers=2) as pool:
            meta_fut = pool.submit(_fetch_search_meta, query)
            yt_fut = pool.submit(_yt_extract)
            videos, playlists = yt_fut.result()
            meta_map = meta_fut.result()

        video_items = _video_cards(videos.get("entries"), meta_map=meta_map)
        playlist_items = []
        for entry in playlists.get("entries") or []:
            playlist_id = entry.get("id") or entry.get("playlist_id")
            if not PLAYLIST_ID.fullmatch(playlist_id or ""):
                continue
            thumbs = entry.get("thumbnails") or []
            playlist_items.append({
                "id": playlist_id,
                "title": entry.get("title") or "YouTube playlist",
                "author": entry.get("uploader") or entry.get("channel") or "Playlist",
                "thumbnail": (thumbs[-1].get("url") if thumbs else None),
                "video_count": entry.get("playlist_count") or entry.get("view_count"),
                "kind": "playlist",
            })
        playlist_first = "playlist" in query.casefold()
        ordered = ([*playlist_items, *video_items] if playlist_first else [*video_items, *playlist_items])
        page = ordered[offset:offset + 10]
        return jsonify({"results": page, "has_more": len(ordered) > offset + 10})
    except Exception:
        log.exception("YouTube search failed")
        return jsonify({"error": "Search is temporarily unavailable. Try again shortly."}), 502


def _video_cards(entries, exclude="", meta_map=None):
    cards = []
    meta_map = meta_map or {}
    for entry in entries or []:
        video_id = entry.get("id")
        if not VIDEO_ID.fullmatch(video_id or "") or video_id == exclude:
            continue
        m = meta_map.get(video_id) or {}
        cards.append({
            "id": video_id,
            "title": entry.get("title") or "Untitled video",
            "author": entry.get("uploader") or entry.get("channel") or "YouTube creator",
            "channel_id": entry.get("channel_id") or "",
            "channel_url": entry.get("channel_url") or entry.get("uploader_url") or "",
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "views_text": m.get("views_text") or "",
            "published": _entry_published_iso(entry),
            "published_text": m.get("published_text") or "",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        })
    return cards


def _restore_ascending_numbered_playlist(items):
    """Fix playlists yt-dlp returns newest-first when every entry is clearly numbered backwards."""
    numbers = []
    for item in items:
        match = re.search(r"(?<!\d)(\d{1,3})\s*(?=[|:—-])", item["title"][:180])
        if not match:
            return items
        numbers.append(int(match.group(1)))
    if len(numbers) >= 3 and all(left > right for left, right in zip(numbers, numbers[1:])):
        return list(reversed(items))
    return items


def _ytsearch_enriched(query, limit=12, exclude="", timeout_sec=7):
    """Run ytsearch and HTML search-meta lookup concurrently and return enriched video cards."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "socket_timeout": timeout_sec,
        "retries": 1,
    }

    def _run_ytdlp():
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        meta_fut = pool.submit(_fetch_search_meta, query)
        yt_fut = pool.submit(_run_ytdlp)
        data = yt_fut.result()
        meta_map = meta_fut.result()
    return _video_cards(data.get("entries"), exclude, meta_map=meta_map)


@app.get("/api/related")
def related():
    query = request.args.get("q", "").strip()
    exclude = request.args.get("exclude", "")
    if not query or len(query) > 240 or not VIDEO_ID.fullmatch(exclude):
        return jsonify({"error": "Invalid related video request."}), 400
    try:
        return jsonify({"results": _ytsearch_enriched(query, limit=12, exclude=exclude, timeout_sec=8)[:10]})
    except Exception:
        log.exception("Related-video lookup failed")
        return jsonify({"error": "Related videos are temporarily unavailable."}), 502


@app.get("/api/playlist/<playlist_id>")
def playlist(playlist_id):
    if not PLAYLIST_ID.fullmatch(playlist_id or ""):
        return jsonify({"error": "Invalid YouTube playlist ID."}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist", "playlistend": 100, "socket_timeout": 10}) as ydl:
            data = ydl.extract_info(f"https://www.youtube.com/playlist?list={playlist_id}", download=False)
        items = _restore_ascending_numbered_playlist(_video_cards(data.get("entries")))
        return jsonify({"title": data.get("title") or "Playlist", "author": data.get("uploader") or data.get("channel") or "", "results": items})
    except Exception:
        log.exception("Playlist lookup failed")
        return jsonify({"error": "Playlist is temporarily unavailable."}), 502


@app.get("/api/sponsorblock/<video_id>")
def sponsorblock(video_id):
    if not VIDEO_ID.fullmatch(video_id or ""):
        return jsonify({"error": "Invalid YouTube video ID."}), 400
    prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
    try:
        response = requests.get(
            f"https://sponsor.ajay.app/api/skipSegments/{prefix}",
            params={"categories": json.dumps(SPONSOR_CATEGORIES), "actionType": "skip"},
            timeout=(3, 8),
        )
        if response.status_code == 404:
            return jsonify({"video_id": video_id, "segments": []})
        response.raise_for_status()
        result = []
        for video in response.json():
            if video.get("videoID") != video_id:
                continue
            for row in video.get("segments", []):
                bounds = row.get("segment") or []
                if len(bounds) != 2:
                    continue
                try:
                    start, end = float(bounds[0]), float(bounds[1])
                except (TypeError, ValueError):
                    continue
                category = row.get("category")
                if category in SPONSOR_CATEGORIES and start >= 0 and end > start:
                    result.append({"start": start, "end": end, "category": category})
        result.sort(key=lambda segment: segment["start"])
        return jsonify({"video_id": video_id, "segments": result})
    except (requests.RequestException, ValueError):
        log.exception("SponsorBlock lookup failed for %s", video_id)
        return jsonify({"error": "SponsorBlock is temporarily unavailable."}), 502


@app.get("/api/video/<video_id>")
def video_info(video_id):
    if not VIDEO_ID.fullmatch(video_id or ""):
        return jsonify({"error": "Invalid YouTube video ID."}), 400

    title = ""
    author = ""
    channel_id = ""
    channel_url = ""
    duration = None
    view_count = None
    published = ""

    def _fetch_oembed():
        oe = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=(3, 5),
        )
        return oe.json() if oe.status_code == 200 else {}

    def _fetch_watch_meta():
        wr = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers=YT_WEB_HEADERS,
            cookies=YT_WEB_COOKIES,
            timeout=(3, 5),
        )
        return wr.text if wr.status_code == 200 else ""

    with ThreadPoolExecutor(max_workers=2) as pool:
        oe_fut = pool.submit(_fetch_oembed)
        wr_fut = pool.submit(_fetch_watch_meta)
        try:
            oe_data = oe_fut.result() or {}
            title = oe_data.get("title") or ""
            author = oe_data.get("author_name") or ""
            channel_url = oe_data.get("author_url") or ""
        except Exception:
            log.warning("oEmbed lookup failed for %s", video_id)
        try:
            html = wr_fut.result() or ""
            if html:
                m_ch = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{20,30})"', html)
                if m_ch:
                    channel_id = m_ch.group(1)
                m_vw = re.search(r'"viewCount":"(\d+)"', html)
                if m_vw:
                    view_count = int(m_vw.group(1))
                m_pub = re.search(r'"(?:publishDate|uploadDate)":"([^"]+)"', html)
                if m_pub:
                    published = m_pub.group(1)
        except Exception:
            log.warning("Watch HTML meta lookup failed for %s", video_id)

    if not channel_id and channel_url:
        try:
            ch_resp = requests.get(channel_url, headers=YT_WEB_HEADERS, timeout=(3, 5))
            if ch_resp.status_code == 200:
                match = re.search(
                    r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{20,30})"',
                    ch_resp.text,
                )
                if match:
                    channel_id = match.group(1)
        except Exception:
            pass

    if not channel_id:
        try:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "noplaylist": True,
                "socket_timeout": 8,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            title = title or data.get("title") or ""
            author = author or data.get("uploader") or data.get("channel") or ""
            channel_id = data.get("channel_id") or ""
            channel_url = channel_url or data.get("channel_url") or data.get("uploader_url") or ""
            duration = data.get("duration")
            view_count = view_count if view_count is not None else data.get("view_count")
            published = published or _entry_published_iso(data)
        except Exception:
            log.exception("Video metadata lookup failed for %s", video_id)

    if not title and not channel_id:
        return jsonify({"error": "Video metadata is temporarily unavailable."}), 502

    return jsonify({
        "id": video_id,
        "title": title or "YouTube video",
        "author": author or "YouTube creator",
        "channel_id": channel_id,
        "channel_url": channel_url,
        "duration": duration,
        "view_count": view_count,
        "published": published,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    })


def _fetch_channel_rss(channel_id):
    if not CHANNEL_ID.fullmatch(channel_id or ""):
        return []
    try:
        resp = requests.get(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            timeout=(3, 6),
        )
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
        channel_title = root.findtext("atom:title", "", RSS_NS) or "YouTube channel"
        items = []
        for entry in root.findall("atom:entry", RSS_NS)[:15]:
            vid = entry.findtext("yt:videoId", "", RSS_NS)
            if not VIDEO_ID.fullmatch(vid or ""):
                continue
            author = entry.findtext("atom:author/atom:name", "", RSS_NS) or channel_title
            stats = entry.find("media:group/media:community/media:statistics", RSS_NS)
            view_count = None
            if stats is not None:
                try:
                    view_count = int(stats.attrib.get("views", ""))
                except (TypeError, ValueError):
                    view_count = None
            items.append({
                "id": vid,
                "title": entry.findtext("atom:title", "", RSS_NS) or "Untitled video",
                "author": author,
                "channel_id": channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "published": entry.findtext("atom:published", "", RSS_NS) or "",
                "view_count": view_count,
                "duration": None,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            })
        return items
    except Exception:
        log.warning("RSS feed fetch failed for channel %s", channel_id)
        return []


@app.get("/api/channel/<channel_id>/videos")
def channel_videos(channel_id):
    if not CHANNEL_ID.fullmatch(channel_id or ""):
        return jsonify({"error": "Invalid YouTube channel ID."}), 400
    items = _fetch_channel_rss(channel_id)
    return jsonify({"channel_id": channel_id, "results": items})


@app.get("/api/subscriptions/feed")
def subscriptions_feed():
    raw = request.args.get("channels", "")
    channel_ids = []
    seen = set()
    for part in raw.split(","):
        cid = part.strip()
        if cid and cid not in seen and CHANNEL_ID.fullmatch(cid):
            seen.add(cid)
            channel_ids.append(cid)
        if len(channel_ids) >= 25:
            break
    if not channel_ids:
        return jsonify({"results": []})
    merged = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for batch in pool.map(_fetch_channel_rss, channel_ids):
            merged.extend(batch)
    merged.sort(key=lambda item: item.get("published") or "", reverse=True)
    return jsonify({"results": merged[:45]})


_NOISE_RE = re.compile(
    r"\b(?:official|video|music|audio|lyrics|lyric|4k|hd|1080p|uhd|full|version|live|shorts|feat|ft|for|the|and|with|from)\b",
    re.IGNORECASE,
)


def _build_rec_queries(history_items, max_queries=3):
    """Build diverse short search queries from recent watch history items."""
    queries = []
    seen_q = set()
    seen_authors = set()

    # Pass 1: distinct authors/creators first; Pass 2: fallback to remaining items
    ordered = []
    deferred = []
    for item in history_items[:8]:
        if not isinstance(item, dict):
            continue
        author = (item.get("author") or "").strip().casefold()
        if author and author != "youtube creator" and author not in seen_authors:
            seen_authors.add(author)
            ordered.append(item)
        else:
            deferred.append(item)

    for item in [*ordered, *deferred]:
        title = (item.get("title") or "").strip()
        author = (item.get("author") or "").strip()
        if not title or title == "YouTube video":
            continue
        cleaned = re.split(r"[|—/]", re.sub(r"[\(\[].*?[\)\]]", " ", title))[0]
        cleaned = _NOISE_RE.sub(" ", cleaned)
        words = [w for w in re.findall(r"[\w'-]+", cleaned) if len(w) > 1][:4]
        if not words:
            words = re.findall(r"[\w'-]+", title)[:4]
        base = " ".join(words).strip()
        if author and author.casefold() != "youtube creator" and author.casefold() not in base.casefold():
            q = f"{author} {base}".strip()
        else:
            q = base
        q_key = q.casefold()
        if q and q_key not in seen_q:
            seen_q.add(q_key)
            queries.append(q[:100])
        if len(queries) >= max_queries:
            break
    return queries


def _fetch_ytsearch_cards(query, limit=8):
    try:
        return _ytsearch_enriched(query, limit=limit, timeout_sec=5)
    except Exception:
        log.warning("Home recommendation search failed for query: %s", query)
        return []


@app.post("/api/home/feed")
def home_feed():
    """Return personalized Home feed: new subscription uploads + history-based suggestions."""
    payload = request.get_json(silent=True) or {}

    sub_channels = []
    seen_ch = set()
    for cid in payload.get("channels") or []:
        if isinstance(cid, str):
            cid = cid.strip()
            if cid and cid not in seen_ch and CHANNEL_ID.fullmatch(cid):
                seen_ch.add(cid)
                sub_channels.append(cid)
        if len(sub_channels) >= 25:
            break

    history_items = [x for x in (payload.get("history") or []) if isinstance(x, dict)][:8]
    exclude_ids = {
        x for x in (payload.get("exclude_ids") or [])
        if isinstance(x, str) and VIDEO_ID.fullmatch(x)
    }
    for h in history_items:
        hid = h.get("id")
        if isinstance(hid, str) and VIDEO_ID.fullmatch(hid):
            exclude_ids.add(hid)

    rec_queries = _build_rec_queries(history_items, max_queries=2)

    hist_channels = []
    for h in history_items:
        cid = (h.get("channel_id") or "").strip()
        if cid and CHANNEL_ID.fullmatch(cid) and cid not in seen_ch and cid not in hist_channels:
            hist_channels.append(cid)
        if len(hist_channels) >= 3:
            break

    sub_videos = []
    rec_buckets = []

    pool = ThreadPoolExecutor(max_workers=8)
    try:
        sub_futures = [pool.submit(_fetch_channel_rss, cid) for cid in sub_channels]
        hist_ch_futures = [pool.submit(_fetch_channel_rss, cid) for cid in hist_channels]
        search_futures = [pool.submit(_fetch_ytsearch_cards, q, 10) for q in rec_queries]
        all_futures = [*sub_futures, *hist_ch_futures, *search_futures]
        if all_futures:
            wait_futures(all_futures, timeout=12)

        for fut in sub_futures:
            if fut.done():
                sub_videos.extend(fut.result())

        for fut, cap in [*((f, 4) for f in hist_ch_futures), *((f, None) for f in search_futures)]:
            if not fut.done():
                continue
            items = [v for v in fut.result() if v["id"] not in exclude_ids]
            if cap:
                items = items[:cap]
            if items:
                rec_buckets.append(items)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    sub_videos.sort(key=lambda item: item.get("published") or "", reverse=True)
    seen_sub = set()
    unique_subs = []
    for item in sub_videos:
        if item["id"] not in seen_sub:
            seen_sub.add(item["id"])
            unique_subs.append(item)

    sub_top_ids = {s["id"] for s in unique_subs[:12]}

    # Interleave recommendation buckets for creator/topic diversity
    recommended = []
    seen_rec = set()
    max_bucket = max((len(b) for b in rec_buckets), default=0)
    for idx in range(max_bucket):
        for bucket in rec_buckets:
            if idx >= len(bucket):
                continue
            cand = bucket[idx]
            vid = cand["id"]
            if vid in seen_rec or vid in exclude_ids or vid in sub_top_ids:
                continue
            seen_rec.add(vid)
            recommended.append(cand)
            if len(recommended) >= 16:
                break
        if len(recommended) >= 16:
            break

    return jsonify({
        "subscriptions": unique_subs[:12],
        "recommended": recommended[:16],
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "video_stream_proxy": False})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
