/* Coralboard bench — Pico RP2040 firmware, milestone 1 (I2C register map).
 *
 * ##########################################################################
 * # BUILT AND BENCH-VALIDATED FOR RP2040. This file, i2c_target.c and the   #
 * # SDK-free core build cleanly against the pinned Pico SDK 2.1.1 + vmilea   #
 * # i2c_slave with arm-none-eabi-gcc 13.2.1. The UF2 was flashed to a Pico  #
 * # and exercised against the TESTER fixture; see docs/hardware-validation  #
 * # for the exact scope. Electrical timing and real target-device           #
 * # compatibility are still not claimed.                                    #
 * ##########################################################################
 *
 * Design rules this file is written to keep:
 *   - boots unarmed, pins high-impedance until ARM;
 *   - the I2C interrupt path never prints, blocks or allocates;
 *   - lease expiry is driven by a hardware alarm on the IRQ the SDK default
 *     alarm pool actually uses, at higher priority than I2C, so a blocked USB
 *     printf cannot hold the target attached;
 *   - the alarm only parks the pins and seals the core; i2c_slave_deinit runs
 *     later in main context;
 *   - at most 10 000 ms per ARM, enforced by the parser, the core and the
 *     alarm deadline;
 *   - the alarm is scheduled before the bus is enabled, and the attach step
 *     runs with interrupts masked so expiry cannot win the expire-before-
 *     attach race;
 *   - a late alarm callback is ignored unless its captured epoch still
 *     matches the live lease;
 *   - every reply is emitted from the main loop, never from an interrupt.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/irq.h"
#include "hardware/sync.h"
#include "hardware/timer.h"
#include "pico/stdlib.h"
#include "pico/time.h"

#include "core.h"
#include "i2c_target.h"
#include "protocol.h"

#define CB_FW_VERSION "0.2.0"

/* The default alarm pool (used by add_alarm_in_ms) claims hardware alarm
 * PICO_TIME_DEFAULT_ALARM_POOL_HARDWARE_ALARM_NUM, which is not TIMER_IRQ_0.
 * Resolve the real IRQ instead of guessing. */
#define CB_LEASE_ALARM_IRQ \
    hardware_alarm_get_irq_num(PICO_TIME_DEFAULT_ALARM_POOL_HARDWARE_ALARM_NUM)

/* ---------------------------------------------------------------- state -- */

static absolute_time_t lease_deadline;
static volatile alarm_id_t lease_alarm = -1;
static volatile uint32_t lease_epoch;
static volatile bool cleanup_pending;

typedef struct {
    bool armed, sealed, loaded, expired;
    uint8_t addr;
    char tag[CB_TAG_MAX + 1];
    uint32_t crc, cfg, epoch;
    uint32_t emitted, dropped, aborted, drained;
    uint16_t boot_nonce;
    uint32_t lease_left_ms;
} cb_status_snap;

static void snapshot(cb_status_snap *s) {
    cb_core *c = cb_core_instance();
    uint32_t save = save_and_disable_interrupts();
    s->armed = c->armed;
    s->sealed = c->sealed;
    s->loaded = c->loaded;
    s->expired = c->expired;
    s->addr = CB_I2C_ADDR;
    memcpy(s->tag, c->tag, sizeof(s->tag));
    s->crc = c->crc;
    s->cfg = c->cfg;
    s->epoch = c->epoch;
    s->emitted = c->emitted;
    s->dropped = c->dropped;
    s->aborted = c->aborted;
    s->drained = c->drained;
    s->boot_nonce = c->boot_nonce;
    s->lease_left_ms = 0;
    if (c->armed) {
        int64_t us = absolute_time_diff_us(get_absolute_time(), lease_deadline);
        s->lease_left_ms = us > 0 ? (uint32_t)(us / 1000) : 0;
    }
    restore_interrupts(save);
}

/* ----------------------------------------------------------- lease alarm -- */

static int64_t lease_alarm_cb(alarm_id_t id, void *user_data) {
    (void)user_data;
    /* Higher priority than I2C. Do the minimum needed to stop impersonating:
     * park the pads, seal the epoch the alarm was scheduled for. Deinit is
     * deferred. A callback from an older lease must not touch a newer one. */
    cb_core *core = cb_core_instance();
    uint32_t expected = lease_epoch;
    if (id == lease_alarm) lease_alarm = -1;
    if (cb_core_expire_epoch(core, expected)) {
        cb_target_park_fast();
        cleanup_pending = true;
    }
    return 0;
}

static void cancel_lease_alarm(void) {
    alarm_id_t id = lease_alarm;
    lease_alarm = -1;
    lease_epoch = 0;
    if (id >= 0) {
        cancel_alarm(id);
    }
}

static void disarm_target(void) {
    cancel_lease_alarm();
    cleanup_pending = false;
    cb_core_seal(cb_core_instance());
    cb_target_detach();
}

/* --------------------------------------------------------- state helpers -- */

static void print_status(void) {
    cb_status_snap s;
    snapshot(&s);
    printf("OK STATUS armed=%u sealed=%u loaded=%u expired=%u addr=0x%02x "
           "profile=%s crc=%08x cfg=%08x epoch=%08x boot=%04x lease_left_ms=%u "
           "emitted=%u dropped=%u aborted=%u drained=%u\n",
           s.armed ? 1u : 0u, s.sealed ? 1u : 0u, s.loaded ? 1u : 0u,
           s.expired ? 1u : 0u, s.addr, s.loaded ? s.tag : "none",
           s.loaded ? (unsigned)s.crc : 0u, s.loaded ? (unsigned)s.cfg : 0u,
           (unsigned)s.epoch, (unsigned)s.boot_nonce, (unsigned)s.lease_left_ms,
           (unsigned)s.emitted, (unsigned)s.dropped, (unsigned)s.aborted,
           (unsigned)s.drained);
}

static void hex_encode(const uint8_t *in, uint8_t len, char *out) {
    static const char *digits = "0123456789abcdef";
    for (uint8_t i = 0; i < len; i++) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 0x0F];
    }
    out[len * 2] = '\0';
}

/* ------------------------------------------------------------- commands -- */

static void handle_command(const cb_cmd *cmd) {
    cb_core *core = cb_core_instance();

    switch (cmd->type) {
    case CB_CMD_PING:
        printf("OK PING m1-i2c fw=%s\n", CB_FW_VERSION);
        break;

    case CB_CMD_STATUS:
        print_status();
        break;

    case CB_CMD_LOAD: {
        cb_err err = cb_core_load(core, cmd);
        if (err != CB_OK) {
            printf("ERR %s\n", cb_err_name(err));
            break;
        }
        cb_status_snap s;
        snapshot(&s);
        printf("OK LOAD crc=%08x len=%u flags=%02x wrap=%u init=%u cfg=%08x "
               "epoch=%08x\n",
               (unsigned)s.crc, (unsigned)CB_REG_SPACE, (unsigned)cmd->flags,
               (unsigned)cmd->wrap, (unsigned)cmd->initial, (unsigned)s.cfg,
               (unsigned)s.epoch);
        break;
    }

    case CB_CMD_ARM: {
        if (cleanup_pending) {
            /* A previous expiry is still waiting for its main-context deinit. */
            cleanup_pending = false;
            cb_target_detach();
        }
        cb_err err = cb_core_arm(core, cmd->lease_ms);
        if (err != CB_OK) {
            printf("ERR %s\n", cb_err_name(err));
            break;
        }
        uint32_t epoch = core->epoch;

        /* Schedule the expiry alarm *before* the bus is enabled, with
         * interrupts masked so the callback cannot observe a half-installed
         * lease. lease_epoch is visible before the alarm can fire. */
        lease_deadline = make_timeout_time_ms(cmd->lease_ms);
        lease_epoch = epoch;
        uint32_t alarm_save = save_and_disable_interrupts();
        alarm_id_t id = add_alarm_in_ms(cmd->lease_ms, lease_alarm_cb, NULL, false);
        if (id > 0) {
            lease_alarm = id;
        }
        restore_interrupts(alarm_save);
        /* The pinned SDK returns 0 (not a valid id) when the target time has
         * already passed or the pool is full. Treat <= 0 as failure. */
        if (id <= 0) {
            cancel_lease_alarm();
            cb_core_seal(core);
            cb_target_detach();
            printf("ERR TIMER\n");
            break;
        }

        /* Expire-before-attach race: run the attach with interrupts masked and
         * only if the lease is still live. If expiry won, fail closed instead
         * of enabling a target for an epoch that is already sealed. */
        bool attached_ok = false;
        uint32_t attach_save = save_and_disable_interrupts();
        if (core->armed && core->epoch == epoch && !time_reached(lease_deadline)) {
            cb_target_attach();
            attached_ok = true;
        }
        restore_interrupts(attach_save);
        if (!attached_ok) {
            cancel_lease_alarm();
            cb_core_expire_epoch(core, epoch);
            cb_target_detach();
            printf("ERR TIMER\n");
            break;
        }
        cb_status_snap s;
        snapshot(&s);
        printf("OK ARM addr=0x%02x lease_ms=%u cfg=%08x epoch=%08x\n",
               CB_I2C_ADDR, (unsigned)cmd->lease_ms, (unsigned)s.cfg,
               (unsigned)s.epoch);
        break;
    }

    case CB_CMD_DISARM:
        disarm_target();
        {
            cb_status_snap s;
            snapshot(&s);
            printf("OK DISARM sealed=1 emitted=%u dropped=%u aborted=%u "
                   "drained=%u epoch=%08x\n",
                   (unsigned)s.emitted, (unsigned)s.dropped,
                   (unsigned)s.aborted, (unsigned)s.drained,
                   (unsigned)s.epoch);
        }
        break;

    case CB_CMD_TRACE_DRAIN: {
        /* Streaming is allowed while armed. sealed=0 tells the host the trace
         * is still growing; complete=1 is never implied by a drain. */
        uint16_t want = cmd->drain_n;
        uint16_t got = 0;
        cb_trace_ev ev;
        char hex[CB_TRACE_DATA * 2 + 1];
        while (got < want && cb_core_drain(core, &ev)) {
            uint8_t n = (uint8_t)(ev.len > CB_TRACE_DATA ? CB_TRACE_DATA : ev.len);
            hex_encode(ev.data, n, hex);
            printf("T epoch=%08x seq=%u op=%c ptr=%u len=%u data=%s trunc=%u\n",
                   (unsigned)ev.epoch, (unsigned)ev.seq,
                   ev.op == CB_TRACE_OP_READ ? 'r' : 'w', (unsigned)ev.ptr,
                   (unsigned)ev.len, hex, (unsigned)ev.trunc);
            got++;
        }
        cb_status_snap s;
        snapshot(&s);
        printf("OK DRAIN n=%u emitted=%u dropped=%u aborted=%u sealed=%u "
               "epoch=%08x drained=%u\n",
               (unsigned)got, (unsigned)s.emitted, (unsigned)s.dropped,
               (unsigned)s.aborted, s.sealed ? 1u : 0u, (unsigned)s.epoch,
               (unsigned)s.drained);
        break;
    }

    case CB_CMD_TRACE_END: {
        cb_status_snap s;
        snapshot(&s);
        bool complete = cb_core_complete(core);
        printf("OK TRACEEND emitted=%u dropped=%u aborted=%u sealed=%u "
               "complete=%u epoch=%08x cfg=%08x drained=%u\n",
               (unsigned)s.emitted, (unsigned)s.dropped, (unsigned)s.aborted,
               s.sealed ? 1u : 0u, complete ? 1u : 0u, (unsigned)s.epoch,
               (unsigned)s.cfg, (unsigned)s.drained);
        break;
    }

    default:
        printf("ERR BAD_COMMAND\n");
        break;
    }
}

/* ------------------------------------------------------------ main loop -- */

int main(void) {
    cb_core *core = cb_core_instance();
    cb_target_pins_high_z();
    stdio_init_all();

    /* The expiry alarm must be able to preempt the I2C IRQ. The default alarm
     * pool uses hardware alarm PICO_TIME_DEFAULT_ALARM_POOL_HARDWARE_ALARM_NUM,
     * whose IRQ is resolved at compile time -- not TIMER_IRQ_0. Lower numbers
     * are higher priority on RP2040/RP2350. */
    irq_set_priority(CB_LEASE_ALARM_IRQ, 0x00);
    irq_set_priority(I2C0_IRQ, 0x40);

    uint32_t t = time_us_32();
    uint16_t boot_nonce = (uint16_t)(t ^ (t >> 16));
    if (boot_nonce == 0) boot_nonce = 1;
    cb_core_init(core, boot_nonce);

    /* Large command buffers live in .bss: a 1024-byte line plus a ~340-byte
     * parsed command on a 2 KB default stack left no room for the parser and
     * the I2C ISR. PICO_STACK_SIZE is also raised in CMakeLists.txt and the
     * built frames are measured with -fstack-usage. */
    static char line[CB_LINE_MAX];
    static cb_cmd cmd;
    size_t line_len = 0;
    bool line_overflow = false;

    while (true) {
        if (cleanup_pending) {
            cleanup_pending = false;
            cb_target_detach(); /* full deinit, main context, never in IRQ */
        }

        uint32_t irq_save = save_and_disable_interrupts();
        bool still_armed = core->armed;
        restore_interrupts(irq_save);
        if (still_armed && time_reached(lease_deadline)) {
            /* Backstop if the alarm was lost: cancel/clear it first so it can
             * never fire against a later lease, then park and seal. */
            cancel_lease_alarm();
            cb_target_park_fast();
            cb_core_expire(core);
            cleanup_pending = false;
            cb_target_detach();
        }

        int ch = getchar_timeout_us(0); /* non-blocking: stdio_usb stays live */
        if (ch == PICO_ERROR_TIMEOUT) {
            continue;
        }
        if (ch == '\r') {
            continue;
        }
        if (ch == '\n') {
            if (line_overflow) {
                printf("ERR BAD_COMMAND\n");
            } else {
                line[line_len] = '\0';
                cb_err err = cb_parse_line(line, &cmd);
                if (err != CB_OK) {
                    printf("ERR %s\n", cb_err_name(err));
                } else {
                    handle_command(&cmd);
                }
            }
            line_len = 0;
            line_overflow = false;
            continue;
        }
        if (line_len + 1 >= sizeof(line)) {
            line_overflow = true;
            line_len = 0;
            continue;
        }
        line[line_len++] = (char)ch;
    }
}
