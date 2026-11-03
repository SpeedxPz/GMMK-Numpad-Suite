/* GMMK Numpad suite firmware add-on, hooked into the stock 0088 application.
 *
 * Hooks (patched BL instructions in the stock app, see fw/build.py):
 *   0x080140A0  main(): bl FUN_080115f8         -> patch_init
 *   0x08012A02  USB:    bl FUN_080145fc (0, buf) -> patch_command
 *   0x080141AE  BT:     bl FUN_080145fc (1, buf) -> patch_command
 *   0x08014236  loop:   bl FUN_08008be0         -> patch_tick
 *
 * Features (bits reported by command 0x60):
 *   0x01 info          0x60 replies 07 60 'G' 'S' <version> <features> <boots> <dropped> <max depth>
 *   0x02 live_leds     0x61 set / 0x62 clear per-key colours in RAM only (no flash write)
 *   0x04 command_queue frames that arrive while the stock job/read is pending are queued instead
 *                      of overwriting the stock parser's single buffer (the "commands overwrite each
 *                      other" race: an apply for slot 1/1 + a write for 1/2 50 ms later activated 1/2)
 *   0x08 knob_fix      knob step 0 (CORE's properties [11]=0, which silences the knob) runs as 1
 *   0x10 peek          0x63 reads RAM / flash into the reply buffer (diagnostics)
 *
 * Our code lives at 0x08016800 (free flash) and our variables at 0x20005000 (RAM the stock app
 * never touches: it ends at 0x200049E8).
 */
#include "stock.h"

#define PATCH_VERSION 4
#define FEATURES      0x1Fu

#define CMD_SUITE_INFO 0x60  /* 0x60..0x7F are unused by the stock parser */
#define CMD_LIVE_SET   0x61
#define CMD_LIVE_CLEAR 0x62
#define CMD_PEEK       0x63

#define LIVE_BREATHE 0x01u   /* 0x61 [4] mode bits */
#define LIVE_REMOVE  0x02u

#define QUEUE_LEN 8

extern uint32_t __data_load[], __data_start[], __data_end[], __bss_start[], __bss_end[];

/* ---- interrupts: USB frames arrive in the USB interrupt, Bluetooth frames in the main loop ---- */
static inline uint32_t irq_save(void)
{
    uint32_t primask;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(primask) : : "memory");
    return primask;
}

static inline void irq_restore(uint32_t primask)
{
    __asm volatile("msr primask, %0" : : "r"(primask) : "memory");
}

static void copy(volatile uint8_t *d, const volatile uint8_t *s, uint32_t n)
{
    while (n--)
        *d++ = *s++;
}

/* ---- state (.bss, zeroed by patch_init) ---- */
static uint32_t boot_count;

struct frame {
    uint8_t src;
    uint8_t data[STOCK_FRAME_LEN];
};
static struct frame queue[QUEUE_LEN];
static uint8_t q_head, q_count, q_max, q_dropped;
/* Set when a dispatched frame raised a stock flag; cleared by the tick once the main loop has
 * run the job. The stock loop clears its own flag BEFORE running the job, so its flag alone would
 * let a USB frame overwrite the buffer while the job is still reading it. */
static uint8_t armed;

static uint8_t live_used[STOCK_KEY_ENTRIES];      /* entries we changed */
static uint8_t live_backup[STOCK_KEY_ENTRIES][5]; /* their content before our first change */
static uint8_t live_value[STOCK_KEY_ENTRIES][5];  /* what we wrote last */
static uint8_t live_gate_saved, live_gate_backup;

void patch_init(void)
{
    stock_system_init();  /* the call we replaced */
    /* The stock startup only initialises its own RAM; ours above 0x200049E8 is random after reset. */
    for (uint32_t *s = __data_load, *d = __data_start; d < __data_end;)
        *d++ = *s++;
    for (uint32_t *d = __bss_start; d < __bss_end;)
        *d++ = 0;
    boot_count++;
}

/* ---- 0x60: version / features / queue diagnostics ---- */
static void suite_info(void)
{
    volatile uint8_t *r = STOCK_REPLY;
    r[0] = 7;
    r[1] = CMD_SUITE_INFO;
    r[2] = 'G';
    r[3] = 'S';
    r[4] = PATCH_VERSION;
    r[5] = FEATURES;
    r[6] = (uint8_t)boot_count;
    r[7] = q_dropped;
    r[8] = q_max;
}

/* ---- 0x63: read memory (diagnostics) ----
 * Frame [4..7] address (little endian), [8] length (max 240). Reply: 07 63 <addr x4> <len> <data>.
 * Only SRAM and the application flash can be read; anything else answers with length 0. */
static void peek(const uint8_t *f)
{
    uint32_t addr = f[4] | (f[5] << 8) | (f[6] << 16) | ((uint32_t)f[7] << 24);
    uint32_t len = f[8] > 240 ? 240 : f[8];
    int ram = addr >= 0x20000000u && addr + len <= 0x20007000u;
    int rom = addr >= 0x08000000u && addr + len <= 0x08020000u;
    volatile uint8_t *r = STOCK_REPLY;
    r[0] = 7;
    r[1] = CMD_PEEK;
    copy(r + 2, f + 4, 4);
    r[6] = (ram || rom) ? (uint8_t)len : 0;
    if (ram || rom)
        copy(r + 7, (const volatile uint8_t *)addr, len);
}

/* ---- 0x61 / 0x62: live LEDs (RAM only) ---- */
static int find_entry(uint8_t led)
{
    for (uint32_t i = 0; i < STOCK_KEY_ENTRIES; i++) {
        volatile uint8_t *e = STOCK_KEY_TABLE + i * 5;
        if ((e[1] != 0 || live_used[i]) && e[0] == led)
            return (int)i;
    }
    for (uint32_t i = 0; i < STOCK_KEY_ENTRIES; i++) {  /* no entry for this LED yet: a free one */
        volatile uint8_t *e = STOCK_KEY_TABLE + i * 5;
        if (e[1] == 0 && !live_used[i])
            return (int)i;
    }
    return -1;
}

/* A profile/layer switch reloads the table (FUN_0800beec) and cmd 0x06 overwrites it, so entries we
 * changed may hold someone else's colour now. Forget those, or every switch would leave more entries
 * marked as ours until no free one is left (seen: after 5 switches almost no live LED landed). The
 * vis byte is only checked for "cleared": the renderer animates it for breathing entries. */
static void live_forget_stale(void)
{
    int any = 0;
    for (uint32_t i = 0; i < STOCK_KEY_ENTRIES; i++) {
        if (!live_used[i])
            continue;
        volatile uint8_t *e = STOCK_KEY_TABLE + i * 5;
        const uint8_t *v = live_value[i];
        if (e[0] != v[0] || e[2] != v[2] || e[3] != v[3] || e[4] != v[4] || (v[1] != 0 && e[1] == 0))
            live_used[i] = 0;
        else
            any = 1;
    }
    if (!any)
        live_gate_saved = 0;  /* nothing of ours left: the next set saves the current gate again */
}

/* Frame: [1]=0x61 [2],[3]=profile/layer (ignored) [4]=mode [5]=count, then count x [led, r, g, b].
 * mode bit0 = breathing, bit1 = remove the override (the LED shows the effect again). */
static void live_set(const uint8_t *f)
{
    uint8_t mode = f[4], count = f[5];
    if (count > (STOCK_FRAME_LEN - 6) / 4)
        count = (STOCK_FRAME_LEN - 6) / 4;
    live_forget_stale();
    if (!live_gate_saved) {
        live_gate_backup = STOCK_KEY_LAYER_ON;
        live_gate_saved = 1;
    }
    for (uint32_t n = 0; n < count; n++) {
        const uint8_t *p = f + 6 + n * 4;
        uint8_t led = p[0];
        if (led >= STOCK_KEY_ENTRIES || STOCK_LED_VALID[led] != 1)
            continue;
        int i = find_entry(led);
        if (i < 0)
            continue;
        volatile uint8_t *e = STOCK_KEY_TABLE + i * 5;
        if (!live_used[i]) {
            copy(live_backup[i], e, 5);
            live_used[i] = 1;
        }
        uint8_t v[5] = {led, (mode & LIVE_REMOVE) ? 0 : (mode & LIVE_BREATHE) ? 0xFE : 0xFF, p[1], p[2], p[3]};
        copy(e, v, 5);
        copy(live_value[i], v, 5);
    }
    STOCK_KEY_LAYER_ON = 1;  /* make sure the per-key layer renders */
}

/* Undo our changes. Entries that no longer hold what we wrote (the table was reloaded by a profile
 * switch or overwritten by cmd 0x06 since) are left alone. The vis byte is not compared, because
 * the renderer animates it for breathing entries. */
static void live_clear(void)
{
    for (uint32_t i = 0; i < STOCK_KEY_ENTRIES; i++) {
        if (!live_used[i])
            continue;
        volatile uint8_t *e = STOCK_KEY_TABLE + i * 5;
        if (e[0] == live_value[i][0] && e[2] == live_value[i][2] && e[3] == live_value[i][3] &&
            e[4] == live_value[i][4])
            copy(e, live_backup[i], 5);
        live_used[i] = 0;
    }
    if (live_gate_saved) {
        STOCK_KEY_LAYER_ON = live_gate_backup;
        live_gate_saved = 0;
    }
}

/* ---- command dispatch + queue ---- */
static int stock_busy(void)
{
    return armed || STOCK_JOB_PENDING || STOCK_READ_PENDING;
}

static void dispatch(int src, uint8_t *buf)
{
    /* Same acceptance rule as the stock parser: USB frames carry report ID 7 in buf[0]. */
    if (src == 1 || (src == 0 && buf[0] == 7)) {
        switch (buf[1]) {
        case CMD_SUITE_INFO:
            suite_info();
            return;
        case CMD_LIVE_SET:
            live_set(buf);
            return;
        case CMD_LIVE_CLEAR:
            live_clear();
            return;
        case CMD_PEEK:
            peek(buf);
            return;
        }
    }
    stock_parse_command(src, buf);
    if (STOCK_JOB_PENDING || STOCK_READ_PENDING)
        armed = 1;
}

void patch_command(int src, uint8_t *buf)
{
    uint32_t primask = irq_save();
    if (q_count == 0 && !stock_busy()) {
        dispatch(src, buf);
    } else if (q_count < QUEUE_LEN) {  /* keep order: once anything waits, everything waits */
        struct frame *f = &queue[(q_head + q_count) % QUEUE_LEN];
        f->src = (uint8_t)src;
        copy(f->data, buf, STOCK_FRAME_LEN);
        q_count++;
        if (q_count > q_max)
            q_max = q_count;
    } else if (q_dropped < 255) {
        q_dropped++;
    }
    irq_restore(primask);
}

/* Main loop, right after the stock job / read dispatch: feed queued frames. */
void patch_tick(void)
{
    stock_loop_tail();  /* the call we replaced */
    if (STOCK_KNOB_STEP == 0)  /* CORE writes 0 there, which silences the knob */
        STOCK_KNOB_STEP = 1;
    uint32_t primask = irq_save();
    if (!STOCK_JOB_PENDING && !STOCK_READ_PENDING)
        armed = 0;  /* this loop pass already ran the job that armed us */
    while (q_count && !stock_busy()) {
        struct frame *f = &queue[q_head];
        q_head = (uint8_t)((q_head + 1) % QUEUE_LEN);
        q_count--;
        dispatch(f->src, f->data);
    }
    irq_restore(primask);
}
