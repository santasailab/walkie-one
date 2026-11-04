#pragma once
#include <stddef.h>
#include <stdint.h>

// Incremental server-frame reader, no extensions negotiated (RFC 6455 §5).
// Control payloads have their own storage so a ping cannot overwrite an audio
// message between fragments. The caller owns capacity + 1 bytes for data/NUL.
class WebSocketReader {
 public:
  enum Result { More, Message, Invalid };
  void reset(uint8_t *buffer, size_t capacity) {
    *this = WebSocketReader();
    buffer_ = buffer;
    capacity_ = capacity;
  }
  bool expired(uint32_t now) const {
    return (partial_ && uint32_t(now - frameStarted_) >= 8000) ||
           (fragment_ && uint32_t(now - messageStarted_) >= 8000);
  }
  const uint8_t *data() const { return output_; }
  size_t size() const { return outputSize_; }
  uint8_t opcode() const { return outputOpcode_; }

  Result feed(uint8_t byte, uint32_t now) {
    if (bad_ || expired(now) || !buffer_) return invalid();
    if (phase_ == First) {
      partial_ = true;
      frameStarted_ = now;
      fin_ = (byte & 0x80) != 0;
      opcode_ = byte & 0x0f;
      if ((byte & 0x70) ||
          !(opcode_ == 0 || opcode_ == 1 || opcode_ == 2 ||
            opcode_ == 8 || opcode_ == 9 || opcode_ == 10) ||
          (opcode_ >= 8 && !fin_) || (opcode_ == 0 && !fragment_) ||
          ((opcode_ == 1 || opcode_ == 2) && fragment_)) return invalid();
      if (opcode_ == 1 || opcode_ == 2) {
        fragment_ = opcode_;
        total_ = 0;
        messageStarted_ = now;
      }
      phase_ = Size;
      return More;
    }
    if (phase_ == Size) {
      if (byte & 0x80) return invalid(); // A server must not mask its frames.
      prefix_ = byte;
      length_ = byte;
      if (opcode_ >= 8 && prefix_ > 125) return invalid();
      if (prefix_ <= 125) return payload();
      extended_ = prefix_ == 126 ? 2 : 8;
      length_ = 0;
      phase_ = Extended;
      return More;
    }
    if (phase_ == Extended) {
      if (extended_ == 8 && (byte & 0x80)) return invalid();
      length_ = (length_ << 8) | byte;
      if (--extended_) return More;
      if ((prefix_ == 126 && length_ < 126) ||
          (prefix_ == 127 && length_ <= 65535)) return invalid();
      return payload();
    }
    if (opcode_ >= 8) control_[read_++] = byte;
    else { buffer_[total_++] = byte; ++read_; }
    return read_ == length_ ? finish() : More;
  }

 private:
  enum Phase { First, Size, Extended, Payload } phase_ = First;
  uint8_t *buffer_ = nullptr;
  const uint8_t *output_ = nullptr;
  size_t capacity_ = 0, total_ = 0, read_ = 0, outputSize_ = 0;
  uint64_t length_ = 0;
  uint32_t frameStarted_ = 0, messageStarted_ = 0;
  uint8_t opcode_ = 0, fragment_ = 0, prefix_ = 0, extended_ = 0, outputOpcode_ = 0;
  uint8_t control_[126] = {};
  bool fin_ = false, partial_ = false, bad_ = false;

  Result invalid() { bad_ = true; return Invalid; }
  Result payload() {
    if ((opcode_ < 8 && length_ > capacity_ - total_) ||
        (opcode_ == 8 && length_ == 1)) return invalid();
    read_ = 0;
    phase_ = Payload;
    return length_ ? More : finish();
  }
  Result finish() {
    phase_ = First;
    partial_ = false;
    if (opcode_ >= 8) {
      outputOpcode_ = opcode_;
      outputSize_ = read_;
      control_[read_] = 0;
      output_ = control_;
      return Message;
    }
    if (!fin_) return More;
    outputOpcode_ = fragment_;
    outputSize_ = total_;
    buffer_[total_] = 0;
    output_ = buffer_;
    fragment_ = 0;
    total_ = 0;
    return Message;
  }
};
