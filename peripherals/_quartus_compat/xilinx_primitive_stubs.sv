// =============================================================================
// Stubs for Xilinx-specific synthesis primitives, used by labs that target
// 7-series boards directly (e.g. 5_4_yrv_plus references BUFG).
// These are pass-through modules so non-Xilinx toolchains can still elaborate
// and synthesize the design — Vivado uses its own unisim library and never
// sees these.
// =============================================================================

module BUFG (input  I, output O);
    assign O = I;
endmodule

module IBUFG (input  I, output O);
    assign O = I;
endmodule

module BUFGCE (input  I, input  CE, output O);
    assign O = I & CE;
endmodule
