# Build a compatible personal server

Python is one implementation. Any language can implement these two WebSocket
interfaces. This contract needs no database, company identity provider or
company signing key. Read the exact [Device schema](../schema/events.json) and
[Link implementation](../link-runtime/link.py) alongside these examples.

## Connections and authority

The device opens `wss://YOUR-HOST/device`. The connector opens
`wss://YOUR-HOST/link`. Both send `Authorization: <credential>`; the personal
server also accepts `Bearer <credential>`. No credentials in query strings.
Browser-origin connections and browser subprotocol authentication are not
supported by this minimal server. TLS verification must remain enabled.

The private configuration registers every accepted credential. Generated device
tokens look like `v1.<device-id>.device.<43 base64url characters>`; the Link token
looks like `v2.<32 hex channel ID>.<32 hex Link ID>.link.<43 base64url characters>`.
The two IDs are locally generated UUIDs retained for Link protocol
compatibility; they require no registration service or database. The final segment
is 32 cryptographically random bytes. Treat the entire token as opaque and
compare it in constant time against the registered value. The name alone grants
no authority. A device token cannot authenticate on `/link` or vice versa.
These personal tokens are unrelated to company credentials/signatures.

Device IDs use `[a-z0-9-]{1,32}` and identify routes, not customer accounts.
All registered devices belong to one trusted assistant group. Keep their live
sockets in a map keyed by authenticated device ID; never route by a claimed ID
from device input. One Link socket serves the group. The current reference caps
the group at sixteen devices. Credential and route changes take effect on restart.

## Device audio and playback

The server sends `peer` and `presence` when connectivity changes. For example:

```json
{"v":1,"type":"peer","mac":true}
{"v":1,"type":"presence","cloud":true,"link":true,"agent":"ready","ack_confirmations":false}
```

Here `cloud` means the selected server is connected, including an owner server.
`agent` is `ready`, `busy`, or `unavailable`. Do not infer assistant readiness
merely from an open socket. The reference server receives it from Link.

1. The handheld sends `hello` with `autoplay:false, preload:true`.
2. On Talk it sends `{"v":1,"type":"talk.start"}`, binary PCM chunks, then
   `{"v":1,"type":"talk.end"}`. PCM is little-endian signed 16-bit, mono,
   **24,000 Hz**. The maximum recording is 60 seconds (2,880,000 bytes).
3. The server transcribes that span and submits one text request to Link.
4. After a matching result, the server synthesizes MP3: MPEG-2 Layer III,
   mono, 24,000 Hz, 48 kbit/s, maximum 1 MiB.
5. It sends `audio.start` with a unique `c_...` ID, MP3 binary chunks, and
   `audio.end` with that same ID and `complete:true`. Only then send `digest`
   announcing the unread reply. Required fields and examples are in the schema.
6. The handheld stages the entire audio span, plays on Listen, and sends
   `{"v":1,"type":"ack","id":"c_..."}` after complete playback. Match
   both the authenticated device and exact audio ID before clearing playback.

`audio.retry` requests the retained span again. `listen` with nothing staged
requests a status response. Legacy `cancel` never cancels assistant execution;
spoken stop/cancel are ordinary speech. `dismiss` does not delete replies.
The reference does not advertise durable `ack.confirmed` support. Duplicate
acknowledgments are safe no-ops. Disconnect discards that socket's pending audio.

Never forward raw microphone frames to the current Link: its interface is text.
The reference accepts `hello`, `status` and `firmware.status` without sending
them to company infrastructure. It offers no firmware updates or company pairing.

## Link frames

Immediately after authenticating Link, send a primary-device notification:

```json
{"v":1,"type":"link.primary","pair":"walkie-1"}
```

Link announces availability with `link.presence`, including `agent` and optional
`device_presence:true`. A busy announcement includes its `busy_id`; an unavailable
announcement may include a normalized `failure`. For Link versions requesting
device presence, send `{"v":1,"type":"link.device","pair":"walkie-1","online":true}`
on changes. Other versions must not receive that optional control.

Every routed payload consists of **two consecutive text messages**. Serialize
the envelope and payload under one write lock so concurrent sends cannot mix:

```json
{"v":1,"type":"link.route","pair":"walkie-1","primary":true}
{"v":1,"type":"link.turn.request","id":"32-lowercase-hex-characters","text":"What time is it?","language":"en-US"}
```

The server creates a random 32-character hexadecimal turn ID. Text is capped at
4,000 characters. Link sends its response using the reverse envelope:

```json
{"v":1,"type":"link.output","pair":"walkie-1"}
{"v":1,"type":"link.turn.result","id":"same-turn-id","text":"It is noon.","needs_answer":false}
```

Validate the credential, registered device, turn ID, message type and text size.
A result belongs to its requesting device, regardless of which device is primary
when it finishes. Reject foreign routes; ignore stale completed-turn results.
Device reconnects invalidate audio delivery from the older connection.

After the matching handheld playback acknowledgment, send a routed
`{"v":1,"type":"link.turn.ack","id":"same-turn-id"}`. Link confirms with
a routed `link.turn.acknowledged`. This distinguishes generated text from speech
that was actually heard. A generated result must never produce a premature receipt.

Failures use a routed `link.turn.error` with the same ID and a normalized reason,
such as `busy`, `unavailable`, `authentication_expired`, `permission_blocked`,
`process_failed`, `request_failed`, `update_required`, or `response_too_long`.
Provider logs/credentials stay in the connector. Provider permission questions
may carry `provider_interaction_ttl_seconds`; expire unplayed questions at that
deadline. The existing Link owns permission validation and provider interaction.

The maintained Link accepts one active assistant turn at a time. Several devices
can connect. A different device gets a bounded busy response while work is active.
For a Link advertising `steering:true`, a follow-up from the active device is
transcribed and sent as routed `link.turn.steer` with a fresh `id`, the active
`turn_id`, `text`, and `language`. The correlated `link.turn.steered` receipt
reports `accepted`, `inactive`, `unknown`, or `rejected`. An uncertain send
must not be replayed. Only an explicit `inactive` receipt allows a new turn.
The sender device must match the active request. A busy presence can also carry
`busy_id` for a provider operation resumed after local approval.
It must not silently queue or replay assistant commands. The default shared
assistant conversation is a trust-group choice, not per-user privacy isolation.

`link.proactive.send` uses the same envelope. The minimal server returns a
correlated `link.proactive.receipt` with `status:"rejected"` and
`reason:"cloud_unavailable"`, because durable unsolicited delivery is unsupported.
It never reports a message queued when it cannot preserve it.

## Compatibility and testing

Use [the runnable rehearsal](rehearse.py) and
[the socket tests](../server-runtime/tests/test_personal_server.py) as executable examples.
Test wrong-role/unknown credentials, two devices, busy work, wrong-device replies
and receipts, reconnects, stale responses, oversized input, speech failures,
and assistant disconnects. Run the maintained Link against your implementation.
Schema compatibility does not prove a modem, TLS trust store or speaker works;
those require a real device. Keep protocol changes versioned.

The company deployment adds identity, ownership, SIM eligibility and durable
encrypted delivery. Those company services are outside this repository. A personal server satisfies these interfaces locally; it has
no reason to call company APIs to authorize its own registered devices.
