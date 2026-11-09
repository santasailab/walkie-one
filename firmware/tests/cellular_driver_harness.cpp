#define RELAY_BOARD_06 1
#include "cellular.cpp"

#include <stdexcept>

void resetDriver() {
  cleanup();
  initialized = false;
  desired = cellular_policy::emptyConfig();
  desiredGeneration = 1;
  state.store(State::Off);
  negotiatedBaud.store(0);
  receivedBytes.store(0);
  probeBaud.store(0);
  networkUtc = 0;
  fake_cell::stored.clear();
  fake_cell::putCalls = 0;
  fake_cell::log.clear();
  fake_cell::now = 0;
  fake_cell::beginCalls = fake_cell::cmdCalls = 0;
  fake_cell::onDelay = fake_cell::onData = nullptr;
  fake_cell::rawSilent = false;
  fake_cell::modemBaud = 115200;
  fake_cell::modemApn.clear();
  fake_cell::radioOnline = true;
  fake_cell::rejectProfile = fake_cell::ignoreBaudChange = false;
  fake_cell::saveFails = false;
  fake_cell::rawReply.clear();
  fake_cell::rawDelayMs = 0;
  fake_cell::baudChanges.clear();
  fake_cell::attached = fake_cell::ip = true;
  fake_cell::clock = "+CCLK: \"70/01/01,00:00:12+00\"";
  assert(cellularSetup());
}

int main() {
  resetDriver();
  assert(!desired.enabled && !cellularConnected());
  for (const int pin : {21, 14, 15, 42, 16, 39}) assert(fake_cell::pins[pin] == LOW);
  assert(cellularCommand("CELL START"));
  assert(!desired.enabled && fake_cell::beginCalls == 0);
  cellularCommand("CELL APN internet\rAT+CFUN=0");
  assert(!desired.apn[0]);
  cellularCommand(String(std::string("CELL APN a\0", 11) + std::string(200, 'x')));
  assert(!desired.apn[0]);
  cellularCommand("CELL APN example.private");
  assert(!desired.enabled && fake_cell::beginCalls == 0);
  cellularCommand("CELL START");
  assert(desired.enabled && fake_cell::beginCalls == 0); // Commands never dial on main.

  uint32_t generation;
  Config config = snapshot(generation);
  int ceiling = 921600;
  assert(connectAttempt(config, generation, ceiling));
  assert(negotiatedBaud.load() == 460800 && fake_cell::lastBaud == 460800);
  assert(cellularConnected() && cellularRssiDbm() == -83);
  assert(fake_cell::apn == "example.private");
  assert(fake_cell::pins[39] == LOW && fake_cell::pins[14] == LOW);
  const unsigned commands = fake_cell::cmdCalls;
  String response;
  assert(!commandAt("AT+CSQ", response, generation)); // DATA mode is exclusive.
  cellularPrintStatus();
  assert(fake_cell::cmdCalls == commands);
  assert(fake_cell::log.find("example.private") == std::string::npos);
  cellularCommand("CELL STOP");
  assert(!cellularConnected() && !desired.enabled && fake_cell::pins[21] == LOW);
  assert(!powerPin(kKey, HIGH, generation) && fake_cell::pins[14] == LOW);
  assert(persist(desired));
  Config saved{};
  std::memcpy(&saved, fake_cell::stored.data(), sizeof(saved));
  assert(cellular_policy::validConfig(saved) && !saved.enabled);
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::onData = [] { cellularCommand("CELL STOP"); };
  ceiling = 921600;
  assert(!connectAttempt(config, generation, ceiling));
  assert(!cellularConnected() && fake_cell::pins[21] == LOW);
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::rawSilent = true;
  ceiling = 921600;
  assert(!connectAttempt(config, generation, ceiling));
  assert(state.load() == State::NoAt && fake_cell::now < 28000);
  assert(fake_cell::baudChanges.size() == 3);
  assert(fake_cell::baudChanges.front().first >= 23300); // Default115200 stays stable20s after power sequence.
  assert(receivedBytes.load() == 0 && probeBaud.load() == 230400);
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::rawReply = "OK\r\n";
  fake_cell::rawDelayMs = 800;
  assert(discoverBaud(generation) == 115200);
  assert(fake_cell::baudChanges.empty() && fake_cell::now == 800);
  assert(receivedBytes.load() == 4 && probeBaud.load() == 115200);
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::attached = false;
  ceiling = 921600;
  assert(!connectAttempt(config, generation, ceiling));
  assert(state.load() == State::AttachError && fake_cell::now < 95000);
  cleanup();

  // The network's clock is read once attached, asked for before the radio
  // comes up, and keeps running; a modem with no network time offers none.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  int64_t networkTime = -1;
  assert(!cellularNetworkTime(networkTime));
  ceiling = 921600;
  assert(connectAttempt(config, generation, ceiling));
  assert(!cellularNetworkTime(networkTime));
  cleanup();
  fake_cell::clock = "+CCLK: \"26/11/06,12:00:00-32\"";
  fake_cell::modemBaud = 115200;
  ceiling = 921600;
  assert(connectAttempt(config, generation, ceiling));
  assert(cellularNetworkTime(networkTime) && networkTime == 1793966400);
  fake_cell::now += 90500;
  assert(cellularNetworkTime(networkTime) && networkTime == 1793966400 + 90);
  cleanup();
  assert(cellularNetworkTime(networkTime) && networkTime == 1793966400 + 90);

  // A failed modem profile write cannot reach PPP or attempt a data session.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::rejectProfile = true;
  ceiling = 921600;
  assert(!connectAttempt(config, generation, ceiling));
  assert(state.load() == State::ApnError && fake_cell::beginCalls == 0);
  cleanup();

  // A modem that acknowledges but ignores a baud change must fail the raw AT check.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  config = snapshot(generation);
  fake_cell::ignoreBaudChange = true;
  ceiling = 921600;
  assert(!connectAttempt(config, generation, ceiling));
  assert(state.load() == State::BaudError && ceiling == 230400 && fake_cell::beginCalls == 0);
  cleanup();

  // Settings loaded from storage at setup are already stored: the worker's
  // first pass must neither rewrite them nor announce "settings saved", and
  // must still start the modem with them.
  resetDriver();
  {
    Config stored = cellular_policy::emptyConfig();
    stored.enabled = 1;
    std::strcpy(stored.apn, "internet");
    const auto *bytes = reinterpret_cast<const uint8_t *>(&stored);
    fake_cell::stored.assign(bytes, bytes + sizeof stored);
  }
  initialized = false;  // resetDriver() already ran setup on empty storage
  assert(cellularSetup());
  assert(desired.enabled && std::strcmp(desired.apn, "internet") == 0);
  fake_cell::onDelay = [] {
    if (state.load() == State::Starting) throw std::runtime_error("modem starting");
  };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(fake_cell::putCalls == 0);
  assert(fake_cell::log.find("settings saved") == std::string::npos);
  fake_cell::onDelay = nullptr;
  cleanup();

  // An APN/START submitted before the worker first runs must still be persisted.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  fake_cell::onDelay = [] { throw std::runtime_error("stop simulated worker"); };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(fake_cell::stored.size() == sizeof(Config));
  std::memcpy(&saved, fake_cell::stored.data(), sizeof(saved));
  assert(saved.enabled && std::strcmp(saved.apn, "internet") == 0);
  fake_cell::onDelay = nullptr;
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  fake_cell::rawSilent = true;
  // A silent modem is retried without limit; the supply stays off between
  // attempts and each wait is longer than the last.
  static uint32_t backoffStarted, longestBackoff;
  backoffStarted = longestBackoff = 0;
  fake_cell::onDelay = [] {
    if (state.load() == State::Backoff) {
      assert(fake_cell::pins[21] == LOW);
      if (!backoffStarted) backoffStarted = fake_cell::now;
      longestBackoff = std::max(longestBackoff, fake_cell::now - backoffStarted);
    } else backoffStarted = 0;
    if (attemptCount.load() >= 8) throw std::runtime_error("still retrying");
  };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(attemptCount.load() == 8 && !cellularConnected());
  assert(longestBackoff >= cellular_policy::kMaxRetryDelayMs - 10);
  assert(fake_cell::log.find("attempt limit") == std::string::npos);
  fake_cell::onDelay = nullptr;
  cleanup();

  // A requested restart power-cycles a live session and dials again with the
  // same settings; it is not a configuration change and writes nothing.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  static int restartPhase;
  static uint32_t restartGeneration;
  restartPhase = 0;
  fake_cell::onDelay = [] {
    if (restartPhase == 0 && cellularConnected()) {
      restartGeneration = desiredGeneration;
      fake_cell::stored.clear();
      cellularRestart();
      restartPhase = 1;
    } else if (restartPhase == 1 && !cellularConnected()) {
      assert(fake_cell::pins[21] == LOW);
      restartPhase = 2;
    } else if (restartPhase == 2 && cellularConnected()) {
      throw std::runtime_error("reconnected");
    }
  };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(fake_cell::beginCalls == 2 && desired.enabled);
  assert(desiredGeneration == restartGeneration && fake_cell::stored.empty());
  assert(fake_cell::log.find("restart requested") != std::string::npos);
  fake_cell::onDelay = nullptr;
  cleanup();

  // A restart requested as the session comes up is not lost to the worker's
  // own bookkeeping: the new session is restarted too.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  fake_cell::onData = [] { cellularRestart(); };
  static uint32_t upSince;
  upSince = 0;
  fake_cell::onDelay = [] {
    if (fake_cell::beginCalls >= 2) throw std::runtime_error("restarted");
    if (!cellularConnected()) return;
    fake_cell::onData = nullptr;
    if (!upSince) upSince = fake_cell::now;
    if (fake_cell::now - upSince >= 1000) throw std::runtime_error("request lost");
  };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &outcome) {
    assert(std::string(outcome.what()) == "restarted");
  }
  assert(fake_cell::log.find("restart requested") != std::string::npos);
  fake_cell::onDelay = nullptr; fake_cell::onData = nullptr;
  cleanup();

  // A restart requested with no session is forgotten, not applied to the next.
  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  cellularRestart();
  static uint32_t onlineSince;
  onlineSince = 0;
  fake_cell::onDelay = [] {
    if (!cellularConnected()) { assert(!onlineSince); return; }
    if (!onlineSince) onlineSince = fake_cell::now;
    if (fake_cell::now - onlineSince >= 1000) throw std::runtime_error("stayed online");
  };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(fake_cell::beginCalls == 1);
  fake_cell::onDelay = nullptr;
  cleanup();

  resetDriver();
  cellularCommand("CELL APN internet"); cellularCommand("CELL START");
  fake_cell::saveFails = true;
  fake_cell::onDelay = [] { throw std::runtime_error("storage failure"); };
  try { worker(nullptr); assert(false); } catch (const std::runtime_error &) {}
  assert(state.load() == State::StorageError && !desired.enabled);
  assert(fake_cell::pins[21] == LOW && fake_cell::beginCalls == 0);
  fake_cell::onDelay = nullptr;
  cleanup();
}
