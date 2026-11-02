/* Coralboard TESTER — second Pico, I2C master test fixture.
 *
 * Scope: bounded ASCII commands over the default SDK USB CDC descriptor
 * (same 2e8a:000a / interface 0+1 / EP 0x81,0x02,0x82 layout as the sensor
 * firmware), and only three explicit bus operations against 0x48.
 *
 * There is deliberately no startup transaction, no scan, no address
 * parameter, no background timer and no I2C interrupt.
 */

#include <stdio.h>
#include <string.h>

#include "hardware/i2c.h"
#include "pico/stdlib.h"

#include "i2c_master.h"
#include "tester_core.h"

static const t_bus_ops bus_ops = {
    tester_bus_write,
    tester_bus_read,
    tester_bus_write_read,
};

int main(void)
{
    /* Before USB exists: GP4/GP5 are SIO inputs, pulls disabled, I2C block
     * reset. The first bus activity can only be an explicit command. */
    tester_pins_park();

    stdio_init_all();
    setvbuf(stdout, NULL, _IONBF, 0);

    static char line[TESTER_LINE_MAX + 1];
    static char reply[TESTER_REPLY_MAX];
    static t_stats stats;
    size_t line_len = 0;
    bool overflow = false;

    t_stats_init(&stats);
    memset(line, 0, sizeof(line));

    for (;;) {
        int ch = getchar_timeout_us(0);
        if (ch == PICO_ERROR_TIMEOUT) {
            continue;
        }
        if (ch == '\r') {
            continue;
        }
        if (ch == '\n') {
            if (overflow) {
                printf("ERR TOO_LONG fw=" TESTER_FW_VERSION "\n");
            } else {
                line[line_len] = '\0';
                t_handle_line(line, &bus_ops, reply, sizeof(reply), &stats);
                printf("%s\n", reply);
            }
            line_len = 0;
            overflow = false;
            continue;
        }
        if (line_len >= TESTER_LINE_MAX) {
            /* Discard the rest of this line, but still answer once. */
            overflow = true;
            line_len = 0;
            continue;
        }
        line[line_len++] = (char)ch;
    }
}
