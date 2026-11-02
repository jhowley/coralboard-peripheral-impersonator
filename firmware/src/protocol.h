/* Coralboard bench — Pico firmware command protocol (shared, SDK-free).
 *
 * This header and cmd.c depend only on <stdint.h>/<stddef.h>/<string.h> so the
 * parser can be compiled and tested on the host (firmware/host_test). The
 * SDK-facing code lives in main.c / i2c_target.c and is NOT built by that test.
 *
 * Protocol (ASCII, '\n' terminated, bounded to CB_LINE_MAX bytes):
 *   PING
 *   STATUS
 *   LOAD crc=<8 hex> flags=<2 hex> wrap=<1..256> init=<0..255> tag=<..>
 *        wmask=<64 hex> image=<512 hex>
 *   ARM lease_ms=<1..10000>
 *   DISARM
 *   TRACE DRAIN n=<1..64>
 *   TRACE END
 */
#ifndef CB_PROTOCOL_H
#define CB_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define CB_LINE_MAX 1024
#define CB_REG_SPACE 256
#define CB_LEASE_MAX_MS 10000u
#define CB_TRACE_QUEUE 256
#define CB_DRAIN_MAX 64
#define CB_TRACE_DATA 8
#define CB_I2C_ADDR 0x48
#define CB_TAG_MAX 32
#define CB_LOAD_FLAG_AUTO_INC 0x01u
#define CB_WMASK_BYTES (CB_REG_SPACE / 8)

typedef enum {
    CB_CMD_NONE = 0,
    CB_CMD_PING,
    CB_CMD_STATUS,
    CB_CMD_LOAD,
    CB_CMD_ARM,
    CB_CMD_DISARM,
    CB_CMD_TRACE_DRAIN,
    CB_CMD_TRACE_END
} cb_cmd_type;

typedef enum {
    CB_OK = 0,
    CB_ERR_BAD_COMMAND,
    CB_ERR_BAD_VALUE,
    CB_ERR_BAD_IMAGE,
    CB_ERR_CRC,
    CB_ERR_ARM_ACTIVE,
    CB_ERR_NOT_LOADED,
    CB_ERR_LEASE_TOO_LONG,
    CB_ERR_TRACE_PENDING,
    CB_ERR_TIMER
} cb_err;

typedef struct {
    cb_cmd_type type;
    uint32_t crc;                    /* LOAD */
    uint8_t flags;                   /* LOAD */
    uint16_t wrap;                   /* LOAD: pointer wraps at this modulus */
    uint8_t initial;                 /* LOAD: pointer value at ARM */
    uint8_t wmask[CB_WMASK_BYTES];   /* LOAD: one bit per writable register */
    uint8_t image[CB_REG_SPACE];     /* LOAD: decoded register image */
    char tag[CB_TAG_MAX + 1];        /* LOAD: profile tag, NUL terminated */
    uint32_t lease_ms;               /* ARM */
    uint16_t drain_n;                /* TRACE DRAIN */
} cb_cmd;

/* Parse one line (no trailing '\n' required). Returns CB_OK or a cb_err.
 * On error `out` is left zeroed. Never allocates, never blocks. */
cb_err cb_parse_line(const char *line, cb_cmd *out);

/* CRC-32 (IEEE 802.3, same polynomial/seed as zlib crc32). */
uint32_t cb_crc32(const uint8_t *data, size_t len);

/* Incremental form of cb_crc32. Start with 0xFFFFFFFF and invert the result,
 * exactly like zlib.crc32(data, crc). */
uint32_t cb_crc32_update(uint32_t crc, const uint8_t *data, size_t len);

/* Decode exactly `out_len` bytes of lowercase hex. Returns 0 on success. */
int cb_hex_to_bytes(const char *hex, size_t hex_len, uint8_t *out, size_t out_len);

const char *cb_err_name(cb_err err);

/* 1 when the profile allows writes to `reg`, 0 otherwise. */
int cb_wmask_allows(const uint8_t *wmask, uint16_t reg);

#endif /* CB_PROTOCOL_H */
