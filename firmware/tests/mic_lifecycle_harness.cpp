#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <initializer_list>

#ifdef RELAY_BOARD_06
#include "board_profile.h"
#endif
constexpr int ESP_OK = 0, I2S_NUM_0 = 0, I2S_NUM_1 = 1, I2S_ROLE_MASTER = 1;
constexpr int I2S_DATA_BIT_WIDTH_16BIT = 16, I2S_SLOT_MODE_STEREO = 2;
constexpr int I2S_DATA_BIT_WIDTH_32BIT = 32, I2S_SLOT_MODE_MONO = 1;
constexpr int I2S_STD_SLOT_LEFT = 1, I2S_GPIO_UNUSED = -1;
#ifndef RELAY_BOARD_06
constexpr int PIN_MIC_BCLK = 4, PIN_MIC_LRCL = 5, PIN_MIC_DOUT = 6;
#endif
constexpr int SAMPLE_RATE_HZ = 24000, MIC_SHIFT = 14, LOW = 0;
using gpio_num_t = int;
struct Channel { bool enabled = false; };
using i2s_chan_handle_t = Channel*;
struct i2s_chan_config_t { int port; int dma_desc_num = 0, dma_frame_num = 0; };
struct i2s_std_config_t {
  int clk_cfg;
  struct { int slot_mask; } slot_cfg;
  struct {
    int mclk, bclk, ws, dout, din;
    struct { bool a, b, c; } invert_flags;
  } gpio_cfg;
};
struct i2s_pdm_rx_config_t {
  int clk_cfg;
  struct { int bits, mode; } slot_cfg;
  struct { int clk, din; struct { bool invert; } invert_flags; } gpio_cfg;
};
#define I2S_CHANNEL_DEFAULT_CONFIG(a, b) i2s_chan_config_t{a}
#define I2S_PDM_RX_CLK_DEFAULT_CONFIG(a) (a)
#define I2S_PDM_RX_SLOT_DEFAULT_CONFIG(a, b) {a, b}
#define I2S_STD_CLK_DEFAULT_CONFIG(a) (a)
#define I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(a, b) {0}
static i2s_chan_handle_t rxChan = nullptr;
static bool micCapturing = false;
struct MicLock {};
#define ESP_ERROR_CHECK(result) assert((result) == ESP_OK)
static int liveChannels = 0, generation = 0, failInit = 0, failEnable = 0;
int i2s_new_channel(i2s_chan_config_t* cfg, void*, i2s_chan_handle_t* out) {
#ifdef RELAY_BOARD_06
  assert(cfg->port == I2S_NUM_0);
#else
  assert(cfg->port == I2S_NUM_1);
#endif
  *out = new Channel; ++liveChannels; ++generation; return ESP_OK;
}
int i2s_channel_init_std_mode(Channel*, i2s_std_config_t*) { return failInit; }
int i2s_channel_init_pdm_rx_mode(Channel*, i2s_pdm_rx_config_t* cfg) {
  assert(cfg->clk_cfg == 24000);
  assert(cfg->slot_cfg.bits == 16 && cfg->slot_cfg.mode == I2S_SLOT_MODE_STEREO);
  assert(cfg->gpio_cfg.clk == 6 && cfg->gpio_cfg.din == 7);
  return failInit;
}
int i2s_channel_enable(Channel* c) {
  if (failEnable) return failEnable;
  c->enabled = true; return ESP_OK;
}
int i2s_channel_disable(Channel* c) { c->enabled = false; return ESP_OK; }
int i2s_del_channel(Channel* c) {
  assert(!c->enabled); delete c; --liveChannels; return ESP_OK;
}
struct {
  template<class... T> void printf(const char*, T...) {}
  void println(const char*) {}
} Serial;
static bool recording = false, talkSent = false, playing = false;
static bool wsOpen = true;
static uint32_t recStartMs = 0, now = 0, micSamplesRead = 0, micClipped = 0;
static uint64_t micEnergy = 0;
static int32_t micPeak = 0;
static size_t micBatchFill = 0;
struct { void reset() {} int32_t process(int32_t v) { return v; } } micHighPass;
static constexpr size_t MIC_FRAME = 240, MIC_BATCH = 960;
static int16_t micBatch[MIC_BATCH];
static int reads = 0, talkEnds = 0, samplesPerRead = 1;
int i2s_channel_read(Channel* c, void* output, size_t, size_t* count, int) {
  assert(c->enabled);
  ++reads;
  for (int i = 0; i < samplesPerRead; ++i) {
#ifdef RELAY_BOARD_06
    static_cast<int16_t*>(output)[i * 2] = 1234;
    static_cast<int16_t*>(output)[i * 2 + 1] = -2345;
#else
    static_cast<int32_t*>(output)[i] = 1 << MIC_SHIFT;
#endif
  }
  *count = samplesPerRead * sizeof(int32_t);
  return ESP_OK;
}
struct { bool canRequest(bool open) { return open; } } endpointAvailability;
struct { int talk = 38; } buttons;
static bool held = true;
int digitalRead(int) { return held ? LOW : 1; }
uint32_t millis() { return now; }
void pausePlayback() { playing = false; }
bool pttIsSpeech(uint32_t ms) { return ms >= 250; }
void wsSendText(const char* text) {
  assert(!rxChan || !rxChan->enabled);
  if (strstr(text, "talk.end")) ++talkEnds;
}
void flushMicBatch() { assert(!rxChan || !rxChan->enabled); micBatchFill = 0; }
void sendMicFrames(bool) { micBatchFill = 0; }
#ifdef RELAY_BOARD_06
int playbackMutex = 0, portMAX_DELAY = 0;
bool board06AmpOn = false;
void xSemaphoreTake(int, int) {}
void xSemaphoreGive(int) {}
void ringClear() {}
bool board06DrainSpeaker() { board06AmpOn = false; return true; }
#endif
// SKETCH_FUNCTIONS
int main() {
  assert(setupMic());
  assert(rxChan && !rxChan->enabled);  // Boot must never clock/capture the mic.
  startRecording();
  assert(recording && rxChan->enabled);
  pumpMic();
  assert(micSamplesRead == 1 && micBatchFill == 1);
#ifdef RELAY_BOARD_06
  assert(micBatch[0] == (RELAY_PCB06_MIC_SLOT ? -2345 : 1234));
#else
  assert(micBatch[0] == 1);
#endif
  micBatchFill = 42;
  now = 300;
  held = false;
  guardMicRelease();  // Still runs when the network/main loop is blocked.
  assert(!rxChan->enabled && !micCapturing);
  stopRecording();
  assert(!recording && rxChan == nullptr && liveChannels == 0);
  int previous = generation;
  held = true;
  startRecording();
  assert(generation > previous);  // No old DMA queue survives the boundary.
  now += 40;
  stopRecording();  // Tap: no speech, but still tear down capture.
  assert(!recording && rxChan == nullptr && micBatchFill == 0);
  stopRecording();  // Idempotent error/disconnect cleanup.
  held = false;
  startRecording();
  assert(!recording && rxChan == nullptr);
  held = true;
  failInit = 1;
  startRecording();
  assert(!recording && rxChan == nullptr && liveChannels == 0);
  failInit = 0;
  failEnable = 1;
  startRecording();
  assert(!recording && rxChan == nullptr && liveChannels == 0);
  failEnable = 0;
  // Exercise both timeout owners, including millis rollover, without waiting.
  for (uint32_t started : {1000u, UINT32_MAX - 30000u}) {
    for (bool guardFirst : {false, true}) {
      now = started;
      startRecording();
      for (uint32_t elapsed : {60000u, 60001u, 899999u}) {
        now = started + elapsed;
        guardMicRelease();
        assert(recording && micCapturing && rxChan->enabled);
        int before = reads;
        pumpMic();
        assert(recording && reads == before + 1);
      }
      now = started + 900000u;
      int before = reads, ends = talkEnds;
      if (guardFirst) {
        guardMicRelease();  // The network loop can be blocked at the deadline.
        assert(!micCapturing && !rxChan->enabled);
      }
      pumpMic();
      assert(!recording && rxChan == nullptr && liveChannels == 0);
      assert(reads == before && talkEnds == ends + 1);
      pumpMic();  // A held button must not emit another end or reopen capture.
      assert(!recording && talkEnds == ends + 1);
    }
  }
  now = 1000;
  startRecording();
  now += 120000;
  held = false;
  guardMicRelease();  // Releasing a long recording still stops capture at once.
  assert(!micCapturing && !rxChan->enabled);
  pumpMic();
  assert(!recording && rxChan == nullptr);
  held = true;
  startRecording();
  now += 899000;
  micSamplesRead = 24000u * 900 - 1;
  samplesPerRead = 2;  // The audio clock may run slightly faster than millis.
  pumpMic();
  assert(micSamplesRead == 24000u * 900);
  assert(!recording && rxChan == nullptr && liveChannels == 0);
}
