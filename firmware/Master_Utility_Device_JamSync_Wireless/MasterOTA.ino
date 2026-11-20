// ==================== MASTER UPDATE: Master Utility + Master LTC, always together ====================
//
// From the web page (http://192.168.4.1/client-update), both master images are uploaded first and
// stored raw (identity-tag checked, see ClientOTA.ino): master utility -> slot type 4, master LTC ->
// slot type 5. Then, ALWAYS as a pair:
//   1. stop the Si5351 sample clock (so even an LTC firmware with a flash-unsafe ISR cannot crash)
//   2. MD5 both stored images
//   3. stream the LTC image to the LTC chip over SPI (CMD_FW_BEGIN / CMD_FW_DATA / CMD_FW_END).
//      The LTC accepts only the next expected offset, checks a CRC16 per packet, checks its own
//      identity tag and the MD5 before committing, then reboots into the new firmware.
//   4. ONLY if the LTC succeeded: install the utility image on this chip (MD5-checked) and reboot.
// If step 3 fails, nothing is installed anywhere: both chips keep the same (old) version.
// If step 4 fails (unlikely - the image is local and verified), the LTC is new and the utility old.
// That combination still works: the update fields live in spare bytes of the SPI status and the
// commands are only used when the LTC announces support (fwMagic), so mixed versions talk as before.
// The page shows both versions and warns when they differ; pressing Update again fixes it.
//
// Every step runs in short slices from loop(), so the web server, ESP-NOW and the watchdog keep going.

#include <MD5Builder.h>

#define FW_STATE_IDLE      0
#define FW_STATE_RECEIVING 1
#define FW_STATE_DONE      2
#define FW_STATE_ERROR     3
#define FW_MAGIC           0x5A
#define FW_CHUNK           56     // payload bytes per SPI packet
#define MFW_SPI_HZ         1000000   // SPI clock for update packets (the link's proven speed)
#define MFW_GAP_US         300       // pause between update packets

static_assert(offsetof(SPI_Command, command) == 6, "SPI firmware packets expect the command byte at offset 6");

enum MfwStep : uint8_t { MF_IDLE, MF_MD5, MF_BEGIN, MF_SEND, MF_END, MF_VERIFY, MF_WAIT_LTC, MF_SELF, MF_RESTART };

static volatile bool mfwStartReq = false;
static uint8_t mfwStep = MF_IDLE;
static LockedText mfwMsg;   // see LockedText (ClientOTA.ino): shared with the web handlers
static uint8_t mfwPct = 0;
static uint8_t mfwAttempt = 0;
static unsigned long mfwMsgAt = 0;   // when mfwMsg was set (stale "could not start" messages expire)
static uint32_t mfwLtcSize = 0, mfwUtilSize = 0;
static uint32_t mfwPos = 0;              // MD5 / self-install position
static uint8_t mfwMd5Phase = 0;          // 0 = LTC image, 1 = utility image
static uint32_t mfwNext = 0, mfwAcked = 0;
static uint8_t mfwMd5Ltc[16], mfwMd5Util[16];
static MD5Builder mfwMd5;
static unsigned long mfwStepT0 = 0, mfwLastProgress = 0, mfwRestartAt = 0, mfwLastEnd = 0;
// last SPI response, captured right after each transfer
static uint32_t mfwRespExpected = 0;
static uint8_t mfwRespState = 0, mfwRespErr = 0;
static uint32_t mfwRespUptime = 0;     // LTC uptime from the same reply (detects an LTC restart)
static bool mfwRespUptimeOk = false;
static uint32_t mfwLtcUptimeAtStart = 0;
static uint8_t mfwOdd = 0;             // consecutive valid replies with an unexpected state
// Build fingerprints (first 8 bytes of the ELF SHA-256 in the image's app description). The utility is
// installed ONLY after the LTC verifiably runs the new LTC build - status replies alone are not trusted.
static uint8_t mfwRespRunId[8];
static uint8_t mfwNewLtcId[8], mfwOldLtcId[8];
static bool mfwOldLtcIdKnown = false;
static bool mfwLtcAlreadyNew = false;
static bool mfwLtcFailed = false;       // LTC transfer failed: verify anyway (it may still have switched)
static String mfwLtcFailWhy;
static uint8_t mfwDoneHits = 0, mfwNewHits = 0, mfwOldHits = 0;
static unsigned long mfwLastPoll = 0;

bool mfwBusy() { return mfwStep != MF_IDLE || mfwStartReq; }

static uint16_t mfwCrc16(const uint8_t* d, size_t n, uint16_t c = 0xFFFF) {
    while (n--) {
        c ^= (uint16_t)(*d++) << 8;
        for (int i = 0; i < 8; i++) c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
    }
    return c;
}

// One SPI transfer carrying a firmware command. Returns true if the response came from an LTC that
// speaks the update protocol (a missed/garbled transfer returns false and is simply retried).
static bool mfwXfer(uint8_t cmd, uint32_t val, const uint8_t* payload, uint8_t len) {
    SpiCmdGuard spiGuard;
    memset(&spiCmd, 0, sizeof(spiCmd));
    uint8_t* raw = (uint8_t*)&spiCmd;
    raw[0] = val & 0xFF; raw[1] = (val >> 8) & 0xFF; raw[2] = (val >> 16) & 0xFF; raw[3] = (val >> 24) & 0xFF;
    raw[6] = cmd;
    raw[7] = len;
    if (len) memcpy(raw + 8, payload, len);
    uint16_t c = mfwCrc16(raw + 6, 58, mfwCrc16(raw, 4));   // over [0..3] and [6..63] - command byte included
    raw[64] = c & 0xFF;
    raw[65] = c >> 8;
    // Update packets only: 4 MHz clock and a short chip-select setup (the LTC re-arms its SPI slave
    // immediately after firmware packets). The CRC protects every packet; a bad one is re-sent.
    memset(&spiStatus, 0, sizeof(spiStatus));
    uint32_t savedDiv = hspi.getClockDivider();
    hspi.setFrequency(MFW_SPI_HZ);
    portENTER_CRITICAL(&spiMux);
    digitalWrite(SPI_CS, LOW);
    delayMicroseconds(20);
    hspi.transferBytes((uint8_t*)&spiCmd, (uint8_t*)&spiStatus, SPI_PACKET_SIZE);
    digitalWrite(SPI_CS, HIGH);
    portEXIT_CRITICAL(&spiMux);
    hspi.setClockDivider(savedDiv);
    mfwRespExpected = spiStatus.fwExpected;
    mfwRespState = spiStatus.fwState;
    mfwRespErr = spiStatus.fwError;
    mfwRespUptimeOk = (spiStatus.diagMagic == 0xD1);
    mfwRespUptime = spiStatus.diagUptimeS;
    memcpy(mfwRespRunId, spiStatus.fwRunId, sizeof(mfwRespRunId));
    return spiStatus.fwMagic == FW_MAGIC;
}

static void mfwSaveResult(const String& msg) {
    Preferences p;
    p.begin("fwupd", false);
    p.putString("last", msg);
    p.end();
}

static String mfwLtcErrText(uint8_t e) {
    static const char* const updErr[] = { "ok", "write", "erase", "read", "space", "size", "stream", "MD5",
                                          "magic byte", "activate", "no partition", "bad argument", "abort", "decrypt" };
    if (e >= 20) {
        uint8_t u = e - 20;
        return String("LTC install failed (Update error ") + u + (u < 14 ? String(" = ") + updErr[u] : String("")) + ")";
    }
    switch (e) {
        case 1: return "LTC could not start the update";
        case 2: return "LTC flash write failed";
        case 3: return "LTC image check failed (MD5/size)";
        case 4: return "LTC timed out waiting for data";
        case 5: return "not an LTC firmware (ID tag missing)";
        case 6: return "data arrived corrupted (MD5 of received bytes wrong)";
        default: return String("LTC error ") + e;
    }
}

// Failure before the LTC committed anything: abort on the LTC, bring the clock back, restart so the
// LTC is re-synced from the RTC. Both chips keep their current (matching) firmware.
static void mfwFail(const String& why) {
    for (int i = 0; i < 3; i++) { mfwXfer(CMD_FW_ABORT, 0, nullptr, 0); delay(5); }
    si5351.output_enable(SI5351_CLK0, 1);
    mfwMsg = "FAILED - nothing was changed: " + why;
    mfwSaveResult(mfwMsg.get());
    mfwStep = MF_RESTART;
    mfwRestartAt = millis() + 4000;
}

static void mfwEnterVerify() {
    mfwMsg = "Checking which firmware the LTC is running";
    mfwNewHits = mfwOldHits = 0;
    mfwStep = MF_VERIFY;
    mfwStepT0 = millis();
}

// LTC-side failure during the transfer: retry the whole LTC transfer once, then give up.
static void mfwRetryOrFail(const String& why) {
    if (mfwAttempt < 2) {
        mfwAttempt++;
        for (int i = 0; i < 3; i++) { mfwXfer(CMD_FW_ABORT, 0, nullptr, 0); delay(5); }
        mfwMsg = "Retrying the LTC transfer (" + why + ")";
        mfwStep = MF_BEGIN;
        mfwStepT0 = millis();
        return;
    }
    for (int i = 0; i < 3; i++) { mfwXfer(CMD_FW_ABORT, 0, nullptr, 0); delay(5); }
    mfwLtcFailed = true;
    mfwLtcFailWhy = why + " (after a retry)";
    mfwEnterVerify();
}

static const char* mfwCheckStart() {
    if (mfwStep != MF_IDLE) return "a master update is already running";
    if (cotaRunning) return "a client update is running";
    if (otaMode) return "the master is in OTA mode";
    if (ltcSystemLocked) return "the LTC is locked - unlock it first";
    if (!cotaSlots[2].valid || !cotaSlots[3].valid) return "upload BOTH the master utility and the master LTC firmware";
    // The utility image is installed into the next OTA slot: it must not be stored in that same slot
    // (cannot happen with the default partition layout, but never overwrite the source being read).
    if (cotaSlots[2].part == esp_ota_get_next_update_partition(NULL))
        return "this flash layout cannot hold the master utility image - use the OTA button";
    if (!ltcFwSupported)
        return "the master LTC firmware is too old for web updates - update the LTC once with the OTA button";
    return nullptr;
}

// ---------------------------------------------------------------------------------------------
void mfwLoop() {
    if (mfwStartReq) {
        mfwStartReq = false;
        const char* err = mfwCheckStart();
        if (err) { mfwMsg = String("Could not start: ") + err; mfwMsgAt = millis(); return; }
        mfwLtcSize = cotaSlots[3].size;
        mfwUtilSize = cotaSlots[2].size;
        {
            uint8_t hdr[0xB8];
            if (esp_partition_read(cotaSlots[3].part, 0, hdr, sizeof(hdr)) != ESP_OK ||
                hdr[0x20] != 0x32 || hdr[0x21] != 0x54 || hdr[0x22] != 0xCD || hdr[0x23] != 0xAB) {
                mfwMsg = "Could not start: the LTC file has no build fingerprint (app description)";
                mfwMsgAt = millis();
                return;
            }
            memcpy(mfwNewLtcId, hdr + 0xB0, 8);   // ELF SHA-256 of the new LTC build
        }
        mfwOldLtcIdKnown = false;
        for (int i = 0; i < 20 && !mfwOldLtcIdKnown; i++) {
            if (mfwXfer(CMD_POLL_STATUS, 0, nullptr, 0)) { memcpy(mfwOldLtcId, mfwRespRunId, 8); mfwOldLtcIdKnown = true; }
            else delay(5);
        }
        // Repair case: the LTC already runs this build (e.g. an earlier update got the LTC done but
        // not the utility) -> skip the LTC transfer and only install the utility.
        mfwLtcAlreadyNew = mfwOldLtcIdKnown && memcmp(mfwOldLtcId, mfwNewLtcId, 8) == 0;
        mfwLtcFailed = false;
        mfwLtcFailWhy = "";
        mfwDoneHits = 0;
        // 1. no sample clock -> no LTC interrupts while the LTC chip writes its flash
        si5351.output_enable(SI5351_CLK0, 0);
        si5351.output_enable(SI5351_CLK1, 0);
        mfwMd5.begin();
        mfwMd5Phase = 0;
        mfwPos = 0;
        mfwPct = 0;
        mfwMsg = "Checking the files";
        mfwAttempt = 1;
        mfwStep = MF_MD5;
        mfwStepT0 = millis();
        return;
    }
    unsigned long now = millis();

    switch (mfwStep) {
    case MF_IDLE:
        // Messages expire (cleared here in loop(), never from a web handler):
        // "Could not start ..." after 15 s, anything else (the last update's result) after 10 min.
        if (!mfwStartReq && !mfwMsg.empty()) {
            unsigned long age = now - mfwMsgAt;
            if (age > 600000UL || (age > 15000UL && mfwMsg.startsWith("Could not start"))) mfwMsg = "";
        }
        return;

    case MF_MD5: {   // 2. MD5 of both stored images, 32 KB per loop pass
        const CotaSlot& sl = cotaSlots[mfwMd5Phase == 0 ? 3 : 2];
        uint32_t total = (mfwMd5Phase == 0) ? mfwLtcSize : mfwUtilSize;
        uint8_t buf[1024];
        for (uint32_t budget = 32768; budget && mfwPos < total;) {
            uint32_t n = total - mfwPos < sizeof(buf) ? total - mfwPos : sizeof(buf);
            if (esp_partition_read(sl.part, mfwPos, buf, n) != ESP_OK) { mfwFail("could not read the stored file"); return; }
            mfwMd5.add(buf, n);
            mfwPos += n;
            budget -= n < budget ? n : budget;
        }
        if (mfwPos >= total) {
            mfwMd5.calculate();
            mfwMd5.getBytes(mfwMd5Phase == 0 ? mfwMd5Ltc : mfwMd5Util);
            if (mfwMd5Phase == 0) {
                mfwMd5Phase = 1; mfwPos = 0; mfwMd5.begin();
            } else {
                if (mfwLtcAlreadyNew) {   // LTC already runs the new build: only the utility is missing
                    mfwMsg = "LTC already runs this firmware - installing the utility";
                    mfwStep = MF_WAIT_LTC;
                    mfwStepT0 = now - 3000;
                } else {
                    mfwMsg = "Starting the LTC update";
                    mfwStep = MF_BEGIN;
                    mfwStepT0 = now;
                }
            }
        }
        return;
    }

    case MF_BEGIN:   // 3a. announce size + MD5, wait until the LTC is receiving
        if (mfwXfer(CMD_FW_BEGIN, mfwLtcSize, mfwMd5Ltc, 16)) {
            delay(3);
            if (mfwXfer(CMD_POLL_STATUS, 0, nullptr, 0)) {
                if (mfwRespState == FW_STATE_RECEIVING && mfwRespExpected == 0) {
                    mfwLtcUptimeAtStart = mfwRespUptimeOk ? mfwRespUptime : 0;
                    mfwOdd = 0;
                    mfwNext = 0;
                    mfwAcked = 0;
                    mfwLastProgress = now;
                    mfwMsg = "Sending the LTC firmware";
                    mfwStep = MF_SEND;
                    return;
                }
                if (mfwRespState == FW_STATE_ERROR) { mfwRetryOrFail(mfwLtcErrText(mfwRespErr)); return; }
            }
        }
        if (now - mfwStepT0 > 8000) mfwRetryOrFail("the LTC did not answer");
        else delay(20);
        return;

    case MF_SEND: {   // 3b. pipelined data packets, ~80 ms per loop pass
        uint8_t buf[FW_CHUNK];
        unsigned long t = millis();
        while (millis() - t < 80) {
            if (mfwNext >= mfwLtcSize) { mfwStep = MF_END; mfwStepT0 = millis(); mfwLastEnd = 0; mfwOdd = 0; break; }
            uint8_t len = (mfwLtcSize - mfwNext) < FW_CHUNK ? (uint8_t)(mfwLtcSize - mfwNext) : FW_CHUNK;
            if (esp_partition_read(cotaSlots[3].part, mfwNext, buf, len) != ESP_OK) { mfwFail("could not read the stored LTC file"); return; }
            uint32_t sent = mfwNext;
            if (mfwXfer(CMD_FW_DATA, sent, buf, len)) {
                if (mfwRespState != FW_STATE_RECEIVING) {
                    // Status replies carry no checksum: only act if the LTC says so several times in a row
                    if (++mfwOdd >= 8) {
                        if (mfwRespState == FW_STATE_DONE) { mfwEnterVerify(); return; }
                        if (mfwRespUptimeOk && mfwRespUptime + 2 < mfwLtcUptimeAtStart)
                            mfwRetryOrFail("the LTC restarted during the update");
                        else if (mfwRespState == FW_STATE_ERROR)
                            mfwRetryOrFail(mfwLtcErrText(mfwRespErr));
                        else
                            mfwRetryOrFail("the LTC left update mode");
                        return;
                    }
                    delayMicroseconds(MFW_GAP_US);
                    continue;   // ignore this reply, resend the same packet
                }
                mfwOdd = 0;
                // The response shows the LTC state BEFORE this packet: where it wants to continue.
                uint32_t e = mfwRespExpected;
                mfwNext = (e == sent) ? sent + len : e;
                if (e > mfwAcked) { mfwAcked = e; mfwLastProgress = millis(); }
            }
            // invalid response = transfer missed (LTC busy writing flash): resend the same packet
            delayMicroseconds(MFW_GAP_US);
            if (millis() - mfwLastProgress > 15000) { mfwRetryOrFail("the LTC stopped accepting data"); return; }
        }
        mfwPct = (uint8_t)((uint64_t)mfwAcked * 80 / (mfwLtcSize ? mfwLtcSize : 1));
        return;
    }

    case MF_END:   // 3c. commit on the LTC (it verifies tag + MD5), resend END until it answers
        if (now - mfwLastEnd > 600) {
            mfwLastEnd = now;
            if (mfwXfer(CMD_FW_END, mfwLtcSize, nullptr, 0) && mfwRespState == FW_STATE_RECEIVING &&
                mfwRespExpected < mfwLtcSize) {
                mfwNext = mfwRespExpected;   // a packet went missing: send the rest again
                mfwStep = MF_SEND;
                return;
            }
        }
        delay(30);
        if (mfwXfer(CMD_POLL_STATUS, 0, nullptr, 0)) {
            if (mfwRespState == FW_STATE_DONE) {
                if (++mfwDoneHits >= 3) {   // a single garbled reply must not count as DONE
                    mfwPct = 85;
                    mfwEnterVerify();
                    return;
                }
            } else {
                mfwDoneHits = 0;
            }
            if (mfwRespState == FW_STATE_ERROR) {
                if (++mfwOdd >= 5) { mfwRetryOrFail(mfwLtcErrText(mfwRespErr)); return; }
            } else {
                mfwOdd = 0;
            }
        }
        if (now - mfwStepT0 > 20000) mfwRetryOrFail("the LTC did not confirm the update");
        return;

    case MF_VERIFY:     // the LTC reboots ~1.5 s after DONE; read the fingerprint it is RUNNING
        if (now - mfwLastPoll < 150) return;
        mfwLastPoll = now;
        if (mfwXfer(CMD_POLL_STATUS, 0, nullptr, 0)) {
            if (memcmp(mfwRespRunId, mfwNewLtcId, 8) == 0) {
                mfwOldHits = 0;
                if (++mfwNewHits >= 3) {   // the LTC verifiably runs the NEW build -> complete the pair
                    mfwMsg = mfwLtcFailed ? "LTC is on the new firmware after all - installing the utility"
                                          : "LTC verified on the new firmware - installing the utility";
                    mfwStep = MF_WAIT_LTC;
                    mfwStepT0 = now - 3000;
                    return;
                }
            } else if (mfwOldLtcIdKnown && memcmp(mfwRespRunId, mfwOldLtcId, 8) == 0 && now - mfwStepT0 > 6000) {
                mfwNewHits = 0;
                if (++mfwOldHits >= 3) {   // still the old build well after any reboot -> nothing changed
                    mfwFail(mfwLtcFailed ? mfwLtcFailWhy : String("the LTC did not start the new firmware"));
                    return;
                }
            } else {
                mfwNewHits = mfwOldHits = 0;
            }
        }
        if (now - mfwStepT0 > 25000) {
            mfwMsg = "FAILED: could not confirm which firmware the LTC runs - the utility was NOT changed. "
                     "Check the versions table and press Update again.";
            mfwSaveResult(mfwMsg.get());
            si5351.output_enable(SI5351_CLK0, 1);
            mfwStep = MF_RESTART;
            mfwRestartAt = now + 4000;
        }
        return;

    case MF_WAIT_LTC:   // only reached once the LTC verifiably runs the new build
        if (now - mfwStepT0 < 3000) return;
        // 4. install the utility image on this chip (only reached when the LTC succeeded)
        {
            char hex[33];
            for (int i = 0; i < 16; i++) sprintf(hex + 2 * i, "%02x", mfwMd5Util[i]);
            if (!Update.begin(mfwUtilSize) || !Update.setMD5(hex)) {
                mfwMsg = String("LTC updated, but the utility install could not start (") + Update.errorString() +
                         ") - versions differ, press Update again";
                mfwSaveResult(mfwMsg.get());
                Update.abort();
                mfwStep = MF_RESTART;
                mfwRestartAt = now + 4000;
                return;
            }
        }
        mfwPos = 0;
        mfwMsg = "Installing the utility firmware";
        mfwStep = MF_SELF;
        return;

    case MF_SELF: {   // 16 KB per loop pass
        uint8_t buf[1024];
        for (int k = 0; k < 16 && mfwPos < mfwUtilSize; k++) {
            uint32_t n = mfwUtilSize - mfwPos < sizeof(buf) ? mfwUtilSize - mfwPos : sizeof(buf);
            if (esp_partition_read(cotaSlots[2].part, mfwPos, buf, n) != ESP_OK || Update.write(buf, n) != n) {
                Update.abort();
                mfwMsg = "LTC updated, but the utility install failed - versions differ, press Update again";
                mfwSaveResult(mfwMsg.get());
                mfwStep = MF_RESTART;
                mfwRestartAt = millis() + 4000;
                return;
            }
            mfwPos += n;
        }
        mfwPct = 85 + (uint8_t)((uint64_t)mfwPos * 15 / (mfwUtilSize ? mfwUtilSize : 1));
        if (mfwPos >= mfwUtilSize) {
            if (Update.end(true)) {
                mfwPct = 100;
                mfwMsg = "Master updated: utility + LTC installed together";
            } else {
                mfwMsg = String("LTC updated, but the utility image check failed (") + Update.errorString() +
                         ") - versions differ, press Update again";
            }
            mfwSaveResult(mfwMsg.get());
            mfwStep = MF_RESTART;
            mfwRestartAt = millis() + 2500;
        }
        return;
    }

    case MF_RESTART:
        if ((long)(now - mfwRestartAt) >= 0) ESP.restart();
        return;
    }
}

// ---------------------------------------------------------------------------------------------
String mfwStatusJsonPart() {
    char buf[200];
    char msg[160];
    mfwMsg.copyTo(msg, sizeof(msg));
    cotaJsonClean(msg);
    snprintf(buf, sizeof(buf), "{\"busy\":%s,\"pct\":%u,\"uv\":\"%u.%u.%u\",\"lv\":\"%u.%u.%u\",\"ltcWeb\":%s,",
             mfwBusy() ? "true" : "false", mfwPct,
             FIRMWARE_VERSION_MAJOR, FIRMWARE_VERSION_MINOR, FIRMWARE_VERSION_PATCH,
             ltcVerMajor, ltcVerMinor, ltcVerPatch, ltcFwSupported ? "true" : "false");
    String j = buf;
    j += "\"msg\":\"" + String(msg) + "\"";
    j += ",\"diag\":{\"valid\":" + String(ltcDiagValid ? "true" : "false") + ",\"lost\":" + String(ltcDiagNetLost) +
         ",\"gap\":" + String(ltcDiagMaxGapUs) + ",\"up\":" + String(ltcDiagUptimeS) + ",\"events\":[";
    for (uint8_t i = 0; i < mltcEvCount; i++) {
        if (i) j += ",";
        j += "{\"ms\":" + String(mltcEvMs[i]) + ",\"lost\":" + String(mltcEvLost[i]) + ",\"gap\":" + String(mltcEvGap[i]) + "}";
    }
    j += "]}";
    j += rstJson(true, rstSelf, ltcRst, ltcRstValid, (unsigned long)(millis() / 1000), rstLowBatt);
    j += ",\"crash\":\"" + String(crashTxt) + "\"";
    const char* keys[2] = { "utility", "ltc" };
    for (int s = 0; s < 2; s++) {
        CotaSlot& sl = cotaSlots[2 + s];
        char nm[48]; strncpy(nm, sl.name, sizeof(nm) - 1); nm[sizeof(nm) - 1] = 0; cotaJsonClean(nm);
        j += ",\"" + String(keys[s]) + "\":{\"valid\":" + String(sl.valid ? "true" : "false");
        j += ",\"size\":" + String(sl.size) + ",\"name\":\"" + String(nm) + "\"}";
    }
    j += "}";
    return j;
}

void mfwSetupRoutes() {
    server.on("/master_fw/start", HTTP_POST, [](AsyncWebServerRequest* req) {
        const char* err = mfwStartReq ? "a master update is already running" : mfwCheckStart();
        if (err) { req->send(400, "application/json", "{\"ok\":false,\"error\":\"" + String(err) + "\"}"); return; }
        mfwMsg = "Starting";
        mfwMsgAt = millis();
        mfwStartReq = true;   // loop() does the work
        req->send(200, "application/json", "{\"ok\":true}");
    });
}

// Called once from setup(): show the result of the last master update on the page.
void mfwBegin() {
    Preferences p;
    p.begin("fwupd", true);
    String last = p.getString("last", "");
    p.end();
    if (last.length()) {
        // Show the result of the update that just restarted the master - for this boot only
        // (10 minutes, see MF_IDLE), so it does not come back after every later restart.
        mfwMsg = last;
        mfwMsgAt = millis();
        Preferences w;
        w.begin("fwupd", false);
        w.remove("last");
        w.end();
    }
}
