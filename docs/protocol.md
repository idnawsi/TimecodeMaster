# Protocols

Three interfaces are defined in the firmware: the SPI link between the two chips in a unit, the ESP-NOW link between units, and the HTTP / WebSocket interface of the master. None of them is a published standard, and each is implemented on both ends inside this repository, so the source is the reference. Structure definitions are duplicated in each sketch; keep them identical when changing one.

## 1. SPI link (Utility to LTC device)

| Item | Value |
|---|---|
| Master | Utility device, `SPIClass(HSPI)`, `SPI_MODE1` |
| Slave | LTC device, ESP-IDF `spi_slave` on `SPI2_HOST`, mode 1, queue depth 1 |
| Pins | SCLK 14, MISO 12, MOSI 13, CS 15 |
| Packet | 68 bytes in both directions per transaction (master images); 64 bytes in the client images |
| Clock | 1 MHz |

Every transaction exchanges one command packet (master to slave) and one status packet (slave to master). The slave prepares its reply before the transfer, so a reply reflects the state at the end of the previous transaction. For live timecode the master sends `CMD_POLL_STATUS_LIVE` twice with a 500 us gap: the first makes the slave prepare the live data, the second returns it.

Both structures are packed and checked at compile time against the packet size.

### Command packet (`SPI_Command`, 68 bytes)

| Offset | Field | Size | Notes |
|---|---|---|---|
| 0 | sampleRate | 4 | 47952 or 48000 |
| 4 | fps_thousandths | 2 | 23976, 24000, 25000, 29970, 30000 |
| 6 | command | 1 | see below |
| 7 | hours | 1 | |
| 8 | minutes | 1 | |
| 9 | seconds | 1 | |
| 10 | frames | 1 | for `CMD_SET_LTC_OFFSET`, the signed offset |
| 11 | dropFrame | 1 | |
| 12 | running | 1 | |
| 13 | espNowConnected | 1 | for the OLED |
| 14 | syncIntervalMinutes | 1 | for the OLED |
| 15 | numClients | 1 | |
| 16 | syncedClients | 1 | |
| 17 | wifiSSID | 20 | |
| 37 | wifiPassword | 9 | 8 characters and NUL |
| 46 | otaSSID | 12 | |
| 58 | userBits | 8 | month, day, year, camera ID nibbles |
| 66 | padding | 2 | |

Commands:

| Value | Name | Purpose |
|---|---|---|
| 0 | CMD_POLL_STATUS | Return status |
| 1 | CMD_SYNC_TIMECODE | Set time, rate, drop-frame, run state |
| 2 | CMD_SET_RUNNING | Start or stop |
| 3 | CMD_UPDATE_ESPNOW_STATUS | Connection and client counts for the display |
| 4 | CMD_SET_FPS | Change frame rate |
| 5 | CMD_SEND_WIFI_INFO | SSID and password for the OLED |
| 6 | CMD_POLL_STATUS_LIVE | Status with sub-frame position |
| 7 | CMD_IMMEDIATE_SYNC | Atomic time set |
| 8 | CMD_LOCK_DEVICE | Lock or unlock |
| 9 | CMD_ENTER_OTA | Enter OTA mode |
| 10 to 13 | CMD_FW_BEGIN / DATA / END / ABORT | Firmware update of the LTC chip |
| 14 | CMD_CLEAR_RSTLOG | Clear the reset counters |
| 15 | CMD_SET_LTC_OFFSET | Output offset in frames |

### Status packet (`SPI_Status`, 68 bytes)

Fields, in order: `si5351InterruptCount` (4), `fps_thousandths` (2), hours, minutes, seconds, frames, dropFrame, running, synced, batteryLevel (0 to 4), batteryPresent, isLineLevel, jamSyncMode, systemLocked, syncAck, lowBatteryShutdown, bitIndex (0 to 79), bitSamplePos, firmware version (3 bytes), otaProgress, otaSSID (12), then the firmware-update state (`fwExpected` 4, `fwState`, `fwError`, `fwMagic` 0x5A), clock diagnostics (`diagNetLost` 4, `diagMaxGapUs` 2, `diagUptimeS` 4, `diagMagic` 0xD1), the running image's fingerprint (`fwRunId`, 8), and the reset log (`rstReason`, `rstBrown`, `rstWdt`, `rstPanic`).

### Firmware update of the master LTC chip

The Utility streams an LTC image in packets whose payload is up to 56 bytes. `[0..3]` is the offset (or the total size in `BEGIN`), `[6]` the command, `[7]` the payload length, `[8..63]` the payload and `[64..65]` a CRC16 over bytes 0 to 3 and 6 to 63. The slave accepts only the next expected offset, so a lost or repeated packet is resent without harm. Before commit, the whole image must contain this image's identity tag and match the MD5 announced in `BEGIN`. Otherwise nothing is committed.

## 2. ESP-NOW (master to clients)

All units use Wi-Fi channel 1. The master runs in `WIFI_AP_STA` mode and its access point is on channel 1 as well. Messages are unicast to paired peers; pairing requests are broadcast. The default PHY rate is 6 Mbps and can be set to 1 Mbps for range from the master's main page. TX power is fixed at 11 dBm.

### Message structure (`struct_message`)

| Field | Type | Notes |
|---|---|---|
| type | char[8] | Message type, see below |
| hours, minutes, seconds, frames | uint8 each | |
| fps_thousandths | uint16 | |
| dropFrame, running, synced | uint8 | |
| batteryLevel, batteryPresent | uint8 | |
| timestamp, masterTimestamp | unsigned long | `millis()` of the sender |
| mac | char[18] | |
| uptime | unsigned long | Client uptime in seconds; used to detect a reboot |
| jamSyncMode | uint8 | Sender's jam-sync state |
| sampleRate | uint32 | 47952 or 48000 |
| utility / ltc version | 3 x uint8 each | major, minor, patch |
| otaState | uint8 | 0 normal, 1 updating, 2 waiting, 3 error |
| deviceName | char[3] | Camera ID |
| userBits | uint8[8] | Date and camera ID |

The structure is not declared packed. Both ends are built with the same compiler and definition. The `type` field is 8 bytes and some names fill it without a terminating NUL, so comparisons use `strncmp` with the field size.

### Types

| Type | Direction | Meaning |
|---|---|---|
| `PAIR_REQ` | client to master (broadcast) | Unpaired client asks to join. Only handled in pairing mode |
| `PAIR_CONF`, `PAIR_OK` | master to client | Pairing accepted |
| `UNPAIR`, `UNPAIR_REQ` | either | End the pairing |
| `REG` | client to master | Registration on start-up |
| `HB` | client to master, every 5 s | Heartbeat with state, battery, versions, uptime, jam-sync state |
| `HB_ACK` | master to client | Heartbeat answer |
| `SYNC` | master to client | Time, frame rate, drop frame, run state, sample rate, user bits |
| `RUN` | master to client | Start or stop |
| `SET_NAME` | master to client | Set the camera ID |
| `ALIVE` | master to client, every 10 s | Keep-alive (not sent while power save is active) |
| `PSB` | master, broadcast | Power-save beacon |
| `WAKE` | client to master | Power save: the client radio is on |

A client in jam-sync SEARCHING, FREERUN, or in the lock state ignores `SYNC` and `RUN`. The master does not send `SYNC` to such a client.

### Binary packets

Control packets that are not a `struct_message` start with a byte in the range 0x10 to 0x1B:

| First byte | Direction | Meaning |
|---|---|---|
| 0x10 | master to client | OTA command: flags (utility, LTC), AP SSID, AP password, protocol version (67 bytes) |
| 0x11 | client to master | OTA command acknowledged |
| 0x12, 0x13, 0x14 | client to master | OTA progress, done, error (progress, state, 32-character status) |
| 0x15 | client to master | Reset log and radio status |
| 0x16 | master to client | Clear the reset log |
| 0x17 | master to client | Radio settings: TX index, rate, power save |
| 0x18, 0x19, 0x1B | both | Radio test probes and results |

### Client firmware update

1. The browser uploads the client images to the master. They are stored raw in the spare OTA partition and the SPIFFS partition, and checked for the identity tag.
2. The master sends the 0x10 command to the selected clients, one at a time by default or up to four in parallel, resending after 4 s up to three times.
3. A client joins the master's access point, downloads `/firmware?type=2` (Utility) and flashes it, reboots, then downloads `/firmware?type=3` (LTC) and passes it to its own LTC chip over SPI.
4. Progress comes back as 0x12, 0x13 or 0x14. A client is abandoned after 10 minutes.

## 3. HTTP and WebSocket (master Utility)

The master is an access point named `TimecodeMaster-XXXX` (XXXX from the chip ID), address 192.168.4.1, channel 1, up to 8 stations, WPA2 with an 8-character random password (hardware random number generator, generated once), stored in flash, and shown on the OLED when the Wi-Fi button is pressed. Up to 5 WebSocket clients are served at once.

The interface has no login beyond the Wi-Fi password, and several state-changing endpoints use GET. Treat the access point as the security boundary.

### HTTP endpoints

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | Main page |
| `/timecode`, `/getrtc`, `/getsync`, `/getsamplerate`, `/sysinfo`, `/clients` | GET | Current state as JSON or text |
| `/command?cmd=start` / `stop` / `syncall` / `syncfromrtc` | GET | Run control and sync |
| `/setfps?fps=...&dropframe=...` | GET | Frame rate |
| `/settimecode?hours=..&minutes=..&seconds=..&frames=..` | GET | Set time |
| `/setsync?interval=..` | GET | Automatic sync interval in minutes (0 = off) |
| `/setsamplerate?rate=..` | GET | Sample rate (47952 or 48000) |
| `/setrtc` | POST | Set the RTC (year, month, day, hour, minute, second) |
| `/set_cam_id`, `/get_cam_id`, `/set_client_name` | POST / GET | Camera ID and client names |
| `/startpairing`, `/stoppairing`, `/pairclient?mac=..`, `/unpairclient?mac=..` | GET | Pairing |
| `/ltcofs/set` | | LTC output offset per unit |
| `/radio/set`, `/radio/test`, `/radio/test/start` | | Radio settings and test |
| `/rstlog/clear` | | Clear reset counters and the stored crash dump |
| `/firmware`, `/update`, `/master_fw/start` | | Firmware page, Utility upload, master LTC update |
| `/client-update`, `/client_fw`, `/client_ota/start`, `/client_ota/status`, `/client_ota/progress`, `/client_ota/cancel` | | Client update |

### WebSocket `/ws`

The page keeps a WebSocket open for live updates. Server messages are JSON (timecode, RTC, client table, battery, diagnostics). Client messages are text commands with a prefix: `CMD:`, `SET_FPS:`, `SET_TIMECODE:`, `SET_RTC:`, `SET_RTC_AT:`, `SET_SYNC:`, `PAIRING:`. Each browser has a bounded queue of four pending messages so that a sleeping phone cannot exhaust memory.

## 4. Local OTA mode

Holding the OTA button for 3 s on a Utility device starts a local access point (`TCM-U-XXXX` for the Utility, `TCM-L-XXXX` for its LTC chip) with an upload page. The password is defined in each sketch (`OTA_PASSWORD`). Change it before deploying outside a lab. The mode ends after 5 minutes.
