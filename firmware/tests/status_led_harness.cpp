#include <assert.h>
#include <initializer_list>

#include "status_led.h"

// Reads the light at an "on" phase of every blink rate, then at an "off" phase.
static Rgb lit(const StatusInputs &in) { return statusLed(in, 1000 + 500 + 250); }
static Rgb dark(const StatusInputs &in) { return statusLed(in, 0); }

int main() {
#ifdef RELAY_BOARD_06
  assert(ledDuty(255, false) == 255 && ledDuty(48, false) == 48);
#else
  assert(ledDuty(255, false) == 20 && ledDuty(128, false) == 10);
#endif
  assert(ledDuty(0, false) == 0 && ledDuty(0, true) == 255);
  assert(ledDuty(255, true) == 255 - ledDuty(255, false));
  StatusInputs ready;
  ready.cloudConnected = true;
  ready.assistantReady = true;
  assert(lit(ready) == kOff);  // idle and healthy: no light

  // Successful pairing adds no light; power and activity keep their meanings.
  StatusInputs paired = ready;
  paired.pairing = PairingLight::Paired;
  for (uint32_t now = 0; now <= 30000; now += 250)
    assert(statusLed(paired, now) == kOff);

  StatusInputs talking = ready;
  talking.recording = true;
  assert(lit(talking) == kWhite && dark(talking) == kWhite);  // solid

  StatusInputs speaking = ready;
  speaking.playing = true;
  assert(lit(speaking) == kBlue && dark(speaking) == kBlue);

  StatusInputs waiting = ready;
  waiting.replyWaiting = true;
  assert(lit(waiting) == kGreen && dark(waiting) == kOff);  // slow blink
  assert(statusLed(waiting, 999) == kOff && statusLed(waiting, 1000) == kGreen);

  StatusInputs working = ready;
  working.assistantWorking = true;
  assert(lit(working) == kBlue && dark(working) == kOff);
  assert(statusLed(working, 999) == kOff && statusLed(working, 1000) == kBlue);
  working.replyWaiting = true;
  assert(lit(working) == kGreen && dark(working) == kOff);
  working.replyWaiting = false;
  assert(lit(working) == kBlue && dark(working) == kOff); // other work remains
  working.playing = true;
  assert(lit(working) == kBlue);
  working.recording = true;
  assert(lit(working) == kWhite);
  working.playing = working.recording = false;
  working.cloudConnected = false;
  working.offlineMs = kOfflineGraceMs;
  assert(lit(working) == kRed); // stale work cannot hide lost connectivity

  StatusInputs pairing = ready;
  pairing.pairing = PairingLight::Open;
  assert(statusLed(pairing, 500) == kWhite && statusLed(pairing, 0) == kOff);
  pairing.pairing = PairingLight::Paired;
  assert(lit(pairing) == kOff && dark(pairing) == kOff);
  pairing.recording = true;
  assert(lit(pairing) == kWhite);  // success does not mask normal activity
  pairing.recording = false;
  pairing.connectionInProgress = true;
  assert(statusLed(pairing, 500) == kWhite);  // an explicit recheck is not ready
  pairing.connectionInProgress = false;
  pairing.pairing = PairingLight::Failed;
  assert(dark(pairing) == kRed);

  StatusInputs noAssistant;
  noAssistant.cloudConnected = true;
  assert(statusLed(noAssistant, 1000) == kRed && statusLed(noAssistant, 0) == kOff);
  noAssistant.connectionInProgress = true;
  assert(statusLed(noAssistant, 500) == kWhite && dark(noAssistant) == kOff);
  noAssistant.connectionInProgress = false;
  assert(lit(noAssistant) == kRed);  // failed check replaces checking
  pairing.assistantReady = false;
  pairing.pairing = PairingLight::Paired;
  assert(statusLed(pairing, 500) == kWhite);  // tokens alone are not success

  StatusInputs offline;
  offline.offlineMs = kOfflineGraceMs - 1;
  assert(lit(offline) == kOff);  // booting or a brief reconnect is not an error
  offline.offlineMs = kOfflineGraceMs;
  assert(statusLed(offline, 250) == kRed && statusLed(offline, 500) == kOff);  // fast blink

  // Precedence: sleep > pairing result > talking > speaking > waiting reply > problems.
  StatusInputs all = offline;
  all.replyWaiting = true;
  assert(lit(all) == kGreen);  // a spoken-claim answer shows before any assistant
  all.playing = true;
  assert(lit(all) == kBlue);
  all.recording = true;
  assert(lit(all) == kWhite);
  all.pairing = PairingLight::Failed;
  assert(lit(all) == kRed);
  all.asleep = true;
  assert(lit(all) == kOff);

  // Charging stays orange across blink boundaries; charged is steady green.
  StatusInputs charging = ready;
  charging.battery = BatteryLight::Charging;
  for (uint32_t now : {0U, 999U, 1000U, 1999U, 2000U, 3000U, 30000U})
    assert(statusLed(charging, now) == kOrange);
  assert(kOrange.red == 255 && kOrange.green > 0 && kOrange.green < 255);
  StatusInputs charged = ready;
  charged.battery = BatteryLight::Charged;
  for (uint32_t now = 0; now <= 30000; now += 250)
    assert(statusLed(charged, now) == kGreen);

  // New messages outrank charging/full, including their dark blink phase.
  for (BatteryLight battery : {BatteryLight::Charging, BatteryLight::Charged}) {
    StatusInputs message = ready;
    message.battery = battery;
    message.replyWaiting = true;
    assert(statusLed(message, 0) == kOff && statusLed(message, 1000) == kGreen);
    message.replyWaiting = false;
    message.assistantWorking = true;
    assert(statusLed(message, 0) == kOff && statusLed(message, 1000) == kBlue);
    message.recording = true;
    assert(statusLed(message, 0) == kWhite);
  }
  paired.battery = BatteryLight::Charging;
  assert(statusLed(paired, 0) == kOrange && statusLed(paired, 1000) == kOrange);
  paired.battery = BatteryLight::Charged;
  assert(statusLed(paired, 0) == kGreen);
  charging.cloudConnected = false;
  charging.offlineMs = kOfflineGraceMs - 1;
  assert(statusLed(charging, 1000) == kOrange);
  charging.offlineMs = kOfflineGraceMs;
  assert(statusLed(charging, 1000) == kOff && statusLed(charging, 250) == kRed);

  // Switched off: charging/full still show; no other status turns on the LED.
  StatusInputs off = all;
  off.battery = BatteryLight::Charging;
  assert(statusLed(off, 0) == kOrange && statusLed(off, 1000) == kOrange);
  off.battery = BatteryLight::Charged;
  assert(lit(off) == kGreen && dark(off) == kGreen);
  off.battery = BatteryLight::None;
  assert(statusLed(off, 1000) == kOff);
  return 0;
}
