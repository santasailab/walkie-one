#include <assert.h>

#include "endpoint_availability.h"

int main() {
  EndpointAvailability endpoint;

  // The established peer fact stays usable for a rolling relay generation
  // only until authoritative presence selects the Link state.
  assert(endpoint.applyPeer(true));
  assert(endpoint.usable());
  assert(!endpoint.applyPresence(false, false));
  assert(!endpoint.usable());
  assert(!endpoint.applyPeer(true));
  assert(!endpoint.usable());

  // A selected Link is authoritative. Its unavailable Agent must not be
  // overridden by the compatibility peer fact or an in-flight digest.
  assert(!endpoint.applyPresence(true, false));
  assert(!endpoint.usable());
  assert(endpoint.canRequest(true));
  assert(!endpoint.canRequest(false));
  endpoint.applyDigest();
  assert(!endpoint.usable());
  assert(endpoint.presenceSeen());
  assert(endpoint.applyPresence(true, true));
  assert(endpoint.usable());

  assert(!endpoint.working());
  endpoint.applyPresence(false, false, false, true);
  assert(endpoint.connecting() && !endpoint.usable());
  endpoint.applyPeer(true);
  assert(endpoint.connecting() && !endpoint.usable());
  endpoint.applyPresence(true, true);
  assert(!endpoint.connecting() && endpoint.usable());
  endpoint.applyPresence(true, true, true);
  assert(endpoint.usable() && endpoint.working());
  endpoint.applyPresence(true, true, false);
  assert(!endpoint.working());
  endpoint.applyPresence(false, true, true);
  assert(!endpoint.working());
  endpoint.applyPresence(true, true, true);

  // Reconnect clears presence knowledge. A rolling relay that only forwards a
  // digest can still establish the compatibility availability fact.
  endpoint.clear();
  assert(!endpoint.usable() && !endpoint.working());
  assert(!endpoint.connecting());
  endpoint.applyDigest();
  assert(endpoint.usable());
  assert(endpoint.canRequest(true));

  return 0;
}
