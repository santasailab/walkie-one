# Squelch Gate feedback

The original project sound assets and their 24 kHz mono PCM conversions are
included under the project's community license. `provenance.json` contains
per-file source and PCM hashes; `conversion.json` records resampling settings.
Private discussion links and vendor request identifiers are not part of this export.

The opening is 220 ms, closing 330 ms, and held bed about 763 ms before looping.
The current runtime silences the held bed and turns the amplifier off during
that interval. Opening/closing cues retain their fades and volume in
`pcb06_ptt_player.h`; generated samples are
in `pcb06_ptt_sounds.h`. `tools/generate_ptt_sounds.py` rebuilds them, with no
network call. Tests compare the committed bytes and allow one PCM unit of CPU
rounding when reproducing FFmpeg resampling.
