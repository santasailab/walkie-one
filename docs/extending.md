# Change speech or connect another assistant

Start with the [working server setup](../personal-server/README.md), then replace
one interface at a time. Run the local rehearsal and affected tests after each
change. Keep routing, playback receipts, and authorization outside provider code.

## Speech services

`server-runtime/speech.py` separates recognition (`transcribe(pcm) -> str`) and
synthesis (`synthesize(text, voice=None) -> bytes`). The personal server's private
`server.json` selects each independently under `speech`:

| Selection | Private settings |
| --- | --- |
| `deepgram` | `DEEPGRAM_API_KEY`; optional `DEEPGRAM_STT_MODEL` and `DEEPGRAM_TTS_MODEL` |
| `http` | `WALKIEONE_SPEECH_URL` and `WALKIEONE_SPEECH_TOKEN` |

Set `WALKIEONE_STT_PROVIDER` and `WALKIEONE_TTS_PROVIDER` explicitly. Missing
credentials fail startup; a failed request does not fall back to company services.
The shared speech module also retains an Azure adapter for compatibility tests,
but the personal-server configuration currently exposes only the choices above.

The supplied Deepgram adapter uses its [recognition API](https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded)
and [synthesis API](https://developers.deepgram.com/reference/text-to-speech/speak-request).
It sends `mip_opt_out=true`; this is not a zero-retention guarantee. Review provider
terms, costs, supported voices, and content handling. Live accounts and device
decoding still need validation.

### Your HTTP speech service

Use an HTTPS base URL such as `https://speech.example.org/v1`. Both requests send
`Authorization: Bearer <your token>`. The adapter refuses redirects and uses a
15-second timeout. Provider keys remain on the speech server.

- `POST /transcribe`: binary little-endian PCM16, mono, 24,000 Hz. Return
  `{"text":"recognized words"}`. The response is limited to 64 KiB and text to
  4,000 characters. The personal server caps each recording at 60 seconds.
- `POST /synthesize`: JSON `{"text":"reply","voice":null}`. Return MP3 bytes,
  MPEG-2 Layer III, mono, 24,000 Hz, 48 kbit/s, at most 1 MiB. Input text is
  limited to 4,000 characters and an optional voice to 128 characters.

The software checks the MP3 envelope; the device must validate actual frames.
Tests in `server-runtime/tests/test_speech_providers.py` exercise malformed
responses, bounds, redirects, provider selection, and safe error messages.

## Custom command adapter

Install a trusted executable locally. Create a mode-0600 JSON file outside the
repository containing its argument array, for example:

```json
["/absolute/path/to/python3", "/absolute/path/to/my-assistant.py"]
```

Then run:

```sh
.venv/bin/python link-runtime/independent.py \
  --profile "$HOME/walkie-private/link.json" \
  --agent custom --command-file /private/command.json --check
```

Remove `--check` to start it. Nothing is downloaded or evaluated as shell text.
The program reads one JSON line from stdin:

```json
{"version":1,"prompt":"Hello","session_id":null,"scope":"device"}
```

It writes one JSON object to stdout:

```json
{"text":"Hello from my assistant","session_id":"optional-id","needs_answer":false}
```

Use `session_id` to resume your own conversation. Diagnostics belong on stderr,
with credentials and private content removed. Input is capped at 128 KiB, output
at 16 KiB, speakable text at 4,000 characters, and a turn at 180 seconds.
Cancellation stops the process group. Timeout, malformed output, or a nonzero
exit fails the turn without replaying potentially completed work.

This adapter is trusted code running with your user permissions, not a sandbox.
Link strips its transport and speech credentials from the child environment.
Give the assistant only its own credentials and intended access. A simple echo
fixture is enough to test connectivity before connecting a real assistant.

## Full assistant integration

`link-runtime/agent_endpoint.py` defines `AgentEndpoint`: readiness, running a
turn, cancellation, and optional steering. Requests contain text and session
state; responses contain speakable text and bounded question metadata.

Preserve these behaviors when adding a provider:

1. Resume the correct conversation. A follow-up may steer the active operation;
   it must not silently create another task or replay an uncertain command.
2. Bind steering and approvals to the requesting device and exact operation.
   Treat unsupported steering as unsupported, not as a successful new request.
3. Keep provider approvals. Spoken consent is valid only after the matching
   question was played, before expiry, and for that pending interaction.
4. Bound input/output and normalize failures. Never send raw provider stderr,
   keys, or internal diagnostics to the handheld.
5. Cancel and shut down the provider process tree. Ignore stale results and
   make reconnects safe without repeating side effects.

The maintained runtime tests include fixture Codex/Claude processes for steering,
approval, cancellation, duplicate results, and shutdown. A custom command adapter
only supports ordinary final replies and questions; it does not implement the
full deferred-approval and live-steering interfaces automatically.

You can also implement Link in your own application using the
[transport protocol](../personal-server/PROTOCOL.md). Test with two simulated
devices before hardware: replies and playback receipts must stay with their
originating device, and another device must not steer its active conversation.
