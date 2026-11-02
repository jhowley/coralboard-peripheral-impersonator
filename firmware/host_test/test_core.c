/* Host-side tests for the firmware's SDK-free state/trace/lease core.
 *
 * Compiles firmware/src/core.c and cmd.c with the host gcc. This is NOT the
 * firmware build: it proves the core's data path and lifecycle only. The SDK
 * adapter (main.c / i2c_target.c) is covered by the real ARM build, but no
 * board was flashed or executed.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "core.h"
#include "protocol.h"

static int failures = 0;
static int cases = 0;

#define CHECK(cond, name) do { \
    cases++; \
    if (cond) { printf("ok   - %s\n", name); } \
    else { printf("FAIL - %s (%s:%d)\n", name, __FILE__, __LINE__); failures++; } \
} while (0)

static const char *TAG = "bench-temp-raw-v1";

static void make_image(uint8_t *image) {
    memset(image, 0, CB_REG_SPACE);
    image[0] = 0x19;
    image[1] = 0x60;
    image[2] = 0x4B;
    image[3] = 0x50;
}

static void make_wmask(uint8_t *wmask) {
    memset(wmask, 0, CB_WMASK_BYTES);
    wmask[0] = 0x0E; /* registers 1..3 */
}

static void make_load(cb_cmd *cmd, const uint8_t *image, const uint8_t *wmask,
                      uint8_t flags, uint16_t wrap, uint8_t initial,
                      const char *tag) {
    memset(cmd, 0, sizeof(*cmd));
    memcpy(cmd->image, image, CB_REG_SPACE);
    memcpy(cmd->wmask, wmask, CB_WMASK_BYTES);
    cmd->crc = cb_crc32(cmd->image, CB_REG_SPACE);
    cmd->flags = flags;
    cmd->wrap = wrap;
    cmd->initial = initial;
    strncpy(cmd->tag, tag, CB_TAG_MAX);
    cmd->tag[CB_TAG_MAX] = '\0';
    cmd->type = CB_CMD_LOAD;
}

static void load_default(cb_core *c, uint16_t nonce) {
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 4, 0, TAG);
    cb_core_init(c, nonce);
    assert(cb_core_load(c, &cmd) == CB_OK);
}

static void read_n(cb_core *c, int n) {
    for (int i = 0; i < n; i++) (void)cb_core_bus_read(c);
    cb_core_bus_finish(c);
}

static void read_seg(cb_core *c, int n, uint8_t *out) {
    for (int i = 0; i < n; i++) out[i] = cb_core_bus_read(c);
    cb_core_bus_finish(c);
}

static void write_seg(cb_core *c, const uint8_t *bytes, int n) {
    for (int i = 0; i < n; i++) cb_core_bus_write(c, bytes[i]);
    cb_core_bus_finish(c);
}

static void test_epochs(void) {
    cb_core c;
    load_default(&c, 0xBEEF);
    uint32_t e1 = c.epoch;
    CHECK((e1 >> 16) == 0xBEEF && (e1 & 0xFFFF) == 1, "LOAD starts epoch boot|counter");
    CHECK(cb_core_arm(&c, 100) == CB_OK, "ARM accepted");
    uint32_t e2 = c.epoch;
    CHECK(e2 != e1 && (e2 & 0xFFFF) == 2, "ARM starts a new epoch");
    CHECK(c.next_seq == 0 && c.emitted == 0, "ARM clears sequence and counters");
    cb_core_seal(&c);
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 4, 0, TAG);
    CHECK(cb_core_load(&c, &cmd) == CB_OK, "LOAD after sealed lease accepted");
    CHECK((c.epoch & 0xFFFF) == 3 && c.epoch != e1 && c.epoch != e2,
          "LOAD after a lease gets a distinct epoch");
}

static void test_config_binding(void) {
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES], wmask2[CB_WMASK_BYTES];
    make_image(image);
    make_wmask(wmask);
    memcpy(wmask2, wmask, sizeof(wmask));
    wmask2[0] = 0x0C;
    uint32_t a = cb_core_config_crc(TAG, image, wmask, 1, 4, 0);
    CHECK(a == cb_core_config_crc(TAG, image, wmask, 1, 4, 0), "cfg hash deterministic");
    CHECK(a != cb_core_config_crc(TAG, image, wmask2, 1, 4, 0), "cfg binds wmask");
    CHECK(a != cb_core_config_crc(TAG, image, wmask, 0, 4, 0), "cfg binds flags");
    CHECK(a != cb_core_config_crc(TAG, image, wmask, 1, 8, 0), "cfg binds wrap");
    CHECK(a != cb_core_config_crc(TAG, image, wmask, 1, 4, 2), "cfg binds initial");
    CHECK(a != cb_core_config_crc("other", image, wmask, 1, 4, 0), "cfg binds tag");
}

static void test_arm_restores_template(void) {
    cb_core c;
    load_default(&c, 7);
    uint8_t w[3] = {0x01, 0x99, 0x88};
    CHECK(cb_core_arm(&c, 100) == CB_OK, "arm for restore test");
    write_seg(&c, w, 3);
    CHECK(c.image[1] == 0x99 && c.image[2] == 0x88, "lease writes land in image");
    cb_core_seal(&c);
    CHECK(cb_core_arm(&c, 100) == CB_OK, "second arm accepted");
    CHECK(c.image[1] == 0x60 && c.image[2] == 0x4B, "fresh ARM restores template");
    CHECK(c.pointer == 0, "fresh ARM applies initial pointer");
    CHECK(c.emitted == 0 && c.next_seq == 0, "fresh ARM clears the trace");
}

static void test_pointer_and_wrap(void) {
    cb_core c;
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    image[2] = 0x77;
    make_wmask(wmask);
    make_load(&cmd, image, wmask, 0 /* no auto inc */, 4, 2, TAG);
    cb_core_init(&c, 1);
    CHECK(cb_core_load(&c, &cmd) == CB_OK, "load with initial pointer 2");
    CHECK(cb_core_arm(&c, 100) == CB_OK, "arm with initial pointer 2");
    CHECK(c.pointer == 2, "initial pointer is respected");
    CHECK(cb_core_bus_read(&c) == 0x77, "read starts at initial pointer");
    cb_core_bus_finish(&c);
    CHECK(c.pointer == 2, "static profile does not advance the pointer");
    uint8_t ptr = 0x10;
    cb_core_bus_write(&c, ptr);
    cb_core_bus_finish(&c);
    CHECK(c.pointer == 0, "pointer write honours wrap modulus (0x10 % 4)");
    uint8_t data[2] = {0xAA, 0xBB};
    cb_core_bus_write(&c, 0x02); /* pointer */
    cb_core_bus_write(&c, data[0]);
    cb_core_bus_write(&c, data[1]);
    cb_core_bus_finish(&c);
    CHECK(c.image[2] == 0xAA && c.image[3] == 0xBB,
          "data write advances modulo wrap (2,3)");
}

static void test_pointer_init_rejected(void) {
    cb_core c;
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, 0, 4, 4, TAG);
    cb_core_init(&c, 1);
    CHECK(cb_core_load(&c, &cmd) == CB_ERR_BAD_VALUE,
          "initial >= wrap is rejected, not silently reduced");
}

static void test_sr_read_idiom(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    uint8_t ptr = 0x01;
    write_seg(&c, &ptr, 1); /* FINISH fires at repeated START */
    (void)cb_core_bus_read(&c);
    (void)cb_core_bus_read(&c);
    cb_core_bus_finish(&c); /* FINISH fires at STOP */
    cb_trace_ev e;
    CHECK(cb_core_drain(&c, &e) && e.op == CB_TRACE_OP_WRITE && e.ptr == 1 && e.len == 0,
          "Sr idiom: pointer write is its own segment");
    CHECK(c.epoch == e.epoch, "event carries the current epoch");
    CHECK(cb_core_drain(&c, &e) && e.op == CB_TRACE_OP_READ && e.ptr == 1 && e.len == 2,
          "Sr idiom: read is the second segment");
    CHECK(e.data[0] == 0x60 && e.data[1] == 0x4B, "read bytes are recorded");
}

static void test_long_read_len(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    write_seg(&c, (const uint8_t[]){0x00}, 1);
    read_n(&c, 256);
    cb_trace_ev ev[2];
    cb_trace_ev e;
    CHECK(cb_core_drain(&c, &e) && e.op == CB_TRACE_OP_WRITE, "long read: pointer event");
    ev[0] = e;
    CHECK(cb_core_drain(&c, &e) && e.op == CB_TRACE_OP_READ, "long read event present");
    ev[1] = e;
    CHECK(ev[1].len == 256, "256-byte read keeps len=256 (not clamped)");
    CHECK(ev[1].trunc == 1, "256-byte read is marked truncated");
}

static void test_overflow(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    for (int i = 0; i < CB_TRACE_QUEUE + 1; i++) read_n(&c, 1);
    CHECK(c.dropped == 1, "ring overflow increments dropped");
    CHECK(c.emitted == CB_TRACE_QUEUE, "emitted counts stored events only");
    cb_core_seal(&c);
    CHECK(!cb_core_complete(&c), "overflow cannot be complete");
}

static void test_abort_and_zero(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    cb_core_bus_write(&c, 0x01); /* segment in progress, no FINISH */
    CHECK(cb_core_expire(&c), "expiry fires while armed");
    CHECK(c.aborted == 1, "in-flight transfer at expiry is counted aborted");
    CHECK(c.xfer_op == 0, "expiry resets transfer state");
    CHECK(!cb_core_complete(&c), "aborted lease is not complete");

    cb_core d;
    load_default(&d, 4);
    cb_core_arm(&d, 100);
    cb_core_seal(&d);
    CHECK(d.emitted == 0, "zero-event lease really has no events");
    CHECK(!cb_core_complete(&d), "zero-observation lease is not complete");
}

static void test_drain_cursor_and_pending(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    read_n(&c, 1);
    read_n(&c, 1);
    cb_core_seal(&c);
    cb_trace_ev e;
    CHECK(cb_core_drain(&c, &e) && cb_core_drain(&c, &e), "two events drained");
    CHECK(c.drained == 2 && c.emitted == 2, "drain cursor matches emitted");
    CHECK(cb_core_complete(&c), "sealed, drained, no loss => complete");

    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 4, 0, TAG);
    CHECK(cb_core_load(&c, &cmd) == CB_OK, "LOAD after fully drained epoch accepted");

    cb_core_arm(&c, 100);
    read_n(&c, 1);
    cb_core_seal(&c);
    CHECK(cb_core_load(&c, &cmd) == CB_ERR_TRACE_PENDING,
          "LOAD with undrained events is refused");
}

static void test_streaming_while_armed(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    read_n(&c, 1);
    cb_trace_ev e;
    CHECK(cb_core_drain(&c, &e), "drain is allowed while armed");
    CHECK(!c.sealed && c.armed, "streaming drain does not seal the lease");
    CHECK(c.drained == 1, "streaming advances the drained cursor");
    CHECK(!cb_core_complete(&c), "streaming drain is not a complete campaign");
}

static void test_wmask(void) {
    cb_core c;
    load_default(&c, 3);
    cb_core_arm(&c, 100);
    uint8_t seg[2] = {0x00, 0x55}; /* pointer 0, data to read-only reg 0 */
    write_seg(&c, seg, 2);
    CHECK(c.image[0] == 0x19, "write outside wmask is ignored");
    cb_trace_ev e;
    CHECK(cb_core_drain(&c, &e), "wmask case recorded a segment");
    CHECK(e.op == CB_TRACE_OP_WRITE && e.len == 1 && e.data[0] == 0x55,
          "wmask does not suppress the received-byte trace");
}

/* B2: the core supplies one byte per RD_REQ with a per-byte wrap. These cases
 * pin the byte model the Python simulator must reproduce. */
static void test_read_wrap_bytes(void) {
    cb_core c;
    load_default(&c, 3); /* image 19 60 4b 50, auto-inc, wrap 4 */
    CHECK(cb_core_arm(&c, 100) == CB_OK, "arm for per-byte wrap read");
    uint8_t ptr = 0x02;
    write_seg(&c, &ptr, 1);
    uint8_t out[6];
    read_seg(&c, 6, out);
    const uint8_t expect6[6] = {0x4B, 0x50, 0x19, 0x60, 0x4B, 0x50};
    CHECK(memcmp(out, expect6, 6) == 0,
          "read across pointer_wrap wraps per byte (2,3,0,1,2,3)");
    CHECK(c.pointer == 0, "pointer after read 2+6 wraps at 4");

    cb_core w1;
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 1, 0, TAG);
    cb_core_init(&w1, 4);
    CHECK(cb_core_load(&w1, &cmd) == CB_OK, "load wrap=1");
    CHECK(cb_core_arm(&w1, 100) == CB_OK, "arm wrap=1");
    uint8_t out3[3];
    read_seg(&w1, 3, out3);
    CHECK(out3[0] == 0x19 && out3[1] == 0x19 && out3[2] == 0x19,
          "wrap=1 repeats the same register for every requested byte");

    cb_core w256;
    make_image(image);
    image[255] = 0xEE;
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 256, 255, TAG);
    cb_core_init(&w256, 5);
    CHECK(cb_core_load(&w256, &cmd) == CB_OK, "load wrap=256 initial 255");
    CHECK(cb_core_arm(&w256, 100) == CB_OK, "arm wrap=256 initial 255");
    uint8_t out2[2];
    read_seg(&w256, 2, out2);
    CHECK(out2[0] == 0xEE && out2[1] == 0x19,
          "wrap=256 read crosses 255 -> 0 without leaving the image");
    CHECK(w256.pointer == 1, "wrap=256 pointer advances past 255");

    cb_core stat;
    make_load(&cmd, image, wmask, 0 /* no auto-inc */, 4, 2, TAG);
    cb_core_init(&stat, 6);
    CHECK(cb_core_load(&stat, &cmd) == CB_OK, "load static read policy");
    CHECK(cb_core_arm(&stat, 100) == CB_OK, "arm static read policy");
    uint8_t flat[3];
    read_seg(&stat, 3, flat);
    CHECK(flat[0] == flat[1] && flat[1] == flat[2] && flat[0] == image[2],
          "auto_increment=false repeats the pointer register");
    CHECK(stat.pointer == 2, "auto_increment=false does not advance");

    cb_core full;
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 256, 0, TAG);
    cb_core_init(&full, 7);
    CHECK(cb_core_load(&full, &cmd) == CB_OK, "load 256-byte read case");
    CHECK(cb_core_arm(&full, 100) == CB_OK, "arm 256-byte read case");
    {
        uint8_t all[256];
        read_seg(&full, 256, all);
        CHECK(memcmp(all, image, 256) == 0,
              "256-byte read returns the full image in order");
        CHECK(full.pointer == 0, "256-byte read wraps pointer back to 0");
    }
}

/* B1: a late alarm callback for an older epoch must not seal a newer lease. */
static void test_expire_epoch_guard(void) {
    cb_core c;
    load_default(&c, 5);
    CHECK(cb_core_arm(&c, 100) == CB_OK, "arm for epoch guard");
    uint32_t e1 = c.epoch;
    CHECK(!cb_core_expire_epoch(&c, e1 - 1),
          "stale alarm epoch is rejected");
    CHECK(c.armed && !c.sealed, "stale alarm leaves the lease armed");
    CHECK(cb_core_expire_epoch(&c, e1), "matching alarm epoch expires the lease");
    CHECK(!c.armed && c.sealed && c.expired, "matching expiry seals the epoch");
    CHECK(cb_core_arm(&c, 100) == CB_OK, "re-arm after expiry");
    uint32_t e2 = c.epoch;
    CHECK(e2 != e1, "re-arm starts a distinct epoch");
    CHECK(!cb_core_expire_epoch(&c, e1),
          "late callback from the previous lease is ignored");
    CHECK(c.armed && !c.sealed, "new lease survives the stale callback");
    CHECK(c.epoch == e2, "stale callback did not mutate the epoch");
}

/* Deterministic transcript compared with the Python simulator by
 * tests/test_core_parity.py. */
static void print_transcript(void) {
    uint8_t image[CB_REG_SPACE], wmask[CB_WMASK_BYTES];
    cb_cmd cmd;
    cb_core c;
    make_image(image);
    make_wmask(wmask);
    make_load(&cmd, image, wmask, CB_LOAD_FLAG_AUTO_INC, 4, 0, TAG);
    cb_core_init(&c, 0xBEEF);
    if (cb_core_load(&c, &cmd) != CB_OK) {
        printf("FAIL load\n");
        return;
    }
    if (cb_core_arm(&c, 1000) != CB_OK) {
        printf("FAIL arm\n");
        return;
    }
    printf("C %08x %08x\n", (unsigned)c.cfg, (unsigned)c.epoch);
    write_seg(&c, (const uint8_t[]){0x00}, 1);
    read_n(&c, 1);
    write_seg(&c, (const uint8_t[]){0x01}, 1);
    read_n(&c, 2);
    write_seg(&c, (const uint8_t[]){0x02, 0xAA}, 2);
    read_n(&c, 1);
    /* Cross-wrap read: pointer 2 under wrap 4 must wrap per byte. */
    write_seg(&c, (const uint8_t[]){0x02}, 1);
    read_n(&c, 6);
    cb_core_seal(&c);

    cb_trace_ev e;
    while (cb_core_drain(&c, &e)) {
        char hex[CB_TRACE_DATA * 2 + 1];
        static const char *digits = "0123456789abcdef";
        uint8_t n = (uint8_t)(e.len > CB_TRACE_DATA ? CB_TRACE_DATA : e.len);
        for (uint8_t i = 0; i < n; i++) {
            hex[i * 2] = digits[e.data[i] >> 4];
            hex[i * 2 + 1] = digits[e.data[i] & 0x0F];
        }
        hex[n * 2] = '\0';
        printf("E %08x %u %c %u %u %u %s\n", (unsigned)e.epoch, (unsigned)e.seq,
               e.op == CB_TRACE_OP_READ ? 'r' : 'w', (unsigned)e.ptr,
               (unsigned)e.len, (unsigned)e.trunc, hex);
    }
    printf("D %u %u %u %u complete=%u\n", (unsigned)c.emitted,
           (unsigned)c.dropped, (unsigned)c.aborted, (unsigned)c.drained,
           cb_core_complete(&c) ? 1u : 0u);
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--transcript") == 0) {
        print_transcript();
        return 0;
    }

    CHECK(cb_crc32((const uint8_t *)"123456789", 9) == 0xCBF43926u,
          "crc32 matches the standard check value");
    test_epochs();
    test_config_binding();
    test_arm_restores_template();
    test_pointer_and_wrap();
    test_pointer_init_rejected();
    test_sr_read_idiom();
    test_long_read_len();
    test_overflow();
    test_abort_and_zero();
    test_drain_cursor_and_pending();
    test_streaming_while_armed();
    test_wmask();
    test_read_wrap_bytes();
    test_expire_epoch_guard();

    printf("---\n%d cases, %d failure(s)\n", cases, failures);
    return failures == 0 ? 0 : 1;
}
