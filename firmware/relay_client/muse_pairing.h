#pragma once

// Muse Gadget pairing, community mode, protocol v5. Ported from Meta's
// musegadget Linux SDK (Apache-2.0: pairing.py, ble_setup.py,
// ble_framing.py) at commit 4268880. The Muse phone app pairs this Device
// over Bluetooth and hands it Muse device tokens. The Device forwards them
// to the cloud and keeps nothing.
//
// Everything here runs on the Arduino loop except Setup::onWrite and
// Setup::onDisconnect, which the Bluetooth host task calls.

#include <stddef.h>
#include <stdint.h>

#include <deque>
#include <mutex>
#include <string>
#include <vector>

struct cJSON;

namespace muse {

std::string b64url(const uint8_t *data, size_t len);
bool unb64url(const char *text, std::vector<uint8_t> &out, size_t maxChars = 4096);

// 0xFE, chunk index, chunk count, then a fragment that fits one notification.
std::vector<std::string> encodeChunks(const std::string &data, int mtu);

class ChunkAssembler {
 public:
  // Returns true with a complete message; a write without 0xFE is whole.
  bool feed(const uint8_t *packet, size_t len, std::string &message);
  void reset();

 private:
  std::string buffer_;
  int total_ = 0;
  int next_ = 0;
};

struct Identity {
  std::string nodeId;    // homelink-xxxxxx
  std::string deviceId;  // hatch-link:<mac>
  std::string mac;       // lowercase Wi-Fi station MAC
  std::string firmware;
  std::string sdkToken;  // may be empty
};

using Clock = uint32_t (*)();
using Random = void (*)(uint8_t *out, size_t len);

class Session {
 public:
  Session(const Identity &identity, Clock now, Random random);
  ~Session();
  Session(const Session &) = delete;
  Session &operator=(const Session &) = delete;

  // pairing_client_hello -> pairing_ready JSON, or the wire error.
  const char *hello(const cJSON *message, std::string &ready);
  // One pairing_encrypted record from the phone. Any failure clears the session.
  bool open(const cJSON *envelope, std::string &plaintext);
  // Nonzero generation once the first record proves the session.
  uint32_t clientFinished(const cJSON *command);
  uint32_t markProvisioning();
  // Device-to-phone record; false without a session or for stale work.
  bool sealJson(const std::string &plaintext, uint32_t generation, std::string &envelope);
  bool sealStatus(const char *status, uint32_t generation, std::string &envelope);
  bool confirmed();
  bool provisioning(uint32_t generation);
  void reset();

  // Tests only: use this scalar and nonce for the next hello.
  void fixNextKey(const uint8_t scalar[32], const uint8_t nonce[16]);

 private:
  enum class State { Idle, WaitClientFinished, Ready, Provisioning };
  bool expire();
  void clear();
  void restart();

  Identity identity_;
  Clock now_;
  Random random_;
  State state_ = State::Idle;
  uint32_t generation_ = 0;
  uint32_t deadline_ = 0;
  uint8_t rxKey_[32] = {0};
  uint8_t txKey_[32] = {0};
  std::string sessionId_;
  uint64_t rxCounter_ = 0;
  uint64_t txCounter_ = 0;
  bool fixed_ = false;
  uint8_t fixedScalar_[32] = {0};
  uint8_t fixedNonce_[16] = {0};
};

class Transport {
 public:
  virtual ~Transport() = default;
  virtual void sendPackets(const std::vector<std::string> &packets) = 0;
  virtual int mtu() = 0;
  virtual void disconnect(uint32_t delayMs) = 0;
};

struct Provision {
  std::string accessToken;
  std::string refreshToken;
  std::string noiseHost;
  std::string apiUrlV2;
};

class Setup {
 public:
  Setup(Transport &transport, const Identity &identity, Clock now, Random random);

  void onWrite(const uint8_t *packet, size_t len);  // Bluetooth host task
  void onDisconnect();                              // Bluetooth host task

  // Arduino loop: handle queued phone commands. The Device is already online
  // on `ssid`; the phone's Wi-Fi fields are ignored, never stored.
  void poll(bool online, const char *ssid);
  // Once per pairing, after provision_v2 and the Wi-Fi statuses.
  bool takeProvision(Provision &out);
  // Report the cloud's verdict to the phone (auth_ok or auth_failed).
  void finishProvision(bool ok);
  Session &session() { return session_; }

 private:
  void handle(const std::string &raw, bool decrypted, bool online, const char *ssid);
  void provision(const cJSON *command, bool online);
  void sendStatus(const char *status, uint32_t generation = 0);
  void sendJson(const std::string &json);
  void sendEncrypted(const std::string &json);

  Transport &transport_;
  Identity identity_;
  Session session_;
  std::mutex inboxLock_;
  std::deque<std::string> inbox_;
  ChunkAssembler assembler_;
  bool disconnected_ = false;
  bool plaintextBlocked_ = false;
  uint32_t provisionGeneration_ = 0;
  bool provisionReady_ = false;
  bool provisionInFlight_ = false;
  Provision pending_;
};

}  // namespace muse
