#include <assert.h>
#include <string.h>

#include "button_map.h"

int main() {
  const int allowed[] = {38, 39, 40, 7, 8};
  const ButtonMap defaults{38, 8, 40, 39};
  ButtonMap map = defaults;

  // Play moved to the volume-down switch, which then has no volume role.
  assert(parseButtonMap("38 39 40 -1", allowed, 5, map));
  assert(map.talk == 38 && map.play == 39 && map.volumeUp == 40 && map.volumeDown == -1);
  char text[32];
  formatButtonMap(map, text, sizeof(text));
  assert(strcmp(text, "38 39 40 -1") == 0);

  // Invalid maps never change the current one.
  const char *bad[] = {
      "",                 // nothing
      "38 39 40",         // too few
      "38 39 40 -1 7",    // too many
      "38 39 40 -1 x",    // trailing junk
      "-1 39 40 7",       // Talk is required
      "38 -1 40 7",       // Play is required
      "38 39 40 41",      // not a button input on this board
      "38 38 40 7",       // one switch, two jobs
      "38 39 39 -1",      // ditto
      "talk play up down",
  };
  for (const char *candidate : bad) {
    ButtonMap untouched = map;
    assert(!parseButtonMap(candidate, allowed, 5, untouched));
    assert(memcmp(&untouched, &map, sizeof(map)) == 0);
  }

  // Back to the board defaults, with surrounding whitespace.
  assert(parseButtonMap("  38 8 40 39 ", allowed, 5, map));
  assert(memcmp(&map, &defaults, sizeof(map)) == 0);
  return 0;
}
