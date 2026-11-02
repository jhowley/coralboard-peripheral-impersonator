/* TESTER command core — see tester_core.h for the contract. */

#include "tester_core.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#define MAX_TOKENS 4

typedef enum {
    REQ_PING,
    REQ_STATUS,
    REQ_WRITE,
    REQ_READ,
    REQ_WRITE_READ,
    REQ_INVALID
} request_kind;

typedef struct {
    request_kind kind;
    uint8_t wdata[TESTER_MAX_BYTES];
    uint8_t wlen;
    uint8_t rcount;
} request;

static const char *const LAST_NONE = "none";

static size_t replyf(char *out, size_t cap, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

static size_t replyf(char *out, size_t cap, const char *fmt, ...)
{
    va_list ap;
    int n;
    if (cap == 0) {
        return 0;
    }
    va_start(ap, fmt);
    n = vsnprintf(out, cap, fmt, ap);
    va_end(ap);
    if (n < 0) {
        out[0] = '\0';
        return 0;
    }
    if ((size_t)n >= cap) {
        /* Keep the buffer NUL-terminated; callers size it for the worst reply. */
        out[cap - 1] = '\0';
        return cap - 1;
    }
    return (size_t)n;
}

static size_t error_reply(char *out, size_t cap, const char *code)
{
    return replyf(out, cap, "ERR %s fw=" TESTER_FW_VERSION, code);
}

static size_t error_reply_phase(char *out, size_t cap, const char *code,
                                char phase, const char *field, size_t count)
{
    return replyf(out, cap, "ERR %s phase=%c %s=%u fw=" TESTER_FW_VERSION,
                  code, phase, field, (unsigned)count);
}

void t_stats_init(t_stats *st)
{
    if (st == NULL) {
        return;
    }
    memset(st, 0, sizeof(*st));
    st->last = LAST_NONE;
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Hex byte string: even length, 2..64 chars, hex digits only.
 * Returns 0 ok, -1 malformed, -2 too long. */
static int parse_hex(const char *s, uint8_t *out, uint8_t *out_len)
{
    size_t n = strlen(s);
    size_t i;

    if (n > (size_t)TESTER_MAX_BYTES * 2) {
        return -2;
    }
    if (n == 0 || (n & 1u) != 0) {
        return -1;
    }
    for (i = 0; i < n; i += 2) {
        int hi = hex_value(s[i]);
        int lo = hex_value(s[i + 1]);
        if (hi < 0 || lo < 0) {
            return -1;
        }
        out[i / 2] = (uint8_t)((hi << 4) | lo);
    }
    *out_len = (uint8_t)(n / 2);
    return 0;
}

/* Decimal byte count: digits only, 1..4 digits, 1..32.
 * Returns 0 ok, -1 bad value, -2 too long. */
static int parse_count(const char *s, uint8_t *out)
{
    size_t n = strlen(s);
    size_t i;
    unsigned value = 0;

    if (n == 0 || n > 4) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') {
            return -1;
        }
        value = value * 10u + (unsigned)(s[i] - '0');
    }
    if (value == 0) {
        return -1;
    }
    if (value > TESTER_MAX_BYTES) {
        return -2;
    }
    *out = (uint8_t)value;
    return 0;
}

/* Splits on runs of single spaces. Leading/trailing spaces, tabs and any
 * non-printable byte are rejected by the caller before this runs.
 * Returns token count, or -1 when there are more than MAX_TOKENS. */
static int tokenize(char *s, char *tok[MAX_TOKENS])
{
    int n = 0;
    char *p = s;

    while (*p != '\0') {
        if (*p == ' ') {
            p++;
            continue;
        }
        if (n == MAX_TOKENS) {
            return -1;
        }
        tok[n++] = p;
        while (*p != '\0' && *p != ' ') {
            p++;
        }
        if (*p == ' ') {
            *p++ = '\0';
        }
    }
    return n;
}

static int parse_request(const char *line, request *req, const char **err)
{
    char buf[TESTER_LINE_MAX + 1];
    char *tok[MAX_TOKENS];
    size_t len = strlen(line);
    size_t i;
    int ntok;

    *err = NULL;
    memset(req, 0, sizeof(*req));
    req->kind = REQ_INVALID;

    if (len > TESTER_LINE_MAX) {
        *err = "TOO_LONG";
        return -1;
    }
    if (len == 0) {
        *err = "BAD_COMMAND";
        return -1;
    }
    for (i = 0; i < len; i++) {
        unsigned char c = (unsigned char)line[i];
        if (c < 0x20 || c > 0x7e) {
            *err = "BAD_COMMAND";
            return -1;
        }
    }
    if (line[0] == ' ' || line[len - 1] == ' ') {
        *err = "BAD_COMMAND";
        return -1;
    }
    memcpy(buf, line, len + 1);
    ntok = tokenize(buf, tok);
    if (ntok <= 0) {
        *err = "BAD_COMMAND";
        return -1;
    }

    if (strcmp(tok[0], "PING") == 0) {
        if (ntok != 1) {
            *err = "BAD_COMMAND";
            return -1;
        }
        req->kind = REQ_PING;
        return 0;
    }
    if (strcmp(tok[0], "STATUS") == 0) {
        if (ntok != 1) {
            *err = "BAD_COMMAND";
            return -1;
        }
        req->kind = REQ_STATUS;
        return 0;
    }
    if (strcmp(tok[0], "W") == 0) {
        int rc;
        if (ntok != 2) {
            *err = "BAD_COMMAND";
            return -1;
        }
        rc = parse_hex(tok[1], req->wdata, &req->wlen);
        if (rc == -2) {
            *err = "TOO_LONG";
            return -1;
        }
        if (rc != 0) {
            *err = "BAD_HEX";
            return -1;
        }
        req->kind = REQ_WRITE;
        return 0;
    }
    if (strcmp(tok[0], "R") == 0) {
        int rc;
        if (ntok != 2) {
            *err = "BAD_COMMAND";
            return -1;
        }
        rc = parse_count(tok[1], &req->rcount);
        if (rc == -2) {
            *err = "TOO_LONG";
            return -1;
        }
        if (rc != 0) {
            *err = "BAD_VALUE";
            return -1;
        }
        req->kind = REQ_READ;
        return 0;
    }
    if (strcmp(tok[0], "WR") == 0) {
        int rc;
        if (ntok != 3) {
            *err = "BAD_COMMAND";
            return -1;
        }
        rc = parse_hex(tok[1], req->wdata, &req->wlen);
        if (rc == -2) {
            *err = "TOO_LONG";
            return -1;
        }
        if (rc != 0) {
            *err = "BAD_HEX";
            return -1;
        }
        rc = parse_count(tok[2], &req->rcount);
        if (rc == -2) {
            *err = "TOO_LONG";
            return -1;
        }
        if (rc != 0) {
            *err = "BAD_VALUE";
            return -1;
        }
        req->kind = REQ_WRITE_READ;
        return 0;
    }

    *err = "BAD_COMMAND";
    return -1;
}

static void hex_encode(const uint8_t *in, size_t len, char *out)
{
    static const char digits[] = "0123456789abcdef";
    size_t i;

    for (i = 0; i < len; i++) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 0x0f];
    }
    out[len * 2] = '\0';
}

static const char *bus_code(t_bus_rc rc)
{
    switch (rc) {
    case T_BUS_NAK:     return "NAK";
    case T_BUS_TIMEOUT: return "TIMEOUT";
    case T_BUS_ERROR:   return "BUS";
    case T_BUS_OK:      break;
    }
    return "BUS";
}

static const char *bus_last(t_bus_rc rc)
{
    switch (rc) {
    case T_BUS_OK:      return "ok";
    case T_BUS_NAK:     return "nak";
    case T_BUS_TIMEOUT: return "timeout";
    case T_BUS_ERROR:   return "bus";
    }
    return "bus";
}

static void count_result(t_stats *st, t_bus_rc rc, bool success)
{
    st->ops++;
    if (success) {
        st->ok++;
        st->last = "ok";
        return;
    }
    st->err++;
    if (rc == T_BUS_TIMEOUT) {
        st->timeouts++;
    }
    st->last = bus_last(rc);
}

static size_t handle_write(const request *req, const t_bus_ops *bus,
                           char *out, size_t cap, t_stats *st)
{
    size_t sent = 0;
    t_bus_rc rc = bus->write(req->wdata, req->wlen, &sent);

    if (rc == T_BUS_OK && sent == req->wlen) {
        count_result(st, rc, true);
        return replyf(out, cap, "OK W n=%u fw=" TESTER_FW_VERSION,
                      (unsigned)sent);
    }
    if (rc == T_BUS_OK) {
        /* Port contract violation: OK with a count that does not match. */
        count_result(st, T_BUS_ERROR, false);
        return error_reply(out, cap, "BUS");
    }
    if (sent > req->wlen) {
        sent = req->wlen;
    }
    count_result(st, rc, false);
    return error_reply_phase(out, cap, bus_code(rc), 'w', "at", sent);
}

static size_t handle_read(const request *req, const t_bus_ops *bus,
                          char *out, size_t cap, t_stats *st)
{
    uint8_t data[TESTER_MAX_BYTES];
    char hex[TESTER_MAX_BYTES * 2 + 1];
    size_t got = 0;
    t_bus_rc rc;

    memset(data, 0, sizeof(data));
    rc = bus->read(data, req->rcount, &got);

    if (rc == T_BUS_OK && got == req->rcount) {
        hex_encode(data, got, hex);
        count_result(st, rc, true);
        return replyf(out, cap, "OK R n=%u data=%s fw=" TESTER_FW_VERSION,
                      (unsigned)got, hex);
    }
    if (rc == T_BUS_OK) {
        count_result(st, T_BUS_ERROR, false);
        return error_reply(out, cap, "BUS");
    }
    if (got > req->rcount) {
        got = req->rcount;
    }
    count_result(st, rc, false);
    return error_reply_phase(out, cap, bus_code(rc), 'r', "n", got);
}

static size_t handle_write_read(const request *req, const t_bus_ops *bus,
                                char *out, size_t cap, t_stats *st)
{
    uint8_t data[TESTER_MAX_BYTES];
    char hex[TESTER_MAX_BYTES * 2 + 1];
    size_t wsent = 0;
    size_t rgot = 0;
    t_bus_rc rc;

    memset(data, 0, sizeof(data));
    rc = bus->write_read(req->wdata, req->wlen, data, req->rcount,
                         &wsent, &rgot);

    if (rc == T_BUS_OK && wsent == req->wlen && rgot == req->rcount) {
        hex_encode(data, rgot, hex);
        count_result(st, rc, true);
        return replyf(out, cap,
                      "OK WR wn=%u rn=%u data=%s fw=" TESTER_FW_VERSION,
                      (unsigned)wsent, (unsigned)rgot, hex);
    }
    if (rc == T_BUS_OK) {
        count_result(st, T_BUS_ERROR, false);
        return error_reply(out, cap, "BUS");
    }
    if (wsent != req->wlen) {
        if (wsent > req->wlen) {
            wsent = req->wlen;
        }
        count_result(st, rc, false);
        return error_reply_phase(out, cap, bus_code(rc), 'w', "at", wsent);
    }
    if (rgot > req->rcount) {
        rgot = req->rcount;
    }
    count_result(st, rc, false);
    return error_reply_phase(out, cap, bus_code(rc), 'r', "n", rgot);
}

static size_t handle_status(char *out, size_t cap, const t_stats *st)
{
    return replyf(out, cap,
                  "OK STATUS role=" TESTER_ROLE " fw=" TESTER_FW_VERSION
                  " addr=0x%02x i2c=0 sda=4 scl=5 hz=100000 "
                  "pins=input pulls=off ops=%u ok=%u err=%u timeouts=%u "
                  "last=%s",
                  TESTER_I2C_ADDR, (unsigned)st->ops, (unsigned)st->ok,
                  (unsigned)st->err, (unsigned)st->timeouts,
                  st->last != NULL ? st->last : LAST_NONE);
}

size_t t_handle_line(const char *line, const t_bus_ops *bus,
                     char *out, size_t cap, t_stats *st)
{
    request req;
    const char *err = NULL;

    if (out == NULL || cap == 0) {
        return 0;
    }
    out[0] = '\0';
    if (line == NULL || bus == NULL || st == NULL) {
        return error_reply(out, cap, "BAD_COMMAND");
    }
    if (parse_request(line, &req, &err) != 0) {
        return error_reply(out, cap, err != NULL ? err : "BAD_COMMAND");
    }

    switch (req.kind) {
    case REQ_PING:
        return replyf(out, cap, "OK PING " TESTER_ROLE " fw=" TESTER_FW_VERSION);
    case REQ_STATUS:
        return handle_status(out, cap, st);
    case REQ_WRITE:
        return handle_write(&req, bus, out, cap, st);
    case REQ_READ:
        return handle_read(&req, bus, out, cap, st);
    case REQ_WRITE_READ:
        return handle_write_read(&req, bus, out, cap, st);
    case REQ_INVALID:
        break;
    }
    return error_reply(out, cap, "BAD_COMMAND");
}
