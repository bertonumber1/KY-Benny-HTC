# VR-N7600 — FULL CAPABILITY MATRIX ("button for button")

Everything the radio + vendor HT app can do, mapped to protocol commands and
to this web UI. Sources: decompiled vendor app (`sources/com/benshikj/`,
R.java UI surface), our live captures (FINDINGS.txt), and cross-checks against
the open-source benlink + HTCommander implementations of the same protocol.

Legend: ✅ in web UI · 🔧 backend only (API, no UI yet) · 🕳 not implemented ·
🔬 needs live-radio RE (structure unknown)

## 1. COMMAND SET (GAIA group 2 — all 77 commands)

| id | command | what it does | status |
|----|---------|--------------|--------|
| 1  | GET_DEV_ID | device id blob | 🔧 via debug console |
| 2/3| SET/GET_REG_TIMES | registration counters | 🔧 debug |
| 4  | GET_DEV_INFO | model caps: fw/hw ver, channel count, VFO/DMR/NOAA/GMRS flags | ✅ Radio info card |
| 5  | READ_STATUS | battery level/voltage/percent (StatusType 1-4) | ✅ battery in LCD |
| 6/7/8 | REGISTER/CANCEL/GET_NOTIFICATION | subscribe to events | ✅ auto on connect |
| 9  | EVENT_NOTIFICATION | radio→us push (see §3) | ✅ |
| 10/11/12 | READ/WRITE/STORE_SETTINGS | main 52-field settings block | ✅ Settings tab (43 fields) |
| 13/14 | READ/WRITE_RF_CH | channel memories (24B + DMR ext) | ✅ Channels tab |
| 15/16 | GET/SET_IN_SCAN | scan membership shortcut | ✅ via channel flag |
| 17 | SET_REMOTE_DEVICE_ADDR | BT peer addr | 🕳 (pairing plumbing) |
| 18/19 | GET/DEL_TRUSTED_DEVICE | BT bond list | 🕳 |
| 20 | GET_HT_STATUS | power/TX/RX/squelch/scan/ch/GPS/**RSSI 0-15**/region | ✅ LCD + S-meter |
| 21 | SET_HT_ON_OFF | radio power on/off | ✅ Dashboard |
| 22/23 | GET/SET_VOLUME | volume 0-15 | ✅ Dashboard |
| 24 | RADIO_GET_STATUS | FM broadcast radio: on/seeking + freq (u16×10 kHz at payload[3..4], flags 0x80=on 0x10=seek at payload[1]) | 🔧 NEW: /api/fm |
| 25 | RADIO_SET_MODE | FM broadcast on/off | 🔧 NEW |
| 26/27 | RADIO_SEEK_UP/DOWN | FM broadcast seek | 🔧 NEW |
| 28 | RADIO_SET_FREQ | FM broadcast tune | 🔧 NEW |
| 29/30 | READ/WRITE_ADVANCED_SETTINGS | 123B calibration/adv block (shared 21B freq-range header + AGC step tables) | 🔬 raw captured, layout WIP |
| 31 | HT_SEND_DATA | TNC TX (fragmented, §4) | ✅ APRS send |
| 32 | SET_POSITION | push phone GPS to radio | 🕳 planned (share browser loc) |
| 33/34 | READ/WRITE_BSS_SETTINGS | APRS/BSS beacon config | ✅ APRS tab |
| 35 | FREQ_MODE_SET_PAR | VFO (frequency) mode set freq/mod | 🔬 payload untested |
| 36 | FREQ_MODE_GET_STATUS | VFO mode status: payload[1..4] u32be = top 2 bits modulation, low 30 bits freq Hz | 🔧 NEW |
| 37/38 | READ/WRITE_RDA1846S_AGC | RF chip AGC (5B) | 🔬 raw captured |
| 39 | READ_FREQ_RANGE | band limits (21B, = header of 29/63) | 🔧 debug |
| 40 | WRITE_DE_EMPH_COEFFS | audio de-emphasis coefficients (app engineering menu) | 🕳 |
| 41 | STOP_RINGING | stop alert tone | 🔧 NEW |
| 42 | SET_TX_TIME_LIMIT | TOT override | ✅ via settings field |
| 43 | SET_IS_DIGITAL_SIGNAL | digital-signal flag | 🕳 |
| 44 | SET_HL | TX power high/low toggle | 🔧 debug |
| 45/54 | SET/GET_DID | device name (ASCII in blob) | ✅ name shown |
| 46/47 | SET/GET_IBA | in-band announce? (app: "iba" calib field) | 🕳 |
| 48 | SET_TRUSTED_DEVICE_NAME | rename bond | 🕳 |
| 49/50 | SET/GET_VOC | vocoder param (DMR) | 🕳 |
| 51 | SET_PHONE_STATUS | tell radio the phone state | 🕳 |
| 52 | READ_RF_STATUS | live per-VFO RF levels (31B; vendor RF-status screen shows rssi/rssi2/rssi_r/rssi_rr + noise×4) | 🔬 polled raw, decode next live session |
| 53 | PLAY_TONE | play tone on radio speaker | 🔧 debug |
| 55/56/75 | GET_PF / SET_PF / GET_PF_ACTIONS | programmable buttons — **fully decoded, §2** | 🔧 NEW: /api/pf |
| 57 | RX_DATA | (reply channel for data) | ✅ via events |
| 58/59/60/73 | WRITE_REGION_CH / WRITE_REGION_NAME / SET_REGION / READ_REGION_NAME | channel-group zones | 🔧 NEW: region read/switch |
| 61/62 | SET/GET_PP_ID | ?? (pp_id in app settings UI) | 🕳 |
| 63/64 | READ/WRITE_ADVANCED_SETTINGS2 | 61B adv block #2 | 🔬 raw captured |
| 65 | UNLOCK | unlock radio (ch_data_lock etc.) | 🕳 |
| 66 | DO_PROG_FUNC | trigger a PF effect remotely (remote button press!) | 🔬 payload = effect code? test live |
| 67/68 | SET/GET_MSG | canned/stored messages | 🕳 |
| 69 | BLE_CONN_PARAM | BLE tuning | 🕳 |
| 70 | SET_TIME | sync radio clock | 🕳 planned (1-click) |
| 71/72 | SET/GET_APRS_PATH | digi path string | ✅ APRS tab |
| 74 | SET_DEV_ID | write device id | 🕳 |
| 76 | GET_POSITION | radio GPS fix | ✅ + map plot planned |
| 77 | SET_SATELLITE_INFO | satellite pass data (app has full sat-tracking UI) | 🕳 phase 4+ |

## 2. PROGRAMMABLE BUTTONS — decoded ✔

`GET_PF(55)` reply = status + 16 × (key byte, effect byte).
Key byte = `button_id<<4 | action_type`. Radio exposes 4 physical
buttons × 4 gestures each (the fixed order we captured:
`06 08 04 02` = btn0 × {LOW_TO_HIGH, SHORT_SINGLE, DOUBLE, LONG}, then btn1
`16 18 14 12`, btn2 `26 28 24 22`, btn3 `36 38 34 32`).

Action types: 1 SHORT, 2 LONG, 3 VERY_LONG, 4 DOUBLE, 5 REPEAT,
6 LOW_TO_HIGH (press), 7 HIGH_TO_LOW (release), 8 SHORT_SINGLE, 13 TRIPLE.

**Effect codes** (what a button press does):

| code | effect | code | effect |
|------|--------|------|--------|
| 0 | DISABLE | 12 | TOGGLE_CH_SCAN |
| 1 | ALARM | 13 | MAIN_PTT |
| 2 | ALARM_AND_MUTE | 14 | SUB_PTT |
| 3 | TOGGLE_OFFLINE (talkaround) | 15 | TOGGLE_MONITOR |
| 4 | TOGGLE_RADIO_TX | 16 | BT_PAIRING |
| 5 | TOGGLE_TX_POWER | 17 | TOGGLE_DOUBLE_CH |
| 6 | TOGGLE_FM (broadcast) | 18 | TOGGLE_AB_CH |
| 7 | PREV_CHANNEL | 19 | SEND_LOCATION |
| 8 | NEXT_CHANNEL | 20 | ONE_CLICK_LINK |
| 9 | T_CALL (1750 Hz) | 21 | VOL_DOWN |
| 10 | PREV_REGION | 22 | VOL_UP |
| 11 | NEXT_REGION | 23 | TOGGLE_MUTE |

Radio also advertises codes 0x18-0x1f in GET_PF_ACTIONS — newer firmware
effects, labels unknown (shown as UNKNOWN_24…31 in the UI).

`SET_PF(56)` request = the 16 **effect** bytes only, in the fixed key order
above (verified safe read-modify-write 2026-07-15).

Our radio's current map (btn/gesture → effect):
btn0: press=DISABLE, single=VOL_DOWN(21), double=TOGGLE_MUTE(23), long=TOGGLE_AB_CH(18)
btn1: press=DISABLE, single=VOL_UP(22), double=DISABLE, long=TOGGLE_OFFLINE(3)
btn2: single=NEXT_CHANNEL(8), rest DISABLE
btn3: double=PREV_CHANNEL(7), rest DISABLE

## 3. EVENTS the radio pushes (EVENT_NOTIFICATION type byte)

1 HT_STATUS_CHANGED (incl. RSSI — drives the S-meter live) ·
2 DATA_RXD (TNC fragments → APRS feed) · 3 NEW_INQUIRY_DATA ·
4 RESTORE_FACTORY_SETTINGS · 5 HT_CH_CHANGED · 6 HT_SETTINGS_CHANGED ·
7 RINGING_STOPPED · 8 RADIO_STATUS_CHANGED (FM broadcast) · 9 USER_ACTION ·
10 SYSTEM_EVENT · 11 BSS_SETTINGS_CHANGED · 12 DATA_TXD ·
13 POSITION_CHANGED · 14 FREQ_SCAN_STATUS_CHANGED

## 4. VENDOR APP UI SURFACE (from R.java) → our coverage

| app screen / control | protocol | web UI |
|---|---|---|
| Dual-VFO home screen w/ signal bars (`icon_rssi`,`level_rssi`,`iv_signal`) | GET_HT_STATUS.rssi + events | ✅ NEW LCD + S-meter |
| Channel manager (`channel_manager`,`edit_channel`,`del_rf_ch`) | 13/14 | ✅ |
| Region manager (`region_manager`) | 58/59/60/73 | 🔧 read+switch |
| Settings (all `general_settings` fields) | 10/11/12 | ✅ 43 fields |
| APRS settings + share location (`aprs_settings`,`share_location_by_aprs`) | 33/34/71/72 | ✅ |
| FM broadcast radio (`fm`,`seek_up`,`seek_down`) | 24-28 | 🔧 NEW card |
| NOAA weather (`noaa_group`,`wx_ch`,`wx_mode`) | settings wx_mode/noaa_ch | ✅ fields |
| Programmable buttons (`programmable_button`,`ptt_actions`) | 55/56/75 | 🔧 NEW panel |
| RF status diagnostics (`show_rf_status`, rssi×4 + noise×4) | 52 | 🔬 raw poll |
| DTMF keyboard / decode (`dtmf_keyboard`,`dtmf_decode`,`dtmf_speed`) | app-side audio via HFP | 🕳 phase 3 (audio) |
| Morse code tools (`morse_code_*`) | app-side audio | 🕳 phase 3 |
| T-Call / tone burst (`t_call`,`tcall`) | PF effect 9 / DO_PROG_FUNC | 🔬 |
| Freq scan (`freq_scan`) | FREQ_MODE_* + event 14 | 🕳 |
| Satellite tracking (`amateur_radio_satellite`,`tle`,`min_elevation_angle`) | 77 | 🕳 |
| Engineering menu (vco/power_pa/rssi_offset/noise_offset/agc/de-emph…) | 29/30/37/38/40/44/46/47 | 🔬 debug console only — calibration, handle with care |
| Firmware update (`firmware_check_update`) | vendor cloud + DFU | ❌ out of scope (by design) |
| Cloud/account/teams/chat/maps downloads | gRPC to vendor servers | ❌ **explicit non-goal — no telemetry** |
| Audio: hold-to-speak / intercom (`hold_to_speak`,`float_ptt`,`audio`) | HFP (0000111F) + aghfp settings | 🕳 phase 3 = voice bridge + PTT |

## 5. S-METER TRUTH CHAIN (what "genuine sync" means here)

The radio itself measures RSSI and reports it two ways:
1. `GET_HT_STATUS` / HT_STATUS_CHANGED: 4-bit `rssi` 0..15 (the same value
   the radio's own screen bars show — the vendor app maps it ×(100/15) to %).
2. `READ_RF_STATUS(52)`: the diagnostics block with per-VFO rssi/noise
   (undecoded 31B — we poll and log raw so the layout can be diffed live).

The web LCD S-meter uses (1) — the radio's own reading, pushed by the radio
on change and polled every `rf_poll_seconds` while the Dashboard is open.
Mapping used: rssi 0..9 → S1..S9, 10..15 → S9+10 … S9+60 dB over.
