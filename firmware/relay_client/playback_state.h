#pragma once
#include <stddef.h>
#include <stdint.h>

// Callers hold the playback mutex across state changes and speaker writes.
// A pause retains both the compressed span and the last submitted sample.
struct PlaybackState {
  volatile size_t samples = 0, played = 0;
  volatile uint32_t generation = 0;
  volatile bool staged = false, ready = false, finished = false, failed = false;

  void clear() {
    ++generation;
    samples = played = 0;
    staged = ready = finished = failed = false;
  }
  void stage(size_t count) { samples = count; played = 0; staged = true; }
  bool play() {
    if (!staged) return false;
    staged = false;
    ready = true;
    return true;
  }
  bool pause() {
    if (!ready || finished || failed) return false;
    ready = false;
    staged = true;
    ++generation;  // any decoded block from before the pause is now stale
    return true;
  }
  bool current(uint32_t expected) const { return ready && generation == expected; }
  void end(uint32_t expected, bool drained) {
    if (!current(expected)) return;
    ready = false;
    if (drained) finished = true; else failed = true;
  }
};
