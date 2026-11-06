#pragma once

class String;

bool cellularSetup();
void cellularService();
bool cellularConnected();
// Power-cycle a live data session and dial again with the saved settings.
void cellularRestart();
int cellularRssiDbm();
void cellularPrintStatus();
bool cellularCommand(const String &command);
