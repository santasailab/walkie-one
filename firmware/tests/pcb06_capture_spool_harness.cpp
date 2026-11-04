#include <algorithm>
#include <cassert>
#include <cstdint>
#include <vector>
#include "pcb06_capture_spool.h"

using Spool = Pcb06CaptureSpool;

static void requireZero(const std::vector<int16_t> &storage) {
  assert(std::all_of(storage.begin(), storage.end(), [](int16_t x) { return x == 0; }));
}

int main() {
  std::vector<int16_t> memory(32);
  Spool spool(memory.data(), memory.size());
  int16_t input[] = {10, 20, 30, 40}, output[8] = {};

  // A connected physical press is the only admission path. Waiting for the
  // network may not turn a tap into speech or leak pre-threshold PCM.
  assert(!spool.begin(100, false, true));
  assert(!spool.begin(100, true, false));
  assert(spool.begin(100, true, true));
  assert(!spool.begin(100, true, true));
  assert(spool.append(input, 4));
  assert(!spool.needsStart(349));
  assert(spool.copyPending(output, 8).samples == 0);
  assert(spool.needsStart(350));
  assert(spool.markStarted(350));
  auto first = spool.copyPending(output, 2);
  assert(first.samples == 2 && output[0] == 10 && output[1] == 20);
  assert(spool.pending() == 4);  // Copying is not delivery.
  assert(!spool.finish());
  assert(spool.acknowledge(first));
  assert(!spool.acknowledge(first));
  assert(memory[0] == 0 && memory[1] == 0 && spool.pending() == 2);
  spool.stop(400);
  assert(spool.state() == Spool::State::Draining && !spool.needsEnd());
  assert(!spool.append(input, 1));
  assert(!spool.begin(500, true, true));  // Still uploading after release.
  auto last = spool.copyPending(output, 8);
  assert(last.samples == 2 && output[0] == 30 && output[1] == 40);
  assert(spool.acknowledge(last));
  assert(spool.needsEnd());
  assert(spool.finish() && !spool.finish());
  requireZero(memory);

  // The release guard records actual duration while the sender is stalled.
  assert(spool.begin(1000, true, true));
  assert(spool.append(input, 4));
  spool.stop(1249);
  assert(spool.state() == Spool::State::Discarded);
  assert(!spool.needsStart(10000) && !spool.needsEnd());
  requireZero(memory);
  assert(spool.begin(UINT32_MAX - 100, true, true));
  assert(spool.append(input, 4));
  spool.stop(149);  // Exactly 250 ms, across millis rollover.
  assert(spool.durationMs(10000) == 250 && spool.needsStart(10000));
  assert(spool.markStarted(10000));
  assert(spool.acknowledge(spool.copyPending(output, 8)));
  assert(spool.needsEnd() && spool.finish());

  // A failed/partial transport write must not acknowledge or complete a turn.
  assert(spool.begin(1000, true, true));
  assert(spool.append(input, 4) && spool.markStarted(1300));
  auto stale = spool.copyPending(output, 4);
  spool.abort(Spool::Failure::Transport);
  assert(spool.failure() == Spool::Failure::Transport && !spool.needsEnd());
  assert(!spool.acknowledge(stale) && spool.pending() == 0);
  requireZero(memory);
  assert(spool.begin(2000, true, true));
  assert(spool.append(input, 4) && spool.markStarted(2300));
  assert(!spool.acknowledge(stale));  // Cannot consume a later press's PCM.
  spool.abort(Spool::Failure::Disconnected);
  assert(!spool.needsStart(10000) && !spool.needsEnd());
  requireZero(memory);

  // Overflow fails explicitly instead of sending a success with missing words.
  assert(spool.begin(0, true, true));
  for (int i = 0; i < 8; ++i) assert(spool.append(input, 4));
  assert(spool.remaining() == 0 && !spool.append(input, 1));
  assert(spool.state() == Spool::State::Failed);
  assert(spool.failure() == Spool::Failure::Overflow && !spool.needsEnd());
  spool.abort(Spool::Failure::Transport);
  spool.abort(Spool::Failure::Disconnected);
  assert(spool.failure() == Spool::Failure::Overflow);
  requireZero(memory);

  // A full 60-second capture fits even if the network delivers no bytes until
  // after release. Then drain in arbitrary chunks and verify every sample.
  std::vector<int16_t> full(Spool::kMaxSamples + 16);
  Spool longTurn(full.data(), full.size());
  assert(longTurn.begin(1000, true, true));
  std::vector<int16_t> block(256), sent(2048);
  for (size_t offset = 0; offset < Spool::kMaxSamples; offset += block.size()) {
    for (size_t i = 0; i < block.size(); ++i)
      block[i] = static_cast<int16_t>((offset + i) % 30001);
    assert(longTurn.append(block.data(), block.size()));
  }
  assert(longTurn.remaining() == 0 && longTurn.captured() == Spool::kMaxSamples);
  longTurn.stop(61000);
  assert(longTurn.markStarted(90000));
  size_t total = 0;
  while (longTurn.pending()) {
    auto chunk = longTurn.copyPending(sent.data(), 2017);
    for (size_t i = 0; i < chunk.samples; ++i)
      assert(sent[i] == static_cast<int16_t>((total + i) % 30001));
    total += chunk.samples;
    assert(longTurn.acknowledge(chunk));
  }
  assert(total == 1440000 && longTurn.needsEnd() && longTurn.finish());
  requireZero(full);

  // Slow concurrent progress: producer appends while an older chunk is in
  // flight. A later append must not alter the copied data or its acknowledgment.
  assert(spool.begin(0, true, true));
  assert(spool.append(input, 4) && spool.markStarted(250));
  auto inflight = spool.copyPending(output, 4);
  assert(spool.append(input, 4));
  assert(output[0] == 10 && output[3] == 40);
  assert(spool.acknowledge(inflight) && spool.pending() == 4);
  spool.stop(300);
  assert(spool.acknowledge(spool.copyPending(output, 4)));
  assert(spool.finish());
  requireZero(memory);
}
