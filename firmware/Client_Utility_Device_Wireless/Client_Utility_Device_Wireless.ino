/*
 * =========================================================
 * ==      CLIENT UTILITY DEVICE (SPI MASTER) SKETCH      ==
 * =========================================================
 * FINAL VERSION - All fixes applied + WIRELESS LOCK MODE:
 * - SPI structure alignment fixed
 * - Connection monitoring improved
 * - Periodic status updates to slave
 * - WiFi and ESP-NOW disabled when device enters lock mode
 * - Wireless re-enabled when device exits lock mode
 */

#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_now.h>
#include <cmath>
#include <Preferences.h>
#include <si5351.h>
#include <esp_wifi.h>
#include <SPI.h>
#include <SPIFFS.h>
#include <HTTPClient.h>

// The core defines verifyRollbackLater() as a weak C function; ours must have C linkage to replace it
// (declared here, before any auto-generated prototype). See OTA SAFETY above setup().
extern "C" bool verifyRollbackLater();

// ==================== FIRMWARE IDENTITY ====================
// Identity tag. The master checks it before pushing a firmware to all clients (ClientOTA.ino),
// so a wrong file cannot be installed on every client in one click.
const char TCM_FIRMWARE_ID[] = "TCM-FWID:CLIENT-UTILITY";

// ==================== POWER ENABLE PIN ====================
#define POWER_EN_PIN 5  // Controls power to OLED/Si5351 via P-FET

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
#define THIS_DEVICE_TYPE DEVICE_CLIENT_UTILITY
#define FIRMWARE_VERSION_MAJOR 1
#define FIRMWARE_VERSION_MINOR 3
#define FIRMWARE_VERSION_PATCH 34

// Bench experiment (rsTestTask below): always 0 in release builds, which compile to exactly the same image as before.
// The test image is built with -DRADIO_SLEEP_TEST=1; it runs once after a power-up, then restarts into normal operation.
#ifndef RADIO_SLEEP_TEST
#define RADIO_SLEEP_TEST 0
#endif
#if RADIO_SLEEP_TEST
volatile bool rsStartReq = false;   // the master's page asked for the sleep test
volatile bool rsOffReq = false;     // ... or for the radio to be switched off for rsOffSec seconds
volatile uint16_t rsOffSec = 0;
volatile unsigned long rsLastCmd = 0;   // the master sends each command 3 times: act on the first only
#endif

// OTA state variables
volatile bool otaMode = false;
unsigned long otaModeStartTime = 0;
unsigned long otaButtonPressStart = 0;
bool otaButtonWasPressed = false;
String otaSSID = "";
String ltcOTASSID = "";              // LTC device's OTA SSID
bool otaFirmwareValid = false;       // Firmware filename validation flag
String otaFirmwareError = "";        // Error message for invalid firmware
WebServer otaServer(80);

/*
 * spi_protocol.h - FIXED with proper packing
 */
#include <stdint.h>

#define SPI_PACKET_SIZE 64

enum SPI_CommandType : uint8_t {
    CMD_POLL_STATUS   = 0,
    CMD_SYNC_TIMECODE = 1,
    CMD_SET_RUNNING   = 2,
    CMD_UPDATE_ESPNOW_STATUS = 3,
    CMD_LOCK_DEVICE = 8,     // Match master utility enum
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

// ==================== JAM SYNC MODE ENUM ====================
enum JamSyncMode : uint8_t {
    JAM_SYNC_DISABLED = 0,
    JAM_SYNC_SEARCHING = 1,
    JAM_SYNC_LOCKED = 2,
    JAM_SYNC_FREERUN = 3
};

// Compile-time guard: each struct must exactly equal the SPI transfer size.
// Prevents the receive-buffer overflow/overread that occurs if a field is added
// without updating _padding (the bug that left SPI_Command at 61 vs a 64-byte transfer).
static_assert(sizeof(SPI_Command) == SPI_PACKET_SIZE, "SPI_Command size mismatch!");
static_assert(sizeof(SPI_Status)  == SPI_PACKET_SIZE, "SPI_Status size mismatch!");
Preferences preferences;
Si5351 si5351;
uint32_t si5351InterruptCount = 0;
// Clock check of our LTC chip (missed/extra sample pulses), forwarded to the master in the heartbeat
bool ltcDiagValid = false;
int32_t ltcDiagNetLost = 0;
uint32_t ltcDiagSinceS = 0;   // LTC: seconds since its clock check was (re)started
uint8_t ltcRst[4] = {0, 0, 0, 0};   // the LTC chip's reset reason / brownout / watchdog / crash counts
bool ltcRstValid = false;
// Radio test state (the test itself is further down; the receive callback needs these first)
static int16_t rtSum[16];
static uint8_t rtCnt[16];
static bool rtRun = false;
static uint8_t rtCombo = 0, rtProbe = 0;
static unsigned long rtAt = 0;
volatile bool rtGoPending = false, rtReplyPending = false;
int8_t masterRssi = 0;            // how well WE hear the master (dBm, smoothed); reported to the master
unsigned long masterRssiAt = 0;
volatile bool radioPendFlag = false;   // the master sent a new TX power / rate (applied in the heartbeat)
volatile uint8_t radioPendTx = 3, radioPendRate = 1, radioPendPs = 0;
// Radio power save (optional, set on the master's page): the radio sleeps between the master's beacons (see psTask)
volatile bool psEnabled = false;
volatile bool psRadioOff = false;             // the radio is switched off right now: nothing may be sent
volatile unsigned long psBeaconAt = 0;        // when the last beacon of the master arrived
volatile uint16_t psInterval = 2000;          // beacon period announced by the master (ms)
volatile uint8_t psFlags = 0;                 // bit 0 = the master wants every client to stay awake
SemaphoreHandle_t psMux = nullptr;            // the heartbeat (esp_now_send) and the radio on/off switching never overlap
struct PsGuard {
    PsGuard()  { if (psMux) xSemaphoreTakeRecursive(psMux, portMAX_DELAY); }
    ~PsGuard() { if (psMux) xSemaphoreGiveRecursive(psMux); }
};
volatile bool ltcOfsPending = false;   // the master set our LTC output offset (sent to the LTC chip from utilityTask)
volatile int8_t ltcOfsValue = 0;
int8_t ltcOfsNow = 0;                    // what our LTC chip reports (0 on older LTC firmware)
volatile bool rstClearPending = false;   // the master asked us to clear the reset log (done in the heartbeat, not in the radio callback)
// ==================== WIFI RADIO SETTINGS (set on the master's firmware page) ====================
// The ESP-NOW data rate decides how long the radio's current bursts are; it is kept in NVS and the master pushes it to
// every client. TX power is fixed at 11 dBm (index 3): measured with the radio test, changing it did not change the
// received signal, so the selector was removed. The index stays in the packets and the table for compatibility.
static const wifi_power_t RADIO_TX_TABLE[8] = { WIFI_POWER_5dBm, WIFI_POWER_7dBm, WIFI_POWER_8_5dBm, WIFI_POWER_11dBm,
                                                WIFI_POWER_13dBm, WIFI_POWER_15dBm, WIFI_POWER_17dBm, WIFI_POWER_19_5dBm };
uint8_t radioTxIdx = 3;     // index into the table: 3 = 11 dBm (the previous fixed value)
uint8_t radioRateIdx = 1;   // ESP-NOW rate: 0 = 1 Mbps (longest range), 1 = 6 Mbps (6x shorter airtime, lower current)
static void radioLoad() {
    Preferences rp;
    if (rp.begin("radio", true)) {
        radioTxIdx = 3;   // TX power is fixed at 11 dBm: the setting had no measurable effect on the ESP-NOW signal
        radioRateIdx = rp.getUChar("rate", 1);
        psEnabled = rp.getUChar("ps", 0) != 0;
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
        rp.putUChar("ps", psEnabled ? 1 : 0);
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
uint16_t ltcErrFirstS = 0xFFFF, ltcErrLastS = 0, ltcErrWins = 0;   // when wrong-pulse windows happened
uint16_t ltcDiagMaxGapUs = 0;

TaskHandle_t UtilityTaskHandle = NULL;

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
    uint8_t userBits[8];  // LTC user bits: [0-5]=MMDDYY, [6-7]=camId
} struct_message;

void utilityTask(void* parameter);
void checkConnectionAndHeartbeat();
void checkPairingRequests();
void checkPairButton();
void updateSyncLED();
void updateStatusLED();
float getFpsFloat(uint16_t fps_thousandths);
uint32_t getSampleRateForFps(uint16_t fps_thousandths);
void sendESPNowHeartbeat();
void processESPNowPacket(const uint8_t *mac, const struct_message& data);
void sendPairingRequest();
void sendESPNowRegistration();
void unpairMaster();
void loadPairedMaster();
void savePairedMaster();
void loadDeviceName();
void saveDeviceName(const char* name);
void setupESP_NOW();
void pollLTCStatus();
void sendSyncToSlave();
void sendRunningStateToSlave(bool running);
void sendEspNowStatusToSlave();
void sendEspNowStatusToSlaveForced();
void sendEspNowStatusToSlaveInternal(bool force);
void setupSPI_Master();
void performSPITransaction();
void setupSi5351();
void updateSi5351Frequency(uint32_t freq, uint16_t fps_thousandths = 0);
void registerESPNowCallbacks();
void disableWireless();
void enableWireless();
void handleLockStateChange(bool isLocked);

uint8_t masterMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
bool isPaired = false;
String pairedMasterMac = "";
char deviceName[3] = "";  // Cam ID (2 chars), stored in Preferences
uint8_t currentUserBits[8] = {0};  // Current user bits (date from master + local cam ID)
unsigned long systemStartTime = 0;

// Convert char to nibble: '0'-'9' = 0-9, 'A'-'Z' = 0-15 (mod 16)
uint8_t charToNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'Z') return (c - 'A') % 16;  // A=0, B=1, ... P=15, Q=0, ...
    if (c >= 'a' && c <= 'z') return (c - 'a') % 16;
    return 0;
}

// Build cam ID from device name - cam ID is at positions 6-7 (last 2 nibbles)
// NOTE: Nibbles are stored in REVERSE order (second char first, first char second)
// because LTC monitors read nibble pairs as: second_nibble*10 + first_nibble
void buildCamIdFromName(uint8_t* userBits) {
    if (strlen(deviceName) >= 2) {
        userBits[6] = charToNibble(deviceName[1]);  // Second char stored first
        userBits[7] = charToNibble(deviceName[0]);  // First char stored second
    } else if (strlen(deviceName) == 1) {
        userBits[6] = 0;                            // No second char
        userBits[7] = charToNibble(deviceName[0]);  // First char stored second
    } else {
        userBits[6] = 0;
        userBits[7] = 0;
    }
}

#define SPI_MOSI 13
#define SPI_MISO 12
#define SPI_SCLK 14
#define SPI_CS 15

SPIClass hspi(HSPI);
SPI_Command spiCmd;
SPI_Status spiStatus;
portMUX_TYPE spiMux = portMUX_INITIALIZER_UNLOCKED;

// Serializes construction+transfer of the shared spiCmd buffer across tasks.
// The ESP-NOW receive callback (WiFi task) sends SPI sync commands while
// utilityTask polls status; without this lock two concurrent fills interleave
// and the LTC device receives a torn packet. RAII guard releases on every
// return path. Recursive to allow nested guarded calls.
SemaphoreHandle_t spiCmdLock = NULL;
struct SpiCmdGuard {
    SpiCmdGuard()  { if (spiCmdLock) xSemaphoreTakeRecursive(spiCmdLock, portMAX_DELAY); }
    ~SpiCmdGuard() { if (spiCmdLock) xSemaphoreGiveRecursive(spiCmdLock); }
};

// ==================== BATTERY, LEVEL & JAM SYNC INFO FROM SPI ====================
uint8_t ltcBatteryLevel = 0;
bool ltcBatteryPresent = true;  // NEW: From SPI status
bool ltcIsLineLevel = true;
uint8_t ltcJamSyncMode = JAM_SYNC_DISABLED;
bool ltcSystemLocked = false;
bool previousLockState = false;  // Track previous lock state for change detection
// LTC firmware version (from SPI)
uint8_t ltcVersionMajor = 0;
uint8_t ltcVersionMinor = 0;
uint8_t ltcVersionPatch = 0;
// Remote OTA state
uint8_t remoteOtaState = 0;  // 0=normal, 1=updating, 2=waiting, 3=error

// ==================== REMOTE OTA CONFIGURATION ====================
bool spiffsInitialized = false;

// Remote OTA state machine
enum RemoteOTAState : uint8_t {
    ROTA_IDLE = 0,
    ROTA_RECEIVED_COMMAND = 1,
    ROTA_SWITCHING_TO_WIFI = 2,
    ROTA_CONNECTING_TO_MASTER = 3,
    ROTA_DOWNLOADING_UTILITY = 4,
    ROTA_FLASHING_UTILITY = 5,
    ROTA_DOWNLOADING_LTC = 6,
    ROTA_PROXY_LTC_UPDATE = 7,
    ROTA_COMPLETE = 8,
    ROTA_ERROR = 9
};
volatile RemoteOTAState rotaState = ROTA_IDLE;
unsigned long rotaStateStartTime = 0;

// OTA command from Master
struct RemoteOTACommand {
    bool pending;
    bool updateUtility;
    bool updateLtc;
    char masterSSID[32];
    char masterPassword[32];
    uint8_t masterMac[6];
};
RemoteOTACommand rotaCommand = {false, false, false, "", "", {0}};

// Remote OTA timeouts
const unsigned long ROTA_WIFI_CONNECT_TIMEOUT = 30000;   // 30s to connect to Master AP
const unsigned long ROTA_DOWNLOAD_TIMEOUT = 120000;      // 120s to download firmware
const unsigned long ROTA_LTC_CONNECT_TIMEOUT = 15000;    // 15s to connect to LTC AP
const unsigned long ROTA_LTC_UPLOAD_TIMEOUT = 60000;     // 60s to upload to LTC

// ESP-NOW OTA message types
#define ESPNOW_OTA_CMD  0x10
#define ESPNOW_OTA_ACK  0x11
#define ESPNOW_OTA_PROG 0x12
#define ESPNOW_OTA_DONE 0x13
#define ESPNOW_OTA_ERR  0x14

// OTA status message structure (sent to Master)
typedef struct {
    uint8_t msgType;      // ESPNOW_OTA_PROG, ESPNOW_OTA_DONE, or ESPNOW_OTA_ERR
    uint8_t progress;     // 0-100 percentage
    uint8_t state;        // Current ROTA state
    char status[32];      // Status message
} OTAStatus;

// Track if we can send ESP-NOW messages (before WiFi switch or after restore)
bool espNowAvailable = true;
// Last remote-OTA error, kept so it can be reported once ESP-NOW is back (it is down during WiFi STA)
char rotaLastError[32] = "";

// Function declarations for remote OTA
void handleRemoteOTACommand(const uint8_t* mac, const uint8_t* data, int len);
void processRemoteOTA();
bool downloadAndFlashFirmware(const char* url, bool isUtility);
bool proxyLTCUpdate();
void sendOTAProgress(uint8_t progress, const char* status);
void sendOTAComplete();
void sendOTAError(const char* error);
void restoreNormalOperation();

// ==================== WIRELESS MANAGER STATE ====================
bool wirelessEnabled = true;
bool wirelessInitialized = false;

// Deferred wireless disable state machine
enum WirelessState {
    WIRELESS_ACTIVE,
    WIRELESS_PENDING_DISABLE,
    WIRELESS_DISABLED,
    WIRELESS_PENDING_ENABLE
};
volatile WirelessState wirelessState = WIRELESS_ACTIVE;
unsigned long wirelessStateChangeTime = 0;
const unsigned long WIRELESS_DISABLE_DELAY_MS = 500;  // 500ms delay to avoid Si5351 glitch

unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 5000;  // 5 seconds - faster detection on set

unsigned long lastMasterResponse = 0;
const unsigned long MASTER_TIMEOUT = 15000;  // 15 seconds - faster feedback for crew
unsigned long lastMasterTimestamp = 0;

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
  unsigned long lastConnectionAttempt;
  uint32_t sampleRate;
};

volatile LTC_Timecode ltcTimecode = {0, 0, 0, 0, 25000, false, false, 0, 0, 0, false, false, 0};
portMUX_TYPE ltcTimerMux = portMUX_INITIALIZER_UNLOCKED;

const int STATUS_LED_PIN = 2;
const int SYNC_LED_PIN = 4;
const int PAIR_BUTTON_PIN = 26;

unsigned long syncLedStartTime = 0;
bool syncLedBlinking = false;
int syncLedBlinksRemaining = 0;
bool syncLedState = false;
unsigned long statusLedLastUpdate = 0;
bool statusLedState = false;

unsigned long pairButtonPressTime = 0;
bool pairButtonPressed = false;
const unsigned long UNPAIR_HOLD_TIME = 3000;

const unsigned long RECONNECT_INTERVAL = 5000;

bool lastEspNowState = false;
unsigned long lastEspNowUpdate = 0;
const unsigned long ESPNOW_UPDATE_INTERVAL = 1000;

// ==================== WIRELESS MANAGEMENT FUNCTIONS ====================
void disableWireless() {
    if (!wirelessEnabled) {
        return; // Already disabled
    }

    // Deinitialize ESP-NOW first
    esp_err_t result = esp_now_deinit();

    // Disable WiFi
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);

    // Additional power saving - stop WiFi modem
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

    // Re-enable WiFi
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    // Start WiFi modem
    esp_wifi_start();

    // Configure WiFi settings
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_ps(WIFI_PS_NONE);

    // Re-initialize ESP-NOW
    esp_err_t result = esp_now_init();
    if (result == ESP_OK) {
        wirelessEnabled = true;
        psRadioOff = false;   // the radio is on again (power save may have left this set before the lock)
        // TX power / ESP-NOW rate from the master's page (default 11 dBm / 6 Mbps)
        radioApplyAll();
    } else {
        return;
    }

    // Re-register ESP-NOW callbacks
    registerESPNowCallbacks();

    // Re-add broadcast peer
    uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;

    esp_now_del_peer(broadcastAddress); // Remove if exists
    esp_now_add_peer(&peerInfo);

    // Reload paired master if applicable
    loadPairedMaster();
}

void handleLockStateChange(bool isLocked) {
    if (isLocked) {
        // Schedule wireless disable for later (avoid Si5351 glitch)
        wirelessState = WIRELESS_PENDING_DISABLE;
        wirelessStateChangeTime = millis() + WIRELESS_DISABLE_DELAY_MS;
        Serial.printf("Wireless disable scheduled in %dms...\n", WIRELESS_DISABLE_DELAY_MS);
    } else {
        // Schedule wireless enable for later
        wirelessState = WIRELESS_PENDING_ENABLE;
        wirelessStateChangeTime = millis() + WIRELESS_DISABLE_DELAY_MS;
        Serial.printf("Wireless enable scheduled in %dms...\n", WIRELESS_DISABLE_DELAY_MS);
    }
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

    if (spiStatus.fps_thousandths >= 23000 && spiStatus.fps_thousandths <= 30000) {
        portENTER_CRITICAL(&ltcTimerMux);
        ltcTimecode.hours = spiStatus.hours;
        ltcTimecode.minutes = spiStatus.minutes;
        ltcTimecode.seconds = spiStatus.seconds;
        ltcTimecode.frames = spiStatus.frames;
        ltcTimecode.fps_thousandths = spiStatus.fps_thousandths;
        ltcTimecode.dropFrame = spiStatus.dropFrame;
        ltcTimecode.running = spiStatus.running;
        ltcTimecode.synced = spiStatus.synced;
        portEXIT_CRITICAL(&ltcTimerMux);

        si5351InterruptCount = spiStatus.si5351InterruptCount;
        ltcBatteryLevel = spiStatus.batteryLevel;
        ltcBatteryPresent = (spiStatus.batteryPresent == 1);
        ltcIsLineLevel = (spiStatus.isLineLevel == 1);
        ltcJamSyncMode = spiStatus.jamSyncMode;
        // Read LTC firmware version for remote OTA
        ltcVersionMajor = spiStatus.firmwareVersionMajor;
        ltcVersionMinor = spiStatus.firmwareVersionMinor;
        ltcVersionPatch = spiStatus.firmwareVersionPatch;
        ltcDiagValid = (spiStatus.diagMagic == 0xD1);
        ltcDiagNetLost = spiStatus.diagNetLost;
        ltcDiagMaxGapUs = spiStatus.diagMaxGapUs;
        ltcDiagSinceS = spiStatus.diagUptimeS;
        ltcErrFirstS = spiStatus.diagErrFirstS;
        ltcErrLastS = spiStatus.diagErrLastS;
        ltcErrWins = spiStatus.diagErrWins;
        ltcRst[0] = spiStatus.rstReason; ltcRst[1] = spiStatus.rstBrown; ltcRst[2] = spiStatus.rstWdt; ltcRst[3] = spiStatus.rstPanic;
        ltcRstValid = (spiStatus.diagMagic == 0xD1 && spiStatus.rstReason != 0);
        ltcOfsNow = spiStatus.ltcOutOffset;
        
        static unsigned long lastDebugPrint = 0;
        if (millis() - lastDebugPrint > 2000) {
            Serial.printf("SPI RX: TC=%02d:%02d:%02d:%02d FPS=%d Batt=%d Ver=%d.%d.%d\n", 
                spiStatus.hours, spiStatus.minutes, spiStatus.seconds, spiStatus.frames,
                spiStatus.fps_thousandths, spiStatus.batteryLevel,
                ltcVersionMajor, ltcVersionMinor, ltcVersionPatch);
            lastDebugPrint = millis();
        }

        // Auto-update Si5351 frequency based on FPS (for jam sync mode)
        static uint16_t lastFpsForSampleRate = 0;
        if (spiStatus.fps_thousandths != lastFpsForSampleRate) {
            uint32_t optimalRate = getSampleRateForFps(spiStatus.fps_thousandths);
            updateSi5351Frequency(optimalRate, spiStatus.fps_thousandths);
            lastFpsForSampleRate = spiStatus.fps_thousandths;
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
        if (spiStatus.otaSSID[0] != '\0') {   // also needed for remote OTA (otaMode is false there)
            ltcOTASSID = String(spiStatus.otaSSID);
        }

        // Check if LTC device is requesting shutdown due to low battery
        if (spiStatus.lowBatteryShutdown == 1) {
            // Disable Si5351 clock outputs to save power
            si5351.output_enable(SI5351_CLK0, 0);
            si5351.output_enable(SI5351_CLK1, 0);

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

void sendSyncToSlave() {
    // Don't send sync commands when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked) {
        return;
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SYNC_TIMECODE;

    portENTER_CRITICAL(&ltcTimerMux);
    spiCmd.hours = ltcTimecode.hours;
    spiCmd.minutes = ltcTimecode.minutes;
    spiCmd.seconds = ltcTimecode.seconds;
    spiCmd.frames = ltcTimecode.frames;
    spiCmd.fps_thousandths = ltcTimecode.fps_thousandths;
    spiCmd.dropFrame = ltcTimecode.dropFrame;
    spiCmd.running = ltcTimecode.running;
    spiCmd.espNowConnected = ltcTimecode.espNowConnected;
    spiCmd.isPaired = isPaired ? 1 : 0;
    spiCmd.sampleRate = ltcTimecode.sampleRate;
    portEXIT_CRITICAL(&ltcTimerMux);

    // Send cam ID to LTC (max 2 chars)
    strncpy(spiCmd.deviceName, deviceName, 2);
    spiCmd.deviceName[2] = '\0';

    // Include current user bits (date from Master + local cam ID)
    buildCamIdFromName(currentUserBits);
    memcpy(spiCmd.userBits, currentUserBits, 8);

    performSPITransaction();
}

void sendRunningStateToSlave(bool running) {
    // Don't send running state commands when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked) {
        return;
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SET_RUNNING;
    spiCmd.running = running;
    spiCmd.espNowConnected = ltcTimecode.espNowConnected;
    spiCmd.isPaired = isPaired ? 1 : 0;
    performSPITransaction();
}

void sendEspNowStatusToSlave() {
    sendEspNowStatusToSlaveInternal(false);
}

void sendEspNowStatusToSlaveForced() {
    // Force send - used for critical status changes like unpair that must always be sent
    sendEspNowStatusToSlaveInternal(true);
}

void sendEspNowStatusToSlaveInternal(bool force) {
    // Don't send ESP-NOW status when LTC device is in JAM SYNC LOCKED/FREERUN mode or manually LOCKED
    // UNLESS force is true (used for critical updates like unpair)
    if (!force && (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked)) {
        return;  // Silently skip - this is called frequently
    }

    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_UPDATE_ESPNOW_STATUS;
    spiCmd.espNowConnected = ltcTimecode.espNowConnected;
    spiCmd.isPaired = isPaired ? 1 : 0;
    // Send cam ID to LTC (max 2 chars)
    strncpy(spiCmd.deviceName, deviceName, 2);
    spiCmd.deviceName[2] = '\0';
    // Include current user bits (date from Master + local cam ID)
    buildCamIdFromName(currentUserBits);
    memcpy(spiCmd.userBits, currentUserBits, 8);
    performSPITransaction();
}

void sendLockCommandToLTC() {
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_LOCK_DEVICE;
    spiCmd.running = 1;  // 1 = lock
    performSPITransaction();
}

// Set our LTC chip's output offset (frames)
void sendLtcOffsetToLTC(int8_t v) {
    SpiCmdGuard spiGuard;
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_SET_LTC_OFFSET;
    spiCmd.frames = (uint8_t)v;
    performSPITransaction();
}

// Ask our LTC chip to clear its reset log counters
void sendClearRstLogToLTC() {
    SpiCmdGuard spiGuard;
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_CLEAR_RSTLOG;
    performSPITransaction();
}

// ==================== Si5351 SETUP ====================
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
// MUST match the master's setupSi5351 exactly to keep clients drift-free.

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

void setupSi5351() {
    preferences.begin("timecode_client", true);
    uint32_t savedFreq = preferences.getUInt("si5351_freq", 48000);
    uint16_t savedFps = preferences.getUShort("last_fps", 25000);
    int32_t corr = preferences.getInt("si5351_corr", 0);
    preferences.end();

    // Get optimal rate based on saved FPS (not just the raw saved frequency)
    // This ensures correct rate even if preferences were corrupted or from old version
    uint32_t optimalRate = getSampleRateForFps(savedFps);

    // Validate saved frequency is a valid LTC sample rate, otherwise use FPS-based rate
    if (savedFreq != 47952 && savedFreq != 48000) {
        savedFreq = optimalRate;
    }

    if (si5351.init(SI5351_CRYSTAL_LOAD_8PF, 0, corr)) {
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

void updateSi5351Frequency(uint32_t freq, uint16_t fps_thousandths) {
    // PLL_A stays locked at 768 MHz — only the multisynth divider changes.
    // No PLL relock, no output glitch, instant frequency switch.
    si5351_applyMS(freq);

    // Save to preferences for persistence (including FPS for correct init on next boot)
    preferences.begin("timecode_client", false);
    preferences.putUInt("si5351_freq", freq);
    if (fps_thousandths > 0) {
        preferences.putUShort("last_fps", fps_thousandths);
    }
    preferences.end();
}

// ==================== TIMECODE UTILITY FUNCTIONS ====================
uint8_t getNominalFps(uint16_t fps_thousandths) {
    if (fps_thousandths == 23976) return 24;
    if (fps_thousandths == 29970) return 30;
    return fps_thousandths / 1000;
}

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

// ==================== ESP-NOW FUNCTIONS ====================
void loadPairedMaster() {
    preferences.begin("timecode_client", true);
    isPaired = preferences.getBool("paired", false);
    pairedMasterMac = preferences.getString("master_mac", "");

    if (isPaired && pairedMasterMac.length() == 17) {
        // sscanf with %X writes 4-byte unsigned int, but masterMac is uint8_t array
        unsigned int tempMac[6];
        sscanf(pairedMasterMac.c_str(), "%02X:%02X:%02X:%02X:%02X:%02X",
               &tempMac[0], &tempMac[1], &tempMac[2], &tempMac[3], &tempMac[4], &tempMac[5]);
        for (int i = 0; i < 6; i++) masterMac[i] = (uint8_t)tempMac[i];

        esp_now_peer_info_t peerInfo = {};
        memcpy(peerInfo.peer_addr, masterMac, 6);
        peerInfo.channel = 0;
        peerInfo.encrypt = false;

        esp_now_del_peer(masterMac);
        esp_now_add_peer(&peerInfo);
        // Don't set espNowConnected here - only set when master actually responds
    } else {
        isPaired = false;
        uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
        memcpy(masterMac, broadcastAddress, 6);
    }

    preferences.end();
}

void savePairedMaster() {
    preferences.begin("timecode_client", false);
    preferences.putBool("paired", isPaired);
    preferences.putString("master_mac", pairedMasterMac);
    preferences.end();
}

void loadDeviceName() {
    // Use local Preferences for consistency with saveDeviceName
    Preferences prefs;
    prefs.begin("timecode_client", true);
    String name = prefs.getString("device_name", "");
    strncpy(deviceName, name.c_str(), sizeof(deviceName) - 1);
    deviceName[sizeof(deviceName) - 1] = '\0';
    prefs.end();
    Serial.printf("Loaded device name: %s\n", strlen(deviceName) > 0 ? deviceName : "(not set)");
}

void saveDeviceName(const char* name) {
    // Use local Preferences to avoid conflicts with global object (ESP-NOW callback context)
    Preferences prefs;
    prefs.begin("timecode_client", false);
    prefs.putString("device_name", name);
    prefs.end();
    strncpy(deviceName, name, sizeof(deviceName) - 1);
    deviceName[sizeof(deviceName) - 1] = '\0';
    Serial.printf("Saved device name: %s\n", strlen(deviceName) > 0 ? deviceName : "(cleared)");
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

void registerESPNowCallbacks() {
    esp_now_register_send_cb([](const wifi_tx_info_t *tx_info, esp_now_send_status_t status) {
    });

    esp_now_register_recv_cb([](const esp_now_recv_info *recv_info, const uint8_t *incomingData, int len) {
        if (len == (int)sizeof(struct_message) && recv_info && memcmp(incomingData, "PSB", 4) == 0 &&
            memcmp(recv_info->src_addr, masterMac, 6) == 0) {   // the master's power-save beacon
            const struct_message* pb = (const struct_message*)incomingData;
            unsigned long nowMs = millis();
            psInterval = (pb->masterTimestamp >= 500 && pb->masterTimestamp <= 10000) ? (uint16_t)pb->masterTimestamp : 2000;
            psFlags = pb->userBits[0];
            lastMasterResponse = nowMs;
            psBeaconAt = nowMs;
            return;
        }
#if RADIO_SLEEP_TEST
        if (len >= 2 && (incomingData[0] == 0x20 || incomingData[0] == 0x1F) && incomingData[1] == 1 && isPaired &&
            recv_info && memcmp(recv_info->src_addr, masterMac, 6) == 0) {   // sleep test commands from our master
            if (millis() - rsLastCmd > 2000) {
                rsLastCmd = millis();
                if (incomingData[0] == 0x20) rsStartReq = true;
                else if (len >= 4) { rsOffSec = ((uint16_t)incomingData[2] << 8) | incomingData[3]; rsOffReq = true; }
            }
            return;
        }
#endif
        if (len >= 4 && incomingData[0] == 0x18 && incomingData[1] == 1 && recv_info && recv_info->rx_ctrl &&
            memcmp(recv_info->src_addr, masterMac, 6) == 0) {   // radio test probe from the master (kept out of the normal signal reading)
            uint8_t tag = incomingData[2];
            if (tag < 16) { rtSum[tag] += recv_info->rx_ctrl->rssi; if (rtCnt[tag] < 255) rtCnt[tag]++; }
            return;
        }
        if (recv_info && recv_info->rx_ctrl && memcmp(recv_info->src_addr, masterMac, 6) == 0) {   // signal of the master at this client
            int r = recv_info->rx_ctrl->rssi;
            masterRssi = (masterRssiAt && millis() - masterRssiAt < 30000) ? (int8_t)((masterRssi + r) / 2) : (int8_t)r;
            masterRssiAt = millis();
        }
        // Check for lock command (1 byte)
        if (len == 1 && incomingData[0] == 0xFF) {
            // Send lock command to LTC device via SPI
            sendLockCommandToLTC();
            return;
        }

        // The master asks to clear the reset log: {0x16, 1}. Only from our paired master.
        if (len >= 2 && incomingData[0] == 0x16 && isPaired && memcmp(recv_info->src_addr, masterMac, 6) == 0) {
            rstClearPending = true;
            return;
        }

        // The master sets our LTC output offset: {0x21, 1, frames (signed)}. Only from our paired master.
        if (len >= 3 && incomingData[0] == 0x21 && incomingData[1] == 1 && isPaired && recv_info &&
            memcmp(recv_info->src_addr, masterMac, 6) == 0) {
            ltcOfsValue = (int8_t)incomingData[2];
            ltcOfsPending = true;
            return;
        }

        // The master sets TX power / ESP-NOW rate: {0x17, 1, txIndex, rateIndex}. Only from our paired master.
        if (len >= 4 && incomingData[0] == 0x17 && incomingData[1] == 1 && incomingData[2] <= 7 && incomingData[3] <= 1 &&
            isPaired && memcmp(recv_info->src_addr, masterMac, 6) == 0) {
            radioPendTx = 3;   // power is fixed; the byte is ignored
            radioPendRate = incomingData[3];
            radioPendPs = (len >= 5) ? (incomingData[4] ? 1 : 0) : (psEnabled ? 1 : 0);
            radioPendFlag = true;
            return;
        }

        // Radio test: 0x19 = start your sweep, 0x1B = send me your table. Only from our paired master.
        if (len >= 2 && (incomingData[0] == 0x19 || incomingData[0] == 0x1B) && incomingData[1] == 1 &&
            isPaired && memcmp(recv_info->src_addr, masterMac, 6) == 0) {
            if (incomingData[0] == 0x19) rtGoPending = true; else rtReplyPending = true;
            return;
        }

        // Check for Remote OTA command
        if (len > 0 && incomingData[0] == ESPNOW_OTA_CMD) {
            handleRemoteOTACommand(recv_info->src_addr, incomingData, len);
            return;
        }

        // Regular timecode sync packet
        if (len == sizeof(struct_message)) {
            struct_message receivedData;
            memcpy(&receivedData, incomingData, sizeof(receivedData));
            processESPNowPacket(recv_info->src_addr, receivedData);
        }
    });
}

void setupESP_NOW() {
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);  // Balanced power saving for stability
    radioApplyTx();  // TX power chosen on the master's page

    if (esp_now_init() != ESP_OK) {
        return;
    }

    // REDUCE TX POWER to minimize voltage ripple on TCXO rail
    // Default is ~19.5dBm. 11dBm reduces peak current significantly; the master's page sets the value (and the ESP-NOW rate).
    radioApplyAll();

    registerESPNowCallbacks();

    uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, broadcastAddress, 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if (esp_now_add_peer(&peerInfo) == ESP_OK) {
        memcpy(masterMac, broadcastAddress, 6);
    }

    loadPairedMaster();

    wirelessInitialized = true;  // Mark wireless as initialized
}

#if RADIO_SLEEP_TEST
// ===================== Radio sleep experiment (test image only) =====================
// Started from the master's web page ("Radio sleep test"): shows on that page what the client is doing and the results,
// so no serial port is needed. The phases last 10 s each; read the current on the 3.3 V rail in the last 5 s.
//   A  listening all the time (what the firmware does today)
//   B  radio completely off (the floor)
//   C  radio switched off and on again: wake, send a heartbeat, wait 50 ms for the answer, sleep; every 2 s
//   D  ESP-IDF's own ESP-NOW sleep (wake window 50 ms, interval 2 s), not connected to the master
//   E  the same, connected to the master's access point (only if built with RSLEEP_AP_SSID / RSLEEP_AP_PASS)
// The page can also switch this client's radio off for N seconds (to see the difference on a power meter).
// When the test has finished the client restarts and behaves like a normal release again.
// Packets: client -> master {0x1D,1,phase,secHi,secLo} (what it is doing now), {0x1E,1,index,text...} (a result line);
//          master -> client {0x20,1} (start the test), {0x1F,1,secHi,secLo} (radio off for N seconds).
static volatile uint32_t rsAcks = 0;      // HB_ACK counter (the master answers every heartbeat at once)
static volatile bool rsRadioOff = false;  // the normal heartbeat stays quiet while the radio is off

static uint32_t rsWake(bool* ok) {        // radio on again; returns how long that took (microseconds)
    uint32_t t0 = micros();
    esp_err_t a = esp_wifi_start();
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    esp_err_t b = esp_now_init();
    radioApplyAll();
    registerESPNowCallbacks();
    esp_now_peer_info_t p = {};
    p.channel = 0;
    p.encrypt = false;
    memset(p.peer_addr, 0xFF, 6);
    esp_now_add_peer(&p);
    memcpy(p.peer_addr, masterMac, 6);
    esp_now_add_peer(&p);
    rsRadioOff = false;
    *ok = (a == ESP_OK) && (b == ESP_OK);
    return micros() - t0;
}

static void rsSleep() {                   // radio off, in the documented order
    rsRadioOff = true;
    esp_now_deinit();
    esp_wifi_stop();
}

// tell the master's page what we are doing (3 copies: the packet has no acknowledgement)
static void rsAnnounce(char phase, uint16_t sec) {
    uint8_t pkt[5] = { 0x1D, 1, (uint8_t)phase, (uint8_t)(sec >> 8), (uint8_t)sec };
    for (int i = 0; i < 3; i++) { esp_now_send(masterMac, pkt, sizeof(pkt)); vTaskDelay(pdMS_TO_TICKS(40)); }
    vTaskDelay(pdMS_TO_TICKS(60));        // let the last frame leave before the radio is switched off
}

static void rsLine(uint8_t idx, const char* text) {
    uint8_t pkt[200];
    size_t n = strlen(text);
    if (n > 190) n = 190;
    pkt[0] = 0x1E; pkt[1] = 1; pkt[2] = idx;
    memcpy(pkt + 3, text, n);
    for (int i = 0; i < 3; i++) { esp_now_send(masterMac, pkt, 3 + n); vTaskDelay(pdMS_TO_TICKS(60)); }
}

static void rsWait(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms)); }

// send a heartbeat and wait up to waitMs for the master's answer; returns the answer time in ms, or -1
static int rsPing(uint32_t waitMs) {
    uint32_t a0 = rsAcks, t0 = millis();
    sendESPNowHeartbeat();
    while (millis() - t0 < waitMs) {
        if (rsAcks != a0) return (int)(millis() - t0);
        vTaskDelay(1);
    }
    return -1;
}

static void rsRunTest() {
    const uint32_t PHASE_MS = 10000;
    char sum[5][190];
    int n = 0;
    bool ok;

    rsAnnounce('S', 10);                  // 10 s to get the meter ready
    rsWait(10000);

    // ---- A: listening all the time
    rsAnnounce('A', PHASE_MS / 1000);
    uint32_t a0 = rsAcks;
    rsWait(PHASE_MS);
    snprintf(sum[n++], sizeof(sum[0]), "A listening: %lu answers from the master in %lu s", (unsigned long)(rsAcks - a0), (unsigned long)(PHASE_MS / 1000));

    // ---- B: radio off
    rsAnnounce('B', PHASE_MS / 1000);
    rsSleep();
    rsWait(PHASE_MS);
    uint32_t w = rsWake(&ok);
    snprintf(sum[n++], sizeof(sum[0]), "B off: wake-up from fully off took %lu ms (%s)", (unsigned long)(w / 1000), ok ? "ok" : "FAILED");

    // ---- C: switch off and on every 2 s
    rsAnnounce('C', PHASE_MS / 1000);
    uint32_t cycles = 0, hits = 0, wMin = 0xFFFFFFFF, wMax = 0, wSum = 0, ackSum = 0, fails = 0;
    uint32_t cStart = millis();
    while (millis() - cStart < PHASE_MS) {
        uint32_t t0 = millis();
        bool wok;
        uint32_t wk = rsWake(&wok);
        if (!wok) fails++;
        int ack = rsPing(50);
        cycles++;
        if (ack >= 0) { hits++; ackSum += ack; }
        wSum += wk;
        if (wk < wMin) wMin = wk;
        if (wk > wMax) wMax = wk;
        rsSleep();
        while (millis() - t0 < 2000) vTaskDelay(pdMS_TO_TICKS(10));
    }
    rsWake(&ok);
    snprintf(sum[n++], sizeof(sum[0]), "C cycling: %lu cycles, wake-up min/avg/max %lu/%lu/%lu ms, master answered %lu (avg %lu ms), failed wakes %lu",
             (unsigned long)cycles, (unsigned long)(wMin / 1000), (unsigned long)(cycles ? wSum / cycles / 1000 : 0), (unsigned long)(wMax / 1000),
             (unsigned long)hits, (unsigned long)(hits ? ackSum / hits : 0), (unsigned long)fails);

    // ---- D: ESP-IDF's own sleep, not connected
    rsAnnounce('D', PHASE_MS / 1000);
    esp_err_t e1 = esp_now_set_wake_window(50);
    esp_err_t e2 = esp_wifi_connectionless_module_set_wake_interval(2000);
    esp_err_t e3 = esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    uint32_t dTry = 0, dHit = 0;
    uint32_t dStart = millis();
    while (millis() - dStart < PHASE_MS) {
        dTry++;
        if (rsPing(300) >= 0) dHit++;
        rsWait(1700);
    }
    snprintf(sum[n++], sizeof(sum[0]), "D built-in sleep, not connected: %s / %s / %s; master answered %lu of %lu heartbeats",
             esp_err_to_name(e1), esp_err_to_name(e2), esp_err_to_name(e3), (unsigned long)dHit, (unsigned long)dTry);

#ifdef RSLEEP_AP_SSID
    // ---- E: the same, connected to the master's access point
    rsAnnounce('E', PHASE_MS / 1000);
    WiFi.begin(RSLEEP_AP_SSID, RSLEEP_AP_PASS);
    uint32_t eT = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - eT < 20000) vTaskDelay(pdMS_TO_TICKS(250));
    bool conn = WiFi.status() == WL_CONNECTED;
    esp_err_t f1 = esp_now_set_wake_window(50);
    esp_err_t f2 = esp_wifi_connectionless_module_set_wake_interval(2000);
    esp_err_t f3 = esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    uint32_t eTry = 0, eHit = 0;
    uint32_t eStart = millis();
    while (millis() - eStart < PHASE_MS) {
        eTry++;
        if (rsPing(300) >= 0) eHit++;
        rsWait(1700);
    }
    snprintf(sum[n++], sizeof(sum[0]), "E built-in sleep, connected=%d: %s / %s / %s; master answered %lu of %lu heartbeats",
             conn, esp_err_to_name(f1), esp_err_to_name(f2), esp_err_to_name(f3), (unsigned long)eHit, (unsigned long)eTry);
    WiFi.disconnect();
#endif

    // back to a clean radio, then report
    rsSleep();
    rsWake(&ok);
    rsWait(500);
    for (int i = 0; i < n; i++) rsLine((uint8_t)i, sum[i]);
    rsAnnounce('Z', 0);
    rsWait(2000);
    ESP.restart();                        // normal operation again
}

static void rsTestTask(void*) {
    uint32_t lastHello = 0;
    for (;;) {
        if (!rsRadioOff && isPaired && millis() - lastHello >= 5000) {   // "the test firmware is here": the master's page shows it
            lastHello = millis();
            uint8_t hello[5] = { 0x1D, 1, 'T', 0, 0 };
            esp_now_send(masterMac, hello, sizeof(hello));
        }
        if (rsOffReq) {
            rsOffReq = false;
            uint16_t sec = rsOffSec;
            if (sec < 5) sec = 5;
            if (sec > 600) sec = 600;
            bool ok;
            rsAnnounce('O', sec);
            rsSleep();
            rsWait((uint32_t)sec * 1000);
            rsWake(&ok);
            rsWait(300);
            rsAnnounce('Z', 0);
        } else if (rsStartReq) {
            rsStartReq = false;
            if (isPaired) rsRunTest();
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

static void rsTestStart() {
    xTaskCreatePinnedToCore(rsTestTask, "rsTest", 12288, NULL, 1, NULL, 1);
}
#endif   // RADIO_SLEEP_TEST

void processESPNowPacket(const uint8_t *mac, const struct_message& data) {
    // Skip processing if wireless is disabled
    if (!wirelessEnabled) {
        return;
    }

    char macStr[18];
    snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    // Handle pairing requests (must be before isPaired check)
    if (MSG_TYPE_IS(data, "PAIR_OK") || MSG_TYPE_IS(data, "PAIR_CONF")) {
        if (isPaired) return;

        esp_now_peer_info_t peerInfo = {};
        memcpy(peerInfo.peer_addr, mac, 6);
        peerInfo.channel = 0;
        peerInfo.encrypt = false;

        esp_now_del_peer(mac);
        if (esp_now_add_peer(&peerInfo) == ESP_OK) {
            isPaired = true;
            pairedMasterMac = String(macStr);
            memcpy(masterMac, mac, 6);
            ltcTimecode.espNowConnected = true;
            savePairedMaster();
            sendESPNowRegistration();

            syncLedBlinking = true;
            syncLedBlinksRemaining = 3;
            syncLedStartTime = millis();
            digitalWrite(SYNC_LED_PIN, HIGH);
        }
        return;
    }

    // ============ SECURITY FIX: Check pairing BEFORE processing commands ============
    if (!isPaired) {
        return;  // Reject all commands from unpaired state
    }

    // Verify packet is from our paired master (prevents rogue master attacks)
    if (memcmp(mac, masterMac, 6) != 0) {
        return;  // Reject commands from wrong master
    }
    // ============ END SECURITY FIX ============

    // UNPAIR now protected - only accepted from paired master
    if (MSG_TYPE_IS(data, "UNPAIR")) {
        unpairMaster();
        return;
    }

    // SET_NAME - Master is setting our device name
    if (MSG_TYPE_IS(data, "SET_NAME")) {
        saveDeviceName(data.deviceName);
        // Forward new name to Client LTC for OLED display
        sendEspNowStatusToSlaveForced();
        return;
    }

    lastMasterResponse = millis();

    if (!ltcTimecode.espNowConnected) {
        ltcTimecode.espNowConnected = true;
        // Immediately update slave
        sendEspNowStatusToSlave();
        lastEspNowState = true;
    }

    if (MSG_TYPE_IS(data, "SYNC")) {
        // IMPORTANT: Don't accept ESP-NOW sync if device is in JAM SYNC mode or locked!
        // If synced to external LTC, we don't want master to overwrite that timecode.
        if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcJamSyncMode == JAM_SYNC_SEARCHING || ltcSystemLocked) {
            // Still update connection status and timestamp
            lastMasterTimestamp = data.masterTimestamp;
            lastMasterResponse = millis();
            return;  // Reject sync but maintain connection
        }

        lastMasterTimestamp = data.masterTimestamp;

        portENTER_CRITICAL(&ltcTimerMux);
        ltcTimecode.hours = data.hours;
        ltcTimecode.minutes = data.minutes;
        ltcTimecode.seconds = data.seconds;
        ltcTimecode.frames = data.frames;
        ltcTimecode.fps_thousandths = data.fps_thousandths;
        ltcTimecode.dropFrame = data.dropFrame;
        ltcTimecode.running = data.running;
        ltcTimecode.synced = true;
        ltcTimecode.espNowConnected = true;
        ltcTimecode.lastSyncMillis = millis();
        portEXIT_CRITICAL(&ltcTimerMux);

        // Update SI5351 frequency if sample rate changed
        // Allow 47952 (NTSC optimal) or 48000 (integer fps)
        bool sampleRateChanged = false;
        if (data.sampleRate == 47952 || data.sampleRate == 48000) {
            if (ltcTimecode.sampleRate != data.sampleRate) {
                sampleRateChanged = true;
                updateSi5351Frequency(data.sampleRate, data.fps_thousandths);
            }
            portENTER_CRITICAL(&ltcTimerMux);
            ltcTimecode.sampleRate = data.sampleRate;
            portEXIT_CRITICAL(&ltcTimerMux);
        }

        // Wait for Si5351 to stabilize if sample rate changed
        // This prevents timing glitches during FPS transitions
        if (sampleRateChanged) {
            delay(10);  // 10ms for Si5351 clock stabilization
        }

        // === CRITICAL FIX: Send SPI IMMEDIATELY from callback ===
        // Don't defer to task - that adds 50-100ms of variable delay!
        SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
        memset(&spiCmd, 0, sizeof(spiCmd));
        spiCmd.command = CMD_SYNC_TIMECODE;
        if (data.uptime == 0xC1EA2000UL) spiCmd._padding[0] = 0xC5;   // master: restart the clock check
        spiCmd.hours = data.hours;
        spiCmd.minutes = data.minutes;
        spiCmd.seconds = data.seconds;
        spiCmd.frames = data.frames;
        spiCmd.fps_thousandths = data.fps_thousandths;
        spiCmd.dropFrame = data.dropFrame;
        spiCmd.running = data.running;
        spiCmd.espNowConnected = true;
        spiCmd.isPaired = isPaired ? 1 : 0;  // Include pairing status
        spiCmd.sampleRate = data.sampleRate;
        // Send cam ID to LTC (max 2 chars)
        strncpy(spiCmd.deviceName, deviceName, 2);
        spiCmd.deviceName[2] = '\0';

        // Build user bits: copy date from Master, use local cam ID
        memcpy(currentUserBits, data.userBits, 6);      // Copy date nibbles (0-5) from Master
        buildCamIdFromName(currentUserBits);            // Replace cam ID (nibbles 6-7) with local
        memcpy(spiCmd.userBits, currentUserBits, 8);

        // Direct SPI transaction (fast, ~1ms)
        portENTER_CRITICAL(&spiMux);
        digitalWrite(SPI_CS, LOW);
        delayMicroseconds(500);
        hspi.transferBytes((uint8_t*)&spiCmd, (uint8_t*)&spiStatus, SPI_PACKET_SIZE);
        digitalWrite(SPI_CS, HIGH);
        portEXIT_CRITICAL(&spiMux);
        // === END CRITICAL FIX ===

        syncLedBlinking = true;
        syncLedBlinksRemaining = 3;
        syncLedStartTime = millis();
        digitalWrite(SYNC_LED_PIN, HIGH);
    }
    else if (MSG_TYPE_IS(data, "RUN")) {
        // Don't accept RUN state changes if in JAM SYNC mode or locked
        // The external LTC source controls the running state when jam synced
        if (ltcJamSyncMode == JAM_SYNC_LOCKED || ltcJamSyncMode == JAM_SYNC_FREERUN || ltcSystemLocked) {
            return;
        }

        portENTER_CRITICAL(&ltcTimerMux);
        ltcTimecode.running = data.running;
        portEXIT_CRITICAL(&ltcTimerMux);

        sendRunningStateToSlave(data.running);
    }
    else if (MSG_TYPE_IS(data, "HB_ACK")) {
        // Heartbeat acknowledged - connection is alive
        ltcTimecode.espNowConnected = true;
#if RADIO_SLEEP_TEST
        rsAcks++;
#endif
    }
}

void sendPairingRequest() {
    if (isPaired || !wirelessEnabled) return;

    struct_message msg;
    memset(&msg, 0, sizeof(msg));
    SET_MSG_TYPE(msg, "PAIR_REQ");
    strcpy(msg.mac, WiFi.macAddress().c_str());
    msg.timestamp = millis();

    uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_now_send(broadcastAddress, (uint8_t*)&msg, sizeof(msg));
}

void sendESPNowRegistration() {
    if (!wirelessEnabled) return;

    struct_message msg;
    memset(&msg, 0, sizeof(msg));
    SET_MSG_TYPE(msg, "REG");
    strcpy(msg.mac, WiFi.macAddress().c_str());
    msg.timestamp = millis();
    msg.uptime = (millis() - systemStartTime) / 1000;

    esp_now_send(masterMac, (uint8_t*)&msg, sizeof(msg));
}

// ---- Radio test (power sweep) - started from the master's page. We (1) transmit probe frames to the master at every
// TX power / rate, (2) record how loudly we hear the master's probes, (3) report that table when asked.
// A probe is {0x18, 1, tag, seq} with tag = rateIdx*8 + 3 (levelIdx is always 3 = 11 dBm) (rate 0 = 1 Mbps, 1 = 6 Mbps).

static void radioTestClientLoop() {
    unsigned long now = millis();
    if (rtGoPending) {
        rtGoPending = false;
        memset(rtSum, 0, sizeof(rtSum));
        memset(rtCnt, 0, sizeof(rtCnt));
        rtRun = true; rtCombo = 0; rtProbe = 0; rtAt = now + 100;
    }
    if (rtReplyPending) {
        rtReplyPending = false;
        uint8_t pkt[34];
        pkt[0] = 0x1C; pkt[1] = 1;
        for (int i = 0; i < 16; i++) {
            pkt[2 + i] = rtCnt[i] ? (uint8_t)(int8_t)(rtSum[i] / rtCnt[i]) : 0;   // average dBm we heard the master at
            pkt[18 + i] = rtCnt[i];
        }
        esp_now_send(masterMac, pkt, sizeof(pkt));
    }
    if (!rtRun || now < rtAt) return;
    uint8_t rate = rtCombo, level = 3;      // one combination per rate, at the fixed 11 dBm
    if (rtProbe == 0) {                       // switch to this power / rate, let the radio settle
        radioApplyRateTo(rate);
        rtProbe = 1;
        rtAt = now + 30;
        return;
    }
    uint8_t pkt[4] = { 0x18, 1, (uint8_t)(rate * 8 + level), rtProbe };
    esp_now_send(masterMac, pkt, sizeof(pkt));
    rtProbe++;
    rtAt = now + 35;
    if (rtProbe > 5) {                        // 5 probes sent for this combination
        rtProbe = 0;
        rtCombo++;
        rtAt = now + 40;
        if (rtCombo >= 2) { rtRun = false; radioApplyAll(); }   // done: back to the saved setting
    }
}

// Reset log of both chips of this unit -> master (first byte 0x15; the master shows it on its web page)
void sendResetInfoToMaster() {
    int8_t heard = (masterRssiAt && millis() - masterRssiAt < 20000) ? masterRssi : 0;   // 0 = not heard recently
    uint8_t pkt[17] = { 0x15, 1, rstSelf[0], rstSelf[1], rstSelf[2], rstSelf[3],
                        ltcRst[0], ltcRst[1], ltcRst[2], ltcRst[3], (uint8_t)(ltcRstValid ? 1 : 0),
                        radioTxIdx, radioRateIdx,                 // [11] [12]: our TX power / ESP-NOW rate setting
                        radioHwNow(), (uint8_t)heard,             // [13] what our radio reports, [14] how well we hear the master (dBm)
                        (uint8_t)(psEnabled ? 1 : 0),             // [15] radio power save on
                        (uint8_t)ltcOfsNow };                     // [16] LTC output offset (frames, signed)
    esp_now_send(masterMac, pkt, sizeof(pkt));
}

void sendESPNowHeartbeat() {
    if (!isPaired || !wirelessEnabled) return;
    PsGuard psg;
    if (psRadioOff) return;   // power save: the radio is asleep
#if RADIO_SLEEP_TEST
    if (rsRadioOff) return;
#endif
    if (radioPendFlag) {
        radioPendFlag = false;
        radioTxIdx = radioPendTx;
        radioRateIdx = radioPendRate;
        psEnabled = radioPendPs != 0;
        radioSave();
        radioApplyAll();
        sendResetInfoToMaster();                // report the new setting at once
    }
    if (rstClearPending) {
        rstClearPending = false;
        rstLogClear();                          // this chip
        sendClearRstLogToLTC();                 // our LTC chip
        ltcRst[1] = ltcRst[2] = ltcRst[3] = 0;
        sendResetInfoToMaster();                // report the cleared state at once
    }
    static uint8_t rstTick = 0;
    if (rstTick++ % 5 == 0) sendResetInfoToMaster();   // every 5th heartbeat (and the first)

    struct_message msg;
    memset(&msg, 0, sizeof(msg));
    SET_MSG_TYPE(msg, "HB");
    strcpy(msg.mac, WiFi.macAddress().c_str());

    portENTER_CRITICAL(&ltcTimerMux);
    msg.hours = ltcTimecode.hours;
    msg.minutes = ltcTimecode.minutes;
    msg.seconds = ltcTimecode.seconds;
    msg.frames = ltcTimecode.frames;
    msg.fps_thousandths = ltcTimecode.fps_thousandths;
    msg.running = ltcTimecode.running;
    msg.synced = ltcTimecode.synced;
    msg.batteryLevel = ltcBatteryLevel;
    msg.batteryPresent = ltcBatteryPresent ? 1 : 0;
    portEXIT_CRITICAL(&ltcTimerMux);

    msg.timestamp = millis();
    msg.uptime = (millis() - systemStartTime) / 1000;
    msg.jamSyncMode = ltcJamSyncMode;  // Include jam sync mode
    // Include version info for remote OTA
    msg.utilityVersionMajor = FIRMWARE_VERSION_MAJOR;
    msg.utilityVersionMinor = FIRMWARE_VERSION_MINOR;
    msg.utilityVersionPatch = FIRMWARE_VERSION_PATCH;
    msg.ltcVersionMajor = ltcVersionMajor;
    msg.ltcVersionMinor = ltcVersionMinor;
    msg.ltcVersionPatch = ltcVersionPatch;
    msg.otaState = remoteOtaState;
    // Clock check of our LTC chip, in two fields the master never read before (old masters ignore it)
    if (ltcDiagValid) {
        msg.sampleRate = (uint32_t)ltcDiagNetLost;
        msg.masterTimestamp = 0xD1000000UL | ltcDiagMaxGapUs;
        memcpy(msg.userBits, &ltcDiagSinceS, 4);   // userBits are unused in a heartbeat
        msg.userBits[4] = 0xD2;
        // when did windows with wrong pulse counts happen: first / last second (255 = none or >= 255 s), count
        msg.userBits[5] = (ltcErrFirstS == 0xFFFF) ? 255 : (ltcErrFirstS > 254 ? 254 : (uint8_t)ltcErrFirstS);
        msg.userBits[6] = (ltcErrWins == 0) ? 255 : (ltcErrLastS > 254 ? 254 : (uint8_t)ltcErrLastS);
        msg.userBits[7] = ltcErrWins > 255 ? 255 : (uint8_t)ltcErrWins;
    }
    // Include device name
    strncpy(msg.deviceName, deviceName, sizeof(msg.deviceName) - 1);
    msg.deviceName[sizeof(msg.deviceName) - 1] = '\0';

    esp_now_send(masterMac, (uint8_t*)&msg, sizeof(msg));
}

void unpairMaster() {
    if (isPaired) {
        struct_message msg;
        memset(&msg, 0, sizeof(msg));
        SET_MSG_TYPE(msg, "UNPAIR_REQ");
        strcpy(msg.mac, WiFi.macAddress().c_str());
        esp_now_send(masterMac, (uint8_t*)&msg, sizeof(msg));

        esp_now_del_peer(masterMac);

        preferences.begin("timecode_client", false);
        preferences.putBool("paired", false);
        preferences.remove("master_mac");
        preferences.end();

        isPaired = false;
        pairedMasterMac = "";
        ltcTimecode.espNowConnected = false;
        ltcTimecode.synced = false;

        uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
        memcpy(masterMac, broadcastAddress, 6);
    }
}

void checkPairButton() {
    bool buttonState = digitalRead(PAIR_BUTTON_PIN) == LOW;

    if (buttonState && !pairButtonPressed) {
        // Button just pressed - start timing
        pairButtonPressed = true;
        pairButtonPressTime = millis();
    }
    else if (buttonState && pairButtonPressed) {
        // Button still held - check if 3 seconds reached
        if (isPaired && (millis() - pairButtonPressTime >= UNPAIR_HOLD_TIME)) {
            // Trigger unpair immediately when 3 seconds is reached (while still holding)
            unpairMaster();

            // Immediately notify LTC device of unpair status via SPI (forced - bypasses JAM SYNC/lock checks)
            sendEspNowStatusToSlaveForced();

            syncLedBlinking = true;
            syncLedBlinksRemaining = 5;
            syncLedStartTime = millis();
            syncLedState = true;
            digitalWrite(SYNC_LED_PIN, HIGH);

            // Reset button state to prevent re-triggering while still held
            pairButtonPressed = false;
        }
    }
    else if (!buttonState && pairButtonPressed) {
        // Button released - reset state
        pairButtonPressed = false;
    }
}

float getFpsFloat(uint16_t fps_thousandths) {
    if (fps_thousandths == 23976) return 24000.0f / 1001.0f;  // Exact 23.976023976...
    if (fps_thousandths == 29970) return 30000.0f / 1001.0f;  // Exact 29.970029970...
    return fps_thousandths / 1000.0f;
}

// Get optimal sample rate for given FPS - eliminates timing drift for NTSC rates
uint32_t getSampleRateForFps(uint16_t fps_thousandths) {
    switch (fps_thousandths) {
        case 23976:  // 23.976 fps - NTSC film
        case 29970:  // 29.97 fps - NTSC video
            return 47952;  // 48000000/1001 Hz - gives exact integer samples/bit
        default:
            return 48000;  // Standard rate for integer FPS (24, 25, 30)
    }
}

void updateSyncLED() {
    if (!syncLedBlinking) return;

    unsigned long currentTime = millis();
    if (currentTime - syncLedStartTime >= 150) {
        syncLedState = !syncLedState;
        digitalWrite(SYNC_LED_PIN, syncLedState ? HIGH : LOW);
        syncLedStartTime = currentTime;

        if (!syncLedState) {
            syncLedBlinksRemaining--;
            if (syncLedBlinksRemaining <= 0) {
                syncLedBlinking = false;
                digitalWrite(SYNC_LED_PIN, LOW);
            }
        }
    }
}

void updateStatusLED() {
    unsigned long currentTime = millis();
    unsigned long interval = ltcTimecode.espNowConnected ?
                            (ltcTimecode.synced ? 1000 : 500) : 200;

    if (currentTime - statusLedLastUpdate > interval) {
        statusLedLastUpdate = currentTime;
        statusLedState = !statusLedState;
        digitalWrite(STATUS_LED_PIN, statusLedState ? HIGH : LOW);
    }
}

// ==================== CONNECTION MANAGEMENT - IMPROVED ====================
void checkConnectionAndHeartbeat() {
    if (!wirelessEnabled) return;  // Skip if wireless is disabled

    unsigned long currentTime = millis();

    // Check for timeout - IMPROVED with immediate slave notification
    if (ltcTimecode.espNowConnected && currentTime - lastMasterResponse > MASTER_TIMEOUT) {
        ltcTimecode.espNowConnected = false;
        ltcTimecode.synced = false;

        // IMMEDIATELY notify slave of disconnection
        sendEspNowStatusToSlave();
        lastEspNowState = false;
    }

    if (ltcTimecode.espNowConnected) {
        if (currentTime - lastHeartbeat >= HEARTBEAT_INTERVAL) {
            lastHeartbeat = currentTime;
            sendESPNowHeartbeat();
        }
    } else {
        if (currentTime - ltcTimecode.lastConnectionAttempt >= RECONNECT_INTERVAL) {
            ltcTimecode.lastConnectionAttempt = currentTime;
            if (isPaired) {
                sendESPNowHeartbeat();
            } else {
                sendPairingRequest();
            }
        }
    }
}

void checkPairingRequests() {
    if (isPaired || !wirelessEnabled) return;

    static unsigned long lastPairingRequest = 0;
    unsigned long currentTime = millis();
    if (currentTime - lastPairingRequest >= 1000) {
        lastPairingRequest = currentTime;
        sendPairingRequest();
    }
}

// ==================== UTILITY TASK - IMPROVED ====================
void utilityTask(void* parameter) {
    static unsigned long lastDebug = 0;
    static unsigned long lastIntCount = 0;
    static unsigned long lastSlaveStatusPoll = 0;

    while (true) {
        unsigned long now = millis();

        // Check OTA button and handle OTA mode
        checkOTAButton();
        if (otaMode) {
            handleOTAModeInLoop();
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;  // Skip normal operations while in OTA mode
        }

        // Process remote OTA state machine
        if (rotaState != ROTA_IDLE) {
            processRemoteOTA();
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;  // Skip normal operations during remote OTA
        }

        // Handle pending wireless state changes (deferred to avoid Si5351 glitch)
        if (wirelessState == WIRELESS_PENDING_DISABLE && now >= wirelessStateChangeTime) {
            wirelessState = WIRELESS_DISABLED;
            disableWireless();
        } else if (wirelessState == WIRELESS_PENDING_ENABLE && now >= wirelessStateChangeTime) {
            wirelessState = WIRELESS_ACTIVE;
            enableWireless();
        }

        // Lock mode optimization
        uint16_t pollInterval = ltcSystemLocked ? 2000 : 200;
        uint16_t taskDelay = ltcSystemLocked ? 500 : 100;

        if (now - lastSlaveStatusPoll >= pollInterval) {
            lastSlaveStatusPoll = now;
            pollLTCStatus();
        }
        if (ltcOfsPending) {   // LTC output offset from the master's page
            ltcOfsPending = false;
            sendLtcOffsetToLTC(ltcOfsValue);
            ltcOfsNow = ltcOfsValue;   // the LTC applies it on this command (its status confirms it from the next poll)
            PsGuard g;
            if (!psRadioOff) sendResetInfoToMaster();   // report it back at once (radio may be asleep with power save)
        }

        // Skip wireless operations when locked
        if (!ltcSystemLocked) {
            checkConnectionAndHeartbeat();
            if (!isPaired) {
                checkPairingRequests();
            }
        }

        checkPairButton();
        updateSyncLED();
        updateStatusLED();
        if (!ltcSystemLocked) radioTestClientLoop();   // radio test (power sweep), see above

        // Periodic status updates
        if (wirelessEnabled && !ltcSystemLocked && now - lastEspNowUpdate >= ESPNOW_UPDATE_INTERVAL) {
            lastEspNowUpdate = now;
            bool statusChanged = (lastEspNowState != ltcTimecode.espNowConnected);
            bool periodicUpdate = ((now / 10000) % 10 == 0) && ((now % 10000) < ESPNOW_UPDATE_INTERVAL);
            if (statusChanged || periodicUpdate) {
                sendEspNowStatusToSlave();
                if (statusChanged) {
                    lastEspNowState = ltcTimecode.espNowConnected;
                }
            }
        }

        // Debug output
        uint32_t debugInterval = ltcSystemLocked ? 60000 : 10000;
        if (now - lastDebug >= debugInterval) {
            unsigned long timingNow = millis();
            unsigned long currentCount = si5351InterruptCount;
            lastIntCount = currentCount;
            lastDebug = timingNow;
        }

        // Simple delay for task scheduling (finer steps while a radio test is running)
        vTaskDelay(pdMS_TO_TICKS(rtRun ? 5 : taskDelay));
    }
}

// ==================== OTA UPDATE FUNCTIONS ====================
String generateOTASSID() {
    uint64_t chipId = ESP.getEfuseMac();
    char suffix[5];
    snprintf(suffix, sizeof(suffix), "%04X", (uint16_t)(chipId & 0xFFFF));
    return "TCC-U-" + String(suffix);  // TimeCode Client - Utility
}

void handleOTARoot() {
    String html = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>OTA Update - Client Utility</title>
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
        .partner { margin-top: 20px; padding: 15px; background: #0f3460; border-radius: 5px; border: 1px dashed #4ecca3; }
    </style>
</head>
<body>
    <div class='container'>
        <h1>OTA Update</h1>
        <div class='info'>
            <p><strong>Device:</strong> Client Utility</p>
            <p><strong>Version:</strong> )rawliteral" + String(FIRMWARE_VERSION_MAJOR) + "." + String(FIRMWARE_VERSION_MINOR) + "." + String(FIRMWARE_VERSION_PATCH) + R"rawliteral(</p>
            <p><strong>Hardware ID:</strong> )rawliteral" + otaSSID.substring(6) + R"rawliteral(</p>
        </div>
        <div class='warning'>
            Only upload CLIENT UTILITY firmware to this device!
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
            <p><strong>Partner Device (LTC):</strong> )rawliteral" + (ltcOTASSID.length() > 0 ? ltcOTASSID : "TCC-L-" + otaSSID.substring(6)) + R"rawliteral(</p>
            <p>Connect to that SSID to update the LTC device</p>
            <p>Password: )rawliteral" + String(OTA_PASSWORD) + R"rawliteral(</p>
        </div>
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
        // Validate filename - must contain "client" AND "utility" (case insensitive)
        String filename = upload.filename;
        filename.toLowerCase();

        bool hasMaster = filename.indexOf("master") != -1;
        bool hasClient = filename.indexOf("client") != -1;
        bool hasLtc = filename.indexOf("ltc") != -1;
        bool hasUtility = filename.indexOf("utility") != -1;

        if (!hasClient || !hasUtility) {
            // Wrong firmware file
            otaFirmwareValid = false;
            if (hasMaster && hasUtility) {
                otaFirmwareError = "Wrong firmware! This is CLIENT UTILITY but you uploaded MASTER UTILITY firmware.";
            } else if (hasClient && hasLtc) {
                otaFirmwareError = "Wrong firmware! This is CLIENT UTILITY but you uploaded CLIENT LTC firmware.";
            } else if (hasMaster && hasLtc) {
                otaFirmwareError = "Wrong firmware! This is CLIENT UTILITY but you uploaded MASTER LTC firmware.";
            } else if (!hasUtility && hasLtc) {
                otaFirmwareError = "Wrong firmware! This is UTILITY device but you uploaded LTC firmware.";
            } else if (!hasClient && hasMaster) {
                otaFirmwareError = "Wrong firmware! This is CLIENT device but you uploaded MASTER firmware.";
            } else {
                otaFirmwareError = "Invalid firmware. Filename must contain 'client' and 'utility'.";
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

void sendOTACommandToLTC() {
    // Stop the 48 kHz sample clock BEFORE the LTC chip starts its WiFi / flash writes.
    // An LTC build whose sample ISR is not flash-safe would panic and reboot when a pulse lands in a
    // flash-write window (this blocked OTA of the fixed LTC firmware). With no clock there are no
    // interrupts. The local OTA path restarts the unit afterwards; the remote-OTA path turns the
    // clock back on in restoreNormalOperation().
    si5351.output_enable(SI5351_CLK0, 0);
    si5351.output_enable(SI5351_CLK1, 0);
    delay(150);

    // Send command to LTC device to enter OTA mode, including our SSID.
    // NOTE: the fill is protected by SpiCmdGuard (blocking mutex) - it must
    // NOT sit inside a portENTER_CRITICAL spinlock (blocking FreeRTOS calls
    // are illegal with interrupts disabled). performSPITransaction() takes
    // the spiMux spinlock internally for the transfer itself.
    SpiCmdGuard spiGuard;  // serialize shared spiCmd fill+transfer
    memset(&spiCmd, 0, sizeof(spiCmd));
    spiCmd.command = CMD_ENTER_OTA;
    // Copy our SSID so LTC device can display it
    strncpy(spiCmd.otaSSID, otaSSID.c_str(), sizeof(spiCmd.otaSSID) - 1);
    spiCmd.otaSSID[sizeof(spiCmd.otaSSID) - 1] = '\0';  // Ensure null termination

    performSPITransaction();
}

void enterOTAMode() {
    if (otaMode) return;

    otaMode = true;
    otaModeStartTime = millis();
    otaSSID = generateOTASSID();

    // Generate LTC SSID (same suffix, different prefix)
    // Our SSID is TCC-U-XXXX, LTC will be TCC-L-XXXX
    ltcOTASSID = "TCC-L-" + otaSSID.substring(6);

    // Stop ESP-NOW and disconnect wireless
    esp_now_deinit();
    WiFi.disconnect(true);
    delay(100);

    // Signal LTC device to also enter OTA mode (SSID is now set)
    sendOTACommandToLTC();
    delay(100);

    // Start WiFi AP for OTA with retry logic
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

    // Setup OTA web server
    otaServer.on("/", HTTP_GET, handleOTARoot);
    otaServer.on("/update", HTTP_POST, handleOTAUpdate, handleOTAUpload);
    otaServer.begin();

    // LED indication - rapid blinking
    digitalWrite(STATUS_LED_PIN, HIGH);
    digitalWrite(SYNC_LED_PIN, HIGH);
}

void exitOTAMode() {
    if (!otaMode) return;

    otaMode = false;
    otaServer.stop();
    WiFi.softAPdisconnect(true);

    // Restart the device to restore normal operation
    ESP.restart();
}

void checkOTAButton() {
    // Check for timeout in OTA mode
    if (otaMode) {
        if (millis() - otaModeStartTime > OTA_MODE_TIMEOUT) {
            exitOTAMode();
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

void handleOTAModeInLoop() {
    if (!otaMode) return;

    // Handle OTA web server
    otaServer.handleClient();

    // Poll LTC device periodically to get its SSID
    static unsigned long lastSPIPoll = 0;
    if (millis() - lastSPIPoll > 500) {
        pollLTCStatus();  // This will update ltcOTASSID from spiStatus
        lastSPIPoll = millis();
    }

    // LED blinking indication
    static unsigned long lastBlink = 0;
    if (millis() - lastBlink > 200) {
        digitalWrite(STATUS_LED_PIN, !digitalRead(STATUS_LED_PIN));
        digitalWrite(SYNC_LED_PIN, !digitalRead(SYNC_LED_PIN));
        lastBlink = millis();
    }
}

// ==================== REMOTE OTA FUNCTIONS ====================

// Handle OTA command received via ESP-NOW from Master
void handleRemoteOTACommand(const uint8_t* mac, const uint8_t* data, int len) {
    if (len < 67) return;  // Minimum size for OTA command struct

    // Only the master we are paired with may start an update
    if (!isPaired || memcmp(mac, masterMac, 6) != 0) return;

    // Already updating (the master re-sent the command because our ACK was lost): just re-ACK
    if (rotaState != ROTA_IDLE) {
        uint8_t ack = ESPNOW_OTA_ACK;
        esp_now_send(mac, &ack, 1);
        return;
    }

    // Parse OTA command
    uint8_t msgType = data[0];
    uint8_t updateFlags = data[1];

    rotaCommand.pending = true;
    rotaCommand.updateUtility = (updateFlags & 0x01) != 0;
    rotaCommand.updateLtc = (updateFlags & 0x02) != 0;
    memcpy(rotaCommand.masterSSID, &data[2], 32);
    memcpy(rotaCommand.masterPassword, &data[34], 32);
    memcpy(rotaCommand.masterMac, mac, 6);

    Serial.printf("Remote OTA command received: Utility=%d, LTC=%d, SSID=%s\n",
                  rotaCommand.updateUtility, rotaCommand.updateLtc, rotaCommand.masterSSID);

    // Send ACK back to Master
    uint8_t ack = ESPNOW_OTA_ACK;
    esp_now_send(rotaCommand.masterMac, &ack, 1);

    // Start the remote OTA state machine
    rotaState = ROTA_RECEIVED_COMMAND;
    rotaStateStartTime = millis();
    remoteOtaState = 1;  // Mark as updating for ESP-NOW status
}

// Main remote OTA state machine
void processRemoteOTA() {
    unsigned long now = millis();

    switch (rotaState) {
        case ROTA_RECEIVED_COMMAND:
            Serial.println("Remote OTA: Received command, switching to WiFi STA");
            sendOTAProgress(5, "Switching to WiFi");
            rotaState = ROTA_SWITCHING_TO_WIFI;
            rotaStateStartTime = now;
            break;

        case ROTA_SWITCHING_TO_WIFI:
            // Deinit ESP-NOW and switch to WiFi STA mode
            espNowAvailable = false;  // ESP-NOW no longer available
            esp_now_deinit();
            WiFi.mode(WIFI_STA);
            WiFi.setTxPower(WIFI_POWER_11dBm);   // firmware update: never below the default power
            WiFi.begin(rotaCommand.masterSSID, rotaCommand.masterPassword);
            Serial.printf("Connecting to Master AP: %s\n", rotaCommand.masterSSID);
            rotaState = ROTA_CONNECTING_TO_MASTER;
            rotaStateStartTime = now;
            break;

        case ROTA_CONNECTING_TO_MASTER:
            if (WiFi.status() == WL_CONNECTED) {
                Serial.printf("Connected to Master! IP: %s\n", WiFi.localIP().toString().c_str());
                sendOTAProgress(10, "Connected to Master");
                if (rotaCommand.updateUtility) {
                    rotaState = ROTA_DOWNLOADING_UTILITY;
                } else if (rotaCommand.updateLtc) {
                    rotaState = ROTA_DOWNLOADING_LTC;
                } else {
                    rotaState = ROTA_COMPLETE;
                }
                rotaStateStartTime = now;
            } else if (now - rotaStateStartTime > ROTA_WIFI_CONNECT_TIMEOUT) {
                Serial.println("Failed to connect to Master AP");
                sendOTAError("WiFi connect timeout");
                rotaState = ROTA_ERROR;
            }
            break;

        case ROTA_DOWNLOADING_UTILITY:
            Serial.println("Downloading Client Utility firmware...");
            sendOTAProgress(20, "Downloading Utility");
            if (downloadAndFlashFirmware("http://192.168.4.1/firmware?type=2", true)) {
                Serial.println("Utility firmware flashed successfully!");
                // If we just flashed utility and LTC update is also needed,
                // we need to reboot to apply utility update first
                if (rotaCommand.updateLtc) {
                    // Store LTC update pending in preferences for after reboot
                    Preferences prefs;
                    prefs.begin("rota", false);
                    prefs.putBool("ltc_pending", true);
                    prefs.putBytes("master_mac", rotaCommand.masterMac, 6);
                    prefs.putString("master_ssid", rotaCommand.masterSSID);
                    prefs.putString("master_pass", rotaCommand.masterPassword);
                    prefs.end();
                }
                sendOTAProgress(50, "Utility complete, rebooting");
                delay(500);
                ESP.restart();
            } else {
                sendOTAError("Utility download failed");
                rotaState = ROTA_ERROR;
            }
            break;

        case ROTA_DOWNLOADING_LTC:
            Serial.println("Downloading Client LTC firmware...");
            sendOTAProgress(60, "Downloading LTC");
            if (downloadLTCFirmware()) {
                Serial.println("LTC firmware downloaded to SPIFFS");
                rotaState = ROTA_PROXY_LTC_UPDATE;
                rotaStateStartTime = now;
            } else {
                sendOTAError("LTC download failed");
                rotaState = ROTA_ERROR;
            }
            break;

        case ROTA_PROXY_LTC_UPDATE:
            Serial.println("Proxying LTC update...");
            sendOTAProgress(80, "Updating LTC");
            if (proxyLTCUpdate()) {
                Serial.println("LTC update complete!");
                rotaState = ROTA_COMPLETE;
            } else {
                sendOTAError("LTC proxy failed");
                rotaState = ROTA_ERROR;
            }
            break;

        case ROTA_COMPLETE:
            Serial.println("Remote OTA complete!");
            sendOTAProgress(100, "Complete");
            sendOTAComplete();
            delay(1000);
            restoreNormalOperation();
            break;

        case ROTA_ERROR:
            Serial.println("Remote OTA error, restoring normal operation");
            remoteOtaState = 3;  // Error state
            delay(2000);
            restoreNormalOperation();
            break;

        default:
            break;
    }
}

// Download firmware from Master and flash it
bool downloadAndFlashFirmware(const char* url, bool isUtility) {
    HTTPClient http;
    http.begin(url);
    http.setTimeout(60000);  // per-read timeout, ms (uint16_t: 120000 would wrap to 54464)

    int httpCode = http.GET();
    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("HTTP GET failed: %d\n", httpCode);
        http.end();
        return false;
    }

    int contentLength = http.getSize();
    if (contentLength <= 0) {
        Serial.println("Invalid content length");
        http.end();
        return false;
    }

    Serial.printf("Firmware size: %d bytes\n", contentLength);

    if (!Update.begin(contentLength)) {
        Serial.println("Not enough space for OTA");
        http.end();
        return false;
    }

    WiFiClient* stream = http.getStreamPtr();
    size_t written = 0;
    uint8_t buff[1024];
    int lastProgress = 0;

    while (http.connected() && written < contentLength) {
        size_t available = stream->available();
        if (available) {
            int bytesRead = stream->readBytes(buff, min(available, sizeof(buff)));
            size_t bytesWritten = Update.write(buff, bytesRead);
            if (bytesWritten != bytesRead) {
                Serial.println("Write error during OTA");
                http.end();
                Update.abort();
                return false;
            }
            written += bytesWritten;

            int progress = (written * 100) / contentLength;
            if (progress != lastProgress && progress % 10 == 0) {
                Serial.printf("OTA Progress: %d%%\n", progress);
                sendOTAProgress(20 + (progress * 30 / 100), "Flashing");
                lastProgress = progress;
            }
        }
        rotaBlinkLeds();
        delay(1);
    }

    http.end();

    if (written != contentLength) {
        Serial.println("Download incomplete");
        Update.abort();
        return false;
    }

    if (!Update.end(true)) {
        Serial.printf("Update error: %s\n", Update.errorString());
        return false;
    }

    return true;
}

// The LTC chip's OTA page (WebServer) only accepts a multipart/form-data file upload whose file
// name contains "client" and "ltc". The downloaded image is stored already wrapped in that body.
#define LTC_MP_BOUNDARY "----TimecodeMasterOTA"
static const char LTC_MP_HEAD[] = "--" LTC_MP_BOUNDARY "\r\n"
    "Content-Disposition: form-data; name=\"firmware\"; filename=\"client_ltc.bin\"\r\n"
    "Content-Type: application/octet-stream\r\n\r\n";
static const char LTC_MP_TAIL[] = "\r\n--" LTC_MP_BOUNDARY "--\r\n";

// Download LTC firmware to SPIFFS for proxy update
bool downloadLTCFirmware() {
    if (!spiffsInitialized) {
        Serial.println("SPIFFS not initialized");
        return false;
    }

    HTTPClient http;
    http.begin("http://192.168.4.1/firmware?type=3");  // Client LTC firmware
    http.setTimeout(60000);  // per-read timeout, ms (uint16_t: 120000 would wrap to 54464)

    int httpCode = http.GET();
    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("HTTP GET failed: %d\n", httpCode);
        http.end();
        return false;
    }

    int contentLength = http.getSize();
    Serial.printf("LTC firmware size: %d bytes\n", contentLength);

    SPIFFS.remove("/client_ltc.bin");   // free the space of any earlier attempt
    File file = SPIFFS.open("/client_ltc.bin", "w");
    if (!file) {
        Serial.println("Failed to open file for writing");
        http.end();
        return false;
    }
    file.write((const uint8_t*)LTC_MP_HEAD, strlen(LTC_MP_HEAD));

    WiFiClient* stream = http.getStreamPtr();
    size_t written = 0;
    uint8_t buff[1024];

    while (http.connected() && written < contentLength) {
        size_t available = stream->available();
        if (available) {
            int bytesRead = stream->readBytes(buff, min(available, sizeof(buff)));
            if (file.write(buff, bytesRead) != (size_t)bytesRead) {   // SPIFFS full
                file.close();
                http.end();
                return false;
            }
            written += bytesRead;
            static int lastLtcPct = -1;
            int pct = contentLength > 0 ? (int)(written * 100 / contentLength) : 0;
            if (pct / 10 != lastLtcPct / 10) {
                lastLtcPct = pct;
                sendOTAProgress(60 + pct * 20 / 100, "Downloading LTC");
            }
        }
        rotaBlinkLeds();
        delay(1);
    }

    file.write((const uint8_t*)LTC_MP_TAIL, strlen(LTC_MP_TAIL));
    file.close();
    http.end();

    return contentLength > 0 && written == (size_t)contentLength;
}

// Proxy update to LTC device
bool proxyLTCUpdate() {
    // Send command to LTC to enter OTA mode
    sendOTACommandToLTC();
    delay(2000);  // Wait for LTC to start its AP

    // Disconnect from Master's AP
    WiFi.disconnect();
    delay(500);

    // Get LTC's SSID from last known value or poll
    pollLTCStatus();
    String ltcSSID = ltcOTASSID;
    if (ltcSSID.length() == 0) {
        // Generate expected SSID
        ltcSSID = "TCC-L-" + otaSSID.substring(6);
    }

    Serial.printf("Connecting to LTC AP: %s\n", ltcSSID.c_str());

    // Connect to LTC's AP
    WiFi.setTxPower(WIFI_POWER_11dBm);   // firmware update: never below the default power
    WiFi.begin(ltcSSID.c_str(), OTA_PASSWORD);
    unsigned long connectStart = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - connectStart < ROTA_LTC_CONNECT_TIMEOUT) {
        rotaBlinkLeds();
        delay(100);
    }

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Failed to connect to LTC AP");
        return false;
    }

    Serial.println("Connected to LTC AP, uploading firmware...");

    // Read firmware from SPIFFS and upload to LTC
    File file = SPIFFS.open("/client_ltc.bin", "r");
    if (!file) {
        Serial.println("Failed to open LTC firmware file");
        return false;
    }

    HTTPClient http;
    http.begin("http://192.168.4.1/update");
    http.addHeader("Content-Type", "multipart/form-data; boundary=" LTC_MP_BOUNDARY);
    http.setTimeout(ROTA_LTC_UPLOAD_TIMEOUT);

    int httpCode = http.sendRequest("POST", &file, file.size());
    file.close();
    // The LTC replies HTTP 200 for success AND failure - only its page text tells them apart
    String reply = (httpCode > 0) ? http.getString() : String();
    http.end();

    if (httpCode == HTTP_CODE_OK && reply.indexOf("Update Successful") >= 0) {
        Serial.println("LTC firmware uploaded successfully");
        SPIFFS.remove("/client_ltc.bin");  // Clean up
        return true;
    } else {
        Serial.printf("LTC upload failed: %d\n", httpCode);
        return false;
    }
}

// Send progress to Master via ESP-NOW (if available)
// Both LEDs blink fast during a remote update (called from the download / upload loops)
void rotaBlinkLeds() {
    static unsigned long last = 0;
    if (millis() - last < 100) return;
    last = millis();
    static bool on = false;
    on = !on;
    digitalWrite(STATUS_LED_PIN, on ? HIGH : LOW);
    digitalWrite(SYNC_LED_PIN, on ? LOW : HIGH);
}

static String rotaUrlEncode(const char* s) {
    String o;
    for (; *s; s++) {
        char c = *s;
        if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.') o += c;
        else { char b[4]; snprintf(b, sizeof(b), "%%%02X", (uint8_t)c); o += b; }
    }
    return o;
}

void sendOTAProgress(uint8_t progress, const char* status) {
    rotaBlinkLeds();
    // While on the master's WiFi (ESP-NOW is off) report progress over HTTP so the master's page
    // stays live. Only on the MASTER's network (the LTC's OTA network also uses 192.168.4.1).
    if (!espNowAvailable && WiFi.status() == WL_CONNECTED && WiFi.SSID() == String(rotaCommand.masterSSID)) {
        HTTPClient h;
        h.setTimeout(400);
        String url = "http://192.168.4.1/client_ota/progress?mac=" + WiFi.macAddress() + "&p=" + String(progress) +
                     "&s=" + rotaUrlEncode(status);
        if (h.begin(url)) { h.GET(); h.end(); }
    }
    Serial.printf("OTA Progress: %d%% - %s\n", progress, status);

    if (espNowAvailable && rotaCommand.masterMac[0] != 0) {
        OTAStatus otaStatus;
        otaStatus.msgType = ESPNOW_OTA_PROG;
        otaStatus.progress = progress;
        otaStatus.state = rotaState;
        strncpy(otaStatus.status, status, sizeof(otaStatus.status) - 1);
        otaStatus.status[sizeof(otaStatus.status) - 1] = '\0';

        esp_err_t result = esp_now_send(rotaCommand.masterMac, (uint8_t*)&otaStatus, sizeof(otaStatus));
        if (result != ESP_OK) {
            Serial.printf("Failed to send OTA progress: %d\n", result);
        }
    }
}

// Send completion to Master
void sendOTAComplete() {
    Serial.println("OTA Complete notification");
    remoteOtaState = 0;  // Back to normal

    if (espNowAvailable && rotaCommand.masterMac[0] != 0) {
        OTAStatus otaStatus;
        otaStatus.msgType = ESPNOW_OTA_DONE;
        otaStatus.progress = 100;
        otaStatus.state = rotaState;
        strncpy(otaStatus.status, "Update complete", sizeof(otaStatus.status) - 1);
        otaStatus.status[sizeof(otaStatus.status) - 1] = '\0';

        esp_now_send(rotaCommand.masterMac, (uint8_t*)&otaStatus, sizeof(otaStatus));
    }
}

// Send error to Master
void sendOTAError(const char* error) {
    Serial.printf("OTA Error: %s\n", error);
    remoteOtaState = 3;  // Error state
    if (error != rotaLastError) {
        strncpy(rotaLastError, error, sizeof(rotaLastError) - 1);
        rotaLastError[sizeof(rotaLastError) - 1] = '\0';
    }

    if (espNowAvailable && rotaCommand.masterMac[0] != 0) {
        OTAStatus otaStatus;
        otaStatus.msgType = ESPNOW_OTA_ERR;
        otaStatus.progress = 0;
        otaStatus.state = rotaState;
        strncpy(otaStatus.status, error, sizeof(otaStatus.status) - 1);
        otaStatus.status[sizeof(otaStatus.status) - 1] = '\0';

        esp_now_send(rotaCommand.masterMac, (uint8_t*)&otaStatus, sizeof(otaStatus));
    }
}

// Restore normal ESP-NOW operation after remote OTA
void restoreNormalOperation() {
    bool failed = (rotaState == ROTA_ERROR);   // decide DONE vs ERROR before the state is reset
    WiFi.disconnect();
    WiFi.mode(WIFI_OFF);
    delay(100);

    // Turn the sample clock back on (it was stopped before the LTC chip was put into OTA).
    si5351.output_enable(SI5351_CLK0, 1);

    // Re-initialize ESP-NOW
    setupESP_NOW();
    espNowAvailable = true;  // ESP-NOW is available again

    rotaState = ROTA_IDLE;
    rotaCommand.pending = false;
    remoteOtaState = 0;

    // Send the final status to the Master now that ESP-NOW is available again
    // (previously this always reported DONE, even after an error)
    if (failed) {
        sendOTAError(rotaLastError[0] ? rotaLastError : "update failed");
    } else {
        sendOTAComplete();
    }
    rotaLastError[0] = '\0';

    Serial.println("Normal operation restored");
}

// ==================== SETUP ====================
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
    radioLoad();    // TX power / ESP-NOW rate chosen on the master's page
    setCpuFrequencyMhz(80);  // Set CPU to 80MHz for power saving (minimum for WiFi)
    btStop();                // Disable Bluetooth radio for power saving
    // FIRST: Enable power to OLED/Si5351 before any I2C init
    pinMode(POWER_EN_PIN, OUTPUT);
    digitalWrite(POWER_EN_PIN, HIGH);
    delay(50);  // Allow power to stabilize

    // NOTE: These delays are intentional - Utility must wait for LTC device to boot
    delay(1000);
    systemStartTime = millis();

    pinMode(STATUS_LED_PIN, OUTPUT);
    pinMode(SYNC_LED_PIN, OUTPUT);
    pinMode(PAIR_BUTTON_PIN, INPUT_PULLUP);
    pinMode(OTA_BUTTON_PIN, INPUT_PULLUP);  // OTA update button
    digitalWrite(STATUS_LED_PIN, LOW);
    digitalWrite(SYNC_LED_PIN, LOW);
    Wire.begin(21, 22);
    Wire.setClock(100000);
    delay(1000);

    setupSi5351();
    delay(2000);  // Wait for LTC device Si5351 to be ready
    spiCmdLock = xSemaphoreCreateRecursiveMutex();
    setupSPI_Master();
    delay(1000);
    setupESP_NOW();
    loadDeviceName();  // Load custom device name from Preferences

    // === FIX: Store the Task Handle so we can notify it later ===
    xTaskCreatePinnedToCore(utilityTask, "UtilityTask", 16384, NULL, 1, &UtilityTaskHandle, 0);

    delay(1000);  // Wait for LTC device to be fully ready
    if (isPaired) {
        sendESPNowRegistration();
    } else {
        sendPairingRequest();
    }

    ltcTimecode.lastConnectionAttempt = millis();

    // Initialize SPIFFS for remote OTA (proxy LTC firmware storage)
    if (SPIFFS.begin(true)) {
        spiffsInitialized = true;
        Serial.println("SPIFFS initialized for remote OTA");
    } else {
        Serial.println("SPIFFS initialization failed");
    }

    // Check for pending LTC update (after Utility update and reboot)
    Preferences prefs;
    prefs.begin("rota", true);  // Read-only first
    bool ltcPending = prefs.getBool("ltc_pending", false);
    prefs.end();

    if (ltcPending && spiffsInitialized) {
        Serial.println("Pending LTC update detected, resuming...");

        // Retrieve stored Master credentials
        prefs.begin("rota", false);
        String masterSSID = prefs.getString("master_ssid", "");
        String masterPass = prefs.getString("master_pass", "");
        prefs.getBytes("master_mac", rotaCommand.masterMac, 6);
        prefs.putBool("ltc_pending", false);  // Clear the flag
        prefs.end();

        if (masterSSID.length() > 0) {
            strncpy(rotaCommand.masterSSID, masterSSID.c_str(), sizeof(rotaCommand.masterSSID) - 1);
            strncpy(rotaCommand.masterPassword, masterPass.c_str(), sizeof(rotaCommand.masterPassword) - 1);
            rotaCommand.updateUtility = false;
            rotaCommand.updateLtc = true;
            rotaCommand.pending = true;

            // Skip normal ESP-NOW operation and go straight to LTC update
            espNowAvailable = false;
            esp_now_deinit();
            WiFi.mode(WIFI_STA);
            WiFi.setTxPower(WIFI_POWER_11dBm);   // firmware update: never below the default power
            WiFi.begin(rotaCommand.masterSSID, rotaCommand.masterPassword);
            rotaState = ROTA_CONNECTING_TO_MASTER;
            rotaStateStartTime = millis();  // Initialize timeout tracking
            Serial.printf("Reconnecting to Master AP: %s\n", rotaCommand.masterSSID);
        }
    }

    // OTA Rollback Protection: Mark firmware as valid after successful startup
    // If device crashes before this point, bootloader will rollback to previous firmware
    radioApplyAll();   // everything is initialised now: make sure power and rate are applied
    esp_ota_mark_app_valid_cancel_rollback();
    psMux = xSemaphoreCreateRecursiveMutex();
    xTaskCreatePinnedToCore(psTask, "psTask", 8192, NULL, 2, NULL, 1);   // radio power save (idle unless enabled on the master's page)
#if RADIO_SLEEP_TEST
    rsTestStart();     // bench experiment, test image only
#endif
}

// ==================== RADIO POWER SAVE ====================
// While enabled (and paired, connected and idle) the radio sleeps between the master's beacons: it wakes shortly before
// the next beacon, listens for PS_WINDOW_MS after it (sending our heartbeat meanwhile) and goes back to sleep. If a beacon
// is missed the radio simply stays on until one is heard again, so a problem can only cost power, never the link.
#define PS_WINDOW_MS 160       // normal listening window after a beacon
#define PS_WINDOW_CMD_MS 500   // when the beacon says a command is coming
static void psSendWake() {     // tell the master "my radio is on": it sends time-critical messages only when we have
    struct_message w;
    memset(&w, 0, sizeof(w));
    SET_MSG_TYPE(w, "WAKE");
    w.timestamp = millis();
    esp_now_send(masterMac, (uint8_t*)&w, sizeof(w));
}

static bool psWake() {
    PsGuard psg;
    esp_err_t a = esp_wifi_start();
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
    esp_err_t b = esp_now_init();
    radioApplyAll();
    registerESPNowCallbacks();
    esp_now_peer_info_t p = {};
    p.channel = 0;
    p.encrypt = false;
    memset(p.peer_addr, 0xFF, 6);
    esp_now_add_peer(&p);
    memcpy(p.peer_addr, masterMac, 6);
    esp_now_add_peer(&p);
    psRadioOff = false;
    return a == ESP_OK && b == ESP_OK;
}

static void psSleep() {
    PsGuard psg;
    psRadioOff = true;
    esp_now_deinit();
    esp_wifi_stop();
}

static bool psEligible() {
    return psEnabled && isPaired && wirelessEnabled && !otaMode && rotaState == ROTA_IDLE && !ltcSystemLocked &&
           !rtRun && !rtGoPending && millis() > 45000;
}

// If the master is not heard for a while (switched off, out of range, locked) the radio does NOT stay on all the time:
// it listens for 2.5 s (longer than one beacon interval) and sleeps for 12 s, and goes back to the normal cycle as soon as
// a beacon is heard. (Only after a beacon has been heard once since start-up: a master without power save sends none.)
static void psTask(void*) {
    enum { P_WAIT, P_WINDOW } st = P_WAIT;
    unsigned long waitSince = millis(), windowAt = 0, handled = 0, winMs = PS_WINDOW_MS, lastWakeMsg = 0, huntSince = 0;
    uint32_t wakeMs = 250;   // how long waking the radio takes (measured)
    uint8_t miss = 0;
    bool lost = false;
    for (;;) {
        if (!psEligible()) {
            if (psRadioOff && wirelessEnabled) psWake();   // we had put the radio to sleep and things changed: bring it back
            st = P_WAIT;
            waitSince = millis();
            miss = 0;
            lost = false;
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        unsigned long now = millis();
        if (st == P_WAIT) {
            unsigned long b = psBeaconAt;
            if (b != handled && (long)(b - waitSince) >= 0) {   // a beacon arrived while we were listening
                handled = b;
                windowAt = b;
                winMs = (psFlags & 2) ? PS_WINDOW_CMD_MS : PS_WINDOW_MS;
                st = P_WINDOW;
                miss = 0;
                lost = false;
                if (now - lastWakeMsg > 150) { lastWakeMsg = now; PsGuard g; if (!psRadioOff) psSendWake(); }   // we heard the beacon: say so
            } else {
                if (now - waitSince > (unsigned long)psInterval + 400) {   // none for a whole interval
                    waitSince = now;
                    if (miss < 250) miss++;
                }
                if (!lost && miss >= 3 && psBeaconAt != 0) { lost = true; huntSince = now; }
                if (lost && now - huntSince > 2500) {          // a whole listen heard nothing: sleep a long while
                    psSleep();
                    unsigned long until = millis() + 12000;
                    while ((long)(until - millis()) > 0 && psEligible()) vTaskDelay(pdMS_TO_TICKS(5));
                    if (psEligible()) {
                        psWake();
                        { PsGuard g; if (!psRadioOff) { lastWakeMsg = millis(); psSendWake(); } }
                        huntSince = millis();
                        waitSince = millis();
                    }
                    continue;
                }
                vTaskDelay(pdMS_TO_TICKS(5));
            }
            continue;
        }
        // P_WINDOW: the window is open
        if (now - windowAt >= 8 && now - lastHeartbeat >= 3500) {   // heartbeat at the start of the window
            lastHeartbeat = now;
            sendESPNowHeartbeat();
        }
        if (now - windowAt < winMs) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        if (psFlags & 1) { st = P_WAIT; waitSince = now; continue; }   // the master wants everyone awake
        unsigned long lead = wakeMs + 70;
        unsigned long target = windowAt + psInterval - lead;           // when to start waking
        if ((long)(target - now) < 60) { st = P_WAIT; waitSince = now; continue; }   // too little time to be worth sleeping
        psSleep();
        while ((long)(target - millis()) > 0 && psEligible()) vTaskDelay(pdMS_TO_TICKS(5));
        unsigned long t0 = millis();
        psWake();
        uint32_t took = millis() - t0;
        if (took > wakeMs || wakeMs > took + 100) wakeMs = took;       // follow the measured wake-up time (never too short)
        { PsGuard g; if (!psRadioOff) { lastWakeMsg = millis(); psSendWake(); } }   // "my radio is on" (the master waits for this)
        st = P_WAIT;
        waitSince = millis();
    }
}

void loop() {
    delay(1);
}
