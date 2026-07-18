import asyncio
import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse, parse_qs
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


@bot.event
async def on_connect() -> None:
    print("Connected to Discord gateway.")


@bot.event
async def on_disconnect() -> None:
    print("Disconnected from Discord gateway.")


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Connected to {len(bot.guilds)} guild(s)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as exc:
        print(f"Slash command sync failed: {exc}")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("You do not have permission to use that command.")
        return
    await ctx.send(f"An error occurred: {error}")


YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "no_warnings": True,
    "extract_flat": False,
}

# Leichtgewichtige Optionen nur zum Auflisten von Playlist-Einträgen
# (kein voller Stream-Resolve pro Track -> deutlich schneller).
YDL_PLAYLIST_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "noplaylist": False,
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
        self.history: list[Song] = []
        self.current_song: Optional[Song] = None
        self.playing = False
        self.volume = 0.5
        # Wird gesetzt, wenn der naechste Track nicht "normal" aus der Queue
        # kommen soll (z.B. bei !previous), sondern ein bestimmter Song ist.
        self._pending_song: Optional[Song] = None

    def is_spotify_url(self, query: str) -> bool:
        parsed = urlparse(query)
        return parsed.netloc in {"open.spotify.com", "spotify.com"}

    def is_url(self, query: str) -> bool:
        """Erkennt beliebige Links (YouTube, SoundCloud, Bandcamp, Vimeo,
        Twitch-Clips, usw.) statt nur YouTube/SoundCloud fest zu verdrahten.
        yt-dlp unterstuetzt selbst hunderte Plattformen - wir muessen die
        Plattform hier nicht einzeln kennen, nur erkennen, dass es ein Link ist.
        """
        parsed = urlparse(query)
        return bool(parsed.scheme) and bool(parsed.netloc)

    def is_youtube_playlist_url(self, query: str) -> bool:
        parsed = urlparse(query)
        if "youtube.com" not in parsed.netloc and "youtu.be" not in parsed.netloc:
            return False
        qs = parse_qs(parsed.query)
        has_list = "list" in qs
        has_video = "v" in qs or "youtu.be" in parsed.netloc
        # Reiner Playlist-Link (kein einzelnes Video) -> als Playlist behandeln.
        return has_list and not has_video

    def is_soundcloud_set_url(self, query: str) -> bool:
        parsed = urlparse(query)
        return "soundcloud.com" in parsed.netloc and "/sets/" in parsed.path

    def is_playlist_url(self, query: str) -> bool:
        return self.is_youtube_playlist_url(query) or self.is_soundcloud_set_url(query)

    async def _extract_info(self, query: str) -> dict:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(YDL_OPTIONS)

        if self.is_spotify_url(query):
            resolved_query = await self._resolve_spotify_query(query)
            if resolved_query:
                query = resolved_query

        if self.is_url(query):
            source_query = query
        else:
            source_query = f"ytsearch1:{query}"

        try:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(source_query, download=False))
        except Exception as exc:
            if not self.is_url(query):
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch1:{query}", download=False))
            else:
                raise RuntimeError(f"Unable to resolve track: {exc}") from exc

        if not isinstance(info, dict):
            raise RuntimeError("Could not get track metadata")

        if info.get("entries"):
            return info["entries"][0]
        return info

    async def _extract_playlist_entries(self, query: str) -> tuple[str, list[Song], int]:
        """Listet alle Tracks einer Playlist/eines Sets auf.
        Gibt (Playlist-Titel, Song-Liste, Anzahl uebersprungener Eintraege) zurueck.
        """
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(YDL_PLAYLIST_OPTIONS)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))

        if not isinstance(info, dict):
            raise RuntimeError("Could not get playlist metadata")

        entries = info.get("entries") or []
        songs: list[Song] = []
        skipped = 0

        for entry in entries:
            if not entry:
                skipped += 1
                continue
            track_url = self._entry_to_url(entry)
            if not track_url:
                skipped += 1
                continue
            title = entry.get("title") or track_url
            songs.append(Song(title=title, url=track_url, requested_by=""))

        return info.get("title", query), songs, skipped

    def _entry_to_url(self, entry: dict) -> Optional[str]:
        url = entry.get("url") or entry.get("webpage_url")
        if url and str(url).startswith("http"):
            return url
        video_id = entry.get("id")
        ie_key = (entry.get("ie_key") or "").lower()
        if video_id and "youtube" in ie_key:
            return f"https://www.youtube.com/watch?v={video_id}"
        if video_id and "soundcloud" in ie_key and url:
            return url
        return None

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
            raise RuntimeError("No playable audio URL was found for this track")

        return discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)

    async def _play_song(self, ctx: commands.Context, song: Song) -> None:
        self.playing = True
        self.current_song = song

        try:
            source = await self._build_source(song.url)
            source = discord.PCMVolumeTransformer(source, volume=self.volume)
        except Exception as exc:
            await ctx.send(f"Unable to play {song.title}: {exc}")
            self.playing = False
            self.current_song = None
            await self._play_next(ctx)
            return

        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return

        await ctx.send(f"Now playing: {song.title}")
        ctx.voice_client.play(
            source,
            after=lambda error: self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self._finish_song(ctx))),
        )

    async def _play_next(self, ctx: commands.Context) -> None:
        if not self.queue:
            self.playing = False
            self.current_song = None
            await ctx.send("The queue is empty.")
            return

        if self.current_song is not None:
            self.history.append(self.current_song)

        song = self.queue.pop(0)
        await self._play_song(ctx, song)

    async def _finish_song(self, ctx: commands.Context) -> None:
        # !previous hat einen bestimmten Song zum Abspielen vorgemerkt.
        if self._pending_song is not None:
            song = self._pending_song
            self._pending_song = None
            if self.current_song is not None:
                self.history.append(self.current_song)
            await self._play_song(ctx, song)
            return

        if self.current_song is not None:
            self.history.append(self.current_song)
        self.current_song = None

        if self.queue:
            await self._play_next(ctx)
        else:
            self.playing = False
            await ctx.send("The queue is empty.")

    @commands.hybrid_command(name="ping", description="Check if the bot is responsive")
    async def ping(self, ctx: commands.Context) -> None:
        await ctx.send("pong")

    @commands.hybrid_command(name="help", description="Show available commands")
    async def help_command(self, ctx: commands.Context) -> None:
        help_text = (
            "Commands:\n"
            "!join - join your voice channel\n"
            "!play <url, playlist url, or search> - queue a song or an entire playlist\n"
            "!queue - show the queue\n"
            "!skip / !next - play the next song\n"
            "!previous / !back - play the previous song again\n"
            "!pause - pause playback\n"
            "!resume - resume playback\n"
            "!stop - stop and clear queue\n"
            "!leave - leave voice channel\n"
            "!now - show current song"
        )
        await ctx.send(help_text)

    @commands.hybrid_command(name="join", description="Join your current voice channel")
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

    @commands.hybrid_command(name="play", description="Play a song or playlist from URL or search query")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        if ctx.interaction is not None:
            await ctx.defer()

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel first.")
            return

        if not ctx.voice_client:
            await ctx.author.voice.channel.connect()

        if ctx.voice_client and ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)

        # Playlist-Link: alle Tracks auflisten und in die Queue packen.
        if self.is_url(query) and self.is_playlist_url(query):
            try:
                playlist_title, songs, skipped = await self._extract_playlist_entries(query)
            except Exception as exc:
                await ctx.send(f"I could not resolve that playlist: {exc}")
                return

            if not songs:
                await ctx.send("I could not find any playable tracks in that playlist.")
                return

            for song in songs:
                song.requested_by = str(ctx.author)
                self.queue.append(song)

            message = f"Queued {len(songs)} tracks from playlist: {playlist_title}"
            if skipped:
                message += f" ({skipped} entries skipped)"
            await ctx.send(message)

            if not self.playing:
                await self._play_next(ctx)
            return

        # Einzelner Track (URL oder Suchbegriff).
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

    @commands.hybrid_command(name="queue", description="Show the current queue")
    async def queue_command(self, ctx: commands.Context) -> None:
        if not self.queue:
            await ctx.send("The queue is empty.")
            return

        items = "\n".join(f"{index + 1}. {song.title}" for index, song in enumerate(self.queue))
        await ctx.send(f"Queue:\n{items}")

    @commands.hybrid_command(name="skip", aliases=["next"], description="Skip to the next track")
    async def skip(self, ctx: commands.Context) -> None:
        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
            await ctx.send("Skipped the current track.")
        else:
            await ctx.send("Nothing is playing right now.")

    @commands.hybrid_command(name="previous", aliases=["back"], description="Play the previous track again")
    async def previous(self, ctx: commands.Context) -> None:
        if not self.history:
            await ctx.send("There is no previous track.")
            return

        if not ctx.voice_client or not ctx.voice_client.is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        prev_song = self.history.pop()

        # Den aktuellen Song wieder vorne in die Queue legen, damit man mit
        # !skip / !next wieder dorthin zurueckkommt.
        if self.current_song is not None:
            self.queue.insert(0, self.current_song)

        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            self._pending_song = prev_song
            ctx.voice_client.stop()  # loest _finish_song aus, das _pending_song abspielt
        else:
            await self._play_song(ctx, prev_song)

    @commands.hybrid_command(name="pause", description="Pause playback")
    async def pause(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Playback paused.")
        else:
            await ctx.send("Nothing is playing right now.")

    @commands.hybrid_command(name="resume", description="Resume playback")
    async def resume(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Playback resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, ctx: commands.Context) -> None:
        if ctx.voice_client:
            ctx.voice_client.stop()
        self.queue.clear()
        self.history.clear()
        self._pending_song = None
        self.current_song = None
        self.playing = False
        await ctx.send("Stopped playback and cleared the queue.")

    @commands.hybrid_command(name="leave", description="Leave the voice channel")
    async def leave(self, ctx: commands.Context) -> None:
        if ctx.voice_client and ctx.voice_client.is_connected():
            await ctx.voice_client.disconnect()
            await ctx.send("Left the voice channel.")
        else:
            await ctx.send("I am not connected to a voice channel.")

    @commands.hybrid_command(name="now", description="Show the currently playing song")
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
    except discord.LoginFailure:
        print("Login failed. Check your DISCORD_TOKEN in the .env file.")
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