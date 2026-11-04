#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// When the 06 board downloads, installs and gives up on a firmware release.
// No flash, network or clock calls, so host tests drive it with a fake clock.
// Design: docs/superpowers/specs/2026-10-15-firmware-ota-updates.md.

constexpr uint32_t kOtaIdleBeforeInstallMs = 15UL * 60 * 1000;
constexpr uint32_t kOtaFirstRetryMs = 5UL * 60 * 1000;
constexpr uint32_t kOtaMaxRetryMs = 6UL * 60 * 60 * 1000;
// ponytail: start spread sized for a handful of Devices; widen it from
// rollout and download-load measurements before a large fleet.
constexpr uint32_t kOtaStartJitterMs = 2UL * 60 * 1000;
constexpr uint32_t kOtaDeferMs = 60UL * 1000;  // busy or low battery: look again
constexpr uint32_t kOtaVerifyDeadlineMs = 10UL * 60 * 1000;  // from LTE registration
constexpr int kOtaMinBatteryPercent = 50;                   // or plugged in
constexpr uint8_t kOtaFailedBootsToBlacklist = 2;

// "major.minor.patch", each 0-9999, as one orderable number; -1 if malformed.
inline int64_t otaVersionKey(const char *text) {
  if (!text) return -1;
  int64_t key = 0;
  for (int part = 0; part < 3; ++part) {
    int64_t value = 0, digits = 0;
    while (*text >= '0' && *text <= '9' && digits < 5) value = value * 10 + (*text++ - '0'), ++digits;
    if (!digits || digits > 4) return -1;
    key = key * 10000 + value;
    if (part < 2 && *text++ != '.') return -1;
  }
  return *text ? -1 : key;
}

// The exact bytes the release key signs; tools/restore.py and
// tools/release.py build the same message.
inline int otaSignedMessage(char *out, size_t size, const char *board,
                            const char *version, const char *sha256) {
  return snprintf(out, size, "walkieone-firmware\n%s\n%s\n%s\n", board, version, sha256);
}

// What survives restarts, saved in NVS on transitions (never per tick).
struct OtaRecord {
  uint32_t layout = 1;
  int64_t revision = -1;      // newest target revision accepted
  char target[16] = "";       // accepted target version; "" = none
  char sha256[65] = "";       // its image identity
  uint16_t attempts = 0;      // failed downloads of this target
  uint32_t retryWaitMs = 0;   // wait owed when saved; restarts in full after a reboot
  char staged[16] = "";       // verified image in the spare slot
  char installing[16] = "";   // booted into at the last restart, not yet proven
  char failing[16] = "";      // version whose boots failed...
  uint8_t failedBoots = 0;    // ...this many times
  char blacklisted[16] = "";  // never installed again; a newer release repairs it
};

enum class OtaOffer { Ignored, Cleared, Accepted, Unchanged };
enum class OtaBoot { Normal, Unproven, RolledBack };

class OtaPolicy {
 public:
  explicit OtaPolicy(const OtaRecord &saved) : r_(saved) {}
  const OtaRecord &record() const { return r_; }
  bool dirty() const { return dirty_; }
  void saved() { dirty_ = false; }

  // Reconciles the record with the image that actually booted. A retry wait
  // owed from before the restart starts over now: with no wall clock, waiting
  // again is the safe side.
  OtaBoot boot(const char *running, uint32_t now) {
    nextAttemptAt_ = now + r_.retryWaitMs;
    if (!r_.installing[0]) return OtaBoot::Normal;
    if (!strcmp(r_.installing, running)) return OtaBoot::Unproven;
    // The new image crashed, missed its deadline or never booted.
    if (strcmp(r_.failing, r_.installing)) {
      copy(r_.failing, r_.installing);
      r_.failedBoots = 0;
    }
    if (++r_.failedBoots >= kOtaFailedBootsToBlacklist) copy(r_.blacklisted, r_.installing);
    r_.installing[0] = r_.staged[0] = 0;  // the spare slot is no longer trusted
    dirty_ = true;
    return OtaBoot::RolledBack;
  }

  // The new image reached the relay: keep it.
  void proven() {
    r_.installing[0] = r_.staged[0] = r_.target[0] = r_.sha256[0] = r_.failing[0] = 0;
    r_.failedBoots = 0;
    r_.attempts = 0;
    r_.retryWaitMs = 0;
    dirty_ = true;
  }

  // A firmware.available frame. `image` means url/size/sha256/signature came
  // with it. Every accepted, cleared or repeated offer must be acknowledged.
  OtaOffer offer(int64_t revision, const char *version, const char *sha256, bool image,
                 const char *running, uint32_t now, uint32_t jitterMs) {
    if (revision < 0 || revision < r_.revision) return OtaOffer::Ignored;
    if (revision != r_.revision) { r_.revision = revision; dirty_ = true; }
    const bool wanted = image && otaVersionKey(version) > otaVersionKey(running) &&
                        strcmp(version, r_.blacklisted) != 0;
    if (!wanted) {
      // A pause, a withdrawal or nothing newer: drop any staged image.
      if (r_.target[0] || r_.staged[0]) {
        r_.target[0] = r_.sha256[0] = r_.staged[0] = 0;
        r_.attempts = 0;
        r_.retryWaitMs = 0;
        dirty_ = true;
        return OtaOffer::Cleared;
      }
      return OtaOffer::Unchanged;
    }
    if (!strcmp(version, r_.target) && !strcmp(sha256, r_.sha256)) return OtaOffer::Unchanged;
    copy(r_.target, version);
    copy(r_.sha256, sha256);
    r_.staged[0] = 0;
    r_.attempts = 0;
    r_.retryWaitMs = 0;
    nextAttemptAt_ = now + jitterMs;
    dirty_ = true;
    return OtaOffer::Accepted;
  }

  bool wantsDownload(uint32_t now) const {
    return r_.target[0] && !r_.staged[0] && !r_.installing[0] &&
           int32_t(now - nextAttemptAt_) >= 0;
  }
  // Exponential backoff with jitter (half to all of the step), capped.
  // `random` is any 32-bit random value. Returns the wait in milliseconds.
  uint32_t downloadFailed(uint32_t now, uint32_t random) {
    if (r_.attempts < 0xffff) ++r_.attempts;
    uint32_t step = kOtaFirstRetryMs;
    for (uint16_t i = 1; i < r_.attempts && step < kOtaMaxRetryMs; ++i) step *= 2;
    if (step > kOtaMaxRetryMs) step = kOtaMaxRetryMs;
    const uint32_t wait = step / 2 + random % (step / 2 + 1);
    r_.retryWaitMs = wait;
    nextAttemptAt_ = now + wait;
    dirty_ = true;
    return wait;
  }
  // Talk, playback or a low battery got in the way; that is not a failure.
  void downloadDeferred(uint32_t now) { nextAttemptAt_ = now + kOtaDeferMs; }
  void downloadStaged() {
    copy(r_.staged, r_.target);
    r_.attempts = 0;
    r_.retryWaitMs = 0;
    dirty_ = true;
  }

  // Restart into the staged image only after 15 minutes without use, and only
  // while the relay's latest target still names it.
  bool wantsInstall(uint32_t now, uint32_t lastUseMs) const {
    return r_.staged[0] && !strcmp(r_.staged, r_.target) && !r_.installing[0] &&
           uint32_t(now - lastUseMs) >= kOtaIdleBeforeInstallMs;
  }
  void installing() {
    copy(r_.installing, r_.staged);
    dirty_ = true;
  }

 private:
  template <size_t N> static void copy(char (&out)[N], const char *in) {
    snprintf(out, N, "%s", in ? in : "");
  }
  OtaRecord r_;
  uint32_t nextAttemptAt_ = 0;
  bool dirty_ = false;
};
