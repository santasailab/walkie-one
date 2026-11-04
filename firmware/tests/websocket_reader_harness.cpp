#include <assert.h>
#include <string.h>
#include <vector>
#include "websocket_reader.h"

int main() {
  uint8_t buffer[257] = {};
  WebSocketReader reader;
  reader.reset(buffer, 256);
  auto feed = [&](std::vector<uint8_t> bytes, uint32_t now = 10) {
    WebSocketReader::Result result = WebSocketReader::More;
    for (uint8_t byte : bytes) result = reader.feed(byte, now);
    return result;
  };
  // Regression: binary AB, an interleaved ping, then continuation CD.
  assert(feed({0x02, 2, 'A', 'B'}) == WebSocketReader::More);
  assert(feed({0x89, 1, 'x'}) == WebSocketReader::Message);
  assert(reader.opcode() == 9 && reader.size() == 1 && reader.data()[0] == 'x');
  assert(feed({0x80, 2, 'C', 'D'}) == WebSocketReader::Message);
  assert(reader.opcode() == 2 && reader.size() == 4);
  assert(memcmp(reader.data(), "ABCD", 4) == 0);
  assert(feed({0x81, 0}) == WebSocketReader::Message);
  assert(reader.opcode() == 1 && reader.size() == 0);
  // Header and body may arrive a byte at a time, on different loop iterations.
  assert(feed({0x82}) == WebSocketReader::More);
  assert(feed({126}) == WebSocketReader::More);
  assert(feed({0}) == WebSocketReader::More);
  assert(feed({126}) == WebSocketReader::More);
  for (unsigned i = 0; i < 125; ++i) assert(feed({'a'}) == WebSocketReader::More);
  assert(feed({'b'}) == WebSocketReader::Message);
  assert(reader.size() == 126 && reader.data()[125] == 'b');
  assert(feed({0x88, 0}) == WebSocketReader::Message);
  assert(reader.opcode() == 8);

  for (const auto &bad : std::vector<std::vector<uint8_t>>{
      {0x80}, {0xc1}, {0x83}, {0x09}, {0x81, 0x80}, {0x89, 126},
      {0x88, 1}, {0x82, 126, 0, 10}, {0x82, 126, 1, 1},
      {0x82, 127, 0x80}, {0x82, 127, 0, 0, 0, 0, 0, 0, 0, 126},
      {0x02, 0, 0x81}}) {
    reader.reset(buffer, 256);
    assert(feed(bad) == WebSocketReader::Invalid);
  }
  // A slow trickle and pings cannot indefinitely extend a partial message.
  reader.reset(buffer, 256);
  assert(feed({0x02, 1, 'a'}, UINT32_MAX - 1000) == WebSocketReader::More);
  assert(!reader.expired(5000));
  assert(feed({0x89, 0}, 5000) == WebSocketReader::Message);
  assert(reader.expired(7000));
  assert(feed({0x80}, 7000) == WebSocketReader::Invalid);
  reader.reset(buffer, 256); // Reconnecting forgets the incomplete old frame.
  assert(feed({0x81, 1, 'z'}) == WebSocketReader::Message);
  assert(reader.data()[0] == 'z');
}
