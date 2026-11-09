/*
 * relay_client — the handheld's production firmware, talking to cloud/relay.py.
 *
 * Contract: schema/events.json in this repository. Text frames are that JSON.
 * Binary frames are raw PCM16 mono 24 kHz little-endian microphone audio up,
 * and MP3 (MPEG-2 Layer III, mono, 24 kHz) replies down, one stream per span. WebSocket already separates the two,
 * so there is no envelope and no base64 anywhere.
 *
 *   Device and Link each dial the cloud relay. It routes live frames, while
 *   bounded encrypted mailbox text remains in the control plane until exact
 *   playback or expiry.
 *
 * Build - NOTE the FQBN and OPI PSRAM option:
 *   arduino-cli compile --fqbn esp32:esp32:esp32s3 \
 *     --board-options PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB firmware/relay_client
 *   arduino-cli upload -p <port> --fqbn esp32:esp32:esp32s3 \
 *     --board-options PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB firmware/relay_client
 *
 * Commands (115200 baud): h for the list.
 */

#include <Arduino.h>
#include <Network.h>
#include <NetworkClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <driver/i2s_std.h>          // ESP-IDF 5.x API - NOT driver/i2s.h
#include <mbedtls/base64.h>
#include <mbedtls/x509.h>
#include <esp_task_wdt.h>
#include <esp_random.h>
#include <esp_mac.h>
#include <esp_cpu.h>
#include <esp_netif.h>
#include <esp_heap_caps.h>
#include <lwip/ip_addr.h>
#include <time.h>
#include <esp_sntp.h>
#include <new>
#include "config.h"
#include "board_profile.h"
#ifdef RELAY_BOARD_06
#include <driver/i2s_pdm.h>
#include <driver/gpio.h>
#include <Wire.h>
#include "battery_gauge.h"
#include "battery_watch.h"
#include "power_status.h"
static PowerStatus board06Power;
static BatteryWatch batteryWatch;
#include <atomic>
#include "pcb06_capture_spool.h"
#include "pcb06_button_cues.h"
#include "pcb06_button_sounds.h"
#include "pcb06_ptt_player.h"
#include "pcb06_ptt_sounds.h"
#include "pcb06_ota.h"
static OtaPolicy board06Ota{OtaRecord{}};
static pcb06_ota::Image board06OtaImage{};  // latest offered image; RAM only
static bool board06OtaHasImage = false;
static bool board06OtaFresh = false;     // the relay offered on this connection
static bool board06OtaUnproven = false;  // first run of a new image
static bool board06QuietBoot = false;    // started by an update, not a person
static uint32_t board06LastUseMs = 0, board06RegisteredAt = 0;
// Keep a new image "pending verify" until it reaches the relay
// (board06OtaProve); Arduino would otherwise accept it at boot.
extern "C" bool verifyRollbackLater() { return true; }
#endif
#include "src/audio/mp3_codec.h"
#include "volume_button.h"
#include "volume_store.h"
#include "repaired_speaker.h"
#include "pdm_speaker.h"
#include "status_led.h"
#include "playback_state.h"
#include "long_hold.h"
#include "button_map.h"
#include "endpoint_availability.h"
#include "listen_gesture.h"
#include "played_receipt.h"
#include "websocket_reader.h"
#include "mic_filter.h"
#include "reconnect_policy.h"
#include "data_path_watch.h"
#include "socket_timeouts.h"
#include "boot_report.h"
#include "clock_seed.h"
#include "ptt_gesture.h"
#ifndef RELAY_CELLULAR  // Muse pairing needs Bluetooth and Wi-Fi; not the legacy cellular board
#include "wifi_settings.h"
#include "wifi_password.h"
#include "muse_pairing.h"
#include "muse_ble.h"
#include <cJSON.h>
#include <esp_mac.h>
#endif

#ifdef RELAY_CELLULAR
#include "cellular.h"
#include "cellular_policy.h"  // the power-off budget a restart waits for
#ifdef RELAY_BOARD_06
// tools/build_pcb06.py --version sets the release version; hello reports it.
#ifndef RELAY_FW_SEMVER
#define RELAY_FW_SEMVER "0.9.9"
#endif
#define FW_VERSION RELAY_FW_SEMVER "-pcb06"
#else
#define FW_VERSION "0.6.3-cellular"
#endif
#elif defined(RELAY_ASSEMBLED_S3)
#ifdef RELAY_PDM_SPEAKER
#define FW_VERSION "0.8.4-pcb-pdm"
#elif defined(RELAY_REPAIRED_QUIET_AUDIO)
#define FW_VERSION "0.7.4-pcb-quiet"
#else
#define FW_VERSION "0.7.4-pcb-wifi"
#endif
#else
#define FW_VERSION "0.5.4-relay"
#endif


// COMMON CATHODE, confirmed on hardware: a colour lights when its pin is HIGH.
// Runtime-switchable so the other wiring can be tried without unsoldering.
#ifdef RELAY_BOARD_06
static bool ledCommonAnode = true;
#else
static bool ledCommonAnode = false;
#endif

static const int SAMPLE_RATE_HZ = 24000;
#ifdef RELAY_BOARD_06
static const uint32_t MAX_RECORDING_MS = Pcb06CaptureSpool::kMaxCaptureMs;
#else
static const uint32_t MAX_RECORDING_MS = 15 * 60 * 1000;
#endif
static const uint32_t MAX_RECORDING_SAMPLES = SAMPLE_RATE_HZ * (MAX_RECORDING_MS / 1000);
#if defined(RELAY_PDM_SPEAKER) && (defined(RELAY_REPAIRED_QUIET_AUDIO) || !defined(RELAY_ASSEMBLED_S3))
#error "RELAY_PDM_SPEAKER is only for the repaired assembled unit, without RELAY_REPAIRED_QUIET_AUDIO"
#endif
// MAX98357A supports 48 kHz, not the 24 kHz audio rate. Duplicate each input
// sample into two output frames; each frame carries identical L/R samples.
#ifdef RELAY_REPAIRED_QUIET_AUDIO
#ifndef RELAY_ASSEMBLED_S3
#error "The repaired quiet profile is only qualified for the assembled Wi-Fi unit"
#endif
static const int SPEAKER_RATE_HZ = 32000;
#else
static const int SPEAKER_RATE_HZ = 48000;
#endif

// ---- Endpoint -------------------------------------------------------------
// One host for every customer. The credential is the only thing that differs,
// and it is what decides whose Mac this device reaches.
static const char *HOST = RELAY_HOST;
static const char *PATH = RELAY_PATH;
static const int   PORT = 443;

// Every unit receives its credential after the common firmware image is
// flashed. Preferences stores it in the ESP32's NVS partition, so it survives
// resets and firmware updates without baking a customer secret into config.h.
// The namespace and key must stay stable across releases.
static Preferences provisionStore;
#ifndef RELAY_CELLULAR
static WifiPasswordInput wifiPassword;
#endif

static const int kButtonInputs[] = BUTTON_INPUT_PINS;
static ButtonMap buttons{PIN_BTN_SPEAK, PIN_BTN_LISTEN, PIN_BTN_VOLUME_UP, PIN_BTN_VOLUME_DOWN};
static String relayCredential;
static String relayPairId;
static const size_t CREDENTIAL_MAX = 128;   // matches cloud/relay.py
static const size_t PAIR_ID_MAX = 32;       // matches cloud/relay.py

// The relay compares the whole Authorization header against the credential.
// No "Bearer " prefix - it tolerates one, but sending it is not the contract.

// ISRG Root X2, the trust anchor for this endpoint's chain. Pinned rather than
// setInsecure(): the far end of this socket runs shell commands on somebody's
// Mac, so an unauthenticated TLS session would let anyone on the path inject
// one. The chain is leaf -> YE2 -> ISRG Root YE -> X2, so X2 verifies it in two
// hops.
//
// ISRG Root X1 follows as a second anchor. Firmware updates arrive over this
// same socket, so a single anchor has no way back the day the server's chain
// stops ending at it (an RSA certificate, a retired cross-signature). Both are
// the same CA's public roots; the hostname check is unchanged.
static const char *RELAY_CA = R"CERT(
-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFfLn
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
)CERT";

// ---- Playback ----
// Production replies are staged completely in PSRAM before the first sample
// reaches I2S. Cellular stalls can make playback start later, but can no longer
// empty the speaker in the middle of a word. The span holds the compressed MP3
// reply; it is validated in full at audio.end and decoded on demand by
// audioTask. The cap matches the cloud's synthesized-speech limit.
static const size_t PLAYBACK_CAP_BYTES = Mp3Playback::kMaxBytes;
static uint8_t *playbackSpan = nullptr;
static Mp3Playback *mp3Playback = nullptr;
// Held by audioTask while it decodes, so a new span never reuses its source.
static SemaphoreHandle_t playbackMutex = nullptr;
static PlaybackState playback;
static volatile size_t playbackBytes = 0;     // MP3 received for this span
static volatile int32_t playbackPeak = 0;     // largest |sample| in the staged span
static volatile bool playbackReceiving = false;
static volatile bool playbackOverflow = false;

// Serial tone and speaker diagnostics. PSRAM: NimBLE needs internal RAM for
// Muse pairing, and a 96 KB internal ring left too little to start it.
static const size_t RING_SZ = 49152;
static int16_t *spkRing = nullptr;
static volatile size_t ringHead = 0, ringTail = 0;
static volatile uint32_t underruns = 0;
#ifdef RELAY_PDM_SPEAKER
static int volume = 10; // PdmSpeaker puts each reply's peak at its approved level at 10.
#elif defined(RELAY_REPAIRED_QUIET_AUDIO)
static int volume = 1; // Audition2/sample4: gain2 x volume1/10 = amplitude/5.
#else
static int volume = 5;                       // 0..10
#endif
static bool muted = false;
static VolumeStore volumeStore;
// Lowered on a low battery so loud replies cannot brown out the modem
// (battery_watch.h). The chosen `volume` is kept and returns on charge.
// ponytail: replies and tones only; button clicks and the Talk sound are
// short and keep `volume`. Cap them too if they show up in the sag test.
static volatile int volumeCeiling = 10;
static bool volumeFeedback = false;
static uint32_t volumeChangedAt = 0;
static int AMP_CAP_PCT = 100;
static int gainEighths = 16;
#ifdef RELAY_BOARD_06
static Pcb06CueQueue board06CueQueue;
static Pcb06CuePlayer board06CuePlayer;  // Only audioTask advances this cursor.
static Pcb06PttPlayer board06PttPlayer(
    pcb06_ptt_sounds::kPress.data(), pcb06_ptt_sounds::kPressSamples,
    pcb06_ptt_sounds::kRelease.data(), pcb06_ptt_sounds::kReleaseSamples,
    pcb06_ptt_sounds::kLoop.data(), pcb06_ptt_sounds::kLoopSamples);
static bool board06PttOwnsOutput = false; // Protected by playbackMutex.
static Pcb06ButtonSample board06ButtonSample;
static bool board06ListenPending = false, board06ListenWaitForCue = false;
static bool board06PlayCueAccepted = false;
static uint32_t board06ListenRequestedAt = 0;
#endif

static i2s_chan_handle_t txChan = nullptr;
static i2s_chan_handle_t rxChan = nullptr;
static SemaphoreHandle_t micMutex = nullptr;
static bool micCapturing = false;
struct MicLock {
  MicLock() { xSemaphoreTake(micMutex, portMAX_DELAY); }
  ~MicLock() { xSemaphoreGive(micMutex); }
};

// Current voice captures clipped 0.65-0.96% of their samples at shift 11.
// One more bit preserves the measured speech range with 6 dB more headroom.
#ifdef RELAY_BOARD_06
static const int MIC_SHIFT = 0;  // PDM-to-PCM hardware returns signed PCM16.
#else
static const int MIC_SHIFT = 12;
#endif
static MicHighPass micHighPass;
static int32_t micPeak = 0;
static uint32_t micFramesSent = 0;
// Per-recording capture health, printed with talk.end: samples actually read
// versus held time exposes dropped audio; RMS and clipping expose bad input.
static uint32_t micSamplesRead = 0, micClipped = 0;
static uint64_t micEnergy = 0;
// Frames go out 2048 samples at a time. The rest holds the first
// PTT_TALK_HOLD_MS of speech until the press proves to be more than a tap.
static const size_t MIC_FRAME = 2048;
static const size_t MIC_BATCH = 4 * MIC_FRAME;
static int16_t *micBatch = nullptr;  // PSRAM; internal RAM is kept for TLS and Bluetooth
static size_t micBatchFill = 0;
#ifdef RELAY_BOARD_06
static Pcb06CaptureSpool *captureSpool = nullptr;
static int16_t board06TxChunk[MIC_FRAME];  // Owned only by the network loop.
static std::atomic<bool> board06DmaOverflow{false};
static uint32_t board06LastMicData = 0;  // Protected by micMutex.
static size_t board06MicTrimRemaining = 0;  // Opening-cue frames; protected by micMutex.
static_assert(pcb06_ptt_sounds::kSampleRate == SAMPLE_RATE_HZ);
static void board06TransportFailed();
static bool IRAM_ATTR board06MicOverflow(i2s_chan_handle_t, i2s_event_data_t *, void *) {
  board06DmaOverflow.store(true, std::memory_order_relaxed);
  return false;
}
#endif

// ---- WebSocket ----
static WiFiClientSecure tls;
// 8 KB, down from 48 KB. That size existed to hold OpenAI's base64 audio
// messages; the relay protocol's largest frame is a 4096-byte MP3 chunk and
// its control JSON is well under 1 KB.
static const size_t WS_BUF = 8192;
static volatile uint32_t truncatedFrames = 0;
static uint8_t *wsBuf = nullptr;
static bool wsOpen = false;

// ---- Protocol state ----
static bool     playing        = false;      // staged span is draining to I2S
static bool     recording      = false;      // Speak held
static bool     talkSent       = false;      // this press became speech
static char     spanId[65]     = {0};        // id from the current audio.start
static PlayedReceipt playedReceipt;
static bool confirmsPlayedReceipts = false;
static char     pendingAck[65] = {0};        // ack after staged playback drains
static int      ledMissed      = 0;          // last digest
static EndpointAvailability endpointAvailability;

static void clearAvailability() {
  endpointAvailability.clear();
}
static uint32_t recStartMs     = 0;
static uint32_t lastStatusMs   = 0;
// The relay pings every 240 s and the radio wakes for that anyway, so the
// status report rides on a ping instead of waking the radio on its own.
// Owner screens drop a battery sample older than 20 minutes.
static const uint32_t STATUS_WITH_PING_MS = 12UL * 60 * 1000;
static_assert(STATUS_WITH_PING_MS + 240000 < 20UL * 60 * 1000,
              "status must stay fresher than the 20-minute battery display rule");
// Nothing to do: nap so the idle task halts the CPU until the next interrupt.
// ponytail: a fixed nap, not light sleep. Sleeping the chip needs the modem's
// UART sleep and RI wake first (firmware/todo.md, battery life on the 06 board).
static const uint32_t IDLE_NAP_MS = 10;
// Board-independent retry timing; the first boot dial is the only fast path.
static reconnect_policy::Policy reconnectPolicy;
// Restarts a bearer that reports data while the relay stays out of reach.
static DataPathWatch dataPath;
static_assert(kTlsCertificateRejected == MBEDTLS_ERR_X509_CERT_VERIFY_FAILED,
              "data_path_watch.h must match the pinned mbedTLS");
static bool wsReachedRelay = false;  // the last dial got as far as a TLS session
// The relay pings every 240 s. Silence well past that is not a quiet link, it
// is a black-holed one: a carrier NAT rebind leaves TCP "connected" here while
// nothing can cross, and tls.connected() cannot see the difference. Without
// this the first hint is the 15-minute status write failing its retransmits.
static const uint32_t RX_DEADLINE_MS = 390000;    // 1.5 * 240 s + slack
static uint32_t lastRxMs       = 0;
// Soft off is its own runtime: switching off or on restarts the firmware.
static RTC_NOINIT_ATTR uint32_t softOffMarker;
static RTC_NOINIT_ATTR BrownoutRecord brownoutRecord;
static bool     asleep         = false;      // this is the switched-off runtime
static volatile bool offRequested = false;   // close the socket, then restart off
// 06 battery: set by loop(), read by powerTask() while switched off.
static volatile bool batteryEmpty = false;    // latest gauge sample: empty, no USB
static volatile bool switchOnRefused = false; // loop() shows it in orange
static uint32_t framesRecvd    = 0;

static void ledSet(Rgb color);
static void powerDownCue();
static void ledService();
static int networkRssi();
static bool networkConnected();
static void networkService();
static void abandonPlayback();
static void retryPlayback();

// ESP-IDF only promises true randomness from esp_random() while Wi-Fi or
// Bluetooth RF is on; the cellular-only 06 board never turns either on, so the
// RNG may be pseudo-random. Fold in the factory MAC (unique per unit) and the
// cycle counter (loop phase differs per unit) so a relay restart cannot give
// the fleet one shared reconnect delay. murmur3's finalizer spreads every bit.
static uint32_t jitterRandom() {
  uint8_t mac[6] = {0};
  esp_efuse_mac_get_default(mac);
  uint32_t x = esp_random() ^ esp_cpu_get_cycle_count() ^
               ((uint32_t(mac[3]) << 16 | uint32_t(mac[4]) << 8 | mac[5]) * 0x9E3779B9u);
  x ^= x >> 16; x *= 0x85EBCA6Bu; x ^= x >> 13; x *= 0xC2B2AE35u; x ^= x >> 16;
  return x;
}

// Failed dials and drops share one clock/entropy-driven policy.
static void scheduleRetry(int closeCode = 0) {
  uint32_t wait = reconnectPolicy.retry(millis(), jitterRandom(), closeCode);
  Serial.printf("[ws] retry in %lu ms\n", (unsigned long)wait);
}

// Spread IP recovery without cancelling a slower authorization retry.
static void retryNow() {
  reconnectPolicy.networkAvailable(millis(), jitterRandom());
}

// A dial never reached the relay, or its open socket went silent. Redialing
// over a dead modem session cannot recover, so a sustained run restarts it.
static void dataPathUnreachable() {
#ifdef RELAY_CELLULAR
  if (!networkConnected() || !dataPath.unreachable(millis())) return;
  Serial.println("[net] relay unreachable over a live bearer; restarting the modem");
  cellularRestart();
#endif
}

// ---------------------------------------------------------------------------
// Factory provisioning
// ---------------------------------------------------------------------------

static bool pairChar(char c) {
  return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-';
}

// This validates the credential's public shape before it is committed. The
// relay still verifies the HMAC signature; the signing key never belongs on a
// device or warehouse station.
static bool parseDeviceCredential(const String &credential, String &pairId) {
  if (credential.length() == 0 || credential.length() > CREDENTIAL_MAX ||
      !credential.startsWith("v1.")) return false;

  int pairEnd = credential.indexOf('.', 3);
  if (pairEnd < 4) return false;
  pairId = credential.substring(3, pairEnd);
  if (pairId.length() == 0 || pairId.length() > PAIR_ID_MAX) return false;
  for (size_t i = 0; i < pairId.length(); i++) {
    if (!pairChar(pairId[i])) return false;
  }

  const String rolePrefix = ".device.";
  if (!credential.substring(pairEnd).startsWith(rolePrefix)) return false;
  String signature = credential.substring(pairEnd + rolePrefix.length());
  if (signature.length() != 43) return false;  // base64url SHA-256, no padding
  for (size_t i = 0; i < signature.length(); i++) {
    char c = signature[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
  }
  return true;
}

static void printProvisioningStatus() {
  if (relayCredential.length()) {
    Serial.printf("[provision] provisioned pair=%s\n", relayPairId.c_str());
  } else {
    Serial.println("[provision] UNPROVISIONED - send PROVISION <device-credential>");
  }
}

// A stored map lives beside the credential, so firmware updates keep it.
static void loadButtonMap() {
  const String stored = provisionStore.getString("buttons", "");
  if (stored.length() &&
      !parseButtonMap(stored.c_str(), kButtonInputs,
                      sizeof(kButtonInputs) / sizeof(kButtonInputs[0]), buttons)) {
    Serial.println("[buttons] ignoring an invalid stored map");
  }
  char text[32];
  formatButtonMap(buttons, text, sizeof(text));
  Serial.printf("[buttons] talk play volume-up volume-down: %s%s\n", text,
                stored.length() ? " (stored)" : " (board default)");
}

// `BUTTONS` prints the map, `BUTTONS DEFAULT` forgets the stored one, and
// `BUTTONS <talk> <play> <volume-up> <volume-down>` stores a new one. A change
// applies after the restart that follows.
static void buttonsCommand(String args) {
  args.trim();
  if (!args.length()) {
    char text[32];
    formatButtonMap(buttons, text, sizeof(text));
    Serial.printf("[buttons] %s\n", text);
    return;
  }
  if (args.equalsIgnoreCase("DEFAULT")) {
    provisionStore.remove("buttons");
  } else {
    ButtonMap map{};
    if (!parseButtonMap(args.c_str(), kButtonInputs,
                        sizeof(kButtonInputs) / sizeof(kButtonInputs[0]), map)) {
      Serial.println("[buttons] ERROR expected BUTTONS <talk> <play> <volume-up> <volume-down>"
                     " using this board's button inputs, -1 for none");
      return;
    }
    char text[32];
    formatButtonMap(map, text, sizeof(text));
    if (!provisionStore.putString("buttons", text)) {
      Serial.println("[buttons] ERROR could not save");
      return;
    }
  }
  Serial.println("[buttons] saved; restarting");
  Serial.flush();
  modemOffForRestart();
  ESP.restart();
}

// The stored volume lives beside the credential; without one the board default
// above stands.
static void loadVolume() {
  volume = VolumeStore::restore(provisionStore.getUChar("volume", 255), volume);
  volumeStore.loaded(volume);
}

// A flash write stalls the audio paths, so the volume is written once it has
// rested and never during Talk or playback.
static void volumeSaveService() {
  if (recording || playing || !volumeStore.due(volume, millis())) return;
  const bool saved = provisionStore.putUChar("volume", uint8_t(volume)) != 0;
  if (!saved) Serial.println("[vol] could not save");
  volumeStore.written(saved, millis());
}

static void loadProvisioning() {
  if (!provisionStore.begin("relay", false)) {
    Serial.println("[provision] ERROR could not open NVS");
    return;
  }
  relayCredential = provisionStore.getString("credential", "");
  if (relayCredential.length() &&
      !parseDeviceCredential(relayCredential, relayPairId)) {
    // Never put a malformed value on the Authorization header. Keep the bad
    // bytes out of logs too: credentials are not serial-console diagnostics.
    provisionStore.remove("credential");
    relayCredential = "";
    relayPairId = "";
    Serial.println("[provision] invalid stored credential removed");
  }
  printProvisioningStatus();
}

static void provision(const String &candidate) {
  String credential = candidate;
  credential.trim();
  String pairId;
  if (!parseDeviceCredential(credential, pairId)) {
    Serial.println("[provision] ERROR invalid device credential");
    return;
  }

  size_t written = provisionStore.putString("credential", credential);
  if (written == 0 || provisionStore.getString("credential", "") != credential) {
    Serial.println("[provision] ERROR NVS write failed");
    return;
  }

  relayCredential = credential;
  relayPairId = pairId;
  tls.stop();
  wsOpen = false;
  clearAvailability();
  abandonPlayback();
  playedReceipt.clear();
  reconnectPolicy = reconnect_policy::Policy();
  retryNow();
  Serial.printf("[provision] OK pair=%s\n", relayPairId.c_str());
}

// ---------------------------------------------------------------------------
// I2S
// ---------------------------------------------------------------------------

#ifdef RELAY_BOARD_06
static bool board06AmpOn = false;
// Caller owns playbackMutex after audio tasks start.
static void board06SetAmp(bool enabled) {
  digitalWrite(PIN_AMP_SD, enabled ? HIGH : LOW);
  board06AmpOn = enabled;
}

static bool board06DrainSpeaker() {
  static const int16_t silence[240 * 4] = {};
  bool drained = true;
  for (int i = 0; drained && i < 5; ++i) {
    size_t written = 0;
    drained = i2s_channel_write(txChan, silence, sizeof(silence), &written, 100) == ESP_OK
        && written == sizeof(silence);
  }
  board06SetAmp(false);
  return drained;
}

// The opening sound means the microphone is open. A press cannot become
// speech with the relay away, or while an earlier request is still uploading
// (startRecording refuses both), so that press is silent from down to up.
// Decided once, on the press: recovery halfway must not start a late sound.
static bool board06TalkRefused(bool down) {
  static bool wasDown = false, refused = false;
  if (!down) refused = false;
  else if (!wasDown) refused = !wsOpen || (recording && !micCapturing);
  wasDown = down;
  return refused;
}

// Physical release is observed here even while loop() waits on the network.
// Reply-owned DMA must first pass through the existing pause/drain handoff.
static void board06ObservePtt() {
  static uint32_t lastFlushEpoch = UINT32_MAX;
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  const bool down = digitalRead(buttons.talk) == LOW;
  // Every sample, not only when audible: it tracks the press edge itself.
  const bool refused = board06TalkRefused(down);
  board06PttPlayer.observe(down, muted || volume == 0 || offRequested || refused, millis());
  if (board06PttPlayer.flushing() && (board06PttOwnsOutput || !playing)) {
    if (lastFlushEpoch != board06PttPlayer.flushEpoch()) {
      lastFlushEpoch = board06PttPlayer.flushEpoch();
      board06CueQueue.cancel();
      board06CuePlayer.clear();
    }
    board06SetAmp(false);
  }
  xSemaphoreGive(playbackMutex);
}

static bool board06PlayPttChunk() {
  static uint32_t lastProgress = 0;
  static Pcb06PttPlayer::Phase lastPhase = Pcb06PttPlayer::Phase::Idle;
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  if (!board06PttPlayer.active() || (playing && !board06PttOwnsOutput)) {
    xSemaphoreGive(playbackMutex);
    return false;
  }
  if (!board06PttOwnsOutput) lastProgress = millis();
  const uint8_t *data = board06PttPlayer.data(volume);
  if (!data || !board06PttPlayer.bytes()) {
    board06PttOwnsOutput = false;
    board06SetAmp(false);
    xSemaphoreGive(playbackMutex);
    return false;
  }
  board06PttOwnsOutput = true;
  board06SetAmp(board06PttPlayer.ampAllowed());
  size_t written = 0;
  const auto error = i2s_channel_write(txChan, data, board06PttPlayer.bytes(), &written, 10);
  const bool valid = board06PttPlayer.commit(written);
  if (written && valid) lastProgress = millis();
  if (!valid || (error != ESP_OK && error != ESP_ERR_TIMEOUT)
      || uint32_t(millis() - lastProgress) >= 250 || board06PttPlayer.overdue(millis())) {
    board06PttPlayer.fail();
    Serial.println("[ptt-sound] output failed");
  }
  if (!board06PttPlayer.active()) {
    board06PttOwnsOutput = false;
    board06SetAmp(false);
  }
  const auto phase = board06PttPlayer.phase();
  if (phase != lastPhase) {
    Serial.printf("[ptt-sound] phase=%d volume=%d\n", int(phase), volume);
    lastPhase = phase;
  }
  xSemaphoreGive(playbackMutex);
  if (!written) vTaskDelay(1);
  return true;
}

// One short write per audio-task iteration. Cues have their own cursor and
// never advance a reply, acknowledge it, or enter the microphone PCM stream.
static bool board06PlayCueChunk() {
  static uint32_t startedAt = 0;
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  if (!board06CuePlayer.active()) {
    Pcb06CueRequest cue;
    if (!board06CueQueue.pop(cue, millis()) || muted || volume == 0 || offRequested) {
      if (muted || volume == 0 || offRequested) board06CueQueue.cancel();
      xSemaphoreGive(playbackMutex);
      return false;
    }
    const bool action = cue.kind == Pcb06CueKind::Talk || cue.kind == Pcb06CueKind::Play;
    board06CuePlayer.start(cue, action ? pcb06_sounds::kPlayTalk : pcb06_sounds::kVolumeTick,
        action ? pcb06_sounds::kPlayTalkSamples : pcb06_sounds::kVolumeTickSamples,
        board06PttPlayer.active() ? 0 : Pcb06CuePlayer::kDrainSamples);
    startedAt = millis();
  }
  if (muted || volume == 0 || offRequested) board06CueQueue.cancel();
  if (!board06CueQueue.valid(board06CuePlayer.request())) {
    board06CuePlayer.cancel();
    board06SetAmp(false);
  }
  board06SetAmp(!board06CuePlayer.silent());
  const uint8_t *data = board06CuePlayer.data();
  size_t written = 0;
  const auto error = i2s_channel_write(txChan, data, board06CuePlayer.bytes(), &written, 10);
  const bool validWrite = board06CuePlayer.commit(written);
  if (!validWrite || (error != ESP_OK && error != ESP_ERR_TIMEOUT)
      || uint32_t(millis() - startedAt) >= 250) {
    board06CueQueue.cancel();
    board06CuePlayer.clear();
    board06SetAmp(false);
    Serial.println("[cue] output failed");
  } else if (!board06CuePlayer.active()) {
    if (!board06PttPlayer.active()) board06SetAmp(false);
  }
  xSemaphoreGive(playbackMutex);
  if (!written) vTaskDelay(1);
  return true;
}
#endif

static bool setupI2S() {
#ifdef RELAY_BOARD_06
  constexpr i2s_port_t speakerPort = I2S_NUM_1;
#else
  constexpr i2s_port_t speakerPort = I2S_NUM_0;
#endif
  i2s_chan_config_t chan_cfg =
      I2S_CHANNEL_DEFAULT_CONFIG(speakerPort, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = 8;
  chan_cfg.dma_frame_num = 240;
  chan_cfg.auto_clear = true;

  if (i2s_new_channel(&chan_cfg, &txChan, nullptr) != ESP_OK) {
    Serial.println("[i2s] i2s_new_channel failed");
    return false;
  }
  // STEREO with the sample in both slots: the MAX98357A picks its channel from
  // the voltage on SD, so a left-only stream can be silently discarded.
  i2s_std_config_t std_cfg = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SPEAKER_RATE_HZ),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = (gpio_num_t)PIN_AMP_BCLK,
          .ws   = (gpio_num_t)PIN_AMP_LRC,
          .dout = (gpio_num_t)PIN_AMP_DIN,
          .din  = I2S_GPIO_UNUSED,
          .invert_flags = {false, false, false},
      },
  };
  if (i2s_channel_init_std_mode(txChan, &std_cfg) != ESP_OK) return false;
  if (i2s_channel_enable(txChan) != ESP_OK) return false;
  Serial.printf("[i2s] TX up: %d Hz, BCLK=%d WS=%d DATA=%d\n",
                SPEAKER_RATE_HZ, PIN_AMP_BCLK, PIN_AMP_LRC, PIN_AMP_DIN);
  return true;
}

static bool setupMic() {
#ifdef RELAY_BOARD_06
  i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
#else
  i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
#endif
  cfg.dma_desc_num = 8;
  cfg.dma_frame_num = 240;
  if (i2s_new_channel(&cfg, nullptr, &rxChan) != ESP_OK) return false;

#ifdef RELAY_BOARD_06
  i2s_pdm_rx_config_t pdm = {
      .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
      .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg = {
          .clk = (gpio_num_t)PIN_MIC_CLK,
          .din = (gpio_num_t)PIN_MIC_DOUT,
          .invert_flags = {false},
      },
  };
  const auto initialized = i2s_channel_init_pdm_rx_mode(rxChan, &pdm);
#else
  i2s_std_config_t std = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = (gpio_num_t)PIN_MIC_BCLK,
          .ws   = (gpio_num_t)PIN_MIC_LRCL,
          .dout = I2S_GPIO_UNUSED,
          .din  = (gpio_num_t)PIN_MIC_DOUT,
          .invert_flags = {false, false, false},
      },
  };
  std.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
  const auto initialized = i2s_channel_init_std_mode(rxChan, &std);
#endif
  if (initialized != ESP_OK) {
    i2s_del_channel(rxChan);
    rxChan = nullptr;
    return false;
  }
#ifdef RELAY_BOARD_06
  i2s_event_callbacks_t callbacks = {};
  callbacks.on_recv_q_ovf = board06MicOverflow;
  if (i2s_channel_register_event_callback(rxChan, &callbacks, nullptr) != ESP_OK) {
    i2s_del_channel(rxChan); rxChan = nullptr;
    return false;
  }
#endif
  // Configured but stopped: no microphone clocks or DMA capture at boot.
  Serial.printf("[mic] RX idle, shift=%d\n", MIC_SHIFT);
  return true;
}

static inline size_t ringAvail() {
  return (ringHead + RING_SZ - ringTail) % RING_SZ;
}

static void ringPush(const int16_t *src, size_t n) {
  for (size_t i = 0; i < n; i++) {
    size_t next = (ringHead + 1) % RING_SZ;
    if (next == ringTail) return;            // full: drop rather than overwrite
    spkRing[ringHead] = src[i];
    ringHead = next;
  }
}

// Throw away diagnostic audio still queued in the internal ring.
static void ringClear() {
  ringTail = ringHead;
}

// Only the loop task writes a span, and the audio task cannot read it until
// playback.ready is published by a fresh Listen gesture after audio.end. This
// keeps network timing out of the speaker path without locking every frame.
static void playbackClear() {
  // Wait for any decoder read to finish before reusing its source buffer.
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  playback.clear();
  playbackReceiving = false;
  playbackOverflow = false;
  playbackBytes = 0;
  playbackPeak = 0;
  xSemaphoreGive(playbackMutex);
}

static void playbackStart() {
  playbackClear();
  playbackReceiving = true;
}

static void playbackAppend(const uint8_t *src, size_t n) {
  if (!playbackReceiving || playbackOverflow) return;
  if (n > PLAYBACK_CAP_BYTES - playbackBytes) {
    playbackOverflow = true;
    Serial.printf("[rx] audio span exceeds %lu bytes - discarding\n",
                  (unsigned long)PLAYBACK_CAP_BYTES);
    return;
  }
  memcpy(playbackSpan + playbackBytes, src, n);
  playbackBytes += n;
}

static bool playbackCommit() {
  playbackReceiving = false;
  if (playbackOverflow) return false;
  // Decode every frame now: a damaged or truncated reply is retried instead of
  // lighting green and failing halfway through playback.
  size_t samples = 0;
  int32_t peak = 0;
  if (!mp3Playback->prepare(playbackSpan, playbackBytes, samples, peak,
                            []() { esp_task_wdt_reset(); })) {
    Serial.printf("[rx] invalid MP3 span (%lu bytes) - discarding\n",
                  (unsigned long)playbackBytes);
    return false;
  }
  playback.stage(samples);
  playbackPeak = peak;
  // Complete and safe to advertise, but still silent until Listen. Publishing
  // playback.ready here was the five-second dead-air bug: the Mac could only
  // begin this download after the button press.
  return true;
}

static bool playbackPlayStaged() {
  if (playedReceipt.pending()) return false;
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  const bool started = !recording && playback.play();
  if (started) playing = true;
  xSemaphoreGive(playbackMutex);
  return started;
}

// Drain the already submitted samples before opening the microphone. At most
// one DMA queue (60 ms on the slowest profile) remains audible after the press.
// The audio task holds this mutex across writes and cursor updates, so resume
// cannot skip a partial block or accept completion from an obsolete generation.
static void pausePlayback() {
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  if (playback.pause()) {
    static const int16_t silence[240 * 4] = {};
    for (int drain = 0; drain < 5; ++drain) {
      size_t written = 0;
      if (i2s_channel_write(txChan, silence, sizeof(silence), &written, 100) != ESP_OK
          || written != sizeof(silence)) {
        playback.staged = false;
        playback.failed = true;
        break;
      }
    }
    playing = false;
  }
#ifdef RELAY_BOARD_06
  board06SetAmp(false);
#endif
  xSemaphoreGive(playbackMutex);
}

static volatile uint32_t framesWritten = 0;
static volatile int32_t  peakWindow = 0;
static volatile int32_t  peakPreGain = 0;
static volatile uint32_t clipped = 0;

// Pinned to core 0 so TLS reads on core 1 cannot starve I2S. Production audio
// comes from the complete PSRAM span; the internal ring is only for diagnostics.
static void audioTask(void *) {
  static int16_t block[240 * 4];
  static int16_t decoded[241];
#ifdef RELAY_REPAIRED_QUIET_AUDIO
  static RepairedSpeaker profile;
  uint32_t profileSource = UINT32_MAX;
#endif
#ifdef RELAY_PDM_SPEAKER
  static PdmSpeaker pdm;
#endif
  for (;;) {
#ifdef RELAY_BOARD_06
    // Local feedback is the sole speaker exception during PTT capture.
    // Microphone DMA and filtering continue on their own higher-priority task.
    board06ObservePtt();
    if (board06PttPlayer.flushing() && board06PlayPttChunk()) continue;
    if (board06PlayCueChunk()) continue;
    if (board06PlayPttChunk()) continue;
#endif
    bool staged = playback.ready;
    uint32_t generation = playback.generation;
#ifdef RELAY_REPAIRED_QUIET_AUDIO
    const uint32_t source = staged ? generation : UINT32_MAX;
    if (source != profileSource) { profile.reset(); profileSource = source; }
    const RepairedSpeaker before = profile;
#endif
    const size_t played = playback.played;
    const size_t total = playback.samples;
    size_t avail = staged ? (total > played ? total - played : 0) : ringAvail();
    if (avail == 0) {
#ifdef RELAY_BOARD_06
      xSemaphoreTake(playbackMutex, portMAX_DELAY);
      if (board06AmpOn && !playback.ready && ringAvail() == 0) board06DrainSpeaker();
      xSemaphoreGive(playbackMutex);
#endif
#ifdef RELAY_PDM_SPEAKER
      // Idle at mid-level, not auto_clear's all-zero DATA: this amp's input
      // drifts for about two seconds after leaving all-zero, clipping the
      // start of every reply on one side. Paused while recording because DATA
      // couples into the mic lines.
      if (!recording) {
        for (size_t i = 0; i < 240; i++)
          pdm.encode(0, 1, volume, false, reinterpret_cast<uint16_t *>(block + i * 4));
        size_t wrote = 0;
        xSemaphoreTake(playbackMutex, portMAX_DELAY);
        const bool idle = !playback.ready && !recording;
        const bool wroteIdle = idle && i2s_channel_write(txChan, block, sizeof(block), &wrote, 20) == ESP_OK;
        xSemaphoreGive(playbackMutex);
        if (wroteIdle) continue;
      }
#endif
      vTaskDelay(1);
      continue;
    }

    size_t n = avail < 240 ? avail : 240;
#ifdef RELAY_REPAIRED_QUIET_AUDIO
    if (!staged) n -= n % 3; // Diagnostic producers append 240-sample blocks.
    if (!n) { vTaskDelay(1); continue; }
    const bool hasNext = avail > n;
    const size_t readCount = n + (hasNext ? 1 : 0);
#else
    const size_t readCount = n;
#endif
    if (staged) {
      xSemaphoreTake(playbackMutex, portMAX_DELAY);
      const bool valid = playback.ready && playback.generation == generation;
      const bool decodedOk = valid && mp3Playback->read(played, decoded, readCount);
      // prepare() decoded this span once already, so a failure here is a fault.
      if (valid && !decodedOk) { playback.ready = false; playback.failed = true; }
      xSemaphoreGive(playbackMutex);
      if (!decodedOk) { vTaskDelay(1); continue; }
    }
    for (size_t i = 0; i < n; i++) {
      int32_t raw;
      if (staged) {
        raw = decoded[i];
      } else {
        raw = spkRing[ringTail];
        ringTail = (ringTail + 1) % RING_SZ;
      }

      int32_t rawMag = raw < 0 ? -raw : raw;
      if (rawMag > peakPreGain) peakPreGain = rawMag;

#ifdef RELAY_REPAIRED_QUIET_AUDIO
      decoded[i] = static_cast<int16_t>(raw);
#elif defined(RELAY_PDM_SPEAKER)
      // ponytail: a partial-write retry re-encodes from the advanced modulator
      // state; that only changes the noise pattern, not the audio.
      // Staged replies are scaled to their own peak; the live ring uses a
      // typical cloud speech peak.
      pdm.encode(raw, staged ? playbackPeak : 12700, volume, muted,
                 reinterpret_cast<uint16_t *>(block + i * 4));
#else
      int32_t s = (raw * gainEighths) / 8;
      s = (s * min(volume, int(volumeCeiling)) * AMP_CAP_PCT) / (10 * 100);
      if (muted) s = 0;
      if (s > 32767) { s = 32767; clipped++; }
      if (s < -32768) { s = -32768; clipped++; }

      int32_t mag = s < 0 ? -s : s;
      if (mag > peakWindow) peakWindow = mag;

      block[i * 4] = block[i * 4 + 1] = (int16_t)s;
      block[i * 4 + 2] = block[i * 4 + 3] = (int16_t)s;
#endif
    }
#ifdef RELAY_REPAIRED_QUIET_AUDIO
    if (hasNext && !staged) decoded[n] = spkRing[ringTail];
    const size_t outputFrames = profile.render(decoded, n, hasNext,
        gainEighths, volume, AMP_CAP_PCT, muted, block);
    const size_t outputBytes = outputFrames * 2 * sizeof(int16_t);
    for (size_t i = 0; i < outputFrames * 2; ++i) {
      const int32_t mag = block[i] < 0 ? -int32_t(block[i]) : block[i];
      if (mag > peakWindow) peakWindow = mag;
    }
#else
    const size_t outputBytes = n * 4 * sizeof(int16_t);
#endif
    // Retry short writes, but a speaker stuck for 3 s fails the span (no ack,
    // so the cloud replays it) instead of looping on the same block forever.
    xSemaphoreTake(playbackMutex, portMAX_DELAY);
    if (staged && !playback.current(generation)) {
      xSemaphoreGive(playbackMutex);
      continue;
    }
#ifdef RELAY_BOARD_06
    if (recording) {
      ringClear();
      board06SetAmp(false);
      xSemaphoreGive(playbackMutex);
      vTaskDelay(1);
      continue;
    }
    board06SetAmp(!muted && volume > 0);
#endif
    size_t sent = 0;
    const uint32_t writeStarted = millis();
    while (sent < outputBytes && (!staged || playback.generation == generation)) {
      size_t wrote = 0;
      const esp_err_t err = i2s_channel_write(txChan,
          reinterpret_cast<const uint8_t *>(block) + sent, outputBytes - sent, &wrote, 1000);
      sent += wrote;
      if (err != ESP_OK || wrote == 0) {
        underruns++;
        if (millis() - writeStarted >= 3000) {
          if (staged) playback.end(generation, false);
          break;
        }
        vTaskDelay(1);
      }
    }
    if (sent == outputBytes) {
      framesWritten += n;
      if (staged && playback.generation == generation) {
        playback.played = played + n;
        if (played + n >= total) {
          // Writes enqueue DMA. Feed silence through more than the eight DMA
          // descriptors before publishing completion, so ack means heard.
          memset(block, 0, sizeof(block));
          bool drained = true;
          for (int drain = 0; drained && drain < 5; ++drain) {
            size_t silenceWritten = 0;
            drained = i2s_channel_write(txChan, block, sizeof(block), &silenceWritten, 1000) == ESP_OK
                && silenceWritten == sizeof(block);
          }
          playback.end(generation, drained);
#ifdef RELAY_BOARD_06
          board06SetAmp(false);
#endif
        }
      }
    }
#ifdef RELAY_BOARD_06
    if (sent != outputBytes) board06SetAmp(false);
#endif
#ifdef RELAY_REPAIRED_QUIET_AUDIO
    else profile = before; // Preserve DSP state when retrying a partial write.
#endif
    xSemaphoreGive(playbackMutex);
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

static WebSocketReader wsReader;
static const uint8_t *wsFrame = nullptr;

// One sender for every opcode. A client MUST mask, per RFC 6455.
static bool wsSendFrame(uint8_t opcode, const uint8_t *payload, size_t len) {
  if (!tls.connected()) return false;
  const uint32_t started = millis();
  uint8_t hdr[14];
  size_t h = 0;
  hdr[h++] = 0x80 | opcode;
  if (len < 126) hdr[h++] = 0x80 | (uint8_t)len;
  else if (len < 65536) {
    hdr[h++] = 0x80 | 126;
    hdr[h++] = (len >> 8) & 0xFF; hdr[h++] = len & 0xFF;
  } else {
    hdr[h++] = 0x80 | 127;
    for (int i = 7; i >= 0; i--) hdr[h++] = (uint8_t)((uint64_t)len >> (8 * i));
  }
  uint8_t mask[4];
  for (int i = 0; i < 4; i++) mask[i] = (uint8_t)esp_random();
  memcpy(hdr + h, mask, 4); h += 4;
  if (tls.write(hdr, h) != (int)h) return false;
  // Every completed write is progress. A slow uplink may take several stalls
  // of up to kSocketTimeoutMs to move one frame; only no progress is a hang.
  esp_task_wdt_reset();

  uint8_t chunk[512];
  size_t sent = 0;
  while (sent < len) {
    if (uint32_t(millis() - started) >= kFrameSendMs) {
      Serial.println("[ws] frame could not be sent in time - closing");
      tls.stop();
      return false;
    }
    size_t n = min(sizeof(chunk), len - sent);
    for (size_t i = 0; i < n; i++) chunk[i] = payload[sent + i] ^ mask[(sent + i) & 3];
    if (tls.write(chunk, n) != (int)n) return false;
    esp_task_wdt_reset();
    sent += n;
  }
  return true;
}

static bool wsSendText(const char *s) {
  return wsSendFrame(0x1, (const uint8_t *)s, strlen(s));
}
static bool wsSendBinary(const uint8_t *b, size_t n) {
  return wsSendFrame(0x2, b, n);
}

// Consume only available bytes, with a per-loop budget so buttons and LEDs
// keep running during a partial download. Invalid/oversized spans reconnect;
// truncated audio is never treated as a complete response.
static int wsReadFrame(uint8_t *opcodeOut) {
  if (wsReader.expired(millis())) return -2;
  for (size_t budget = 0; budget < 2048 && tls.available(); ++budget) {
    int byte = tls.read();
    if (byte < 0) return -1;
    auto result = wsReader.feed(uint8_t(byte), millis());
    if (result == WebSocketReader::Invalid) return -2;
    if (result == WebSocketReader::Message) {
      *opcodeOut = wsReader.opcode();
      wsFrame = wsReader.data();
      return int(wsReader.size());
    }
  }
  return -1;
}

static void ensureDns() {
  IPAddress probe;
  if (Network.hostByName(HOST, probe)) {
    Serial.printf("[dns] %s -> %s\n", HOST, probe.toString().c_str());
    return;
  }
  Serial.println("[dns] resolve failed - installing 1.1.1.1");
  esp_netif_dns_info_t dns = {};
  dns.ip.type = ESP_IPADDR_TYPE_V4;
  dns.ip.u_addr.ip4.addr = ipaddr_addr("1.1.1.1");
  esp_netif_t *netif = esp_netif_get_default_netif();
  if (netif) esp_netif_set_dns_info(netif, ESP_NETIF_DNS_MAIN, &dns);
  if (Network.hostByName(HOST, probe))
    Serial.printf("[dns] recovered: %s\n", probe.toString().c_str());
  else
    Serial.println("[dns] still failing - this network blocks outbound DNS");
}

// When this firmware was built. The compiler always knows, and no network can
// take it away - which matters on the device VLANs this thing actually lives
// on, where UDP/123 and outbound 80 are both blocked.
//
// Plus a day of margin, for two reasons: __TIME__ is local wall clock with no
// zone attached, and the server's certificate was issued at some point earlier
// on the same day it was built. A build timestamp that lands a few hours before
// notBefore fails validation just as hard as 1970 does.
static time_t buildTime() {
  static const char *months = "JanFebMarAprMayJunJulAugSepOctNovDec";
  char mon[8] = {0};
  int d = 0, y = 0, h = 0, mi = 0, s = 0;
  sscanf(__DATE__, "%7s %d %d", mon, &d, &y);
  sscanf(__TIME__, "%d:%d:%d", &h, &mi, &s);
  const char *p = strstr(months, mon);
  struct tm tmv = {};
  tmv.tm_mday = d;
  tmv.tm_mon  = p ? (int)((p - months) / 3) : 0;
  tmv.tm_year = y - 1900;
  tmv.tm_hour = h; tmv.tm_min = mi; tmv.tm_sec = s;
  return mktime(&tmv) + 86400;
}

// A pinned certificate is checked against notBefore/notAfter, and an ESP32 with
// no clock thinks it is 1970 - so every handshake fails with an expiry error
// that looks nothing like "the clock is wrong".
//
// Seed from the build stamp so TLS can proceed on a network that gives us no
// time at all, and let SNTP overwrite it with the real thing when the network
// allows. NTP by IP, never by name: these VLANs hand out a resolver that never
// answers, and "pool.ntp.org" is then a name nothing can look up.
//
// ponytail: the build stamp drifts out of the certificate's window after about
// three months of uptime with no SNTP. A reflash fixes it; an RTC or a working
// NTP path fixes it properly.
//
// The mobile network's own time, when the modem has it, replaces that floor:
// it is right however old the image is. Looked at before every dial, because a
// later modem session may bring the time the first one lacked.
static volatile bool sntpSynced = false;  // set from the SNTP task's first answer

static void seedClock() {
  int64_t network = 0;
#ifdef RELAY_CELLULAR
  if (!cellularNetworkTime(network)) network = 0;
#endif
  const int64_t seed = clockSeed(time(nullptr), buildTime(), network, sntpSynced);
  if (!seed) return;
  struct timeval tv = { .tv_sec = (time_t)seed, .tv_usec = 0 };
  settimeofday(&tv, nullptr);
  Serial.printf("[time] clock set from the %s: %s",
                network && seed == network ? "mobile network" : "build stamp",
                ctime(&tv.tv_sec));
}

static bool clockReady() {
  static bool started = false;
  if (!started) {
    ensureDns();                    // fixes the resolver for everything after
    sntp_set_time_sync_notification_cb([](struct timeval *) { sntpSynced = true; });
    configTime(0, 0, "216.239.35.0", "162.159.200.1");
    started = true;
  }
  seedClock();
  return time(nullptr) > kClockUnsetBeforeS;
}

// Stream::setTimeout() is the per-character read wait in milliseconds; it
// never bounded the socket. setConnectionTimeout() does: the TCP connect, and
// a TLS write that stops making progress.
static void configureRelaySocket() {
  tls.setCACert(RELAY_CA);
  tls.setTimeout(kUpgradeReadMs);
  tls.setConnectionTimeout(kSocketTimeoutMs);
  // Without this the handshake can block forever on a network that drops the
  // SYN instead of refusing it - and a blocked loop() takes the serial console
  // with it, so the board looks dead rather than stuck.
  tls.setHandshakeTimeout(kTlsHandshakeS);
}

// Reads the upgrade response: the 101 status line, then headers up to the
// blank line. Each line feeds the watchdog, so the whole response has
// kUpgradeTotalMs. Headers that stop arriving are a failed dial, not the end
// of the headers: what is still in flight would be read as WebSocket frames.
static bool wsAwaitUpgrade() {
  const uint32_t started = millis();
  while (!tls.available() && uint32_t(millis() - started) < 10000) {
    delay(10);
    esp_task_wdt_reset();
  }
  String status = tls.readStringUntil('\n');
  esp_task_wdt_reset();
  if (status.indexOf("101") < 0) {
    Serial.printf("[ws] handshake rejected: %s\n", status.c_str());
    return false;
  }
  while (tls.connected() && uint32_t(millis() - started) < kUpgradeTotalMs) {
    String line = tls.readStringUntil('\n');
    esp_task_wdt_reset();  // each header line may wait kUpgradeReadMs
    if (line == "\r") return true;
    if (!line.length()) break;
  }
  Serial.println("[ws] upgrade response never finished");
  return false;
}

static bool wsConnect() {
  if (!relayCredential.length()) return false;
  wsReachedRelay = false;
  ensureDns();
  tls.stop();
  configureRelaySocket();

  // tls.connect() blocks, and verifying a four-deep ECDSA chain over a slow
  // VLAN can outlast the 20 s task watchdog - which reboots the board in the
  // middle of the handshake and reads as a crash rather than a slow network.
  // The bring-up sketch never hit this because setInsecure() verified nothing.
  // Step out of the watchdog for exactly this call, and time it, so a slow
  // handshake and a refused one do not look the same.
  uint32_t tlsStart = millis();
  esp_task_wdt_delete(nullptr);
  bool up = tls.connect(HOST, PORT);
  esp_task_wdt_add(nullptr);
  esp_task_wdt_reset();
  Serial.printf("[ws] TLS %s in %lu ms\n", up ? "up" : "FAILED",
                (unsigned long)(millis() - tlsStart));
  if (!up) {
    char err[160] = {0};
    wsReachedRelay = dialReachedRelay(false, tls.lastError(err, sizeof(err)));
    time_t now = time(nullptr);
    Serial.printf("[ws] mbedtls says: %s\n", err[0] ? err : "(nothing)");
    Serial.printf("[ws] clock is %s", ctime(&now));
    Serial.printf("[ws] heap %lu, press I to retry without verifying the cert\n",
                  (unsigned long)ESP.getFreeHeap());
    return false;
  }
  wsReachedRelay = true;
  time_t verifiedAt = time(nullptr);
  Serial.printf("[ws] clock is %s", ctime(&verifiedAt));  // the time the certificate passed at
  uint8_t rnd[16];
  for (int i = 0; i < 16; i++) rnd[i] = (uint8_t)esp_random();
  unsigned char key[32];
  size_t keyLen = 0;
  mbedtls_base64_encode(key, sizeof(key), &keyLen, rnd, 16);
  key[keyLen] = 0;

  String req = String("GET ") + PATH + " HTTP/1.1\r\n" +
               "Host: " + HOST + "\r\n" +
               "Upgrade: websocket\r\n" +
               "Connection: Upgrade\r\n" +
               "Sec-WebSocket-Key: " + (const char *)key + "\r\n" +
               "Sec-WebSocket-Version: 13\r\n" +
               "Authorization: " + relayCredential + "\r\n\r\n";
  tls.print(req);

  if (!wsAwaitUpgrade()) {
    tls.stop();
    return false;
  }
  Serial.printf("[ws] connected to %s%s\n", HOST, PATH);
  return true;
}

// ---------------------------------------------------------------------------
// Tiny JSON reads. No parser: control messages are a handful of flat objects
// and pulling in a library for that is not worth the flash.
// ---------------------------------------------------------------------------

// Finds the value after "key":, tolerating whitespace around the colon.
//
// Matching "\"type\":\"digest\"" as one literal missed every single message the
// Mac sends: Python's json.dumps separates with ", " and ": " by default, so
// the wire actually carries {"v": 1, "type": "digest", ...}. Audio still played,
// because binary frames never go through here - but no ack was ever sent, and
// an unacked span is one the Mac replays forever.
static const char *jsonFind(const char *msg, const char *key) {
  char needle[32];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char *p = strstr(msg, needle);
  if (!p) return nullptr;
  p += strlen(needle);
  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') return nullptr;
  p++;
  while (*p == ' ' || *p == '\t') p++;
  return p;
}

static bool jsonIs(const char *msg, const char *key, const char *want) {
  const char *p = jsonFind(msg, key);
  if (!p || *p != '"') return false;
  p++;
  size_t n = strlen(want);
  return strncmp(p, want, n) == 0 && p[n] == '"';
}

static bool jsonStr(const char *msg, const char *key, char *out, size_t outSz) {
  const char *p = jsonFind(msg, key);
  if (!p || *p != '"') return false;
  p++;
  const char *e = strchr(p, '"');
  if (!e) return false;
  size_t n = (size_t)(e - p);
  if (n >= outSz) n = outSz - 1;
  memcpy(out, p, n);
  out[n] = 0;
  return true;
}

static bool jsonInt(const char *msg, const char *key, int *out) {
  const char *p = jsonFind(msg, key);
  if (!p) return false;
  if (*p != '-' && (*p < '0' || *p > '9')) return false;
  *out = atoi(p);
  return true;
}

static bool jsonTrue(const char *msg, const char *key) {
  const char *p = jsonFind(msg, key);
  return p && strncmp(p, "true", 4) == 0;
}

static bool jsonFalse(const char *msg, const char *key) {
  const char *p = jsonFind(msg, key);
  return p && strncmp(p, "false", 5) == 0;
}

// ---------------------------------------------------------------------------
// Protocol
// ---------------------------------------------------------------------------

static void sendHello() {
  // autoplay:false or the endpoint treats this like a browser tab and pushes speech
  // at a handset lying on a table with nobody near it. preload:true says this
  // firmware can hold that speech silently until the local Listen button.
  // "updates" is the image's kind, fixed at compile time: only the release
  // workflow defines RELAY_OFFICIAL_BUILD, and only that image takes auto updates.
#ifdef RELAY_BOARD_06
  // board and firmware let the relay offer updates; a version that failed to
  // boot twice is reported so it is never offered again.
#ifdef RELAY_OFFICIAL_BUILD
  const char *kind = "auto";
#else
  const char *kind = "manual";
#endif
  const char *failed = board06Ota.record().blacklisted;
  char hello[192];
  snprintf(hello, sizeof hello,
           "{\"v\":1,\"type\":\"hello\",\"autoplay\":false,\"preload\":true,"
           "\"board\":\"06\",\"firmware\":\"" RELAY_FW_SEMVER "\"%s%s%s,\"updates\":\"%s\"}",
           failed[0] ? ",\"firmware_failed\":\"" : "", failed, failed[0] ? "\"" : "", kind);
  wsSendText(hello);
  Serial.printf("[tx] hello firmware=%s kind=%s\n", RELAY_FW_SEMVER, kind);
#elif defined(RELAY_OFFICIAL_BUILD)
  wsSendText("{\"v\":1,\"type\":\"hello\",\"autoplay\":false,\"preload\":true,\"updates\":\"auto\"}");
  Serial.println("[tx] hello kind=auto");
#else
  wsSendText("{\"v\":1,\"type\":\"hello\",\"autoplay\":false,\"preload\":true,\"updates\":\"manual\"}");
  Serial.println("[tx] hello kind=manual");
#endif
}

static void sendStatus() {
  char battery[96] = "";
#ifdef RELAY_BOARD_06
  BatteryReading reading;
  if (readBatteryGauge(Wire, reading)) {
    const char *power = board06Power.state();
    if (power)
      snprintf(battery, sizeof(battery), ",\"battery_pct\":%d,\"power_state\":\"%s\"",
               reading.roundedPercent(), power);
    else
      snprintf(battery, sizeof(battery), ",\"battery_pct\":%d", reading.roundedPercent());
  }
  board06Power.reported();
#endif
  char msg[192];
  snprintf(msg, sizeof(msg),
           "{\"v\":1,\"type\":\"status\",\"rssi\":%d,\"fw\":\"%s\"%s}",
           networkRssi(), FW_VERSION, battery);
  wsSendText(msg);
  lastStatusMs = millis();
  Serial.println("[tx] status");
}

static void sendAck(const char *id) {
  char msg[112];
  snprintf(msg, sizeof(msg), "{\"v\":1,\"type\":\"ack\",\"id\":\"%s\"}", id);
  wsSendText(msg);
  Serial.printf("[tx] ack %s\n", id);
}

// A replayed span stays staged behind its unconfirmed receipt, so resend that
// receipt now instead of at its backed-off retry (up to five minutes after a
// mailbox outage). Its backoff is kept: a still-failing mailbox is not hammered.
static void announceConnection() {
  sendHello();
  if (playedReceipt.pending()) sendAck(playedReceipt.id());
  sendStatus();
}

// Sends whole frames of raw PCM16, or everything with `all`, in order.
static void sendMicFrames(bool all) {
  size_t sent = 0;
  while (wsOpen && (micBatchFill - sent >= MIC_FRAME || (all && sent < micBatchFill))) {
    size_t n = micBatchFill - sent < MIC_FRAME ? micBatchFill - sent : MIC_FRAME;
    if (wsSendBinary((const uint8_t *)(micBatch + sent), n * sizeof(micBatch[0]))) {
      micFramesSent++;
    } else {
      wsOpen = false;
    }
    sent += n;
  }
  if (!wsOpen) sent = micBatchFill;
  memmove(micBatch, micBatch + sent, (micBatchFill - sent) * sizeof(micBatch[0]));
  micBatchFill -= sent;
}

static void flushMicBatch() { sendMicFrames(true); }

// The microphone opens on the press; talk.start waits for PTT_TALK_HOLD_MS so
// a tap stays on the Device (taps open Muse pairing).
// Called only with micMutex held. A separate task checks the physical input,
// so a slow network write cannot leave microphone clocks running on release.
// A driver that refuses to stop is discarded, never a reason to restart the
// firmware in someone's hand: the next press builds a new channel. Only a
// channel that cannot be released either still restarts, because it would
// hold the microphone port and leave Talk dead until the next restart anyway.
static void stopMicCapture() {
  if (!micCapturing) return;
  micCapturing = false;
  if (i2s_channel_disable(rxChan) == ESP_OK) return;
  Serial.println("[mic] capture did not stop cleanly; channel discarded");
  ESP_ERROR_CHECK(i2s_del_channel(rxChan));
  rxChan = nullptr;
}

#ifdef RELAY_BOARD_06
// Caller holds micMutex; freeze duration when the physical input is observed,
// not when a slow network write eventually returns.
static void board06StopCaptureLocked(uint32_t now) {
  stopMicCapture();
  const uint32_t stopped = (uint32_t)(now - recStartMs) >= MAX_RECORDING_MS
      ? recStartMs + MAX_RECORDING_MS : now;
  if (board06DmaOverflow.exchange(false, std::memory_order_relaxed))
    captureSpool->abort(Pcb06CaptureSpool::Failure::Capture);
  else captureSpool->stop(stopped);
}

static void board06AbortCaptureLocked(Pcb06CaptureSpool::Failure failure) {
  stopMicCapture();
  captureSpool->abort(failure);
}

// One bounded worker iteration. No socket operation may occur under this lock.
static void board06CaptureStep() {
  MicLock guard;
  if (!captureSpool || !micCapturing) return;
  uint32_t now = millis();
  if (digitalRead(buttons.talk) != LOW || (uint32_t)(now - recStartMs) >= MAX_RECORDING_MS) {
    board06StopCaptureLocked(now);
    return;
  }
  if (board06DmaOverflow.exchange(false, std::memory_order_relaxed)) {
    board06AbortCaptureLocked(Pcb06CaptureSpool::Failure::Capture);
    return;
  }
  int16_t raw[256 * 2] = {}, pcm[256] = {};
  size_t bytes = 0;
  const auto error = i2s_channel_read(rxChan, raw, sizeof(raw), &bytes, 4);
  now = millis();
  if ((error != ESP_OK && error != ESP_ERR_TIMEOUT) || bytes % (2 * sizeof(int16_t))
      || board06DmaOverflow.exchange(false, std::memory_order_relaxed)) {
    board06AbortCaptureLocked(Pcb06CaptureSpool::Failure::Capture);
  } else if (digitalRead(buttons.talk) != LOW || (uint32_t)(now - recStartMs) >= MAX_RECORDING_MS) {
    board06StopCaptureLocked(now);
  } else {
    size_t count = bytes / (2 * sizeof(int16_t));
    const size_t remaining = MAX_RECORDING_SAMPLES - micSamplesRead;
    if (count > remaining) count = remaining;
    size_t uploaded = 0;
    for (size_t i = 0; i < count; ++i) {
      int32_t sample = micHighPass.process(raw[i * 2 + RELAY_PCB06_MIC_SLOT]);
      ++micSamplesRead;
      // Keep clocking/filtering the microphone while the opening cue plays,
      // but never put those initial frames in the upload spool.
      if (board06MicTrimRemaining) { --board06MicTrimRemaining; continue; }
      sample *= RELAY_PCB06_MIC_GAIN;
      if (sample > 32767 || sample < -32768) ++micClipped;
      if (sample > 32767) sample = 32767;
      if (sample < -32768) sample = -32768;
      pcm[uploaded++] = (int16_t)sample;
      micEnergy += uint64_t(int64_t(sample) * sample);
      const int32_t magnitude = sample < 0 ? -sample : sample;
      if (magnitude > micPeak) micPeak = magnitude;
    }
    if (count) board06LastMicData = now;
    if (!captureSpool->append(pcm, uploaded)) stopMicCapture();
    else if (micSamplesRead >= MAX_RECORDING_SAMPLES) board06StopCaptureLocked(now);
    else if ((uint32_t)(now - board06LastMicData) >= 500)
      board06AbortCaptureLocked(Pcb06CaptureSpool::Failure::Capture);
  }
  memset(raw, 0, sizeof(raw));
  memset(pcm, 0, sizeof(pcm));
}

static void board06CaptureTask(void *) {
  for (;;) {
    board06CaptureStep();
    vTaskDelay(1);
  }
}
#endif

static void guardMicRelease() {
  MicLock guard;
  if (micCapturing && (digitalRead(buttons.talk) != LOW
      || millis() - recStartMs >= MAX_RECORDING_MS)) {
#ifdef RELAY_BOARD_06
    board06StopCaptureLocked(millis());
#else
    stopMicCapture();
#endif
  }
}

static void micGuardTask(void *) {
  for (;;) {
    guardMicRelease();
    vTaskDelay(1);
  }
}

static void startRecording() {
  MicLock guard;
  // Endpoint availability still drives the warning LED, but a live cloud owns
  // the spoken refusal. Let the User ask so cloud can explain a missing Link,
  // Agent, or Speech adapter; a total cloud outage remains indicator-only.
  if (recording || digitalRead(buttons.talk) != LOW
      || !endpointAvailability.canRequest(wsOpen)) return;
  // Push-to-talk is barge-in only once sound is actually playing. Stop it on
  // the press, before the mic opens, or the speaker leaks into the request.
  // A silent preload may continue into PSRAM while the microphone is open;
  // discarding it here strands the Mac waiting forever for an ack.
  if (playing) pausePlayback();
#ifdef RELAY_BOARD_06
  xSemaphoreTake(playbackMutex, portMAX_DELAY);
  ringClear();
  // A local button click may overlap the mic; never wait for that feedback.
  // Reply playback was already drained by pausePlayback above.
  if (board06AmpOn && !board06CuePlayer.active() && !board06PttOwnsOutput) board06DrainSpeaker();
  xSemaphoreGive(playbackMutex);
#endif
#ifdef RELAY_BOARD_06
  recStartMs = millis();
  if (!captureSpool || !captureSpool->begin(recStartMs, digitalRead(buttons.talk) == LOW, wsOpen)) return;
  board06DmaOverflow.store(false, std::memory_order_relaxed);
  board06LastMicData = millis();
#endif
  // A new channel after each stop discards the old driver's DMA queue.
  if (!rxChan && !setupMic()) {
#ifdef RELAY_BOARD_06
    captureSpool->abort(Pcb06CaptureSpool::Failure::Capture);
#endif
    return;
  }
  if (i2s_channel_enable(rxChan) != ESP_OK) {
    i2s_del_channel(rxChan);
    rxChan = nullptr;
#ifdef RELAY_BOARD_06
    captureSpool->abort(Pcb06CaptureSpool::Failure::Capture);
#endif
    return;
  }
  micCapturing = true;
  recording = true;
  talkSent = false;
#ifndef RELAY_BOARD_06
  recStartMs = millis();
#endif
  micHighPass.reset();
#ifdef RELAY_BOARD_06
  board06MicTrimRemaining = muted || volume == 0 ? 0 : pcb06_ptt_sounds::kPressSamples;
#endif
  micPeak = 0;
  micSamplesRead = micClipped = 0;
  micEnergy = 0;
  micBatchFill = 0;
}

static void sendTalkStart() {
#ifdef RELAY_BOARD_06
  uint32_t generation;
  {
    MicLock guard;
    if (!recording || !captureSpool->needsStart(millis())) return;
    generation = captureSpool->generation();
  }
  if (!wsOpen || !wsSendText("{\"v\":1,\"type\":\"talk.start\"}")) {
    board06TransportFailed();
    return;
  }
  bool accepted;
  {
    MicLock guard;
    accepted = generation == captureSpool->generation() && captureSpool->markStarted(millis());
    talkSent = accepted;
  }
  if (!accepted) { board06TransportFailed(); return; }
#else
  if (!recording || talkSent) return;
  talkSent = true;
  wsSendText("{\"v\":1,\"type\":\"talk.start\"}");
#endif
  Serial.println("[tx] talk.start");
}

static void stopRecording() {
#ifdef RELAY_BOARD_06
  MicLock guard;
  if (!recording) return;
  if (!wsOpen) board06AbortCaptureLocked(Pcb06CaptureSpool::Failure::Disconnected);
  else if (micCapturing) board06StopCaptureLocked(millis());
  if (rxChan) { i2s_del_channel(rxChan); rxChan = nullptr; }
  // A genuine released speech turn remains busy until all bytes and talk.end drain.
  if (!wsOpen || (!captureSpool->active() && captureSpool->state() != Pcb06CaptureSpool::State::Failed)) {
    recording = false;
    talkSent = false;
    memset(board06TxChunk, 0, sizeof(board06TxChunk));
  }
#else
  if (!recording) return;
  // Stop capture before any socket operation can block. Already collected
  // post-press samples may finish transmitting after release.
  {
    MicLock guard;
    stopMicCapture();
    if (rxChan && i2s_del_channel(rxChan) != ESP_OK)
      Serial.println("[mic] channel was not released cleanly");
    rxChan = nullptr;
  }
  // A release can beat the loop's talk.start check by a few milliseconds.
  if (!talkSent && pttIsSpeech(millis() - recStartMs)) sendTalkStart();
  if (!talkSent) {
    recording = false;
    micBatchFill = 0;
    return;
  }
  flushMicBatch();
  recording = false;
  wsSendText("{\"v\":1,\"type\":\"talk.end\"}");
  const uint32_t heldMs = millis() - recStartMs;
  const uint32_t settled = micSamplesRead > 2400 ? micSamplesRead - 2400 : 0;
  Serial.printf("[tx] talk.end (%lu ms, peak %ld) captured %lu/%lu samples, rms %lu, clipped %lu\n",
                (unsigned long)heldMs, (long)micPeak, (unsigned long)micSamplesRead,
                (unsigned long)(uint64_t(heldMs) * SAMPLE_RATE_HZ / 1000),
                settled ? (unsigned long)sqrt(double(micEnergy) / settled) : 0UL,
                (unsigned long)micClipped);
#endif
}

// Playback was cut short. Per the contract we send NOTHING: the ack is what
// clears the application queue, so staying quiet is what makes it replay.
static void abandonPlayback() {
  playbackClear();
  ringClear();
  playing = false;
  pendingAck[0] = 0;
  Serial.println("[rx] playback cut - no ack, the Mac will replay it");
}

static bool hasPendingPlayback() {
  return playbackReceiving || playback.staged || playback.ready || playing
      || pendingAck[0];
}

// Every connection-ended path discards unheard audio without acknowledging it.
static void connectionEnded(int closeCode = 0) {
  tls.stop();
  wsOpen = false;
#ifdef RELAY_BOARD_06
  board06OtaFresh = false;  // install only on a target heard since reconnecting
#endif
  clearAvailability();
  stopRecording();
  if (hasPendingPlayback()) abandonPlayback();
  if (closeCode == 4401) {
    playedReceipt.clear();
    spanId[0] = 0;
    ledMissed = 0;
  }
  scheduleRetry(closeCode);
}

#ifdef RELAY_BOARD_06
static void board06TransportFailed() {
  const char *reason = "transport";
  {
    MicLock guard;
    board06AbortCaptureLocked(Pcb06CaptureSpool::Failure::Transport);
    switch (captureSpool->failure()) {
      case Pcb06CaptureSpool::Failure::Capture: reason = "capture/overrun"; break;
      case Pcb06CaptureSpool::Failure::Overflow: reason = "spool overflow"; break;
      case Pcb06CaptureSpool::Failure::Disconnected: reason = "disconnected"; break;
      default: break;
    }
  }
  memset(board06TxChunk, 0, sizeof(board06TxChunk));
  // Incomplete spoken requests end by losing the socket, never by talk.end.
  connectionEnded();
  Serial.printf("[mic] %s failure; incomplete request discarded\n", reason);
}
#endif

static void retryPlayback() {
  char id[65] = {0};
  const char *source = pendingAck[0] ? pendingAck : spanId;
  strncpy(id, source, sizeof(id) - 1);
  abandonPlayback();
  if (!wsOpen || !id[0]) return;
  char msg[112];
  snprintf(msg, sizeof(msg),
           "{\"v\":1,\"type\":\"audio.retry\",\"id\":\"%s\"}", id);
  wsSendText(msg);
  Serial.printf("[tx] audio.retry %s\n", id);
}

// ---------------------------------------------------------------------------
// Muse pairing. Three quick PTT taps let the Muse phone app pair this Device
// as a Muse gadget over Bluetooth (LED blinks white). The tokens it hands
// over go straight to the cloud, which then talks to Muse itself; nothing is
// stored here. Ready returns to normal status; red means failed.
// Any PTT press closes the window.
// ---------------------------------------------------------------------------

enum class MuseMode { Off, Asking, Open, Saving, Paired, Failed };
static MuseMode museMode = MuseMode::Off;

#ifdef RELAY_CELLULAR
static void museStop() {}
static void museService() {}
static void museStart() { Serial.println("[muse] pairing needs the Wi-Fi build"); }
#else
// Arduino releases Bluetooth memory at boot unless the sketch claims it.
bool btInUse() { return true; }

static uint32_t museSince = 0;
static const uint32_t MUSE_ASK_MS = 10000;      // cloud answers muse.pair.start
static const uint32_t MUSE_WINDOW_MS = 600000;  // the phone pairs within 10 min
static const uint32_t MUSE_SAVE_MS = 30000;     // cloud confirms the tokens
static const uint32_t MUSE_RESULT_MS = 3000;    // minimum pairing result window
// After pairing the cloud still checks Muse and switches it on (about 10 s).
// Keep showing the connection check until then, instead of "no assistant" red.
static const uint32_t MUSE_READY_MS = 30000;

class MuseTransport : public muse::Transport {
 public:
  void sendPackets(const std::vector<std::string> &packets) override {
    for (size_t i = 0; i < packets.size(); i++) {
      if (i) delay(50);  // Meta's stagger between notifications
      muse_ble_notify((const uint8_t *)packets[i].data(), packets[i].size());
    }
  }
  int mtu() override { return muse_ble_mtu(); }
  void disconnect(uint32_t delayMs) override { muse_ble_disconnect_after(delayMs); }
};
static MuseTransport museTransport;
static muse::Setup *museSetup = nullptr;

static uint32_t museClock() { return millis(); }
static void museRandom(uint8_t *out, size_t len) { esp_fill_random(out, len); }
static void museOnWrite(const uint8_t *data, size_t len) {
  if (museSetup) museSetup->onWrite(data, len);
}
static void museOnDisconnect() {
  if (museSetup) museSetup->onDisconnect();
}

// The Wi-Fi station MAC names the gadget, as on Meta's ESP32 firmware.
static void museMac(uint8_t mac[6], char text[18]) {
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(text, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static const char *museSsid() {
#ifdef RELAY_CELLULAR
  return "";
#else
  static String ssid;
  ssid = WiFi.SSID();
  return ssid.c_str();
#endif
}

static void museStop() {
  if (museMode == MuseMode::Off) return;
  muse_ble_end();
  delete museSetup;
  museSetup = nullptr;
  museMode = MuseMode::Off;
  Serial.printf("[muse] pairing closed, heap %lu\n", (unsigned long)ESP.getFreeHeap());
}

static void museResult(bool ok, const char *why) {
  if (museSetup && museMode == MuseMode::Saving) museSetup->finishProvision(ok);
  museMode = ok ? MuseMode::Paired : MuseMode::Failed;
  museSince = millis();
  Serial.printf("[muse] %s%s%s\n", ok ? "paired" : "pairing failed",
                why && *why ? ": " : "", why ? why : "");
}

static void museStart() {
  if (museMode != MuseMode::Off) return;
  museMode = MuseMode::Asking;
  museSince = millis();
  if (!wsOpen || !wsSendText("{\"v\":1,\"type\":\"muse.pair.start\"}")) {
    museResult(false, "cloud unreachable");
    return;
  }
  Serial.println("[tx] muse.pair.start");
}

static void museOpen(const char *sdkToken) {
  uint8_t mac[6];
  char macText[18], node[16], name[24];
  museMac(mac, macText);
  snprintf(node, sizeof(node), "homelink-%02x%02x%02x", mac[3], mac[4], mac[5]);
  snprintf(name, sizeof(name), "MuseGadget-%02X%02X%02X", mac[3], mac[4], mac[5]);
  muse::Identity identity{node, std::string("hatch-link:") + macText, macText,
                          FW_VERSION, sdkToken};
  museSetup = new muse::Setup(museTransport, identity, museClock, museRandom);
  if (!muse_ble_begin(name, museOnWrite, museOnDisconnect)) {
    delete museSetup;
    museSetup = nullptr;
    museResult(false, "Bluetooth did not start");
    return;
  }
  museMode = MuseMode::Open;
  museSince = millis();
  Serial.printf("[muse] pairing open as %s, heap %lu\n", name,
                (unsigned long)ESP.getFreeHeap());
}

static void museWipe(cJSON *item) {
  for (; item; item = item->next) {
    if (item->valuestring) memset(item->valuestring, 0, strlen(item->valuestring));
    museWipe(item->child);
  }
}

static void museSendTokens(muse::Provision &provision) {
  uint8_t mac[6];
  char macText[18];
  museMac(mac, macText);
  cJSON *root = cJSON_CreateObject();
  cJSON_AddNumberToObject(root, "v", 1);
  cJSON_AddStringToObject(root, "type", "muse.pair.complete");
  cJSON_AddStringToObject(root, "access_token", provision.accessToken.c_str());
  cJSON_AddStringToObject(root, "refresh_token", provision.refreshToken.c_str());
  cJSON_AddStringToObject(root, "noise_host", provision.noiseHost.empty()
                                                  ? "hatch.metaaivm.com"
                                                  : provision.noiseHost.c_str());
  if (!provision.apiUrlV2.empty()) {
    cJSON_AddStringToObject(root, "api_url_v2", provision.apiUrlV2.c_str());
  }
  cJSON_AddStringToObject(root, "mac", macText);
  char *json = cJSON_PrintUnformatted(root);
  const bool sent = json && wsOpen && wsSendText(json);
  if (json) {
    memset(json, 0, strlen(json));
    cJSON_free(json);
  }
  museWipe(root);
  cJSON_Delete(root);
  memset(&provision.accessToken[0], 0, provision.accessToken.size());
  memset(&provision.refreshToken[0], 0, provision.refreshToken.size());
  museMode = MuseMode::Saving;
  museSince = millis();
  if (!sent) {
    museResult(false, "cloud unreachable");
    return;
  }
  Serial.println("[tx] muse.pair.complete");
}

static void museService() {
  if (museMode == MuseMode::Off) return;
  const uint32_t age = millis() - museSince;
  if (museSetup) museSetup->poll(networkConnected(), museSsid());
  switch (museMode) {
    case MuseMode::Asking:
      if (age > MUSE_ASK_MS || !wsOpen) museResult(false, "no answer from the cloud");
      break;
    case MuseMode::Open: {
      muse::Provision provision;
      if (museSetup->takeProvision(provision)) {
        museSendTokens(provision);
      } else if (age > MUSE_WINDOW_MS) {
        Serial.println("[muse] pairing window closed");
        museStop();
      }
      break;
    }
    case MuseMode::Saving:
      if (age > MUSE_SAVE_MS || !wsOpen) museResult(false, "cloud did not confirm");
      break;
    case MuseMode::Paired:
      if ((age > MUSE_RESULT_MS && endpointAvailability.usable()) || age > MUSE_READY_MS)
        museStop();
      break;
    default:  // Failed: hold the colour, let the phone read it
      if (age > MUSE_RESULT_MS) museStop();
      break;
  }
}

#endif  // RELAY_CELLULAR

#ifdef RELAY_BOARD_06
static void board06OtaSave() {
  if (board06Ota.dirty() && pcb06_ota::save(board06Ota.record())) board06Ota.saved();
}

// Progress for the relay's current target; it keeps only matching reports.
static void board06OtaStatus(const char *state, const char *version, uint32_t retryMs = 0) {
  if (!wsOpen) return;
  const OtaRecord &record = board06Ota.record();
  char message[192];
  const int n = snprintf(message, sizeof message,
      "{\"v\":1,\"type\":\"firmware.status\",\"revision\":%lld,\"version\":\"%s\","
      "\"state\":\"%s\",\"attempt\":%u", (long long)record.revision, version, state,
      unsigned(record.attempts));
  if (retryMs)
    snprintf(message + n, sizeof message - n, ",\"retry_after_s\":%lu}",
             (unsigned long)(retryMs < 1000 ? 1 : retryMs / 1000));
  else
    snprintf(message + n, sizeof message - n, "}");
  wsSendText(message);
  Serial.printf("[ota] %s %s\n", state, version);
}

// The new image reached the relay: keep it.
static void board06OtaProve() {
  pcb06_ota::keep();
  board06OtaUnproven = false;
  board06Ota.proven();
  board06OtaSave();
  board06OtaStatus("installed", RELAY_FW_SEMVER);
}

// firmware.available. Manual builds ignore every offer; the relay sends them
// none. Every accepted, withdrawn or repeated target is acknowledged.
static void board06OtaOffer(const char *msg) {
#ifdef RELAY_OFFICIAL_BUILD
  const char *p = jsonFind(msg, "revision");
  if (!p || *p < '0' || *p > '9') return;  // installs nothing without a revision
  pcb06_ota::Image offer{};
  if (!jsonStr(msg, "version", offer.version, sizeof offer.version)) return;
  const bool image = jsonStr(msg, "url", offer.url, sizeof offer.url) &&
                     jsonStr(msg, "sha256", offer.sha256, sizeof offer.sha256) &&
                     jsonStr(msg, "signature", offer.signature, sizeof offer.signature) &&
                     jsonInt(msg, "size", &offer.size);
  const OtaOffer result = board06Ota.offer(strtoll(p, nullptr, 10), offer.version, offer.sha256,
      image, RELAY_FW_SEMVER, millis(), esp_random() % kOtaStartJitterMs);
  if (result != OtaOffer::Ignored) {
    board06OtaFresh = true;
    board06OtaHasImage = image && !strcmp(offer.version, board06Ota.record().target);
    if (board06OtaHasImage) board06OtaImage = offer;
    if (result == OtaOffer::Cleared && pcb06_ota::state == pcb06_ota::Download::Running)
      pcb06_ota::stopRequested = true;
    board06OtaSave();
    board06OtaStatus("accepted", offer.version);
  }
  // Any target proves the relay accepted this image's hello.
  if (board06OtaUnproven) board06OtaProve();
#endif
}
#endif

static void handleMessage(char *msg, size_t len) {
#ifdef RELAY_BOARD_06
  if (jsonIs(msg, "type", "firmware.available")) {
    board06OtaOffer(msg);
    return;
  }
#endif
#ifndef RELAY_CELLULAR
  if (jsonIs(msg, "type", "muse.pair.open")) {
    if (museMode != MuseMode::Asking) return;
    char token[257] = {0};
    char reason[32] = {0};
    if (jsonTrue(msg, "ok")) {
      jsonStr(msg, "sdk_token", token, sizeof(token));
      museOpen(token);
      memset(token, 0, sizeof(token));
    } else {
      jsonStr(msg, "reason", reason, sizeof(reason));
      museResult(false, reason);
    }
    return;
  }
  if (jsonIs(msg, "type", "muse.pair.done")) {
    if (museMode != MuseMode::Saving) return;
    char reason[32] = {0};
    jsonStr(msg, "reason", reason, sizeof(reason));
    museResult(jsonTrue(msg, "ok"), reason);
    return;
  }
#endif
  if (jsonIs(msg, "type", "peer")) {
    bool peerAvailable = jsonTrue(msg, "mac");
    bool becameAvailable = endpointAvailability.applyPeer(peerAvailable);
    Serial.printf("[rx] compatibility peer %s; endpoint %s\n",
                  peerAvailable ? "available" : "unavailable",
                  endpointAvailability.usable() ? "usable" : "unavailable");
    // The relay does not retain our original hello, so repeat capabilities
    // whenever the compatibility availability projection reappears.
    if (becameAvailable) sendHello();
    return;
  }
  if (jsonIs(msg, "type", "ack.confirmed")) {
    char id[65] = {0};
    if (jsonStr(msg, "id", id, sizeof(id)) && playedReceipt.confirm(id)) {
      if (strcmp(pendingAck, id) == 0) abandonPlayback();
    }
    return;
  }
  if (jsonIs(msg, "type", "presence")) {
    confirmsPlayedReceipts = jsonTrue(msg, "ack_confirmations");

    char agent[16] = {0};
    char connectionState[24] = {0};
    jsonStr(msg, "connection_state", connectionState, sizeof(connectionState));
    bool connecting = strcmp(connectionState, "setup_required") == 0 ||
                      strcmp(connectionState, "pairing") == 0 ||
                      strcmp(connectionState, "checking") == 0 ||
                      strcmp(connectionState, "ready_to_activate") == 0;
    bool linkAvailable = jsonTrue(msg, "link");
    jsonStr(msg, "agent", agent, sizeof(agent));
    bool agentAvailable = strcmp(agent, "ready") == 0 || strcmp(agent, "busy") == 0;
    bool becameAvailable =
        endpointAvailability.applyPresence(linkAvailable, agentAvailable, strcmp(agent, "busy") == 0, connecting);
    Serial.printf("[rx] cloud=connected Link=%s Agent=%s Connection=%s\n",
                  linkAvailable ? "connected" : "unavailable", agent, connectionState);
    if (becameAvailable) sendHello();
    return;
  }
  if (jsonIs(msg, "type", "audio.start")) {
    jsonStr(msg, "id", spanId, sizeof(spanId));
    // A different Snapshot means the server advanced or retired the old route.
    // Its old played receipt must never block speech on the replacement route.
    if (playedReceipt.pending() && strcmp(spanId, playedReceipt.id()) != 0)
      playedReceipt.clear();
    playbackStart();
    playing = false;
    pendingAck[0] = 0;
    Serial.printf("[rx] audio.start %s\n", spanId);
    return;
  }
  if (jsonIs(msg, "type", "audio.end")) {
    if (!playbackReceiving) {
      Serial.println("[rx] stale audio.end ignored");
      return;
    }
    char id[65] = {0};
    jsonStr(msg, "id", id, sizeof(id));
    if (strcmp(id, spanId) != 0 || jsonFalse(msg, "complete")) {
      abandonPlayback();
      return;
    }
    if (!playbackCommit()) {
      // The application must retain this unacked response. Reconnecting gives its
      // existing queue a clean opportunity to replay it.
      abandonPlayback();
      tls.stop();
      wsOpen = false;
      clearAvailability();
      scheduleRetry();
      return;
    }
    // Ack only after audioTask writes the last staged sample to I2S.
    strncpy(pendingAck, spanId, sizeof(pendingAck) - 1);
    pendingAck[sizeof(pendingAck) - 1] = 0;
    Serial.printf("[rx] audio.end %s (staged %lu ms, %lu bytes MP3)\n", pendingAck,
                  (unsigned long)(uint64_t(playback.samples) * 1000 / SAMPLE_RATE_HZ),
                  (unsigned long)playbackBytes);
    return;
  }
  if (jsonIs(msg, "type", "digest")) {
    // Backward compatibility with a relay predating authoritative presence.
    // Once presence was seen, it wins over a digest already in flight.
    endpointAvailability.applyDigest();
    int missed = 0;
    if (!jsonInt(msg, "missed", &missed)) missed = 0;
    ledMissed = missed;
    const bool needsAnswer = jsonTrue(msg, "needs_answer");
    Serial.printf("[rx] digest missed=%d needs_answer=%d\n",
                  ledMissed, needsAnswer ? 1 : 0);
    return;
  }
  if (jsonIs(msg, "type", "cancel")) {
    // cancel is device->Mac by contract, so this should never arrive. Log it
    // rather than acting on it: guessing at a message we do not own is how
    // two implementations drift apart quietly.
    Serial.println("[rx] cancel (device->mac only - ignored)");
    return;
  }
  Serial.printf("[rx] unhandled: %.120s\n", msg);
}

// Reads the mic and streams it as binary frames while Speak is held.
//
// Reply playback is paused before capture, and physical release stops the mic.
// PCB06 local PTT feedback is an explicit exception: it never enters this PCM
// path digitally, but its acoustic pickup must be checked on the real board.
static void pumpMic() {
#ifdef RELAY_BOARD_06
  if (!recording) return;
  if (!wsOpen) { stopRecording(); return; }
  bool failed, discarded;
  {
    MicLock guard;
    failed = captureSpool->state() == Pcb06CaptureSpool::State::Failed;
    discarded = captureSpool->state() == Pcb06CaptureSpool::State::Discarded;
  }
  if (failed) { board06TransportFailed(); return; }
  if (discarded) { stopRecording(); return; }
  sendTalkStart();
  if (!recording || !wsOpen) return;

  Pcb06CaptureSpool::Chunk chunk;
  {
    MicLock guard;
    chunk = captureSpool->copyPending(board06TxChunk, MIC_FRAME);
  }
  if (chunk.samples) {
    // The capture worker keeps appending while this full frame is on the wire.
    const bool sent = wsSendBinary(reinterpret_cast<const uint8_t *>(board06TxChunk),
                                   chunk.samples * sizeof(int16_t));
    memset(board06TxChunk, 0, sizeof(board06TxChunk));
    bool consumed = false;
    if (sent) {
      MicLock guard;
      consumed = captureSpool->acknowledge(chunk);
    }
    if (!consumed) { board06TransportFailed(); return; }
    ++micFramesSent;
  }

  uint32_t generation = 0, heldMs = 0, samples = 0, capturedSamples = 0, clippedSamples = 0;
  int32_t peak = 0;
  uint64_t energy = 0;
  {
    MicLock guard;
    if (!captureSpool->needsEnd()) return;
    generation = captureSpool->generation();
    heldMs = captureSpool->durationMs(millis());
    samples = captureSpool->captured(); capturedSamples = micSamplesRead;
    clippedSamples = micClipped; peak = micPeak; energy = micEnergy;
  }
  if (!wsSendText("{\"v\":1,\"type\":\"talk.end\"}")) {
    board06TransportFailed();
    return;
  }
  bool finished;
  {
    MicLock guard;
    finished = generation == captureSpool->generation() && captureSpool->finish();
    if (finished) {
      if (rxChan) { i2s_del_channel(rxChan); rxChan = nullptr; }
      recording = false;
      talkSent = false;
    }
  }
  if (!finished) { board06TransportFailed(); return; }
  Serial.printf("[tx] talk.end (%lu ms held, peak %ld) uploaded %lu samples (captured %lu, trimmed %lu), rms %lu, clipped %lu\n",
      (unsigned long)heldMs, (long)peak, (unsigned long)samples,
      (unsigned long)capturedSamples, (unsigned long)(capturedSamples - samples),
      samples ? (unsigned long)sqrt(double(energy) / samples) : 0UL,
      (unsigned long)clippedSamples);
#else
  if (!recording) return;
  if (!wsOpen || digitalRead(buttons.talk) != LOW) {
    stopRecording();
    return;
  }
  if (rxChan == nullptr) return;

  // A latching switch left closed would stream forever.
  if (millis() - recStartMs >= MAX_RECORDING_MS) {
    Serial.println("[mic] 15 min limit - forcing talk.end");
    stopRecording();
    return;
  }

  static int32_t raw[256];

  // Take whatever is available. With timeout 0 this returns ESP_ERR_TIMEOUT
  // unless the whole request is already buffered - but it still reports the
  // partial count, and discarding that sent one frame in 25 seconds.
  size_t bytesRead = 0;
  bool captured;
  {
    MicLock guard;
    captured = micCapturing;
    if (captured) i2s_channel_read(rxChan, raw, sizeof(raw), &bytesRead, 4);
  }
  if (!captured) {
    stopRecording();
    return;
  }
  // A release during the bounded read must not contribute another batch.
  if (digitalRead(buttons.talk) != LOW) {
    stopRecording();
    return;
  }
  size_t n = bytesRead / sizeof(int32_t);
  // Bound the payload too: the audio clock can drift ahead of millis(), and
  // the relay rejects the entire span if even one sample exceeds its cap.
  const size_t remaining = MAX_RECORDING_SAMPLES - micSamplesRead;
  if (n > remaining) n = remaining;
  if (n == 0) return;

  for (size_t i = 0; i < n; i++) {
    int32_t v = raw[i] >> MIC_SHIFT;
    v = micHighPass.process(v);
    if (v > 32767 || v < -32768) micClipped++;
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    if (++micSamplesRead > 2400) micEnergy += uint64_t(int64_t(v) * v);  // skip filter settling
    int32_t mag = v < 0 ? -v : v;
    if (mag > micPeak) micPeak = mag;
    if (micBatchFill == MIC_BATCH) {
      // Only a stalled loop gets here before talk.start; keep the newest.
      memmove(micBatch, micBatch + MIC_FRAME,
              (micBatchFill - MIC_FRAME) * sizeof(micBatch[0]));
      micBatchFill -= MIC_FRAME;
    }
    micBatch[micBatchFill++] = (int16_t)v;
  }
  if (micSamplesRead >= MAX_RECORDING_SAMPLES) {
    stopRecording();
    return;
  }
  if (talkSent) sendMicFrames(false);
#endif
}

// ---------------------------------------------------------------------------
// Buttons and LED
// ---------------------------------------------------------------------------

static void ledSet(Rgb color) {
  ledcWrite(PIN_LED_R, ledDuty(color.red, ledCommonAnode));
  ledcWrite(PIN_LED_G, ledDuty(color.green, ledCommonAnode));
  ledcWrite(PIN_LED_B, ledDuty(color.blue, ledCommonAnode));
}

// One colour at a time, highest priority first. The two states the device
// knows by itself beat the two the server sets, because they are happening now.
//
// Blue carries work and playback. Complete unread speech always uses the same
// blinking green attention signal, including when the Agent expects an answer.
static void ledService() {
  // How long the cloud has been unreachable; a boot or blip is not an error.
  static uint32_t offlineSince = 0;
  static bool wasOnline = false;
  const uint32_t now = millis();
  if (wsOpen) wasOnline = true;
  else if (wasOnline || !offlineSince) { wasOnline = false; offlineSince = now; }

  StatusInputs in;
  in.asleep = asleep;
  in.pairing = museMode == MuseMode::Off      ? PairingLight::Off
               : museMode == MuseMode::Paired ? PairingLight::Paired
               : museMode == MuseMode::Failed ? PairingLight::Failed
                                              : PairingLight::Open;
#ifdef RELAY_BOARD_06
  bool uploading = false;
  if (micMutex) {
    MicLock guard;
    in.recording = micCapturing;
    uploading = recording && captureSpool && captureSpool->state() == Pcb06CaptureSpool::State::Draining;
  }
#else
  in.recording = recording;
#endif
  in.volumeChanged = volumeFeedback && uint32_t(now - volumeChangedAt) < 350;
  if (!in.volumeChanged) volumeFeedback = false;
  in.playing = playing;
  in.replyWaiting = playback.staged && ledMissed > 0;
  in.cloudConnected = wsOpen;
  in.offlineMs = wsOpen ? 0 : uint32_t(now - offlineSince);
  in.assistantReady = endpointAvailability.usable();
  in.assistantWorking = endpointAvailability.working();
#ifdef RELAY_BOARD_06
  if (uploading) { in.assistantWorking = true; in.assistantReady = true; }
#endif
  in.connectionInProgress = endpointAvailability.connecting();
#ifdef RELAY_BOARD_06
  // A plugged-in Device is charging until the charger stops AND the gauge
  // agrees it is full: CHRG alone also releases on taper or timeout.
  in.battery = board06Power.usb()
      ? (board06Power.charging() || !batteryWatch.full() ? BatteryLight::Charging
                                                         : BatteryLight::Charged)
      : BatteryLight::None;
#endif
  ledSet(statusLed(in, now));
}

// Soft off and on each restart the firmware into the other state, so nothing
// a stuck runtime holds (socket, TLS, modem session, audio, locks) survives
// the gesture. Saved networks, the credential and button map live in NVS, and
// unplayed replies in the cloud mailbox. The switched-off runtime starts only
// the light and Listen, then light-sleeps; the battery stays connected (about
// 1-2 mA), so this is not a storage power-off.
// setup() latches the modem supply low within milliseconds of a restart.
// Ask the cellular worker to power the module down in order first, and give
// it a bounded wait; with the modem already off this returns at once.
static void modemOffForRestart() {
#ifdef RELAY_CELLULAR
  cellularShutdown();
  const uint32_t start = millis();
  while (!cellularPoweredDown() && millis() - start < cellular_policy::kPowerOffBudgetMs) delay(50);
  if (!cellularPoweredDown()) Serial.println("[cell] power-off did not finish in time");
#endif
}

static void restartInto(bool off) {
  softOffMarker = off ? kSoftOffMarker : 0;
  Serial.printf("[power] %s - restarting\n", off ? "soft off" : "on");
  modemOffForRestart();
  esp_restart();
}

// The orderly half of switching off, at the top of loop(). Closing the socket
// lets the cloud see the Device leave now rather than at its ping timeout.
static void switchOff() {
  { MicLock guard; stopMicCapture(); }
  powerDownCue();
  tls.stop();   // lwip_close: queues a FIN, never waits on the peer
  delay(100);   // let the FIN out before the restart stops the radio
  restartInto(true);
}

// Times the Listen power hold apart from loop(), which a TLS handshake or a
// stuck send can block for tens of seconds. A running loop closes its socket
// first; a stalled one is cut off after kSoftOffGraceMs. Takes no locks.
static void powerTask(void *) {
  // The hold that restarted the firmware is usually still down.
  LongHold hold(digitalRead(buttons.play) == LOW);
  for (;;) {
    if (hold.update(digitalRead(buttons.play) == LOW, millis(),
                    asleep ? kSoftOnHoldMs : kSoftOffHoldMs)) {
      if (asleep && batteryEmpty) {
        switchOnRefused = true;  // too empty to run the modem; loop() shows it
      } else if (asleep) {
        restartInto(false);
      } else {
        offRequested = true;
        vTaskDelay(pdMS_TO_TICKS(kSoftOffGraceMs));
        restartInto(true);
      }
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// Light sleep until Play is pressed; stay awake while it is held so the
// switch-on hold can be timed. Stay awake briefly after a release too: if
// powerTask (polling every 10 ms) missed it, the next press would continue
// the old hold and switch on early.
static void softOffSleep() {
  static uint32_t releasedAt = 0;
  if (digitalRead(buttons.play) == LOW) { releasedAt = millis(); delay(20); return; }
  if (millis() - releasedAt < 50) { delay(10); return; }
#ifdef RELAY_BOARD_06
  // On USB power, stay awake to show the charging light; plugging in wakes it.
  if (digitalRead(PIN_USB_PRESENT_N) == LOW) { delay(20); return; }
  gpio_wakeup_enable((gpio_num_t)PIN_USB_PRESENT_N, GPIO_INTR_LOW_LEVEL);
#endif
  ledSet(kOff);  // a charging light must not freeze on through the sleep
  gpio_wakeup_enable((gpio_num_t)buttons.play, GPIO_INTR_LOW_LEVEL);
  esp_sleep_enable_gpio_wakeup();
  esp_light_sleep_start();
  gpio_wakeup_disable((gpio_num_t)buttons.play);
#ifdef RELAY_BOARD_06
  gpio_wakeup_disable((gpio_num_t)PIN_USB_PRESENT_N);
#endif
}

static void buttonService() {
#ifdef RELAY_BOARD_06
  // The action and its cue use exactly the same GPIO/time snapshot: a clock
  // tick or input edge between two readers must not cue the old volume.
  board06ButtonSample = {millis(), {
      digitalRead(buttons.talk) == LOW, digitalRead(buttons.play) == LOW,
      buttons.volumeUp >= 0 && digitalRead(buttons.volumeUp) == LOW,
      buttons.volumeDown >= 0 && digitalRead(buttons.volumeDown) == LOW}};
#endif
  static VolumeButton volumeUp, volumeDown;
  if (buttons.volumeUp >= 0 || buttons.volumeDown >= 0) {
#ifdef RELAY_BOARD_06
    const bool up = board06ButtonSample.pressed[2], down = board06ButtonSample.pressed[3];
    const bool raise = volumeUp.update(up, board06ButtonSample.now);
    const bool lower = volumeDown.update(down, board06ButtonSample.now);
#else
    const bool up = buttons.volumeUp >= 0 && digitalRead(buttons.volumeUp) == LOW;
    const bool down = buttons.volumeDown >= 0 && digitalRead(buttons.volumeDown) == LOW;
    const bool raise = volumeUp.update(up, millis());
    const bool lower = volumeDown.update(down, millis());
#endif
    if ((raise || lower) && up && down) {
      Serial.println("[vol] both inputs active; volume unchanged");
      volumeFeedback = true; volumeChangedAt = millis();
    } else if ((raise && !down) || (lower && !up)) {
      volume = constrain(volume + (raise ? 1 : -1), 0, 10);
      volumeFeedback = true; volumeChangedAt = millis();
      Serial.printf("[vol] %d/10\n", volume);
    }
  }
  // powerTask owns the Play power hold and restarts before its release. A
  // Play still held from that restart is not a press.
  static int lastListen = digitalRead(buttons.play);
  static int lastSpeak = HIGH, lastPower = HIGH;
  static uint32_t lastChange = 0;
  static bool listenPressedOnSnapshot = false;
  static uint32_t listenPressedGeneration = 0;
  if (millis() - lastChange < 15) return;          // crude debounce

#ifdef RELAY_BOARD_06
  int listen = board06ButtonSample.pressed[1] ? LOW : HIGH;
  int speak = board06ButtonSample.pressed[0] ? LOW : HIGH;
#else
  int listen = digitalRead(buttons.play);
  int speak  = digitalRead(buttons.talk);
#endif
  int power  = PIN_BTN_POWER >= 0 ? digitalRead(PIN_BTN_POWER) : HIGH;

  if (listen != lastListen) {
    lastChange = millis();
    if (listen == LOW) {                            // pressed
      if (playing || ringAvail() > 0) {
        // Keep the complete span and cursor for the next Listen.
        if (playing) pausePlayback(); else ringClear();
      } else if (playback.staged) {
        // Wait for release so a power hold cannot also start playback.
        listenPressedOnSnapshot = !recording && !playedReceipt.pending();
        listenPressedGeneration = playback.generation;
        if (listenPressedOnSnapshot) Serial.println("[btn] release to play");
      } else if (wsOpen) {
        if (endpointAvailability.usable()) {
          // Ask for a silent preload only. This press cannot authorize a
          // reply that arrives later, even if Listen is still held then.
#ifdef RELAY_BOARD_06
          board06ListenPending = true;
          board06ListenWaitForCue = !board06PlayCueAccepted;
          board06ListenRequestedAt = board06ButtonSample.now;
#else
          wsSendText("{\"v\":1,\"type\":\"listen\"}");
          Serial.println("[tx] listen");
#endif
        } else {
          Serial.println("[btn] listen ignored - Agent Endpoint unavailable");
        }
      }
    } else if (listenPressedOnSnapshot) {            // released
      if (classifyListenRelease(true, playback.staged) == ListenReleaseAction::Play
          && listenPressedGeneration == playback.generation && playbackPlayStaged()) {
        Serial.println("[btn] playing staged response");
      }
      listenPressedOnSnapshot = false;
    }
    lastListen = listen;
  }

  // Momentary hold-to-talk: release always ends the current recording.
  // Quick taps never leave the Device; three of them open Muse pairing.
  static PttTaps pttTaps;
  static uint32_t pttPressedAt = 0;
  static bool pttClosedMuse = false;
  if (speak != lastSpeak) {
    lastChange = millis();
    if (speak == LOW) {
      pttPressedAt = millis();
      pttClosedMuse = museMode != MuseMode::Off && museMode != MuseMode::Paired;
      if (museMode != MuseMode::Off) museStop();
      if (!pttClosedMuse) startRecording();
    } else {
      stopRecording();
      if (!pttClosedMuse && pttTaps.release(pttPressedAt, millis())) museStart();
    }
    lastSpeak = speak;
  }

  if (power != lastPower) {
    lastChange = millis();
    if (power == LOW) offRequested = true;
    lastPower = power;
  }
}

// ---------------------------------------------------------------------------
// Diagnostics, unchanged from the bring-up sketch
// ---------------------------------------------------------------------------

#ifdef RELAY_BOARD_06
// Called after buttonService: volume changes and immediate PTT capture take
// effect first. Play/Volume release and hold-repeat never queue a click; Talk
// has a separate physical-input envelope in the audio task.
static void board06ButtonCueService() {
  static Pcb06CueDownstroke edges[4];
  static bool wasSilent = false;
  const bool silent = muted || volume == 0;
  if (silent && !wasSilent) board06CueQueue.cancel();
  wasSilent = silent;
  const Pcb06CueKind kinds[] = {Pcb06CueKind::Talk, Pcb06CueKind::Play,
                               Pcb06CueKind::VolumeUp, Pcb06CueKind::VolumeDown};
  const char *names[] = {"talk", "play", "volume-up", "volume-down"};
  const bool bothVolume = board06ButtonSample.pressed[2] && board06ButtonSample.pressed[3];
  const uint32_t now = board06ButtonSample.now;
  for (size_t i = 1; i < 4; ++i) {
    if (!edges[i].update(board06ButtonSample.pressed[i], now)) continue;
    if (i == 1) { board06PlayCueAccepted = true; board06ListenWaitForCue = false; }
    if (i >= 2 && bothVolume) continue;
    const bool queued = board06CueQueue.push(kinds[i], volume, silent, now);
    Serial.printf("[cue] %s volume=%d %s\n", names[i], volume,
                  queued ? "queued" : silent ? "silent" : "queue-full");
  }
  if (!board06ButtonSample.pressed[1]) board06PlayCueAccepted = false;
  if (board06ListenPending && (!board06ButtonSample.pressed[1]
      || uint32_t(now - board06ListenRequestedAt) >= Pcb06CueQueue::kMaxAgeMs))
    board06ListenWaitForCue = false;
}

// Only run after cue sampling has accepted this downstroke (or observed its
// release/expiry), so a slow TLS send cannot hold up the local Play click.
static void board06ServiceListenRequest() {
  if (!board06ListenPending || board06ListenWaitForCue) return;
  board06ListenPending = false;
  if (!wsOpen || !endpointAvailability.usable() || recording || offRequested) return;
  wsSendText("{\"v\":1,\"type\":\"listen\"}");
  Serial.println("[tx] listen");
}
#endif

static void toneSweep() {
  Serial.println("[tone] 300 -> 1200 Hz sweep, 3 s");
  const int total = SAMPLE_RATE_HZ * 3;
  static int16_t buf[240];
  float phase = 0.0f;
  int written = 0;
  while (written < total) {
    for (int i = 0; i < 240; i++) {
      float t = (float)(written + i) / total;
      float freq = 300.0f + t * 900.0f;
      phase += 2.0f * PI * freq / SAMPLE_RATE_HZ;
      if (phase > 2 * PI) phase -= 2 * PI;
      buf[i] = (int16_t)(sinf(phase) * 12000);
    }
    ringPush(buf, 240);
    written += 240;
    while (ringAvail() > RING_SZ / 2) { delay(2); esp_task_wdt_reset(); }
  }
  Serial.println("[tone] queued");
}

static void ledTest() {
  struct { Rgb color; const char *name; } steps[] = {
    {kOff, "OFF"}, {kWhite, "WHITE (talking; aqua on the 06 board)"},
    {kBlue, "BLUE (working / speaking)"}, {kRed, "RED"},
    {kOrange, "ORANGE (charging)"}, {kGreen, "GREEN (reply waiting / charged)"},
    {kOff, "OFF"},
  };
  Serial.printf("[led] assuming common %s\n",
                ledCommonAnode ? "ANODE" : "CATHODE");
  for (auto &st : steps) {
    ledSet(st.color);
    Serial.printf("[led] %s\n", st.name);
    delay(1200);
    esp_task_wdt_reset();
  }
}

static void buttonMonitor() {
  const int pins[] = {buttons.play, buttons.talk, PIN_BTN_POWER,
                     buttons.volumeUp, buttons.volumeDown};
  const char *nm[] = {"LISTEN", "PTT", "ON/OFF", "VOLUME UP", "VOLUME DOWN"};
  const int n = sizeof(pins) / sizeof(pins[0]);
  int last[n];
  Serial.println("[btn] resting state - connected buttons should read HIGH:");
  for (int i = 0; i < n; i++) {
    last[i] = pins[i] >= 0 ? digitalRead(pins[i]) : HIGH;
    if (pins[i] >= 0) Serial.printf("[btn] %-12s GPIO%d %s\n", nm[i], pins[i], last[i] ? "HIGH" : "LOW");
  }
  uint32_t t0 = millis();
  while (millis() - t0 < 25000) {
    for (int i = 0; i < n; i++) {
      if (pins[i] < 0) continue;
      int now = digitalRead(pins[i]);
      if (now != last[i]) {
        last[i] = now;
        Serial.printf("[btn] %-14s %s\n", nm[i], now ? "released" : "PRESSED");
      }
    }
    delay(15);
    esp_task_wdt_reset();
    if (Serial.available()) { Serial.read(); break; }
  }
  Serial.println("[btn] done");
}

// A square-wave tone (0 Hz: a gap) through the internal ring, which the audio
// task plays whenever no reply is. Talk clears it on the 06 board.
static void pushTone(int freq, int ms) {
  if (!spkRing) return;  // the switched-off runtime has no speaker
  static int16_t buf[240];
  const int half = freq ? SAMPLE_RATE_HZ / freq / 2 : 0;
  int phase = 0; bool high = true;
  for (int blk = 0; blk < SAMPLE_RATE_HZ / 1000 * ms / 240; blk++) {
    for (int i = 0; i < 240; i++) {
      buf[i] = !freq ? 0 : high ? 9000 : -9000;
      if (freq && ++phase >= half) { phase = 0; high = !high; }
    }
    ringPush(buf, 240);
    while (ringAvail() > RING_SZ / 2) delay(2);
  }
}

static void waitForTones(uint32_t limitMs) {
  const uint32_t t0 = millis();
  while (ringAvail() > 0 && millis() - t0 < limitMs) delay(10);
}

// Rising chirp on switch-on; battery level does not add an LED pattern.
static void bootChirp() {
  pushTone(800, 120);
  pushTone(1200, 120);
  waitForTones(1500);
  delay(150);
  ledSet(kOff);
}

// The 5 s hold has landed: a falling chirp and a fade say "let go". Stays well
// inside kSoftOffGraceMs, after which powerTask restarts regardless.
static void powerDownCue() {
  pushTone(1200, 120);
  pushTone(800, 120);
  for (int level = 255; level > 0; level -= 15) {
    ledSet(scaled(kWhite, uint8_t(level)));
    delay(20);
  }
  ledSet(kOff);
  waitForTones(300);
}

// The battery is empty: keep the LED dark and give three low beeps when the
// speaker is running, then switch off or remain off.
static void showBatteryEmpty() {
  Serial.println("[battery] empty - charge before switching on");
  ledSet(kOff);
  for (int i = 0; i < 3; ++i) {
    pushTone(520, 150);
    delay(500);
  }
}

#ifdef RELAY_BOARD_06
// The first gauge read, before the modem, speaker or microphone are powered.
// Returns whether this boot runs switched off. A boot that would start on an
// empty battery (switched on, or restarted by the brownout it just caused)
// stays off instead, and so does every restart after it, until it is charged.
static bool board06StartsOff(bool asleepAtBoot) {
  const auto allowed = []() {
    BatteryReading reading{};
    const bool read = readBatteryGauge(Wire, reading);
    return batteryAllowsStart(read, reading.roundedPercent(), reading.volts,
                              digitalRead(PIN_USB_PRESENT_N) == LOW);
  };
  // Two empty readings, as switching off while running takes: one unsettled
  // sample just after a reset must not keep a charged Device off.
  bool empty = !allowed();
  if (empty) {
    delay(500);
    empty = !allowed();
  }
  batteryEmpty = empty;
  if (asleepAtBoot || !batteryEmpty) return asleepAtBoot;
  Serial.println("[battery] empty at start - staying off");
  softOffMarker = kSoftOffMarker;
  switchOnRefused = true;  // loop() gives the empty-battery signal once
  return true;
}

// Reads the gauge every 30 s and at once after a plug change, once the power
// state has settled. Returns a warning that is now due, if any.
static BatteryAlert batteryService() {
  static uint32_t sampledAt = 0;
  static bool sampled = false, wasUsb = false;
  if (!board06Power.state()) return BatteryAlert::None;
  const bool usb = board06Power.usb();
  const uint32_t now = millis();
  if (sampled && usb == wasUsb && uint32_t(now - sampledAt) < 30000) return BatteryAlert::None;
  sampled = true;
  wasUsb = usb;
  sampledAt = now;
  BatteryReading reading;
  if (!readBatteryGauge(Wire, reading)) {
    batteryWatch.unknown();
    batteryEmpty = false;
    volumeCeiling = 10;
    return BatteryAlert::None;
  }
  const int percent = reading.roundedPercent();
  const BatteryAlert alert =
      batteryWatch.sample(percent, reading.volts, usb, recording || playing);
  batteryEmpty = !usb && batteryEmptyNow(percent, reading.volts);
  volumeCeiling = batteryWatch.volumeCeiling();
  return alert;
}

// Warnings wait until Talk and playback are done. At empty the Device switches
// itself off; soft off keeps every reply in the cloud mailbox.
static void board06BatteryWarnings(BatteryAlert alert) {
  static bool pending = false;
  if (alert != BatteryAlert::None) {
    pending = true;
    Serial.printf("[battery] %s\n", alert == BatteryAlert::Low ? "low" : "very low");
  }
  if (recording || playing || offRequested) return;
  if (batteryWatch.empty()) {
    showBatteryEmpty();
    offRequested = true;
    return;
  }
  if (pending) {
    pending = false;
    pushTone(660, 100);  // two short low beeps
    pushTone(0, 80);
    pushTone(660, 100);
  }
}
#endif

#ifdef RELAY_BOARD_06
static void board06Status() {
  Serial.println("[board] PCB06: Talk1 Play5 Vol+2 Vol-4; RGB47/48/44 active-low");
  Serial.printf("[board] PDM CLK6 DATA7 24000Hz stereo -> slot %d; speaker I2S1 48000Hz BCLK10 WS11 DATA40 EN13\n",
                RELAY_PCB06_MIC_SLOT);
  Serial.printf("[mic] input gain=%dx (after high-pass, before PCM16 clamp)\n",
                RELAY_PCB06_MIC_GAIN);
  Serial.printf("[ptt] held static gain=%d/%d; opening/closing unchanged\n",
                Pcb06PttPlayer::kBedGainNumerator, Pcb06PttPlayer::kBedGainDenominator);
  Serial.printf("[mic] opening crop=%lu ms when feedback is audible at press; no end crop\n",
                (unsigned long)(pcb06_ptt_sounds::kPressSamples * 1000 / SAMPLE_RATE_HZ));
  Serial.printf("[board] flash=%lu PSRAM=%lu USB=%s mux=%d amp=%d\n",
      (unsigned long)ESP.getFlashChipSize(), (unsigned long)ESP.getPsramSize(),
      digitalRead(PIN_USB_PRESENT_N) == LOW ? "present" : "absent",
      digitalRead(PIN_USB_MUX_SEL), digitalRead(PIN_AMP_SD));
  BatteryReading reading;
  if (readBatteryGauge(Wire, reading))
    Serial.printf("[battery] MAX17048 %.3f V %.1f%% (estimate); charge-status=%s\n",
      reading.volts, reading.percent,
      digitalRead(PIN_CHG_STAT_N) == LOW ? "asserted" : "not asserted");
  else Serial.println("[battery] gauge unavailable");
  Serial.printf("[power] %s; recent stable inputs (uptime ms, USB, CHRG):\n",
                board06Power.state() ? board06Power.state() : "unknown");
  for (unsigned i = 0; i < board06Power.count(); ++i) {
    const auto sample = board06Power.observation(i);
    Serial.printf("[power] %lu usb=%d charge=%d\n",
                  (unsigned long)sample.at, sample.usb, sample.charge);
  }
}

struct Board06MicStats {
  uint32_t frames = 0, windows = 0;
  double rms[2] = {}, peakWindowRms[2] = {};
  int32_t peak[2] = {};
};
// Explicit declarations keep Arduino's generated prototypes after this type.
static void board06MeasureMic(uint32_t durationMs, MicHighPass *filters, Board06MicStats &result);
static void board06PrintMic(const char *label, const Board06MicStats &result);

static void board06MeasureMic(uint32_t durationMs, MicHighPass *filters, Board06MicStats &result) {
  int16_t raw[256 * 2];
  int64_t sum[2] = {}, squares[2] = {};
  uint32_t windowFrames = 0;
  const uint32_t started = millis();
  while ((uint32_t)(millis() - started) < durationMs) {
    size_t bytes = 0;
    i2s_channel_read(rxChan, raw, sizeof(raw), &bytes, 40);
    for (size_t i = 0; i < bytes / (2 * sizeof(int16_t)); ++i) {
      for (int slot = 0; slot < 2; ++slot) {
        const int32_t sample = filters[slot].process(raw[2 * i + slot]);
        sum[slot] += sample;
        squares[slot] += int64_t(sample) * sample;
        const int32_t magnitude = sample < 0 ? -sample : sample;
        if (magnitude > result.peak[slot]) result.peak[slot] = magnitude;
      }
      ++result.frames;
      if (++windowFrames == SAMPLE_RATE_HZ / 10) {
        for (int slot = 0; slot < 2; ++slot) {
          const double mean = double(sum[slot]) / windowFrames;
          const double variance = double(squares[slot]) / windowFrames - mean * mean;
          const double rms = sqrt(variance > 0 ? variance : 0);
          result.rms[slot] += rms;
          if (rms > result.peakWindowRms[slot]) result.peakWindowRms[slot] = rms;
          sum[slot] = squares[slot] = 0;
        }
        ++result.windows;
        windowFrames = 0;
      }
    }
    esp_task_wdt_reset();
    delay(1);
  }
  if (result.windows) for (double &rms : result.rms) rms /= result.windows;
}

static void board06PrintMic(const char *label, const Board06MicStats &result) {
  Serial.printf("[mic-local] %s: frames=%lu L rms=%.1f peak-rms=%.1f peak=%ld; R rms=%.1f peak-rms=%.1f peak=%ld\n",
      label, (unsigned long)result.frames, result.rms[0], result.peakWindowRms[0], (long)result.peak[0],
      result.rms[1], result.peakWindowRms[1], (long)result.peak[1]);
}

static void board06MicDiagnostic() {
  if (recording || playing || ringAvail()) {
    Serial.println("[mic-local] finish Talk/playback before testing");
    return;
  }
  MicLock guard;
  if (micCapturing) return;
  if (!rxChan && !setupMic()) { Serial.println("[mic-local] PDM setup failed"); return; }
  if (i2s_channel_enable(rxChan) != ESP_OK) {
    i2s_del_channel(rxChan); rxChan = nullptr;
    Serial.println("[mic-local] PDM start failed");
    return;
  }
  // A requested, local-only capture; the network loop is not called here.
  // Keep micCapturing false: the diagnostic owns the channel under micMutex.
  MicHighPass filters[2];
  Board06MicStats settle, quiet, speech;
  board06MeasureMic(300, filters, settle);
  Serial.println("[mic-local] BLUE: quiet for 3 seconds, then GREEN: speak for 7 seconds. No upload.");
  ledSet(kBlue);
  delay(1500);
  board06MeasureMic(3000, filters, quiet);
  ledSet(kGreen);
  board06MeasureMic(7000, filters, speech);
  i2s_channel_disable(rxChan);
  i2s_del_channel(rxChan); rxChan = nullptr;
  ledSet(kOff);
  board06PrintMic("quiet", quiet);
  board06PrintMic("speech", speech);
  Serial.printf("[mic-local] mono upload uses slot %d; capture stopped. Compare both slots before qualification.\n",
                RELAY_PCB06_MIC_SLOT);
}

// Returns true while it owns a CELL/APN line. Never waits for more bytes.
static bool board06SerialService() {
  static char line[128];
  static size_t length = 0;
  static bool active = false, overflow = false;
  if (!active) {
    if (!Serial.available()) return false;
    if (Serial.peek() != 'C' && Serial.peek() != 'A') {
      if (!recording) return false;
      for (int i = 0; i < 32 && Serial.available(); ++i) Serial.read();
      return true;  // Ignore diagnostics during Talk without interrupting capture.
    }
    active = true;
  }
  for (int i = 0; i < 32 && Serial.available(); ++i) {
    const char c = Serial.read();
    if (c == '\n') {
      line[length] = '\0';
      if (overflow) Serial.println("[cell] command too long; ignored");
      else {
        String command(line);
        command.trim();
        if ((recording || playing) && command != "CELL STATUS")
          Serial.println("[cell] finish Talk/playback before changing modem settings");
        else if (!cellularCommand(command))
          Serial.println("[cell] unknown command; use CELL STATUS/START/STOP or CELL APN <value>");
      }
      length = 0; active = false; overflow = false;
      return true;
    }
    if (!overflow) {
      if (length + 1 < sizeof(line)) line[length++] = c;
      else overflow = true;
    }
  }
  return true;
}
#endif

static void help() {
  Serial.println();
  Serial.println("=== relay client ===");
  Serial.printf("  endpoint  wss://%s%s\n", HOST, PATH);
#ifdef RELAY_BOARD_06
  Serial.println("  g   PCB06 hardware status; j local stereo PDM mic check (no upload)");
  Serial.println("  CELL STATUS / CELL START / CELL STOP; CELL APN <value> (modem off)");
#endif
  Serial.println("  p   provisioning status");
  Serial.println("  PROVISION <credential>   store/replace credential in NVS");
  Serial.println("  t   local tone sweep (no network)");
  Serial.println("  l   LED test    L flip anode/cathode    b button monitor");
  Serial.println("  n   send listen (as if the top button)");
  Serial.println("  Q   open Muse pairing (as if PTT were tapped three times)");
  Serial.println("  c   send cancel     S send status");
  Serial.println("  i   mic diagnostic  + - volume   < > gain   M amp mute");
#ifdef RELAY_CELLULAR
  Serial.println("  x   disconnect      m cellular status             h help");
#else
  Serial.println("  x   disconnect      w wifi scan  m station MAC  h help");
  Serial.println("  WIFI <name>  join and save a network (password on the next line)");
  Serial.println("  WIFI  list saved networks    WIFI FORGET <name>  remove one");
#endif
  Serial.println();
}

#ifdef RELAY_CELLULAR
static int networkRssi() {
  return cellularRssiDbm();
}

static bool networkConnected() {
  return cellularConnected();
}

static void networkService() {
  cellularService();
}
#else
static int networkRssi() {
  return WiFi.RSSI();
}

static bool networkConnected() {
  return WiFi.status() == WL_CONNECTED;
}

static void wifiService() {
  savedWifi.service(millis());
  static bool announced = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!announced) {
      announced = true;
      Serial.printf("[wifi] up, IP %s, RSSI %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
    }
    return;
  }
  announced = false;
}

static void networkService() {
  wifiService();
}
#endif

// ---------------------------------------------------------------------------

#ifdef RELAY_BOARD_06
// Downloads in the background while idle and charged enough, restarts into a
// staged image only after 15 minutes without use, and rolls a new image back
// if it never reaches the relay. Manual builds never get this far.
static void board06OtaService() {
#ifdef RELAY_OFFICIAL_BUILD
  const uint32_t now = millis();
  const bool *pressed = board06ButtonSample.pressed;
  const bool busy = recording || playing || offRequested;
  if (busy || pressed[0] || pressed[1] || pressed[2] || pressed[3] ||
      (playback.staged && playback.played) || museMode != MuseMode::Off)
    board06LastUseMs = now;
  if (board06OtaUnproven) {
    // The deadline starts at LTE registration, so poor coverage costs nothing.
    if (!board06RegisteredAt && networkConnected()) board06RegisteredAt = now | 1;
    if (board06RegisteredAt && uint32_t(now - board06RegisteredAt) >= kOtaVerifyDeadlineMs) {
      Serial.println("[ota] new image never reached the relay; rolling back");
      pcb06_ota::rollBack();
    }
    return;
  }
  const char *target = board06Ota.record().target;
  const bool current = !strcmp(pcb06_ota::job.version, target);  // not withdrawn meanwhile
  switch (pcb06_ota::state) {
    case pcb06_ota::Download::Running:
      if (busy || !wsOpen) pcb06_ota::stopRequested = true;
      return;
    case pcb06_ota::Download::Staged:
      if (current) {
        board06Ota.downloadStaged();
        board06OtaSave();
        board06OtaStatus("staged", target);
      }
      break;
    case pcb06_ota::Download::Failed:
      if (current) {
        const uint32_t wait = board06Ota.downloadFailed(now, esp_random());
        board06OtaSave();
        board06OtaStatus("retrying", target, wait);
      }
      break;
    case pcb06_ota::Download::Stopped:
      board06Ota.downloadDeferred(now);
      break;
    case pcb06_ota::Download::Idle:
      break;
  }
  pcb06_ota::state = pcb06_ota::Download::Idle;
  if (!wsOpen || !board06OtaFresh || busy) return;
  if (board06Ota.wantsInstall(now, board06LastUseMs)) {
    Serial.printf("[ota] installing %s after 15 idle minutes\n", board06Ota.record().staged);
    board06Ota.installing();
    board06OtaSave();
    if (board06Ota.dirty() || !pcb06_ota::install()) {
      // Unsaved or unbootable: count a failed boot and download it again.
      board06Ota.boot(RELAY_FW_SEMVER, now);
      board06OtaSave();
      return;
    }
    tls.stop();
    delay(100);
    restartInto(false);
  }
  if (!board06OtaHasImage || !board06Ota.wantsDownload(now)) return;
  // The modem can brown out on transmit bursts when the battery is low.
  BatteryReading battery;
  if (digitalRead(PIN_USB_PRESENT_N) != LOW &&
      !(readBatteryGauge(Wire, battery) && battery.percent >= kOtaMinBatteryPercent)) {
    board06Ota.downloadDeferred(now);
    return;
  }
  if (!pcb06_ota::signedByRelease(board06OtaImage)) {
    Serial.println("[ota] offer is not signed by the release key");
    const uint32_t wait = board06Ota.downloadFailed(now, esp_random());
    board06OtaSave();
    board06OtaStatus("retrying", target, wait);
    return;
  }
  if (!pcb06_ota::start(board06OtaImage)) {
    board06Ota.downloadDeferred(now);
    return;
  }
  board06OtaStatus("downloading", target);
#endif
}
#endif

// An explicit declaration keeps Arduino's generated prototype after the type.
static BootCause bootCause(int resetReason);
static BootCause bootCause(int resetReason) {
  switch (resetReason) {
    case ESP_RST_POWERON: return BootCause::PowerOn;
    case ESP_RST_SW: return BootCause::Requested;
    case ESP_RST_PANIC:
    case ESP_RST_CPU_LOCKUP: return BootCause::Crash;
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT: return BootCause::Watchdog;
    case ESP_RST_BROWNOUT:
    case ESP_RST_PWR_GLITCH: return BootCause::Brownout;
    default: return BootCause::Other;
  }
}

// A loop() stuck for 20 s panics. The restart keeps the on/off marker, so a
// stuck on runtime comes back on and a stuck off runtime stays off.
static void watchLoop() {
  esp_task_wdt_config_t wdt = {.timeout_ms = kLoopWatchdogMs, .idle_core_mask = 0,
                               .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(nullptr);
}

void setup() {
#ifdef RELAY_BOARD_06
  // Preload the output latches before enabling them, ahead of blocking startup.
  for (int pin : {PIN_USB_MUX_SEL, PIN_MODEM_PWR_EN, PIN_MODEM_PWRKEY,
                  PIN_MODEM_RESET, PIN_MODEM_USB_BOOT, PIN_MODEM_DTR, PIN_AMP_SD}) {
    gpio_set_level((gpio_num_t)pin, 0);
    // Arduino 3.x digitalWrite requires pinMode's peripheral-manager ownership.
    // A raw gpio_set_direction enables output but leaves later writes ignored.
    pinMode(pin, OUTPUT);
  }
  pinMode(PIN_BTN_BOOT, INPUT);
  pinMode(PIN_USB_PRESENT_N, INPUT);
  pinMode(PIN_CHG_STAT_N, INPUT);
  pinMode(PIN_MODEM_RI, INPUT);
#endif
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE  // Serial is HWCDC
  // Plugged into a computer with no app reading the port, each print otherwise
  // waits up to 20 x 100 ms. The [stat] line alone then stalls loop() for most
  // of every 3 s: late PTT, and mic DMA overflow that loses most speech.
  Serial.setTxTimeoutMs(0);
#endif
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2500) delay(10);

  Serial.println();
  Serial.printf("=== relay client %s ===\n", FW_VERSION);
  Serial.printf("chip %s, heap %lu\n", ESP.getChipModel(),
                (unsigned long)ESP.getFreeHeap());
  const BootCause cause = bootCause(esp_reset_reason());
  Serial.printf("[boot] cause=%s\n", bootCauseName(cause));
  // The firmware restarting itself gets no chirp; an update restart neither.
  bool quietBoot = bootIsUnattended(cause);

#ifndef RELAY_OFFICIAL_BUILD
  Serial.println("[build] custom build; automatic updates off");
#endif

  asleep = bootsOff(softOffMarker, esp_reset_reason() == ESP_RST_POWERON);
  if (!asleep) softOffMarker = 0;
  // Before anything is powered: a third brownout restart in a row stays off,
  // like an empty battery, until someone charges it and switches it on.
  if (brownoutStaysOff(brownoutRecord, cause) && !asleep) {
    Serial.println("[power] repeated brownout restarts - staying off");
    asleep = true;
    softOffMarker = kSoftOffMarker;
    switchOnRefused = true;  // loop() gives the empty-battery signal once
  }
  loadProvisioning();
  loadButtonMap();
  loadVolume();
#ifdef RELAY_BOARD_06
  board06Ota = OtaPolicy(pcb06_ota::load());
  const OtaBoot otaBoot = board06Ota.boot(RELAY_FW_SEMVER, millis());
  board06OtaUnproven = pcb06_ota::unproven();
  // Already kept, or the same version arrived by cable: nothing left to prove.
  if (otaBoot == OtaBoot::Unproven && !board06OtaUnproven) board06Ota.proven();
  if (otaBoot == OtaBoot::RolledBack)
    Serial.printf("[ota] the last update did not start (failed boots: %u)\n",
                  unsigned(board06Ota.record().failedBoots));
  if (board06OtaUnproven) Serial.println("[ota] new image: waiting to reach the relay");
  // ponytail: a switch-on during a rolled-back first run is also quiet; rare.
  board06QuietBoot = board06OtaUnproven || otaBoot == OtaBoot::RolledBack;
  quietBoot = quietBoot || board06QuietBoot;
  board06OtaSave();
#endif
  pinMode(buttons.play, RELAY_BUTTON_INPUT_MODE);
  pinMode(buttons.talk, RELAY_BUTTON_INPUT_MODE);

  // Hardware PWM keeps every color dim even while the loop waits on networking.
  // Set the inactive level before attaching to avoid a full-bright boot flash.
  for (int pin : {PIN_LED_R, PIN_LED_G, PIN_LED_B}) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, ledCommonAnode ? HIGH : LOW);
    if (!ledcAttach(pin, 5000, 8)) Serial.println("[led] PWM setup failed");
    ledcWrite(pin, ledDuty(0, ledCommonAnode));
  }
  ledSet(kOff);
#ifdef RELAY_BOARD_06
  // Both runtimes read the fuel gauge: switched off, for the charging light
  // and to refuse switching on with an empty battery.
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);
  Wire.setTimeOut(20);
  asleep = board06StartsOff(asleep);
#endif

  // 4 KiB: esp_restart() runs the Wi-Fi shutdown handler on this stack.
  if (xTaskCreate(powerTask, "power", 4096, nullptr, 5, nullptr) != pdPASS) {
    Serial.println("FATAL: power switch unavailable");
    while (true) delay(1000);
  }
  if (asleep) {
    // No network, microphone, speaker or audio task while switched off.
    if (PIN_AMP_SD >= 0) { pinMode(PIN_AMP_SD, OUTPUT); digitalWrite(PIN_AMP_SD, LOW); }
    watchLoop();
    Serial.println("[power] off - hold Listen 2 s to switch on");
    return;
  }

  playbackSpan = (uint8_t *)heap_caps_malloc(
      PLAYBACK_CAP_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!playbackSpan) {
    Serial.printf("FATAL: need %lu bytes of PSRAM for complete audio staging "
                  "(largest block %lu)\n",
                  (unsigned long)PLAYBACK_CAP_BYTES,
                  (unsigned long)heap_caps_get_largest_free_block(
                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    while (true) delay(1000);
  }
  mp3Playback = new (std::nothrow) Mp3Playback();
  playbackMutex = xSemaphoreCreateMutex();
  if (!mp3Playback || !mp3Playback->available() || !playbackMutex) {
    Serial.println("FATAL: no memory for the MP3 decoder");
    while (true) delay(1000);
  }

#ifdef RELAY_BOARD_06
  int16_t *captureStorage = (int16_t *)heap_caps_malloc(
      Pcb06CaptureSpool::kMaxSamples * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (captureStorage) captureSpool = new (std::nothrow) Pcb06CaptureSpool(captureStorage, Pcb06CaptureSpool::kMaxSamples);
  if (!captureSpool) { Serial.println("FATAL: need 2.88 MB PSRAM for 60 s Talk spool"); while (true) delay(1000); }
#else
  micBatch = (int16_t *)heap_caps_malloc(MIC_BATCH * sizeof(int16_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!micBatch) { Serial.println("FATAL: no PSRAM for mic buffer"); while (true) delay(1000); }
#endif
  spkRing = (int16_t *)heap_caps_malloc(RING_SZ * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!spkRing) { Serial.println("FATAL: no PSRAM for tone ring"); while (true) delay(1000); }
  Serial.printf("[audio] %lu-byte MP3 reply buffer; PSRAM free %lu of %lu\n",
                (unsigned long)PLAYBACK_CAP_BYTES,
                (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                (unsigned long)heap_caps_get_total_size(MALLOC_CAP_SPIRAM));

  wsBuf = (uint8_t *)malloc(WS_BUF);
  if (!wsBuf) { Serial.println("FATAL: no RAM for ws buffer"); while (true) delay(1000); }

  if (!setupI2S()) { Serial.println("FATAL: speaker I2S failed"); while (true) delay(1000); }
  micMutex = xSemaphoreCreateMutex();
  if (!micMutex) { Serial.println("FATAL: no mic lock"); while (true) delay(1000); }
  if (!setupMic()) { Serial.println("WARN: mic I2S failed - speaker only"); }

  // 8 KiB: the MP3 decoder runs on this stack (its scratch is in PSRAM).
  if (xTaskCreatePinnedToCore(audioTask, "audioOut", 8192, nullptr, 6, nullptr, 0) != pdPASS) {
    Serial.println("FATAL: audio task allocation failed");
    while (true) delay(1000);
  }

#ifdef RELAY_BOARD_06
  if (xTaskCreatePinnedToCore(board06CaptureTask, "micCapture", 4096, nullptr, 7, nullptr, 0) != pdPASS) {
    Serial.println("FATAL: microphone capture worker unavailable");
    while (true) delay(1000);
  }
#endif
  if (xTaskCreatePinnedToCore(micGuardTask, "micGuard", 2048, nullptr, 7, nullptr, 0) != pdPASS) {
    Serial.println("FATAL: microphone release guard unavailable");
    while (true) delay(1000);
  }
  if (PIN_BTN_POWER >= 0) pinMode(PIN_BTN_POWER, INPUT_PULLUP);
  if (buttons.volumeUp >= 0) pinMode(buttons.volumeUp, RELAY_BUTTON_INPUT_MODE);
  if (buttons.volumeDown >= 0) pinMode(buttons.volumeDown, RELAY_BUTTON_INPUT_MODE);

#ifndef RELAY_BOARD_06
  if (PIN_AMP_SD >= 0) { pinMode(PIN_AMP_SD, OUTPUT); digitalWrite(PIN_AMP_SD, HIGH); }
#endif

#ifdef RELAY_CELLULAR
  if (!cellularSetup()) {
    Serial.println("[cell] ERROR modem subsystem did not start");
  }
#else
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  Serial.printf("[wifi] station MAC %s\n", WiFi.macAddress().c_str());
  setupSavedWifi();
#endif

  watchLoop();

  // An update restarts the Device by itself after 15 idle minutes, maybe at
  // night: no chirp or flash for the new image's first run, or for the old
  // image coming back after a failed one. The same after a crash, watchdog or
  // brownout restart.
  if (!quietBoot) bootChirp();
  help();
}

void loop() {
  esp_task_wdt_reset();
  static bool settled = false;
  if (!settled && !asleep && millis() >= kBrownoutSettledMs) {
    settled = true;
    brownoutSettled(brownoutRecord);
  }
#ifdef RELAY_BOARD_06
  board06Power.update(digitalRead(PIN_USB_PRESENT_N) == LOW,
                      digitalRead(PIN_CHG_STAT_N) == LOW, millis());
  const BatteryAlert batteryAlert = batteryService();
#endif
  if (offRequested) switchOff();
  if (asleep) {
    // Switched off: powerTask watches Listen; no network, no audio.
    if (switchOnRefused) {
      switchOnRefused = false;
      showBatteryEmpty();
    }
    ledService();
    softOffSleep();
    return;
  }
  // Talk, playback, a reply arriving, unread bytes or pairing keep it running.
  if (!recording && !playing && !playbackReceiving && !offRequested &&
      museMode == MuseMode::Off && !(wsOpen && tls.available()))
    delay(IDLE_NAP_MS);
  networkService();

  // A new IP session needs a fresh socket, with jitter after the first boot dial.
  static bool hadNetwork = false;
  bool haveNetwork = networkConnected();
  if (!haveNetwork && hadNetwork) {
    // An old TCP socket cannot survive moving to a different access point.
    if (wsOpen) connectionEnded();
    dataPath.bearerDown();
  }
  if (haveNetwork && !hadNetwork) retryNow();
  hadNetwork = haveNetwork;

  buttonService();
  volumeSaveService();
#ifdef RELAY_BOARD_06
  board06ButtonCueService();
  board06ServiceListenRequest();
  board06BatteryWarnings(batteryAlert);
  board06OtaService();
#endif
  ledService();
  museService();
#ifdef RELAY_BOARD_06
  const bool board06SerialBusy = board06SerialService();
#endif

#ifndef RELAY_CELLULAR
  if (wifiPassword.expire(millis()))
    Serial.println("[wifi] ERROR password timed out; send a newline before the next command");
  if (wifiPassword.active()) {
    while (Serial.available() && wifiPassword.active()) {
      const auto result = wifiPassword.feed(Serial.read());
      if (result == WifiPasswordInput::Result::Ready) {
        if (recording || playing || museMode != MuseMode::Off) {
          Serial.println("[wifi] ERROR busy; finish audio or pairing before changing Wi-Fi");
        } else if (savedWifi.request(wifiPassword.network(), millis())) {
          tls.stop();
          wsOpen = false;
          clearAvailability();
        } else Serial.println("[wifi] ERROR network not changed");
        wifiPassword.clear();
      } else if (result == WifiPasswordInput::Result::Invalid) {
        Serial.println("[wifi] ERROR password must be empty or 8-63 bytes; nothing changed");
        wifiPassword.clear();
      }
    }
  }
  // A pending password never blocks the network, buttons or watchdog.
  if (!wifiPassword.active() && Serial.available()) {
#elif defined(RELAY_BOARD_06)
  if (!board06SerialBusy && !recording && Serial.available()) {
#else
  if (Serial.available()) {
#endif
    int c = Serial.read();
    if (recording) {
      // Bench commands can block for seconds. Never leave capture running
      // while the loop cannot observe a physical release.
      stopRecording();
      Serial.println("[serial] finish Talk before running commands");
      return;
    }
    switch (c) {
      case 'P': {
        // The warehouse tool sends one newline-terminated command. Reading the
        // remainder here preserves all existing one-key diagnostic commands.
        String rest = Serial.readStringUntil('\n');
        if (rest.startsWith("ROVISION ")) provision(rest.substring(9));
        else Serial.println("[provision] ERROR expected PROVISION <credential>");
        break;
      }
#ifndef RELAY_CELLULAR
      case 'W': {
        String rest = Serial.readStringUntil('\n');
        if (rest.endsWith("\r")) rest.remove(rest.length() - 1);
        if (rest == "IFI") {
          const auto &profiles = savedWifi.profiles();
          Serial.printf("[wifi] %u saved network(s)\n", profiles.count);
          for (size_t i = 0; i < profiles.count; ++i)
            Serial.printf("[wifi]   %s\n", profiles.networks[i].ssid);
          break;
        }
        if (rest.startsWith("IFI FORGET ")) {
          if (recording || playing || museMode != MuseMode::Off) {
            Serial.println("[wifi] ERROR busy; finish audio or pairing before changing Wi-Fi");
            break;
          }
          if (savedWifi.forget(rest.substring(11).c_str())) {
            tls.stop(); wsOpen = false; clearAvailability();
            Serial.println("[wifi] network forgotten");
          } else Serial.println("[wifi] ERROR could not forget network; nothing changed");
          break;
        }
        if (!rest.startsWith("IFI ") || rest.length() < 5 || rest.length() > 4 + 32) {
          Serial.println("[wifi] ERROR expected WIFI <network name, up to 32 characters>");
          break;
        }
        const String ssid = rest.substring(4);
        Serial.printf("[wifi] password for \"%s\" (empty line for none):\n", ssid.c_str());
        wifiPassword.begin(ssid.c_str(), millis());
        break;
      }
#endif
      case 'B': {
        String rest = Serial.readStringUntil('\n');
        if (rest.startsWith("UTTONS")) buttonsCommand(rest.substring(6));
        else Serial.println("[buttons] ERROR expected BUTTONS ...");
        break;
      }
#ifdef RELAY_BOARD_06
      case 'g': board06Status(); break;
      case 'j': board06MicDiagnostic(); break;
#endif
      case 'p': printProvisioningStatus(); break;
      case 't': toneSweep(); break;
      case 'l': ledTest(); break;
      case 'L': ledCommonAnode = !ledCommonAnode; ledTest(); break;
      case 'b': buttonMonitor(); break;
      case 'n':
        if (wsOpen) { wsSendText("{\"v\":1,\"type\":\"listen\"}"); Serial.println("[tx] listen"); }
        else Serial.println("[ws] not connected");
        break;
      case 'Q': museStart(); break;  // as if PTT were tapped three times
      case 'c':
        if (wsOpen) { wsSendText("{\"v\":1,\"type\":\"cancel\"}"); Serial.println("[tx] cancel"); }
        break;
      case 'S': if (wsOpen) sendStatus(); break;
      case '+': if (volume < 10) volume++; volumeFeedback = true; volumeChangedAt = millis(); Serial.printf("[vol] %d/10\n", volume); break;
      case '-': if (volume > 0) volume--; volumeFeedback = true; volumeChangedAt = millis(); Serial.printf("[vol] %d/10\n", volume); break;
      case '<': if (gainEighths > 8) gainEighths -= 4;
                Serial.printf("[gain] %.2fx\n", gainEighths / 8.0); break;
      case '>': if (gainEighths < 48) gainEighths += 4;
                Serial.printf("[gain] %.2fx\n", gainEighths / 8.0); break;
      case 'M': {
        muted = !muted;
#ifdef RELAY_BOARD_06
        xSemaphoreTake(playbackMutex, portMAX_DELAY);
        // Unmuting does not power the amp until the next real audio block.
        if (muted) board06SetAmp(false);
        xSemaphoreGive(playbackMutex);
#else
        if (PIN_AMP_SD >= 0) digitalWrite(PIN_AMP_SD, muted ? LOW : HIGH);
#endif
        Serial.printf("[amp] %s\n", muted ? "MUTED" : "enabled");
        break;
      }
      case 'i': {
        Serial.printf("[mic] rxChan=%s recording=%d shift=%d peak=%ld sent=%lu\n",
                      rxChan ? "OK" : "NULL", recording ? 1 : 0, MIC_SHIFT,
                      (long)micPeak, (unsigned long)micFramesSent);
        break;
      }
      case 'x': tls.stop(); wsOpen = false; Serial.println("[ws] closed"); break;
      case 'm':
#ifdef RELAY_CELLULAR
        cellularPrintStatus();
#else
        Serial.printf("[wifi] station MAC %s\n", WiFi.macAddress().c_str());
#endif
        break;
      case 'w': {
#ifdef RELAY_CELLULAR
        Serial.println("[cell] Wi-Fi is disabled in this build");
#else
        savedWifi.restart();
        tls.stop(); wsOpen = false; clearAvailability();
        int n = WiFi.scanNetworks();
        for (int i = 0; i < n; i++)
          Serial.printf("[wifi]   \"%s\" ch%d %d dBm\n", WiFi.SSID(i).c_str(),
                        WiFi.channel(i), WiFi.RSSI(i));
        WiFi.scanDelete();
#endif
        break;
      }
      case 'D': {
        // Which layer is actually broken. A device VLAN that eats NTP and
        // port 80 may well eat 443 too, and a hung TLS handshake looks the
        // same as a wrong certificate from up here.
        struct { const char *host; int port; } probes[] = {
          {HOST, 443}, {HOST, 80}, {"1.1.1.1", 443}, {"1.1.1.1", 53},
        };
        for (auto &pr : probes) {
          NetworkClient c;
          uint32_t t = millis();
          esp_task_wdt_delete(nullptr);
          bool ok = c.connect(pr.host, pr.port, 15000);
          esp_task_wdt_add(nullptr);
          esp_task_wdt_reset();
          Serial.printf("[probe] tcp %s:%d  %s in %lu ms\n", pr.host, pr.port,
                        ok ? "OPEN" : "blocked", (unsigned long)(millis() - t));
          c.stop();
        }
        Serial.printf("[probe] heap free %lu, largest block %lu\n",
                      (unsigned long)ESP.getFreeHeap(),
                      (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        break;
      }
      case 'h': help(); break;
      default: break;
    }
  }

  static uint32_t lastStat = 0;
  if (millis() - lastStat > 3000) {
    lastStat = millis();
    Serial.printf("[stat] staged=%luB(%lums) receiving=%d held=%d ready=%d ring=%lu "
                  "rx=%lu under=%lu trunc=%lu peak_out=%ld heap=%lu minheap=%lu rssi=%d %s\n",
                  (unsigned long)playbackBytes,
                  (unsigned long)(uint64_t(playback.samples) * 1000 / SAMPLE_RATE_HZ),
                  playbackReceiving ? 1 : 0, playback.staged ? 1 : 0,
                  playback.ready ? 1 : 0,
                  (unsigned long)ringAvail(),
                  (unsigned long)framesRecvd,
                  (unsigned long)underruns,
                  (unsigned long)truncatedFrames, (long)peakWindow,
                  (unsigned long)ESP.getFreeHeap(),
                  (unsigned long)ESP.getMinFreeHeap(), networkRssi(),
                  wsOpen ? "ws:up" : "ws:down");
    peakWindow = 0; peakPreGain = 0;
  }

  // Ack once the complete staged span has actually drained. This is what
  // clears the Mac's queue, so it has to mean "played", not "received".
  if (playback.failed) {
    Serial.println("[audio] MP3 decode or speaker write failed");
    retryPlayback();
  }
  if (playback.finished) {
    if (pendingAck[0]) {
      if (confirmsPlayedReceipts) playedReceipt.remember(pendingAck, millis());
      sendAck(pendingAck);
    }
    pendingAck[0] = 0;
    playing = false;
    playbackClear();
  }

  if (wsOpen && playedReceipt.due(millis())) sendAck(playedReceipt.id());

  if (!networkConnected()) {
    clearAvailability();
    if (hasPendingPlayback()) abandonPlayback();
    return;
  }

  // An unprovisioned unit may join Wi-Fi for factory diagnostics, but it must
  // never make an anonymous/retry loop against the public relay.
  if (!relayCredential.length()) return;

  if (!wsOpen) {
    if (!reconnectPolicy.due(millis())) return;
    if (!clockReady()) {
      static uint32_t said = 0;
      if (millis() - said > 5000) { said = millis(); Serial.println("[time] waiting for SNTP"); }
      return;
    }
    reconnectPolicy.attempting();
    if (wsConnect()) {
      wsReader.reset(wsBuf, WS_BUF - 1);
      wsOpen = true;
      clearAvailability();
      reconnectPolicy.connected(millis());
      dataPath.reached();
      lastRxMs = millis();
      announceConnection();
    } else {
      // A relay that answered, even to refuse, proves the bearer works.
      if (wsReachedRelay) dataPath.reached();
      else dataPathUnreachable();
      scheduleRetry();
    }
    return;
  }

  if (!tls.connected()) {
    Serial.println("[ws] disconnected");
    connectionEnded();
    return;
  }

  // Nothing at all from the relay, not even its ping. The socket is open here
  // and dead on the wire; only tearing it down gets us a working one.
  if (millis() - lastRxMs > RX_DEADLINE_MS) {
    Serial.printf("[ws] silent %lu ms - link is dead, reconnecting\n",
                  (unsigned long)(millis() - lastRxMs));
    dataPathUnreachable();
    connectionEnded();
    return;
  }

  reconnectPolicy.service(millis());

#ifndef RELAY_BOARD_06
  if (recording && !talkSent && pttIsSpeech(millis() - recStartMs)) sendTalkStart();
#endif
  pumpMic();

  // A stable power transition updates the owner promptly, coalesced to at most
  // one report per 5 seconds. Periodic reports ride on pings (below).
#ifdef RELAY_BOARD_06
  if (board06Power.needsReport() && millis() - lastStatusMs >= 5000) sendStatus();
#endif

  uint8_t opcode = 0;
  int len = wsReadFrame(&opcode);
  if (len == -2) {
    Serial.println("[ws] invalid or incomplete frame - reconnecting");
    connectionEnded();
    return;
  }
  if (len >= 0) {
    lastRxMs = millis();
    if (opcode == 0x1) {
      handleMessage((char *)wsFrame, len);
    } else if (opcode == 0x2) {
      framesRecvd++;
      playbackAppend(wsFrame, len);
    } else if (opcode == 0x9) {
      // Echo the ping's payload. A zero-length pong never matches the pending
      // ping on the server, so its ping_timeout fires and the socket is closed
      // every few minutes - which looks exactly like bad signal.
      wsSendFrame(0xA, wsFrame, len);
      if (millis() - lastStatusMs >= STATUS_WITH_PING_MS) sendStatus();
    } else if (opcode == 0x8) {
      int code = len >= 2 ? ((wsFrame[0] << 8) | wsFrame[1]) : 1005;
      Serial.printf("[ws] closed %d %.*s\n", code, len > 2 ? len - 2 : 0,
                    (char *)wsFrame + 2);
      if (code == 4401) {
        Serial.println("[ws] 4401 unauthorized - check the credential. Slow retry.");
      } else if (code == 4409) {
        Serial.println("[ws] 4409 replaced - something else is using this pair. Slow retry.");
      }
      connectionEnded(code);
    }
  }
}
