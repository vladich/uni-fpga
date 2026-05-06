"""
Transform a raw harvested signal map into the categorical board schema:

    Board:
      id: <board_id>
      fpga: { producer, family, part }
      defaults: { iostandard }
      pinBanks:
        <bank>: { pins: <scalar | list | mapping>, ... }

The classifier maps signal names to bank addresses by regex. Anything we can't
classify lands in `unclassified:` for review — never silently dropped.

Run:
    python -m tools.curate_board <board_id>             # writes config/boards/<id>.yml
    python -m tools.curate_board <board_id> --dry-run   # prints to stdout
"""

import argparse
import os
import re
import sys
from collections import Counter, OrderedDict

import yaml


REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
RAW_DIR = os.path.join(REPO, "config", "boards", "_raw")
OUT_DIR = os.path.join(REPO, "config", "boards")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
#
# `classify(signal)` returns a "destination" tuple describing where the signal
# goes in the output, or None if we don't recognize it.
#
# Destination tuples take three shapes:
#   ('scalar',   bank, meta)                        -> banks[bank] = pin
#   ('list',     bank, index, meta)                 -> banks[bank].pins[index] = pin
#   ('subkey',   bank, subkey, meta)                -> banks[bank].pins[subkey] = pin
#   ('sublist',  bank, subkey, index, meta)         -> banks[bank].pins[subkey][index] = pin
#
# `meta` carries bank-level metadata (frequency_mhz for clocks, etc.).


def _named_button(name):
    return {"BTNC": 0, "BTNU": 1, "BTNL": 2, "BTNR": 3, "BTND": 4}.get(name)


# Digilent Pmod signal numbering: the silkscreen is JA[1..4, 7..10] (skipping
# 5/6 = GND/VCC). We collapse these into 0-based indices [0..7].
_PMOD_INDEX = {1: 0, 2: 1, 3: 2, 4: 3, 7: 4, 8: 5, 9: 6, 10: 7}


def classify(signal):
    # Vendor XDC/QSF/CST/PCF files mix upper- and lower-case freely. We compare
    # in upper-case so the rules don't have to enumerate both forms.
    s = signal.upper()

    # ---- Clocks -----------------------------------------------------------
    m = re.match(r"^CLK(\d+)MHZ$", s)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    m = re.match(r"^MAX10_CLK\d+_(\d+)$", s)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    m = re.match(r"^CLK_(\d+)$", s)   # CLK_50, CLK_100, CLK_125 (Zynq Zybo Z7 uses CLK_125)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    m = re.match(r"^CLKIN_(\d+)$", s)   # Altera dev kit: CLKIN_50, CLKIN_125
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    m = re.match(r"^OSC_(\d+)(?:_.*)?$", s)   # OSC_50, OSC_50_B3B (Terasic)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    if s in ("CLK", "CLOCK", "OSC", "SYS_CLK", "CLK_IN", "CLKIN", "MAIN_CLK", "BOARD_CLK"):
        return ("scalar", "clk", {})

    # ---- Reset ------------------------------------------------------------
    if s in ("CPU_RESETN", "CPU_RESET_N", "BTNCPURESET",
             "RESET_N", "RESETN", "RST_N", "RSTN", "RST_IN"):
        return ("scalar", "cpu_resetn", {})
    if s in ("RESET", "RST"):
        return ("scalar", "cpu_reset", {})

    # Terasic-style clocks with bank-suffix annotation: CLOCK_50_B8A, OSC_50_B3B
    m = re.match(r"^CLOCK_(\d+)(?:_.*)?$", s)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    # Multi-clock variants: CLOCK2_50, CLOCK3_50, CLOCK4_50 (DE1-SoC etc.)
    m = re.match(r"^CLOCK(\d+)_(\d+)$", s)
    if m:
        idx = int(m.group(1))
        freq = int(m.group(2))
        return ("scalar", "clk{}_{}mhz".format(idx, freq).lower(),
                {"frequency_mhz": freq})

    # CLK_<freq>M (a7_lite_35t)
    m = re.match(r"^CLK_(\d+)M$", s)
    if m:
        freq = int(m.group(1))
        return ("scalar", "clk{}mhz".format(freq).lower(), {"frequency_mhz": freq})

    # ChipKit-style reset (Arty A7)
    if s == "CK_RST":
        return ("scalar", "cpu_resetn", {})

    # FPGA_CLK1_50, FPGA_CLK2_50, FPGA_CLK3_50 (DE10-Nano)
    m = re.match(r"^FPGA_CLK(\d+)_(\d+)$", s)
    if m:
        idx = int(m.group(1))
        freq = int(m.group(2))
        return ("scalar", "fpga_clk{}_{}mhz".format(idx, freq).lower(),
                {"frequency_mhz": freq})

    # SMA clock connectors (external clock in/out via SMA jack)
    if s == "CLKIN_SMA":
        return ("scalar", "clkin_sma", {})
    if s == "CLKOUT_SMA":
        return ("scalar", "clkout_sma", {})

    # Reserved pins (board-defined for future use)
    m = re.match(r"^RESERVE\[(\d+)\]$", s)
    if m:
        return ("list", "reserved", int(m.group(1)), {})

    # Quartus internal config pins (JTAG / dual-purpose). Exposed as a
    # bank so users can see them but they're typically driven by Quartus.
    if s in ("~ALTERA_DCLK~", "~ALTERA_DATA0~", "~ALTERA_NCSO~", "~ALTERA_NCEO~"):
        return ("scalar", "altera_config_" + s.strip("~").lower().replace("altera_", ""), {})

    # ---- Switches ---------------------------------------------------------
    m = re.match(r"^SW\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_switches", int(m.group(1)), {})

    m = re.match(r"^SW_N\[(\d+)\]$", s)     # active-low switches (zeowaa)
    if m:
        return ("list", "onboard_switches", int(m.group(1)), {})

    # ---- LEDs (single bank) -----------------------------------------------
    m = re.match(r"^LED\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_leds", int(m.group(1)), {})

    m = re.match(r"^LED_N\[(\d+)\]$", s)    # active-low LEDs (zeowaa)
    if m:
        return ("list", "onboard_leds", int(m.group(1)), {})

    m = re.match(r"^LED_OUT_([A-Z])$", s)   # QMtech-style LED_OUT_A..F
    if m:
        return ("list", "onboard_leds", ord(m.group(1)) - ord("A"), {})

    # icebreaker-style LED1, LED2, ... (1-based, no brackets, NOT followed by _R/G/B)
    m = re.match(r"^LED(\d+)$", s)
    if m and not re.match(r"^LED\d+_(?:R|G|B|RED|GREEN|BLUE)$", s):
        idx = int(m.group(1))
        # 1-based label -> 0-based index.
        return ("list", "onboard_leds", max(0, idx - 1), {})

    m = re.match(r"^LEDR\[(\d+)\]$", s)   # Altera red LEDs (primary bank)
    if m:
        return ("list", "onboard_leds", int(m.group(1)), {})

    m = re.match(r"^LEDG\[(\d+)\]$", s)   # Altera green LEDs (secondary)
    if m:
        return ("list", "onboard_leds_green", int(m.group(1)), {})

    if s == "LED":
        return ("scalar", "onboard_led", {})

    # icebreaker single-LED conveniences
    if s == "LEDR_N":
        return ("scalar", "onboard_led_red", {})
    if s == "LEDG_N":
        return ("scalar", "onboard_led_green", {})

    # ---- RGB LEDs ---------------------------------------------------------
    m = re.match(r"^LED(\d+)_([RGB])$", s)
    if m:
        idx = int(m.group(1))
        chan = m.group(2).lower()
        # Many boards label them LED16/LED17 (continuing from LED[15]); collapse
        # to a 0-based RGB index.
        return ("subkey", "onboard_rgb_led_{}".format(idx), chan, {})

    if s in ("LED_RED_N", "LED_R_N"):
        return ("subkey", "onboard_rgb_led_0", "r", {})
    if s in ("LED_GRN_N", "LED_GREEN_N", "LED_G_N"):
        return ("subkey", "onboard_rgb_led_0", "g", {})
    if s in ("LED_BLU_N", "LED_BLUE_N", "LED_B_N"):
        return ("subkey", "onboard_rgb_led_0", "b", {})

    # Nexys 4 alternative naming: RGB1_RED/GREEN/BLUE, RGB2_*
    m = re.match(r"^RGB(\d+)_(RED|GREEN|BLUE)$", s)
    if m:
        idx = int(m.group(1))
        chan = {"RED": "r", "GREEN": "g", "BLUE": "b"}[m.group(2)]
        return ("subkey", "onboard_rgb_led_{}".format(idx), chan, {})

    m = re.match(r"^LED_RGB\[(\d+)\]$", s)
    if m:
        idx = int(m.group(1))
        return ("subkey", "onboard_rgb_led_0", ["r", "g", "b"][idx], {})

    # ---- 7-segment, Xilinx/Digilent style (CA..CG, DP, AN[*]) -------------
    if s in ("CA", "CB", "CC", "CD", "CE", "CF", "CG", "DP"):
        return ("subkey", "onboard_7seg", s.lower(), {})

    m = re.match(r"^AN\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_7seg", "anodes", int(m.group(1)), {})

    # ---- 7-segment, Altera per-digit (HEX0[0..7], HEX1[0..7], ...) --------
    m = re.match(r"^HEX(\d+)\[(\d+)\]$", s)
    if m:
        digit, seg = int(m.group(1)), int(m.group(2))
        return ("sublist", "onboard_7seg", "hex{}".format(digit), seg, {})

    # DE0-style: HEX0_D[0..6] for segments, HEX0_DP for decimal point
    m = re.match(r"^HEX(\d+)_D\[(\d+)\]$", s)
    if m:
        digit, seg = int(m.group(1)), int(m.group(2))
        return ("sublist", "onboard_7seg", "hex{}".format(digit), seg, {})

    m = re.match(r"^HEX(\d+)_DP$", s)
    if m:
        digit = int(m.group(1))
        # DP follows the 7 segments at index 7.
        return ("sublist", "onboard_7seg", "hex{}".format(digit), 7, {})

    # ---- Push-buttons -----------------------------------------------------
    idx = _named_button(s)
    if idx is not None:
        return ("list", "onboard_buttons", idx, {})

    m = re.match(r"^BTN\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^BTN(\d+)$", s)          # icebreaker BTN1, BTN2, BTN3 (1-based, no brackets)
    if m:
        return ("list", "onboard_buttons", max(0, int(m.group(1)) - 1), {})

    m = re.match(r"^CKEY\[(\d+)\]$", s)     # omdazz_epm570
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    if s == "BTN_N":
        return ("scalar", "onboard_button", {})

    m = re.match(r"^KEY\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^KEY(\d+)$", s)          # Alinx-style KEY2, KEY3 (no brackets)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^KEY_IN\[(\d+)\]$", s)   # Alinx-style key inputs
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^KEY_N\[(\d+)\]$", s)    # active-low keys (emooc_cc, zeowaa)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^KEY_SW\[(\d+)\]$", s)   # combined keys/switches (omdazz, rzrd)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    m = re.match(r"^BTN_([A-Z])$", s)       # QMtech-style BTN_A..E
    if m:
        return ("list", "onboard_buttons", ord(m.group(1)) - ord("A"), {})

    m = re.match(r"^BUTTON\[(\d+)\]$", s)   # DE0-style BUTTON[*]
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})

    # ---- Pmod headers ------------------------------------------------------
    # Digilent uses several conventions across boards:
    #   - JA[1..4, 7..10]      (silkscreen)            — Nexys, Basys, Arty
    #   - JA[0..7]             (0-based)               — Zybo Z7, Eclypse Z7
    #   - GPIO_JA[0..7]        (Zynq XDC alt naming)
    m = re.match(r"^J([A-E])\[(\d+)\]$", s)
    if m:
        header, pin = m.group(1).lower(), int(m.group(2))
        if pin in _PMOD_INDEX:
            return ("list", "pmod_j{}".format(header), _PMOD_INDEX[pin], {})
        if 0 <= pin <= 7:
            return ("list", "pmod_j{}".format(header), pin, {})

    m = re.match(r"^GPIO_J([A-E])\[(\d+)\]$", s)
    if m:
        header, pin = m.group(1).lower(), int(m.group(2))
        return ("list", "pmod_j{}".format(header), pin, {})

    # XADC differential Pmod
    m = re.match(r"^XA_([NP])\[(\d+)\]$", s)
    if m:
        side = m.group(1).lower()
        idx = int(m.group(2)) - 1
        return ("sublist", "pmod_xadc", side, idx, {"iostandard": "LVDS"})

    # icebreaker P1A/P1B/P2 etc.
    m = re.match(r"^P(\d+)([AB])(\d+)$", s)
    if m:
        bank, side, pin = m.group(1), m.group(2).lower(), int(m.group(3))
        if pin in _PMOD_INDEX:
            return ("list", "pmod_p{}{}".format(bank, side), _PMOD_INDEX[pin], {})

    # icebreaker single-Pmod naming: P2_1, P2_2, ... (no A/B side)
    m = re.match(r"^P(\d+)_(\d+)$", s)
    if m:
        bank, pin = m.group(1), int(m.group(2))
        if pin in _PMOD_INDEX:
            return ("list", "pmod_p{}".format(bank), _PMOD_INDEX[pin], {})

    # Generic "PMOD_<n>[idx]" / "PMOD[idx]" (Tang boards)
    m = re.match(r"^PMOD_(\d+)\[(\d+)\]$", s)
    if m:
        return ("list", "pmod_{}".format(m.group(1)), int(m.group(2)), {})
    m = re.match(r"^PMOD\[(\d+)\]$", s)
    if m:
        return ("list", "pmod", int(m.group(1)), {})

    # ---- VGA --------------------------------------------------------------
    m = re.match(r"^VGA_([RGB])\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_vga", m.group(1).lower(), int(m.group(2)), {})

    m = re.match(r"^VGA_OUT_([RGB])\[(\d+)\]$", s)   # alinx_ax301
    if m:
        return ("sublist", "onboard_vga", m.group(1).lower(), int(m.group(2)), {})

    m = re.match(r"^VGA_RGB\[(\d+)\]$", s)            # piswords6 (combined bus)
    if m:
        return ("sublist", "onboard_vga", "rgb", int(m.group(1)), {})

    # Mixed-case Digilent style: vgaRed/vgaGreen/vgaBlue (after upper-casing
    # they become VGARED, VGAGREEN, VGABLUE)
    m = re.match(r"^VGA(RED|GREEN|BLUE)\[(\d+)\]$", s)
    if m:
        chan = {"RED": "r", "GREEN": "g", "BLUE": "b"}[m.group(1)]
        return ("sublist", "onboard_vga", chan, int(m.group(2)), {})

    if s in ("VGA_HS", "HS", "HSYNC", "VGA_HSYNC", "VGA_OUT_HS"):
        return ("subkey", "onboard_vga", "hs", {})
    if s in ("VGA_VS", "VS", "VSYNC", "VGA_VSYNC", "VGA_OUT_VS"):
        return ("subkey", "onboard_vga", "vs", {})

    # Single-bit RGB outputs (omdazz, rzrd — only 1 bit per channel)
    if s in ("VGA_R",):
        return ("subkey", "onboard_vga", "r", {})
    if s in ("VGA_G",):
        return ("subkey", "onboard_vga", "g", {})
    if s in ("VGA_B",):
        return ("subkey", "onboard_vga", "b", {})

    # Bare RED/GREEN/BLUE indexed (some Altera boards use this without a VGA_ prefix)
    m = re.match(r"^(RED|GREEN|BLUE)\[(\d+)\]$", s)
    if m:
        chan = {"RED": "r", "GREEN": "g", "BLUE": "b"}[m.group(1)]
        return ("sublist", "onboard_vga", chan, int(m.group(2)), {})

    # ---- UART -------------------------------------------------------------
    # Two naming conventions are mixed across vendors:
    #   (A) Host/connector-perspective with _IN/_OUT suffix (Digilent XDC):
    #       UART_TXD_IN  = host TX going *into* FPGA  -> FPGA RX
    #       UART_RXD_OUT = host RX coming *out* of FPGA -> FPGA TX
    #   (B) FPGA-perspective (most others):
    #       UART_TX / TX = FPGA TX,  UART_RX / RX = FPGA RX
    # The bank stores FPGA-perspective tx/rx so peripheral bindings stay sane.
    if s == "UART_TXD_IN":
        return ("subkey", "onboard_uart", "rx", {})
    if s == "UART_RXD_OUT":
        return ("subkey", "onboard_uart", "tx", {})
    if s in ("UART_TX", "TX", "USB_UART_TX", "UART_TXD", "TXD", "RSTX"):
        return ("subkey", "onboard_uart", "tx", {})
    if s in ("UART_RX", "RX", "USB_UART_RX", "UART_RXD", "RXD", "RSRX"):
        return ("subkey", "onboard_uart", "rx", {})
    if s == "UART_CTS":
        return ("subkey", "onboard_uart", "cts", {})
    if s == "UART_RTS":
        return ("subkey", "onboard_uart", "rts", {})

    # ---- microSD ----------------------------------------------------------
    if s == "SD_RESET":
        return ("subkey", "onboard_microsd", "reset", {})
    if s == "SD_CD":
        return ("subkey", "onboard_microsd", "cd", {})
    if s == "SD_SCK":
        return ("subkey", "onboard_microsd", "sck", {})
    if s == "SD_CMD":
        return ("subkey", "onboard_microsd", "cmd", {})
    m = re.match(r"^SD_DAT\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_microsd", "dat", int(m.group(1)), {})

    # ---- On-board PDM mic (Nexys/Basys naming) ----------------------------
    if s in ("M_CLK", "MIC_CLK"):
        return ("subkey", "onboard_mic", "clk", {})
    if s in ("M_DATA", "MIC_DATA"):
        return ("subkey", "onboard_mic", "data", {})
    if s in ("M_LRSEL", "MIC_LRSEL"):
        return ("subkey", "onboard_mic", "lrsel", {})

    # ---- PWM amplifier ----------------------------------------------------
    if s == "AUD_PWM":
        return ("subkey", "onboard_pwm_amp", "pwm", {})
    if s == "AUD_SD":
        return ("subkey", "onboard_pwm_amp", "sd", {})

    # ---- Ethernet PHY -----------------------------------------------------
    if s == "ETH_MDC":
        return ("subkey", "onboard_ethernet_phy", "mdc", {})
    if s == "ETH_MDIO":
        return ("subkey", "onboard_ethernet_phy", "mdio", {})
    if s == "ETH_RSTN":
        return ("subkey", "onboard_ethernet_phy", "rstn", {})
    if s == "ETH_CRSDV":
        return ("subkey", "onboard_ethernet_phy", "crsdv", {})
    if s == "ETH_RXERR":
        return ("subkey", "onboard_ethernet_phy", "rxerr", {})
    m = re.match(r"^ETH_RXD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ethernet_phy", "rxd", int(m.group(1)), {})
    if s == "ETH_TXEN":
        return ("subkey", "onboard_ethernet_phy", "txen", {})
    m = re.match(r"^ETH_TXD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ethernet_phy", "txd", int(m.group(1)), {})
    if s == "ETH_REFCLK":
        return ("subkey", "onboard_ethernet_phy", "refclk", {})
    if s == "ETH_INTN":
        return ("subkey", "onboard_ethernet_phy", "intn", {})

    # ---- QSPI flash -------------------------------------------------------
    m = re.match(r"^QSPI_DQ\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_qspi_flash", "dq", int(m.group(1)), {})
    if s == "QSPI_CSN":
        return ("subkey", "onboard_qspi_flash", "csn", {})

    # ---- Accelerometer ----------------------------------------------------
    if s == "ACL_MISO":
        return ("subkey", "onboard_accelerometer", "miso", {})
    if s == "ACL_MOSI":
        return ("subkey", "onboard_accelerometer", "mosi", {})
    if s == "ACL_SCLK":
        return ("subkey", "onboard_accelerometer", "sclk", {})
    if s == "ACL_CSN":
        return ("subkey", "onboard_accelerometer", "csn", {})
    m = re.match(r"^ACL_INT(\d+)$", s)
    if m:
        return ("subkey", "onboard_accelerometer", "int{}".format(m.group(1)), {})

    # ---- Temperature sensor -----------------------------------------------
    if s in ("TMP_SCL", "TEMP_SCL"):
        return ("subkey", "onboard_temp_sensor", "scl", {})
    if s in ("TMP_SDA", "TEMP_SDA"):
        return ("subkey", "onboard_temp_sensor", "sda", {})
    if s in ("TMP_INT", "TEMP_INT"):
        return ("subkey", "onboard_temp_sensor", "int", {})
    if s in ("TMP_CT", "TEMP_CT"):
        return ("subkey", "onboard_temp_sensor", "ct", {})

    # ---- USB-HID / PS/2 ---------------------------------------------------
    if s in ("PS2_CLK", "USBKB_CLK"):
        return ("subkey", "onboard_usb_hid", "clk", {})
    if s in ("PS2_DATA", "USBKB_DATA"):
        return ("subkey", "onboard_usb_hid", "data", {})

    # ---- HDMI / DVI / TMDS (typically external on Tang/icebreaker) -------
    if s == "TMDS_CLK_P":
        return ("subkey", "onboard_hdmi", "clk_p", {})
    if s == "TMDS_CLK_N":
        return ("subkey", "onboard_hdmi", "clk_n", {})
    m = re.match(r"^TMDS_D_P\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_p", int(m.group(1)), {})
    m = re.match(r"^TMDS_D_N\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_n", int(m.group(1)), {})

    # ---- LCD (large LCD on Tang Mega 138k etc.) --------------------------
    m = re.match(r"^LARGE_LCD_([RGB])\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_lcd", m.group(1).lower(), int(m.group(2)), {})
    if s == "LARGE_LCD_DE":
        return ("subkey", "onboard_lcd", "de", {})
    if s == "LARGE_LCD_VS":
        return ("subkey", "onboard_lcd", "vs", {})
    if s == "LARGE_LCD_HS":
        return ("subkey", "onboard_lcd", "hs", {})
    if s == "LARGE_LCD_CK":
        return ("subkey", "onboard_lcd", "ck", {})
    if s == "LARGE_LCD_INIT":
        return ("subkey", "onboard_lcd", "init", {})
    if s == "LARGE_LCD_BL":
        return ("subkey", "onboard_lcd", "bl", {})

    # ---- GPIO -------------------------------------------------------------
    m = re.match(r"^GPIO\[(\d+)\]$", s)
    if m:
        return ("list", "gpio", int(m.group(1)), {})
    m = re.match(r"^GPIO_(\d+)\[(\d+)\]$", s)   # GPIO_0[*], GPIO_1[*]
    if m:
        return ("list", "gpio_{}".format(m.group(1)), int(m.group(2)), {})
    m = re.match(r"^GPIO_J(\d+)\[(\d+)\]$", s)  # emooc_cc: GPIO_J7[*]
    if m:
        return ("list", "gpio_j{}".format(m.group(1)), int(m.group(2)), {})
    m = re.match(r"^GPIO_([A-Z])\[(\d+)\]$", s)  # marsohod: gpio_a[*]
    if m:
        return ("list", "gpio_{}".format(m.group(1).lower()), int(m.group(2)), {})
    m = re.match(r"^GPIO(\d+)$", s)             # Marsohod-style GPIO12 (no brackets)
    if m:
        return ("list", "gpio", int(m.group(1)), {})

    # Generic IO[*] (marsohod3gw2)
    m = re.match(r"^IO\[(\d+)\]$", s)
    if m:
        return ("list", "gpio", int(m.group(1)), {})

    # Arduino/Chipkit shield I/O (Arty A7, Cmod A7): ck_io0..ck_ioN
    m = re.match(r"^CK_IO(\d+)$", s)
    if m:
        return ("list", "arduino_io", int(m.group(1)), {})

    # ---- 7-segment displays with non-standard signal names ---------------
    # Many boards use generic segment+digit-select bus naming; map them all
    # into onboard_7seg.segments / onboard_7seg.anodes.
    for seg_pat in (
        r"^SMG_DATA\[(\d+)\]$",         # Alinx
        r"^SEG\[(\d+)\]$",              # omdazz, rzrd, saylinx
        r"^SEG_DATA\[(\d+)\]$",         # alinx_ax301
        r"^ABCDEFGH\[(\d+)\]$",         # emooc_cc, piswords6
        r"^ABCDEFGH_N\[(\d+)\]$",       # zeowaa (active low)
    ):
        m = re.match(seg_pat, s)
        if m:
            return ("sublist", "onboard_7seg", "segments", int(m.group(1)), {})

    for dig_pat in (
        r"^SCAN_SIG\[(\d+)\]$",         # Alinx anode-select
        r"^DIG\[(\d+)\]$",              # omdazz, rzrd, saylinx
        r"^SEG_SEL\[(\d+)\]$",          # alinx_ax301
        r"^DIGIT\[(\d+)\]$",            # piswords6
        r"^DIGIT_N\[(\d+)\]$",          # emooc_cc (active low)
    ):
        m = re.match(dig_pat, s)
        if m:
            return ("sublist", "onboard_7seg", "anodes", int(m.group(1)), {})

    # ---- Audio codec (Terasic boards have CS4231 or WM8731 codec) -------
    if s == "AUD_ADCDAT":
        return ("subkey", "onboard_audio_codec", "adc_dat", {})
    if s == "AUD_ADCLRCK":
        return ("subkey", "onboard_audio_codec", "adc_lrck", {})
    if s == "AUD_BCLK":
        return ("subkey", "onboard_audio_codec", "bclk", {})
    if s == "AUD_DACDAT":
        return ("subkey", "onboard_audio_codec", "dac_dat", {})
    if s == "AUD_DACLRCK":
        return ("subkey", "onboard_audio_codec", "dac_lrck", {})
    if s == "AUD_I2C_SCLK":
        return ("subkey", "onboard_audio_codec", "i2c_sclk", {})
    if s == "AUD_I2C_SDAT":
        return ("subkey", "onboard_audio_codec", "i2c_sdat", {})
    if s == "AUD_MUTE":
        return ("subkey", "onboard_audio_codec", "mute", {})
    if s == "AUD_XCK":
        return ("subkey", "onboard_audio_codec", "xck", {})

    # ---- VGA non-standard variants (Terasic SoCKit and similar) ---------
    if s == "VGA_BLANK_N":
        return ("subkey", "onboard_vga", "blank_n", {})
    if s == "VGA_SYNC_N":
        return ("subkey", "onboard_vga", "sync_n", {})
    if s == "VGA_CLK":
        return ("subkey", "onboard_vga", "clk", {})

    # ---- LCD (alternate naming on Tang Mega 138k) ------------------------
    m = re.match(r"^LCD_([RGB])\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_lcd", m.group(1).lower(), int(m.group(2)), {})
    if s == "LCD_CLK":
        return ("subkey", "onboard_lcd", "ck", {})
    if s == "LCD_EN":
        return ("subkey", "onboard_lcd", "de", {})
    if s == "LCD_VS":
        return ("subkey", "onboard_lcd", "vs", {})
    if s == "LCD_HS":
        return ("subkey", "onboard_lcd", "hs", {})
    if s == "LCD_DE":
        return ("subkey", "onboard_lcd", "de", {})
    if s == "LCD_BL":
        return ("subkey", "onboard_lcd", "bl", {})
    if s == "LCD_INIT":
        return ("subkey", "onboard_lcd", "init", {})

    # Small LCD (Tang Nano)
    if s == "SMALL_LCD_DATA":
        return ("subkey", "onboard_small_lcd", "data", {})
    if s == "SMALL_LCD_CLK":
        return ("subkey", "onboard_small_lcd", "clk", {})
    if s == "SMALL_LCD_RESETN":
        return ("subkey", "onboard_small_lcd", "resetn", {})
    if s == "SMALL_LCD_CS":
        return ("subkey", "onboard_small_lcd", "cs", {})
    if s == "SMALL_LCD_RS":
        return ("subkey", "onboard_small_lcd", "rs", {})

    # ---- TF (TransFlash / microSD) socket — common on Tang boards -------
    if s == "TF_CS":
        return ("subkey", "onboard_microsd", "cs", {})
    if s == "TF_MOSI":
        return ("subkey", "onboard_microsd", "mosi", {})
    if s == "TF_SCLK":
        return ("subkey", "onboard_microsd", "sclk", {})
    if s == "TF_MISO":
        return ("subkey", "onboard_microsd", "miso", {})

    # ---- Serial flash (Tang and others) ---------------------------------
    if s == "FLASH_CLK":
        return ("subkey", "onboard_serial_flash", "clk", {})
    if s == "FLASH_CSB":
        return ("subkey", "onboard_serial_flash", "csb", {})
    if s == "FLASH_MOSI":
        return ("subkey", "onboard_serial_flash", "mosi", {})
    if s == "FLASH_MISO":
        return ("subkey", "onboard_serial_flash", "miso", {})

    # ---- Misc on-board peripherals -------------------------------------
    if s == "BUZZER":
        return ("scalar", "onboard_buzzer", {})
    if s == "EEPROM_SCL":
        return ("subkey", "onboard_eeprom", "scl", {})
    if s == "EEPROM_SDA":
        return ("subkey", "onboard_eeprom", "sda", {})

    # Character LCD (HD44780-style) — omdazz, rzrd, etc.
    if s == "LCD_RS":
        return ("subkey", "onboard_char_lcd", "rs", {})
    if s == "LCD_RW":
        return ("subkey", "onboard_char_lcd", "rw", {})
    if s == "LCD_E":
        return ("subkey", "onboard_char_lcd", "e", {})
    m = re.match(r"^LCD_D\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_char_lcd", "d", int(m.group(1)), {})

    # Pseudo-GPIO using SDRAM pins (omdazz / rzrd quirk — some boards expose
    # SDRAM pins as user GPIO when no SDRAM is needed).
    m = re.match(r"^PSEUDO_GPIO_USING_SDRAM_PINS\[(\d+)\]$", s)
    if m:
        return ("list", "gpio_pseudo_sdram", int(m.group(1)), {})

    # ---- DDR2 SDRAM (Cyclone III dev kit uses ddr2top_/ddr2bot_/ddr2_) ---
    # ddr2top_a[0..N], ddr2bot_a[0..N] etc. (indexed)
    m = re.match(r"^DDR2(TOP|BOT)_([A-Z_]+)\[(\d+)\]$", s)
    if m:
        bank = "onboard_ddr2_" + m.group(1).lower()
        return ("sublist", bank, m.group(2).lower().rstrip("_"), int(m.group(3)), {})

    # ddr2top_<sig>, ddr2bot_<sig> (scalar signals)
    m = re.match(r"^DDR2(TOP|BOT)_([A-Z_]+)$", s)
    if m:
        bank = "onboard_ddr2_" + m.group(1).lower()
        return ("subkey", bank, m.group(2).lower().rstrip("_"), {})

    m = re.match(r"^DDR2_([A-Z_]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ddr2", m.group(1).lower().rstrip("_"), int(m.group(2)), {})

    m = re.match(r"^DDR2_([A-Z_]+)$", s)
    if m:
        return ("subkey", "onboard_ddr2", m.group(1).lower().rstrip("_"), {})

    # ---- DDR3 SDRAM (Terasic SoCKit / DE1-SoC etc.) ----------------------
    m = re.match(r"^DDR3_([A-Z_]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ddr3", m.group(1).lower().rstrip("_"), int(m.group(2)), {})

    m = re.match(r"^DDR3_([A-Z_]+)$", s)
    if m:
        return ("subkey", "onboard_ddr3", m.group(1).lower().rstrip("_"), {})

    # ---- SDRAM (Terasic DE1-SoC etc.) -------------------------------------
    m = re.match(r"^DRAM_([A-Z_]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_sdram", m.group(1).lower().rstrip("_"), int(m.group(2)), {})

    m = re.match(r"^DRAM_([A-Z_]+)$", s)
    if m:
        return ("subkey", "onboard_sdram", m.group(1).lower().rstrip("_"), {})

    # ---- Memory (Marsohod-style mem_*) ----------------------------------
    m = re.match(r"^MEM_([A-Z_]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_mem", m.group(1).lower(), int(m.group(2)), {})

    m = re.match(r"^MEM_([A-Z_]+)$", s)
    if m:
        return ("subkey", "onboard_mem", m.group(1).lower(), {})

    if s == "MEM_CLK":
        return ("subkey", "onboard_mem", "clk", {})

    # ---- Termination blocks (DDR boards) ---------------------------------
    m = re.match(r"^TERMINATION_BLK(\d+)~?_?(RUP|RDN)_PAD$", s)
    if m:
        return ("subkey", "termination_blk{}".format(m.group(1)),
                m.group(2).lower(), {})

    # ---- ADC (Cyclone V boards) -----------------------------------------
    if s == "ADC_CS_N":
        return ("subkey", "onboard_adc", "cs_n", {})
    if s == "ADC_DIN":
        return ("subkey", "onboard_adc", "din", {})
    if s == "ADC_DOUT":
        return ("subkey", "onboard_adc", "dout", {})
    if s == "ADC_SCLK":
        return ("subkey", "onboard_adc", "sclk", {})

    # ---- Arduino IO header (DE10-Lite, DE10-Nano) -----------------------
    m = re.match(r"^ARDUINO_IO\[(\d+)\]$", s)
    if m:
        return ("list", "arduino_io", int(m.group(1)), {})

    if s == "ARDUINO_RESET_N":
        return ("scalar", "arduino_reset_n", {})

    # ---- HDMI transmitter (DE10-Nano on-board ADV7513) ------------------
    m = re.match(r"^HDMI_TX_D\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi_tx", "d", int(m.group(1)), {})
    if s == "HDMI_TX_CLK":
        return ("subkey", "onboard_hdmi_tx", "clk", {})
    if s == "HDMI_TX_DE":
        return ("subkey", "onboard_hdmi_tx", "de", {})
    if s == "HDMI_TX_HS":
        return ("subkey", "onboard_hdmi_tx", "hs", {})
    if s == "HDMI_TX_VS":
        return ("subkey", "onboard_hdmi_tx", "vs", {})
    if s == "HDMI_TX_INT":
        return ("subkey", "onboard_hdmi_tx", "int", {})
    if s == "HDMI_I2C_SCL":
        return ("subkey", "onboard_hdmi_tx", "i2c_scl", {})
    if s == "HDMI_I2C_SDA":
        return ("subkey", "onboard_hdmi_tx", "i2c_sda", {})
    if s == "HDMI_LRCLK":
        return ("subkey", "onboard_hdmi_tx", "lrclk", {})
    if s == "HDMI_MCLK":
        return ("subkey", "onboard_hdmi_tx", "mclk", {})
    if s == "HDMI_SCLK":
        return ("subkey", "onboard_hdmi_tx", "sclk", {})
    if s == "HDMI_I2S":
        return ("subkey", "onboard_hdmi_tx", "i2s", {})

    # ---- HPS (Cyclone V SoC HPS-side I/O) -------------------------------
    m = re.match(r"^HPS_([A-Z0-9_]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hps", m.group(1).lower().rstrip("_"), int(m.group(2)), {})
    m = re.match(r"^HPS_([A-Z0-9_]+)$", s)
    if m:
        return ("subkey", "onboard_hps", m.group(1).lower().rstrip("_"), {})

    # ---- HSMC mezzanine connectors (Cyclone III dev kit, DE2-115) -------
    m = re.match(r"^HSM([AB])_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "hsm{}".format(m.group(1).lower()),
                m.group(2).lower().rstrip("_"), int(m.group(3)), {})
    m = re.match(r"^HSM([AB])_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "hsm{}".format(m.group(1).lower()),
                m.group(2).lower().rstrip("_"), {})

    # ---- Ethernet (Altera-style enet_<sig>) ------------------------------
    m = re.match(r"^ENET_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ethernet_phy", m.group(1).lower().rstrip("_"),
                int(m.group(2)), {})
    m = re.match(r"^ENET_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_ethernet_phy", m.group(1).lower().rstrip("_"), {})

    # ---- emooc_cc style additional GPIO banks (GPIO_P1, GPIO_P2) --------
    m = re.match(r"^GPIO_P(\d+)\[(\d+)\]$", s)
    if m:
        return ("list", "gpio_p{}".format(m.group(1)), int(m.group(2)), {})

    # ---- USB host (Altera dev kits) -------------------------------------
    m = re.match(r"^USB_HOST_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_usb_host", m.group(1).lower().rstrip("_"),
                int(m.group(2)), {})
    m = re.match(r"^USB_HOST_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_usb_host", m.group(1).lower().rstrip("_"), {})

    # ---- Speaker / IRDA / SDCARD / Flash / SRAM (Altera dev kits) -------
    if s == "SPEAKER":
        return ("scalar", "onboard_speaker", {})
    if s == "IRDA_RXD":
        return ("subkey", "onboard_irda", "rxd", {})
    if s == "IRDA_TXD":
        return ("subkey", "onboard_irda", "txd", {})

    m = re.match(r"^SDCARD_([A-Z]+)$", s)
    if m:
        return ("subkey", "onboard_microsd", m.group(1).lower(), {})
    m = re.match(r"^SDCARD_([A-Z]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_microsd", m.group(1).lower(), int(m.group(2)), {})

    # NIOS II / max system controller (Cyclone III dev kit) — group as
    # internal so they don't get lost.
    m = re.match(r"^NIOS_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_nios", m.group(1).lower(), {})
    m = re.match(r"^MAX_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_max_ctrl", m.group(1).lower(), {})

    # ---- I²S signals (audio) --------------------------------------------
    m = re.match(r"^I2S_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_i2s", m.group(1).lower(), {})

    # PS/2 (Altera-style PS2_KBCLK / PS2_MSCLK and Digilent-style above)
    if s == "PS2_KBCLK":
        return ("subkey", "onboard_ps2_keyboard", "clk", {})
    if s == "PS2_KBDAT":
        return ("subkey", "onboard_ps2_keyboard", "data", {})
    if s == "PS2_MSCLK":
        return ("subkey", "onboard_ps2_mouse", "clk", {})
    if s == "PS2_MSDAT":
        return ("subkey", "onboard_ps2_mouse", "data", {})

    # Single-port Altera PS/2 (DE1-SoC)
    if s == "PS2_DAT":
        return ("subkey", "onboard_ps2", "data", {})
    if s == "PS2_CLK2":
        return ("subkey", "onboard_ps2_secondary", "clk", {})
    if s == "PS2_DAT2":
        return ("subkey", "onboard_ps2_secondary", "data", {})

    # ---- DE0-style GPIOn_D[*] / GPIOn_CLKIN[*] ---------------------------
    m = re.match(r"^GPIO(\d+)_D\[(\d+)\]$", s)
    if m:
        return ("list", "gpio_{}".format(m.group(1)), int(m.group(2)), {})
    m = re.match(r"^GPIO(\d+)_CLKIN\[(\d+)\]$", s)
    if m:
        return ("sublist", "gpio_{}".format(m.group(1)), "clkin", int(m.group(2)), {})

    # ---- TV decoder ADV7180 (Terasic boards) ----------------------------
    if s == "TD_CLK27":
        return ("subkey", "onboard_tv_decoder", "clk27", {})
    m = re.match(r"^TD_DATA\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_tv_decoder", "data", int(m.group(1)), {})
    if s == "TD_HS":
        return ("subkey", "onboard_tv_decoder", "hs", {})
    if s == "TD_VS":
        return ("subkey", "onboard_tv_decoder", "vs", {})
    if s == "TD_RESET_N":
        return ("subkey", "onboard_tv_decoder", "reset_n", {})

    # ---- Misc onboard small peripherals --------------------------------
    if s == "FAN_CTRL":
        return ("scalar", "onboard_fan_ctrl", {})
    if s == "FPGA_I2C_SCLK":
        return ("subkey", "onboard_fpga_i2c", "sclk", {})
    if s == "FPGA_I2C_SDAT":
        return ("subkey", "onboard_fpga_i2c", "sdat", {})

    # ---- Serial flash (icebreaker / Tang Nano 4k variants) -------------
    if s == "FLASH_SCK":
        return ("subkey", "onboard_serial_flash", "sck", {})
    if s == "FLASH_SSB":
        return ("subkey", "onboard_serial_flash", "ssb", {})
    if s == "FLASH_DI":
        return ("subkey", "onboard_serial_flash", "di", {})
    if s == "FLASH_DO":
        return ("subkey", "onboard_serial_flash", "do", {})
    if s == "FLASH_CS":
        return ("subkey", "onboard_serial_flash", "cs", {})
    if s == "FLASH_WP":
        return ("subkey", "onboard_serial_flash", "wp", {})
    if s == "FLASH_HOLD":
        return ("subkey", "onboard_serial_flash", "hold", {})
    m = re.match(r"^FLASH_IO(\d+)$", s)
    if m:
        return ("sublist", "onboard_serial_flash", "io", int(m.group(1)), {})

    # ---- HSMC connector (Terasic SoCKit etc.) --------------------------
    m = re.match(r"^HSMC_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "hsmc", m.group(1).lower().rstrip("_"), int(m.group(2)), {})
    m = re.match(r"^HSMC_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "hsmc", m.group(1).lower().rstrip("_"), {})

    # ---- HDMI alternate naming (Tang Nano 9k yosys: TMDSp/TMDSn) -------
    if s == "TMDSP_CLOCK":
        return ("subkey", "onboard_hdmi", "clk_p", {})
    if s == "TMDSN_CLOCK":
        return ("subkey", "onboard_hdmi", "clk_n", {})
    m = re.match(r"^TMDSP\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_p", int(m.group(1)), {})
    m = re.match(r"^TMDSN\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_n", int(m.group(1)), {})

    # Tang Nano 20k: O_TMDS_*
    if s == "O_TMDS_CLK_P":
        return ("subkey", "onboard_hdmi", "clk_p", {})
    if s == "O_TMDS_CLK_N":
        return ("subkey", "onboard_hdmi", "clk_n", {})
    m = re.match(r"^O_TMDS_DATA_P\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_p", int(m.group(1)), {})
    m = re.match(r"^O_TMDS_DATA_N\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_hdmi", "d_n", int(m.group(1)), {})

    # Tang Mega 138k Pro: TMDS_CLK_P_0 / TMDS_D_P_0[0..2] / TMDS_CLK_P_1 / ...
    m = re.match(r"^TMDS_CLK_([PN])_(\d+)$", s)
    if m:
        side = "clk_" + m.group(1).lower()
        return ("subkey", "onboard_hdmi_{}".format(m.group(2)), side, {})
    m = re.match(r"^TMDS_D_([PN])_(\d+)\[(\d+)\]$", s)
    if m:
        side = "d_" + m.group(1).lower()
        return ("sublist", "onboard_hdmi_{}".format(m.group(2)), side, int(m.group(3)), {})

    # Tang Primer 25k: TMDS_0_CLK_P / TMDS_0_D_P[0..2] (index-prefix variant)
    m = re.match(r"^TMDS_(\d+)_CLK_([PN])$", s)
    if m:
        side = "clk_" + m.group(2).lower()
        return ("subkey", "onboard_hdmi_{}".format(m.group(1)), side, {})
    m = re.match(r"^TMDS_(\d+)_D_([PN])\[(\d+)\]$", s)
    if m:
        side = "d_" + m.group(2).lower()
        return ("sublist", "onboard_hdmi_{}".format(m.group(1)), side, int(m.group(3)), {})

    # ---- HDMI EDID I2C -------------------------------------------------
    if s == "EDID_CLK":
        return ("subkey", "onboard_hdmi_edid", "clk", {})
    if s == "EDID_DAT":
        return ("subkey", "onboard_hdmi_edid", "dat", {})

    # ---- Headphone I²S (Tang Nano 20k) ----------------------------------
    if s == "HP_BCK":
        return ("subkey", "onboard_headphone", "bck", {})
    if s == "HP_DIN":
        return ("subkey", "onboard_headphone", "din", {})
    if s == "HP_WS":
        return ("subkey", "onboard_headphone", "ws", {})

    # ---- Misc Tang Nano 20k peripherals ---------------------------------
    if s == "PA_EN":
        return ("scalar", "onboard_pa_enable", {})
    if s == "WS2812":
        return ("scalar", "onboard_ws2812", {})

    # Joystick (Tang Nano 20k)
    if s == "JOYSTICK_MISO2":
        return ("subkey", "onboard_joystick", "miso", {})
    if s == "JOYSTICK_CS2":
        return ("subkey", "onboard_joystick", "cs", {})

    # ---- microSD alternate naming (Tang Nano 9k 4-bit SD mode) ----------
    if s == "SD_CLK":
        return ("subkey", "onboard_microsd", "sck", {})
    if s == "SD_SCLK":
        return ("subkey", "onboard_microsd", "sclk", {})
    if s == "SD_MOSI":
        return ("subkey", "onboard_microsd", "mosi", {})
    if s == "SD_MISO":
        return ("subkey", "onboard_microsd", "miso", {})
    if s == "SD_CS":
        return ("subkey", "onboard_microsd", "cs", {})
    m = re.match(r"^SD_DAT(\d+)$", s)
    if m:
        return ("sublist", "onboard_microsd", "dat", int(m.group(1)), {})

    # ---- UART2 (second UART on Tang Mega 138k Pro) ----------------------
    if s == "UART2_RXD":
        return ("subkey", "onboard_uart2", "rx", {})
    if s == "UART2_TXD":
        return ("subkey", "onboard_uart2", "tx", {})

    # ---- Camera (DVP / OV5640 on Tang Nano 4k) -------------------------
    if s == "CAM_SCL":
        return ("subkey", "onboard_camera", "scl", {})
    if s == "CAM_SDA":
        return ("subkey", "onboard_camera", "sda", {})
    if s == "DVP_VSYNC":
        return ("subkey", "onboard_camera", "vsync", {})
    if s == "DVP_HSYNC":
        return ("subkey", "onboard_camera", "hsync", {})
    if s == "DVP_PCLK":
        return ("subkey", "onboard_camera", "pclk", {})
    m = re.match(r"^PIXDATA(\d+)$", s)
    if m:
        return ("sublist", "onboard_camera", "data", int(m.group(1)), {})

    # ---- Marsohod-style lowercase signals -------------------------------
    if s == "SERIAL_RX":
        return ("subkey", "onboard_uart", "rx", {})
    if s == "SERIAL_TX":
        return ("subkey", "onboard_uart", "tx", {})

    # 7-segment with letter-suffix segments (marsohod_mcy316)
    m = re.match(r"^SEG_([A-G]|P)$", s)
    if m:
        # a..g map to ca..cg; p (decimal point) maps to dp.
        sub = m.group(1).lower()
        mapping = {"a": "ca", "b": "cb", "c": "cc", "d": "cd",
                   "e": "ce", "f": "cf", "g": "cg", "p": "dp"}
        return ("subkey", "onboard_7seg", mapping[sub], {})

    # PCM / I²S audio codec (marsohod)
    m = re.match(r"^PCM_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_pcm", m.group(1).lower(), {})

    if s == "SOUND_OUT_R":
        return ("subkey", "onboard_audio_out", "r", {})
    if s == "SOUND_OUT_L":
        return ("subkey", "onboard_audio_out", "l", {})

    # marsohod_mcy316 input-suffixed GPIO: gpio_a_i[*], gpio_b_i[*], gpio<N>_i
    m = re.match(r"^GPIO_([A-Z])_I\[(\d+)\]$", s)
    if m:
        return ("list", "gpio_{}_i".format(m.group(1).lower()), int(m.group(2)), {})
    m = re.match(r"^GPIO(\d+)_I$", s)
    if m:
        return ("list", "gpio_i", int(m.group(1)), {})

    # ---- Camera/AD/Analog buses (Cyclone III dev kit) -------------------
    if s == "ANALOG_SCL":
        return ("subkey", "onboard_analog", "scl", {})
    if s == "ANALOG_SDA":
        return ("subkey", "onboard_analog", "sda", {})

    # Pixel-clock differential pairs (dk_dev_3c120n): pclk0p, pclk0n, pclk1p, pclk1n
    m = re.match(r"^PCLK(\d+)([PN])$", s)
    if m:
        return ("subkey", "pclk_{}".format(m.group(1)), m.group(2).lower(), {})

    # ADA / ADB (analog-digital A/B) data buses on Cyclone III dev kit
    m = re.match(r"^AD([AB])_D\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_adc_{}".format(m.group(1).lower()),
                "d", int(m.group(2)), {})
    m = re.match(r"^AD([AB])_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_adc_{}".format(m.group(1).lower()),
                m.group(2).lower().rstrip("_"), {})

    # ADC data bus (marsohod3gw2)
    m = re.match(r"^ADC_D\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_adc", "d", int(m.group(1)), {})

    if s == "ADC_CLK":
        return ("subkey", "onboard_adc", "clk", {})

    # ---- PCIe (Terasic SoCKit) ------------------------------------------
    if s in ("PCIE_PERST_N", "PCIE_PERST_n"):
        return ("subkey", "onboard_pcie", "perst_n", {})
    if s in ("PCIE_WAKE_N", "PCIE_WAKE_n"):
        return ("subkey", "onboard_pcie", "wake_n", {})
    m = re.match(r"^PCIE_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_pcie", m.group(1).lower().rstrip("_"), int(m.group(2)), {})
    m = re.match(r"^PCIE_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_pcie", m.group(1).lower().rstrip("_"), {})

    # ---- SI5338 PLL/clock generator (Terasic SoCKit) -------------------
    if s == "SI5338_SCL":
        return ("subkey", "onboard_si5338", "scl", {})
    if s == "SI5338_SDA":
        return ("subkey", "onboard_si5338", "sda", {})

    # ---- TEMP sensor SPI variant (Terasic SoCKit) ----------------------
    if s == "TEMP_CS_N":
        return ("subkey", "onboard_temp_sensor", "cs_n", {})
    if s == "TEMP_DIN":
        return ("subkey", "onboard_temp_sensor", "din", {})
    if s == "TEMP_DOUT":
        return ("subkey", "onboard_temp_sensor", "dout", {})
    if s == "TEMP_SCLK":
        return ("subkey", "onboard_temp_sensor", "sclk", {})

    # ---- USB host bridge (Terasic SoCKit / DE1-SoC: USB_B2_*, USB_*) ---
    if s == "USB_B2_CLK":
        return ("subkey", "onboard_usb", "clk", {})
    m = re.match(r"^USB_B2_DATA\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_usb", "data", int(m.group(1)), {})
    if s == "USB_EMPTY":
        return ("subkey", "onboard_usb", "empty", {})
    if s == "USB_FULL":
        return ("subkey", "onboard_usb", "full", {})
    if s == "USB_OE_N":
        return ("subkey", "onboard_usb", "oe_n", {})
    if s == "USB_RD_N":
        return ("subkey", "onboard_usb", "rd_n", {})
    if s == "USB_RESET_N":
        return ("subkey", "onboard_usb", "reset_n", {})
    if s == "USB_SCL":
        return ("subkey", "onboard_usb", "scl", {})
    if s == "USB_SDA":
        return ("subkey", "onboard_usb", "sda", {})
    if s == "USB_INT0":
        return ("subkey", "onboard_usb", "int0", {})
    if s == "USB_INT1":
        return ("subkey", "onboard_usb", "int1", {})
    if s == "USB_WR_N":
        return ("subkey", "onboard_usb", "wr_n", {})
    if s == "USB_DREQ":
        return ("subkey", "onboard_usb", "dreq", {})
    if s == "USB_DACK":
        return ("subkey", "onboard_usb", "dack", {})

    # ---- FT2232 USB-serial bridge (marsohod3gw2): FTB*, FTD[*] ---------
    if s == "FTB0":
        return ("subkey", "onboard_ft_bridge", "b0", {})
    if s == "FTB1":
        return ("subkey", "onboard_ft_bridge", "b1", {})
    m = re.match(r"^FTD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_ft_bridge", "d", int(m.group(1)), {})

    # ---- HDMI differential pairs (colorlight75b: HDMI_P[*], HDMI_N[*]) -
    m = re.match(r"^HDMI_P\[(\d+)\]$", s)
    if m:
        idx = int(m.group(1))
        # Convention: index 3 = clock pair, 0/1/2 = data pairs.
        if idx == 3:
            return ("subkey", "onboard_hdmi", "clk_p", {})
        return ("sublist", "onboard_hdmi", "d_p", idx, {})
    m = re.match(r"^HDMI_N\[(\d+)\]$", s)
    if m:
        idx = int(m.group(1))
        if idx == 3:
            return ("subkey", "onboard_hdmi", "clk_n", {})
        return ("sublist", "onboard_hdmi", "d_n", idx, {})

    # ---- 7-segment with letter-suffix segments (orangepi_msoc) ---------
    # HGFEDCBA[0..7]: bit 0 = A, bit 1 = B, ..., bit 6 = G, bit 7 = H (=DP).
    m = re.match(r"^HGFEDCBA\[(\d+)\]$", s)
    if m:
        idx = int(m.group(1))
        seg_names = ["ca", "cb", "cc", "cd", "ce", "cf", "cg", "dp"]
        if 0 <= idx < len(seg_names):
            return ("subkey", "onboard_7seg", seg_names[idx], {})

    # ---- Mixed-case audio aliases (Nexys 4 mixed-case board_specific) --
    if s in ("MICCLK",):
        return ("subkey", "onboard_mic", "clk", {})
    if s == "MICDATA":
        return ("subkey", "onboard_mic", "data", {})
    if s == "MICLRSEL":
        return ("subkey", "onboard_mic", "lrsel", {})
    if s == "AMPPWM":
        return ("subkey", "onboard_pwm_amp", "pwm", {})
    if s == "AMPSD":
        return ("subkey", "onboard_pwm_amp", "sd", {})

    # ---- Alt VGA naming (de2): VGA_BLANK, VGA_SYNC (without _N) --------
    if s == "VGA_BLANK":
        return ("subkey", "onboard_vga", "blank", {})
    if s == "VGA_SYNC":
        return ("subkey", "onboard_vga", "sync", {})

    # ---- Generic board I²C bus (de2: I2C_SCLK / I2C_SDAT) --------------
    if s == "I2C_SCLK":
        return ("subkey", "onboard_i2c", "sclk", {})
    if s == "I2C_SDAT":
        return ("subkey", "onboard_i2c", "sdat", {})
    if s == "I2C_SCL":
        return ("subkey", "onboard_i2c", "scl", {})
    if s == "I2C_SDA":
        return ("subkey", "onboard_i2c", "sda", {})

    # ---- QMtech Kintex 7: AN/BN/CN/DN — 4-digit anode-select for 7seg --
    if s in ("AN", "BN", "CN", "DN"):
        return ("sublist", "onboard_7seg", "anodes",
                {"AN": 0, "BN": 1, "CN": 2, "DN": 3}[s], {})

    # ---- Character LCD with single bracket bus (omdazz_epm570 LCD[*]) --
    m = re.match(r"^LCD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_char_lcd", "d", int(m.group(1)), {})

    # ---- DAC outputs (Cyclone III dev kit: da[*], db[*]) ----------------
    m = re.match(r"^DA\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_dac_a", "d", int(m.group(1)), {})
    m = re.match(r"^DB\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_dac_b", "d", int(m.group(1)), {})

    # ---- Flash/SRAM shared bus (Cyclone III dev kit: fsa[*], fsd[*]) ---
    m = re.match(r"^FSA\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_flash_sram", "a", int(m.group(1)), {})
    m = re.match(r"^FSD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_flash_sram", "d", int(m.group(1)), {})

    # ---- Parallel flash control signals (Cyclone III dev kit) ---------
    m = re.match(r"^FLASH_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_parallel_flash", m.group(1).lower().rstrip("_"), {})

    # ---- SRAM control bus (Cyclone III dev kit) ----------------------
    m = re.match(r"^SRAM_([A-Z_0-9]+)\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_sram", m.group(1).lower().rstrip("_"), int(m.group(2)), {})
    m = re.match(r"^SRAM_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_sram", m.group(1).lower().rstrip("_"), {})

    # ---- Parallel-bus LCD controller (Cyclone III dev kit) -----------
    m = re.match(r"^LCD_DATA\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_lcd_controller", "data", int(m.group(1)), {})
    m = re.match(r"^LCD_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_lcd_controller", m.group(1).lower().rstrip("_"), {})

    # ---- USB host extension signals (Cyclone III dev kit) -----------
    m = re.match(r"^USB_FD\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_usb", "fd", int(m.group(1)), {})
    m = re.match(r"^USB_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_usb", m.group(1).lower().rstrip("_"), {})

    # ---- Cyclone III dev kit: seven_seg_<letter> + user_pb / user_dipsw / user_led
    m = re.match(r"^SEVEN_SEG_([A-G]|DP)$", s)
    if m:
        seg = m.group(1).lower()
        if seg == "dp":
            return ("subkey", "onboard_7seg", "dp", {})
        return ("subkey", "onboard_7seg", "c" + seg, {})

    if s == "SEVEN_SEG_MINUS":
        return ("subkey", "onboard_7seg", "minus", {})

    m = re.match(r"^SEVEN_SEG_SEL\[(\d+)\]$", s)
    if m:
        return ("sublist", "onboard_7seg", "anodes", int(m.group(1)), {})

    m = re.match(r"^USER_LED\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_leds", int(m.group(1)), {})
    m = re.match(r"^USER_PB\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_buttons", int(m.group(1)), {})
    m = re.match(r"^USER_DIPSW\[(\d+)\]$", s)
    if m:
        return ("list", "onboard_switches", int(m.group(1)), {})

    # ---- MAX2 system controller (Cyclone III dev kit) -----------------
    m = re.match(r"^MAX2_([A-Z_0-9]+)$", s)
    if m:
        return ("subkey", "onboard_max_ctrl", m.group(1).lower().rstrip("_"), {})

    # ---- Speaker (Cyclone III dev kit, omdazz, etc.) -------------------
    if s == "SPEAKER_OUT":
        return ("scalar", "onboard_speaker", {})

    # Nothing matched.
    return None


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def place(banks, dest, info, default_iostd):
    pin = info["pin"]
    iostd = info.get("iostandard")
    kind = dest[0]

    def merge_meta(b, meta):
        for k, v in meta.items():
            b.setdefault("_meta", {}).setdefault(k, v)

    def record_iostd(b, sub, idx_or_key, iostd):
        if iostd is None or iostd == default_iostd:
            return
        b.setdefault("_overrides", {})[pin] = iostd

    if kind == "scalar":
        _, bank, meta = dest
        b = banks.setdefault(bank, {"_kind": "scalar"})
        b["pin"] = pin
        merge_meta(b, meta)
        record_iostd(b, None, None, iostd)

    elif kind == "list":
        _, bank, idx, meta = dest
        b = banks.setdefault(bank, {"_kind": "list", "_pins": {}})
        b["_pins"][idx] = pin
        merge_meta(b, meta)
        record_iostd(b, None, idx, iostd)

    elif kind == "subkey":
        _, bank, sub, meta = dest
        b = banks.setdefault(bank, {"_kind": "map", "_pins": OrderedDict()})
        b["_pins"][sub] = pin
        merge_meta(b, meta)
        record_iostd(b, sub, None, iostd)

    elif kind == "sublist":
        _, bank, sub, idx, meta = dest
        b = banks.setdefault(bank, {"_kind": "map", "_pins": OrderedDict()})
        sl = b["_pins"].setdefault(sub, {})
        sl[idx] = pin
        merge_meta(b, meta)
        record_iostd(b, sub, idx, iostd)


def _flatten_indexed(d):
    """Convert {0: 'A', 1: 'B', 2: 'C'} into ['A', 'B', 'C'] (sparse → None)."""
    if not d:
        return []
    max_idx = max(d.keys())
    return [d.get(i) for i in range(max_idx + 1)]


def render_pins(bank):
    """Render a bank's pin-data into a YAML-ready scalar/list/dict."""
    kind = bank["_kind"]
    if kind == "scalar":
        return bank["pin"]
    if kind == "list":
        return _flatten_indexed(bank["_pins"])
    if kind == "map":
        out = OrderedDict()
        for sub, val in bank["_pins"].items():
            if isinstance(val, dict):  # was a sublist (idx -> pin)
                out[sub] = _flatten_indexed(val)
            else:
                out[sub] = val
        return out
    raise ValueError("unknown bank kind: " + kind)


# ---------------------------------------------------------------------------
# YAML emission (custom, to keep the file readable rather than PyYAML-dumped)
# ---------------------------------------------------------------------------

def _scalar(v):
    """Render a single pin value safely for YAML.

    Quote if:
      - All-digit (would be parsed as int)        — e.g. ICE40 PCF "35"
      - Contains comma (Gowin diff-pair "H5,J5")  — would split in flow context
      - Contains brace (would break flow mapping)
    """
    if v is None:
        return "null"
    s = str(v)
    if s.isdigit() or "," in s or "{" in s or "}" in s:
        return '"{}"'.format(s)
    return s


def _yaml_value(v, indent=0):
    """Inline YAML for scalars and short lists; block style for dicts and long lists."""
    if v is None:
        return "null"
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    return _scalar(v)


def emit_board_yaml(board_id, fpga_info, default_iostd, banks, unclassified, sources):
    lines = []
    lines.append("# {} pin map.".format(board_id))
    lines.append("# Generated by tools/curate_board.py — re-run to refresh.")
    lines.append("# Source(s):")
    for s in sources:
        lines.append("#   - " + s)
    lines.append("")
    lines.append("Board:")
    lines.append("  id: " + board_id)
    if fpga_info:
        lines.append("  fpga:")
        for k in ("producer", "family", "part"):
            if k in fpga_info:
                v = fpga_info[k]
                vs = '"{}"'.format(v) if any(c in v for c in " ()") else v
                lines.append("    {}: {}".format(k, vs))
    if default_iostd:
        lines.append("  defaults:")
        lines.append("    iostandard: " + default_iostd)
    lines.append("")
    lines.append("  pinBanks:")

    for bank_name, bank in banks.items():
        rendered = render_pins(bank)
        meta = bank.get("_meta", {})
        overrides = bank.get("_overrides", {})

        if isinstance(rendered, list):
            lines.append("    {}:".format(bank_name))
            lines.append("      pins: " + _yaml_value(rendered))
            for k, v in meta.items():
                lines.append("      {}: {}".format(k, v))
            if overrides:
                lines.append("      overrides:")
                for pin, iostd in overrides.items():
                    lines.append("        {}: {}".format(_scalar(pin), iostd))
        elif isinstance(rendered, OrderedDict):
            lines.append("    {}:".format(bank_name))
            lines.append("      pins:")
            for sub, val in rendered.items():
                if isinstance(val, list):
                    lines.append("        {}: {}".format(sub, _yaml_value(val)))
                else:
                    lines.append("        {}: {}".format(sub, _scalar(val)))
            for k, v in meta.items():
                lines.append("      {}: {}".format(k, v))
            if overrides:
                lines.append("      overrides:")
                for pin, iostd in overrides.items():
                    lines.append("        {}: {}".format(_scalar(pin), iostd))
        else:
            # scalar
            inline = "{ pins: " + _scalar(rendered)
            for k, v in meta.items():
                inline += ", {}: {}".format(k, v)
            inline += " }"
            lines.append("    {}: {}".format(bank_name, inline))

    if unclassified:
        lines.append("")
        lines.append("  # Signals that did not match any classification rule. Either extend")
        lines.append("  # tools/curate_board.py with new rules, or move them into pinBanks by hand.")
        lines.append("  unclassified:")
        for entry in unclassified:
            sig = entry["signal"]
            sig_q = '"{}"'.format(sig) if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sig) else sig
            inline = "{ pin: " + _scalar(entry["pin"])
            if "iostandard" in entry and entry["iostandard"] != default_iostd:
                inline += ", iostandard: " + entry["iostandard"]
            inline += " }"
            lines.append("    {}: {}".format(sig_q, inline))

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FPGA info lookup (board_id -> producer/family/part from boards.yml)
# ---------------------------------------------------------------------------

def load_fpga_info(board_id):
    boards_yml = os.path.join(REPO, "config", "boards.yml")
    if not os.path.exists(boards_yml):
        return {}
    with open(boards_yml) as f:
        data = yaml.safe_load(f)
    for b in data.get("Boards", []):
        if b.get("Id") == board_id:
            info = {}
            if "PartProducer" in b:
                info["producer"] = b["PartProducer"]
            if "PartFamily" in b:
                info["family"] = b["PartFamily"]
            if "Part" in b:
                info["part"] = b["Part"]
            return info
    return {}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def curate(board_id):
    raw_path = os.path.join(RAW_DIR, board_id + ".yml")
    if not os.path.exists(raw_path):
        raise FileNotFoundError(raw_path)
    with open(raw_path) as f:
        raw = yaml.safe_load(f)

    signals = raw.get("signals") or {}
    sources = [s.strip() for s in raw.get("source", "").split(",") if s.strip()]

    iostd_counts = Counter(info["iostandard"] for info in signals.values() if "iostandard" in info)
    default_iostd = iostd_counts.most_common(1)[0][0] if iostd_counts else None

    banks = OrderedDict()
    unclassified = []
    for signal, info in signals.items():
        dest = classify(signal)
        if dest is None:
            unclassified.append({"signal": signal, **info})
        else:
            place(banks, dest, info, default_iostd)

    fpga_info = load_fpga_info(board_id)
    return emit_board_yaml(board_id, fpga_info, default_iostd, banks, unclassified, sources)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board_id")
    p.add_argument("--dry-run", action="store_true",
                   help="Print to stdout instead of writing config/boards/<id>.yml")
    args = p.parse_args(argv)

    out = curate(args.board_id)
    if args.dry_run:
        sys.stdout.write(out)
        return 0
    out_path = os.path.join(OUT_DIR, args.board_id + ".yml")
    with open(out_path, "w") as f:
        f.write(out)
    print("Wrote {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
