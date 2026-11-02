#ifndef CB_I2C_TARGET_H
#define CB_I2C_TARGET_H

/* Adapter around the SDK's pico_i2c_slave library. This is the only place
 * that talks to the SDK's raw I2C byte functions, so main.c stays free of bus
 * details.
 *
 * Pins: GP4 = SDA, GP5 = SCL, 3.3 V only (see docs/wiring.md).
 *
 * FINISH semantics (checked against the official pico-sdk source
 * src/rp2_common/pico_i2c_slave/i2c_slave.c): the library raises
 * I2C_SLAVE_FINISH on STOP_DET, START_DET (repeated START) and TX_ABORT.
 * One FINISH therefore closes one bus segment, not necessarily a whole
 * START..STOP transaction.
 */

#include <stdbool.h>
#include <stdint.h>

#define CB_PIN_SDA 4
#define CB_PIN_SCL 5

void cb_target_pins_high_z(void);
void cb_target_attach(void);
void cb_target_detach(void);

/* Alarm-context fast release: disable the I2C IRQ and park both pads, but do
 * NOT deinit the peripheral. Call cb_target_detach() later from main context
 * to finish cleanup. Safe to call from a higher-priority IRQ. */
void cb_target_park_fast(void);

#endif /* CB_I2C_TARGET_H */
