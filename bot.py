import asyncio
import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "no_warnings": True,
    "extract_flat": False,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass
class Song:
    title: str
    url: str
    requested_by: str


class MusicBot(commands.Cog):
    def __init__(self, bot_instance: commands.Bot) -> None:
        self.bot = bot_instance
        self.queue: list[Song] = []
        self.current_song: Optional[Song] = None
        self.playing = False
        self.volume = 0.5

    def is_spotify_url(self, query: str) -> bool:
        parsed = urlparse(query)
        return parsed.netloc in {"open.spotify.com", "spotify.com"}

    def is_youtube_url(self, query: str) -> bool:
        return "youtube.com" in query or "youtu.be" in query

    def is_soundcloud_url(self, query: str) -> bool:
        return "soundcloud.com" in query

    async def _extract_info(self, query: str) -> dict:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(YDL_OPTIONS)

        if self.is_spotify_url(query):
            resolved_query = await self._resolve_spotify_query(query)
            if resolved_query:
                query = resolved_query

        if self.is_youtube_url(query) or self.is_soundcloud_url(query):
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
        else:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch1:{query}", download=False))

        if not isinstance(info, dict):
            raise RuntimeError("Could not get track metadata")

        if info.get("entries"):
            return info["entries"][0]
        return info

    async def _resolve_spotify_query(self, query: str) -> Optional[str]:
        try:
            encoded_url = quote(query, safe="")
            request = Request(
                f"https://open.spotify.com/oembed?url={encoded_url}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urlopen(request, timeout=10) as response:
                data = json.load(response)

            title = data.get("title", "").strip()
            author = data.get("author_name", "").strip()
            if title:
                return f"{title} {author}".strip() if author else title
        except Exception:
            pass

        parsed = urlparse(query)
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            return f"{path_parts[-1]} spotify"
        return None

    async def _build_source(self, query: str) -> discord.AudioSource:
        info = await self._extract_info(query)
        stream_url = info.get("url")

        if not stream_url:
            for fmt in info.get("formats", []):
                if fmt.get("acodec") != "none" and fmt.get("url"):
                    stream_url = fmt["url"]
                    break

        if not stream_url:
            raise RuntimeError("No playable audio URL was found")

        return discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)

    async def _play_next(self, ctx: commands.Context) -> None:
        if not self.queue:
            self.playing = False
            self.current_song = None
            await ctx.send("The queue is empty.")
            return

        self.playing = True
        song = self.queue.pop(0)
        self.current_song = song

        try:
            source = await self._build_source(song.url)
            source = discord.PCMVolumeTransformer(source, volume=self.volume)
        except Exception as exc:
            await ctx.send(f"Unable to play {song.title}: {exc}")
            await self._play_next(ctx)
            return

        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return

        await ctx.send(f"Now playing: {song.title}")
        ctx.voice_client.play(
            source,
            after=lambda error: self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self._finish_song(ctx))),
        )

    async def _finish_song(self, ctx: commands.Context) -> None:
        self.current_song = None
        if self.queue:
            await self._play_next(ctx)

    @commands.command(name="join")
    async def join(self, ctx: commands.Context) -> None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel first.")
            return

        channel = ctx.author.voice.channel
        if ctx.voice_client and ctx.voice_client.is_connected():
            if ctx.voice_client.channel != channel:
                await ctx.voice_client.move_to(channel)
            await ctx.send(f"Already in {channel.name}.")
            return

        await channel.connect()
        await ctx.send(f"Joined {channel.name}.")

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel first.")
            return

        if not ctx.voice_client:
            await ctx.author.voice.channel.connect()

        if ctx.voice_client and ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)

        try:
            info = await self._extract_info(query)
            title = info.get("title", query)
        except Exception as exc:
            await ctx.send(f"I could not resolve that URL/query: {exc}")
            return

        song = Song(title=title, url=query, requested_by=str(ctx.author))
        self.queue.append(song)
        await ctx.send(f"Queued: {song.title}")

        if not self.playing:
            await self._play_next(ctx)

    @commands.command(name="queue")
    async def queue_command(self, ctx: commands.Context) -> None:
        if not self.queue:
            await ctx.send("The queue is empty.")
            return

        items = "\n".join(f"{index + 1}. {song.title}" for index, song in enumerate(self.queue))
        await ctx.send(f"Queue:\n{items}")

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("Skipped the current track.")
        else:
            await ctx.send("Nothing is playing right now.")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Playback paused.")
        else:
            await ctx.send("Nothing is playing right now.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Playback resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context) -> None:
        if ctx.voice_client:
            ctx.voice_client.stop()
        self.queue.clear()
        self.current_song = None
        self.playing = False
        await ctx.send("Stopped playback and cleared the queue.")

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_connected():
            await ctx.voice_client.disconnect()
            await ctx.send("Left the voice channel.")
        else:
            await ctx.send("I am not connected to a voice channel.")

    @commands.command(name="now")
    async def now(self, ctx: commands.Context) -> None:
        if self.current_song:
            await ctx.send(f"Now playing: {self.current_song.title}")
        else:
            await ctx.send("Nothing is playing right now.")


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in your environment or .env file")

    try:
        await bot.add_cog(MusicBot(bot))
        await bot.start(token)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Shutting down bot...")
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")
