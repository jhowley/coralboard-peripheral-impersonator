/* Coralboard bench — SDK-free firmware core. See core.h. */

#include "core.h"

#include <string.h>

#include "core_port.h"

static cb_core g_core;

cb_core *cb_core_instance(void) { return &g_core; }

void cb_core_init(cb_core *c, uint16_t boot_nonce) {
    memset(c, 0, sizeof(*c));
    c->boot_nonce = boot_nonce;
    c->sealed = true;
    c->epoch = (uint32_t)boot_nonce << 16;
}

uint32_t cb_core_config_crc(const char *tag, const uint8_t *image,
                            const uint8_t *wmask, uint8_t flags,
                            uint16_t wrap, uint8_t initial) {
    uint32_t crc = 0xFFFFFFFFu;
    uint8_t tail[4];
    size_t tag_len = strlen(tag) + 1; /* include the NUL separator */
    crc = cb_crc32_update(crc, (const uint8_t *)tag, tag_len);
    crc = cb_crc32_update(crc, image, CB_REG_SPACE);
    crc = cb_crc32_update(crc, wmask, CB_WMASK_BYTES);
    tail[0] = flags;
    tail[1] = (uint8_t)(wrap & 0xFFu);
    tail[2] = (uint8_t)(wrap >> 8);
    tail[3] = initial;
    crc = cb_crc32_update(crc, tail, sizeof(tail));
    return ~crc;
}

static uint32_t next_epoch_unlocked(cb_core *c) {
    c->epoch_counter++;
    c->epoch = ((uint32_t)c->boot_nonce << 16) | (uint32_t)c->epoch_counter;
    return c->epoch;
}

static void reset_xfer_unlocked(cb_core *c) {
    c->xfer_op = 0;
    c->xfer_ptr = 0;
    c->xfer_len = 0;
    c->xfer_store = 0;
    c->xfer_trunc = 0;
    memset(c->xfer_data, 0, sizeof(c->xfer_data));
}

static void reset_trace_unlocked(cb_core *c) {
    c->ring_head = 0;
    c->ring_tail = 0;
    c->ring_count = 0;
    c->emitted = 0;
    c->dropped = 0;
    c->aborted = 0;
    c->drained = 0;
    c->next_seq = 0;
    reset_xfer_unlocked(c);
}

static void trace_push_unlocked(cb_core *c, uint8_t op, uint8_t ptr,
                                uint16_t len, const uint8_t *data,
                                uint8_t trunc) {
    if (c->ring_count >= CB_TRACE_QUEUE) {
        if (c->dropped != UINT32_MAX) c->dropped++;
        return;
    }
    cb_trace_ev *ev = &c->ring[c->ring_head];
    ev->epoch = c->epoch;
    ev->seq = c->next_seq++;
    ev->op = op;
    ev->ptr = ptr;
    ev->len = len;
    ev->trunc = trunc;
    memset(ev->data, 0, CB_TRACE_DATA);
    for (uint8_t i = 0; i < CB_TRACE_DATA && i < len; i++) ev->data[i] = data[i];
    c->ring_head = (uint16_t)((c->ring_head + 1u) % CB_TRACE_QUEUE);
    c->ring_count++;
    if (c->emitted != UINT32_MAX) c->emitted++;
}

cb_err cb_core_load(cb_core *c, const cb_cmd *cmd) {
    uint32_t save = cb_core_lock();
    if (c->armed) {
        cb_core_unlock(save);
        return CB_ERR_ARM_ACTIVE;
    }
    if (c->ring_count > 0) {
        cb_core_unlock(save);
        return CB_ERR_TRACE_PENDING;
    }
    cb_err err = CB_OK;
    if (cmd->wrap < 1 || cmd->wrap > CB_REG_SPACE || cmd->initial >= cmd->wrap) {
        err = CB_ERR_BAD_VALUE;
    } else if (cb_crc32(cmd->image, CB_REG_SPACE) != cmd->crc) {
        err = CB_ERR_CRC;
    }
    if (err != CB_OK) {
        cb_core_unlock(save);
        return err;
    }
    memcpy(c->template_image, cmd->image, CB_REG_SPACE);
    memcpy(c->image, cmd->image, CB_REG_SPACE);
    memcpy(c->wmask, cmd->wmask, CB_WMASK_BYTES);
    memcpy(c->tag, cmd->tag, sizeof(c->tag));
    c->tag[CB_TAG_MAX] = '\0';
    c->crc = cmd->crc;
    c->flags = cmd->flags;
    c->wrap = cmd->wrap;
    c->initial = cmd->initial;
    c->cfg = cb_core_config_crc(c->tag, c->image, c->wmask, c->flags,
                                c->wrap, c->initial);
    c->pointer = c->initial;
    c->loaded = true;
    c->armed = false;
    c->sealed = true;
    c->expired = false;
    reset_trace_unlocked(c);
    next_epoch_unlocked(c);
    cb_core_unlock(save);
    return CB_OK;
}

cb_err cb_core_arm(cb_core *c, uint32_t lease_ms) {
    uint32_t save = cb_core_lock();
    if (!c->loaded) {
        cb_core_unlock(save);
        return CB_ERR_NOT_LOADED;
    }
    if (c->armed) {
        cb_core_unlock(save);
        return CB_ERR_ARM_ACTIVE;
    }
    if (lease_ms == 0 || lease_ms > CB_LEASE_MAX_MS) {
        cb_core_unlock(save);
        return CB_ERR_LEASE_TOO_LONG;
    }
    /* A lease always starts from the loaded template, never from the previous
     * lease's writes. */
    memcpy(c->image, c->template_image, CB_REG_SPACE);
    c->pointer = c->initial;
    c->sealed = false;
    c->expired = false;
    reset_trace_unlocked(c);
    next_epoch_unlocked(c);
    c->armed = true;
    cb_core_unlock(save);
    return CB_OK;
}

static void seal_unlocked(cb_core *c) {
    if (c->armed) {
        if (c->xfer_op != 0) {
            if (c->aborted != UINT32_MAX) c->aborted++;
            reset_xfer_unlocked(c);
        }
        c->armed = false;
    }
    c->sealed = true;
    reset_xfer_unlocked(c);
}

void cb_core_seal(cb_core *c) {
    uint32_t save = cb_core_lock();
    seal_unlocked(c);
    cb_core_unlock(save);
}

bool cb_core_expire(cb_core *c) {
    uint32_t save = cb_core_lock();
    bool fired = c->armed;
    if (fired) {
        seal_unlocked(c);
        c->expired = true;
    }
    cb_core_unlock(save);
    return fired;
}

bool cb_core_expire_epoch(cb_core *c, uint32_t epoch) {
    uint32_t save = cb_core_lock();
    bool fired = c->armed && c->epoch == epoch;
    if (fired) {
        seal_unlocked(c);
        c->expired = true;
    }
    cb_core_unlock(save);
    return fired;
}

bool cb_core_complete(const cb_core *c) {
    uint32_t save = cb_core_lock();
    bool ok = c->loaded && c->sealed && !c->armed &&
              c->dropped == 0 && c->aborted == 0 && c->ring_count == 0 &&
              c->emitted > 0 && c->drained == c->emitted &&
              c->next_seq == c->emitted;
    cb_core_unlock(save);
    return ok;
}

bool cb_core_drain(cb_core *c, cb_trace_ev *out) {
    uint32_t save = cb_core_lock();
    bool have = c->ring_count > 0;
    if (have) {
        *out = c->ring[c->ring_tail];
        c->ring_tail = (uint16_t)((c->ring_tail + 1u) % CB_TRACE_QUEUE);
        c->ring_count--;
        if (c->drained != UINT32_MAX) c->drained++;
    }
    cb_core_unlock(save);
    return have;
}

void cb_core_bus_write(cb_core *c, uint8_t byte) {
    uint32_t save = cb_core_lock();
    if (!c->armed) {
        cb_core_unlock(save);
        return;
    }
    if (c->xfer_op != CB_TRACE_OP_WRITE) {
        c->xfer_op = CB_TRACE_OP_WRITE;
        c->xfer_len = 0;
        c->xfer_store = 0;
        c->xfer_trunc = 0;
        /* D8: the declared modulus applies to pointer initialisation too. */
        c->pointer = (uint8_t)(byte % c->wrap);
        c->xfer_ptr = c->pointer;
        cb_core_unlock(save);
        return; /* first byte of a write segment is the pointer */
    }
    if (cb_wmask_allows(c->wmask, c->pointer)) {
        c->image[c->pointer] = byte;
    }
    if (c->xfer_store < CB_TRACE_DATA) {
        c->xfer_data[c->xfer_store++] = byte;
    } else {
        c->xfer_trunc = 1;
    }
    if (c->xfer_len != UINT16_MAX) c->xfer_len++;
    c->pointer = (uint8_t)((c->pointer + 1u) % c->wrap);
    cb_core_unlock(save);
}

uint8_t cb_core_bus_read(cb_core *c) {
    uint32_t save = cb_core_lock();
    if (!c->armed) {
        cb_core_unlock(save);
        return 0;
    }
    if (c->xfer_op != CB_TRACE_OP_READ) {
        c->xfer_op = CB_TRACE_OP_READ;
        c->xfer_len = 0;
        c->xfer_store = 0;
        c->xfer_trunc = 0;
        c->xfer_ptr = c->pointer;
    }
    uint8_t value = c->image[c->pointer];
    if (c->xfer_store < CB_TRACE_DATA) {
        c->xfer_data[c->xfer_store++] = value;
    } else {
        c->xfer_trunc = 1;
    }
    if (c->xfer_len != UINT16_MAX) c->xfer_len++;
    if (c->flags & CB_LOAD_FLAG_AUTO_INC) {
        c->pointer = (uint8_t)((c->pointer + 1u) % c->wrap);
    }
    cb_core_unlock(save);
    return value;
}

void cb_core_bus_finish(cb_core *c) {
    uint32_t save = cb_core_lock();
    if (c->xfer_op == 0) {
        cb_core_unlock(save);
        return;
    }
    trace_push_unlocked(c, c->xfer_op, c->xfer_ptr, c->xfer_len,
                        c->xfer_data, c->xfer_trunc);
    reset_xfer_unlocked(c);
    cb_core_unlock(save);
}
