#include <assert.h>
#include "listen_gesture.h"

int main() {
  assert(classifyListenRelease(false, false) == ListenReleaseAction::None);
  assert(classifyListenRelease(true, false) == ListenReleaseAction::None);
  assert(classifyListenRelease(false, true) == ListenReleaseAction::None);
  // Duration and a relay's legacy dismissal flag cannot change this outcome.
  assert(classifyListenRelease(true, true) == ListenReleaseAction::Play);
  return 0;
}
