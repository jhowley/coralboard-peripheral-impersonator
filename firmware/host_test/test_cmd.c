/* Host-side tests for the firmware command parser (see firmware/SDK_NOTES.md).
 *
 * These run with the host gcc and prove parser behaviour only. They say nothing
 * about the I2C target, the USB CDC link or the lease timer; those are covered
 * by the ARM build and its static evidence, not by this harness.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "protocol.h"

static int failures = 0;
static int cases = 0;

#define CHECK(cond, name) do { \
    cases++; \
    if (cond) { printf("ok   - %s\n", name); } \
    else { printf("FAIL - %s (%s:%d)\n", name, __FILE__, __LINE__); failures++; } \
} while (0)

static void hex_encode(const uint8_t *in, size_t len, char *out) {
    static const char *digits = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 0x0F];
    }
    out[len * 2] = '\0';
}

static void make_image(uint8_t *image) {
    memset(image, 0, CB_REG_SPACE);
    image[0] = 0x19;
    image[1] = 0x60;
    image[2] = 0x4B;
    image[3] = 0x50;
}

/* writable window used by every LOAD case: registers 1, 2 and 3 */
static void make_wmask(uint8_t *wmask) {
    memset(wmask, 0, CB_WMASK_BYTES);
    wmask[0] = 0x0E; /* bits 1..3 */
}

/* Builds a full LOAD line from caller-supplied parts so a negative case can
 * break exactly one field. */
static void make_line(char *buf, size_t cap, uint32_t crc, const char *tag,
                      const char *flags_hex, const char *wrap_dec,
                      const char *init_dec, const char *wmask_hex,
                      const char *image_hex) {
    int n = snprintf(buf, cap,
                     "LOAD crc=%08x flags=%s wrap=%s init=%s tag=%s wmask=%s image=%s",
                     (unsigned)crc, flags_hex, wrap_dec, init_dec, tag,
                     wmask_hex, image_hex);
    assert(n > 0 && (size_t)n < cap);
}

int main(void) {
    cb_cmd cmd;
    char line[CB_LINE_MAX + 64];
    char image_hex[CB_REG_SPACE * 2 + 1];
    char wmask_hex[CB_WMASK_BYTES * 2 + 1];
    uint8_t image[CB_REG_SPACE];
    uint8_t wmask[CB_WMASK_BYTES];
    uint32_t crc;
    int ok;

    make_image(image);
    make_wmask(wmask);
    hex_encode(image, CB_REG_SPACE, image_hex);
    hex_encode(wmask, CB_WMASK_BYTES, wmask_hex);

    /* CRC-32 check value from the canonical CHECK string: ties this C
     * implementation to zlib.crc32 used by the host tooling. */
    crc = cb_crc32((const uint8_t *)"123456789", 9);
    CHECK(crc == 0xCBF43926u, "crc32 matches the standard check value 0xcbf43926");

    /* trivial verbs */
    CHECK(cb_parse_line("PING", &cmd) == CB_OK && cmd.type == CB_CMD_PING, "PING");
    CHECK(cb_parse_line("STATUS", &cmd) == CB_OK && cmd.type == CB_CMD_STATUS, "STATUS");
    CHECK(cb_parse_line("DISARM", &cmd) == CB_OK && cmd.type == CB_CMD_DISARM, "DISARM");
    CHECK(cb_parse_line("  STATUS  ", &cmd) == CB_OK, "leading/trailing spaces tolerated");
    CHECK(cb_parse_line("TRACE END", &cmd) == CB_OK && cmd.type == CB_CMD_TRACE_END,
          "TRACE END");
    CHECK(cb_parse_line("TRACE DRAIN n=16", &cmd) == CB_OK &&
          cmd.type == CB_CMD_TRACE_DRAIN && cmd.drain_n == 16, "TRACE DRAIN n=16");

    /* malformed */
    CHECK(cb_parse_line("", &cmd) == CB_ERR_BAD_COMMAND, "empty line rejected");
    CHECK(cb_parse_line("STATUSX", &cmd) == CB_ERR_BAD_COMMAND, "unknown verb rejected");
    CHECK(cb_parse_line("STATUS junk=1", &cmd) == CB_ERR_BAD_COMMAND, "STATUS with args rejected");
    CHECK(cb_parse_line("TRACE", &cmd) == CB_ERR_BAD_COMMAND, "bare TRACE rejected");
    CHECK(cb_parse_line("TRACE FLUSH", &cmd) == CB_ERR_BAD_COMMAND, "unknown TRACE verb rejected");
    CHECK(cb_parse_line("TRACE DRAIN n=0", &cmd) == CB_ERR_BAD_VALUE, "DRAIN n=0 rejected");
    CHECK(cb_parse_line("TRACE DRAIN n=65", &cmd) == CB_ERR_BAD_VALUE, "DRAIN n=65 rejected");
    CHECK(cb_parse_line("TRACE DRAIN", &cmd) == CB_ERR_BAD_COMMAND, "DRAIN without n rejected");

    /* ARM bounds */
    CHECK(cb_parse_line("ARM", &cmd) == CB_ERR_BAD_COMMAND, "ARM without lease rejected");
    CHECK(cb_parse_line("ARM lease_ms=100", &cmd) == CB_OK && cmd.lease_ms == 100,
          "ARM lease_ms=100");
    CHECK(cb_parse_line("ARM lease_ms=10000", &cmd) == CB_OK, "ARM at the 10s ceiling accepted");
    CHECK(cb_parse_line("ARM lease_ms=10001", &cmd) == CB_ERR_LEASE_TOO_LONG,
          "ARM over the 10s ceiling rejected");
    CHECK(cb_parse_line("ARM lease_ms=0", &cmd) == CB_ERR_LEASE_TOO_LONG, "ARM lease 0 rejected");
    CHECK(cb_parse_line("ARM lease_ms=-5", &cmd) == CB_ERR_BAD_VALUE, "negative lease rejected");
    CHECK(cb_parse_line("ARM lease_ms=10s", &cmd) == CB_ERR_BAD_VALUE, "lease with suffix rejected");
    CHECK(cb_parse_line("ARM timeout_ms=10", &cmd) == CB_ERR_BAD_COMMAND, "unknown ARM key rejected");

    /* LOAD happy path */
    crc = cb_crc32(image, CB_REG_SPACE);
    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "01", "4", "0", wmask_hex, image_hex);
    ok = cb_parse_line(line, &cmd) == CB_OK && cmd.type == CB_CMD_LOAD;
    CHECK(ok, "LOAD accepted");
    if (ok) {
        CHECK(cmd.image[0] == 0x19 && cmd.image[3] == 0x50, "LOAD decodes the register image");
        CHECK(strcmp(cmd.tag, "bench-temp-raw-v1") == 0, "LOAD keeps the tag");
        CHECK(cmd.flags == 0x01, "LOAD keeps the auto-increment flag");
        CHECK(cmd.wrap == 4, "LOAD keeps the pointer wrap");
        CHECK(cmd.initial == 0, "LOAD keeps the pointer initial value");
        CHECK(cmd.crc == crc, "LOAD echoes the crc");
        CHECK(cb_wmask_allows(cmd.wmask, 1) && cb_wmask_allows(cmd.wmask, 3),
              "LOAD keeps the writable window");
        CHECK(!cb_wmask_allows(cmd.wmask, 0) && !cb_wmask_allows(cmd.wmask, 255),
              "writable window excludes registers 0 and 255");
    }

    /* LOAD rejections: one broken field at a time */
    make_line(line, sizeof(line), crc + 1, "bench-temp-raw-v1", "01", "4", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_CRC, "LOAD with wrong crc rejected");

    make_line(line, sizeof(line), crc, "Bad_Tag", "01", "4", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_VALUE, "LOAD with uppercase tag rejected");

    make_line(line, sizeof(line), crc, "BAD TAG", "01", "4", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_COMMAND,
          "LOAD with a space inside the tag is unparseable");

    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "02", "4", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_VALUE, "LOAD with unknown flag bit rejected");

    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "01", "0", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_VALUE, "LOAD with wrap=0 rejected");

    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "01", "300", "0", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_VALUE, "LOAD with wrap=300 rejected");

    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "01", "4", "0", wmask_hex, "0011");
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_IMAGE, "LOAD with short image rejected");

    snprintf(line, sizeof(line), "LOAD crc=%08x flags=01 tag=t1 wmask=%s image=%s",
             (unsigned)crc, wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_COMMAND, "LOAD without wrap rejected");

    snprintf(line, sizeof(line),
             "LOAD crc=%08x flags=01 wrap=4 tag=t1 wmask=%s image=%s",
             (unsigned)crc, wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_COMMAND, "LOAD without init rejected");

    make_line(line, sizeof(line), crc, "t1", "01", "4", "4", wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_VALUE,
          "LOAD with init >= wrap rejected explicitly");

    snprintf(line, sizeof(line), "LOAD crc=%08x flags=01 wrap=4 init=0 tag=t1 wmask=0e image=%s",
             (unsigned)crc, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_IMAGE, "LOAD with short wmask rejected");

    {
        char bad_wmask[CB_WMASK_BYTES * 2 + 1];
        memcpy(bad_wmask, wmask_hex, sizeof(bad_wmask));
        bad_wmask[4] = 'Z';
        make_line(line, sizeof(line), crc, "t1", "01", "4", "0", bad_wmask, image_hex);
        CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_IMAGE, "LOAD with non-hex wmask rejected");
    }

    {
        char bad_image[CB_REG_SPACE * 2 + 1];
        memcpy(bad_image, image_hex, sizeof(bad_image));
        bad_image[10] = 'Z';
        make_line(line, sizeof(line), crc, "t1", "01", "4", "0", wmask_hex, bad_image);
        CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_IMAGE, "LOAD with non-hex image rejected");
    }

    {
        char bad_image[CB_REG_SPACE * 2 + 1];
        memcpy(bad_image, image_hex, sizeof(bad_image));
        bad_image[10] = 'A'; /* uppercase hex must not slip through */
        make_line(line, sizeof(line), crc, "t1", "01", "4", "0", wmask_hex, bad_image);
        CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_IMAGE, "LOAD rejects uppercase hex");
    }

    snprintf(line, sizeof(line),
             "LOAD crc=%08x flags=01 wrap=4 init=0 tag=t1 wmask=%s image=%s padding=1",
             (unsigned)crc, wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_COMMAND, "LOAD with unknown key rejected");

    snprintf(line, sizeof(line),
             "LOAD crc=%08x crc=%08x flags=01 wrap=4 init=0 tag=t1 wmask=%s image=%s",
             (unsigned)crc, (unsigned)crc, wmask_hex, image_hex);
    CHECK(cb_parse_line(line, &cmd) == CB_ERR_BAD_COMMAND, "LOAD with duplicate key rejected");

    /* longest legal line must still fit the parser's expectations */
    make_line(line, sizeof(line), crc, "bench-temp-raw-v1", "01", "4", "0", wmask_hex, image_hex);
    CHECK(strlen(line) < CB_LINE_MAX, "a full LOAD line fits inside CB_LINE_MAX");
    CHECK(cb_parse_line(line, &cmd) == CB_OK, "exactly-sized line accepted");

    CHECK(cb_parse_line(NULL, &cmd) == CB_ERR_BAD_COMMAND, "NULL line rejected");

    printf("---\n%d cases, %d failure(s)\n", cases, failures);
    return failures == 0 ? 0 : 1;
}
