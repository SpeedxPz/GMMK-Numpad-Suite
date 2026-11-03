/* What our code uses from the stock GMMK Numpad application (firmware 0088), by address.
 * Every entry is documented in .knowledge/firmware-re.md / firmware-update.md.
 *
 * Stock functions are called through pointers with the Thumb bit set (address | 1): the
 * Cortex-M3 only runs Thumb code, and a plain absolute symbol could make the linker emit BLX into
 * ARM state, which faults.
 */
#pragma once
#include <stdint.h>

/* ---- functions ---- */
/* Report command parser: src 0 = USB (buf[0] must be report ID 7), 1 = Bluetooth frame.
 * Copies 256 bytes of the frame into one buffer (0x200010FD) plus globals and raises a flag;
 * the main loop runs the job later. */
#define stock_parse_command ((void (*)(int src, uint8_t *buf))0x080145FDu)
/* First call in main(): clock / system init. Our init hook runs it first. */
#define stock_system_init   ((void (*)(void))0x080115F9u)
/* Main loop call at 0x08014236, right after the job / read dispatch. Our tick runs it first. */
#define stock_loop_tail     ((void (*)(void))0x08008BE1u)

/* ---- command pipeline state (set by the parser, cleared by the main loop) ---- */
/* Write job pending: main loop at 0x080141B2 clears it, THEN runs FUN_08007bbc (the job). */
#define STOCK_JOB_PENDING  (*(volatile uint8_t *)0x2000020Fu)
/* Read request pending (0x83/0x8A/0x8F): main loop clears it, then FUN_08007af4 fills the reply. */
#define STOCK_READ_PENDING (*(volatile uint8_t *)0x20000210u)
#define STOCK_FRAME_LEN    256u  /* bytes the parser copies from the frame */

/* Reply buffer of the read requests: what a USB GET_REPORT for feature report 7 returns. */
#define STOCK_REPLY ((volatile uint8_t *)0x200011FDu)

/* Transport flag: non-zero while connected over Bluetooth. */
#define STOCK_BT_ACTIVE (*(volatile uint8_t *)0x2000001Fu)

/* ---- per-key lighting ---- */
/* RAM per-key table rendered every lighting tick by FUN_0800ce14 (via FUN_0800afb0):
 * 42 entries of [led index, vis, r, g, b]; vis 0 = entry unused, 0xFF = solid, 0xFE.. = breathing
 * (the renderer animates the vis byte in place). Loaded from external flash for the active slot by
 * FUN_0800beec and overwritten by cmd 0x06 (FUN_08010574). */
#define STOCK_KEY_TABLE   ((volatile uint8_t *)0x20001A74u)
#define STOCK_KEY_ENTRIES 42u
/* 1 = LED index is a real LED (31 of the 42 grid positions), 0xFF = no LED. */
#define STOCK_LED_VALID   ((const uint8_t *)0x08015E40u)
/* The renderer only runs FUN_0800ce14 while this is 1 (per-key layer enabled; loaded per slot). */
#define STOCK_KEY_LAYER_ON (*(volatile uint8_t *)0x200002A9u)

/* ---- knob ---- */
/* Volume steps per knob detent (cmd 0x07 [11], per slot; FUN_0800a0c8 adds it per click).
 * 0 = knob silent (Glorious CORE always writes 0). */
#define STOCK_KNOB_STEP (*(volatile uint8_t *)0x200002D3u)
