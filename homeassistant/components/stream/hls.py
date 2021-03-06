"""Provide functionality to stream HLS."""
import asyncio
from collections import deque
import io
from typing import Any, Callable, List

from aiohttp import web

from homeassistant.core import callback

from .const import FORMAT_CONTENT_TYPE, MAX_SEGMENTS, NUM_PLAYLIST_SEGMENTS
from .core import Segment, StreamOutput, StreamView
from .fmp4utils import get_codec_string, get_init, get_m4s


@callback
def async_setup_hls(hass):
    """Set up api endpoints."""
    hass.http.register_view(HlsPlaylistView())
    hass.http.register_view(HlsSegmentView())
    hass.http.register_view(HlsInitView())
    hass.http.register_view(HlsMasterPlaylistView())
    return "/api/hls/{}/master_playlist.m3u8"


class HlsMasterPlaylistView(StreamView):
    """Stream view used only for Chromecast compatibility."""

    url = r"/api/hls/{token:[a-f0-9]+}/master_playlist.m3u8"
    name = "api:stream:hls:master_playlist"
    cors_allowed = True

    @staticmethod
    def render(track):
        """Render M3U8 file."""
        # Need to calculate max bandwidth as input_container.bit_rate doesn't seem to work
        # Calculate file size / duration and use a small multiplier to account for variation
        # hls spec already allows for 25% variation
        segment = track.get_segment(track.segments[-1])
        bandwidth = round(
            segment.segment.seek(0, io.SEEK_END) * 8 / segment.duration * 1.2
        )
        codecs = get_codec_string(segment.segment)
        lines = [
            "#EXTM3U",
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},CODECS="{codecs}"',
            "playlist.m3u8",
        ]
        return "\n".join(lines) + "\n"

    async def handle(self, request, stream, sequence):
        """Return m3u8 playlist."""
        track = stream.hls_output()
        # Wait for a segment to be ready
        if not track.segments:
            if not await track.recv():
                return web.HTTPNotFound()
        headers = {"Content-Type": FORMAT_CONTENT_TYPE["hls"]}
        return web.Response(body=self.render(track).encode("utf-8"), headers=headers)


class HlsPlaylistView(StreamView):
    """Stream view to serve a M3U8 stream."""

    url = r"/api/hls/{token:[a-f0-9]+}/playlist.m3u8"
    name = "api:stream:hls:playlist"
    cors_allowed = True

    @staticmethod
    def render_preamble(track):
        """Render preamble."""
        return [
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{track.target_duration}",
            '#EXT-X-MAP:URI="init.mp4"',
        ]

    @staticmethod
    def render_playlist(track):
        """Render playlist."""
        segments = track.segments[-NUM_PLAYLIST_SEGMENTS:]

        if not segments:
            return []

        playlist = ["#EXT-X-MEDIA-SEQUENCE:{}".format(segments[0])]

        for sequence in segments:
            segment = track.get_segment(sequence)
            playlist.extend(
                [
                    "#EXTINF:{:.04f},".format(float(segment.duration)),
                    f"./segment/{segment.sequence}.m4s",
                ]
            )

        return playlist

    def render(self, track):
        """Render M3U8 file."""
        lines = ["#EXTM3U"] + self.render_preamble(track) + self.render_playlist(track)
        return "\n".join(lines) + "\n"

    async def handle(self, request, stream, sequence):
        """Return m3u8 playlist."""
        track = stream.hls_output()
        # Wait for a segment to be ready
        if not track.segments:
            if not await track.recv():
                return web.HTTPNotFound()
        headers = {"Content-Type": FORMAT_CONTENT_TYPE["hls"]}
        return web.Response(body=self.render(track).encode("utf-8"), headers=headers)


class HlsInitView(StreamView):
    """Stream view to serve HLS init.mp4."""

    url = r"/api/hls/{token:[a-f0-9]+}/init.mp4"
    name = "api:stream:hls:init"
    cors_allowed = True

    async def handle(self, request, stream, sequence):
        """Return init.mp4."""
        track = stream.hls_output()
        segments = track.get_segment()
        if not segments:
            return web.HTTPNotFound()
        headers = {"Content-Type": "video/mp4"}
        return web.Response(body=get_init(segments[0].segment), headers=headers)


class HlsSegmentView(StreamView):
    """Stream view to serve a HLS fmp4 segment."""

    url = r"/api/hls/{token:[a-f0-9]+}/segment/{sequence:\d+}.m4s"
    name = "api:stream:hls:segment"
    cors_allowed = True

    async def handle(self, request, stream, sequence):
        """Return fmp4 segment."""
        track = stream.hls_output()
        segment = track.get_segment(int(sequence))
        if not segment:
            return web.HTTPNotFound()
        headers = {"Content-Type": "video/iso.segment"}
        return web.Response(
            body=get_m4s(segment.segment, int(sequence)),
            headers=headers,
        )


class HlsStreamOutput(StreamOutput):
    """Represents HLS Output formats."""

    def __init__(self, hass) -> None:
        """Initialize HlsStreamOutput."""
        super().__init__(hass)
        self._cursor = None
        self._event = asyncio.Event()
        self._segments = deque(maxlen=MAX_SEGMENTS)

    @property
    def container_options(self) -> Callable[[int], dict]:
        """Return Callable which takes a sequence number and returns container options."""
        return lambda sequence: {
            # Removed skip_sidx - see https://github.com/home-assistant/core/pull/39970
            "movflags": "frag_custom+empty_moov+default_base_moof+frag_discont",
            "avoid_negative_ts": "make_non_negative",
            "fragment_index": str(sequence),
        }

    @property
    def segments(self) -> List[int]:
        """Return current sequence from segments."""
        return [s.sequence for s in self._segments]

    @property
    def target_duration(self) -> int:
        """Return the max duration of any given segment in seconds."""
        segment_length = len(self._segments)
        if not segment_length:
            return 1
        durations = [s.duration for s in self._segments]
        return round(max(durations)) or 1

    def get_segment(self, sequence: int = None) -> Any:
        """Retrieve a specific segment, or the whole list."""
        if not sequence:
            return self._segments

        for segment in self._segments:
            if segment.sequence == sequence:
                return segment
        return None

    async def recv(self) -> Segment:
        """Wait for and retrieve the latest segment."""
        last_segment = max(self.segments, default=0)
        if self._cursor is None or self._cursor <= last_segment:
            await self._event.wait()

        if not self._segments:
            return None

        segment = self.get_segment()[-1]
        self._cursor = segment.sequence
        return segment

    def _async_put(self, segment: Segment) -> None:
        """Store output from event loop."""
        self._segments.append(segment)
        self._event.set()
        self._event.clear()

    def cleanup(self):
        """Handle cleanup."""
        self._event.set()
        self._segments = deque(maxlen=MAX_SEGMENTS)
