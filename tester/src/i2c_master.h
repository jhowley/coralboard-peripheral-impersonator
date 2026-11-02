/* TESTER I2C master port: I2C0, GP4=SDA, GP5=SCL, 100 kHz, address 0x48.
 *
 * Every public call is one explicit transaction. The block is started only
 * inside that call and is reset + the pins are returned to SIO inputs with
 * pulls disabled on every exit path, including timeout. No scanning, no
 * configurable address, no background traffic.
 */

#ifndef TESTER_I2C_MASTER_H
#define TESTER_I2C_MASTER_H

#include <stddef.h>
#include <stdint.h>

#include "tester_core.h"

/* Put GP4/GP5 back to high-impedance inputs and reset the I2C block.
 * Safe to call before stdio_init_all(). */
void tester_pins_park(void);

t_bus_rc tester_bus_write(const uint8_t *data, size_t len, size_t *sent);
t_bus_rc tester_bus_read(uint8_t *dst, size_t len, size_t *got);
t_bus_rc tester_bus_write_read(const uint8_t *wdata, size_t wlen,
                               uint8_t *rdst, size_t rlen,
                               size_t *wsent, size_t *rgot);

#endif
