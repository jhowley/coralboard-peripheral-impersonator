/* Coralboard bench — SDK-free firmware core (state, trace ring, epochs).
 *
 * This file is compiled by both the Pico firmware and the host test harness
 * (firmware/host_test/test_core.c). It contains no stdio, no SDK calls and no
 * allocation. Interrupt locking is provided by core_port.h: on the target it
 * masks interrupts; in the host harness it is a no-op.
 */
#ifndef CB_CORE_H
#define CB_CORE_H

#include <stdbool.h>
#include <stdint.h>

#include "protocol.h"

typedef struct {
    uint32_t epoch;
    uint32_t seq;
    uint8_t op;      /* 1 = read, 2 = write */
    uint8_t ptr;
    uint8_t trunc;
    uint16_t len;
    uint8_t data[CB_TRACE_DATA];
} cb_trace_ev;

typedef struct {
    /* loaded configuration (template is what ARM restores) */
    uint8_t template_image[CB_REG_SPACE];
    uint8_t image[CB_REG_SPACE];
    uint8_t wmask[CB_WMASK_BYTES];
    char tag[CB_TAG_MAX + 1];
    uint32_t crc;
    uint32_t cfg;
    uint8_t flags;
    uint16_t wrap;
    uint8_t initial;

    /* identity */
    uint16_t boot_nonce;
    uint16_t epoch_counter;
    uint32_t epoch;

    bool loaded;
    bool armed;
    bool sealed;
    bool expired;
    uint8_t pointer;

    /* bounded trace ring */
    cb_trace_ev ring[CB_TRACE_QUEUE];
    uint16_t ring_head;
    uint16_t ring_tail;
    uint16_t ring_count;
    uint32_t emitted;
    uint32_t dropped;
    uint32_t aborted;
    uint32_t drained;
    uint32_t next_seq;

    /* per-segment accumulation, interrupt context only */
    uint8_t xfer_op;
    uint8_t xfer_ptr;
    uint8_t xfer_trunc;
    uint16_t xfer_len;
    uint16_t xfer_store;
    uint8_t xfer_data[CB_TRACE_DATA];
} cb_core;

#define CB_TRACE_OP_READ 1u
#define CB_TRACE_OP_WRITE 2u

/* Single device instance used by the firmware. Host tests may use it too. */
cb_core *cb_core_instance(void);

/* Zero all state and fix the boot nonce. */
void cb_core_init(cb_core *c, uint16_t boot_nonce);

/* CRC over tag\0 || image[256] || wmask[32] || flags || wrap_le16 || initial. */
uint32_t cb_core_config_crc(const char *tag, const uint8_t *image,
                            const uint8_t *wmask, uint8_t flags,
                            uint16_t wrap, uint8_t initial);

/* LOAD: only while disarmed and only when no undrained events remain.
 * Starts a new epoch, clears the trace and installs the template. */
cb_err cb_core_load(cb_core *c, const cb_cmd *cmd);

/* ARM: new epoch, image restored from template, pointer = initial,
 * sequence/counters cleared, sealed=false. Lease timing is the caller's. */
cb_err cb_core_arm(cb_core *c, uint32_t lease_ms);

/* DISARM: seal, count an in-flight transfer as aborted, reset transfer state. */
void cb_core_seal(cb_core *c);

/* Lease expiry: same as seal but only when armed. Returns true if it fired. */
bool cb_core_expire(cb_core *c);

/* Lease expiry guarded by the epoch the alarm was scheduled for. A late
 * callback that belongs to an older lease returns false and mutates nothing,
 * so it can never seal a newer epoch. */
bool cb_core_expire_epoch(cb_core *c, uint32_t epoch);

/* Completeness of the current epoch (host still requires seq/epoch checks). */
bool cb_core_complete(const cb_core *c);

/* Pop one event and advance the drained cursor. Returns false when empty. */
bool cb_core_drain(cb_core *c, cb_trace_ev *out);

/* Bus callbacks. Call from the I2C ISR only. */
void cb_core_bus_write(cb_core *c, uint8_t byte);
uint8_t cb_core_bus_read(cb_core *c);
void cb_core_bus_finish(cb_core *c);

#endif /* CB_CORE_H */
