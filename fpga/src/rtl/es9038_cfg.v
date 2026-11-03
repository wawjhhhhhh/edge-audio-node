//----------------------------------------------------------------------------
// File name:        es9038_cfg
// Descriptions:     Bring-up I2C configuration AND live status read-back for
//                   the external ES9038Q2M.
//
// 2026-10-28 14:50.  Reading the DAC board's own enable LED to steer this
// debug has failed four times in one night: the same LED was interpreted as
// "audio present", "not working", "clock locked" and "muted", and each of
// those readings was later contradicted by the next one.  So this version
// stops asking anyone to interpret a brightness, and removes two variables:
//
//   1. ADDRESS.  The ES9038Q2M sits at 0x48 when its ADDR pins are 00, but
//      this module's ADDR straps are undocumented, and the board's own 8-pin
//      MCU shares the bus.  So each sweep walks 0x48/0x49/0x4A/0x4B, and
//      whichever chip is really there gets configured.
//   2. READOUT.  The answer is encoded as three states that cannot be
//      confused with one another, not as a brightness:
//
//        L15 (alive)  ~1.4 s blink  -> this module is clocked at all
//        H15          OFF           -> nothing answered on any address
//                     ~3 Hz blink   -> a chip answered, but is NOT locked
//                     solid ON      -> a chip answered AND reports DPLL lock
//
// About every 1.37 s, for each of the four addresses:
//   1. write the configuration table below (8 registers);
//   2. read Register 64 (Chip ID and Status) back.
//
// Datasheet facts behind that table (verified in D:/vitis_tmp/es9038q2m.pdf):
//   * Reg 15/16 (volume L/R) power up at 8'd80 == -40 dB, not 0 dB;
//   * Reg 1[3:2] auto_select defaults to 2'b11 (auto DSD/SPDIF/serial), which
//     can mis-read a plain 16-in-32-bit I2S stream and never decode it;
//   * Reg 7[0] is a system mute.
//   The onboard ES8388 decodes the identical nets correctly, so the stream
//   itself is not in doubt -- only what this chip does with it.
//----------------------------------------------------------------------------
module es9038_cfg #(
    parameter CLK_FREQ   = 26'd12_288_000,   // aud_mclk
    parameter I2C_FREQ   = 18'd100_000,      // SCL, well under MCLK/200
    parameter SLAVE_ADDR = 7'b1001000        // 0x48, the ES9038Q2M base addr
)(
    input  wire clk,
    input  wire rst_n,

    output reg  cfg_done,
    output wire scl,
    inout  wire sda,

    output reg  hb = 1'b1,   // 3-state readout (see header); powers up
                             // lit so the pin itself can be identified
    output reg  lock_stat,   // R64[0] as read from the chip that answered
    output wire alive,       // free blink: proves this module is clocked

    // 2026-10-28 22:xx.  The LEDs proved unreadable, so the same information
    // is now a 32-bit bus the PS can fetch over JTAG with one `mrd`:
    //   [3:0]   which of 0x48/0x49/0x4A/0x4B ACKed  (0 = bus totally silent)
    //   [7:4]   which of them reported DPLL lock (R64[0])
    //   [8]     any_lock          [9]  cfg_done
    //   [11:10] sequencer state    [12] live SDA level  [13] live SCL level
    //   [31:24] last R64 read back
    output wire [31:0] stat
);

//--------------------------------------------------------------- register table
// idx 0..7 are writes, idx 8 is the R64 status read-back.
localparam ENTRIES = 4'd9;
localparam NADDR   = 2'd4;          // 0x48 0x49 0x4A 0x4B

reg [3:0]  idx;                     // 0..8  within one chip
reg [1:0]  aidx;                    // 0..3  which candidate address
reg [15:0] entry;

always @(*) begin
    case (idx)
        4'd0: entry = {8'h01, 8'hC0};   // serial_length=2'b11 (32-bit, the chip
                                        // default): datasheet says DATA_CLK must
                                        // be 2*serial_length*FSR = 64*48k = 3.072M,
                                        // which is exactly our BCLK. 0x80 declared
                                        // a non-32-bit word and never matched the
                                        // 64-BCLK frame. I2S, auto_select off, serial
        4'd1: entry = {8'h02, 8'h00};   // all defaults: automute_config=0,
                                        // ch1/ch2_mix_sel=0 (0x04 poked the
                                        // reserved ch2_mix_sel=01)
        4'd2: entry = {8'h04, 8'h00};   // automute_time  = 0 (disabled)
        4'd3: entry = {8'h05, 8'h00};   // automute_level = 0
        4'd4: entry = {8'h06, 8'h42};   // de-emphasis bypass, vol rate dflt
        4'd5: entry = {8'h07, 8'h80};   // filter dflt, system mute cleared
        4'd6: entry = {8'h0F, 8'h0C};   // volume L = -6 dB (power-on -40 dB)
        4'd7: entry = {8'h10, 8'h0C};   // volume R = -6 dB
        default: entry = {8'h40, 8'h00};// R64: chip id + automute + lock
    endcase
end

wire        is_read  = (idx == ENTRIES - 4'd1);
wire [6:0]  cur_addr = SLAVE_ADDR + {5'd0, aidx};
wire [3:0]  onehot   = (4'd1 << aidx);

//--------------------------------------------------------------- I2C master
wire        dri_clk;
wire        i2c_exec;
wire        i2c_done;
wire        i2c_ack;
wire [7:0]  i2c_data_r;

i2c_dri_var #(
    .CLK_FREQ (CLK_FREQ),
    .I2C_FREQ (I2C_FREQ)
) u_i2c_dri (
    .clk        (clk          ),
    .rst_n      (rst_n        ),
    .slave_addr (cur_addr     ),
    .i2c_exec   (i2c_exec     ),
    .bit_ctrl   (1'b0         ),           // 8-bit register address
    .i2c_rh_wl  (is_read      ),           // 0 = write, 1 = read
    .i2c_addr   (entry[15:8]  ),
    .i2c_data_w (entry[7:0]   ),
    .i2c_data_r (i2c_data_r   ),
    .i2c_done   (i2c_done     ),
    .i2c_ack    (i2c_ack      ),
    .scl        (scl          ),
    .sda        (sda          ),
    .dri_clk    (dri_clk      )
);

//--------------------------------------------------------------- sequencer
localparam s_idle = 2'd0, s_exec = 2'd1, s_wait = 2'd2, s_gap = 2'd3;

reg [1:0]  st;
reg [3:0]  c_tmp;               // addresses that ACKed during this sweep
reg [3:0]  l_tmp;               // addresses that reported lock this sweep
reg [3:0]  contact;             // last sweep's result -> drives the LED
reg [3:0]  locked;
reg [23:0] tick;                // 2^24 / 12.288 MHz == 1.365 s
reg [23:0] timeout;
reg [15:0] gap;
reg [7:0]  r64_last;            // last byte read from R64, for the stat bus
wire       sda_i = sda;         // live level of the shared bus pins

// Leave the board's own MCU ~600 ms of clear air before we take the bus.
reg [23:0] por;
wire       por_done = (por == 24'd7_372_800);

// Both LEDs are forced ON for the first 6 s after configuration.  That is a
// pin identity check: it is driven from the flop's power-up value, so it is
// lit even if this module never receives a clock.  Read it like this:
//   lit 6 s, then led1 blinks        -> pins right, module running
//   lit and STAYS lit (never blinks) -> pins right, but no clock: frozen at
//                                       the power-up value, so clk is dead
//   never lit at all                 -> H15/L15 are not these LEDs
reg        hello = 1'b1;
reg [26:0] hcnt  = 27'd0;      // 6 s * 12.288 MHz

wire       any_contact = |contact;
wire       any_lock    = |locked;
wire       blink3      = tick[21];    // 2^21/12.288M = 0.171 s -> ~2.9 Hz

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        por       <= 24'd0;
        tick      <= 24'd0;
        idx       <= 4'd0;
        aidx      <= 2'd0;
        st        <= s_idle;
        cfg_done  <= 1'b0;
        timeout   <= 24'd0;
        gap       <= 16'd0;
        c_tmp     <= 4'd0;
        l_tmp     <= 4'd0;
        contact   <= 4'd0;
        locked    <= 4'd0;
        hb        <= 1'b0;
        lock_stat <= 1'b0;
    end else begin
        if (!por_done) por <= por + 24'd1;
        tick <= tick + 24'd1;
        if (hello) begin
            if (hcnt == 27'd73_728_000) begin
                hello <= 1'b0;
                hcnt  <= 27'd0;
            end else hcnt <= hcnt + 27'd1;
        end
        hb   <= (hello || any_lock) ? 1'b1 : (any_contact ? blink3 : 1'b0);

        case (st)
        s_idle: begin
            if (por_done && tick == 24'd0) begin
                idx   <= 4'd0;
                aidx  <= 2'd0;
                c_tmp <= 4'd0;
                l_tmp <= 4'd0;
                st    <= s_exec;
            end
        end
        s_exec: begin
            st      <= s_wait;
            timeout <= 24'd0;
        end
        s_wait: begin
            timeout <= timeout + 24'd1;
            if (i2c_done) begin
                gap <= 16'd0;
                st  <= s_gap;
                if (!i2c_ack) begin                   // 0 == ACKed
                    c_tmp <= c_tmp | onehot;          // something is there
                    if (is_read) begin
                        lock_stat <= i2c_data_r[0];
                        r64_last  <= i2c_data_r;
                        if (i2c_data_r[0]) l_tmp <= l_tmp | onehot;
                    end
                end
            end else if (timeout > 24'd2_500_000) begin // ~200 ms stuck
                gap <= 16'd0;
                st  <= s_gap;                          // skip this entry
            end
        end
        s_gap: begin
            gap <= gap + 16'd1;
            if (gap > 16'd2457) begin                  // ~200 us
                if (idx == ENTRIES - 4'd1) begin        // finished this chip
                    if (aidx == NADDR - 2'd1) begin     // finished the sweep
                        contact  <= c_tmp;
                        locked   <= l_tmp;
                        cfg_done <= 1'b1;
                        st       <= s_idle;
                    end else begin
                        aidx <= aidx + 2'd1;            // next candidate
                        idx  <= 4'd0;
                        st   <= s_exec;
                    end
                end else begin
                    idx <= idx + 4'd1;
                    st  <= s_exec;
                end
            end
        end
        default: st <= s_idle;
        endcase
    end
end

assign i2c_exec = (st == s_exec);
assign alive     = hello | tick[23];

/* 8 clk-probe + 8 R64 + 2 pad + scl + sda + st[1:0] + cfg_done + any_lock
   + locked[3:0] + contact[3:0] = 32.
   tick[23:16] is the "is this module clocked at all" probe: it changes every
   256 clocks (20.8 us), so two reads that differ prove aud_mclk is arriving. */
assign stat = {tick[23:16], r64_last, 2'b0, scl, sda_i,
               st, cfg_done, any_lock, locked, contact};

endmodule
