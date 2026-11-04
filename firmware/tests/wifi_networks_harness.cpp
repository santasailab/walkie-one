#include "wifi_networks.h"
#include "wifi_password.h"
#include <assert.h>
#include <map>
#include <string>
#include <vector>

using namespace saved_wifi;

static Network network(const char *ssid, const char *password = "test-password") {
  Network value;
  assert(strlen(ssid) < sizeof(value.ssid));
  assert(strlen(password) < sizeof(value.password));
  strcpy(value.ssid, ssid);
  strcpy(value.password, password);
  return value;
}

struct FakePlatform final : Platform {
  LoadResult loadResult = LoadResult::Missing;
  Profiles disk;
  bool saveWorks = true, scanDone = true;
  int writes = 0, scans = 0;
  std::string connectedName;
  std::map<std::string, int16_t> visible;
  std::vector<Network> attempts;
  std::vector<std::string> reports;

  LoadResult load(Profiles &profiles) override { profiles = disk; return loadResult; }
  bool save(const Profiles &profiles) override {
    ++writes;
    if (!saveWorks) return false;
    disk = profiles;
    loadResult = LoadResult::Loaded;
    return true;
  }
  bool connected() override { return !connectedName.empty(); }
  bool connectedTo(const char *ssid) override { return connectedName == ssid; }
  void disconnect() override { connectedName.clear(); }
  void join(const Network &value) override { attempts.push_back(value); }
  void startScan() override { ++scans; }
  bool scanReady() override { return scanDone; }
  int16_t strength(const Network &value) override {
    auto found = visible.find(value.ssid);
    return found == visible.end() ? kNotSeen : found->second;
  }
  void stopScan() override {}
  void report(const char *message, const char *ssid = "") override {
    reports.push_back(std::string(message) + " " + ssid);
  }
};

static void migrateAndRoam() {
  FakePlatform p;
  Manager manager(p);
  manager.begin(network("Home"));
  assert(p.disk.count == 1 && p.writes == 1);
  assert(manager.request(network("Work"), 0));
  assert(p.disk.count == 1); // Not saved until DHCP succeeds.
  p.connectedName = "Work";
  manager.service(1);
  assert(p.disk.count == 2 && p.writes == 2);
  for (const auto &line : p.reports) assert(line.find("test-password") == std::string::npos);
  const int scans = p.scans;
  manager.service(100000);
  assert(p.scans == scans && p.writes == 2); // No scan or flash wear when online.

  // Reboot with Work's stronger signal absent; select Home from a fresh scan.
  p.connectedName.clear();
  p.visible = {{"Home", -40}, {"Untrusted open hotspot", -10}};
  Manager reboot(p);
  reboot.begin(network("Obsolete"));
  reboot.service(0);
  reboot.service(1);
  assert(std::string(p.attempts.back().ssid) == "Home");
  p.connectedName = "Home";
  reboot.service(2);
  assert(p.writes == 2);

  // Walking back to Work reconnects without rebooting or rewriting settings.
  p.connectedName.clear();
  p.visible = {{"Home", -90}, {"Work", -35}};
  reboot.service(3);
  reboot.service(4);
  assert(std::string(p.attempts.back().ssid) == "Work");
  p.connectedName = "Work";
  reboot.service(5);
  reboot.restart(); // Soft off/wake uses the same list.
  reboot.service(6);
  reboot.service(7);
  assert(std::string(p.attempts.back().ssid) == "Work" && p.writes == 2);
}

static void passwordFailuresAndFallback() {
  FakePlatform p;
  Manager m(p);
  m.begin(network("Home", "original-password"));
  assert(m.request(network("Home", "wrong-password"), 0));
  m.service(kJoinMs);
  assert(p.disk.count == 1 && std::string(p.disk.networks[0].password) == "original-password");
  m.service(kJoinMs + 1);
  assert(std::string(p.attempts.back().password) == "original-password");
  assert(m.request(network("Work"), 20000));
  p.connectedName = "Work";
  m.service(20001);
  m.restart();
  p.visible = {{"Home", -20}, {"Work", -60}};
  m.service(21000); m.service(21001);
  assert(std::string(p.attempts.back().ssid) == "Home");
  m.service(21001 + kJoinMs); // Strongest has a bad/stale password: try Work.
  assert(std::string(p.attempts.back().ssid) == "Work");
  m.service(21001 + 2 * kJoinMs);
  const size_t attempts = p.attempts.size();
  m.service(21001 + 2 * kJoinMs + kRetryMs - 1);
  assert(p.attempts.size() == attempts);
  const int scans = p.scans;
  m.service(21001 + 2 * kJoinMs + kRetryMs);
  assert(p.scans == scans + 1);

  // Storage failure is reported and retains the previous durable password.
  p.saveWorks = false;
  assert(m.request(network("Home", "new-password"), 100000));
  p.connectedName = "Home";
  m.service(100001);
  assert(std::string(p.disk.networks[0].password) == "original-password");
  assert(std::string(m.profiles().networks[0].password) == "original-password");
  assert(!m.forget("Home"));
}

static void capacityForgettingAndCorruption() {
  FakePlatform p;
  Manager m(p);
  m.begin(Network{});
  for (size_t i = 0; i < kCapacity; ++i) {
    const auto name = "Network " + std::to_string(i);
    assert(m.request(network(name.c_str()), 0));
    p.connectedName = name;
    m.service(1);
  }
  assert(!m.request(network("Ninth"), 2));
  assert(m.request(network("Network 0", "replacement-password"), 3));
  p.connectedName = "Network 0";
  m.service(4);
  assert(p.disk.count == kCapacity);
  assert(std::string(p.disk.networks[0].password) == "replacement-password");
  for (size_t i = 0; i < kCapacity; ++i)
    assert(m.forget(("Network " + std::to_string(i)).c_str()));
  Manager reboot(p);
  reboot.begin(network("Legacy home"));
  reboot.service(0);
  assert(reboot.profiles().count == 0); // Forgotten networks never reappear.
  assert(!reboot.forget("Unknown"));
  p.disk.version = 99;
  const int writes = p.writes;
  Manager corrupt(p);
  corrupt.begin(network("Legacy home"));
  assert(p.writes == writes && p.disk.version == 99);
  assert(!corrupt.request(network("Work"), 0));
  // The legacy network can still work when the new store cannot be read.
  corrupt.service(0); corrupt.service(1);
  assert(std::string(p.attempts.back().ssid) == "Legacy home");

  Profiles invalid;
  invalid.count = 9;
  assert(!invalid.valid());
  invalid.count = 1;
  memset(invalid.networks[0].ssid, 'x', 33);
  assert(!invalid.valid());
  invalid.networks[0] = network("Home");
  invalid.networks[1] = invalid.networks[0];
  invalid.count = 2;
  assert(!invalid.valid());
}

static void hiddenAndTimeoutRollover() {
  FakePlatform p;
  Manager m(p);
  m.begin(network("Hidden"));
  p.scanDone = false;
  const uint32_t start = UINT32_MAX - 100;
  m.service(start);
  m.service(start + kScanMs - 1);
  assert(p.attempts.empty());
  m.service(start + kScanMs);
  assert(std::string(p.attempts.back().ssid) == "Hidden");
  p.connectedName = "Hidden";
  m.service(start + kScanMs + 1);
  assert(p.writes == 1);
  assert(m.request(network("Other"), UINT32_MAX - 10));
  const size_t attempts = p.attempts.size();
  m.service(kJoinMs - 12);
  assert(p.attempts.size() == attempts);
  m.service(kJoinMs - 11); // Timeout over rollover; old saved list survives.
  assert(m.profiles().count == 1 && p.scans == 2);
}

static WifiPasswordInput::Result feed(WifiPasswordInput &input, const std::string &value) {
  auto result = WifiPasswordInput::Result::Waiting;
  for (char c : value) result = input.feed(c);
  return result;
}

static void passwordInput() {
  using Result = WifiPasswordInput::Result;
  WifiPasswordInput input;
  input.begin("Home with spaces ", 0);
  assert(feed(input, " password with spaces \r\n") == Result::Ready);
  assert(std::string(input.network().password) == " password with spaces ");
  assert(std::string(input.network().ssid) == "Home with spaces ");
  input.begin("Open", 0);
  assert(feed(input, "\n") == Result::Ready);
  input.begin("Home", 0);
  assert(feed(input, std::string(63, 'x') + "\r\n") == Result::Ready);
  input.begin("Home", 0);
  assert(feed(input, std::string(64, 'x') + "\n") == Result::Invalid);
  input.begin("Home", 0);
  assert(feed(input, "short\n") == Result::Invalid);
  input.begin("Home", 0);
  assert(feed(input, std::string(200, 'x')) == Result::Waiting && input.active());
  assert(feed(input, "\n") == Result::Invalid);
  input.begin("Home", UINT32_MAX - 10);
  assert(!input.expire(59988));
  assert(input.expire(59989));
  assert(input.active()); // Discard a late password, including diagnostic letters.
  assert(feed(input, "rQmSp-secret\n") == Result::Waiting && !input.active());
  assert(input.network().password[0] == 0);
}

int main() {
  migrateAndRoam();
  passwordFailuresAndFallback();
  capacityForgettingAndCorruption();
  hiddenAndTimeoutRollover();
  passwordInput();
}
