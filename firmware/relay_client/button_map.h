#pragma once
#include <stdio.h>
#include <string.h>

// Which physical input does each job. Board defaults come from
// board_profile.h; a Device may store an override (serial `BUTTONS`) that
// survives firmware updates, e.g. to route Play to another switch while one
// is faulty. Pins are GPIO numbers; -1 means no such button.
struct ButtonMap {
  int talk, play, volumeUp, volumeDown;
};

// Parses "talk play volume-up volume-down". Talk and Play are required; each
// used pin must be one of `allowed` and appear once. Leaves `out` untouched
// unless the whole map is valid.
inline bool parseButtonMap(const char *text, const int *allowed, int allowedCount,
                           ButtonMap &out) {
  ButtonMap map{};
  char extra = 0;
  if (sscanf(text, " %d %d %d %d %c", &map.talk, &map.play, &map.volumeUp,
             &map.volumeDown, &extra) != 4)
    return false;
  if (map.talk < 0 || map.play < 0) return false;
  const int pins[] = {map.talk, map.play, map.volumeUp, map.volumeDown};
  for (int i = 0; i < 4; ++i) {
    if (pins[i] == -1) continue;
    bool known = false;
    for (int j = 0; j < allowedCount; ++j) known = known || pins[i] == allowed[j];
    if (!known) return false;
    for (int j = 0; j < i; ++j)
      if (pins[j] == pins[i]) return false;
  }
  out = map;
  return true;
}

inline void formatButtonMap(const ButtonMap &map, char *out, size_t size) {
  snprintf(out, size, "%d %d %d %d", map.talk, map.play, map.volumeUp, map.volumeDown);
}
