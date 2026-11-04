#include <assert.h>
#include <initializer_list>
#include "played_receipt.h"

int main() {
  PlayedReceipt receipt;
  const char *id = "c_0123456789abcdef0123456789abcdef";
  assert(!receipt.remember("c_live_only", 0));
  assert(receipt.remember(id, UINT32_MAX - 2000));
  assert(!receipt.due(1000));
  assert(receipt.due(3000));
  assert(!receipt.due(3001));
  assert(!receipt.remember(id, 4000));
  assert(!receipt.confirm("c_ffffffffffffffffffffffffffffffff"));
  assert(receipt.pending());
  uint32_t now = 3000;
  for (uint32_t delay : {10000u, 20000u, 40000u, 80000u, 160000u, 300000u,
                         300000u, 300000u}) {
    assert(!receipt.due(now + delay - 1));
    now += delay;
    assert(receipt.due(now));
    assert(!receipt.remember(id, now));  // Cannot replace an unconfirmed receipt.
    assert(!receipt.confirm("c_ffffffffffffffffffffffffffffffff"));
  }
  assert(receipt.confirm(id));
  assert(!receipt.pending());
  assert(!receipt.due(now + 300000));
  assert(receipt.remember(id, now));
  assert(!receipt.due(now + 4999));
  assert(receipt.due(now + 5000));
  receipt.clear();
  assert(receipt.remember(id, UINT32_MAX - 2000));
  assert(!receipt.due(2998));
  assert(receipt.due(2999));
  assert(!receipt.due(12998));
  assert(receipt.due(12999));
  return 0;
}
