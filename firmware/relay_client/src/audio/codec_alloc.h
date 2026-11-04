#pragma once
// All large codec allocations live in PSRAM; internal RAM remains for TLS/I2S.
#ifdef ESP_PLATFORM
#include <esp_heap_caps.h>
#define calloc(n, s) heap_caps_calloc(n, s, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
#define malloc(s) heap_caps_malloc(s, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
#define realloc(p, s) heap_caps_realloc(p, s, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
#define free(p) heap_caps_free(p)
#endif
