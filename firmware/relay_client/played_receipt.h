#pragma once
#include <stdint.h>
#include <string.h>

// One exact played Snapshot receipt, retained across reconnects in RAM.
// Playback waits for its confirmation before producing another receipt.
class PlayedReceipt {
 public:
  void clear() { id_[0] = 0; retryMs_ = kInitialRetryMs; }
  bool pending() const { return id_[0] != 0; }
  const char *id() const { return id_; }
  bool remember(const char *id, uint32_t now) {
    if (pending() || strlen(id) != 34 || strncmp(id, "c_", 2) != 0) return false;
    for (unsigned i = 2; i < 34; ++i)
      if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f')))
        return false;
    memcpy(id_, id, 35);
    last_ = now;
    retryMs_ = kInitialRetryMs;
    return true;
  }
  bool due(uint32_t now) {
    if (!pending() || uint32_t(now - last_) < retryMs_) return false;
    last_ = now;
    retryMs_ = retryMs_ < kMaximumRetryMs / 2 ? retryMs_ * 2 : kMaximumRetryMs;
    return true;
  }
  bool confirm(const char *id) {
    if (!pending() || strcmp(id_, id) != 0) return false;
    clear();
    return true;
  }

 private:
  static constexpr uint32_t kInitialRetryMs = 5000;
  static constexpr uint32_t kMaximumRetryMs = 300000;
  uint32_t retryMs_ = kInitialRetryMs;
  char id_[35] = {0};
  uint32_t last_ = 0;
};
