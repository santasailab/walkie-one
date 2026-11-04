#pragma once

class String;

bool cellularSetup();
void cellularService();
bool cellularConnected();
int cellularRssiDbm();
void cellularPrintStatus();
bool cellularCommand(const String &command);
