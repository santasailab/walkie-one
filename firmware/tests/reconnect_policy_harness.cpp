#include <assert.h>
#include <initializer_list>
#include <stdint.h>

#include "reconnect_policy.h"

int main() {
  using namespace reconnect_policy;
  assert(fullJitterDelay(0, kMinimumMs) == 0);
  assert(fullJitterDelay(kMinimumMs, kMinimumMs) == kMinimumMs);
  assert(fullJitterDelay(42, UINT32_MAX) == 42);
  assert(!heldStable(29999, 0));
  assert(heldStable(30000, 0));

  for (uint32_t start : {0u, UINT32_MAX - 20000}) {
    Policy policy;
    uint32_t now = start;
    assert(policy.due(now));
    assert(policy.networkAvailable(now, 5000) == 0);
    policy.attempting();
    // Opening a socket is not success: short-lived opens must keep growing.
    for (uint32_t ceiling : {1000u, 2000u, 4000u, 8000u, 16000u, 32000u, 60000u, 60000u}) {
      policy.connected(now);
      now += 29999;
      assert(policy.retry(now, ceiling) == ceiling);
      assert(!policy.due(now + ceiling - 1));
      now += ceiling;
      assert(policy.due(now));
      policy.attempting();
    }
    policy.connected(now);
    now += 30000;
    assert(policy.retry(now, 30000) == 30000); // Stable drop, even without service().
    assert(!policy.due(now + 29999));
    assert(policy.due(now + 30000));
    policy.attempting();
    assert(policy.retry(now + 30000, 60000) == 60000);

    for (uint32_t random : {0u, 2500u, 5000u, 5001u, UINT32_MAX}) {
      const uint32_t delay = policy.networkAvailable(now, random);
      assert(delay == random % 5001);
      if (delay) assert(!policy.due(now + delay - 1));
      assert(policy.due(now + delay));
    }
  }

  // Full jitter at both stable-drop boundaries and across arbitrary entropy.
  for (uint32_t random : {0u, 15000u, 30000u, 30001u, UINT32_MAX}) {
    Policy policy;
    policy.attempting();
    policy.connected(100);
    policy.service(30100);
    // A later drop uses the stable window already earned by service().
    assert(policy.retry(30101, random) == random % 30001);
  }

  Policy refused;
  uint32_t now = UINT32_MAX - 100;
  for (unsigned strike = 1; strike <= 300; ++strike) {
    refused.attempting();
    refused.connected(now);
    now += 100; // HTTP upgrade followed by rejection is not stable.
    uint32_t ceiling = strike < 5 ? 900000 : 21600000;
    assert(refused.retry(now, ceiling, 4401) == ceiling);
    assert(!refused.due(now + ceiling - 1));
    // A carrier bounce neither clears the strikes nor advances the deadline.
    assert(refused.networkAvailable(now + 1, 0) == ceiling - 1);
    now += ceiling;
    assert(refused.due(now));
    // A failed TLS dial between refusals must retain the slower ceiling.
    refused.attempting();
    assert(refused.retry(now, ceiling) == ceiling);
    now += ceiling;
  }
  refused.attempting();
  refused.connected(now);
  now += 30000;
  refused.service(now);
  assert(refused.retry(now, 30000) == 30000);
  refused.attempting();
  refused.connected(now);
  assert(refused.retry(now, 900000, 4401) == 900000); // Strikes reset on stability.
  refused = Policy(); // An explicitly provisioned credential starts fresh.
  assert(refused.networkAvailable(now, 5000) == 0);

  // A Device left without service for weeks must not revive an expired auth wait.
  Policy offline;
  offline.attempting();
  offline.retry(100, 900000, 4401);
  assert(offline.networkAvailable(100u + 900000u + 0x80000000u, 5000) == 5000);

  Policy replaced;
  for (unsigned i = 0; i < 10; ++i) {
    replaced.attempting();
    replaced.connected(now);
    assert(replaced.retry(now, 900000, 4409) == 900000);
    now += 900000;
  }
  return 0;
}
