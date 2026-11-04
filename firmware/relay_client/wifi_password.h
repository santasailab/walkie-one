#pragma once

#include "wifi_networks.h"

// Nonblocking, bounded serial input. Spaces are password bytes, not formatting.
// Even an invalid or timed-out line is consumed in full so secret characters
// cannot escape into the one-key diagnostic commands.
class WifiPasswordInput {
 public:
  enum class Result { Waiting, Ready, Invalid };
  void begin(const char *ssid, uint32_t now) {
    clear();
    strncpy(network_.ssid, ssid, sizeof(network_.ssid) - 1);
    active_ = true;
    started_ = now;
  }
  bool active() const { return active_ || discard_; }
  const saved_wifi::Network &network() const { return network_; }
  bool expire(uint32_t now) {
    if (!active_ || uint32_t(now - started_) < 60000) return false;
    clear();
    discard_ = true;
    return true;
  }
  Result feed(char c) {
    if (discard_) {
      if (c == '\n') discard_ = false;
      return Result::Waiting;
    }
    if (!active_) return Result::Waiting;
    if (c == '\n') {
      active_ = false;
      if (length_ && line_[length_ - 1] == '\r') --length_;
      if (invalid_ || length_ >= sizeof(network_.password)) return Result::Invalid;
      memcpy(network_.password, line_, length_);
      network_.password[length_] = 0;
      return network_.valid() ? Result::Ready : Result::Invalid;
    }
    if (c == 0 || length_ == sizeof(line_)) invalid_ = true;
    else line_[length_++] = c;
    return Result::Waiting;
  }
  void clear() {
    network_ = saved_wifi::Network{};
    memset(line_, 0, sizeof(line_));
    length_ = 0;
    active_ = discard_ = invalid_ = false;
  }

 private:
  saved_wifi::Network network_;
  char line_[64] = {}; // 63 password bytes plus an optional CR.
  size_t length_ = 0;
  uint32_t started_ = 0;
  bool active_ = false, discard_ = false, invalid_ = false;
};
