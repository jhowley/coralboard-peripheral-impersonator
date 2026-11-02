/* TESTER command core: strict ASCII parser, bus dispatch, reply formatting.
 *
 * Deliberately SDK-free and allocation-free so the same code is compiled into
 * the RP2040 firmware and into the host-side behavioural tests. The core owns
 * every reply string; main.c only moves bytes to USB and i2c_master.c only
 * moves bytes on the bus.
 */

#ifndef TESTER_CORE_H
#define TESTER_CORE_H

#include <stddef.h>
#include <stdint.h>

#define TESTER_FW_VERSION "1.0.0"
#define TESTER_ROLE       "TESTER"
#define TESTER_I2C_ADDR   0x48
#define TESTER_MAX_BYTES  32
#define TESTER_LINE_MAX   256
#define TESTER_REPLY_MAX  256

typedef enum {
    T_BUS_OK = 0,
    T_BUS_NAK,      /* address or data not acknowledged */
    T_BUS_TIMEOUT,  /* bounded transfer budget expired */
    T_BUS_ERROR     /* any other hardware abort */
} t_bus_rc;

/* One call per command. Ports report the number of bytes actually moved;
 * a success return with a short count is a contract violation. */
typedef struct {
    t_bus_rc (*write)(const uint8_t *data, size_t len, size_t *sent);
    t_bus_rc (*read)(uint8_t *dst, size_t len, size_t *got);
    t_bus_rc (*write_read)(const uint8_t *wdata, size_t wlen,
                           uint8_t *rdst, size_t rlen,
                           size_t *wsent, size_t *rgot);
} t_bus_ops;

typedef struct {
    uint32_t ops;       /* bus commands attempted */
    uint32_t ok;        /* completed transactions */
    uint32_t err;       /* failed transactions (nak + timeout + bus) */
    uint32_t timeouts;
    const char *last;   /* none | ok | nak | timeout | bus */
} t_stats;

void t_stats_init(t_stats *st);

/* Handle one command line (no newline). Writes exactly one NUL-terminated
 * reply line into out and returns its length. Never calls the bus for PING,
 * STATUS or invalid input. */
size_t t_handle_line(const char *line, const t_bus_ops *bus,
                     char *out, size_t cap, t_stats *st);

#endif
