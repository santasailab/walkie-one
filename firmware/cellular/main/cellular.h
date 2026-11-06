#pragma once

#include <stdint.h>

class String;

bool cellularSetup();
void cellularService();
bool cellularConnected();
// Power-cycle a live data session and dial again with the saved settings.
void cellularRestart();
// The mobile network's clock (UTC seconds since 1970), once a session has
// learned it. False until then, and on networks that do not send it.
bool cellularNetworkTime(int64_t &utcSeconds);
int cellularRssiDbm();
void cellularPrintStatus();
bool cellularCommand(const String &command);
