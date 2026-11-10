#pragma once

#include <stdint.h>

class String;

bool cellularSetup();
void cellularService();
bool cellularConnected();
// Power-cycle a live data session and dial again with the saved settings.
void cellularRestart();
// Before switching off: end the session, power the module down in order and
// stay down until the restart. The saved settings are not changed.
void cellularShutdown();
// True once the modem supply is off (also before it was ever switched on).
bool cellularPoweredDown();
// The mobile network's clock (UTC seconds since 1970), once a session has
// learned it. False until then, and on networks that do not send it.
bool cellularNetworkTime(int64_t &utcSeconds);
int cellularRssiDbm();
void cellularPrintStatus();
bool cellularCommand(const String &command);
