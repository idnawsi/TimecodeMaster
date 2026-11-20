// ==================== CLIENT OTA: master pushes firmware to every paired client ====================
//
// One click on http://192.168.4.1/client-update:
//   1. The browser uploads the client firmware(s) to the master. They are stored RAW in two flash
//      partitions the master does not use: the spare OTA app slot and the SPIFFS data partition.
//      (No filesystem, nothing to format; contents are only trusted until the master reboots.)
//   2. The master sends an ESP-NOW OTA command to the selected clients: one at a time (default,
//      most robust) or up to COTA_MAX_PARALLEL at once.
//   3. That client joins the master's WiFi AP, downloads /firmware?type=2 (client utility), flashes
//      and reboots; then downloads /firmware?type=3 (client LTC) and hands it to its LTC chip.
//   4. The client reports ACK / PROGRESS / DONE / ERROR over ESP-NOW; the master starts the next.
//
// Protocol (must match Client_Utility_Device_Wireless):
//   CMD  (master->client, 67 bytes): [0]=0x10 [1]=flags(bit0 utility, bit1 LTC) [2..33]=AP SSID
//                                    [34..65]=AP password [66]=protocol version
//   ACK  (client->master, 1 byte)  : 0x11
//   PROG/DONE/ERR (client->master) : {uint8 type(0x12/0x13/0x14), uint8 progress, uint8 state, char status[32]}

#include <esp_partition.h>
#include <esp_heap_caps.h>

#define COTA_CMD  0x10
#define COTA_ACK  0x11
#define COTA_PROG 0x12
#define COTA_DONE 0x13
#define COTA_ERR  0x14
#define COTA_FLAG_UTILITY 0x01
#define COTA_FLAG_LTC     0x02
#define COTA_PROTOCOL_VERSION 1

const unsigned long COTA_ACK_TIMEOUT_MS     = 4000;                  // resend the command after this
const uint8_t       COTA_MAX_SENDS          = 3;
const unsigned long COTA_CLIENT_TIMEOUT_MS  = 10UL * 60UL * 1000UL;  // give up on one client after 10 min
const uint8_t       COTA_MAX_PARALLEL       = 4;                     // clients updating at the same time (max)
const unsigned long COTA_ONLINE_WINDOW_MS   = 20000;                 // "online" = heard from in the last 20 s

struct CotaSlot {
    const esp_partition_t* part;
    uint32_t size;        // bytes of firmware stored
    uint32_t erasedTo;    // erase progress during upload
    bool valid;           // complete image stored
    bool writing;         // upload in progress
    bool idFound;         // firmware identity tag seen in the image
    uint8_t tail[40];     // last bytes of the previous chunk (tag may span chunks)
    uint8_t tailLen;
    char name[48];        // uploaded file name
};

// Firmware slot types: 2 = client utility, 3 = client LTC, 4 = master utility, 5 = master LTC.
// Expected identity tag ("TCM-FWID:CLIENT-UTILITY", ...), assembled at run time from separate pieces
// so the master's own image never contains another unit's tag (volatile pointers stop the compiler
// from folding them into one literal).
static const char* volatile COTA_ID_PARTS[] = { "TCM-FWID:", "CLIENT-", "UTILITY", "LTC", "MASTER-" };
static void cotaExpectedId(int type, char* out, size_t n) {
    out[0] = 0;
    strlcat(out, COTA_ID_PARTS[0], n);
    strlcat(out, (type == 4 || type == 5) ? COTA_ID_PARTS[4] : COTA_ID_PARTS[1], n);
    strlcat(out, (type == 2 || type == 4) ? COTA_ID_PARTS[2] : COTA_ID_PARTS[3], n);
}
static const char* cotaSlotLabel(int type) {
    switch (type) {
        case 2: return "CLIENT UTILITY";
        case 3: return "CLIENT LTC";
        case 4: return "MASTER UTILITY";
        default: return "MASTER LTC";
    }
}

static bool cotaContains(const uint8_t* h, size_t n, const char* needle, size_t m) {
    if (m == 0 || n < m) return false;
    for (size_t i = 0; i + m <= n; i++)
        if (h[i] == (uint8_t)needle[0] && memcmp(h + i, needle, m) == 0) return true;
    return false;
}

// Feed one upload chunk to the identity-tag search (handles a tag split across two chunks).
static void cotaScanId(CotaSlot* s, int type, const uint8_t* data, size_t len) {
    if (s->idFound || len == 0) return;
    char id[40];
    cotaExpectedId(type, id, sizeof(id));
    size_t m = strlen(id);
    uint8_t joint[sizeof(s->tail) + sizeof(id)];
    size_t jl = s->tailLen;
    memcpy(joint, s->tail, jl);
    size_t take = len < m - 1 ? len : m - 1;
    memcpy(joint + jl, data, take);
    jl += take;
    if (cotaContains(joint, jl, id, m) || cotaContains(data, len, id, m)) { s->idFound = true; return; }
    // keep the last m-1 bytes seen
    if (len >= m - 1) {
        memcpy(s->tail, data + len - (m - 1), m - 1);
        s->tailLen = m - 1;
    } else {
        size_t keep = (s->tailLen + len > m - 1) ? (m - 1 - len) : s->tailLen;
        memmove(s->tail, s->tail + s->tailLen - keep, keep);
        memcpy(s->tail + keep, data, len);
        s->tailLen = keep + len;
    }
}
// [0] client utility (type 2), [1] client LTC (3), [2] master utility (4), [3] master LTC (5).
// Utility images share the bigger partition, LTC images the smaller one: storing one invalidates the
// other slot on the same partition (client and master updates are never stored at the same time).
static CotaSlot cotaSlots[4];
static bool cotaSlotsInit = false;
static String cotaUploadError;   // only touched by the web server task

// Status text written by loop() AND read/written by web handlers (which run on the async_tcp task).
// An Arduino String must never be shared like that: two tasks re-assigning the same String free its
// heap block twice -> heap corruption (the web server then sent freed memory as page content).
// This keeps the text in a fixed buffer behind a lock instead.
class LockedText {
    char b[200] = {0};
    portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
public:
    void set(const char* s) {
        portENTER_CRITICAL(&mux);
        strncpy(b, s, sizeof(b) - 1);
        b[sizeof(b) - 1] = 0;
        portEXIT_CRITICAL(&mux);
    }
    LockedText& operator=(const char* s) { set(s); return *this; }
    LockedText& operator=(const String& s) { set(s.c_str()); return *this; }
    void copyTo(char* out, size_t n) {
        portENTER_CRITICAL(&mux);
        strncpy(out, b, n - 1);
        out[n - 1] = 0;
        portEXIT_CRITICAL(&mux);
    }
    String get() { char t[sizeof(b)]; copyTo(t, sizeof(t)); return String(t); }
    bool startsWith(const char* p) { char t[sizeof(b)]; copyTo(t, sizeof(t)); return strncmp(t, p, strlen(p)) == 0; }
    bool empty() { char t[2]; copyTo(t, sizeof(t)); return t[0] == 0; }
};

enum CotaState : uint8_t { CS_WAITING, CS_SENT, CS_ACKED, CS_PROGRESS, CS_DONE, CS_ERROR, CS_TIMEOUT, CS_SKIPPED };
static const char* const COTA_STATE_NAMES[] = { "waiting", "command sent", "accepted", "updating", "done", "error", "timed out", "skipped" };

struct CotaEntry {
    uint8_t mac[6];
    char name[3];
    volatile uint8_t state;
    volatile uint8_t progress;
    char status[33];
    unsigned long started;
    volatile unsigned long lastMsg;
    uint8_t sends;
};
static CotaEntry cota[MAX_ESPNOW_CLIENTS];
static int cotaCount = 0;
static bool cotaRunning = false;
static uint8_t cotaParallel = 1;      // how many clients may update at the same time
static uint8_t cotaFlags = 0;
static portMUX_TYPE cotaMux = portMUX_INITIALIZER_UNLOCKED;
// Web handlers run on the async_tcp task: they only post requests; loop() does the work.
static volatile bool cotaStartReq = false;
static volatile bool cotaCancelReq = false;
static volatile uint8_t cotaReqFlags = 0;
static volatile uint8_t cotaReqParallel = 1;
// Selected clients ("AA:BB:CC:DD:EE:FF,..." upper case); empty = every online paired client.
// Written by the web handler BEFORE it sets cotaStartReq; read by loop() after.
static char cotaReqMacs[MAX_ESPNOW_CLIENTS * 18 + 1];
static LockedText cotaLastError;
static unsigned long cotaLastErrorAt = 0;

// ---------------------------------------------------------------------------------------------
static void cotaInitSlots() {
    if (cotaSlotsInit) return;
    memset(cotaSlots, 0, sizeof(cotaSlots));
    const esp_partition_t* spare = esp_ota_get_next_update_partition(NULL);
    const esp_partition_t* data = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, NULL);
#ifdef ESP_PARTITION_SUBTYPE_DATA_LITTLEFS
    if (!data) data = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_LITTLEFS, NULL);
#endif
    // The client utility image is the bigger one -> give it the bigger partition.
    const esp_partition_t* big = spare;
    const esp_partition_t* small = data;
    if (data && (!spare || data->size > spare->size)) { big = data; small = spare; }
    cotaSlots[0].part = big;     // client utility
    cotaSlots[1].part = small;   // client LTC
    cotaSlots[2].part = big;     // master utility
    cotaSlots[3].part = small;   // master LTC
    cotaSlotsInit = true;
}

static int cotaTypeParam(AsyncWebServerRequest* req) {
    if (!req->hasParam("type")) return -1;
    int t = req->getParam("type")->value().toInt();
    return (t >= 2 && t <= 5) ? t : -1;
}

static CotaSlot* cotaSlotFor(int type) {
    cotaInitSlots();
    if (type >= 2 && type <= 5) return &cotaSlots[type - 2];
    return nullptr;
}

static void cotaJsonClean(char* s) {   // keep client-supplied text JSON-safe
    for (; *s; s++) if (*s == '"' || *s == '\\' || (uint8_t)*s < 0x20) *s = ' ';
}

static void cotaSetState(CotaEntry& e, uint8_t state, const char* status) {
    portENTER_CRITICAL(&cotaMux);
    e.state = state;
    if (status) { strncpy(e.status, status, sizeof(e.status) - 1); e.status[sizeof(e.status) - 1] = 0; }
    e.lastMsg = millis();
    portEXIT_CRITICAL(&cotaMux);
}

static void cotaSendCmd(CotaEntry& e) {
    uint8_t pkt[67];
    memset(pkt, 0, sizeof(pkt));
    pkt[0] = COTA_CMD;
    pkt[1] = cotaFlags;
    strncpy((char*)&pkt[2], ssidString.c_str(), 31);
    strncpy((char*)&pkt[34], generatedPassword.c_str(), 31);
    pkt[66] = COTA_PROTOCOL_VERSION;   // the client requires >= 67 bytes
    esp_now_send(e.mac, pkt, sizeof(pkt));
    e.sends++;
    if (e.state == CS_WAITING) e.started = millis();
    cotaSetState(e, CS_SENT, "command sent");
}

static bool cotaIsActive(uint8_t st) { return st == CS_SENT || st == CS_ACKED || st == CS_PROGRESS; }

// ---------------------------------------------------------------------------------------------
// Called from the ESP-NOW receive callback (WiFi task) for packets whose first byte is 0x11..0x14.
void clientOta_onEspNow(const uint8_t* mac, const uint8_t* data, int len) {
    if (len < 1) return;
    portENTER_CRITICAL(&cotaMux);   // the list is rebuilt by loop() when an update starts
    for (int i = 0; i < cotaCount; i++) {
        if (memcmp(cota[i].mac, mac, 6) != 0) continue;
        CotaEntry& e = cota[i];
        if (data[0] == COTA_ACK) {
            if (e.state == CS_SENT) { e.state = CS_ACKED; strcpy(e.status, "accepted"); }
        } else if (len >= 3) {
            char st[33] = {0};
            if (len > 3) memcpy(st, data + 3, (len - 3) < 32 ? (len - 3) : 32);
            if (data[0] == COTA_PROG) { e.state = CS_PROGRESS; e.progress = data[1]; }
            else if (data[0] == COTA_DONE) { e.state = CS_DONE; e.progress = 100; }
            else if (data[0] == COTA_ERR) { e.state = CS_ERROR; }
            if (st[0]) { strncpy(e.status, st, sizeof(e.status) - 1); e.status[sizeof(e.status) - 1] = 0; }
        }
        e.lastMsg = millis();
        break;
    }
    portEXIT_CRITICAL(&cotaMux);
}

// Called from loop()
static String cotaStart(bool doUtility, bool doLtc, const char* macs, uint8_t parallel);

// ---------------------------------------------------------------------------------------------
// "Clear reset log": zeroes the brownout / watchdog / crash counters on the master's two chips and, with an
// ESP-NOW packet {0x16, 1}, on every ONLINE client (its Utility clears itself and its LTC). Offline clients
// keep their counts until they are online during a later clear.
static volatile bool rstClearReq = false;
static uint8_t rstClearRepeat = 0;
static unsigned long rstClearAt = 0;

static bool rstClientOnline(int i) {
    return espNowClients[i].paired && (millis() - espNowClients[i].lastSeen <= COTA_ONLINE_WINDOW_MS);
}

// ---------------------------------------------------------------------------------------------
// Radio power save, master side. While it is on, the master sends a broadcast "PSB" beacon every PS_INTERVAL_MS from a
// precise timer; each client wakes just before the next one and listens for PS_WINDOW_MS after it. Everything meant for
// a client therefore goes out right after a beacon:
//  - time-critical sends (sync, run state) call psGate() first and then build the message, so it is as fresh as ever;
//  - other commands (lock, names, settings, reset-log clear) go through espSendClient(): they wait in a small queue and are
//    sent after the next two beacons (no acknowledgement exists, so each is sent twice);
//  - updates, pairing and the tests ask for "stay awake": the beacon carries that flag and the clients keep listening.
#define PS_INTERVAL_MS 2000
#define PS_Q 8
struct PsQ { bool used; uint8_t mac[6]; uint8_t len; uint8_t data[140]; uint8_t reps; };
static PsQ psQ[PS_Q];
static portMUX_TYPE psQMux = portMUX_INITIALIZER_UNLOCKED;
static unsigned long psHoldUntil = 0, psHoldSince = 0;   // "all clients stay awake" requested until / since
static volatile bool psCmdPending = false;               // a command is waiting for the next window: the beacon asks clients to keep it open longer
static unsigned long psGateOpenUntil = 0;                // a gate has just passed: following sends of the same action go straight out

void espSendClient(const uint8_t* mac, const uint8_t* d, size_t n) {
    if (!psActive() || n > sizeof(psQ[0].data)) { esp_now_send(mac, d, n); return; }
    portENTER_CRITICAL(&psQMux);
    int slot = -1;
    for (int i = 0; i < PS_Q; i++) {
        if (psQ[i].used && memcmp(psQ[i].mac, mac, 6) == 0 && psQ[i].len == n && memcmp(psQ[i].data, d, n) == 0) { slot = i; break; }   // same command: just refresh it
        if (!psQ[i].used && slot < 0) slot = i;
    }
    if (slot < 0) slot = 0;   // queue full: overwrite (very rare)
    psQ[slot].used = true;
    memcpy(psQ[slot].mac, mac, 6);
    psQ[slot].len = (uint8_t)n;
    memcpy(psQ[slot].data, d, n);
    psQ[slot].reps = 2;
    portEXIT_CRITICAL(&psQMux);
}

static void psFlushQueue() {
    PsQ item;
    for (int i = 0; i < PS_Q; i++) {
        bool have = false;
        portENTER_CRITICAL(&psQMux);
        if (psQ[i].used) {
            item = psQ[i];
            if (--psQ[i].reps == 0) psQ[i].used = false;
            have = true;
        }
        portEXIT_CRITICAL(&psQMux);
        if (have) esp_now_send(item.mac, item.data, item.len);
    }
}

// Wait (until limitMs after the last beacon) for every power-save client to say it is awake: a client that woke a little
// late would otherwise get a time-critical message late (the radio retries it) or not at all.
static void psWaitReady(unsigned long limitMs) {
    for (;;) {
        bool all = true;
        for (int i = 0; i < espNowClientCount; i++) {
            if (!espNowClients[i].paired || !espNowClients[i].radioPsValid || !espNowClients[i].radioPs || !rstClientOnline(i)) continue;
            long d = (long)(espNowClients[i].awakeAt - psLastBeaconMs);
            if (espNowClients[i].awakeAt == 0 || d < -700 || d > 700) all = false;
        }
        if (all || millis() - psLastBeaconMs >= limitMs) return;
        vTaskDelay(1);
    }
}

void psGate(uint32_t maxAgeMs) {
    if (!psActive() || !wirelessEnabled) return;
    if ((long)(psGateOpenUntil - millis()) > 0) return;                // the same action already waited for this window
    if (millis() - psLastBeaconMs <= maxAgeMs) {                        // a window has just opened (short one: ~160 ms)
        psWaitReady(70);
        psGateOpenUntil = psLastBeaconMs + 150;
        return;
    }
    psCmdPending = true;                                                // the next beacon asks the clients to keep the window open ~500 ms
    uint32_t c = psBeaconCount;
    unsigned long t0 = millis();
    while (psBeaconCount == c && millis() - t0 < PS_INTERVAL_MS + 600) vTaskDelay(1);
    psWaitReady(300);
    psGateOpenUntil = psLastBeaconMs + 480;
}

static void psRequestAwake(unsigned long forMs) {
    unsigned long now = millis();
    if ((long)(psHoldUntil - now) <= 0) psHoldSince = now;
    psHoldUntil = now + forMs;
}

// true when it is fine to start something that needs every client listening (the clients learn it from the next beacons)
static bool psAwakeReady(unsigned long forMs) {
    if (!psActive()) return true;
    psRequestAwake(forMs);
    return millis() - psHoldSince >= 2 * PS_INTERVAL_MS + 700;
}

static void rstClearSendToClients() {
    uint8_t pkt[2] = { 0x16, 1 };
    for (int i = 0; i < espNowClientCount; i++)
        if (rstClientOnline(i)) espSendClient(espNowClients[i].mac, pkt, sizeof(pkt));
}

static void rstClearLoop() {
    if (rstClearReq) {
        rstClearReq = false;
        rstLogClear();                       // master Utility
        sendClearRstLogToLTC();              // master LTC (SPI)
        ltcRst[1] = ltcRst[2] = ltcRst[3] = 0;
        for (int i = 0; i < espNowClientCount; i++) {   // online clients clear themselves: show it at once
            if (!rstClientOnline(i)) continue;
            espNowClients[i].rstU[1] = espNowClients[i].rstU[2] = espNowClients[i].rstU[3] = 0;
            espNowClients[i].rstL[1] = espNowClients[i].rstL[2] = espNowClients[i].rstL[3] = 0;
        }
        if (wirelessEnabled) { rstClearSendToClients(); rstClearRepeat = 2; rstClearAt = millis(); }   // 3 sends: no ACK on this packet
    } else if (rstClearRepeat && wirelessEnabled && millis() - rstClearAt >= 400) {
        rstClearSendToClients();
        rstClearRepeat--;
        rstClearAt = millis();
    }
}

// ---------------------------------------------------------------------------------------------
// WiFi radio settings (TX power + ESP-NOW rate): chosen on the page, kept on every unit, pushed to the clients.
// Order matters: the clients are told FIRST (still over the old, stronger / slower setting) and the master switches about
// a second later, so a lower power or faster rate can never strand a client that has not heard the new setting yet.
static volatile bool radioSetReq = false;
static volatile uint8_t radioReqTx = 3, radioReqRate = 1, radioReqPs = 0;
// (radioApplyPending is a global in the main file: the main page shows it)
static uint8_t radioPendTx = 3, radioPendRate = 1, radioPendPs = 0;
static uint8_t radioRepeat = 0;
static unsigned long radioAt = 0;
static unsigned long radioFixAt[MAX_ESPNOW_CLIENTS];

static void radioSendTo(const uint8_t* mac, uint8_t tx, uint8_t rate, uint8_t ps) {
    uint8_t pkt[5] = { 0x17, 1, tx, rate, ps };
    espSendClient(mac, pkt, sizeof(pkt));
}

static void radioSendAll(uint8_t tx, uint8_t rate, uint8_t ps) {   // every PAIRED client, online or not (it may just be booting)
    for (int i = 0; i < espNowClientCount; i++)
        if (espNowClients[i].paired) radioSendTo(espNowClients[i].mac, tx, rate, ps);
}

static void radioLoopMaster() {
    unsigned long now = millis();
    if (radioSetReq) {
        radioSetReq = false;
        radioPendTx = radioReqTx;
        radioPendRate = radioReqRate;
        radioPendPs = radioReqPs;
        radioApplyPending = true;
        if (wirelessEnabled) { radioSendAll(radioPendTx, radioPendRate, radioPendPs); radioRepeat = 2; } else radioRepeat = 0;
        radioAt = now;
    } else if (radioApplyPending && now - radioAt >= 400) {
        if (radioRepeat && wirelessEnabled) {
            radioSendAll(radioPendTx, radioPendRate, radioPendPs);   // 3 sends in all: this packet has no acknowledgement
            radioRepeat--;
            radioAt = now;
        } else {                                        // clients have been told: now switch the master itself
            radioApplyPending = false;
            radioTxIdx = radioPendTx;
            radioRateIdx = radioPendRate;
            if (psOn && !radioPendPs) psOffUntil = now + 7000;   // clients may still be asleep: keep the beacons (and the queue) going a little longer
            psOn = radioPendPs != 0;
            radioSave();
            radioApplyAll();
        }
    }
    // A client that reports a different setting (it was offline or missed the packets) is told again, at most every 30 s
    if (!radioApplyPending && wirelessEnabled) {
        for (int i = 0; i < espNowClientCount; i++) {
            if (!espNowClients[i].paired || !espNowClients[i].radioValid) continue;
            if (espNowClients[i].radioTx == radioTxIdx && espNowClients[i].radioRate == radioRateIdx &&
                (!espNowClients[i].radioPsValid || espNowClients[i].radioPs == (psOn ? 1 : 0))) continue;
            if (now - radioFixAt[i] < 30000) continue;
            radioFixAt[i] = now;
            radioSendTo(espNowClients[i].mac, radioTxIdx, radioRateIdx, psOn ? 1 : 0);
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Radio test (power sweep), started from the main page. Phase 1: the clients transmit probe frames to the master at every
// TX power / rate (the master records how loudly it hears them). Phase 2: the master does the same and the clients record
// it. Phase 3: the clients report their tables. A probe is {0x18, 1, tag, seq}, tag = rateIdx*8 + levelIdx.
enum { RT_IDLE = 0, RT_CLIENTS = 1, RT_MASTER = 2, RT_COLLECT = 3, RT_DONE = 4 };
static uint8_t rtState = RT_IDLE;
static volatile bool rtStartReq = false;
static bool rtTarget[MAX_ESPNOW_CLIENTS];
static uint8_t rtCombo = 0, rtProbe = 0, rtRepeat = 0;
static unsigned long rtAt = 0, rtDeadline = 0;

static void rtSendAll(const uint8_t* pkt, size_t n, bool onlyUnreplied) {
    for (int i = 0; i < espNowClientCount; i++)
        if (rtTarget[i] && (!onlyUnreplied || !probeReplied[i])) esp_now_send(espNowClients[i].mac, pkt, n);
}

static void radioTestLoopMaster() {
    unsigned long now = millis();
    if (rtStartReq && psAwakeReady(60000)) {
        rtStartReq = false;
        if (rtState == RT_CLIENTS || rtState == RT_MASTER || rtState == RT_COLLECT) return;
        memset(probeSum, 0, sizeof(probeSum));
        memset(probeCnt, 0, sizeof(probeCnt));
        memset(probeM2C, 0, sizeof(probeM2C));
        memset(probeM2Cn, 0, sizeof(probeM2Cn));
        memset(probeReplied, 0, sizeof(probeReplied));
        int n = 0;
        for (int i = 0; i < MAX_ESPNOW_CLIENTS; i++) { rtTarget[i] = (i < espNowClientCount) && rstClientOnline(i); if (rtTarget[i]) n++; }
        if (!n || !wirelessEnabled) { rtState = RT_DONE; return; }
        uint8_t go[2] = { 0x19, 1 };
        rtSendAll(go, sizeof(go), false);
        rtRepeat = 2;
        rtAt = now + 300;
        rtDeadline = now + 6500;              // the clients' sweep takes about 4 s
        rtState = RT_CLIENTS;
        return;
    }
    switch (rtState) {
    case RT_CLIENTS:
        if (rtRepeat && now >= rtAt) {        // the start command is sent 3 times (no acknowledgement)
            uint8_t go[2] = { 0x19, 1 };
            rtSendAll(go, sizeof(go), false);
            rtRepeat--;
            rtAt = now + 300;
        }
        if (now >= rtDeadline) { rtState = RT_MASTER; rtCombo = 0; rtProbe = 0; rtAt = now + 100; }
        break;
    case RT_MASTER: {
        if (now < rtAt) break;
        uint8_t rate = rtCombo, level = 3;      // one combination per rate, at the fixed 11 dBm
        if (rtProbe == 0) {                   // switch to this power / rate, let the radio settle
            radioApplyRateTo(rate);
            rtProbe = 1;
            rtAt = now + 30;
            break;
        }
        uint8_t pkt[4] = { 0x18, 1, (uint8_t)(rate * 8 + level), rtProbe };
        rtSendAll(pkt, sizeof(pkt), false);
        rtProbe++;
        rtAt = now + 35;
        if (rtProbe > 5) {
            rtProbe = 0;
            rtCombo++;
            rtAt = now + 40;
            if (rtCombo >= 2) {              // sweep finished: back to the saved setting, then ask for the clients' tables
                radioApplyAll();
                rtState = RT_COLLECT;
                rtRepeat = 3;
                rtAt = now + 150;
                rtDeadline = now + 5000;
            }
        }
        break;
    }
    case RT_COLLECT: {
        bool all = true;
        for (int i = 0; i < espNowClientCount; i++) if (rtTarget[i] && !probeReplied[i]) all = false;
        if (all || now >= rtDeadline) { rtState = RT_DONE; break; }
        if (rtRepeat && now >= rtAt) {
            uint8_t req[2] = { 0x1B, 1 };
            rtSendAll(req, sizeof(req), true);
            rtRepeat--;
            rtAt = now + 700;
        }
        break;
    }
    default:
        break;
    }
}

static String radioTestJson() {
    String j = "{\"state\":" + String(rtState) + ",\"clients\":[";
    bool first = true;
    for (int i = 0; i < espNowClientCount; i++) {
        if (!rtTarget[i]) continue;
        if (!first) j += ",";
        first = false;
        char nm[3] = { espNowClients[i].deviceName[0], espNowClients[i].deviceName[1], 0 };
        cotaJsonClean(nm);
        j += "{\"mac\":\"" + espNowClients[i].macString + "\",\"name\":\"" + String(nm) + "\",\"replied\":" + String(probeReplied[i] ? "true" : "false");
        j += ",\"c2m\":[";
        for (int t = 0; t < 16; t++) { if (t) j += ","; j += String(probeCnt[i][t] ? probeSum[i][t] / probeCnt[i][t] : 0); }
        j += "],\"c2mN\":[";
        for (int t = 0; t < 16; t++) { if (t) j += ","; j += String(probeCnt[i][t]); }
        j += "],\"m2c\":[";
        for (int t = 0; t < 16; t++) { if (t) j += ","; j += String(probeM2C[i][t]); }
        j += "],\"m2cN\":[";
        for (int t = 0; t < 16; t++) { if (t) j += ","; j += String(probeM2Cn[i][t]); }
        j += "]}";
    }
    j += "]}";
    return j;
}

// What makes the clients stay awake: the beacon tells them
static bool psStayAwakeNow() {
    return (long)(psHoldUntil - millis()) > 0 || pairingMode || cotaRunning || cotaStartReq || mfwBusy() || otaMode ||
           (rtState >= RT_CLIENTS && rtState <= RT_COLLECT) || rtStartReq;
}

static void psBeaconCb(void*) {   // runs in the esp_timer task, every PS_INTERVAL_MS
    if (!psActive() || !wirelessEnabled || otaMode) return;
    struct_message b;
    memset(&b, 0, sizeof(b));
    SET_MSG_TYPE(b, "PSB");
    b.timestamp = millis();
    b.masterTimestamp = PS_INTERVAL_MS;
    b.userBits[0] = (psStayAwakeNow() ? 1 : 0) | (psCmdPending ? 2 : 0);
    psCmdPending = false;
    esp_now_send(broadcastAddress, (uint8_t*)&b, sizeof(b));
    psLastBeaconMs = millis();
    psBeaconCount = psBeaconCount + 1;
    psFlushQueue();   // the queued commands follow the beacon at once
}

static void psMasterBegin() {
    static bool started = false;
    if (started) return;
    started = true;
    esp_timer_create_args_t a = {};
    a.callback = psBeaconCb;
    a.name = "psBeacon";
    esp_timer_handle_t h = nullptr;
    if (esp_timer_create(&a, &h) == ESP_OK) esp_timer_start_periodic(h, (uint64_t)PS_INTERVAL_MS * 1000ULL);
}

// ---- LTC output offset (frames), set per unit from the main page: the master's own LTC over SPI, a client's over
// ESP-NOW {0x21, 1, frames}. Done here in loop() (the web handler only records the request).
static volatile bool ltcOfsReq = false;
static bool ltcOfsReqMaster = false;
static int ltcOfsReqIdx = -1;
static int8_t ltcOfsReqVal = 0;
static uint8_t ltcOfsMac[6];
static uint8_t ltcOfsRepeat = 0;
static unsigned long ltcOfsAt = 0;

static void ltcOfsLoop() {
    static bool bootSent = false;
    if (!bootSent && millis() > 8000) { bootSent = true; sendLtcOffsetToLTC(masterLtcOffset); }   // LTC also keeps it in NVS
    if (ltcOfsReq) {
        ltcOfsReq = false;
        if (ltcOfsReqMaster) {
            masterLtcOffset = ltcOfsReqVal;
            ltcOfsSave();
            sendLtcOffsetToLTC(masterLtcOffset);
            sendWebSocketData();
        } else if (ltcOfsReqIdx >= 0 && ltcOfsReqIdx < espNowClientCount) {
            memcpy(ltcOfsMac, espNowClients[ltcOfsReqIdx].mac, 6);
            ltcOfsRepeat = 3;   // no acknowledgement: 3 sends (with power save they wait for the client's window)
            ltcOfsAt = 0;
        }
    }
    if (ltcOfsRepeat && millis() >= ltcOfsAt) {
        uint8_t pkt[3] = { 0x21, 1, (uint8_t)ltcOfsReqVal };
        espSendClient(ltcOfsMac, pkt, sizeof(pkt));
        ltcOfsRepeat--;
        ltcOfsAt = millis() + 150;
    }
}

void clientOta_loop() {
    psMasterBegin();
    ltcOfsLoop();
    rstClearLoop();
    radioLoopMaster();
    radioTestLoopMaster();
    if (cotaCancelReq) {   // a client already flashing finishes; the remaining ones are skipped
        cotaCancelReq = false;
        for (int i = 0; i < cotaCount; i++)
            if (cota[i].state == CS_WAITING) cotaSetState(cota[i], CS_SKIPPED, "cancelled");
    }
    if (cotaStartReq && psAwakeReady(180000)) {   // power save: let every client hear "stay awake" first
        cotaStartReq = false;
        cotaLastError = cotaStart(cotaReqFlags & COTA_FLAG_UTILITY, cotaReqFlags & COTA_FLAG_LTC, cotaReqMacs, cotaReqParallel);
        cotaLastErrorAt = millis();
    }
    if (!cotaRunning) return;
    unsigned long now = millis();
    int active = 0, waiting = 0;
    for (int ci = 0; ci < cotaCount; ci++) {
        CotaEntry& e = cota[ci];
        uint8_t st = e.state;

        if (st == CS_SENT && now - e.lastMsg > COTA_ACK_TIMEOUT_MS) {
            if (e.sends < COTA_MAX_SENDS) cotaSendCmd(e);
            else cotaSetState(e, CS_ERROR, "no answer from client");
        } else if ((st == CS_ACKED || st == CS_PROGRESS) && now - e.started > COTA_CLIENT_TIMEOUT_MS) {
            cotaSetState(e, CS_TIMEOUT, "timed out");
        } else if ((st == CS_ACKED || st == CS_PROGRESS) && !(cotaFlags & COTA_FLAG_LTC)) {
            // Utility-only update: the client reboots into the new firmware and does not send DONE.
            // Detect the reboot from its heartbeat (uptime is in SECONDS, restarted after the command).
            for (int i = 0; i < espNowClientCount; i++) {
                if (memcmp(espNowClients[i].mac, e.mac, 6) != 0) continue;
                unsigned long sinceStart = now - e.started;
                if (sinceStart > 15000 && (long)(espNowClients[i].lastSeen - e.started) > 15000 &&
                    espNowClients[i].uptime < sinceStart / 1000) {
                    char msg[33];
                    snprintf(msg, sizeof(msg), "rebooted, utility v%u.%u.%u",
                             espNowClients[i].utilityVersionMajor, espNowClients[i].utilityVersionMinor,
                             espNowClients[i].utilityVersionPatch);
                    portENTER_CRITICAL(&cotaMux);
                    e.progress = 100;
                    portEXIT_CRITICAL(&cotaMux);
                    cotaSetState(e, CS_DONE, msg);
                }
                break;
            }
        }
        if (cotaIsActive(e.state)) active++;
        else if (e.state == CS_WAITING) waiting++;
    }

    // Start waiting clients until the allowed number are updating at the same time
    for (int ci = 0; ci < cotaCount && active < cotaParallel; ci++) {
        if (cota[ci].state != CS_WAITING) continue;
        cotaSendCmd(cota[ci]);
        active++;
        waiting--;
    }
    if (active == 0 && waiting == 0) cotaRunning = false;   // all finished (or skipped)
}

// ---------------------------------------------------------------------------------------------
// Reset log JSON of one unit (both chips): last reset reason, brownouts, watchdog resets, crashes
static String rstJson(bool valid, const uint8_t* u, const uint8_t* l, bool lvalid, unsigned long up, int lowb = -1) {
    char b[230];
    snprintf(b, sizeof(b), ",\"rst\":{\"v\":%s,\"u\":[%u,%u,%u,%u],\"lv\":%s,\"l\":[%u,%u,%u,%u],\"up\":%lu,\"lowb\":%d}",
             valid ? "true" : "false", u[0], u[1], u[2], u[3], lvalid ? "true" : "false", l[0], l[1], l[2], l[3], up, lowb);
    return String(b);
}

// How many brownout / watchdog / crash resets have been recorded on any known chip (0 = all clean)
static unsigned rstBadTotal() {
    unsigned n = rstSelf[1] + rstSelf[2] + rstSelf[3] + rstLowBatt;
    if (ltcRstValid) n += ltcRst[1] + ltcRst[2] + ltcRst[3];
    for (int i = 0; i < espNowClientCount; i++) {
        if (!espNowClients[i].paired || !espNowClients[i].rstValid) continue;
        n += espNowClients[i].rstU[1] + espNowClients[i].rstU[2] + espNowClients[i].rstU[3];
        if (espNowClients[i].rstLValid) n += espNowClients[i].rstL[1] + espNowClients[i].rstL[2] + espNowClients[i].rstL[3];
    }
    return n;
}

static String cotaStatusJson() {
    cotaInitSlots();
    String j = "{\"running\":";
    j += (cotaRunning || cotaStartReq) ? "true" : "false";
    {
        char le[64];
        if (!cotaLastError.empty() && millis() - cotaLastErrorAt > 15000) cotaLastError = "";   // stale
        cotaLastError.copyTo(le, sizeof(le)); cotaJsonClean(le);
        j += ",\"lastError\":\"" + String(le) + "\"";
    }
    j += ",\"flags\":" + String(cotaFlags) + ",\"par\":" + String(cotaParallel);
    j += ",\"radio\":{\"tx\":" + String(radioTxIdx) + ",\"rate\":" + String(radioRateIdx) + ",\"pending\":" + String(radioApplyPending ? "true" : "false") + "}";
    j += ",\"locked\":" + String((ltcSystemLocked || !wirelessEnabled || otaMode) ? "true" : "false");
    const char* keys[2] = { "utility", "ltc" };
    for (int s = 0; s < 2; s++) {
        CotaSlot& sl = cotaSlots[s];
        char nm[48]; strncpy(nm, sl.name, sizeof(nm) - 1); nm[sizeof(nm) - 1] = 0; cotaJsonClean(nm);
        j += ",\"" + String(keys[s]) + "\":{\"valid\":" + String(sl.valid ? "true" : "false");
        j += ",\"size\":" + String(sl.size);
        j += ",\"capacity\":" + String(sl.part ? sl.part->size : 0);
        j += ",\"name\":\"" + String(nm) + "\"}";
    }
    j += ",\"clients\":[";
    unsigned long now = millis();
    for (int i = 0; i < cotaCount; i++) {
        CotaEntry e;
        portENTER_CRITICAL(&cotaMux);
        memcpy((void*)&e, (const void*)&cota[i], sizeof(e));
        portEXIT_CRITICAL(&cotaMux);
        char mac[18];
        snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X", e.mac[0], e.mac[1], e.mac[2], e.mac[3], e.mac[4], e.mac[5]);
        char st[33]; strncpy(st, e.status, 32); st[32] = 0; cotaJsonClean(st);
        char nm[3] = {e.name[0], e.name[1], 0}; cotaJsonClean(nm);
        if (i) j += ",";
        j += "{\"mac\":\"" + String(mac) + "\",\"name\":\"" + String(nm) + "\",\"state\":\"" + COTA_STATE_NAMES[e.state];
        j += "\",\"progress\":" + String(e.progress) + ",\"status\":\"" + String(st) + "\"";
        j += ",\"current\":" + String(cotaRunning && cotaIsActive(e.state) ? "true" : "false");
        j += ",\"secs\":" + String(e.started ? (now - e.started) / 1000 : 0) + "}";
    }
    j += "]";
    // Every paired client with the firmware versions of BOTH its chips (from its heartbeat)
    j += ",\"pairs\":[";
    bool first = true;
    for (int i = 0; i < espNowClientCount; i++) {
        if (!espNowClients[i].paired) continue;
        const ESPNowClient& c = espNowClients[i];
        char mac[18];
        snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X", c.mac[0], c.mac[1], c.mac[2], c.mac[3], c.mac[4], c.mac[5]);
        char nm[3] = {c.deviceName[0], c.deviceName[1], 0}; cotaJsonClean(nm);
        char ver[64];
        snprintf(ver, sizeof(ver), "\"uv\":\"%u.%u.%u\",\"lv\":\"%u.%u.%u\"",
                 c.utilityVersionMajor, c.utilityVersionMinor, c.utilityVersionPatch,
                 c.ltcVersionMajor, c.ltcVersionMinor, c.ltcVersionPatch);
        if (!first) j += ",";
        first = false;
        j += "{\"mac\":\"" + String(mac) + "\",\"name\":\"" + String(nm) + "\"," + String(ver);
        j += ",\"online\":" + String(now - c.lastSeen <= COTA_ONLINE_WINDOW_MS ? "true" : "false");
        j += ",\"diag\":{\"valid\":" + String(c.diagValid ? "true" : "false") + ",\"lost\":" + String(c.diagNetLost) +
             ",\"gap\":" + String(c.diagMaxGapUs) + ",\"up\":" + String(c.diagSinceValid ? c.diagSinceS : c.uptime) +
             ",\"ef\":" + String(c.diagErrFirstS) + ",\"el\":" + String(c.diagErrLastS) + ",\"ew\":" + String(c.diagErrWins) + "}" +
             rstJson(c.rstValid, c.rstU, c.rstL, c.rstLValid, c.uptime) +
             ",\"radio\":{\"v\":" + String(c.radioValid ? "true" : "false") + ",\"tx\":" + String(c.radioTx) + ",\"rate\":" + String(c.radioRate) + "}}";
    }
    j += "]";
    j += ",\"master\":" + mfwStatusJsonPart();
    j += "}";
    return j;
}

static String cotaStart(bool doUtility, bool doLtc, const char* macs, uint8_t parallel) {
    cotaInitSlots();
    if (cotaRunning) return "an update is already running";
    if (mfwBusy()) return "a master update is running";
    if (otaMode || !wirelessEnabled || ltcSystemLocked) return "master is locked, wireless is off or in OTA mode";
    if (!doUtility || !doLtc) return "utility and LTC must be updated together - choose both files";
    if (doUtility && !cotaSlots[0].valid) return "no client utility firmware uploaded";
    if (doLtc && !cotaSlots[1].valid) return "no client LTC firmware uploaded";

    cotaFlags = (doUtility ? COTA_FLAG_UTILITY : 0) | (doLtc ? COTA_FLAG_LTC : 0);
    portENTER_CRITICAL(&cotaMux);
    cotaCount = 0;   // the ESP-NOW callback ignores the list while it is rebuilt
    portEXIT_CRITICAL(&cotaMux);
    unsigned long now = millis();
    int online = 0;
    int n = 0;
    for (int i = 0; i < espNowClientCount && n < MAX_ESPNOW_CLIENTS; i++) {
        if (!espNowClients[i].paired) continue;
        if (macs[0]) {   // only the selected clients
            char m[18];
            const uint8_t* a = espNowClients[i].mac;
            snprintf(m, sizeof(m), "%02X:%02X:%02X:%02X:%02X:%02X", a[0], a[1], a[2], a[3], a[4], a[5]);
            if (!strstr(macs, m)) continue;
        }
        CotaEntry& e = cota[n++];
        memset((void*)&e, 0, sizeof(e));
        memcpy(e.mac, espNowClients[i].mac, 6);
        e.name[0] = espNowClients[i].deviceName[0];
        e.name[1] = espNowClients[i].deviceName[1];
        if (now - espNowClients[i].lastSeen > COTA_ONLINE_WINDOW_MS) {
            e.state = CS_SKIPPED;
            strcpy(e.status, "offline");
        } else {
            e.state = CS_WAITING;
            strcpy(e.status, "waiting");
            online++;
        }
    }
    if (online == 0)   // list stays empty: no stale table
        return macs[0] ? "none of the selected clients is online" : "no paired client is online";
    portENTER_CRITICAL(&cotaMux);
    cotaCount = n;
    portEXIT_CRITICAL(&cotaMux);
    cotaParallel = parallel < 1 ? 1 : (parallel > COTA_MAX_PARALLEL ? COTA_MAX_PARALLEL : parallel);
    cotaRunning = true;   // clientOta_loop() sends the commands
    return "";
}

// ---------------------------------------------------------------------------------------------
static void cotaHandleUpload(AsyncWebServerRequest* req, const String& filename, size_t index,
                             uint8_t* data, size_t len, bool final) {
    int type = cotaTypeParam(req);
    CotaSlot* s = cotaSlotFor(type);
    if (!s) return;
    if (index == 0) {
        s->valid = false;
        s->writing = false;
        s->size = 0;
        s->erasedTo = 0;
        s->name[0] = 0;
        s->idFound = false;
        s->tailLen = 0;
        cotaUploadError = "";
        if (!s->part) { cotaUploadError = "no free flash partition to store it"; return; }
        if (cotaRunning) { cotaUploadError = "a client update is running"; return; }
        if (mfwBusy()) { cotaUploadError = "a master update is running"; return; }
        if (s->part->type == ESP_PARTITION_TYPE_APP) {
            // Right after the master itself was updated, the spare app slot still holds the rollback
            // copy until the new firmware is confirmed. Never overwrite it in that state.
            esp_ota_img_states_t st;
            if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK &&
                st == ESP_OTA_IMG_PENDING_VERIFY) {
                cotaUploadError = "master firmware not confirmed yet - restart the master and retry";
                return;
            }
        }
        String fn = filename;
        fn.toLowerCase();
        const char* who = (type <= 3) ? "client" : "master";
        const char* what = (type == 2 || type == 4) ? "utility" : "ltc";
        if (fn.indexOf(who) < 0 || fn.indexOf(what) < 0) {
            cotaUploadError = String("file name must contain '") + who + "' and '" + what + "'";
            return;
        }
        if (len == 0 || data[0] != 0xE9) { cotaUploadError = "not an ESP32 firmware image"; return; }
        // The partner slot on the same partition (client <-> master image of the same chip type)
        // is about to be overwritten.
        CotaSlot* partner = cotaSlotFor(type <= 3 ? type + 2 : type - 2);
        if (partner && partner->part == s->part) partner->valid = false;
        strncpy(s->name, filename.c_str(), sizeof(s->name) - 1);
        s->writing = true;
    }
    if (!s->writing) return;
    size_t end = index + len;
    if (end > s->part->size) {
        cotaUploadError = "firmware too large for the storage partition";
        s->writing = false;
        return;
    }
    while (s->erasedTo < end) {   // erase just ahead of the data: 64 KB blocks where aligned, else 4 KB sectors
        uint32_t chunk = (s->erasedTo % 65536 == 0 && s->erasedTo + 65536 <= s->part->size) ? 65536 : 4096;
        if (esp_partition_erase_range(s->part, s->erasedTo, chunk) != ESP_OK) {
            cotaUploadError = "flash erase failed";
            s->writing = false;
            return;
        }
        s->erasedTo += chunk;
    }
    if (len && esp_partition_write(s->part, index, data, len) != ESP_OK) {
        cotaUploadError = "flash write failed";
        s->writing = false;
        return;
    }
    cotaScanId(s, type, data, len);
    if (final) {
        if (!s->idFound) {   // never install a file unless it identifies itself as the right firmware
            cotaUploadError = String("this is not a ") + cotaSlotLabel(type) + " firmware (ID tag missing)";
            s->writing = false;
            return;
        }
        s->size = end;
        s->valid = true;
        s->writing = false;
    }
}

extern const char COTA_PAGE[] PROGMEM;

void clientOta_setupRoutes() {
    cotaInitSlots();

    server.on("/client-update", HTTP_GET, [](AsyncWebServerRequest* req) {
        sendPageChunked(req, COTA_PAGE);
    });

    server.on("/client_fw", HTTP_POST,
        [](AsyncWebServerRequest* req) {
            CotaSlot* s = cotaSlotFor(cotaTypeParam(req));
            if (s && s->valid) req->send(200, "application/json", "{\"ok\":true,\"size\":" + String(s->size) + "}");
            else req->send(400, "application/json", "{\"ok\":false,\"error\":\"" +
                           (cotaUploadError.length() ? cotaUploadError : String("upload failed")) + "\"}");
        },
        [](AsyncWebServerRequest* req, String filename, size_t index, uint8_t* data, size_t len, bool final) {
            cotaHandleUpload(req, filename, index, data, len, final);
        });

    // Clients download from here: /firmware?type=2 (client utility) or type=3 (client LTC)
    server.on("/firmware", HTTP_GET, [](AsyncWebServerRequest* req) {
        int type = cotaTypeParam(req);
        CotaSlot* s = (type == 2 || type == 3) ? cotaSlotFor(type) : nullptr;   // client images only
        if (!s || !s->valid || !s->part) { req->send(404, "text/plain", "no firmware stored"); return; }
        const esp_partition_t* p = s->part;
        uint32_t total = s->size;
        AsyncWebServerResponse* r = req->beginResponse("application/octet-stream", total,
            [p, total](uint8_t* buf, size_t maxLen, size_t index) -> size_t {
                if (index >= total) return 0;
                size_t n = total - index;
                if (n > maxLen) n = maxLen;
                if (esp_partition_read(p, index, buf, n) != ESP_OK) return 0;
                return n;
            });
        req->send(r);
    });

    // A client downloading its firmware is on the master's WiFi with ESP-NOW switched off: it reports
    // its progress here instead, so the page stays live (?mac=AA:BB:..&p=NN&s=text)
    server.on("/client_ota/progress", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (req->hasParam("mac") && req->hasParam("p")) {
            String mac = req->getParam("mac")->value();
            uint8_t p = (uint8_t)req->getParam("p")->value().toInt();
            String st = req->hasParam("s") ? req->getParam("s")->value() : String("");
            portENTER_CRITICAL(&cotaMux);
            for (int i = 0; i < cotaCount; i++) {
                char m[18];
                snprintf(m, sizeof(m), "%02X:%02X:%02X:%02X:%02X:%02X", cota[i].mac[0], cota[i].mac[1], cota[i].mac[2],
                         cota[i].mac[3], cota[i].mac[4], cota[i].mac[5]);
                if (!mac.equalsIgnoreCase(m)) continue;
                CotaEntry& e = cota[i];
                if (e.state == CS_SENT || e.state == CS_ACKED || e.state == CS_PROGRESS) {
                    e.state = CS_PROGRESS;
                    e.progress = p > 100 ? 100 : p;
                    strncpy(e.status, st.c_str(), sizeof(e.status) - 1);
                    e.status[sizeof(e.status) - 1] = 0;
                    e.lastMsg = millis();
                }
                break;
            }
            portEXIT_CRITICAL(&cotaMux);
        }
        req->send(200, "text/plain", "ok");
    });

    // Memory / stack health of the master utility (shown on the firmware page)
    server.on("/sysinfo", HTTP_GET, [](AsyncWebServerRequest* req) {
        TaskHandle_t loopT = xTaskGetHandle("loopTask");
        TaskHandle_t webT = xTaskGetHandle("async_tcp");
        char b[340];
        snprintf(b, sizeof(b),
                 "{\"free\":%u,\"minFree\":%u,\"maxBlock\":%u,\"heapOk\":%s,\"loopStack\":%u,\"webStack\":%u,\"up\":%lu,\"reset\":%d,\"autoSync\":%lu,\"rstBad\":%u}",
                 (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap(), (unsigned)ESP.getMaxAllocHeap(),
                 heap_caps_check_integrity_all(true) ? "true" : "false",
                 loopT ? (unsigned)uxTaskGetStackHighWaterMark(loopT) : 0u,
                 webT ? (unsigned)uxTaskGetStackHighWaterMark(webT) : 0u,
                 (unsigned long)(millis() / 1000), (int)esp_reset_reason(), (unsigned long)syncIntervalMinutes, rstBadTotal());
        req->send(200, "application/json", b);
    });

    server.on("/client_ota/status", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->send(200, "application/json", cotaStatusJson());
    });

    // Validates only (read-only) and posts a request; loop() builds the client list and starts.
    server.on("/client_ota/start", HTTP_POST, [](AsyncWebServerRequest* req) {
        bool u = req->hasParam("u") && req->getParam("u")->value() == "1";
        bool l = req->hasParam("l") && req->getParam("l")->value() == "1";
        const char* err = nullptr;
        if (cotaRunning || cotaStartReq) err = "an update is already running";
        else if (mfwBusy()) err = "a master update is running";
        else if (otaMode || !wirelessEnabled || ltcSystemLocked) err = "master is locked, wireless is off or in OTA mode";
        else if (!u || !l) err = "utility and LTC must be updated together - choose both files";
        else if (u && !cotaSlots[0].valid) err = "no client utility firmware uploaded";
        else if (l && !cotaSlots[1].valid) err = "no client LTC firmware uploaded";
        String macs = req->hasParam("macs") ? req->getParam("macs")->value() : String("");
        macs.toUpperCase();
        if (!err && macs.length() >= sizeof(cotaReqMacs)) err = "too many clients selected";
        if (err) { req->send(400, "application/json", "{\"ok\":false,\"error\":\"" + String(err) + "\"}"); return; }
        cotaLastError = "";
        strncpy(cotaReqMacs, macs.c_str(), sizeof(cotaReqMacs) - 1);
        cotaReqMacs[sizeof(cotaReqMacs) - 1] = 0;
        cotaReqParallel = req->hasParam("par") ? (uint8_t)req->getParam("par")->value().toInt() : 1;
        cotaReqFlags = (u ? COTA_FLAG_UTILITY : 0) | (l ? COTA_FLAG_LTC : 0);
        cotaStartReq = true;
        req->send(200, "application/json", "{\"ok\":true}");
    });

    server.on("/ltcofs/set", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasParam("who") || !req->hasParam("v")) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"missing value\"}");
            return;
        }
        int v = req->getParam("v")->value().toInt();
        if (v < -30 || v > 30) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"offset must be -30 to 30 frames\"}");
            return;
        }
        if (cotaRunning || cotaStartReq || mfwBusy() || ltcOfsReq || ltcOfsRepeat) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"busy - try again in a moment\"}");
            return;
        }
        String who = req->getParam("who")->value();
        int idx = -1;
        if (who != "master") {
            for (int i = 0; i < espNowClientCount; i++)
                if (espNowClients[i].paired && espNowClients[i].macString.equalsIgnoreCase(who)) { idx = i; break; }
            if (idx < 0) {
                req->send(400, "application/json", "{\"ok\":false,\"error\":\"client not found\"}");
                return;
            }
        }
        ltcOfsReqMaster = (who == "master");
        ltcOfsReqIdx = idx;
        ltcOfsReqVal = (int8_t)v;
        ltcOfsReq = true;   // loop() does the work
        req->send(200, "application/json", "{\"ok\":true}");
    });

    server.on("/radio/test", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->send(200, "application/json", radioTestJson());
    });

    server.on("/radio/test/start", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (cotaRunning || cotaStartReq || mfwBusy()) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"an update is running - try again when it has finished\"}");
            return;
        }
        if (!wirelessEnabled || ltcSystemLocked || otaMode) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"wireless is off (locked / OTA mode)\"}");
            return;
        }
        if (rtState == RT_CLIENTS || rtState == RT_MASTER || rtState == RT_COLLECT || radioApplyPending || radioSetReq) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"a radio test or a setting change is already running\"}");
            return;
        }
        rtStartReq = true;   // loop() runs the test
        req->send(200, "application/json", "{\"ok\":true}");
    });

    server.on("/radio/set", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (rtState == RT_CLIENTS || rtState == RT_MASTER || rtState == RT_COLLECT) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"a radio test is running - wait about 15 seconds\"}");
            return;
        }
        if (cotaRunning || cotaStartReq || mfwBusy()) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"an update is running - try again when it has finished\"}");
            return;
        }
        if (!req->hasParam("rate")) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"missing value\"}");
            return;
        }
        int rate = req->getParam("rate")->value().toInt();
        if (rate < 0 || rate > 1) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"value out of range\"}");
            return;
        }
        radioReqRate = (uint8_t)rate;
        radioReqPs = (req->hasParam("ps") ? req->getParam("ps")->value().toInt() : (psOn ? 1 : 0)) ? 1 : 0;
        radioSetReq = true;   // loop() tells the clients first, then switches the master
        req->send(200, "application/json", "{\"ok\":true}");
    });

    server.on("/rstlog/clear", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (cotaRunning || cotaStartReq || mfwBusy()) {
            req->send(400, "application/json", "{\"ok\":false,\"error\":\"an update is running - try again when it has finished\"}");
            return;
        }
        int online = 0;
        for (int i = 0; i < espNowClientCount; i++) if (rstClientOnline(i)) online++;
        rstClearReq = true;   // loop() does the work (NVS / SPI / ESP-NOW)
        req->send(200, "application/json", "{\"ok\":true,\"clients\":" + String(online) + "}");
    });

    server.on("/client_ota/cancel", HTTP_POST, [](AsyncWebServerRequest* req) {
        cotaCancelReq = true;   // handled in loop()
        req->send(200, "application/json", "{\"ok\":true}");
    });
}

const char COTA_PAGE[] PROGMEM = R"=====(<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firmware update</title><script>(function(){var d=false;function chk(){if(d)return;d=true;try{if(document.getElementById('pageEnd')){sessionStorage.removeItem('pgTry');return}var n=+(sessionStorage.getItem('pgTry')||0);if(n<4){sessionStorage.setItem('pgTry',n+1);setTimeout(function(){location.reload()},1500+n*1000)}}catch(e){}}window.addEventListener('load',chk);setTimeout(chk,9000)})()</script><style>
body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:16px}
.card{background:#16213e;border-radius:10px;padding:16px;margin:0 auto 16px;max-width:680px}
h1{font-size:20px;color:#4ecca3;margin:0 0 8px}h2{font-size:17px;color:#4ecca3;margin:0 0 8px}
label{display:block;margin:12px 0 4px;font-weight:bold}input[type=file]{width:100%;color:#ccc}
.muted{color:#999;font-size:13px}.ok{color:#4ecca3}.err{color:#e94560}.warn{color:#f5a623}
button{background:#4ecca3;color:#111;border:0;border-radius:6px;padding:12px 16px;font-size:15px;font-weight:bold;cursor:pointer;width:100%;margin-top:12px}
button:disabled{background:#555;color:#999;cursor:default}button.sec{background:#0f3460;color:#eee}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px;border-bottom:1px solid #0f3460;text-align:left}
.bar{background:#0f3460;border-radius:4px;height:10px;overflow:hidden;margin-top:8px}.bar i{display:block;height:10px;background:#4ecca3}
.ver{font-size:15px;margin:6px 0}a{color:#4ecca3}
</style></head><body>

<div class="card"><a href="/" style="display:inline-block;margin-bottom:10px;text-decoration:none;font-weight:bold">&larr; Back to main page</a><h1>Firmware update</h1>
<p class="muted">Every unit is a pair of chips (Utility + LTC). They are <b>always updated together</b>, so both run the same version. Both files are checked (name and built-in identity tag) before anything is installed.</p></div>

<div class="card"><h2>Master</h2>
<div class="ver" id="mver">Utility -, LTC -</div><div id="mnote" class="muted"></div>
<label>Master Utility firmware (.bin)</label><input type="file" id="mu" accept=".bin"><div class="muted" id="mus"></div>
<label>Master LTC firmware (.bin)</label><input type="file" id="ml" accept=".bin"><div class="muted" id="mls"></div>
<button id="mgo" onclick="goMaster()">Update master (Utility + LTC together)</button>
<div class="bar"><i id="mbar" style="width:0%"></i></div>
<div id="mmsg" class="muted" style="margin-top:8px"></div>
<p class="muted">Order: the LTC is installed first; only if that succeeds is the Utility installed. If anything fails, nothing is changed. The master restarts at the end - reconnect to its WiFi and reload this page.</p></div>

<div class="card"><h2>Clients</h2>
<label>Client Utility firmware (.bin)</label><input type="file" id="cu" accept=".bin"><div class="muted" id="cus"></div>
<label>Client LTC firmware (.bin)</label><input type="file" id="cl" accept=".bin"><div class="muted" id="cls"></div>
<label>Clients to update</label>
<table><thead><tr><th style="width:28px"><input type="checkbox" id="call" onchange="selAll(this.checked)" title="select all online"></th><th>Cam</th><th>Status</th><th>Utility / LTC</th></tr></thead><tbody id="csel"><tr><td colspan="4" class="muted">no paired clients</td></tr></tbody></table>
<label>How</label>
<div><label style="display:inline;font-weight:normal"><input type="radio" name="par" value="1" checked> One at a time (safest: a problem stops at one unit)</label></div>
<div><label style="display:inline;font-weight:normal"><input type="radio" name="par" value="4"> Up to 4 at once (faster; those units are out of sync together)</label></div>
<button id="cgo" onclick="goClients(false)">Update selected clients (Utility + LTC together)</button>
<button id="cgoall" onclick="goClients(true)">Update all online clients</button>
<button class="sec" onclick="cancelAll()">Stop: start no more clients (running ones finish)</button>
<div id="cmsg" class="muted" style="margin-top:8px"></div>
<table style="margin-top:10px"><thead><tr><th>Cam</th><th>State</th><th style="width:35%">Progress</th></tr></thead><tbody id="tb"><tr><td colspan="3" class="muted">No client update started yet.</td></tr></tbody></table></div>

<div class="card"><h2>Installed versions</h2>
<table><thead><tr><th>Unit</th><th>Utility</th><th>LTC</th><th>Pair</th></tr></thead><tbody id="vt"></tbody></table>
<p class="muted">"Mismatch" means the two chips of a unit run different versions (for example after an interrupted update). It still works, but update that unit again to bring both chips to the same version.</p>
<p class="muted"><a href="/">&larr; back</a></p></div>

<div class="card"><h2>Reset log (power health)</h2>
<div id="rsts" class="muted" style="margin-bottom:8px"></div>
<div style="overflow-x:auto"><table><thead><tr><th>Unit</th><th>Utility restart</th><th>LTC restart</th><th>Utility up</th><th>Utility B / W / C</th><th>LTC B / W / C</th></tr></thead><tbody id="rstt"></tbody></table></div>
<button class="sec" id="rstclr" onclick="clearRst()">Clear reset log (master + online clients)</button>
<div id="rstmsg" class="muted" style="margin:6px 0"></div>
<p class="muted"><b>B / W / C</b> = brownouts / watchdog resets / crashes. Every chip remembers why it last restarted and counts them (kept across restarts; counting starts when this firmware version was installed). <b>BROWNOUT</b> = the supply voltage dipped too low and the chip was reset by the power monitor (typical causes: WiFi transmit peaks on a weak battery, regulator or connector). A restart of a Utility while its LTC keeps running also shifts that unit's timecode until the next Sync. Clients report every ~10 s while online.</p></div>

<div class="card"><h2>Clock check (LTC sample pulses)</h2>
<table><thead><tr><th>Unit</th><th>Missed (+) / extra (-) pulses</th><th>&asymp; ppm</th><th>Worst gap</th><th>Measured over</th></tr></thead><tbody id="ct"></tbody></table>
<h2 style="margin-top:14px">Master LTC events</h2><table><thead><tr><th>When</th><th>Missed pulses</th><th>Longest gap</th></tr></thead><tbody id="ev"></tbody></table>
<div class="muted" id="sys" style="margin:10px 0"></div>
<p class="muted">Pressing Sync (page or button) restarts these counters on every unit. Each LTC counts, inside its sample interrupt, clock pulses that were missed or doubled since it started. 0 = no pulse lost: any timecode drift then comes from the crystals or from the reference you compared with. A positive number means the LTC lost time (missed interrupts); negative means it gained time (extra pulses, e.g. noise on the clock line). Normal: worst gap below ~40 &micro;s.</p></div>

<script>
let st=null;
function $(i){return document.getElementById(i)}
function kb(n){return (n/1024).toFixed(0)+' KB'}
let mLocal=false,cLocal=false;   // true while this page is uploading/starting: do not overwrite its message
function say(id,t,c){$(id).innerHTML='<span class="'+(c||'')+'">'+t+'</span>'}
function pair(u,l){return u==l?'<span class="ok">matched</span>':'<span class="warn">mismatch</span>'}
function up(type,file,msgId){return new Promise((res,rej)=>{const x=new XMLHttpRequest();const f=new FormData();f.append('firmware',file,file.name);
x.open('POST','/client_fw?type='+type);x.upload.onprogress=e=>{if(e.lengthComputable)say(msgId,'Uploading '+file.name+': '+Math.round(100*e.loaded/e.total)+'%')};
x.onload=()=>{let r={};try{r=JSON.parse(x.responseText)}catch(e){};x.status==200&&r.ok?res():rej(r.error||('HTTP '+x.status))};x.onerror=()=>rej('network error');x.send(f)})}
async function need(type,input,stored,msgId){if($(input).files[0])return up(type,$(input).files[0],msgId);if(!stored)throw 'choose both files'}
async function goMaster(){$('mgo').disabled=true;mLocal=true;say('mmsg','Preparing...');$('mbar').style.width='0%';try{
 if(!(st&&st.master.ltcWeb))throw 'the master LTC firmware is too old for web updates - update it once with the OTA button';
 await need(4,'mu',st&&st.master.utility.valid,'mmsg');await need(5,'ml',st&&st.master.ltc.valid,'mmsg');
 const r=await fetch('/master_fw/start',{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;say('mmsg','Master update started','ok')}
 catch(e){say('mmsg','Error: '+e,'err');$('mgo').disabled=false}setTimeout(()=>{mLocal=false},3000);poll()}
const sel=new Set();let selSig='';
function tg(cb){if(cb.checked)sel.add(cb.dataset.mac);else sel.delete(cb.dataset.mac)}
function selAll(on){if(!st)return;for(const p of st.pairs){if(on&&p.online)sel.add(p.mac);else sel.delete(p.mac)}selSig='';poll()}
async function goClients(all){$('cgo').disabled=true;$('cgoall').disabled=true;cLocal=true;say('cmsg','Preparing...');try{
 const macs=all?'':[...sel].join(',');if(!all&&!macs)throw 'select at least one client (or use Update all online clients)';
 const par=document.querySelector('input[name=par]:checked').value;
 await need(2,'cu',st&&st.utility.valid,'cmsg');await need(3,'cl',st&&st.ltc.valid,'cmsg');
 const r=await fetch('/client_ota/start?u=1&l=1&par='+par+(macs?'&macs='+encodeURIComponent(macs):''),{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;say('cmsg','Client update started','ok')}
 catch(e){say('cmsg','Error: '+e,'err');$('cgo').disabled=false;$('cgoall').disabled=false}setTimeout(()=>{cLocal=false},3000);poll()}
async function cancelAll(){await fetch('/client_ota/cancel',{method:'POST'});poll()}
const RN={0:'unknown',1:'power on',2:'reset button',3:'software restart',4:'CRASH',5:'WATCHDOG',6:'WATCHDOG',7:'WATCHDOG',8:'deep sleep',9:'BROWNOUT',10:'SDIO'};
const RBAD={4:1,5:1,6:1,7:1,9:1};
function rr(ok,a){if(!ok)return '<span class="muted">not reported</span>';const bad=RBAD[a[0]]&&(a[1]+a[2]+a[3]>0);return '<span class="'+(bad?'err':(RBAD[a[0]]?'muted':'ok'))+'">'+(RN[a[0]]||a[0])+(RBAD[a[0]]&&!bad?' (cleared)':'')+'</span>'}
function rc(ok,a){if(!ok)return '<span class="muted">-</span>';return [a[1],a[2],a[3]].map(n=>'<span class="'+(n>0?'err':'')+'">'+n+'</span>').join(' / ')}
function upTxt(s){return s>=7200?(s/3600).toFixed(1)+' h':Math.round(s/60)+' min'}
async function clearRst(){
 if(!confirm('Clear the reset log on the master and on every online client?\n\nOffline clients keep their counts until you clear again while they are online. Clients need firmware 1.3.14 or newer to clear.'))return;
 try{const r=await fetch('/rstlog/clear',{method:'POST'});const j=await r.json();if(!j.ok)throw j.error;
  say('rstmsg','Cleared on the master. Sent to '+j.clients+' online client(s) - their numbers update within about 10 s.','ok')}
 catch(e){say('rstmsg','Could not clear: '+e,'err')}poll()}
function slotTxt(s){return s.valid?('Stored on master: '+s.name+' ('+kb(s.size)+')'):''}
async function poll(){try{const r=await fetch('/client_ota/status');st=await r.json();const m=st.master;
 $('mver').innerHTML='Utility v'+m.uv+', LTC v'+m.lv+' &nbsp; '+pair(m.uv,m.lv);
 $('mnote').innerHTML=m.ltcWeb?'':'<span class="warn">The master LTC runs an older firmware without web update support. Update the master LTC once with the OTA button; after that both are updated here.</span>';
 $('mus').textContent=slotTxt(m.utility);$('mls').textContent=slotTxt(m.ltc);$('cus').textContent=slotTxt(st.utility);$('cls').textContent=slotTxt(st.ltc);
 if(!mLocal||m.busy){if(m.busy||m.msg){const c=m.msg.indexOf('FAILED')==0||m.msg.indexOf('differ')>=0?'err':(m.msg.indexOf('updated')>=0?'ok':'');say('mmsg',m.msg,c)}else $('mmsg').innerHTML='';
  $('mbar').style.width=(m.busy?m.pct:(m.msg.indexOf('installed together')>=0?100:0))+'%'}
 $('mgo').disabled=m.busy||st.running;$('cgo').disabled=$('cgoall').disabled=m.busy||st.running;$('rstclr').disabled=m.busy||st.running;
 for(const p of st.pairs)if(!p.online)sel.delete(p.mac);
 const sig=JSON.stringify(st.pairs.map(p=>[p.mac,p.online,p.uv,p.lv,p.name]));
 if(sig!==selSig){selSig=sig;$('csel').innerHTML=st.pairs.length?st.pairs.map(p=>'<tr><td><input type="checkbox" data-mac="'+p.mac+'" onchange="tg(this)"'+(sel.has(p.mac)?' checked':'')+(p.online?'':' disabled')+'></td><td>'+(p.name||p.mac)+'</td><td class="'+(p.online?'ok':'muted')+'">'+(p.online?'online':'offline')+'</td><td>'+p.uv+' / '+p.lv+'</td></tr>').join(''):'<tr><td colspan="4" class="muted">no paired clients</td></tr>'}
 if(st.clients.length){let h='';for(const c of st.clients){const cls=c.state=='done'?'ok':((c.state=='error'||c.state=='timed out')?'err':'');
  h+='<tr><td>'+(c.name||c.mac)+'</td><td class="'+cls+'">'+(c.current&&st.running?'&#9654; ':'')+c.state+(c.current&&st.running?' ('+c.secs+' s)':'')+'<div class="muted">'+c.status+'</div></td><td><div class="bar"><i style="width:'+c.progress+'%"></i></div></td></tr>'}
  $('tb').innerHTML=h}
 if(!cLocal){if(!st.running&&st.lastError)say('cmsg','Could not start: '+st.lastError,'err');else if(!st.running&&$('cmsg').textContent.indexOf('Could not start')==0)$('cmsg').innerHTML=''}
 let v='<tr><td>Master</td><td>'+m.uv+'</td><td>'+m.lv+'</td><td>'+pair(m.uv,m.lv)+'</td></tr>';
 for(const p of st.pairs)v+='<tr><td>'+(p.name||p.mac)+(p.online?'':' <span class="muted">(offline)</span>')+'</td><td>'+p.uv+'</td><td>'+p.lv+'</td><td>'+pair(p.uv,p.lv)+'</td></tr>';
 $('vt').innerHTML=v;
 {const rows=[{n:'Master',r:m.rst,on:true}].concat(st.pairs.map(p=>({n:p.name||p.mac,r:p.rst,on:p.online})));let h='',bad=[];
  for(const x of rows){const r=x.r||{v:false,u:[0,0,0,0],l:[0,0,0,0],lv:false,up:0};
   const tag=x.on?'':' <span class="muted">(offline, last known)</span>';
   h+='<tr><td>'+x.n+tag+'</td><td>'+rr(r.v,r.u)+'</td><td>'+rr(r.lv,r.l)+'</td><td>'+(r.v&&x.on?upTxt(r.up):'-')+'</td><td>'+rc(r.v,r.u)+'</td><td>'+rc(r.lv,r.l)+'</td></tr>';
   if(r.v){if(r.u[1])bad.push(x.n+' Utility: '+r.u[1]+' brownout(s)');if(r.u[2])bad.push(x.n+' Utility: '+r.u[2]+' watchdog reset(s)');if(r.u[3])bad.push(x.n+' Utility: '+r.u[3]+' crash(es)')}
   if(r.lowb>0)bad.push(x.n+': '+r.lowb+' low-battery shutdown(s) - the unit switched itself off because the battery was low');
   if(r.lv){if(r.l[1])bad.push(x.n+' LTC: '+r.l[1]+' brownout(s)');if(r.l[2])bad.push(x.n+' LTC: '+r.l[2]+' watchdog reset(s)');if(r.l[3])bad.push(x.n+' LTC: '+r.l[3]+' crash(es)')}}
  $('rstt').innerHTML=h;
  $('rsts').innerHTML=bad.length?'<span class="err">&#9888; '+bad.join(' &middot; ')+'</span>':'<span class="ok">No brownout, watchdog or crash reset recorded on any unit.</span>';
  if(m.crash)$('rsts').innerHTML+='<div style="margin-top:8px;word-break:break-all;color:#ffb0b0">Last crash on the master Utility (send this line for analysis): '+m.crash+'</div>'}
 function dg(name,d){if(!d||!d.valid)return '<tr><td>'+name+'</td><td colspan="4" class="muted">not available (older firmware)</td></tr>';
  const ppm=d.up>0?(d.lost/(48000*d.up)*1e6):0;const h=(d.up/3600).toFixed(1);const cls=d.lost==0?'ok':(Math.abs(ppm)>0.5?'err':'warn');
  let ex='';if(d.ew>0)ex='<tr><td></td><td colspan="4" class="muted">wrong-pulse windows: '+d.ew+(d.ew>=255?'+':'')+', first at '+(d.ef>=254?'&ge;254':d.ef)+' s, last at '+(d.el>=254?'&ge;254':d.el)+' s after start/Sync</td></tr>';
  return '<tr><td>'+name+'</td><td class="'+cls+'">'+d.lost+'</td><td class="'+cls+'">'+ppm.toFixed(2)+'</td><td>'+d.gap+' &micro;s</td><td>'+h+' h</td></tr>'+ex}
 if(m.diag&&m.diag.events){const ev=m.diag.events.slice().sort((a,b)=>b.ms-a.ms).slice(0,12);
  $('ev').innerHTML=ev.length?ev.map(e=>{const ago=Math.max(0,m.diag.up-e.ms/1000);return '<tr><td>'+(ago<120?ago.toFixed(0)+' s':(ago/60).toFixed(1)+' min')+' ago <span class="muted">(LTC '+(e.ms/60000).toFixed(1)+' min)</span></td><td>'+e.lost+'</td><td>'+(e.gap>=65535?'&gt;10 ms (clock stopped/blocked)':e.gap+' &micro;s')+'</td></tr>'}).join(''):'<tr><td colspan="3" class="muted">no events</td></tr>'}
 let c=dg('Master LTC',m.diag);for(const p of st.pairs)c+=dg((p.name||p.mac)+' LTC',p.diag);$('ct').innerHTML=c}catch(e){}}
// Poll only while this tab is visible: a hidden tab must not keep the master busy while another
// page (e.g. the 100 KB main page) is loading.
async function sys(){try{const r=await fetch('/sysinfo');const s=await r.json();const kb=n=>(n/1024).toFixed(0)+' KB';
 $('sys').innerHTML='Master memory: free '+kb(s.free)+' (lowest '+kb(s.minFree)+', largest block '+kb(s.maxBlock)+') &middot; heap check '+(s.heapOk?'<span class="ok">OK</span>':'<span class="err">FAILED</span>')+' &middot; spare stack: loop '+s.loopStack+' B, web '+s.webStack+' B &middot; up '+(s.up/60).toFixed(0)+' min &middot; auto-sync: '+(s.autoSync>0?'<span class="warn">every '+s.autoSync+' min</span>':'<span class="ok">off</span>')}catch(e){}}
setInterval(()=>{if(!document.hidden)poll()},1000);setInterval(()=>{if(!document.hidden)sys()},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();sys()}});poll();sys();
</script><div id="pageEnd" style="display:none"></div></body></html>)=====";
