#pragma once

// Select explicitly in config.h. The assembled prototype was inspected over
// USB on 2026-10-01; its speaker routing differs from the fb5acde7 design file.
// This is not a blanket profile for every N8R2/custom PCB.
#if defined(RELAY_BOARD_06)
#ifndef RELAY_CELLULAR
#error "PCB06 requires the UART cellular target"
#endif
#if defined(RELAY_ASSEMBLED_S3) || defined(RELAY_PDM_SPEAKER) || defined(RELAY_REPAIRED_QUIET_AUDIO)
#error "PCB06 cannot use the legacy assembled-board audio profiles"
#endif
#define PIN_LED_R 47
#define PIN_LED_G 48
#define PIN_LED_B 44
#define PIN_BTN_SPEAK 1
#define PIN_BTN_LISTEN 5
#define PIN_BTN_VOLUME_UP 2
#define PIN_BTN_VOLUME_DOWN 4
#define BUTTON_INPUT_PINS {1, 2, 4, 5}
#define PIN_BTN_POWER -1
#define PIN_BTN_BOOT 0
#define PIN_AMP_SD 13
#define PIN_AMP_BCLK 10
#define PIN_AMP_LRC 11
#define PIN_AMP_DIN 40
#define PIN_MIC_CLK 6
#define PIN_MIC_DOUT 7
#define PIN_I2C_SDA 8
#define PIN_I2C_SCL 9
#define PIN_MODEM_PWR_EN 21
#define PIN_MODEM_PWRKEY 14
#define PIN_MODEM_RESET 15
#define PIN_MODEM_DTR 16
#define PIN_MODEM_TX 17
#define PIN_MODEM_RX 18
#define PIN_MODEM_RI 12
#define PIN_MODEM_USB_BOOT 42
#define PIN_USB_MUX_SEL 39
#define PIN_USB_PRESENT_N 41
#define PIN_CHG_STAT_N 43
// Capture both PDM slots; choose one deterministically for the mono wire format.
// The local j diagnostic reports both. Override at build time after qualification.
#ifndef RELAY_PCB06_MIC_SLOT
#define RELAY_PCB06_MIC_SLOT 0
#endif
#if RELAY_PCB06_MIC_SLOT != 0 && RELAY_PCB06_MIC_SLOT != 1
#error "RELAY_PCB06_MIC_SLOT must be 0 (left) or 1 (right)"
#endif
// PCB06 input calibration, approved in a live listening test. Apply after rumble removal,
// before the existing PCM16 clamp; this does not change microphone hardware.
#ifndef RELAY_PCB06_MIC_GAIN
#define RELAY_PCB06_MIC_GAIN 4
#endif
#if RELAY_PCB06_MIC_GAIN < 1 || RELAY_PCB06_MIC_GAIN > 8
#error "RELAY_PCB06_MIC_GAIN must be an integer from 1 through 8"
#endif
#define RELAY_BUTTON_INPUT_MODE INPUT
#elif defined(RELAY_ASSEMBLED_S3)
#ifdef RELAY_CELLULAR
#error "The assembled S3 profile currently supports Wi-Fi only"
#endif
#define PIN_LED_R 35
#define PIN_LED_G 36
#define PIN_LED_B 37
// Defaults; a Device may store another map (serial `BUTTONS`, button_map.h).
#define PIN_BTN_SPEAK 38
#define PIN_BTN_LISTEN 8
// Individually verified on the repaired unit, 2026-10-05.
#define PIN_BTN_VOLUME_UP 40
#define PIN_BTN_VOLUME_DOWN 39
// J9 pins 1-4 and J12: every input a stored button map may use.
#define BUTTON_INPUT_PINS {38, 39, 40, 7, 8}
#define PIN_BTN_POWER -1
#define PIN_AMP_SD -1
#define PIN_MIC_BCLK 4
#define PIN_MIC_LRCL 5
#define PIN_MIC_DOUT 6
#define PIN_AMP_BCLK 15
#ifdef RELAY_ASSEMBLED_BYPASS_WS47
#define PIN_AMP_LRC 47
#else
#define PIN_AMP_LRC 14
#endif
#define PIN_AMP_DIN 41
#else
#define PIN_LED_R 21
#define PIN_LED_G 39
#define PIN_LED_B 40
#define PIN_BTN_LISTEN 1
#define PIN_BTN_SPEAK 2
#define PIN_BTN_VOLUME_UP -1
#define PIN_BTN_VOLUME_DOWN -1
#define PIN_BTN_POWER 42
#define BUTTON_INPUT_PINS {1, 2}
#define PIN_AMP_SD 41
#define PIN_MIC_BCLK 7
#define PIN_MIC_LRCL 8
#define PIN_MIC_DOUT 9
#define PIN_AMP_BCLK 10
#define PIN_AMP_LRC 11
#define PIN_AMP_DIN 12
#endif

#ifndef RELAY_BUTTON_INPUT_MODE
#define RELAY_BUTTON_INPUT_MODE INPUT_PULLUP
#endif
