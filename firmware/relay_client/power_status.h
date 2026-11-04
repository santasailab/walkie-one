#pragma once
#include <stdint.h>

// PCB06: USB_PRESENT_N senses cable power, CHG_STAT_N is the LTC4085-1
// open-drain charge-cycle indication. Neither measures net battery current.
// Inactive CHRG does not prove a full battery: it also covers taper/timeout.
class PowerStatus {
 public:
  struct Observation { uint32_t at; bool usb; bool charge; };
  bool update(bool usb, bool charge, uint32_t now) {
    if (!started_ || usb != candidateUsb_ || charge != candidateCharge_) {
      started_ = true;
      candidateUsb_ = usb;
      candidateCharge_ = charge;
      candidateAt_ = now;
      return false;
    }
    if (uint32_t(now - candidateAt_) < 1000 ||
        (known_ && usb == usb_ && charge == charge_)) return false;
    known_ = true;
    usb_ = usb;
    charge_ = charge;
    dirty_ = true;
    history_[next_] = {now, usb, charge};
    next_ = (next_ + 1) % 8;
    if (count_ < 8) ++count_;
    return true;
  }
  const char *state() const {
    if (!known_) return nullptr;
    return !usb_ ? "on_battery" : charge_ ? "charging" : "plugged_in";
  }
  bool usb() const { return known_ && usb_; }
  bool charging() const { return known_ && usb_ && charge_; }
  bool needsReport() const { return dirty_; }
  void reported() { dirty_ = false; }
  unsigned count() const { return count_; }
  Observation observation(unsigned index) const {
    return history_[(next_ + 8 - count_ + index) % 8];
  }
 private:
  bool started_ = false, known_ = false, dirty_ = false;
  bool candidateUsb_ = false, candidateCharge_ = false;
  bool usb_ = false, charge_ = false;
  uint32_t candidateAt_ = 0;
  Observation history_[8] = {};
  unsigned next_ = 0, count_ = 0;
};
