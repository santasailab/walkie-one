# MP3 playback

Cloud replies arrive as MP3: MPEG-2 Layer III, mono, 24 kHz (Azure
`audio-24khz-48kbitrate-mono-mp3`). `mp3_codec.*` validates a complete reply and
decodes it on demand. It accepts any MPEG-2 Layer III mono 24 kHz bitrate, an
optional leading ID3v2 tag and a trailing ID3v1 tag, and rejects everything
else, including a truncated last frame. Microphone audio goes up as raw PCM, so
the Device has no encoder. The playback cursor survives Listen/Speak pauses
in RAM. Resume reads from that sample with the existing decoder state; the
speaker-write mutex drains queued samples before recording, and only the final
drain permits acknowledgment. Reconnect clears the cursor and compressed span.

minimp3 is pinned to `ea99364f61c14656440e8d77e9c233ccf3124633` from
https://github.com/lieff/minimp3. It is CC0 (public domain; notice in its
header), so it carries no distribution obligations. The one API change adds
caller-owned scratch storage to `mp3dec_decode_frame`; no shared static
workspace and no 16 KiB decoder stack allocation. MP3-only, scalar decoding.
