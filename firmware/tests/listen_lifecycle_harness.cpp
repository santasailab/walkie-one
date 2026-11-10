// Execute the production button and message handlers. Hardware and MP3
// validation are stubbed; all playback admission/state transitions are real.
#define RELAY_CELLULAR
#include <cassert>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include "endpoint_availability.h"
#include "listen_gesture.h"
#include "playback_state.h"
#include "played_receipt.h"
#include "ptt_gesture.h"
#include "volume_button.h"

constexpr int LOW = 0, HIGH = 1, PIN_BTN_POWER = -1, ESP_OK = 0;
constexpr int SAMPLE_RATE_HZ = 24000, portMAX_DELAY = 0;
static uint32_t now = 100;
static uint32_t millis() { return now; }
static struct { int play = 8, talk = 38, volumeUp = -1, volumeDown = -1; } buttons;
static int listenLevel = HIGH, speakLevel = HIGH;
static int digitalRead(int pin) { return pin == buttons.play ? listenLevel : speakLevel; }
static int constrain(int n, int low, int high) { return n < low ? low : n > high ? high : n; }
static struct {
  void printf(const char*, ...) {}
  void println(const char*) {}
} Serial;
static struct { void stop() {} } tls;
static bool wsOpen = true, recording = false, playing = false;
static bool playbackReceiving = false, playbackOverflow = false;
static bool confirmsPlayedReceipts = false, offRequested = false, volumeFeedback = false;
static uint32_t volumeChangedAt = 0;
static int volume = 5, ledMissed = 0, playbackMutex = 0, txChan = 0;
static size_t playbackBytes = 0;
static int32_t playbackPeak = 0;
static uint8_t playbackSpan[1];
static char spanId[65] = {}, pendingAck[65] = {};
static PlaybackState playback;
static PlayedReceipt playedReceipt;
static EndpointAvailability endpointAvailability;
static void xSemaphoreTake(int, int) {}
static void xSemaphoreGive(int) {}
static int i2s_channel_write(int, const void*, size_t n, size_t* written, int) {
  *written = n; return ESP_OK;
}
static void esp_task_wdt_reset() {}
static struct Decoder {
  bool prepare(const uint8_t*, size_t, size_t& samples, int32_t& peak, void (*)()) {
    samples = 24000; peak = 1000; return true;
  }
} decoder;
static Decoder* mp3Playback = &decoder;
static size_t ringAvail() { return 0; }
static void ringClear() {}
static void sendHello() {}
static void clearAvailability() { endpointAvailability.clear(); }
static void scheduleRetry() {}
static int listenRequests = 0;
static void wsSendText(const char* text) {
  if (strstr(text, "listen")) ++listenRequests;
}
static void startRecording() { recording = true; }
static void stopRecording() { recording = false; }
enum class MuseMode { Off, Paired };
static std::atomic<MuseMode> museMode{MuseMode::Off};
static std::atomic<bool> pttClosedMuse{false};
static void museStart() {}
static void museStop() { museMode = MuseMode::Off; }

// SKETCH_FUNCTIONS

static void message(const char* type, const char* id = "reply") {
  char text[256];
  snprintf(text, sizeof(text), "{\"v\":1,\"type\":\"%s\",\"id\":\"%s\"}", type, id);
  handleMessage(text, strlen(text));
}
static void listen(int level) {
  now += 30;  // Above the physical button debounce interval.
  listenLevel = level;
  buttonService();
}
static void click() { listen(LOW); listen(HIGH); }
static void silent() {
  assert(!playing && !playback.ready && "reply played without a fresh Listen click");
}

int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string scenario = argv[1];
  endpointAvailability.applyPresence(true, true);
  buttonService();  // Initialize the physical released state.

  if (scenario == "no-click" || scenario == "idle-single-click" || scenario == "idle-double-click") {
    if (scenario != "no-click") click();
    if (scenario == "idle-double-click") click();
    now += 60000;
    message("audio.start");
    message("audio.end");
  } else if (scenario == "during-download") {
    message("audio.start");
    click(); click();
    message("audio.end");
  } else if (scenario == "held-download") {
    listen(LOW);
    message("audio.start");
    message("audio.end");
    silent();  // A power hold must not start newly arrived speech either.
    listen(HIGH);
  } else if (scenario == "replacement-during-hold") {
    message("audio.start", "old-reply");
    message("audio.end", "old-reply");
    listen(LOW);
    message("audio.start", "new-reply");
    message("audio.end", "new-reply");
    listen(HIGH);  // The press was for the old span, not its replacement.
  } else if (scenario == "receipt-confirmation") {
    message("audio.start");
    message("audio.end");
    const char* receipt = "c_0123456789abcdef0123456789abcdef";
    assert(playedReceipt.remember(receipt, now));
    click();  // The receipt gate makes this click ineligible for playback.
    silent();
    message("ack.confirmed", receipt);
    assert(!playedReceipt.pending());
  } else {
    assert(false && "unknown scenario");
  }
  silent();
  assert(playback.staged && pendingAck[0]);

  // A fresh press/release plays the ready reply. Pause/resume keeps its cursor.
  listen(LOW);
  silent();
  listen(HIGH);
  assert(playing && playback.ready && !playback.staged);
  playback.played = 12000;
  click();
  silent();
  assert(playback.staged && playback.played == 12000);
  click();
  assert(playing && playback.ready && playback.played == 12000);
}
