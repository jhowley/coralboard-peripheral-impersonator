/* TESTER I2C master port — bounded, explicit transactions only.
 *
 * Why not i2c_write_timeout_us()/i2c_read_timeout_us()? In SDK 2.1.1 the read
 * path waits in `while (!i2c_get_write_available(i2c))` with no timeout check,
 * and neither helper aborts or disables the controller on timeout. It leaves
 * restart_on_next set and only resets that state on the next call. A target
 * that stretches SCL can therefore wedge the SDK helper beyond the requested
 * budget. This port drives the same registers directly and checks a single
 * 90 ms deadline at every wait, then aborts, resets, parks and (after a
 * timeout) clocks a bounded bus-clear so the next command starts from idle.
 */

#include "i2c_master.h"

#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "pico/stdlib.h"
#include "pico/time.h"

#define TESTER_I2C_INST   i2c0
#define TESTER_SDA_PIN    4
#define TESTER_SCL_PIN    5
#define TESTER_I2C_HZ     100000

/* 90 ms transfer budget + <=2 ms abort wait + <=3 ms bus clear keeps a failed
 * command under the 100 ms operation limit. */
#define TESTER_XFER_BUDGET_US 90000u
#define TESTER_ABORT_WAIT_US   2000u
#define TESTER_CLEAR_LOW_US       5u
#define TESTER_CLEAR_HIGH_US    300u

typedef enum {
    WAIT_OK = 0,
    WAIT_TIMEOUT,
    WAIT_ABORT
} wait_result;

static uint64_t deadline_us;

static i2c_hw_t *hw(void)
{
    return i2c_get_hw(TESTER_I2C_INST);
}

static bool expired(void)
{
    return time_us_64() >= deadline_us;
}

/* TX_ABRT is clear-on-read: capture the reason before clearing the flag. */
static bool take_abort(uint32_t *reason)
{
    i2c_hw_t *i = hw();
    if ((i->raw_intr_stat & I2C_IC_RAW_INTR_STAT_TX_ABRT_BITS) == 0) {
        return false;
    }
    *reason = i->tx_abrt_source;
    i->clr_tx_abrt;
    return true;
}

static t_bus_rc abort_result(uint32_t reason)
{
    if (reason & (I2C_IC_TX_ABRT_SOURCE_ABRT_7B_ADDR_NOACK_BITS |
                  I2C_IC_TX_ABRT_SOURCE_ABRT_TXDATA_NOACK_BITS)) {
        return T_BUS_NAK;
    }
    return T_BUS_ERROR;
}

/* Wait for the shift register + FIFO to drain. */
static wait_result wait_tx_empty(uint32_t *reason)
{
    i2c_hw_t *i = hw();
    for (;;) {
        if (take_abort(reason)) {
            return WAIT_ABORT;
        }
        if (i->raw_intr_stat & I2C_IC_RAW_INTR_STAT_TX_EMPTY_BITS) {
            return WAIT_OK;
        }
        if (expired()) {
            return WAIT_TIMEOUT;
        }
        tight_loop_contents();
    }
}

static wait_result wait_stop(uint32_t *reason)
{
    i2c_hw_t *i = hw();
    for (;;) {
        if (take_abort(reason)) {
            return WAIT_ABORT;
        }
        if (i->raw_intr_stat & I2C_IC_RAW_INTR_STAT_STOP_DET_BITS) {
            i->clr_stop_det;
            return WAIT_OK;
        }
        if (expired()) {
            return WAIT_TIMEOUT;
        }
        tight_loop_contents();
    }
}

static void park_pin(uint pin)
{
    gpio_set_function(pin, GPIO_FUNC_SIO);
    gpio_set_dir(pin, GPIO_IN);
    gpio_disable_pulls(pin);
}

void tester_pins_park(void)
{
    /* Release the pads before resetting the block: a block reset while the
     * pads are still muxed to I2C can glitch the lines. */
    park_pin(TESTER_SDA_PIN);
    park_pin(TESTER_SCL_PIN);
    i2c_deinit(TESTER_I2C_INST);
}

/* One START: the transfer begins when the first data_cmd is written. */
static void start_block(void)
{
    i2c_hw_t *i = hw();
    i->enable = 0;
    i->tar = TESTER_I2C_ADDR;
    i->enable = 1;
}

/* The block is already started and the bus is idle. Waits for TX room one
 * byte at a time, checking the deadline and aborts. */
static t_bus_rc write_phase(const uint8_t *data, size_t len, bool nostop,
                            size_t *sent)
{
    i2c_hw_t *i = hw();
    size_t n;
    uint32_t reason = 0;

    *sent = 0;
    for (n = 0; n < len; n++) {
        while (i2c_get_write_available(TESTER_I2C_INST) == 0) {
            if (take_abort(&reason)) {
                *sent = n;
                return abort_result(reason);
            }
            if (expired()) {
                *sent = n;
                return T_BUS_TIMEOUT;
            }
            tight_loop_contents();
        }

        uint32_t cmd = data[n];
        if (n == len - 1 && !nostop) {
            cmd |= I2C_IC_DATA_CMD_STOP_BITS;
        }
        i->data_cmd = cmd;

        wait_result wr = wait_tx_empty(&reason);
        if (wr == WAIT_ABORT) {
            *sent = n; /* byte n was not acknowledged */
            return abort_result(reason);
        }
        if (wr == WAIT_TIMEOUT) {
            *sent = n;
            return T_BUS_TIMEOUT;
        }
        /* The abort may arrive just after TX_EMPTY; do not report success
         * for a byte the target did not acknowledge. */
        if (take_abort(&reason)) {
            *sent = n;
            return abort_result(reason);
        }
        *sent = n + 1;
    }

    if (!nostop) {
        wait_result wr = wait_stop(&reason);
        if (wr == WAIT_ABORT) {
            return abort_result(reason);
        }
        if (wr == WAIT_TIMEOUT) {
            return T_BUS_TIMEOUT;
        }
    }
    return T_BUS_OK;
}

/* The block is already started. restart=true makes the first read byte issue
 * a repeated START instead of a (new) START. */
static t_bus_rc read_phase(uint8_t *dst, size_t len, bool restart, bool stop,
                           size_t *got)
{
    i2c_hw_t *i = hw();
    size_t n;
    uint32_t reason = 0;

    *got = 0;
    for (n = 0; n < len; n++) {
        while (i2c_get_write_available(TESTER_I2C_INST) == 0) {
            if (take_abort(&reason)) {
                *got = n;
                return abort_result(reason);
            }
            if (expired()) {
                *got = n;
                return T_BUS_TIMEOUT;
            }
            tight_loop_contents();
        }

        uint32_t cmd = I2C_IC_DATA_CMD_CMD_BITS;
        if (n == 0 && restart) {
            cmd |= I2C_IC_DATA_CMD_RESTART_BITS;
        }
        if (n == len - 1 && stop) {
            cmd |= I2C_IC_DATA_CMD_STOP_BITS;
        }
        i->data_cmd = cmd;

        for (;;) {
            if (take_abort(&reason)) {
                *got = n;
                return abort_result(reason);
            }
            if (i2c_get_read_available(TESTER_I2C_INST) > 0) {
                dst[n] = (uint8_t)i->data_cmd;
                break;
            }
            if (expired()) {
                *got = n;
                return T_BUS_TIMEOUT;
            }
            tight_loop_contents();
        }
        *got = n + 1;
    }

    if (stop) {
        wait_result wr = wait_stop(&reason);
        if (wr == WAIT_ABORT) {
            return abort_result(reason);
        }
        if (wr == WAIT_TIMEOUT) {
            return T_BUS_TIMEOUT;
        }
    }
    return T_BUS_OK;
}

/* Bounded recovery only after a timeout: abort the master, wait briefly for
 * the FSM, then (in the caller) reset/park. A target that held SCL low may
 * still be stretching; up to nine SCL pulses plus a STOP ask it to release.
 * No address is transmitted and every step is bounded, so this can never
 * turn into an unbounded operation. */
static void abort_master(void)
{
    i2c_hw_t *i = hw();
    uint64_t end = time_us_64() + TESTER_ABORT_WAIT_US;

    /* ABORT has meaning only while ENABLE=1, so set both bits. */
    i->enable = I2C_IC_ENABLE_ENABLE_BITS | I2C_IC_ENABLE_ABORT_BITS;
    while ((i->status & I2C_IC_STATUS_MST_ACTIVITY_BITS) != 0 &&
           time_us_64() < end) {
        tight_loop_contents();
    }
}

static void bus_clear(void)
{
    /* SDA released (input), SCL open-drain output. */
    park_pin(TESTER_SDA_PIN);
    gpio_set_function(TESTER_SCL_PIN, GPIO_FUNC_SIO);
    gpio_put(TESTER_SCL_PIN, 0);
    gpio_set_dir(TESTER_SCL_PIN, GPIO_OUT);

    for (int pulse = 0; pulse < 9; pulse++) {
        /* open-drain low: drive, then release to let the 5.1k pull-up rise */
        gpio_put(TESTER_SCL_PIN, 0);
        gpio_set_dir(TESTER_SCL_PIN, GPIO_OUT);
        busy_wait_us_32(TESTER_CLEAR_LOW_US);
        gpio_set_dir(TESTER_SCL_PIN, GPIO_IN);
        uint64_t end = time_us_64() + TESTER_CLEAR_HIGH_US;
        while (gpio_get(TESTER_SCL_PIN) == 0 && time_us_64() < end) {
            tight_loop_contents();
        }
        if (gpio_get(TESTER_SCL_PIN) == 0) {
            break; /* still held low; report via pins=input, do not spin */
        }
    }

    /* STOP: with SCL low, bring SDA low, release SCL high, then release SDA. */
    gpio_put(TESTER_SCL_PIN, 0);
    gpio_set_dir(TESTER_SCL_PIN, GPIO_OUT);
    busy_wait_us_32(TESTER_CLEAR_LOW_US);
    gpio_put(TESTER_SDA_PIN, 0);
    gpio_set_dir(TESTER_SDA_PIN, GPIO_OUT);
    busy_wait_us_32(TESTER_CLEAR_LOW_US);
    gpio_set_dir(TESTER_SCL_PIN, GPIO_IN);
    busy_wait_us_32(TESTER_CLEAR_LOW_US);
    gpio_set_dir(TESTER_SDA_PIN, GPIO_IN);
    busy_wait_us_32(TESTER_CLEAR_LOW_US);
}

/* Every exit path: optionally abort, reset the block, park the pins. */
static void release_bus(t_bus_rc rc)
{
    if (rc == T_BUS_TIMEOUT) {
        abort_master();
    }
    tester_pins_park();
    if (rc == T_BUS_TIMEOUT) {
        bus_clear();
    }
}

static void begin_bus(void)
{
    i2c_init(TESTER_I2C_INST, TESTER_I2C_HZ);
    gpio_set_function(TESTER_SDA_PIN, GPIO_FUNC_I2C);
    gpio_set_function(TESTER_SCL_PIN, GPIO_FUNC_I2C);
    gpio_disable_pulls(TESTER_SDA_PIN);
    gpio_disable_pulls(TESTER_SCL_PIN);
    deadline_us = time_us_64() + TESTER_XFER_BUDGET_US;
}

t_bus_rc tester_bus_write(const uint8_t *data, size_t len, size_t *sent)
{
    t_bus_rc rc;

    *sent = 0;
    begin_bus();
    start_block();
    rc = write_phase(data, len, false, sent);
    release_bus(rc);
    return rc;
}

t_bus_rc tester_bus_read(uint8_t *dst, size_t len, size_t *got)
{
    t_bus_rc rc;

    *got = 0;
    begin_bus();
    start_block();
    rc = read_phase(dst, len, false, true, got);
    release_bus(rc);
    return rc;
}

t_bus_rc tester_bus_write_read(const uint8_t *wdata, size_t wlen,
                               uint8_t *rdst, size_t rlen,
                               size_t *wsent, size_t *rgot)
{
    t_bus_rc rc;

    *wsent = 0;
    *rgot = 0;
    begin_bus();
    start_block();
    rc = write_phase(wdata, wlen, true, wsent);
    if (rc == T_BUS_OK) {
        rc = read_phase(rdst, rlen, true, true, rgot);
    }
    release_bus(rc);
    return rc;
}
