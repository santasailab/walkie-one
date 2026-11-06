#include <cassert>
#include <cstring>
#include <initializer_list>

#include "boot_report.h"

// esp_reset_reason_t, in the pinned ESP-IDF's order.
enum {
  ESP_RST_UNKNOWN, ESP_RST_POWERON, ESP_RST_EXT, ESP_RST_SW, ESP_RST_PANIC,
  ESP_RST_INT_WDT, ESP_RST_TASK_WDT, ESP_RST_WDT, ESP_RST_DEEPSLEEP,
  ESP_RST_BROWNOUT, ESP_RST_SDIO, ESP_RST_USB, ESP_RST_JTAG, ESP_RST_EFUSE,
  ESP_RST_PWR_GLITCH, ESP_RST_CPU_LOCKUP
};

// SKETCH_FUNCTIONS

int main() {
  assert(bootCause(ESP_RST_POWERON) == BootCause::PowerOn);
  assert(bootCause(ESP_RST_SW) == BootCause::Requested);
  assert(bootCause(ESP_RST_PANIC) == BootCause::Crash);
  assert(bootCause(ESP_RST_CPU_LOCKUP) == BootCause::Crash);
  for (int reason : {ESP_RST_INT_WDT, ESP_RST_TASK_WDT, ESP_RST_WDT})
    assert(bootCause(reason) == BootCause::Watchdog);
  assert(bootCause(ESP_RST_BROWNOUT) == BootCause::Brownout);
  assert(bootCause(ESP_RST_PWR_GLITCH) == BootCause::Brownout);
  for (int reason : {ESP_RST_UNKNOWN, ESP_RST_EXT, ESP_RST_USB, ESP_RST_JTAG, ESP_RST_DEEPSLEEP})
    assert(bootCause(reason) == BootCause::Other);

  // A person switched it on, plugged it in or flashed it: the chirp is theirs.
  for (BootCause cause : {BootCause::PowerOn, BootCause::Requested, BootCause::Other})
    assert(!bootIsUnattended(cause));
  // The firmware restarted itself, maybe at night: no chirp.
  for (BootCause cause : {BootCause::Crash, BootCause::Watchdog, BootCause::Brownout})
    assert(bootIsUnattended(cause));

  for (BootCause cause : {BootCause::PowerOn, BootCause::Requested, BootCause::Crash,
                          BootCause::Watchdog, BootCause::Brownout, BootCause::Other})
    assert(std::strlen(bootCauseName(cause)) > 0);
  assert(!std::strcmp(bootCauseName(BootCause::Watchdog), "watchdog"));
}
