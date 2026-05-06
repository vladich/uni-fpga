"""
Mapping of physical board ID -> representative variant dir + all variants.

A variant directory in basics-graphics-music/boards/ corresponds to
"this PCB + this peripheral configuration + this toolchain". We collapse
variants of the same physical PCB into a single board entry and treat the
variant differences as peripheral configurations (TM1638, HDMI/LCD/DVI carrier,
yosys vs vendor toolchain).

The "representative" variant is the directory we harvest the canonical board
pinout from. We pick the simplest variant (no add-on peripherals) when
available, preferring vendor toolchain over yosys (vendor formats carry
IOSTANDARD info that yosys formats omit).
"""

# (board_id, representative_variant, [all_variants])
BOARDS = [
    ("a7_lite_35t", "a7_lite_35t", ["a7_lite_35t"]),
    ("alinx_ax301", "alinx_ax301", ["alinx_ax301"]),
    ("alinx_ax4010", "alinx_ax4010", ["alinx_ax4010"]),
    ("alinx_ax7035b", "alinx_ax7035b", ["alinx_ax7035b"]),
    ("arty_a7", "arty_a7_100", [
        "arty_a7_35", "arty_a7_35_pmod_mic3",
        "arty_a7_100", "arty_a7_100_pmod_mic3",
    ]),
    ("basys3", "basys3", ["basys3"]),
    ("c5gx", "c5gx", ["c5gx"]),
    ("colorlight75b", "colorlight75b_tm1638_ecp5_yosys", ["colorlight75b_tm1638_ecp5_yosys"]),
    ("de0", "de0", ["de0"]),
    ("de0_cv", "de0_cv", ["de0_cv"]),
    ("de0_nano", "de0_nano_vga_pmod", ["de0_nano_vga666", "de0_nano_vga_pmod"]),
    ("de0_nano_soc", "de0_nano_soc_vga_pmod", ["de0_nano_soc_vga666", "de0_nano_soc_vga_pmod"]),
    ("de1", "de1", ["de1"]),
    ("de1_soc", "de1_soc", ["de1_soc"]),
    ("de2", "de2", ["de2"]),
    ("de2_115", "de2_115", ["de2_115"]),
    ("de10_lite", "de10_lite", ["de10_lite", "de10_lite_tm1638_virtual_switches"]),
    ("de10_nano", "de10_nano", ["de10_nano"]),
    ("dk_dev_3c120n", "dk_dev_3c120n", ["dk_dev_3c120n"]),
    ("eclypse_z7", "eclypse_z7", ["eclypse_z7"]),
    ("emooc_cc", "emooc_cc", ["emooc_cc"]),
    # fireant: only has board_specific.sdc, no actual pin assignments — skipped.
    ("ice40hx8k_evb", "ice40hx8k_evb_yosys", ["ice40hx8k_evb_yosys"]),
    ("icebreaker", "icebreaker_bare", [
        "icebreaker_bare",
        "icebreaker_dvi_12b_no_tm1638_yosys", "icebreaker_dvi_12b_tm1638_yosys",
        "icebreaker_dvi_24b_no_tm1638_yosys", "icebreaker_dvi_24b_tm1638_yosys",
        "icebreaker_no_dvi_no_tm1638_yosys", "icebreaker_no_dvi_tm1638_yosys",
    ]),
    ("karnix_ecp5", "karnix_ecp5_yosys", ["karnix_ecp5_yosys"]),
    ("marsohod3gw2", "marsohod3gw2", ["marsohod3gw2"]),
    ("marsohod_mcy112", "marsohod_mcy112", ["marsohod_mcy112"]),
    ("marsohod_mcy316", "marsohod_mcy316", ["marsohod_mcy316"]),
    ("nexys4", "nexys4", ["nexys4"]),
    ("nexys4_ddr", "nexys4_ddr", ["nexys4_ddr"]),
    ("nexys_a7", "nexys_a7", ["nexys_a7", "nexys_a7_50", "nexys_a7_100"]),
    ("omdazz", "omdazz", ["omdazz", "omdazz_pmod_mic3"]),
    ("omdazz_epm570", "omdazz_epm570", [
        "omdazz_epm570", "omdazz_epm570_quartus_13_1_or_older",
    ]),
    ("orangecrab_ecp5", "orangecrab_ecp5_yosys", ["orangecrab_ecp5_yosys"]),
    ("orangepi_msoc", "orangepi_msoc", ["orangepi_msoc"]),
    ("piswords6", "piswords6", ["piswords6"]),
    ("qmtech_kintex_7", "qmtech_kintex_7", ["qmtech_kintex_7"]),
    ("rzrd", "rzrd", ["rzrd", "rzrd_pmod_mic3"]),
    ("saylinx", "saylinx", ["saylinx", "saylinx_pmod_mic3"]),
    ("tang_mega_138k", "tang_mega_138k_lcd_480_272_tm1638", [
        "tang_mega_138k_lcd_480_272_tm1638",
    ]),
    ("tang_mega_138k_pro", "tang_mega_138k_pro_lcd_480_272_no_tm1638", [
        "tang_mega_138k_pro_lcd_480_272_no_tm1638",
        "tang_mega_138k_pro_lcd_480_272_tm1638",
    ]),
    ("tang_nano_4k", "tang_nano_4k_hdmi_no_tm1638", [
        "tang_nano_4k_hdmi_no_tm1638", "tang_nano_4k_hdmi_tm1638",
    ]),
    ("tang_nano_9k", "tang_nano_9k_lcd_480_272_no_tm1638", [
        "tang_nano_9k_hdmi_no_ip_tm1638",
        "tang_nano_9k_hdmi_no_tm1638",
        "tang_nano_9k_hdmi_tm1638",
        "tang_nano_9k_lcd_480_272_no_tm1638",
        "tang_nano_9k_lcd_480_272_no_tm1638_yosys",
        "tang_nano_9k_lcd_480_272_tm1638",
        "tang_nano_9k_lcd_480_272_tm1638_hackathon",
        "tang_nano_9k_lcd_480_272_tm1638_yosys",
        "tang_nano_9k_lcd_800_480_no_tm1638",
        "tang_nano_9k_lcd_800_480_no_tm1638_yosys",
        "tang_nano_9k_lcd_800_480_tm1638",
        "tang_nano_9k_lcd_800_480_tm1638_hackathon",
        "tang_nano_9k_lcd_800_480_tm1638_yosys",
        "tang_nano_9k_lcd_ml6485_no_tm1638_yosys",
        "tang_nano_9k_lcd_ml6485_tm1638_yosys",
        "tang_nano_9k_tm1638_sd",
    ]),
    ("tang_nano_20k", "tang_nano_20k_lcd_480_272_no_tm1638", [
        "tang_nano_20k_hdmi_no_tm1638",
        "tang_nano_20k_hdmi_tm1638",
        "tang_nano_20k_lcd_480_272_no_tm1638",
        "tang_nano_20k_lcd_480_272_tm1638",
        "tang_nano_20k_lcd_800_480_no_tm1638",
        "tang_nano_20k_lcd_800_480_tm1638",
        "tang_nano_20k_lcd_800_480_tm1638_alt",
    ]),
    ("tang_primer_20k_dock", "tang_primer_20k_dock_no_hdmi_no_tm1638", [
        "tang_primer_20k_dock_hdmi_no_tm1638",
        "tang_primer_20k_dock_hdmi_no_tm1638_yosys",
        "tang_primer_20k_dock_hdmi_tm1638",
        "tang_primer_20k_dock_hdmi_tm1638_alt",
        "tang_primer_20k_dock_hdmi_tm1638_yosys",
        "tang_primer_20k_dock_lcd_800_480_no_tm1638",
        "tang_primer_20k_dock_lcd_800_480_tm1638",
        "tang_primer_20k_dock_lcd_800_480_tm1638_alt",
        "tang_primer_20k_dock_no_hdmi_no_tm1638",
        "tang_primer_20k_dock_no_hdmi_tm1638",
    ]),
    ("tang_primer_20k_lite", "tang_primer_20k_lite", ["tang_primer_20k_lite"]),
    ("tang_primer_25k", "tang_primer_25k_pmod_vga", [
        "tang_primer_25k_pmod_hdmi",
        "tang_primer_25k_pmod_hub75e_led_matrix",
        "tang_primer_25k_pmod_hub75e_led_matrix_bright",
        "tang_primer_25k_pmod_vga",
    ]),
    ("terasic_sockit", "terasic_sockit", ["terasic_sockit"]),
    # trion_t20: empty directory, no constraint files — skipped.
    ("zeowaa", "zeowaa", ["zeowaa", "zeowaa_wo_dig_0"]),
    ("zybo_z7", "zybo_z7", ["zybo_z7"]),
]


def by_id():
    return {bid: (rep, variants) for bid, rep, variants in BOARDS}
