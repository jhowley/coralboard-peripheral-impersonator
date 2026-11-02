/* Native behavioural tests for the TESTER command core.
 *
 * These tests are written BEFORE src/tester_core.c exists. They pin the
 * observable contract: exact reply lines, strict parsing, one bus call per
 * bus command, no bus call for PING/STATUS/bad input, and no success line
 * when the bus reports a short count.
 *
 * Build/run: tests/run_tests.sh
 */

#include <stdio.h>
#include <string.h>

#include "tester_core.h"

static int failures;
static int checks;

#define CHECK(cond) do { \
    checks++; \
    if (!(cond)) { \
        failures++; \
        printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    } \
} while (0)

/* ------------------------------------------------------------- fake bus -- */

static int bus_calls;
static uint8_t seen_w[TESTER_MAX_BYTES];
static size_t seen_wlen;
static size_t seen_rlen;
static t_bus_rc write_rc, read_rc;
static size_t write_sent, read_got;
static uint8_t read_data[TESTER_MAX_BYTES];

static void bus_reset(void)
{
    bus_calls = 0;
    memset(seen_w, 0, sizeof(seen_w));
    seen_wlen = 0;
    seen_rlen = 0;
    write_rc = T_BUS_OK;
    read_rc = T_BUS_OK;
    write_sent = 0;
    read_got = 0;
    memset(read_data, 0, sizeof(read_data));
}

static t_bus_rc fake_write(const uint8_t *data, size_t len, size_t *sent)
{
    bus_calls++;
    seen_wlen = len;
    memcpy(seen_w, data, len);
    if (write_rc == T_BUS_OK) {
        *sent = len;
    } else {
        *sent = write_sent;
    }
    return write_rc;
}

static t_bus_rc fake_read(uint8_t *dst, size_t len, size_t *got)
{
    bus_calls++;
    seen_rlen = len;
    if (read_rc == T_BUS_OK) {
        memcpy(dst, read_data, len);
        *got = len;
    } else {
        *got = read_got;
    }
    return read_rc;
}

static t_bus_rc fake_write_read(const uint8_t *w, size_t wlen, uint8_t *r,
                                size_t rlen, size_t *wsent, size_t *rgot)
{
    bus_calls++;
    seen_wlen = wlen;
    memcpy(seen_w, w, wlen);
    seen_rlen = rlen;
    if (write_rc != T_BUS_OK) {
        *wsent = write_sent;
        *rgot = 0;
        return write_rc;
    }
    *wsent = wlen;
    if (read_rc != T_BUS_OK) {
        *rgot = read_got;
        return read_rc;
    }
    memcpy(r, read_data, rlen);
    *rgot = rlen;
    return T_BUS_OK;
}

static const t_bus_ops fake_ops = {
    fake_write, fake_read, fake_write_read,
};

/* --------------------------------------------------------------- helper -- */

static t_stats stats;

static void call(const char *line, const char *want, int want_calls)
{
    char reply[TESTER_REPLY_MAX];
    bus_calls = 0;
    seen_wlen = 0;
    seen_rlen = 0;
    memset(reply, 0, sizeof(reply));
    t_handle_line(line, &fake_ops, reply, sizeof(reply), &stats);
    if (strcmp(reply, want) != 0) {
        failures++;
        printf("FAIL line=%s\n  got  %s\n  want %s\n", line, reply, want);
    }
    checks++;
    if (bus_calls != want_calls) {
        failures++;
        printf("FAIL line=%s bus_calls=%d want=%d\n", line, bus_calls, want_calls);
    }
    checks++;
}

static void fresh_stats(void)
{
    memset(&stats, 0, sizeof(stats));
}

/* ----------------------------------------------------------------- tests -- */

static void test_identity_and_parser(void)
{
    fresh_stats();
    bus_reset();
    call("PING", "OK PING TESTER fw=1.0.0", 0);
    call("ping", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("PING now", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("HELLO", "ERR BAD_COMMAND fw=1.0.0", 0);
    call(" W 00", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("W\t00", "ERR BAD_COMMAND fw=1.0.0", 0);

    fresh_stats();
    call("STATUS",
         "OK STATUS role=TESTER fw=1.0.0 addr=0x48 i2c=0 sda=4 scl=5 "
         "hz=100000 pins=input pulls=off ops=0 ok=0 err=0 timeouts=0 last=none",
         0);
}

static void test_write_limits(void)
{
    const char *h32 = "000102030405060708090a0b0c0d0e0f"
                      "101112131415161718191a1b1c1d1e1f";
    const char *h33 = "000102030405060708090a0b0c0d0e0f"
                      "101112131415161718191a1b1c1d1e1f20";

    fresh_stats();
    call("W 00", "OK W n=1 fw=1.0.0", 1);
    CHECK(seen_wlen == 1 && seen_w[0] == 0x00);

    call("W 0A1b", "OK W n=2 fw=1.0.0", 1);
    CHECK(seen_wlen == 2 && seen_w[0] == 0x0a && seen_w[1] == 0x1b);

    call("W  00", "OK W n=1 fw=1.0.0", 1);

    bus_reset();
    {
        char line[80];
        char want[64];
        snprintf(line, sizeof(line), "W %s", h32);
        snprintf(want, sizeof(want), "OK W n=32 fw=1.0.0");
        call(line, want, 1);
        CHECK(seen_wlen == 32);
    }

    {
        char line[80];
        snprintf(line, sizeof(line), "W %s", h33);
        call(line, "ERR TOO_LONG fw=1.0.0", 0);
    }

    call("W 0", "ERR BAD_HEX fw=1.0.0", 0);
    call("W 0g", "ERR BAD_HEX fw=1.0.0", 0);
    call("W", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("W 00 11", "ERR BAD_COMMAND fw=1.0.0", 0);
}

static void test_read_limits(void)
{
    fresh_stats();
    bus_reset();
    memset(read_data, 0xA5, sizeof(read_data));
    call("R 1", "OK R n=1 data=a5 fw=1.0.0", 1);
    CHECK(seen_rlen == 1);
    call("R 004", "OK R n=4 data=a5a5a5a5 fw=1.0.0", 1);

    {
        char want[TESTER_REPLY_MAX];
        char data[TESTER_MAX_BYTES * 2 + 1];
        size_t i;
        for (i = 0; i < 32; i++) {
            read_data[i] = (uint8_t)i;
            snprintf(data + i * 2, 3, "%02x", (unsigned)i);
        }
        snprintf(want, sizeof(want), "OK R n=32 data=%s fw=1.0.0", data);
        call("R 32", want, 1);
    }

    call("R 0", "ERR BAD_VALUE fw=1.0.0", 0);
    call("R 33", "ERR TOO_LONG fw=1.0.0", 0);
    call("R -1", "ERR BAD_VALUE fw=1.0.0", 0);
    call("R +1", "ERR BAD_VALUE fw=1.0.0", 0);
    call("R 4x", "ERR BAD_VALUE fw=1.0.0", 0);
    call("R", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("R 1 2", "ERR BAD_COMMAND fw=1.0.0", 0);
}

static void test_write_read_limits(void)
{
    fresh_stats();
    bus_reset();
    memset(read_data, 0x5A, sizeof(read_data));
    call("WR 00 4", "OK WR wn=1 rn=4 data=5a5a5a5a fw=1.0.0", 1);
    CHECK(seen_wlen == 1 && seen_w[0] == 0x00 && seen_rlen == 4);

    call("WR 0001 2", "OK WR wn=2 rn=2 data=5a5a fw=1.0.0", 1);
    call("WR 00", "ERR BAD_COMMAND fw=1.0.0", 0);
    call("WR 00 0", "ERR BAD_VALUE fw=1.0.0", 0);
    call("WR 00 33", "ERR TOO_LONG fw=1.0.0", 0);
    call("WR 0 2", "ERR BAD_HEX fw=1.0.0", 0);
    call("WR 00 2 3", "ERR BAD_COMMAND fw=1.0.0", 0);
}

static void test_long_line(void)
{
    char line[TESTER_LINE_MAX + 20];
    memset(line, 'A', sizeof(line) - 1);
    line[sizeof(line) - 1] = '\0';
    fresh_stats();
    call(line, "ERR TOO_LONG fw=1.0.0", 0);
}

static void test_bus_errors(void)
{
    fresh_stats();
    bus_reset();
    write_rc = T_BUS_NAK;
    write_sent = 1;
    call("W 0011", "ERR NAK phase=w at=1 fw=1.0.0", 1);

    write_rc = T_BUS_TIMEOUT;
    write_sent = 0;
    call("W 0011", "ERR TIMEOUT phase=w at=0 fw=1.0.0", 1);
    CHECK(stats.timeouts == 1);

    write_rc = T_BUS_ERROR;
    write_sent = 2;
    call("W 0011", "ERR BUS phase=w at=2 fw=1.0.0", 1);

    read_rc = T_BUS_NAK;
    read_got = 2;
    call("R 4", "ERR NAK phase=r n=2 fw=1.0.0", 1);

    read_rc = T_BUS_TIMEOUT;
    read_got = 0;
    call("R 4", "ERR TIMEOUT phase=r n=0 fw=1.0.0", 1);

    write_rc = T_BUS_OK;
    read_rc = T_BUS_NAK;
    read_got = 3;
    call("WR 00 4", "ERR NAK phase=r n=3 fw=1.0.0", 1);

    write_rc = T_BUS_NAK;
    write_sent = 0;
    call("WR 00 4", "ERR NAK phase=w at=0 fw=1.0.0", 1);
}

/* A bus that returns OK with a short count is a bug in the port layer.
 * The core must never turn that into a success line or invent data. */
static t_bus_rc short_write(const uint8_t *data, size_t len, size_t *sent)
{
    (void)data;
    bus_calls++;
    *sent = len ? len - 1 : 0;
    return T_BUS_OK;
}

static t_bus_rc short_read(uint8_t *dst, size_t len, size_t *got)
{
    (void)dst;
    bus_calls++;
    *got = len ? len - 1 : 0;
    return T_BUS_OK;
}

static t_bus_rc short_write_read(const uint8_t *w, size_t wlen, uint8_t *r,
                                 size_t rlen, size_t *wsent, size_t *rgot)
{
    (void)w; (void)r;
    bus_calls++;
    *wsent = wlen;
    *rgot = rlen ? rlen - 1 : 0;
    return T_BUS_OK;
}

static void test_contract_violation(void)
{
    char reply[TESTER_REPLY_MAX];
    t_bus_ops bad = { short_write, short_read, short_write_read };

    fresh_stats();
    bus_calls = 0;
    t_handle_line("W 0011", &bad, reply, sizeof(reply), &stats);
    CHECK(strcmp(reply, "ERR BUS fw=1.0.0") == 0);
    CHECK(strstr(reply, "OK") == NULL);

    bus_calls = 0;
    t_handle_line("R 4", &bad, reply, sizeof(reply), &stats);
    CHECK(strcmp(reply, "ERR BUS fw=1.0.0") == 0);

    bus_calls = 0;
    t_handle_line("WR 00 4", &bad, reply, sizeof(reply), &stats);
    CHECK(strcmp(reply, "ERR BUS fw=1.0.0") == 0);
}

static void test_status_counters(void)
{
    char reply[TESTER_REPLY_MAX];
    fresh_stats();
    bus_reset();

    memset(read_data, 1, sizeof(read_data));
    call("R 2", "OK R n=2 data=0101 fw=1.0.0", 1);
    read_rc = T_BUS_TIMEOUT;
    read_got = 1;
    call("R 2", "ERR TIMEOUT phase=r n=1 fw=1.0.0", 1);

    bus_reset();
    t_handle_line("STATUS", &fake_ops, reply, sizeof(reply), &stats);
    CHECK(strstr(reply, "ops=2 ok=1 err=1 timeouts=1 last=timeout") != NULL);
    CHECK(bus_calls == 0);
}

int main(void)
{
    test_identity_and_parser();
    test_write_limits();
    test_read_limits();
    test_write_read_limits();
    test_long_line();
    test_bus_errors();
    test_contract_violation();
    test_status_counters();

    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
