#pragma once
#include <stdint.h>

// The relay socket's time limits, named with their units because the ESP32
// core's setters do not agree on one: Stream::setTimeout() and
// setConnectionTimeout() take milliseconds, setHandshakeTimeout() seconds.

// loop() must come round within this or the task watchdog restarts the Device.
constexpr uint32_t kLoopWatchdogMs = 20000;
// The TCP connect, and how long one TLS write may stall with no progress.
// Cellular uplinks pause for seconds at a handover; the core's 3 s default
// closed the socket and discarded the spoken request being uploaded.
constexpr uint32_t kSocketTimeoutMs = 15000;
// Per-character wait while reading the WebSocket upgrade response, and the
// limit on the whole response.
constexpr uint32_t kUpgradeReadMs = 5000;
constexpr uint32_t kUpgradeTotalMs = 20000;
// Completed writes reset the loop watchdog, so progress alone must not hold
// loop() for ever: one frame that cannot cross in this long is a dead link.
constexpr uint32_t kFrameSendMs = 30000;
constexpr uint32_t kTlsHandshakeS = 30;

static_assert(kSocketTimeoutMs + 2000 <= kLoopWatchdogMs,
              "a stalled write must fail before the loop watchdog fires");
static_assert(kUpgradeReadMs + 2000 <= kLoopWatchdogMs,
              "a stalled upgrade read must fail before the loop watchdog fires");
