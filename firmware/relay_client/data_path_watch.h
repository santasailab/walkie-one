#pragma once
#include <stdint.h>

// The bearer can report an address while nothing crosses it: a stale PPP
// session, a dropped carrier NAT binding. Redialing the relay over that session
// never recovers; only a new modem session does. This decides when repeated
// failures to reach the relay are the bearer's fault and it should be restarted.
//
// A relay restart or short outage looks the same from here, so one restart
// needs both several failures and a sustained period, and each restart that
// does not help doubles the period before the next.
// What the pinned core's lastError() reports when mbedTLS rejects the relay's
// certificate (MBEDTLS_ERR_X509_CERT_VERIFY_FAILED). The sketch asserts it.
constexpr int kTlsCertificateRejected = -0x2700;

// Whether a dial proved the bearer carries data. A TLS session does, and so
// does a certificate this Device rejected: it came from the relay, and
// restarting the modem cannot repair a clock or a chain. A timeout or an
// unanswered connect (the core reports -1) proves nothing.
inline bool dialReachedRelay(bool tlsUp, int tlsError) {
  return tlsUp || tlsError == kTlsCertificateRejected;
}

class DataPathWatch {
 public:
  static constexpr uint32_t kFirstWaitMs = 3UL * 60 * 1000;
  static constexpr uint32_t kMaxWaitMs = 60UL * 60 * 1000;
  static constexpr uint8_t kMinFailures = 3;

  // The relay answered, even to refuse: the bearer carries data.
  void reached() {
    failing_ = false;
    waitMs_ = kFirstWaitMs;
  }
  // The bearer itself went down; the modem driver is already redialing.
  void bearerDown() { failing_ = false; }
  // A dial that never reached the relay, or an open socket gone silent, while
  // the bearer reports data. True when the bearer should be restarted.
  bool unreachable(uint32_t now) {
    if (!failing_) {
      failing_ = true;
      since_ = now;
      failures_ = 0;
    }
    if (failures_ < UINT8_MAX) ++failures_;
    if (failures_ < kMinFailures || uint32_t(now - since_) < waitMs_) return false;
    failing_ = false;
    waitMs_ = waitMs_ < kMaxWaitMs / 2 ? waitMs_ * 2 : kMaxWaitMs;
    return true;
  }

 private:
  uint32_t since_ = 0, waitMs_ = kFirstWaitMs;
  uint8_t failures_ = 0;
  bool failing_ = false;
};
