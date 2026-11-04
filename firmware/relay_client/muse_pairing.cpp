#include "muse_pairing.h"

#include <string.h>

#include <algorithm>

#include "cJSON.h"
#include "mbedtls/base64.h"
#include "mbedtls/bignum.h"
#include "mbedtls/ecdh.h"
#include "mbedtls/ecp.h"
#include "mbedtls/gcm.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"

namespace muse {
namespace {

constexpr int kVersion = 5;
constexpr const char *kModel = "hatch_link";
constexpr const char *kSuite = "p256-hkdf-sha256-aes-gcm-v1";
constexpr const char *kAuth = "none";
constexpr const char *kPolicy = "confirm_app";  // the phone app collects consent
constexpr const char *kRecordLabel = "hatch-link ble setup v1";
constexpr const char *kSessionIdLabel = "hatch-link session id v1";
constexpr const char *kCurrentConnection = "Use current connection";
constexpr uint32_t kClientFinishedMs = 60000;
constexpr uint32_t kConfirmedMs = 120000;
constexpr uint32_t kProvisioningMs = 120000;
constexpr uint32_t kDisconnectAfterErrorMs = 300;
constexpr size_t kMaxPacket = 160;
constexpr size_t kHeader = 3;
constexpr size_t kMaxMessage = 8192;
constexpr uint8_t kMagic = 0xFE;
constexpr uint8_t kToDevice = 0;
constexpr uint8_t kFromDevice = 1;

const char *const kSensitive[] = {"provision", "provision_v2", "wifi_scan",
                                  "ota",       "device.ota",   "unpair",
                                  "set_wifi",  "set_auth"};
const char *const kPlaintextStatuses[] = {
    "error_encryption_required", "error_pairing_invalid_hello",
    "error_pairing_unavailable", "error_pairing_decrypt"};

template <size_t N>
bool listed(const char *value, const char *const (&list)[N]) {
  for (const char *item : list) {
    if (strcmp(value, item) == 0) return true;
  }
  return false;
}

int rng(void *context, unsigned char *out, size_t len) {
  (*static_cast<Random *>(context))(out, len);
  return 0;
}

const char *text(const cJSON *object, const char *key) {
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
  return cJSON_IsString(item) && item->valuestring ? item->valuestring : nullptr;
}

std::string print(cJSON *root) {
  char *printed = cJSON_PrintUnformatted(root);
  std::string out = printed ? printed : "";
  cJSON_free(printed);
  cJSON_Delete(root);
  return out;
}

void wipe(cJSON *item) {
  for (; item; item = item->next) {
    if (item->valuestring) {
      mbedtls_platform_zeroize(item->valuestring, strlen(item->valuestring));
    }
    wipe(item->child);
  }
}

void wipe(std::string &secret) {
  if (!secret.empty()) mbedtls_platform_zeroize(&secret[0], secret.size());
  secret.clear();
}

void sha256(const void *data, size_t len, uint8_t out[32]) {
  mbedtls_sha256(static_cast<const unsigned char *>(data), len, out, 0);
}

void recordNonce(uint8_t direction, uint64_t counter, uint8_t out[12]) {
  out[0] = direction;
  out[1] = out[2] = out[3] = 0;
  for (int i = 0; i < 8; i++) out[4 + i] = uint8_t(counter >> (56 - 8 * i));
}

std::string recordAad(const std::string &sessionId, uint8_t direction,
                      uint64_t counter) {
  return std::string(kRecordLabel) + "|" + sessionId + "|" +
         (direction == kToDevice ? "m2d" : "d2m") + "|" + std::to_string(counter);
}

bool parseCounter(const char *value, uint64_t &out) {
  if (!value || !*value) return false;
  uint64_t counter = 0;
  for (const char *c = value; *c; c++) {
    if (*c < '0' || *c > '9') return false;
    uint64_t digit = uint64_t(*c - '0');
    if (counter > (UINT64_MAX - digit) / 10) return false;
    counter = counter * 10 + digit;
  }
  out = counter;
  return true;
}

bool gcm(bool encrypt, const uint8_t key[32], uint8_t direction, uint64_t counter,
         const std::string &aad, const std::vector<uint8_t> &in,
         std::vector<uint8_t> &out, uint8_t tag[16]) {
  uint8_t iv[12];
  recordNonce(direction, counter, iv);
  out.assign(in.size(), 0);
  mbedtls_gcm_context context;
  mbedtls_gcm_init(&context);
  int rc = mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key, 256);
  const auto *a = reinterpret_cast<const unsigned char *>(aad.data());
  if (rc == 0 && encrypt) {
    rc = mbedtls_gcm_crypt_and_tag(&context, MBEDTLS_GCM_ENCRYPT, in.size(), iv,
                                   sizeof(iv), a, aad.size(), in.data(),
                                   out.data(), 16, tag);
  } else if (rc == 0) {
    rc = mbedtls_gcm_auth_decrypt(&context, in.size(), iv, sizeof(iv), a,
                                  aad.size(), tag, 16, in.data(), out.data());
  }
  mbedtls_gcm_free(&context);
  return rc == 0;
}

}  // namespace

std::string b64url(const uint8_t *data, size_t len) {
  size_t size = 0;
  mbedtls_base64_encode(nullptr, 0, &size, data, len);
  std::string out(size, '\0');
  if (mbedtls_base64_encode(reinterpret_cast<unsigned char *>(&out[0]), size,
                            &size, data, len) != 0) {
    return "";
  }
  out.resize(size);
  for (char &c : out) {
    if (c == '+') c = '-';
    if (c == '/') c = '_';
  }
  while (!out.empty() && out.back() == '=') out.pop_back();
  return out;
}

bool unb64url(const char *value, std::vector<uint8_t> &out, size_t maxChars) {
  if (!value) return false;
  std::string padded(value);
  if (padded.empty() || padded.size() > maxChars || padded.size() % 4 == 1) {
    return false;
  }
  for (char &c : padded) {
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
      continue;
    }
    if (c == '-') {
      c = '+';
    } else if (c == '_') {
      c = '/';
    } else {
      return false;
    }
  }
  while (padded.size() % 4) padded += '=';
  size_t size = 0;
  out.assign(padded.size() / 4 * 3, 0);
  if (mbedtls_base64_decode(out.data(), out.size(), &size,
                            reinterpret_cast<const unsigned char *>(padded.data()),
                            padded.size()) != 0) {
    return false;
  }
  out.resize(size);
  return true;
}

std::vector<std::string> encodeChunks(const std::string &data, int mtu) {
  size_t notifyMax = std::min<size_t>(mtu > 3 ? size_t(mtu - 3) : 20, kMaxPacket);
  size_t usable = notifyMax - kHeader;
  size_t total = data.empty() ? 1 : (data.size() + usable - 1) / usable;
  if (total > 255) return {};
  std::vector<std::string> packets;
  for (size_t i = 0; i < total; i++) {
    std::string packet = {char(kMagic), char(i), char(total)};
    packet += data.substr(i * usable, usable);
    packets.push_back(packet);
  }
  return packets;
}

bool ChunkAssembler::feed(const uint8_t *packet, size_t len, std::string &message) {
  if (len < kHeader || packet[0] != kMagic) {
    message.assign(reinterpret_cast<const char *>(packet), len);
    return true;
  }
  int index = packet[1];
  int total = packet[2];
  if (total == 0) {
    reset();
    return false;
  }
  if (index == 0 || total != total_) {
    reset();
    total_ = total;
  }
  if (index != next_ || index >= total_ ||
      buffer_.size() + len - kHeader > kMaxMessage) {
    reset();
    return false;
  }
  buffer_.append(reinterpret_cast<const char *>(packet + kHeader), len - kHeader);
  next_ = index + 1;
  if (next_ < total_) return false;
  message.swap(buffer_);
  reset();
  return true;
}

void ChunkAssembler::reset() {
  wipe(buffer_);
  total_ = 0;
  next_ = 0;
}

// ---------------------------------------------------------------------------

Session::Session(const Identity &identity, Clock now, Random random)
    : identity_(identity), now_(now), random_(random) {
  if (identity_.firmware.empty()) identity_.firmware = "unknown";
  restart();
}

Session::~Session() { clear(); }

void Session::fixNextKey(const uint8_t scalar[32], const uint8_t nonce[16]) {
  memcpy(fixedScalar_, scalar, sizeof(fixedScalar_));
  memcpy(fixedNonce_, nonce, sizeof(fixedNonce_));
  fixed_ = true;
}

const char *Session::hello(const cJSON *message, std::string &ready) {
  const cJSON *version = cJSON_GetObjectItemCaseSensitive(message, "version");
  const char *auth = text(message, "pairing_auth");
  const char *policy = text(message, "pairing_policy");
  if (!cJSON_IsNumber(version) || version->valuedouble != kVersion || !auth ||
      strcmp(auth, kAuth) != 0 || !policy || strcmp(policy, kPolicy) != 0) {
    return "error_pairing_invalid_hello";
  }
  restart();
  std::vector<uint8_t> mobilePub, mobileNonce;
  if (!unb64url(text(message, "mobile_pub"), mobilePub) ||
      !unb64url(text(message, "mobile_nonce"), mobileNonce) ||
      mobilePub.size() != 65 || mobilePub[0] != 0x04 || mobileNonce.size() != 16) {
    restart();
    return "error_pairing_invalid_hello";
  }

  mbedtls_ecp_group group;
  mbedtls_ecp_point devicePoint, mobilePoint;
  mbedtls_mpi devicePrivate, shared;
  mbedtls_ecp_group_init(&group);
  mbedtls_ecp_point_init(&devicePoint);
  mbedtls_ecp_point_init(&mobilePoint);
  mbedtls_mpi_init(&devicePrivate);
  mbedtls_mpi_init(&shared);
  uint8_t devicePub[65], deviceNonce[16], secret[32];
  size_t written = 0;
  bool ok = mbedtls_ecp_group_load(&group, MBEDTLS_ECP_DP_SECP256R1) == 0 &&
            mbedtls_ecp_point_read_binary(&group, &mobilePoint, mobilePub.data(),
                                          mobilePub.size()) == 0 &&
            mbedtls_ecp_check_pubkey(&group, &mobilePoint) == 0;
  if (ok && fixed_) {
    ok = mbedtls_mpi_read_binary(&devicePrivate, fixedScalar_, 32) == 0 &&
         mbedtls_ecp_mul(&group, &devicePoint, &devicePrivate, &group.G, rng,
                         &random_) == 0;
    memcpy(deviceNonce, fixedNonce_, sizeof(deviceNonce));
    fixed_ = false;
  } else if (ok) {
    ok = mbedtls_ecp_gen_keypair(&group, &devicePrivate, &devicePoint, rng,
                                 &random_) == 0;
    random_(deviceNonce, sizeof(deviceNonce));
  }
  ok = ok &&
       mbedtls_ecp_point_write_binary(&group, &devicePoint,
                                      MBEDTLS_ECP_PF_UNCOMPRESSED, &written,
                                      devicePub, sizeof(devicePub)) == 0 &&
       written == sizeof(devicePub) &&
       mbedtls_ecdh_compute_shared(&group, &shared, &mobilePoint, &devicePrivate,
                                   rng, &random_) == 0 &&
       mbedtls_mpi_write_binary(&shared, secret, sizeof(secret)) == 0;
  mbedtls_mpi_free(&shared);
  mbedtls_mpi_free(&devicePrivate);
  mbedtls_ecp_point_free(&mobilePoint);
  mbedtls_ecp_point_free(&devicePoint);
  mbedtls_ecp_group_free(&group);
  if (!ok) {
    mbedtls_platform_zeroize(secret, sizeof(secret));
    restart();
    return "error_pairing_unavailable";
  }

  const std::string mobilePubText = b64url(mobilePub.data(), mobilePub.size());
  const std::string devicePubText = b64url(devicePub, sizeof(devicePub));
  const std::string mobileNonceText = b64url(mobileNonce.data(), mobileNonce.size());
  const std::string deviceNonceText = b64url(deviceNonce, sizeof(deviceNonce));
  const std::string transcript =
      std::string("hatch-link-pairing-v5\nversion=5\ninitiator_role=mobile\n") +
      "responder_role=link\ndevice_id=" + identity_.deviceId +
      "\nnode_id=" + identity_.nodeId + "\nmac=" + identity_.mac +
      "\nmodel=" + kModel + "\nfirmware_version=" + identity_.firmware +
      "\nselected_cipher_suite=" + kSuite + "\npairing_auth=" + kAuth +
      "\npairing_auth_epoch=0\npairing_policy=" + kPolicy +
      "\nconfirm_timeout_seconds=0\nmobile_pub=" + mobilePubText +
      "\ndevice_pub=" + devicePubText + "\nmobile_nonce=" + mobileNonceText +
      "\ndevice_nonce=" + deviceNonceText;
  uint8_t transcriptHash[32], salt[32], sessionSecret[32], session[32];
  sha256(transcript.data(), transcript.size(), transcriptHash);
  uint8_t saltInput[16 + 16 + 32];
  memcpy(saltInput, mobileNonce.data(), 16);
  memcpy(saltInput + 16, deviceNonce, 16);
  memcpy(saltInput + 32, transcriptHash, 32);
  sha256(saltInput, sizeof(saltInput), salt);
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  const auto *label = reinterpret_cast<const unsigned char *>(kRecordLabel);
  ok = mbedtls_hkdf(md, salt, sizeof(salt), secret, sizeof(secret), label,
                    strlen(kRecordLabel), sessionSecret, sizeof(sessionSecret)) == 0 &&
       mbedtls_hkdf_expand(md, sessionSecret, sizeof(sessionSecret),
                           reinterpret_cast<const unsigned char *>("mobile->device"),
                           14, rxKey_, sizeof(rxKey_)) == 0 &&
       mbedtls_hkdf_expand(md, sessionSecret, sizeof(sessionSecret),
                           reinterpret_cast<const unsigned char *>("device->mobile"),
                           14, txKey_, sizeof(txKey_)) == 0;
  std::string sessionInput = kSessionIdLabel;
  sessionInput.append(reinterpret_cast<const char *>(transcriptHash), 32);
  sessionInput.append(reinterpret_cast<const char *>(secret), 32);
  sha256(sessionInput.data(), sessionInput.size(), session);
  wipe(sessionInput);
  mbedtls_platform_zeroize(secret, sizeof(secret));
  mbedtls_platform_zeroize(sessionSecret, sizeof(sessionSecret));
  if (!ok) {
    restart();
    return "error_pairing_unavailable";
  }
  sessionId_ = b64url(session, 16);
  rxCounter_ = txCounter_ = 0;
  state_ = State::WaitClientFinished;
  deadline_ = now_() + kClientFinishedMs;

  cJSON *root = cJSON_CreateObject();
  cJSON_AddStringToObject(root, "type", "pairing_ready");
  cJSON_AddNumberToObject(root, "version", kVersion);
  cJSON_AddStringToObject(root, "device_id", identity_.deviceId.c_str());
  cJSON_AddStringToObject(root, "node_id", identity_.nodeId.c_str());
  cJSON_AddStringToObject(root, "mac", identity_.mac.c_str());
  cJSON_AddStringToObject(root, "model", kModel);
  cJSON_AddStringToObject(root, "firmware_version", identity_.firmware.c_str());
  cJSON_AddStringToObject(root, "pairing_auth", kAuth);
  cJSON_AddNumberToObject(root, "pairing_auth_epoch", 0);
  cJSON_AddStringToObject(root, "pairing_policy", kPolicy);
  cJSON_AddStringToObject(root, "device_pub", devicePubText.c_str());
  cJSON_AddStringToObject(root, "device_nonce", deviceNonceText.c_str());
  cJSON_AddStringToObject(root, "transcript_hash",
                          b64url(transcriptHash, sizeof(transcriptHash)).c_str());
  cJSON_AddStringToObject(root, "session_id", sessionId_.c_str());
  ready = print(root);
  return nullptr;
}

bool Session::open(const cJSON *envelope, std::string &plaintext) {
  if (expire() || state_ == State::Idle) {
    restart();
    return false;
  }
  const char *session = text(envelope, "session_id");
  uint64_t counter = 0;
  std::vector<uint8_t> ciphertext, tag, plain;
  bool ok = session && sessionId_ == session &&
            parseCounter(text(envelope, "counter"), counter) &&
            counter == rxCounter_ &&
            unb64url(text(envelope, "ciphertext"), ciphertext, 16384) &&
            unb64url(text(envelope, "tag"), tag) && tag.size() == 16 &&
            gcm(false, rxKey_, kToDevice, counter,
                recordAad(sessionId_, kToDevice, counter), ciphertext, plain,
                tag.data());
  if (!ok) {
    restart();
    return false;
  }
  rxCounter_++;
  plaintext.assign(plain.begin(), plain.end());
  mbedtls_platform_zeroize(plain.data(), plain.size());
  return true;
}

uint32_t Session::clientFinished(const cJSON *command) {
  const cJSON *only = cJSON_IsObject(command) ? command->child : nullptr;
  bool exact = only && !only->next && only->string &&
               strcmp(only->string, "action") == 0 && cJSON_IsString(only) &&
               strcmp(only->valuestring, "pairing_client_finished") == 0;
  if (!exact || expire() || state_ != State::WaitClientFinished || rxCounter_ != 1) {
    restart();
    return 0;
  }
  generation_++;
  state_ = State::Ready;
  deadline_ = now_() + kConfirmedMs;
  return generation_;
}

uint32_t Session::markProvisioning() {
  if (!expire() && state_ == State::Ready) {
    generation_++;
    state_ = State::Provisioning;
    deadline_ = now_() + kProvisioningMs;
  }
  return state_ == State::Provisioning ? generation_ : 0;
}

bool Session::sealJson(const std::string &plaintext, uint32_t generation,
                       std::string &envelope) {
  if ((generation && generation != generation_) || expire() || state_ == State::Idle) {
    return false;
  }
  uint64_t counter = txCounter_;
  std::vector<uint8_t> in(plaintext.begin(), plaintext.end()), sealed;
  uint8_t tag[16];
  bool ok = gcm(true, txKey_, kFromDevice, counter,
                recordAad(sessionId_, kFromDevice, counter), in, sealed, tag);
  mbedtls_platform_zeroize(in.data(), in.size());
  if (!ok) return false;
  txCounter_++;
  cJSON *root = cJSON_CreateObject();
  cJSON_AddStringToObject(root, "type", "pairing_encrypted");
  cJSON_AddStringToObject(root, "session_id", sessionId_.c_str());
  cJSON_AddStringToObject(root, "counter", std::to_string(counter).c_str());
  cJSON_AddStringToObject(root, "ciphertext", b64url(sealed.data(), sealed.size()).c_str());
  cJSON_AddStringToObject(root, "tag", b64url(tag, sizeof(tag)).c_str());
  envelope = print(root);
  return true;
}

bool Session::sealStatus(const char *status, uint32_t generation, std::string &envelope) {
  cJSON *root = cJSON_CreateObject();
  cJSON_AddStringToObject(root, "type", "status");
  cJSON_AddStringToObject(root, "status", status);
  // Apps read only type and status, so older ones ignore the token.
  if (!identity_.sdkToken.empty() && strcmp(status, "pairing_confirmed") == 0) {
    cJSON_AddStringToObject(root, "sdk_token", identity_.sdkToken.c_str());
  }
  std::string plaintext = print(root);
  bool sealed = sealJson(plaintext, generation, envelope);
  wipe(plaintext);
  return sealed;
}

bool Session::confirmed() {
  expire();
  return state_ == State::Ready || state_ == State::Provisioning;
}

bool Session::provisioning(uint32_t generation) {
  return generation != 0 && generation == generation_ && !expire() &&
         state_ == State::Provisioning;
}

void Session::reset() { restart(); }

bool Session::expire() {
  if (state_ == State::Idle || int32_t(now_() - deadline_) <= 0) return false;
  // Drop the keys but keep the generation, so the owner of the expired
  // session can still recognise it.
  clear();
  return true;
}

void Session::clear() {
  state_ = State::Idle;
  deadline_ = 0;
  mbedtls_platform_zeroize(rxKey_, sizeof(rxKey_));
  mbedtls_platform_zeroize(txKey_, sizeof(txKey_));
  sessionId_.clear();
  rxCounter_ = txCounter_ = 0;
}

void Session::restart() {
  generation_++;
  clear();
}

// ---------------------------------------------------------------------------

Setup::Setup(Transport &transport, const Identity &identity, Clock now, Random random)
    : transport_(transport), identity_(identity), session_(identity, now, random) {}

void Setup::onWrite(const uint8_t *packet, size_t len) {
  std::lock_guard<std::mutex> lock(inboxLock_);
  std::string message;
  if (assembler_.feed(packet, len, message) && inbox_.size() < 8) {
    inbox_.push_back(std::move(message));
  }
}

void Setup::onDisconnect() {
  std::lock_guard<std::mutex> lock(inboxLock_);
  assembler_.reset();
  for (std::string &message : inbox_) wipe(message);
  inbox_.clear();
  disconnected_ = true;
}

void Setup::poll(bool online, const char *ssid) {
  for (;;) {
    std::string raw;
    bool dropped;
    {
      std::lock_guard<std::mutex> lock(inboxLock_);
      dropped = disconnected_;
      disconnected_ = false;
      if (!dropped && inbox_.empty()) return;
      if (!inbox_.empty()) {
        raw.swap(inbox_.front());
        inbox_.pop_front();
      }
    }
    if (dropped) {
      plaintextBlocked_ = false;
      session_.reset();
    }
    if (!raw.empty()) handle(raw, false, online, ssid);
    wipe(raw);
  }
}

bool Setup::takeProvision(Provision &out) {
  if (!provisionReady_) return false;
  out = pending_;
  wipe(pending_.accessToken);
  wipe(pending_.refreshToken);
  provisionReady_ = false;
  provisionInFlight_ = true;
  return true;
}

void Setup::finishProvision(bool ok) {
  provisionInFlight_ = false;
  sendStatus(ok ? "auth_ok" : "auth_failed", provisionGeneration_);
  if (!ok) transport_.disconnect(500);
}

// cJSON recurses per nesting level; anyone in Bluetooth range could otherwise
// exhaust the loop stack. Same bound as Meta's ESP32 firmware.
static bool depthValid(const std::string &raw) {
  unsigned depth = 0;
  bool quoted = false, escaped = false;
  for (char c : raw) {
    if (quoted) {
      if (escaped) {
        escaped = false;
      } else if (c == '\\') {
        escaped = true;
      } else if (c == '"') {
        quoted = false;
      }
    } else if (c == '"') {
      quoted = true;
    } else if (c == '{' || c == '[') {
      if (++depth > 32) return false;
    } else if ((c == '}' || c == ']') && depth) {
      depth--;
    }
  }
  return true;
}

void Setup::handle(const std::string &raw, bool decrypted, bool online, const char *ssid) {
  cJSON *command =
      depthValid(raw) ? cJSON_ParseWithLength(raw.data(), raw.size()) : nullptr;
  if (!cJSON_IsObject(command)) {
    cJSON_Delete(command);
    sendStatus("error_invalid_command");
    return;
  }
  const char *action = text(command, "action");
  if (!action) action = "";
  const bool sensitive = listed(action, kSensitive);

  if (!decrypted && strcmp(action, "pairing_client_hello") == 0) {
    std::string ready;
    const char *error = session_.hello(command, ready);
    if (error) {
      sendStatus(error);
    } else {
      plaintextBlocked_ = true;
      sendJson(ready);
    }
  } else if (!decrypted && strcmp(action, "pairing_encrypted") == 0) {
    std::string plaintext;
    if (session_.open(command, plaintext)) {
      handle(plaintext, true, online, ssid);
    } else {
      sendStatus("error_pairing_decrypt");
      transport_.disconnect(kDisconnectAfterErrorMs);
    }
    wipe(plaintext);
  } else if (strcmp(action, "get_device_info") == 0) {
    // Public metadata, safe in plaintext at any point.
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "device_info");
    cJSON_AddStringToObject(root, "node_id", identity_.nodeId.c_str());
    cJSON_AddStringToObject(root, "version", identity_.firmware.c_str());
    cJSON_AddStringToObject(root, "device_id", identity_.deviceId.c_str());
    cJSON_AddStringToObject(root, "mac", identity_.mac.c_str());
    cJSON_AddStringToObject(root, "model", kModel);
    cJSON_AddNumberToObject(root, "pairing_protocol", kVersion);
    cJSON_AddStringToObject(root, "pairing_auth", kAuth);
    cJSON_AddNumberToObject(root, "pairing_auth_epoch", 0);
    cJSON_AddStringToObject(root, "pairing_policy", kPolicy);
    cJSON_AddStringToObject(root, "build_sha", "");
    // false makes the app loop without saying why (muse-gadget-sdk #101).
    cJSON_AddBoolToObject(root, "network_ready", online);
    sendJson(print(root));
  } else if (!decrypted && plaintextBlocked_) {
    // Plaintext after a hello is a downgrade attempt; say nothing.
  } else if (!decrypted && sensitive) {
    sendStatus("error_encryption_required");
  } else if (decrypted && strcmp(action, "pairing_client_finished") == 0) {
    uint32_t generation = session_.clientFinished(command);
    if (generation) {
      sendStatus("pairing_confirmed", generation);
    } else {
      sendStatus("error_pairing_decrypt");
      transport_.disconnect(kDisconnectAfterErrorMs);
    }
  } else if (decrypted && sensitive && !session_.confirmed()) {
    sendStatus("error_pairing_confirm_required");
  } else if (decrypted && strcmp(action, "wifi_scan") == 0) {
    // Already online: offer one open network the app preselects; whatever
    // credentials come back are ignored.
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "wifi_scan_result");
    cJSON *networks = cJSON_AddArrayToObject(root, "networks");
    if (online) {
      cJSON *entry = cJSON_CreateObject();
      cJSON_AddStringToObject(entry, "ssid", ssid && *ssid ? ssid : kCurrentConnection);
      cJSON_AddNumberToObject(entry, "rssi", -40);
      cJSON_AddBoolToObject(entry, "secure", false);
      cJSON_AddItemToArray(networks, entry);
    }
    sendEncrypted(print(root));
  } else if (decrypted && strcmp(action, "provision_v2") == 0) {
    provision(command, online);
  } else {
    sendStatus("error_unknown_action");
  }
  if (decrypted) wipe(command);
  cJSON_Delete(command);
}

void Setup::provision(const cJSON *command, bool online) {
  const char *access = text(command, "access_token");
  const char *refresh = text(command, "refresh_token");
  const char *type = text(command, "token_type");
  if (!cJSON_IsString(cJSON_GetObjectItemCaseSensitive(command, "ssid")) ||
      !cJSON_IsString(cJSON_GetObjectItemCaseSensitive(command, "password")) ||
      !access || !*access || !refresh || !*refresh || !type ||
      strcmp(type, "device") != 0) {
    sendStatus("error_missing_credentials");
    return;
  }
  if (provisionReady_ || provisionInFlight_) {
    sendStatus("error_operation_in_progress");
    return;
  }
  uint32_t generation = session_.markProvisioning();
  if (!generation) {
    sendStatus("error_pairing_confirm_required");
    return;
  }
  provisionGeneration_ = generation;
  sendStatus("wifi_connecting", generation);
  if (!online) {
    sendStatus("wifi_failed", generation);
    return;
  }
  sendStatus("wifi_connected", generation);
  const char *noise = text(command, "noise_host");
  const char *api = text(command, "api_url_v2");
  pending_.accessToken = access;
  pending_.refreshToken = refresh;
  pending_.noiseHost = noise ? noise : "";
  pending_.apiUrlV2 = api && strncmp(api, "https://", 8) == 0 ? api : "";
  provisionReady_ = true;
}

void Setup::sendStatus(const char *status, uint32_t generation) {
  std::string envelope;
  if (session_.sealStatus(status, generation, envelope)) {
    transport_.sendPackets(encodeChunks(envelope, transport_.mtu()));
    return;
  }
  if (generation || plaintextBlocked_ || !listed(status, kPlaintextStatuses)) return;
  transport_.sendPackets({status});
}

void Setup::sendJson(const std::string &json) {
  transport_.sendPackets(encodeChunks(json, transport_.mtu()));
}

void Setup::sendEncrypted(const std::string &json) {
  std::string envelope;
  if (session_.sealJson(json, 0, envelope)) sendJson(envelope);
}

}  // namespace muse
