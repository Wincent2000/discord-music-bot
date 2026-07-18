# Discord Music Bot

A simple Python Discord music bot that can queue tracks from Spotify links, YouTube links, and SoundCloud links.

## Features
- Join a voice channel
- Play tracks from YouTube, Spotify, or SoundCloud URLs
- Queue multiple songs
- Pause, resume, skip, stop, and leave

## Requirements
- Python 3.10+
- FFmpeg installed and available on your PATH
- A Discord bot token
- No Spotify credentials are required for Spotify link resolution

### Install FFmpeg on Linux
If you are using this container or Ubuntu/Debian, run:
```bash
sudo apt update && sudo apt install -y ffmpeg
```

## Setup
1. Create and activate a virtual environment:
   - python -m venv .venv
   - source .venv/bin/activate
2. Install dependencies:
   - pip install -r requirements.txt
3. Copy the environment example:
   - cp .env.example .env
4. Edit .env and set your Discord token.

## Run
- python bot.py

## Commands
- !join
- !play <url or search query>
- !queue
- !skip
- !pause
- !resume
- !stop
- !leave
- !now

## Notes
- Spotify support uses a public Spotify metadata lookup, so no Spotify API credentials are required. Playback still relies on YouTube/yt-dlp resolution.
- For best results, keep FFmpeg installed and ensure your bot has permission to connect to voice channels.
