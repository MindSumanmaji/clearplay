#!/usr/bin/env python3
"""Lightweight search and SponsorBlock API for a YouTube-embedded web player.

Video playback is served directly by YouTube's official embed player. This
server does not extract, proxy, remux, cache, or deliver video streams.
"""

from concurrent.futures import ThreadPoolExecutor
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
RSS_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
SPONSOR_CATEGORIES = [
    "sponsor", "selfpromo", "interaction", "intro", "outro", "preview",
    "music_offtopic", "filler",
]


@app.get("/")
@app.get("/watch")
def index():
    return send_from_directory(os.path.join(app.root_path, "static"), "index.html")


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
        with yt_dlp.YoutubeDL(options) as ydl:
            videos = ydl.extract_info(f"ytsearch{page_end}:{query}", download=False)
            playlists = {}
            try:
                playlist_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D"
                playlists = ydl.extract_info(playlist_url, download=False) or {}
            except Exception:
                log.warning("Playlist search skipped for query: %s", query)
        video_items = _video_cards(videos.get("entries"))
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


def _video_cards(entries, exclude=""):
    cards = []
    for entry in entries or []:
        video_id = entry.get("id")
        if not VIDEO_ID.fullmatch(video_id or "") or video_id == exclude:
            continue
        cards.append({
            "id": video_id,
            "title": entry.get("title") or "Untitled video",
            "author": entry.get("uploader") or entry.get("channel") or "YouTube creator",
            "channel_id": entry.get("channel_id") or "",
            "channel_url": entry.get("channel_url") or entry.get("uploader_url") or "",
            "duration": entry.get("duration"),
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


@app.get("/api/related")
def related():
    query = request.args.get("q", "").strip()
    exclude = request.args.get("exclude", "")
    if not query or len(query) > 240 or not VIDEO_ID.fullmatch(exclude):
        return jsonify({"error": "Invalid related video request."}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist", "noplaylist": True, "socket_timeout": 8}) as ydl:
            data = ydl.extract_info(f"ytsearch12:{query}", download=False)
        return jsonify({"results": _video_cards(data.get("entries"), exclude)[:10]})
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

    try:
        oe = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=(3, 5),
        )
        if oe.status_code == 200:
            oe_data = oe.json()
            title = oe_data.get("title") or ""
            author = oe_data.get("author_name") or ""
            channel_url = oe_data.get("author_url") or ""
            if channel_url:
                ch_resp = requests.get(channel_url, timeout=(3, 5))
                if ch_resp.status_code == 200:
                    match = re.search(
                        r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{20,30})"',
                        ch_resp.text,
                    )
                    if match:
                        channel_id = match.group(1)
    except Exception:
        log.warning("oEmbed fallback lookup failed for %s", video_id)

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
            items.append({
                "id": vid,
                "title": entry.findtext("atom:title", "", RSS_NS) or "Untitled video",
                "author": author,
                "channel_id": channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "published": entry.findtext("atom:published", "", RSS_NS) or "",
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


@app.get("/health")
def health():
    return jsonify({"ok": True, "video_stream_proxy": False})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
