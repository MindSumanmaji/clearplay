# Clearplay Web Prototype

A lightweight YouTube search page with the official YouTube embedded player. Search metadata and SponsorBlock lookups use a small Flask server. Video playback is delivered by YouTube directly to the viewer's browser; this application does not download, proxy, remux, cache, or serve video bytes.

YouTube controls playback eligibility and may show ads in embedded videos. This project does not promise ad-free playback. SponsorBlock handles community-submitted in-video segments only.

## Run locally

Requirements: Python 3.10 or later.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python proxy.py
```

Open http://localhost:8080. Enter a search term or paste a YouTube URL/video ID. The server listens on `127.0.0.1:8080` by default; `HOST` and `PORT` can override it.

## Features

- Search YouTube videos and playlists together, with 10 results per page and a View more button. Queries containing “playlist” put playlist matches first.
- Play videos with YouTube's official IFrame Player API.
- Seek backward or forward 10 seconds and change playback speed where the YouTube player supports it.
- Show title/creator-based related picks for a single video, or ordered playlist entries with automatic next-video playback when a playlist is opened.
- Fetch SponsorBlock segments server-side, draw timeline markers, and choose Skip, Ask, Show, or Off by category. Preferences stay in the current browser's local storage.
- Serve `/health` for a small server status check.

## Limits

- The server still handles search metadata and SponsorBlock lookups, but it does not carry video traffic. Search depends on yt-dlp and may need updates when YouTube changes its pages.
- YouTube's embedded player may show advertisements or reject a video for account, region, age, or embedding restrictions. YouTube owns the player and playback behavior.
- SponsorBlock categories and segment timestamps are community data and may be missing or inaccurate.
- Google sign-in, YouTube account linking, account activity history, direct stream extraction, FFmpeg muxing, and the video/thumbnail proxy have been removed from this web prototype.

The Android app remains separately in `Skipii/` and was not modified.
