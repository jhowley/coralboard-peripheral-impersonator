/* Pico I2C-target adapter (pico_i2c_slave).
 *
 * API checked against the pinned sources:
 *   pico-sdk/src/rp2_common/hardware_i2c/i2c.c
 *   pico_i2c_slave/i2c_slave/i2c_slave.c
 *   pico_i2c_slave/example_mem/example_mem.c
 * FINISH is delivered on STOP_DET, START_DET and TX_ABRT (not only STOP).
 */

#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "hardware/irq.h"
/* Official pico-sdk exposes pico/i2c_slave.h; the vmilea checkout exposes
 * i2c_slave.h. Both have the same three-event API. */
#if defined(__has_include)
#  if __has_include("pico/i2c_slave.h")
#    include "pico/i2c_slave.h"
#  else
#    include "i2c_slave.h"
#  endif
#else
#  include "i2c_slave.h"
#endif

#include "core.h"
#include "i2c_target.h"
#include "protocol.h"

/* GP4/GP5 are I2C0 on every RP2040/RP2350 board pinout. */
#define CB_I2C_INST i2c0
#define CB_I2C_IRQ I2C0_IRQ
#define CB_I2C_BAUD 100000u

static bool attached;

static void bus_handler(i2c_inst_t *i2c, i2c_slave_event_t event) {
    cb_core *core = cb_core_instance();
    switch (event) {
    case I2C_SLAVE_RECEIVE:
        cb_core_bus_write(core, i2c_read_byte_raw(i2c));
        break;
    case I2C_SLAVE_REQUEST:
        i2c_write_byte_raw(i2c, cb_core_bus_read(core));
        break;
    case I2C_SLAVE_FINISH:
        cb_core_bus_finish(core);
        break;
    default:
        break;
    }
}

void cb_target_pins_high_z(void) {
    /* GPIO_FUNC_NULL detaches the pad from every peripheral; input direction
     * plus disabled pulls leaves it genuinely high-impedance, so the bench
     * cannot hold SDA/SCL low once disarmed. */
    gpio_set_function(CB_PIN_SDA, GPIO_FUNC_NULL);
    gpio_set_function(CB_PIN_SCL, GPIO_FUNC_NULL);
    gpio_disable_pulls(CB_PIN_SDA);
    gpio_disable_pulls(CB_PIN_SCL);
    gpio_set_dir(CB_PIN_SDA, GPIO_IN);
    gpio_set_dir(CB_PIN_SCL, GPIO_IN);
    attached = false;
}

void cb_target_park_fast(void) {
    /* Called from the lease-expiry alarm, which has higher priority than the
     * I2C IRQ. Release the bus immediately and leave deinit to main context. */
    irq_set_enabled(CB_I2C_IRQ, false);
    gpio_set_function(CB_PIN_SDA, GPIO_FUNC_NULL);
    gpio_set_function(CB_PIN_SCL, GPIO_FUNC_NULL);
    gpio_disable_pulls(CB_PIN_SDA);
    gpio_disable_pulls(CB_PIN_SCL);
    gpio_set_dir(CB_PIN_SDA, GPIO_IN);
    gpio_set_dir(CB_PIN_SCL, GPIO_IN);
}

void cb_target_attach(void) {
    /* Internal pull-ups are ~50 kOhm and are only a backstop: the fixture must
     * provide real pull-ups (see docs/wiring.md). */
    gpio_disable_pulls(CB_PIN_SDA);
    gpio_disable_pulls(CB_PIN_SCL);
    gpio_set_function(CB_PIN_SDA, GPIO_FUNC_I2C);
    gpio_set_function(CB_PIN_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(CB_PIN_SDA);
    gpio_pull_up(CB_PIN_SCL);
    /* Follow the pinned library's documented order: i2c_init() performs the
     * peripheral reset/unreset, IC_CON setup, FIFO watermarks, DMA and the
     * baudrate (SDA hold / spike filter) that i2c_slave_init() does not.
     * i2c_slave_init() then switches the block to slave mode and unmasks the
     * IRQ. Calling only i2c_slave_init() left reset SDA-hold/spike values. */
    (void)i2c_init(CB_I2C_INST, CB_I2C_BAUD);
    /* i2c_init() reprograms the block; keep the I2C IRQ below the lease alarm
     * even if a future SDK touches NVIC state here. */
    irq_set_priority(CB_I2C_IRQ, 0x40);
    i2c_slave_init(CB_I2C_INST, CB_I2C_ADDR, bus_handler);
    attached = true;
}

void cb_target_detach(void) {
    if (attached) {
        i2c_slave_deinit(CB_I2C_INST);
    }
    /* Release the pins even if attach() never completed or fast-park ran. */
    cb_target_pins_high_z();
}
