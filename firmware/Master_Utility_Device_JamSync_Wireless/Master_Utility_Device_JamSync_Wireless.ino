/*
 * =========================================================
 * ==      TIMECODE MASTER - UTILITY DEVICE (MASTER)     ==
 * =========================================================
 * This device handles:
 * - si5351 clock generation (48kHz for LTC Device)
 * - ESP-NOW wireless communication with clients
 * - WiFi Access Point
 * - Web Server & WebSocket interface
 * - RTC (DS3231) timekeeping
 * - SPI Master communication with LTC Device
 *
 * Communicates with LTC Device via SPI:
 * - Sends timecode sync, FPS, running state commands
 * - Receives current timecode status and battery level
 *
 * WIRELESS LOCK MODE:
 * - Disables WiFi AP, ESP-NOW, and WebServer when device enters lock mode
 * - Re-enables all wireless when device exits lock mode
 */

#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <AsyncWebSocket.h>
#include <Wire.h>
#include "esp_core_dump.h"
#include <RTClib.h>
#include <Preferences.h>
#include <cmath>
#include <esp_now.h>
#include <esp_wifi.h>
#include <si5351.h>
#include <esp_timer.h>
#include <SPI.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_random.h>
#include "bootloader_random.h"

// The core defines verifyRollbackLater() as a weak C function; ours must have C linkage to replace it
// (declared here, before any auto-generated prototype). See OTA SAFETY above setup().
extern "C" bool verifyRollbackLater();

// ==================== FIRMWARE IDENTITY ====================
// Identity tag. The master checks it before pushing a firmware to all clients (ClientOTA.ino),
// so a wrong file cannot be installed on every client in one click.
const char TCM_FIRMWARE_ID[] = "TCM-FWID:MASTER-UTILITY";

// ==================== POWER ENABLE PIN ====================

// ==================== OTA UPDATE CONFIGURATION ====================
#define OTA_BUTTON_PIN 33
#define OTA_BUTTON_HOLD_TIME 3000  // Hold button 3 seconds to enter OTA mode
#define OTA_MODE_TIMEOUT 300000     // 5 minutes timeout in OTA mode
#define OTA_PASSWORD "timecodemaster"

// Device type for firmware validation
enum DeviceType : uint8_t {
    DEVICE_MASTER_UTILITY = 1,
    DEVICE_MASTER_LTC = 2,
    DEVICE_CLIENT_UTILITY = 3,
    DEVICE_CLIENT_LTC = 4
};
#define THIS_DEVICE_TYPE DEVICE_MASTER_UTILITY

#define FIRMWARE_VERSION_MAJOR 1
#define FIRMWARE_VERSION_MINOR 3
#define FIRMWARE_VERSION_PATCH 37

// OTA state variables
volatile bool otaMode = false;
unsigned long otaModeStartTime = 0;
unsigned long otaButtonPressStart = 0;
bool otaButtonWasPressed = false;
String otaSSID = "";
String ltcOTASSID = "";              // LTC device's OTA SSID (received via SPI)
size_t otaContentLength = 0;
bool otaStarted = false;
bool otaUpdateSuccess = false;       // Flag to trigger delayed restart
unsigned long otaRestartTime = 0;    // When to restart after successful update
bool otaFirmwareValid = false;       // Firmware filename validation flag
String otaFirmwareError = "";        // Error message for invalid firmware

// ==================== SPI PROTOCOL DEFINITION ====================
#include <stdint.h>

#define SPI_PACKET_SIZE 68  // Must fit SPI_Command struct (66 bytes + padding)

enum SPI_CommandType : uint8_t {
    CMD_POLL_STATUS   = 0,
    CMD_SYNC_TIMECODE = 1,
    CMD_SET_RUNNING   = 2,
    CMD_UPDATE_ESPNOW_STATUS = 3,
    CMD_SET_FPS       = 4,
    CMD_SEND_WIFI_INFO = 5,
    CMD_POLL_STATUS_LIVE = 6,
    CMD_IMMEDIATE_SYNC = 7,  // Immediate atomic sync command
    CMD_LOCK_DEVICE = 8,     // Lock device command
    CMD_FW_BEGIN = 10,       // Master LTC firmware update over SPI (see MasterOTA.ino):
    CMD_FW_DATA = 11,        //   begin (size + MD5) / data (offset, CRC16) / end (commit) / abort.
    CMD_FW_END = 12,         //   Old LTC firmware ignores these; new firmware announces support
    CMD_FW_ABORT = 13,       //   with fwMagic in SPI_Status.
    CMD_ENTER_OTA = 9,       // Enter OTA update mode
    CMD_CLEAR_RSTLOG = 14,   // Clear the reset log (brownout / watchdog / crash counters) of this chip
    CMD_SET_LTC_OFFSET = 15  // LTC output offset in frames (signed, in the "frames" field)
};

#pragma pack(push, 1)
struct SPI_Command {
    uint32_t sampleRate;        // 4 bytes - Sample rate (47952 or 48000)
    uint16_t fps_thousandths;   // 2 bytes
    SPI_CommandType command;    // 1 byte
    uint8_t hours;              // 1 byte
    uint8_t minutes;            // 1 byte
    uint8_t seconds;            // 1 byte
    uint8_t frames;             // 1 byte
    uint8_t dropFrame;          // 1 byte
    uint8_t running;            // 1 byte
    uint8_t espNowConnected;    // 1 byte
    uint8_t syncIntervalMinutes; // 1 byte
    uint8_t numClients;         // 1 byte
    uint8_t syncedClients;      // 1 byte
    char wifiSSID[20];          // 20 bytes
    char wifiPassword[9];       // 9 bytes (8 chars + null)
    char otaSSID[12];           // 12 bytes - OTA SSID (used with CMD_ENTER_OTA)
    uint8_t userBits[8];        // 8 bytes - LTC user bits (date + cam ID)
    uint8_t _padding[2];        // 68 - 66 = 2 bytes
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
    char otaSSID[12];           // 12 bytes - LTC OTA SSID (received when in OTA mode)
    uint32_t fwExpected;        // 4 bytes - firmware update: next byte offset the LTC expects
    uint8_t fwState;            // 1 byte  - 0 idle, 1 receiving, 2 done (rebooting), 3 error
    uint8_t fwError;            // 1 byte  - error code when fwState == 3
    uint8_t fwMagic;            // 1 byte  - 0x5A = this LTC supports firmware update over SPI
    int32_t diagNetLost;        // 4 bytes - clock check: missed (+) / extra (-) sample pulses since boot
    uint16_t diagMaxGapUs;      // 2 bytes - longest gap between two sample interrupts (us)
    uint32_t diagUptimeS;       // 4 bytes - LTC uptime in seconds (for the ppm estimate)
    uint8_t diagMagic;          // 1 byte  - 0xD1 = clock diagnostics present
    uint8_t fwRunId[8];         // 8 bytes - first bytes of the RUNNING firmware's ELF SHA-256 (build fingerprint)
    uint8_t rstReason;          // 1 byte  - why this chip last restarted (esp_reset_reason_t)
    uint8_t rstBrown;           // 1 byte  - brownout resets counted (NVS)
    uint8_t rstWdt;             // 1 byte  - watchdog resets counted
    uint8_t rstPanic;           // 1 byte  - crash (panic) resets counted
};
#pragma pack(pop)

// Compile-time guard: each struct must exactly equal the SPI transfer size.
// Prevents the receive-buffer overflow/overread that occurs if a field is added
// without updating _padding (the bug that left SPI_Status at 64 vs a 68-byte transfer).
static_assert(sizeof(SPI_Command) == SPI_PACKET_SIZE, "SPI_Command size mismatch!");
static_assert(sizeof(SPI_Status)  == SPI_PACKET_SIZE, "SPI_Status size mismatch!");

// ==================== JAM SYNC MODE ENUM ====================
enum JamSyncMode : uint8_t {
    JAM_SYNC_DISABLED = 0,
    JAM_SYNC_SEARCHING = 1,
    JAM_SYNC_LOCKED = 2,
    JAM_SYNC_FREERUN = 3
};

// ==================== SPI CONFIGURATION ====================
#define SPI_MOSI 13
#define SPI_MISO 12
#define SPI_SCLK 14
#define SPI_CS 15
// FRAME_READY_PIN 23 - REMOVED (dead code - signal came after sync, not before)

SPIClass hspi(HSPI);
SPI_Command spiCmd;
SPI_Status spiStatus;
portMUX_TYPE spiMux = portMUX_INITIALIZER_UNLOCKED;

// Serializes construction+transfer of the shared spiCmd buffer across tasks.
// Async web handlers, ESP-NOW callbacks and loop() can all initiate SPI
// commands; without this lock two concurrent fills interleave and the LTC
// device receives a torn packet. (ESP-NOW packets are built in LOCAL
// struct_message copies and need no lock.) RAII guard releases on every
// return path.
// RECURSIVE because guarded functions can nest (e.g. manualSyncAllClients
// holds the lock while sendWebSocketData() -> pollLTCStatus() takes it again).
SemaphoreHandle_t spiCmdLock = NULL;
struct SpiCmdGuard {
    SpiCmdGuard()  { if (spiCmdLock) xSemaphoreTakeRecursive(spiCmdLock, portMAX_DELAY); }
    ~SpiCmdGuard() { if (spiCmdLock) xSemaphoreGiveRecursive(spiCmdLock); }
};

// ==================== GLOBAL OBJECTS ====================
RTC_DS3231 rtc;
Si5351 si5351;
Preferences preferences;
AsyncWebServer server(80);
AsyncWebSocket ws("/ws");

// ==================== TIMING & DEBOUNCE ====================
unsigned long lastCommandTime = 0;
const unsigned long COMMAND_DEBOUNCE = 1000;  // 1 second debounce for all webpage buttons
unsigned long systemStartTime = 0;
unsigned long lastWSUpdate = 0;  // For periodic WebSocket timecode updates
unsigned long lastFullWSUpdate = 0;  // For periodic full WebSocket updates
unsigned long lastRunningStateSetTime = 0;  // Track when we last set running state
unsigned long lastFpsSetTime = 0;  // Track when we last set FPS
unsigned long lastTimecodeSetTime = 0;  // Track when we last set timecode
const unsigned long STATE_PROTECT_MS = 5;  // Window (ms) after a local timecode set during which SPI status may not overwrite it
// Deferred post-FPS-change resync: SET_FPS schedules this instead of blocking the
// async WebSocket task with delay(); loop() runs the resync once the time is reached.
// Both flags are written from the async_tcp task and read by loop() (other
// core), so they must be volatile. Writers must store the TIME before the
// FLAG - loop() tests the flag first, so publishing the flag last guarantees
// it never sees flag=true with a stale time.
volatile bool pendingFpsResync = false;
volatile unsigned long pendingFpsResyncTime = 0;
// Deferred manual sync: async WS/HTTP handlers set this flag instead of calling
// manualSyncAllClients() directly, because that function busy-waits up to one
// frame (~40ms) for the LTC frame boundary and must not stall the async_tcp task.
// loop() executes the sync within ~1ms of the request.
volatile bool pendingManualSync = false;
volatile bool pendingRtcSync = false;   // "Sync from RTC" waits for the RTC second tick (up to 1.1 s): run by loop(), not in the web task

// ==================== SI5351 CONFIGURATION ====================
// RAM cache of the sample rate. The shared Preferences object is NOT
// thread-safe (single NVS handle); runtime readers on the WiFi/async tasks
// must use this cache. NVS is only touched at setup (loadSampleRate) and on
// explicit rate changes (saveSampleRate).
volatile uint32_t currentSampleRate = 48000;
int32_t si5351_correction = 0;
bool si5351_calibrated = false;
bool isCalibrating = false;
volatile uint32_t cal_count = 0;
volatile uint32_t si5351InterruptCount = 0;
float lastMeasuredSi5351Freq = 0.0f;

void IRAM_ATTR cal_isr() { cal_count++; }

// ==================== ESP-NOW CONFIGURATION ====================
typedef struct struct_message {
    char type[8];
    uint8_t hours;
    uint8_t minutes;
    uint8_t seconds;
    uint8_t frames;
    uint16_t fps_thousandths;
    uint8_t dropFrame;
    uint8_t running;
    uint8_t synced;
    uint8_t batteryLevel;
    uint8_t batteryPresent;  // NEW: 0=Missing, 1=Present
    unsigned long timestamp;
    unsigned long masterTimestamp;
    char mac[18];
    unsigned long uptime;
    uint8_t jamSyncMode;  // Client's jam sync mode
    uint32_t sampleRate;  // Sample rate (47952 or 48000)
    // Version info for remote OTA
    uint8_t utilityVersionMajor;
    uint8_t utilityVersionMinor;
    uint8_t utilityVersionPatch;
    uint8_t ltcVersionMajor;
    uint8_t ltcVersionMinor;
    uint8_t ltcVersionPatch;
    uint8_t otaState;  // 0=normal, 1=updating, 2=waiting, 3=error
    char deviceName[3];  // Cam ID (2 chars + null)
    uint8_t userBits[8];  // LTC user bits: [0-5]=date/reel/scene, [6-7]=camId
} struct_message;


struct ESPNowClient {
    uint8_t mac[6];
    unsigned long lastSeen;
    bool registered;
    String macString;
    uint8_t hours;
    uint8_t minutes;
    uint8_t seconds;
    uint8_t frames;
    uint16_t fps_thousandths;
    bool running;
    bool synced;
    bool paired;
    unsigned long uptime;
    uint8_t jamSyncMode;  // Client's jam sync mode
    uint8_t batteryLevel;   // 0-4 bars
    bool batteryPresent;    // True if battery detected
    bool needsInitialSync;  // Flag: client needs sync after reconnect/master boot
    unsigned long lastSyncSent;  // Timestamp of last sync sent to this client
    char deviceName[3];  // Cam ID from client (2 chars + null)
    // Version info from client
    uint8_t utilityVersionMajor;
    uint8_t utilityVersionMinor;
    uint8_t utilityVersionPatch;
    uint8_t ltcVersionMajor;
    uint8_t ltcVersionMinor;
    uint8_t ltcVersionPatch;
    // Clock check of the client's LTC chip, carried in two heartbeat fields older masters never read
    bool diagValid;
    int32_t diagNetLost;
    uint16_t diagMaxGapUs;
    bool rstValid;          // reset log received from this client's Utility (0x15 packet)
    bool rstLValid;         // ... and from its LTC chip
    bool radioValid;        // the client reported its TX power / ESP-NOW rate (0x15 packet, 13 bytes)
    uint8_t radioTx, radioRate;
    int8_t rssi;            // signal strength of this client at the master (dBm, smoothed)
    uint8_t radioHw;        // what the client's radio reports as its TX power (0.25 dBm units)
    int8_t rssiFromMaster;  // how well the client hears the master (dBm; 0 = unknown), reported by the client
    uint8_t radioPs;        // the client's power-save setting (0 / 1), reported by the client
    bool radioPsValid;
    unsigned long awakeAt;  // when the client last announced "my radio is on" (power save), 0 = never
    int8_t ltcOfs;          // the client's LTC output offset (frames), reported by the client
    bool ltcOfsValid;
    unsigned long rssiAt;   // millis() of the last packet it was measured on (0 = never)
    uint8_t rstU[4];        // Utility: last reset reason, brownouts, watchdog resets, crashes
    uint8_t rstL[4];        // LTC chip: same
    bool diagSinceValid;
    uint32_t diagSinceS;    // seconds since the client LTC's clock check was (re)started
    uint8_t diagErrFirstS;  // first / last second (255 = none) and count of windows with wrong pulse counts
    uint8_t diagErrLastS;
    uint8_t diagErrWins;
};

const int MAX_ESPNOW_CLIENTS = 10;
ESPNowClient espNowClients[MAX_ESPNOW_CLIENTS];
int espNowClientCount = 0;

unsigned long lastESPNowSend = 0;
const unsigned long ESPNOW_SEND_INTERVAL = 1000;  // Changed from 100ms to 1 second

// ESP-NOW Broadcast address (FF:FF:FF:FF:FF:FF)
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ==================== PAIRING CONFIGURATION ====================
struct UnpairedClient {
    uint8_t mac[6];
    String macString;
    unsigned long firstSeen;
    unsigned long lastSeen;
    bool active;
};

const int MAX_UNPAIRED_CLIENTS = 5;
UnpairedClient unpairedClients[MAX_UNPAIRED_CLIENTS];
int unpairedClientCount = 0;

bool pairingMode = false;
unsigned long pairingModeStartTime = 0;
const unsigned long PAIRING_MODE_DURATION = 60000;

// ==================== TIMECODE STRUCTURE ====================
struct Timecode {
  uint16_t fps_thousandths;
  uint8_t hours;
  uint8_t minutes;
  uint8_t seconds;
  uint8_t frames;
  bool dropFrame;
  bool running;
};

Timecode currentTimecode = {25000, 0, 0, 0, 0, false, true};

// Sub-frame position from last live SPI poll (for precise sync timing)
uint8_t lastBitIndex = 0;       // LTC bit position (0-79)
uint8_t lastBitSamplePos = 0;   // Sample position within bit (0-24 typically)

// ==================== NETWORK CONFIGURATION ====================
String ssidString = "";
String generatedPassword = "";

unsigned long syncIntervalMinutes = 0;
unsigned long lastAutoSyncTime = 0;
const unsigned long SYNC_CHECK_INTERVAL = 60000;

const int MAX_WEBSOCKET_CLIENTS = 5;

// ==================== RTC CACHE ====================
String cachedRTCString = "2025-01-01 00:00:00";
unsigned long lastRTCUpdate = 0;
const unsigned long RTC_UPDATE_INTERVAL = 1000;
// Date cache for buildUserBits: rtc.now() is TWO I2C transactions and is NOT
// safe to run concurrently from the WiFi task and loop() (register-pointer
// race yields garbage dates). Only loop-task updateRTCache()/setupRTC()
// touch the RTC; every other task reads these cached values.
volatile uint8_t cachedRtcMonth = 1;
volatile uint8_t cachedRtcDay = 1;
volatile uint8_t cachedRtcYear = 25;  // 2-digit year

// The RTC (DS3231) and the Si5351 share one I2C bus. A transaction is several steps, so two tasks using the bus at
// the same time (web server, loop, utility task) corrupt each other: a garbage time read, or a write to the wrong
// place. Every access takes this lock first. Never take it inside an interrupt.
SemaphoreHandle_t i2cMutex = nullptr;
struct I2cGuard {
    I2cGuard()  { if (i2cMutex) xSemaphoreTakeRecursive(i2cMutex, portMAX_DELAY); }
    ~I2cGuard() { if (i2cMutex) xSemaphoreGiveRecursive(i2cMutex); }
};
// 0 = fine, 1 = the RTC lost its battery and the time was never set again, 2 = RTC chip not found
volatile uint8_t rtcWarn = 0;
static DateTime rtcNow() { I2cGuard g; return rtc.now(); }
static bool rtcSetLoad() {   // false when the RTC lost power and nobody has set the time since (survives a reboot)
    Preferences rp;
    bool v = true;
    if (rp.begin("rtc", true)) { v = rp.getUChar("set", 1) != 0; rp.end(); }
    return v;
}
static void rtcSetSave(bool v) {
    Preferences rp;
    if (rp.begin("rtc", false)) { rp.putUChar("set", v ? 1 : 0); rp.end(); }
}
static bool rtcValid(int y, int mo, int d, int h, int mi, int sec) {
    if (y < 2020 || y > 2099 || mo < 1 || mo > 12 || h < 0 || h > 23 || mi < 0 || mi > 59 || sec < 0 || sec > 59) return false;
    static const uint8_t dm[12] = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };
    int md = dm[mo - 1];
    if (mo == 2 && ((y % 4 == 0 && y % 100 != 0) || y % 400 == 0)) md = 29;
    return d >= 1 && d <= md;
}

// ==================== USER BITS ====================
// Fixed layout: [MMDDYY][CC] = 8 nibbles
// - MMDDYY: Month, Day, Year (from RTC, rollover handled by LTC device)
// - CC: Camera ID (2 chars, alphanumeric)
char masterCamId[3] = "00";

// ==================== HARDWARE PINS ====================
const int SYNC_BUTTON_PIN = 26;
const int STATUS_LED_PIN = 2;

// ==================== BATTERY, LEVEL & JAM SYNC INFO FROM SPI ====================
uint8_t ltcBatteryLevel = 0;
bool ltcBatteryPresent = true;  // NEW: From SPI status
bool ltcIsLineLevel = true;
// Master LTC firmware version + whether it can be updated over SPI (from its SPI status)
uint8_t ltcVerMajor = 0, ltcVerMinor = 0, ltcVerPatch = 0;
bool ltcFwSupported = false;
// Clock check of the master LTC (missed/extra sample pulses, see the LTC's clockDiagTick)
bool ltcDiagValid = false;
int32_t ltcDiagNetLost = 0;
uint16_t ltcDiagMaxGapUs = 0;
uint32_t ltcDiagUptimeS = 0;
uint8_t ltcRst[4] = {0, 0, 0, 0};   // the master LTC chip's reset reason / brownout / watchdog / crash counts
bool ltcRstValid = false;
// ==================== WIFI RADIO SETTINGS (set on the master's firmware page) ====================
// The ESP-NOW data rate decides how long the radio's current bursts are; it is kept in NVS and the master pushes it to
// every client. TX power is fixed at 11 dBm (index 3): measured with the radio test, changing it did not change the
// received signal, so the selector was removed. The index stays in the packets and the table for compatibility.
static const wifi_power_t RADIO_TX_TABLE[8] = { WIFI_POWER_5dBm, WIFI_POWER_7dBm, WIFI_POWER_8_5dBm, WIFI_POWER_11dBm,
                                                WIFI_POWER_13dBm, WIFI_POWER_15dBm, WIFI_POWER_17dBm, WIFI_POWER_19_5dBm };
uint8_t radioTxIdx = 3;     // index into the table: 3 = 11 dBm (the previous fixed value)
uint8_t radioRateIdx = 1;   // ESP-NOW rate: 0 = 1 Mbps (longest range), 1 = 6 Mbps (6x shorter airtime, lower current)
// Radio power save (optional, default OFF): the master sends a short beacon every 2 s; clients sleep their radio between
// the beacons and listen for ~160 ms after each one. Commands for a client wait for its next window (see ClientOTA.ino).
bool psOn = false;                              // chosen on the main page, kept in NVS
unsigned long psOffUntil = 0;                   // beacons go on for a few seconds after power save is switched off
volatile uint32_t psBeaconCount = 0;            // beacons sent so far
volatile unsigned long psLastBeaconMs = 0;      // when the last one went out
static bool psActive() { return psOn || (long)(psOffUntil - millis()) > 0; }
// LTC output offset of the MASTER's own LTC (frames, + = the timecode it sends is ahead); clients keep their own.
int8_t masterLtcOffset = 0;
static void ltcOfsLoad() {
    Preferences rp;
    if (rp.begin("ltcout", true)) {
        int v = rp.getChar("ofs", 0);
        rp.end();
        if (v >= -30 && v <= 30) masterLtcOffset = (int8_t)v;
    }
}
static void ltcOfsSave() {
    Preferences rp;
    if (rp.begin("ltcout", false)) { rp.putChar("ofs", masterLtcOffset); rp.end(); }
}
bool radioApplyPending = false;   // master: a new setting was sent to the clients and is about to be applied here
static void radioLoad() {
    Preferences rp;
    if (rp.begin("radio", true)) {
        radioTxIdx = 3;   // TX power is fixed at 11 dBm: the setting had no measurable effect on the ESP-NOW signal
        radioRateIdx = rp.getUChar("rate", 1);
        psOn = rp.getUChar("ps", 0) != 0;
        rp.end();
    }
    if (radioTxIdx > 7) radioTxIdx = 3;
    if (radioRateIdx > 1) radioRateIdx = 1;
}
static void radioSave() {
    Preferences rp;
    if (rp.begin("radio", false)) {
        rp.putUChar("tx", radioTxIdx);
        rp.putUChar("rate", radioRateIdx);
        rp.putUChar("ps", psOn ? 1 : 0);
        rp.end();
    }
}
static void radioApplyTx() { WiFi.setTxPower(RADIO_TX_TABLE[radioTxIdx]); }
static void radioApplyRateTo(uint8_t idx) {   // needs ESP-NOW to be initialised; harmless (returns an error) otherwise
    wifi_phy_rate_t r = idx ? WIFI_PHY_RATE_6M : WIFI_PHY_RATE_1M_L;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    esp_wifi_config_espnow_rate(WIFI_IF_STA, r);
    esp_wifi_config_espnow_rate(WIFI_IF_AP, r);
#pragma GCC diagnostic pop
}
static void radioApplyRate() { radioApplyRateTo(radioRateIdx); }
static void radioApplyAll() { radioApplyTx(); radioApplyRate(); }
// What the radio itself reports as its current maximum TX power (0.25 dBm units; 20 = 5 dBm ... 78 = 19.5 dBm).
// Proves the setting reached the radio and was not reset by a WiFi restart.
static uint8_t radioHwNow() {
    int8_t q = 0;
    esp_wifi_get_max_tx_power(&q);
    return (uint8_t)q;
}
// ==================== RESET LOG: why did this chip last restart? ====================
// A brownout (supply dip), watchdog reset or crash is counted in NVS (kept across restarts) and
// reported to the master's web page, so power problems are visible without a serial monitor.
uint8_t rstLowBatt = 0;   // how many times this unit switched itself off because the battery was low (kept in NVS)
uint8_t rstSelf[4] = {0, 0, 0, 0};   // [0] last reset reason (esp_reset_reason_t), [1] brownouts, [2] watchdog, [3] crashes
static void rstLogBoot() {
    esp_reset_reason_t r = esp_reset_reason();
    rstSelf[0] = (uint8_t)r;
    Preferences rp;
    if (!rp.begin("rstlog", false)) return;
    rstSelf[1] = rp.getUChar("brown", 0);
    rstSelf[2] = rp.getUChar("wdt", 0);
    rstSelf[3] = rp.getUChar("panic", 0);
    rstLowBatt = rp.getUChar("lowb", 0);
    if (r == ESP_RST_BROWNOUT) { if (rstSelf[1] < 255) rstSelf[1]++; rp.putUChar("brown", rstSelf[1]); }
    else if (r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT) { if (rstSelf[2] < 255) rstSelf[2]++; rp.putUChar("wdt", rstSelf[2]); }
    else if (r == ESP_RST_PANIC) { if (rstSelf[3] < 255) rstSelf[3]++; rp.putUChar("panic", rstSelf[3]); }
    rp.end();
}
// "Clear reset log" from the master's page: zero the counters (the last reset reason stays)
// ==================== LAST CRASH (core dump summary) ====================
// On a crash the ESP32 stores a core dump in its own flash partition; it survives restarts and firmware updates. Its summary
// (crashing task, program counter, backtrace) is read at start-up and shown on the firmware page, so the crash can be traced
// to a line of code (with the ELF file of that build) without a serial monitor. "Clear reset log" erases it.
char crashTxt[320] = "";
static void crashInfoLoad() {
    crashTxt[0] = 0;
    size_t addr = 0, sz = 0;
    if (esp_core_dump_image_get(&addr, &sz) != ESP_OK) return;   // no crash stored
    esp_core_dump_summary_t* s = (esp_core_dump_summary_t*)calloc(1, sizeof(esp_core_dump_summary_t));
    if (!s) return;
    if (esp_core_dump_get_summary(s) == ESP_OK) {
        char task[17];
        memcpy(task, s->exc_task, 16);
        task[16] = 0;
        for (char* p = task; *p; p++) if (*p < 32 || *p > 126 || *p == '"' || *p == '\\' || *p == '<' || *p == '>') *p = '?';
        char build[9];
        int b = 0;
        for (; b < 8 && b < (int)sizeof(s->app_elf_sha256) && isxdigit(s->app_elf_sha256[b]); b++) build[b] = (char)s->app_elf_sha256[b];
        build[b] = 0;
        int n = snprintf(crashTxt, sizeof(crashTxt), "task %s, cause %lu, addr 0x%08lx, pc 0x%08lx, build %s, backtrace",
                         task, (unsigned long)s->ex_info.exc_cause, (unsigned long)s->ex_info.exc_vaddr, (unsigned long)s->exc_pc, build);
        for (uint32_t i = 0; i < s->exc_bt_info.depth && i < 16 && n > 0 && n < (int)sizeof(crashTxt) - 14; i++)
            n += snprintf(crashTxt + n, sizeof(crashTxt) - n, " 0x%08lx", (unsigned long)s->exc_bt_info.bt[i]);
        if (s->exc_bt_info.corrupted && n > 0 && n < (int)sizeof(crashTxt) - 14) snprintf(crashTxt + n, sizeof(crashTxt) - n, " (corrupted)");
    }
    free(s);
}

// The low-battery shutdown leaves no trace otherwise: the unit just goes dark until power is cycled
static void rstLogLowBatt() {
    Preferences rp;
    if (rp.begin("rstlog", false)) {
        uint8_t n = rp.getUChar("lowb", 0);
        if (n < 255) n++;
        rp.putUChar("lowb", n);
        rp.end();
    }
}
static void rstLogClear() {
    rstSelf[1] = rstSelf[2] = rstSelf[3] = 0;
    rstLowBatt = 0;
    if (crashTxt[0]) { esp_core_dump_image_erase(); crashTxt[0] = 0; }   // the stored crash goes too
    Preferences rp;
    if (rp.begin("rstlog", false)) {
        rp.putUChar("lowb", 0);
        rp.putUChar("brown", 0);
        rp.putUChar("wdt", 0);
        rp.putUChar("panic", 0);
        rp.end();
    }
}
// A user-triggered sync (Sync button on the page / hardware button, set timecode, rate change)
// also restarts the clock check on every unit. Automatic interval syncs do not.
volatile bool diagResetReq = false;
#define DIAG_RESET_SYNC_MARK 0xC1EA2000UL   // in struct_message.uptime of a SYNC (unused there)
// Master LTC clock events (from the otaSSID field outside OTA mode), newest kept
uint8_t mltcEvSeq[16];
uint32_t mltcEvMs[16];
int16_t mltcEvLost[16];
uint16_t mltcEvGap[16];
uint8_t mltcEvCount = 0;
void mltcEventAdd(uint8_t seq, uint32_t ms, int16_t lost, uint16_t gap) {
    for (uint8_t i = 0; i < mltcEvCount; i++)
        if (mltcEvSeq[i] == seq && mltcEvMs[i] == ms) return;   // already have it
    uint8_t slot = mltcEvCount < 16 ? mltcEvCount++ : 0;
    if (mltcEvCount == 16 && slot == 0) {                        // full: replace the oldest
        uint32_t oldest = mltcEvMs[0];
        for (uint8_t i = 1; i < 16; i++) if (mltcEvMs[i] < oldest) { oldest = mltcEvMs[i]; slot = i; }
    }
    mltcEvSeq[slot] = seq; mltcEvMs[slot] = ms; mltcEvLost[slot] = lost; mltcEvGap[slot] = gap;
}
uint8_t ltcJamSyncMode = JAM_SYNC_DISABLED;
bool ltcSystemLocked = false;
bool previousLockState = false;  // Track previous lock state for change detection

// Frame timecode for sync operations
Timecode lastFrameTimecode = {25000, 0, 0, 0, 0, false, true};

// ==================== WIRELESS MANAGER STATE ====================
bool wirelessEnabled = true;
bool wirelessInitialized = false;

// Deferred wireless disable state machine
enum WirelessState {
    WIRELESS_ACTIVE,
    WIRELESS_PENDING_DISABLE,
    WIRELESS_DISABLED,
    WIRELESS_PENDING_ENABLE,
    WIRELESS_PENDING_POST_ENABLE_SYNC  // New state for post-enable sync
};
volatile WirelessState wirelessState = WIRELESS_ACTIVE;
unsigned long wirelessStateChangeTime = 0;
const unsigned long WIRELESS_DISABLE_DELAY_MS = 500;  // 500ms delay to avoid Si5351 glitch
const unsigned long WIRELESS_POST_ENABLE_SYNC_DELAY_MS = 500;  // Wait for WiFi to stabilize before sync

// ==================== FUNCTION PROTOTYPES ====================
// FPS Helper Functions
uint8_t getNominalFps(uint16_t fps_thousandths);
float getFpsFloat(uint16_t fps_thousandths);
uint16_t fpsFloatToThousandths(float fps);
uint32_t getSampleRateForFps(uint16_t fps_thousandths);

// SPI Functions
void setupSPI_Master();
void performSPITransaction();
void pollLTCStatus();
void sendSyncToLTCDevice();
void sendRunningStateToLTCDevice(bool running);
void sendFPSChangeToLTCDevice();
void sendEspNowStatusToLTCDevice();
void sendEspNowStatusToLTCDeviceForced();
void sendEspNowStatusToLTCDeviceInternal(bool force);
void sendWiFiInfoToLTCDevice();
void IRAM_ATTR onFrameReady();  // ISR for frame-ready interrupt

// ESP-NOW Client Management
int findOrAddESPNowClient(const uint8_t *mac);
void updateESPNowClientStatus(const uint8_t *mac, struct_message data);
void cleanupStaleESPNowClients();

// Pairing Functions
void startPairingMode();
void stopPairingMode();
void addUnpairedClient(const uint8_t *mac);
void removeUnpairedClient(const uint8_t *mac);
void pairClient(const uint8_t *mac);
void unpairClient(const uint8_t *mac);
void sendPairingConfirmation(const uint8_t *mac);
void sendUnpairCommand(const uint8_t *mac);
void loadPairedClients();
void savePairedClients();
void sendWebSocketPairingUpdate();
void checkPairingModeTimeout();

// RTC Management
void setupRTC();
void syncFromRTC(bool isManualSync = true);
bool setRTC(int year, int month, int day, int hour, int minute, int second);
void updateRTCache();
String getRTCString();

// Network
void setupWiFi();
String generateWiFiPassword();
void loadOrGeneratePassword();

// WebSocket
void onWebSocketEvent(AsyncWebSocket *server, AsyncWebSocketClient *client, AwsEventType type, void *arg, uint8_t *data, size_t len);
void handleWebSocketMessage(String message, AsyncWebSocketClient *client);
void sendWebSocketData();
void sendWebSocketTimecodeUpdate();
void sendWebSocketRTCUpdate();
void sendWebSocketClientsUpdate();
void sendWebSocketClientsDetailed();
void setupWebSocket();

// Web Server
void setupWebServer();

// Settings
void saveSettings();
void saveSyncSettings();
void loadSyncSettings();
void loadSettings();

// Broadcast and Sync
void manualSyncAllClients();
void broadcastRunningState();
void broadcastLockCommand();
void lockMasterLTCDevice();
void checkAutoSync();
void checkSyncButton();
uint32_t waitForFrameBoundary(uint16_t fps_thousandths);

// ESP-NOW Functions
void setupESP_NOW();
void processESPNowPacket(const uint8_t *mac, struct_message data);
void sendESPNowSync(const uint8_t *mac, int maxRetries = 3);
void sendESPNowRunState();
void broadcastSyncESPNow(bool isManualSync = false);
void sendPeriodicAlive();
void registerESPNowCallbacks();

// One-click client OTA (ClientOTA.ino)
void clientOta_onEspNow(const uint8_t* mac, const uint8_t* data, int len);
void clientOta_loop();
void espSendClient(const uint8_t* mac, const uint8_t* d, size_t n);   // command for a client: waits for its window when power save is on
void psGate(uint32_t maxAgeMs);                                       // wait for the start of a client window (power save)
void clientOta_setupRoutes();
// Master Utility + Master LTC update together (MasterOTA.ino)
bool mfwBusy();
void mfwLoop();
void mfwSetupRoutes();
void mfwBegin();
String mfwStatusJsonPart();

// Wireless Management Functions
void disableWireless();
void enableWireless();
void handleLockStateChange(bool isLocked);

// Si5351 Functions
void setupSi5351();
void loadCalibration();
void saveCalibration();

// User Bits Functions (Fixed layout: MMDDYY + CamID)
uint8_t charToNibble(char c);
char nibbleToChar(uint8_t n);
void buildUserBits(uint8_t* userBits, const char* camId);
void loadMasterCamId();
void saveMasterCamId(const char* camId);

// Utility Functions
String formatUptime(unsigned long seconds);

// OTA Update Functions
void checkOTAButton();
void enterOTAMode();
void exitOTAMode();
void setupOTAWebServer();
void sendOTACommandToLTC();
String getDeviceTypeString(uint8_t type);
String generateOTASSID();

// ==================== UTILITY FUNCTIONS ====================
String formatUptime(unsigned long seconds) {
    unsigned long days = seconds / 86400;
    seconds %= 86400;
    unsigned long hours = seconds / 3600;
    seconds %= 3600;
    unsigned long minutes = seconds / 60;
    seconds %= 60;

    String result = "";
    if (days > 0) {
        result += String(days) + "d ";
    }
    if (hours > 0 || days > 0) {
        result += String(hours) + "h ";
    }
    if (minutes > 0 || hours > 0 || days > 0) {
        result += String(minutes) + "m ";
    }
    result += String(seconds) + "s";

    return result;
}

// ==================== WIRELESS MANAGEMENT FUNCTIONS ====================
void disableWireless() {
    if (!wirelessEnabled) {
        return; // Already disabled
    }


    // Deinitialize ESP-NOW first
    esp_now_deinit();

    // Disable WiFi AP
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_OFF);

    // Stop WiFi modem
    esp_wifi_stop();

    wirelessEnabled = false;
}

void enableWireless() {
    if (wirelessEnabled) {
        return; // Already enabled
    }

    if (!wirelessInitialized) {
        return;
    }


    // Re-enable WiFi in AP+STA mode
    WiFi.mode(WIFI_AP_STA);

    // Start WiFi modem
    esp_wifi_start();

    // Re-create WiFi AP
    WiFi.softAP(ssidString.c_str(), generatedPassword.c_str(), 1, 0, 8);   // ch 1 (default), up to 8 stations
    IPAddress IP = WiFi.softAPIP();

    // Re-initialize ESP-NOW
    esp_err_t result = esp_now_init();
    if (result == ESP_OK) {
        wirelessEnabled = true;
        // TX power / ESP-NOW rate from the firmware page (default 11 dBm / 6 Mbps)
        radioApplyAll();
    } else {
        return;
    }

    // Re-register ESP-NOW callbacks
    registerESPNowCallbacks();

    // Re-add broadcast peer
    esp_now_peer_info_t broadcastPeerInfo = {};
    memcpy(broadcastPeerInfo.peer_addr, broadcastAddress, 6);
    broadcastPeerInfo.channel = 0;
    broadcastPeerInfo.encrypt = false;

    esp_now_add_peer(&broadcastPeerInfo);

    // Re-add all paired clients (for individual messages, not sync)
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            esp_now_peer_info_t peerInfo = {};
            memcpy(peerInfo.peer_addr, espNowClients[i].mac, 6);
            peerInfo.channel = 0;
            peerInfo.encrypt = false;

            esp_now_del_peer(espNowClients[i].mac); // Remove if exists
            esp_now_add_peer(&peerInfo);
        }
    }

    // Schedule post-enable sync (non-blocking)
    wirelessState = WIRELESS_PENDING_POST_ENABLE_SYNC;
    wirelessStateChangeTime = millis() + WIRELESS_POST_ENABLE_SYNC_DELAY_MS;
    Serial.printf("Post-enable sync scheduled in %dms...\n", WIRELESS_POST_ENABLE_SYNC_DELAY_MS);
}

// Signal strength of each client as the master hears it (dBm, smoothed over the last few packets; shown on the main page)
static void rssiNote(const uint8_t* mac, int rssi) {
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) != 0) continue;
        ESPNowClient& c = espNowClients[i];
        unsigned long now = millis();
        c.rssi = (c.rssiAt && now - c.rssiAt < 30000) ? (int8_t)((c.rssi + rssi) / 2) : (int8_t)rssi;
        c.rssiAt = now;
        return;
    }
}

// ---- Radio test (see ClientOTA.ino): what the master hears from each client's probe frames, per tag = rate*8 + level,
// and (reported by the client) what each client heard from the master's probes.
static int16_t probeSum[MAX_ESPNOW_CLIENTS][16];
static uint8_t probeCnt[MAX_ESPNOW_CLIENTS][16];
static int8_t  probeM2C[MAX_ESPNOW_CLIENTS][16];
static uint8_t probeM2Cn[MAX_ESPNOW_CLIENTS][16];
static bool    probeReplied[MAX_ESPNOW_CLIENTS];
static void probeFromClientNote(const uint8_t* mac, uint8_t tag, int rssi) {
    if (tag >= 16) return;
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) != 0) continue;
        probeSum[i][tag] += rssi;
        if (probeCnt[i][tag] < 255) probeCnt[i][tag]++;
        return;
    }
}
static void probeReplyFromClient(const uint8_t* mac, const uint8_t* d, int len) {
    if (len < 34) return;
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) != 0) continue;
        for (int t = 0; t < 16; t++) { probeM2C[i][t] = (int8_t)d[2 + t]; probeM2Cn[i][t] = d[18 + t]; }
        probeReplied[i] = true;
        return;
    }
}

// Reset log from a client (first byte 0x15): [1]=1 version, [2..5] Utility r/brown/wdt/panic, [6..9] LTC, [10] LTC valid
void rstInfoFromClient(const uint8_t* mac, const uint8_t* d, int len) {
    if (len < 11 || d[1] != 1) return;
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) != 0) continue;
        memcpy(espNowClients[i].rstU, d + 2, 4);
        memcpy(espNowClients[i].rstL, d + 6, 4);
        espNowClients[i].rstLValid = d[10] != 0;
        espNowClients[i].rstValid = true;
        if (len >= 13) { espNowClients[i].radioTx = d[11]; espNowClients[i].radioRate = d[12]; espNowClients[i].radioValid = true; }
        if (len >= 15) { espNowClients[i].radioHw = d[13]; espNowClients[i].rssiFromMaster = (int8_t)d[14]; }
        if (len >= 16) { espNowClients[i].radioPs = d[15] ? 1 : 0; espNowClients[i].radioPsValid = true; }
        if (len >= 17) { espNowClients[i].ltcOfs = (int8_t)d[16]; espNowClients[i].ltcOfsValid = true; }
        break;
    }
}

void registerESPNowCallbacks() {
    esp_now_register_send_cb([](const wifi_tx_info_t *tx_info, esp_now_send_status_t status) {
    });

    esp_now_register_recv_cb([](const esp_now_recv_info *recv_info, const uint8_t *incomingData, int len) {
        if (len >= 4 && incomingData[0] == 0x18 && incomingData[1] == 1 && recv_info && recv_info->rx_ctrl) {   // radio test probe (kept out of the normal signal reading)
            probeFromClientNote(recv_info->src_addr, incomingData[2], recv_info->rx_ctrl->rssi);
            return;
        }
        if (len >= 34 && incomingData[0] == 0x1C && incomingData[1] == 1) {   // a client's table of what it heard
            probeReplyFromClient(recv_info->src_addr, incomingData, len);
            return;
        }
        if (recv_info && recv_info->rx_ctrl && recv_info->src_addr) rssiNote(recv_info->src_addr, recv_info->rx_ctrl->rssi);   // signal strength of this client
        // Client-OTA status packets (ACK/PROGRESS/DONE/ERROR, first byte 0x11..0x14; see ClientOTA.ino).
        // struct_message starts with an ASCII type string, so these can never collide.
        if (len >= 1 && incomingData[0] == 0x15) {   // reset log of a client
            rstInfoFromClient(recv_info->src_addr, incomingData, len);
            return;
        }
        if (len >= 1 && incomingData[0] >= 0x11 && incomingData[0] <= 0x14) {
            clientOta_onEspNow(recv_info->src_addr, incomingData, len);
            return;
        }
        if (len == sizeof(struct_message)) {
            struct_message receivedData;
            memcpy(&receivedData, incomingData, sizeof(receivedData));
            processESPNowPacket(recv_info->src_addr, receivedData);
        }
    });
}

void handleLockStateChange(bool isLocked) {
    if (isLocked) {
        // Schedule wireless disable for later (avoid Si5351 glitch)
        // With radio power save the clients only hear the lock command in their next two windows (~5 s): keep the radio on until then
        wirelessState = WIRELESS_PENDING_DISABLE;
        wirelessStateChangeTime = millis() + (psActive() ? 5500UL : WIRELESS_DISABLE_DELAY_MS);
        Serial.printf("Wireless disable scheduled in %dms...\n", (int)(psActive() ? 5500UL : WIRELESS_DISABLE_DELAY_MS));
    } else {
        // Schedule wireless enable for later
        wirelessState = WIRELESS_PENDING_ENABLE;
        wirelessStateChangeTime = millis() + WIRELESS_DISABLE_DELAY_MS;
        Serial.printf("Wireless enable scheduled in %dms...\n", WIRELESS_DISABLE_DELAY_MS);
    }
}

// Wi-Fi password: 8 random characters from the hardware RNG. It used to be the 4 hex digits of the (public) SSID
// plus 4 characters from random() after randomSeed(micros()) - and in this core randomSeed() switches random()
// to a software PRNG seeded by a boot-time counter, so the secret part had ~20 bits (cracked offline in seconds).
// No 0/O/1/l/I: the password is read off the OLED.
String generateWiFiPassword() {
  const char charset[] = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const uint32_t n = sizeof(charset) - 1;
  const uint32_t limit = (0xFFFFFFFFu / n) * n;   // no modulo bias
  bootloader_random_enable();                      // real entropy for esp_random() while the radio is still off
  String password;
  while (password.length() < 8) {
    uint32_t r = esp_random();
    if (r < limit) password += charset[r % n];
  }
  bootloader_random_disable();
  return password;
}

void loadOrGeneratePassword() {
  preferences.begin("wifi_config", false);

  generatedPassword = preferences.getString("wifi_password", "");

  // pw_ver 2 = made by the hardware-RNG generator. A password stored by an older firmware (weak, see above) is
  // replaced ONCE - phones have to join again with the new password shown on the OLED.
  if (generatedPassword.length() == 0 || preferences.getUChar("pw_ver", 0) < 2) {
    generatedPassword = generateWiFiPassword();
    preferences.putString("wifi_password", generatedPassword);
    preferences.putUChar("pw_ver", 2);
  }

  preferences.end();
}

// ==================== SPI FUNCTIONS ====================
void setupSPI_Master() {
    hspi.begin(SPI_SCLK, SPI_MISO, SPI_MOSI, SPI_CS);
    hspi.setDataMode(SPI_MODE1);
    pinMode(SPI_CS, OUTPUT);
    digitalWrite(SPI_CS, HIGH);
}

void performSPITransaction() {
    portENTER_CRITICAL(&spiMux);

    memset(&spiStatus, 0, sizeof(spiStatus));

    digitalWrite(SPI_CS, LOW);
    delayMicroseconds(500);
    hspi.transferBytes((uint8_t*)&spiCmd, (uint8_t*)&spiStatus, SPI_PACKET_SIZE);
    digitalWrite(SPI_CS, HIGH);

    portEXIT_CRITICAL(&spiMux);

    // Update local timecode from SPI status
    if (spiStatus.fps_thousandths >= 23000 && spiStatus.fps_thousandths <= 30000) {
        // DON'T overwrite timecode if we recently set it explicitly
        // The LTC device response may be stale (hasn't processed command yet)
        unsigned long timeSinceTimecodeSet = millis() - lastTimecodeSetTime;
        if (timeSinceTimecodeSet > STATE_PROTECT_MS) {
            currentTimecode.hours = spiStatus.hours;
            currentTimecode.minutes = spiStatus.minutes;
            currentTimecode.seconds = spiStatus.seconds;
            currentTimecode.frames = spiStatus.frames;
        }

        si5351InterruptCount = spiStatus.si5351InterruptCount;
        ltcBatteryLevel = spiStatus.batteryLevel;
        ltcBatteryPresent = (spiStatus.batteryPresent == 1);
        ltcIsLineLevel = (spiStatus.isLineLevel == 1);
        ltcJamSyncMode = spiStatus.jamSyncMode;
        ltcVerMajor = spiStatus.firmwareVersionMajor;
        ltcVerMinor = spiStatus.firmwareVersionMinor;
        ltcVerPatch = spiStatus.firmwareVersionPatch;
        ltcFwSupported = (spiStatus.fwMagic == 0x5A);
        ltcDiagValid = (spiStatus.diagMagic == 0xD1);
        ltcRst[0] = spiStatus.rstReason; ltcRst[1] = spiStatus.rstBrown; ltcRst[2] = spiStatus.rstWdt; ltcRst[3] = spiStatus.rstPanic;
        ltcRstValid = (spiStatus.diagMagic == 0xD1 && spiStatus.rstReason != 0);
        ltcDiagNetLost = spiStatus.diagNetLost;
        ltcDiagMaxGapUs = spiStatus.diagMaxGapUs;
        if (spiStatus.diagUptimeS + 2 < ltcDiagUptimeS) mltcEvCount = 0;   // LTC restarted: old event times are void
        ltcDiagUptimeS = spiStatus.diagUptimeS;

        // Update sub-frame position for precise sync timing
        lastBitIndex = spiStatus.bitIndex;
        lastBitSamplePos = spiStatus.bitSamplePos;

        // Pick up FPS changes from LTC device (e.g., button press on LTC)
        // and auto-update Si5351 frequency for optimal timing
        static uint16_t lastFpsFromLTC = 0;
        if (spiStatus.fps_thousandths != lastFpsFromLTC && spiStatus.fps_thousandths >= 23000) {
            currentTimecode.fps_thousandths = spiStatus.fps_thousandths;
            currentTimecode.dropFrame = spiStatus.dropFrame;
            uint32_t optimalRate = getSampleRateForFps(spiStatus.fps_thousandths);
            uint32_t currentRate = currentSampleRate;
            if (optimalRate != currentRate) {
                saveSampleRate(optimalRate);
                updateSi5351Frequency(optimalRate);
            }
            lastFpsFromLTC = spiStatus.fps_thousandths;
            saveSettings();
        }

        // Update lock state and check for changes
        bool newLockState = (spiStatus.systemLocked == 1);
        if (newLockState != previousLockState) {
            previousLockState = newLockState;
            ltcSystemLocked = newLockState;
            handleLockStateChange(newLockState);
        } else {
            ltcSystemLocked = newLockState;
        }

        // Update LTC OTA SSID if available (during OTA mode)
        if (otaMode && spiStatus.otaSSID[0] != '\0') {
            ltcOTASSID = String(spiStatus.otaSSID);
        } else if (!otaMode && (uint8_t)spiStatus.otaSSID[0] == 0xE7) {   // master LTC clock event
            const uint8_t* o = (const uint8_t*)spiStatus.otaSSID;
            uint32_t ms; int16_t lost; uint16_t gap;
            memcpy(&ms, o + 2, 4); memcpy(&lost, o + 6, 2); memcpy(&gap, o + 8, 2);
            mltcEventAdd(o[1], ms, lost, gap);
        }

        // Check if LTC device is requesting shutdown due to low battery
        if (spiStatus.lowBatteryShutdown == 1 && !otaMode) {
            // Disable Si5351 clock outputs to save power
            {
                I2cGuard g;
                si5351.output_enable(SI5351_CLK0, 0);
                si5351.output_enable(SI5351_CLK1, 0);
            }

            rstLogLowBatt();   // remember it: shown on the firmware page's reset log

            // Brief delay to ensure everything is settled
            delay(200);

            // Enable wake on charger connected (GPIO 39 = HIGH when USB 5V present via voltage divider)
            esp_sleep_enable_ext0_wakeup(GPIO_NUM_39, 1);
            esp_deep_sleep_start();
            // Code never reaches here - ESP32 is now in deep sleep
        }
    }
}

void pollLTCStatus() {
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_POLL_STATUS;
    performSPITransaction();
}

void pollLTCStatusLive() {
    // Poll LIVE timecode from ISR (bypasses 25ms snapshot delay)
    // Used for frame-exact manual sync operations
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_POLL_STATUS_LIVE;
    performSPITransaction();

}

void sendSyncToLTCDevice() {
    if (mfwBusy()) return;  // master firmware update in progress
    // Don't send sync commands when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        return;
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SYNC_TIMECODE;
    spiCmd.hours = currentTimecode.hours;
    spiCmd.minutes = currentTimecode.minutes;
    spiCmd.seconds = currentTimecode.seconds;
    spiCmd.frames = currentTimecode.frames;
    spiCmd.fps_thousandths = currentTimecode.fps_thousandths;
    spiCmd.dropFrame = currentTimecode.dropFrame;
    spiCmd.running = currentTimecode.running;
    spiCmd.espNowConnected = (espNowClientCount > 0);
    spiCmd.syncIntervalMinutes = syncIntervalMinutes;
    spiCmd.sampleRate = currentSampleRate;

    // Build and include user bits (configurable: cam ID, date, reel, scene)
    buildUserBits(spiCmd.userBits, masterCamId);

    performSPITransaction();
}

void sendRunningStateToLTCDevice(bool running) {
    // Don't send running state commands when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        return;
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SET_RUNNING;
    spiCmd.running = running;
    spiCmd.espNowConnected = (espNowClientCount > 0);
    performSPITransaction();
}

void sendFPSChangeToLTCDevice() {
    // Don't send FPS change commands when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        return;
    }

    // Automatically update Si5351 sample rate for optimal timing based on FPS
    uint32_t optimalRate = getSampleRateForFps(currentTimecode.fps_thousandths);
    uint32_t currentRate = currentSampleRate;
    if (optimalRate != currentRate) {
        saveSampleRate(optimalRate);
        updateSi5351Frequency(optimalRate);
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SET_FPS;
    spiCmd.fps_thousandths = currentTimecode.fps_thousandths;
    spiCmd.dropFrame = currentTimecode.dropFrame;
    performSPITransaction();
}

void sendEspNowStatusToLTCDevice() {
    sendEspNowStatusToLTCDeviceInternal(false);
}

void sendEspNowStatusToLTCDeviceForced() {
    // Force send - used for critical status changes like cam ID that must always be sent
    sendEspNowStatusToLTCDeviceInternal(true);
}

void sendEspNowStatusToLTCDeviceInternal(bool force) {
    // Don't send ESP-NOW status when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    // UNLESS force is true (used for critical updates)
    if (otaMode || (!force && (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked))) {
        return;  // Silently skip - this is called frequently
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_UPDATE_ESPNOW_STATUS;
    spiCmd.espNowConnected = (espNowClientCount > 0);
    spiCmd.syncIntervalMinutes = syncIntervalMinutes;
    spiCmd.numClients = espNowClientCount;
    spiCmd.syncedClients = 0; // Count synced clients
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired && espNowClients[i].synced) {
            spiCmd.syncedClients++;
        }
    }
    // Include user bits with current cam ID
    buildUserBits(spiCmd.userBits, masterCamId);
    performSPITransaction();
}

void sendWiFiInfoToLTCDevice() {
    // Don't send WiFi info when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        return;
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SEND_WIFI_INFO;

    // Copy SSID (max 19 chars + null terminator)
    strncpy(spiCmd.wifiSSID, ssidString.c_str(), 19);
    spiCmd.wifiSSID[19] = '\0';

    // Copy password (max 8 chars + null terminator)
    strncpy(spiCmd.wifiPassword, generatedPassword.c_str(), 8);
    spiCmd.wifiPassword[8] = '\0';
    performSPITransaction();
}

// Ask the master LTC chip to clear its reset log counters
void sendLtcOffsetToLTC(int8_t v) {   // master's own LTC output offset
    SpiCmdGuard spiGuard;
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SET_LTC_OFFSET;
    spiCmd.frames = (uint8_t)v;
    performSPITransaction();
}

void sendClearRstLogToLTC() {
    SpiCmdGuard spiGuard;
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_CLEAR_RSTLOG;
    performSPITransaction();
}

// ==================== FPS HELPER FUNCTIONS ====================
uint8_t getNominalFps(uint16_t fps_thousandths) {
  if (fps_thousandths == 23976) return 24;
  if (fps_thousandths == 29970) return 30;
  return fps_thousandths / 1000;
}

float getFpsFloat(uint16_t fps_thousandths) {
  if (fps_thousandths == 23976) return 24000.0f / 1001.0f;  // Exact 23.976023976...
  if (fps_thousandths == 29970) return 30000.0f / 1001.0f;  // Exact 29.970029970...
  return fps_thousandths / 1000.0f;
}

uint16_t fpsFloatToThousandths(float fps) {
  if (fabs(fps - 23.976f) < 0.001f) return 23976;
  if (fabs(fps - 29.97f) < 0.001f) return 29970;

  if (fabs(fps - 24.0f) < 0.01f) return 24000;
  if (fabs(fps - 25.0f) < 0.01f) return 25000;
  if (fabs(fps - 30.0f) < 0.01f) return 30000;

  return 25000;
}

// Get optimal sample rate for given FPS - eliminates timing drift for NTSC rates
uint32_t getSampleRateForFps(uint16_t fps_thousandths) {
  switch (fps_thousandths) {
    case 23976:  // 23.976 fps - NTSC film
    case 29970:  // 29.97 fps - NTSC video
      return 47952;  // 48000000/1001 Hz - gives exact integer samples/bit (25 and 20)
    default:
      return 48000;  // Standard rate for integer FPS (24, 25, 30)
  }
}

// Check if frames 0,1 should be dropped (29.97 DF only)
bool shouldDropFrame(uint8_t minutes, uint8_t seconds, uint8_t frames, bool dropFrameEnabled, uint16_t fps_thousandths) {
  if (!dropFrameEnabled || fps_thousandths != 29970) return false;
  if (seconds != 0 || frames > 1) return false;
  if ((minutes % 10) == 0) return false;  // Don't drop on 10-minute marks
  return true;
}

// Advance timecode by N frames with proper drop-frame handling
void advanceTimecode(uint8_t &hours, uint8_t &minutes, uint8_t &seconds, uint8_t &frames,
                     uint8_t framesToAdd, uint16_t fps_thousandths, bool dropFrame) {
  uint8_t fpsInt = getNominalFps(fps_thousandths);

  for (uint8_t i = 0; i < framesToAdd; i++) {
    frames++;

    // Frame wrap-around
    if (frames >= fpsInt) {
      frames = 0;
      seconds++;

      if (seconds >= 60) {
        seconds = 0;
        minutes++;

        if (minutes >= 60) {
          minutes = 0;
          hours++;

          if (hours >= 24) {
            hours = 0;
          }
        }
      }

      // Handle drop-frame: skip frames 0,1 at minute boundaries (except 10-min marks)
      if (shouldDropFrame(minutes, seconds, frames, dropFrame, fps_thousandths)) {
        frames = 2;
      }
    }
  }
}

// Get samples per bit for frame rate (must match LTC device's getSamplesPerBitForFps)
uint8_t getSamplesPerBitForFps(uint16_t fps_thousandths) {
    switch (fps_thousandths) {
        case 23976: return 25;  // 47952 / (24000/1001 * 80) = 25.0 exact
        case 24000: return 25;  // 48000 / (24 * 80) = 25.0 exact
        case 25000: return 24;  // 48000 / (25 * 80) = 24.0 exact
        case 29970: return 20;  // 47952 / (30000/1001 * 80) = 20.0 exact
        case 30000: return 20;  // 48000 / (30 * 80) = 20.0 exact
        default:    return 24;  // Safe default
    }
}

// Wait for LTC frame boundary for precise sync timing
// Returns microseconds waited (0 if already at boundary)
// After calling this, the timecode will have just advanced to the next frame
uint32_t waitForFrameBoundary(uint16_t fps_thousandths) {
    // Poll live status to get current sub-frame position
    pollLTCStatusLive();
    delayMicroseconds(500);
    pollLTCStatusLive();

    // If already near frame boundary (bitIndex < 5), no need to wait
    if (lastBitIndex < 5) {
        return 0;
    }

    // Calculate frame duration and bit duration
    float fpsFloat = getFpsFloat(fps_thousandths);
    uint32_t frameDurationUs = (uint32_t)(1000000.0f / fpsFloat);
    uint32_t bitDurationUs = frameDurationUs / 80;

    // Calculate remaining bits until frame boundary
    uint8_t remainingBits = 80 - lastBitIndex;

    // Get correct samples per bit for this FPS (varies: 20-25 depending on rate)
    uint8_t samplesPerBit = getSamplesPerBitForFps(fps_thousandths);

    // Add partial bit time based on sample position
    uint32_t partialBitUs = 0;
    if (lastBitSamplePos > 0 && samplesPerBit > 0) {
        partialBitUs = (bitDurationUs * lastBitSamplePos) / samplesPerBit;
    }

    // Total wait time: remaining bits minus partial bit already elapsed
    uint32_t waitTimeUs = (remainingBits * bitDurationUs) - partialBitUs;

    // Limit wait to avoid blocking too long (max ~one frame)
    if (waitTimeUs > frameDurationUs) {
        waitTimeUs = frameDurationUs;
    }

    // Wait for frame boundary
    if (waitTimeUs > 1000) {
        // Use delay for longer waits (more accurate for >1ms)
        delay(waitTimeUs / 1000);
        uint32_t remainingUs = waitTimeUs % 1000;
        if (remainingUs > 0) {
            delayMicroseconds(remainingUs);
        }
    } else if (waitTimeUs > 0) {
        delayMicroseconds(waitTimeUs);
    }

    return waitTimeUs;
}

// ==================== SETTINGS PERSISTENCE ====================
void saveSettings() {
  preferences.begin("timecode", false);
  preferences.putUShort("fps1000", currentTimecode.fps_thousandths);
  preferences.putBool("dropFrame", currentTimecode.dropFrame);
  preferences.putBool("running", currentTimecode.running);
  preferences.end();
}

void saveSyncSettings() {
  preferences.begin("timecode", false);
  preferences.putULong("syncInterval", syncIntervalMinutes);
  preferences.end();
}

void loadSyncSettings() {
  preferences.begin("timecode", true);
  syncIntervalMinutes = preferences.getULong("syncInterval", 0);
  preferences.end();
}

void loadSettings() {
  preferences.begin("timecode", true);
  currentTimecode.fps_thousandths = preferences.getUShort("fps1000", 25000);
  currentTimecode.dropFrame = preferences.getBool("dropFrame", false);
  currentTimecode.running = preferences.getBool("running", true);
  preferences.end();
}

// ==================== CLIENT NAME FUNCTIONS ====================
// Store client names in "client_names" namespace
// Key: sanitized MAC (colons replaced with underscores), Value: name string

String sanitizeMacForKey(const String& mac) {
    String key = mac;
    key.replace(":", "_");
    return key;
}

String getClientName(const String& mac) {
    preferences.begin("client_names", true);
    String key = sanitizeMacForKey(mac);
    String name = preferences.getString(key.c_str(), "");
    preferences.end();
    return name.length() > 0 ? name : mac;  // Return MAC if no name set
}

void saveClientName(const String& mac, const String& name) {
    preferences.begin("client_names", false);
    String key = sanitizeMacForKey(mac);
    if (name.length() > 0) {
        preferences.putString(key.c_str(), name);
    } else {
        preferences.remove(key.c_str());  // Empty name = delete
    }
    preferences.end();
}

void sendClientNameToDevice(int clientIndex, const char* name);  // Forward declaration

// ==================== SI5351 FUNCTIONS ====================
void loadCalibration() {
  preferences.begin("si5351_cal", true);
  si5351_calibrated = preferences.getBool("calibrated", false);
  si5351_correction = preferences.getInt("correction", 0);
  preferences.end();
}

void saveCalibration() {
  preferences.begin("si5351_cal", false);
  preferences.putBool("calibrated", si5351_calibrated);
  preferences.putInt("correction", si5351_correction);
  preferences.end();
}

uint32_t loadSampleRate() {
  preferences.begin("si5351_cal", true);
  uint32_t rate = preferences.getUInt("sampleRate", 48000);
  preferences.end();
  // Validate sample rate - allow 47952 (NTSC optimal) or 48000 (integer fps)
  if (rate != 47952 && rate != 48000) {
    rate = 48000; // Default to 48000 if invalid
  }
  currentSampleRate = rate;
  return rate;
}

void saveSampleRate(uint32_t sampleRate) {
  currentSampleRate = sampleRate;
  preferences.begin("si5351_cal", false);
  preferences.putUInt("sampleRate", sampleRate);
  preferences.end();
}

// ==================== USER BITS FUNCTIONS ====================
// Convert char to nibble: '0'-'9' = 0-9, 'A'-'Z' = 0-15 (mod 16)
uint8_t charToNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'Z') return (c - 'A') % 16;  // A=0, B=1, ... P=15, Q=0, ...
    if (c >= 'a' && c <= 'z') return (c - 'a') % 16;
    return 0;
}

// Convert nibble to char: 0-9 = '0'-'9', 10-15 = 'A'-'F'
char nibbleToChar(uint8_t n) {
    if (n < 10) return '0' + n;
    return 'A' + (n - 10);
}

// Load master camera ID from preferences
void loadMasterCamId() {
    preferences.begin("user_bits", true);
    String id = preferences.getString("master_cam", "00");
    preferences.end();
    strncpy(masterCamId, id.c_str(), 2);
    masterCamId[2] = '\0';
}

// Save master camera ID to preferences
void saveMasterCamId(const char* camId) {
    strncpy(masterCamId, camId, 2);
    masterCamId[2] = '\0';
    preferences.begin("user_bits", false);
    preferences.putString("master_cam", masterCamId);
    preferences.end();
}

// Build user bits: Fixed layout [MMDDYY][CC] = 8 nibbles
// Reads date from RTC. Date rollover at midnight is handled by the LTC device.
// Nibbles stored in REVERSE order (units first, tens second) because
// LTC monitors read nibble pairs as: second_nibble*10 + first_nibble
void buildUserBits(uint8_t* userBits, const char* camId) {
    // Read the loop-task-maintained date cache - NEVER rtc.now() here: this
    // runs from the WiFi task (ESP-NOW callbacks) and async web handlers,
    // and concurrent DS3231 reads corrupt each other (see cache declaration).
    uint8_t month = cachedRtcMonth;
    uint8_t day = cachedRtcDay;
    uint8_t year = cachedRtcYear;

    // MMDDYY - units first, tens second for each pair
    userBits[0] = month % 10;  // Month units
    userBits[1] = month / 10;  // Month tens
    userBits[2] = day % 10;    // Day units
    userBits[3] = day / 10;    // Day tens
    userBits[4] = year % 10;   // Year units
    userBits[5] = year / 10;   // Year tens

    // Camera ID - second char first, first char second
    userBits[6] = charToNibble(camId[1]);
    userBits[7] = charToNibble(camId[0]);
}

// ==================== SUB-PPM PRECISION FREQUENCY GENERATION ====================
// Strategy: single fixed VCO at 768.000000 MHz with switchable integer multisynth.
//
//   PLL_A:  25 MHz × (30 + 18/25)  =  768.000000 MHz       (exact within Si5351 regs)
//   Output = VCO / (MS × R),  R = 16 (SI5351_OUTPUT_CLK_DIV_16):
//     MS = 1000  →  768e6 / 16000  =  48000.0000000 Hz     EXACT (24 / 25 / 30 fps)
//     MS = 1001  →  768e6 / 16016  =  47952.0479520... Hz  EXACT (= 48e6/1001, true NTSC)
//
// Switching FPS only re-writes MS — PLL stays locked, no relock glitch.
// Both rates inherit crystal accuracy directly (sub-ppm given a TCXO).

// Build Si5351 multisynth register set for an integer-only divider M.
// Per AN619: P1 = 128·M − 512, P2 = 0, P3 = 1 for integer mode.
static void si5351_buildIntegerMS(uint32_t M, struct Si5351RegSet *r) {
    r->p1 = 128UL * M - 512UL;
    r->p2 = 0;
    r->p3 = 1;
}

// Apply the multisynth for the requested LTC sample rate.
// Caller must have already programmed PLL_A to 768 MHz.
static void si5351_applyMS(uint32_t freq) {
    I2cGuard g;
    struct Si5351RegSet ms_reg;
    if (freq == 47952) {
        // True NTSC: 48,000,000 / 1001 Hz
        si5351_buildIntegerMS(1001, &ms_reg);
    } else {
        // Integer FPS: exactly 48,000 Hz
        si5351_buildIntegerMS(1000, &ms_reg);
    }
    // int_mode = 1 (integer-only mode for lowest jitter and bit-exact division)
    // r_div    = SI5351_OUTPUT_CLK_DIV_16   (R = 16)
    // div_by_4 = 0
    si5351.set_ms(SI5351_CLK0, ms_reg, 1, SI5351_OUTPUT_CLK_DIV_16, 0);
    si5351.set_ms_source(SI5351_CLK0, SI5351_PLLA);
}

// After a crash in the middle of an I2C transfer a chip on the bus can keep SDA pulled low, and nothing on the bus
// answers until power is removed. If SDA is stuck low at start-up, clock it free (9 pulses on SCL, then a STOP).
static void i2cBusRecover() {
    pinMode(21, INPUT_PULLUP);
    pinMode(22, INPUT_PULLUP);
    delayMicroseconds(20);
    if (digitalRead(21) == HIGH) return;   // bus idle: nothing to do
    for (int i = 0; i < 9 && digitalRead(21) == LOW; i++) {
        pinMode(22, OUTPUT);
        digitalWrite(22, LOW);
        delayMicroseconds(5);
        pinMode(22, INPUT_PULLUP);
        delayMicroseconds(5);
    }
    pinMode(21, OUTPUT);
    digitalWrite(21, LOW);
    delayMicroseconds(5);
    pinMode(22, INPUT_PULLUP);
    delayMicroseconds(5);
    pinMode(21, INPUT_PULLUP);
}

void setupSi5351() {
    if (!i2cMutex) i2cMutex = xSemaphoreCreateRecursiveMutex();   // setup() is single-threaded, so this is safe
    I2cGuard g;
    i2cBusRecover();

    // Get optimal sample rate for current FPS (loaded earlier in loadSettings)
    uint32_t optimalRate = getSampleRateForFps(currentTimecode.fps_thousandths);
    uint32_t savedRate = loadSampleRate();

    // Use optimal rate for current FPS, save if different
    if (optimalRate != savedRate) {
        saveSampleRate(optimalRate);
    }

    loadCalibration();

    if (si5351.init(SI5351_CRYSTAL_LOAD_8PF, 0, si5351_correction)) {
    } else {
        return;
    }

    // Lock PLL_A to 768.000 MHz once — survives all FPS changes.
    si5351.set_pll(76800000000ULL, SI5351_PLLA);

    // Apply the integer MS divider for the current rate.
    si5351_applyMS(optimalRate);

    si5351.drive_strength(SI5351_CLK0, SI5351_DRIVE_8MA);
    si5351.output_enable(SI5351_CLK1, 0);  // CLK1 unused (LTC is clocked from CLK0) - keep disabled

    // Reset PLL_A so the new feedback ratio locks cleanly - with the output still OFF. Enabling it
    // before the reset sent the LTC a burst of wrong-frequency pulses while the PLL relocked
    // (thousands of "extra pulses" on the clock check after every restart).
    si5351.pll_reset(SI5351_PLLA);
    delay(10);                             // PLL lock time is < 10 ms
    si5351.output_enable(SI5351_CLK0, 1);
}

void updateSi5351Frequency(uint32_t freq) {
    // PLL_A stays locked at 768 MHz — only the multisynth divider changes.
    // No PLL relock, no output glitch, instant frequency switch.
    si5351_applyMS(freq);
}

// ==================== RTC MANAGEMENT ====================
void updateRTCache() {
  if (millis() - lastRTCUpdate > RTC_UPDATE_INTERVAL) {
    DateTime now = rtcNow();
    cachedRtcMonth = now.month();
    cachedRtcDay = now.day();
    cachedRtcYear = now.year() % 100;
    char buffer[30];
    snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d %02d:%02d:%02d",
             now.year(), now.month(), now.day(),
             now.hour(), now.minute(), now.second());
    cachedRTCString = String(buffer);
    lastRTCUpdate = millis();
  }
}

String getRTCString() {
  return cachedRTCString;
}

void setupRTC() {
  if (!i2cMutex) i2cMutex = xSemaphoreCreateRecursiveMutex();
  I2cGuard g;

  Wire.begin(21, 22);
  Wire.setClock(100000);
  delay(100);

  bool rtcFound = false;
  for (int i = 0; i < 3; i++) {
    if (rtc.begin()) {
      rtcFound = true;
      break;
    }
    delay(500);
  }

  if (!rtcFound) {
    rtcWarn = 2;
    return;
  }

  if (rtc.lostPower()) {
    // The clock has to run, so it starts from the firmware's build date, but that is NOT the real time:
    // remember it (the chip's own flag is cleared by adjust) and warn on the page until someone sets it.
    rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    rtcSetSave(false);
  }
  rtcWarn = rtcSetLoad() ? 0 : 1;

  DateTime now = rtcNow();
  cachedRtcMonth = now.month();
  cachedRtcDay = now.day();
  cachedRtcYear = now.year() % 100;

  unsigned long currentMillis = millis();
  uint8_t nominalFps = getNominalFps(currentTimecode.fps_thousandths);
  float fpsFloat = getFpsFloat(currentTimecode.fps_thousandths);
  uint16_t msIntoSecond = currentMillis % 1000;
  uint8_t exactFrames = (msIntoSecond * fpsFloat) / 1000.0f;

  if (exactFrames >= nominalFps) {
    exactFrames = nominalFps - 1;
  }

  currentTimecode.hours = now.hour();
  currentTimecode.minutes = now.minute();
  currentTimecode.seconds = now.second();
  currentTimecode.frames = exactFrames;

  sendSyncToLTCDevice();
}

void syncFromRTC(bool isManualSync) {
  DateTime now = rtcNow();

  // The DS3231 keeps whole seconds only, so the sub-second part cannot be read. What can be found is the moment the
  // second changes: wait for that tick (at most 1.1 s, polled every 2 ms) and count the frames from it. Before, the
  // frame came from millis() % 1000, which has no relation to the RTC tick: time of day was off by up to ~1 s.
  // If the RTC never ticks (stopped chip), fall back to the old millis() phase.
  bool edgeFound = false;
  int64_t edgeUs = 0;
  {
    uint8_t firstSec = now.second();
    int64_t t0 = esp_timer_get_time();
    while (esp_timer_get_time() - t0 < 1100000LL) {
      delay(2);
      int64_t tPoll = esp_timer_get_time();
      DateTime n2 = rtcNow();
      if (n2.second() != firstSec) { now = n2; edgeUs = tPoll; edgeFound = true; break; }
    }
  }

  unsigned long currentMillis = millis();
  uint8_t rtcSecond = now.second();
  uint8_t rtcMinute = now.minute();
  uint8_t rtcHour = now.hour();

  uint8_t nominalFps = getNominalFps(currentTimecode.fps_thousandths);
  float fpsFloat = getFpsFloat(currentTimecode.fps_thousandths);

  uint16_t msIntoSecond = currentMillis % 1000;
  uint8_t exactFrames = (msIntoSecond * fpsFloat) / 1000.0f;
  if (edgeFound) {   // frames since the RTC tick (a few ms: normally frame 0)
    int64_t sinceUs = esp_timer_get_time() - edgeUs;
    if (sinceUs < 0) sinceUs = 0;
    exactFrames = (uint8_t)((sinceUs * (double)fpsFloat) / 1000000.0);
  }

  if (exactFrames >= nominalFps) {
    exactFrames = nominalFps - 1;
  }

  // CRITICAL FIX: Handle drop frame timecode
  // For 29.97fps drop frame, frames 0 and 1 are skipped at minute boundaries
  // (except every 10th minute: 0, 10, 20, 30, 40, 50)
  if (currentTimecode.dropFrame && currentTimecode.fps_thousandths == 29970) {
    if (rtcSecond == 0 && exactFrames < 2 && (rtcMinute % 10) != 0) {
      exactFrames = 2;  // Skip frames 0 and 1
    }
  }

  currentTimecode.hours = rtcHour;
  currentTimecode.minutes = rtcMinute;
  currentTimecode.seconds = rtcSecond;
  currentTimecode.frames = exactFrames;
  lastTimecodeSetTime = millis();  // Protect from SPI overwrite

  // TIMING FIX: Send to ESP-NOW clients FIRST, then Master LTC
  // This compensates for wireless transmission delay (~2-3ms)
  // Same approach as manualSyncAllClients() and broadcastRunningState()
  broadcastSyncESPNow(true);

  // Small delay to let ESP-NOW transmission start
  delayMicroseconds(500);

  // THEN send to Master LTC via SPI (instant)
  sendSyncToLTCDevice();

  // Restore original RTC values for display
  currentTimecode.hours = rtcHour;
  currentTimecode.minutes = rtcMinute;
  currentTimecode.seconds = rtcSecond;
  currentTimecode.frames = exactFrames;
}

bool setRTC(int year, int month, int day, int hour, int minute, int second) {
  if (rtcWarn == 2 || !rtcValid(year, month, day, hour, minute, second)) return false;   // bad numbers never reach the chip
  DateTime newTime(year, month, day, hour, minute, second);
  {
    I2cGuard g;
    rtc.adjust(newTime);
  }
  rtcSetSave(true);
  rtcWarn = 0;

  currentTimecode.hours = hour;
  currentTimecode.minutes = minute;
  currentTimecode.seconds = second;

  // Handle drop frame: skip frames 0,1 at minute boundaries (except every 10th minute)
  if (currentTimecode.dropFrame && currentTimecode.fps_thousandths == 29970 &&
      second == 0 && (minute % 10) != 0) {
    currentTimecode.frames = 2;
  } else {
    currentTimecode.frames = 0;
  }

  sendSyncToLTCDevice();
  saveSettings();
  return true;
}

// Set the RTC on an exact second boundary. The page sends the time of an UPCOMING whole second and how many ms
// from now it will start; this task sleeps until then, spins the last few ms, and writes. A DS3231 starts counting
// its new second the moment it is written, so writing on the boundary is what makes the RTC match the phone.
struct RtcAtReq { int y, mo, d, h, mi, s; int64_t deadlineUs; };
static RtcAtReq rtcAtReq;
static volatile bool rtcAtBusy = false;
static void rtcAtTask(void*) {
    RtcAtReq r = rtcAtReq;
    int64_t rem = r.deadlineUs - esp_timer_get_time();
    if (rem > 4000) vTaskDelay(pdMS_TO_TICKS((rem - 4000) / 1000));
    while (esp_timer_get_time() < r.deadlineUs) { }
    if (setRTC(r.y, r.mo, r.d, r.h, r.mi, r.s)) broadcastSyncESPNow(true);
    sendWebSocketData();
    rtcAtBusy = false;
    vTaskDelete(NULL);
}

// ==================== NETWORK ====================
void setupWiFi() {

  uint32_t chipId = 0;
  for(int i=0; i<17; i=i+8) {
    chipId |= ((ESP.getEfuseMac() >> (40-i)) & 0xff) << i;
  }

  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%04X", (uint16_t)(chipId & 0xFFFF));

  ssidString = "TimecodeMaster-" + String(suffix);

  // Use WIFI_AP_STA from the start to avoid mode change issues later
  // Mode changes can disrupt the softAP configuration on ESP32
  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(WIFI_PS_MIN_MODEM);  // Balanced power saving for WebSocket stability
  IPAddress local_IP(192, 168, 4, 1);
  IPAddress gateway(192, 168, 4, 1);
  IPAddress subnet(255, 255, 255, 0);

  WiFi.softAPConfig(local_IP, gateway, subnet);

  // Retry softAP creation if it fails (can happen on cold boot)
  bool apStarted = false;
  for (int retry = 0; retry < 3 && !apStarted; retry++) {
    // Channel 1 (the default) and up to 8 stations: clients updating in parallel + the browser
    apStarted = WiFi.softAP(ssidString.c_str(), generatedPassword.c_str(), 1, 0, 8);
    if (!apStarted) {
      delay(500);
    }
  }

  delay(500);

}

// ---- ESP-NOW message names --------------------------------------------------------------------
// struct_message.type is only 8 bytes, but some names are longer ("PAIR_CONF", "UNPAIR_REQ") or fill
// all 8 bytes without room for the NUL ("PAIR_REQ", "SET_NAME"). Older firmware strcpy()'d them, which
// overflowed into the hours/minutes/seconds bytes (undefined behaviour), and older receivers strcmp()'d
// across those bytes. To stay wire-compatible with units still running old firmware, SET_MSG_TYPE
// writes exactly the same bytes as before - deliberately, through the raw struct bytes - and these
// messages carry no timecode. MSG_TYPE_IS compares only the 8-byte field, so new receivers never
// depend on the bytes after it. Old and new units understand each other in both directions.
#define SET_MSG_TYPE(m, name) do { \
        size_t _n = strlen(name) + 1; \
        size_t _max = sizeof(m) - offsetof(struct_message, type); \
        memcpy(((uint8_t*)&(m)) + offsetof(struct_message, type), (name), _n < _max ? _n : _max); \
    } while (0)
#define MSG_TYPE_IS(m, name) (strncmp((m).type, (name), sizeof((m).type)) == 0)

// ==================== PAIRING FUNCTIONS ====================
void startPairingMode() {
    if (pairingMode) return;

    pairingMode = true;
    pairingModeStartTime = millis();
    unpairedClientCount = 0;

    sendWebSocketPairingUpdate();
}

void stopPairingMode() {
    if (!pairingMode) return;

    pairingMode = false;
    pairingModeStartTime = 0;

    sendWebSocketPairingUpdate();
}

void addUnpairedClient(const uint8_t *mac) {
    if (!pairingMode) return;

    char macStr[18];
    snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    for (int i = 0; i < unpairedClientCount; i++) {
        if (memcmp(unpairedClients[i].mac, mac, 6) == 0) {
            unpairedClients[i].lastSeen = millis();
            unpairedClients[i].active = true;
            return;
        }
    }

    if (unpairedClientCount < MAX_UNPAIRED_CLIENTS) {
        memcpy(unpairedClients[unpairedClientCount].mac, mac, 6);
        unpairedClients[unpairedClientCount].macString = String(macStr);
        unpairedClients[unpairedClientCount].firstSeen = millis();
        unpairedClients[unpairedClientCount].lastSeen = millis();
        unpairedClients[unpairedClientCount].active = true;
        unpairedClientCount++;

        sendWebSocketPairingUpdate();
    }
}

void removeUnpairedClient(const uint8_t *mac) {
    for (int i = 0; i < unpairedClientCount; i++) {
        if (memcmp(unpairedClients[i].mac, mac, 6) == 0) {
            for (int j = i; j < unpairedClientCount - 1; j++) {
                unpairedClients[j] = unpairedClients[j + 1];
            }
            unpairedClientCount--;
            sendWebSocketPairingUpdate();
            return;
        }
    }
}

void pairClient(const uint8_t *mac) {
    int clientIndex = -1;
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) == 0) {
            clientIndex = i;
            break;
        }
    }

    if (clientIndex == -1) {
        clientIndex = findOrAddESPNowClient(mac);
        if (clientIndex == -1) {
            return;
        }
    }

    espNowClients[clientIndex].paired = true;
    espNowClients[clientIndex].needsInitialSync = false;  // Will be synced immediately
    removeUnpairedClient(mac);
    savePairedClients();
    sendPairingConfirmation(mac);

    char macStr[18];
    snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);


    sendESPNowSync(mac);
    espNowClients[clientIndex].lastSyncSent = millis();
    sendWebSocketPairingUpdate();
    sendWebSocketClientsUpdate();
    sendWebSocketClientsDetailed();
}

void unpairClient(const uint8_t *mac) {
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) == 0) {
            char macStr[18];
            snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
                     mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);


            sendUnpairCommand(mac);
            esp_now_del_peer(mac);

            for (int j = i; j < espNowClientCount - 1; j++) {
                espNowClients[j] = espNowClients[j + 1];
            }
            espNowClientCount--;


            savePairedClients();
            sendWebSocketPairingUpdate();
            sendWebSocketClientsUpdate();
            sendWebSocketClientsDetailed();

            return;
        }
    }

}

void sendPairingConfirmation(const uint8_t *mac) {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled

    struct_message pairMsg;
    memset(&pairMsg, 0, sizeof(pairMsg));
    SET_MSG_TYPE(pairMsg, "PAIR_CONF");
    pairMsg.timestamp = millis();

    String masterMac = WiFi.macAddress();
    masterMac.toCharArray(pairMsg.mac, sizeof(pairMsg.mac));

    esp_now_send(mac, (uint8_t *) &pairMsg, sizeof(pairMsg));
}

void sendUnpairCommand(const uint8_t *mac) {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled

    struct_message unpairMsg;
    memset(&unpairMsg, 0, sizeof(unpairMsg));
    SET_MSG_TYPE(unpairMsg, "UNPAIR");
    unpairMsg.timestamp = millis();

    esp_now_send(mac, (uint8_t *) &unpairMsg, sizeof(unpairMsg));
}

void sendClientNameToDevice(int clientIndex, const char* name) {
    if (!wirelessEnabled) return;
    if (clientIndex < 0 || clientIndex >= espNowClientCount) return;
    if (!espNowClients[clientIndex].paired) return;

    struct_message nameMsg;
    memset(&nameMsg, 0, sizeof(nameMsg));
    SET_MSG_TYPE(nameMsg, "SET_NAME");
    nameMsg.timestamp = millis();
    strncpy(nameMsg.deviceName, name, sizeof(nameMsg.deviceName) - 1);
    nameMsg.deviceName[sizeof(nameMsg.deviceName) - 1] = '\0';

    espSendClient(espNowClients[clientIndex].mac, (uint8_t*)&nameMsg, sizeof(nameMsg));
    Serial.printf("Sent name '%s' to client %s\n", name, espNowClients[clientIndex].macString.c_str());
}

void loadPairedClients() {
    preferences.begin("paired_clients", true);

    int count = preferences.getInt("count", 0);

    for (int i = 0; i < count && i < MAX_ESPNOW_CLIENTS; i++) {
        String key = "mac_" + String(i);
        String macStr = preferences.getString(key.c_str(), "");

        if (macStr.length() == 17) {
            uint8_t mac[6];
            unsigned int tempMac[6];
            sscanf(macStr.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
                   &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]);
            for (int j = 0; j < 6; j++) mac[j] = (uint8_t)tempMac[j];

            int clientIndex = findOrAddESPNowClient(mac);
            if (clientIndex >= 0) {
                espNowClients[clientIndex].paired = true;
                espNowClients[clientIndex].needsInitialSync = true;  // Mark for sync after master boot
                espNowClients[clientIndex].lastSyncSent = 0;

                esp_now_peer_info_t peerInfo = {};
                memcpy(peerInfo.peer_addr, mac, 6);
                peerInfo.channel = 0;
                peerInfo.encrypt = false;

                if (!esp_now_is_peer_exist(mac)) {
                    esp_now_add_peer(&peerInfo);
                }
            }
        }
    }

    preferences.end();
}

void savePairedClients() {
    preferences.begin("paired_clients", false);
    int oldCount = preferences.getInt("count", 0);

    for (int i = 0; i < oldCount; i++) {
        String key = "mac_" + String(i);
        preferences.remove(key.c_str());
    }

    int pairedCount = 0;
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            pairedCount++;
        }
    }

    preferences.putInt("count", pairedCount);

    int savedCount = 0;
    for (int i = 0; i < espNowClientCount && savedCount < pairedCount; i++) {
        if (espNowClients[i].paired) {
            String key = "mac_" + String(savedCount);
            preferences.putString(key.c_str(), espNowClients[i].macString);
            savedCount++;
        }
    }

    preferences.end();
}

void sendWebSocketPairingUpdate() {
    if (wsCount() == 0) return;

    char json[512];
    int pos = 0;

    pos += snprintf(json + pos, sizeof(json) - pos,
        "{\"type\":\"pairing_update\",\"pairingMode\":%s,\"pairingTimeRemaining\":%lu,\"unpairedClients\":[",
        pairingMode ? "true" : "false",
        pairingMode ? (PAIRING_MODE_DURATION - (millis() - pairingModeStartTime)) / 1000 : 0);

    for (int i = 0; i < unpairedClientCount && pos < (int)sizeof(json) - 50; i++) {
        if (i > 0) pos += snprintf(json + pos, sizeof(json) - pos, ",");
        pos += snprintf(json + pos, sizeof(json) - pos,
            "{\"mac\":\"%s\",\"timeSeen\":%lu}",
            unpairedClients[i].macString.c_str(),
            (millis() - unpairedClients[i].firstSeen) / 1000);
    }

    snprintf(json + pos, sizeof(json) - pos, "]}");
    wsBroadcast(json);
}

void checkPairingModeTimeout() {
    if (pairingMode && (millis() - pairingModeStartTime >= PAIRING_MODE_DURATION)) {
        stopPairingMode();
    }
}

// ==================== ESP-NOW FUNCTIONS ====================
void setupESP_NOW() {
    // WiFi mode already set to WIFI_AP_STA in setupWiFi()
    // Don't change mode here as it can disrupt the softAP

    if (esp_now_init() != ESP_OK) {
        // Retry once after a short delay
        delay(100);
        if (esp_now_init() != ESP_OK) {
            return;
        }
    }

    // REDUCE TX POWER to minimize voltage ripple on TCXO rail
    // Default is ~19.5dBm. 11dBm reduces peak current significantly; the firmware page sets the value (and the ESP-NOW rate).
    radioApplyAll();

    registerESPNowCallbacks();

    // Add broadcast peer for simultaneous sync to all clients
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;

    esp_now_add_peer(&peerInfo);

    loadPairedClients();

    wirelessInitialized = true;  // Mark wireless as initialized
}

void processESPNowPacket(const uint8_t *mac, struct_message data) {
    // Skip processing if wireless is disabled
    if (!wirelessEnabled) {
        return;
    }

    char macStr[18];
    snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    if (MSG_TYPE_IS(data, "UNPAIR_REQ")) {
        unpairClient(mac);
        return;
    }

    bool alreadyPaired = false;
    int pairedClientIndex = -1;
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) == 0 && espNowClients[i].paired) {
            alreadyPaired = true;
            pairedClientIndex = i;
            break;
        }
    }

    if (MSG_TYPE_IS(data, "PAIR_REQ")) {
        if (pairingMode && !alreadyPaired) {
            bool alreadyConnected = false;
            for (int i = 0; i < espNowClientCount; i++) {
                if (memcmp(espNowClients[i].mac, mac, 6) == 0) {
                    alreadyConnected = true;
                    break;
                }
            }

            if (!alreadyConnected) {
                addUnpairedClient(mac);
            }
        }
        return;
    }

    if (MSG_TYPE_IS(data, "REG")) {

        if (alreadyPaired) {

            bool shouldSync = false;
            unsigned long previousUptime = espNowClients[pairedClientIndex].uptime;
            unsigned long newUptime = data.uptime;

            // Case 1: Client restarted (uptime went backwards)
            if (previousUptime > 0 && newUptime < previousUptime) {
                shouldSync = true;
            }
            
            // Case 2: Master just booted and client was waiting (needsInitialSync flag)
            // This handles: client boots first, then master boots
            if (espNowClients[pairedClientIndex].needsInitialSync) {
                shouldSync = true;
            }
            
            // Case 3: Client just booted (uptime < 30 s - the client sends SECONDS; this compared against
            // 30000 before, which re-synced every client on each heartbeat for its first ~8.3 hours)
            // This handles: master running, client power cycles
            // Skip if master just booted (< 10s) - syncFromRTC() in setup() already sent sync
            if (millis() > 10000 && newUptime < 30 && (millis() - espNowClients[pairedClientIndex].lastSyncSent > 5000)) {
                shouldSync = true;
            }

            espNowClients[pairedClientIndex].lastSeen = millis();
            espNowClients[pairedClientIndex].registered = true;
            espNowClients[pairedClientIndex].uptime = newUptime;

            esp_now_peer_info_t peerInfo = {};
            memcpy(peerInfo.peer_addr, mac, 6);
            peerInfo.channel = 0;
            peerInfo.encrypt = false;

            esp_now_add_peer(&peerInfo);

            if (shouldSync) {
                // Send sync to this specific client
                sendESPNowSync(mac);
                espNowClients[pairedClientIndex].needsInitialSync = false;
                espNowClients[pairedClientIndex].lastSyncSent = millis();
            }

            sendWebSocketClientsUpdate();
            sendWebSocketClientsDetailed();

        } else {

            int clientIndex = findOrAddESPNowClient(mac);
            if (clientIndex >= 0) {
                espNowClients[clientIndex].lastSeen = millis();
                espNowClients[clientIndex].macString = String(macStr);
            }

            if (pairingMode) {
                bool isConnected = false;
                for (int i = 0; i < espNowClientCount; i++) {
                    if (memcmp(espNowClients[i].mac, mac, 6) == 0 &&
                        (millis() - espNowClients[i].lastSeen < 10000)) {
                        isConnected = true;
                        break;
                    }
                }

                if (!isConnected) {
                    addUnpairedClient(mac);
                }
            }
        }
        return;
    }

    if (MSG_TYPE_IS(data, "HB")) {
        if (alreadyPaired) {
            // Check for restart BEFORE updating status (uptime)
            bool shouldSync = false;
            unsigned long previousUptime = espNowClients[pairedClientIndex].uptime;
            unsigned long newUptime = data.uptime;

            // Case 1: Client restarted (uptime went backwards)
            if (previousUptime > 0 && newUptime < previousUptime) {
                shouldSync = true;
            }

            // Case 2: Master just booted (needsInitialSync flag)
            if (espNowClients[pairedClientIndex].needsInitialSync) {
                shouldSync = true;
            }

            // Case 3: Client just booted (uptime < 30 s - the client sends SECONDS; this compared against
            // 30000 before, which re-synced every client on each heartbeat for its first ~8.3 hours)
            if (millis() > 10000 && newUptime < 30 && (millis() - espNowClients[pairedClientIndex].lastSyncSent > 5000)) {
                shouldSync = true;
            }

            // Now update status (including uptime)
            updateESPNowClientStatus(mac, data);

            if (!espNowClients[pairedClientIndex].registered) {
                espNowClients[pairedClientIndex].registered = true;
            }

            // Sync if needed
            if (shouldSync) {
                sendESPNowSync(mac);
                espNowClients[pairedClientIndex].needsInitialSync = false;
                espNowClients[pairedClientIndex].lastSyncSent = millis();
            }

            struct_message ackMsg;
            memset(&ackMsg, 0, sizeof(ackMsg));
            SET_MSG_TYPE(ackMsg, "HB_ACK");
            ackMsg.timestamp = millis();
            esp_now_send(mac, (uint8_t *) &ackMsg, sizeof(ackMsg));

            removeUnpairedClient(mac);
        }
        return;
    }

    if (MSG_TYPE_IS(data, "ALIVE")) {
        if (alreadyPaired) {
            espNowClients[pairedClientIndex].lastSeen = millis();
        }
        return;
    }

    if (MSG_TYPE_IS(data, "WAKE")) {   // power save: the client's radio is on and listening now
        if (alreadyPaired) {
            espNowClients[pairedClientIndex].lastSeen = millis();
            espNowClients[pairedClientIndex].awakeAt = millis();
        }
        return;
    }
}

int findOrAddESPNowClient(const uint8_t *mac) {
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) == 0) {
            return i;
        }
    }

    if (espNowClientCount < MAX_ESPNOW_CLIENTS) {
        memcpy(espNowClients[espNowClientCount].mac, mac, 6);
        espNowClients[espNowClientCount].lastSeen = millis();
        espNowClients[espNowClientCount].registered = true;
        espNowClients[espNowClientCount].paired = false;
        espNowClients[espNowClientCount].needsInitialSync = false;
        espNowClients[espNowClientCount].lastSyncSent = 0;
        espNowClients[espNowClientCount].uptime = 0;
        espNowClients[espNowClientCount].rstValid = false;     // a re-used slot must not show another unit's reset log / radio settings
        espNowClients[espNowClientCount].rstLValid = false;
        espNowClients[espNowClientCount].radioValid = false;
        espNowClients[espNowClientCount].rssiAt = 0;
        espNowClients[espNowClientCount].radioHw = 0;
        espNowClients[espNowClientCount].rssiFromMaster = 0;
        espNowClients[espNowClientCount].radioPsValid = false;
        espNowClients[espNowClientCount].awakeAt = 0;
        espNowClients[espNowClientCount].ltcOfsValid = false;

        char macStr[18];
        snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        espNowClients[espNowClientCount].macString = String(macStr);


        esp_now_peer_info_t peerInfo = {};
        memcpy(peerInfo.peer_addr, mac, 6);
        peerInfo.channel = 0;
        peerInfo.encrypt = false;

        esp_now_add_peer(&peerInfo);

        return espNowClientCount++;
    }

    return -1;
}

void updateESPNowClientStatus(const uint8_t *mac, struct_message data) {
    for (int i = 0; i < espNowClientCount; i++) {
        if (memcmp(espNowClients[i].mac, mac, 6) == 0) {
            espNowClients[i].hours = data.hours;
            espNowClients[i].minutes = data.minutes;
            espNowClients[i].seconds = data.seconds;
            espNowClients[i].frames = data.frames;
            espNowClients[i].fps_thousandths = data.fps_thousandths;
            espNowClients[i].running = data.running;
            espNowClients[i].synced = data.synced;
            espNowClients[i].batteryLevel = data.batteryLevel;
            espNowClients[i].batteryPresent = (data.batteryPresent == 1);
            espNowClients[i].lastSeen = millis();
            espNowClients[i].uptime = data.uptime;
            espNowClients[i].jamSyncMode = data.jamSyncMode;  // Store client's jam sync mode

            // Update device name from heartbeat
            if (strlen(data.deviceName) > 0) {
                strncpy(espNowClients[i].deviceName, data.deviceName, sizeof(espNowClients[i].deviceName) - 1);
                espNowClients[i].deviceName[sizeof(espNowClients[i].deviceName) - 1] = '\0';
            } else {
                // Client has no name - check if Master has one stored
                String storedName = getClientName(espNowClients[i].macString);
                if (storedName != espNowClients[i].macString) {
                    // Master has a name stored, send it to client
                    sendClientNameToDevice(i, storedName.c_str());
                    strncpy(espNowClients[i].deviceName, storedName.c_str(), sizeof(espNowClients[i].deviceName) - 1);
                    espNowClients[i].deviceName[sizeof(espNowClients[i].deviceName) - 1] = '\0';
                }
            }

            // Update version info from heartbeat
            espNowClients[i].utilityVersionMajor = data.utilityVersionMajor;
            espNowClients[i].utilityVersionMinor = data.utilityVersionMinor;
            espNowClients[i].utilityVersionPatch = data.utilityVersionPatch;
            espNowClients[i].ltcVersionMajor = data.ltcVersionMajor;
            espNowClients[i].ltcVersionMinor = data.ltcVersionMinor;
            espNowClients[i].ltcVersionPatch = data.ltcVersionPatch;
            if ((data.masterTimestamp & 0xFF000000UL) == 0xD1000000UL) {   // clock check from a v1.3+ client
                espNowClients[i].diagValid = true;
                espNowClients[i].diagNetLost = (int32_t)data.sampleRate;
                espNowClients[i].diagMaxGapUs = data.masterTimestamp & 0xFFFF;
                espNowClients[i].diagSinceValid = (data.userBits[4] == 0xD2);
                memcpy(&espNowClients[i].diagSinceS, data.userBits, 4);
                espNowClients[i].diagErrFirstS = data.userBits[5];
                espNowClients[i].diagErrLastS = data.userBits[6];
                espNowClients[i].diagErrWins = data.userBits[7];
            }
            return;
        }
    }
}

void sendPeriodicAlive() {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled
    if (psActive()) return;        // power save: the beacon is the keep-alive

    static unsigned long lastAlive = 0;
    if (millis() - lastAlive > 10000) {
        lastAlive = millis();

        // LOCAL message: this paced send loop (delay(10) per client, up to
        // ~100ms) must not hold the shared-buffer lock or touch espNowData -
        // esp_now_send copies the buffer synchronously, so a local is safe.
        struct_message aliveMsg;
        memset(&aliveMsg, 0, sizeof(aliveMsg));
        SET_MSG_TYPE(aliveMsg, "ALIVE");
        aliveMsg.timestamp = millis();
        aliveMsg.masterTimestamp = millis();

        // Send to each paired client individually, NOT broadcast
        for (int i = 0; i < espNowClientCount; i++) {
            if (espNowClients[i].paired) {
                esp_now_send(espNowClients[i].mac, (uint8_t *) &aliveMsg, sizeof(aliveMsg));
                delay(10);
            }
        }
    }
}

void sendESPNowSync(const uint8_t *mac, int maxRetries) {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled

    // Wait for frame boundary for sub-frame precision (~0.02ms accuracy)
    // This aligns the sync transmission with the start of an LTC frame
    waitForFrameBoundary(currentTimecode.fps_thousandths);

    // Get fresh timecode immediately after frame boundary
    pollLTCStatusLive();
    delayMicroseconds(500);
    pollLTCStatusLive();

    // Add +1 frame compensation for client boot after master scenario
    // +2 made client ahead, 0 made client late, +1 is the middle ground
    uint8_t targetFrames = currentTimecode.frames;
    uint8_t targetSeconds = currentTimecode.seconds;
    uint8_t targetMinutes = currentTimecode.minutes;
    uint8_t targetHours = currentTimecode.hours;
    advanceTimecode(targetHours, targetMinutes, targetSeconds, targetFrames,
                    1, currentTimecode.fps_thousandths, currentTimecode.dropFrame);

    // LOCAL message: the retry loop below can sleep up to 2 x delay(50) and
    // must not hold the shared-buffer lock (it would stall async handlers and
    // could convoy with the WiFi task that services esp_now_send itself).
    struct_message syncMsg;
    memset(&syncMsg, 0, sizeof(syncMsg));
    SET_MSG_TYPE(syncMsg, "SYNC");
    syncMsg.hours = targetHours;
    syncMsg.minutes = targetMinutes;
    syncMsg.seconds = targetSeconds;
    syncMsg.frames = targetFrames;
    syncMsg.fps_thousandths = currentTimecode.fps_thousandths;
    syncMsg.dropFrame = currentTimecode.dropFrame;
    syncMsg.running = currentTimecode.running;
    syncMsg.timestamp = millis();
    syncMsg.masterTimestamp = millis();
    syncMsg.sampleRate = currentSampleRate;

    // Build user bits with placeholder cam ID - clients will replace with their own
    buildUserBits(syncMsg.userBits, "00");

    // Send sync to clients
    bool success = false;
    for (int attempt = 0; attempt < maxRetries && !success; attempt++) {
        esp_err_t result = esp_now_send(mac, (uint8_t *) &syncMsg, sizeof(syncMsg));

        if (result == ESP_OK) {
            success = true;
        } else {
            if (attempt < maxRetries - 1) {
                delay(50);
            }
        }
    }

    lastESPNowSend = millis();
}

void sendESPNowRunState() {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled

    psGate(120);   // power save: inside a client window (a sync that gated already passes straight through)

    // NO rate limiting for RUN state - must be immediate for sync
    struct_message runMsg;
    memset(&runMsg, 0, sizeof(runMsg));
    SET_MSG_TYPE(runMsg, "RUN");
    runMsg.running = currentTimecode.running;
    runMsg.timestamp = millis();

    // Send to all paired clients with minimal delay
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            esp_now_send(espNowClients[i].mac, (uint8_t *) &runMsg, sizeof(runMsg));
            delayMicroseconds(100);  // 100µs between clients (same as sync)
        }
    }

    lastESPNowSend = millis();
}

void broadcastSyncESPNow(bool isManualSync) {
    if (!wirelessEnabled) return;

    unsigned long now = millis();
    
    // Rate limiting
    if (!isManualSync && (now - lastESPNowSend < ESPNOW_SEND_INTERVAL)) {
        return;
    }
    if (isManualSync && (now - lastESPNowSend < 50)) {
        return;
    }

    // Power save: wait for the start of a client window; a manual sync then sends the time as it is NOW
    unsigned long gateT0 = millis();
    psGate(25);
    unsigned long gateWaited = millis() - gateT0;

    // For manual sync, use currentTimecode which was just set by caller
    // For auto sync, poll fresh data
    Timecode syncTimecode;

    if (isManualSync) {
        // Manual sync (from syncFromRTC): DISABLED +1 frame compensation - testing
        syncTimecode = currentTimecode;
        if (gateWaited > 20 && syncTimecode.running) {   // we waited for the window: move the time on by that much
            float fr = gateWaited * (syncTimecode.fps_thousandths / 1000.0f) / 1000.0f + 0.5f;
            uint8_t add = fr > 255.0f ? 255 : (uint8_t)fr;
            if (add) advanceTimecode(syncTimecode.hours, syncTimecode.minutes, syncTimecode.seconds,
                                     syncTimecode.frames, add, syncTimecode.fps_thousandths, syncTimecode.dropFrame);
        }
        // advanceTimecode removed - send same timecode as master
    } else {
        // Auto sync: wait for frame boundary for sub-frame precision
        waitForFrameBoundary(currentTimecode.fps_thousandths);

        // Get fresh frame data immediately after frame boundary
        // Must poll TWICE: first poll triggers live data preparation,
        // second poll retrieves the actual live data
        pollLTCStatusLive();
        delayMicroseconds(500);
        pollLTCStatusLive();
        syncTimecode = currentTimecode;

        // Add +1 frame compensation for ESP-NOW transmission delay
        advanceTimecode(syncTimecode.hours, syncTimecode.minutes, syncTimecode.seconds,
                        syncTimecode.frames, 1, syncTimecode.fps_thousandths, syncTimecode.dropFrame);
    }

    struct_message syncMsg;
    memset(&syncMsg, 0, sizeof(syncMsg));
    SET_MSG_TYPE(syncMsg, "SYNC");
    syncMsg.hours = syncTimecode.hours;
    syncMsg.minutes = syncTimecode.minutes;
    syncMsg.seconds = syncTimecode.seconds;
    syncMsg.frames = syncTimecode.frames;
    syncMsg.fps_thousandths = syncTimecode.fps_thousandths;
    syncMsg.dropFrame = syncTimecode.dropFrame;
    syncMsg.running = syncTimecode.running;
    syncMsg.timestamp = millis();
    syncMsg.masterTimestamp = millis();
    syncMsg.sampleRate = currentSampleRate;

    // Build user bits with placeholder cam ID - clients will replace with their own
    buildUserBits(syncMsg.userBits, "00");

    // Unicast to each paired client with 100µs delay for reliable delivery
    // ESP-NOW unicast has ACK mechanism unlike broadcast which is fire-and-forget
    // 100µs per client = 1ms max timing difference for 10 clients
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            // DON'T send sync if client is in any JAM SYNC mode (searching, locked, or freerun)
            if (espNowClients[i].jamSyncMode != JAM_SYNC_LOCKED && espNowClients[i].jamSyncMode != JAM_SYNC_FREERUN && espNowClients[i].jamSyncMode != JAM_SYNC_SEARCHING) {
                esp_now_send(espNowClients[i].mac, (uint8_t *) &syncMsg, sizeof(syncMsg));
                delayMicroseconds(100);  // 100µs between clients
            }
        }
    }

    lastESPNowSend = millis();
}

void cleanupStaleESPNowClients() {
    static unsigned long lastCleanup = 0;
    unsigned long currentTime = millis();

    if (currentTime - lastCleanup > 60000) {
        lastCleanup = currentTime;

        for (int i = 0; i < espNowClientCount; i++) {
            bool isOnline = (currentTime - espNowClients[i].lastSeen < 120000);

            if (espNowClients[i].paired) {
                if (!isOnline && espNowClients[i].registered) {
                    espNowClients[i].registered = false;
                }
            } else {
                if (!isOnline) {

                    esp_now_del_peer(espNowClients[i].mac);

                    for (int j = i; j < espNowClientCount - 1; j++) {
                        espNowClients[j] = espNowClients[j + 1];
                    }
                    espNowClientCount--;
                    i--;
                }
            }
        }
    }
}

// ==================== BROADCAST AND SYNC ====================

// New helper function to get live data efficiently
void getLiveLTCStatus() {
    // Poll twice: first poll triggers live data prep, second retrieves it.
    pollLTCStatusLive();
    delayMicroseconds(500); // Wait for LTC device to prepare the data
    pollLTCStatusLive();
}

// ==================== ATOMIC MANUAL SYNC ====================
void manualSyncAllClients() {
    bool diagReset = diagResetReq;   // consumed here even if this sync is skipped
    diagResetReq = false;
    if (mfwBusy()) return;  // master firmware update in progress

    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        return;
    }

    psGate(25);   // power save: start right at the beginning of a client window (no-op otherwise)

    // Wait for frame boundary for sub-frame precision (~0.02ms accuracy)
    waitForFrameBoundary(currentTimecode.fps_thousandths);

    // Get current timecode immediately after frame boundary using the new helper
    getLiveLTCStatus();

    // TIMING FIX: Send to ESP-NOW clients FIRST, then Master LTC
    Timecode clientTimecode = currentTimecode;

    struct_message syncMsg2;
    memset(&syncMsg2, 0, sizeof(syncMsg2));
    SET_MSG_TYPE(syncMsg2, "SYNC");
    syncMsg2.hours = clientTimecode.hours;
    syncMsg2.minutes = clientTimecode.minutes;
    syncMsg2.seconds = clientTimecode.seconds;
    syncMsg2.frames = clientTimecode.frames;
    syncMsg2.fps_thousandths = clientTimecode.fps_thousandths;
    syncMsg2.dropFrame = clientTimecode.dropFrame;
    syncMsg2.running = clientTimecode.running;
    syncMsg2.timestamp = millis();
    syncMsg2.masterTimestamp = millis();
    syncMsg2.sampleRate = currentSampleRate;
    if (diagReset) syncMsg2.uptime = DIAG_RESET_SYNC_MARK;   // clients restart their clock check
    buildUserBits(syncMsg2.userBits, "00");

    if (wirelessEnabled) {
        for (int i = 0; i < espNowClientCount; i++) {
            if (espNowClients[i].paired) {
                esp_now_send(espNowClients[i].mac, (uint8_t *) &syncMsg2, sizeof(syncMsg2));
                delayMicroseconds(100);
            }
        }
    }

    delayMicroseconds(500);

    // SPI packet: original timecode for master
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_IMMEDIATE_SYNC;
    if (diagReset) spiCmd._padding[0] = 0xC5;   // master LTC restarts its clock check
    spiCmd.hours = currentTimecode.hours;
    spiCmd.minutes = currentTimecode.minutes;
    spiCmd.seconds = currentTimecode.seconds;
    spiCmd.frames = currentTimecode.frames;
    spiCmd.fps_thousandths = currentTimecode.fps_thousandths;
    spiCmd.dropFrame = currentTimecode.dropFrame;
    spiCmd.running = currentTimecode.running;
    spiCmd.sampleRate = currentSampleRate;
    buildUserBits(spiCmd.userBits, masterCamId);

    portENTER_CRITICAL(&spiMux);
    digitalWrite(SPI_CS, LOW);
    delayMicroseconds(100);
    hspi.transferBytes((uint8_t*)&spiCmd, (uint8_t*)&spiStatus, SPI_PACKET_SIZE);
    digitalWrite(SPI_CS, HIGH);
    portEXIT_CRITICAL(&spiMux);

    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            espNowClients[i].needsInitialSync = false;
            espNowClients[i].lastSyncSent = millis();
        }
    }

    lastTimecodeSetTime = millis();
    lastESPNowSend = millis();

    sendWebSocketData();
}
void broadcastRunningState() {
    // Send to clients FIRST for better timing alignment
    // ESP-NOW transmission takes ~1-2ms, by the time SPI completes
    // clients will already be processing their RUN command
    sendESPNowRunState();
    sendRunningStateToLTCDevice(currentTimecode.running);
}

void broadcastLockCommand() {

    // Create lock command packet
    struct {
        uint8_t command;  // 0xFF = LOCK command
    } lockPacket;

    lockPacket.command = 0xFF;  // Special command byte for LOCK

    // Send to all paired clients. With power save this goes through the queue (sent after the next two beacons); the master
    // keeps its radio on long enough for that (see handleLockStateChange).
    for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired) {
            espSendClient(espNowClients[i].mac, (uint8_t*)&lockPacket, sizeof(lockPacket));
        }
    }
}

void lockMasterLTCDevice() {
    if (otaMode) return;  // never clock lock commands into a device being flashed

    // Send lock command via SPI
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_LOCK_DEVICE;
    spiCmd.running = 1;  // Use running field as lock flag (1 = lock)

    performSPITransaction();

    ltcSystemLocked = true;
}

void checkAutoSync() {
    static unsigned long lastCheck = 0;
    if (millis() - lastCheck < SYNC_CHECK_INTERVAL) return;
    lastCheck = millis();

    // Don't perform auto-sync when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked || otaMode) {
        // Still update lastAutoSyncTime to prevent immediate sync when exiting LOCKED mode
        if (syncIntervalMinutes > 0) {
            unsigned long intervalMs = syncIntervalMinutes * 60000UL;
            if (millis() - lastAutoSyncTime >= intervalMs) {
                lastAutoSyncTime = millis();
            }
        }
        return;
    }

    // Never auto-sync inside the post-FPS-change stabilization window -
    // manualSyncAllClients would sample the LTC mid-transition (same gate as
    // the deferred manual sync path)
    if (pendingFpsResync) return;

    if (syncIntervalMinutes > 0) {
        unsigned long intervalMs = syncIntervalMinutes * 60000UL;
        if (millis() - lastAutoSyncTime >= intervalMs) {
            // Sync clients TO master's current timecode (master doesn't jump)
            // Previously used syncFromRTC which synced to RTC and made master jump
            manualSyncAllClients();
            lastAutoSyncTime = millis();
        }
    }
}

void checkSyncButton() {
    // Block button input when LTC device is locked
    if (ltcSystemLocked) return;

    static bool lastButtonState = HIGH;
    static unsigned long buttonPressStartTime = 0;
    static bool syncTriggered = false;
    const unsigned long longPressTime = 3000;  // 3 seconds

    bool buttonState = digitalRead(SYNC_BUTTON_PIN);

    if (buttonState == LOW && lastButtonState == HIGH) {
        // Button just pressed
        buttonPressStartTime = millis();
        syncTriggered = false;
    }

    if (buttonState == LOW && !syncTriggered) {
        // Button is being held down
        if (millis() - buttonPressStartTime >= longPressTime) {
            // Button held for 3+ seconds. Skip while the FPS stabilization
            // window is open (retries next loop; syncTriggered stays false).
            if (!pendingFpsResync) {
                diagResetReq = true;
                manualSyncAllClients();  // Perform atomic frame-aligned manual sync
                syncTriggered = true;  // Prevent multiple triggers while held
            }
        }
    }

    lastButtonState = buttonState;
}

// ==================== WEBSOCKET FUNCTIONS ====================
// The library's list of connected browsers is not locked: it is changed by the web server task
// (connect / disconnect) while loop() walks it for every broadcast. A browser disconnecting in the
// middle of a broadcast was freed while loop() was still writing a message into it -> heap
// corruption (the main page was then sent with garbage). Every access from our code now holds
// wsMutex, and the DISCONNECT event (raised before the client's memory is released) waits for it.
// Each browser also gets at most WS_MAX_BACKLOG queued messages: a slow/sleeping browser can no
// longer pile up ~100 KB of queued updates.
SemaphoreHandle_t wsMutex = nullptr;
const size_t WS_MAX_BACKLOG = 4;
struct WsLock {
    WsLock() { if (wsMutex) xSemaphoreTakeRecursive(wsMutex, portMAX_DELAY); }
    ~WsLock() { if (wsMutex) xSemaphoreGiveRecursive(wsMutex); }
};
size_t wsCount() {
    WsLock l;
    return ws.count();
}
void wsBroadcast(const char* text) {
    size_t n = strlen(text);
    if (!n) return;
    WsLock l;
    AsyncWebSocketSharedBuffer buf;   // one copy shared by every browser
    for (auto& c : ws.getClients()) {
        if (c.status() != WS_CONNECTED || c.queueLen() >= WS_MAX_BACKLOG) continue;   // busy browser: skip this update
        if (!buf) buf = std::make_shared<std::vector<uint8_t>>((const uint8_t*)text, (const uint8_t*)text + n);
        c.text(buf);
    }
}

void onWebSocketEvent(AsyncWebSocket *server, AsyncWebSocketClient *client, AwsEventType type, void *arg, uint8_t *data, size_t len) {
  switch (type) {
    case WS_EVT_CONNECT:
      if (wsCount() >= MAX_WEBSOCKET_CLIENTS) {
        client->close();
      } else {
        sendWebSocketData();
        sendWebSocketPairingUpdate();
      }
      break;

    case WS_EVT_DISCONNECT:
      { WsLock l; }   // the client is freed after this returns: let a running broadcast finish first
      break;

    case WS_EVT_DATA:
      {
        AwsFrameInfo *info = (AwsFrameInfo*)arg;
        if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {
          if (len < 255) {
            char* message = (char*)malloc(len + 1);
            if (message) {
              memcpy(message, data, len);
              message[len] = 0;
              String messageStr = String(message);
              free(message);

              handleWebSocketMessage(messageStr, client);
            }
          }
        }
      }
      break;

    case WS_EVT_ERROR:
      break;

    case WS_EVT_PONG:
      break;
  }
}

void handleWebSocketMessage(String message, AsyncWebSocketClient *client) {
  // Handle PING immediately without debounce check
  if (message == "PING") {
    client->text("PONG");
    return;
  }

  // Block all commands when LTC device is locked (except PING for connection
  // health) and during OTA mode (stale/reconnected UIs must not drive SPI or
  // ESP-NOW while firmware is being flashed)
  if (ltcSystemLocked || otaMode) {
    return;
  }

  // Debounce check ONLY for actual user commands
  if (millis() - lastCommandTime < COMMAND_DEBOUNCE) {
        return;
  }
  lastCommandTime = millis();

  if (message.startsWith("CMD:")) {
    String command = message.substring(4);

    if (command == "start") {
      currentTimecode.running = true;
      lastRunningStateSetTime = millis();  // Track when running state changed
      saveSettings();
      broadcastRunningState();
      sendWebSocketData();  // Immediately update webpage
      lastFullWSUpdate = millis();  // Reset timer to prevent immediate duplicate update
      lastWSUpdate = millis();
    } else if (command == "stop") {
      currentTimecode.running = false;
      lastRunningStateSetTime = millis();  // Track when running state changed
      saveSettings();
      broadcastRunningState();
      sendWebSocketData();  // Immediately update webpage
      lastFullWSUpdate = millis();  // Reset timer to prevent immediate duplicate update
      lastWSUpdate = millis();
    } else if (command == "syncall") {
      // Check if there are any paired clients to sync
      int pairedCount = 0;
      for (int i = 0; i < espNowClientCount; i++) {
          if (espNowClients[i].paired) pairedCount++;
      }
      if (pairedCount == 0) {
          client->text("WARN:No paired clients to sync");
      }
      pendingManualSync = true; diagResetReq = true;  // Executed by loop() - keeps async_tcp task unblocked
      lastFullWSUpdate = millis();  // Reset timer
      lastWSUpdate = millis();
    } else if (command == "syncfromrtc") {

      // syncFromRTC() waits for the RTC second tick (up to 1.1 s): executed by loop() - keeps async_tcp task unblocked
      pendingRtcSync = true;
      lastFullWSUpdate = millis();
      lastWSUpdate = millis();
    } else if (command == "lockall") {
      
      //broadcastSyncESPNow(true);  // Fast, non-blocking, ESP-NOW only (clients get update)
      //manualSyncAllClients();
      // 2. Lock master LTC device via SPI
      lockMasterLTCDevice();

      // 3. Broadcast lock command to all ESP-NOW clients
      broadcastLockCommand();

      // Note: Wireless will disable automatically after 500ms delay (handled in utilityTask)
      sendWebSocketData();
      lastFullWSUpdate = millis();
      lastWSUpdate = millis();
    }

  } else if (message.startsWith("SET_FPS:")) {
    String fpsData = message.substring(8);
    int colonIndex = fpsData.indexOf(':');
    if (colonIndex != -1) {
        String fpsStr = fpsData.substring(0, colonIndex);
        bool dropFrame = fpsData.substring(colonIndex + 1).toInt() == 1;

        float fps = fpsStr.toFloat();

        uint16_t fpsThousandths;
        if (fpsStr == "23.976") {
            fpsThousandths = 23976;
        } else if (fpsStr == "29.97") {
            fpsThousandths = 29970;
        } else {
            fpsThousandths = (uint16_t)(fps * 1000);
        }

        currentTimecode.fps_thousandths = fpsThousandths;
        currentTimecode.dropFrame = dropFrame && (fpsThousandths == 29970);
        lastFpsSetTime = millis();  // Track when FPS was changed
        saveSettings();

        sendFPSChangeToLTCDevice();

        // Defer the resync instead of blocking the async WebSocket task with delay().
        // The LTC device + Si5351 need ~2 frame times to stabilize at the new rate;
        // loop() runs broadcastSyncESPNow() once pendingFpsResyncTime is reached.
        float newFpsFloat = getFpsFloat(fpsThousandths);
        uint32_t stabilizeMs = (uint32_t)(2000.0f / newFpsFloat) + 10;  // 2 frames + margin
        pendingFpsResyncTime = millis() + stabilizeMs;  // time BEFORE flag (see decl)
        pendingFpsResync = true;

        sendWebSocketData();  // Immediately reflect the new FPS on the webpage
        lastFullWSUpdate = millis();  // Reset timer to prevent immediate duplicate update
        lastWSUpdate = millis();
    }
  } else if (message.startsWith("SET_TIMECODE:")) {
    String tcData = message.substring(13);

    // Parse timecode components
    int parts[4];
    int lastIndex = 0;
    for (int i = 0; i < 4; i++) {
      int colonIndex = tcData.indexOf(':', lastIndex);
      if (colonIndex == -1) colonIndex = tcData.length();
      parts[i] = tcData.substring(lastIndex, colonIndex).toInt();
      lastIndex = colonIndex + 1;
    }

    // Set timecode immediately
    currentTimecode.hours = parts[0];
    currentTimecode.minutes = parts[1];
    currentTimecode.seconds = parts[2];
    currentTimecode.frames = parts[3];

    // Validate drop frame: skip frames 0,1 at minute boundaries (except every 10th minute)
    if (currentTimecode.dropFrame && currentTimecode.fps_thousandths == 29970 &&
        currentTimecode.seconds == 0 && currentTimecode.frames < 2 &&
        (currentTimecode.minutes % 10) != 0) {
      currentTimecode.frames = 2;
    }
    lastTimecodeSetTime = millis();  // Protect from SPI overwrite

    // Push the new timecode to the LTC device first (fast SPI write, same order
    // as the /settimecode HTTP endpoint), then let loop() run the frame-aligned
    // client sync off the async_tcp task.
    sendSyncToLTCDevice();
    pendingManualSync = true; diagResetReq = true;

    saveSettings();
    lastFullWSUpdate = millis();  // Reset timer to prevent immediate duplicate update
    lastWSUpdate = millis();
  } else if (message.startsWith("SET_SYNC:")) {
    String intervalStr = message.substring(9);
    syncIntervalMinutes = intervalStr.toInt();
    saveSyncSettings();
    sendEspNowStatusToLTCDevice();
    sendWebSocketData();  // Immediately update webpage
    lastFullWSUpdate = millis();  // Reset timer to prevent immediate duplicate update
    lastWSUpdate = millis();
  } else if (message.startsWith("SET_RTC_AT:")) {
    // SET_RTC_AT:year:month:day:hour:minute:second:waitMs  (the time given is that of the second that starts in waitMs)
    String d = message.substring(11);
    long v[7];
    int idx = 0, last = 0, colons = 0;
    for (unsigned k = 0; k < d.length(); k++) if (d[k] == ':') colons++;
    for (int i = 0; i < 7 && colons == 6; i++) {
      int c = d.indexOf(':', last);
      if (c == -1) c = d.length();
      v[i] = d.substring(last, c).toInt();
      last = c + 1;
      idx++;
    }
    if (idx == 7 && v[6] >= 0 && v[6] <= 2500 && !rtcAtBusy && rtcValid(v[0], v[1], v[2], v[3], v[4], v[5])) {
      rtcAtReq = { (int)v[0], (int)v[1], (int)v[2], (int)v[3], (int)v[4], (int)v[5], esp_timer_get_time() + v[6] * 1000LL };
      rtcAtBusy = true;
      if (xTaskCreate(rtcAtTask, "rtcAt", 12288, nullptr, 3, nullptr) != pdPASS) rtcAtBusy = false;
    }
  } else if (message.startsWith("SET_RTC:")) {
    String rtcData = message.substring(8);
    int parts[6];
    int lastIndex = 0;
    for (int i = 0; i < 6; i++) {
      int colonIndex = rtcData.indexOf(':', lastIndex);
      if (colonIndex == -1) colonIndex = rtcData.length();
      parts[i] = rtcData.substring(lastIndex, colonIndex).toInt();
      lastIndex = colonIndex + 1;
    }

    int colons = 0;
    for (unsigned k = 0; k < rtcData.length(); k++) if (rtcData[k] == ':') colons++;
    if (colons == 5 && setRTC(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])) {
      broadcastSyncESPNow(true);  // Manual RTC update - needs fresh data
    }
    sendWebSocketData();  // Immediately update webpage
  } else if (message.startsWith("PAIRING:")) {
    String pairingCmd = message.substring(8);
    if (pairingCmd == "start") {
      startPairingMode();
    } else if (pairingCmd == "stop") {
      stopPairingMode();
    } else if (pairingCmd.startsWith("pair:")) {
      String macStr = pairingCmd.substring(5);
      uint8_t mac[6];
      unsigned int tempMac[6];
      if (sscanf(macStr.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
                 &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)tempMac[i];
        pairClient(mac);
      }
    } else if (pairingCmd.startsWith("unpair:")) {
      String macStr = pairingCmd.substring(7);
      uint8_t mac[6];
      unsigned int tempMac[6];
      if (sscanf(macStr.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
                 &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)tempMac[i];
        unpairClient(mac);
      }
    }
  }
  // PING is now handled at the top of this function
}

void sendWebSocketData() {
    if (wsCount() == 0) return;

    // Rate limit this function
    static unsigned long lastFullUpdate = 0;
    if (millis() - lastFullUpdate < 1000) {
        return;
    }
    lastFullUpdate = millis();

    // Poll LTC device for current status (but skip if we recently set timecode to avoid stale data)
    if (millis() - lastTimecodeSetTime > STATE_PROTECT_MS) {
        pollLTCStatus();
    }
    DateTime now = rtcNow();
    unsigned long masterUptime = (millis() - systemStartTime) / 1000;

    // Determine lock status and label
    String lockStatus;
    String lockLabel;
    bool isLocked = (ltcSystemLocked || ltcJamSyncMode == JAM_SYNC_LOCKED);

    if (ltcJamSyncMode == JAM_SYNC_SEARCHING) {
        lockStatus = "SEARCHING";
        lockLabel = "Searching for external LTC signal...";
    } else if (isLocked) {
        lockStatus = "LOCKED";
        lockLabel = "Device is locked. All settings and changes are blocked. Unlock or restart the device to make changes.";
    } else {
        lockStatus = "UNLOCKED";
        lockLabel = "All changes will initiate to all unlocked device(s).";
    }

    char fpsString[10];
    switch(currentTimecode.fps_thousandths) {
        case 23976: strcpy(fpsString, "23.976"); break;
        case 24000: strcpy(fpsString, "24.000"); break;
        case 25000: strcpy(fpsString, "25.000"); break;
        case 29970: strcpy(fpsString, "29.97"); break;
        case 30000: strcpy(fpsString, "30.000"); break;
        default: snprintf(fpsString, sizeof(fpsString), "%.3f", getFpsFloat(currentTimecode.fps_thousandths));
    }

    // Use a static buffer to avoid stack allocation and fragmentation
    static char jsonBuffer[1536];
    char* p = jsonBuffer;
    char* end = jsonBuffer + sizeof(jsonBuffer);

    p += snprintf(p, end - p, "{\"type\":\"full_update\",\"calibration\":{\"calibrated\":%s,\"correction\":%ld},",
                  si5351_calibrated ? "true" : "false", si5351_correction);

    p += snprintf(p, end - p, "\"timecode\":{\"hours\":%d,\"minutes\":%d,\"seconds\":%d,\"frames\":%d,\"fps\":\"%s\",\"dropFrame\":%s,\"running\":%s},",
                  currentTimecode.hours, currentTimecode.minutes, currentTimecode.seconds, currentTimecode.frames,
                  fpsString, currentTimecode.dropFrame ? "true" : "false", currentTimecode.running ? "true" : "false");

    p += snprintf(p, end - p, "\"rtc\":{\"year\":%d,\"month\":%d,\"day\":%d,\"hour\":%d,\"minute\":%d,\"second\":%d,\"warn\":%d},",
                  now.year(), now.month(), now.day(), now.hour(), now.minute(), now.second(), (int)rtcWarn);

    p += snprintf(p, end - p, "\"sync\":{\"interval\":%d},\"clients\":%d,\"uptime\":%lu,\"battery\":%d,\"batteryPresent\":%s,\"audioLevel\":\"%s\",",
                  syncIntervalMinutes, espNowClientCount, masterUptime, ltcBatteryLevel,
                  ltcBatteryPresent ? "true" : "false", ltcIsLineLevel ? "LINE" : "MIC");
                  
    p += snprintf(p, end - p, "\"lockStatus\":\"%s\",\"lockLabel\":\"%s\",", lockStatus.c_str(), lockLabel.c_str());

    p += snprintf(p, end - p, "\"masterUtilityVersion\":\"%d.%d.%d\",\"masterLtcVersion\":\"%d.%d.%d\","
                              "\"radio\":{\"tx\":%d,\"rate\":%d,\"pending\":%s,\"hw\":%d,\"ps\":%d,\"lofs\":%d}}",
                  FIRMWARE_VERSION_MAJOR, FIRMWARE_VERSION_MINOR, FIRMWARE_VERSION_PATCH,
                  spiStatus.firmwareVersionMajor, spiStatus.firmwareVersionMinor, spiStatus.firmwareVersionPatch,
                  radioTxIdx, radioRateIdx, radioApplyPending ? "true" : "false", (int)radioHwNow(), psOn ? 1 : 0, (int)masterLtcOffset);

    wsBroadcast(jsonBuffer);
}

void sendWebSocketTimecodeUpdate() {
    if (wsCount() == 0) return;

    // Poll LTC device for current status (but skip if we recently set timecode to avoid stale data)
    // Note: Only timecode needs protection - utility is master for FPS and running state
    unsigned long timeSinceTimecodeSet = millis() - lastTimecodeSetTime;
    if (timeSinceTimecodeSet > STATE_PROTECT_MS) {
        pollLTCStatus();
    }
    // No debug message here since this runs every 200ms
    DateTime now = rtcNow();

    unsigned long masterUptime = (millis() - systemStartTime) / 1000;

    // Determine lock status and label
    String lockStatus;
    String lockLabel;

    // Determine lock status based on jam sync mode and manual lock
    if (ltcJamSyncMode == JAM_SYNC_SEARCHING) {
        // Special case: searching mode
        lockStatus = "SEARCHING";
        lockLabel = "Searching for external LTC signal...";
    } else if (ltcSystemLocked || ltcJamSyncMode == JAM_SYNC_LOCKED) {
        // Either manually locked or jam sync locked - display as LOCKED
        lockStatus = "LOCKED";
        lockLabel = "Device is locked. All settings and changes are blocked. Unlock or restart the device to make changes.";
    } else {
        lockStatus = "UNLOCKED";
        lockLabel = "All changes will initiate to all unlocked device(s).";
    }

    char json[512];
    snprintf(json, sizeof(json),
        "{\"type\":\"timecode_update\",\"hours\":%d,\"minutes\":%d,\"seconds\":%d,\"frames\":%d,"
        "\"running\":%s,\"rtc_hour\":%d,\"rtc_minute\":%d,\"rtc_second\":%d,"
        "\"rtc_year\":%d,\"rtc_month\":%d,\"rtc_day\":%d,\"uptime\":%lu,"
        "\"lockStatus\":\"%s\",\"lockLabel\":\"%s\"}",
        currentTimecode.hours, currentTimecode.minutes, currentTimecode.seconds, currentTimecode.frames,
        currentTimecode.running ? "true" : "false",
        now.hour(), now.minute(), now.second(),
        now.year(), now.month(), now.day(),
        masterUptime, lockStatus.c_str(), lockLabel.c_str());

    wsBroadcast(json);
}

void sendWebSocketRTCUpdate() {
  if (wsCount() == 0) return;

  DateTime now = rtcNow();

  char json[200];
  snprintf(json, sizeof(json),
    "{\"type\":\"rtc_update\",\"hour\":%d,\"minute\":%d,\"second\":%d}",
    now.hour(), now.minute(), now.second());

  wsBroadcast(json);
}

void sendWebSocketClientsUpdate() {
  if (wsCount() == 0) return;

  int pairedCount = 0;
  for (int i = 0; i < espNowClientCount; i++) {
    if (espNowClients[i].paired) {
      pairedCount++;
    }
  }

  char json[100];
  snprintf(json, sizeof(json),
    "{\"type\":\"clients_update\",\"count\":%d}",
    pairedCount);

  wsBroadcast(json);
}

void sendWebSocketClientsDetailed() {
    if (wsCount() == 0) return;

    // Use a large static buffer to build the JSON.
    // This is the most memory-efficient way for complex JSON on ESP32.
    // Size is estimated: 256 bytes per client * 10 clients + 256 bytes for wrapper = ~2.8KB
    static char jsonBuffer[4608];   // ~370 bytes per client x 10
    char* p = jsonBuffer;
    char* end = jsonBuffer + sizeof(jsonBuffer);

    p += snprintf(p, end - p, "{\"type\":\"clients_detailed\",\"clients\":[");

    for (int i = 0; i < espNowClientCount; i++) {
        if (p >= end) break; // Prevent buffer overflow

        unsigned long timeSinceSeen = millis() - espNowClients[i].lastSeen;
        bool isOnline = (timeSinceSeen < 120000);

        char fpsString[10];
        switch(espNowClients[i].fps_thousandths) {
            case 23976: strcpy(fpsString, "23.976"); break;
            case 24000: strcpy(fpsString, "24.000"); break;
            case 25000: strcpy(fpsString, "25.000"); break;
            case 29970: strcpy(fpsString, "29.97"); break;
            case 30000: strcpy(fpsString, "30.000"); break;
            default: snprintf(fpsString, sizeof(fpsString), "%.3f", getFpsFloat(espNowClients[i].fps_thousandths));
        }

        const char* jamSyncModeStr;
        switch(espNowClients[i].jamSyncMode) {
            case JAM_SYNC_DISABLED: jamSyncModeStr = "DISABLED"; break;
            case JAM_SYNC_SEARCHING: jamSyncModeStr = "SEARCHING"; break;
            case JAM_SYNC_LOCKED: jamSyncModeStr = "LOCKED"; break;
            case JAM_SYNC_FREERUN: jamSyncModeStr = "FREERUN"; break;
            default: jamSyncModeStr = "UNKNOWN"; break;
        }

        const char* statusStr;
        if (espNowClients[i].paired) {
            statusStr = isOnline ? "Paired & Online" : "Paired (Offline)";
        } else {
            statusStr = "Not Paired";
        }

        int rssiShown = (espNowClients[i].rssiAt && millis() - espNowClients[i].rssiAt < 20000) ? espNowClients[i].rssi : 0;   // 0 = not heard recently
        p += snprintf(p, end - p, "%s{\"mac\":\"%s\",\"name\":\"%s\",\"online\":%s,\"synced\":%s,\"running\":%s,\"paired\":%s,"
                                  "\"timecode\":\"%02d:%02d:%02d:%02d\",\"fps\":\"%s\",\"lastSeen\":%lu,\"uptime\":%lu,"
                                  "\"battery\":%d,\"batteryPresent\":%s,\"jamSync\":\"%s\","
                                  "\"utilityVersion\":\"%d.%d.%d\",\"ltcVersion\":\"%d.%d.%d\",\"status\":\"%s\","
                                  "\"rssi\":%d,\"rtx\":%d,\"rrate\":%d,\"rv\":%s,\"hw\":%d,\"rssiM\":%d,\"rps\":%d,\"lofs\":%d}",
                      (i > 0) ? "," : "",
                      espNowClients[i].macString.c_str(),
                      strlen(espNowClients[i].deviceName) > 0 ? espNowClients[i].deviceName : espNowClients[i].macString.c_str(),
                      isOnline ? "true" : "false",
                      espNowClients[i].synced ? "true" : "false",
                      espNowClients[i].running ? "true" : "false",
                      espNowClients[i].paired ? "true" : "false",
                      espNowClients[i].hours, espNowClients[i].minutes, espNowClients[i].seconds, espNowClients[i].frames,
                      fpsString,
                      timeSinceSeen / 1000,
                      espNowClients[i].uptime,
                      espNowClients[i].batteryLevel,
                      espNowClients[i].batteryPresent ? "true" : "false",
                      jamSyncModeStr,
                      espNowClients[i].utilityVersionMajor, espNowClients[i].utilityVersionMinor, espNowClients[i].utilityVersionPatch,
                      espNowClients[i].ltcVersionMajor, espNowClients[i].ltcVersionMinor, espNowClients[i].ltcVersionPatch,
                      statusStr,
                      rssiShown, espNowClients[i].radioTx, espNowClients[i].radioRate, espNowClients[i].radioValid ? "true" : "false",
                      (int)espNowClients[i].radioHw, (int)espNowClients[i].rssiFromMaster,
                      espNowClients[i].radioPsValid ? (int)espNowClients[i].radioPs : 2,
                      espNowClients[i].ltcOfsValid ? (int)espNowClients[i].ltcOfs : 99);
    }
    
    if (p < end) {
        p += snprintf(p, end - p, "]}");
    }

    wsBroadcast(jsonBuffer);
}

void setupWebSocket() {
  if (!wsMutex) wsMutex = xSemaphoreCreateRecursiveMutex();
  ws.onEvent(onWebSocketEvent);
  server.addHandler(&ws);
}

// ==================== HTML PAGE ====================
// Full web UI (minified CSS/JS), served on "/" in normal mode.

const char HTML_PAGE[] PROGMEM = R"=====(
<!DOCTYPE HTML>
<html>
<head>
  <title>Timecode Master</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script>(function(){var d=false;function chk(){if(d)return;d=true;try{if(document.getElementById('pageEnd')){sessionStorage.removeItem('pgTry');return}var n=+(sessionStorage.getItem('pgTry')||0);if(n<4){sessionStorage.setItem('pgTry',n+1);setTimeout(function(){location.reload()},1500+n*1000)}}catch(e){}}window.addEventListener('load',chk);setTimeout(chk,9000)})()</script>
  <style>:root{--primary:#7850c8;--primary-dark:#6340a8;--secondary:#5078c8;--success:#00ff88;--warning:#ffc850;--danger:#ff5050;--text:#e5e5e5;--text-muted:#777;--card-bg:rgba(10, 10, 20, 0.6);--border:rgba(120, 80, 200, 0.15)}*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',sans-serif;background:linear-gradient(to bottom,#0a0a0a 0%,#1a1520 50%,#0a0a0a 100%);color:var(--text);min-height:100vh;line-height:1.6;position:relative;overflow-x:hidden}body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(circle at 20% 30%,rgb(120 80 200 / .08) 0%,transparent 50%),radial-gradient(circle at 80% 60%,rgb(80 120 200 / .06) 0%,transparent 50%),radial-gradient(circle at 50% 80%,rgb(150 100 180 / .05) 0%,transparent 50%);pointer-events:none;z-index:0}.container{max-width:1400px;margin:0 auto;padding:20px 40px;position:relative;z-index:1}header{text-align:center;margin-bottom:40px;padding:40px 20px 30px;border-bottom:1px solid rgb(255 255 255 / .1);background:radial-gradient(ellipse at center,rgb(40 30 60 / .3) 0%,transparent 70%)}h1{font-size:3.5rem;font-weight:200;background:linear-gradient(135deg,#ffffff 0%,#888 100%);-webkit-background-clip:text;-webkit-text-fill-color:#fff0;background-clip:text;margin-bottom:15px;letter-spacing:-2px;line-height:1.1}.subtitle{color:#999;font-size:1.2rem;font-weight:300;letter-spacing:.5px}.connection-status{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 20px;border-radius:20px;font-size:.85rem;font-weight:600;margin-top:10px;border:2px solid}.status-connected{background:rgb(0 255 136 / .2);border-color:rgb(0 255 136 / .5);color:#fff}.status-disconnected{background:rgb(255 80 80 / .2);border-color:rgb(255 80 80 / .5);color:#fff}.status-connecting{background:rgb(255 200 80 / .2);border-color:rgb(255 200 80 / .5);color:#fff}.status-indicator{width:8px;height:8px;border-radius:50%;animation:pulse 2s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}.dashboard{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:30px}@media (max-width:768px){*{box-sizing:border-box}body{overflow-x:hidden;margin:0;padding:0}.container{padding:15px;max-width:100vw;width:100%;margin:0;box-sizing:border-box}header{padding:20px 0;margin:0 0 20px 0;text-align:center;width:100%;box-sizing:border-box}h1{font-size:2.5rem;padding:0;margin:0;text-align:center;width:100%}.subtitle{padding:0;margin:0;text-align:center;width:100%}.connection-status{margin-left:auto;margin-right:auto;display:inline-flex}.dashboard{display:grid!important;grid-template-columns:1fr!important;grid-template-rows:auto!important;gap:15px!important;margin:0!important;padding:0!important;width:100%!important;box-sizing:border-box!important}.card{padding:20px!important;border-radius:15px;margin:0!important;box-sizing:border-box!important;width:100%!important;max-width:100%!important;grid-column:1 / -1!important}.card:not(:last-child){margin-bottom:15px!important}.card-title{font-size:1.3rem}.ltc-time{font-size:2.5rem}.time-display{margin:15px 0}.control-grid{grid-template-columns:1fr 1fr}.client-info{grid-template-columns:1fr;gap:8px}.client-header{flex-direction:column;align-items:flex-start;gap:8px}.status-badge{align-self:flex-start}.stats-grid{grid-template-columns:1fr}}.card{background:var(--card-bg);border-radius:20px;padding:30px;border:1px solid var(--border);backdrop-filter:blur(10px);transition:all 0.4s ease}.card:hover{background:rgb(20 15 30 / .7);border-color:rgb(120 80 200 / .3);box-shadow:0 10px 40px rgb(120 80 200 / .15)}.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.card-title{font-size:1.6rem;font-weight:300;color:#fff;letter-spacing:-.5px}.status-badge{padding:6px 16px;border-radius:20px;font-size:.7rem;font-weight:500;text-transform:uppercase;letter-spacing:2px;border:1px solid}.status-running{background:rgb(0 255 136 / .15);border-color:rgb(0 255 136 / .3);color:var(--success)}.status-stopped{background:rgb(255 80 80 / .15);border-color:rgb(255 80 80 / .3);color:var(--danger)}.status-syncing,.status-warning{background:rgb(255 200 80 / .15);border-color:rgb(255 200 80 / .3);color:var(--warning)}.status-locked{background:rgb(255 80 80 / .15);border-color:rgb(255 80 80 / .3);color:var(--danger)}.status-unlocked{background:rgb(0 255 136 / .15);border-color:rgb(0 255 136 / .3);color:var(--success)}.status-freerun,.status-searching{background:rgb(120 80 200 / .15);border-color:rgb(120 80 200 / .3);color:var(--primary)}.time-display{font-family:'Monaco','Consolas',monospace;text-align:center;margin:20px 0}.ltc-time{font-size:3.5rem;font-weight:700;color:var(--success);text-shadow:0 0 20px rgb(0 255 136 / .3);line-height:1.2;transition:all 0.1s ease}.rtc-time{font-size:1.5rem;color:var(--text-muted);margin-top:10px}.rtc-date{font-size:1.2rem;color:var(--text-muted);margin-top:5px}.fps-display{font-size:1.1rem;color:var(--text-muted);text-align:center}.control-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:20px 0}.btn{padding:12px 24px;border:1px solid;border-radius:8px;font-size:.9rem;font-weight:500;cursor:pointer;transition:all 0.3s ease;display:flex;align-items:center;justify-content:center;gap:8px;text-transform:uppercase;letter-spacing:1px}.btn:hover:not(:disabled){transform:translateY(-2px)}.btn:active{transform:translateY(0)}.btn:disabled{opacity:.5;cursor:not-allowed;transform:none;position:relative}.btn.processing{opacity:.7;cursor:wait;pointer-events:none}.btn.processing::after{content:attr(data-countdown);position:absolute;right:10px;font-size:.8rem;font-weight:700;color:rgb(255 255 255 / .8)}.btn-primary{background:rgb(120 80 200 / .2);border-color:rgb(120 80 200 / .4);color:#fff}.btn-primary:hover:not(:disabled){background:rgb(120 80 200 / .4);border-color:rgb(120 80 200 / .6);box-shadow:0 4px 20px rgb(120 80 200 / .3)}.btn-success{background:rgb(0 255 136 / .15);border-color:rgb(0 255 136 / .3);color:var(--success)}.btn-success:hover:not(:disabled){background:rgb(0 255 136 / .25);border-color:rgb(0 255 136 / .5);box-shadow:0 4px 20px rgb(0 255 136 / .2)}.btn-danger{background:rgb(255 80 80 / .2);border-color:rgb(255 80 80 / .4);color:var(--danger)}.btn-danger:hover:not(:disabled){background:rgb(255 80 80 / .3);border-color:rgb(255 80 80 / .5);box-shadow:0 4px 20px rgb(255 80 80 / .3)}.btn-warning{background:rgb(255 200 80 / .15);border-color:rgb(255 200 80 / .3);color:var(--warning)}.btn-warning:hover:not(:disabled){background:rgb(255 200 80 / .25);border-color:rgb(255 200 80 / .5);box-shadow:0 4px 20px rgb(255 200 80 / .3)}.form-group{margin-bottom:20px}label{display:block;margin-bottom:8px;font-weight:600;color:var(--text)}select,input{width:100%;padding:12px;border:1px solid rgb(120 80 200 / .3);border-radius:8px;background:rgb(255 255 255 / .05);color:var(--text);font-size:.95rem;transition:all 0.3s ease}select:focus,input:focus{outline:none;border-color:rgb(120 80 200 / .6);background:rgb(255 255 255 / .08)}select option{background:#1a1a2a;color:#fff;padding:10px}.checkbox-group{display:flex;align-items:center;gap:10px;margin:15px 0}.checkbox-group input[type="checkbox"]{width:auto;transform:scale(1.2)}.clients-grid{display:grid;gap:15px;margin-top:20px}.client-card{background:rgb(255 255 255 / .02);border:1px solid rgb(80 120 200 / .3);border-radius:12px;padding:20px;transition:all 0.3s ease}.client-card:hover{background:rgb(255 255 255 / .04);border-color:rgb(80 120 200 / .5)}.client-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px;flex-wrap:wrap}.client-info{display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:.85rem}.client-timecode{font-family:'Monaco','Consolas',monospace;color:var(--success);font-weight:700}.client-mac{font-family:monospace;color:#5078c8;font-weight:500;font-size:.9rem;word-break:break-all;max-width:100%}.no-clients{text-align:center;padding:40px;color:var(--text-muted)}.sync-config{display:flex;gap:12px;align-items:end}.sync-input{flex:1}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-top:20px}.stat-card{background:rgb(255 255 255 / .05);border-radius:12px;padding:15px;text-align:center}.stat-value{font-size:1.8rem;font-weight:700;color:var(--primary);margin-bottom:5px}.stat-label{font-size:.85rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px}.flash{animation:flash 0.3s ease}@keyframes flash{0%,100%{opacity:1}50%{opacity:.7}}.toast{position:fixed;top:20px;right:20px;max-width:calc(100vw - 40px);overflow-wrap:anywhere;background:var(--success);color:#fff;padding:15px 20px;border-radius:12px;box-shadow:0 8px 32px rgb(0 0 0 / .3);transform:translateX(400px);transition:transform 0.3s ease;z-index:1000}.toast.show{transform:translateX(0)}.toast.error{background:var(--danger)}.toast.warning{background:var(--warning)}.pairing-section{margin-top:20px;padding-top:20px;border-top:1px solid var(--border)}.pairing-mode-active{border:2px solid rgb(255 200 80 / .5);animation:pulse-border 2s infinite}@keyframes pulse-border{0%,100%{border-color:rgb(255 200 80 / .5);box-shadow:0 0 20px rgb(255 200 80 / .1)}50%{border-color:rgb(120 80 200 / .5);box-shadow:0 0 30px rgb(120 80 200 / .2)}}.unpaired-client{background:rgb(255 200 80 / .1);border-color:rgb(255 200 80 / .3)}.paired-client{background:rgb(0 255 136 / .05);border-color:rgb(0 255 136 / .3)}.pairing-controls{display:flex;gap:10px;margin-bottom:15px}.unpaired-list{max-height:200px;overflow-y:auto;margin-top:10px}</style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Timecode Master</h1>
      <div class="subtitle">Professional LTC Timecode Generator & Sync System</div>
      <div id="connectionStatus" class="connection-status status-connecting">
        <div class="status-indicator"></div>
        <span>CONNECTING...</span>
      </div>
    </header>

    <div class="dashboard">
      <!-- Current Timecode Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">Live Timecode</div>
          <div id="status" class="status-badge status-unlocked">UNLOCKED</div>
        </div>
        <div class="time-display">
          <div id="currentTimecode" class="ltc-time">00:00:00:00</div>
          <div id="currentRTCTime" class="rtc-time">--:--:--</div>
          <div id="currentRTCDate" class="rtc-date">----/--/--</div>
          <div id="rtcWarn" style="display:none;margin-top:6px;padding:6px 10px;border-radius:6px;background:rgba(255,80,80,0.15);color:#ff5050;font-size:0.8rem"></div>
          <div id="fps-display" class="fps-display">25.000 fps</div>
          <div id="lockStatusLabel" class="lock-status-label" style="font-size: 0.85rem; color: #666; margin-top: 10px; padding: 8px; background: rgba(0,0,0,0.05); border-radius: 4px; text-align: center; display: none;"></div>
        </div>
        
        <div class="control-grid">
          <button type="button" class="btn btn-success" onclick="sendCommand('start')">
             START
          </button>
          <button type="button" class="btn btn-danger" onclick="sendCommand('stop')">
             STOP
          </button>
          <button type="button" class="btn btn-primary" onclick="sendCommand('syncall')">
             SYNC ALL
          </button>
          <button type="button" id="lockAllBtn" class="btn btn-warning" onclick="lockAllDevices()">
             LOCK ALL
          </button>
        </div>
        
        <div class="stats-grid">
          <div class="stat-card">
            <div id="connectedCount" class="stat-value">0</div>
            <div class="stat-label">Clients Connected</div>
          </div>
          <div class="stat-card">
            <div id="syncStatus" class="stat-value">OFF</div>
            <div class="stat-label">Auto Sync</div>
          </div>
          <div class="stat-card">
    <div id="masterUptime" class="stat-value">0s</div>
    <div class="stat-label">Master Uptime</div>
</div>
          <div class="stat-card">
            <div id="masterCamId" class="stat-value">00</div>
            <div class="stat-label">Master Cam ID</div>
            <button class="btn btn-secondary" onclick="editMasterCamId()" style="margin-top:8px;padding:6px 12px;font-size:0.8rem;">EDIT</button>
          </div>
        </div>
        <div style="margin-top:20px;padding:15px;background:rgba(255,255,255,0.02);border-radius:12px;border:1px solid var(--border);">
          <div style="font-size:0.9rem;color:var(--text-muted);margin-bottom:10px;">Master Firmware Versions</div>
          <div style="display:flex;gap:20px;font-size:0.85rem;">
            <div><strong>Utility:</strong> <span id="masterUtilityVersion">--</span></div>
            <div><strong>LTC:</strong> <span id="masterLtcVersion">--</span></div>
            <div><strong>LTC out offset:</strong> <span id="masterLtcOfs">0 fr</span> <a href="#" style="color:var(--secondary)" onclick="return setLtcOfs('master','the master',masterLtcOfsVal)">set</a></div>
          </div>
        </div>
      </div>

      <!-- Configuration Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">Configuration</div>
        </div>
        
        <div class="form-group">
          <div class="form-group">
            <label for="fps">Frame Rate</label>
            <select id="fps" name="fps" onchange="pendingFpsChange=true; updateFPSPreview(); updateFrameLimit();">
              <option value="23.976">23.976 fps (Film)</option>
              <option value="24">24.000 fps</option>
              <option value="25">25.000 fps (PAL)</option>
              <option value="29.97">29.97 fps (NTSC)</option>
              <option value="30">30.000 fps</option>
            </select>
          </div>

          <div class="checkbox-group">
            <input type="checkbox" id="dropframe" name="dropframe" value="1" onchange="pendingFpsChange=true; updateFPSPreview()">
            <label for="dropframe">Enable Drop Frame (29.97fps only)</label>
          </div>

          <div class="form-group">
            <label>Preview: <span id="fpsPreview">25.000 fps</span></label>
            <button type="button" class="btn btn-primary" onclick="setFPS()" style="width: 100%; margin-top: 10px;">
               APPLY FPS SETTINGS
            </button>
          </div>

        </div>
      </div>
    </div>

    <div class="dashboard">
      <!-- RTC & Sync Settings -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">RTC & Auto-Sync</div>
        </div>
        
        <div class="form-group">
          <label>RTC Time Control</label>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;">
            <button type="button" class="btn btn-primary" onclick="syncFromRTC()">
               SYNC FROM RTC
            </button>
            <button type="button" class="btn btn-warning" onclick="showRTCDialog()">
             SET RTC
            </button>
          </div>
        </div>
        
        <!-- Auto-Sync Interval - temporarily hidden
        <div class="form-group">
          <label>Auto-Sync Interval</label>
          <div class="sync-config">
            <div class="sync-input">
              <input type="number" id="syncInterval" min="0" max="1440" value="0" placeholder="Minutes">
            </div>
            <button class="btn btn-primary" onclick="setSyncConfig()">
               SET
            </button>
          </div>
          <div style="font-size: 0.85rem; color: var(--text-muted); margin-top: 8px;">
            Set to 0 to disable auto-sync. Current: <span id="currentSyncInterval">0</span> minutes
          </div>
        </div>
        -->
      </div>

      <!-- Connected Clients -->
      <div class="card" id="clientsCard">
        <div class="card-header">
          <div class="card-title">Connected Clients</div>
          <div class="status-badge" id="clientCount">0 clients</div>
        </div>
        
        <div id="clientsList" class="clients-grid">
          <div class="no-clients">
               <div>No clients connected</div>
            <div style="font-size: 0.9rem; margin-top: 5px;">Clients will appear here when they connect</div>
          </div>
        </div>

        <!-- WiFi radio: TX power / ESP-NOW rate (applies to the master and every client) -->
        <div class="pairing-section" id="radioSection">
          <div class="card-title">WiFi Radio</div>
          <div style="font-size:0.85rem;color:var(--text-muted);margin:8px 0 14px">The 6 Mbps rate makes the radio's current bursts shorter (helps a weak battery) but arrives about 5 dB quieter than 1 Mbps. Go back to 1 Mbps if a client drops out. Applies to the master and every client. TX power is fixed at 11 dBm: a slider for it was tried and did not change the real signal. "Signal at master" is how loudly the master hears that client, "Signal at client" is how loudly the client hears the master.</div>
          <div class="form-group"><label for="radioRate">ESP-NOW rate</label>
            <select id="radioRate" onchange="radioDirty=true"><option value="1">6 Mbps - lower current (default)</option><option value="0">1 Mbps - longest range</option></select></div>
          <div class="form-group"><label for="radioPs">Radio power save</label>
            <select id="radioPs" onchange="radioDirty=true"><option value="0">Off - client radios always listening (default)</option><option value="1">On - client radios sleep between 2 s check-ins</option></select>
            <div style="font-size:0.78rem;color:var(--text-muted);margin-top:4px">On: each client wakes every 2 s, calls the master and listens for a moment. Saves roughly 40% of a client's battery use. Commands (Sync, Start/Stop, Lock, names, settings) take up to 2 s to reach clients. Updates, pairing and tests keep all clients awake automatically.</div></div>
          <button class="btn btn-primary" id="radioApplyBtn" onclick="applyRadio()">APPLY TO MASTER + ALL CLIENTS</button>
          <div id="radioMsg" style="font-size:0.85rem;margin-top:10px;color:var(--text-muted)"></div>
          <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border)">
            <div style="font-weight:600;margin-bottom:6px">Radio test</div>
            <div style="font-size:0.85rem;color:var(--text-muted);margin-bottom:10px">Compares the two rates, in both directions, and shows how loudly each side really hears the other (average of 5 frames each). Takes a few seconds: do not move the units.</div>
            <button class="btn btn-warning" id="radioTestBtn" onclick="startRadioTest()">RUN RADIO TEST</button>
            <div id="radioTestOut" style="margin-top:12px;font-size:0.85rem"></div>
          </div>
        </div>

        <!-- Pairing Section -->
        <div class="pairing-section">
          <div class="card-title">Client Pairing</div>
          <div class="pairing-controls">
            <button class="btn btn-warning" id="startPairingBtn" onclick="startPairingMode()">
               START PAIRING
            </button>
            <button class="btn btn-danger" id="stopPairingBtn" onclick="stopPairingMode()" style="display: none;">
               STOP PAIRING
            </button>
          </div>
          
          <div id="pairingStatus" style="display: none;">
            <div style="color: var(--warning); font-weight: 600; margin-bottom: 10px;">
               Pairing Mode Active - <span id="pairingTimeRemaining">60</span>s remaining
            </div>
            
            <div id="unpairedClientsList" class="unpaired-list">
              <!-- Unpaired clients will appear here -->
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Toast Notifications -->
  <div id="toast" class="toast"></div>

  <!-- RTC Set Dialog -->
  <div id="rtcDialog" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 2000; align-items: center; justify-content: center;">
    <div class="card" style="max-width: 400px; margin: 20px;">
      <div class="card-header">
        <div class="card-title">Set RTC Time</div>
      </div>
      <div class="form-group">
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;">
          <div>
            <label>Year</label>
            <input type="number" id="rtcYear" value="2024" min="2020" max="2100">
          </div>
          <div>
            <label>Month</label>
            <input type="number" id="rtcMonth" value="1" min="1" max="12">
          </div>
          <div>
            <label>Day</label>
            <input type="number" id="rtcDay" value="1" min="1" max="31">
          </div>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 15px;">
          <div>
            <label>Hour</label>
            <input type="number" id="rtcHour" value="0" min="0" max="23">
          </div>
          <div>
            <label>Minute</label>
            <input type="number" id="rtcMinute" value="0" min="0" max="59">
          </div>
          <div>
            <label>Second</label>
            <input type="number" id="rtcSecond" value="0" min="0" max="59">
          </div>
        </div>
      </div>
      <button class="btn btn-warning" onclick="setRTCFromPhone()" style="width: 100%; margin-top: 16px;">
        USE THIS DEVICE'S TIME NOW
      </button>
      <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 6px;">Sends this phone's or computer's clock (local time) in one tap. Accurate to about a second, so make sure the phone's own clock is right.</div>
      <div style="display: flex; gap: 10px; margin-top: 14px;">
        <button class="btn btn-primary" onclick="setRTC()" style="flex: 1;">
           SET RTC
        </button>
        <button class="btn" onclick="hideRTCDialog()" style="flex: 1; background: var(--border); color: var(--text);">
          <span>X</span> CANCEL
        </button>
      </div>
    </div>
  </div>

  <script>let ws;let reconnectTimeout;let pingInterval;let lastClientListHash='';let currentState={running:!0,fps:25.000,dropFrame:!1,syncInterval:0,clients:0};let currentMaxFrames=24;let pendingFpsChange=false;function formatUptime(seconds){const days=Math.floor(seconds/86400);seconds%=86400;const hours=Math.floor(seconds/3600);seconds%=3600;const minutes=Math.floor(seconds/60);seconds%=60;let result='';if(days>0)result+=days+'d ';if(hours>0||days>0)result+=hours+'h ';if(minutes>0||hours>0||days>0)result+=minutes+'m ';result+=seconds+'s';return result}
function formatLastSeen(seconds){const months=Math.floor(seconds/2592000);const days=Math.floor(seconds/86400);const hours=Math.floor(seconds/3600);const minutes=Math.floor(seconds/60);if(months>0){return months+' month'+(months>1?'s':'')+' ago'}else if(days>0){return days+' day'+(days>1?'s':'')+' ago'}else if(hours>0){return hours+' hour'+(hours>1?'s':'')+' ago'}else if(minutes>0){return minutes+' minute'+(minutes>1?'s':'')+' ago'}else{return seconds+' second'+(seconds!==1?'s':'')+' ago'}}
function updateFrameLimit(){const fpsSelect=document.getElementById('fps');const framesInput=document.getElementById('frames');if(!fpsSelect||!framesInput){return}
const fps=fpsSelect.value;if(fps==='23.976'||fps==='24'){currentMaxFrames=23}else if(fps==='25'){currentMaxFrames=24}else if(fps==='29.97'||fps==='30'){currentMaxFrames=29}
framesInput.max=currentMaxFrames;if(parseInt(framesInput.value)>currentMaxFrames){framesInput.value=currentMaxFrames}}
function validateFramesInput(input){const value=parseInt(input.value);if(isNaN(value)||value<0){input.value=0}else if(value>currentMaxFrames){input.value=currentMaxFrames}}
function validateTimecodeInput(input){const value=parseInt(input.value);const max=parseInt(input.max);if(isNaN(value)||value<0){input.value=0}else if(value>max){input.value=max}}
function setTimecode(){const now=Date.now();if(now-lastCommandTime<CLIENT_DEBOUNCE_MS){console.log('setTimecode ignored - too fast (client debounce)');showToast('Please wait - processing...','warning');return}
lastCommandTime=now;disableAllButtons();validateFramesInput(document.getElementById('frames'));validateTimecodeInput(document.getElementById('hours'));validateTimecodeInput(document.getElementById('minutes'));validateTimecodeInput(document.getElementById('seconds'));const hours=document.getElementById('hours').value;const minutes=document.getElementById('minutes').value;const seconds=document.getElementById('seconds').value;const frames=document.getElementById('frames').value;if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`SET_TIMECODE:${hours}:${minutes}:${seconds}:${frames}`);showToast(`Timecode set to ${hours}:${minutes}:${seconds}:${frames}`)}else{showToast('WebSocket not connected','error')}}
function calibrateClient(){const clientMac=document.getElementById('calibrationClient').value;if(!clientMac){showToast('Please select a client first','error');return}
if(!confirm('This will restart and recalibrate the selected client. This takes 2-3 minutes. Continue?')){return}
if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`CALIBRATE_CLIENT:${clientMac}`);showToast('Calibration command sent to client','warning')}else{showToast('WebSocket not connected','error')}}
function updateCalibrationClientsList(clients){const clientHash=clients.filter(c=>c.paired&&c.online).map(c=>c.mac).sort().join(',');if(clientHash===lastClientListHash){return}
lastClientListHash=clientHash;const select=document.getElementById('calibrationClient');const currentValue=select.value;select.innerHTML='<option value="">Select client to calibrate</option>';clients.forEach(client=>{if(client.paired&&client.online){const option=document.createElement('option');option.value=client.mac;option.textContent=`${client.mac} (${client.timecode})`;if(client.mac===currentValue){option.selected=!0}
select.appendChild(option)}})}
function connectWebSocket(){const protocol=window.location.protocol==='https:'?'wss:':'ws:';const wsUrl=`${protocol}//${window.location.host}/ws`;if(ws&&ws.readyState===WebSocket.OPEN){ws.close()}
try{ws=new WebSocket(wsUrl);ws.onopen=function(){console.log('WebSocket connected');updateConnectionStatus('connected');clearTimeout(reconnectTimeout);startPing()};ws.onclose=function(event){console.log('WebSocket disconnected:',event.code,event.reason);updateConnectionStatus('disconnected');if(pingInterval)clearInterval(pingInterval);reconnectTimeout=setTimeout(connectWebSocket,5000)};ws.onerror=function(error){console.error('WebSocket error:',error);updateConnectionStatus('error')};ws.onmessage=function(event){try{if(event.data==='PONG'){return}
if(event.data.startsWith('WARN:')){showToast(event.data.substring(5),'warning');return}
const data=JSON.parse(event.data);handleWebSocketMessage(data)}catch(e){console.error('Error parsing WebSocket message:',e)}}}catch(error){console.error('Error creating WebSocket:',error);updateConnectionStatus('error');reconnectTimeout=setTimeout(connectWebSocket,5000)}}
function startPing(){if(pingInterval)clearInterval(pingInterval);pingInterval=setInterval(()=>{if(ws&&ws.readyState===WebSocket.OPEN){ws.send('PING')}},10000)}
function updateConnectionStatus(status){const statusEl=document.getElementById('connectionStatus');statusEl.className=`connection-status status-${status}`;switch(status){case 'connected':statusEl.innerHTML='<div class="status-indicator" style="background: var(--success);"></div><span>CONNECTED</span>';break;case 'disconnected':statusEl.innerHTML='<div class="status-indicator" style="background: var(--danger);"></div><span>DISCONNECTED - RECONNECTING...</span>';break;case 'connecting':statusEl.innerHTML='<div class="status-indicator" style="background: var(--warning);"></div><span>CONNECTING...</span>';break;case 'error':statusEl.innerHTML='<div class="status-indicator" style="background: var(--danger);"></div><span>CONNECTION ERROR</span>';break}}
function handleWebSocketMessage(data){switch(data.type){case 'full_update':updateAllDisplays(data);break;case 'timecode_update':updateTimecodeDisplay(data);if(data.uptime!==undefined){document.getElementById('masterUptime').textContent=formatUptime(data.uptime)}
if(data.rtc_hour!==undefined){updateRTCDisplay({hour:data.rtc_hour,minute:data.rtc_minute,second:data.rtc_second,year:data.rtc_year,month:data.rtc_month,day:data.rtc_day})}
break;case 'rtc_update':updateRTCDisplay(data);break;case 'clients_update':updateClientsDisplay(data);break;case 'clients_detailed':updateClientsDetailed(data);break;case 'pairing_update':updatePairingDisplay(data);break}}
function updateAllDisplays(data){const timecodeData={hours:data.timecode.hours,minutes:data.timecode.minutes,seconds:data.timecode.seconds,frames:data.timecode.frames,fps:data.timecode.fps,dropFrame:data.timecode.dropFrame,running:data.timecode.running,lockStatus:data.lockStatus,lockLabel:data.lockLabel};updateTimecodeDisplay(timecodeData);if(data.rtc){updateRTCDisplay({year:data.rtc.year,month:data.rtc.month,day:data.rtc.day,hour:data.rtc.hour,minute:data.rtc.minute,second:data.rtc.second,warn:data.rtc.warn})}
if(data.uptime!==undefined){document.getElementById('masterUptime').textContent=formatUptime(data.uptime)}
if(data.masterUtilityVersion){document.getElementById('masterUtilityVersion').textContent='v'+data.masterUtilityVersion}
if(data.masterLtcVersion){document.getElementById('masterLtcVersion').textContent='v'+data.masterLtcVersion}
if(data.sync){updateSyncConfigDisplay(data.sync)}
if(data.radio){updateRadioDisplay(data.radio)}
if(data.clients!==undefined){updateClientsDisplay({count:data.clients})}}
function updateTimecodeDisplay(data){const timecodeStr=String(data.hours).padStart(2,'0')+':'+String(data.minutes).padStart(2,'0')+':'+String(data.seconds).padStart(2,'0')+':'+String(data.frames).padStart(2,'0');document.getElementById('currentTimecode').textContent=timecodeStr;if(data.fps!==undefined){let fpsDisplay;if(typeof data.fps==='string'){fpsDisplay=data.fps}else{const fpsValue=parseFloat(data.fps);if(Math.abs(fpsValue-23.976)<0.001){fpsDisplay='23.976'}else if(Math.abs(fpsValue-29.97)<0.001){fpsDisplay='29.97'}else{fpsDisplay=fpsValue.toFixed(3)}}
document.getElementById('fps-display').textContent=fpsDisplay+' fps'+(data.dropFrame?' (DF)':'');let selectorValue=fpsDisplay;if(fpsDisplay==='24.000')selectorValue='24';if(fpsDisplay==='25.000')selectorValue='25';if(fpsDisplay==='30.000')selectorValue='30';if(fpsDisplay==='23.976')selectorValue='23.976';if(fpsDisplay==='29.97')selectorValue='29.97';const fpsSelect=document.getElementById('fps');const dropframeCheckbox=document.getElementById('dropframe');if(!pendingFpsChange&&document.activeElement!==fpsSelect&&document.activeElement!==dropframeCheckbox){fpsSelect.value=selectorValue;dropframeCheckbox.checked=data.dropFrame;let previewText=selectorValue+' fps';if(data.dropFrame&&selectorValue==='29.97'){previewText='29.97 fps (DF)'}
document.getElementById('fpsPreview').textContent=previewText;updateFrameLimit()}}
currentState.running=data.running;const statusEl=document.getElementById('status');const lockLabelEl=document.getElementById('lockStatusLabel');if(!window.lastLockStatus){window.lastLockStatus='';window.lastLockLabel='';window.lastRunningState=null}
if(data.lockStatus){let newStatusText='';let newStatusClass='';switch(data.lockStatus){case 'LOCKED':newStatusText='LOCKED';newStatusClass='status-badge status-locked';break;case 'UNLOCKED':newStatusText='UNLOCKED';newStatusClass='status-badge status-unlocked';break;case 'SEARCHING':newStatusText='SEARCHING';newStatusClass='status-badge status-searching';break}
if(statusEl.textContent!==newStatusText||statusEl.className!==newStatusClass){statusEl.textContent=newStatusText;statusEl.className=newStatusClass}
if(lockLabelEl&&data.lockLabel){if(window.lastLockLabel!==data.lockLabel){lockLabelEl.textContent=data.lockLabel;window.lastLockLabel=data.lockLabel}
if(lockLabelEl.style.display!=='block'){lockLabelEl.style.display='block'}}else if(lockLabelEl&&lockLabelEl.style.display!=='none'){lockLabelEl.style.display='none';window.lastLockLabel=''}
window.lastLockStatus=data.lockStatus;window.lastRunningState=data.running}else{if(data.jamSync&&(data.jamSync==='LOCKED'||data.jamSync==='FREERUN'||data.jamSync==='SEARCHING')){switch(data.jamSync){case 'LOCKED':case 'FREERUN':if(statusEl.textContent!=='LOCKED'){statusEl.textContent='LOCKED';statusEl.className='status-badge status-locked'}
break;case 'SEARCHING':if(statusEl.textContent!=='JAM SYNC SEARCHING'){statusEl.textContent='JAM SYNC SEARCHING';statusEl.className='status-badge status-searching'}
break}}else{if(data.running){if(statusEl.textContent!=='RUNNING'){statusEl.textContent='RUNNING';statusEl.className='status-badge status-running'}}else{if(statusEl.textContent!=='STOPPED'){statusEl.textContent='STOPPED';statusEl.className='status-badge status-stopped'}}}
if(lockLabelEl&&lockLabelEl.style.display!=='none'){lockLabelEl.style.display='none';window.lastLockLabel=''}}}
function updateRTCDisplay(data){const rtcTime=String(data.hour).padStart(2,'0')+':'+String(data.minute).padStart(2,'0')+':'+String(data.second).padStart(2,'0');document.getElementById('currentRTCTime').textContent=rtcTime;if(data.year!==undefined&&data.month!==undefined&&data.day!==undefined){const rtcDate=String(data.year)+'/'+String(data.month).padStart(2,'0')+'/'+String(data.day).padStart(2,'0');document.getElementById('currentRTCDate').textContent=rtcDate}if(data.warn!==undefined){const w=document.getElementById('rtcWarn');if(w){w.style.display=data.warn?'block':'none';w.textContent=data.warn==2?'RTC chip not found - the time shown is not real':'RTC battery was lost - this time is NOT real. Press Set RTC and enter the correct time.'}}}
function updateClientsDisplay(data){document.getElementById('connectedCount').textContent=data.count;document.getElementById('clientCount').textContent=data.count+' clients'}
function updateClientsDetailed(data){const clientsList=document.getElementById('clientsList');clientsList.innerHTML='';const count=data.clients?data.clients.length:0;document.getElementById('connectedCount').textContent=count;document.getElementById('clientCount').textContent=count+' clients';if(!data.clients||data.clients.length===0){clientsList.innerHTML=`
          <div class="no-clients">
            <div>No clients connected</div>
            <div style="font-size: 0.9rem; margin-top: 5px;">Clients will appear here when they connect</div>
          </div>
        `;return}
data.clients.forEach(client=>{const clientCard=document.createElement('div');clientCard.className=`client-card ${client.paired ? 'paired-client' : 'unpaired-client'}`;const isJamRunning=(client.jamSync==='LOCKED'||client.jamSync==='FREERUN');const statusClass=client.online?(client.synced||isJamRunning?'status-connected':'status-warning'):'status-disconnected';const statusText=client.online?(client.synced||isJamRunning?'SYNCED':'NOT SYNCED'):'OFFLINE';const safeName=(client.name||client.mac).replace(/'/g,"\\'");let fpsDisplay;if(typeof client.fps==='string'){fpsDisplay=client.fps}else{const fpsValue=parseFloat(client.fps);if(Math.abs(fpsValue-23.976)<0.001){fpsDisplay='23.976'}else if(Math.abs(fpsValue-29.97)<0.001){fpsDisplay='29.97'}else if(Math.abs(fpsValue-24)<0.001){fpsDisplay='24.000'}else if(Math.abs(fpsValue-25)<0.001){fpsDisplay='25.000'}else if(Math.abs(fpsValue-30)<0.001){fpsDisplay='30.000'}else{fpsDisplay=fpsValue.toFixed(3)}}
let jamSyncBadge='';if(client.jamSync){let jamSyncClass='';let jamSyncText='';switch(client.jamSync){case 'LOCKED':jamSyncClass='status-locked';jamSyncText='JAM SYNC LOCKED';break;case 'FREERUN':jamSyncClass='status-freerun';jamSyncText='JAM SYNC FREERUN';break;case 'SEARCHING':jamSyncClass='status-searching';jamSyncText='JAM SYNC SEARCHING';break;default:jamSyncClass='';jamSyncText=''}
if(jamSyncText){jamSyncBadge=`<div class="status-badge ${jamSyncClass}" style="margin-left: 8px;">${jamSyncText}</div>`}}
clientCard.innerHTML=`
          <div class="client-header">
            <div class="client-mac" style="cursor:pointer" onclick="renameClient('${client.mac}','${safeName}')" title="Click to rename">
              ${client.name||client.mac}
              <br><span style="font-size:0.7rem;color:#666">${client.mac}</span>
            </div>
            <div style="display: flex; gap: 8px;">
              <div class="status-badge ${statusClass}">${statusText}</div>
              ${jamSyncBadge}
            </div>
          </div>
          <div class="client-info">
            <div><strong>Status:</strong> ${client.running ? 'RUNNING' : 'STOPPED'}</div>
            <div class="client-timecode"><strong>Timecode:</strong> ${client.timecode}</div>
            <div><strong>FPS:</strong> ${fpsDisplay}</div>
            <div><strong>Jam Sync:</strong> ${client.jamSync || 'DISABLED'}</div>
            <div><strong>Last Seen:</strong> ${formatLastSeen(client.lastSeen)}</div>
            <div><strong>Signal at master:</strong> ${signalText(client.rssi)}</div>
            <div><strong>Signal at client:</strong> ${client.rssiM ? signalText(client.rssiM) : '<span style="color:#777">N/A</span>'}</div>
            <div><strong>LTC out offset:</strong> ${ofsText(client.lofs)} <a href="#" style="color:var(--secondary)" onclick="return setLtcOfs('${client.mac}','${safeName}',${client.lofs===undefined?99:client.lofs})">set</a></div>
            <div><strong>Paired:</strong> ${client.paired ? 'YES' : 'NO'}</div>
            <div><strong>Uptime:</strong> ${client.uptime ? formatUptime(client.uptime) : 'N/A'}</div>
            <div><strong>Utility:</strong> v${client.utilityVersion || '0.0.0'}</div>
            <div><strong>LTC:</strong> v${client.ltcVersion || '0.0.0'}</div>
            <div><strong>Radio:</strong> ${radioText(client)}</div>
          </div>
          <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;">
            ${!client.paired ?
              `<button class="btn btn-primary" onclick="pairClient('${client.mac}')" style="padding: 8px 12px; font-size: 0.8rem;">PAIR CLIENT</button>` :
              `<button class="btn btn-danger" onclick="unpairClient('${client.mac}')" style="padding: 8px 12px; font-size: 0.8rem;">UNPAIR</button>
               <button class="btn btn-secondary" onclick="renameClient('${client.mac}','${safeName}')" style="padding: 8px 12px; font-size: 0.8rem;">EDIT NAME</button>`
            }
          </div>
        `;clientsList.appendChild(clientCard)})}
function updatePairingDisplay(data){const pairingStatus=document.getElementById('pairingStatus');const startPairingBtn=document.getElementById('startPairingBtn');const stopPairingBtn=document.getElementById('stopPairingBtn');const clientsCard=document.getElementById('clientsCard');if(data.pairingMode){pairingStatus.style.display='block';startPairingBtn.style.display='none';stopPairingBtn.style.display='block';clientsCard.classList.add('pairing-mode-active');startPairingCountdown(data.pairingTimeRemaining);const unpairedList=document.getElementById('unpairedClientsList');unpairedList.innerHTML='';if(data.unpairedClients.length===0){unpairedList.innerHTML='<div style="color: var(--text-muted); text-align: center; padding: 20px;">No unpaired clients detected</div>'}else{data.unpairedClients.forEach(client=>{const clientDiv=document.createElement('div');clientDiv.className='client-card unpaired-client';clientDiv.innerHTML=`
              <div class="client-header">
                <div class="client-mac">${client.mac}</div>
                <div class="status-badge status-warning">UNPAIRED</div>
              </div>
              <div class="client-info">
                <div><strong>Detected:</strong> ${client.timeSeen} seconds ago</div>
              </div>
              <div style="margin-top: 10px;">
                <button class="btn btn-success" onclick="pairClient('${client.mac}')" style="padding: 8px 12px; font-size: 0.8rem;">PAIR THIS CLIENT</button>
              </div>
            `;unpairedList.appendChild(clientDiv)})}}else{pairingStatus.style.display='none';startPairingBtn.style.display='block';stopPairingBtn.style.display='none';clientsCard.classList.remove('pairing-mode-active');stopPairingCountdown()}}
let pairingCountdownInterval=null;let pairingTimeRemaining=0;function startPairingCountdown(seconds){stopPairingCountdown();pairingTimeRemaining=seconds;document.getElementById('pairingTimeRemaining').textContent=pairingTimeRemaining;pairingCountdownInterval=setInterval(()=>{pairingTimeRemaining--;if(pairingTimeRemaining<=0){pairingTimeRemaining=0;stopPairingCountdown()}
document.getElementById('pairingTimeRemaining').textContent=pairingTimeRemaining},1000)}
function stopPairingCountdown(){if(pairingCountdownInterval){clearInterval(pairingCountdownInterval);pairingCountdownInterval=null}}
let radioDirty=false,radioMaster=null;
const RADIO_RATE=['1 Mbps','6 Mbps'];
function updateRadioDisplay(r){radioMaster=r;const rt=document.getElementById('radioRate'),ps=document.getElementById('radioPs'),m=document.getElementById('radioMsg');if(!rt)return;
if(!radioDirty&&document.activeElement!==rt&&document.activeElement!==ps){rt.value=r.rate;if(ps)ps.value=r.ps?'1':'0'}
if(m){m.textContent=(r.pending?'Switching... ':'Active on the master: ')+RADIO_RATE[r.rate]+(r.ps?' - power save ON':'')}
if(r.lofs!==undefined){masterLtcOfsVal=r.lofs;const e=document.getElementById('masterLtcOfs');if(e)e.innerHTML=ofsText(r.lofs)}}
let masterLtcOfsVal=0;
function ofsText(v){if(v===undefined||v===99)return '<span style="color:#777">N/A</span>';return v?((v>0?'+':'')+v+' fr'):'0 fr'}
async function setLtcOfs(who,name,cur){const v=prompt('LTC output offset for '+name+', in frames (-30 to 30).\n+ = the timecode it sends is that many frames ahead, - = behind.\nUse it when this camera\'s picture is off against the others on a clap.',(cur===99||cur===undefined)?0:cur);if(v===null)return false;
const n=parseInt(v,10);if(isNaN(n)||n<-30||n>30||String(n)!==String(v).trim().replace(/^\+/,'')){showToast('Enter a whole number from -30 to 30','error');return false}
try{const r=await fetch('/ltcofs/set?who='+encodeURIComponent(who)+'&v='+n,{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;showToast('LTC offset '+(n>0?'+':'')+n+' fr sent to '+name)}catch(e){showToast('Could not set: '+e,'error')}return false}
async function applyRadio(){const rt=document.getElementById('radioRate').value,ps=document.getElementById('radioPs').value;
try{const r=await fetch('/radio/set?rate='+rt+'&ps='+ps,{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;radioDirty=false;showToast('Sent to all paired clients; the master switches in about a second')}
catch(e){showToast('Could not apply: '+e,'error')}}
function signalText(r){if(!r)return '<span style="color:#777">N/A</span>';const q=r>=-60?4:(r>=-70?3:(r>=-80?2:(r>=-90?1:0)));const c=q>=3?'#00ff88':(q==2?'#ffc850':'#ff5050');
const bars=['&#9602;','&#9604;','&#9606;','&#9608;'].map(function(b,i){return '<span style="color:'+(i<q?c:'#444')+'">'+b+'</span>'}).join('');
return '<span style="color:'+c+'">'+r+' dBm</span> '+bars+' <span style="color:#777;font-size:0.75rem">'+['Very weak','Weak','Fair','Good','Excellent'][q]+'</span>'}
function radioText(c){if(!c.rv)return '<span style="color:#777">not reported</span>';const diff=radioMaster&&(c.rrate!=radioMaster.rate||(c.rps!==2&&!!c.rps!==!!radioMaster.ps));
return RADIO_RATE[c.rrate]+(c.rps===1?' &middot; power save':'')+(diff?' <span style="color:#ffc850">(updating...)</span>':'')}
async function startRadioTest(){try{const r=await fetch('/radio/test/start',{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;showToast('Radio test started (a few seconds)');pollRadioTest(true)}catch(e){showToast('Could not start: '+e,'error')}}
async function pollRadioTest(loop){try{const r=await fetch('/radio/test');const j=await r.json();renderRadioTest(j);if(loop&&j.state>=1&&j.state<=3){setTimeout(function(){pollRadioTest(true)},1000)}}catch(e){if(loop)setTimeout(function(){pollRadioTest(true)},2000)}}
function renderRadioTest(j){const o=document.getElementById('radioTestOut');if(!o)return;
if(j.state>=1&&j.state<=3){o.innerHTML='<span style="color:#ffc850">Testing... '+['','clients are transmitting','the master is transmitting','collecting the results'][j.state]+' - do not move the units</span>';return}
if(!j.clients||!j.clients.length){o.textContent=j.state==4?'No online client took part in the test.':'';return}
let h='';
for(const c of j.clients){
h+='<div style="margin:12px 0 4px;font-weight:600">'+(c.name||c.mac)+(c.replied?'':' <span style="color:#ff5050;font-weight:400">(did not report back: needs firmware 1.3.18)</span>')+'</div>';
h+='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:0.8rem"><tr style="color:#999"><th style="text-align:left;padding:4px">Rate</th><th>client &rarr; master (dBm heard)</th><th>master &rarr; client (dBm heard)</th></tr>';
const cell=function(a,n,t){const v=n[t]>0?a[t]:null;return '<td style="padding:4px;text-align:center">'+(v===null?'&ndash;':v)+'</td>'};
for(let r=1;r>=0;r--){h+='<tr style="border-top:1px solid #333"><td style="padding:4px">'+RADIO_RATE[r]+'</td>'+cell(c.c2m,c.c2mN,r*8+3)+cell(c.m2c,c.m2cN,r*8+3)+'</tr>'}
h+='</table></div>';
const diffTxt=function(name,a,n){const x=n[3]>0?a[3]:null,y=n[11]>0?a[11]:null;if(x===null||y===null)return '';const d=y-x;return '<div style="margin-top:4px;color:#999">'+name+': 6 Mbps is '+(d>=0?'+':'')+d+' dB vs 1 Mbps</div>'};
h+=diffTxt('client&rarr;master',c.c2m,c.c2mN)+diffTxt('master&rarr;client',c.m2c,c.m2cN)}
o.innerHTML=h}
function updateSyncConfigDisplay(data){currentState.syncInterval=data.interval;const curSync=document.getElementById('currentSyncInterval');if(curSync){curSync.textContent=data.interval}const syncIntervalInput=document.getElementById('syncInterval');if(syncIntervalInput&&document.activeElement!==syncIntervalInput){syncIntervalInput.value=data.interval}
document.getElementById('syncStatus').textContent=data.interval>0?'ON':'OFF';document.getElementById('syncStatus').style.color=data.interval>0?'var(--success)':'var(--danger)'}
let lastCommandTime=0;const CLIENT_DEBOUNCE_MS=1000;let debounceInterval=null;function disableAllButtons(){const buttons=document.querySelectorAll('.btn');buttons.forEach(btn=>{btn.classList.add('processing');btn.disabled=!0});let remainingSeconds=Math.ceil(CLIENT_DEBOUNCE_MS/1000);updateButtonCountdown(remainingSeconds);if(debounceInterval)clearInterval(debounceInterval);debounceInterval=setInterval(()=>{remainingSeconds--;if(remainingSeconds<=0){clearInterval(debounceInterval);enableAllButtons()}else{updateButtonCountdown(remainingSeconds)}},1000)}
function updateButtonCountdown(seconds){const buttons=document.querySelectorAll('.btn.processing');buttons.forEach(btn=>{btn.setAttribute('data-countdown',`(${seconds}s)`)})}
function enableAllButtons(){const buttons=document.querySelectorAll('.btn');buttons.forEach(btn=>{btn.classList.remove('processing');btn.disabled=!1;btn.removeAttribute('data-countdown')})}
function sendCommand(cmd){const now=Date.now();if(now-lastCommandTime<CLIENT_DEBOUNCE_MS){console.log('Command ignored - too fast (client debounce)');showToast('Please wait - processing...','warning');return}
lastCommandTime=now;disableAllButtons();if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`CMD:${cmd}`);showToast(`Command sent: ${cmd}`);if(cmd==='start'){document.getElementById('status').textContent='RUNNING';document.getElementById('status').className='status-badge status-running'}else if(cmd==='stop'){document.getElementById('status').textContent='STOPPED';document.getElementById('status').className='status-badge status-stopped'}else if(cmd==='syncall'){document.getElementById('status').className='status-badge status-syncing pulse';setTimeout(()=>{const statusEl=document.getElementById('status');if(currentState.running){statusEl.className='status-badge status-running'}else{statusEl.className='status-badge status-stopped'}},2000)}}else{showToast('WebSocket not connected','error')}}
function lockAllDevices(){const lockBtn=document.getElementById('lockAllBtn');if(!confirm('WARNING: Locking all devices will disable WiFi and wireless connectivity.\n\nYou will need to manually unlock each device to re-enable WiFi.\n\nDo you want to continue? *locking will dramatically improve the battery life')){return}
lockBtn.disabled=!0;let countdown=3;lockBtn.textContent=`LOCKING IN ${countdown}...`;const countdownInterval=setInterval(()=>{countdown--;if(countdown>0){lockBtn.textContent=`LOCKING IN ${countdown}...`}else{clearInterval(countdownInterval);lockBtn.textContent='LOCKING...';if(ws&&ws.readyState===WebSocket.OPEN){ws.send('CMD:lockall');showToast('Locking all devices...','warning');setTimeout(()=>{lockBtn.textContent='LOCKED';lockBtn.className='btn btn-danger';lockBtn.disabled=!1},1000)}else{showToast('WebSocket not connected','error');lockBtn.textContent='LOCK ALL';lockBtn.disabled=!1}}},1000)}
function setFPS(){const now=Date.now();if(now-lastCommandTime<CLIENT_DEBOUNCE_MS){console.log('setFPS ignored - too fast (client debounce)');showToast('Please wait - processing...','warning');return}
lastCommandTime=now;disableAllButtons();const fps=document.getElementById('fps').value;const dropframe=document.getElementById('dropframe').checked?'1':'0';let fpsValue=fps;pendingFpsChange=false;if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`SET_FPS:${fpsValue}:${dropframe}`);showToast(`FPS set to ${fpsValue} ${dropframe === '1' ? '(Drop Frame)' : ''}`)}else{showToast('WebSocket not connected','error')}}
function setSyncConfig(){const now=Date.now();if(now-lastCommandTime<CLIENT_DEBOUNCE_MS){console.log('setSyncConfig ignored - too fast (client debounce)');showToast('Please wait - processing...','warning');return}
lastCommandTime=now;disableAllButtons();const interval=document.getElementById('syncInterval').value;if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`SET_SYNC:${interval}`);showToast(`Auto-sync ${interval > 0 ? 'enabled (' + interval + ' min)' : 'disabled'}`)}else{showToast('WebSocket not connected','error')}}
function syncFromRTC(){sendCommand('syncfromrtc')}
function setRTC(){const year=document.getElementById('rtcYear').value;const month=document.getElementById('rtcMonth').value;const day=document.getElementById('rtcDay').value;const hour=document.getElementById('rtcHour').value;const minute=document.getElementById('rtcMinute').value;const second=document.getElementById('rtcSecond').value;const v=[year,month,day,hour,minute,second].map(Number);const dim=new Date(v[0],v[1],0).getDate();if(v.some(isNaN)||v[0]<2020||v[0]>2099||v[1]<1||v[1]>12||v[2]<1||v[2]>dim||v[3]<0||v[3]>23||v[4]<0||v[4]>59||v[5]<0||v[5]>59){showToast('Invalid date or time','error');return}if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`SET_RTC:${year}:${month}:${day}:${hour}:${minute}:${second}`);showToast('RTC time set successfully');hideRTCDialog()}else{showToast('WebSocket not connected','error')}}
function startPairingMode(){if(ws&&ws.readyState===WebSocket.OPEN){ws.send('PAIRING:start');showToast('Pairing mode started - searching for clients')}else{showToast('WebSocket not connected','error')}}
function stopPairingMode(){if(ws&&ws.readyState===WebSocket.OPEN){ws.send('PAIRING:stop');showToast('Pairing mode stopped')}else{showToast('WebSocket not connected','error')}}
function pairClient(mac){if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`PAIRING:pair:${mac}`);showToast(`Pairing with client ${mac}`)}else{showToast('WebSocket not connected','error')}}
function unpairClient(mac){if(ws&&ws.readyState===WebSocket.OPEN){ws.send(`PAIRING:unpair:${mac}`);showToast(`Unpaired client ${mac}`)}else{showToast('WebSocket not connected','error')}}
function fillRTCFromDevice(){const d=new Date(),set=function(i,v){document.getElementById(i).value=v};set('rtcYear',d.getFullYear());set('rtcMonth',d.getMonth()+1);set('rtcDay',d.getDate());set('rtcHour',d.getHours());set('rtcMinute',d.getMinutes());set('rtcSecond',d.getSeconds())}
async function setRTCFromPhone(){if(!(ws&&ws.readyState===WebSocket.OPEN)){showToast('WebSocket not connected','error');return}
let rtt=1e9;for(let i=0;i<3;i++){const a=performance.now();try{await fetch('/getrtc',{cache:'no-store'});rtt=Math.min(rtt,performance.now()-a)}catch(e){}}
if(rtt>1500)rtt=0;let t=Date.now(),nxt=Math.ceil(t/1000)*1000;if(nxt-t<400+rtt)nxt+=1000;
const wait=Math.max(0,Math.round(nxt-t-rtt/2)),d=new Date(nxt);
ws.send('SET_RTC_AT:'+d.getFullYear()+':'+(d.getMonth()+1)+':'+d.getDate()+':'+d.getHours()+':'+d.getMinutes()+':'+d.getSeconds()+':'+wait);
showToast('RTC set from this device (network delay '+Math.round(rtt)+' ms compensated)');hideRTCDialog()}
function showRTCDialog(){fillRTCFromDevice();document.getElementById('rtcDialog').style.display='flex'}
function hideRTCDialog(){document.getElementById('rtcDialog').style.display='none'}
function updateFPSPreview(){const fps=document.getElementById('fps').value;const dropframe=document.getElementById('dropframe').checked;let previewText=fps+' fps';if(dropframe&&fps==='29.97'){previewText='29.97 fps (DF)'}
document.getElementById('fpsPreview').textContent=previewText}
function showToast(message,type='success'){const toast=document.getElementById('toast');toast.textContent=message;toast.className=`toast ${type} show`;setTimeout(()=>{toast.classList.remove('show')},3000)}
function renameClient(mac,currentName){const newName=prompt('Enter Cam ID for '+mac+' (00-99):',currentName===mac?'':currentName);if(newName===null)return;const fd=new FormData();fd.append('mac',mac);fd.append('name',newName);fetch('/set_client_name',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{if(d.error)showToast(d.error,'error');else showToast('Client renamed')}).catch(e=>showToast('Failed to rename','error'))}
function editMasterCamId(){const current=document.getElementById('masterCamId').textContent;const newId=prompt('Enter Master Cam ID (00-99):',current);if(newId===null)return;const fd=new FormData();fd.append('camId',newId);fetch('/set_cam_id',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{if(d.error)showToast(d.error,'error');else{showToast('Cam ID updated');document.getElementById('masterCamId').textContent=String(parseInt(newId)||0).padStart(2,'0')}}).catch(e=>showToast('Failed to update','error'))}
function loadMasterCamId(){fetch('/get_cam_id').then(r=>r.json()).then(d=>{if(d.camId)document.getElementById('masterCamId').textContent=d.camId}).catch(e=>console.error('Failed to load cam ID:',e))}
document.addEventListener('DOMContentLoaded',function(){connectWebSocket();updateFrameLimit();updateFPSPreview();loadMasterCamId()});document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'){if(!ws||ws.readyState!==WebSocket.OPEN){console.log('Tab visible - reconnecting WebSocket');connectWebSocket()}}});window.addEventListener('online',function(){console.log('Network online - reconnecting WebSocket');setTimeout(connectWebSocket,1000)});window.addEventListener('pagehide',function(){if(ws){ws.close()}});</script>
<a id="fwlink" href="/client-update" style="position:fixed;right:12px;bottom:12px;background:#4ecca3;color:#111;padding:8px 12px;border-radius:6px;text-decoration:none;font-weight:bold;font-family:Arial;z-index:999">Firmware update</a><script>setTimeout(function(){fetch('/sysinfo').then(function(r){return r.json()}).then(function(s){if(s.rstBad>0){var a=document.getElementById('fwlink');a.innerHTML='Firmware update <span style="background:#e94560;color:#fff;border-radius:10px;padding:1px 7px;margin-left:6px;font-size:12px">\u26A0 '+s.rstBad+'</span>';a.title=s.rstBad+' brownout / watchdog / crash reset(s) recorded - open Firmware update, card Reset log'}}).catch(function(){})},3000)</script>
<div id="pageEnd" style="display:none"></div>
</body>
</html>
)=====";

// ==================== OTA PAGE HTML (Dynamic) ====================
String generateOTAPage() {
    String ltcSSID = ltcOTASSID.length() > 0 ? ltcOTASSID : "TCM-L-" + otaSSID.substring(6);

    String html = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>OTA Update - Master Utility</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 500px; margin: 0 auto; padding: 20px; background: #16213e; border-radius: 10px; }
        h1 { color: #e94560; text-align: center; }
        .info { background: #0f3460; padding: 15px; border-radius: 5px; margin: 15px 0; }
        .info p { margin: 5px 0; }
        .warning { background: #ff6b35; color: #000; padding: 10px; border-radius: 5px; margin: 15px 0; }
        form { margin-top: 20px; }
        input[type='file'] { width: 100%; padding: 10px; margin: 10px 0; background: #0f3460; border: 1px solid #e94560; color: #fff; border-radius: 5px; box-sizing: border-box; }
        input[type='submit'] { width: 100%; padding: 15px; background: #e94560; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        input[type='submit']:hover { background: #ff6b6b; }
        .partner { margin-top: 20px; padding: 15px; background: #0f3460; border-radius: 5px; border: 1px dashed #e94560; }
    </style>
</head>
<body>
    <div class='container'>
        <h1>OTA Update</h1>
        <div class='info'>
            <p><strong>Device:</strong> Master Utility</p>
            <p><strong>Version:</strong> )rawliteral" + String(FIRMWARE_VERSION_MAJOR) + "." + String(FIRMWARE_VERSION_MINOR) + "." + String(FIRMWARE_VERSION_PATCH) + R"rawliteral(</p>
            <p><strong>Hardware ID:</strong> )rawliteral" + otaSSID.substring(6) + R"rawliteral(</p>
        </div>
        <div class='warning'>
            Only upload MASTER UTILITY firmware to this device!
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
        <div class='partner'>
            <p><strong>Partner Device (LTC):</strong> )rawliteral" + ltcSSID + R"rawliteral(</p>
            <p>Connect to that SSID to update the LTC device</p>
            <p>Password: )rawliteral" + String(OTA_PASSWORD) + R"rawliteral(</p>
        </div>
    </div>
</body>
</html>
)rawliteral";
    return html;
}

// ==================== WEB SERVER SETUP ====================
// Pages are sent as a CHUNKED response that copies each piece out of flash on demand. The library's plain send_P() path
// duplicated / garbled pieces of the large main page under memory pressure (AsyncTCP issue 126 describes exactly this and
// its maintainers recommend a chunked response for big pages). The pages also carry an end marker and reload themselves
// if they arrive damaged.
static void sendPageChunked(AsyncWebServerRequest* req, PGM_P page) {
    const size_t total = strlen_P(page);
    AsyncWebServerResponse* r = req->beginChunkedResponse("text/html", [page, total](uint8_t* buf, size_t maxLen, size_t index) -> size_t {
        if (index >= total) return 0;
        size_t n = total - index;
        if (n > maxLen) n = maxLen;
        memcpy_P(buf, page + index, n);
        return n;
    });
    r->addHeader("Cache-Control", "no-store");
    req->send(r);
}

void setupWebServer() {
  // One-click OTA of all paired clients (ClientOTA.ino): /client-update, /client_fw, /firmware, /client_ota/*
  clientOta_setupRoutes();
  mfwSetupRoutes();

  // Main page - serves OTA page when in OTA mode, main page otherwise
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (otaMode) {
      request->send(200, "text/html", generateOTAPage());
    } else {
      sendPageChunked(request, HTML_PAGE);
    }
  });

  // OTA firmware upload handler - only works in OTA mode
  server.on("/update", HTTP_POST,
    [](AsyncWebServerRequest *request) {
      if (!otaMode) {
        request->send(403, "text/plain", "OTA mode not active");
        return;
      }

      // Check if firmware was rejected due to filename validation
      if (!otaFirmwareValid && otaFirmwareError.length() > 0) {
        String errorHtml = "<html><body style='background:#1a1a2e;color:#e94560;text-align:center;padding:50px;font-family:Arial;'>";
        errorHtml += "<h1>Wrong Firmware!</h1>";
        errorHtml += "<p style='font-size:18px;'>" + otaFirmwareError + "</p>";
        errorHtml += "<p style='margin-top:30px;'><a href='/' style='color:#4ecca3;font-size:16px;'>Try Again</a></p>";
        errorHtml += "</body></html>";
        AsyncWebServerResponse *response = request->beginResponse(200, "text/html", errorHtml);
        response->addHeader("Connection", "close");
        request->send(response);
        otaFirmwareError = "";  // Clear error for next attempt
        return;
      }

      bool shouldRestart = !Update.hasError();
      AsyncWebServerResponse *response = request->beginResponse(200, "text/html",
        shouldRestart ?
        "<html><body style='background:#1a1a2e;color:#4ecca3;text-align:center;padding:50px;font-family:Arial;'>"
        "<h1 style='color:#4ecca3;'>Update Successful!</h1><p>Device is restarting...</p></body></html>" :
        "<html><body style='background:#1a1a2e;color:#e94560;text-align:center;padding:50px;font-family:Arial;'>"
        "<h1>Update Failed!</h1><p>Please try again.</p><a href='/' style='color:#4ecca3;'>Back</a></body></html>"
      );
      response->addHeader("Connection", "close");
      request->send(response);
      if (shouldRestart) {
        // Set flag for delayed restart - allows response to be fully sent
        otaUpdateSuccess = true;
        otaRestartTime = millis() + 2000;  // Restart in 2 seconds
      }
    },
    [](AsyncWebServerRequest *request, String filename, size_t index, uint8_t *data, size_t len, bool final) {
      if (!otaMode) return;

      if (!index) {
        // Validate filename - must contain "master" AND "utility" (case insensitive)
        String fname = filename;
        fname.toLowerCase();

        bool hasMaster = fname.indexOf("master") != -1;
        bool hasClient = fname.indexOf("client") != -1;
        bool hasLtc = fname.indexOf("ltc") != -1;
        bool hasUtility = fname.indexOf("utility") != -1;

        if (!hasMaster || !hasUtility) {
          // Wrong firmware file
          otaFirmwareValid = false;
          if (hasClient && hasUtility) {
            otaFirmwareError = "Wrong firmware! This is MASTER UTILITY but you uploaded CLIENT UTILITY firmware.";
          } else if (hasMaster && hasLtc) {
            otaFirmwareError = "Wrong firmware! This is MASTER UTILITY but you uploaded MASTER LTC firmware.";
          } else if (hasClient && hasLtc) {
            otaFirmwareError = "Wrong firmware! This is MASTER UTILITY but you uploaded CLIENT LTC firmware.";
          } else if (!hasUtility && hasLtc) {
            otaFirmwareError = "Wrong firmware! This is UTILITY device but you uploaded LTC firmware.";
          } else if (!hasMaster && hasClient) {
            otaFirmwareError = "Wrong firmware! This is MASTER device but you uploaded CLIENT firmware.";
          } else {
            otaFirmwareError = "Invalid firmware. Filename must contain 'master' and 'utility'.";
          }
          return;
        }

        otaFirmwareValid = true;
        otaFirmwareError = "";

        otaContentLength = request->contentLength();
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
          Update.printError(Serial);
          return;
        }
        otaStarted = true;
      }

      if (!otaFirmwareValid) return;  // Skip if firmware invalid

      if (otaStarted && len) {
        if (Update.write(data, len) != len) {
          Update.printError(Serial);
        }
      }

      if (final) {
        if (Update.end(true)) {
          otaStarted = false;
        } else {
          Update.printError(Serial);
        }
      }
    }
  );

  // Timecode endpoint
  server.on("/timecode", HTTP_GET, [](AsyncWebServerRequest *request) {
    pollLTCStatus();

    char fpsBuffer[16];
    float fpsValue = getFpsFloat(currentTimecode.fps_thousandths);
    snprintf(fpsBuffer, sizeof(fpsBuffer), "%.3f", fpsValue);

    String json = "{";
    json += "\"hours\":" + String(currentTimecode.hours) + ",";
    json += "\"minutes\":" + String(currentTimecode.minutes) + ",";
    json += "\"seconds\":" + String(currentTimecode.seconds) + ",";
    json += "\"frames\":" + String(currentTimecode.frames) + ",";
    json += "\"fps_thousandths\":" + String(currentTimecode.fps_thousandths) + ",";
    json += "\"fps\":" + String(fpsBuffer) + ",";
    json += "\"dropFrame\":" + String(currentTimecode.dropFrame ? "true" : "false") + ",";
    json += "\"running\":" + String(currentTimecode.running ? "true" : "false");
    json += "}";

    request->send(200, "application/json", json);
  });

  // RTC endpoint
  server.on("/getrtc", HTTP_GET, [](AsyncWebServerRequest *request) {
    DateTime now = rtcNow();

    String json = "{";
    json += "\"year\":" + String(now.year()) + ",";
    json += "\"month\":" + String(now.month()) + ",";
    json += "\"day\":" + String(now.day()) + ",";
    json += "\"hour\":" + String(now.hour()) + ",";
    json += "\"minute\":" + String(now.minute()) + ",";
    json += "\"second\":" + String(now.second());
    json += "}";

    request->send(200, "application/json", json);
  });

  // Sync settings endpoint
  server.on("/getsync", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = "{";
    json += "\"syncInterval\":" + String(syncIntervalMinutes);
    json += "}";
    request->send(200, "application/json", json);
  });

  // Get Master Cam ID endpoint
  server.on("/get_cam_id", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = "{\"camId\":\"" + String(masterCamId) + "\"}";
    request->send(200, "application/json", json);
  });

  // Set Master Cam ID endpoint
  server.on("/set_cam_id", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (request->hasParam("camId", true)) {
      String newId = request->getParam("camId", true)->value();
      newId.trim();

      // Validate: must be numeric 0-99
      int idNum = newId.toInt();
      if (newId.length() == 0 || idNum < 0 || idNum > 99) {
        request->send(400, "application/json", "{\"error\":\"Cam ID must be 00-99\"}");
        return;
      }
      // Check if input was actually numeric
      for (unsigned int i = 0; i < newId.length(); i++) {
        if (!isdigit(newId[i])) {
          request->send(400, "application/json", "{\"error\":\"Cam ID must be numeric (00-99)\"}");
          return;
        }
      }

      // Format as 2-digit with leading zero
      char formattedId[3];
      snprintf(formattedId, sizeof(formattedId), "%02d", idNum);

      // Check for duplicate with client IDs
      for (int i = 0; i < espNowClientCount; i++) {
        if (espNowClients[i].paired && strcmp(espNowClients[i].deviceName, formattedId) == 0) {
          request->send(400, "application/json", "{\"error\":\"ID already used by a client\"}");
          return;
        }
      }

      masterCamId[0] = formattedId[0];
      masterCamId[1] = formattedId[1];
      masterCamId[2] = '\0';
      saveMasterCamId(masterCamId);
      // Send updated cam ID to Master LTC for OLED display
      sendEspNowStatusToLTCDeviceForced();
      request->send(200, "application/json", "{\"success\":true}");
    } else {
      request->send(400, "application/json", "{\"error\":\"Missing camId parameter\"}");
    }
  });

  // Set RTC endpoint
  server.on("/setrtc", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (request->hasParam("year") && request->hasParam("month") && request->hasParam("day") &&
        request->hasParam("hour") && request->hasParam("minute") && request->hasParam("second")) {

      int year = request->getParam("year")->value().toInt();
      int month = request->getParam("month")->value().toInt();
      int day = request->getParam("day")->value().toInt();
      int hour = request->getParam("hour")->value().toInt();
      int minute = request->getParam("minute")->value().toInt();
      int second = request->getParam("second")->value().toInt();

      if (!setRTC(year, month, day, hour, minute, second)) {
        request->send(400, "text/plain", "Invalid date or time");
        return;
      }
      broadcastSyncESPNow(true);  // Manual HTTP RTC update - needs fresh data
      sendWebSocketData();
    }
    request->send(200, "text/plain", "OK");
  });

  // Clients endpoint
  server.on("/clients", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = "[";

    for (int i = 0; i < espNowClientCount; i++) {
      if (i > 0) json += ",";

      unsigned long timeSinceSeen = millis() - espNowClients[i].lastSeen;
      bool isOnline = (timeSinceSeen < 120000);

      json += "{";
      json += "\"mac\":\"" + espNowClients[i].macString + "\",";
      json += "\"online\":" + String(isOnline ? "true" : "false") + ",";
      json += "\"synced\":" + String(espNowClients[i].synced ? "true" : "false") + ",";
      json += "\"running\":" + String(espNowClients[i].running ? "true" : "false") + ",";
      json += "\"paired\":" + String(espNowClients[i].paired ? "true" : "false") + ",";
      json += "\"timecode\":\"" + String(espNowClients[i].hours) + ":";
      json += String(espNowClients[i].minutes) + ":";
      json += String(espNowClients[i].seconds) + ":";
      json += String(espNowClients[i].frames) + "\",";
      json += "\"fps_thousandths\":" + String(espNowClients[i].fps_thousandths) + ",";
      json += "\"lastSeen\":" + String(timeSinceSeen / 1000);
      json += "}";
    }

    json += "]";
    request->send(200, "application/json", json);
  });

  // Command endpoint
  server.on("/command", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("cmd")) {
      String cmd = request->getParam("cmd")->value();

      if (cmd == "start") {
        currentTimecode.running = true;
        saveSettings();
        broadcastRunningState();
        sendWebSocketData();
        request->send(200, "text/plain", "OK");
      }
      else if (cmd == "stop") {
        currentTimecode.running = false;
        saveSettings();
        broadcastRunningState();
        sendWebSocketData();
        request->send(200, "text/plain", "OK");
      }
      else if (cmd == "syncall") {
        pendingManualSync = true; diagResetReq = true;  // Executed by loop() - keeps async_tcp task unblocked
        request->send(200, "text/plain", "OK");
      }
      else if (cmd == "syncfromrtc") {
        // syncFromRTC() waits for the RTC second tick (up to 1.1 s): executed by loop() - keeps async_tcp task unblocked
        pendingRtcSync = true;
        request->send(200, "text/plain", "OK");
      }
    } else {
      request->send(400, "text/plain", "Missing command");
    }
  });

  // Set FPS endpoint
  server.on("/setfps", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("fps")) {
      String fpsString = request->getParam("fps")->value();
      float fps = fpsString.toFloat();


      uint16_t fpsThousandths = fpsFloatToThousandths(fps);

      currentTimecode.fps_thousandths = fpsThousandths;
      currentTimecode.dropFrame = (request->hasParam("dropframe")) &&
                                   (request->getParam("dropframe")->value() == "1") &&
                                   (fpsThousandths == 29970);

      lastFpsSetTime = millis();  // Track when FPS was changed

      saveSettings();
      sendFPSChangeToLTCDevice();

      // Defer the resync instead of blocking the async_tcp task with delay():
      // the LTC device + Si5351 need ~2 frame times to stabilize at the new
      // rate; loop() runs broadcastSyncESPNow() once the time is reached.
      float newFpsFloat = getFpsFloat(fpsThousandths);
      uint32_t stabilizeMs = (uint32_t)(2000.0f / newFpsFloat) + 10;  // 2 frames + margin
      pendingFpsResyncTime = millis() + stabilizeMs;  // time BEFORE flag (see decl)
      pendingFpsResync = true;

      request->send(200, "text/plain", "OK");
    } else {
      request->send(400, "text/plain", "Missing fps parameter");
    }
  });

  // Set sync interval endpoint
  server.on("/setsync", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("interval")) {
      syncIntervalMinutes = request->getParam("interval")->value().toInt();
      saveSyncSettings();
      sendEspNowStatusToLTCDevice();
      sendWebSocketData();

      request->send(200, "text/plain", "OK");
    } else {
      request->send(400, "text/plain", "Missing interval");
    }
  });

  // Set timecode endpoint
  server.on("/settimecode", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("hours") && request->hasParam("minutes") &&
        request->hasParam("seconds") && request->hasParam("frames")) {

      uint8_t hours = request->getParam("hours")->value().toInt();
      uint8_t minutes = request->getParam("minutes")->value().toInt();
      uint8_t seconds = request->getParam("seconds")->value().toInt();
      uint8_t frames = request->getParam("frames")->value().toInt();

      currentTimecode.hours = hours;
      currentTimecode.minutes = minutes;
      currentTimecode.seconds = seconds;
      currentTimecode.frames = frames;

      // Validate drop frame: skip frames 0,1 at minute boundaries (except every 10th minute)
      if (currentTimecode.dropFrame && currentTimecode.fps_thousandths == 29970 &&
          currentTimecode.seconds == 0 && currentTimecode.frames < 2 &&
          (currentTimecode.minutes % 10) != 0) {
        currentTimecode.frames = 2;
      }

      saveSettings();
      sendSyncToLTCDevice();
      pendingManualSync = true; diagResetReq = true;  // Frame-aligned client sync runs in loop()
    }
    request->send(200, "text/plain", "OK");
  });

  // Pairing endpoints
  server.on("/startpairing", HTTP_GET, [](AsyncWebServerRequest *request) {
    startPairingMode();
    request->send(200, "text/plain", "Pairing mode started");
  });

  server.on("/stoppairing", HTTP_GET, [](AsyncWebServerRequest *request) {
    stopPairingMode();
    request->send(200, "text/plain", "Pairing mode stopped");
  });

  server.on("/pairclient", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("mac")) {
      String macStr = request->getParam("mac")->value();
      uint8_t mac[6];
      unsigned int tempMac[6];
      if (sscanf(macStr.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
                 &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)tempMac[i];
        pairClient(mac);
        request->send(200, "text/plain", "Client paired");
      } else {
        request->send(400, "text/plain", "Invalid MAC address");
      }
    } else {
      request->send(400, "text/plain", "Missing MAC parameter");
    }
  });

  server.on("/unpairclient", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("mac")) {
      String macStr = request->getParam("mac")->value();
      uint8_t mac[6];
      unsigned int tempMac[6];
      if (sscanf(macStr.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
                 &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)tempMac[i];
        unpairClient(mac);
        request->send(200, "text/plain", "Client unpaired");
      } else {
        request->send(400, "text/plain", "Invalid MAC address");
      }
    } else {
      request->send(400, "text/plain", "Missing MAC parameter");
    }
  });

  // Set sample rate endpoint (47952 or 48000)
  server.on("/setsamplerate", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("rate")) {
      uint32_t rate = request->getParam("rate")->value().toInt();

      // Validate sample rate - allow 47952 (NTSC optimal) or 48000 (integer fps)
      if (rate == 47952 || rate == 48000) {
        saveSampleRate(rate);
        updateSi5351Frequency(rate);
        sendSyncToLTCDevice(); // Broadcast new sample rate to LTC device via SPI
        pendingManualSync = true; diagResetReq = true;  // Frame-aligned client sync runs in loop()

        request->send(200, "text/plain", "Sample rate set to " + String(rate) + " Hz");
      } else {
        request->send(400, "text/plain", "Invalid sample rate. Use 47952 or 48000");
      }
    } else {
      request->send(400, "text/plain", "Missing rate parameter");
    }
  });

  // Get current sample rate endpoint
  server.on("/getsamplerate", HTTP_GET, [](AsyncWebServerRequest *request) {
    uint32_t rate = currentSampleRate;
    request->send(200, "text/plain", String(rate));
  });

  // Set client name endpoint
  server.on("/set_client_name", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!request->hasParam("mac", true) || !request->hasParam("name", true)) {
      request->send(400, "application/json", "{\"error\":\"Missing mac or name parameter\"}");
      return;
    }

    String mac = request->getParam("mac", true)->value();
    String name = request->getParam("name", true)->value();
    name.trim();

    // Find client by MAC
    int clientIdx = -1;
    for (int i = 0; i < espNowClientCount; i++) {
      if (espNowClients[i].macString == mac) {
        clientIdx = i;
        break;
      }
    }

    if (clientIdx < 0) {
      request->send(404, "application/json", "{\"error\":\"Client not found\"}");
      return;
    }

    // Allow empty to clear
    if (name.length() == 0) {
      saveClientName(mac, "");
      espNowClients[clientIdx].deviceName[0] = '\0';
      sendClientNameToDevice(clientIdx, "");
      request->send(200, "application/json", "{\"success\":true}");
      return;
    }

    // Validate: must be numeric 0-99
    int idNum = name.toInt();
    if (idNum < 0 || idNum > 99) {
      request->send(400, "application/json", "{\"error\":\"Cam ID must be 00-99\"}");
      return;
    }
    // Check if input was actually numeric
    for (unsigned int i = 0; i < name.length(); i++) {
      if (!isdigit(name[i])) {
        request->send(400, "application/json", "{\"error\":\"Cam ID must be numeric (00-99)\"}");
        return;
      }
    }

    // Format as 2-digit with leading zero
    char formattedId[3];
    snprintf(formattedId, sizeof(formattedId), "%02d", idNum);

    // Check for duplicate with master ID
    if (strcmp(masterCamId, formattedId) == 0) {
      request->send(400, "application/json", "{\"error\":\"ID already used by Master\"}");
      return;
    }

    // Check for duplicate with other client IDs
    for (int i = 0; i < espNowClientCount; i++) {
      if (i != clientIdx && espNowClients[i].paired && strcmp(espNowClients[i].deviceName, formattedId) == 0) {
        request->send(400, "application/json", "{\"error\":\"ID already used by another client\"}");
        return;
      }
    }

    // Save to Master's Preferences
    saveClientName(mac, formattedId);

    // Update local struct
    strncpy(espNowClients[clientIdx].deviceName, formattedId, sizeof(espNowClients[clientIdx].deviceName) - 1);
    espNowClients[clientIdx].deviceName[sizeof(espNowClients[clientIdx].deviceName) - 1] = '\0';

    // Send to client device via ESP-NOW
    sendClientNameToDevice(clientIdx, formattedId);

    request->send(200, "application/json", "{\"success\":true}");
  });

  server.begin();
}

// ==================== OTA UPDATE FUNCTIONS ====================
String getDeviceTypeString(uint8_t type) {
    switch(type) {
        case DEVICE_MASTER_UTILITY: return "Master Utility";
        case DEVICE_MASTER_LTC: return "Master LTC";
        case DEVICE_CLIENT_UTILITY: return "Client Utility";
        case DEVICE_CLIENT_LTC: return "Client LTC";
        default: return "Unknown";
    }
}

String generateOTASSID() {
    uint64_t chipId = ESP.getEfuseMac();
    char suffix[5];
    snprintf(suffix, sizeof(suffix), "%04X", (uint16_t)(chipId & 0xFFFF));
    return "TCM-U-" + String(suffix);  // TimeCode Master - Utility
}

void sendOTACommandToLTC() {
    // Stop the 48 kHz sample clock BEFORE the LTC chip starts its WiFi / flash writes.
    // An LTC build whose sample ISR is not flash-safe would panic and reboot when a pulse lands in a
    // flash-write window (this blocked OTA of the fixed LTC firmware). With no clock there are no
    // interrupts. The unit restarts after OTA anyway, which brings the clock back.
    si5351.output_enable(SI5351_CLK0, 0);
    si5351.output_enable(SI5351_CLK1, 0);
    delay(150);

    // Send command to LTC device to enter OTA mode
    // Include our SSID so LTC can display both SSIDs
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_ENTER_OTA;
    strncpy(spiCmd.otaSSID, otaSSID.c_str(), sizeof(spiCmd.otaSSID) - 1);
    spiCmd.otaSSID[sizeof(spiCmd.otaSSID) - 1] = '\0';  // Ensure null termination

    performSPITransaction();
}

void enterOTAMode() {
    if (otaMode) return;

    otaMode = true;
    otaModeStartTime = millis();
    otaSSID = generateOTASSID();

    // Signal LTC device to also enter OTA mode (before stopping WiFi)
    sendOTACommandToLTC();
    delay(100);

    // Stop normal operations completely. wirelessEnabled=false makes every
    // ESP-NOW sender's guard block - the stack is deinitialized below, and
    // stale web/WS handlers must not be able to reach esp_now_send.
    wirelessEnabled = false;
    { WsLock l; ws.closeAll(); }
    server.end();
    esp_now_deinit();
    WiFi.softAPdisconnect(true);
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    delay(200);

    // Start fresh OTA access point with retry logic
    WiFi.mode(WIFI_AP);
    bool apStarted = false;
    for (int retry = 0; retry < 3 && !apStarted; retry++) {
        WiFi.setTxPower(WIFI_POWER_11dBm);   // OTA mode: always reachable, whatever the radio setting
        apStarted = WiFi.softAP(otaSSID.c_str(), OTA_PASSWORD);
        if (!apStarted) {
            delay(500);
        }
    }
    delay(500);

    // Restart the server - it will now serve OTA page on "/" due to otaMode check
    server.begin();
}

void exitOTAMode() {
    if (!otaMode) return;

    otaMode = false;
    server.end();

    // Restart the device to restore normal operation
    ESP.restart();
}

void checkOTAButton() {
    // Handle OTA mode operations
    if (otaMode) {
        // Check for timeout in OTA mode
        if (millis() - otaModeStartTime > OTA_MODE_TIMEOUT) {
            exitOTAMode();
        }

        // Poll LTC device periodically to get its SSID
        static unsigned long lastOTAPoll = 0;
        if (millis() - lastOTAPoll > 500) {
            pollLTCStatus();  // This will update ltcOTASSID from spiStatus
            lastOTAPoll = millis();
        }
        return;
    }

    // Block button input when LTC device is locked (only for entering OTA, not exiting)
    if (ltcSystemLocked) return;

    bool buttonPressed = (digitalRead(OTA_BUTTON_PIN) == LOW);

    if (buttonPressed && !otaButtonWasPressed) {
        // Button just pressed
        otaButtonPressStart = millis();
        otaButtonWasPressed = true;
    } else if (buttonPressed && otaButtonWasPressed) {
        // Button held - check if hold time reached
        if (millis() - otaButtonPressStart >= OTA_BUTTON_HOLD_TIME) {
            enterOTAMode();
        }
    } else if (!buttonPressed && otaButtonWasPressed) {
        // Button released before hold time
        otaButtonWasPressed = false;
    }
}

// ==================== SETUP & LOOP ====================
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
    crashInfoLoad();   // details of the last crash, if one is stored
    ltcOfsLoad();      // master's LTC output offset (sent to the LTC chip from loop)
    radioLoad();    // TX power / ESP-NOW rate chosen on the master's page
  setCpuFrequencyMhz(80);  // Set CPU to 80MHz for power saving (minimum for WiFi)
  btStop();                // Disable Bluetooth radio for power saving
  // NOTE: no POWER_EN GPIO on the Master board - OLED/Si5351 power comes from the
  // SW1/Q3 power path (GPIO5 is unconnected on U12), so there is nothing to enable here.
  delay(500);  // let the rails and the LTC half settle
  systemStartTime = millis();

  pinMode(SYNC_BUTTON_PIN, INPUT_PULLUP);
  pinMode(STATUS_LED_PIN, OUTPUT);
  pinMode(OTA_BUTTON_PIN, INPUT_PULLUP);  // OTA update button

  // Load settings
  loadOrGeneratePassword();
  loadSettings();
  loadSyncSettings();
  loadMasterCamId();

  // Initialize Si5351 clock generator
  setupSi5351();

  // Initialize SPI Master BEFORE RTC so sync commands work
  spiCmdLock = xSemaphoreCreateRecursiveMutex();
  setupSPI_Master();
  delay(1000);  // Allow SPI to stabilize

  // Initialize RTC (this will sync timecode to LTC device)
  setupRTC();

  // Initialize network
  setupWiFi();
  setupESP_NOW();
  setupWebSocket();
  setupWebServer();
  mfwBegin();   // load the result of the last master update for the page

  // Initialize variables
  lastAutoSyncTime = millis();
  updateRTCache();

  // Initial sync to LTC device with fresh RTC time
  // syncFromRTC() now properly sends RTC-calculated timecode to LTC device
  // and broadcasts to ESP-NOW clients without overwriting via pollLTCStatusLive()
  delay(1000);
  syncFromRTC(false);

  // Send WiFi info to LTC device
  delay(100);
  sendWiFiInfoToLTCDevice();

  // OTA Rollback Protection: Mark firmware as valid after successful startup
  // If device crashes before this point, bootloader will rollback to previous firmware
  radioApplyAll();   // everything is initialised now: make sure power and rate are applied
  esp_ota_mark_app_valid_cancel_rollback();
}

void loop() {
  // Check OTA button (10 second hold to enter OTA mode)
  checkOTAButton();

  // If in OTA mode, skip normal operations (AsyncWebServer handles OTA requests automatically)
  if (otaMode) {
    // Check if we need to restart after successful OTA update
    if (otaUpdateSuccess && millis() >= otaRestartTime) {
      ESP.restart();
    }
    delay(10);
    return;
  }

  // Handle pending wireless state changes (deferred to avoid Si5351 glitch)
  // (wraparound-safe compares - this device runs for months)
  unsigned long now = millis();
  if (wirelessState == WIRELESS_PENDING_DISABLE && (long)(now - wirelessStateChangeTime) >= 0) {
    wirelessState = WIRELESS_DISABLED;
    disableWireless();
  } else if (wirelessState == WIRELESS_PENDING_ENABLE && (long)(now - wirelessStateChangeTime) >= 0) {
    enableWireless();  // This will set state to WIRELESS_PENDING_POST_ENABLE_SYNC
  } else if (wirelessState == WIRELESS_PENDING_POST_ENABLE_SYNC && (long)(now - wirelessStateChangeTime) >= 0) {
    wirelessState = WIRELESS_ACTIVE;
    // NOTE: Removed manualSyncAllClients() - unlock should not force resync
    // Devices maintain their own timecode via Si5351 clock
  }

  // Deferred post-FPS-change resync (scheduled by SET_FPS to keep the async WS task non-blocking).
  // The deadline is SNAPSHOTTED FIRST and every check uses the snapshot: a
  // concurrent SET_FPS re-arm (which stores time first, flag last) either
  // moves the deadline before our snapshot (condition fails, we wait) or is
  // caught by the post-clear recheck (flag restored, we wait). Either way the
  // resync can never fire inside a new stabilization window.
  if (pendingFpsResync) {
    unsigned long armedDeadline = pendingFpsResyncTime;  // snapshot BEFORE testing
    if ((long)(now - armedDeadline) >= 0) {
      pendingFpsResync = false;
      if (pendingFpsResyncTime != armedDeadline) {
        pendingFpsResync = true;  // concurrent re-arm: honor the newer deadline
        return;
      }
      // Bypass broadcastSyncESPNow's 1s auto-sync rate limit: this resync is
      // one-shot and already waited out the stabilization window - if the
      // rate limit swallowed it, clients would keep the OLD frame rate
      // forever (SYNC is the only message carrying fps_thousandths).
      lastESPNowSend = millis() - ESPNOW_SEND_INTERVAL - 1;
      broadcastSyncESPNow(false);  // frame-boundary-aligned sync at the now-stabilized rate
      sendWebSocketData();
      lastFullWSUpdate = millis();
      lastWSUpdate = millis();
    }
  }

  // Deferred manual sync requested by an async WS/HTTP handler.
  // Gated on !pendingFpsResync so a manual sync can never sample the LTC while
  // the Si5351/LTC are still stabilizing at a new rate, and rate-limited so a
  // request storm cannot starve the rest of loop().
  if (pendingRtcSync) {
    pendingRtcSync = false;
    syncFromRTC();           // timecode calc, ESP-NOW broadcast, LTC sync (waits for the RTC second tick)
    saveSettings();
    sendWebSocketData();
    lastFullWSUpdate = millis();
    lastWSUpdate = millis();
  }
  static unsigned long lastDeferredManualSync = 0;
  if (pendingManualSync &&!pendingFpsResync && now - lastDeferredManualSync >= 250) {
    pendingManualSync = false;
    lastDeferredManualSync = now;
    manualSyncAllClients();  // frame-boundary wait + sync (no-op while locked)
    sendWebSocketData();     // refresh UI even when the sync was skipped (locked)
    lastFullWSUpdate = millis();
    lastWSUpdate = millis();
  }

  // Poll LTC device status periodically
  static unsigned long lastSPIPoll = 0;
  if (millis() - lastSPIPoll > 1000) { // Poll every 1 second (changed from 100ms)
    pollLTCStatus();
    lastSPIPoll = millis();
  }

  // Send WebSocket updates periodically (every 200ms for smooth updates)
  if (millis() - lastWSUpdate > 200) {
    sendWebSocketTimecodeUpdate();
    lastWSUpdate = millis();
  }

  // Send full WebSocket data periodically (every 2 seconds for less frequent full updates)
  if (millis() - lastFullWSUpdate > 2000) {
    sendWebSocketData();
    lastFullWSUpdate = millis();
  }

  // (ws.cleanupClients() is no longer called here: the library already removes a browser when it
  // disconnects, and calling it from loop() let two tasks erase from the same list at once.)

  // Send client list details periodically (every 3 seconds)
  static unsigned long lastClientUpdate = 0;
  if (millis() - lastClientUpdate > 3000) {
    sendWebSocketClientsDetailed();
    lastClientUpdate = millis();
  }

  // Check pairing mode timeout
  checkPairingModeTimeout();

  // Advance the one-click client OTA (ClientOTA.ino)
  clientOta_loop();
  mfwLoop();

  // Update RTC cache
  updateRTCache();

  // Check for auto-sync
  checkAutoSync();

  // Check sync button
  checkSyncButton();

  // Send periodic alive messages
  sendPeriodicAlive();

  // Cleanup stale clients
  cleanupStaleESPNowClients();

  // Update ESP-NOW status to LTC device periodically
  static unsigned long lastEspNowUpdate = 0;
  if (millis() - lastEspNowUpdate > 5000) { // Every 5 seconds
    sendEspNowStatusToLTCDevice();
    lastEspNowUpdate = millis();
  }

  // Debug output
  // static unsigned long lastDebug = 0;
  // if (millis() - lastDebug >= 10000) {
  //   Serial.printf("Si5351 Interrupt Count: %lu\n", si5351InterruptCount);
  //   Serial.printf("LTC Battery Level: %d/4 (%s)\n",
  //                 ltcBatteryLevel,
  //                 ltcIsLineLevel ? "LINE" : "MIC");
  //   Serial.printf("ESP-NOW Clients: %d\n", espNowClientCount);
  //   lastDebug = millis();
  // }

  // Status LED blink
  static unsigned long lastBlink = 0;
  if (millis() - lastBlink > 1000) {
    lastBlink = millis();
    digitalWrite(STATUS_LED_PIN, !digitalRead(STATUS_LED_PIN));
  }

  delay(1);
}
