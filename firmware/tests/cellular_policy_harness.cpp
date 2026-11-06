#include "cellular_policy.h"

#include <cassert>
#include <cstring>
#include <string>

using namespace cellular_policy;

int main() {
  for (const char *ok : {"OK\r\n", "\r\nOK\r\n", "\t OK \t\n", "AT\r\r\nOK\r\n"})
    assert(terminalResult(ok, std::strlen(ok)) == AtResult::Ok);
  for (const char *error : {"ERROR\r\n", "\tERROR \r", "+CME ERROR: 10\r\n", "+CMS ERROR: unknown\n"})
    assert(terminalResult(error, std::strlen(error)) == AtResult::Error);
  for (const char *data : {"OK", "O", "NOK\r\n", "OKAY\r\n", "+DATA: OK\r\n", "NO ERROR HERE\r\n", "AT+ERROR?\r\n"})
    assert(terminalResult(data, std::strlen(data)) == AtResult::Pending);
  auto config = emptyConfig();
  assert(validConfig(config) && !config.enabled);
  config.enabled = 1;
  assert(!validConfig(config)); // Never auto-start without an APN.
  std::strcpy(config.apn, "internet.example-1");
  assert(validConfig(config));
  config.version = 2;
  assert(!validConfig(config));
  config.version = 1;
  config.enabled = 2;
  assert(!validConfig(config));
  config.enabled = 1;
  std::memset(config.apn, 'x', sizeof(config.apn));
  assert(!validConfig(config)); // Truncated/corrupt NVS cannot escape its buffer.

  assert(validApn("internet"));
  assert(validApn(std::string(100, 'a').c_str()));
  assert(!validApn(std::string(101, 'a').c_str()));
  for (const char *bad : {"", "a b", "a\rAT+CFUN=0", "a\nAT", "a\";AT", "a\\b", "a\tb", "a_1"})
    assert(!validApn(bad));

  const char *sim7670 = "\r\n+IPR: (600,1200,2400,4800,9600,19200,38400,57600,115200,230400,460800)\r\nOK\r\n";
  assert(preferredBaud(sim7670) == 460800);
  assert(preferredBaud("AT+IPR=?\r\n+IPR: (115200,460800,921600)\r\nOK") == 921600);
  assert(preferredBaud("+IPR: (115200,460800,921600)", slowerBaud(921600)) == 460800);
  assert(preferredBaud("+IPR: (115200,460800)", slowerBaud(460800)) == 115200);
  assert(preferredBaud("+IPR: (9216000)\r\n+CGNSSIPR: (921600)") == 0);
  assert(preferredBaud("ERROR\r\n") == 0);
  assert(preferredBaud("+CGNSSIPR: (921600)") == 0);
  assert(preferredBaud("+IPR: (115200)", 0) == 0);

  assert(taggedNumber("+CGATT: 1\r\nOK", "+CGATT:") == 1);
  assert(taggedNumber("+CGATT: 0", "+CGATT:") == 0);
  assert(taggedNumber("+CGATT: 1x", "+CGATT:") == -1);
  assert(signalDbm("+CSQ: 15,99\r\nOK") == -83);
  assert(signalDbm("+CSQ: 0,99") == -113);
  assert(signalDbm("+CSQ: 31,99") == -51);
  assert(signalDbm("+CSQ: 99,99") == -1);
  assert(signalDbm("ERROR") == -1);

  // Network time is local with a quarter-hour offset; the result is UTC.
  assert(networkTimeUtc("\r\n+CCLK: \"26/11/06,12:00:00-32\"\r\n\r\nOK\r\n") == 1793995200);
  assert(networkTimeUtc("+CCLK: \"27/01/01,02:30:00+08\"") == 1798763400);
  assert(networkTimeUtc("+CCLK: \"24/02/29,23:59:59+00\"") == 1709251199);
  assert(networkTimeUtc("+CCLK: \"69/12/31,23:59:59+00\"") == 3155759999);
  // A modem that has not heard the network reports its 1970/1980 default.
  for (const char *unset : {"+CCLK: \"70/01/01,00:00:12+00\"", "+CCLK: \"80/01/06,00:01:40+00\"",
                            "+CCLK: \"23/12/31,23:59:59+00\"", "+CCLK: \"99/12/31,23:59:59+00\""})
    assert(networkTimeUtc(unset) == 0);
  for (const char *bad : {"", "ERROR\r\n", "+CCLK: \"26/11/06,12:00:00\"", "+CCLK: \"26/13/06,12:00:00+00\"",
                          "+CCLK: \"26/00/06,12:00:00+00\"", "+CCLK: \"26/11/31,12:00:00+00\"",
                          "+CCLK: \"25/02/29,12:00:00+00\"", "+CCLK: \"26/11/06,24:00:00+00\"",
                          "+CCLK: \"26/11/06,12:60:00+00\"", "+CCLK: \"26/11/06,12:00:61+00\"",
                          "+CCLK: \"26/11/06,12:00:00+97\"", "+CCLK: \"26/11/06 12:00:00+00\"",
                          "+CCLK: \"2x/11/06,12:00:00+00\"", "+CCLK: 26/11/06,12:00:00+00", "+CCLK: \"26/11/06,12:00"})
    assert(networkTimeUtc(bad) == 0);
  assert(networkTimeUtc(nullptr) == 0);

  assert(!expired(0xfffffff0u, 0xffffff00u, 300));
  assert(expired(0x30u, 0xffffff00u, 300)); // Retry/registration clocks survive rollover.
  // No attempt limit: a Device out of coverage keeps trying, ever more slowly.
  assert(retryDelayMs(0) == 5000 && retryDelayMs(1) == 5000 && retryDelayMs(2) == 15000);
  assert(retryDelayMs(3) == 30000 && retryDelayMs(4) == 60000 && retryDelayMs(5) == 120000);
  assert(retryDelayMs(6) == 240000 && retryDelayMs(7) == kMaxRetryDelayMs && kMaxRetryDelayMs == 300000);
  assert(retryDelayMs(1000) == kMaxRetryDelayMs && retryDelayMs(~0u) == kMaxRetryDelayMs);
}
