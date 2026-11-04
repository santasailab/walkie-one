#include <assert.h>
#include <initializer_list>
#include <stdint.h>
#include <string.h>
#include "played_receipt.h"
#include "reconnect_policy.h"

static uint32_t now = 100, entropy = 0;
static uint32_t millis() { return now; }
static uint32_t jitterRandom() { return entropy; }
static struct { void printf(const char *, ...) {} } Serial;
static struct { bool stopped = false; void stop() { stopped = true; } } tls;
static bool wsOpen = true, recording = true, playing = false;
static bool available = true;
static int ledMissed = 1, playbackMutex = 0;
static constexpr int portMAX_DELAY = 0;
static char spanId[65] = "staged-audio";
static PlayedReceipt playedReceipt;
static reconnect_policy::Policy reconnectPolicy;
static struct {
  bool staged = true;
  bool play() { return staged; }
} playback;
static void xSemaphoreTake(int, int) {}
static void xSemaphoreGive(int) {}
static void clearAvailability() { available = false; }
static void stopRecording() { recording = false; }
static bool hasPendingPlayback() { return playback.staged; }
static void abandonPlayback() { playback.staged = false; playing = false; }
static char sent[256];
static void sendHello() { strcat(sent, "hello;"); }
static void sendStatus() { strcat(sent, "status;"); }
static void sendAck(const char *id) { strcat(sent, id); strcat(sent, ";"); }

// SKETCH_FUNCTIONS

int main() {
  const char *id = "c_0123456789abcdef0123456789abcdef";
  assert(playedReceipt.remember(id, now));
  recording = false;
  assert(!playbackPlayStaged()); // Pending receipt blocks newer staged speech.
  assert(playback.staged && !playing);
  assert(playedReceipt.confirm(id));
  assert(!playing); // Confirmation never consumes an earlier blocked click.
  assert(playbackPlayStaged());
  assert(playing);

  // Execute the actual sketch teardown, timer wrapper and network-return caller.
  for (int code : {1006, 4401, 4409}) {
    reconnectPolicy = reconnect_policy::Policy();
    reconnectPolicy.attempting();
    reconnectPolicy.connected(now);
    now += 30000;
    entropy = code == 1006 ? 30000 : 900000;
    wsOpen = available = recording = playback.staged = playing = true;
    tls.stopped = false;
    strcpy(spanId, "staged-audio");
    ledMissed = 1;
    playedReceipt.clear();
    assert(playedReceipt.remember(id, now));
    connectionEnded(code);
    assert(tls.stopped && !wsOpen && !available && !recording);
    assert(!playback.staged && !playing);
    assert(playedReceipt.pending() == (code != 4401));
    if (code == 4401) assert(!spanId[0] && ledMissed == 0);
    assert(!reconnectPolicy.due(now + entropy - 1));
    assert(reconnectPolicy.due(now + entropy));
    entropy = 5000;
    retryNow();
    const uint32_t wait = code == 1006 ? 5000 : 900000;
    assert(!reconnectPolicy.due(now + wait - 1));
    now += wait;
    assert(reconnectPolicy.due(now));
  }

  // Reconnect resends a backed-off receipt at once, without resetting its backoff.
  playedReceipt.clear();
  assert(playedReceipt.remember(id, now));
  for (uint32_t delay : {5000u, 10000u, 20000u, 40000u, 80000u, 160000u}) {
    now += delay;
    assert(playedReceipt.due(now));
  }
  now += 1000;
  sent[0] = 0;
  announceConnection();
  assert(strcmp(sent, "hello;c_0123456789abcdef0123456789abcdef;status;") == 0);
  assert(!playedReceipt.due(now + 299000 - 1));  // Still on the 300 s step.
  assert(playedReceipt.due(now + 299000));
  assert(playedReceipt.confirm(id));
  sent[0] = 0;
  announceConnection();
  assert(strcmp(sent, "hello;status;") == 0);
}
