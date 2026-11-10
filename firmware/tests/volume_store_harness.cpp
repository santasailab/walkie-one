#include <cassert>
#include <cstddef>
#include <cstdint>

#include "volume_store.h"

static uint32_t fakeNow = 0;
static uint32_t millis() { return fakeNow; }
static struct {
  int stored = 255, writes = 0;
  bool fails = false;
  uint8_t getUChar(const char *, uint8_t fallback) { return stored == 255 ? fallback : uint8_t(stored); }
  size_t putUChar(const char *, uint8_t value) {
    ++writes;
    if (fails) return 0;
    stored = value;
    return 1;
  }
} provisionStore;
static struct { void println(const char *) {} } Serial;
static int volume = 5;
static bool recording = false, playing = false;
static VolumeStore volumeStore;

// SKETCH_FUNCTIONS

int main() {
  assert(VolumeStore::restore(0, 5) == 0 && VolumeStore::restore(10, 5) == 10);
  assert(VolumeStore::restore(11, 5) == 5 && VolumeStore::restore(255, 7) == 7);

  // Nothing stored: the board default stands and nothing is written.
  loadVolume();
  assert(volume == 5);
  fakeNow += 60000; volumeSaveService();
  assert(provisionStore.writes == 0);

  // Three quick clicks are one write, once the volume has rested.
  for (int clicks = 0; clicks < 3; ++clicks) {
    ++volume; fakeNow += 300; volumeSaveService();
  }
  fakeNow += VolumeStore::kSettleMs - 1; volumeSaveService();
  assert(provisionStore.writes == 0);
  fakeNow += 1; volumeSaveService();
  assert(provisionStore.writes == 1 && provisionStore.stored == 8);
  fakeNow += 60000; volumeSaveService();
  assert(provisionStore.writes == 1);

  // A flash write stalls audio: never during Talk or playback, then at once.
  volume = 2; volumeSaveService();
  recording = true;
  fakeNow += 60000; volumeSaveService();
  assert(provisionStore.writes == 1);
  recording = false; playing = true; volumeSaveService();
  assert(provisionStore.writes == 1);
  playing = false; volumeSaveService();
  assert(provisionStore.writes == 2 && provisionStore.stored == 2);

  // Back to the saved value before it settles: nothing to write.
  volume = 3; volumeSaveService();
  volume = 2; fakeNow += 60000; volumeSaveService();
  assert(provisionStore.writes == 2);

  // A failed write is tried again.
  volume = 9; volumeSaveService();
  provisionStore.fails = true;
  fakeNow += VolumeStore::kSettleMs; volumeSaveService();
  assert(provisionStore.writes == 3 && provisionStore.stored == 2);
  provisionStore.fails = false;
  fakeNow += VolumeStore::kSettleMs; volumeSaveService();
  assert(provisionStore.writes == 4 && provisionStore.stored == 9);

  // The next start restores it; a corrupt value falls back to the default.
  volume = 5; loadVolume();
  assert(volume == 9);
  fakeNow += 60000; volumeSaveService();
  assert(provisionStore.writes == 4);
  provisionStore.stored = 200; volume = 5; loadVolume();
  assert(volume == 5);

  // millis() rollover while settling.
  fakeNow = 0xfffffff0u; volume = 6; volumeSaveService();
  fakeNow += VolumeStore::kSettleMs; volumeSaveService();
  assert(provisionStore.stored == 6);
}
