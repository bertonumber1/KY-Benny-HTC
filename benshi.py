"""Benshi radio protocol (VR-N7600 / Vero / BTECH family).

Derived from the radio's own companion-app protocol (GAIA-framed command set)
for own-hardware interoperability.

Frame formats
-------------
BLE GATT (service 00001100-d102-11e1-9b23-00025b00a5a5):
    [group:u16be][command:u16be][payload...]
    written to char ...1101, replies/events indicated on char ...1102.

RFCOMM (GAIA SPP, service 00001107-d102-11e1-9b23-00025b00a5a5):
    [0xFF][0x01][flags][len(payload)][group:u16be][command:u16be][payload...]
    (+1 trailing XOR checksum byte if flags bit0 set; we always send flags=0)

Replies have bit15 set on the command id; payload[0] is a status byte.
Events arrive as group=BASIC, command=EVENT_NOTIFICATION (no reply bit),
payload[0] = event type.

All multi-bit fields are MSB-first big-endian bitfields.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict

GATT_SERVICE = "00001100-d102-11e1-9b23-00025b00a5a5"
GATT_WRITE = "00001101-d102-11e1-9b23-00025b00a5a5"
GATT_INDICATE = "00001102-d102-11e1-9b23-00025b00a5a5"
RFCOMM_GAIA_UUID = "00001107-d102-11e1-9b23-00025b00a5a5"

GROUP_BASIC = 2
GROUP_EXTENDED = 10
REPLY_BIT = 0x8000


class Cmd(enum.IntEnum):
    UNKNOWN = 0
    GET_DEV_ID = 1
    SET_REG_TIMES = 2
    GET_REG_TIMES = 3
    GET_DEV_INFO = 4
    READ_STATUS = 5
    REGISTER_NOTIFICATION = 6
    CANCEL_NOTIFICATION = 7
    GET_NOTIFICATION = 8
    EVENT_NOTIFICATION = 9
    READ_SETTINGS = 10
    WRITE_SETTINGS = 11
    STORE_SETTINGS = 12
    READ_RF_CH = 13
    WRITE_RF_CH = 14
    GET_IN_SCAN = 15
    SET_IN_SCAN = 16
    SET_REMOTE_DEVICE_ADDR = 17
    GET_TRUSTED_DEVICE = 18
    DEL_TRUSTED_DEVICE = 19
    GET_HT_STATUS = 20
    SET_HT_ON_OFF = 21
    GET_VOLUME = 22
    SET_VOLUME = 23
    RADIO_GET_STATUS = 24
    RADIO_SET_MODE = 25
    RADIO_SEEK_UP = 26
    RADIO_SEEK_DOWN = 27
    RADIO_SET_FREQ = 28
    READ_ADVANCED_SETTINGS = 29
    WRITE_ADVANCED_SETTINGS = 30
    HT_SEND_DATA = 31
    SET_POSITION = 32
    READ_BSS_SETTINGS = 33
    WRITE_BSS_SETTINGS = 34
    FREQ_MODE_SET_PAR = 35
    FREQ_MODE_GET_STATUS = 36
    READ_RDA1846S_AGC = 37
    WRITE_RDA1846S_AGC = 38
    READ_FREQ_RANGE = 39
    WRITE_DE_EMPH_COEFFS = 40
    STOP_RINGING = 41
    SET_TX_TIME_LIMIT = 42
    SET_IS_DIGITAL_SIGNAL = 43
    SET_HL = 44
    SET_DID = 45
    SET_IBA = 46
    GET_IBA = 47
    SET_TRUSTED_DEVICE_NAME = 48
    SET_VOC = 49
    GET_VOC = 50
    SET_PHONE_STATUS = 51
    READ_RF_STATUS = 52
    PLAY_TONE = 53
    GET_DID = 54
    GET_PF = 55
    SET_PF = 56
    RX_DATA = 57
    WRITE_REGION_CH = 58
    WRITE_REGION_NAME = 59
    SET_REGION = 60
    SET_PP_ID = 61
    GET_PP_ID = 62
    READ_ADVANCED_SETTINGS2 = 63
    WRITE_ADVANCED_SETTINGS2 = 64
    UNLOCK = 65
    DO_PROG_FUNC = 66
    SET_MSG = 67
    GET_MSG = 68
    BLE_CONN_PARAM = 69
    SET_TIME = 70
    SET_APRS_PATH = 71
    GET_APRS_PATH = 72
    READ_REGION_NAME = 73
    SET_DEV_ID = 74
    GET_PF_ACTIONS = 75
    GET_POSITION = 76
    SET_SATELLITE_INFO = 77


class Event(enum.IntEnum):
    UNKNOWN = 0
    HT_STATUS_CHANGED = 1
    DATA_RXD = 2
    NEW_INQUIRY_DATA = 3
    RESTORE_FACTORY_SETTINGS = 4
    HT_CH_CHANGED = 5
    HT_SETTINGS_CHANGED = 6
    RINGING_STOPPED = 7
    RADIO_STATUS_CHANGED = 8
    USER_ACTION = 9
    SYSTEM_EVENT = 10
    BSS_SETTINGS_CHANGED = 11
    DATA_TXD = 12
    POSITION_CHANGED = 13
    FREQ_SCAN_STATUS_CHANGED = 14


class Status(enum.IntEnum):
    SUCCESS = 0
    NOT_SUPPORTED = 1
    NOT_AUTHENTICATED = 2
    INSUFFICIENT_RESOURCES = 3
    AUTHENTICATING = 4
    INVALID_PARAMETER = 5
    INCORRECT_STATE = 6
    IN_PROGRESS = 7


class StatusType(enum.IntEnum):
    BATTERY_LEVEL = 1
    BATTERY_VOLTAGE = 2
    RC_BATTERY_LEVEL = 3
    BATTERY_LEVEL_AS_PERCENTAGE = 4


MODULATIONS = ["FM", "AM", "DMR"]  # 2-bit modulation field


class ProtocolError(Exception):
    pass


class CommandFailed(ProtocolError):
    def __init__(self, cmd: int, status: int):
        self.cmd = cmd
        self.status = status
        try:
            name = Status(status).name
        except ValueError:
            name = str(status)
        try:
            cname = Cmd(cmd).name
        except ValueError:
            cname = str(cmd)
        super().__init__(f"{cname} failed: {name}")


# ---------------------------------------------------------------- bitfields

class BitReader:
    """MSB-first big-endian bit reader."""

    def __init__(self, data: bytes, bit_offset: int = 0):
        self.data = data
        self.pos = bit_offset
        self.nbits = len(data) * 8

    def remaining(self) -> int:
        return self.nbits - self.pos

    def bool(self) -> bool:
        return self.uint(1) == 1

    def uint(self, bits: int) -> int:
        if self.remaining() < bits:
            raise ProtocolError("short payload")
        val = 0
        pos = self.pos
        for _ in range(bits):
            byte = self.data[pos >> 3]
            val = (val << 1) | ((byte >> (7 - (pos & 7))) & 1)
            pos += 1
        self.pos = pos
        return val

    def sint(self, bits: int) -> int:
        v = self.uint(bits)
        if v & (1 << (bits - 1)):
            v -= 1 << bits
        return v

    def bytes(self, n: int) -> bytes:
        return bytes(self.uint(8) for _ in range(n))

    def seek(self, bit_pos: int):
        self.pos = bit_pos


class BitWriter:
    """MSB-first big-endian bit writer."""

    def __init__(self, nbytes: int):
        self.buf = bytearray(nbytes)
        self.pos = 0

    def uint(self, val: int, bits: int) -> "BitWriter":
        val &= (1 << bits) - 1
        pos = self.pos
        for i in range(bits - 1, -1, -1):
            if (val >> i) & 1:
                self.buf[pos >> 3] |= 1 << (7 - (pos & 7))
            pos += 1
        self.pos = pos
        return self

    def bool(self, val: bool) -> "BitWriter":
        return self.uint(1 if val else 0, 1)

    def bytes_at(self, byte_offset: int, data: bytes):
        self.buf[byte_offset:byte_offset + len(data)] = data

    def seek(self, bit_pos: int):
        self.pos = bit_pos


# ------------------------------------------------------------------ framing

def ble_frame(group: int, cmd: int, payload: bytes = b"") -> bytes:
    return bytes([group >> 8, group & 0xFF, cmd >> 8, cmd & 0xFF]) + payload


def parse_ble_frame(data: bytes):
    if len(data) < 4:
        raise ProtocolError("frame too short")
    group = (data[0] << 8) | data[1]
    cmd = (data[2] << 8) | data[3]
    return group, cmd, bytes(data[4:])


def gaia_frame(group: int, cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) > 254:
        raise ProtocolError("payload too long")
    return bytes([0xFF, 0x01, 0x00, len(payload)]) + ble_frame(group, cmd, payload)


class GaiaDeframer:
    """Incremental parser for the RFCOMM byte stream."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf.extend(data)
        out = []
        while True:
            # resync to 0xFF start-of-frame
            while self.buf and self.buf[0] != 0xFF:
                self.buf.pop(0)
            if len(self.buf) < 8:
                break
            flags = self.buf[2]
            plen = self.buf[3]
            total = 8 + plen + (1 if flags & 1 else 0)
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            del self.buf[:total]
            group = (frame[4] << 8) | frame[5]
            cmd = (frame[6] << 8) | frame[7]
            out.append((group, cmd, frame[8:8 + plen]))
        return out


# ------------------------------------------------------------------ structs

@dataclass
class DevInfo:
    vendor_id: int = 0
    product_id: int = 0
    hw_ver: int = 0
    soft_ver: int = 0
    support_radio: bool = False
    support_medium_power: bool = False
    fixed_loc_speaker_vol: bool = False
    not_support_soft_power_ctrl: bool = False
    have_no_speaker: bool = False
    have_hm_speaker: bool = False
    region_count: int = 0
    support_noaa: bool = False
    gmrs: bool = False
    support_vfo: bool = False
    support_dmr: bool = False
    channel_count: int = 16
    freq_range_count: int = 0

    @classmethod
    def parse(cls, payload: bytes) -> "DevInfo":
        d = cls()
        r = BitReader(payload, 8)  # skip status byte
        if len(payload) == 5:
            d.product_id = r.uint(8)
            d.hw_ver = r.uint(8)
            d.soft_ver = r.uint(16)
            return d
        d.vendor_id = r.uint(8)
        d.product_id = r.uint(16)
        d.hw_ver = r.uint(8)
        d.soft_ver = r.uint(16)
        d.support_radio = r.bool()
        d.support_medium_power = r.bool()
        d.fixed_loc_speaker_vol = r.bool()
        d.not_support_soft_power_ctrl = r.bool()
        d.have_no_speaker = r.bool()
        d.have_hm_speaker = r.bool()
        d.region_count = r.uint(6)
        d.support_noaa = r.bool()
        d.gmrs = r.bool()
        d.support_vfo = r.bool()
        d.support_dmr = r.bool()
        d.channel_count = r.uint(8) or 16
        try:
            d.freq_range_count = r.uint(4)
        except ProtocolError:
            pass
        return d

    def channel_ext_size(self) -> int:
        """Extra per-channel bytes appended on DMR firmware."""
        if not self.support_dmr:
            return 0
        return 6 if self.soft_ver >= 113 else 2


@dataclass
class Channel:
    channel_id: int = 0
    tx_mod: int = 0          # 0=FM 1=AM 2=DMR
    tx_freq: int = 0         # Hz
    rx_mod: int = 0
    rx_freq: int = 0         # Hz
    tx_sub_audio: int = 0    # 0=off, <6700 DCS code, else CTCSS Hz*100
    rx_sub_audio: int = 0
    scan: bool = False
    tx_at_max_power: bool = False
    talk_around: bool = False      # "offline" in vendor naming
    bandwidth_wide: bool = False   # True=25kHz
    pre_de_emph_bypass: bool = False
    sign: bool = False
    tx_at_med_power: bool = False
    tx_disable: bool = False
    fixed_freq: bool = False
    fixed_bandwidth: bool = False
    fixed_tx_power: bool = False
    mute: bool = False
    bclo: bool = False
    rev: bool = False
    name: str = ""
    # DMR extension
    tx_color: int = 0
    rx_color: int = 0
    slot: int = 0
    dmr_id: int = 1

    @staticmethod
    def _freq_diff_apply(rx: int, txfield: int) -> int:
        # vendor FD codes when the freq-diff flag is set
        if txfield >= 1000 or txfield < 0:
            return rx + txfield
        code = txfield if txfield < 6 else 5
        step = 5_000_000 if rx > 300_000_000 else 600_000
        if code == 0:   # FD_NONE_FREQ
            return 0
        if code == 2:   # FD_AUTO_ADD
            return rx + step
        if code == 3:   # FD_AUTO_SUB
            return rx - step
        return rx

    @classmethod
    def parse(cls, payload: bytes, byte_offset: int, ext_size: int = 0,
              channel_id: int = 0) -> "Channel":
        if byte_offset + 24 > len(payload):
            raise ProtocolError("channel payload too short")
        c = cls(channel_id=channel_id)
        r = BitReader(payload, byte_offset * 8)
        c.tx_mod = r.uint(2)
        c.tx_freq = r.uint(30)
        c.rx_mod = r.uint(2)
        c.rx_freq = r.uint(30)
        c.tx_sub_audio = r.uint(16)
        c.rx_sub_audio = r.uint(16)
        c.scan = r.bool()
        c.tx_at_max_power = r.bool()
        c.talk_around = r.bool()
        c.bandwidth_wide = r.bool()
        c.pre_de_emph_bypass = r.bool()
        c.sign = r.bool()
        c.tx_at_med_power = r.bool()
        c.tx_disable = r.bool()
        c.fixed_freq = r.bool()
        c.fixed_bandwidth = r.bool()
        c.fixed_tx_power = r.bool()
        c.mute = r.bool()
        freq_diff = r.bool()
        c.bclo = r.bool()
        c.rev = r.bool()
        r.bool()  # spare
        if freq_diff:
            c.tx_freq = cls._freq_diff_apply(c.rx_freq, c.tx_freq)
        raw_name = payload[byte_offset + 14:byte_offset + 24]
        c.name = raw_name.split(b"\x00")[0].decode("gb2312", "replace").strip()
        if ext_size > 0 and byte_offset + 24 + ext_size <= len(payload):
            r.seek((byte_offset + 24) * 8)
            c.tx_color = r.uint(4)
            c.rx_color = r.uint(4)
            c.slot = r.uint(1)
            r.uint(7)
            if ext_size >= 6:
                c.dmr_id = r.uint(32)
        return c

    def to_bytes(self, ext_size: int = 0) -> bytes:
        w = BitWriter(24 + ext_size)
        w.uint(self.tx_mod, 2)
        w.uint(self.tx_freq, 30)
        w.uint(self.rx_mod, 2)
        w.uint(self.rx_freq, 30)
        w.uint(self.tx_sub_audio, 16)
        w.uint(self.rx_sub_audio, 16)
        w.bool(self.scan)
        w.bool(self.tx_at_max_power)
        w.bool(self.talk_around)
        w.bool(self.bandwidth_wide)
        w.bool(self.pre_de_emph_bypass)
        w.bool(self.sign)
        w.bool(self.tx_at_med_power)
        w.bool(self.tx_disable)
        w.bool(self.fixed_freq)
        w.bool(self.fixed_bandwidth)
        w.bool(self.fixed_tx_power)
        w.bool(self.mute)
        w.bool(False)  # freq_diff: we always write absolute freqs
        w.bool(self.bclo)
        w.bool(self.rev)
        w.bool(False)  # spare
        name = self.name[:10].encode("gb2312", "replace")[:10]
        w.bytes_at(14, name)
        if ext_size > 0:
            w.seek(24 * 8)
            w.uint(self.tx_color, 4)
            w.uint(self.rx_color, 4)
            w.uint(self.slot, 1)
            w.uint(0, 7)
            if ext_size >= 6:
                did = self.dmr_id & 0xFFFFFF
                w.uint(did if did else 1, 32)
        return bytes(w.buf)


SETTINGS_FIELDS = [
    # (name, bits) in wire order; 'bool' encoded as 1 bit
    ("channel_a_lo", 4), ("channel_b_lo", 4), ("scan", 1),
    ("aghfp_call_mode", 1), ("double_channel", 2), ("squelch_level", 4),
    ("tail_elim", 1), ("audio_relay_en", 1), ("auto_power_on", 1),
    ("keep_aghfp_link", 1), ("mic_gain", 3), ("tx_hold_time", 4),
    ("tx_time_limit", 5), ("local_speaker", 2), ("bt_mic_gain", 3),
    ("adaptive_response", 1), ("dis_tone", 1), ("power_saving_mode", 1),
    ("auto_power_off", 3), ("auto_share_loc_ch_lo", 5), ("hm_speaker", 2),
    ("positioning_system", 4), ("time_offset", 6), ("use_freq_range_2", 1),
    ("ptt_lock", 1), ("leading_sync_bit_en", 1), ("pairing_at_power_on", 1),
    ("screen_timeout", 5), ("kiss_upload_tx_msg", 1), ("kiss_en", 1),
    ("imperial_unit", 1), ("channel_a_hi", 4), ("channel_b_hi", 4),
    ("wx_mode", 2), ("noaa_ch", 4), ("vfo1_tx_power_x", 2),
    ("vfo2_tx_power_x", 2), ("dis_digital_mute", 1), ("signaling_ecc_en", 1),
    ("ch_data_lock", 1), ("auto_share_loc_ch_hi", 3), ("kiss_tx_delay", 8),
    ("kiss_tx_tail", 8), ("vox_en", 1), ("vox_level", 3), ("dis_bt_mic", 1),
    ("vox_delay", 3), ("ns_en", 1), ("alarm_volume", 4),
    ("use_custom_location", 1), ("gpwpl_upload_en", 1), ("vfo1_mod_freq_x", 1),
]


@dataclass
class Settings:
    raw: bytes = b""
    fields: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: bytes, byte_offset: int = 1) -> "Settings":
        s = cls(raw=bytes(payload[byte_offset:]))
        r = BitReader(payload, byte_offset * 8)
        f = {}
        try:
            for name, bits in SETTINGS_FIELDS:
                f[name] = r.uint(bits)
        except ProtocolError:
            pass
        f["channel_a"] = f.get("channel_a_lo", 0) + f.get("channel_a_hi", 0) * 16
        f["channel_b"] = f.get("channel_b_lo", 0) + f.get("channel_b_hi", 0) * 16
        f["auto_share_loc_ch"] = (f.get("auto_share_loc_ch_lo", 0)
                                  + (f.get("auto_share_loc_ch_hi", 0) << 5))
        s.fields = f
        return s

    def to_bytes(self) -> bytes:
        f = dict(self.fields)
        if "channel_a" in f:
            f["channel_a_lo"] = f["channel_a"] % 16
            f["channel_a_hi"] = f["channel_a"] // 16
        if "channel_b" in f:
            f["channel_b_lo"] = f["channel_b"] % 16
            f["channel_b_hi"] = f["channel_b"] // 16
        if "auto_share_loc_ch" in f:
            f["auto_share_loc_ch_lo"] = f["auto_share_loc_ch"] & 0x1F
            f["auto_share_loc_ch_hi"] = f["auto_share_loc_ch"] >> 5
        size = max(22, len(self.raw))
        w = BitWriter(size)
        if self.raw:
            w.buf[:len(self.raw)] = self.raw
            # clear the bitfield region we rewrite, keep the tail
            # (custom location etc.) intact
            nbits = sum(b for _, b in SETTINGS_FIELDS)
            for bit in range(nbits):
                w.buf[bit >> 3] &= ~(1 << (7 - (bit & 7))) & 0xFF
        for name, bits in SETTINGS_FIELDS:
            w.uint(f.get(name, 0), bits)
        return bytes(w.buf)


@dataclass
class HTStatus:
    is_power_on: bool = False
    is_in_tx: bool = False
    is_sq: bool = False
    is_in_rx: bool = False
    double_channel: int = 0   # 0=off 1=A 2=B
    is_scan: bool = False
    is_radio: bool = False
    curr_ch_id: int = 0
    is_gps_locked: bool = False
    is_hfp_connected: bool = False
    is_aoc_connected: bool = False
    rssi: int = -1            # 0..15
    curr_region: int = -1

    @classmethod
    def parse(cls, payload: bytes, byte_offset: int = 1) -> "HTStatus":
        st = cls()
        r = BitReader(payload, byte_offset * 8)
        st.is_power_on = r.bool()
        st.is_in_tx = r.bool()
        st.is_sq = r.bool()
        st.is_in_rx = r.bool()
        st.double_channel = r.uint(2)
        st.is_scan = r.bool()
        st.is_radio = r.bool()
        st.curr_ch_id = r.uint(4)
        st.is_gps_locked = r.bool()
        st.is_hfp_connected = r.bool()
        st.is_aoc_connected = r.bool()
        r.uint(1)
        if r.remaining() > 0:
            st.rssi = r.uint(4)
            st.curr_region = r.uint(6)
            st.curr_ch_id += r.uint(4) * 16
        return st


@dataclass
class Position:
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: int | None = None
    speed_kmh: int | None = None
    bearing: int | None = None
    time: int | None = None
    accuracy: int | None = None

    @classmethod
    def parse(cls, payload: bytes, byte_offset: int = 1) -> "Position":
        p = cls()
        r = BitReader(payload, byte_offset * 8)
        p.latitude = r.sint(24) / 30000.0
        p.longitude = r.sint(24) / 30000.0
        try:
            alt = r.sint(16)
            if alt != -32768:
                p.altitude = alt
            spd = r.sint(16)
            if spd >= 0:
                p.speed_kmh = spd
            brg = r.sint(16)
            if brg >= 0:
                p.bearing = brg
            t = r.uint(32)
            if t:
                p.time = t
            acc = r.uint(16)
            if acc > 0:
                p.accuracy = acc
        except ProtocolError:
            pass
        return p


def sub_audio_text(v: int) -> str:
    if v == 0:
        return ""
    if v < 6700:
        return f"DCS {v}"
    return f"{v / 100:.1f} Hz"


def struct_dict(obj) -> dict:
    d = asdict(obj)
    d.pop("raw", None)
    return d


# ------------------------------------------------------------- BSS / APRS

BSS_FIELDS = [
    ("max_fwd_times", 4), ("time_to_live", 4),
    ("ptt_release_send_location", 1), ("ptt_release_send_id_info", 1),
    ("ptt_release_send_bss_user_id", 1), ("should_share_location", 1),
    ("send_pwr_voltage", 1), ("packet_format", 1),        # 0=BSS 1=APRS
    ("allow_position_check", 1), ("_unk0", 1), ("aprs_ssid", 4),
    ("smart_beacon_en", 1), ("mic_e_en", 1), ("send_id_by_aprs", 1),
    ("_unk1", 1), ("location_share_interval", 8),          # unit = 10 s
]
# then: bss_user_id_lower u32, ptt_release_id_info[12], beacon_message[18],
# aprs_symbol[2], aprs_callsign[6], bss_user_id_upper u32,
# V2 ext: smart_beacon_min_interval:4, smart_beacon_max_interval:5, _unk2:7


@dataclass
class BssSettings:
    fields: dict = field(default_factory=dict)
    size: int = 52          # 46 (fw<50), 50 (fw 50-135), 52 (fw>=136)

    @staticmethod
    def size_for_fw(soft_ver: int) -> int:
        if soft_ver < 50:
            return 46
        return 50 if soft_ver < 136 else 52

    @classmethod
    def parse(cls, payload: bytes, byte_offset: int = 1) -> "BssSettings":
        data = payload[byte_offset:]
        s = cls(size=len(data))
        r = BitReader(data)
        f = {}
        for name, bits in BSS_FIELDS:
            f[name] = r.uint(bits)
        user_lo = r.uint(32)
        f["ptt_release_id_info"] = r.bytes(12).split(b"\x00")[0].decode("utf-8", "replace")
        f["beacon_message"] = r.bytes(18).split(b"\x00")[0].decode("utf-8", "replace")
        f["aprs_symbol"] = r.bytes(2).decode("latin1")
        f["aprs_callsign"] = r.bytes(6).split(b"\x00")[0].decode("utf-8", "replace").strip()
        user_hi = 0
        try:
            user_hi = r.uint(32)
            f["smart_beacon_min_interval"] = r.uint(4)
            f["smart_beacon_max_interval"] = r.uint(5)
            f["_unk2"] = r.uint(7)
        except ProtocolError:
            pass
        f["bss_user_id"] = (user_hi << 32) | user_lo
        s.fields = f
        return s

    def to_bytes(self) -> bytes:
        f = self.fields
        w = BitWriter(self.size)
        for name, bits in BSS_FIELDS:
            w.uint(int(f.get(name, 0)), bits)
        uid = int(f.get("bss_user_id", 0))
        w.uint(uid & 0xFFFFFFFF, 32)

        def padstr(s, n):
            b = str(s).encode("utf-8", "replace")[:n]
            return b + bytes(n - len(b))

        for ch in padstr(f.get("ptt_release_id_info", ""), 12):
            w.uint(ch, 8)
        for ch in padstr(f.get("beacon_message", ""), 18):
            w.uint(ch, 8)
        sym = (str(f.get("aprs_symbol", "/$")) + "/$")[:2]
        w.uint(ord(sym[0]), 8)
        w.uint(ord(sym[1]), 8)
        for ch in padstr(f.get("aprs_callsign", ""), 6):
            w.uint(ch, 8)
        if self.size >= 50:
            w.uint((uid >> 32) & 0xFFFFFFFF, 32)
        if self.size >= 52:
            w.uint(int(f.get("smart_beacon_min_interval", 0)), 4)
            w.uint(int(f.get("smart_beacon_max_interval", 0)), 5)
            w.uint(int(f.get("_unk2", 0)), 7)
        return bytes(w.buf)


# ------------------------------------------------------------ TNC fragments

FRAG_FINAL = 0x80
FRAG_WITH_CHANNEL = 0x40
FRAG_DATA_MAX = 53


def fragment_tnc(data: bytes, channel_id: int | None = None) -> list[bytes]:
    """Split a TNC frame into HT_SEND_DATA payloads (app-compatible)."""
    out = []
    pos, frag_id = 0, 0
    with_ch = channel_id is not None
    while pos < len(data) or not out:
        room = FRAG_DATA_MAX - (1 if with_ch else 0)
        chunk = data[pos:pos + room]
        pos += len(chunk)
        hdr = frag_id & 0x3F
        if pos >= len(data):
            hdr |= FRAG_FINAL
        if with_ch:
            hdr |= FRAG_WITH_CHANNEL
        frame = bytes([hdr]) + chunk + (bytes([channel_id]) if with_ch else b"")
        out.append(frame)
        frag_id += 1
        with_ch = False
    return out


class TncReassembler:
    """Rebuild complete frames from DATA_RXD fragment payloads."""

    def __init__(self):
        self.buf = bytearray()
        self.channel_id = None

    def feed(self, frag: bytes):
        """frag = event payload after the event-type byte.
        Returns (frame, channel_id) when complete, else None."""
        if not frag:
            return None
        hdr = frag[0]
        body = frag[1:]
        if (hdr & 0x3F) == 0:
            self.buf.clear()
            self.channel_id = None
        if hdr & FRAG_WITH_CHANNEL and body:
            self.channel_id = body[-1]
            body = body[:-1]
        self.buf.extend(body)
        if hdr & FRAG_FINAL:
            frame, channel_id = bytes(self.buf), self.channel_id
            self.buf.clear()
            self.channel_id = None
            return frame, channel_id
        return None
