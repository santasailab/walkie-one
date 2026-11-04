#pragma once

#include <stdint.h>

enum class ListenReleaseAction {
  None,
  Play,
};

// Power holds are consumed by the power handler before reaching this helper.
// Every other release preserves the reply and may start playback.
inline ListenReleaseAction classifyListenRelease(bool stagedAtPress,
                                                  bool stagedAtRelease) {
  if (!stagedAtPress || !stagedAtRelease) return ListenReleaseAction::None;
  return ListenReleaseAction::Play;
}
