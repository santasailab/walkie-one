#include <cassert>
#include <cstdint>

#include "data_path_watch.h"

#define RELAY_CELLULAR 1
static uint32_t fakeNow = 0;
static bool bearer = true;
static int restarts = 0;
static uint32_t millis() { return fakeNow; }
static bool networkConnected() { return bearer; }
static void cellularRestart() { ++restarts; }
static struct { void println(const char *) {} } Serial;
static DataPathWatch dataPath;

// SKETCH_FUNCTIONS

int main() {
  // Three failures are not enough inside the first three minutes: a relay
  // restart or a short outage must not power-cycle every modem.
  DataPathWatch watch;
  assert(!watch.unreachable(1000));
  assert(!watch.unreachable(61000));
  assert(!watch.unreachable(1000 + DataPathWatch::kFirstWaitMs - 1));
  assert(watch.unreachable(1000 + DataPathWatch::kFirstWaitMs));

  // Time alone is not enough either: one slow failure is one failure.
  DataPathWatch slow;
  assert(!slow.unreachable(0));
  assert(!slow.unreachable(10 * DataPathWatch::kFirstWaitMs));
  assert(slow.unreachable(10 * DataPathWatch::kFirstWaitMs + 1));

  // Each restart that does not help doubles the wait for the next, to an hour.
  uint32_t now = 1000 + DataPathWatch::kFirstWaitMs;
  uint32_t wait = DataPathWatch::kFirstWaitMs;
  for (int restart = 0; restart < 8; ++restart) {
    wait = wait * 2 < DataPathWatch::kMaxWaitMs ? wait * 2 : DataPathWatch::kMaxWaitMs;
    assert(!watch.unreachable(now));
    assert(!watch.unreachable(now + 1));
    assert(!watch.unreachable(now + wait - 1));
    assert(watch.unreachable(now + wait));
    now += wait;
  }
  assert(wait == DataPathWatch::kMaxWaitMs && DataPathWatch::kMaxWaitMs == 3600000);

  // Reaching the relay ends the episode and the escalation.
  watch.reached();
  assert(!watch.unreachable(now));
  assert(!watch.unreachable(now + 1));
  assert(watch.unreachable(now + DataPathWatch::kFirstWaitMs));

  // A lost bearer ends the episode (the modem driver is already redialing)
  // but keeps the escalated wait until the relay is reached again.
  assert(!watch.unreachable(now));
  assert(!watch.unreachable(now + 1));
  watch.bearerDown();
  assert(!watch.unreachable(now + 2 * DataPathWatch::kFirstWaitMs));
  assert(!watch.unreachable(now + 2 * DataPathWatch::kFirstWaitMs + 1));
  assert(!watch.unreachable(now + 4 * DataPathWatch::kFirstWaitMs - 1));
  assert(watch.unreachable(now + 4 * DataPathWatch::kFirstWaitMs));

  // millis() rollover inside an episode.
  DataPathWatch wrap;
  assert(!wrap.unreachable(0xffffff00u));
  assert(!wrap.unreachable(0xffffff80u));
  assert(!wrap.unreachable(0xffffff00u + DataPathWatch::kFirstWaitMs - 1));
  assert(wrap.unreachable(0xffffff00u + DataPathWatch::kFirstWaitMs));

  // A certificate the Device rejects came from the relay: the bearer works,
  // and restarting the modem cannot fix a clock or a chain. A dial that timed
  // out or was never answered (the core reports -1) proves nothing.
  assert(dialReachedRelay(true, 0));
  assert(dialReachedRelay(false, kTlsCertificateRejected) && kTlsCertificateRejected == -0x2700);
  assert(!dialReachedRelay(false, -1) && !dialReachedRelay(false, 0));
  assert(!dialReachedRelay(false, -0x0050));  // reset mid-handshake: could be anyone

  // The sketch only blames the modem while the bearer claims to carry data.
  bearer = false;
  for (int i = 0; i < 10; ++i) { fakeNow += DataPathWatch::kFirstWaitMs; dataPathUnreachable(); }
  assert(restarts == 0);
  bearer = true;
  dataPathUnreachable();
  fakeNow += 1; dataPathUnreachable();
  fakeNow += DataPathWatch::kFirstWaitMs - 2; dataPathUnreachable();
  assert(restarts == 0);
  fakeNow += 1; dataPathUnreachable();
  assert(restarts == 1);
  dataPathUnreachable();
  assert(restarts == 1);
}
