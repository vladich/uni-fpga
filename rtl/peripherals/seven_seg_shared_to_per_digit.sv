// =============================================================================
// seven_seg_shared_to_per_digit
//
// Adapter for boards whose 7-segment display has independent per-digit segment
// lines (Terasic DE0/DE1/DE2/DE10-Lite — `HEX0[7:0]`, `HEX1[7:0]`, ...).
// The capability presents the shared-segments + digit-select model to user
// code; this adapter fans the currently-selected-digit's segments out to
// every per-digit pin set, with the unselected positions blanked.
//
// `abcdefgh` maps as: bit 7 = decimal point, bits 6..0 = G..A (matches
// basics-graphics-music's swap_bits.svh convention). `digit` is one-hot,
// active-high.
//
// Active-low output polarity: most Altera HEX displays are common-anode and
// active-low. Set `active_low_segments = 1` to invert.
// =============================================================================

module seven_seg_shared_to_per_digit
# (
    parameter int digits              = 6,
    parameter bit active_low_segments = 1'b1
)
(
    input  [          7:0]   abcdefgh,
    input  [digits   - 1:0]  digit,

    // hex_o[d][7:0] is the segment pattern for digit position d.
    output logic [digits-1:0][7:0] hex_o
);

    wire [7:0] seg_polarized = active_low_segments ? ~abcdefgh : abcdefgh;
    wire [7:0] seg_blank     = active_low_segments ? 8'hFF     : 8'h00;

    genvar d;
    generate
        for (d = 0; d < digits; d++) begin : g_digit
            assign hex_o[d] = digit[d] ? seg_polarized : seg_blank;
        end
    endgenerate

endmodule
