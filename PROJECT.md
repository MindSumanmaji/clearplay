# Clearplay — Lightweight YouTube Embed Prototype

## Goal

Provide YouTube search with an official embedded player and optional SponsorBlock segment controls. Do not claim ad-free playback: ads and embed restrictions are controlled by YouTube.

## Architecture

```text
Browser ── search metadata ──> Flask + yt-dlp ──> YouTube
   ├──── SponsorBlock lookup ─> Flask ──────────> SponsorBlock API
   └──── official embedded player ──────────────> YouTube
```

The Flask server does not extract or proxy video streams, run FFmpeg, or serve video bytes. Google sign-in, YouTube account linking, and account activity history are not part of this prototype. Android code remains isolated in `Skipii/`.

## Files

| File | Purpose |
|---|---|
| `proxy.py` | Flask search and SponsorBlock lookup endpoints; `/health`. |
| `static/index.html` | Search UI, embedded player, seeking, speed, and SponsorBlock controls. |
| `requirements.txt` | Flask, requests, and yt-dlp dependencies. |
| `README.md` | Setup and limitations. |

## Run

Install requirements and run `python proxy.py`, then open `http://localhost:8080`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web application |
| GET | `/api/search?q=...` | YouTube search metadata |
| GET | `/api/sponsorblock/<id>` | Public SponsorBlock segments |
| GET | `/health` | Server status, including `video_stream_proxy: false` |
