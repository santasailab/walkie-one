#define RELAY_BOARD_06 1
#define RELAY_PCB06_MIC_SLOT 0
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstddef>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <string>
#include "mic_filter.h"
#include "status_led.h"

constexpr int ESP_OK = 0, I2S_NUM_0 = 0, I2S_ROLE_MASTER = 1;
constexpr int I2S_DATA_BIT_WIDTH_16BIT = 16, I2S_SLOT_MODE_STEREO = 2;
constexpr int PIN_MIC_CLK = 6, PIN_MIC_DOUT = 7, SAMPLE_RATE_HZ = 24000, MIC_SHIFT = 0;
using gpio_num_t = int;
struct Channel { bool enabled = false; };
using i2s_chan_handle_t = Channel*;
struct i2s_event_data_t {};
struct i2s_event_callbacks_t { bool (*on_recv_q_ovf)(i2s_chan_handle_t, i2s_event_data_t*, void*); };
bool board06MicOverflow(i2s_chan_handle_t, i2s_event_data_t*, void*) { return false; }
int i2s_channel_register_event_callback(Channel*, i2s_event_callbacks_t* callbacks, void*) {
  assert(callbacks->on_recv_q_ovf); return ESP_OK;
}
struct i2s_chan_config_t { int id; int dma_desc_num = 0, dma_frame_num = 0; };
struct i2s_pdm_rx_config_t {
  int clk_cfg;
  struct { int bits, mode; } slot_cfg;
  struct { int clk, din; struct { bool clk_inv; } invert_flags; } gpio_cfg;
};
#define I2S_CHANNEL_DEFAULT_CONFIG(a, b) i2s_chan_config_t{a}
#define I2S_PDM_RX_CLK_DEFAULT_CONFIG(a) (a)
#define I2S_PDM_RX_SLOT_DEFAULT_CONFIG(a, b) {a, b}
static uint32_t now = 0;
static int live = 0, enables = 0, disables = 0, amplitude = 10;
static bool emptyRead = false, failEnable = false, recording = false, playing = false;
static bool micCapturing = false;
static i2s_chan_handle_t rxChan = nullptr;
struct MicLock {};
size_t ringAvail() { return 0; }
static std::string logText;
struct {
  void println(const char* s) { logText += s; logText += '\n'; }
  void printf(const char* fmt, ...) {
    char out[1024]; va_list args; va_start(args, fmt);
    vsnprintf(out, sizeof(out), fmt, args); va_end(args); logText += out;
  }
} Serial;
uint32_t millis() { return now; }
void delay(int ms) { now += ms; }
void esp_task_wdt_reset() {}
void ledSet(Rgb color) { amplitude = color.green ? 1000 : 10; }
int i2s_new_channel(i2s_chan_config_t* config, void*, i2s_chan_handle_t* out) {
  assert(config->id == I2S_NUM_0);
  *out = new Channel; ++live; return ESP_OK;
}
int i2s_channel_init_pdm_rx_mode(Channel*, i2s_pdm_rx_config_t* config) {
  assert(config->clk_cfg == 24000 && config->slot_cfg.bits == 16);
  assert(config->slot_cfg.mode == I2S_SLOT_MODE_STEREO);
  assert(config->gpio_cfg.clk == 6 && config->gpio_cfg.din == 7);
  return ESP_OK;
}
int i2s_channel_enable(Channel* channel) {
  if (failEnable) return 1;
  channel->enabled = true; ++enables; return ESP_OK;
}
int i2s_channel_disable(Channel* channel) {
  assert(channel->enabled); channel->enabled = false; ++disables; return ESP_OK;
}
int i2s_del_channel(Channel* channel) {
  assert(!channel->enabled); delete channel; --live; return ESP_OK;
}
int i2s_channel_read(Channel* channel, void* out, size_t bytes, size_t* read, int timeout) {
  assert(channel->enabled);
  if (emptyRead) { *read = 0; now += timeout; return 1; }
  auto* samples = static_cast<int16_t*>(out);
  for (size_t i = 0; i < bytes / 4; ++i) {
    samples[i * 2] = i % 2 ? -amplitude : amplitude;
    samples[i * 2 + 1] = i % 2 ? -amplitude * 2 : amplitude * 2;
  }
  *read = bytes; now += 11; return ESP_OK;
}
// No network functions exist here; the extracted diagnostic must never call one.
// SKETCH_FUNCTIONS
int main(int argc, char** argv) {
  assert(argc == 2);
  emptyRead = std::string(argv[1]) == "silent-driver";
  failEnable = std::string(argv[1]) == "enable-failure";
  assert(setupMic() && rxChan && !rxChan->enabled);
  board06MicDiagnostic();
  assert(!rxChan && !live && !recording && !micCapturing);
  assert(now < 13000);
  if (failEnable) {
    assert(enables == 0 && disables == 0);
    assert(logText.find("PDM start failed") != std::string::npos);
  } else {
    assert(enables == 1 && disables == 1);
    assert(logText.find("capture stopped") != std::string::npos);
    assert(logText.find("quiet: frames=") != std::string::npos);
    assert(logText.find("speech: frames=") != std::string::npos);
    if (!emptyRead) {
      const size_t speech = logText.find("speech: frames=");
      const double left = std::strtod(logText.c_str() + logText.find("L rms=", speech) + 6, nullptr);
      const double right = std::strtod(logText.c_str() + logText.find("R rms=", speech) + 6, nullptr);
      assert(left > 500 && left < 1500);
      assert(std::fabs(right - left * 2) < left * 0.02);
    }
  }
}
