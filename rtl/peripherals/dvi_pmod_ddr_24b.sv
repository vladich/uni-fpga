// =============================================================================
// dvi_pmod_ddr_24b
//
// Driver for the 1BitSquared DVI 24b Pmod carrier (used on icebreaker DVI
// 24-bit configurations). The carrier connects to two adjacent Pmod headers
// (16 pins total) and uses DDR (double-data-rate) IO blocks to carry 16 logical
// signals per pin per pixel clock. The bit packing is taken from
// basics-graphics-music's icebreaker_dvi_24b_tm1638_yosys/board_specific_top.sv.
//
// TODO: this module is a STUB — it generates VGA-style timing and packs the
// 8-8-8 RGB into the rising/falling edge halves expected by the carrier, but
// the SB_IO DDR primitives are NOT instantiated here (they are iCE40-specific
// and require a vendor or yosys-specific path). The actual DDR IO needs to be
// wired in the codegen-generated top.sv, where SB_IO can be reached.
//
// Until the codegen wires SB_IO, this stub still produces a syntactically
// valid module signature so YAML validation passes.
// =============================================================================

module dvi_pmod_ddr_24b
# (
    parameter int clk_mhz   = 12,
    parameter int pixel_mhz = 25
)
(
    input                clk,
    input                rst,

    output logic [9:0]   x,
    output logic [9:0]   y,

    input        [7:0]   red_i,
    input        [7:0]   green_i,
    input        [7:0]   blue_i,

    // The 16 physical pins (top and bottom Pmod headers).
    output logic [7:0]   pmod_a,
    output logic [7:0]   pmod_b
);

    // TODO: implement VGA-style raster timing for x/y, then pack
    // {rising_edge_data, falling_edge_data} per the icebreaker DVI Pmod's
    // bit-mapping and drive the SB_IO instances in the parent module.

    assign x      = '0;
    assign y      = '0;
    assign pmod_a = '0;
    assign pmod_b = '0;

endmodule
