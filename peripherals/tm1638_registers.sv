/*============================================================================
SPDX-License-Identifier: Apache-2.0

Copyright 2023 Alexander Kirichenko
Copyright 2023 Ruslan Zalata (HCW-132 variation support)

Based on https://github.com/alangarf/tm1638-verilog
Copyright 2017 Alan Garfield
Copyright Contributors to the basics-graphics-music project.
==============================================================================*/

// Note: the `hex` output and internal init array were originally declared
// as multi-dim packed arrays ([w_digit-1:0][w_seg-1:0]). yosys 0.36's
// Verilog frontend does not accept multi-dim packed arrays in port
// declarations, so this version flattens them to a 1-D `[w_digit*w_seg-1:0]`
// vector. Indexing convention preserved: `hex[i*w_seg +: w_seg]` gives the
// digit-`i` segment data, equivalent to the original `hex[i]`.

module tm1638_registers
# (
    parameter                     w_digit = 8,
                                  w_seg   = 8,
                                  r_init  = 0
)
(
    input                                  clk,
    input                                  rst,
    input        [ w_seg   - 1:0]          hgfedcba,
    input        [ w_digit - 1:0]          digit,
    output       [ w_digit*w_seg - 1:0]    hex
);

`ifdef EMULATE_DYNAMIC_7SEG_ON_STATIC_WITHOUT_STICKY_FLOPS
    localparam static_hex = 0;
`else
    localparam static_hex = 1;
`endif

    // Init values for digits 0..7. Stored in ascending order so init_seg[0]
    // is the digit-0 init pattern. Concatenation in original code put digit
    // 0 in the high bits ({d0, d1, ..., d7}); here we store them flat with
    // explicit indexing.
    function [w_seg-1:0] init_seg(input int idx);
        case (idx)
            0: init_seg = 8'b00111111; // 0
            1: init_seg = 8'b00000110; // 1
            2: init_seg = 8'b01011011; // 2
            3: init_seg = 8'b01001111; // 3
            4: init_seg = 8'b01100110; // 4
            5: init_seg = 8'b01101101; // 5
            6: init_seg = 8'b01111101; // 6
            7: init_seg = 8'b00000111; // 7
            default: init_seg = '0;
        endcase
    endfunction

    ////////////// TM1563 data /////////////////

    // HEX registered (unpacked array of vectors — yosys-compatible).
    logic [w_seg - 1:0] r_hex [w_digit];

    genvar i;
    generate
        for (i = 0; i < w_digit; i++) begin : gen_r_hex
            always @( posedge clk or posedge rst)
                if (rst)
                    r_hex[i] <= r_init ? init_seg(i) : '0;
                else if (digit [i])
                    r_hex[i] <= hgfedcba;
        end
    endgenerate

    // HEX combinational
    wire [w_seg - 1:0] c_hex [w_digit];

    generate
        for (i = 0; i < w_digit; i++) begin : assign_registers
            assign c_hex[i] = digit [i] ? hgfedcba : '0;
            // Select combinational or registered HEX (blink or not).
            // Slot the per-digit segment vector into its position in the
            // flat output bus.
            assign hex[i*w_seg +: w_seg] = static_hex ? r_hex[i] : c_hex[i];
        end
    endgenerate

endmodule
