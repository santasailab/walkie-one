// Plays the Muse phone app against muse::Setup using Meta's published
// community_app_v5 vector (muse-gadget-sdk linux/tests/vectors).
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include "cJSON.h"
#include "mbedtls/gcm.h"
#include "muse_pairing.h"

namespace {

const char *kMobilePub =
    "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU";
const char *kDevicePub =
    "BHzyexiNA09-ilI4AwS1GsPAiWnid_IbNaYLSPxHZpl4B3dVENuO0EApPZrGn3Qw27p9reY86YIpngS3nSJ4c9E";
const char *kMobileNonce = "AAECAwQFBgcICQoLDA0ODw";
const char *kDeviceNonce = "EBESExQVFhcYGRobHB0eHw";
const char *kTranscriptHash = "18Rs196tzddPGuj18pZ8rWObWYpwoDUIwocjnUASj_0";
const char *kSessionId = "z0eNLGw5mczvD4a1F2ubSQ";
const char *kFinishedCiphertext = "uwNX3KEkix5T6nWce3s2SKPPh-5E31x-UIHFSK6MpMUg562B";
const char *kFinishedTag = "zT4kU6XK-rM7rvt3VEITLg";
const char *kMobileTxHex = "ee25e7f1eb3c05cc8465634e5f634deed858862b1c4c35fa434dc926f7facaeb";
const char *kMobileRxHex = "9abb2954b05e2f5f7112e9bfd41fd970602e27ed07453c1cff51f575de5d0b4e";

uint32_t clockMs = 1000;
uint32_t now() { return clockMs; }
void random(uint8_t *out, size_t len) {
  for (size_t i = 0; i < len; i++) out[i] = uint8_t(i * 7 + 3);
}

std::vector<uint8_t> hex(const char *text) {
  std::vector<uint8_t> out;
  for (size_t i = 0; text[i] && text[i + 1]; i += 2) {
    unsigned value;
    sscanf(text + i, "%2x", &value);
    out.push_back(uint8_t(value));
  }
  return out;
}

struct Phone : muse::Transport {
  std::vector<std::string> received;  // complete messages from the Device
  muse::ChunkAssembler assembler;
  int disconnects = 0;
  void sendPackets(const std::vector<std::string> &packets) override {
    for (const std::string &packet : packets) {
      std::string message;
      if (assembler.feed(reinterpret_cast<const uint8_t *>(packet.data()),
                         packet.size(), message)) {
        received.push_back(message);
      }
    }
  }
  int mtu() override { return 185; }
  void disconnect(uint32_t) override { disconnects++; }
};

void write(muse::Setup &setup, const std::string &message, int mtu = 23) {
  for (const std::string &packet : muse::encodeChunks(message, mtu)) {
    setup.onWrite(reinterpret_cast<const uint8_t *>(packet.data()), packet.size());
  }
  setup.poll(true, "Home Wi-Fi");
}

std::string field(const std::string &json, const char *key) {
  cJSON *root = cJSON_Parse(json.c_str());
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
  std::string value = cJSON_IsString(item) ? item->valuestring
                      : cJSON_IsBool(item) ? (cJSON_IsTrue(item) ? "true" : "false")
                                           : "";
  cJSON_Delete(root);
  return value;
}

std::string gcm(bool encrypt, const char *keyHex, int direction, int counter,
                const std::string &in, std::vector<uint8_t> &tag) {
  std::vector<uint8_t> key = hex(keyHex);
  uint8_t iv[12] = {uint8_t(direction), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, uint8_t(counter)};
  std::string aad = std::string("hatch-link ble setup v1|") + kSessionId + "|" +
                    (direction == 0 ? "m2d" : "d2m") + "|" + std::to_string(counter);
  std::string out(in.size(), '\0');
  mbedtls_gcm_context context;
  mbedtls_gcm_init(&context);
  assert(mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key.data(), 256) == 0);
  const auto *a = reinterpret_cast<const unsigned char *>(aad.data());
  const auto *i = reinterpret_cast<const unsigned char *>(in.data());
  auto *o = reinterpret_cast<unsigned char *>(&out[0]);
  int rc = encrypt ? mbedtls_gcm_crypt_and_tag(&context, MBEDTLS_GCM_ENCRYPT, in.size(),
                                               iv, 12, a, aad.size(), i, o, 16, tag.data())
                   : mbedtls_gcm_auth_decrypt(&context, in.size(), iv, 12, a, aad.size(),
                                              tag.data(), 16, i, o);
  mbedtls_gcm_free(&context);
  assert(rc == 0);
  return out;
}

// What the phone reads out of the Device's next encrypted record.
std::string opened(const std::string &envelope, int counter) {
  assert(field(envelope, "type") == "pairing_encrypted");
  assert(field(envelope, "session_id") == kSessionId);
  assert(field(envelope, "counter") == std::to_string(counter));
  std::vector<uint8_t> sealed, tag;
  assert(muse::unb64url(field(envelope, "ciphertext").c_str(), sealed, 16384));
  assert(muse::unb64url(field(envelope, "tag").c_str(), tag));
  return gcm(false, kMobileRxHex, 1, counter, std::string(sealed.begin(), sealed.end()), tag);
}

std::string sealed(const std::string &plaintext, int counter) {
  std::vector<uint8_t> tag(16);
  std::string ciphertext = gcm(true, kMobileTxHex, 0, counter, plaintext, tag);
  return std::string("{\"action\":\"pairing_encrypted\",\"session_id\":\"") + kSessionId +
         "\",\"counter\":\"" + std::to_string(counter) + "\",\"ciphertext\":\"" +
         muse::b64url(reinterpret_cast<const uint8_t *>(ciphertext.data()),
                      ciphertext.size()) +
         "\",\"tag\":\"" + muse::b64url(tag.data(), tag.size()) + "\"}";
}

muse::Identity identity() {
  return {"homelink-000001", "hatch-link:02:00:00:00:00:01", "02:00:00:00:00:01",
          "1.0.0", "mgst_test"};
}

void begin(muse::Setup &setup, Phone &phone) {
  std::vector<uint8_t> scalar = hex(
      "0000000000000000000000000000000000000000000000000000000000000002");
  std::vector<uint8_t> nonce;
  assert(muse::unb64url(kDeviceNonce, nonce));
  setup.session().fixNextKey(scalar.data(), nonce.data());
  write(setup, std::string("{\"action\":\"pairing_client_hello\",\"version\":5,") +
                   "\"pairing_auth\":\"none\",\"pairing_policy\":\"confirm_app\"," +
                   "\"mobile_pub\":\"" + kMobilePub + "\",\"mobile_nonce\":\"" +
                   kMobileNonce + "\"}");
  const std::string &ready = phone.received.back();
  assert(field(ready, "type") == "pairing_ready");
  assert(field(ready, "device_pub") == kDevicePub);
  assert(field(ready, "transcript_hash") == kTranscriptHash);
  assert(field(ready, "session_id") == kSessionId);
}

}  // namespace

int main() {
  // The full happy path, byte for byte against Meta's vector.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    write(setup, "{\"action\":\"get_device_info\"}");
    assert(field(phone.received.back(), "model") == "hatch_link");
    assert(field(phone.received.back(), "pairing_policy") == "confirm_app");
    assert(field(phone.received.back(), "network_ready") == "true");

    begin(setup, phone);
    // Sensitive commands in plaintext after a hello earn silence.
    size_t before = phone.received.size();
    write(setup, "{\"action\":\"provision_v2\"}");
    assert(phone.received.size() == before);

    write(setup, std::string("{\"action\":\"pairing_encrypted\",\"session_id\":\"") +
                     kSessionId + "\",\"counter\":\"0\",\"ciphertext\":\"" +
                     kFinishedCiphertext + "\",\"tag\":\"" + kFinishedTag + "\"}");
    std::string confirmed = opened(phone.received.back(), 0);
    assert(field(confirmed, "status") == "pairing_confirmed");
    assert(field(confirmed, "sdk_token") == "mgst_test");

    write(setup, sealed("{\"action\":\"wifi_scan\"}", 1));
    assert(opened(phone.received.back(), 1).find("\"ssid\":\"Home Wi-Fi\"") !=
           std::string::npos);

    write(setup, sealed("{\"action\":\"provision_v2\",\"ssid\":\"x\",\"password\":\"y\","
                        "\"access_token\":\"device-access\",\"refresh_token\":\"device-refresh\","
                        "\"token_type\":\"device\",\"noise_host\":\"hatch.metaaivm.com\","
                        "\"api_url_v2\":\"https://api.muse.ai\"}",
                        2));
    size_t n = phone.received.size();
    assert(field(opened(phone.received[n - 2], 2), "status") == "wifi_connecting");
    assert(field(opened(phone.received[n - 1], 3), "status") == "wifi_connected");

    muse::Provision provision;
    assert(setup.takeProvision(provision));
    assert(provision.accessToken == "device-access");
    assert(provision.refreshToken == "device-refresh");
    assert(provision.noiseHost == "hatch.metaaivm.com");
    assert(provision.apiUrlV2 == "https://api.muse.ai");
    assert(!setup.takeProvision(provision));
    setup.finishProvision(true);
    assert(field(opened(phone.received.back(), 4), "status") == "auth_ok");
  }

  // A forged record drops the session and the link, telling the phone nothing.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    begin(setup, phone);
    size_t before = phone.received.size();
    write(setup, std::string("{\"action\":\"pairing_encrypted\",\"session_id\":\"") +
                     kSessionId + "\",\"counter\":\"0\",\"ciphertext\":\"" +
                     kFinishedCiphertext + "\",\"tag\":\"AAAAAAAAAAAAAAAAAAAAAA\"}");
    assert(phone.received.size() == before && phone.disconnects == 1);
    assert(!setup.session().confirmed());
  }

  // Provisioning is refused until the phone proves the session.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    begin(setup, phone);
    write(setup, std::string("{\"action\":\"pairing_encrypted\",\"session_id\":\"") +
                     kSessionId + "\",\"counter\":\"0\",\"ciphertext\":\"" +
                     kFinishedCiphertext + "\",\"tag\":\"" + kFinishedTag + "\"}");
    // A replayed counter is fatal too.
    write(setup, sealed("{\"action\":\"wifi_scan\"}", 0));
    assert(phone.disconnects == 1 && !setup.session().confirmed());
  }

  // The session expires; late records cannot open it.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    begin(setup, phone);
    clockMs += 60001;
    write(setup, std::string("{\"action\":\"pairing_encrypted\",\"session_id\":\"") +
                     kSessionId + "\",\"counter\":\"0\",\"ciphertext\":\"" +
                     kFinishedCiphertext + "\",\"tag\":\"" + kFinishedTag + "\"}");
    assert(phone.disconnects == 1);
  }

  // Wrong policy or version is refused in plaintext, before any key exists.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    write(setup, "{\"action\":\"pairing_client_hello\",\"version\":5,"
                 "\"pairing_auth\":\"none\",\"pairing_policy\":\"confirm_press\"}");
    assert(phone.received.back() == "error_pairing_invalid_hello");
  }

  // Deep nesting from a stranger in range is refused before parsing.
  {
    Phone phone;
    muse::Setup setup(phone, identity(), now, random);
    write(setup, std::string(4000, '[') + std::string(4000, ']'));
    assert(phone.received.empty());
    write(setup, "{\"action\":\"get_device_info\"}");
    assert(field(phone.received.back(), "type") == "device_info");
  }

  // Framing round trip at the smallest MTU, and a 255-chunk ceiling.
  {
    std::string big(4000, 'x');
    muse::ChunkAssembler assembler;
    std::string message;
    for (const std::string &packet : muse::encodeChunks(big, 23)) {
      assert(packet.size() <= 20);
      if (assembler.feed(reinterpret_cast<const uint8_t *>(packet.data()), packet.size(),
                         message)) {
        break;
      }
    }
    assert(message == big);
    assert(muse::encodeChunks(std::string(20000, 'x'), 23).empty());
  }
  return 0;
}
