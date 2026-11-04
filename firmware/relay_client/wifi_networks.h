#pragma once

#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace saved_wifi {

constexpr size_t kCapacity = 8;
constexpr int16_t kNotSeen = -32768;
constexpr uint32_t kJoinMs = 15000;
constexpr uint32_t kScanMs = 10000;
constexpr uint32_t kRetryMs = 10000;

struct Network {
  char ssid[33] = {};
  char password[64] = {};

  bool valid() const {
    const size_t name = strnlen(ssid, sizeof(ssid));
    const size_t pass = strnlen(password, sizeof(password));
    return name > 0 && name < sizeof(ssid) &&
           (pass == 0 || (pass >= 8 && pass < sizeof(password)));
  }
};

// One versioned NVS blob: only bytes, no compiler-dependent ints or pointers.
// Updating it is atomic; a power cut cannot mix one SSID with another password.
struct Profiles {
  uint8_t version = 1;
  uint8_t count = 0;
  Network networks[kCapacity] = {};

  int find(const char *ssid) const {
    for (size_t i = 0; i < count; ++i)
      if (strcmp(networks[i].ssid, ssid) == 0) return int(i);
    return -1;
  }

  bool valid() const {
    if (version != 1 || count > kCapacity) return false;
    for (size_t i = 0; i < count; ++i) {
      if (!networks[i].valid()) return false;
      for (size_t j = 0; j < i; ++j)
        if (strcmp(networks[i].ssid, networks[j].ssid) == 0) return false;
    }
    return true;
  }

  bool put(const Network &network) {
    if (!network.valid()) return false;
    int index = find(network.ssid);
    if (index < 0) {
      if (count == kCapacity) return false;
      index = count++;
    }
    networks[index] = network;
    return true;
  }

  bool remove(const char *ssid) {
    const int index = find(ssid);
    if (index < 0) return false;
    for (size_t i = size_t(index); i + 1 < count; ++i) networks[i] = networks[i + 1];
    networks[--count] = Network{};
    return true;
  }
};
static_assert(sizeof(Profiles) == 2 + kCapacity * 97, "NVS format changed");

enum class LoadResult { Missing, Loaded, Error };

// Hardware boundary also lets host tests exercise actual reconnect and storage
// behavior, including failed passwords and power-cycle migration.
struct Platform {
  virtual ~Platform() = default;
  virtual LoadResult load(Profiles &profiles) = 0;
  virtual bool save(const Profiles &profiles) = 0;
  virtual bool connected() = 0;
  virtual bool connectedTo(const char *ssid) = 0;
  virtual void disconnect() = 0;
  virtual void join(const Network &network) = 0;
  virtual void startScan() = 0;
  virtual bool scanReady() = 0;
  virtual int16_t strength(const Network &network) = 0;
  virtual void stopScan() = 0;
  virtual void report(const char *message, const char *ssid = "") = 0;
};

class Manager {
 public:
  explicit Manager(Platform &platform) : platform_(platform) {}

  // Returns true only on a successful first migration, for the optional
  // engineering build's initial Wi-Fi setting.
  bool begin(const Network &legacy) {
    Profiles loaded;
    const auto result = platform_.load(loaded);
    writable_ = result != LoadResult::Error && loaded.valid();
    if (result == LoadResult::Loaded && loaded.valid()) profiles_ = loaded;
    else {
      profiles_ = Profiles{};
      // Import the old ESP32 station setting only on the first upgrade. An
      // intentionally empty list must stay empty after forgetting everything.
      if (legacy.valid()) profiles_.put(legacy);
      if (writable_) writable_ = platform_.save(profiles_);
    }
    if (!writable_) platform_.report("ERROR saved networks unavailable; cannot change settings");
    restart();
    return result == LoadResult::Missing && writable_;
  }

  const Profiles &profiles() const { return profiles_; }

  // Try in RAM first. A failed replacement cannot destroy a working password.
  bool request(const Network &network, uint32_t now) {
    if (!writable_ || !network.valid()) return false;
    if (profiles_.count == kCapacity && profiles_.find(network.ssid) < 0) {
      platform_.report("ERROR eight networks saved; use WIFI FORGET <name> first");
      return false;
    }
    restart();
    pending_ = true;
    attempt(network, now);
    return true;
  }

  bool forget(const char *ssid) {
    Profiles updated = profiles_;
    if (!writable_ || !updated.remove(ssid) || !platform_.save(updated)) return false;
    profiles_ = updated;
    restart();
    return true;
  }

  // Used for soft off, wake, and operator diagnostics; no NVS writes.
  void restart() {
    platform_.stopScan();
    platform_.disconnect();
    candidate_ = Network{};
    pending_ = false;
    phase_ = Phase::Idle;
  }

  void service(uint32_t now) {
    if (phase_ == Phase::Online) {
      if (platform_.connected()) return; // Never roam during a healthy link.
      phase_ = Phase::Idle;
    }
    if (phase_ == Phase::Joining) {
      if (platform_.connectedTo(candidate_.ssid)) {
        if (pending_) {
          Profiles updated = profiles_;
          updated.put(candidate_);
          if (platform_.save(updated)) {
            profiles_ = updated;
            platform_.report("saved", candidate_.ssid);
          } else platform_.report("ERROR joined but could not save; old networks retained", candidate_.ssid);
        }
        platform_.report("connected", candidate_.ssid);
        candidate_ = Network{};
        pending_ = false;
        phase_ = Phase::Online;
        return;
      }
      if (uint32_t(now - started_) < kJoinMs) return;
      platform_.report("could not join", candidate_.ssid);
      platform_.disconnect();
      candidate_ = Network{};
      if (pending_) {
        pending_ = false;
        phase_ = Phase::Idle;
      } else {
        next(now);
        return;
      }
    }
    if (phase_ == Phase::Waiting) {
      if (uint32_t(now - started_) < kRetryMs) return;
      phase_ = Phase::Idle;
    }
    if (phase_ == Phase::Idle) {
      if (!profiles_.count) return;
      for (size_t i = 0; i < kCapacity; ++i) { tried_[i] = false; strengths_[i] = kNotSeen; }
      platform_.startScan();
      phase_ = Phase::Scanning;
      started_ = now;
      return;
    }
    if (phase_ == Phase::Scanning) {
      const bool ready = platform_.scanReady();
      if (!ready && uint32_t(now - started_) < kScanMs) return;
      if (ready)
        for (size_t i = 0; i < profiles_.count; ++i)
          strengths_[i] = platform_.strength(profiles_.networks[i]);
      platform_.stopScan();
      next(now);
    }
  }

 private:
  enum class Phase { Idle, Scanning, Joining, Online, Waiting };
  Platform &platform_;
  Profiles profiles_;
  Network candidate_;
  Phase phase_ = Phase::Idle;
  bool writable_ = false;
  bool pending_ = false;
  bool tried_[kCapacity] = {};
  int16_t strengths_[kCapacity] = {};
  uint32_t started_ = 0;

  void attempt(const Network &network, uint32_t now) {
    candidate_ = network;
    platform_.join(candidate_);
    platform_.report("joining", candidate_.ssid);
    phase_ = Phase::Joining;
    started_ = now;
  }

  void next(uint32_t now) {
    int best = -1;
    for (size_t i = 0; i < profiles_.count; ++i)
      if (!tried_[i] && (best < 0 || strengths_[i] > strengths_[best])) best = int(i);
    if (best >= 0) {
      tried_[best] = true;
      // Visible networks first; direct attempts also support hidden networks
      // and missed scans. Every saved network gets a turn before rescanning.
      attempt(profiles_.networks[best], now);
    } else {
      phase_ = Phase::Waiting;
      started_ = now;
    }
  }
};

} // namespace saved_wifi
