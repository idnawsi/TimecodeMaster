/*
 * =========================================================
 * ==      CLIENT LTC GENERATOR WITH JAM SYNC            ==
 * ==      INSTANT LOCK VERSION (Sync Word Detection)    ==
 * =========================================================
 * Multi-Task Version with INSTANT JAM SYNC capability
 *
 * Features:
 * - LTC SMPTE timecode generation (GPIO 25 DAC output)
 * - JAM SYNC: Read external LTC and sync to it INSTANTLY
 * - INSTANT LOCK: Sync word detection in ISR for <1ms accuracy
 * - OLED display (128x32, I2C on GPIO 5/4)
 * - Battery monitoring (GPIO 32)
 * - Mic/Line level switching (GPIO 19)
 * - Jam Sync button (GPIO 33)
 * - SPI Slave communication with Utility Device
 * - LTC Input on GPIO 35 (ADC input)
 *
 * JAM SYNC MODES:
 * - DISABLED: Normal generation (current behavior)
 * - SEARCHING: Looking for external LTC signal
 * - LOCKED: Synced to external LTC (frame-exact)
 * - FREERUN: External LTC lost, free-running with last sync 
 */

#include <cmath>
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_wifi.h>
#include "driver/spi_slave.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "hal/dac_ll.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_ADS1X15.h>
#include <Preferences.h>

// The core defines verifyRollbackLater() as a weak C function; ours must have C linkage to replace it
// (declared here, before any auto-generated prototype). See OTA SAFETY above setup().
extern "C" bool verifyRollbackLater();

// ==================== FIRMWARE IDENTITY ====================
// Identity tag. The master checks it before pushing a firmware to all clients (ClientOTA.ino),
// so a wrong file cannot be installed on every client in one click.
const char TCM_FIRMWARE_ID[] = "TCM-FWID:CLIENT-LTC";

// ==================== OTA UPDATE CONFIGURATION ====================
// OTA is initiated only by the Utility device via SPI (no button on LTC)
#define OTA_MODE_TIMEOUT 300000     // 5 minutes timeout in OTA mode
#define OTA_PASSWORD "timecodemaster"

// Device type for firmware validation
enum DeviceType : uint8_t {
    DEVICE_MASTER_UTILITY = 1,
    DEVICE_MASTER_LTC = 2,
    DEVICE_CLIENT_UTILITY = 3,
    DEVICE_CLIENT_LTC = 4
};
#define THIS_DEVICE_TYPE DEVICE_CLIENT_LTC
#define FIRMWARE_VERSION_MAJOR 1
#define FIRMWARE_VERSION_MINOR 3
#define FIRMWARE_VERSION_PATCH 34

// OTA state variables
volatile bool otaMode = false;
volatile bool otaRequestedViaSPI = false;  // Flag for SPI-triggered OTA
unsigned long otaModeStartTime = 0;
String otaSSID = "";
String utilityOTASSID = "";  // Utility device's OTA SSID (received via SPI)
char deviceName[3] = "";               // Device name/Cam ID from Master (max 2 chars + null)
uint8_t currentUserBits[8] = {0};      // LTC user bits (date + cam ID) from Master

// Tracked date for user bits rollover (decoded from currentUserBits, rolls over at midnight)
uint8_t trackedMonth = 1;
uint8_t trackedDay = 1;
uint8_t trackedYear = 25;  // 2-digit year

bool otaFirmwareValid = false;         // Firmware filename validation flag
String otaFirmwareError = "";          // Error message for invalid firmware
WebServer otaServer(80);

Preferences preferences;
volatile bool frameBufferValid = false;  // TRUE only when frameBits[] contains correct timecode
SemaphoreHandle_t jamSyncSemaphore = NULL;
// ==================== SPI PROTOCOL DEFINITION ====================
#include <stdint.h>

#define SPI_PACKET_SIZE 64

enum SPI_CommandType : uint8_t {
    CMD_POLL_STATUS   = 0,
    CMD_SYNC_TIMECODE = 1,
    CMD_SET_RUNNING   = 2,
    CMD_UPDATE_ESPNOW_STATUS = 3,
    CMD_LOCK_DEVICE = 8,     // Match other devices
    CMD_ENTER_OTA = 9,       // Enter OTA update mode
    CMD_CLEAR_RSTLOG = 14,   // Clear the reset log (brownout / watchdog / crash counters) of this chip
    CMD_SET_LTC_OFFSET = 15  // LTC output offset in frames (signed, in the "frames" field)
};

#pragma pack(push, 1)
struct SPI_Command {
    uint32_t sampleRate;        // 4 bytes - Sample rate (47952 or 48000)
    uint16_t fps_thousandths;   // 2 bytes
    SPI_CommandType command;    // 1 byte
    uint8_t hours;
    uint8_t minutes;
    uint8_t seconds;
    uint8_t frames;
    uint8_t dropFrame;
    uint8_t running;
    uint8_t espNowConnected;
    uint8_t isPaired;           // NEW: Pairing status
    char deviceName[3];         // 3 bytes - Cam ID (2 chars + null, from Master)
    uint8_t userBits[8];        // 8 bytes - LTC user bits (date + cam ID)
    char otaSSID[12];           // 12 bytes - OTA SSID (used with CMD_ENTER_OTA)
    uint8_t _padding[26];       // 64 - 38 = 26 bytes
};

struct SPI_Status {
    uint32_t si5351InterruptCount; // 4 bytes
    uint16_t fps_thousandths;   // 2 bytes
    uint8_t hours;              // 1 byte
    uint8_t minutes;            // 1 byte
    uint8_t seconds;            // 1 byte
    uint8_t frames;             // 1 byte
    uint8_t dropFrame;          // 1 byte
    uint8_t running;            // 1 byte
    uint8_t synced;             // 1 byte
    uint8_t batteryLevel;       // 1 byte (0-4)
    uint8_t batteryPresent;     // 1 byte (0=Missing, 1=Present)
    uint8_t isLineLevel;        // 1 byte (0=MIC, 1=LINE)
    uint8_t jamSyncMode;        // 1 byte (jam sync mode)
    uint8_t systemLocked;       // 1 byte (manual lock state)
    uint8_t syncAck;            // 1 byte - Acknowledgment that sync was applied
    uint8_t lowBatteryShutdown; // 1 byte - LTC requesting shutdown due to low battery
    uint8_t bitIndex;           // 1 byte - Current LTC bit position (0-79) for sub-frame sync
    uint8_t bitSamplePos;       // 1 byte - Sample position within bit (0-24 typically)
    // Firmware version info (for remote OTA)
    uint8_t firmwareVersionMajor;  // 1 byte
    uint8_t firmwareVersionMinor;  // 1 byte
    uint8_t firmwareVersionPatch;  // 1 byte
    uint8_t otaProgress;           // 1 byte - OTA progress 0-100
    char otaSSID[12];           // 12 bytes - LTC OTA SSID (sent when in OTA mode)
    int32_t diagNetLost;        // 4 bytes - clock check: missed (+) / extra (-) sample pulses since boot
    uint16_t diagMaxGapUs;      // 2 bytes - longest gap between two sample interrupts (us)
    uint32_t diagUptimeS;       // 4 bytes - LTC uptime in seconds (for the ppm estimate)
    uint8_t diagMagic;          // 1 byte  - 0xD1 = clock diagnostics present
    uint16_t diagErrFirstS;     // 2 bytes - seconds (since boot/reset) of the first window with >= 2 wrong pulses (0xFFFF = none)
    uint16_t diagErrLastS;      // 2 bytes - seconds of the latest such window
    uint16_t diagErrWins;       // 2 bytes - how many such 43 ms windows
    uint8_t rstReason;          // 1 byte  - why this chip last restarted (esp_reset_reason_t)
    uint8_t rstBrown;           // 1 byte  - brownout resets counted (NVS)
    uint8_t rstWdt;             // 1 byte  - watchdog resets counted
    uint8_t rstPanic;           // 1 byte  - crash (panic) resets counted
    int8_t ltcOutOffset;        // 1 byte  - LTC output offset in frames (set from the master's page)
    uint8_t _padding[4];        // spare (64-byte packet)
};
#pragma pack(pop)

// Compile-time guard: each struct must exactly equal the SPI transfer size.
// Prevents the receive-buffer overflow/overread that occurs if a field is added
// without updating _padding (the bug that left SPI_Command at 61 vs a 64-byte transfer).
static_assert(sizeof(SPI_Command) == SPI_PACKET_SIZE, "SPI_Command size mismatch!");
static_assert(sizeof(SPI_Status)  == SPI_PACKET_SIZE, "SPI_Status size mismatch!");

// ==================== JAM SYNC CONFIGURATION ====================
#define LTC_INPUT_PIN 35
// JAM_SYNC_BUTTON_PIN 33 - REMOVED (now handled by long-press on LEVEL_BUTTON_PIN)
#define FPS_BUTTON_PIN 26
// LOCK_UNLOCK_BUTTON_PIN 27 - REMOVED (now handled by long-press on FPS_BUTTON_PIN)
#define JAM_SYNC_INDICATOR_PIN 27  // HIGH when in JAM_SYNC_SEARCHING mode

enum JamSyncMode : uint8_t {
    JAM_SYNC_DISABLED = 0,
    JAM_SYNC_SEARCHING = 1,
    JAM_SYNC_LOCKED = 2,      // Phase-locked, instant sync
    JAM_SYNC_FREERUN = 3      // Lost signal, continue from last sync
};

volatile JamSyncMode jamSyncMode = JAM_SYNC_DISABLED;
volatile bool jamSyncEnabled = false;
volatile uint32_t lastLtcInputEdgeTime = 0;
volatile uint32_t ltcInputLostTime = 0;
volatile bool ltcInputPresent = false;

// Convert nibble to display character (inverse of charToNibble on Utility)
char nibbleToChar(uint8_t nibble) {
    if (nibble <= 9) return '0' + nibble;
    if (nibble <= 15) return 'A' + (nibble - 10);  // For hex compatibility
    return '?';
}

// ==================== LOCK MODE CONFIGURATION ====================
volatile bool systemLocked = false;  // Lock mode state

// ==================== BOOT SYNC COMPENSATION ====================
// Track if first sync after boot has been received
// Only apply boot compensation on the FIRST sync, not subsequent syncs
volatile bool firstSyncReceived = false;
#define LEVEL_BUTTON_LONG_PRESS_MS 3000  // 3 seconds for Jam Sync toggle
#define FPS_BUTTON_LONG_PRESS_MS 5000    // 5 seconds for Lock/Unlock toggle

// LTC Decoder state (oversized buffer to handle misalignment)
#define LTC_DECODER_BUFFER_SIZE 100  // Collect extra bits to handle frame misalignment
#define LTC_FRAME_SIZE 80            // Actual LTC frame is 80 bits
volatile uint8_t ltcDecoderBits[LTC_DECODER_BUFFER_SIZE];
volatile int ltcDecoderBitIndex = 0;
volatile bool ltcDecoderLevel = false;
volatile uint32_t ltcDecoderLastEdgeTime = 0;
volatile uint32_t ltcDecoderBitDuration = 0;
volatile bool ltcFrameReady = false;
volatile bool ltcPendingHalfBit = false;  // Track if we've seen first half of a '1' bit

// INSTANT LOCK: Sync word detection variables
volatile bool syncWordDetected = false;
volatile uint32_t syncWordDetectedTime = 0;  // When sync word was detected (micros)
volatile int syncWordBitPosition = 0;        // Bit position where sync word ends
volatile bool jamSyncDebugInitialized = false;  // Track if debug counters are initialized
volatile bool phaseLocked = false;           // True when phase is locked (before decode completes)
volatile uint8_t lockedFrameBits[LTC_FRAME_SIZE];  // Copy of frame bits when sync detected

// LTC VERIFICATION: Track consecutive valid frames before locking
#define LTC_VERIFICATION_FRAMES_REQUIRED 2  // Consecutive valid frames required before locking (~80ms at 25fps)
volatile int ltcVerificationCount = 0;
volatile uint8_t ltcLastVerifiedHours = 0;
volatile uint8_t ltcLastVerifiedMinutes = 0;
volatile uint8_t ltcLastVerifiedSeconds = 0;
volatile uint8_t ltcLastVerifiedFrames = 0;
volatile bool ltcVerificationActive = false;

// FPS Auto-detection
#define BIT_DURATION_SAMPLES 20
volatile uint32_t bitDurations[BIT_DURATION_SAMPLES];
volatile int bitDurationIndex = 0;
volatile int bitDurationCount = 0;

// Jam sync thresholds (relaxed for better signal capture)
#define LTC_INPUT_THRESHOLD 2048
#define LTC_SIGNAL_TIMEOUT_MS 3000      // 3 seconds before officially entering FREERUN mode
#define LTC_SIGNAL_LOSS_DETECT_MS 100   // 100ms to detect signal loss (for ISR fallback)
#define LTC_BIT_DURATION_MIN_US 170     // Minimum half-bit duration (~208us for 30fps, -20% tolerance)
#define LTC_BIT_DURATION_MAX_US 310     // Maximum half-bit duration (~260us for 23.976fps, +20% tolerance)
#define LTC_FULL_BIT_MIN_US 360         // Minimum full-bit duration (~416us for 30fps, -15% tolerance)
#define LTC_FULL_BIT_MAX_US 580         // Maximum full-bit duration (~521us for 23.976fps, +10% tolerance)

// ==================== OLED CONFIGURATION ====================
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 32
#define OLED_RESET -1
#define SCREEN_ADDRESS 0x3C

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire1, OLED_RESET);

// ADS1115 16-bit ADC for battery voltage reading (uses Wire1, same as OLED)
Adafruit_ADS1115 ads;
bool adsInitialized = false;

// OLED resilience - keep timecode running even if display fails
bool displayInitialized = false;
unsigned long lastDisplayRetry = 0;
const unsigned long DISPLAY_RETRY_INTERVAL = 5000;  // Retry every 5 seconds

// ==================== AUDIO LEVEL CONFIGURATION ====================
#define LEVEL_BUTTON_PIN 19
#define LEVEL_INDICATOR_PIN 21  // GPIO 21: HIGH when LINE level, LOW when MIC level
// NOTE: LINE and MIC intentionally use the same full-swing DAC values.
// The actual output level is set by external analog circuitry switched via
// LEVEL_INDICATOR_PIN (GPIO 21); the old in-DAC attenuation values were:
// LINE 170/80, MIC 135/110.
#define LINE_LEVEL_HIGH 255
#define LINE_LEVEL_LOW  0
#define MIC_LEVEL_HIGH  255
#define MIC_LEVEL_LOW   0

volatile bool isLineLevel = true;
volatile uint8_t ltcHigh = LINE_LEVEL_HIGH;
volatile uint8_t ltcLow = LINE_LEVEL_LOW;

// ==================== BATTERY CONFIGURATION ====================
// Battery voltage read from ADS1115 ADC (I2C address 0x48, channel A0)
// Battery voltage thresholds - 4 bar system (real battery voltage after x2 multiplier)
const float BATTERY_VOLTAGE_3BAR = 3.8;   // Full (3 bars)
const float BATTERY_VOLTAGE_2BAR = 3.6;   // 2 bars
const float BATTERY_VOLTAGE_1BAR = 3.4;   // 1 bar
const float BATTERY_VOLTAGE_0BAR = 3.2;   // 0 bars (critical) - icon blinks; warning zone 0.1V above the 3.1V shutdown
const float BATTERY_HYSTERESIS_VOLTAGE = 0.1;  // 100mV hysteresis band
float batteryVoltage = 0.0;
int batteryLevel = 0;  // 0-3 bars (4 levels)
bool batteryPresent = true;  // NEW: Detect if battery is plugged in
unsigned long lastBatteryRead = 0;
const unsigned long BATTERY_READ_INTERVAL = 1000;
int lastStableBatteryLevel = 0;
// NEW: Charging detection
#define CHARGING_PIN 36  // VP pin - LOW when charging, HIGH when not charging
volatile bool isCharging = false;
bool wasCharging = false;  // Track previous charging state
unsigned long chargingStoppedTime = 0;  // When charging stopped
const unsigned long CHARGING_STABILIZE_DELAY = 10000;  // Wait 10 seconds after unplugging for voltage to stabilize
const float CHARGING_VOLTAGE_OFFSET = 0.2;  // Charging adds ~0.2V to battery reading, subtract to get real level
unsigned long lastChargingAnimFrame = 0;
const unsigned long CHARGING_ANIM_INTERVAL = 500;  // 500ms between animation frames
int chargingAnimLevel = 0;
// NEW: Low battery blinking
unsigned long lastBatteryBlinkTime = 0;
const unsigned long BATTERY_BLINK_INTERVAL = 500;  // 500ms blink interval
bool batteryBlinkState = false;

// ==================== LOW BATTERY SHUTDOWN ====================
// Real battery voltage (after x2 multiplier from voltage divider)
const float BATTERY_SHUTDOWN_VOLTAGE = 3.1f;      // Shutdown threshold (real battery voltage); resumes at +hysteresis = 3.2V
const float BATTERY_SHUTDOWN_HYSTERESIS = 0.1f;   // Resume threshold hysteresis (prevents oscillation)
const unsigned long SHUTDOWN_STABLE_TIME = 3000;   // Require 3 seconds of stable low reading
const unsigned long SHUTDOWN_WARNING_TIME = 2000;  // Show warning for 2 seconds before shutdown
const unsigned long STARTUP_GRACE_PERIOD = 10000;  // 10 seconds grace period after boot for battery stabilization
unsigned long bootTime = 0;                        // Set in setup() to track boot time

enum ShutdownState : uint8_t {
    SHUTDOWN_NORMAL = 0,
    SHUTDOWN_WARNING = 1,
    SHUTDOWN_PENDING = 2
};
ShutdownState shutdownState = SHUTDOWN_NORMAL;
unsigned long lowBatteryStartTime = 0;       // When low battery was first detected
unsigned long shutdownWarningStart = 0;      // When warning started displaying
bool lowBatteryConfirmed = false;            // True after stable low reading for SHUTDOWN_STABLE_TIME

// ==================== SPI SLAVE CONFIGURATION ====================
#define SPI_MOSI 13
#define SPI_MISO 12
#define SPI_SCLK 14
#define SPI_CS 15
#define HSPI_HOST_IDF SPI2_HOST

static WORD_ALIGNED_ATTR SPI_Command spiCmdBuffer;
static WORD_ALIGNED_ATTR SPI_Status spiStatusBuffer;
spi_slave_transaction_t trans;

// ==================== LTC SMPTE Audio Configuration ====================
#define OUTPUT_PIN      25
#define BITS_PER_FRAME  80

volatile uint32_t SAMPLE_RATE = 48000;  // Dynamic: 48000 for integer fps, 47952 for NTSC
volatile uint8_t frameBits[BITS_PER_FRAME];
volatile int bitIndex = 0;
volatile uint32_t bit_sample_pos = 0;   // Integer sample counter (no floating-point drift)
volatile bool currentLevel = false;
volatile bool ltcEnabled = true;
volatile uint8_t samples_per_bit = 25;  // Integer samples per bit (no floating-point drift)

// Pre-built frame buffer for ISR optimization (buildLTCFrame moved outside ISR)
volatile uint8_t nextFrameBits[BITS_PER_FRAME];
volatile bool nextFrameReady = false;
volatile bool needNextFrame = false;
// Next frame timecode values (calculated in task, used by buildLTCFrameInto)
volatile uint8_t nextHours = 0;
volatile uint8_t nextMinutes = 0;
volatile uint8_t nextSeconds = 0;
volatile uint8_t nextFrames = 0;
volatile bool nextDropFrame = false;
volatile uint16_t nextFps = 24000;
TaskHandle_t FrameBuildTaskHandle = NULL;

volatile uint32_t si5351InterruptCount = 0;

// ==================== CLOCK INTEGRITY DIAGNOSTICS ====================
// Checks whether Si5351 clock pulses are MISSED (the LTC loses time) or EXTRA pulses arrive (e.g.
// ringing on the clock line - the LTC gains time). Every 2048 pulses (~43 ms) the ISR compares the
// pulses it counted with the pulses expected from the elapsed CPU cycle count (one rounding per
// window). Interrupt latency only matters at window edges and cannot accumulate; the CPU crystal's
// own error over 43 ms is ~0.04 pulse. Reported to the Utility over SPI and shown on the master's
// firmware page. Also tracks the longest gap between two ISR runs (worst interrupt latency).
volatile int32_t clkNetLostSamples = 0;    // + missed pulses, - extra pulses (since boot)
volatile uint32_t clkMaxGapCycles = 0;     // longest gap between two ISR runs
static uint32_t clkLastCycles = 0;
static uint32_t clkWinStart = 0;
static uint32_t clkWinPulses = 0;
static uint32_t clkWinRate = 0;
// ==================== RESET LOG: why did this chip last restart? ====================
// A brownout (supply dip), watchdog reset or crash is counted in NVS (kept across restarts) and
// reported to the master's web page, so power problems are visible without a serial monitor.
uint8_t rstSelf[4] = {0, 0, 0, 0};   // [0] last reset reason (esp_reset_reason_t), [1] brownouts, [2] watchdog, [3] crashes
static void rstLogBoot() {
    esp_reset_reason_t r = esp_reset_reason();
    rstSelf[0] = (uint8_t)r;
    Preferences rp;
    if (!rp.begin("rstlog", false)) return;
    rstSelf[1] = rp.getUChar("brown", 0);
    rstSelf[2] = rp.getUChar("wdt", 0);
    rstSelf[3] = rp.getUChar("panic", 0);
    if (r == ESP_RST_BROWNOUT) { if (rstSelf[1] < 255) rstSelf[1]++; rp.putUChar("brown", rstSelf[1]); }
    else if (r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT) { if (rstSelf[2] < 255) rstSelf[2]++; rp.putUChar("wdt", rstSelf[2]); }
    else if (r == ESP_RST_PANIC) { if (rstSelf[3] < 255) rstSelf[3]++; rp.putUChar("panic", rstSelf[3]); }
    rp.end();
}
// "Clear reset log" from the master's page: zero the counters (the last reset reason stays)
static void rstLogClear() {
    rstSelf[1] = rstSelf[2] = rstSelf[3] = 0;
    Preferences rp;
    if (rp.begin("rstlog", false)) {
        rp.putUChar("brown", 0);
        rp.putUChar("wdt", 0);
        rp.putUChar("panic", 0);
        rp.end();
    }
}
static bool clkHaveLast = false;
// Clock-check reset ("start over"): requested by a manual sync (flag in the sync command),
// carried out by the ISR itself so nothing races with it. Time base for "measured over".
static volatile bool clkResetReq = false;
static volatile uint32_t clkResetAtMs = 0;
// When do windows with >= 2 wrong pulses happen (seconds since boot/reset)? Tells a one-off burst at
// start-up (few windows, early) from a long rate mismatch (thousands of windows) or a periodic problem.
static volatile uint16_t clkErrFirstS = 0xFFFF, clkErrLastS = 0, clkErrWins = 0;
static inline void IRAM_ATTR clockDiagTick() {
    uint32_t now = esp_cpu_get_cycle_count();
    uint32_t rate = SAMPLE_RATE;
    if (clkResetReq) {
        clkResetReq = false;
        clkNetLostSamples = 0;
        clkMaxGapCycles = 0;
        clkHaveLast = false;
        clkErrFirstS = 0xFFFF; clkErrLastS = 0; clkErrWins = 0;
        clkResetAtMs = (uint32_t)(esp_timer_get_time() / 1000);
    }
    if (!clkHaveLast) {
        clkHaveLast = true;
        clkLastCycles = clkWinStart = now;
        clkWinPulses = 0;
        clkWinRate = rate;
        return;
    }
    uint32_t gap = now - clkLastCycles;
    clkLastCycles = now;
    if (gap >= 2400000 || rate != clkWinRate) {   // clock stopped > 10 ms, or rate changed: new window
        clkWinStart = now;
        clkWinPulses = 0;
        clkWinRate = rate;
        return;
    }
    if (gap > clkMaxGapCycles) clkMaxGapCycles = gap;
    if (++clkWinPulses >= 2048) {
        uint32_t period = 240000000UL / rate;     // ~5000 CPU cycles at 240 MHz
        uint32_t expected = ((now - clkWinStart) + period / 2) / period;
        int32_t d = (int32_t)expected - (int32_t)clkWinPulses;
        clkNetLostSamples += d;
        if (d >= 2 || d <= -2) {
            uint32_t sec = ((uint32_t)(esp_timer_get_time() / 1000) - clkResetAtMs) / 1000;
            if (sec > 65534) sec = 65534;
            if (clkErrFirstS == 0xFFFF) clkErrFirstS = (uint16_t)sec;
            clkErrLastS = (uint16_t)sec;
            if (clkErrWins < 65535) clkErrWins = clkErrWins + 1;
        }
        clkWinStart = now;
        clkWinPulses = 0;
    }
}

// ==================== BCD LOOKUP TABLES (Performance Optimization) ====================
// Pre-calculated BCD values for 0-59 (eliminates slow modulo/division in ISR)
// DRAM_ATTR ensures these are in RAM, not Flash - critical for ISR access
static DRAM_ATTR const uint8_t bcdUnits[60] = {
  0,1,2,3,4,5,6,7,8,9,0,1,2,3,4,5,6,7,8,9,0,1,2,3,4,5,6,7,8,9,
  0,1,2,3,4,5,6,7,8,9,0,1,2,3,4,5,6,7,8,9,0,1,2,3,4,5,6,7,8,9
};
static DRAM_ATTR const uint8_t bcdTens[60] = {
  0,0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,2,2,2,2,2,2,2,2,2,2,
  3,3,3,3,3,3,3,3,3,3,4,4,4,4,4,4,4,4,4,4,5,5,5,5,5,5,5,5,5,5
};

// ==================== TIMECODE STRUCTURE ====================
struct LTC_Timecode {
  uint8_t hours;
  uint8_t minutes;
  uint8_t seconds;
  uint8_t frames;
  uint16_t fps_thousandths;
  bool dropFrame;
  bool running;
  uint32_t totalFrames;
  unsigned long lastSyncMillis;
  unsigned long masterTimestamp;
  bool synced;
  bool espNowConnected;
  bool isPaired;              // NEW: Pairing status from utility device
  unsigned long lastConnectionAttempt;
};

volatile LTC_Timecode ltcTimecode = {0, 0, 0, 0, 25000, false, false, 0, 0, 0, false, false, false, 0};
portMUX_TYPE ltcTimerMux = portMUX_INITIALIZER_UNLOCKED;
volatile LTC_Timecode ltcSnapshot; // Snapshot for Core 0 tasks
portMUX_TYPE snapshotMux = portMUX_INITIALIZER_UNLOCKED; // Mutex for the snapshot
// === END FIX ===
volatile uint32_t missedInterruptCount = 0;

TaskHandle_t UtilityTaskHandle = NULL;
TaskHandle_t SPITaskHandle = NULL;
TaskHandle_t JamSyncTaskHandle = NULL;

// ==================== FORWARD DECLARATIONS ====================
// IRAM_ATTR functions - called from ISR, must be in IRAM to avoid Flash cache misses
uint8_t IRAM_ATTR getNominalFps(uint16_t fps_thousandths);
bool IRAM_ATTR shouldDropFrame(uint8_t minutes, uint8_t seconds, uint8_t frames, bool dropFrameEnabled, uint16_t fps_thousandths);
void IRAM_ATTR buildLTCFrame();
void decodeLTCFrame();
void calculateTiming();
float getFpsFloat(uint16_t fps_thousandths);

// ==================== DATE ROLLOVER FUNCTIONS ====================
// Days in each month (non-leap year) - DRAM_ATTR for ISR access
static DRAM_ATTR const uint8_t daysInMonth[] = {0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};

// Check if year is a leap year (2-digit year, assumes 2000s)
// IRAM_ATTR: Called from ISR via incrementTrackedDate()
bool IRAM_ATTR isLeapYear(uint8_t year) {
    uint16_t fullYear = 2000 + year;
    return (fullYear % 4 == 0 && fullYear % 100 != 0) || (fullYear % 400 == 0);
}

// Get days in a given month (handles leap year for February)
// IRAM_ATTR: Called from ISR via incrementTrackedDate()
uint8_t IRAM_ATTR getDaysInMonth(uint8_t month, uint8_t year) {
    if (month == 2 && isLeapYear(year)) return 29;
    if (month >= 1 && month <= 12) return daysInMonth[month];
    return 31;  // Fallback
}

// Increment tracked date by one day
// IRAM_ATTR: Called from onLtcTimer ISR when hours wraps from 23 to 0
void IRAM_ATTR incrementTrackedDate() {
    trackedDay++;
    if (trackedDay > getDaysInMonth(trackedMonth, trackedYear)) {
        trackedDay = 1;
        trackedMonth++;
        if (trackedMonth > 12) {
            trackedMonth = 1;
            trackedYear++;
            if (trackedYear > 99) trackedYear = 0;  // Wrap 99->00
        }
    }
    // Update currentUserBits with new date
    currentUserBits[0] = trackedMonth % 10;  // Month units
    currentUserBits[1] = trackedMonth / 10;  // Month tens
    currentUserBits[2] = trackedDay % 10;    // Day units
    currentUserBits[3] = trackedDay / 10;    // Day tens
    currentUserBits[4] = trackedYear % 10;   // Year units
    currentUserBits[5] = trackedYear / 10;   // Year tens
}

// Decode date from user bits (nibbles 0-5 = MMDDYY)
void decodeTrackedDateFromUserBits() {
    // Nibbles are stored as: units first, tens second
    trackedMonth = currentUserBits[1] * 10 + currentUserBits[0];
    trackedDay = currentUserBits[3] * 10 + currentUserBits[2];
    trackedYear = currentUserBits[5] * 10 + currentUserBits[4];
}

// ==================== LTC DECODER FUNCTIONS ====================
uint8_t decodeBCD(uint8_t bits[], int startBit, int numBits) {
    uint8_t value = 0;
    for (int i = 0; i < numBits; i++) {
        value |= (bits[startBit + i] << i);
    }
    return value;
}

// Verify timecode is valid and incrementing forward (allow frame skips during verification)
bool verifyTimecodeIncrement(uint8_t hours, uint8_t minutes, uint8_t seconds, uint8_t frames) {
    if (ltcVerificationCount == 0) {
        return true;
    }

    uint8_t maxFrames = getNominalFps(ltcTimecode.fps_thousandths);
    uint32_t currentTotal = ((uint32_t)hours * 3600 + (uint32_t)minutes * 60 + (uint32_t)seconds) * maxFrames + frames;
    uint32_t lastTotal = ((uint32_t)ltcLastVerifiedHours * 3600 + (uint32_t)ltcLastVerifiedMinutes * 60 + (uint32_t)ltcLastVerifiedSeconds) * maxFrames + ltcLastVerifiedFrames;

    if (currentTotal <= lastTotal) {
        return false;
    }

    uint32_t frameDiff = currentTotal - lastTotal;
    if (frameDiff > 15) {
        return false;
    }

    return true;
}
// Search for sync word in bit buffer
int findSyncWord() {
    // Search for sync word 0xBFFC (binary: 0011111111111101)
    // Sync word is at bits 64-79 of the frame, so frame starts 64 bits before it
    for (int syncWordPos = 64; syncWordPos <= ltcDecoderBitIndex - 16; syncWordPos++) {
        uint16_t syncWord = 0;
        for (int i = 0; i < 16; i++) {
            syncWord |= (ltcDecoderBits[syncWordPos + i] << i);
        }

        if (syncWord == 0xBFFC) {
            // Found sync word! Frame starts 64 bits before this position
            int frameStart = syncWordPos - 64;
            return frameStart;
        }
    }

    return -100;  // No sync word found
}

void decodeLTCFrame() {
    int frameStart = 0;

    if (!phaseLocked) {
        frameStart = findSyncWord();
        if (frameStart < -50) {
            ltcDecoderBitIndex = 0;
            ltcPendingHalfBit = false;
            return;
        }
        if (frameStart < 0 || frameStart + LTC_FRAME_SIZE > ltcDecoderBitIndex) {
            ltcDecoderBitIndex = 0;
            ltcPendingHalfBit = false;
            return;
        }

        portENTER_CRITICAL(&ltcTimerMux);
        for (int i = 0; i < 80; i++) {
            lockedFrameBits[i] = ltcDecoderBits[frameStart + i];
        }
        phaseLocked = true;
        ltcTimecode.running = false;
        frameBufferValid = false;
        portEXIT_CRITICAL(&ltcTimerMux);
    }

    uint16_t syncWordCheck = 0;
    for (int i = 0; i < 16; i++) {
        syncWordCheck |= (lockedFrameBits[64 + i] << i);
    }

    if (syncWordCheck != 0xBFFC) {
        ltcDecoderBitIndex = 0;
        ltcPendingHalfBit = false;
        ltcFrameReady = false;
        syncWordDetected = false;
        phaseLocked = false;
        portENTER_CRITICAL(&ltcTimerMux);
        frameBufferValid = false;
        portEXIT_CRITICAL(&ltcTimerMux);
        memset((void*)ltcDecoderBits, 0, LTC_DECODER_BUFFER_SIZE);
        return;
    }

    uint8_t frameUnits   = decodeBCD((uint8_t*)lockedFrameBits, 0, 4);
    uint8_t frameTens    = decodeBCD((uint8_t*)lockedFrameBits, 8, 2);
    uint8_t secondUnits  = decodeBCD((uint8_t*)lockedFrameBits, 16, 4);
    uint8_t secondTens   = decodeBCD((uint8_t*)lockedFrameBits, 24, 3);
    uint8_t minuteUnits  = decodeBCD((uint8_t*)lockedFrameBits, 32, 4);
    uint8_t minuteTens   = decodeBCD((uint8_t*)lockedFrameBits, 40, 3);
    uint8_t hourUnits    = decodeBCD((uint8_t*)lockedFrameBits, 48, 4);
    uint8_t hourTens     = decodeBCD((uint8_t*)lockedFrameBits, 56, 2);
    bool dropFrameBit    = lockedFrameBits[10];

    uint8_t frames  = frameTens * 10 + frameUnits;
    uint8_t seconds = secondTens * 10 + secondUnits;
    uint8_t minutes = minuteTens * 10 + minuteUnits;
    uint8_t hours   = hourTens * 10 + hourUnits;

    uint8_t maxFrames = getNominalFps(ltcTimecode.fps_thousandths);
    if (hours > 23 || minutes > 59 || seconds > 59 || frames >= maxFrames) {
        ltcDecoderBitIndex = 0;
        ltcPendingHalfBit = false;
        ltcFrameReady = false;
        syncWordDetected = false;
        phaseLocked = false;
        portENTER_CRITICAL(&ltcTimerMux);
        frameBufferValid = false;
        portEXIT_CRITICAL(&ltcTimerMux);
        memset((void*)ltcDecoderBits, 0, LTC_DECODER_BUFFER_SIZE);
        return;
    }

    if (!verifyTimecodeIncrement(hours, minutes, seconds, frames)) {
        ltcVerificationCount = 0;
        ltcVerificationActive = false;
        ltcDecoderBitIndex = 0;
        ltcPendingHalfBit = false;
        ltcFrameReady = false;
        syncWordDetected = false;
        phaseLocked = false;
        portENTER_CRITICAL(&ltcTimerMux);
        frameBufferValid = false;
        portEXIT_CRITICAL(&ltcTimerMux);
        memset((void*)ltcDecoderBits, 0, LTC_DECODER_BUFFER_SIZE);
        return;
    }

    ltcLastVerifiedHours   = hours;
    ltcLastVerifiedMinutes = minutes;
    ltcLastVerifiedSeconds = seconds;
    ltcLastVerifiedFrames  = frames;
    ltcVerificationCount++;

    if (ltcVerificationCount >= LTC_VERIFICATION_FRAMES_REQUIRED) {
        frames++;
        if (frames >= maxFrames) {
            frames = 0;
            seconds++;
            if (seconds >= 60) {
                seconds = 0;
                minutes++;
                if (minutes >= 60) {
                    minutes = 0;
                    hours++;
                    if (hours >= 24) hours = 0;
                }
            }
        }

        // FIX: Check for drop-frame after rollover (29.97 DF only)
        // Skip frames 0,1 at minute boundaries except 10-minute marks
        if (shouldDropFrame(minutes, seconds, frames, dropFrameBit, ltcTimecode.fps_thousandths)) {
            frames = 2;
        }

        portENTER_CRITICAL(&ltcTimerMux);
        ltcTimecode.hours   = hours;
        ltcTimecode.minutes = minutes;
        ltcTimecode.seconds = seconds;
        ltcTimecode.frames  = frames;
        ltcTimecode.dropFrame = dropFrameBit;
        bitIndex       = 0;
        bit_sample_pos = 0;  // Integer reset
        currentLevel   = false;
        buildLTCFrame();
        frameBufferValid   = true;
        ltcTimecode.running = true;
        portEXIT_CRITICAL(&ltcTimerMux);

        jamSyncMode      = JAM_SYNC_FREERUN;
        digitalWrite(JAM_SYNC_INDICATOR_PIN, LOW);  // Turn off indicator when locked/freerun
        jamSyncEnabled   = false;
        ltcInputPresent  = false;
        systemLocked     = true;  // Auto-lock on signal loss to prevent accidental changes

        detachLtcInputIsr();

        portENTER_CRITICAL(&snapshotMux);
        memcpy((void*)&ltcSnapshot, (void*)&ltcTimecode, sizeof(LTC_Timecode));
        portEXIT_CRITICAL(&snapshotMux);

        ltcVerificationCount = 0;
        ltcVerificationActive = false;
    } else {
        if (!ltcVerificationActive) {
            portENTER_CRITICAL(&ltcTimerMux);
            phaseLocked = true;
            ltcTimecode.running = false;
            frameBufferValid = false;
            portEXIT_CRITICAL(&ltcTimerMux);
            ltcVerificationActive = true;
        }
    }

    ltcDecoderBitIndex = 0;
    ltcPendingHalfBit = false;
    ltcFrameReady = false;
    syncWordDetected = false;
    phaseLocked = false;
}
// ==================== LTC INPUT ISR ====================
static volatile uint32_t lastLtcEdgeTime = 0;
static volatile uint32_t ltcEdgeCount = 0;

// ---- IRAM-safe DAC write for the sample ISR -----------------------------------------------
// Arduino's dacWrite() is a heavy flash-resident function (pin-bus lookup, logging, DAC driver).
// It must NOT be called from an ISR that is allowed to run while flash is being written (NVS
// saves on FPS change) - the chip panics with "Cache disabled but cached memory region accessed".
// dac_ll_update_output_value() is a tiny inline register write. GPIO25 = DAC_CHAN_0.
// setup() still calls dacWrite() once (task context) to initialise/enable the DAC channel.
static inline void IRAM_ATTR ltcDacWrite(uint8_t v) { dac_ll_update_output_value(DAC_CHAN_0, v); }

// ==================== DIRECT IRAM GPIO INTERRUPTS ====================
// The Arduino core is built with CONFIG_ARDUINO_ISR_IRAM unset, so attachInterrupt() dispatches
// through flash-resident code and the ISR is held off during ANY flash write/erase (NVS saves
// of fps / level / drop-frame). The sample ISR counts clock edges, so a missed edge is lost time.
// Register both ISRs straight with the IDF GPIO service using ESP_INTR_FLAG_IRAM so flash
// operations cannot delay them. (ISRs must only call IRAM/ROM-safe code: no micros()/millis().)
#ifndef LTC_CLK_PIN
#define LTC_CLK_PIN 34   // Si5351 CLK0 sample clock input
#endif
void IRAM_ATTR onLtcTimer();
void IRAM_ATTR onLtcInputEdge();
static void IRAM_ATTR isrClkTrampoline(void*) { onLtcTimer(); }
static void IRAM_ATTR isrInTrampoline(void*)  { onLtcInputEdge(); }
static bool isrServiceReady = false;
static void ensureIsrService() {
    if (isrServiceReady) return;
    // Level 3 (highest level usable from C): the sample-clock interrupt pre-empts the SPI slave, I2C,
    // timer and tick interrupts (all level 1). At level 1 any of them could hold it off for more than
    // one 20.8 us sample period -> the next clock edge merged with the pending one -> a lost sample
    // (measured: ~2 ppm = about 2 frames per 10 h). Task critical sections still mask it (they are short).
    esp_err_t e = gpio_install_isr_service(ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL3);   // INVALID_STATE = already installed
    isrServiceReady = (e == ESP_OK || e == ESP_ERR_INVALID_STATE);
}
static void attachLtcClockIsr() {
    ensureIsrService();
    gpio_config_t io = {};
    io.pin_bit_mask = 1ULL << LTC_CLK_PIN;
    io.mode = GPIO_MODE_INPUT;
    io.intr_type = GPIO_INTR_POSEDGE;
    gpio_config(&io);
    gpio_isr_handler_add((gpio_num_t)LTC_CLK_PIN, isrClkTrampoline, NULL);
}
static void attachLtcInputIsr() {
    ensureIsrService();
    gpio_set_intr_type((gpio_num_t)LTC_INPUT_PIN, GPIO_INTR_ANYEDGE);
    gpio_isr_handler_add((gpio_num_t)LTC_INPUT_PIN, isrInTrampoline, NULL);
}
static void detachLtcInputIsr() {
    gpio_isr_handler_remove((gpio_num_t)LTC_INPUT_PIN);
    gpio_set_intr_type((gpio_num_t)LTC_INPUT_PIN, GPIO_INTR_DISABLE);
}

void IRAM_ATTR onLtcInputEdge() {
    if (!jamSyncEnabled) return;

    uint32_t now = (uint32_t)esp_timer_get_time();
    // FIX: Handle micros overflow (occurs every ~71 minutes)
    // Unsigned subtraction handles overflow correctly in C/C++
    uint32_t edgeDuration = now - lastLtcEdgeTime;

    // Sanity check: reject obviously wrong durations (overflow glitch or noise)
    // Valid LTC bit durations are 170-580μs; anything > 1000μs is invalid
    if (edgeDuration > 1000) {
        lastLtcEdgeTime = now;
        return;
    }

    bool isHalfBit = (edgeDuration >= LTC_BIT_DURATION_MIN_US &&
                      edgeDuration <= LTC_BIT_DURATION_MAX_US);
    bool isFullBit = (edgeDuration >= LTC_FULL_BIT_MIN_US &&
                      edgeDuration <= LTC_FULL_BIT_MAX_US);

    if (lastLtcEdgeTime != 0 && (isHalfBit || isFullBit)) {
        ltcInputPresent = true;
        lastLtcInputEdgeTime = (uint32_t)esp_timer_get_time();  // microseconds (low 32 bits); no 64-bit divide in the ISR
        ltcEdgeCount++;

        // SEARCHING MODE: Collect bits for initial lock
        if (jamSyncMode == JAM_SYNC_SEARCHING) {
            if (!ltcFrameReady && ltcDecoderBitIndex < LTC_DECODER_BUFFER_SIZE) {
                // Decode bit
                if (isFullBit) {
                    ltcPendingHalfBit = false;
                    ltcDecoderBits[ltcDecoderBitIndex++] = 0;
                } else if (isHalfBit) {
                    if (ltcPendingHalfBit) {
                        ltcPendingHalfBit = false;
                        ltcDecoderBits[ltcDecoderBitIndex++] = 1;
                    } else {
                        ltcPendingHalfBit = true;
                    }
                }

                // Store bit duration for FPS detection
                if (bitDurationCount < BIT_DURATION_SAMPLES) {
                    bitDurations[bitDurationIndex] = edgeDuration;
                    bitDurationIndex = (bitDurationIndex + 1) % BIT_DURATION_SAMPLES;
                    bitDurationCount++;
                }

                // FRAME BOUNDARY DETECTION: Search for sync word when we have enough bits
                if (ltcDecoderBitIndex >= 80 && !syncWordDetected && !ltcFrameReady) {
                    // Search through buffer for sync word 0xBFFC at ANY position
                    for (int syncWordPos = 16; syncWordPos <= ltcDecoderBitIndex && syncWordPos < LTC_DECODER_BUFFER_SIZE; syncWordPos++) {
                        uint16_t syncWord = 0;
                        for (int i = 0; i < 16; i++) {
                            if (syncWordPos - 16 + i >= 0 && syncWordPos - 16 + i < LTC_DECODER_BUFFER_SIZE) {
                                syncWord |= (ltcDecoderBits[syncWordPos - 16 + i] << i);
                            }
                        }

                        if (syncWord == 0xBFFC) {
                            // SYNC WORD FOUND! This is the END of a frame
                            int frameStart = syncWordPos - 80;
                            // Check if we have complete frame
                            if (frameStart >= 0 && frameStart + 80 <= ltcDecoderBitIndex) {
                                syncWordDetected = true;
                                syncWordDetectedTime = now;
                                syncWordBitPosition = syncWordPos;

                                // *** ATOMIC PHASE LOCK ***
                                portENTER_CRITICAL_ISR(&ltcTimerMux);
                                // Copy frame bits for decode (aligned frame)
                                for (int i = 0; i < 80; i++) {
                                    lockedFrameBits[i] = ltcDecoderBits[frameStart + i];
                                }
                                bitIndex = 0;
                                bit_sample_pos = 0;  // Integer reset
                                currentLevel = false;
                                phaseLocked = true;
                                ltcTimecode.running = false;  // STOP generation
                                frameBufferValid = false; // Buffer not ready yet
                                portEXIT_CRITICAL_ISR(&ltcTimerMux);

                                // --- WAKE UP THE TASK ---
                                BaseType_t xHigherPriorityTaskWoken = pdFALSE;
                                xSemaphoreGiveFromISR(jamSyncSemaphore, &xHigherPriorityTaskWoken);
                                if (xHigherPriorityTaskWoken) {
                                    portYIELD_FROM_ISR();
                                }
                                // ltcFrameReady = true; // No longer needed
                                break; // Found it, stop searching
                            }
                        }
                    }
                }

                // *** THIS IS THE FIX ***
                // Fallback: Buffer full without finding sync word - trigger task to handle it
                if (ltcDecoderBitIndex >= LTC_DECODER_BUFFER_SIZE && !ltcFrameReady) {
                    // ltcFrameReady = true; // <-- OLD LINE, REMOVED

                    // NEW FIX: Give the semaphore to wake the task.
                    // The task will call decodeLTCFrame(), fail the sync word check,
                    // reset the decoder, and try again. This prevents the deadlock.
                    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
                    xSemaphoreGiveFromISR(jamSyncSemaphore, &xHigherPriorityTaskWoken);
                    if (xHigherPriorityTaskWoken) {
                        portYIELD_FROM_ISR();
                    }
                }
            }
        }
        // LOCKED MODE: Just track signal presence (no bit collection)
    }

    lastLtcEdgeTime = now;
}
// ==================== JAM SYNC TASK ====================
void jamSyncTask(void* parameter) {
    disableCore1WDT();

    pinMode(LTC_INPUT_PIN, INPUT_PULLDOWN);

    while (true) {
        // FIX: Use timeout instead of blocking forever to allow signal loss detection
        // Wait up to 100ms for semaphore, then check for signal timeout
        if (xSemaphoreTake(jamSyncSemaphore, pdMS_TO_TICKS(100)) == pdTRUE) {
            // This code now runs *microseconds* after the ISR
            if (jamSyncEnabled && jamSyncMode == JAM_SYNC_SEARCHING) {
                syncWordDetected = false;
                decodeLTCFrame();
            }
        }

        // FIX: Signal timeout monitoring (was removed, now restored)
        // Check if we're in SEARCHING mode but haven't received edges for too long
        if (jamSyncEnabled && jamSyncMode == JAM_SYNC_SEARCHING) {
            unsigned long timeSinceLastEdge = ((uint32_t)esp_timer_get_time() - lastLtcInputEdgeTime) / 1000U;  // ms, wrap-safe

            // If no valid LTC edges for LTC_SIGNAL_TIMEOUT_MS, give up searching
            if (lastLtcInputEdgeTime != 0 && timeSinceLastEdge > LTC_SIGNAL_TIMEOUT_MS) {
                // Reset decoder state
                ltcDecoderBitIndex = 0;
                ltcPendingHalfBit = false;
                ltcFrameReady = false;
                syncWordDetected = false;
                phaseLocked = false;
                ltcVerificationCount = 0;
                ltcVerificationActive = false;
                ltcInputPresent = false;

                // Stay in SEARCHING mode but clear the decoder
                // User can disable jam sync manually if they want to stop
                memset((void*)ltcDecoderBits, 0, LTC_DECODER_BUFFER_SIZE);
            }
        }
    }
}

// ==================== LTC GENERATION ISR ====================
void IRAM_ATTR onLtcTimer() {
  si5351InterruptCount++;
  clockDiagTick();   // missed / extra clock pulse check

  portENTER_CRITICAL_ISR(&ltcTimerMux);
  bool isRunning = ltcTimecode.running;
  bool isEnabled = ltcEnabled;
  bool bufferValid = frameBufferValid;

  if (!isRunning || !isEnabled || !bufferValid) {
    portEXIT_CRITICAL_ISR(&ltcTimerMux);
    ltcDacWrite(128);
    return;
  }

  // Read variables needed for math - all integers now!
  uint8_t current_samples_per_bit = samples_per_bit;  // Integer - no floating point!
  uint8_t bit = frameBits[bitIndex];

  // Integer increment - no floating point drift!
  bit_sample_pos++;
  uint32_t local_bit_sample_pos = bit_sample_pos;

  portEXIT_CRITICAL_ISR(&ltcTimerMux);
  __asm__ __volatile__ ("" : : : "memory");

  // --- Math and DAC writes happen OUTSIDE the lock ---
  // Integer mid-point calculation (e.g., 25/2=12, 20/2=10)
  uint8_t mid_point = current_samples_per_bit / 2;

  // Mid-bit crossing (for bit == 1) - integer comparison
  if (bit == 1 && ((local_bit_sample_pos - 1) < mid_point) && (local_bit_sample_pos >= mid_point)) {
    currentLevel = !currentLevel;
    ltcDacWrite(currentLevel ? ltcHigh : ltcLow);
  }

  // Bit end crossing - integer comparison
  if (local_bit_sample_pos >= current_samples_per_bit) {
    currentLevel = !currentLevel;
    ltcDacWrite(currentLevel ? ltcHigh : ltcLow);

    portENTER_CRITICAL_ISR(&ltcTimerMux);

    // Variables for snapshot update (declared outside if block for scope)
    bool updateSnapshot = false;
    LTC_Timecode snapshotCopy;

    // SAFETY: Re-check bit_sample_pos wasn't reset by sync handler
    // Prevents underflow if sync happened between critical sections
    // (sync sets bit_sample_pos=0, subtracting 25 would cause underflow to ~4 billion,
    //  making timecode run ~25x faster until next sync or reboot)
    if (bit_sample_pos >= current_samples_per_bit) {
      bit_sample_pos -= current_samples_per_bit;  // Integer subtraction
      bitIndex++;

      // Frame complete - increment timecode IN THE ISR
      if (bitIndex >= BITS_PER_FRAME) {
      bitIndex = 0;

      // Step 1: Increment frame counter
      ltcTimecode.frames++;
      ltcTimecode.totalFrames++;

      // Step 2: Handle frame wrap-around (frames >= frameLimit)
      uint8_t frameLimit = getNominalFps(ltcTimecode.fps_thousandths);
      if (ltcTimecode.frames >= frameLimit) {
        ltcTimecode.frames = 0;
        ltcTimecode.seconds++;
        if (ltcTimecode.seconds >= 60) {
          ltcTimecode.seconds = 0;
          ltcTimecode.minutes++;
          if (ltcTimecode.minutes >= 60) {
            ltcTimecode.minutes = 0;
            ltcTimecode.hours++;
            if (ltcTimecode.hours >= 24) {
              ltcTimecode.hours = 0;
              // Date rollover: increment tracked date
              incrementTrackedDate();
            }
          }
        }

        // Step 3: AFTER wrap-around, check if drop frame applies
        // Drop frame skips frames 0 and 1 at minute boundaries (except every 10th minute)
        if (shouldDropFrame(ltcTimecode.minutes, ltcTimecode.seconds,
                           ltcTimecode.frames, ltcTimecode.dropFrame,
                           ltcTimecode.fps_thousandths)) {
          ltcTimecode.frames = 2;
          ltcTimecode.totalFrames += 2;  // Account for skipped frames
        }
      }

      // Use pre-built frame if available, otherwise build directly (fallback)
      if (nextFrameReady) {
        // Fast copy from pre-built buffer (much faster than building in ISR)
        memcpy((void*)frameBits, (void*)nextFrameBits, BITS_PER_FRAME);
        nextFrameReady = false;
      } else {
        // Fallback: build directly if task didn't pre-build in time
        buildLTCFrame();
      }
      // Signal task to pre-build next frame
      needNextFrame = true;

        // Cache snapshot while holding ltcTimerMux
        updateSnapshot = true;
        memcpy((void*)&snapshotCopy, (void*)&ltcTimecode, sizeof(LTC_Timecode));
      }
    }
    // If sync reset bit_sample_pos, skip bit advancement - next ISR will start fresh
    portEXIT_CRITICAL_ISR(&ltcTimerMux);

    // Update snapshot AFTER releasing ltcTimerMux to avoid nested critical sections
    if (updateSnapshot) {
      portENTER_CRITICAL_ISR(&snapshotMux);
      memcpy((void*)&ltcSnapshot, (void*)&snapshotCopy, sizeof(LTC_Timecode));
      portEXIT_CRITICAL_ISR(&snapshotMux);
    }
  }
}
void calculateTiming() {
    // Get optimal sample rate and integer samples_per_bit for current FPS
    uint32_t new_sample_rate = getSampleRateForFps(ltcTimecode.fps_thousandths);
    uint8_t new_samples_per_bit = getSamplesPerBitForFps(ltcTimecode.fps_thousandths);

    // MINIMAL critical section - just variable updates
    portENTER_CRITICAL_ISR(&ltcTimerMux);
    SAMPLE_RATE = new_sample_rate;
    samples_per_bit = new_samples_per_bit;
    portEXIT_CRITICAL_ISR(&ltcTimerMux);

    // Note: Si5351 frequency should be updated separately if needed
}

// ==================== LTC HELPER FUNCTIONS ====================
float getFpsFloat(uint16_t fps_thousandths) {
  // Use exact NTSC fractions for precise timing
  if (fps_thousandths == 23976) return 24000.0f / 1001.0f;  // Exact 23.976023976...
  if (fps_thousandths == 29970) return 30000.0f / 1001.0f;  // Exact 29.970029970...
  return fps_thousandths / 1000.0f;
}

// Get optimal Si5351 sample rate for frame rate - ensures integer samples_per_bit
uint32_t getSampleRateForFps(uint16_t fps_thousandths) {
    // NTSC rates (23.976, 29.97) need 47952 Hz for exact integer samples/bit
    // Integer rates (24, 25, 30) work with 48000 Hz
    switch (fps_thousandths) {
        case 23976:
        case 29970:
            return 47952;  // 48000000/1001 ≈ 47952.048, gives exact 25 and 20 samples/bit
        default:
            return 48000;  // Standard rate for integer frame rates
    }
}

// Get integer samples per bit for frame rate (zero drift!)
uint8_t getSamplesPerBitForFps(uint16_t fps_thousandths) {
    switch (fps_thousandths) {
        case 23976: return 25;  // 47952 / (24000/1001 * 80) = 25.0 exact
        case 24000: return 25;  // 48000 / (24 * 80) = 25.0 exact
        case 25000: return 24;  // 48000 / (25 * 80) = 24.0 exact
        case 29970: return 20;  // 47952 / (30000/1001 * 80) = 20.0 exact
        case 30000: return 20;  // 48000 / (30 * 80) = 20.0 exact
        default:    return 25;
    }
}

// ==================== LTC OUTPUT OFFSET ====================
// A camera records LTC through its audio path, whose delay differs per camera model / mode, so two cameras fed the
// same timecode can show pictures a frame or two apart. The master's page sets an offset per unit: the timecode
// WRITTEN into this unit's LTC signal is shifted by that many frames (+ = ahead). The unit's own time, its display,
// jam sync and the sync between units are not affected. Kept in NVS.
volatile int8_t ltcOutOffset = 0;

static void ltcOutLoad() {
    Preferences p;
    if (p.begin("ltcout", true)) {
        int v = p.getChar("ofs", 0);
        p.end();
        if (v >= -30 && v <= 30) ltcOutOffset = (int8_t)v;
    }
}

static void ltcOutSet(int8_t v) {
    if (v < -30) v = -30;
    if (v > 30) v = 30;
    if (v == ltcOutOffset) return;
    ltcOutOffset = v;
    Preferences p;
    if (p.begin("ltcout", false)) {
        p.putChar("ofs", v);
        p.end();
    }
}

// Shift a timecode label by ltcOutOffset frames (drop-frame aware, wraps at 24 h). IRAM: also used in the ISR path.
static inline void IRAM_ATTR ltcOutLabel(uint8_t& h, uint8_t& m, uint8_t& s, uint8_t& f, bool df, uint16_t fps) {
    int32_t off = ltcOutOffset;
    if (off == 0) return;
    int32_t nom = (fps == 23976) ? 24 : (fps == 29970) ? 30 : (int32_t)(fps + 500) / 1000;
    if (nom <= 0 || h > 23 || m > 59 || s > 59 || f >= nom) return;
    bool drop = df && fps == 29970;
    int32_t totalMin = (int32_t)h * 60 + m;
    int32_t idx = (totalMin * 60 + s) * nom + f;
    if (drop) idx -= 2 * (totalMin - totalMin / 10);
    int32_t day = 24 * 3600 * nom - (drop ? 2 * (1440 - 144) : 0);
    idx = ((idx + off) % day + day) % day;
    if (drop) {   // back to a frame label: 17982 frames per 10 minutes, 1798 per dropping minute
        int32_t d = idx / 17982, r = idx % 17982;
        idx += 18 * d + (r > 1 ? 2 * ((r - 2) / 1798) : 0);
    }
    f = (uint8_t)(idx % nom);
    idx /= nom;
    s = (uint8_t)(idx % 60);
    idx /= 60;
    m = (uint8_t)(idx % 60);
    h = (uint8_t)((idx / 60) % 24);
}

void IRAM_ATTR buildLTCFrame() {
  uint32_t* framePtr = (uint32_t*)frameBits;
  for(int i = 0; i < 20; i++) {
    framePtr[i] = 0;
  }

  // Bounds checking to prevent array overflow from corrupted data
  // All values are clamped to valid ranges before lookup
  uint8_t oh = ltcTimecode.hours, om = ltcTimecode.minutes, os_ = ltcTimecode.seconds, of = ltcTimecode.frames;
  ltcOutLabel(oh, om, os_, of, ltcTimecode.dropFrame, ltcTimecode.fps_thousandths);   // LTC output offset
  uint8_t safeFrames = (of < 60) ? of : 0;
  uint8_t safeSeconds = (os_ < 60) ? os_ : 0;
  uint8_t safeMinutes = (om < 60) ? om : 0;
  uint8_t safeHours = (oh < 24) ? oh : 0;

  uint8_t frameUnits = bcdUnits[safeFrames];
  uint8_t frameTens = bcdTens[safeFrames];
  uint8_t secondUnits = bcdUnits[safeSeconds];
  uint8_t secondTens = bcdTens[safeSeconds];
  uint8_t minuteUnits = bcdUnits[safeMinutes];
  uint8_t minuteTens = bcdTens[safeMinutes];
  uint8_t hourUnits = bcdUnits[safeHours];
  uint8_t hourTens = bcdTens[safeHours];

  frameBits[0] = (frameUnits >> 0) & 1;
  frameBits[1] = (frameUnits >> 1) & 1;
  frameBits[2] = (frameUnits >> 2) & 1;
  frameBits[3] = (frameUnits >> 3) & 1;

  frameBits[8] = (frameTens >> 0) & 1;
  frameBits[9] = (frameTens >> 1) & 1;

  bool effectiveDropFrame = (ltcTimecode.fps_thousandths == 29970) ? ltcTimecode.dropFrame : false;
  frameBits[10] = effectiveDropFrame ? 1 : 0;

  frameBits[16] = (secondUnits >> 0) & 1;
  frameBits[17] = (secondUnits >> 1) & 1;
  frameBits[18] = (secondUnits >> 2) & 1;
  frameBits[19] = (secondUnits >> 3) & 1;

  frameBits[24] = (secondTens >> 0) & 1;
  frameBits[25] = (secondTens >> 1) & 1;
  frameBits[26] = (secondTens >> 2) & 1;

  frameBits[32] = (minuteUnits >> 0) & 1;
  frameBits[33] = (minuteUnits >> 1) & 1;
  frameBits[34] = (minuteUnits >> 2) & 1;
  frameBits[35] = (minuteUnits >> 3) & 1;

  frameBits[40] = (minuteTens >> 0) & 1;
  frameBits[41] = (minuteTens >> 1) & 1;
  frameBits[42] = (minuteTens >> 2) & 1;

  frameBits[48] = (hourUnits >> 0) & 1;
  frameBits[49] = (hourUnits >> 1) & 1;
  frameBits[50] = (hourUnits >> 2) & 1;
  frameBits[51] = (hourUnits >> 3) & 1;

  frameBits[56] = (hourTens >> 0) & 1;
  frameBits[57] = (hourTens >> 1) & 1;

  // === USER BITS (SMPTE 12M) ===
  // UB1 (bits 4-7): currentUserBits[0] (month tens)
  frameBits[4] = (currentUserBits[0] >> 0) & 1;
  frameBits[5] = (currentUserBits[0] >> 1) & 1;
  frameBits[6] = (currentUserBits[0] >> 2) & 1;
  frameBits[7] = (currentUserBits[0] >> 3) & 1;

  // UB2 (bits 12-15): currentUserBits[1] (month units)
  frameBits[12] = (currentUserBits[1] >> 0) & 1;
  frameBits[13] = (currentUserBits[1] >> 1) & 1;
  frameBits[14] = (currentUserBits[1] >> 2) & 1;
  frameBits[15] = (currentUserBits[1] >> 3) & 1;

  // UB3 (bits 20-23): currentUserBits[2] (day tens)
  frameBits[20] = (currentUserBits[2] >> 0) & 1;
  frameBits[21] = (currentUserBits[2] >> 1) & 1;
  frameBits[22] = (currentUserBits[2] >> 2) & 1;
  frameBits[23] = (currentUserBits[2] >> 3) & 1;

  // UB4 (bits 28-31): currentUserBits[3] (day units)
  frameBits[28] = (currentUserBits[3] >> 0) & 1;
  frameBits[29] = (currentUserBits[3] >> 1) & 1;
  frameBits[30] = (currentUserBits[3] >> 2) & 1;
  frameBits[31] = (currentUserBits[3] >> 3) & 1;

  // UB5 (bits 36-39): currentUserBits[4] (year tens)
  frameBits[36] = (currentUserBits[4] >> 0) & 1;
  frameBits[37] = (currentUserBits[4] >> 1) & 1;
  frameBits[38] = (currentUserBits[4] >> 2) & 1;
  frameBits[39] = (currentUserBits[4] >> 3) & 1;

  // UB6 (bits 44-47): currentUserBits[5] (year units)
  frameBits[44] = (currentUserBits[5] >> 0) & 1;
  frameBits[45] = (currentUserBits[5] >> 1) & 1;
  frameBits[46] = (currentUserBits[5] >> 2) & 1;
  frameBits[47] = (currentUserBits[5] >> 3) & 1;

  // UB7 (bits 52-55): currentUserBits[6] (cam ID first nibble)
  frameBits[52] = (currentUserBits[6] >> 0) & 1;
  frameBits[53] = (currentUserBits[6] >> 1) & 1;
  frameBits[54] = (currentUserBits[6] >> 2) & 1;
  frameBits[55] = (currentUserBits[6] >> 3) & 1;

  // UB8 (bits 60-63): currentUserBits[7] (cam ID second nibble)
  frameBits[60] = (currentUserBits[7] >> 0) & 1;
  frameBits[61] = (currentUserBits[7] >> 1) & 1;
  frameBits[62] = (currentUserBits[7] >> 2) & 1;
  frameBits[63] = (currentUserBits[7] >> 3) & 1;

  // === SYNC WORD (bits 64-79) ===
  frameBits[64] = 0; frameBits[65] = 0; frameBits[66] = 1; frameBits[67] = 1;
  frameBits[68] = 1; frameBits[69] = 1; frameBits[70] = 1; frameBits[71] = 1;
  frameBits[72] = 1; frameBits[73] = 1;
  frameBits[74] = 1; frameBits[75] = 1;
  frameBits[76] = 1; frameBits[77] = 1; frameBits[78] = 0; frameBits[79] = 1;
}

// Build LTC frame into specified buffer (runs in task, not ISR)
void buildLTCFrameInto(volatile uint8_t* buffer, uint8_t hours, uint8_t minutes,
                       uint8_t seconds, uint8_t frames, bool dropFrame, uint16_t fps) {
  ltcOutLabel(hours, minutes, seconds, frames, dropFrame, fps);   // LTC output offset
  uint32_t* framePtr = (uint32_t*)buffer;
  for(int i = 0; i < 20; i++) {
    framePtr[i] = 0;
  }

  uint8_t safeFrames = (frames < 60) ? frames : 0;
  uint8_t safeSeconds = (seconds < 60) ? seconds : 0;
  uint8_t safeMinutes = (minutes < 60) ? minutes : 0;
  uint8_t safeHours = (hours < 24) ? hours : 0;

  uint8_t frameUnits = bcdUnits[safeFrames];
  uint8_t frameTens = bcdTens[safeFrames];
  uint8_t secondUnits = bcdUnits[safeSeconds];
  uint8_t secondTens = bcdTens[safeSeconds];
  uint8_t minuteUnits = bcdUnits[safeMinutes];
  uint8_t minuteTens = bcdTens[safeMinutes];
  uint8_t hourUnits = bcdUnits[safeHours];
  uint8_t hourTens = bcdTens[safeHours];

  buffer[0] = (frameUnits >> 0) & 1;
  buffer[1] = (frameUnits >> 1) & 1;
  buffer[2] = (frameUnits >> 2) & 1;
  buffer[3] = (frameUnits >> 3) & 1;

  buffer[8] = (frameTens >> 0) & 1;
  buffer[9] = (frameTens >> 1) & 1;

  bool effectiveDropFrame = (fps == 29970) ? dropFrame : false;
  buffer[10] = effectiveDropFrame ? 1 : 0;

  buffer[16] = (secondUnits >> 0) & 1;
  buffer[17] = (secondUnits >> 1) & 1;
  buffer[18] = (secondUnits >> 2) & 1;
  buffer[19] = (secondUnits >> 3) & 1;

  buffer[24] = (secondTens >> 0) & 1;
  buffer[25] = (secondTens >> 1) & 1;
  buffer[26] = (secondTens >> 2) & 1;

  buffer[32] = (minuteUnits >> 0) & 1;
  buffer[33] = (minuteUnits >> 1) & 1;
  buffer[34] = (minuteUnits >> 2) & 1;
  buffer[35] = (minuteUnits >> 3) & 1;

  buffer[40] = (minuteTens >> 0) & 1;
  buffer[41] = (minuteTens >> 1) & 1;
  buffer[42] = (minuteTens >> 2) & 1;

  buffer[48] = (hourUnits >> 0) & 1;
  buffer[49] = (hourUnits >> 1) & 1;
  buffer[50] = (hourUnits >> 2) & 1;
  buffer[51] = (hourUnits >> 3) & 1;

  buffer[56] = (hourTens >> 0) & 1;
  buffer[57] = (hourTens >> 1) & 1;

  // === USER BITS (SMPTE 12M) ===
  // UB1 (bits 4-7): currentUserBits[0] (month tens)
  buffer[4] = (currentUserBits[0] >> 0) & 1;
  buffer[5] = (currentUserBits[0] >> 1) & 1;
  buffer[6] = (currentUserBits[0] >> 2) & 1;
  buffer[7] = (currentUserBits[0] >> 3) & 1;

  // UB2 (bits 12-15): currentUserBits[1] (month units)
  buffer[12] = (currentUserBits[1] >> 0) & 1;
  buffer[13] = (currentUserBits[1] >> 1) & 1;
  buffer[14] = (currentUserBits[1] >> 2) & 1;
  buffer[15] = (currentUserBits[1] >> 3) & 1;

  // UB3 (bits 20-23): currentUserBits[2] (day tens)
  buffer[20] = (currentUserBits[2] >> 0) & 1;
  buffer[21] = (currentUserBits[2] >> 1) & 1;
  buffer[22] = (currentUserBits[2] >> 2) & 1;
  buffer[23] = (currentUserBits[2] >> 3) & 1;

  // UB4 (bits 28-31): currentUserBits[3] (day units)
  buffer[28] = (currentUserBits[3] >> 0) & 1;
  buffer[29] = (currentUserBits[3] >> 1) & 1;
  buffer[30] = (currentUserBits[3] >> 2) & 1;
  buffer[31] = (currentUserBits[3] >> 3) & 1;

  // UB5 (bits 36-39): currentUserBits[4] (year tens)
  buffer[36] = (currentUserBits[4] >> 0) & 1;
  buffer[37] = (currentUserBits[4] >> 1) & 1;
  buffer[38] = (currentUserBits[4] >> 2) & 1;
  buffer[39] = (currentUserBits[4] >> 3) & 1;

  // UB6 (bits 44-47): currentUserBits[5] (year units)
  buffer[44] = (currentUserBits[5] >> 0) & 1;
  buffer[45] = (currentUserBits[5] >> 1) & 1;
  buffer[46] = (currentUserBits[5] >> 2) & 1;
  buffer[47] = (currentUserBits[5] >> 3) & 1;

  // UB7 (bits 52-55): currentUserBits[6] (cam ID first nibble)
  buffer[52] = (currentUserBits[6] >> 0) & 1;
  buffer[53] = (currentUserBits[6] >> 1) & 1;
  buffer[54] = (currentUserBits[6] >> 2) & 1;
  buffer[55] = (currentUserBits[6] >> 3) & 1;

  // UB8 (bits 60-63): currentUserBits[7] (cam ID second nibble)
  buffer[60] = (currentUserBits[7] >> 0) & 1;
  buffer[61] = (currentUserBits[7] >> 1) & 1;
  buffer[62] = (currentUserBits[7] >> 2) & 1;
  buffer[63] = (currentUserBits[7] >> 3) & 1;

  // === SYNC WORD (bits 64-79) ===
  buffer[64] = 0; buffer[65] = 0; buffer[66] = 1; buffer[67] = 1;
  buffer[68] = 1; buffer[69] = 1; buffer[70] = 1; buffer[71] = 1;
  buffer[72] = 1; buffer[73] = 1;
  buffer[74] = 1; buffer[75] = 1;
  buffer[76] = 1; buffer[77] = 1; buffer[78] = 0; buffer[79] = 1;
}

// Task to pre-build next LTC frame (runs on Core 1, high priority)
void frameBuildTask(void* parameter) {
  while (true) {
    if (needNextFrame && ltcTimecode.running) {
      // Read current timecode atomically
      portENTER_CRITICAL(&ltcTimerMux);
      uint8_t h = ltcTimecode.hours;
      uint8_t m = ltcTimecode.minutes;
      uint8_t s = ltcTimecode.seconds;
      uint8_t f = ltcTimecode.frames;
      bool df = ltcTimecode.dropFrame;
      uint16_t fps = ltcTimecode.fps_thousandths;
      portEXIT_CRITICAL(&ltcTimerMux);

      // Calculate next frame timecode (outside critical section)
      uint8_t frameLimit = getNominalFps(fps);
      f++;
      if (f >= frameLimit) {
        f = 0;
        s++;
        if (s >= 60) {
          s = 0;
          m++;
          if (m >= 60) {
            m = 0;
            h++;
            if (h >= 24) {
              h = 0;
            }
          }
        }
        // Check drop frame after wrap
        if (shouldDropFrame(m, s, f, df, fps)) {
          f = 2;
        }
      }

      // Build next frame into buffer
      buildLTCFrameInto(nextFrameBits, h, m, s, f, df, fps);

      // Store next timecode values for ISR to use
      portENTER_CRITICAL(&ltcTimerMux);
      nextHours = h;
      nextMinutes = m;
      nextSeconds = s;
      nextFrames = f;
      nextDropFrame = df;
      nextFps = fps;
      nextFrameReady = true;
      needNextFrame = false;
      portEXIT_CRITICAL(&ltcTimerMux);
    }
    vTaskDelay(1);  // 1ms check interval - plenty of time at 24-30fps
  }
}

uint8_t IRAM_ATTR getNominalFps(uint16_t fps_thousandths) {
  if (fps_thousandths == 23976) return 24;
  if (fps_thousandths == 29970) return 30;
  return fps_thousandths / 1000;
}

// Advance timecode by specified number of frames (for sync compensation)
void advanceTimecodeByFrames(uint8_t* hours, uint8_t* minutes, uint8_t* seconds, uint8_t* frames,
                              uint8_t framesToAdd, uint16_t fps_thousandths, bool dropFrame) {
    uint8_t nominalFps = getNominalFps(fps_thousandths);

    for (uint8_t i = 0; i < framesToAdd; i++) {
        (*frames)++;

        // Handle frame overflow
        if (*frames >= nominalFps) {
            *frames = 0;
            (*seconds)++;

            // Handle second overflow
            if (*seconds >= 60) {
                *seconds = 0;
                (*minutes)++;

                // Handle minute overflow
                if (*minutes >= 60) {
                    *minutes = 0;
                    (*hours)++;

                    // Handle hour overflow (24-hour wrap)
                    if (*hours >= 24) {
                        *hours = 0;
                    }
                }

                // Apply drop-frame compensation at minute boundaries (except every 10th minute)
                if (dropFrame && fps_thousandths == 29970) {
                    if ((*minutes % 10) != 0) {
                        *frames = 2;  // Skip frames 0 and 1
                    }
                }
            }
        }
    }
}

bool IRAM_ATTR shouldDropFrame(uint8_t minutes, uint8_t seconds, uint8_t frames, bool dropFrameEnabled, uint16_t fps_thousandths) {
  if (!dropFrameEnabled || fps_thousandths != 29970) return false;
  if (seconds != 0 || frames > 1) return false;
  if ((minutes % 10) == 0) return false;
  return true;
}

void syncLTCWithCurrentSettings() {
    // Get optimal sample rate and integer samples_per_bit for current FPS
    uint32_t new_sample_rate = getSampleRateForFps(ltcTimecode.fps_thousandths);
    uint8_t new_samples_per_bit = getSamplesPerBitForFps(ltcTimecode.fps_thousandths);

    // Pre-build frame OUTSIDE critical section to minimize ISR blocking
    uint8_t tempFrameBits[BITS_PER_FRAME];
    buildLTCFrameInto((volatile uint8_t*)tempFrameBits, ltcTimecode.hours, ltcTimecode.minutes,
                      ltcTimecode.seconds, ltcTimecode.frames, ltcTimecode.dropFrame,
                      ltcTimecode.fps_thousandths);

    // ATOMIC: Update ALL ISR state in a single critical section
    portENTER_CRITICAL(&ltcTimerMux);
    {
        bitIndex = 0;
        bit_sample_pos = 0;  // Integer reset
        currentLevel = false;
        SAMPLE_RATE = new_sample_rate;  // Update sample rate for new FPS
        samples_per_bit = new_samples_per_bit;  // Integer samples per bit

        // Fast memcpy instead of slow buildLTCFrame() - prevents ISR starvation
        memcpy((void*)frameBits, tempFrameBits, BITS_PER_FRAME);
        frameBufferValid = true;  // Mark buffer valid

        // Clear stale pre-built frame and request fresh build
        nextFrameReady = false;
        needNextFrame = true;
    }
    portEXIT_CRITICAL(&ltcTimerMux);

    // Update snapshot AFTER main critical section
    portENTER_CRITICAL(&snapshotMux);
    memcpy((void*)&ltcSnapshot, (void*)&ltcTimecode, sizeof(LTC_Timecode));
    portEXIT_CRITICAL(&snapshotMux);
}
// ==================== BATTERY FUNCTIONS ====================
void readBatteryLevel() {
    if (millis() - lastBatteryRead < BATTERY_READ_INTERVAL) return;

    // Check charging state first
    isCharging = (digitalRead(CHARGING_PIN) == LOW);

    // Track charging state transitions for stabilization after unplugging
    if (wasCharging && !isCharging) {
        // Charger was just unplugged - start stabilization period
        chargingStoppedTime = millis();
    }
    wasCharging = isCharging;

    // During stabilization period after unplugging, keep battery level frozen
    // This prevents the display from dropping suddenly when charger is removed
    if (chargingStoppedTime > 0 && (millis() - chargingStoppedTime < CHARGING_STABILIZE_DELAY)) {
        lastBatteryRead = millis();
        return;  // Keep battery level frozen during voltage stabilization
    }

    // Clear stabilization timer once period is complete
    if (chargingStoppedTime > 0 && (millis() - chargingStoppedTime >= CHARGING_STABILIZE_DELAY)) {
        chargingStoppedTime = 0;
    }

    // Read battery voltage using ADS1115 ADC (channel A0)
    if (adsInitialized) {
        // Without a battery the TP4056 output oscillates and its charging pin flips, so the readings jump around. The
        // presence flag must not flicker with that: "no battery" evidence counts 4, a clean reading counts 1, and the flag
        // switches to "absent" below 6 and back to "present" only above 12 (a missing battery is sticky).
        static int8_t presScore = 16;
        static bool presState = true;
        int n = (isCharging || presScore < 16 || !presState) ? 5 : 1;   // a single sample while everything looks normal
        float minV = 10.0, maxV = 0.0, sumV = 0.0;
        for (int i = 0; i < n; i++) {
            int16_t adc0 = ads.readADC_SingleEnded(0);
            float v = adc0 * 0.000125 * 2.0;
            if (v < minV) minV = v;
            if (v > maxV) maxV = v;
            sumV += v;
            if (i < n - 1) delay(2);   // short delay between samples
        }
        bool looksAbsent = (n > 1 && maxV - minV > 0.15) || (maxV < 0.5);   // large spread (floating output) or no voltage at all
        if (looksAbsent) presScore = (presScore > 4) ? presScore - 4 : 0;
        else if (presScore < 16) presScore++;
        if (presState && presScore < 6) presState = false;
        else if (!presState && presScore > 12) presState = true;
        batteryPresent = presState;
        batteryVoltage = isCharging ? maxV : (sumV / n);   // charging: the highest sample (as before); otherwise the average
    }

    // Apply charging voltage offset
    float effectiveVoltage = batteryVoltage;
    if (isCharging && batteryPresent) {
        effectiveVoltage = batteryVoltage - CHARGING_VOLTAGE_OFFSET;
    }

    // Determine battery level
    int newLevel;
    if (!batteryPresent) newLevel = 0;
    else if (effectiveVoltage >= BATTERY_VOLTAGE_3BAR) newLevel = 3;
    else if (effectiveVoltage >= BATTERY_VOLTAGE_2BAR) newLevel = 2;
    else if (effectiveVoltage >= BATTERY_VOLTAGE_1BAR) newLevel = 1;
    else newLevel = 0;

    // Apply hysteresis
    if (newLevel != lastStableBatteryLevel && batteryPresent) {
        float hysteresisThreshold;

        // Calculate the voltage threshold we need to cross to change level
        switch (lastStableBatteryLevel) {
            case 3:
                // Going down from 3 bars: need to drop below 2.2V - hysteresis
                hysteresisThreshold = BATTERY_VOLTAGE_3BAR - BATTERY_HYSTERESIS_VOLTAGE;
                break;
            case 2:
                if (newLevel > 2) {
                    // Going up to 3 bars: need to reach 2.2V + hysteresis
                    hysteresisThreshold = BATTERY_VOLTAGE_3BAR + BATTERY_HYSTERESIS_VOLTAGE;
                } else {
                    // Going down to 1 bar: need to drop below 1.9V - hysteresis
                    hysteresisThreshold = BATTERY_VOLTAGE_2BAR - BATTERY_HYSTERESIS_VOLTAGE;
                }
                break;
            case 1:
                if (newLevel > 1) {
                    // Going up to 2 bars: need to reach 1.9V + hysteresis
                    hysteresisThreshold = BATTERY_VOLTAGE_2BAR + BATTERY_HYSTERESIS_VOLTAGE;
                } else {
                    // Going down to 0 bars: need to drop below 1.75V - hysteresis
                    hysteresisThreshold = BATTERY_VOLTAGE_1BAR - BATTERY_HYSTERESIS_VOLTAGE;
                }
                break;
            case 0:
                // Going up from 0 bars: need to reach 1.75V + hysteresis
                hysteresisThreshold = BATTERY_VOLTAGE_1BAR + BATTERY_HYSTERESIS_VOLTAGE;
                break;
            default:
                hysteresisThreshold = BATTERY_VOLTAGE_2BAR;
                break;
        }

        // Check if we've crossed the hysteresis threshold
        bool shouldChange = false;
        if (newLevel > lastStableBatteryLevel) {
            // Going up - need to be above upper threshold
            shouldChange = (batteryVoltage >= hysteresisThreshold);
        } else {
            // Going down - need to be below lower threshold
            shouldChange = (batteryVoltage <= hysteresisThreshold);
        }

        if (shouldChange) {
            lastStableBatteryLevel = newLevel;
            batteryLevel = newLevel;
        }
        // If not crossing threshold, keep the old stable level
    }

    // Ensure batteryLevel matches stable level when not changing
    batteryLevel = lastStableBatteryLevel;

    lastBatteryRead = millis();
}

// ==================== LOW BATTERY SHUTDOWN HANDLER ====================
// Non-blocking state machine for graceful shutdown on low battery
void handleLowBatteryShutdown() {
    unsigned long now = millis();

    // Skip during startup grace period - battery readings need time to stabilize
    // Also ensures charging state is properly detected before making shutdown decisions
    if (now - bootTime < STARTUP_GRACE_PERIOD) {
        return;
    }

    // Don't shutdown if charging or if no battery is detected
    if (isCharging || !batteryPresent) {
        shutdownState = SHUTDOWN_NORMAL;
        lowBatteryConfirmed = false;
        lowBatteryStartTime = 0;
        return;
    }

    // Check if battery is below shutdown threshold
    bool isBelowThreshold = (batteryVoltage < BATTERY_SHUTDOWN_VOLTAGE);

    // If we were in shutdown sequence but voltage recovered (hysteresis)
    if (!isBelowThreshold && batteryVoltage >= (BATTERY_SHUTDOWN_VOLTAGE + BATTERY_SHUTDOWN_HYSTERESIS)) {
        shutdownState = SHUTDOWN_NORMAL;
        lowBatteryConfirmed = false;
        lowBatteryStartTime = 0;
        return;
    }

    switch (shutdownState) {
        case SHUTDOWN_NORMAL:
            if (isBelowThreshold) {
                // Start timing low battery condition
                if (lowBatteryStartTime == 0) {
                    lowBatteryStartTime = now;
                } else if (now - lowBatteryStartTime >= SHUTDOWN_STABLE_TIME) {
                    // Low battery confirmed after stable period
                    lowBatteryConfirmed = true;
                    shutdownState = SHUTDOWN_WARNING;
                    shutdownWarningStart = now;
                }
            } else {
                lowBatteryStartTime = 0;  // Reset if voltage recovered
            }
            break;

        case SHUTDOWN_WARNING:
            // Display warning with countdown (only update every 500ms to reduce flicker)
            {
                static unsigned long lastWarningUpdate = 0;
                unsigned long elapsed = now - shutdownWarningStart;

                if (displayInitialized && (now - lastWarningUpdate >= 500 || lastWarningUpdate == 0)) {
                    lastWarningUpdate = now;
                    int secondsLeft = (SHUTDOWN_WARNING_TIME - elapsed) / 1000 + 1;
                    if (secondsLeft < 1) secondsLeft = 1;

                    display.clearDisplay();
                    display.setTextSize(1);
                    display.setTextColor(SSD1306_WHITE);
                    display.setCursor(20, 10);
                    display.println(F("LOW BATTERY"));
                    display.setCursor(15, 25);
                    display.println(F("Please Charge"));
                    display.setCursor(30, 45);
                    display.print(F("Shutdown: "));
                    display.print(secondsLeft);
                    display.display();
                }

                // After warning time, move to shutdown pending
                if (elapsed >= SHUTDOWN_WARNING_TIME) {
                    shutdownState = SHUTDOWN_PENDING;
                }
            }
            break;

        case SHUTDOWN_PENDING:
            // Set SPI flag so Utility knows to shutdown too
            spiStatusBuffer.lowBatteryShutdown = 1;

            // Show final message
            if (displayInitialized) {
                display.clearDisplay();
                display.setTextSize(1);
                display.setCursor(25, 25);
                display.println(F("POWER OFF"));
                display.display();
                delay(500);  // Show message briefly

                // Turn off OLED (SSD1306 command 0xAE)
                display.ssd1306_command(SSD1306_DISPLAYOFF);
            }

            // Give Utility time to read the flag (one more SPI poll cycle ~100ms)
            delay(200);  // Brief delay for final SPI transfer

            // Enable wake on charger connected (GPIO 39 = HIGH when USB 5V present via voltage divider)
            esp_sleep_enable_ext0_wakeup(GPIO_NUM_39, 1);
            esp_deep_sleep_start();
            // Code never reaches here - ESP32 is now in deep sleep
            break;
    }
}

void drawBatteryIcon(int x, int y, int level) {
    // Check charging state in real-time (digital read is fast, no need to wait for battery read interval)
    isCharging = (digitalRead(CHARGING_PIN) == LOW);
    unsigned long now = millis();

    if (!batteryPresent) {
        // Show "NO BAT" or an empty icon with an X
        display.drawRect(x, y, 14, 8, SSD1306_WHITE);
        display.fillRect(x + 14, y + 2, 2, 4, SSD1306_WHITE);
        display.drawLine(x + 2, y + 2, x + 12, y + 6, SSD1306_WHITE);
        display.drawLine(x + 2, y + 6, x + 12, y + 2, SSD1306_WHITE);
        return;
    }

    // Update blink state for low battery (below 1.6V)
    // Skip blinking during startup grace period - battery readings need time to stabilize
    if (batteryVoltage < BATTERY_VOLTAGE_0BAR && !isCharging && (now - bootTime >= STARTUP_GRACE_PERIOD)) {
        if (now - lastBatteryBlinkTime >= BATTERY_BLINK_INTERVAL) {
            lastBatteryBlinkTime = now;
            batteryBlinkState = !batteryBlinkState;
        }
        // Don't draw battery icon if in blink-off state
        if (!batteryBlinkState) {
            return;
        }
    }

    // Draw battery outline
    display.drawRect(x, y, 14, 8, SSD1306_WHITE);
    display.fillRect(x + 14, y + 2, 2, 4, SSD1306_WHITE);

    // Handle charging animation - blink the last bar while charging
    if (isCharging) {
        // Toggle blink state every CHARGING_ANIM_INTERVAL
        if (now - lastChargingAnimFrame >= CHARGING_ANIM_INTERVAL) {
            lastChargingAnimFrame = now;
            chargingAnimLevel = !chargingAnimLevel;  // Toggle between 0 and 1
        }

        // Draw battery bars with last bar blinking
        int currentLevel = (lastStableBatteryLevel > 0) ? lastStableBatteryLevel : 1;
        int barWidth = 3;
        int barSpacing = 1;
        int startX = x + 2;

        for (int i = 0; i < currentLevel && i < 3; i++) {
            int barX = startX + i * (barWidth + barSpacing);
            if (barX + barWidth <= x + 13) {
                // Draw all bars except the last one solidly
                // The last bar blinks (only draw when chargingAnimLevel == 1)
                if (i < currentLevel - 1 || chargingAnimLevel == 1) {
                    display.fillRect(barX, y + 2, barWidth, 4, SSD1306_WHITE);
                }
            }
        }
    } else {
        // Reset blink state when not charging
        chargingAnimLevel = 0;

        // Normal battery display (only draw bars if level > 0)
        // 3 bars max for levels 1-3 (level 0 = empty, no bars)
        if (level > 0) {
            int barWidth = 3;
            int barSpacing = 1;
            int startX = x + 2;
            for (int i = 0; i < level && i < 3; i++) {
                int barX = startX + i * (barWidth + barSpacing);
                // Don't draw bar if it would overlap with right edge (x+12 or beyond)
                if (barX + barWidth <= x + 13) {
                    display.fillRect(barX, y + 2, barWidth, 4, SSD1306_WHITE);
                }
            }
        }
    }
}

// ==================== BUTTON HANDLING ====================
// SHORT PRESS (<3s): Toggle LINE/MIC audio output level
// LONG PRESS (>=3s): Enable/Disable Jam Sync mode
void checkLevelButton() {
    // Block if system is locked
    if (systemLocked) return;

    static unsigned long lastDebounceTime = 0;
    static unsigned long buttonPressStartTime = 0;
    static bool debouncedButtonState = HIGH;
    static bool lastButtonState = HIGH;
    static bool longPressTriggered = false;
    const unsigned long debounceDelay = 50;

    bool currentButtonState = digitalRead(LEVEL_BUTTON_PIN);

    if (currentButtonState != lastButtonState) {
        lastDebounceTime = millis();
    }

    if ((millis() - lastDebounceTime) > debounceDelay) {
        // Button pressed (HIGH -> LOW transition)
        if (debouncedButtonState == HIGH && currentButtonState == LOW) {
            buttonPressStartTime = millis();
            longPressTriggered = false;
        }

        // Button is being held down - check for long press
        if (currentButtonState == LOW && !longPressTriggered) {
            unsigned long holdDuration = millis() - buttonPressStartTime;

            if (holdDuration >= LEVEL_BUTTON_LONG_PRESS_MS) {
                longPressTriggered = true;

                // LONG PRESS: Toggle Jam Sync mode
                if (jamSyncMode == JAM_SYNC_LOCKED || jamSyncMode == JAM_SYNC_FREERUN || jamSyncMode == JAM_SYNC_SEARCHING) {
                    // Disable jam sync - LTC KEEPS RUNNING
                    jamSyncEnabled = false;
                    jamSyncMode = JAM_SYNC_DISABLED;
                    digitalWrite(JAM_SYNC_INDICATOR_PIN, LOW);  // Turn off indicator
                    ltcDecoderBitIndex = 0;
                    ltcPendingHalfBit = false;
                    ltcInputPresent = false;
                    syncWordDetected = false;
                    phaseLocked = false;
                    detachLtcInputIsr();
                } else {
                    // Enable and start searching - LTC STOPS until locked
                    jamSyncEnabled = true;
                    jamSyncMode = JAM_SYNC_SEARCHING;
                    digitalWrite(JAM_SYNC_INDICATOR_PIN, HIGH);  // Turn on indicator
                    ltcDecoderBitIndex = 0;
                    ltcPendingHalfBit = false;
                    ltcFrameReady = false;
                    syncWordDetected = false;
                    phaseLocked = false;
                    lastLtcEdgeTime = 0;
                    ltcEdgeCount = 0;
                    bitDurationCount = 0;
                    bitDurationIndex = 0;
                    jamSyncDebugInitialized = false;

                    // Reset verification state
                    ltcVerificationCount = 0;
                    ltcVerificationActive = false;
                    ltcLastVerifiedHours = 0;
                    ltcLastVerifiedMinutes = 0;
                    ltcLastVerifiedSeconds = 0;
                    ltcLastVerifiedFrames = 0;

                    // STOP LTC output until we lock
                    portENTER_CRITICAL(&ltcTimerMux);
                    ltcTimecode.running = false;
                    portEXIT_CRITICAL(&ltcTimerMux);

                    attachLtcInputIsr();
                }
            }
        }

        // Button released (LOW -> HIGH transition)
        if (debouncedButtonState == LOW && currentButtonState == HIGH) {
            // Only trigger short press if long press wasn't triggered
            if (!longPressTriggered) {
                // SHORT PRESS: Toggle LINE/MIC level
                isLineLevel = !isLineLevel;

                if (isLineLevel) {
                    ltcHigh = LINE_LEVEL_HIGH;
                    ltcLow = LINE_LEVEL_LOW;
                } else {
                    ltcHigh = MIC_LEVEL_HIGH;
                    ltcLow = MIC_LEVEL_LOW;
                }
                digitalWrite(LEVEL_INDICATOR_PIN, isLineLevel ? HIGH : LOW);
                preferences.begin("ltc-config", false);
                preferences.putBool("isLineLevel", isLineLevel);
                preferences.end();
            }
        }

        debouncedButtonState = currentButtonState;
    }

    lastButtonState = currentButtonState;
}

// SHORT PRESS (<5s): Cycle through FPS rates (23.976 → 24 → 25 → 29.97 → 29.97DF → 30)
// LONG PRESS (>=5s): Lock/Unlock device (disables wireless)
void checkFpsButton() {
    static unsigned long lastDebounceTime = 0;
    static unsigned long buttonPressStartTime = 0;
    static bool debouncedButtonState = HIGH;
    static bool lastButtonState = HIGH;
    static bool longPressTriggered = false;
    const unsigned long debounceDelay = 50;

    bool currentButtonState = digitalRead(FPS_BUTTON_PIN);

    if (currentButtonState != lastButtonState) {
        lastDebounceTime = millis();
    }

    if ((millis() - lastDebounceTime) > debounceDelay) {
        // Button pressed (HIGH -> LOW transition)
        if (debouncedButtonState == HIGH && currentButtonState == LOW) {
            buttonPressStartTime = millis();
            longPressTriggered = false;
        }

        // Button is being held down - check for long press
        if (currentButtonState == LOW && !longPressTriggered) {
            unsigned long holdDuration = millis() - buttonPressStartTime;

            if (holdDuration >= FPS_BUTTON_LONG_PRESS_MS) {
                longPressTriggered = true;

                // LONG PRESS: Toggle Lock/Unlock
                systemLocked = !systemLocked;
            }
        }

        // Button released (LOW -> HIGH transition)
        if (debouncedButtonState == LOW && currentButtonState == HIGH) {
            // Only trigger short press if long press wasn't triggered and system not locked
            if (!longPressTriggered && !systemLocked) {
                // SHORT PRESS: Cycle FPS

                // Don't allow FPS change when jam sync is locked
                if (jamSyncMode == JAM_SYNC_LOCKED) {
                    debouncedButtonState = currentButtonState;
                    lastButtonState = currentButtonState;
                    return;
                }

                // Cycle through FPS rates
                uint16_t currentFps = ltcTimecode.fps_thousandths;
                bool currentDropFrame = ltcTimecode.dropFrame;
                uint16_t newFps;
                bool newDropFrame = false;

                switch(currentFps) {
                    case 23976:
                        newFps = 24000;
                        break;
                    case 24000:
                        newFps = 25000;
                        break;
                    case 25000:
                        newFps = 29970;
                        newDropFrame = false;
                        break;
                    case 29970:
                        if (!currentDropFrame) {
                            newFps = 29970;
                            newDropFrame = true;
                        } else {
                            newFps = 30000;
                        }
                        break;
                    case 30000:
                    default:
                        newFps = 23976;
                        break;
                }

                // Update FPS
                portENTER_CRITICAL(&ltcTimerMux);
                {
                    ltcTimecode.fps_thousandths = newFps;
                    ltcTimecode.dropFrame = newDropFrame;

                    // Clamp frame counter to the valid range for the new FPS.
                    // Prevents an out-of-range frame number when shifting to a lower
                    // max (e.g. 25->24/23.976, where frame 24 would otherwise persist).
                    if (ltcTimecode.frames >= getNominalFps(newFps)) {
                        ltcTimecode.frames = 0;
                    }

                    buildLTCFrame();
                }
                portEXIT_CRITICAL(&ltcTimerMux);

                // Manually update the snapshot so the OLED refresh task sees the change
                portENTER_CRITICAL(&snapshotMux);
                {
                    ltcSnapshot.fps_thousandths = newFps;
                    ltcSnapshot.dropFrame = newDropFrame;
                    ltcSnapshot.frames = ltcTimecode.frames;
                }
                portEXIT_CRITICAL(&snapshotMux);

                // Recalculate timing for new FPS
                calculateTiming();

                // Save to preferences
                preferences.begin("ltc-config", false);
                preferences.putUShort("fps", newFps);
                preferences.putBool("dropFrame", newDropFrame);
                preferences.end();
            }
        }

        debouncedButtonState = currentButtonState;
    }

    lastButtonState = currentButtonState;
}

// ==================== OLED DISPLAY FUNCTIONS ====================
void updateOLEDDisplay() {
    // OLED resilience: retry connection if display failed
    if (!displayInitialized) {
        if (millis() - lastDisplayRetry >= DISPLAY_RETRY_INTERVAL) {
            lastDisplayRetry = millis();
            // Try to reinitialize display
            if (display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS, false, false)) {
                displayInitialized = true;
                display.clearDisplay();
                display.setTextColor(SSD1306_WHITE);
                display.setTextWrap(false);
            }
        }
        return;  // Skip display update - timecode keeps running
    }

    try {
        // === FIX: Read from ltcSnapshot, not ltcTimecode ===
        uint8_t hours, minutes, seconds, frames;
        uint16_t fps_1000;
        bool espNowConn, synced, dropFrame, paired;
        
        portENTER_CRITICAL(&snapshotMux); // <-- Use snapshot mutex
        {
            hours = ltcSnapshot.hours;        // <-- Use snapshot struct
            minutes = ltcSnapshot.minutes;
            seconds = ltcSnapshot.seconds;
            frames = ltcSnapshot.frames;
            fps_1000 = ltcSnapshot.fps_thousandths;
            espNowConn = ltcSnapshot.espNowConnected;
            synced = ltcSnapshot.synced;
            dropFrame = ltcSnapshot.dropFrame;
            paired = ltcSnapshot.isPaired;
        }
        portEXIT_CRITICAL(&snapshotMux); // <-- Release snapshot mutex
        
        // This is a volatile global, reading it directly is fine
        JamSyncMode currentJamMode = jamSyncMode; 
        // === END FIX ===

        String currentStatusStr;
        if (systemLocked) {
            currentStatusStr = "LOCKED";
        } else if (currentJamMode == JAM_SYNC_DISABLED) {
            // Status priority: SYNC > CONN > PRD > DISC
            // SYNC = paired + connected + synced (master online and synced)
            // CONN = paired + connected but not synced (master online but not yet synced)
            // PRD  = paired but not connected (master offline)
            // DISC = not paired
            if (espNowConn && synced) currentStatusStr = "SYNC";
            else if (espNowConn) currentStatusStr = "CONN";
            else if (paired) currentStatusStr = "PRD";
            else currentStatusStr = "DISC";
        } else if (currentJamMode == JAM_SYNC_SEARCHING) {
            currentStatusStr = "JSRC";
        } else if (currentJamMode == JAM_SYNC_LOCKED) {
            currentStatusStr = ltcInputPresent ? "JLCK" : "JDRP";  // Locked or Dropout
        } else if (currentJamMode == JAM_SYNC_FREERUN) {
            currentStatusStr = "JRUN";
        }

        String fpsString;
        bool showDF = false;
        switch(fps_1000) {
            case 23976: fpsString = "23.98"; break;
            case 24000: fpsString = "24"; break;
            case 25000: fpsString = "25"; break;
            case 29970:
                fpsString = "29.97";
                if (dropFrame) { showDF = true; }
                break;
            case 30000: fpsString = "30"; break;
            default: fpsString = String(getFpsFloat(fps_1000), 1);
        }
        if (showDF) { fpsString += "DF"; }

        display.clearDisplay();
        display.setTextSize(1);

        drawBatteryIcon(0, 0, batteryLevel);
        display.setCursor(18, 0);
        display.print(fpsString);

        // Calculate position for status to avoid overlap with FPS
        int fpsWidth = fpsString.length() * 6;
        int statusStartX = 18 + fpsWidth + 6;  // 6px spacing after FPS
        display.setCursor(statusStartX, 0);
        display.print(currentStatusStr);

        String levelStr = isLineLevel ? "LINE" : "MIC";
        int levelWidth = levelStr.length() * 6;
        display.setCursor(SCREEN_WIDTH - levelWidth, 0);
        display.print(levelStr);

        display.setTextSize(2);
        int timecodeY = 19;
        display.setCursor(5, timecodeY); if (hours < 10) display.print("0"); display.print(hours);
        display.setCursor(25, timecodeY); display.print(":");
        display.setCursor(35, timecodeY); if (minutes < 10) display.print("0"); display.print(minutes);
        display.setCursor(55, timecodeY); display.print(":");
        display.setCursor(65, timecodeY); if (seconds < 10) display.print("0"); display.print(seconds);
        display.setCursor(85, timecodeY); display.print(":");
        display.setCursor(95, timecodeY); if (frames < 10) display.print("0"); display.print(frames);

        // Battery voltage display (below status bar, right side)
        display.setTextSize(1);
        char voltStr[8];
        dtostrf(batteryVoltage, 4, 2, voltStr);
        strcat(voltStr, "V");
        int voltWidth = strlen(voltStr) * 6;
        display.setCursor(SCREEN_WIDTH - voltWidth, 9);
        display.print(voltStr);

        // LTC output offset (set from the master's page), centre of the second row - only when not 0
        if (ltcOutOffset != 0) {
            char ofsStr[8];
            snprintf(ofsStr, sizeof(ofsStr), "OFS%+d", (int)ltcOutOffset);
            display.setCursor((SCREEN_WIDTH - (int)strlen(ofsStr) * 6) / 2, 9);
            display.print(ofsStr);
        }

        // Device name display (below battery icon, left side)
        if (deviceName[0] != '\0') {
            display.setCursor(0, 9);
            display.print("ID:");
            display.print(deviceName);
        }

        display.display();
    } catch (...) {
        // Display failed during operation - mark for retry
        displayInitialized = false;
        lastDisplayRetry = millis();  // Start retry timer
    }
}

// ==================== SPI SLAVE SETUP ====================
void setupSPI_Slave() {
    esp_err_t ret;

    spi_bus_config_t buscfg = {
        .mosi_io_num = SPI_MOSI,
        .miso_io_num = SPI_MISO,
        .sclk_io_num = SPI_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = SPI_PACKET_SIZE
    };

    spi_slave_interface_config_t slvcfg = {
        .spics_io_num = SPI_CS,
        .flags = 0,
        .queue_size = 1,
        .mode = 1,
        .post_setup_cb = NULL,
        .post_trans_cb = NULL
    };

    ret = spi_slave_initialize(HSPI_HOST_IDF, &buscfg, &slvcfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK) {
        return;
    }

    memset(&trans, 0, sizeof(trans));
    trans.tx_buffer = &spiStatusBuffer;
    trans.rx_buffer = &spiCmdBuffer;
    trans.length = SPI_PACKET_SIZE * 8;
}

// ==================== SPI TASK (CORE 0 - MEDIUM PRIORITY) ====================
// ==================== SPI TASK (CORE 0 - MEDIUM PRIORITY) ====================
void spiTask(void* parameter) {
    while (true) {
        // === FIX: Read from ltcSnapshot, not ltcTimecode ===
        uint8_t h, m, s, f;
        uint16_t fps;
        uint8_t df, run, sync;

        portENTER_CRITICAL(&snapshotMux); // <-- Use snapshot mutex
        {
            h = ltcSnapshot.hours;        // <-- Use snapshot struct
            m = ltcSnapshot.minutes;
            s = ltcSnapshot.seconds;
            f = ltcSnapshot.frames;
            fps = ltcSnapshot.fps_thousandths;
            df = ltcSnapshot.dropFrame;
            run = ltcSnapshot.running;
            sync = ltcSnapshot.synced;
        }
        portEXIT_CRITICAL(&snapshotMux); // <-- Release snapshot mutex
        // === END FIX ===

        spiStatusBuffer.hours = h;
        spiStatusBuffer.minutes = m;
        spiStatusBuffer.seconds = s;
        spiStatusBuffer.frames = f;
        spiStatusBuffer.fps_thousandths = fps;
        spiStatusBuffer.dropFrame = df;
        spiStatusBuffer.running = run;
        spiStatusBuffer.synced = sync;
        spiStatusBuffer.batteryLevel = batteryLevel;
        spiStatusBuffer.batteryPresent = batteryPresent ? 1 : 0;
        spiStatusBuffer.isLineLevel = isLineLevel ? 1 : 0;
        spiStatusBuffer.jamSyncMode = (uint8_t)jamSyncMode;
        spiStatusBuffer.systemLocked = systemLocked ? 1 : 0;
        spiStatusBuffer.syncAck = 0;  // Client doesn't use syncAck
        spiStatusBuffer.lowBatteryShutdown = (shutdownState == SHUTDOWN_PENDING) ? 1 : 0;
        // Include OTA SSID if in OTA mode
        if (otaMode && otaSSID.length() > 0) {
            strncpy(spiStatusBuffer.otaSSID, otaSSID.c_str(), sizeof(spiStatusBuffer.otaSSID) - 1);
            spiStatusBuffer.otaSSID[sizeof(spiStatusBuffer.otaSSID) - 1] = '\0';
        } else {
            memset(spiStatusBuffer.otaSSID, 0, sizeof(spiStatusBuffer.otaSSID));
        }
        // Snapshot data represents frame boundary, so sub-frame position is 0
        spiStatusBuffer.bitIndex = 0;
        spiStatusBuffer.bitSamplePos = 0;
        // Include firmware version for remote OTA
        spiStatusBuffer.firmwareVersionMajor = FIRMWARE_VERSION_MAJOR;
        spiStatusBuffer.firmwareVersionMinor = FIRMWARE_VERSION_MINOR;
        spiStatusBuffer.firmwareVersionPatch = FIRMWARE_VERSION_PATCH;
        spiStatusBuffer.otaProgress = 0;  // TODO: Update during OTA
        spiStatusBuffer.diagNetLost = clkNetLostSamples;
        {
            uint32_t us = clkMaxGapCycles / 240;
            spiStatusBuffer.diagMaxGapUs = us > 65535 ? 65535 : (uint16_t)us;
        }
        spiStatusBuffer.diagUptimeS = ((uint32_t)(esp_timer_get_time() / 1000) - clkResetAtMs) / 1000;   // since reset/boot
        spiStatusBuffer.diagErrFirstS = clkErrFirstS;
        spiStatusBuffer.diagErrLastS = clkErrLastS;
        spiStatusBuffer.diagErrWins = clkErrWins;
        spiStatusBuffer.rstReason = rstSelf[0];
        spiStatusBuffer.rstBrown = rstSelf[1];
        spiStatusBuffer.rstWdt = rstSelf[2];
        spiStatusBuffer.rstPanic = rstSelf[3];
        spiStatusBuffer.ltcOutOffset = ltcOutOffset;
        spiStatusBuffer.diagMagic = 0xD1;

        // Never arm a second transaction: spi_slave_transmit() leaves a timed-out transaction armed,
        // and queueing again would arm another one on the same buffers - the master's next packet
        // would then land in spiCmdBuffer while this one is still being processed (corrupted
        // firmware data, lost or mixed-up commands).
        static bool spiArmed = false;
        esp_err_t ret = ESP_OK;
        if (!spiArmed) {
            ret = spi_slave_queue_trans(HSPI_HOST_IDF, &trans, pdMS_TO_TICKS(10));
            if (ret == ESP_OK) spiArmed = true;
        }
        if (spiArmed) {
            spi_slave_transaction_t* doneTrans = nullptr;
            ret = spi_slave_get_trans_result(HSPI_HOST_IDF, &doneTrans, pdMS_TO_TICKS(10));
            if (ret == ESP_OK) spiArmed = false;
        }
        if (ret == ESP_OK) {
            if (systemLocked && spiCmdBuffer.command != CMD_POLL_STATUS && spiCmdBuffer.command != CMD_CLEAR_RSTLOG) {
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }

            switch (spiCmdBuffer.command) {
                case CMD_SYNC_TIMECODE:
                    {
                        if (spiCmdBuffer._padding[0] == 0xC5) clkResetReq = true;   // manual sync: restart the clock check
                        // === IMMEDIATE SYNC: Do everything atomically ===
                        // Track changes BEFORE updating (for preferences save)
                        bool fpsChanged = (ltcTimecode.fps_thousandths != spiCmdBuffer.fps_thousandths);
                        bool dropFrameChanged = (ltcTimecode.dropFrame != spiCmdBuffer.dropFrame);

                        // Update sample rate if valid (before calculating timing)
                        bool sampleRateChanged = false;
                        if (spiCmdBuffer.sampleRate == 47952 || spiCmdBuffer.sampleRate == 48000) {
                            if (SAMPLE_RATE != spiCmdBuffer.sampleRate) {
                                sampleRateChanged = true;
                            }
                            SAMPLE_RATE = spiCmdBuffer.sampleRate;
                        }

                        // Get optimal sample rate and integer samples_per_bit for FPS
                        uint32_t new_sample_rate = getSampleRateForFps(spiCmdBuffer.fps_thousandths);
                        uint8_t new_samples_per_bit = getSamplesPerBitForFps(spiCmdBuffer.fps_thousandths);

                        // Boot compensation removed - master's +2 frame compensation is sufficient
                        // after removing the 2-frame delay from Master Utility
                        firstSyncReceived = true;

                        // Pre-build frame OUTSIDE critical section to minimize ISR blocking
                        uint8_t tempFrameBits[BITS_PER_FRAME];
                        buildLTCFrameInto((volatile uint8_t*)tempFrameBits, spiCmdBuffer.hours,
                                          spiCmdBuffer.minutes, spiCmdBuffer.seconds, spiCmdBuffer.frames,
                                          spiCmdBuffer.dropFrame, spiCmdBuffer.fps_thousandths);

                        // ATOMIC: Update ALL ISR state in a single critical section
                        portENTER_CRITICAL(&ltcTimerMux);

                        // Reset LTC generator state
                        bitIndex = 0;
                        bit_sample_pos = 0;  // Integer reset
                        currentLevel = false;
                        SAMPLE_RATE = new_sample_rate;  // Update sample rate
                        samples_per_bit = new_samples_per_bit;  // Integer samples per bit

                        // Set new timecode
                        ltcTimecode.hours = spiCmdBuffer.hours;
                        ltcTimecode.minutes = spiCmdBuffer.minutes;
                        ltcTimecode.seconds = spiCmdBuffer.seconds;
                        ltcTimecode.frames = spiCmdBuffer.frames;
                        ltcTimecode.fps_thousandths = spiCmdBuffer.fps_thousandths;
                        ltcTimecode.dropFrame = spiCmdBuffer.dropFrame;
                        ltcTimecode.running = spiCmdBuffer.running;
                        ltcTimecode.espNowConnected = spiCmdBuffer.espNowConnected;
                        ltcTimecode.isPaired = spiCmdBuffer.isPaired;
                        ltcTimecode.synced = true;
                        ltcTimecode.lastSyncMillis = millis();

                        // Copy user bits from SPI command and decode tracked date
                        memcpy(currentUserBits, spiCmdBuffer.userBits, 8);
                        decodeTrackedDateFromUserBits();

                        // Fast memcpy instead of slow buildLTCFrame() - prevents ISR starvation
                        memcpy((void*)frameBits, tempFrameBits, BITS_PER_FRAME);
                        frameBufferValid = true;

                        // Clear stale pre-built frame and request fresh build
                        // Prevents ISR from using old frame data after sync
                        nextFrameReady = false;
                        needNextFrame = true;

                        portEXIT_CRITICAL(&ltcTimerMux);

                        // Update snapshot AFTER main critical section
                        portENTER_CRITICAL(&snapshotMux);
                        memcpy((void*)&ltcSnapshot, (void*)&ltcTimecode, sizeof(LTC_Timecode));
                        portEXIT_CRITICAL(&snapshotMux);

                        // Save settings to preferences if changed (persists across reboots)
                        if (sampleRateChanged || fpsChanged || dropFrameChanged) {
                            preferences.begin("ltc-config", false);
                            if (sampleRateChanged) {
                                preferences.putUInt("sampleRate", SAMPLE_RATE);
                            }
                            if (fpsChanged) {
                                preferences.putUShort("fps", spiCmdBuffer.fps_thousandths);
                            }
                            if (dropFrameChanged) {
                                preferences.putBool("dropFrame", spiCmdBuffer.dropFrame);
                            }
                            preferences.end();
                        }
                    }
                    break;

                case CMD_SET_RUNNING:
                    // === FIX: Use mutexes, not noInterrupts() ===
                    portENTER_CRITICAL(&ltcTimerMux);
                    {
                        ltcTimecode.running = spiCmdBuffer.running;
                        ltcTimecode.espNowConnected = spiCmdBuffer.espNowConnected;
                        ltcTimecode.isPaired = spiCmdBuffer.isPaired;
                    }
                    portEXIT_CRITICAL(&ltcTimerMux);
                    
                    portENTER_CRITICAL(&snapshotMux);
                    {
                        ltcSnapshot.running = spiCmdBuffer.running;
                        ltcSnapshot.espNowConnected = spiCmdBuffer.espNowConnected;
                        ltcSnapshot.isPaired = spiCmdBuffer.isPaired;
                    }
                    portEXIT_CRITICAL(&snapshotMux);
                    // === END FIX ===
                    break;

                case CMD_UPDATE_ESPNOW_STATUS:
                    // === FIX: Use mutexes, not noInterrupts() ===
                    portENTER_CRITICAL(&ltcTimerMux);
                    {
                        ltcTimecode.espNowConnected = spiCmdBuffer.espNowConnected;
                        ltcTimecode.isPaired = spiCmdBuffer.isPaired;
                    }
                    portEXIT_CRITICAL(&ltcTimerMux);

                    portENTER_CRITICAL(&snapshotMux);
                    {
                        ltcSnapshot.espNowConnected = spiCmdBuffer.espNowConnected;
                        ltcSnapshot.isPaired = spiCmdBuffer.isPaired;
                    }
                    portEXIT_CRITICAL(&snapshotMux);

                    // Copy device name/cam ID if provided (max 2 chars)
                    if (spiCmdBuffer.deviceName[0] != '\0') {
                        strncpy(deviceName, spiCmdBuffer.deviceName, 2);
                        deviceName[2] = '\0';
                    }

                    // Copy user bits (date + cam ID) and decode tracked date
                    memcpy(currentUserBits, spiCmdBuffer.userBits, 8);
                    decodeTrackedDateFromUserBits();
                    // === END FIX ===
                    break;

                case CMD_LOCK_DEVICE:
                    {
                        bool lockState = (spiCmdBuffer.running == 1);
                        systemLocked = lockState;

                        // REMOVED: FLASH write is unnecessary (never loaded on boot)
                        // and it blocks SPI for 10-100ms causing race conditions
                        // preferences.begin("ltc-config", false);
                        // preferences.putBool("systemLocked", systemLocked);
                        // preferences.end();
                    }
                    break;

                case CMD_ENTER_OTA:
                    {
                        // Enter OTA mode - signaled by Utility device
                        // Extract utility device's SSID from command
                        if (spiCmdBuffer.otaSSID[0] != '\0') {
                            utilityOTASSID = String(spiCmdBuffer.otaSSID);
                        }
                        otaRequestedViaSPI = true;  // Signal to main task to enter OTA
                    }
                    break;

                case CMD_POLL_STATUS:
                    break;
                case CMD_SET_LTC_OFFSET:
                    ltcOutSet((int8_t)spiCmdBuffer.frames);
                    break;

                case CMD_CLEAR_RSTLOG:
                    rstLogClear();
                    break;
                default:
                    break;
            }

            memset(&spiCmdBuffer, 0, sizeof(spiCmdBuffer));
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// ==================== OTA UPDATE FUNCTIONS ====================
String generateOTASSID() {
    uint64_t chipId = ESP.getEfuseMac();
    char suffix[5];
    snprintf(suffix, sizeof(suffix), "%04X", (uint16_t)(chipId & 0xFFFF));
    return "TCC-L-" + String(suffix);  // TimeCode Client - LTC
}

void handleOTARoot() {
    String html = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>OTA Update - Client LTC</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 500px; margin: 0 auto; padding: 20px; background: #16213e; border-radius: 10px; }
        h1 { color: #4ecca3; text-align: center; }
        .info { background: #0f3460; padding: 15px; border-radius: 5px; margin: 15px 0; }
        .info p { margin: 5px 0; }
        .warning { background: #ff6b35; color: #000; padding: 10px; border-radius: 5px; margin: 15px 0; }
        form { margin-top: 20px; }
        input[type='file'] { width: 100%; padding: 10px; margin: 10px 0; background: #0f3460; border: 1px solid #4ecca3; color: #fff; border-radius: 5px; box-sizing: border-box; }
        input[type='submit'] { width: 100%; padding: 15px; background: #4ecca3; color: #000; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        input[type='submit']:hover { background: #3db892; }
    </style>
</head>
<body>
    <div class='container'>
        <h1>OTA Update</h1>
        <div class='info'>
            <p><strong>Device:</strong> Client LTC</p>
            <p><strong>Version:</strong> )rawliteral" + String(FIRMWARE_VERSION_MAJOR) + "." + String(FIRMWARE_VERSION_MINOR) + "." + String(FIRMWARE_VERSION_PATCH) + R"rawliteral(</p>
            <p><strong>Hardware ID:</strong> )rawliteral" + otaSSID.substring(6) + R"rawliteral(</p>
        </div>
        <div class='warning'>
            Only upload CLIENT LTC firmware to this device!
        </div>
        <form id='updateForm' method='POST' action='/update' enctype='multipart/form-data' onsubmit='return handleSubmit()'>
            <input type='file' name='firmware' id='firmwareFile' accept='.bin' required>
            <input type='submit' id='submitBtn' value='Upload Firmware'>
            <div id='progressDiv' style='display:none;margin-top:15px;'>
                <div style='background:#0f3460;border-radius:5px;overflow:hidden;'>
                    <div id='progressBar' style='width:0%;height:20px;background:#e94560;transition:width 0.3s;'></div>
                </div>
                <p id='progressText' style='text-align:center;margin-top:5px;'>Uploading... 0%</p>
            </div>
        </form>
        <script>
            function handleSubmit() {
                var btn = document.getElementById('submitBtn');
                var fileInput = document.getElementById('firmwareFile');
                if (!fileInput.files.length) return false;

                btn.value = 'Uploading...';
                btn.disabled = true;
                btn.style.background = '#666';
                btn.style.cursor = 'not-allowed';
                document.getElementById('progressDiv').style.display = 'block';

                var formData = new FormData(document.getElementById('updateForm'));
                var xhr = new XMLHttpRequest();

                xhr.upload.onprogress = function(e) {
                    if (e.lengthComputable) {
                        var pct = Math.round((e.loaded / e.total) * 100);
                        document.getElementById('progressBar').style.width = pct + '%';
                        document.getElementById('progressText').innerText = pct < 100 ? 'Uploading... ' + pct + '%' : 'Installing... Please wait!';
                        if (pct >= 100) {
                            btn.value = 'Installing... Do not turn off!';
                            document.getElementById('progressBar').style.background = '#4ecca3';
                        }
                    }
                };

                xhr.onload = function() {
                    document.body.innerHTML = xhr.responseText;
                };

                xhr.onerror = function() {
                    btn.value = 'Upload Failed - Try Again';
                    btn.disabled = false;
                    btn.style.background = '#e94560';
                    btn.style.cursor = 'pointer';
                };

                xhr.open('POST', '/update', true);
                xhr.send(formData);
                return false;
            }
        </script>
    </div>
</body>
</html>
)rawliteral";
    otaServer.send(200, "text/html", html);
}

void handleOTAUpdate() {
    otaServer.sendHeader("Connection", "close");

    // Check if firmware was rejected due to filename validation
    if (!otaFirmwareValid && otaFirmwareError.length() > 0) {
        String errorHtml = "<html><body style='background:#1a1a2e;color:#e94560;text-align:center;padding:50px;font-family:Arial;'>";
        errorHtml += "<h1>Wrong Firmware!</h1>";
        errorHtml += "<p style='font-size:18px;'>" + otaFirmwareError + "</p>";
        errorHtml += "<p style='margin-top:30px;'><a href='/' style='color:#4ecca3;font-size:16px;'>Try Again</a></p>";
        errorHtml += "</body></html>";
        otaServer.send(200, "text/html", errorHtml);
        otaFirmwareError = "";  // Clear error for next attempt
        return;
    }

    if (Update.hasError()) {
        otaServer.send(200, "text/html",
            "<html><body style='background:#1a1a2e;color:#e94560;text-align:center;padding:50px;font-family:Arial;'><h1>Update Failed!</h1><a href='/' style='color:#4ecca3;'>Back</a></body></html>"
        );
    } else {
        otaServer.send(200, "text/html",
            "<html><body style='background:#1a1a2e;color:#4ecca3;text-align:center;padding:50px;font-family:Arial;'><h1 style='color:#4ecca3;'>Update Successful!</h1><p>Device is restarting...</p></body></html>"
        );
        delay(1000);
        ESP.restart();
    }
}

void handleOTAUpload() {
    HTTPUpload& upload = otaServer.upload();
    if (upload.status == UPLOAD_FILE_START) {
        // Validate filename - must contain "client" AND "ltc" (case insensitive)
        String filename = upload.filename;
        filename.toLowerCase();

        bool hasMaster = filename.indexOf("master") != -1;
        bool hasClient = filename.indexOf("client") != -1;
        bool hasLtc = filename.indexOf("ltc") != -1;
        bool hasUtility = filename.indexOf("utility") != -1;

        if (!hasClient || !hasLtc) {
            // Wrong firmware file
            otaFirmwareValid = false;
            if (hasMaster && hasLtc) {
                otaFirmwareError = "Wrong firmware! This is CLIENT LTC but you uploaded MASTER LTC firmware.";
            } else if (hasClient && hasUtility) {
                otaFirmwareError = "Wrong firmware! This is CLIENT LTC but you uploaded CLIENT UTILITY firmware.";
            } else if (hasMaster && hasUtility) {
                otaFirmwareError = "Wrong firmware! This is CLIENT LTC but you uploaded MASTER UTILITY firmware.";
            } else if (!hasLtc && hasUtility) {
                otaFirmwareError = "Wrong firmware! This is LTC device but you uploaded UTILITY firmware.";
            } else if (!hasClient && hasMaster) {
                otaFirmwareError = "Wrong firmware! This is CLIENT device but you uploaded MASTER firmware.";
            } else {
                otaFirmwareError = "Invalid firmware. Filename must contain 'client' and 'ltc'.";
            }
            return;
        }

        otaFirmwareValid = true;
        otaFirmwareError = "";

        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
            Update.printError(Serial);
        }
    } else if (upload.status == UPLOAD_FILE_WRITE) {
        if (!otaFirmwareValid) return;  // Skip writing if firmware invalid
        if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
            Update.printError(Serial);
        }
    } else if (upload.status == UPLOAD_FILE_END) {
        if (!otaFirmwareValid) return;  // Skip if firmware invalid
        if (!Update.end(true)) {
            Update.printError(Serial);
        }
    }
}

void enterOTAMode() {
    if (otaMode) return;

    otaMode = true;
    otaRequestedViaSPI = false;  // Clear the flag
    otaModeStartTime = millis();
    otaSSID = generateOTASSID();

    // Stop LTC generation
    ltcTimecode.running = false;

    // Show OTA mode on display FIRST (before WiFi - matches Master LTC approach)
    if (displayInitialized) {
        display.clearDisplay();
        display.setTextSize(1);
        display.setCursor(0, 0);
        // Line 1: Utility SSID
        display.print("U:");
        if (utilityOTASSID.length() > 0) {
            display.println(utilityOTASSID);
        } else {
            display.println("(waiting...)");
        }
        // Line 2: LTC SSID (this device)
        display.print("L:");
        display.println(otaSSID);
        // Line 3: Password
        display.print("P:");
        display.println(OTA_PASSWORD);
        display.display();
    }

    // Start WiFi AP for OTA with retry logic
    WiFi.mode(WIFI_AP);
    bool apStarted = false;
    for (int retry = 0; retry < 3 && !apStarted; retry++) {
        apStarted = WiFi.softAP(otaSSID.c_str(), OTA_PASSWORD);
        if (!apStarted) {
            delay(500);
        }
    }
    delay(500);

    // Setup OTA web server
    otaServer.on("/", HTTP_GET, handleOTARoot);
    otaServer.on("/update", HTTP_POST, handleOTAUpdate, handleOTAUpload);
    otaServer.begin();
}

void exitOTAMode() {
    if (!otaMode) return;

    otaMode = false;
    otaServer.stop();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_OFF);

    // Restart the device to restore normal operation
    ESP.restart();
}

void checkOTARequest() {
    // Check if OTA was requested via SPI from Utility device
    if (otaRequestedViaSPI && !otaMode) {
        enterOTAMode();
        return;
    }

    // Check for timeout in OTA mode
    if (otaMode) {
        if (millis() - otaModeStartTime > OTA_MODE_TIMEOUT) {
            exitOTAMode();
        }
    }
}

void handleOTAModeInLoop() {
    if (!otaMode) return;

    // Handle OTA web server
    otaServer.handleClient();

    // Update display with countdown (128x32 OLED - 4 lines)
    static unsigned long lastDisplayUpdate = 0;
    if (displayInitialized && millis() - lastDisplayUpdate > 1000) {
        unsigned long remaining = (OTA_MODE_TIMEOUT - (millis() - otaModeStartTime)) / 1000;
        IPAddress ip = WiFi.softAPIP();

        display.clearDisplay();
        display.setTextSize(1);
        display.setCursor(0, 0);
        // Line 1: Utility SSID
        display.print("U:");
        if (utilityOTASSID.length() > 0) {
            display.println(utilityOTASSID);
        } else {
            display.println("(N/A)");
        }
        // Line 2: LTC SSID (this device)
        display.print("L:");
        display.println(otaSSID);
        // Line 3: Password
        display.print("P:");
        display.println(OTA_PASSWORD);
        // Line 4: IP Address and countdown
        display.print("IP:");
        display.print(ip.toString());
        display.print(" ");
        display.print(remaining);
        display.println("s");
        display.display();
        lastDisplayUpdate = millis();
    }
}

// ==================== UTILITY TASK (CORE 0 - LOW PRIORITY) ====================
// Handles all blocking I/O and utility functions
void utilityTask(void* parameter) {

    // Timers for different update rates
    static unsigned long lastBatteryUpdate = 0;
    static unsigned long lastDisplayUpdate = 0;
    static unsigned long lastButtonCheck = 0;

    while (true) {
        unsigned long now = millis();

        // Check OTA request from Utility device (via SPI) and handle OTA mode
        checkOTARequest();
        if (otaMode) {
            handleOTAModeInLoop();
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;  // Skip normal operations while in OTA mode
        }

        // 1. Check buttons frequently for responsiveness (~40Hz / 25ms)
        // LEVEL_BUTTON (GPIO 19): Short press = LINE/MIC toggle, Long press (3s) = Jam Sync toggle
        // FPS_BUTTON (GPIO 26): Short press = FPS cycle, Long press (5s) = Lock/Unlock
        if (now - lastButtonCheck >= 25) {
            checkFpsButton();  // Check FPS button first (handles lock/unlock on long press)
            checkLevelButton();
            lastButtonCheck = now;
        }

        // 2. Update display (~10Hz / 100ms)
        // Skip normal display update when shutdown warning is showing (prevents flickering)
        if (now - lastDisplayUpdate >= 50 && shutdownState != SHUTDOWN_WARNING) {
            updateOLEDDisplay();
            lastDisplayUpdate = now;
        }

        // 3. Read battery level slowly (~0.2Hz / 5000ms)
        if (now - lastBatteryUpdate >= 5000) {
            readBatteryLevel();
            lastBatteryUpdate = now;
        }

        // 4. Handle low battery shutdown (non-blocking, checks every loop)
        handleLowBatteryShutdown();

        // Small delay to maintain cycle time
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// ==================== SETUP ====================
void setupLTC_Client() {
    calculateTiming();

    portENTER_CRITICAL(&ltcTimerMux);
    buildLTCFrame();
    frameBufferValid = true;  // Initial frame is valid
    needNextFrame = true;     // Signal task to pre-build next frame
    portEXIT_CRITICAL(&ltcTimerMux);

    attachLtcClockIsr();
}
// ==================== OTA SAFETY ====================
// Arduino marks a freshly updated firmware "valid" at boot, BEFORE setup() runs, unless the sketch
// defines verifyRollbackLater() returning true. We confirm it ourselves at the END of setup()
// (esp_ota_mark_app_valid_cancel_rollback), so a new firmware that crashes or resets during start-up
// is rolled back to the previous firmware by the bootloader instead of boot-looping (= bricked).
// (Defined here, right before setup(), so it does not move Arduino's auto-prototype insertion point.)
extern "C" bool verifyRollbackLater() { return true; }

void setup() {
    asm volatile("" :: "r"(TCM_FIRMWARE_ID));  // keep the firmware ID tag in the image
    rstLogBoot();   // why did we restart? (counted in NVS, shown on the master page)
    ltcOutLoad();   // LTC output offset (frames), set from the master's page
    setCpuFrequencyMhz(240);  // Full speed - the 48kHz sample ISR needs tight timing headroom
    delay(1000);

    // Pre-initialize WiFi stack (radio stays OFF, no power consumption)
    // This prevents race condition when entering OTA mode later
    WiFi.mode(WIFI_OFF);

    pinMode(OUTPUT_PIN, OUTPUT);
    dacWrite(OUTPUT_PIN, 128);

    preferences.begin("ltc-config", true);
    isLineLevel = preferences.getBool("isLineLevel", true);

    // Load FPS, drop-frame, and sample rate settings
    uint16_t savedFps = preferences.getUShort("fps", 25000);
    bool savedDropFrame = preferences.getBool("dropFrame", false);
    uint32_t savedSampleRate = preferences.getUInt("sampleRate", 48000);
    preferences.end();

    // Apply saved settings
    ltcTimecode.fps_thousandths = savedFps;
    ltcTimecode.dropFrame = savedDropFrame;
    if (savedSampleRate == 47952 || savedSampleRate == 48000) {
        SAMPLE_RATE = savedSampleRate;
    }

    if (isLineLevel) {
        ltcHigh = LINE_LEVEL_HIGH;
        ltcLow = LINE_LEVEL_LOW;
    } else {
        ltcHigh = MIC_LEVEL_HIGH;
        ltcLow = MIC_LEVEL_LOW;
    }
    digitalWrite(LEVEL_INDICATOR_PIN, isLineLevel ? HIGH : LOW);  // Set initial indicator state

    Wire1.begin(5, 4);
    Wire1.setClock(100000);
    Wire1.setTimeOut(50);  // 50ms I2C timeout - prevents blocking on failed OLED
    delay(100);

    if(display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS, false, false)) {
        displayInitialized = true;
        display.clearDisplay();
        display.setTextColor(SSD1306_WHITE);
        display.setTextWrap(false);
        display.display();
    } else {
        displayInitialized = false;  // OLED failed - timecode continues without display
    }

    // Initialize ADS1115 ADC for battery voltage reading (uses Wire1 at default address 0x48)
    if (ads.begin(0x48, &Wire1)) {
        adsInitialized = true;
        ads.setGain(GAIN_ONE);  // +/- 4.096V range, 1 bit = 0.125mV
    } else {
        adsInitialized = false;  // ADS1115 failed - battery reading unavailable
    }

    pinMode(CHARGING_PIN, INPUT);  // VP pin for charging detection
    pinMode(LEVEL_BUTTON_PIN, INPUT_PULLUP);   // GPIO 19: Short=LINE/MIC, Long=JamSync
    pinMode(LEVEL_INDICATOR_PIN, OUTPUT);      // GPIO 21: HIGH=LINE, LOW=MIC indicator
    pinMode(FPS_BUTTON_PIN, INPUT_PULLUP);     // GPIO 26: Short=FPS cycle, Long=Lock/Unlock
    pinMode(JAM_SYNC_INDICATOR_PIN, OUTPUT);   // GPIO 27: HIGH when in JAM_SYNC_SEARCHING mode
    digitalWrite(JAM_SYNC_INDICATOR_PIN, LOW); // Start LOW
    // Note: OTA is initiated only by Utility device via SPI (no button on LTC)

    // Initialize boot time for startup grace period
    bootTime = millis();

    // Force initial charging state detection BEFORE any shutdown logic can run
    isCharging = (digitalRead(CHARGING_PIN) == LOW);
    wasCharging = isCharging;

    // Force initial battery reading (bypass interval check)
    lastBatteryRead = 0;  // Reset to force immediate read
    for (int i = 0; i < 3; i++) {
        // Take multiple readings to get stable initial value
        // Multiply by 2 to get real battery voltage (voltage divider ratio)
        if (adsInitialized) {
            int16_t adc0 = ads.readADC_SingleEnded(0);  // Read A0 channel
            batteryVoltage = adc0 * 0.000125 * 2.0;  // Convert to volts and multiply by 2
        }
        delay(50);  // Brief delay between readings
    }
    // Set initial battery level based on voltage
    float effectiveVoltage = isCharging ? (batteryVoltage - CHARGING_VOLTAGE_OFFSET) : batteryVoltage;
    if (effectiveVoltage >= BATTERY_VOLTAGE_3BAR) batteryLevel = 3;
    else if (effectiveVoltage >= BATTERY_VOLTAGE_2BAR) batteryLevel = 2;
    else if (effectiveVoltage >= BATTERY_VOLTAGE_1BAR) batteryLevel = 1;
    else batteryLevel = 0;
    lastStableBatteryLevel = batteryLevel;
    lastBatteryRead = millis();

    portENTER_CRITICAL(&ltcTimerMux);
    memcpy((void*)&ltcSnapshot, (void*)&ltcTimecode, sizeof(LTC_Timecode));
    portEXIT_CRITICAL(&ltcTimerMux);
    setupLTC_Client();

    // Auto-start timecode on boot (no need to wait for master command)
    portENTER_CRITICAL(&ltcTimerMux);
    ltcTimecode.running = true;
    portEXIT_CRITICAL(&ltcTimerMux);

    portENTER_CRITICAL(&snapshotMux);
    ltcSnapshot.running = true;
    portEXIT_CRITICAL(&snapshotMux);

    setupSPI_Slave();
    jamSyncSemaphore = xSemaphoreCreateBinary();
    // Create Jam Sync Task on Core 0
    xTaskCreatePinnedToCore(
        jamSyncTask,
        "JamSyncTask",
        4096,
        NULL,
        3,
        &JamSyncTaskHandle,
        0
    );

    // Create Frame Build Task on Core 1 (HIGH PRIORITY - pre-builds LTC frames outside ISR)
    xTaskCreatePinnedToCore(
        frameBuildTask,
        "FrameBuildTask",
        2048,
        NULL,
        4,              // Higher priority than JamSync
        &FrameBuildTaskHandle,
        1               // Core 1 - same as LTC timer ISR for cache locality
    );

    // Create SPI Task on Core 0
    xTaskCreatePinnedToCore(
        spiTask,
        "SPITask",
        4096,
        NULL,
        1,
        &SPITaskHandle,
        0
    );

    // Create Utility Task on Core 0
    xTaskCreatePinnedToCore(
        utilityTask,
        "UtilityTask",
        4096,
        NULL,
        1,
        &UtilityTaskHandle,
        0
    );

    // OTA Rollback Protection: Mark firmware as valid after successful startup
    // If device crashes before this point, bootloader will rollback to previous firmware
    esp_ota_mark_app_valid_cancel_rollback();

    vTaskDelete(NULL);
}

void loop() {
    // Unused: setup() deletes the Arduino loop task (vTaskDelete);
    // all work runs in dedicated FreeRTOS tasks.
}
